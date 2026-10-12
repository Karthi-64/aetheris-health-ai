"""Unit tests for the billing service.

Repositories are mocked; no database. What is tested here is the service's
own decisions: which state allows which action, what the totals come to, when
a payment is a replay, and that nothing is written when a rule refuses the
request. The guarantees that only a database can give — the row lock, the
gap-free counter, the unique indexes — are tested against real Postgres in the
repository and integration suites.

``backend/CLAUDE.md`` sets a 100% coverage floor on ``billing_service.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.charges import Charge, ChargeSink
from app.core.exceptions import BusinessRuleError, ConfigurationError, ValidationError
from app.models.billing import InvoiceStatus, PaymentMethod
from app.services.appointment_service import InvoiceDraftSink
from app.services.billing_service import (
    DEFAULT_CURRENCY,
    DEFAULT_TAX_RATE,
    BillingInvoiceDraftSink,
    BillingService,
    CashOnlyPaymentError,
    DiscountAwaitingApprovalError,
    DuplicateAppointmentInvoiceError,
    IdempotencyKeyReuseError,
    InvalidInvoiceStateError,
    InvoiceNotFoundError,
    NoDiscountToApproveError,
    OverpaymentError,
    RefundExceedsPaidError,
)
from app.tests.conftest import FakeSession, RecordingAuditSink
from app.tests.factories import (
    build_create_invoice_request,
    build_invoice_item_model,
    build_invoice_model,
    build_payment_model,
    build_record_payment_request,
    build_record_refund_request,
    build_refund_model,
    build_service_model,
    build_update_invoice_request,
    build_void_request,
)

HOSPITAL_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()
PATIENT_ID = uuid.uuid4()
KEY = "pay-key-0000000001"
TEMPLATE = "INV-{year}-{seq:06d}"


def _hospital(
    settings: dict[str, Any] | None = None,
    *,
    timezone: str = "Asia/Kolkata",
    currency: str = "INR",
) -> MagicMock:
    """A hospital double with the three things billing reads from it."""
    hospital = MagicMock()
    hospital.timezone = timezone
    hospital.currency = currency
    hospital.settings = settings if settings is not None else {}
    return hospital


def _integrity_error(constraint: str) -> IntegrityError:
    """An IntegrityError whose driver message names ``constraint``."""
    return IntegrityError("INSERT", {}, Exception(f'violates unique constraint "{constraint}"'))


def _invoice_repo() -> AsyncMock:
    """A mocked invoice repository that behaves like a tiny in-memory store.

    ``update_invoice`` and ``replace_items`` apply their arguments to the
    instance they are given, and ``create_invoice`` builds one, so a test can
    assert on the invoice the service returns rather than on call arguments.
    """
    repo = AsyncMock()

    async def create_invoice(*, lines: list[dict[str, Any]], **fields: Any) -> Any:
        fields.pop("created_by", None)
        hospital_id = fields["hospital_id"]
        items = [
            build_invoice_item_model(hospital_id=hospital_id, position=position, **line)
            for position, line in enumerate(lines)
        ]
        return build_invoice_model(items=items, **fields)

    async def update_invoice(invoice: Any, *, updated_by: Any = None, **fields: Any) -> Any:
        for name, value in fields.items():
            setattr(invoice, name, value)
        return invoice

    async def replace_items(invoice: Any, lines: list[dict[str, Any]]) -> None:
        invoice.items = [
            build_invoice_item_model(hospital_id=invoice.hospital_id, position=position, **line)
            for position, line in enumerate(lines)
        ]

    async def create_payment(*, invoice: Any, **fields: Any) -> Any:
        return build_payment_model(hospital_id=invoice.hospital_id, invoice_id=invoice.id, **fields)

    async def append_items(invoice: Any, lines: list[dict[str, Any]]) -> None:
        start = len(invoice.items)
        invoice.items = [
            *invoice.items,
            *(
                build_invoice_item_model(
                    hospital_id=invoice.hospital_id, position=start + offset, **line
                )
                for offset, line in enumerate(lines)
            ),
        ]

    repo.append_items.side_effect = append_items
    repo.create_invoice.side_effect = create_invoice
    repo.update_invoice.side_effect = update_invoice
    repo.replace_items.side_effect = replace_items

    async def create_refund(*, invoice: Any, **fields: Any) -> Any:
        return build_refund_model(hospital_id=invoice.hospital_id, invoice_id=invoice.id, **fields)

    repo.create_payment.side_effect = create_payment
    repo.create_refund.side_effect = create_refund
    repo.get_payment_by_idempotency_key.return_value = None
    repo.get_refund_by_idempotency_key.return_value = None
    repo.get_live_invoice_for_appointment.return_value = None
    return repo


def _make_service(
    invoices: AsyncMock,
    *,
    sequences: AsyncMock | None = None,
    catalog: AsyncMock | None = None,
    patients: AsyncMock | None = None,
    appointments: AsyncMock | None = None,
    doctors: AsyncMock | None = None,
    hospitals: AsyncMock | None = None,
    notifier: AsyncMock | None = None,
) -> tuple[BillingService, FakeSession, RecordingAuditSink]:
    """Assemble a service over mocked collaborators, defaulting to a valid world."""
    session = FakeSession()
    audit = RecordingAuditSink()

    if sequences is None:
        sequences = AsyncMock()
        sequences.advance.return_value = (42, TEMPLATE)
    if catalog is None:
        catalog = AsyncMock()
        catalog.get_services_by_ids.return_value = []
    if patients is None:
        patients = AsyncMock()
        patients.get_patient_by_id.return_value = MagicMock()
    if appointments is None:
        appointments = AsyncMock()
    if doctors is None:
        doctors = AsyncMock()
    if hospitals is None:
        hospitals = AsyncMock()
        hospitals.get_by_id.return_value = _hospital()

    service = BillingService(
        invoices,
        sequences,
        catalog,
        patients,
        appointments,
        doctors,
        hospitals,
        session,  # type: ignore[arg-type]
        audit,
        notifier=notifier,
    )
    return service, session, audit


def _issued(**overrides: Any) -> Any:
    """An issued, unpaid 500.00 invoice."""
    values: dict[str, Any] = {
        "hospital_id": HOSPITAL_ID,
        "status": InvoiceStatus.ISSUED,
        "invoice_number": "INV-2026-000007",
        "issued_at": datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return build_invoice_model(**values)


def _appointment(*, fee: str = "800.00", patient_id: uuid.UUID = PATIENT_ID) -> MagicMock:
    """An appointment double carrying the doctor's fee and name."""
    appointment = MagicMock()
    appointment.patient_id = patient_id
    appointment.doctor.consultation_fee = Decimal(fee)
    appointment.doctor.user.first_name = "Priya"
    appointment.doctor.user.last_name = "Sharma"
    return appointment


@pytest.fixture
def repo() -> AsyncMock:
    """A mocked invoice repository."""
    return _invoice_repo()


# ── Drafting ────────────────────────────────────────────────────────────────


class TestCreateInvoice:
    async def test_creates_a_draft_with_computed_totals(self, repo: AsyncMock) -> None:
        service, session, audit = _make_service(repo)
        payload = build_create_invoice_request(
            patient_id=str(PATIENT_ID),
            items=[
                {"description": "Dressing kit", "quantity": "2", "unit_price": "75.00"},
                {"description": "Injection", "quantity": "1", "unit_price": "120.50"},
            ],
        )

        result = await service.create_invoice(HOSPITAL_ID, payload, actor_id=ACTOR_ID)

        assert result.status is InvoiceStatus.DRAFT
        assert result.invoice_number is None
        assert result.subtotal == Decimal("270.50")
        assert result.tax_amount == Decimal("0.00")
        assert result.total == Decimal("270.50")
        assert [item.line_total for item in result.items] == [
            Decimal("150.00"),
            Decimal("120.50"),
        ]
        assert result.currency == "INR"
        assert session.commits == 1
        assert audit.actions() == ["invoice.drafted"]
        assert audit.last().context["source"] == "manual"

    async def test_an_empty_draft_totals_zero(self, repo: AsyncMock) -> None:
        service, _, _ = _make_service(repo)

        result = await service.create_invoice(
            HOSPITAL_ID, build_create_invoice_request(patient_id=str(PATIENT_ID), items=[])
        )

        assert result.items == []
        assert result.total == Decimal("0.00")

    async def test_catalog_line_is_priced_from_the_service(self, repo: AsyncMock) -> None:
        catalog_service = build_service_model(
            hospital_id=HOSPITAL_ID, name="ECG", price=Decimal("450.00"), taxable=False
        )
        catalog = AsyncMock()
        catalog.get_services_by_ids.return_value = [catalog_service]
        service, _, _ = _make_service(repo, catalog=catalog)
        payload = build_create_invoice_request(
            patient_id=str(PATIENT_ID),
            items=[{"service_id": str(catalog_service.id), "quantity": "2"}],
        )

        result = await service.create_invoice(HOSPITAL_ID, payload)

        line = result.items[0]
        assert line.service_id == catalog_service.id
        assert line.description == "ECG"
        assert line.unit_price == Decimal("450.00")
        assert line.line_total == Decimal("900.00")

    async def test_catalog_line_description_can_be_overridden(self, repo: AsyncMock) -> None:
        catalog_service = build_service_model(hospital_id=HOSPITAL_ID, name="ECG")
        catalog = AsyncMock()
        catalog.get_services_by_ids.return_value = [catalog_service]
        service, _, _ = _make_service(repo, catalog=catalog)
        payload = build_create_invoice_request(
            patient_id=str(PATIENT_ID),
            items=[{"service_id": str(catalog_service.id), "description": "ECG (repeat)"}],
        )

        result = await service.create_invoice(HOSPITAL_ID, payload)

        assert result.items[0].description == "ECG (repeat)"

    async def test_taxable_lines_use_the_hospital_rate(self, repo: AsyncMock) -> None:
        taxed = build_service_model(
            hospital_id=HOSPITAL_ID,
            name="Cosmetic procedure",
            price=Decimal("1000.00"),
            taxable=True,
        )
        catalog = AsyncMock()
        catalog.get_services_by_ids.return_value = [taxed]
        hospitals = AsyncMock()
        hospitals.get_by_id.return_value = _hospital({"billing": {"default_tax_rate": "18"}})
        service, _, _ = _make_service(repo, catalog=catalog, hospitals=hospitals)
        payload = build_create_invoice_request(
            patient_id=str(PATIENT_ID),
            items=[
                {"service_id": str(taxed.id)},
                {"description": "Taxed extra", "unit_price": "100.00", "taxable": True},
                {"description": "Untaxed extra", "unit_price": "100.00"},
            ],
        )

        result = await service.create_invoice(HOSPITAL_ID, payload)

        assert [item.tax_rate for item in result.items] == [
            Decimal(18),
            Decimal(18),
            Decimal("0.00"),
        ]
        assert result.subtotal == Decimal("1200.00")
        assert result.tax_amount == Decimal("198.00")
        assert result.total == Decimal("1398.00")

    async def test_unknown_patient_is_rejected_before_any_write(self, repo: AsyncMock) -> None:
        patients = AsyncMock()
        patients.get_patient_by_id.return_value = None
        service, session, audit = _make_service(repo, patients=patients)

        with pytest.raises(ValidationError) as excinfo:
            await service.create_invoice(HOSPITAL_ID, build_create_invoice_request())

        assert excinfo.value.detail["errors"][0]["field"] == "patient_id"
        repo.create_invoice.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_unknown_service_names_the_offending_line(self, repo: AsyncMock) -> None:
        service, _, _ = _make_service(repo)
        payload = build_create_invoice_request(
            items=[
                {"description": "Dressing kit", "unit_price": "75.00"},
                {"service_id": str(uuid.uuid4())},
            ]
        )

        with pytest.raises(ValidationError) as excinfo:
            await service.create_invoice(HOSPITAL_ID, payload)

        assert excinfo.value.detail["errors"][0]["field"] == "items.1.service_id"
        repo.create_invoice.assert_not_awaited()

    async def test_inactive_service_cannot_be_billed(self, repo: AsyncMock) -> None:
        retired = build_service_model(hospital_id=HOSPITAL_ID, code="OLD", is_active=False)
        catalog = AsyncMock()
        catalog.get_services_by_ids.return_value = [retired]
        service, _, _ = _make_service(repo, catalog=catalog)

        with pytest.raises(ValidationError, match="'OLD' is inactive"):
            await service.create_invoice(
                HOSPITAL_ID,
                build_create_invoice_request(items=[{"service_id": str(retired.id)}]),
            )


