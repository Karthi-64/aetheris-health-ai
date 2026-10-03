"""Integration tests for the Billing module.

Real database, real repositories, real services — only the audit sink is a
double (``docs/11-TESTING_STRATEGY.md`` §2.4).

Two halves:

- **The workflow** ``docs/modules/06-billing.md`` §16 asks for: appointment
  complete → invoice draft → issue → payment → status transitions. It runs
  through the *real* appointment service wired to the *real* billing sink, the
  same wiring ``app/api/dependencies/services.py`` ships.
- **The races** §14 describes, run as genuinely separate, really-committing
  transactions: two payments that together overpay, a retried payment, and a
  burst of concurrent issues. These are the guarantees that rest on row locks
  and unique indexes, so nothing short of a real database can test them.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import AppointmentStatus
from app.models.billing import (
    Invoice,
    InvoiceNumberSequence,
    InvoiceStatus,
    Payment,
    Service,
)
from app.models.hospital import Hospital
from app.models.patient import Patient
from app.models.user import User
from app.repositories.appointment_repository import AppointmentRepository
from app.repositories.doctor_repository import DoctorRepository
from app.repositories.hospital_repository import HospitalRepository
from app.repositories.invoice_number_sequence_repository import InvoiceNumberSequenceRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.service_catalog_repository import ServiceCatalogRepository
from app.services.appointment_service import AppointmentService
from app.services.billing_service import (
    BillingInvoiceDraftSink,
    BillingService,
    DuplicateAppointmentInvoiceError,
    InvalidInvoiceStateError,
    OverpaymentError,
)
from app.tests.billing_helpers import (
    insert_appointment,
    insert_doctor,
    insert_patient,
    insert_user,
)
from app.tests.conftest import RecordingAuditSink
from app.tests.factories import (
    build_create_invoice_request,
    build_record_payment_request,
    build_update_invoice_request,
    build_void_request,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.database


class _SlowLockInvoiceRepository(InvoiceRepository):
    """An invoice repository that lingers just after taking the row lock.

    Used only by the race tests. Pausing while the lock is held gives the
    competing task time to reach the same lock and genuinely wait on it.
    Without the pause the tasks could run end-to-end one after another, and a
    race test would pass without ever exercising contention — including
    against code that took no lock at all.
    """

    async def get_invoice_for_update(
        self, hospital_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Invoice | None:
        invoice = await super().get_invoice_for_update(hospital_id, invoice_id)
        await asyncio.sleep(0.05)
        return invoice


def _billing(
    session: AsyncSession, audit: RecordingAuditSink, *, contended: bool = False
) -> BillingService:
    """A fully wired :class:`BillingService` over ``session``.

    :param contended: Use :class:`_SlowLockInvoiceRepository`, for race tests.
    """
    return BillingService(
        _SlowLockInvoiceRepository(session) if contended else InvoiceRepository(session),
        InvoiceNumberSequenceRepository(session),
        ServiceCatalogRepository(session),
        PatientRepository(session),
        AppointmentRepository(session),
        DoctorRepository(session),
        HospitalRepository(session),
        session,
        audit,
    )


def _key() -> str:
    """A fresh idempotency key."""
    return f"pay-{uuid.uuid4().hex}"


# ── The workflow ────────────────────────────────────────────────────────────


@pytest.fixture
def billing(db_session: AsyncSession, audit_sink: RecordingAuditSink) -> BillingService:
    """Billing on the transactional test session."""
    return _billing(db_session, audit_sink)


@pytest.fixture
def appointments(
    db_session: AsyncSession, audit_sink: RecordingAuditSink, billing: BillingService
) -> AppointmentService:
    """The appointment service wired to the **real** billing sink.

    This is the wiring that ships now that Billing exists; until this sprint
    the sink was a null implementation that only logged.
    """
    return AppointmentService(
        AppointmentRepository(db_session),
        PatientRepository(db_session),
        DoctorRepository(db_session),
        HospitalRepository(db_session),
        db_session,
        audit_sink,
        BillingInvoiceDraftSink(billing),
    )


async def _in_progress_visit(
    session: AsyncSession, hospital_id: uuid.UUID, *, fee: str = "800.00"
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a patient mid-consultation and return ``(patient_id, appointment_id)``."""
    patient = await insert_patient(session, hospital_id)
    doctor = await insert_doctor(session, hospital_id, consultation_fee=fee)
    appointment = await insert_appointment(
        session,
        hospital_id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        status=AppointmentStatus.IN_PROGRESS,
    )
    return patient.id, appointment.id


