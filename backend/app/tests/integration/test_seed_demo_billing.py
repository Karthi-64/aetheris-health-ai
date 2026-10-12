"""Integration tests for the billing section of the demo seed.

Same two properties :mod:`app.tests.integration.test_seed_demo_data` checks for
the clinical data: what the seed produces is valid and complete, and running it
twice changes nothing. For billing "valid" has a sharper meaning — the stored
totals must be what the invoice arithmetic produces, and the seeded invoice
numbers must be a gap-free series, because seeded invoices are meant to be
indistinguishable from ones raised through the API.

:func:`~app.seeds.demo_data.seed_demo_data` is called rather than the billing
section on its own, so the test covers the wiring between the two.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.appointment import Appointment, AppointmentStatus
from app.models.billing import (
    Invoice,
    InvoiceItem,
    InvoiceNumberSequence,
    InvoiceStatus,
    Payment,
    Refund,
    Service,
)
from app.models.hospital import Hospital
from app.seeds.demo_billing import SERVICES
from app.seeds.demo_data import seed_demo_data

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

BILLING_MODELS: dict[str, type[Any]] = {
    "services": Service,
    "invoices": Invoice,
    "invoice_items": InvoiceItem,
    "payments": Payment,
    "refunds": Refund,
}


@pytest_asyncio.fixture
async def hospital(db_session: AsyncSession, hospital_id: uuid.UUID) -> Hospital:
    """The tenant the seed writes into."""
    result = await db_session.execute(select(Hospital).where(Hospital.id == hospital_id))
    return result.unique().scalar_one()


async def _counts(session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, int]:
    """Snapshot the billing row counts the seed is responsible for."""
    counts = {}
    for name, model in BILLING_MODELS.items():
        result = await session.execute(
            select(func.count()).select_from(model).where(model.hospital_id == hospital_id)
        )
        counts[name] = int(result.scalar_one())
    return counts


async def _invoices(session: AsyncSession, hospital_id: uuid.UUID) -> list[Invoice]:
    """Every seeded invoice, in the order the seed created them."""
    result = await session.execute(
        select(Invoice).where(Invoice.hospital_id == hospital_id).order_by(Invoice.created_at)
    )
    return list(result.unique().scalars().all())


async def _sequence_value(session: AsyncSession, hospital_id: uuid.UUID) -> int:
    """The hospital's invoice-number counter."""
    result = await session.execute(
        select(InvoiceNumberSequence.current_value).where(
            InvoiceNumberSequence.hospital_id == hospital_id
        )
    )
    return int(result.scalar_one())


