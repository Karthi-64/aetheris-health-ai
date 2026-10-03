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

from app.core.exceptions import BusinessRuleError, ConfigurationError, ValidationError
from app.models.billing import InvoiceStatus, PaymentMethod
from app.services.appointment_service import InvoiceDraftSink
from app.services.billing_service import (
    DEFAULT_CURRENCY,
    DEFAULT_TAX_RATE,
    BillingInvoiceDraftSink,
    BillingService,
    CashOnlyPaymentError,
    DuplicateAppointmentInvoiceError,
    IdempotencyKeyReuseError,
    InvalidInvoiceStateError,
    InvoiceNotFoundError,
    OverpaymentError,
)
from app.tests.conftest import FakeSession, RecordingAuditSink
from app.tests.factories import (
    build_create_invoice_request,
    build_invoice_item_model,
    build_invoice_model,
    build_payment_model,
    build_record_payment_request,
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

    repo.create_invoice.side_effect = create_invoice
    repo.update_invoice.side_effect = update_invoice
    repo.replace_items.side_effect = replace_items
    repo.create_payment.side_effect = create_payment
    repo.get_payment_by_idempotency_key.return_value = None
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