class TestCreateInvoiceForAppointment:
    async def test_links_the_appointment(self, repo: AsyncMock) -> None:
        appointment_id = uuid.uuid4()
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = _appointment()
        service, _, audit = _make_service(repo, appointments=appointments)
        payload = build_create_invoice_request(
            patient_id=str(PATIENT_ID), appointment_id=str(appointment_id)
        )

        result = await service.create_invoice(HOSPITAL_ID, payload)

        assert result.appointment_id == appointment_id
        assert audit.last().context["appointment_id"] == str(appointment_id)

    async def test_unknown_appointment_is_rejected(self, repo: AsyncMock) -> None:
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = None
        service, _, _ = _make_service(repo, appointments=appointments)

        with pytest.raises(ValidationError) as excinfo:
            await service.create_invoice(
                HOSPITAL_ID,
                build_create_invoice_request(appointment_id=str(uuid.uuid4())),
            )

        assert excinfo.value.detail["errors"][0]["field"] == "appointment_id"

    async def test_another_patients_appointment_is_rejected(self, repo: AsyncMock) -> None:
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = _appointment(patient_id=uuid.uuid4())
        service, _, _ = _make_service(repo, appointments=appointments)

        with pytest.raises(ValidationError, match="different patient"):
            await service.create_invoice(
                HOSPITAL_ID,
                build_create_invoice_request(
                    patient_id=str(PATIENT_ID), appointment_id=str(uuid.uuid4())
                ),
            )

    async def test_an_appointment_gets_only_one_live_invoice(self, repo: AsyncMock) -> None:
        existing = build_invoice_model(hospital_id=HOSPITAL_ID)
        repo.get_live_invoice_for_appointment.return_value = existing
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = _appointment()
        service, _, _ = _make_service(repo, appointments=appointments)

        with pytest.raises(DuplicateAppointmentInvoiceError) as excinfo:
            await service.create_invoice(
                HOSPITAL_ID,
                build_create_invoice_request(
                    patient_id=str(PATIENT_ID), appointment_id=str(uuid.uuid4())
                ),
            )

        assert excinfo.value.status_code == 409
        assert excinfo.value.detail["invoice_id"] == str(existing.id)
        repo.create_invoice.assert_not_awaited()

    async def test_a_lost_race_on_the_index_is_a_409(self, repo: AsyncMock) -> None:
        repo.create_invoice.side_effect = _integrity_error("uq_invoices_live_appointment")
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = _appointment()
        service, session, _ = _make_service(repo, appointments=appointments)

        with pytest.raises(DuplicateAppointmentInvoiceError):
            await service.create_invoice(
                HOSPITAL_ID,
                build_create_invoice_request(
                    patient_id=str(PATIENT_ID), appointment_id=str(uuid.uuid4())
                ),
            )

        assert session.savepoints_rolled_back == 1
        assert session.commits == 0

    async def test_any_other_integrity_error_is_not_swallowed(self, repo: AsyncMock) -> None:
        repo.create_invoice.side_effect = _integrity_error("ck_invoices_amounts_non_negative")
        service, _, _ = _make_service(repo)

        with pytest.raises(IntegrityError):
            await service.create_invoice(
                HOSPITAL_ID, build_create_invoice_request(patient_id=str(PATIENT_ID))
            )


class TestDraftFromAppointment:
    async def test_drafts_one_consultation_line_at_the_doctors_fee(self, repo: AsyncMock) -> None:
        appointment_id = uuid.uuid4()
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = _appointment(fee="800.00")
        service, session, audit = _make_service(repo, appointments=appointments)

        result = await service.draft_from_appointment(
            HOSPITAL_ID, appointment_id, actor_id=ACTOR_ID
        )

        assert result is not None
        assert result.status is InvoiceStatus.DRAFT
        assert result.appointment_id == appointment_id
        assert result.patient_id == PATIENT_ID
        assert len(result.items) == 1
        assert result.items[0].description == "Consultation — Dr. Priya Sharma"
        assert result.items[0].service_id is None
        assert result.items[0].tax_rate == Decimal("0.00")
        assert result.total == Decimal("800.00")
        assert session.commits == 1
        assert audit.actions() == ["invoice.drafted"]
        assert audit.last().context["source"] == "appointment"

    async def test_unknown_appointment_drafts_nothing(self, repo: AsyncMock) -> None:
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = None
        service, session, audit = _make_service(repo, appointments=appointments)

        assert await service.draft_from_appointment(HOSPITAL_ID, uuid.uuid4()) is None
        repo.create_invoice.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_is_idempotent_when_the_visit_is_already_invoiced(self, repo: AsyncMock) -> None:
        repo.get_live_invoice_for_appointment.return_value = build_invoice_model()
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = _appointment()
        service, session, audit = _make_service(repo, appointments=appointments)

        assert await service.draft_from_appointment(HOSPITAL_ID, uuid.uuid4()) is None
        repo.create_invoice.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def _complete_with(
        self, repo: AsyncMock, existing: Any, *, locked: Any = None
    ) -> tuple[Any, FakeSession, RecordingAuditSink, uuid.UUID]:
        """Complete a visit whose live invoice is ``existing``."""
        appointment_id = existing.appointment_id
        repo.get_live_invoice_for_appointment.return_value = existing
        repo.get_invoice_for_update.return_value = existing if locked is None else locked
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = _appointment(fee="800.00")
        service, session, audit = _make_service(repo, appointments=appointments)

        result = await service.draft_from_appointment(
            HOSPITAL_ID, appointment_id, actor_id=ACTOR_ID
        )
        return result, session, audit, appointment_id

    async def test_the_fee_joins_a_draft_a_charge_raised_mid_visit(self, repo: AsyncMock) -> None:
        # A lab test ordered during the consultation raised the visit's draft
        # before the visit completed. The fee must not be lost to it.
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID, appointment_id=uuid.uuid4(), consultation_fee_pending=True
        )

        result, session, audit, _ = await self._complete_with(repo, draft)

        assert result is not None
        assert result.id == draft.id
        assert [item.description for item in result.items][-1] == "Consultation — Dr. Priya Sharma"
        # 500.00 already there, plus the 800.00 fee.
        assert result.total == Decimal("1300.00")
        assert draft.consultation_fee_pending is False
        repo.create_invoice.assert_not_awaited()
        assert session.commits == 1
        assert audit.actions() == ["invoice.charges_added"]
        assert audit.last().context["source"] == "appointment"

    async def test_the_fee_is_added_to_a_charge_raised_draft_only_once(
        self, repo: AsyncMock
    ) -> None:
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID, appointment_id=uuid.uuid4(), consultation_fee_pending=True
        )
        await self._complete_with(repo, draft)

        result, _, audit, _ = await self._complete_with(repo, draft)

        assert result is None
        assert draft.total == Decimal("1300.00")
        assert audit.events == []

    async def test_a_second_completion_that_waited_for_the_lock_adds_nothing(
        self, repo: AsyncMock
    ) -> None:
        appointment_id = uuid.uuid4()
        stale = build_invoice_model(
            hospital_id=HOSPITAL_ID, appointment_id=appointment_id, consultation_fee_pending=True
        )
        settled = build_invoice_model(hospital_id=HOSPITAL_ID, appointment_id=appointment_id)

        result, session, audit, _ = await self._complete_with(repo, stale, locked=settled)

        assert result is None
        repo.append_items.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_a_charge_raised_invoice_already_issued_gets_the_fee_on_its_own_draft(
        self, repo: AsyncMock
    ) -> None:
        # AC-1: an issued invoice's lines are frozen.
        issued = _issued(appointment_id=uuid.uuid4(), consultation_fee_pending=True)

        result, session, audit, _ = await self._complete_with(repo, issued)

        assert result is not None
        assert result.id != issued.id
        # Only one live invoice may point at a visit.
        assert result.appointment_id is None
        assert result.total == Decimal("800.00")
        assert issued.total == Decimal("500.00")
        assert issued.consultation_fee_pending is False
        repo.append_items.assert_not_awaited()
        assert session.commits == 1
        assert audit.actions() == ["invoice.drafted"]

    async def test_a_charge_raised_draft_voided_meanwhile_is_replaced(
        self, repo: AsyncMock
    ) -> None:
        appointment_id = uuid.uuid4()
        stale = build_invoice_model(
            hospital_id=HOSPITAL_ID, appointment_id=appointment_id, consultation_fee_pending=True
        )
        voided = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            appointment_id=appointment_id,
            consultation_fee_pending=True,
            status=InvoiceStatus.VOID,
        )

        result, _, audit, _ = await self._complete_with(repo, stale, locked=voided)

        assert result is not None
        assert result.appointment_id == appointment_id
        assert result.total == Decimal("800.00")
        assert audit.actions() == ["invoice.drafted"]

    async def test_losing_a_race_to_a_manual_draft_drafts_nothing(self, repo: AsyncMock) -> None:
        repo.create_invoice.side_effect = _integrity_error("uq_invoices_live_appointment")
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = _appointment()
        service, session, audit = _make_service(repo, appointments=appointments)

        assert await service.draft_from_appointment(HOSPITAL_ID, uuid.uuid4()) is None
        assert session.commits == 0
        assert audit.events == []


