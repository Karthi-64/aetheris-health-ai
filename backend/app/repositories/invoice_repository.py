"""Repository for the invoice aggregate.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it, including lines and payments (CLAUDE.md rules 4 and 5).

Covers ``invoices``, ``invoice_items``, ``payments`` and ``refunds`` because
lines, payments and refunds are never addressed except through their invoice — they are one
consistency boundary. A payment and the ``amount_paid`` it changes must be
written together, under one lock.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, select

from app.models.appointment import Appointment
from app.models.billing import (
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    Payment,
    PaymentMethod,
    Refund,
)
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession


class InvoiceRepository(BaseRepository[Invoice]):
    """Persistence for invoices, their lines, and their payments.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Invoice, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _scoped(self, hospital_id: uuid.UUID) -> Select[tuple[Invoice]]:
        """Return a base SELECT filtered to one hospital."""
        return self._query().where(Invoice.hospital_id == hospital_id)

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[Invoice]],
        *,
        patient_id: uuid.UUID | None = None,
        appointment_id: uuid.UUID | None = None,
        status: InvoiceStatus | None = None,
        issued_on_or_after: datetime | None = None,
        issued_before: datetime | None = None,
        doctor_id: uuid.UUID | None = None,
        discount_pending: bool | None = None,
    ) -> Select[tuple[Invoice]]:
        """Apply the filters shared by list and count (module spec §9).

        The date range arrives as a half-open UTC interval on ``issued_at``.
        Drafts have no ``issued_at`` and therefore never match a date filter,
        which is correct: a draft was not issued on any day.

        :param stmt: The statement to extend.
        :param patient_id: Exact patient filter.
        :param appointment_id: Exact appointment filter.
        :param status: Exact status filter.
        :param issued_on_or_after: Lower bound on ``issued_at``.
        :param issued_before: Upper bound on ``issued_at``, exclusive.
        :param doctor_id: Only invoices for appointments with this doctor.
            Invoices not tied to an appointment never match.
        :param discount_pending: Filter on whether a discount awaits approval —
            ``True`` is the admin's approval queue. ``None`` returns both.
        :returns: The statement with predicates applied.
        """
        if patient_id is not None:
            stmt = stmt.where(Invoice.patient_id == patient_id)
        if appointment_id is not None:
            stmt = stmt.where(Invoice.appointment_id == appointment_id)
        if status is not None:
            stmt = stmt.where(Invoice.status == status)
        if issued_on_or_after is not None:
            stmt = stmt.where(Invoice.issued_at >= issued_on_or_after)
        if issued_before is not None:
            stmt = stmt.where(Invoice.issued_at < issued_before)
        if discount_pending is not None:
            stmt = stmt.where(Invoice.discount_pending_approval.is_(discount_pending))
        if doctor_id is not None:
            stmt = stmt.where(
                Invoice.appointment_id.in_(
                    # Tenant-matched as well as doctor-matched, so the scope
                    # cannot reach an appointment in another hospital.
                    select(Appointment.id).where(
                        Appointment.doctor_id == doctor_id,
                        Appointment.hospital_id == Invoice.hospital_id,
                    )
                )
            )
        return stmt

    @staticmethod
    def _ordered(stmt: Select[tuple[Invoice]]) -> Select[tuple[Invoice]]:
        """Order newest first, with ``id`` as a stable tiebreaker.

        By ``created_at`` rather than ``issued_at`` so drafts, which have no
        issue date, sort alongside everything else instead of all at one end.
        """
        return stmt.order_by(Invoice.created_at.desc(), Invoice.id.desc())

    @staticmethod
    def _build_items(hospital_id: uuid.UUID, lines: Sequence[dict[str, Any]]) -> list[InvoiceItem]:
        """Build line rows, numbering them in the order given.

        :param hospital_id: Owning tenant, stamped on every line.
        :param lines: Column values per line, already priced by the service.
        :returns: Unattached ``InvoiceItem`` instances.
        """
        return [
            InvoiceItem(hospital_id=hospital_id, position=position, **line)
            for position, line in enumerate(lines)
        ]

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_invoice(
        self,
        *,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        lines: Sequence[dict[str, Any]],
        subtotal: Decimal,
        tax_amount: Decimal,
        total: Decimal,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> Invoice:
        """Insert a draft invoice together with its lines.

        Does not commit — the service owns the transaction.

        :param hospital_id: Owning tenant.
        :param patient_id: Patient being billed.
        :param lines: Column values per line, already priced.
        :param subtotal: Sum of line amounts before tax.
        :param tax_amount: Sum of line tax.
        :param total: Invoice total.
        :param created_by: UUID of the acting user. ``None`` when the system
            drafted it from a completed appointment with no actor.
        :param optional_fields: Remaining columns (appointment_id, notes).
        :returns: The persisted draft, lines loaded.
        """
        invoice = Invoice(
            hospital_id=hospital_id,
            patient_id=patient_id,
            status=InvoiceStatus.DRAFT,
            subtotal=subtotal,
            tax_amount=tax_amount,
            total=total,
            created_by=created_by,
            items=self._build_items(hospital_id, lines),
            **optional_fields,
        )
        self._session.add(invoice)
        await self._session.flush()
        await self._session.refresh(invoice)
        return invoice

    async def replace_items(self, invoice: Invoice, lines: Sequence[dict[str, Any]]) -> None:
        """Swap an invoice's whole line set.

        The old rows are deleted by the relationship's ``delete-orphan``
        cascade. Flushes but does not refresh; the caller follows with
        :meth:`update_invoice`, which does.

        :param invoice: The attached draft to modify.
        :param lines: Column values per replacement line, already priced.
        """
        invoice.items = self._build_items(invoice.hospital_id, lines)
        await self._session.flush()

    async def append_items(self, invoice: Invoice, lines: Sequence[dict[str, Any]]) -> None:
        """Add lines to the end of a draft, leaving the existing ones alone.

        Used when another module charges something to an open draft. Flushes
        but does not refresh; the caller follows with :meth:`update_invoice`.

        :param invoice: The attached draft to modify.
        :param lines: Column values per new line, already priced.
        """
        start = max((item.position for item in invoice.items), default=-1) + 1
        for offset, line in enumerate(lines):
            invoice.items.append(
                InvoiceItem(hospital_id=invoice.hospital_id, position=start + offset, **line)
            )
        await self._session.flush()

    async def update_invoice(
        self, invoice: Invoice, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> Invoice:
        """Apply field updates to an existing invoice.

        :param invoice: The attached ORM instance to modify.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated invoice.
        """
        return await self.update(invoice, updated_by=updated_by, **fields)

    async def create_payment(
        self,
        *,
        invoice: Invoice,
        amount: Decimal,
        method: PaymentMethod,
        received_by: uuid.UUID,
        received_at: datetime,
        idempotency_key: str,
        reference: str | None = None,
        notes: str | None = None,
    ) -> Payment:
        """Insert a payment row against an invoice.

        Deliberately does *not* touch ``invoice.amount_paid`` or its status —
        the service does that, so the row and the balance it changes are
        visibly paired at the call site.

        :param invoice: The invoice being paid. Supplies the tenant.
        :param amount: Amount received.
        :param method: How it was paid.
        :param received_by: User recording the payment.
        :param received_at: When it was recorded (UTC).
        :param idempotency_key: Client-supplied key (business rule 6).
        :param reference: Transaction id, cheque number, or UPI reference.
        :param notes: Free-form notes.
        :returns: The persisted payment.
        """
        payment = Payment(
            hospital_id=invoice.hospital_id,
            invoice_id=invoice.id,
            amount=amount,
            method=method,
            reference=reference,
            notes=notes,
            received_by=received_by,
            received_at=received_at,
            idempotency_key=idempotency_key,
            created_by=received_by,
        )
        self._session.add(payment)
        await self._session.flush()
        await self._session.refresh(payment)
        return payment

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_invoice_by_id(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        *,
        doctor_id: uuid.UUID | None = None,
    ) -> Invoice | None:
        """Retrieve one invoice by UUID within a hospital.

        :param hospital_id: The tenant to scope to.
        :param invoice_id: The invoice UUID.
        :param doctor_id: When given, the invoice is returned only if it bills
            an appointment with this doctor.
        :returns: The invoice, or ``None`` if absent, in another tenant, or
            outside the doctor's visits.
        """
        stmt = self._apply_filters(
            self._scoped(hospital_id).where(Invoice.id == invoice_id), doctor_id=doctor_id
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_invoice_for_update(
        self, hospital_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Invoice | None:
        """Retrieve one invoice and lock its row until the transaction ends.

        Every state change and every payment goes through this, so two requests
        acting on one invoice run one after the other rather than both reading
        the same balance and both writing (module spec §14).

        ``of=Invoice`` because the joined ``patient`` load makes this an outer
        join, and Postgres refuses a bare ``FOR UPDATE`` across one.
        ``populate_existing`` because the row may already be in the session
        from an earlier read in the same request; without it the lock would be
        taken but the stale, pre-lock values kept.

        The caller **must** be inside a transaction.

        :param hospital_id: The tenant to scope to.
        :param invoice_id: The invoice UUID.
        :returns: The locked invoice, or ``None`` if absent or in another tenant.
        """
        stmt = (
            self._scoped(hospital_id)
            .where(Invoice.id == invoice_id)
            .with_for_update(of=Invoice)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_live_invoice_for_appointment(
        self, hospital_id: uuid.UUID, appointment_id: uuid.UUID
    ) -> Invoice | None:
        """Find the invoice that currently bills an appointment, if any.

        "Live" means not void — matching the ``uq_invoices_live_appointment``
        index, so this answers exactly the question that index enforces.

        :param hospital_id: The tenant to scope to.
        :param appointment_id: The appointment UUID.
        :returns: The appointment's non-void invoice, or ``None``.
        """
        stmt = self._scoped(hospital_id).where(
            Invoice.appointment_id == appointment_id,
            Invoice.status != InvoiceStatus.VOID,
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_invoices(
        self,
        hospital_id: uuid.UUID,
        *,
        skip: int = 0,
        limit: int = 25,
        **filters: Any,
    ) -> list[Invoice]:
        """List invoices in a hospital, newest first.

        :param hospital_id: The tenant to scope to.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        :returns: A page of invoices.
        """
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        stmt = self._apply_pagination(self._ordered(stmt), skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_invoices(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count invoices matching the filters :meth:`list_invoices` uses.

        :param hospital_id: The tenant to scope to.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        :returns: The number of matching invoices.
        """
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one()

    async def get_payment_by_idempotency_key(
        self, hospital_id: uuid.UUID, idempotency_key: str
    ) -> Payment | None:
        """Find a payment already recorded under this key (business rule 6).

        Not filtered on ``deleted_at``: the unique index is not either, so a
        key that looked free here but was taken there would surface as a 500
        rather than a replayed response.

        :param hospital_id: The tenant to scope to.
        :param idempotency_key: The client-supplied key.
        :returns: The original payment, or ``None``.
        """
        stmt = select(Payment).where(
            Payment.hospital_id == hospital_id,
            Payment.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_payments(self, hospital_id: uuid.UUID, invoice_id: uuid.UUID) -> list[Payment]:
        """Return an invoice's payments, oldest first.

        :param hospital_id: The tenant to scope to.
        :param invoice_id: The invoice whose payments to read.
        :returns: Payments in the order they were recorded.
        """
        stmt = (
            select(Payment)
            .where(
                Payment.hospital_id == hospital_id,
                Payment.invoice_id == invoice_id,
                Payment.deleted_at.is_(None),
            )
            .order_by(Payment.received_at.asc(), Payment.id.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def create_refund(
        self,
        *,
        invoice: Invoice,
        amount: Decimal,
        method: PaymentMethod,
        reason: str,
        refunded_by: uuid.UUID,
        refunded_at: datetime,
        idempotency_key: str,
        reference: str | None = None,
    ) -> Refund:
        """Insert a refund row against an invoice.

        Like :meth:`create_payment`, this does *not* touch the invoice's
        ``amount_refunded`` or status — the service does, so the row and the
        figures it changes are visibly paired at the call site.

        :param invoice: The invoice being refunded. Supplies the tenant.
        :param amount: Amount given back.
        :param method: How the money was returned.
        :param reason: Why the refund was given.
        :param refunded_by: User issuing the refund.
        :param refunded_at: When it was issued (UTC).
        :param idempotency_key: Client-supplied key.
        :param reference: Transaction id or other reference.
        :returns: The persisted refund.
        """
        refund = Refund(
            hospital_id=invoice.hospital_id,
            invoice_id=invoice.id,
            amount=amount,
            method=method,
            reason=reason,
            reference=reference,
            refunded_by=refunded_by,
            refunded_at=refunded_at,
            idempotency_key=idempotency_key,
            created_by=refunded_by,
        )
        self._session.add(refund)
        await self._session.flush()
        await self._session.refresh(refund)
        return refund

    async def get_refund_by_idempotency_key(
        self, hospital_id: uuid.UUID, idempotency_key: str
    ) -> Refund | None:
        """Find a refund already issued under this key.

        Not filtered on ``deleted_at``, for the same reason as
        :meth:`get_payment_by_idempotency_key`: the unique index is not.

        :param hospital_id: The tenant to scope to.
        :param idempotency_key: The client-supplied key.
        :returns: The original refund, or ``None``.
        """
        stmt = select(Refund).where(
            Refund.hospital_id == hospital_id,
            Refund.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_refunds(self, hospital_id: uuid.UUID, invoice_id: uuid.UUID) -> list[Refund]:
        """Return an invoice's refunds, oldest first.

        :param hospital_id: The tenant to scope to.
        :param invoice_id: The invoice whose refunds to read.
        :returns: Refunds in the order they were issued.
        """
        stmt = (
            select(Refund)
            .where(
                Refund.hospital_id == hospital_id,
                Refund.invoice_id == invoice_id,
                Refund.deleted_at.is_(None),
            )
            .order_by(Refund.refunded_at.asc(), Refund.id.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())
