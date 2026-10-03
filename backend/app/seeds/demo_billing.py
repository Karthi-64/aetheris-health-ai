"""Billing demo data — a services catalog and invoices in every reachable state.

A section of the one seed mechanism, called by
:func:`app.seeds.demo_data.seed_demo_data` after the appointments exist. It is
split out for the same reason ``demo_data`` is split from ``seed``: length.

**Everything here is fictional**, including the payment references.

**What a demo gets.** A catalog to pick lines from, and one invoice per state
the frontend has to render:

==================  ========================================================
State               Invoice
==================  ========================================================
``paid``            Ravi Menon's follow-up, settled in cash and by UPI
``void``            Thomas George's visit, raised at the wrong rate
``partially_paid``  The re-issue of that same visit, part-paid by card
``issued``          Ishaan Kulkarni's visit, nothing paid yet
``draft``           Counter items for Meera Nair, not tied to an appointment
==================  ========================================================

The void-then-reissue pair is deliberate: it is business rule 3 ("corrections
require a void + re-issue") as data, and it shows a void invoice keeping its
number while the series carries on.

Nothing is pre-drafted for the appointments that are still in flight, so
completing one in a demo produces its draft live.

**Idempotency.** Looked up by a stable natural key before creating, like every
other seeded entity:

==========  ================================================================
Entity      Natural key
==========  ================================================================
Service     ``(hospital_id, code)`` — the table's unique constraint
Invoice     its appointment: if the appointment has *any* invoice, void or
            not, nothing is created for it. The ad-hoc draft is keyed on the
            patient having any invoice with no appointment.
Payment     created only together with its invoice, and carries a fixed
            ``idempotency_key`` under the tenant-scoped unique index
==========  ================================================================

So a second run creates nothing and allocates no further invoice numbers.

**Built with the real rules.** Amounts come from
:mod:`app.utils.invoice_math` and numbers from the same per-hospital counter
the API uses, so seeded invoices are indistinguishable from ones raised through
the API. The rows are written through the repositories rather than
:class:`~app.services.billing_service.BillingService` only because that service
commits, and a seed section must leave the transaction to its caller.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.billing import InvoiceStatus, PaymentMethod
from app.repositories.invoice_number_sequence_repository import InvoiceNumberSequenceRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.service_catalog_repository import ServiceCatalogRepository
from app.utils.invoice_math import ZERO, compute_invoice_totals, compute_line
from app.utils.mrn import format_mrn

if TYPE_CHECKING:
    import uuid
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.billing import Invoice, Service
    from app.models.hospital import Hospital
    from app.models.patient import Patient

logger = structlog.get_logger(__name__)

__all__ = ["SERVICES", "seed_demo_billing"]

#: (code, name, category, price, taxable, is_active)
#:
#: Clinical services are untaxed and the two non-clinical ones are taxable, so
#: setting ``hospitals.settings["billing"]["default_tax_rate"]`` has something
#: to act on. One retired service makes the ``is_active`` filter demonstrable.
SERVICES: list[tuple[str, str, str, str, bool, bool]] = [
    ("CONS-GEN", "General consultation", "Consultation", "500.00", False, True),
    ("CONS-SPEC", "Specialist consultation", "Consultation", "800.00", False, True),
    ("CONS-FU", "Follow-up consultation", "Consultation", "300.00", False, True),
    ("ECG", "ECG (12-lead)", "Diagnostics", "450.00", False, True),
    ("XRAY-CHEST", "Chest X-ray", "Diagnostics", "600.00", False, True),
    ("LAB-CBC", "Complete blood count", "Laboratory", "350.00", False, True),
    ("PROC-DRESS", "Wound dressing", "Procedure", "250.00", False, True),
    ("COSM-CONS", "Cosmetic consultation", "Cosmetic", "1500.00", True, True),
    ("ADMIN-CERT", "Medical certificate", "Administrative", "200.00", True, True),
    ("LAB-ESR", "ESR (retired panel)", "Laboratory", "150.00", False, False),
]

#: An invoice line as the seed describes it: a catalog code, or an ad-hoc
#: ``(description, unit_price)`` pair, each with a quantity.
_Line = tuple[str | tuple[str, str], str]


async def _seed_services(session: AsyncSession, hospital: Hospital) -> dict[str, Service]:
    """Create the services catalog.

    :param session: The open session.
    :param hospital: The tenant.
    :returns: Services by code.
    """
    repository = ServiceCatalogRepository(session)
    services: dict[str, Service] = {}
    created = 0

    for code, name, category, price, taxable, is_active in SERVICES:
        service = await repository.get_service_by_code(hospital.id, code)
        if service is None:
            service = await repository.create_service(
                hospital_id=hospital.id,
                code=code,
                name=name,
                category=category,
                price=Decimal(price),
                taxable=taxable,
                is_active=is_active,
            )
            created += 1
        services[code] = service

    logger.info("demo_services_seeded", total=len(SERVICES), created=created)
    return services


def _priced_lines(
    plan: list[_Line], services: dict[str, Service]
) -> tuple[list[dict[str, Any]], Decimal, Decimal, Decimal]:
    """Price a list of seed lines with the real invoice arithmetic.

    Every seeded line is untaxed: the demo hospital configures no tax rate, so
    that is what the API would compute for it too.

    :param plan: The lines to price.
    :param services: The catalog, by code.
    :returns: Line column values, then subtotal, tax amount and total.
    """
    lines: list[dict[str, Any]] = []
    amounts = []
    for item, quantity in plan:
        if isinstance(item, str):
            service = services[item]
            service_id, description, unit_price = service.id, service.name, service.price
        else:
            service_id, (description, price) = None, item
            unit_price = Decimal(price)

        amount = compute_line(unit_price, Decimal(quantity), ZERO)
        amounts.append(amount)
        lines.append(
            {
                "service_id": service_id,
                "description": description,
                "quantity": Decimal(quantity),
                "unit_price": unit_price,
                "tax_rate": ZERO,
                "line_total": amount.total,
            }
        )

    totals = compute_invoice_totals(amounts)
    return lines, totals.subtotal, totals.tax_amount, totals.total


class _InvoiceSeeder:
    """Writes seeded invoices through the repositories, in the caller's transaction.

    :param session: The open session.
    :param hospital: The tenant.
    :param services: The seeded catalog, by code.
    :param zone: The clinic's timezone, for the year in an invoice number.
    :param actor_id: User recorded as the author, or ``None``.
    """

    def __init__(
        self,
        session: AsyncSession,
        hospital: Hospital,
        services: dict[str, Service],
        zone: ZoneInfo,
        actor_id: uuid.UUID | None,
    ) -> None:
        self._hospital = hospital
        self._services = services
        self._zone = zone
        self._actor_id = actor_id
        self._invoices = InvoiceRepository(session)
        self._sequences = InvoiceNumberSequenceRepository(session)

    async def draft(
        self,
        patient: Patient,
        plan: list[_Line],
        *,
        appointment: Appointment | None = None,
        notes: str | None = None,
    ) -> Invoice:
        """Insert a draft invoice."""
        lines, subtotal, tax_amount, total = _priced_lines(plan, self._services)
        return await self._invoices.create_invoice(
            hospital_id=self._hospital.id,
            patient_id=patient.id,
            appointment_id=appointment.id if appointment is not None else None,
            notes=notes,
            lines=lines,
            subtotal=subtotal,
            tax_amount=tax_amount,
            total=total,
            created_by=self._actor_id,
        )

    async def issue(self, invoice: Invoice, *, at: datetime) -> Invoice:
        """Give a draft the hospital's next number and mark it issued."""
        sequence_value, template = await self._sequences.advance(self._hospital.id)
        number = format_mrn(template, year=at.astimezone(self._zone).year, sequence=sequence_value)
        return await self._invoices.update_invoice(
            invoice,
            updated_by=self._actor_id,
            invoice_number=number,
            status=InvoiceStatus.ISSUED,
            issued_at=at,
        )

    async def void(self, invoice: Invoice, *, at: datetime, reason: str) -> Invoice:
        """Void an issued invoice. It keeps its number."""
        return await self._invoices.update_invoice(
            invoice,
            updated_by=self._actor_id,
            status=InvoiceStatus.VOID,
            voided_at=at,
            void_reason=reason,
        )

    async def pay(
        self,
        invoice: Invoice,
        amount: str,
        method: PaymentMethod,
        *,
        key: str,
        at: datetime,
        received_by: uuid.UUID,
        reference: str | None = None,
    ) -> Invoice:
        """Record a payment and move the invoice's balance and status with it."""
        paid = Decimal(amount)
        await self._invoices.create_payment(
            invoice=invoice,
            amount=paid,
            method=method,
            reference=reference,
            received_by=received_by,
            received_at=at,
            idempotency_key=key,
        )
        amount_paid = invoice.amount_paid + paid
        return await self._invoices.update_invoice(
            invoice,
            updated_by=self._actor_id,
            amount_paid=amount_paid,
            status=(
                InvoiceStatus.PAID if amount_paid >= invoice.total else InvoiceStatus.PARTIALLY_PAID
            ),
        )

    async def has_invoice_for(self, appointment: Appointment) -> bool:
        """Whether an appointment already has any invoice, void or not."""
        return (
            await self._invoices.count_invoices(self._hospital.id, appointment_id=appointment.id)
            > 0
        )

    async def has_ad_hoc_invoice(self, patient: Patient) -> bool:
        """Whether a patient already has an invoice not tied to an appointment."""
        existing = await self._invoices.list_invoices(
            self._hospital.id, patient_id=patient.id, limit=100
        )
        return any(invoice.appointment_id is None for invoice in existing)