class TestUpdateInvoice:
    async def test_replacing_items_recomputes_totals(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)
        repo.get_invoice_for_update.return_value = draft
        service, session, audit = _make_service(repo)

        result = await service.update_invoice(
            HOSPITAL_ID,
            draft.id,
            build_update_invoice_request(
                items=[{"description": "X-ray", "quantity": "1", "unit_price": "1250.00"}]
            ),
            actor_id=ACTOR_ID,
        )

        assert [item.description for item in result.items] == ["X-ray"]
        assert result.subtotal == result.total == Decimal("1250.00")
        assert session.commits == 1
        assert audit.actions() == ["invoice.updated"]
        assert audit.last().context["changed"] == ["items"]

    async def test_notes_only_leaves_the_lines_alone(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)
        repo.get_invoice_for_update.return_value = draft
        service, _, audit = _make_service(repo)

        result = await service.update_invoice(
            HOSPITAL_ID, draft.id, build_update_invoice_request(notes="Corrected")
        )

        assert result.notes == "Corrected"
        assert result.total == Decimal("500.00")
        repo.replace_items.assert_not_awaited()
        assert audit.last().context["changed"] == ["notes"]

    async def test_notes_can_be_cleared(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID, notes="Old note")
        repo.get_invoice_for_update.return_value = draft
        service, _, _ = _make_service(repo)

        result = await service.update_invoice(
            HOSPITAL_ID, draft.id, build_update_invoice_request(notes=None)
        )

        assert result.notes is None

    @pytest.mark.parametrize(
        "status",
        [
            InvoiceStatus.ISSUED,
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.PAID,
            InvoiceStatus.VOID,
        ],
    )
    async def test_an_issued_invoice_cannot_be_edited(
        self, repo: AsyncMock, status: InvoiceStatus
    ) -> None:
        # AC-1 / business rule 3.
        repo.get_invoice_for_update.return_value = _issued(status=status)
        service, session, audit = _make_service(repo)

        with pytest.raises(InvalidInvoiceStateError) as excinfo:
            await service.update_invoice(
                HOSPITAL_ID, uuid.uuid4(), build_update_invoice_request(notes="x")
            )

        assert excinfo.value.status_code == 400
        assert excinfo.value.detail["current_status"] == status.value
        repo.update_invoice.assert_not_awaited()
        repo.replace_items.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_unknown_invoice_is_a_404(self, repo: AsyncMock) -> None:
        repo.get_invoice_for_update.return_value = None
        service, _, _ = _make_service(repo)

        with pytest.raises(InvoiceNotFoundError):
            await service.update_invoice(
                HOSPITAL_ID, uuid.uuid4(), build_update_invoice_request(notes="x")
            )


# ── Lifecycle ───────────────────────────────────────────────────────────────


class TestIssueInvoice:
    async def test_issues_a_draft_with_the_next_number(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)
        repo.get_invoice_for_update.return_value = draft
        sequences = AsyncMock()
        sequences.advance.return_value = (42, TEMPLATE)
        service, session, audit = _make_service(repo, sequences=sequences)

        result = await service.issue_invoice(HOSPITAL_ID, draft.id, actor_id=ACTOR_ID)

        assert result.status is InvoiceStatus.ISSUED
        assert result.invoice_number == f"INV-{result.issued_at.year}-000042"  # type: ignore[union-attr]
        assert result.issued_at is not None
        sequences.advance.assert_awaited_once_with(HOSPITAL_ID)
        assert session.commits == 1
        assert audit.actions() == ["invoice.issued"]
        assert audit.last().changes == {"status": {"before": "draft", "after": "issued"}}

    async def test_totals_are_recomputed_from_the_lines_at_issue(self, repo: AsyncMock) -> None:
        # The stored totals are stale on purpose: issue must not trust them.
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            subtotal=Decimal("1.00"),
            total=Decimal("1.00"),
            items=[
                build_invoice_item_model(
                    quantity=Decimal(2),
                    unit_price=Decimal("100.00"),
                    tax_rate=Decimal(5),
                    line_total=Decimal("0.00"),
                )
            ],
        )
        repo.get_invoice_for_update.return_value = draft
        service, _, _ = _make_service(repo)

        result = await service.issue_invoice(HOSPITAL_ID, draft.id)

        assert result.subtotal == Decimal("200.00")
        assert result.tax_amount == Decimal("10.00")
        assert result.total == Decimal("210.00")
        assert result.items[0].line_total == Decimal("210.00")

    async def test_a_zero_total_invoice_is_paid_at_issue(self, repo: AsyncMock) -> None:
        # Business rule 11: amount_paid >= total means paid.
        free = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            items=[build_invoice_item_model(unit_price=Decimal("0.00"))],
        )
        repo.get_invoice_for_update.return_value = free
        service, _, audit = _make_service(repo)

        result = await service.issue_invoice(HOSPITAL_ID, free.id)

        assert result.status is InvoiceStatus.PAID
        assert result.total == Decimal("0.00")
        assert audit.last().changes["status"]["after"] == "paid"

    async def test_number_year_follows_the_hospital_timezone(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)
        repo.get_invoice_for_update.return_value = draft
        service, _, _ = _make_service(repo)

        # 31 Dec 20:00 UTC is already 1 Jan 01:30 in Asia/Kolkata.
        number = await service._next_invoice_number(
            HOSPITAL_ID,
            issued_at=datetime(2026, 12, 31, 20, 0, tzinfo=UTC),
            zone=ZoneInfo("Asia/Kolkata"),
        )

        assert number == "INV-2027-000042"

    @pytest.mark.parametrize(
        "status",
        [
            InvoiceStatus.ISSUED,
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.PAID,
            InvoiceStatus.VOID,
        ],
    )
    async def test_only_a_draft_can_be_issued(self, repo: AsyncMock, status: InvoiceStatus) -> None:
        repo.get_invoice_for_update.return_value = _issued(status=status)
        sequences = AsyncMock()
        service, session, _ = _make_service(repo, sequences=sequences)

        with pytest.raises(InvalidInvoiceStateError):
            await service.issue_invoice(HOSPITAL_ID, uuid.uuid4())

        # No number is consumed by a refused issue (AC-2).
        sequences.advance.assert_not_awaited()
        assert session.commits == 0

    async def test_an_empty_draft_cannot_be_issued(self, repo: AsyncMock) -> None:
        repo.get_invoice_for_update.return_value = build_invoice_model(
            hospital_id=HOSPITAL_ID, items=[]
        )
        sequences = AsyncMock()
        service, _, _ = _make_service(repo, sequences=sequences)

        with pytest.raises(BusinessRuleError, match="at least one line"):
            await service.issue_invoice(HOSPITAL_ID, uuid.uuid4())

        sequences.advance.assert_not_awaited()

    async def test_a_broken_number_template_is_a_configuration_error(self, repo: AsyncMock) -> None:
        repo.get_invoice_for_update.return_value = build_invoice_model(hospital_id=HOSPITAL_ID)
        sequences = AsyncMock()
        sequences.advance.return_value = (1, "INV-{nope}")
        service, session, _ = _make_service(repo, sequences=sequences)

        with pytest.raises(ConfigurationError, match="invoice number format is misconfigured"):
            await service.issue_invoice(HOSPITAL_ID, uuid.uuid4())

        assert session.commits == 0

    async def test_unknown_invoice_is_a_404(self, repo: AsyncMock) -> None:
        repo.get_invoice_for_update.return_value = None
        service, _, _ = _make_service(repo)

        with pytest.raises(InvoiceNotFoundError):
            await service.issue_invoice(HOSPITAL_ID, uuid.uuid4())


class TestVoidInvoice:
    async def test_voids_an_unpaid_issued_invoice(self, repo: AsyncMock) -> None:
        invoice = _issued()
        repo.get_invoice_for_update.return_value = invoice
        service, session, audit = _make_service(repo)

        result = await service.void_invoice(
            HOSPITAL_ID, invoice.id, build_void_request(reason="Wrong patient"), actor_id=ACTOR_ID
        )

        assert result.status is InvoiceStatus.VOID
        assert result.void_reason == "Wrong patient"
        assert result.voided_at is not None
        # The number is kept, so the series has no gap.
        assert result.invoice_number == "INV-2026-000007"
        assert session.commits == 1
        assert audit.actions() == ["invoice.voided"]
        assert audit.last().context["reason"] == "Wrong patient"

    @pytest.mark.parametrize("status", [InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID])
    async def test_an_invoice_with_payments_must_be_refunded_first(
        self, repo: AsyncMock, status: InvoiceStatus
    ) -> None:
        repo.get_invoice_for_update.return_value = _issued(
            status=status, amount_paid=Decimal("100.00")
        )
        service, session, _ = _make_service(repo)

        with pytest.raises(InvalidInvoiceStateError, match="refunded first"):
            await service.void_invoice(HOSPITAL_ID, uuid.uuid4(), build_void_request())

        repo.update_invoice.assert_not_awaited()
        assert session.commits == 0

    @pytest.mark.parametrize("status", [InvoiceStatus.DRAFT, InvoiceStatus.VOID])
    async def test_a_draft_or_void_invoice_cannot_be_voided(
        self, repo: AsyncMock, status: InvoiceStatus
    ) -> None:
        repo.get_invoice_for_update.return_value = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            status=status,
            invoice_number=None if status is InvoiceStatus.DRAFT else "INV-2026-000007",
        )
        service, _, _ = _make_service(repo)

        with pytest.raises(InvalidInvoiceStateError) as excinfo:
            await service.void_invoice(HOSPITAL_ID, uuid.uuid4(), build_void_request())

        assert "refunded" not in excinfo.value.message


# ── Payments ────────────────────────────────────────────────────────────────