class TestAppointmentToPaidInvoice:
    async def test_complete_draft_edit_issue_pay(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID,
        appointments: AppointmentService,
        billing: BillingService,
        audit_sink: RecordingAuditSink,
    ) -> None:
        patient_id, appointment_id = await _in_progress_visit(db_session, hospital_id)

        # 1. Completing the consultation drafts the invoice (spec §5.1).
        completed = await appointments.complete(hospital_id, appointment_id, actor_id=actor_id)
        assert completed.status is AppointmentStatus.COMPLETED

        page = await billing.list_invoices(hospital_id, patient_id=patient_id)
        assert page.total_records == 1
        draft = await billing.get_invoice(hospital_id, page.items[0].id)
        assert draft.status is InvoiceStatus.DRAFT
        assert draft.appointment_id == appointment_id
        assert draft.invoice_number is None
        assert [item.description for item in draft.items] == ["Consultation — Dr. Asha Menon"]
        assert draft.total == Decimal("800.00")

        # 2. Billing staff add a line to the draft (spec §5.2).
        consultation = draft.items[0]
        edited = await billing.update_invoice(
            hospital_id,
            draft.id,
            build_update_invoice_request(
                items=[
                    {
                        "description": consultation.description,
                        "unit_price": str(consultation.unit_price),
                    },
                    {"description": "ECG", "unit_price": "450.00"},
                ]
            ),
            actor_id=actor_id,
        )
        assert edited.total == Decimal("1250.00")

        # 3. Issue: a number is assigned and the invoice is frozen (spec §5.3).
        issued = await billing.issue_invoice(hospital_id, draft.id, actor_id=actor_id)
        assert issued.status is InvoiceStatus.ISSUED
        assert issued.invoice_number is not None
        assert issued.invoice_number.endswith("-000001")
        with pytest.raises(InvalidInvoiceStateError):
            await billing.update_invoice(
                hospital_id, draft.id, build_update_invoice_request(notes="too late")
            )

        # 4. A part payment, then the balance (spec §5.4, rule 11).
        part, _ = await billing.record_payment(
            hospital_id,
            draft.id,
            build_record_payment_request(amount="500.00", method="cash"),
            idempotency_key=_key(),
            actor_id=actor_id,
        )
        assert part.invoice.status is InvoiceStatus.PARTIALLY_PAID
        assert part.invoice.balance_due == Decimal("750.00")

        rest, _ = await billing.record_payment(
            hospital_id,
            draft.id,
            build_record_payment_request(amount="750.00", method="upi"),
            idempotency_key=_key(),
            actor_id=actor_id,
        )
        assert rest.invoice.status is InvoiceStatus.PAID
        assert rest.invoice.balance_due == Decimal("0.00")

        payments = await billing.list_payments(hospital_id, draft.id)
        assert [(p.amount, p.method.value) for p in payments] == [
            (Decimal("500.00"), "cash"),
            (Decimal("750.00"), "upi"),
        ]

        # Every mutation along the way left an audit event (CLAUDE.md rule 9).
        assert audit_sink.actions() == [
            "appointment.completed",
            "invoice.drafted",
            "invoice.updated",
            "invoice.issued",
            "invoice.payment_recorded",
            "invoice.payment_recorded",
        ]

    async def test_completion_does_not_duplicate_a_manual_invoice(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID,
        appointments: AppointmentService,
        billing: BillingService,
    ) -> None:
        patient_id, appointment_id = await _in_progress_visit(db_session, hospital_id)
        manual = await billing.create_invoice(
            hospital_id,
            build_create_invoice_request(
                patient_id=str(patient_id), appointment_id=str(appointment_id)
            ),
            actor_id=actor_id,
        )

        await appointments.complete(hospital_id, appointment_id, actor_id=actor_id)

        page = await billing.list_invoices(hospital_id, patient_id=patient_id)
        assert [invoice.id for invoice in page.items] == [manual.id]

    async def test_a_billing_failure_does_not_undo_the_consultation(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID,
        appointments: AppointmentService,
        billing: BillingService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        patient_id, appointment_id = await _in_progress_visit(db_session, hospital_id)

        async def broken(*_args: object, **_kwargs: object) -> None:
            msg = "billing is down"
            raise RuntimeError(msg)

        monkeypatch.setattr(billing, "draft_from_appointment", broken)

        completed = await appointments.complete(hospital_id, appointment_id, actor_id=actor_id)

        assert completed.status is AppointmentStatus.COMPLETED
        monkeypatch.undo()
        assert (await billing.list_invoices(hospital_id, patient_id=patient_id)).total_records == 0

    async def test_void_then_reissue_for_the_same_visit(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID,
        appointments: AppointmentService,
        billing: BillingService,
    ) -> None:
        # Business rule 3: corrections are a void plus a new invoice.
        patient_id, appointment_id = await _in_progress_visit(db_session, hospital_id)
        await appointments.complete(hospital_id, appointment_id, actor_id=actor_id)
        first = (await billing.list_invoices(hospital_id, patient_id=patient_id)).items[0]
        await billing.issue_invoice(hospital_id, first.id, actor_id=actor_id)

        # While the first is live, a second for the same visit is refused.
        with pytest.raises(DuplicateAppointmentInvoiceError):
            await billing.create_invoice(
                hospital_id,
                build_create_invoice_request(
                    patient_id=str(patient_id), appointment_id=str(appointment_id)
                ),
            )

        voided = await billing.void_invoice(
            hospital_id, first.id, build_void_request(), actor_id=actor_id
        )
        replacement = await billing.create_invoice(
            hospital_id,
            build_create_invoice_request(
                patient_id=str(patient_id), appointment_id=str(appointment_id)
            ),
            actor_id=actor_id,
        )
        reissued = await billing.issue_invoice(hospital_id, replacement.id, actor_id=actor_id)

        assert voided.status is InvoiceStatus.VOID
        # The void invoice keeps number 1; the replacement takes 2. No gap.
        assert voided.invoice_number is not None
        assert reissued.invoice_number is not None
        assert voided.invoice_number.endswith("-000001")
        assert reissued.invoice_number.endswith("-000002")

    async def test_hospital_tax_rate_applies_to_taxable_catalog_lines(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        billing: BillingService,
    ) -> None:
        hospital = await db_session.get(Hospital, hospital_id)
        assert hospital is not None
        hospital.settings = {"billing": {"default_tax_rate": "18"}}
        taxed = Service(
            hospital_id=hospital_id,
            code="COSM",
            name="Cosmetic procedure",
            price=Decimal("1000.00"),
        )
        exempt = Service(
            hospital_id=hospital_id,
            code="CONS",
            name="Consultation",
            price=Decimal("500.00"),
            taxable=False,
        )
        db_session.add_all([taxed, exempt])
        await db_session.flush()
        patient = await insert_patient(db_session, hospital_id)

        invoice = await billing.create_invoice(
            hospital_id,
            build_create_invoice_request(
                patient_id=str(patient.id),
                items=[{"service_id": str(taxed.id)}, {"service_id": str(exempt.id)}],
            ),
        )

        assert [(item.description, item.tax_rate) for item in invoice.items] == [
            ("Cosmetic procedure", Decimal("18.00")),
            ("Consultation", Decimal("0.00")),
        ]
        assert invoice.subtotal == Decimal("1500.00")
        assert invoice.tax_amount == Decimal("180.00")
        assert invoice.total == Decimal("1680.00")


# ── The races ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _World:
    """Ids of rows that are really committed for a concurrency test."""

    hospital_id: uuid.UUID
    patient_id: uuid.UUID
    cashier_id: uuid.UUID


@pytest.fixture
async def world(db_engine: AsyncEngine) -> AsyncGenerator[_World]:
    """A committed hospital, patient and cashier, removed again afterwards.

    These tests cannot use the rolled-back ``db_session`` fixture: proving that
    transactions serialize requires them to be genuinely separate and to really
    commit. Everything created under this hospital is deleted on teardown.
    """
    hospital_id = uuid.uuid4()
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        setup.add(
            Hospital(
                id=hospital_id,
                name="Billing Concurrency Test Hospital",
                slug=f"billing-race-{uuid.uuid4().hex[:12]}",
                address={"line1": "1 Test Road", "city": "Hyderabad", "country": "IN"},
                settings={},
            )
        )
        await setup.flush()
        patient = await insert_patient(setup, hospital_id)
        cashier = await insert_user(setup, hospital_id)
        await setup.commit()

    yield _World(hospital_id, patient.id, cashier.id)

    async with AsyncSession(db_engine) as cleanup:
        for model in (Payment, Invoice, InvoiceNumberSequence, Patient, User):
            await cleanup.execute(delete(model).where(model.hospital_id == hospital_id))
        await cleanup.execute(delete(Hospital).where(Hospital.id == hospital_id))
        await cleanup.commit()


async def _committed_draft(
    engine: AsyncEngine, world: _World, *, price: str = "500.00"
) -> uuid.UUID:
    """Create and commit a one-line draft, returning its id."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        invoice = await _billing(session, RecordingAuditSink()).create_invoice(
            world.hospital_id,
            build_create_invoice_request(
                patient_id=str(world.patient_id),
                items=[{"description": "Consultation", "unit_price": price}],
            ),
            actor_id=world.cashier_id,
        )
        return invoice.id


async def _committed_issued(
    engine: AsyncEngine, world: _World, *, price: str = "500.00"
) -> uuid.UUID:
    """Create, issue and commit an invoice, returning its id."""
    invoice_id = await _committed_draft(engine, world, price=price)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await _billing(session, RecordingAuditSink()).issue_invoice(
            world.hospital_id, invoice_id, actor_id=world.cashier_id
        )
    return invoice_id


class TestConcurrentPayments:
    async def test_two_payments_cannot_together_overpay(
        self, db_engine: AsyncEngine, world: _World
    ) -> None:
        # Business rule 9 under contention. Each payment alone fits the 500.00
        # balance; together they do not. Without the row lock both would read
        # a balance of 500.00 and both would be accepted.
        invoice_id = await _committed_issued(db_engine, world, price="500.00")

        async def pay() -> str:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                try:
                    await _billing(session, RecordingAuditSink(), contended=True).record_payment(
                        world.hospital_id,
                        invoice_id,
                        build_record_payment_request(amount="300.00", method="cash"),
                        idempotency_key=_key(),
                        actor_id=world.cashier_id,
                    )
                except OverpaymentError:
                    return "refused"
                return "taken"

        outcomes = await asyncio.gather(pay(), pay())

        assert sorted(outcomes) == ["refused", "taken"]
        async with AsyncSession(db_engine) as check:
            invoice = await check.get(Invoice, invoice_id)
            assert invoice is not None
            assert invoice.amount_paid == Decimal("300.00")
            assert invoice.status is InvoiceStatus.PARTIALLY_PAID
            recorded = await check.scalar(
                select(func.count()).select_from(Payment).where(Payment.invoice_id == invoice_id)
            )
            assert recorded == 1

    async def test_ac3_concurrent_retries_of_one_payment_are_taken_once(
        self, db_engine: AsyncEngine, world: _World
    ) -> None:
        # A client that timed out and retried while the first attempt was still
        # in flight: same key, same body, at the same time.
        invoice_id = await _committed_issued(db_engine, world, price="500.00")
        key = _key()

        async def pay() -> tuple[uuid.UUID, bool]:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                result, created = await _billing(
                    session, RecordingAuditSink(), contended=True
                ).record_payment(
                    world.hospital_id,
                    invoice_id,
                    build_record_payment_request(amount="500.00", method="upi"),
                    idempotency_key=key,
                    actor_id=world.cashier_id,
                )
                return result.payment.id, created

        results = await asyncio.gather(pay(), pay(), pay())

        # One attempt recorded the payment; the others replayed it.
        assert sorted(created for _, created in results) == [False, False, True]
        assert len({payment_id for payment_id, _ in results}) == 1
        async with AsyncSession(db_engine) as check:
            invoice = await check.get(Invoice, invoice_id)
            assert invoice is not None
            assert invoice.amount_paid == Decimal("500.00")
            assert invoice.status is InvoiceStatus.PAID
            recorded = await check.scalar(
                select(func.count()).select_from(Payment).where(Payment.invoice_id == invoice_id)
            )
            assert recorded == 1


class TestConcurrentIssue:
    async def test_ac2_concurrent_issues_get_gap_free_numbers(
        self, db_engine: AsyncEngine, world: _World
    ) -> None:
        count = 8
        invoice_ids = [await _committed_draft(db_engine, world) for _ in range(count)]

        async def issue(invoice_id: uuid.UUID) -> str:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                issued = await _billing(
                    session, RecordingAuditSink(), contended=True
                ).issue_invoice(world.hospital_id, invoice_id, actor_id=world.cashier_id)
                assert issued.invoice_number is not None
                return issued.invoice_number

        numbers = await asyncio.gather(*(issue(invoice_id) for invoice_id in invoice_ids))

        sequence = sorted(int(number.rsplit("-", 1)[1]) for number in numbers)
        assert sequence == list(range(1, count + 1))

    async def test_issuing_one_draft_twice_at_once_issues_it_once(
        self, db_engine: AsyncEngine, world: _World
    ) -> None:
        # Spec §14: "concurrent issue of same invoice from two tabs".
        invoice_id = await _committed_draft(db_engine, world)

        async def issue() -> str:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                try:
                    await _billing(session, RecordingAuditSink(), contended=True).issue_invoice(
                        world.hospital_id, invoice_id, actor_id=world.cashier_id
                    )
                except InvalidInvoiceStateError:
                    return "refused"
                return "issued"

        outcomes = await asyncio.gather(issue(), issue())

        assert sorted(outcomes) == ["issued", "refused"]
        async with AsyncSession(db_engine) as check:
            sequence = await check.get(InvoiceNumberSequence, world.hospital_id)
            assert sequence is not None
            # The refused tab did not consume a number.
            assert sequence.current_value == 1
