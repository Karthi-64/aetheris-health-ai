"""Business logic for invoices and payments.

Owns the invoice lifecycle (``docs/modules/06-billing.md`` §5), the money rules
(§4), the transaction boundary, and an audit record per mutation (CLAUDE.md
rule 9). Returns DTOs, never ORM models.

**Financial truth is computed here and nowhere else.** Line totals and invoice
totals come from :mod:`app.utils.invoice_math`; no request carries a total, and
no client-supplied figure reaches a stored amount (business rule 7).

**Every change to an invoice starts by locking its row.** Issue, void, edit and
payment all go through
:meth:`~app.repositories.invoice_repository.InvoiceRepository.get_invoice_for_update`,
so two requests acting on one invoice run one after the other. That is what
stops two payments from both reading the same balance and together overpaying
(business rule 9), and two tabs from both issuing the same draft (§14).

**Locks are always taken in the same order** — the invoice, then the hospital's
number counter — so two issues can never wait on each other.

**The number is allocated inside the issuing transaction.** If the issue fails
for any reason, the counter increment rolls back with it and the next issue
reuses the number. That is what makes the series gap-free rather than merely
unique (business rule 2, AC-2).

**Two role rules narrow what a caller may do** (spec §3), and both are
enforced here rather than in the routes:

- *Own visits.* A caller holding only ``invoice.read.own`` — a doctor — sees
  the invoices for appointments where they are the doctor, and nothing else.
  An invoice outside that set is reported as not found, exactly like one in
  another hospital, so the scope does not reveal what it hides.
- *Cash only.* A caller holding only ``invoice.payment.record.cash`` — a
  receptionist — may record cash and no other method.

The routes decide *which* rule applies, from the caller's permissions; the
service applies it.

**Discounts are checked against a threshold the hospital sets** (§5.2,
business rule 5). A discount above it marks the draft as awaiting approval,
and the draft cannot be issued until an admin approves it (AC-4). Changing the
discount or the lines afterwards withdraws an approval already given: what was
approved was a specific discount on a specific bill.

**Refunds never rewrite what was received** (§5.5). ``amount_paid`` stays as
it was and ``amount_refunded`` grows beside it, capped by the database at
``amount_paid`` (business rule 10). A full refund closes the invoice as
``refunded``; a partial one leaves its status alone.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent
from app.core.config import settings
from app.core.exceptions import (
    BusinessRuleError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.notifications import NotificationRequest, Notifier, NullNotifier
from app.models.billing import (
    PAYABLE_STATUSES,
    REFUNDABLE_STATUSES,
    InvoiceStatus,
    PaymentMethod,
)
from app.schemas.billing import (
    CreateInvoiceRequest,
    InvoiceLineRequest,
    InvoiceResponse,
    InvoiceSummaryResponse,
    PaymentRecordedResponse,
    PaymentResponse,
    RecordPaymentRequest,
    RecordRefundRequest,
    RefundRecordedResponse,
    RefundResponse,
    UpdateInvoiceRequest,
    VoidInvoiceRequest,
)
from app.schemas.common import Page, PaginationParams
from app.utils.invoice_math import ZERO, compute_invoice_totals, compute_line
from app.utils.mrn import InvalidMrnTemplateError, format_mrn

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.core.charges import Charge
    from app.models.billing import Invoice, Payment, Refund
    from app.repositories.appointment_repository import AppointmentRepository
    from app.repositories.doctor_repository import DoctorRepository
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.invoice_number_sequence_repository import (
        InvoiceNumberSequenceRepository,
    )
    from app.repositories.invoice_repository import InvoiceRepository
    from app.repositories.patient_repository import PatientRepository
    from app.repositories.service_catalog_repository import ServiceCatalogRepository
    from app.utils.invoice_math import InvoiceTotals, LineAmounts

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_CURRENCY",
    "DEFAULT_DISCOUNT_APPROVAL_THRESHOLD_PERCENT",
    "DEFAULT_TAX_RATE",
    "BillingInvoiceDraftSink",
    "BillingService",
    "CashOnlyPaymentError",
    "DiscountAwaitingApprovalError",
    "DuplicateAppointmentInvoiceError",
    "IdempotencyKeyReuseError",
    "InvalidInvoiceStateError",
    "InvoiceNotFoundError",
    "NoDiscountToApproveError",
    "OverpaymentError",
    "RefundExceedsPaidError",
]

#: Used when a hospital row cannot be read. Matches the column default.
DEFAULT_CURRENCY = "INR"

#: Tax applied to taxable lines when the hospital has not configured a rate.
#: Zero, so a hospital that has set nothing up charges no tax rather than a
#: guessed one. Overridable via ``hospitals.settings["billing"]["default_tax_rate"]``.
DEFAULT_TAX_RATE = Decimal("0.00")

#: Share of the subtotal a discount may reach before an admin must approve it,
#: when the hospital has not configured one. Zero: until a hospital opts into
#: a threshold, *every* discount needs approval. Overridable via
#: ``hospitals.settings["billing"]["discount_approval_threshold_percent"]``.
DEFAULT_DISCOUNT_APPROVAL_THRESHOLD_PERCENT = Decimal("0.00")

_MAX_TAX_RATE = Decimal(100)
_HUNDRED = Decimal(100)

#: ``invoice_items.description`` is ``VARCHAR(200)``.
_DESCRIPTION_MAX_LENGTH = 200


# ── Module exceptions ───────────────────────────────────────────────────────


class InvoiceNotFoundError(NotFoundError):
    """Raised when an invoice is absent from the requested hospital.

    Also raised for one in another tenant: a cross-tenant lookup must be
    indistinguishable from a miss.
    """

    def __init__(self, invoice_id: uuid.UUID) -> None:
        super().__init__(message="Invoice not found.", detail={"invoice_id": str(invoice_id)})


class InvalidInvoiceStateError(BusinessRuleError):
    """Raised when an action is not allowed in the invoice's current status.

    400 rather than 409, matching how appointments report an illegal
    transition: the request is wrong against the lifecycle, not in conflict
    with another write.
    """

    def __init__(self, action: str, current: InvoiceStatus, *, hint: str | None = None) -> None:
        message = f"Cannot {action} an invoice that is '{current.value}'."
        if hint:
            message = f"{message} {hint}"
        super().__init__(
            message=message, detail={"action": action, "current_status": current.value}
        )


class OverpaymentError(BusinessRuleError):
    """Raised when a payment would take ``amount_paid`` past ``total`` (rule 9)."""

    def __init__(self, amount: Decimal, balance_due: Decimal) -> None:
        super().__init__(
            message=(
                f"Payment of {amount} exceeds the balance due of {balance_due}. "
                "Record at most the balance."
            ),
            detail={"amount": str(amount), "balance_due": str(balance_due)},
        )


class CashOnlyPaymentError(PermissionDeniedError):
    """Raised when a cash-only caller records a non-cash payment (spec §3).

    403 rather than 400: the request is well-formed and would succeed for
    someone holding ``invoice.payment.record``. What is missing is permission.
    """

    def __init__(self, method: PaymentMethod) -> None:
        super().__init__(
            message=(
                f"You may record cash payments only, not '{method.value}'. "
                "Ask billing staff to record this payment."
            ),
            detail={"method": method.value, "allowed_methods": [PaymentMethod.CASH.value]},
        )


class DiscountAwaitingApprovalError(BusinessRuleError):
    """Raised when a draft is issued while its discount awaits approval (AC-4)."""

    def __init__(self, discount_amount: Decimal) -> None:
        super().__init__(
            message=(
                f"The discount of {discount_amount} is above the hospital's approval "
                "threshold and has not been approved. An admin must approve it before "
                "the invoice can be issued."
            ),
            detail={"discount_amount": str(discount_amount)},
        )


class NoDiscountToApproveError(ConflictError):
    """Raised when there is no discount awaiting approval on the invoice.

    409 because this is what the *second* of two admins approving at once
    sees (module spec §14): the first commit won, and the request now
    conflicts with the invoice's state.
    """

    def __init__(self) -> None:
        super().__init__(message="This invoice has no discount awaiting approval.")


class RefundExceedsPaidError(BusinessRuleError):
    """Raised when a refund would exceed what is left to give back (rule 10)."""

    def __init__(self, amount: Decimal, refundable: Decimal) -> None:
        super().__init__(
            message=(
                f"Refund of {amount} exceeds the {refundable} that can still be refunded "
                "on this invoice."
            ),
            detail={"amount": str(amount), "refundable_amount": str(refundable)},
        )


class IdempotencyKeyReuseError(ConflictError):
    """Raised when an ``Idempotency-Key`` is reused for a *different* request.

    A replay must be the same request. The same key arriving with another
    invoice, amount or method is a client bug, and answering it with the
    original payment or refund would tell the client its new one went through.
    """

    def __init__(self, what: str = "payment") -> None:
        super().__init__(
            message=(
                f"This Idempotency-Key was already used for a different {what}. "
                f"Generate a new key for a new {what}."
            )
        )


class DuplicateAppointmentInvoiceError(ConflictError):
    """Raised when an appointment already has a live (non-void) invoice."""

    def __init__(self, appointment_id: uuid.UUID, invoice_id: uuid.UUID | None = None) -> None:
        detail = {"appointment_id": str(appointment_id)}
        if invoice_id is not None:
            detail["invoice_id"] = str(invoice_id)
        super().__init__(
            message="This appointment already has an invoice. Edit or void that one instead.",
            detail=detail,
        )


def _field_error(field: str, message: str) -> ValidationError:
    """Build a 422 naming one offending field, in the standard error shape."""
    return ValidationError(
        message=message, detail={"errors": [{"field": field, "message": message}]}
    )


# ── Service ─────────────────────────────────────────────────────────────────


class BillingService:
    """Drafting, issuing, paying and voiding invoices.

    :param invoices: Invoice, line and payment data access.
    :param sequences: The per-hospital invoice-number counter.
    :param catalog: Services catalog lookups, for pricing lines.
    :param patients: Patient lookups, for validating who is billed.
    :param appointments: Appointment lookups, for the invoice-to-visit link.
    :param doctors: Doctor lookups, for scoping a doctor to their own visits.
    :param hospitals: Hospital lookups, for currency, tax rate and timezone.
    :param session: Request-scoped session, held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    :param notifier: Where approvers are told a discount is waiting. Optional;
        a service built without one notifies nobody.
    """

    def __init__(
        self,
        invoices: InvoiceRepository,
        sequences: InvoiceNumberSequenceRepository,
        catalog: ServiceCatalogRepository,
        patients: PatientRepository,
        appointments: AppointmentRepository,
        doctors: DoctorRepository,
        hospitals: HospitalRepository,
        session: AsyncSession,
        audit: AuditSink,
        notifier: Notifier | None = None,
    ) -> None:
        self._invoices = invoices
        self._sequences = sequences
        self._catalog = catalog
        self._patients = patients
        self._appointments = appointments
        self._doctors = doctors
        self._hospitals = hospitals
        self._session = session
        self._audit = audit
        self._notifier: Notifier = notifier or NullNotifier()

    # ── Drafting ──────────────────────────────────────────────────────────────

    async def create_invoice(
        self,
        hospital_id: uuid.UUID,
        payload: CreateInvoiceRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> InvoiceResponse:
        """Create a draft invoice (module spec §9, ``POST /invoices``).

        :param hospital_id: The hospital raising the invoice.
        :param payload: Validated creation data.
        :param actor_id: UUID of the acting user.
        :returns: The draft, with computed totals.
        :raises ValidationError: If the patient, appointment or a service is
            unknown in this hospital, or the appointment is another patient's.
        :raises DuplicateAppointmentInvoiceError: If the appointment already
            has a live invoice.
        """
        await self._assert_patient_valid(hospital_id, payload.patient_id)
        if payload.appointment_id is not None:
            await self._assert_appointment_billable(
                hospital_id, payload.appointment_id, payload.patient_id
            )

        currency, tax_rate, _ = await self._hospital_billing_context(hospital_id)
        lines, totals = await self._price_lines(hospital_id, payload.items, tax_rate)

        invoice = await self._insert_draft(
            hospital_id=hospital_id,
            patient_id=payload.patient_id,
            appointment_id=payload.appointment_id,
            notes=payload.notes,
            lines=lines,
            totals=totals,
            actor_id=actor_id,
        )
        await self._record_drafted(invoice, actor_id=actor_id, source="manual")
        await self._session.commit()

        return InvoiceResponse.from_model(invoice, currency=currency)

    async def draft_from_appointment(
        self,
        hospital_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> InvoiceResponse | None:
        """Draft an invoice for a completed appointment (module spec §5.1).

        One line: the doctor's consultation fee. The draft is then open for
        billing staff to add to before it is issued.

        Idempotent: an appointment that already has a live invoice gets no
        second one, whether that invoice was drafted here earlier or raised by
        hand. Returns ``None`` in that case, and when the appointment cannot be
        found.

        The exception is an invoice another module's charge raised while the
        consultation was still going on (:meth:`add_charges`): a lab test
        ordered or a medicine dispensed mid-visit. That invoice does not yet
        carry the fee, so the fee is added to it here, once. If it has been
        issued in the meantime its lines are frozen (AC-1) and the fee goes on
        a draft of its own.

        The consultation line is not taxed. Whether a hospital's consultations
        are taxable is not something the catalog records for a doctor's fee, so
        the line is drafted untaxed and left for billing staff to correct.

        :param hospital_id: The hospital the appointment belongs to.
        :param appointment_id: The appointment that completed.
        :param actor_id: UUID of the user who completed it.
        :returns: The new draft, or ``None`` if nothing was drafted.
        """
        appointment = await self._appointments.get_appointment_by_id(hospital_id, appointment_id)
        if appointment is None:
            logger.warning(
                "invoice.draft_skipped",
                hospital_id=str(hospital_id),
                appointment_id=str(appointment_id),
                reason="appointment_not_found",
            )
            return None

        existing = await self._invoices.get_live_invoice_for_appointment(
            hospital_id, appointment_id
        )
        owed: Invoice | None = None
        if existing is not None and existing.consultation_fee_pending:
            # Lock before trusting the marker: a second completion of the same
            # visit may be adding the fee right now.
            owed = await self._invoices.get_invoice_for_update(hospital_id, existing.id)
            if owed is not None and owed.status is InvoiceStatus.VOID:
                # Voided while we waited: the visit has no live invoice now.
                existing = owed = None
        if existing is not None and (owed is None or not owed.consultation_fee_pending):
            logger.info(
                "invoice.draft_skipped",
                hospital_id=str(hospital_id),
                appointment_id=str(appointment_id),
                invoice_id=str(existing.id),
                reason="already_invoiced",
            )
            return None

        doctor = appointment.doctor
        description = f"Consultation — Dr. {doctor.user.first_name} {doctor.user.last_name}"
        amounts = compute_line(doctor.consultation_fee, Decimal(1), ZERO)
        lines = [
            {
                "service_id": None,
                "description": description[:_DESCRIPTION_MAX_LENGTH],
                "quantity": Decimal(1),
                "unit_price": doctor.consultation_fee,
                "tax_rate": ZERO,
                "line_total": amounts.total,
            }
        ]
        totals = compute_invoice_totals([amounts])
        currency, _, _ = await self._hospital_billing_context(hospital_id)

        if owed is not None and owed.status is InvoiceStatus.DRAFT:
            await self._append_charges(owed, lines, [amounts], "appointment", actor_id)
            invoice = await self._invoices.update_invoice(
                owed, updated_by=actor_id, consultation_fee_pending=False
            )
            await self._session.commit()
            return InvoiceResponse.from_model(invoice, currency=currency)

        if owed is not None:
            # Issued before the visit ended. The fee is no longer owed to this
            # invoice, and only one live invoice may point at a visit.
            await self._invoices.update_invoice(
                owed, updated_by=actor_id, consultation_fee_pending=False
            )

        try:
            invoice = await self._insert_draft(
                hospital_id=hospital_id,
                patient_id=appointment.patient_id,
                appointment_id=appointment_id if owed is None else None,
                notes=None,
                lines=lines,
                totals=totals,
                actor_id=actor_id,
            )
        except DuplicateAppointmentInvoiceError:
            # Lost a race with a manual draft for the same visit. That draft is
            # the invoice; there is nothing left to do.
            logger.info(
                "invoice.draft_skipped",
                hospital_id=str(hospital_id),
                appointment_id=str(appointment_id),
                reason="already_invoiced_race",
            )
            return None

        await self._record_drafted(invoice, actor_id=actor_id, source="appointment")
        await self._session.commit()

        return InvoiceResponse.from_model(invoice, currency=currency)

    async def update_invoice(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        payload: UpdateInvoiceRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> InvoiceResponse:
        """Edit a draft invoice (module spec §5.2, ``PATCH /invoices/{id}``).

        ``items``, when sent, replaces the whole line set and the totals are
        recomputed. Lines are re-priced from the catalog as it stands now.

        ``discount_amount`` sets the invoice-level discount. It must not exceed
        the subtotal and needs a reason. If it is above the hospital's
        threshold the draft is marked as awaiting approval. Any change to the
        discount or to the lines withdraws an approval already given.

        :param hospital_id: The hospital the invoice belongs to.
        :param invoice_id: The invoice to edit.
        :param payload: Validated update data.
        :param actor_id: UUID of the acting user.
        :returns: The updated draft.
        :raises InvoiceNotFoundError: If absent from this tenant.
        :raises InvalidInvoiceStateError: If the invoice is no longer a draft
            (business rule 3, AC-1).
        :raises ValidationError: If a service is unknown or inactive, the
            discount exceeds the subtotal, or a discount has no reason.
        """
        invoice = await self._lock_or_raise(hospital_id, invoice_id)
        if not invoice.is_editable:
            raise InvalidInvoiceStateError(
                "edit",
                invoice.status,
                hint="An issued invoice cannot be changed; void it and raise a new one.",
            )

        currency, tax_rate, _ = await self._hospital_billing_context(hospital_id)
        sent = payload.model_fields_set
        fields: dict[str, Any] = {}
        changed: list[str] = []

        # ── Lines ────────────────────────────────────────────────────────
        new_lines: list[dict[str, Any]] | None = None
        if payload.items is not None:
            new_lines, line_totals = await self._price_lines(hospital_id, payload.items, tax_rate)
            subtotal, tax_amount = line_totals.subtotal, line_totals.tax_amount
            changed.append("items")
        else:
            subtotal, tax_amount = invoice.subtotal, invoice.tax_amount

        # ── Discount ─────────────────────────────────────────────────────
        discount = invoice.discount_amount
        if payload.discount_amount is not None:
            discount = payload.discount_amount
        reason = payload.discount_reason if "discount_reason" in sent else invoice.discount_reason

        if discount > subtotal:
            raise _field_error(
                "discount_amount",
                f"The discount of {discount} exceeds the subtotal of {subtotal}.",
            )
        if discount > ZERO and not reason:
            raise _field_error("discount_reason", "A discount needs a reason.")
        if discount == ZERO:
            reason = None

        discount_changed = discount != invoice.discount_amount
        if discount_changed:
            changed.append("discount_amount")
        if reason != invoice.discount_reason:
            changed.append("discount_reason")

        if discount_changed or new_lines is not None:
            # What an admin approved was this discount on these lines. Either
            # changing means the question is asked again from scratch.
            threshold = await self._discount_threshold_percent(hospital_id)
            fields["discount_pending_approval"] = discount > subtotal * threshold / _HUNDRED
            fields["discount_approved_by"] = None

        fields.update(
            subtotal=subtotal,
            tax_amount=tax_amount,
            discount_amount=discount,
            discount_reason=reason,
            total=subtotal + tax_amount - discount,
        )
        if "notes" in sent:
            fields["notes"] = payload.notes
            changed.append("notes")

        # Tell the approvers when a discount newly needs them: it has just
        # become pending, or it was pending and the amount has changed.
        needs_approval = fields.get("discount_pending_approval") is True and (
            discount_changed or not invoice.discount_pending_approval
        )

        # Validation is done; only now is anything written.
        if new_lines is not None:
            await self._invoices.replace_items(invoice, new_lines)
        invoice = await self._invoices.update_invoice(invoice, updated_by=actor_id, **fields)

        if needs_approval:
            await self._notifier.notify(
                NotificationRequest(
                    kind="billing.discount_approval_requested",
                    hospital_id=hospital_id,
                    recipient_permission="invoice.approve_discount",
                    variables={
                        "patient_name": invoice.patient.full_name,
                        "discount_amount": f"{currency} {invoice.discount_amount}",
                        "action_url": f"{settings.FRONTEND_BASE_URL.rstrip('/')}/billing",
                    },
                    link="/billing",
                    actor_id=actor_id,
                )
            )

        await self._audit.record(
            AuditEvent(
                action="invoice.updated",
                hospital_id=hospital_id,
                target_type="invoice",
                target_id=invoice.id,
                actor_id=actor_id,
                context={
                    "changed": changed,
                    "total": str(invoice.total),
                    "discount_amount": str(invoice.discount_amount),
                    "discount_pending_approval": invoice.discount_pending_approval,
                },
            )
        )
        await self._session.commit()

        logger.info(
            "invoice.updated",
            hospital_id=str(hospital_id),
            invoice_id=str(invoice.id),
            changed=changed,
        )
        return InvoiceResponse.from_model(invoice, currency=currency)

    async def approve_discount(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
    ) -> InvoiceResponse:
        """Approve a draft's above-threshold discount (module spec §5.2).

        The row lock is what settles two admins approving at once (§14): the
        second waits, then finds nothing left to approve and gets a 409.

        :param hospital_id: The hospital the invoice belongs to.
        :param invoice_id: The draft whose discount to approve.
        :param actor_id: UUID of the approving admin.
        :returns: The draft, now free to be issued.
        :raises InvoiceNotFoundError: If absent from this tenant.
        :raises InvalidInvoiceStateError: If it is no longer a draft.
        :raises NoDiscountToApproveError: If no discount is awaiting approval.
        """
        invoice = await self._lock_or_raise(hospital_id, invoice_id)
        if invoice.status != InvoiceStatus.DRAFT:
            raise InvalidInvoiceStateError("approve a discount on", invoice.status)
        if not invoice.discount_pending_approval:
            raise NoDiscountToApproveError

        invoice = await self._invoices.update_invoice(
            invoice,
            updated_by=actor_id,
            discount_pending_approval=False,
            discount_approved_by=actor_id,
        )

        await self._audit.record(
            AuditEvent(
                action="invoice.discount_approved",
                hospital_id=hospital_id,
                target_type="invoice",
                target_id=invoice.id,
                actor_id=actor_id,
                context={
                    "discount_amount": str(invoice.discount_amount),
                    "subtotal": str(invoice.subtotal),
                    "reason": invoice.discount_reason,
                },
            )
        )
        await self._session.commit()

        logger.info(
            "invoice.discount_approved",
            hospital_id=str(hospital_id),
            invoice_id=str(invoice.id),
        )
        currency, _, _ = await self._hospital_billing_context(hospital_id)
        return InvoiceResponse.from_model(invoice, currency=currency)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def issue_invoice(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> InvoiceResponse:
        """Issue a draft: freeze its totals and give it a number (spec §5.3).

        A zero-total invoice becomes ``paid`` at once — there is nothing to
        collect, and business rule 11 says ``amount_paid >= total`` means paid.

        :param hospital_id: The hospital the invoice belongs to.
        :param invoice_id: The draft to issue.
        :param actor_id: UUID of the acting user.
        :returns: The issued invoice.
        :raises InvoiceNotFoundError: If absent from this tenant.
        :raises InvalidInvoiceStateError: If it is not a draft.
        :raises BusinessRuleError: If it has no lines.
        :raises DiscountAwaitingApprovalError: If its discount is above the
            threshold and no admin has approved it (AC-4).
        :raises ConfigurationError: If the hospital's number template is invalid.
        """
        invoice = await self._lock_or_raise(hospital_id, invoice_id)
        if invoice.status != InvoiceStatus.DRAFT:
            raise InvalidInvoiceStateError("issue", invoice.status)
        if not invoice.items:
            raise BusinessRuleError(
                message="An invoice needs at least one line before it can be issued.",
                detail={"invoice_id": str(invoice_id)},
            )

        if invoice.discount_pending_approval:
            raise DiscountAwaitingApprovalError(invoice.discount_amount)

        # Recompute from the stored lines rather than trusting the stored
        # totals: these are the figures that are about to be frozen.
        amounts = []
        for item in invoice.items:
            line = compute_line(item.unit_price, item.quantity, item.tax_rate)
            item.line_total = line.total
            amounts.append(line)
        totals = compute_invoice_totals(amounts, discount_amount=invoice.discount_amount)

        currency, _, zone = await self._hospital_billing_context(hospital_id)
        now = datetime.now(UTC)
        number = await self._next_invoice_number(hospital_id, issued_at=now, zone=zone)
        status = InvoiceStatus.PAID if totals.total == ZERO else InvoiceStatus.ISSUED

        invoice = await self._invoices.update_invoice(
            invoice,
            updated_by=actor_id,
            invoice_number=number,
            subtotal=totals.subtotal,
            tax_amount=totals.tax_amount,
            total=totals.total,
            status=status,
            issued_at=now,
        )

        await self._audit.record(
            AuditEvent(
                action="invoice.issued",
                hospital_id=hospital_id,
                target_type="invoice",
                target_id=invoice.id,
                actor_id=actor_id,
                changes={"status": {"before": InvoiceStatus.DRAFT.value, "after": status.value}},
                context={"invoice_number": number, "total": str(invoice.total)},
            )
        )
        await self._session.commit()

        logger.info(
            "invoice.issued",
            hospital_id=str(hospital_id),
            invoice_id=str(invoice.id),
            invoice_number=number,
        )
        return InvoiceResponse.from_model(invoice, currency=currency)

    async def void_invoice(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        payload: VoidInvoiceRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> InvoiceResponse:
        """Void an issued invoice that has taken no money (module spec §5.6).

        The invoice keeps its number — voiding must not open a gap in the
        series — and stops counting as revenue (AC-6).

        Only ``issued`` qualifies. The spec also names ``partially_paid`` "with
        no payments recorded — otherwise refund first". Refunding everything
        closes the invoice as ``refunded``, so that path ends there rather
        than in a void.

        :param hospital_id: The hospital the invoice belongs to.
        :param invoice_id: The invoice to void.
        :param payload: The reason, which is required (business rule 4).
        :param actor_id: UUID of the acting user.
        :returns: The voided invoice.
        :raises InvoiceNotFoundError: If absent from this tenant.
        :raises InvalidInvoiceStateError: If it is not ``issued``.
        """
        invoice = await self._lock_or_raise(hospital_id, invoice_id)
        if invoice.status != InvoiceStatus.ISSUED:
            hint = (
                "It has payments recorded; those must be refunded first."
                if invoice.amount_paid > ZERO
                else None
            )
            raise InvalidInvoiceStateError("void", invoice.status, hint=hint)

        invoice = await self._invoices.update_invoice(
            invoice,
            updated_by=actor_id,
            status=InvoiceStatus.VOID,
            voided_at=datetime.now(UTC),
            void_reason=payload.reason,
        )

        await self._audit.record(
            AuditEvent(
                action="invoice.voided",
                hospital_id=hospital_id,
                target_type="invoice",
                target_id=invoice.id,
                actor_id=actor_id,
                changes={
                    "status": {
                        "before": InvoiceStatus.ISSUED.value,
                        "after": InvoiceStatus.VOID.value,
                    }
                },
                context={"invoice_number": invoice.invoice_number, "reason": payload.reason},
            )
        )
        await self._session.commit()

        logger.info(
            "invoice.voided",
            hospital_id=str(hospital_id),
            invoice_id=str(invoice.id),
            invoice_number=invoice.invoice_number,
        )
        currency, _, _ = await self._hospital_billing_context(hospital_id)
        return InvoiceResponse.from_model(invoice, currency=currency)

    # ── Payments ──────────────────────────────────────────────────────────────

    async def record_payment(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        payload: RecordPaymentRequest,
        *,
        idempotency_key: str,
        actor_id: uuid.UUID,
        cash_only: bool = False,
    ) -> tuple[PaymentRecordedResponse, bool]:
        """Record a payment against an invoice (module spec §5.4).

        Returns ``(result, created)``. ``created`` is ``False`` when the
        idempotency key matched a payment already recorded — the caller replays
        the original rather than taking the money twice (rule 6, FR-3, AC-3).

        The replay check runs **after** the invoice is locked, not before. Two
        retries of one request both pass a check made before the lock; made
        after it, the second waits for the first to commit and then finds its
        payment.

        :param hospital_id: The hospital the invoice belongs to.
        :param invoice_id: The invoice being paid.
        :param payload: Validated payment data.
        :param idempotency_key: Client-supplied key; required by rule 6.
        :param actor_id: UUID of the user recording the payment.
        :param cash_only: The caller may record cash and nothing else — they
            hold ``invoice.payment.record.cash`` but not the full permission.
        :returns: The payment with the invoice as it now stands, and whether
            the payment was newly created.
        :raises CashOnlyPaymentError: If ``cash_only`` and the method is not cash.
        :raises InvoiceNotFoundError: If absent from this tenant.
        :raises IdempotencyKeyReuseError: If the key belongs to a different payment.
        :raises InvalidInvoiceStateError: If the invoice cannot take payments.
        :raises OverpaymentError: If the amount exceeds the balance (rule 9).
        """
        # Authorization first, before anything is read or locked: a caller who
        # may not make this request learns nothing about the invoice from it.
        if cash_only and payload.method != PaymentMethod.CASH:
            raise CashOnlyPaymentError(payload.method)

        invoice = await self._lock_or_raise(hospital_id, invoice_id)
        currency, _, _ = await self._hospital_billing_context(hospital_id)

        replayed = await self._invoices.get_payment_by_idempotency_key(hospital_id, idempotency_key)
        if replayed is not None:
            self._assert_same_payment(replayed, invoice_id, payload)
            # Nothing was written; committing just releases the row lock.
            await self._session.commit()
            logger.info(
                "invoice.payment_idempotent_replay",
                hospital_id=str(hospital_id),
                invoice_id=str(invoice_id),
                payment_id=str(replayed.id),
            )
            return self._payment_result(replayed, invoice, currency), False

        if invoice.status not in PAYABLE_STATUSES:
            raise InvalidInvoiceStateError(
                "record a payment against",
                invoice.status,
                hint=(
                    "Issue the invoice first." if invoice.status == InvoiceStatus.DRAFT else None
                ),
            )
        if payload.amount > invoice.balance_due:
            raise OverpaymentError(payload.amount, invoice.balance_due)

        before = invoice.status
        amount_paid = invoice.amount_paid + payload.amount
        # Business rule 11: paid in full the moment the balance reaches zero.
        status = (
            InvoiceStatus.PAID if amount_paid >= invoice.total else InvoiceStatus.PARTIALLY_PAID
        )

        try:
            async with self._session.begin_nested():
                payment = await self._invoices.create_payment(
                    invoice=invoice,
                    amount=payload.amount,
                    method=payload.method,
                    reference=payload.reference,
                    notes=payload.notes,
                    received_by=actor_id,
                    received_at=datetime.now(UTC),
                    idempotency_key=idempotency_key,
                )
                invoice = await self._invoices.update_invoice(
                    invoice, updated_by=actor_id, amount_paid=amount_paid, status=status
                )
        except IntegrityError as exc:
            # The only way here is the same key racing in on a *different*
            # invoice: payments on this one are serialized by the lock above.
            if "uq_payments_hospital_idempotency_key" in str(getattr(exc, "orig", exc)):
                raise IdempotencyKeyReuseError from exc
            raise

        await self._audit.record(
            AuditEvent(
                action="invoice.payment_recorded",
                hospital_id=hospital_id,
                target_type="invoice",
                target_id=invoice.id,
                actor_id=actor_id,
                changes={"status": {"before": before.value, "after": status.value}},
                context={
                    "payment_id": str(payment.id),
                    "amount": str(payment.amount),
                    "method": payment.method.value,
                    "amount_paid": str(invoice.amount_paid),
                },
            )
        )
        await self._session.commit()

        logger.info(
            "invoice.payment_recorded",
            hospital_id=str(hospital_id),
            invoice_id=str(invoice.id),
            payment_id=str(payment.id),
            status=status.value,
        )
        return self._payment_result(payment, invoice, currency), True

    # ── Refunds ───────────────────────────────────────────────────────────────

    async def record_refund(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        payload: RecordRefundRequest,
        *,
        idempotency_key: str,
        actor_id: uuid.UUID,
    ) -> tuple[RefundRecordedResponse, bool]:
        """Give money back against an invoice (module spec §5.5).

        Returns ``(result, created)``, with the same replay semantics as
        :meth:`record_payment`: a retried request with the same key returns
        the original refund rather than refunding twice.

        A refund that brings ``amount_refunded`` up to ``amount_paid`` closes
        the invoice as ``refunded``. A smaller one leaves the status as it was.

        :param hospital_id: The hospital the invoice belongs to.
        :param invoice_id: The invoice being refunded.
        :param payload: Validated refund data.
        :param idempotency_key: Client-supplied key.
        :param actor_id: UUID of the admin issuing the refund.
        :returns: The refund with the invoice as it now stands, and whether
            the refund was newly created.
        :raises InvoiceNotFoundError: If absent from this tenant.
        :raises IdempotencyKeyReuseError: If the key belongs to a different refund.
        :raises InvalidInvoiceStateError: If the invoice holds no money to refund.
        :raises RefundExceedsPaidError: If the amount exceeds what is left to
            give back (business rule 10).
        """
        invoice = await self._lock_or_raise(hospital_id, invoice_id)
        currency, _, _ = await self._hospital_billing_context(hospital_id)

        # After the lock, for the same reason as in record_payment.
        replayed = await self._invoices.get_refund_by_idempotency_key(hospital_id, idempotency_key)
        if replayed is not None:
            if (
                replayed.invoice_id != invoice_id
                or replayed.amount != payload.amount
                or replayed.method != payload.method
            ):
                raise IdempotencyKeyReuseError("refund")
            await self._session.commit()
            logger.info(
                "invoice.refund_idempotent_replay",
                hospital_id=str(hospital_id),
                invoice_id=str(invoice_id),
                refund_id=str(replayed.id),
            )
            return self._refund_result(replayed, invoice, currency), False

        if invoice.status not in REFUNDABLE_STATUSES:
            raise InvalidInvoiceStateError(
                "refund",
                invoice.status,
                hint="Only an invoice that has taken a payment can be refunded.",
            )
        if payload.amount > invoice.refundable_amount:
            raise RefundExceedsPaidError(payload.amount, invoice.refundable_amount)

        before = invoice.status
        amount_refunded = invoice.amount_refunded + payload.amount
        status = InvoiceStatus.REFUNDED if amount_refunded == invoice.amount_paid else before

        try:
            async with self._session.begin_nested():
                refund = await self._invoices.create_refund(
                    invoice=invoice,
                    amount=payload.amount,
                    method=payload.method,
                    reason=payload.reason,
                    reference=payload.reference,
                    refunded_by=actor_id,
                    refunded_at=datetime.now(UTC),
                    idempotency_key=idempotency_key,
                )
                invoice = await self._invoices.update_invoice(
                    invoice, updated_by=actor_id, amount_refunded=amount_refunded, status=status
                )
        except IntegrityError as exc:
            # The same key racing in on a *different* invoice; refunds on this
            # one are serialized by the lock above.
            if "uq_refunds_hospital_idempotency_key" in str(getattr(exc, "orig", exc)):
                raise IdempotencyKeyReuseError("refund") from exc
            raise

        await self._audit.record(
            AuditEvent(
                action="invoice.refunded",
                hospital_id=hospital_id,
                target_type="invoice",
                target_id=invoice.id,
                actor_id=actor_id,
                changes={"status": {"before": before.value, "after": status.value}},
                context={
                    "refund_id": str(refund.id),
                    "amount": str(refund.amount),
                    "method": refund.method.value,
                    "reason": refund.reason,
                    "amount_refunded": str(invoice.amount_refunded),
                },
            )
        )
        await self._session.commit()

        logger.info(
            "invoice.refunded",
            hospital_id=str(hospital_id),
            invoice_id=str(invoice.id),
            refund_id=str(refund.id),
            status=status.value,
        )
        return self._refund_result(refund, invoice, currency), True

    # ── Charges from other modules ────────────────────────────────────────────

    async def add_charges(
        self,
        hospital_id: uuid.UUID,
        *,
        patient_id: uuid.UUID,
        appointment_id: uuid.UUID | None,
        charges: list[Charge],
        source: str,
        actor_id: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """Put another module's charges on a draft invoice for the patient.

        This is Billing's side of :class:`~app.core.charges.ChargeSink`: the
        Laboratory charges for tests ordered and the Pharmacy for medicines
        dispensed without either knowing what an invoice looks like.

        The lines join the visit's draft invoice if it has one that is still
        open. Otherwise a new draft is raised — linked to the visit when the
        visit has no invoice yet, standalone when its invoice has already been
        issued, because an issued invoice's lines are frozen (AC-1).

        A draft raised here for a visit is marked as still owing the
        consultation fee, which :meth:`draft_from_appointment` adds when the
        visit completes.

        **Does not commit.** The charge belongs to the caller's transaction, so
        a test is never ordered without being charged, nor charged without
        being ordered.

        The lines are untaxed, as the consultation line is: whether a test or
        a medicine is taxable is not recorded on its catalog, so the draft is
        left for billing staff to correct before it is issued.

        :param hospital_id: The tenant to scope to.
        :param patient_id: Patient being charged.
        :param appointment_id: The visit the charges belong to, if any.
        :param charges: The lines to add.
        :param source: Which module raised them, for the audit trail.
        :param actor_id: UUID of the acting user.
        :returns: The invoice the lines went onto, or ``None`` if there were
            no charges.
        """
        if not charges:
            return None

        amounts = [compute_line(charge.unit_price, charge.quantity, ZERO) for charge in charges]
        lines = [
            {
                "service_id": None,
                "description": charge.description[:_DESCRIPTION_MAX_LENGTH],
                "quantity": charge.quantity,
                "unit_price": charge.unit_price,
                "tax_rate": ZERO,
                "line_total": amount.total,
            }
            for charge, amount in zip(charges, amounts, strict=True)
        ]

        visit_invoice = (
            await self._invoices.get_live_invoice_for_appointment(hospital_id, appointment_id)
            if appointment_id is not None
            else None
        )
        if visit_invoice is not None:
            # Lock before trusting the status: it may be issued between the
            # read above and this write.
            draft = await self._invoices.get_invoice_for_update(hospital_id, visit_invoice.id)
            if draft is not None and draft.status is InvoiceStatus.DRAFT:
                return await self._append_charges(draft, lines, amounts, source, actor_id)

        try:
            invoice = await self._insert_draft(
                hospital_id=hospital_id,
                patient_id=patient_id,
                # Only one live invoice may point at a visit.
                appointment_id=appointment_id if visit_invoice is None else None,
                notes=None,
                lines=lines,
                totals=compute_invoice_totals(amounts),
                actor_id=actor_id,
                consultation_fee_pending=appointment_id is not None and visit_invoice is None,
            )
        except DuplicateAppointmentInvoiceError:
            # The visit gained an invoice between the check and the insert.
            invoice = await self._insert_draft(
                hospital_id=hospital_id,
                patient_id=patient_id,
                appointment_id=None,
                notes=None,
                lines=lines,
                totals=compute_invoice_totals(amounts),
                actor_id=actor_id,
            )
        await self._record_drafted(invoice, actor_id=actor_id, source=source)
        return invoice.id

    async def _append_charges(
        self,
        draft: Invoice,
        lines: list[dict[str, Any]],
        amounts: list[LineAmounts],
        source: str,
        actor_id: uuid.UUID | None,
    ) -> uuid.UUID:
        """Add priced lines to a locked draft and bring its totals up to date.

        A discount on the draft was approved, or waved through, against the
        lines it had then. More lines change that question, so it is asked
        again — the same rule :meth:`update_invoice` applies to an edit.
        """
        existing = [
            compute_line(item.unit_price, item.quantity, item.tax_rate) for item in draft.items
        ]
        totals = compute_invoice_totals([*existing, *amounts])
        fields: dict[str, Any] = {
            "subtotal": totals.subtotal,
            "tax_amount": totals.tax_amount,
            "total": totals.subtotal + totals.tax_amount - draft.discount_amount,
        }
        if draft.discount_amount > ZERO:
            threshold = await self._discount_threshold_percent(draft.hospital_id)
            fields["discount_pending_approval"] = (
                draft.discount_amount > totals.subtotal * threshold / _HUNDRED
            )
            fields["discount_approved_by"] = None

        await self._invoices.append_items(draft, lines)
        invoice = await self._invoices.update_invoice(draft, updated_by=actor_id, **fields)

        await self._audit.record(
            AuditEvent(
                action="invoice.charges_added",
                hospital_id=invoice.hospital_id,
                target_type="invoice",
                target_id=invoice.id,
                actor_id=actor_id,
                context={
                    "source": source,
                    "lines_added": len(lines),
                    "total": str(invoice.total),
                },
            )
        )
        logger.info(
            "invoice.charges_added",
            hospital_id=str(invoice.hospital_id),
            invoice_id=str(invoice.id),
            source=source,
        )
        return invoice.id

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_invoice(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        *,
        own_visits_of: uuid.UUID | None = None,
    ) -> InvoiceResponse:
        """Retrieve one invoice with its lines.

        :param hospital_id: The hospital the invoice belongs to.
        :param invoice_id: The invoice to read.
        :param own_visits_of: Restrict to invoices for appointments where this
            user is the doctor. ``None`` means no restriction.
        :raises InvoiceNotFoundError: If absent from this tenant, or outside
            the caller's own visits.
        """
        invoice = await self._get_or_raise(hospital_id, invoice_id, own_visits_of=own_visits_of)
        currency, _, _ = await self._hospital_billing_context(hospital_id)
        return InvoiceResponse.from_model(invoice, currency=currency)

    async def list_invoices(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        patient_id: uuid.UUID | None = None,
        status: InvoiceStatus | None = None,
        issued_from: date | None = None,
        issued_to: date | None = None,
        discount_pending: bool | None = None,
        own_visits_of: uuid.UUID | None = None,
    ) -> Page[InvoiceSummaryResponse]:
        """List invoices (module spec §9).

        ``issued_from`` and ``issued_to`` are calendar dates in the **hospital's
        own timezone**, both inclusive. A cashier asking for "today's invoices"
        means the clinic's today, and the conversion to UTC instants happens
        here rather than being left to each client.

        :param hospital_id: The hospital to list.
        :param pagination: Page and page size. Defaults to page 1.
        :param patient_id: Only this patient's invoices.
        :param status: Only invoices in this status.
        :param issued_from: Earliest issue date, inclusive.
        :param issued_to: Latest issue date, inclusive.
        :param discount_pending: ``True`` for the admin's approval queue —
            drafts whose discount awaits approval.
        :param own_visits_of: Restrict to invoices for appointments where this
            user is the doctor. ``None`` means no restriction.
        :returns: One page of summaries plus the total count.
        :raises ValidationError: If ``issued_from`` is after ``issued_to``.
        """
        if issued_from is not None and issued_to is not None and issued_from > issued_to:
            raise _field_error("issued_from", "issued_from must not be after issued_to.")

        page_params = pagination or PaginationParams()
        currency, _, zone = await self._hospital_billing_context(hospital_id)

        filters: dict[str, Any] = {
            "patient_id": patient_id,
            "status": status,
            "discount_pending": discount_pending,
        }
        if own_visits_of is not None:
            doctor_id = await self._own_visits_doctor_id(hospital_id, own_visits_of)
            if doctor_id is None:
                # Scoped to "own visits" but not a doctor: there are none.
                return Page[InvoiceSummaryResponse](
                    items=[],
                    page=page_params.page,
                    page_size=page_params.page_size,
                    total_records=0,
                )
            filters["doctor_id"] = doctor_id
        if issued_from is not None:
            filters["issued_on_or_after"] = datetime.combine(issued_from, time.min, tzinfo=zone)
        if issued_to is not None:
            filters["issued_before"] = datetime.combine(
                issued_to + timedelta(days=1), time.min, tzinfo=zone
            )

        rows = await self._invoices.list_invoices(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._invoices.count_invoices(hospital_id, **filters)

        return Page[InvoiceSummaryResponse](
            items=[InvoiceSummaryResponse.from_model(row, currency=currency) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    async def list_payments(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        *,
        own_visits_of: uuid.UUID | None = None,
    ) -> list[PaymentResponse]:
        """Return an invoice's payments, oldest first.

        :param hospital_id: The hospital the invoice belongs to.
        :param invoice_id: The invoice whose payments to read.
        :param own_visits_of: Restrict to invoices for appointments where this
            user is the doctor. ``None`` means no restriction.
        :raises InvoiceNotFoundError: If absent from this tenant, or outside
            the caller's own visits.
        """
        await self._get_or_raise(hospital_id, invoice_id, own_visits_of=own_visits_of)
        rows = await self._invoices.list_payments(hospital_id, invoice_id)
        return [PaymentResponse.from_model(row) for row in rows]

    async def list_refunds(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        *,
        own_visits_of: uuid.UUID | None = None,
    ) -> list[RefundResponse]:
        """Return an invoice's refunds, oldest first.

        :param hospital_id: The hospital the invoice belongs to.
        :param invoice_id: The invoice whose refunds to read.
        :param own_visits_of: Restrict to invoices for appointments where this
            user is the doctor. ``None`` means no restriction.
        :raises InvoiceNotFoundError: If absent from this tenant, or outside
            the caller's own visits.
        """
        await self._get_or_raise(hospital_id, invoice_id, own_visits_of=own_visits_of)
        rows = await self._invoices.list_refunds(hospital_id, invoice_id)
        return [RefundResponse.from_model(row) for row in rows]

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _get_or_raise(
        self,
        hospital_id: uuid.UUID,
        invoice_id: uuid.UUID,
        *,
        own_visits_of: uuid.UUID | None = None,
    ) -> Invoice:
        """Fetch an invoice or raise :class:`InvoiceNotFoundError`.

        With ``own_visits_of`` set, an invoice outside that user's own visits
        raises the same error as one that does not exist.
        """
        doctor_id: uuid.UUID | None = None
        if own_visits_of is not None:
            doctor_id = await self._own_visits_doctor_id(hospital_id, own_visits_of)
            if doctor_id is None:
                raise InvoiceNotFoundError(invoice_id)

        invoice = await self._invoices.get_invoice_by_id(
            hospital_id, invoice_id, doctor_id=doctor_id
        )
        if invoice is None:
            raise InvoiceNotFoundError(invoice_id)
        return invoice

    async def _own_visits_doctor_id(
        self, hospital_id: uuid.UUID, user_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Resolve the doctor profile an "own visits" scope is anchored to.

        A deactivated doctor profile does not count: someone who no longer
        practises here does not keep a window onto its invoices.

        :param hospital_id: The tenant to scope to.
        :param user_id: The user asking.
        :returns: Their active doctor profile's UUID, or ``None`` if they have
            none — in which case they have no visits and so see no invoices.
        """
        doctor = await self._doctors.get_doctor_by_user_id(
            hospital_id, user_id, include_deleted=False
        )
        return doctor.id if doctor is not None else None

    async def _lock_or_raise(self, hospital_id: uuid.UUID, invoice_id: uuid.UUID) -> Invoice:
        """Fetch and row-lock an invoice, or raise :class:`InvoiceNotFoundError`."""
        invoice = await self._invoices.get_invoice_for_update(hospital_id, invoice_id)
        if invoice is None:
            raise InvoiceNotFoundError(invoice_id)
        return invoice

    async def _assert_patient_valid(self, hospital_id: uuid.UUID, patient_id: uuid.UUID) -> None:
        """Check the patient exists in this tenant."""
        if await self._patients.get_patient_by_id(hospital_id, patient_id) is None:
            raise _field_error("patient_id", "Patient not found in this hospital.")

    async def _assert_appointment_billable(
        self, hospital_id: uuid.UUID, appointment_id: uuid.UUID, patient_id: uuid.UUID
    ) -> None:
        """Check an appointment can be invoiced for this patient.

        :raises ValidationError: If the appointment is unknown in this tenant or
            belongs to a different patient.
        :raises DuplicateAppointmentInvoiceError: If it already has a live invoice.
        """
        appointment = await self._appointments.get_appointment_by_id(hospital_id, appointment_id)
        if appointment is None:
            raise _field_error("appointment_id", "Appointment not found in this hospital.")
        if appointment.patient_id != patient_id:
            raise _field_error("appointment_id", "That appointment is for a different patient.")

        existing = await self._invoices.get_live_invoice_for_appointment(
            hospital_id, appointment_id
        )
        if existing is not None:
            raise DuplicateAppointmentInvoiceError(appointment_id, existing.id)

    async def _hospital_billing_context(
        self, hospital_id: uuid.UUID
    ) -> tuple[str, Decimal, ZoneInfo]:
        """Return a hospital's currency, default tax rate and timezone.

        The tax rate is read from the ``hospitals.settings`` JSONB — the same
        place the no-show grace period lives — under
        ``settings["billing"]["default_tax_rate"]``. A missing, malformed or
        out-of-range value falls back to :data:`DEFAULT_TAX_RATE` and is logged:
        a typo in a settings blob must not stop a cashier raising a bill, and
        must not silently apply a nonsense rate either.

        :param hospital_id: The tenant to read.
        :returns: ``(currency, tax_rate_percent, timezone)``.
        """
        hospital = await self._hospitals.get_by_id(hospital_id)
        if hospital is None:
            return DEFAULT_CURRENCY, DEFAULT_TAX_RATE, ZoneInfo("UTC")

        try:
            zone = ZoneInfo(hospital.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "billing.hospital_timezone_invalid",
                hospital_id=str(hospital_id),
                timezone=hospital.timezone,
            )
            zone = ZoneInfo("UTC")

        billing_settings = (hospital.settings or {}).get("billing")
        raw = (
            billing_settings.get("default_tax_rate") if isinstance(billing_settings, dict) else None
        )
        tax_rate = DEFAULT_TAX_RATE
        if raw is not None:
            try:
                candidate = Decimal(str(raw))
            except InvalidOperation:
                candidate = None
            if candidate is not None and candidate.is_finite() and 0 <= candidate <= _MAX_TAX_RATE:
                tax_rate = candidate
            else:
                logger.warning(
                    "billing.tax_rate_setting_invalid",
                    hospital_id=str(hospital_id),
                    value=str(raw),
                )

        return hospital.currency, tax_rate, zone

    async def _discount_threshold_percent(self, hospital_id: uuid.UUID) -> Decimal:
        """Return the share of the subtotal a discount may reach unapproved.

        Read from ``hospitals.settings["billing"]["discount_approval_threshold_percent"]``,
        alongside the tax rate. A missing, malformed or out-of-range value
        falls back to :data:`DEFAULT_DISCOUNT_APPROVAL_THRESHOLD_PERCENT` —
        zero — so a typo in a settings blob makes *more* discounts need
        approval, never fewer.

        :param hospital_id: The tenant to read.
        :returns: The threshold as a percentage, 0 to 100.
        """
        hospital = await self._hospitals.get_by_id(hospital_id)
        billing_settings = (hospital.settings or {}).get("billing") if hospital else None
        raw = (
            billing_settings.get("discount_approval_threshold_percent")
            if isinstance(billing_settings, dict)
            else None
        )
        if raw is None:
            return DEFAULT_DISCOUNT_APPROVAL_THRESHOLD_PERCENT

        try:
            candidate = Decimal(str(raw))
        except InvalidOperation:
            candidate = None
        if candidate is not None and candidate.is_finite() and 0 <= candidate <= _HUNDRED:
            return candidate

        logger.warning(
            "billing.discount_threshold_setting_invalid",
            hospital_id=str(hospital_id),
            value=str(raw),
        )
        return DEFAULT_DISCOUNT_APPROVAL_THRESHOLD_PERCENT

    async def _price_lines(
        self,
        hospital_id: uuid.UUID,
        items: Sequence[InvoiceLineRequest],
        tax_rate: Decimal,
    ) -> tuple[list[dict[str, Any]], InvoiceTotals]:
        """Turn requested lines into priced rows and invoice totals.

        Catalog lines take their name, price and taxability from the service as
        it stands now; ad-hoc lines carry their own. Either way the tax rate is
        the hospital's and the amounts are computed here.

        :param hospital_id: The tenant to resolve services in.
        :param items: The lines as the client described them.
        :param tax_rate: The hospital's tax rate, as a percentage.
        :returns: Column values per line, and the totals they sum to.
        :raises ValidationError: If a line names a service that is unknown in
            this hospital or has been deactivated.
        """
        service_ids = list({item.service_id for item in items if item.service_id is not None})
        services = {
            service.id: service
            for service in await self._catalog.get_services_by_ids(hospital_id, service_ids)
        }

        lines: list[dict[str, Any]] = []
        amounts = []
        for index, item in enumerate(items):
            if item.service_id is not None:
                service = services.get(item.service_id)
                if service is None:
                    raise _field_error(
                        f"items.{index}.service_id", "Service not found in this hospital."
                    )
                if not service.is_active:
                    raise _field_error(
                        f"items.{index}.service_id",
                        f"Service '{service.code}' is inactive and cannot be billed.",
                    )
                description = item.description or service.name
                unit_price = service.price
                taxable = service.taxable
            else:
                # InvoiceLineRequest guarantees both are present on an ad-hoc line.
                assert item.description is not None
                assert item.unit_price is not None
                description = item.description
                unit_price = item.unit_price
                taxable = bool(item.taxable)

            rate = tax_rate if taxable else ZERO
            line = compute_line(unit_price, item.quantity, rate)
            amounts.append(line)
            lines.append(
                {
                    "service_id": item.service_id,
                    "description": description,
                    "quantity": item.quantity,
                    "unit_price": unit_price,
                    "tax_rate": rate,
                    "line_total": line.total,
                }
            )

        return lines, compute_invoice_totals(amounts)

    async def _insert_draft(
        self,
        *,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        appointment_id: uuid.UUID | None,
        notes: str | None,
        lines: Sequence[dict[str, Any]],
        totals: InvoiceTotals,
        actor_id: uuid.UUID | None,
        consultation_fee_pending: bool = False,
    ) -> Invoice:
        """Insert a draft, translating a lost appointment race into a 409.

        The write sits in a savepoint so that a constraint violation rolls back
        only this insert and leaves the caller's transaction usable.

        :raises DuplicateAppointmentInvoiceError: If the appointment gained a
            live invoice between the caller's check and this write.
        """
        try:
            async with self._session.begin_nested():
                return await self._invoices.create_invoice(
                    hospital_id=hospital_id,
                    patient_id=patient_id,
                    appointment_id=appointment_id,
                    notes=notes,
                    lines=lines,
                    subtotal=totals.subtotal,
                    tax_amount=totals.tax_amount,
                    total=totals.total,
                    created_by=actor_id,
                    consultation_fee_pending=consultation_fee_pending,
                )
        except IntegrityError as exc:
            if appointment_id is not None and "uq_invoices_live_appointment" in str(
                getattr(exc, "orig", exc)
            ):
                raise DuplicateAppointmentInvoiceError(appointment_id) from exc
            raise

    async def _record_drafted(
        self, invoice: Invoice, *, actor_id: uuid.UUID | None, source: str
    ) -> None:
        """Record the ``invoice.drafted`` audit event (module spec §5.1 step 4)."""
        await self._audit.record(
            AuditEvent(
                action="invoice.drafted",
                hospital_id=invoice.hospital_id,
                target_type="invoice",
                target_id=invoice.id,
                actor_id=actor_id,
                context={
                    "source": source,
                    "appointment_id": (
                        str(invoice.appointment_id) if invoice.appointment_id else None
                    ),
                    "line_count": len(invoice.items),
                    "total": str(invoice.total),
                },
            )
        )
        logger.info(
            "invoice.drafted",
            hospital_id=str(invoice.hospital_id),
            invoice_id=str(invoice.id),
            source=source,
        )

    async def _next_invoice_number(
        self, hospital_id: uuid.UUID, *, issued_at: datetime, zone: ZoneInfo
    ) -> str:
        """Reserve and render the hospital's next invoice number.

        Must run inside the issuing transaction — see the module docstring.
        The ``{year}`` placeholder is the year in the hospital's timezone, so
        an invoice issued at 00:30 on 1 January in India is numbered into the
        new year even though it is still 31 December in UTC.

        The renderer is :func:`app.utils.mrn.format_mrn`: despite its name it
        is the project's generic, allowlisted ``{year}``/``{seq}`` template
        renderer, capped at 30 characters — the width of ``invoice_number``.

        :raises ConfigurationError: If the stored template is invalid. This is a
            misconfiguration, not user input, and must not be reported to the
            caller as a validation failure.
        """
        sequence_value, template = await self._sequences.advance(hospital_id)
        year = issued_at.astimezone(zone).year

        try:
            return format_mrn(template, year=year, sequence=sequence_value)
        except InvalidMrnTemplateError as exc:
            logger.error(
                "invoice.number_template_invalid",
                hospital_id=str(hospital_id),
                sequence_value=sequence_value,
                reason=str(exc),
            )
            msg = "The hospital's invoice number format is misconfigured."
            raise ConfigurationError(msg, detail={"hospital_id": str(hospital_id)}) from exc

    @staticmethod
    def _assert_same_payment(
        existing: Payment, invoice_id: uuid.UUID, payload: RecordPaymentRequest
    ) -> None:
        """Check a replayed key really is a replay of the same payment.

        :raises IdempotencyKeyReuseError: If invoice, amount or method differ.
        """
        if (
            existing.invoice_id != invoice_id
            or existing.amount != payload.amount
            or existing.method != payload.method
        ):
            raise IdempotencyKeyReuseError

    @staticmethod
    def _refund_result(refund: Refund, invoice: Invoice, currency: str) -> RefundRecordedResponse:
        """Pair a refund with the invoice as it now stands."""
        return RefundRecordedResponse(
            refund=RefundResponse.from_model(refund),
            invoice=InvoiceSummaryResponse.from_model(invoice, currency=currency),
        )

    @staticmethod
    def _payment_result(
        payment: Payment, invoice: Invoice, currency: str
    ) -> PaymentRecordedResponse:
        """Pair a payment with the invoice as it now stands."""
        return PaymentRecordedResponse(
            payment=PaymentResponse.from_model(payment),
            invoice=InvoiceSummaryResponse.from_model(invoice, currency=currency),
        )