class TestRecordPayment:
    async def test_a_partial_payment_moves_to_partially_paid(self, repo: AsyncMock) -> None:
        invoice = _issued()
        repo.get_invoice_for_update.return_value = invoice
        service, session, audit = _make_service(repo)

        result, created = await service.record_payment(
            HOSPITAL_ID,
            invoice.id,
            build_record_payment_request(amount="200.00", method="cash"),
            idempotency_key=KEY,
            actor_id=ACTOR_ID,
        )

        assert created is True
        assert result.payment.amount == Decimal("200.00")
        assert result.payment.method is PaymentMethod.CASH
        assert result.payment.received_by == ACTOR_ID
        assert result.invoice.status is InvoiceStatus.PARTIALLY_PAID
        assert result.invoice.amount_paid == Decimal("200.00")
        assert result.invoice.balance_due == Decimal("300.00")
        assert repo.create_payment.await_args.kwargs["idempotency_key"] == KEY
        assert session.commits == 1
        assert audit.actions() == ["invoice.payment_recorded"]
        assert audit.last().changes == {"status": {"before": "issued", "after": "partially_paid"}}

    async def test_paying_the_balance_marks_the_invoice_paid(self, repo: AsyncMock) -> None:
        # Business rule 11.
        invoice = _issued(status=InvoiceStatus.PARTIALLY_PAID, amount_paid=Decimal("200.00"))
        repo.get_invoice_for_update.return_value = invoice
        service, _, _ = _make_service(repo)

        result, _ = await service.record_payment(
            HOSPITAL_ID,
            invoice.id,
            build_record_payment_request(amount="300.00"),
            idempotency_key=KEY,
            actor_id=ACTOR_ID,
        )

        assert result.invoice.status is InvoiceStatus.PAID
        assert result.invoice.amount_paid == Decimal("500.00")
        assert result.invoice.balance_due == Decimal("0.00")

    async def test_overpayment_is_refused_and_nothing_is_written(self, repo: AsyncMock) -> None:
        # Business rule 9.
        invoice = _issued(status=InvoiceStatus.PARTIALLY_PAID, amount_paid=Decimal("200.00"))
        repo.get_invoice_for_update.return_value = invoice
        service, session, audit = _make_service(repo)

        with pytest.raises(OverpaymentError) as excinfo:
            await service.record_payment(
                HOSPITAL_ID,
                invoice.id,
                build_record_payment_request(amount="300.01"),
                idempotency_key=KEY,
                actor_id=ACTOR_ID,
            )

        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == {"amount": "300.01", "balance_due": "300.00"}
        repo.create_payment.assert_not_awaited()
        assert invoice.amount_paid == Decimal("200.00")
        assert session.commits == 0
        assert audit.events == []

    @pytest.mark.parametrize(
        ("status", "hint"),
        [
            (InvoiceStatus.DRAFT, "Issue the invoice first"),
            (InvoiceStatus.PAID, None),
            (InvoiceStatus.VOID, None),
            (InvoiceStatus.REFUNDED, None),
        ],
    )
    async def test_only_an_open_invoice_takes_payments(
        self, repo: AsyncMock, status: InvoiceStatus, hint: str | None
    ) -> None:
        repo.get_invoice_for_update.return_value = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            status=status,
            invoice_number=None if status is InvoiceStatus.DRAFT else "INV-2026-000007",
        )
        service, session, _ = _make_service(repo)

        with pytest.raises(InvalidInvoiceStateError) as excinfo:
            await service.record_payment(
                HOSPITAL_ID,
                uuid.uuid4(),
                build_record_payment_request(),
                idempotency_key=KEY,
                actor_id=ACTOR_ID,
            )

        assert ("Issue the invoice first" in excinfo.value.message) is (hint is not None)
        repo.create_payment.assert_not_awaited()
        assert session.commits == 0

    async def test_unknown_invoice_is_a_404(self, repo: AsyncMock) -> None:
        repo.get_invoice_for_update.return_value = None
        service, _, _ = _make_service(repo)

        with pytest.raises(InvoiceNotFoundError):
            await service.record_payment(
                HOSPITAL_ID,
                uuid.uuid4(),
                build_record_payment_request(),
                idempotency_key=KEY,
                actor_id=ACTOR_ID,
            )


class TestPaymentIdempotency:
    """Business rule 6, FR-3, AC-3."""

    async def test_a_replay_returns_the_original_and_takes_no_money(self, repo: AsyncMock) -> None:
        invoice = _issued(status=InvoiceStatus.PAID, amount_paid=Decimal("500.00"))
        original = build_payment_model(
            hospital_id=HOSPITAL_ID,
            invoice_id=invoice.id,
            amount=Decimal("500.00"),
            method=PaymentMethod.UPI,
            idempotency_key=KEY,
        )
        repo.get_invoice_for_update.return_value = invoice
        repo.get_payment_by_idempotency_key.return_value = original
        service, _, audit = _make_service(repo)

        result, created = await service.record_payment(
            HOSPITAL_ID,
            invoice.id,
            build_record_payment_request(amount="500.00", method="upi"),
            idempotency_key=KEY,
            actor_id=ACTOR_ID,
        )

        assert created is False
        assert result.payment.id == original.id
        # The invoice is fully paid, so a non-replay would have been refused:
        # the replay is recognised *before* the state check.
        assert result.invoice.status is InvoiceStatus.PAID
        repo.create_payment.assert_not_awaited()
        repo.update_invoice.assert_not_awaited()
        assert audit.events == []

    async def test_the_replay_check_runs_after_the_lock(self, repo: AsyncMock) -> None:
        invoice = _issued()
        repo.get_invoice_for_update.return_value = invoice
        calls: list[str] = []

        async def lock(*_: object) -> Any:
            calls.append("lock")
            return invoice

        async def replay_check(*_: object) -> None:
            calls.append("replay-check")

        repo.get_invoice_for_update.side_effect = lock
        repo.get_payment_by_idempotency_key.side_effect = replay_check
        service, _, _ = _make_service(repo)

        await service.record_payment(
            HOSPITAL_ID,
            invoice.id,
            build_record_payment_request(),
            idempotency_key=KEY,
            actor_id=ACTOR_ID,
        )

        assert calls == ["lock", "replay-check"]

    @pytest.mark.parametrize(
        "difference",
        [{"amount": "499.00"}, {"method": "cash"}, {"other_invoice": True}],
    )
    async def test_a_key_reused_for_a_different_payment_is_a_409(
        self, repo: AsyncMock, difference: dict[str, Any]
    ) -> None:
        invoice = _issued()
        original = build_payment_model(
            hospital_id=HOSPITAL_ID,
            invoice_id=uuid.uuid4() if difference.get("other_invoice") else invoice.id,
            amount=Decimal("500.00"),
            method=PaymentMethod.UPI,
        )
        repo.get_invoice_for_update.return_value = invoice
        repo.get_payment_by_idempotency_key.return_value = original
        service, session, _ = _make_service(repo)
        body = {"amount": "500.00", "method": "upi"}
        body.update({k: v for k, v in difference.items() if k != "other_invoice"})

        with pytest.raises(IdempotencyKeyReuseError) as excinfo:
            await service.record_payment(
                HOSPITAL_ID,
                invoice.id,
                build_record_payment_request(**body),
                idempotency_key=KEY,
                actor_id=ACTOR_ID,
            )

        assert excinfo.value.status_code == 409
        repo.create_payment.assert_not_awaited()
        assert session.commits == 0

    async def test_a_key_racing_in_on_another_invoice_is_a_409(self, repo: AsyncMock) -> None:
        invoice = _issued()
        repo.get_invoice_for_update.return_value = invoice
        repo.create_payment.side_effect = _integrity_error("uq_payments_hospital_idempotency_key")
        service, session, audit = _make_service(repo)

        with pytest.raises(IdempotencyKeyReuseError):
            await service.record_payment(
                HOSPITAL_ID,
                invoice.id,
                build_record_payment_request(),
                idempotency_key=KEY,
                actor_id=ACTOR_ID,
            )

        assert session.savepoints_rolled_back == 1
        assert session.commits == 0
        assert audit.events == []

    async def test_any_other_integrity_error_is_not_swallowed(self, repo: AsyncMock) -> None:
        invoice = _issued()
        repo.get_invoice_for_update.return_value = invoice
        repo.create_payment.side_effect = _integrity_error("ck_invoices_paid_within_total")
        service, _, _ = _make_service(repo)

        with pytest.raises(IntegrityError):
            await service.record_payment(
                HOSPITAL_ID,
                invoice.id,
                build_record_payment_request(),
                idempotency_key=KEY,
                actor_id=ACTOR_ID,
            )


# ── Queries ─────────────────────────────────────────────────────────────────


class TestQueries:
    async def test_get_invoice(self, repo: AsyncMock) -> None:
        invoice = _issued()
        repo.get_invoice_by_id.return_value = invoice
        service, _, _ = _make_service(repo)

        result = await service.get_invoice(HOSPITAL_ID, invoice.id)

        assert result.id == invoice.id
        repo.get_invoice_by_id.assert_awaited_once_with(HOSPITAL_ID, invoice.id, doctor_id=None)

    async def test_get_invoice_in_another_tenant_is_a_404(self, repo: AsyncMock) -> None:
        repo.get_invoice_by_id.return_value = None
        service, _, _ = _make_service(repo)

        with pytest.raises(InvoiceNotFoundError) as excinfo:
            await service.get_invoice(HOSPITAL_ID, uuid.uuid4())

        assert excinfo.value.status_code == 404

    async def test_list_invoices_pages_and_counts(self, repo: AsyncMock) -> None:
        repo.list_invoices.return_value = [_issued(), _issued()]
        repo.count_invoices.return_value = 7
        service, _, _ = _make_service(repo)

        page = await service.list_invoices(
            HOSPITAL_ID, patient_id=PATIENT_ID, status=InvoiceStatus.ISSUED
        )

        assert len(page.items) == 2
        assert page.total_records == 7
        assert page.items[0].currency == "INR"
        filters = repo.list_invoices.await_args.kwargs
        assert filters["patient_id"] == PATIENT_ID
        assert filters["status"] is InvoiceStatus.ISSUED
        assert "issued_on_or_after" not in filters

    async def test_date_range_is_the_hospitals_local_days_inclusive(self, repo: AsyncMock) -> None:
        repo.list_invoices.return_value = []
        repo.count_invoices.return_value = 0
        service, _, _ = _make_service(repo)

        await service.list_invoices(
            HOSPITAL_ID, issued_from=date(2026, 10, 1), issued_to=date(2026, 10, 1)
        )

        filters = repo.list_invoices.await_args.kwargs
        kolkata = ZoneInfo("Asia/Kolkata")
        # One local day: midnight to the next midnight, in Asia/Kolkata.
        assert filters["issued_on_or_after"] == datetime(2026, 10, 1, tzinfo=kolkata)
        assert filters["issued_before"] == datetime(2026, 10, 2, tzinfo=kolkata)
        # The count must use the same filters as the page, or the totals lie.
        paging = {"skip", "limit"}
        assert repo.count_invoices.await_args.kwargs == {
            name: value for name, value in filters.items() if name not in paging
        }

    async def test_an_inverted_date_range_is_a_422(self, repo: AsyncMock) -> None:
        service, _, _ = _make_service(repo)

        with pytest.raises(ValidationError) as excinfo:
            await service.list_invoices(
                HOSPITAL_ID, issued_from=date(2026, 10, 2), issued_to=date(2026, 10, 1)
            )

        assert excinfo.value.status_code == 422
        repo.list_invoices.assert_not_awaited()

    async def test_list_payments(self, repo: AsyncMock) -> None:
        invoice = _issued()
        repo.get_invoice_by_id.return_value = invoice
        repo.list_payments.return_value = [build_payment_model(invoice_id=invoice.id)]
        service, _, _ = _make_service(repo)

        payments = await service.list_payments(HOSPITAL_ID, invoice.id)

        assert [payment.invoice_id for payment in payments] == [invoice.id]
        repo.list_payments.assert_awaited_once_with(HOSPITAL_ID, invoice.id)

    async def test_list_payments_for_an_unknown_invoice_is_a_404(self, repo: AsyncMock) -> None:
        repo.get_invoice_by_id.return_value = None
        service, _, _ = _make_service(repo)

        with pytest.raises(InvoiceNotFoundError):
            await service.list_payments(HOSPITAL_ID, uuid.uuid4())

        repo.list_payments.assert_not_awaited()