class TestSeededBilling:
    """What one run of the seed produces."""

    async def test_seeds_the_services_catalog(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        result = await db_session.execute(select(Service).where(Service.hospital_id == hospital.id))
        services = list(result.unique().scalars().all())

        assert {service.code for service in services} == {code for code, *_ in SERVICES}
        # At least one retired service, so the is_active filter is demonstrable.
        assert any(not service.is_active for service in services)
        # And both taxable and exempt services, so a configured rate shows.
        assert {service.taxable for service in services} == {True, False}

    async def test_every_reachable_invoice_state_is_represented(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        statuses = {invoice.status for invoice in await _invoices(db_session, hospital.id)}

        assert statuses == {
            InvoiceStatus.DRAFT,
            InvoiceStatus.ISSUED,
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.PAID,
            InvoiceStatus.VOID,
            InvoiceStatus.REFUNDED,
        }

    async def test_invoice_numbers_are_a_gap_free_series(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        invoices = await _invoices(db_session, hospital.id)

        numbered = [invoice for invoice in invoices if invoice.invoice_number is not None]
        sequence = sorted(
            int(invoice.invoice_number.rsplit("-", 1)[1])  # type: ignore[union-attr]
            for invoice in numbered
        )

        assert sequence == list(range(1, len(numbered) + 1))
        # The counter agrees with the series, so the next API issue continues it.
        assert await _sequence_value(db_session, hospital.id) == len(numbered)
        # Only the drafts are unnumbered, and the void invoice kept its number.
        assert {i.status for i in invoices if i.invoice_number is None} == {InvoiceStatus.DRAFT}
        assert all(i.invoice_number for i in invoices if i.status is InvoiceStatus.VOID)

    async def test_stored_totals_match_the_lines(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        for invoice in await _invoices(db_session, hospital.id):
            assert invoice.items, f"{invoice!r} has no lines"
            for item in invoice.items:
                assert item.hospital_id == hospital.id
                assert item.line_total == item.unit_price * item.quantity
            assert invoice.subtotal == sum((item.line_total for item in invoice.items), Decimal(0))
            assert invoice.discount_amount <= invoice.subtotal
            assert invoice.total == invoice.subtotal + invoice.tax_amount - invoice.discount_amount

    async def test_amount_paid_and_refunded_match_the_rows(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        invoices = await _invoices(db_session, hospital.id)
        methods_per_paid_invoice = []

        for invoice in invoices:
            payments = list(
                (await db_session.execute(select(Payment).where(Payment.invoice_id == invoice.id)))
                .unique()
                .scalars()
                .all()
            )
            refunds = list(
                (await db_session.execute(select(Refund).where(Refund.invoice_id == invoice.id)))
                .unique()
                .scalars()
                .all()
            )
            paid = sum((payment.amount for payment in payments), Decimal(0))
            refunded = sum((refund.amount for refund in refunds), Decimal(0))

            assert invoice.amount_paid == paid
            assert invoice.amount_refunded == refunded
            assert refunded <= paid
            if invoice.status is InvoiceStatus.PAID:
                assert paid == invoice.total
                assert refunded < paid
                methods_per_paid_invoice.append({payment.method for payment in payments})
            elif invoice.status is InvoiceStatus.REFUNDED:
                assert refunded == paid > 0
            elif invoice.status is InvoiceStatus.PARTIALLY_PAID:
                assert Decimal(0) < paid < invoice.total
            else:
                assert payments == []
                assert refunds == []

        # One paid invoice is settled across two methods, for variety in the list.
        assert any(len(methods) == 2 for methods in methods_per_paid_invoice)
        # And one paid invoice carries a partial refund.
        assert any(i.status is InvoiceStatus.PAID and i.amount_refunded > 0 for i in invoices)

    async def test_one_draft_is_held_for_discount_approval(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        held = [i for i in await _invoices(db_session, hospital.id) if i.discount_pending_approval]

        assert len(held) == 1
        invoice = held[0]
        assert invoice.status is InvoiceStatus.DRAFT
        assert invoice.discount_reason
        # Above the demo hospital's 10% threshold, which is why it is held.
        assert invoice.discount_amount > invoice.subtotal * Decimal("0.10")
        assert invoice.total == invoice.subtotal + invoice.tax_amount - invoice.discount_amount
        assert hospital.settings["billing"]["discount_approval_threshold_percent"] == "10"

    async def test_an_existing_threshold_is_not_overwritten(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        hospital.settings = {
            "no_show_grace_minutes": 45,
            "billing": {"discount_approval_threshold_percent": "25", "default_tax_rate": "5"},
        }
        await db_session.flush()

        await seed_demo_data(db_session, hospital, {})

        assert hospital.settings == {
            "no_show_grace_minutes": 45,
            "billing": {"discount_approval_threshold_percent": "25", "default_tax_rate": "5"},
        }


class TestRelationships:
    """Seeded invoices point at rows that exist and agree with each other."""

    async def test_invoices_bill_the_patient_of_a_completed_appointment(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        linked = [i for i in await _invoices(db_session, hospital.id) if i.appointment_id]
        assert len(linked) == 4

        for invoice in linked:
            appointment = await db_session.get(Appointment, invoice.appointment_id)
            assert appointment is not None
            assert appointment.hospital_id == hospital.id
            assert appointment.patient_id == invoice.patient_id
            assert appointment.status is AppointmentStatus.COMPLETED

    async def test_no_appointment_has_two_live_invoices(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        invoices = await _invoices(db_session, hospital.id)

        live = [
            invoice.appointment_id
            for invoice in invoices
            if invoice.appointment_id and invoice.status is not InvoiceStatus.VOID
        ]
        voided = [i.appointment_id for i in invoices if i.status is InvoiceStatus.VOID]

        assert len(live) == len(set(live))
        # The void invoice's visit was re-issued: void + re-issue, business rule 3.
        assert set(voided) <= set(live)

    async def test_in_flight_appointments_are_left_unbilled(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        # So completing one in a demo drafts its invoice live.
        await seed_demo_data(db_session, hospital, {})

        result = await db_session.execute(
            select(Appointment.id).where(
                Appointment.hospital_id == hospital.id,
                Appointment.status != AppointmentStatus.COMPLETED,
            )
        )
        unfinished = set(result.scalars().all())
        billed = {i.appointment_id for i in await _invoices(db_session, hospital.id)}

        assert unfinished
        assert not unfinished & billed


class TestIdempotency:
    """Running the seed again must not duplicate, renumber, or re-point anything."""

    async def test_second_run_creates_nothing(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        first = await _counts(db_session, hospital.id)

        await seed_demo_data(db_session, hospital, {})
        second = await _counts(db_session, hospital.id)

        assert second == first
        assert first == {
            "services": len(SERVICES),
            "invoices": 8,
            "invoice_items": 14,
            "payments": 5,
            "refunds": 2,
        }

    async def test_second_run_does_not_advance_the_invoice_counter(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        # Re-running must not burn invoice numbers: the series is gap-free by
        # regulation (business rule 2), and a re-seed is not an invoice.
        await seed_demo_data(db_session, hospital, {})
        first = await _sequence_value(db_session, hospital.id)

        await seed_demo_data(db_session, hospital, {})

        assert await _sequence_value(db_session, hospital.id) == first

    async def test_second_run_keeps_the_same_invoices(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        first = {
            (i.id, i.invoice_number, i.status, i.total, i.amount_paid)
            for i in await _invoices(db_session, hospital.id)
        }

        await seed_demo_data(db_session, hospital, {})
        second = {
            (i.id, i.invoice_number, i.status, i.total, i.amount_paid)
            for i in await _invoices(db_session, hospital.id)
        }

        assert second == first

    async def test_seeding_a_second_hospital_does_not_touch_the_first(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital_id: uuid.UUID,
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        first = await _counts(db_session, hospital.id)

        other = await db_session.execute(select(Hospital).where(Hospital.id == other_hospital_id))
        await seed_demo_data(db_session, other.unique().scalar_one(), {})

        assert await _counts(db_session, hospital.id) == first
        # Each hospital gets its own catalog and its own number series from 1.
        assert await _counts(db_session, other_hospital_id) == first
        assert await _sequence_value(db_session, other_hospital_id) == await _sequence_value(
            db_session, hospital.id
        )
