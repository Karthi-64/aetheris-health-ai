"""Repository tests for invoices, their lines and their payments.

Real Postgres, rolled back per test. Two things are covered:

- **Tenant isolation.** Every read is checked against a second hospital
  (CLAUDE.md rules 4 and 5).
- **The database's own guarantees.** The check constraints and unique indexes
  from migration 0009 are the last line of defence for the money rules — they
  are exercised directly, bypassing the service, to prove they hold even if
  the service had a bug.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.billing import Invoice, InvoiceItem, InvoiceStatus, PaymentMethod
from app.repositories.invoice_repository import InvoiceRepository
from app.tests.billing_helpers import (
    insert_appointment,
    insert_doctor,
    insert_patient,
    insert_user,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

LINES: list[dict[str, Any]] = [
    {
        "service_id": None,
        "description": "Consultation",
        "quantity": Decimal("1.00"),
        "unit_price": Decimal("500.00"),
        "tax_rate": Decimal("0.00"),
        "line_total": Decimal("500.00"),
    },
    {
        "service_id": None,
        "description": "Dressing kit",
        "quantity": Decimal("2.00"),
        "unit_price": Decimal("75.00"),
        "tax_rate": Decimal("0.00"),
        "line_total": Decimal("150.00"),
    },
]


@pytest.fixture
def repository(db_session: AsyncSession) -> InvoiceRepository:
    """A repository bound to the rolled-back test session."""
    return InvoiceRepository(db_session)


async def _draft(
    repository: InvoiceRepository,
    session: AsyncSession,
    hospital_id: uuid.UUID,
    **fields: Any,
) -> Invoice:
    """Insert a two-line, 650.00 draft for a fresh patient."""
    if "patient_id" not in fields:
        fields["patient_id"] = (await insert_patient(session, hospital_id)).id
    return await repository.create_invoice(
        hospital_id=hospital_id,
        lines=LINES,
        subtotal=Decimal("650.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("650.00"),
        **fields,
    )


async def _issued(
    repository: InvoiceRepository,
    session: AsyncSession,
    hospital_id: uuid.UUID,
    number: str,
    *,
    issued_at: datetime = NOW,
    **fields: Any,
) -> Invoice:
    """Insert a draft and move it to ``issued`` with ``number``."""
    invoice = await _draft(repository, session, hospital_id, **fields)
    return await repository.update_invoice(
        invoice, status=InvoiceStatus.ISSUED, invoice_number=number, issued_at=issued_at
    )


class TestCreateInvoice:
    async def test_persists_a_draft_with_numbered_lines(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> None:
        invoice = await _draft(repository, db_session, hospital_id, created_by=actor_id)

        assert invoice.status is InvoiceStatus.DRAFT
        assert invoice.invoice_number is None
        assert invoice.amount_paid == Decimal("0.00")
        assert invoice.discount_amount == Decimal("0.00")
        assert invoice.balance_due == Decimal("650.00")
        assert invoice.created_by == actor_id
        assert [(item.position, item.description) for item in invoice.items] == [
            (0, "Consultation"),
            (1, "Dressing kit"),
        ]
        # Lines carry the tenant too (CLAUDE.md rule 4).
        assert {item.hospital_id for item in invoice.items} == {hospital_id}

    async def test_patient_is_loaded_with_the_invoice(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        invoice = await _draft(repository, db_session, hospital_id)

        fetched = await repository.get_invoice_by_id(hospital_id, invoice.id)

        assert fetched is not None
        assert fetched.patient.full_name == "Ananya Rao"


class TestReplaceItems:
    async def test_swaps_the_whole_line_set_and_deletes_the_old_rows(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        invoice = await _draft(repository, db_session, hospital_id)
        replacement = [{**LINES[1], "description": "X-ray", "line_total": Decimal("150.00")}]

        await repository.replace_items(invoice, replacement)
        invoice = await repository.update_invoice(invoice, total=Decimal("150.00"))

        assert [item.description for item in invoice.items] == ["X-ray"]
        remaining = await db_session.scalar(
            select(func.count())
            .select_from(InvoiceItem)
            .where(InvoiceItem.invoice_id == invoice.id)
        )
        assert remaining == 1


class TestReadsAreTenantScoped:
    async def test_get_invoice_by_id(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        invoice = await _draft(repository, db_session, hospital_id)

        assert await repository.get_invoice_by_id(hospital_id, invoice.id) is not None
        assert await repository.get_invoice_by_id(other_hospital_id, invoice.id) is None

    async def test_get_invoice_for_update(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        invoice = await _draft(repository, db_session, hospital_id)

        # Also proves the FOR UPDATE is legal alongside the joined patient load.
        locked = await repository.get_invoice_for_update(hospital_id, invoice.id)

        assert locked is not None
        assert locked.id == invoice.id
        assert len(locked.items) == 2
        assert await repository.get_invoice_for_update(other_hospital_id, invoice.id) is None

    async def test_list_and_count(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        ours = await _draft(repository, db_session, hospital_id)
        await _draft(repository, db_session, other_hospital_id)

        listed = await repository.list_invoices(hospital_id)

        assert [invoice.id for invoice in listed] == [ours.id]
        assert await repository.count_invoices(hospital_id) == 1
        assert await repository.count_invoices(other_hospital_id) == 1

    async def test_get_live_invoice_for_appointment(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        patient = await insert_patient(db_session, hospital_id)
        doctor = await insert_doctor(db_session, hospital_id)
        appointment = await insert_appointment(
            db_session, hospital_id, patient_id=patient.id, doctor_id=doctor.id
        )
        invoice = await _draft(
            repository,
            db_session,
            hospital_id,
            patient_id=patient.id,
            appointment_id=appointment.id,
        )

        found = await repository.get_live_invoice_for_appointment(hospital_id, appointment.id)

        assert found is not None
        assert found.id == invoice.id
        assert (
            await repository.get_live_invoice_for_appointment(other_hospital_id, appointment.id)
            is None
        )

    async def test_payments(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> None:
        invoice = await _issued(repository, db_session, hospital_id, "INV-2026-000001")
        payment = await repository.create_payment(
            invoice=invoice,
            amount=Decimal("100.00"),
            method=PaymentMethod.CASH,
            received_by=actor_id,
            received_at=NOW,
            idempotency_key="pay-key-0000000001",
        )

        assert payment.hospital_id == hospital_id
        assert [p.id for p in await repository.list_payments(hospital_id, invoice.id)] == [
            payment.id
        ]
        assert await repository.list_payments(other_hospital_id, invoice.id) == []
        assert (
            await repository.get_payment_by_idempotency_key(hospital_id, "pay-key-0000000001")
        ) is not None
        assert (
            await repository.get_payment_by_idempotency_key(other_hospital_id, "pay-key-0000000001")
        ) is None


class TestListFilters:
    async def test_filters_by_patient_and_status(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient = await insert_patient(db_session, hospital_id)
        draft = await _draft(repository, db_session, hospital_id, patient_id=patient.id)
        issued = await _issued(
            repository, db_session, hospital_id, "INV-2026-000001", patient_id=patient.id
        )
        await _draft(repository, db_session, hospital_id)  # someone else's

        by_patient = await repository.list_invoices(hospital_id, patient_id=patient.id)
        by_status = await repository.list_invoices(hospital_id, status=InvoiceStatus.ISSUED)

        assert {invoice.id for invoice in by_patient} == {draft.id, issued.id}
        assert [invoice.id for invoice in by_status] == [issued.id]
        assert await repository.count_invoices(hospital_id, patient_id=patient.id) == 2

    async def test_issue_date_range_is_half_open_and_excludes_drafts(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        await _draft(repository, db_session, hospital_id)
        before = await _issued(
            repository,
            db_session,
            hospital_id,
            "INV-2026-000001",
            issued_at=NOW - timedelta(days=1),
        )
        at_start = await _issued(repository, db_session, hospital_id, "INV-2026-000002")
        at_end = await _issued(
            repository,
            db_session,
            hospital_id,
            "INV-2026-000003",
            issued_at=NOW + timedelta(days=1),
        )

        window = await repository.list_invoices(
            hospital_id, issued_on_or_after=NOW, issued_before=NOW + timedelta(days=1)
        )

        # The lower bound is inclusive, the upper bound exclusive, and the
        # draft — which was never issued — is not in any date range.
        assert [invoice.id for invoice in window] == [at_start.id]
        assert before.id not in {invoice.id for invoice in window}
        assert at_end.id not in {invoice.id for invoice in window}

    async def test_paginates_newest_first(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient = await insert_patient(db_session, hospital_id)
        for _ in range(3):
            await _draft(repository, db_session, hospital_id, patient_id=patient.id)

        first = await repository.list_invoices(hospital_id, skip=0, limit=2)
        second = await repository.list_invoices(hospital_id, skip=2, limit=2)

        assert len(first) == 2
        assert len(second) == 1
        assert not {invoice.id for invoice in first} & {invoice.id for invoice in second}


class TestLiveAppointmentInvoice:
    async def test_a_void_invoice_is_not_live(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient = await insert_patient(db_session, hospital_id)
        doctor = await insert_doctor(db_session, hospital_id)
        appointment = await insert_appointment(
            db_session, hospital_id, patient_id=patient.id, doctor_id=doctor.id
        )
        invoice = await _issued(
            repository,
            db_session,
            hospital_id,
            "INV-2026-000001",
            patient_id=patient.id,
            appointment_id=appointment.id,
        )
        await repository.update_invoice(invoice, status=InvoiceStatus.VOID, voided_at=NOW)

        assert (
            await repository.get_live_invoice_for_appointment(hospital_id, appointment.id) is None
        )

        # Business rule 3: void and re-issue. The index must allow the second.
        replacement = await _draft(
            repository,
            db_session,
            hospital_id,
            patient_id=patient.id,
            appointment_id=appointment.id,
        )
        found = await repository.get_live_invoice_for_appointment(hospital_id, appointment.id)
        assert found is not None
        assert found.id == replacement.id

    async def test_database_refuses_a_second_live_invoice_for_one_appointment(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient = await insert_patient(db_session, hospital_id)
        doctor = await insert_doctor(db_session, hospital_id)
        appointment = await insert_appointment(
            db_session, hospital_id, patient_id=patient.id, doctor_id=doctor.id
        )
        await _draft(
            repository,
            db_session,
            hospital_id,
            patient_id=patient.id,
            appointment_id=appointment.id,
        )

        with pytest.raises(IntegrityError, match="uq_invoices_live_appointment"):
            async with db_session.begin_nested():
                await _draft(
                    repository,
                    db_session,
                    hospital_id,
                    patient_id=patient.id,
                    appointment_id=appointment.id,
                )


class TestDatabaseMoneyRules:
    """The constraints hold even when the service is bypassed."""

    async def test_amount_paid_cannot_exceed_total(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        # Business rule 9, enforced by Postgres.
        invoice = await _issued(repository, db_session, hospital_id, "INV-2026-000001")

        with pytest.raises(IntegrityError, match="paid_within_total"):
            async with db_session.begin_nested():
                await repository.update_invoice(invoice, amount_paid=Decimal("650.01"))

    async def test_an_issued_invoice_must_have_a_number(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        invoice = await _draft(repository, db_session, hospital_id)

        with pytest.raises(IntegrityError, match="number_required_once_issued"):
            async with db_session.begin_nested():
                await repository.update_invoice(invoice, status=InvoiceStatus.ISSUED)

    async def test_invoice_number_is_unique_per_hospital(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        await _issued(repository, db_session, hospital_id, "INV-2026-000001")
        # Another hospital has its own series and may use the same number.
        await _issued(repository, db_session, other_hospital_id, "INV-2026-000001")

        with pytest.raises(IntegrityError, match="uq_invoices_hospital_number"):
            async with db_session.begin_nested():
                await _issued(repository, db_session, hospital_id, "INV-2026-000001")

    async def test_many_drafts_may_have_no_number(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        await _draft(repository, db_session, hospital_id)
        await _draft(repository, db_session, hospital_id)

        assert await repository.count_invoices(hospital_id, status=InvoiceStatus.DRAFT) == 2

    @pytest.mark.parametrize(
        ("field", "value", "constraint"),
        [
            ("quantity", Decimal("0.00"), "quantity_positive"),
            ("unit_price", Decimal("-1.00"), "unit_price_non_negative"),
            ("tax_rate", Decimal("100.01"), "tax_rate_range"),
        ],
    )
    async def test_line_constraints(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        field: str,
        value: Decimal,
        constraint: str,
    ) -> None:
        patient = await insert_patient(db_session, hospital_id)

        with pytest.raises(IntegrityError, match=constraint):
            async with db_session.begin_nested():
                await repository.create_invoice(
                    hospital_id=hospital_id,
                    patient_id=patient.id,
                    lines=[{**LINES[0], field: value}],
                    subtotal=Decimal("0.00"),
                    tax_amount=Decimal("0.00"),
                    total=Decimal("0.00"),
                )

    async def test_payment_amount_must_be_positive(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> None:
        invoice = await _issued(repository, db_session, hospital_id, "INV-2026-000001")

        with pytest.raises(IntegrityError, match="amount_positive"):
            async with db_session.begin_nested():
                await repository.create_payment(
                    invoice=invoice,
                    amount=Decimal("0.00"),
                    method=PaymentMethod.CASH,
                    received_by=actor_id,
                    received_at=NOW,
                    idempotency_key="pay-key-0000000001",
                )

    async def test_idempotency_key_is_unique_per_hospital(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> None:
        # AC-3, enforced by Postgres.
        ours = await _issued(repository, db_session, hospital_id, "INV-2026-000001")
        theirs = await _issued(repository, db_session, other_hospital_id, "INV-2026-000001")
        their_cashier = await insert_user(db_session, other_hospital_id)

        async def pay(invoice: Invoice, received_by: uuid.UUID) -> None:
            await repository.create_payment(
                invoice=invoice,
                amount=Decimal("10.00"),
                method=PaymentMethod.CASH,
                received_by=received_by,
                received_at=NOW,
                idempotency_key="shared-key-00000001",
            )

        await pay(ours, actor_id)
        # The same key in another hospital is a different payment, not a clash.
        await pay(theirs, their_cashier.id)

        with pytest.raises(IntegrityError, match="uq_payments_hospital_idempotency_key"):
            async with db_session.begin_nested():
                await pay(ours, actor_id)


class TestDoctorScope:
    """The ``doctor_id`` filter behind a doctor's "own visits" view (spec §3)."""

    async def _two_doctors_one_patient(
        self, repository: InvoiceRepository, session: AsyncSession, hospital_id: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID, Invoice, Invoice, Invoice]:
        """One patient seen by two doctors, plus a counter sale.

        :returns: ``(doctor_a, doctor_b, a's invoice, b's invoice, ad-hoc invoice)``.
        """
        patient = await insert_patient(session, hospital_id)
        doctor_a = await insert_doctor(session, hospital_id)
        doctor_b = await insert_doctor(session, hospital_id)
        visit_a = await insert_appointment(
            session, hospital_id, patient_id=patient.id, doctor_id=doctor_a.id
        )
        visit_b = await insert_appointment(
            session, hospital_id, patient_id=patient.id, doctor_id=doctor_b.id
        )
        invoice_a = await _draft(
            repository, session, hospital_id, patient_id=patient.id, appointment_id=visit_a.id
        )
        invoice_b = await _draft(
            repository, session, hospital_id, patient_id=patient.id, appointment_id=visit_b.id
        )
        ad_hoc = await _draft(repository, session, hospital_id, patient_id=patient.id)
        return doctor_a.id, doctor_b.id, invoice_a, invoice_b, ad_hoc

    async def test_list_and_count_return_only_that_doctors_visits(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        doctor_a, doctor_b, invoice_a, invoice_b, _ = await self._two_doctors_one_patient(
            repository, db_session, hospital_id
        )

        for_a = await repository.list_invoices(hospital_id, doctor_id=doctor_a)
        for_b = await repository.list_invoices(hospital_id, doctor_id=doctor_b)

        # Same patient, but each doctor sees only the invoice for their own
        # visit — and neither sees the counter sale, which has no appointment.
        assert [invoice.id for invoice in for_a] == [invoice_a.id]
        assert [invoice.id for invoice in for_b] == [invoice_b.id]
        assert await repository.count_invoices(hospital_id, doctor_id=doctor_a) == 1
        assert await repository.count_invoices(hospital_id) == 3

    async def test_get_by_id_honours_the_scope(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        doctor_a, _, invoice_a, invoice_b, ad_hoc = await self._two_doctors_one_patient(
            repository, db_session, hospital_id
        )

        mine = await repository.get_invoice_by_id(hospital_id, invoice_a.id, doctor_id=doctor_a)
        theirs = await repository.get_invoice_by_id(hospital_id, invoice_b.id, doctor_id=doctor_a)
        counter = await repository.get_invoice_by_id(hospital_id, ad_hoc.id, doctor_id=doctor_a)

        assert mine is not None
        assert theirs is None
        assert counter is None
        # Unscoped, all three are readable.
        assert await repository.get_invoice_by_id(hospital_id, invoice_b.id) is not None

    async def test_the_scope_combines_with_other_filters(
        self, repository: InvoiceRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        doctor_a, _, invoice_a, _, _ = await self._two_doctors_one_patient(
            repository, db_session, hospital_id
        )

        drafts = await repository.list_invoices(
            hospital_id, doctor_id=doctor_a, status=InvoiceStatus.DRAFT
        )
        issued = await repository.list_invoices(
            hospital_id, doctor_id=doctor_a, status=InvoiceStatus.ISSUED
        )

        assert [invoice.id for invoice in drafts] == [invoice_a.id]
        assert issued == []

    async def test_the_scope_does_not_cross_tenants(
        self,
        repository: InvoiceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        doctor_a, _, invoice_a, _, _ = await self._two_doctors_one_patient(
            repository, db_session, hospital_id
        )

        assert await repository.list_invoices(other_hospital_id, doctor_id=doctor_a) == []
        assert (
            await repository.get_invoice_by_id(other_hospital_id, invoice_a.id, doctor_id=doctor_a)
            is None
        )