# ── Role rules (module spec §3) ─────────────────────────────────────────────


def _doctors(doctor_id: uuid.UUID | None) -> AsyncMock:
    """A doctor repository that resolves the asking user to ``doctor_id``."""
    doctors = AsyncMock()
    if doctor_id is None:
        doctors.get_doctor_by_user_id.return_value = None
    else:
        doctor = MagicMock()
        doctor.id = doctor_id
        doctors.get_doctor_by_user_id.return_value = doctor
    return doctors


class TestOwnVisitsScope:
    """A doctor holding only ``invoice.read.own`` sees their own visits' invoices."""

    async def test_list_is_filtered_to_the_callers_doctor_profile(self, repo: AsyncMock) -> None:
        doctor_id = uuid.uuid4()
        doctors = _doctors(doctor_id)
        repo.list_invoices.return_value = [_issued()]
        repo.count_invoices.return_value = 1
        service, _, _ = _make_service(repo, doctors=doctors)

        page = await service.list_invoices(HOSPITAL_ID, own_visits_of=ACTOR_ID)

        assert page.total_records == 1
        assert repo.list_invoices.await_args.kwargs["doctor_id"] == doctor_id
        # The count is scoped too, or the total would leak how many exist.
        assert repo.count_invoices.await_args.kwargs["doctor_id"] == doctor_id
        # A deactivated doctor profile does not count.
        doctors.get_doctor_by_user_id.assert_awaited_once_with(
            HOSPITAL_ID, ACTOR_ID, include_deleted=False
        )

    async def test_an_unscoped_list_applies_no_doctor_filter(self, repo: AsyncMock) -> None:
        doctors = _doctors(uuid.uuid4())
        repo.list_invoices.return_value = []
        repo.count_invoices.return_value = 0
        service, _, _ = _make_service(repo, doctors=doctors)

        await service.list_invoices(HOSPITAL_ID)

        assert "doctor_id" not in repo.list_invoices.await_args.kwargs
        doctors.get_doctor_by_user_id.assert_not_awaited()

    async def test_a_scoped_caller_who_is_not_a_doctor_sees_nothing(self, repo: AsyncMock) -> None:
        service, _, _ = _make_service(repo, doctors=_doctors(None))

        page = await service.list_invoices(HOSPITAL_ID, pagination=None, own_visits_of=ACTOR_ID)

        assert page.items == []
        assert page.total_records == 0
        # Not "an unfiltered query that happens to be empty": no query at all.
        repo.list_invoices.assert_not_awaited()
        repo.count_invoices.assert_not_awaited()

    async def test_get_passes_the_scope_to_the_repository(self, repo: AsyncMock) -> None:
        doctor_id = uuid.uuid4()
        invoice = _issued()
        repo.get_invoice_by_id.return_value = invoice
        service, _, _ = _make_service(repo, doctors=_doctors(doctor_id))

        result = await service.get_invoice(HOSPITAL_ID, invoice.id, own_visits_of=ACTOR_ID)

        assert result.id == invoice.id
        repo.get_invoice_by_id.assert_awaited_once_with(
            HOSPITAL_ID, invoice.id, doctor_id=doctor_id
        )

    async def test_an_invoice_outside_the_scope_is_a_404(self, repo: AsyncMock) -> None:
        # The repository returns nothing for an invoice that is not theirs,
        # and the service reports it exactly as it reports a missing one.
        repo.get_invoice_by_id.return_value = None
        service, _, _ = _make_service(repo, doctors=_doctors(uuid.uuid4()))

        with pytest.raises(InvoiceNotFoundError) as excinfo:
            await service.get_invoice(HOSPITAL_ID, uuid.uuid4(), own_visits_of=ACTOR_ID)

        assert excinfo.value.status_code == 404

    async def test_get_by_a_scoped_non_doctor_is_a_404_without_a_lookup(
        self, repo: AsyncMock
    ) -> None:
        service, _, _ = _make_service(repo, doctors=_doctors(None))

        with pytest.raises(InvoiceNotFoundError):
            await service.get_invoice(HOSPITAL_ID, uuid.uuid4(), own_visits_of=ACTOR_ID)

        repo.get_invoice_by_id.assert_not_awaited()

    async def test_payments_are_scoped_through_their_invoice(self, repo: AsyncMock) -> None:
        doctor_id = uuid.uuid4()
        repo.get_invoice_by_id.return_value = None
        service, _, _ = _make_service(repo, doctors=_doctors(doctor_id))

        with pytest.raises(InvoiceNotFoundError):
            await service.list_payments(HOSPITAL_ID, uuid.uuid4(), own_visits_of=ACTOR_ID)

        assert repo.get_invoice_by_id.await_args.kwargs == {"doctor_id": doctor_id}
        repo.list_payments.assert_not_awaited()


class TestCashOnly:
    """A receptionist holding only ``invoice.payment.record.cash``."""

    async def test_cash_is_recorded(self, repo: AsyncMock) -> None:
        invoice = _issued()
        repo.get_invoice_for_update.return_value = invoice
        service, session, _ = _make_service(repo)

        result, created = await service.record_payment(
            HOSPITAL_ID,
            invoice.id,
            build_record_payment_request(amount="200.00", method="cash"),
            idempotency_key=KEY,
            actor_id=ACTOR_ID,
            cash_only=True,
        )

        assert created is True
        assert result.payment.method is PaymentMethod.CASH
        assert session.commits == 1

    @pytest.mark.parametrize("method", ["card", "upi", "bank_transfer", "insurance"])
    async def test_any_other_method_is_refused_before_anything_is_read(
        self, repo: AsyncMock, method: str
    ) -> None:
        service, session, audit = _make_service(repo)

        with pytest.raises(CashOnlyPaymentError) as excinfo:
            await service.record_payment(
                HOSPITAL_ID,
                uuid.uuid4(),
                build_record_payment_request(amount="200.00", method=method),
                idempotency_key=KEY,
                actor_id=ACTOR_ID,
                cash_only=True,
            )

        assert excinfo.value.status_code == 403
        assert excinfo.value.detail == {"method": method, "allowed_methods": ["cash"]}
        # Refused on permission alone: the invoice was never locked or looked up,
        # so the refusal says nothing about whether it exists.
        repo.get_invoice_for_update.assert_not_awaited()
        repo.create_payment.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_the_full_permission_records_any_method(self, repo: AsyncMock) -> None:
        invoice = _issued()
        repo.get_invoice_for_update.return_value = invoice
        service, _, _ = _make_service(repo)

        result, _ = await service.record_payment(
            HOSPITAL_ID,
            invoice.id,
            build_record_payment_request(amount="200.00", method="upi"),
            idempotency_key=KEY,
            actor_id=ACTOR_ID,
        )

        assert result.payment.method is PaymentMethod.UPI


# ── Discounts (module spec §5.2, business rule 5, AC-4) ─────────────────────


def _with_threshold(percent: Any) -> AsyncMock:
    """A hospital repository whose hospital sets a discount approval threshold."""
    hospitals = AsyncMock()
    hospitals.get_by_id.return_value = _hospital(
        {"billing": {"discount_approval_threshold_percent": percent}}
    )
    return hospitals


