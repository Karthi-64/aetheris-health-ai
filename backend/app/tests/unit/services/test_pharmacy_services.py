"""Unit tests for the three Pharmacy services.

The repositories are small in-memory fakes rather than mocks: what matters is
the state a step leaves behind — what is on the shelf, what has been
dispensed, what the ledger says — and that reads more clearly as assertions on
rows than on call arguments. The SQL, including the locking, is covered in the
repository and integration suites.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import BusinessRuleError, ValidationError
from app.models.appointment import AppointmentStatus
from app.models.pharmacy import (
    Dispense,
    DispenseItem,
    Medicine,
    MedicineBatch,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    StockMovement,
    StockMovementReason,
    Vendor,
)
from app.schemas.common import PaginationParams
from app.schemas.pharmacy import (
    AdjustStockRequest,
    CancelPrescriptionRequest,
    CreateMedicineRequest,
    CreatePrescriptionRequest,
    CreatePurchaseOrderRequest,
    CreateVendorRequest,
    DispenseRequest,
    ReceiveBatchRequest,
    ReceivePurchaseOrderRequest,
    UpdateBatchRequest,
    UpdateMedicineRequest,
    UpdateVendorRequest,
)
from app.services import pharmacy_common
from app.services.dispensing_service import (
    DispensingService,
    InsufficientStockError,
    PrescriptionNotFoundError,
    PrescriptionStateError,
)
from app.services.pharmacy_catalog_service import (
    BatchNotFoundError,
    DuplicateMedicineSkuError,
    PharmacyCatalogService,
)
from app.services.pharmacy_common import MedicineNotFoundError, hospital_today
from app.services.procurement_service import (
    DuplicateVendorNameError,
    ProcurementService,
    PurchaseOrderNotFoundError,
    PurchaseOrderStateError,
    VendorNotFoundError,
)
from app.tests.conftest import FakeSession, RecordingAuditSink

HOSPITAL_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()
INVOICE_ID = uuid.uuid4()
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
TODAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin "today" for every service, so expiry rules are tested on a known day."""

    async def today(*_: Any) -> date:
        return TODAY

    for module in ("pharmacy_catalog_service", "dispensing_service", "procurement_service"):
        monkeypatch.setattr(f"app.services.{module}.hospital_today", today)


# ── Fakes ───────────────────────────────────────────────────────────────────


class FakeMedicines:
    """In-memory stand-in for ``MedicineRepository``."""

    def __init__(self) -> None:
        self.medicines: dict[uuid.UUID, Medicine] = {}
        self.batches: dict[uuid.UUID, MedicineBatch] = {}
        self.movements: list[StockMovement] = []
        self.raise_on_create: Exception | None = None
        self.lock_calls: list[uuid.UUID] = []

    def add(self, sku: str, *, price: str = "2.50", **overrides: Any) -> Medicine:
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "hospital_id": HOSPITAL_ID,
            "sku": sku,
            "name": sku.title(),
            "generic_name": None,
            "strength": "500 mg",
            "form": "tablet",
            "atc_code": None,
            "unit_price": Decimal(price),
            "requires_prescription": True,
            "is_active": True,
            "created_at": NOW,
            "updated_at": NOW,
        }
        values.update(overrides)
        medicine = Medicine(**values)
        self.medicines[medicine.id] = medicine
        return medicine

    def stock(
        self,
        medicine: Medicine,
        number: str,
        quantity: int,
        *,
        days: int = 365,
        recalled: bool = False,
    ) -> MedicineBatch:
        batch = MedicineBatch(
            id=uuid.uuid4(),
            hospital_id=medicine.hospital_id,
            medicine_id=medicine.id,
            batch_number=number,
            expiry_date=TODAY + timedelta(days=days),
            cost_per_unit=Decimal("1.00"),
            initial_quantity=quantity,
            quantity_on_hand=quantity,
            is_recalled=recalled,
        )
        batch.__dict__["medicine"] = medicine
        self.batches[batch.id] = batch
        return batch

    async def create_medicine(self, **fields: Any) -> Medicine:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        fields.pop("created_by", None)
        fields.pop("hospital_id", None)
        sku = fields.pop("sku")
        price = fields.pop("unit_price")
        return self.add(sku, price=str(price), **fields)

    async def update_medicine(self, medicine: Medicine, *, updated_by: Any = None, **f: Any) -> Any:
        for name, value in f.items():
            setattr(medicine, name, value)
        return medicine

    async def get_medicine_by_id(self, hospital_id: uuid.UUID, medicine_id: uuid.UUID) -> Any:
        medicine = self.medicines.get(medicine_id)
        return medicine if medicine is not None and medicine.hospital_id == hospital_id else None

    async def get_medicines_by_ids(self, hospital_id: uuid.UUID, ids: Any) -> list[Medicine]:
        return [m for m in self.medicines.values() if m.id in ids and m.hospital_id == hospital_id]

    async def get_medicine_by_sku(self, hospital_id: uuid.UUID, sku: str) -> Any:
        return next((m for m in self.medicines.values() if m.sku == sku), None)

    async def list_medicines(
        self, hospital_id: uuid.UUID, *, skip: int, limit: int, **_: Any
    ) -> Any:
        return list(self.medicines.values())[skip : skip + limit]

    async def count_medicines(self, hospital_id: uuid.UUID, **_: Any) -> int:
        return len(self.medicines)

    async def create_batch(self, *, medicine: Medicine, **fields: Any) -> MedicineBatch:
        fields.pop("created_by", None)
        batch = self.stock(medicine, fields["batch_number"], 0)
        batch.expiry_date = fields["expiry_date"]
        batch.cost_per_unit = fields["cost_per_unit"]
        return batch

    async def update_batch(self, batch: MedicineBatch, *, updated_by: Any = None, **f: Any) -> Any:
        for name, value in f.items():
            setattr(batch, name, value)
        return batch

    async def apply_movement(self, batch: MedicineBatch, **fields: Any) -> StockMovement:
        movement = StockMovement(
            id=uuid.uuid4(), hospital_id=batch.hospital_id, batch_id=batch.id, **fields
        )
        self.movements.append(movement)
        batch.quantity_on_hand += fields["quantity_change"]
        if fields["reason"] is StockMovementReason.RECEIVED:
            batch.initial_quantity += fields["quantity_change"]
        return movement

    async def get_batch_by_id(self, hospital_id: uuid.UUID, batch_id: uuid.UUID, **_: Any) -> Any:
        batch = self.batches.get(batch_id)
        return batch if batch is not None and batch.hospital_id == hospital_id else None

    async def get_batch_by_number(
        self, hospital_id: uuid.UUID, medicine_id: uuid.UUID, n: str
    ) -> Any:
        return next(
            (
                b
                for b in self.batches.values()
                if b.medicine_id == medicine_id and b.batch_number == n
            ),
            None,
        )

    async def list_batches(self, hospital_id: uuid.UUID, medicine_id: uuid.UUID, **o: Any) -> Any:
        rows = [b for b in self.batches.values() if b.medicine_id == medicine_id]
        if o.get("in_stock_only"):
            rows = [b for b in rows if b.quantity_on_hand > 0]
        return sorted(rows, key=lambda b: b.expiry_date)

    def _dispensable(self, medicine_id: uuid.UUID, on: date) -> list[MedicineBatch]:
        return sorted(
            (
                b
                for b in self.batches.values()
                if b.medicine_id == medicine_id
                and b.quantity_on_hand > 0
                and b.expiry_date >= on
                and not b.is_recalled
            ),
            key=lambda b: b.expiry_date,
        )

    async def lock_dispensable_batches(
        self, hospital_id: uuid.UUID, medicine_id: uuid.UUID, *, on: date
    ) -> Any:
        self.lock_calls.append(medicine_id)
        return self._dispensable(medicine_id, on)

    async def dispensable_totals(self, hospital_id: uuid.UUID, ids: Any, *, on: date) -> Any:
        totals = {i: sum(b.quantity_on_hand for b in self._dispensable(i, on)) for i in ids}
        return {i: total for i, total in totals.items() if total}