# ── The appointment seam ────────────────────────────────────────────────────


class BillingInvoiceDraftSink:
    """Hands completed appointments to Billing.

    Satisfies the ``InvoiceDraftSink`` protocol that
    :mod:`app.services.appointment_service` has been calling through a null
    implementation, so wiring this in changes one DI provider and nothing in
    the appointment module.

    **Never raises.** The appointment has already been committed as completed
    by the time this runs; a billing failure must not turn a consultation that
    genuinely happened into an error response. The failure is logged, and the
    invoice can be raised by hand.

    :param billing: The billing service to draft through.
    """

    def __init__(self, billing: BillingService) -> None:
        self._billing = billing

    async def draft_invoice_for(
        self, hospital_id: uuid.UUID, appointment_id: uuid.UUID, *, actor_id: uuid.UUID | None
    ) -> None:
        """Draft an invoice for a completed appointment, swallowing failures.

        :param hospital_id: The tenant to scope to.
        :param appointment_id: The appointment that just completed.
        :param actor_id: UUID of the acting user.
        """
        try:
            await self._billing.draft_from_appointment(
                hospital_id, appointment_id, actor_id=actor_id
            )
        except Exception:  # noqa: BLE001 — see the class docstring
            logger.exception(
                "invoice.draft_failed",
                hospital_id=str(hospital_id),
                appointment_id=str(appointment_id),
            )