class TestDiscount:
    """Setting a discount on a draft. The default draft is one 500.00 line."""

    async def _patch(self, repo: AsyncMock, draft: Any, hospitals: AsyncMock, **fields: Any) -> Any:
        repo.get_invoice_for_update.return_value = draft
        service, session, audit = _make_service(repo, hospitals=hospitals)
        result = await service.update_invoice(
            HOSPITAL_ID, draft.id, build_update_invoice_request(**fields), actor_id=ACTOR_ID
        )
        return result, session, audit

    async def test_a_discount_within_the_threshold_needs_no_approval(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)

        result, session, audit = await self._patch(
            repo, draft, _with_threshold("10"), discount_amount="50.00", discount_reason="Staff"
        )

        # Exactly 10% of 500.00: "above the threshold" is strict.
        assert result.discount_amount == Decimal("50.00")
        assert result.discount_reason == "Staff"
        assert result.discount_pending_approval is False
        assert result.total == Decimal("450.00")
        assert session.commits == 1
        assert audit.last().context["changed"] == ["discount_amount", "discount_reason"]

    async def test_a_discount_above_the_threshold_awaits_approval(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)

        result, _, audit = await self._patch(
            repo, draft, _with_threshold("10"), discount_amount="50.01", discount_reason="Hardship"
        )

        assert result.discount_pending_approval is True
        assert result.discount_approved_by is None
        # The total already reflects the discount; only the issue is blocked.
        assert result.total == Decimal("449.99")
        assert audit.last().context["discount_pending_approval"] is True

    async def test_with_no_threshold_configured_every_discount_awaits_approval(
        self, repo: AsyncMock
    ) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)
        hospitals = AsyncMock()
        hospitals.get_by_id.return_value = _hospital({})

        result, _, _ = await self._patch(
            repo, draft, hospitals, discount_amount="0.01", discount_reason="Rounding"
        )

        assert result.discount_pending_approval is True

    async def test_tax_is_charged_on_the_undiscounted_lines(self, repo: AsyncMock) -> None:
        # Business rule 7: total = subtotal + tax_amount - discount_amount.
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            subtotal=Decimal("1000.00"),
            tax_amount=Decimal("180.00"),
            total=Decimal("1180.00"),
        )

        result, _, _ = await self._patch(
            repo, draft, _with_threshold("50"), discount_amount="100.00", discount_reason="Loyalty"
        )

        assert result.tax_amount == Decimal("180.00")
        assert result.total == Decimal("1080.00")

    async def test_a_discount_may_equal_the_subtotal_but_not_exceed_it(
        self, repo: AsyncMock
    ) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)
        result, _, _ = await self._patch(
            repo, draft, _with_threshold("100"), discount_amount="500.00", discount_reason="Waived"
        )
        assert result.total == Decimal("0.00")

        with pytest.raises(ValidationError) as excinfo:
            await self._patch(
                repo,
                build_invoice_model(hospital_id=HOSPITAL_ID),
                _with_threshold("100"),
                discount_amount="500.01",
                discount_reason="Waived",
            )
        assert excinfo.value.detail["errors"][0]["field"] == "discount_amount"

    async def test_a_discount_needs_a_reason(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)
        repo.get_invoice_for_update.return_value = draft
        service, session, audit = _make_service(repo, hospitals=_with_threshold("10"))

        with pytest.raises(ValidationError) as excinfo:
            await service.update_invoice(
                HOSPITAL_ID, draft.id, build_update_invoice_request(discount_amount="10.00")
            )

        assert excinfo.value.detail["errors"][0]["field"] == "discount_reason"
        repo.update_invoice.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_the_existing_reason_carries_over_when_only_the_amount_changes(
        self, repo: AsyncMock
    ) -> None:
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("20.00"),
            discount_reason="Staff",
            total=Decimal("480.00"),
        )

        result, _, audit = await self._patch(
            repo, draft, _with_threshold("10"), discount_amount="30.00"
        )

        assert result.discount_amount == Decimal("30.00")
        assert result.discount_reason == "Staff"
        assert audit.last().context["changed"] == ["discount_amount"]

    async def test_setting_the_discount_to_zero_clears_it_entirely(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            discount_pending_approval=True,
            total=Decimal("300.00"),
        )

        result, _, _ = await self._patch(repo, draft, _with_threshold("10"), discount_amount="0")

        assert result.discount_amount == Decimal("0.00")
        assert result.discount_reason is None
        assert result.discount_pending_approval is False
        assert result.total == Decimal("500.00")

    async def test_changing_the_discount_withdraws_an_approval(self, repo: AsyncMock) -> None:
        approver = uuid.uuid4()
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            discount_approved_by=approver,
            total=Decimal("300.00"),
        )

        result, _, _ = await self._patch(
            repo, draft, _with_threshold("10"), discount_amount="250.00"
        )

        assert result.discount_approved_by is None
        assert result.discount_pending_approval is True

    async def test_changing_the_lines_withdraws_an_approval(self, repo: AsyncMock) -> None:
        # What was approved was 200.00 off a 500.00 bill, not off whatever the
        # bill is edited into afterwards.
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            discount_approved_by=uuid.uuid4(),
            total=Decimal("300.00"),
        )

        result, _, _ = await self._patch(
            repo,
            draft,
            _with_threshold("10"),
            items=[{"description": "X-ray", "unit_price": "1000.00"}],
        )

        assert result.subtotal == Decimal("1000.00")
        assert result.discount_amount == Decimal("200.00")
        assert result.total == Decimal("800.00")
        assert result.discount_approved_by is None
        assert result.discount_pending_approval is True

    async def test_editing_only_the_notes_keeps_an_approval(self, repo: AsyncMock) -> None:
        approver = uuid.uuid4()
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            discount_approved_by=approver,
            total=Decimal("300.00"),
        )

        result, _, _ = await self._patch(repo, draft, _with_threshold("10"), notes="Called patient")

        assert result.discount_approved_by == approver
        assert result.discount_pending_approval is False
        assert result.total == Decimal("300.00")

    async def test_shrinking_the_lines_below_the_discount_is_refused_before_any_write(
        self, repo: AsyncMock
    ) -> None:
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            total=Decimal("300.00"),
        )
        repo.get_invoice_for_update.return_value = draft
        service, _, _ = _make_service(repo, hospitals=_with_threshold("10"))

        with pytest.raises(ValidationError, match="exceeds the subtotal"):
            await service.update_invoice(
                HOSPITAL_ID,
                draft.id,
                build_update_invoice_request(
                    items=[{"description": "Bandage", "unit_price": "50.00"}]
                ),
            )

        # The lines were not replaced: validation runs before the first write.
        repo.replace_items.assert_not_awaited()
        repo.update_invoice.assert_not_awaited()

    @pytest.mark.parametrize("raw", ["ten", -1, 100.5, "NaN", True, {"nested": 1}])
    async def test_a_malformed_threshold_means_every_discount_awaits_approval(
        self, repo: AsyncMock, raw: Any
    ) -> None:
        # A bad setting must make the rule stricter, never looser.
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)

        result, _, _ = await self._patch(
            repo, draft, _with_threshold(raw), discount_amount="1.00", discount_reason="Test"
        )

        assert result.discount_pending_approval is True

    async def test_a_missing_hospital_means_every_discount_awaits_approval(
        self, repo: AsyncMock
    ) -> None:
        hospitals = AsyncMock()
        hospitals.get_by_id.return_value = None
        service, _, _ = _make_service(repo, hospitals=hospitals)

        assert await service._discount_threshold_percent(HOSPITAL_ID) == Decimal("0.00")


class TestDiscountApprovalNotice:
    """Approvers are told when a discount is waiting for them."""

    async def _patch(self, repo: AsyncMock, draft: Any, **fields: Any) -> AsyncMock:
        repo.get_invoice_for_update.return_value = draft
        notifier = AsyncMock()
        service, _, _ = _make_service(repo, hospitals=_with_threshold("10"), notifier=notifier)
        await service.update_invoice(
            HOSPITAL_ID, draft.id, build_update_invoice_request(**fields), actor_id=ACTOR_ID
        )
        return notifier

    async def test_a_discount_that_needs_approval_notifies_the_approvers(
        self, repo: AsyncMock
    ) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)

        notifier = await self._patch(
            repo, draft, discount_amount="200.00", discount_reason="Hardship"
        )

        notifier.notify.assert_awaited_once()
        request = notifier.notify.await_args.args[0]
        assert request.kind == "billing.discount_approval_requested"
        assert request.hospital_id == HOSPITAL_ID
        # Addressed to whoever can act on it, not to named people.
        assert request.recipient_permission == "invoice.approve_discount"
        assert request.recipient_user_ids == ()
        assert request.actor_id == ACTOR_ID
        assert request.link == "/billing"
        assert request.variables["discount_amount"].endswith(" 200.00")
        assert request.variables["patient_name"]
        assert request.secret_variables == {}

    async def test_a_discount_within_the_threshold_notifies_nobody(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(hospital_id=HOSPITAL_ID)

        notifier = await self._patch(repo, draft, discount_amount="50.00", discount_reason="Staff")

        notifier.notify.assert_not_awaited()

    async def test_an_unrelated_edit_to_a_pending_invoice_does_not_notify_again(
        self, repo: AsyncMock
    ) -> None:
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            discount_pending_approval=True,
            total=Decimal("300.00"),
        )

        notifier = await self._patch(repo, draft, notes="Call the patient first")

        notifier.notify.assert_not_awaited()

    async def test_changing_a_pending_discount_asks_again(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            discount_pending_approval=True,
            total=Decimal("300.00"),
        )

        notifier = await self._patch(repo, draft, discount_amount="250.00")

        notifier.notify.assert_awaited_once()
        assert notifier.notify.await_args.args[0].variables["discount_amount"].endswith(" 250.00")


class TestApproveDiscount:
    async def test_approval_clears_the_flag_and_records_the_admin(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            discount_pending_approval=True,
            total=Decimal("300.00"),
        )
        repo.get_invoice_for_update.return_value = draft
        service, session, audit = _make_service(repo)

        result = await service.approve_discount(HOSPITAL_ID, draft.id, actor_id=ACTOR_ID)

        assert result.discount_pending_approval is False
        assert result.discount_approved_by == ACTOR_ID
        assert result.status is InvoiceStatus.DRAFT
        assert session.commits == 1
        assert audit.actions() == ["invoice.discount_approved"]
        assert audit.last().context["discount_amount"] == "200.00"

    async def test_nothing_to_approve_is_a_409(self, repo: AsyncMock) -> None:
        # What the second of two admins approving at once sees (spec §14).
        repo.get_invoice_for_update.return_value = build_invoice_model(hospital_id=HOSPITAL_ID)
        service, session, audit = _make_service(repo)

        with pytest.raises(NoDiscountToApproveError) as excinfo:
            await service.approve_discount(HOSPITAL_ID, uuid.uuid4(), actor_id=ACTOR_ID)

        assert excinfo.value.status_code == 409
        repo.update_invoice.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_only_a_draft_has_a_discount_to_approve(self, repo: AsyncMock) -> None:
        repo.get_invoice_for_update.return_value = _issued()
        service, _, _ = _make_service(repo)

        with pytest.raises(InvalidInvoiceStateError):
            await service.approve_discount(HOSPITAL_ID, uuid.uuid4(), actor_id=ACTOR_ID)

    async def test_unknown_invoice_is_a_404(self, repo: AsyncMock) -> None:
        repo.get_invoice_for_update.return_value = None
        service, _, _ = _make_service(repo)

        with pytest.raises(InvoiceNotFoundError):
            await service.approve_discount(HOSPITAL_ID, uuid.uuid4(), actor_id=ACTOR_ID)


class TestIssueWithDiscount:
    async def test_ac4_a_pending_discount_blocks_the_issue(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            discount_pending_approval=True,
            total=Decimal("300.00"),
        )
        repo.get_invoice_for_update.return_value = draft
        sequences = AsyncMock()
        service, session, _ = _make_service(repo, sequences=sequences)

        with pytest.raises(DiscountAwaitingApprovalError) as excinfo:
            await service.issue_invoice(HOSPITAL_ID, draft.id)

        assert excinfo.value.status_code == 400
        # No invoice number is consumed by a refused issue.
        sequences.advance.assert_not_awaited()
        assert session.commits == 0

    async def test_an_approved_discount_is_carried_into_the_frozen_total(
        self, repo: AsyncMock
    ) -> None:
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            discount_approved_by=uuid.uuid4(),
            total=Decimal("1.00"),  # stale on purpose
        )
        repo.get_invoice_for_update.return_value = draft
        service, _, _ = _make_service(repo)

        result = await service.issue_invoice(HOSPITAL_ID, draft.id)

        assert result.status is InvoiceStatus.ISSUED
        assert result.subtotal == Decimal("500.00")
        assert result.discount_amount == Decimal("200.00")
        assert result.total == Decimal("300.00")

    async def test_a_fully_discounted_invoice_is_paid_at_issue(self, repo: AsyncMock) -> None:
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            discount_amount=Decimal("500.00"),
            discount_reason="Waived",
            total=Decimal("0.00"),
        )
        repo.get_invoice_for_update.return_value = draft
        service, _, _ = _make_service(repo)

        assert (await service.issue_invoice(HOSPITAL_ID, draft.id)).status is InvoiceStatus.PAID