def _people(row: Any) -> None:
    """Attach patient and doctor doubles to a detached prescription."""
    patient = MagicMock()
    patient.full_name = "Ananya Rao"
    patient.mrn = "MRN-2026-00001"
    doctor = MagicMock()
    doctor.user.first_name = "Meera"
    doctor.user.last_name = "Iyer"
    row.__dict__["patient"] = patient
    row.__dict__["doctor"] = doctor


class FakePrescriptions:
    """In-memory stand-in for ``PrescriptionRepository``."""

    def __init__(self, medicines: FakeMedicines) -> None:
        self._medicines = medicines
        self.prescriptions: dict[uuid.UUID, Prescription] = {}
        self.dispenses: list[Dispense] = []

    async def create_prescription(self, *, items: list[dict[str, Any]], **fields: Any) -> Any:
        fields.pop("created_by", None)
        prescription = Prescription(
            id=uuid.uuid4(),
            status=PrescriptionStatus.ACTIVE,
            items=[
                PrescriptionItem(
                    id=uuid.uuid4(),
                    hospital_id=fields["hospital_id"],
                    position=position,
                    quantity_dispensed=0,
                    **values,
                )
                for position, values in enumerate(items)
            ],
            **fields,
        )
        _people(prescription)
        self.prescriptions[prescription.id] = prescription
        return prescription

    async def update_prescription(
        self, prescription: Any, *, updated_by: Any = None, **f: Any
    ) -> Any:
        for name, value in f.items():
            setattr(prescription, name, value)
        return prescription

    async def record_dispensed(self, item: Any, quantity: int, *, updated_by: Any = None) -> None:
        item.quantity_dispensed += quantity

    async def create_dispense(
        self, *, prescription: Any, lines: list[dict[str, Any]], **f: Any
    ) -> Any:
        dispense = Dispense(
            id=uuid.uuid4(),
            hospital_id=prescription.hospital_id,
            prescription_id=prescription.id,
            invoice_id=None,
            items=[],
            **f,
        )
        for values in lines:
            line = DispenseItem(id=uuid.uuid4(), hospital_id=prescription.hospital_id, **values)
            line.__dict__["batch"] = self._medicines.batches[values["batch_id"]]
            dispense.items.append(line)
        self.dispenses.append(dispense)
        return dispense

    async def set_dispense_invoice(self, dispense: Any, invoice_id: uuid.UUID) -> Any:
        dispense.invoice_id = invoice_id
        return dispense

    async def get_prescription_by_id(self, hospital_id: uuid.UUID, pid: uuid.UUID, **_: Any) -> Any:
        row = self.prescriptions.get(pid)
        return row if row is not None and row.hospital_id == hospital_id else None

    async def list_prescriptions(
        self, hospital_id: uuid.UUID, *, skip: int, limit: int, **f: Any
    ) -> Any:
        statuses = f.get("statuses")
        rows = [p for p in self.prescriptions.values() if not statuses or p.status in statuses]
        return rows[skip : skip + limit]

    async def count_prescriptions(self, hospital_id: uuid.UUID, **f: Any) -> int:
        statuses = f.get("statuses")
        return len([p for p in self.prescriptions.values() if not statuses or p.status in statuses])

    async def list_dispenses(self, hospital_id: uuid.UUID, prescription_id: uuid.UUID) -> Any:
        return [d for d in self.dispenses if d.prescription_id == prescription_id]


class _Pharmacy:
    """A dispensing service over fakes, with a stocked shelf."""

    def __init__(self, *, visit_status: AppointmentStatus = AppointmentStatus.IN_PROGRESS) -> None:
        self.medicines = FakeMedicines()
        self.prescriptions = FakePrescriptions(self.medicines)
        self.session = FakeSession()
        self.audit = RecordingAuditSink()
        self.para = self.medicines.add("PARA", price="2.50", name="Paracetamol")
        self.amox = self.medicines.add("AMOX", price="8.00", name="Amoxicillin")

        self.appointment = MagicMock()
        self.appointment.id = uuid.uuid4()
        self.appointment.patient_id = uuid.uuid4()
        self.appointment.doctor_id = uuid.uuid4()
        self.appointment.status = visit_status
        appointments = AsyncMock()
        appointments.get_appointment_by_id.return_value = self.appointment
        self.appointments = appointments

        self.charges = AsyncMock()
        self.charges.add_charges.return_value = INVOICE_ID
        self.service = DispensingService(
            self.prescriptions,  # type: ignore[arg-type]
            self.medicines,  # type: ignore[arg-type]
            appointments,
            AsyncMock(),
            self.session,  # type: ignore[arg-type]
            self.audit,
            charges=self.charges,
        )

    async def prescribe(self, *lines: tuple[Any, int], notes: str | None = None) -> Any:
        items = []
        for medicine, quantity in lines:
            line: dict[str, Any] = {
                "dosage": "1 tablet",
                "frequency": "twice daily",
                "quantity": quantity,
            }
            if isinstance(medicine, str):
                line["medicine_name"] = medicine
            else:
                line["medicine_id"] = str(medicine.id)
            items.append(line)
        return await self.service.create_prescription(
            HOSPITAL_ID,
            CreatePrescriptionRequest.model_validate(
                {"appointment_id": str(self.appointment.id), "notes": notes, "items": items}
            ),
            actor_id=ACTOR_ID,
        )

    async def dispense(self, prescription: Any, body: dict[str, Any] | None = None) -> Any:
        return await self.service.dispense(
            HOSPITAL_ID,
            prescription.id,
            DispenseRequest.model_validate(body) if body is not None else None,
            actor_id=ACTOR_ID,
        )

    def status_of(self, prescription: Any) -> PrescriptionStatus:
        return self.prescriptions.prescriptions[prescription.id].status


# ── Prescribing ─────────────────────────────────────────────────────────────