async def _appointment(session: AsyncSession, hospital: Hospital, key: str) -> Appointment | None:
    """Look up a seeded appointment by its idempotency key."""
    result = await session.execute(
        select(Appointment).where(
            Appointment.hospital_id == hospital.id, Appointment.idempotency_key == key
        )
    )
    return result.unique().scalar_one_or_none()


async def seed_demo_billing(
    session: AsyncSession,
    hospital: Hospital,
    patients: dict[str, Patient],
    *,
    zone: ZoneInfo,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Seed the services catalog and one invoice per lifecycle state.

    Safe to run repeatedly: see the module docstring for the natural key used
    per entity. The caller owns the transaction and commits.

    :param session: An open session inside a transaction.
    :param hospital: The demo hospital every row is scoped to.
    :param patients: Seeded patients by ``"first last"``.
    :param zone: The clinic's timezone.
    :param actor_id: User recorded as the author of seeded invoices.
    """
    services = await _seed_services(session, hospital)
    seeder = _InvoiceSeeder(session, hospital, services, zone, actor_id)
    created = 0

    # ── paid: settled across two methods ─────────────────────────────────────
    visit = await _appointment(session, hospital, "seed-appt-0001")
    if visit is not None and not await seeder.has_invoice_for(visit):
        # `payments.received_by` is NOT NULL. When the seed runs with no acting
        # user, the consulting doctor's account stands in as the recorder.
        cashier = actor_id or visit.doctor.user_id
        issued_at = visit.scheduled_end + timedelta(minutes=10)
        invoice = await seeder.draft(
            visit.patient,
            [("CONS-FU", "1"), ("ECG", "1"), ("LAB-CBC", "1")],
            appointment=visit,
        )
        invoice = await seeder.issue(invoice, at=issued_at)
        invoice = await seeder.pay(
            invoice,
            "500.00",
            PaymentMethod.CASH,
            key="seed-payment-0001-cash",
            at=issued_at + timedelta(minutes=5),
            received_by=cashier,
        )
        await seeder.pay(
            invoice,
            "600.00",
            PaymentMethod.UPI,
            key="seed-payment-0001-upi",
            at=issued_at + timedelta(minutes=6),
            received_by=cashier,
            reference="UPI-DEMO-000001",
        )
        created += 1

    # ── void, then the re-issue that is partially paid ───────────────────────
    visit = await _appointment(session, hospital, "seed-appt-0002")
    if visit is not None and not await seeder.has_invoice_for(visit):
        cashier = actor_id or visit.doctor.user_id
        issued_at = visit.scheduled_end + timedelta(minutes=10)
        wrong = await seeder.draft(visit.patient, [("CONS-GEN", "1")], appointment=visit)
        wrong = await seeder.issue(wrong, at=issued_at)
        await seeder.void(
            wrong,
            at=issued_at + timedelta(minutes=20),
            reason="Raised at the general rate; re-issued at the follow-up rate with the ECG.",
        )

        reissue = await seeder.draft(
            visit.patient, [("CONS-FU", "1"), ("ECG", "1")], appointment=visit
        )
        reissue = await seeder.issue(reissue, at=issued_at + timedelta(minutes=25))
        await seeder.pay(
            reissue,
            "300.00",
            PaymentMethod.CARD,
            key="seed-payment-0002-card",
            at=issued_at + timedelta(minutes=30),
            received_by=cashier,
            reference="CARD-DEMO-000002",
        )
        created += 2

    # ── issued: nothing paid yet ─────────────────────────────────────────────
    visit = await _appointment(session, hospital, "seed-appt-0003")
    if visit is not None and not await seeder.has_invoice_for(visit):
        invoice = await seeder.draft(
            visit.patient,
            [("CONS-SPEC", "1"), (("Nebulisation", "150.00"), "2")],
            appointment=visit,
        )
        await seeder.issue(invoice, at=visit.scheduled_end + timedelta(minutes=10))
        created += 1

    # ── draft: counter items, not tied to an appointment ─────────────────────
    walk_in = patients["Meera Nair"]
    if not await seeder.has_ad_hoc_invoice(walk_in):
        await seeder.draft(
            walk_in,
            [("PROC-DRESS", "1"), (("Crepe bandage", "75.00"), "2")],
            notes="Counter items — awaiting issue.",
        )
        created += 1

    logger.info("demo_invoices_seeded", created=created)