# ── Refunds (module spec §5.5, business rule 10) ────────────────────────────

REFUND_KEY = "refund-key-00000001"


def _paid(**overrides: Any) -> Any:
    """A fully paid 500.00 invoice."""
    values: dict[str, Any] = {"status": InvoiceStatus.PAID, "amount_paid": Decimal("500.00")}
    values.update(overrides)
    return _issued(**values)


class TestRecordRefund:
    async def _refund(
        self, repo: AsyncMock, invoice: Any, *, key: str = REFUND_KEY, **body: Any
    ) -> Any:
        repo.get_invoice_for_update.return_value = invoice
        service, session, audit = _make_service(repo)
        result, created = await service.record_refund(
            HOSPITAL_ID,
            invoice.id,
            build_record_refund_request(**body),
            idempotency_key=key,
            actor_id=ACTOR_ID,
        )
        return result, created, session, audit

    async def test_a_partial_refund_leaves_the_status_alone(self, repo: AsyncMock) -> None:
        invoice = _paid()

        result, created, session, audit = await self._refund(
            repo, invoice, amount="200.00", method="upi", reason="ECG not performed"
        )

        assert created is True
        assert result.refund.amount == Decimal("200.00")
        assert result.refund.reason == "ECG not performed"
        assert result.refund.refunded_by == ACTOR_ID
        assert result.invoice.status is InvoiceStatus.PAID
        assert result.invoice.amount_refunded == Decimal("200.00")
        # What was received is not rewritten.
        assert result.invoice.amount_paid == Decimal("500.00")
        assert repo.create_refund.await_args.kwargs["idempotency_key"] == REFUND_KEY
        assert session.commits == 1
        assert audit.actions() == ["invoice.refunded"]
        assert audit.last().changes == {"status": {"before": "paid", "after": "paid"}}

    async def test_refunding_everything_closes_the_invoice_as_refunded(
        self, repo: AsyncMock
    ) -> None:
        invoice = _paid(amount_refunded=Decimal("200.00"))

        result, _, _, audit = await self._refund(repo, invoice, amount="300.00")

        assert result.invoice.status is InvoiceStatus.REFUNDED
        assert result.invoice.amount_refunded == Decimal("500.00")
        assert audit.last().changes["status"]["after"] == "refunded"

    async def test_a_part_paid_invoice_can_be_refunded_in_full(self, repo: AsyncMock) -> None:
        # The gap the core path left: a part-paid invoice could not be undone.
        invoice = _issued(status=InvoiceStatus.PARTIALLY_PAID, amount_paid=Decimal("200.00"))

        result, _, _, _ = await self._refund(repo, invoice, amount="200.00")

        assert result.invoice.status is InvoiceStatus.REFUNDED

    async def test_rule_10_a_refund_cannot_exceed_what_is_left(self, repo: AsyncMock) -> None:
        invoice = _paid(amount_refunded=Decimal("200.00"))
        repo.get_invoice_for_update.return_value = invoice
        service, session, audit = _make_service(repo)

        with pytest.raises(RefundExceedsPaidError) as excinfo:
            await service.record_refund(
                HOSPITAL_ID,
                invoice.id,
                build_record_refund_request(amount="300.01"),
                idempotency_key=REFUND_KEY,
                actor_id=ACTOR_ID,
            )

        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == {"amount": "300.01", "refundable_amount": "300.00"}
        repo.create_refund.assert_not_awaited()
        assert invoice.amount_refunded == Decimal("200.00")
        assert session.commits == 0
        assert audit.events == []

    @pytest.mark.parametrize(
        "status",
        [InvoiceStatus.DRAFT, InvoiceStatus.ISSUED, InvoiceStatus.VOID, InvoiceStatus.REFUNDED],
    )
    async def test_only_an_invoice_that_took_money_can_be_refunded(
        self, repo: AsyncMock, status: InvoiceStatus
    ) -> None:
        repo.get_invoice_for_update.return_value = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            status=status,
            invoice_number=None if status is InvoiceStatus.DRAFT else "INV-2026-000007",
        )
        service, session, _ = _make_service(repo)

        with pytest.raises(InvalidInvoiceStateError):
            await service.record_refund(
                HOSPITAL_ID,
                uuid.uuid4(),
                build_record_refund_request(),
                idempotency_key=REFUND_KEY,
                actor_id=ACTOR_ID,
            )

        repo.create_refund.assert_not_awaited()
        assert session.commits == 0

    async def test_unknown_invoice_is_a_404(self, repo: AsyncMock) -> None:
        repo.get_invoice_for_update.return_value = None
        service, _, _ = _make_service(repo)

        with pytest.raises(InvoiceNotFoundError):
            await service.record_refund(
                HOSPITAL_ID,
                uuid.uuid4(),
                build_record_refund_request(),
                idempotency_key=REFUND_KEY,
                actor_id=ACTOR_ID,
            )


class TestRefundIdempotency:
    async def test_a_replay_returns_the_original_and_refunds_nothing_more(
        self, repo: AsyncMock
    ) -> None:
        invoice = _paid(status=InvoiceStatus.REFUNDED, amount_refunded=Decimal("500.00"))
        original = build_refund_model(
            hospital_id=HOSPITAL_ID,
            invoice_id=invoice.id,
            amount=Decimal("500.00"),
            method=PaymentMethod.UPI,
        )
        repo.get_invoice_for_update.return_value = invoice
        repo.get_refund_by_idempotency_key.return_value = original
        service, _, audit = _make_service(repo)

        result, created = await service.record_refund(
            HOSPITAL_ID,
            invoice.id,
            build_record_refund_request(amount="500.00", method="upi"),
            idempotency_key=REFUND_KEY,
            actor_id=ACTOR_ID,
        )

        assert created is False
        assert result.refund.id == original.id
        # Already `refunded`, so a non-replay would have been refused: the
        # replay is recognised before the state check.
        repo.create_refund.assert_not_awaited()
        repo.update_invoice.assert_not_awaited()
        assert audit.events == []

    @pytest.mark.parametrize(
        "difference", [{"amount": "499.00"}, {"method": "cash"}, {"other_invoice": True}]
    )
    async def test_a_key_reused_for_a_different_refund_is_a_409(
        self, repo: AsyncMock, difference: dict[str, Any]
    ) -> None:
        invoice = _paid()
        original = build_refund_model(
            hospital_id=HOSPITAL_ID,
            invoice_id=uuid.uuid4() if difference.get("other_invoice") else invoice.id,
            amount=Decimal("500.00"),
            method=PaymentMethod.UPI,
        )
        repo.get_invoice_for_update.return_value = invoice
        repo.get_refund_by_idempotency_key.return_value = original
        service, session, _ = _make_service(repo)
        body = {"amount": "500.00", "method": "upi"}
        body.update({k: v for k, v in difference.items() if k != "other_invoice"})

        with pytest.raises(IdempotencyKeyReuseError, match="different refund") as excinfo:
            await service.record_refund(
                HOSPITAL_ID,
                invoice.id,
                build_record_refund_request(**body),
                idempotency_key=REFUND_KEY,
                actor_id=ACTOR_ID,
            )

        assert excinfo.value.status_code == 409
        repo.create_refund.assert_not_awaited()
        assert session.commits == 0

    async def test_a_key_racing_in_on_another_invoice_is_a_409(self, repo: AsyncMock) -> None:
        invoice = _paid()
        repo.get_invoice_for_update.return_value = invoice
        repo.create_refund.side_effect = _integrity_error("uq_refunds_hospital_idempotency_key")
        service, session, audit = _make_service(repo)

        with pytest.raises(IdempotencyKeyReuseError):
            await service.record_refund(
                HOSPITAL_ID,
                invoice.id,
                build_record_refund_request(),
                idempotency_key=REFUND_KEY,
                actor_id=ACTOR_ID,
            )

        assert session.savepoints_rolled_back == 1
        assert session.commits == 0
        assert audit.events == []

    async def test_any_other_integrity_error_is_not_swallowed(self, repo: AsyncMock) -> None:
        invoice = _paid()
        repo.get_invoice_for_update.return_value = invoice
        repo.create_refund.side_effect = _integrity_error("ck_invoices_refunded_within_paid")
        service, _, _ = _make_service(repo)

        with pytest.raises(IntegrityError):
            await service.record_refund(
                HOSPITAL_ID,
                invoice.id,
                build_record_refund_request(),
                idempotency_key=REFUND_KEY,
                actor_id=ACTOR_ID,
            )


class TestRefundAndQueueQueries:
    async def test_list_refunds(self, repo: AsyncMock) -> None:
        invoice = _paid()
        repo.get_invoice_by_id.return_value = invoice
        repo.list_refunds.return_value = [build_refund_model(invoice_id=invoice.id)]
        service, _, _ = _make_service(repo)

        refunds = await service.list_refunds(HOSPITAL_ID, invoice.id)

        assert [refund.invoice_id for refund in refunds] == [invoice.id]
        repo.list_refunds.assert_awaited_once_with(HOSPITAL_ID, invoice.id)

    async def test_refunds_are_scoped_through_their_invoice(self, repo: AsyncMock) -> None:
        doctor_id = uuid.uuid4()
        repo.get_invoice_by_id.return_value = None
        service, _, _ = _make_service(repo, doctors=_doctors(doctor_id))

        with pytest.raises(InvoiceNotFoundError):
            await service.list_refunds(HOSPITAL_ID, uuid.uuid4(), own_visits_of=ACTOR_ID)

        assert repo.get_invoice_by_id.await_args.kwargs == {"doctor_id": doctor_id}
        repo.list_refunds.assert_not_awaited()

    async def test_the_approval_queue_filter_reaches_list_and_count(self, repo: AsyncMock) -> None:
        repo.list_invoices.return_value = []
        repo.count_invoices.return_value = 0
        service, _, _ = _make_service(repo)

        await service.list_invoices(HOSPITAL_ID, discount_pending=True)

        assert repo.list_invoices.await_args.kwargs["discount_pending"] is True
        assert repo.count_invoices.await_args.kwargs["discount_pending"] is True


# ── Hospital settings ───────────────────────────────────────────────────────