class TestCreatePrescription:
    async def test_the_patient_and_doctor_come_from_the_visit(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100)

        rx = await pharmacy.prescribe(
            (pharmacy.para, 10), ("Vitamin D drops", 1), notes="Review in a week."
        )

        assert rx.status is PrescriptionStatus.ACTIVE
        assert rx.patient_id == pharmacy.appointment.patient_id
        assert rx.doctor_id == pharmacy.appointment.doctor_id
        assert rx.doctor_name == "Dr. Meera Iyer"
        assert rx.notes == "Review in a week."
        catalog_line, free_text = rx.items
        # A catalog line takes its name from the medicine; free text keeps its own.
        assert catalog_line.medicine_name == "Paracetamol 500 mg"
        assert (catalog_line.quantity, catalog_line.quantity_remaining) == (10, 10)
        assert catalog_line.available_quantity == 100
        assert free_text.medicine_id is None
        assert free_text.medicine_name == "Vitamin D drops"
        assert free_text.available_quantity is None
        assert pharmacy.session.commits == 1
        event = pharmacy.audit.last()
        assert event.action == "pharmacy.prescription.created"
        assert event.context["line_count"] == 2
        assert event.context["free_text_lines"] == 1

    async def test_an_unknown_appointment_is_a_422(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.appointments.get_appointment_by_id.return_value = None

        with pytest.raises(ValidationError) as excinfo:
            await pharmacy.prescribe((pharmacy.para, 1))

        assert excinfo.value.detail["errors"][0]["field"] == "appointment_id"

    @pytest.mark.parametrize("status", [AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW])
    async def test_a_visit_that_did_not_happen_cannot_be_prescribed_for(
        self, status: AppointmentStatus
    ) -> None:
        pharmacy = _Pharmacy(visit_status=status)

        with pytest.raises(ValidationError, match=status.value):
            await pharmacy.prescribe((pharmacy.para, 1))

    async def test_an_unknown_or_inactive_medicine_is_a_422_naming_the_line(self) -> None:
        pharmacy = _Pharmacy()
        retired = pharmacy.medicines.add("OLD", is_active=False)
        foreign = pharmacy.medicines.add("FOREIGN", hospital_id=uuid.uuid4())

        with pytest.raises(ValidationError) as inactive:
            await pharmacy.prescribe((pharmacy.para, 1), (retired, 1))
        with pytest.raises(ValidationError) as unknown:
            await pharmacy.prescribe((foreign, 1))

        assert inactive.value.detail["errors"][0]["field"] == "items.1.medicine_id"
        assert "inactive" in inactive.value.message
        assert unknown.value.detail["errors"][0]["field"] == "items.0.medicine_id"
        assert pharmacy.prescriptions.prescriptions == {}


class TestCancelPrescription:
    async def test_an_untouched_prescription_can_be_cancelled(self) -> None:
        pharmacy = _Pharmacy()
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        cancelled = await pharmacy.service.cancel_prescription(
            HOSPITAL_ID,
            rx.id,
            CancelPrescriptionRequest(reason="Allergy reported"),
            actor_id=ACTOR_ID,
        )

        assert cancelled.status is PrescriptionStatus.CANCELLED
        assert cancelled.cancel_reason == "Allergy reported"
        assert pharmacy.audit.last().action == "pharmacy.prescription.cancelled"

    async def test_once_anything_is_dispensed_it_cannot_be_cancelled(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        rx = await pharmacy.prescribe((pharmacy.para, 10))
        await pharmacy.dispense(rx)

        with pytest.raises(PrescriptionStateError, match="dispensed"):
            await pharmacy.service.cancel_prescription(
                HOSPITAL_ID, rx.id, CancelPrescriptionRequest(reason="x")
            )

    async def test_an_unknown_prescription_is_a_404(self) -> None:
        with pytest.raises(PrescriptionNotFoundError):
            await _Pharmacy().service.cancel_prescription(
                HOSPITAL_ID, uuid.uuid4(), CancelPrescriptionRequest(reason="x")
            )


# ── Dispensing ──────────────────────────────────────────────────────────────


class TestDispense:
    async def test_a_full_dispense_moves_stock_bills_and_closes_the_prescription(self) -> None:
        pharmacy = _Pharmacy()
        batch = pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        result = await pharmacy.dispense(rx)

        assert batch.quantity_on_hand == 90
        [line] = result.items
        assert (line.batch_number, line.quantity) == ("P1", 10)
        assert (line.unit_price, line.total) == (Decimal("2.50"), Decimal("25.00"))
        assert result.total_amount == Decimal("25.00")
        assert result.invoice_id == INVOICE_ID
        assert result.warnings == []
        assert pharmacy.status_of(rx) is PrescriptionStatus.DISPENSED
        [movement] = pharmacy.medicines.movements
        assert movement.quantity_change == -10
        assert movement.reason is StockMovementReason.DISPENSED
        assert (movement.reference_type, movement.reference_id) == ("dispense", result.id)
        assert pharmacy.session.commits == 2  # the prescription, then the dispense

    async def test_rule_6_every_dispense_is_charged_once_per_prescription_line(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 6)
        pharmacy.medicines.stock(pharmacy.para, "P2", 100, days=400)
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        await pharmacy.dispense(rx)

        call = pharmacy.charges.add_charges.await_args
        assert call.kwargs["patient_id"] == pharmacy.appointment.patient_id
        assert call.kwargs["appointment_id"] == pharmacy.appointment.id
        assert call.kwargs["source"] == "pharmacy"
        # One bill line although the units came from two batches.
        [charge] = call.kwargs["charges"]
        assert charge.description == "Medicine — Paracetamol 500 mg"
        assert (charge.unit_price, charge.quantity) == (Decimal("2.50"), Decimal(10))

    async def test_ac2_stock_leaves_first_expiry_first_across_batches(self) -> None:
        pharmacy = _Pharmacy()
        late = pharmacy.medicines.stock(pharmacy.para, "LATE", 100, days=300)
        soon = pharmacy.medicines.stock(pharmacy.para, "SOON", 4, days=60)
        middle = pharmacy.medicines.stock(pharmacy.para, "MID", 3, days=120)
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        result = await pharmacy.dispense(rx)

        assert [(i.batch_number, i.quantity) for i in result.items] == [
            ("SOON", 4),
            ("MID", 3),
            ("LATE", 3),
        ]
        assert (soon.quantity_on_hand, middle.quantity_on_hand, late.quantity_on_hand) == (0, 0, 97)

    async def test_later_batches_are_not_touched_once_the_line_is_filled(self) -> None:
        pharmacy = _Pharmacy()
        soon = pharmacy.medicines.stock(pharmacy.para, "SOON", 50, days=60)
        late = pharmacy.medicines.stock(pharmacy.para, "LATE", 50, days=300)
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        result = await pharmacy.dispense(rx)

        assert [(i.batch_number, i.quantity) for i in result.items] == [("SOON", 10)]
        assert (soon.quantity_on_hand, late.quantity_on_hand) == (40, 50)

    async def test_ac3_a_shortage_fails_the_whole_dispense_and_writes_nothing(self) -> None:
        pharmacy = _Pharmacy()
        plenty = pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        scarce = pharmacy.medicines.stock(pharmacy.amox, "A1", 5)
        rx = await pharmacy.prescribe((pharmacy.para, 10), (pharmacy.amox, 21))
        commits = pharmacy.session.commits

        with pytest.raises(InsufficientStockError) as excinfo:
            await pharmacy.dispense(rx)

        assert excinfo.value.status_code == 409
        assert excinfo.value.detail["shortages"] == [
            {
                "prescription_item_id": str(rx.items[1].id),
                "medicine": "Amoxicillin 500 mg",
                "requested": 21,
                "available": 5,
            }
        ]
        # The medicine that was in stock was not touched either.
        assert (plenty.quantity_on_hand, scarce.quantity_on_hand) == (100, 5)
        assert pharmacy.medicines.movements == []
        assert pharmacy.prescriptions.dispenses == []
        assert pharmacy.status_of(rx) is PrescriptionStatus.ACTIVE
        pharmacy.charges.add_charges.assert_not_awaited()
        assert pharmacy.session.commits == commits

    async def test_ac5_an_expired_batch_is_never_dispensed(self) -> None:
        pharmacy = _Pharmacy()
        expired = pharmacy.medicines.stock(pharmacy.para, "OLD", 100, days=-1)
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        with pytest.raises(InsufficientStockError) as excinfo:
            await pharmacy.dispense(rx)

        assert excinfo.value.detail["shortages"][0]["available"] == 0
        assert expired.quantity_on_hand == 100

    async def test_a_batch_expiring_today_is_dispensable_with_a_warning(self) -> None:
        # §14: "expiry today at midnight — dispensable, warning shown".
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "TODAY", 100, days=0)
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        result = await pharmacy.dispense(rx)

        assert result.items[0].batch_number == "TODAY"
        assert result.warnings == ["Paracetamol batch TODAY expires today (2026-10-05)."]

    @pytest.mark.parametrize(("days", "warned"), [(30, True), (31, False)])
    async def test_rule_4_a_batch_within_thirty_days_of_expiry_is_warned_about(
        self, days: int, warned: bool
    ) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100, days=days)
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        result = await pharmacy.dispense(rx)

        assert bool(result.warnings) is warned

    async def test_a_recalled_batch_is_skipped(self) -> None:
        # §14: "dispensing during a batch recall → block".
        pharmacy = _Pharmacy()
        recalled = pharmacy.medicines.stock(pharmacy.para, "BAD", 100, days=30, recalled=True)
        good = pharmacy.medicines.stock(pharmacy.para, "GOOD", 100, days=300)
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        result = await pharmacy.dispense(rx)

        assert [i.batch_number for i in result.items] == ["GOOD"]
        assert (recalled.quantity_on_hand, good.quantity_on_hand) == (100, 90)

    async def test_medicines_are_locked_in_a_fixed_order_whatever_the_prescription_says(
        self,
    ) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        pharmacy.medicines.stock(pharmacy.amox, "A1", 100)
        forwards = await pharmacy.prescribe((pharmacy.para, 1), (pharmacy.amox, 1))
        backwards = await pharmacy.prescribe((pharmacy.amox, 1), (pharmacy.para, 1))

        await pharmacy.dispense(forwards)
        first = list(pharmacy.medicines.lock_calls)
        pharmacy.medicines.lock_calls.clear()
        await pharmacy.dispense(backwards)

        # The same sequence both times is what makes a deadlock impossible.
        assert pharmacy.medicines.lock_calls == first == sorted(first)

    async def test_free_text_lines_are_left_alone_and_do_not_hold_it_open(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        rx = await pharmacy.prescribe((pharmacy.para, 10), ("Vitamin D drops", 1))

        result = await pharmacy.dispense(rx)

        assert len(result.items) == 1
        assert pharmacy.status_of(rx) is PrescriptionStatus.DISPENSED

    async def test_with_no_charge_sink_nothing_is_billed(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        rx = await pharmacy.prescribe((pharmacy.para, 10))
        service = DispensingService(
            pharmacy.prescriptions,  # type: ignore[arg-type]
            pharmacy.medicines,  # type: ignore[arg-type]
            pharmacy.appointments,
            AsyncMock(),
            pharmacy.session,  # type: ignore[arg-type]
            pharmacy.audit,
        )

        result = await service.dispense(HOSPITAL_ID, rx.id, actor_id=ACTOR_ID)

        assert result.invoice_id is None

    async def test_a_failed_charge_fails_the_dispense(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        rx = await pharmacy.prescribe((pharmacy.para, 10))
        pharmacy.charges.add_charges.side_effect = RuntimeError("billing is down")
        commits = pharmacy.session.commits

        with pytest.raises(RuntimeError):
            await pharmacy.dispense(rx)

        # Not committed: the request's transaction rolls the stock back.
        assert pharmacy.session.commits == commits

    async def test_the_audit_names_batches_and_quantities(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        result = await pharmacy.dispense(rx)

        event = pharmacy.audit.last()
        assert event.action == "pharmacy.dispensed"
        assert event.target_id == rx.id
        assert event.changes == {"status": {"before": "active", "after": "dispensed"}}
        assert event.context == {
            "dispense_id": str(result.id),
            "total_amount": "25.00",
            "invoice_id": str(INVOICE_ID),
            "lines": [{"medicine": "PARA", "batch": "P1", "quantity": 10}],
        }

    async def test_a_medicine_deactivated_since_it_was_prescribed_cannot_be_dispensed(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        rx = await pharmacy.prescribe((pharmacy.para, 10))
        pharmacy.para.is_active = False

        with pytest.raises(BusinessRuleError, match="deactivated"):
            await pharmacy.dispense(rx)

    async def test_an_unknown_prescription_is_a_404(self) -> None:
        with pytest.raises(PrescriptionNotFoundError):
            await _Pharmacy().service.dispense(HOSPITAL_ID, uuid.uuid4())

    async def test_another_hospitals_prescription_is_a_404(self) -> None:
        pharmacy = _Pharmacy()
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        with pytest.raises(PrescriptionNotFoundError):
            await pharmacy.service.dispense(uuid.uuid4(), rx.id)


class TestPartialDispense:
    async def _setup(self) -> tuple[_Pharmacy, Any]:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        pharmacy.medicines.stock(pharmacy.amox, "A1", 100)
        return pharmacy, await pharmacy.prescribe((pharmacy.para, 10), (pharmacy.amox, 21))

    async def test_rule_5_a_partial_dispense_needs_a_reason(self) -> None:
        pharmacy, rx = await self._setup()
        body = {"items": [{"prescription_item_id": str(rx.items[0].id), "quantity": 10}]}

        with pytest.raises(ValidationError) as excinfo:
            await pharmacy.dispense(rx, body)

        assert excinfo.value.detail["errors"][0]["field"] == "notes"
        assert pharmacy.medicines.movements == []

    async def test_a_partial_dispense_leaves_the_rest_for_later(self) -> None:
        pharmacy, rx = await self._setup()

        first = await pharmacy.dispense(
            rx,
            {
                "items": [
                    {"prescription_item_id": str(rx.items[0].id), "quantity": 4},
                    {"prescription_item_id": str(rx.items[1].id), "quantity": 21},
                ],
                "notes": "Patient will collect the rest tomorrow.",
            },
        )

        assert first.notes == "Patient will collect the rest tomorrow."
        assert pharmacy.status_of(rx) is PrescriptionStatus.PARTIALLY_DISPENSED
        stored = pharmacy.prescriptions.prescriptions[rx.id]
        assert [(i.quantity_dispensed, i.quantity_remaining) for i in stored.items] == [
            (4, 6),
            (21, 0),
        ]

        # The rest needs no reason: it completes the prescription.
        second = await pharmacy.dispense(rx)
        assert [(i.medicine_name, i.quantity) for i in second.items] == [("Paracetamol", 6)]
        assert pharmacy.status_of(rx) is PrescriptionStatus.DISPENSED
        assert len(await pharmacy.service.list_dispenses(HOSPITAL_ID, rx.id)) == 2

    async def test_naming_every_line_in_full_is_not_partial(self) -> None:
        pharmacy, rx = await self._setup()

        await pharmacy.dispense(
            rx,
            {
                "items": [
                    {"prescription_item_id": str(rx.items[0].id), "quantity": 10},
                    {"prescription_item_id": str(rx.items[1].id), "quantity": 21},
                ]
            },
        )

        assert pharmacy.status_of(rx) is PrescriptionStatus.DISPENSED

    async def test_more_than_is_outstanding_is_a_422(self) -> None:
        # §11: "dispense quantity ≤ prescribed quantity".
        pharmacy, rx = await self._setup()

        with pytest.raises(ValidationError) as excinfo:
            await pharmacy.dispense(
                rx,
                {
                    "items": [{"prescription_item_id": str(rx.items[0].id), "quantity": 11}],
                    "notes": "x",
                },
            )

        assert excinfo.value.detail["errors"][0]["field"] == "items.0.quantity"

    async def test_a_line_from_another_prescription_is_a_422(self) -> None:
        pharmacy, rx = await self._setup()

        with pytest.raises(ValidationError) as excinfo:
            await pharmacy.dispense(
                rx,
                {
                    "items": [{"prescription_item_id": str(uuid.uuid4()), "quantity": 1}],
                    "notes": "x",
                },
            )

        assert excinfo.value.detail["errors"][0]["field"] == "items.0.prescription_item_id"

    async def test_a_free_text_line_cannot_be_dispensed(self) -> None:
        pharmacy = _Pharmacy()
        rx = await pharmacy.prescribe(("Vitamin D drops", 1), (pharmacy.para, 1))

        with pytest.raises(ValidationError, match="free-text"):
            await pharmacy.dispense(
                rx,
                {
                    "items": [{"prescription_item_id": str(rx.items[0].id), "quantity": 1}],
                    "notes": "x",
                },
            )

    async def test_a_finished_or_cancelled_prescription_cannot_be_dispensed(self) -> None:
        pharmacy, rx = await self._setup()
        await pharmacy.dispense(rx)

        with pytest.raises(PrescriptionStateError, match="dispensed"):
            await pharmacy.dispense(rx)

    async def test_a_prescription_of_only_free_text_has_nothing_to_dispense(self) -> None:
        pharmacy = _Pharmacy()
        rx = await pharmacy.prescribe(("Vitamin D drops", 1))

        with pytest.raises(BusinessRuleError, match="Nothing"):
            await pharmacy.dispense(rx)


class TestPrescriptionQueries:
    async def test_get_shows_what_is_in_stock_now(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 7)
        pharmacy.medicines.stock(pharmacy.para, "OLD", 50, days=-3)
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        fetched = await pharmacy.service.get_prescription(HOSPITAL_ID, rx.id)

        # The expired fifty are not "available".
        assert fetched.items[0].available_quantity == 7

    async def test_get_and_dispense_history_in_another_hospital_are_404s(self) -> None:
        pharmacy = _Pharmacy()
        rx = await pharmacy.prescribe((pharmacy.para, 10))

        with pytest.raises(PrescriptionNotFoundError):
            await pharmacy.service.get_prescription(uuid.uuid4(), rx.id)
        with pytest.raises(PrescriptionNotFoundError):
            await pharmacy.service.list_dispenses(uuid.uuid4(), rx.id)

    async def test_the_pending_queue_holds_only_what_is_left_to_dispense(self) -> None:
        pharmacy = _Pharmacy()
        pharmacy.medicines.stock(pharmacy.para, "P1", 100)
        done = await pharmacy.prescribe((pharmacy.para, 1))
        await pharmacy.dispense(done)
        waiting = await pharmacy.prescribe((pharmacy.para, 5), (pharmacy.amox, 5))

        queue = await pharmacy.service.list_prescriptions(HOSPITAL_ID, pending_only=True)
        everything = await pharmacy.service.list_prescriptions(
            HOSPITAL_ID, pagination=PaginationParams(page=1, page_size=1)
        )
        finished = await pharmacy.service.list_prescriptions(
            HOSPITAL_ID, status=PrescriptionStatus.DISPENSED
        )

        assert [p.id for p in queue.items] == [waiting.id]
        assert [i.available_quantity for i in queue.items[0].items] == [99, 0]
        assert (len(everything.items), everything.total_records) == (1, 2)
        assert [p.id for p in finished.items] == [done.id]


# ── Catalog and stock ───────────────────────────────────────────────────────


def _catalog() -> tuple[PharmacyCatalogService, FakeMedicines, FakeSession, RecordingAuditSink]:
    medicines = FakeMedicines()
    session = FakeSession()
    audit = RecordingAuditSink()
    service = PharmacyCatalogService(medicines, AsyncMock(), session, audit)  # type: ignore[arg-type]
    return service, medicines, session, audit


def _new_medicine(**overrides: Any) -> CreateMedicineRequest:
    values: dict[str, Any] = {"sku": "para-500", "name": "Paracetamol", "unit_price": "2.50"}
    values.update(overrides)
    return CreateMedicineRequest.model_validate(values)


def _receipt(**overrides: Any) -> ReceiveBatchRequest:
    values: dict[str, Any] = {
        "batch_number": "b1",
        "expiry_date": (TODAY + timedelta(days=200)).isoformat(),
        "quantity": 50,
        "cost_per_unit": "1.20",
    }
    values.update(overrides)
    return ReceiveBatchRequest.model_validate(values)


class TestMedicines:
    async def test_create_normalises_the_sku_and_audits(self) -> None:
        service, _, session, audit = _catalog()

        created = await service.create_medicine(HOSPITAL_ID, _new_medicine(), actor_id=ACTOR_ID)

        assert created.sku == "PARA-500"
        assert created.unit_price == Decimal("2.50")
        assert created.requires_prescription is True
        assert session.commits == 1
        assert audit.last().action == "pharmacy.medicine.created"
        assert audit.last().changes["unit_price"] == {"before": None, "after": "2.50"}

    async def test_a_duplicate_sku_is_a_409_from_the_check_or_the_database(self) -> None:
        service, medicines, _, _ = _catalog()
        medicines.add("PARA-500")

        with pytest.raises(DuplicateMedicineSkuError):
            await service.create_medicine(HOSPITAL_ID, _new_medicine())

        medicines.medicines.clear()
        medicines.raise_on_create = IntegrityError(
            "INSERT", {}, Exception('violates "uq_medicines_hospital_sku"')
        )
        with pytest.raises(DuplicateMedicineSkuError):
            await service.create_medicine(HOSPITAL_ID, _new_medicine())

        medicines.raise_on_create = IntegrityError("INSERT", {}, Exception("something else"))
        with pytest.raises(IntegrityError):
            await service.create_medicine(HOSPITAL_ID, _new_medicine())

    async def test_update_applies_only_what_changed(self) -> None:
        service, medicines, session, audit = _catalog()
        medicine = medicines.add("PARA-500", price="2.50")

        updated = await service.update_medicine(
            HOSPITAL_ID,
            medicine.id,
            UpdateMedicineRequest.model_validate({"unit_price": "3.00", "is_active": True}),
            actor_id=ACTOR_ID,
        )
        await service.update_medicine(
            HOSPITAL_ID, medicine.id, UpdateMedicineRequest.model_validate({"is_active": True})
        )

        assert updated.unit_price == Decimal("3.00")
        assert audit.last().changes == {"unit_price": {"before": "2.50", "after": "3.00"}}
        assert session.commits == 1  # the no-op wrote nothing
        assert audit.actions() == ["pharmacy.medicine.updated"]

    async def test_get_list_and_404(self) -> None:
        service, medicines, _, _ = _catalog()
        medicine = medicines.add("PARA-500")
        medicines.add("AMOX-500")

        assert (await service.get_medicine(HOSPITAL_ID, medicine.id)).sku == "PARA-500"
        page = await service.list_medicines(
            HOSPITAL_ID, pagination=PaginationParams(page=1, page_size=1)
        )
        assert (len(page.items), page.total_records) == (1, 2)
        with pytest.raises(MedicineNotFoundError):
            await service.get_medicine(uuid.uuid4(), medicine.id)
        with pytest.raises(MedicineNotFoundError):
            await service.update_medicine(
                HOSPITAL_ID, uuid.uuid4(), UpdateMedicineRequest.model_validate({"name": "X"})
            )


class TestBatchesAndStock:
    async def test_receiving_creates_a_batch_through_a_movement(self) -> None:
        service, medicines, _, audit = _catalog()
        medicine = medicines.add("PARA-500")

        batch = await service.receive_batch(HOSPITAL_ID, medicine.id, _receipt(), actor_id=ACTOR_ID)

        assert batch.batch_number == "B1"
        assert (batch.initial_quantity, batch.quantity_on_hand) == (50, 50)
        assert batch.cost_per_unit == Decimal("1.20")
        assert (batch.is_dispensable, batch.expires_soon, batch.is_expired) == (True, False, False)
        [movement] = medicines.movements
        assert (movement.quantity_change, movement.reason) == (50, StockMovementReason.RECEIVED)
        assert audit.last().action == "pharmacy.batch.received"
        assert audit.last().context["quantity_on_hand"] == 50

    async def test_receiving_the_same_batch_again_tops_it_up(self) -> None:
        service, medicines, _, _ = _catalog()
        medicine = medicines.add("PARA-500")
        await service.receive_batch(HOSPITAL_ID, medicine.id, _receipt())

        batch = await service.receive_batch(HOSPITAL_ID, medicine.id, _receipt(quantity=25))

        assert (batch.initial_quantity, batch.quantity_on_hand) == (75, 75)
        assert len(medicines.batches) == 1

    async def test_one_batch_has_one_expiry(self) -> None:
        service, medicines, _, _ = _catalog()
        medicine = medicines.add("PARA-500")
        await service.receive_batch(HOSPITAL_ID, medicine.id, _receipt())

        with pytest.raises(ValidationError, match="already recorded as expiring"):
            await service.receive_batch(
                HOSPITAL_ID,
                medicine.id,
                _receipt(expiry_date=(TODAY + timedelta(days=10)).isoformat()),
            )

    async def test_an_expired_batch_cannot_be_received_but_one_expiring_today_can(self) -> None:
        # §11: "batch expiry ≥ today".
        service, medicines, _, _ = _catalog()
        medicine = medicines.add("PARA-500")

        with pytest.raises(ValidationError) as excinfo:
            await service.receive_batch(
                HOSPITAL_ID,
                medicine.id,
                _receipt(expiry_date=(TODAY - timedelta(days=1)).isoformat()),
            )
        today = await service.receive_batch(
            HOSPITAL_ID, medicine.id, _receipt(batch_number="B2", expiry_date=TODAY.isoformat())
        )

        assert excinfo.value.detail["errors"][0]["field"] == "expiry_date"
        assert (today.days_to_expiry, today.expires_soon, today.is_dispensable) == (0, True, True)

    async def test_stock_cannot_be_received_for_an_inactive_or_unknown_medicine(self) -> None:
        service, medicines, _, _ = _catalog()
        retired = medicines.add("OLD", is_active=False)

        with pytest.raises(BusinessRuleError, match="inactive"):
            await service.receive_batch(HOSPITAL_ID, retired.id, _receipt())
        with pytest.raises(MedicineNotFoundError):
            await service.receive_batch(HOSPITAL_ID, uuid.uuid4(), _receipt())

    async def test_recalling_and_releasing_a_batch(self) -> None:
        service, medicines, session, audit = _catalog()
        medicine = medicines.add("PARA-500")
        batch = medicines.stock(medicine, "B1", 40)

        recalled = await service.update_batch(
            HOSPITAL_ID,
            medicine.id,
            batch.id,
            UpdateBatchRequest(is_recalled=True),
            actor_id=ACTOR_ID,
        )
        again = await service.update_batch(
            HOSPITAL_ID, medicine.id, batch.id, UpdateBatchRequest(is_recalled=True)
        )
        released = await service.update_batch(
            HOSPITAL_ID, medicine.id, batch.id, UpdateBatchRequest(is_recalled=False)
        )

        assert (recalled.is_recalled, recalled.is_dispensable) == (True, False)
        assert again.is_recalled is True
        assert released.is_dispensable is True
        assert audit.actions() == ["pharmacy.batch.recalled", "pharmacy.batch.released"]
        assert session.commits == 2

    async def test_a_batch_is_only_reachable_through_its_own_medicine(self) -> None:
        service, medicines, _, _ = _catalog()
        para = medicines.add("PARA-500")
        amox = medicines.add("AMOX-500")
        batch = medicines.stock(para, "B1", 40)

        with pytest.raises(BatchNotFoundError):
            await service.update_batch(
                HOSPITAL_ID, amox.id, batch.id, UpdateBatchRequest(is_recalled=True)
            )
        with pytest.raises(BatchNotFoundError):
            await service.adjust_stock(
                uuid.uuid4(), para.id, batch.id, AdjustStockRequest(quantity_change=-1, note="x")
            )

    async def test_an_adjustment_is_a_movement_with_a_reason(self) -> None:
        service, medicines, _, audit = _catalog()
        medicine = medicines.add("PARA-500")
        batch = medicines.stock(medicine, "B1", 40)

        adjusted = await service.adjust_stock(
            HOSPITAL_ID,
            medicine.id,
            batch.id,
            AdjustStockRequest(quantity_change=-3, reason="expired", note="Damaged strip"),
            actor_id=ACTOR_ID,
        )

        assert adjusted.quantity_on_hand == 37
        assert adjusted.initial_quantity == 40
        [movement] = medicines.movements
        assert (movement.quantity_change, movement.reason, movement.note) == (
            -3,
            StockMovementReason.EXPIRED,
            "Damaged strip",
        )
        assert audit.last().changes == {"quantity_on_hand": {"before": 40, "after": 37}}

    async def test_an_adjustment_cannot_take_a_batch_below_zero(self) -> None:
        service, medicines, session, _ = _catalog()
        medicine = medicines.add("PARA-500")
        batch = medicines.stock(medicine, "B1", 2)

        with pytest.raises(BusinessRuleError, match="holds 2 units"):
            await service.adjust_stock(
                HOSPITAL_ID, medicine.id, batch.id, AdjustStockRequest(quantity_change=-3, note="x")
            )

        assert batch.quantity_on_hand == 2
        assert session.commits == 0

    async def test_the_stock_position_separates_what_can_be_dispensed(self) -> None:
        service, medicines, _, _ = _catalog()
        medicine = medicines.add("PARA-500")
        medicines.stock(medicine, "GOOD", 100, days=300)
        medicines.stock(medicine, "SOON", 20, days=10)
        medicines.stock(medicine, "OLD", 7, days=-5)
        medicines.stock(medicine, "BAD", 30, days=200, recalled=True)
        medicines.stock(medicine, "EMPTY", 0, days=100)

        stock = await service.get_stock(HOSPITAL_ID, medicine.id)

        assert stock.quantity_on_hand == 157
        assert stock.dispensable_quantity == 120
        assert stock.expiring_soon_quantity == 20
        assert stock.expired_quantity == 7
        assert stock.recalled_quantity == 30
        assert [b.batch_number for b in stock.batches] == ["OLD", "SOON", "EMPTY", "BAD", "GOOD"]
        in_stock = await service.list_batches(HOSPITAL_ID, medicine.id, in_stock_only=True)
        assert "EMPTY" not in [b.batch_number for b in in_stock]

    async def test_stock_and_batches_of_an_unknown_medicine_are_404s(self) -> None:
        service, _, _, _ = _catalog()

        with pytest.raises(MedicineNotFoundError):
            await service.get_stock(HOSPITAL_ID, uuid.uuid4())
        with pytest.raises(MedicineNotFoundError):
            await service.list_batches(HOSPITAL_ID, uuid.uuid4())


# ── Vendors and purchase orders ─────────────────────────────────────────────


class FakeProcurement:
    """In-memory stand-in for ``ProcurementRepository``."""

    def __init__(self, medicines: FakeMedicines) -> None:
        self._medicines = medicines
        self.vendors: dict[uuid.UUID, Vendor] = {}
        self.orders: dict[uuid.UUID, PurchaseOrder] = {}
        self.raise_on_create: Exception | None = None

    async def create_vendor(self, **fields: Any) -> Vendor:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        fields.pop("created_by", None)
        vendor = Vendor(id=uuid.uuid4(), is_active=True, created_at=NOW, **fields)
        self.vendors[vendor.id] = vendor
        return vendor

    async def update_vendor(self, vendor: Vendor, *, updated_by: Any = None, **f: Any) -> Vendor:
        for name, value in f.items():
            setattr(vendor, name, value)
        return vendor

    async def get_vendor_by_id(self, hospital_id: uuid.UUID, vendor_id: uuid.UUID) -> Any:
        vendor = self.vendors.get(vendor_id)
        return vendor if vendor is not None and vendor.hospital_id == hospital_id else None

    async def get_vendor_by_name(self, hospital_id: uuid.UUID, name: str) -> Any:
        return next((v for v in self.vendors.values() if v.name == name), None)

    async def list_vendors(self, hospital_id: uuid.UUID, *, skip: int, limit: int, **_: Any) -> Any:
        return list(self.vendors.values())[skip : skip + limit]

    async def count_vendors(self, hospital_id: uuid.UUID, **_: Any) -> int:
        return len(self.vendors)

    async def create_purchase_order(self, *, items: list[dict[str, Any]], **fields: Any) -> Any:
        fields.pop("created_by", None)
        order = PurchaseOrder(
            id=uuid.uuid4(),
            status=PurchaseOrderStatus.DRAFT,
            ordered_at=None,
            received_at=None,
            created_at=NOW,
            items=[],
            **fields,
        )
        for position, values in enumerate(items):
            line = PurchaseOrderItem(
                id=uuid.uuid4(), hospital_id=fields["hospital_id"], position=position, **values
            )
            line.__dict__["medicine"] = self._medicines.medicines[values["medicine_id"]]
            order.items.append(line)
        order.__dict__["vendor"] = self.vendors[fields["vendor_id"]]
        self.orders[order.id] = order
        return order

    async def update_purchase_order(self, order: Any, *, updated_by: Any = None, **f: Any) -> Any:
        for name, value in f.items():
            setattr(order, name, value)
        return order

    async def get_purchase_order_by_id(
        self, hospital_id: uuid.UUID, oid: uuid.UUID, **_: Any
    ) -> Any:
        order = self.orders.get(oid)
        return order if order is not None and order.hospital_id == hospital_id else None

    async def list_purchase_orders(
        self, hospital_id: uuid.UUID, *, skip: int, limit: int, **f: Any
    ) -> Any:
        rows = [o for o in self.orders.values() if f.get("status") in (None, o.status)]
        return rows[skip : skip + limit]

    async def count_purchase_orders(self, hospital_id: uuid.UUID, **f: Any) -> int:
        return len([o for o in self.orders.values() if f.get("status") in (None, o.status)])


class _Buying:
    """A procurement service over fakes, with a vendor and two medicines."""

    def __init__(self) -> None:
        self.medicines = FakeMedicines()
        self.procurement = FakeProcurement(self.medicines)
        self.session = FakeSession()
        self.audit = RecordingAuditSink()
        self.service = ProcurementService(
            self.procurement,  # type: ignore[arg-type]
            self.medicines,  # type: ignore[arg-type]
            AsyncMock(),
            self.session,  # type: ignore[arg-type]
            self.audit,
        )
        self.para = self.medicines.add("PARA", name="Paracetamol")
        self.amox = self.medicines.add("AMOX", name="Amoxicillin")

    async def vendor(self, name: str = "Acme Pharma") -> Any:
        return await self.service.create_vendor(
            HOSPITAL_ID, CreateVendorRequest(name=name, tax_id="GST1"), actor_id=ACTOR_ID
        )

    async def order(self, vendor: Any, **overrides: Any) -> Any:
        body: dict[str, Any] = {
            "vendor_id": str(vendor.id),
            "items": [
                {"medicine_id": str(self.para.id), "quantity": 500, "unit_price": "1.20"},
                {"medicine_id": str(self.amox.id), "quantity": 200, "unit_price": "4.00"},
            ],
        }
        body.update(overrides)
        return await self.service.create_purchase_order(
            HOSPITAL_ID, CreatePurchaseOrderRequest.model_validate(body), actor_id=ACTOR_ID
        )

    async def sent(self) -> Any:
        order = await self.order(await self.vendor())
        return await self.service.send_purchase_order(HOSPITAL_ID, order.id, actor_id=ACTOR_ID)

    def receipt(self, order: Any, **overrides: Any) -> ReceivePurchaseOrderRequest:
        line: dict[str, Any] = {
            "po_item_id": str(order.items[0].id),
            "batch_number": "pa-1",
            "expiry_date": (TODAY + timedelta(days=300)).isoformat(),
            "quantity": 500,
        }
        line.update(overrides)
        return ReceivePurchaseOrderRequest.model_validate({"items": [line]})


class TestVendors:
    async def test_create_update_get_and_list(self) -> None:
        buying = _Buying()

        vendor = await buying.vendor()
        updated = await buying.service.update_vendor(
            HOSPITAL_ID,
            vendor.id,
            UpdateVendorRequest.model_validate(
                {"contact": "orders@acme.example", "is_active": True}
            ),
            actor_id=ACTOR_ID,
        )

        assert vendor.is_active is True
        assert updated.contact == "orders@acme.example"
        assert buying.audit.actions() == ["pharmacy.vendor.created", "pharmacy.vendor.updated"]
        assert buying.audit.last().changes == {
            "contact": {"before": None, "after": "orders@acme.example"}
        }
        assert (await buying.service.get_vendor(HOSPITAL_ID, vendor.id)).name == "Acme Pharma"
        assert (await buying.service.list_vendors(HOSPITAL_ID)).total_records == 1

    async def test_a_no_op_update_writes_nothing(self) -> None:
        buying = _Buying()
        vendor = await buying.vendor()
        commits = buying.session.commits

        await buying.service.update_vendor(
            HOSPITAL_ID, vendor.id, UpdateVendorRequest.model_validate({"is_active": True})
        )

        assert buying.session.commits == commits

    async def test_names_are_unique_on_create_and_on_rename(self) -> None:
        buying = _Buying()
        await buying.vendor("Acme Pharma")
        other = await buying.vendor("Other Pharma")

        with pytest.raises(DuplicateVendorNameError):
            await buying.vendor("Acme Pharma")
        with pytest.raises(DuplicateVendorNameError):
            await buying.service.update_vendor(
                HOSPITAL_ID, other.id, UpdateVendorRequest.model_validate({"name": "Acme Pharma"})
            )

    async def test_a_duplicate_caught_only_by_the_database_is_still_a_409(self) -> None:
        buying = _Buying()
        buying.procurement.raise_on_create = IntegrityError(
            "INSERT", {}, Exception('violates "uq_vendors_hospital_name"')
        )
        with pytest.raises(DuplicateVendorNameError):
            await buying.vendor()

        buying.procurement.raise_on_create = IntegrityError("INSERT", {}, Exception("other"))
        with pytest.raises(IntegrityError):
            await buying.vendor()

    async def test_another_hospitals_vendor_is_a_404(self) -> None:
        buying = _Buying()
        vendor = await buying.vendor()

        with pytest.raises(VendorNotFoundError):
            await buying.service.get_vendor(uuid.uuid4(), vendor.id)
        with pytest.raises(VendorNotFoundError):
            await buying.service.update_vendor(
                uuid.uuid4(), vendor.id, UpdateVendorRequest.model_validate({"name": "X"})
            )


class TestPurchaseOrders:
    async def test_an_order_is_drafted_with_computed_totals(self) -> None:
        buying = _Buying()

        order = await buying.order(await buying.vendor(), notes="Deliver before noon.")

        assert order.status is PurchaseOrderStatus.DRAFT
        assert order.po_number.startswith("PO-")
        assert order.vendor_name == "Acme Pharma"
        assert [(i.medicine_sku, i.quantity, i.total) for i in order.items] == [
            ("PARA", 500, Decimal("600.00")),
            ("AMOX", 200, Decimal("800.00")),
        ]
        assert order.total_amount == Decimal("1400.00")
        assert buying.audit.last().action == "pharmacy.po.created"

    async def test_an_order_needs_an_active_known_vendor_and_medicines(self) -> None:
        buying = _Buying()
        vendor = await buying.vendor()
        retired = buying.medicines.add("OLD", is_active=False)

        async def field_of(**overrides: Any) -> str:
            with pytest.raises(ValidationError) as excinfo:
                await buying.order(vendor, **overrides)
            return str(excinfo.value.detail["errors"][0]["field"])

        line = {"quantity": 1, "unit_price": "1.00"}
        assert await field_of(vendor_id=str(uuid.uuid4())) == "vendor_id"
        assert (
            await field_of(items=[{"medicine_id": str(uuid.uuid4()), **line}])
            == "items.0.medicine_id"
        )
        assert (
            await field_of(items=[{"medicine_id": str(retired.id), **line}])
            == "items.0.medicine_id"
        )
        buying.procurement.vendors[vendor.id].is_active = False
        assert await field_of() == "vendor_id"

    async def test_send_then_receive_puts_stock_on_the_shelf(self) -> None:
        buying = _Buying()
        order = await buying.sent()
        assert order.status is PurchaseOrderStatus.SENT
        assert order.ordered_at is not None

        received = await buying.service.receive_purchase_order(
            HOSPITAL_ID, order.id, buying.receipt(order), actor_id=ACTOR_ID
        )

        assert received.status is PurchaseOrderStatus.RECEIVED
        assert received.received_at is not None
        [batch] = buying.medicines.batches.values()
        assert (batch.batch_number, batch.quantity_on_hand) == ("PA-1", 500)
        # Cost defaults to the price agreed on the order line.
        assert batch.cost_per_unit == Decimal("1.20")
        [movement] = buying.medicines.movements
        assert (movement.reference_type, movement.reference_id) == ("purchase_order", order.id)
        event = buying.audit.last()
        assert event.action == "pharmacy.po.received"
        assert event.changes == {"status": {"before": "sent", "after": "received"}}
        assert event.context["received"] == [{"medicine": "PARA", "batch": "PA-1", "quantity": 500}]

    async def test_a_delivery_may_be_split_across_batches_and_top_up_an_existing_one(self) -> None:
        buying = _Buying()
        order = await buying.sent()
        existing = buying.medicines.stock(buying.para, "PA-1", 10, days=300)
        later = (TODAY + timedelta(days=400)).isoformat()

        await buying.service.receive_purchase_order(
            HOSPITAL_ID,
            order.id,
            ReceivePurchaseOrderRequest.model_validate(
                {
                    "items": [
                        {
                            "po_item_id": str(order.items[0].id),
                            "batch_number": "PA-1",
                            "expiry_date": existing.expiry_date.isoformat(),
                            "quantity": 300,
                        },
                        {
                            "po_item_id": str(order.items[0].id),
                            "batch_number": "PA-2",
                            "expiry_date": later,
                            "quantity": 150,
                            "cost_per_unit": "1.25",
                        },
                    ]
                }
            ),
        )

        stock = {b.batch_number: b for b in buying.medicines.batches.values()}
        assert stock["PA-1"].quantity_on_hand == 310
        assert (stock["PA-2"].quantity_on_hand, stock["PA-2"].cost_per_unit) == (
            150,
            Decimal("1.25"),
        )

    async def test_two_lines_naming_one_new_batch_make_one_batch(self) -> None:
        buying = _Buying()
        order = await buying.sent()
        expiry = (TODAY + timedelta(days=300)).isoformat()

        await buying.service.receive_purchase_order(
            HOSPITAL_ID,
            order.id,
            ReceivePurchaseOrderRequest.model_validate(
                {
                    "items": [
                        {
                            "po_item_id": str(order.items[0].id),
                            "batch_number": "X1",
                            "expiry_date": expiry,
                            "quantity": 5,
                        },
                        {
                            "po_item_id": str(order.items[1].id),
                            "batch_number": "X1",
                            "expiry_date": expiry,
                            "quantity": 7,
                        },
                    ]
                }
            ),
        )

        # Same number, different medicines: two batches, one each.
        assert sorted(b.quantity_on_hand for b in buying.medicines.batches.values()) == [5, 7]

    async def test_a_bad_receipt_is_a_422_and_puts_nothing_on_the_shelf(self) -> None:
        buying = _Buying()
        order = await buying.sent()
        # Already on the shelf with a different expiry from the one on the receipt.
        buying.medicines.stock(buying.para, "PA-9", 10, days=100)
        before = len(buying.medicines.movements)

        async def field_of(**overrides: Any) -> str:
            with pytest.raises(ValidationError) as excinfo:
                await buying.service.receive_purchase_order(
                    HOSPITAL_ID, order.id, buying.receipt(order, **overrides)
                )
            return str(excinfo.value.detail["errors"][0]["field"])

        assert await field_of(po_item_id=str(uuid.uuid4())) == "items.0.po_item_id"
        yesterday = (TODAY - timedelta(days=1)).isoformat()
        assert await field_of(expiry_date=yesterday) == "items.0.expiry_date"
        assert await field_of(batch_number="PA-9") == "items.0.expiry_date"
        assert len(buying.medicines.movements) == before

    async def test_only_a_sent_order_can_be_received_and_only_once(self) -> None:
        buying = _Buying()
        draft = await buying.order(await buying.vendor())

        with pytest.raises(PurchaseOrderStateError, match="draft"):
            await buying.service.receive_purchase_order(
                HOSPITAL_ID, draft.id, buying.receipt(draft)
            )

        await buying.service.send_purchase_order(HOSPITAL_ID, draft.id)
        await buying.service.receive_purchase_order(HOSPITAL_ID, draft.id, buying.receipt(draft))
        with pytest.raises(PurchaseOrderStateError, match="received"):
            await buying.service.receive_purchase_order(
                HOSPITAL_ID, draft.id, buying.receipt(draft)
            )
        with pytest.raises(PurchaseOrderStateError):
            await buying.service.send_purchase_order(HOSPITAL_ID, draft.id)
        with pytest.raises(PurchaseOrderStateError):
            await buying.service.cancel_purchase_order(HOSPITAL_ID, draft.id)

    async def test_a_draft_or_sent_order_can_be_cancelled(self) -> None:
        buying = _Buying()
        order = await buying.sent()

        cancelled = await buying.service.cancel_purchase_order(
            HOSPITAL_ID, order.id, actor_id=ACTOR_ID
        )

        assert cancelled.status is PurchaseOrderStatus.CANCELLED
        assert buying.audit.last().changes == {"status": {"before": "sent", "after": "cancelled"}}

    async def test_get_list_and_404(self) -> None:
        buying = _Buying()
        order = await buying.sent()

        assert (await buying.service.get_purchase_order(HOSPITAL_ID, order.id)).id == order.id
        sent = await buying.service.list_purchase_orders(
            HOSPITAL_ID, status=PurchaseOrderStatus.SENT
        )
        drafts = await buying.service.list_purchase_orders(
            HOSPITAL_ID, status=PurchaseOrderStatus.DRAFT
        )
        assert (sent.total_records, drafts.total_records) == (1, 0)
        with pytest.raises(PurchaseOrderNotFoundError):
            await buying.service.get_purchase_order(uuid.uuid4(), order.id)
        with pytest.raises(PurchaseOrderNotFoundError):
            await buying.service.send_purchase_order(uuid.uuid4(), order.id)


class TestHospitalToday:
    """Expiry is judged by the date on the pharmacy's wall, not by UTC."""

    @pytest.fixture(autouse=True)
    def _real_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.undo()

    async def _today(self, timezone: str | None) -> date:
        hospitals = AsyncMock()
        hospital = MagicMock()
        hospital.timezone = timezone
        hospitals.get_by_id.return_value = hospital if timezone is not None else None
        return await hospital_today(hospitals, HOSPITAL_ID)

    @staticmethod
    def _freeze(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
        """Pin the clock ``hospital_today`` reads to one instant.

        Without this the test depends on the hour it runs at: two zones that
        are 25 hours apart are one calendar day apart for most of the day and
        two days apart for the rest.
        """

        class FrozenClock:
            """Stands in for ``datetime`` in the module: only ``now`` is read there."""

            @staticmethod
            def now(tz: Any = None) -> datetime:
                return instant.astimezone(tz)

        monkeypatch.setattr(pharmacy_common, "datetime", FrozenClock)

    @pytest.mark.parametrize(
        ("instant", "timezone", "expected"),
        [
            # 10:30 UTC: already tomorrow at UTC+14, still yesterday at UTC-11.
            (datetime(2026, 3, 10, 10, 30, tzinfo=UTC), "Pacific/Kiritimati", date(2026, 3, 11)),
            (datetime(2026, 3, 10, 10, 30, tzinfo=UTC), "Pacific/Pago_Pago", date(2026, 3, 9)),
            (datetime(2026, 3, 10, 10, 30, tzinfo=UTC), "UTC", date(2026, 3, 10)),
            # 12:00 UTC: the same two zones are now one day apart, not two.
            (datetime(2026, 3, 10, 12, 0, tzinfo=UTC), "Pacific/Kiritimati", date(2026, 3, 11)),
            (datetime(2026, 3, 10, 12, 0, tzinfo=UTC), "Pacific/Pago_Pago", date(2026, 3, 10)),
            # 20:00 UTC is 01:30 the next morning in India: a day ahead of UTC.
            (datetime(2026, 3, 10, 20, 0, tzinfo=UTC), "Asia/Kolkata", date(2026, 3, 11)),
            (datetime(2026, 3, 10, 18, 29, tzinfo=UTC), "Asia/Kolkata", date(2026, 3, 10)),
            (datetime(2026, 3, 10, 18, 30, tzinfo=UTC), "Asia/Kolkata", date(2026, 3, 11)),
        ],
    )
    async def test_uses_the_hospitals_timezone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        instant: datetime,
        timezone: str,
        expected: date,
    ) -> None:
        self._freeze(monkeypatch, instant)

        assert await self._today(timezone) == expected

    async def test_falls_back_to_utc(self) -> None:
        utc = datetime.now(UTC).date()

        assert await self._today(None) in (utc, utc + timedelta(days=1))
        assert await self._today("Not/AZone") in (utc, utc + timedelta(days=1))
        assert pharmacy_common.audit_value(Decimal("1.50")) == "1.50"