class TestHospitalBillingContext:
    async def _context(
        self, repo: AsyncMock, hospital: MagicMock | None
    ) -> tuple[str, Decimal, ZoneInfo]:
        hospitals = AsyncMock()
        hospitals.get_by_id.return_value = hospital
        service, _, _ = _make_service(repo, hospitals=hospitals)
        return await service._hospital_billing_context(HOSPITAL_ID)

    async def test_reads_currency_rate_and_timezone(self, repo: AsyncMock) -> None:
        hospital = _hospital(
            {"billing": {"default_tax_rate": "12.5"}}, timezone="Asia/Dubai", currency="AED"
        )

        assert await self._context(repo, hospital) == (
            "AED",
            Decimal("12.5"),
            ZoneInfo("Asia/Dubai"),
        )

    async def test_an_unconfigured_hospital_charges_no_tax(self, repo: AsyncMock) -> None:
        _, rate, _ = await self._context(repo, _hospital({}))

        assert rate == DEFAULT_TAX_RATE == Decimal("0.00")

    @pytest.mark.parametrize(
        "settings",
        [
            {"billing": "not-a-dict"},
            {"billing": {"default_tax_rate": "eighteen"}},
            {"billing": {"default_tax_rate": -1}},
            {"billing": {"default_tax_rate": 100.5}},
            {"billing": {"default_tax_rate": "NaN"}},
            {"billing": {"default_tax_rate": True}},
        ],
    )
    async def test_a_malformed_rate_falls_back_to_zero(
        self, repo: AsyncMock, settings: dict[str, Any]
    ) -> None:
        _, rate, _ = await self._context(repo, _hospital(settings))

        assert rate == DEFAULT_TAX_RATE

    async def test_a_numeric_json_rate_is_accepted(self, repo: AsyncMock) -> None:
        _, rate, _ = await self._context(repo, _hospital({"billing": {"default_tax_rate": 5}}))

        assert rate == Decimal(5)

    async def test_null_settings_are_tolerated(self, repo: AsyncMock) -> None:
        hospital = _hospital()
        hospital.settings = None

        _, rate, _ = await self._context(repo, hospital)

        assert rate == DEFAULT_TAX_RATE

    async def test_an_unknown_timezone_falls_back_to_utc(self, repo: AsyncMock) -> None:
        _, _, zone = await self._context(repo, _hospital(timezone="Mars/Olympus_Mons"))

        assert zone == ZoneInfo("UTC")

    async def test_a_missing_hospital_uses_the_defaults(self, repo: AsyncMock) -> None:
        assert await self._context(repo, None) == (
            DEFAULT_CURRENCY,
            DEFAULT_TAX_RATE,
            ZoneInfo("UTC"),
        )


# ── The appointment seam ────────────────────────────────────────────────────


class TestBillingInvoiceDraftSink:
    def test_satisfies_the_appointment_modules_protocol(self) -> None:
        assert isinstance(BillingInvoiceDraftSink(MagicMock()), InvoiceDraftSink)

    async def test_delegates_to_the_billing_service(self) -> None:
        billing = AsyncMock()
        appointment_id = uuid.uuid4()

        await BillingInvoiceDraftSink(billing).draft_invoice_for(
            HOSPITAL_ID, appointment_id, actor_id=ACTOR_ID
        )

        billing.draft_from_appointment.assert_awaited_once_with(
            HOSPITAL_ID, appointment_id, actor_id=ACTOR_ID
        )

    async def test_a_billing_failure_never_reaches_the_appointment(self) -> None:
        # The consultation is already committed as completed; billing going
        # wrong must not turn that into an error.
        billing = AsyncMock()
        billing.draft_from_appointment.side_effect = RuntimeError("database is down")

        await BillingInvoiceDraftSink(billing).draft_invoice_for(
            HOSPITAL_ID, uuid.uuid4(), actor_id=None
        )


# ── Charges from other modules ──────────────────────────────────────────────


def _charges(*prices: str) -> list[Charge]:
    return [
        Charge(description=f"Lab test — T{index}", unit_price=Decimal(price))
        for index, price in enumerate(prices)
    ]


class TestAddCharges:
    """Billing's side of the charge seam Laboratory and Pharmacy use."""

    def test_the_service_is_a_charge_sink(self, repo: AsyncMock) -> None:
        service, _, _ = _make_service(repo)

        assert isinstance(service, ChargeSink)

    async def _add(self, repo: AsyncMock, **overrides: Any) -> tuple[Any, FakeSession, Any]:
        service, session, audit = _make_service(repo, hospitals=overrides.pop("hospitals", None))
        values: dict[str, Any] = {
            "patient_id": PATIENT_ID,
            "appointment_id": None,
            "charges": _charges("250.00", "400.00"),
            "source": "laboratory",
            "actor_id": ACTOR_ID,
        }
        values.update(overrides)
        return await service.add_charges(HOSPITAL_ID, **values), session, audit

    async def test_no_charges_does_nothing(self, repo: AsyncMock) -> None:
        invoice_id, _, audit = await self._add(repo, charges=[])

        assert invoice_id is None
        repo.create_invoice.assert_not_awaited()
        assert audit.events == []

    async def test_with_no_visit_a_standalone_draft_is_raised(self, repo: AsyncMock) -> None:
        invoice_id, session, audit = await self._add(repo)

        fields = repo.create_invoice.await_args.kwargs
        assert invoice_id is not None
        assert fields["patient_id"] == PATIENT_ID
        assert fields["appointment_id"] is None
        assert fields["consultation_fee_pending"] is False
        assert [line["description"] for line in fields["lines"]] == [
            "Lab test — T0",
            "Lab test — T1",
        ]
        # Untaxed, like the consultation line, for billing staff to correct.
        assert {line["tax_rate"] for line in fields["lines"]} == {Decimal("0")}
        assert fields["subtotal"] == Decimal("650.00")
        assert fields["total"] == Decimal("650.00")
        assert audit.last().action == "invoice.drafted"
        assert audit.last().context["source"] == "laboratory"
        # The charge belongs to the caller's transaction.
        assert session.commits == 0

    async def test_a_visit_with_no_invoice_gets_a_linked_draft(self, repo: AsyncMock) -> None:
        appointment_id = uuid.uuid4()

        await self._add(repo, appointment_id=appointment_id)

        fields = repo.create_invoice.await_args.kwargs
        assert fields["appointment_id"] == appointment_id
        # The visit has not completed yet, so its fee is still to come.
        assert fields["consultation_fee_pending"] is True

    async def test_the_visits_open_draft_takes_the_lines(self, repo: AsyncMock) -> None:
        appointment_id = uuid.uuid4()
        draft = build_invoice_model(hospital_id=HOSPITAL_ID, appointment_id=appointment_id)
        repo.get_live_invoice_for_appointment.return_value = draft
        repo.get_invoice_for_update.return_value = draft

        invoice_id, session, audit = await self._add(repo, appointment_id=appointment_id)

        assert invoice_id == draft.id
        repo.create_invoice.assert_not_awaited()
        assert [item.position for item in draft.items] == [0, 1, 2]
        # 500.00 already there, plus 250.00 and 400.00.
        assert draft.subtotal == Decimal("1150.00")
        assert draft.total == Decimal("1150.00")
        assert audit.last().action == "invoice.charges_added"
        assert audit.last().context == {
            "source": "laboratory",
            "lines_added": 2,
            "total": "1150.00",
        }
        assert session.commits == 0

    async def test_an_issued_visit_invoice_is_left_alone(self, repo: AsyncMock) -> None:
        # AC-1: an issued invoice's lines are frozen. The charge goes on a new
        # draft, which cannot also point at the visit.
        appointment_id = uuid.uuid4()
        issued = _issued(appointment_id=appointment_id)
        repo.get_live_invoice_for_appointment.return_value = issued
        repo.get_invoice_for_update.return_value = issued

        invoice_id, _, _ = await self._add(repo, appointment_id=appointment_id)

        assert invoice_id != issued.id
        assert repo.create_invoice.await_args.kwargs["appointment_id"] is None
        assert repo.create_invoice.await_args.kwargs["consultation_fee_pending"] is False
        repo.append_items.assert_not_awaited()

    async def test_a_draft_issued_while_we_waited_for_the_lock_is_left_alone(
        self, repo: AsyncMock
    ) -> None:
        appointment_id = uuid.uuid4()
        repo.get_live_invoice_for_appointment.return_value = build_invoice_model(
            hospital_id=HOSPITAL_ID, appointment_id=appointment_id
        )
        repo.get_invoice_for_update.return_value = _issued(appointment_id=appointment_id)

        await self._add(repo, appointment_id=appointment_id)

        repo.append_items.assert_not_awaited()
        assert repo.create_invoice.await_args.kwargs["appointment_id"] is None

    async def test_losing_the_race_for_the_visit_falls_back_to_a_standalone_draft(
        self, repo: AsyncMock
    ) -> None:
        appointment_id = uuid.uuid4()
        create = repo.create_invoice.side_effect
        calls: list[Any] = []

        async def racing(**fields: Any) -> Any:
            calls.append(fields["appointment_id"])
            if len(calls) == 1:
                raise _integrity_error("uq_invoices_live_appointment")
            return await create(**fields)

        repo.create_invoice.side_effect = racing

        invoice_id, _, audit = await self._add(repo, appointment_id=appointment_id)

        assert invoice_id is not None
        assert calls == [appointment_id, None]
        assert audit.last().action == "invoice.drafted"

    async def test_more_lines_reopen_the_question_of_an_approved_discount(
        self, repo: AsyncMock
    ) -> None:
        appointment_id = uuid.uuid4()
        approver = uuid.uuid4()
        draft = build_invoice_model(
            hospital_id=HOSPITAL_ID,
            appointment_id=appointment_id,
            discount_amount=Decimal("200.00"),
            discount_reason="Hardship",
            discount_approved_by=approver,
            total=Decimal("300.00"),
        )
        repo.get_live_invoice_for_appointment.return_value = draft
        repo.get_invoice_for_update.return_value = draft

        await self._add(repo, appointment_id=appointment_id, hospitals=_with_threshold("10"))

        # 200.00 off 1150.00 is still above 10%, and the approval is withdrawn.
        assert draft.total == Decimal("950.00")
        assert draft.discount_pending_approval is True
        assert draft.discount_approved_by is None

    async def test_a_long_description_is_cut_to_the_column_width(self, repo: AsyncMock) -> None:
        await self._add(repo, charges=[Charge(description="x" * 500, unit_price=Decimal("1.00"))])

        [line] = repo.create_invoice.await_args.kwargs["lines"]
        assert len(line["description"]) == 200

    async def test_quantity_multiplies_the_line(self, repo: AsyncMock) -> None:
        await self._add(
            repo,
            charges=[
                Charge(
                    description="Paracetamol 500mg",
                    unit_price=Decimal("2.50"),
                    quantity=Decimal(10),
                )
            ],
            source="pharmacy",
        )

        fields = repo.create_invoice.await_args.kwargs
        assert fields["lines"][0]["line_total"] == Decimal("25.00")
        assert fields["total"] == Decimal("25.00")
