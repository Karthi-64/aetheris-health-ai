"""Repository tests for medicines and stock, prescriptions, and procurement.

Real Postgres, rolled back per test. Every read method is checked for tenant
isolation (``backend/CLAUDE.md``: "every repository method has at least one
test that verifies ``hospital_id`` filtering"), and the stock quantity is
checked against its ledger (module spec §16: "stock quantity computation").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.pharmacy import PrescriptionStatus, PurchaseOrderStatus, StockMovementReason
from app.repositories.medicine_repository import MedicineRepository
from app.repositories.prescription_repository import PrescriptionRepository
from app.repositories.procurement_repository import ProcurementRepository
from app.tests.billing_helpers import insert_appointment, insert_doctor, insert_patient, insert_user
from app.tests.pharmacy_helpers import insert_batch, insert_medicine

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.pharmacy import Prescription

pytestmark = pytest.mark.database

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
TODAY = datetime.now(UTC).date()


@pytest.fixture
def medicines(db_session: AsyncSession) -> MedicineRepository:
    """A medicine repository bound to the rolled-back test session."""
    return MedicineRepository(db_session)


@pytest.fixture
def prescriptions(db_session: AsyncSession) -> PrescriptionRepository:
    """A prescription repository bound to the rolled-back test session."""
    return PrescriptionRepository(db_session)


@pytest.fixture
def procurement(db_session: AsyncSession) -> ProcurementRepository:
    """A procurement repository bound to the rolled-back test session."""
    return ProcurementRepository(db_session)


class TestMedicines:
    async def test_sku_is_unique_per_hospital_only(
        self, db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
    ) -> None:
        await insert_medicine(db_session, hospital_id, "PARA")
        await insert_medicine(db_session, other_hospital_id, "PARA")

        with pytest.raises(IntegrityError, match="uq_medicines_hospital_sku"):
            async with db_session.begin_nested():
                await insert_medicine(db_session, hospital_id, "PARA")

    async def test_a_negative_price_is_refused_by_the_database(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        with pytest.raises(IntegrityError, match="unit_price_non_negative"):
            async with db_session.begin_nested():
                await insert_medicine(db_session, hospital_id, "PARA", unit_price="-1.00")

    async def test_every_lookup_is_tenant_scoped(
        self,
        medicines: MedicineRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        mine = await insert_medicine(db_session, hospital_id, "PARA")
        theirs = await insert_medicine(db_session, other_hospital_id, "AMOX")

        assert await medicines.get_medicine_by_id(hospital_id, theirs.id) is None
        assert await medicines.get_medicine_by_sku(hospital_id, "AMOX") is None
        assert await medicines.get_medicines_by_ids(hospital_id, [mine.id, theirs.id]) == [mine]
        assert await medicines.get_medicines_by_ids(hospital_id, []) == []
        assert [m.id for m in await medicines.list_medicines(hospital_id)] == [mine.id]
        assert await medicines.count_medicines(hospital_id) == 1
        assert (await medicines.get_medicine_by_id(hospital_id, mine.id)) is mine
        assert (await medicines.get_medicine_by_sku(hospital_id, "PARA")) is mine

    async def test_list_searches_name_generic_and_sku(
        self, medicines: MedicineRepository, hospital_id: uuid.UUID
    ) -> None:
        for sku, name, generic, active in (
            ("CROC-650", "Crocin", "Paracetamol", True),
            ("AMOX-500", "Mox", "Amoxicillin", True),
            ("OLD-1", "Retired 100%_", None, False),
        ):
            await medicines.create_medicine(
                hospital_id=hospital_id,
                sku=sku,
                name=name,
                generic_name=generic,
                unit_price=Decimal("1.00"),
                is_active=active,
            )

        async def names(**filters: Any) -> list[str]:
            return [m.name for m in await medicines.list_medicines(hospital_id, **filters)]

        assert await names(is_active=True) == ["Crocin", "Mox"]
        assert await names(term="para") == ["Crocin"]  # by generic name
        assert await names(term="cro") == ["Crocin"]  # by brand prefix
        assert await names(term="amox-500") == ["Mox"]  # by exact SKU, any case
        assert await names(term="%") == []  # literal, not a wildcard
        assert await names(is_active=True, skip=1, limit=1) == ["Mox"]
        assert await medicines.count_medicines(hospital_id, is_active=False) == 1

    async def test_update(
        self, medicines: MedicineRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        medicine = await insert_medicine(db_session, hospital_id, "PARA")

        updated = await medicines.update_medicine(medicine, unit_price=Decimal("3.00"))

        assert updated.unit_price == Decimal("3.00")


class TestStock:
    async def test_on_hand_is_always_the_sum_of_the_ledger(
        self, medicines: MedicineRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        user = await insert_user(db_session, hospital_id)
        medicine = await insert_medicine(db_session, hospital_id, "PARA")
        batch = await medicines.create_batch(
            medicine=medicine, batch_number="B1", expiry_date=TODAY, cost_per_unit=Decimal("1.20")
        )
        assert (batch.initial_quantity, batch.quantity_on_hand) == (0, 0)

        for change, reason in (
            (100, StockMovementReason.RECEIVED),
            (-30, StockMovementReason.DISPENSED),
            (50, StockMovementReason.RECEIVED),
            (-5, StockMovementReason.EXPIRED),
            (2, StockMovementReason.ADJUSTED),
        ):
            await medicines.apply_movement(
                batch, quantity_change=change, reason=reason, moved_at=NOW, moved_by=user.id
            )

        ledger = await medicines.list_movements(hospital_id, batch.id)
        assert batch.quantity_on_hand == sum(m.quantity_change for m in ledger) == 117
        # Only receipts count towards what was ever received.
        assert batch.initial_quantity == 150
        assert {m.hospital_id for m in ledger} == {hospital_id}
        assert {m.moved_by for m in ledger} == {user.id}

    async def test_the_database_refuses_to_overdraw_a_batch(
        self, medicines: MedicineRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        # §14: the constraint that decides who gets the last unit.
        batch = await insert_batch(
            db_session, await insert_medicine(db_session, hospital_id, "PARA"), "B1", 5
        )

        with pytest.raises(IntegrityError, match="quantity_on_hand_non_negative"):
            async with db_session.begin_nested():
                await medicines.apply_movement(
                    batch,
                    quantity_change=-6,
                    reason=StockMovementReason.DISPENSED,
                    moved_at=NOW,
                )

    async def test_a_zero_movement_is_refused(
        self, medicines: MedicineRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        batch = await insert_batch(
            db_session, await insert_medicine(db_session, hospital_id, "PARA"), "B1", 5
        )

        with pytest.raises(IntegrityError, match="quantity_change_non_zero"):
            async with db_session.begin_nested():
                await medicines.apply_movement(
                    batch, quantity_change=0, reason=StockMovementReason.ADJUSTED, moved_at=NOW
                )

    async def test_a_batch_number_is_unique_per_medicine(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        para = await insert_medicine(db_session, hospital_id, "PARA")
        amox = await insert_medicine(db_session, hospital_id, "AMOX")
        await insert_batch(db_session, para, "B1", 5)
        await insert_batch(db_session, amox, "B1", 5)  # another medicine may reuse it

        with pytest.raises(IntegrityError, match="uq_medicine_batches_medicine_batch"):
            async with db_session.begin_nested():
                await insert_batch(db_session, para, "B1", 5)

    async def test_ac2_dispensable_batches_come_first_expiry_first(
        self, medicines: MedicineRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        medicine = await insert_medicine(db_session, hospital_id, "PARA")
        await insert_batch(db_session, medicine, "LATE", 10, days=300)
        await insert_batch(db_session, medicine, "SOON", 10, days=20)
        await insert_batch(db_session, medicine, "TODAY", 10, days=0)
        await insert_batch(db_session, medicine, "EXPIRED", 10, days=-1)
        await insert_batch(db_session, medicine, "RECALLED", 10, days=10, recalled=True)
        await insert_batch(db_session, medicine, "EMPTY", 0, days=5)

        locked = await medicines.lock_dispensable_batches(hospital_id, medicine.id, on=TODAY)
        everything = await medicines.list_batches(hospital_id, medicine.id)
        held = await medicines.list_batches(hospital_id, medicine.id, in_stock_only=True)

        # Expiring today still counts (§14); expired, recalled and empty do not.
        assert [b.batch_number for b in locked] == ["TODAY", "SOON", "LATE"]
        assert [b.batch_number for b in everything] == [
            "EXPIRED",
            "TODAY",
            "EMPTY",
            "RECALLED",
            "SOON",
            "LATE",
        ]
        assert "EMPTY" not in [b.batch_number for b in held]
        assert await medicines.dispensable_totals(hospital_id, [medicine.id], on=TODAY) == {
            medicine.id: 30
        }

    async def test_batch_lookups_are_tenant_scoped(
        self,
        medicines: MedicineRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        medicine = await insert_medicine(db_session, hospital_id, "PARA")
        batch = await insert_batch(db_session, medicine, "B1", 5)

        assert await medicines.get_batch_by_id(other_hospital_id, batch.id) is None
        assert await medicines.get_batch_by_id(other_hospital_id, batch.id, for_update=True) is None
        assert await medicines.get_batch_by_number(other_hospital_id, medicine.id, "B1") is None
        assert await medicines.list_batches(other_hospital_id, medicine.id) == []
        assert (
            await medicines.lock_dispensable_batches(other_hospital_id, medicine.id, on=TODAY) == []
        )
        assert await medicines.dispensable_totals(other_hospital_id, [medicine.id], on=TODAY) == {}
        assert await medicines.dispensable_totals(hospital_id, [], on=TODAY) == {}
        assert await medicines.list_movements(other_hospital_id, batch.id) == []
        assert (await medicines.get_batch_by_id(hospital_id, batch.id, for_update=True)) is batch
        assert (await medicines.get_batch_by_number(hospital_id, medicine.id, "B1")) is batch
        assert batch.medicine.sku == "PARA"


async def _prescribe(
    session: AsyncSession,
    prescriptions: PrescriptionRepository,
    hospital_id: uuid.UUID,
    *lines: tuple[Any, int],
    prescribed_at: datetime = NOW,
) -> Prescription:
    """Insert a prescription for a fresh patient, doctor and visit."""
    patient = await insert_patient(session, hospital_id)
    doctor = await insert_doctor(session, hospital_id)
    appointment = await insert_appointment(
        session, hospital_id, patient_id=patient.id, doctor_id=doctor.id
    )
    return await prescriptions.create_prescription(
        hospital_id=hospital_id,
        appointment_id=appointment.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        prescribed_at=prescribed_at,
        items=[
            {
                "medicine_id": medicine.id,
                "medicine_name": medicine.name,
                "dosage": "1 tablet",
                "frequency": "twice daily",
                "quantity": quantity,
            }
            for medicine, quantity in lines
        ],
    )


class TestPrescriptions:
    async def test_create_persists_items_in_order_with_people_loaded(
        self,
        prescriptions: PrescriptionRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        para = await insert_medicine(db_session, hospital_id, "PARA")
        amox = await insert_medicine(db_session, hospital_id, "AMOX")

        rx = await _prescribe(db_session, prescriptions, hospital_id, (amox, 21), (para, 10))

        assert rx.status is PrescriptionStatus.ACTIVE
        assert [(i.position, i.medicine_id, i.quantity_remaining) for i in rx.items] == [
            (0, amox.id, 21),
            (1, para.id, 10),
        ]
        assert {i.hospital_id for i in rx.items} == {hospital_id}
        assert rx.patient.full_name
        assert rx.doctor.user.first_name

    async def test_the_database_refuses_dispensing_more_than_was_prescribed(
        self,
        prescriptions: PrescriptionRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        # §11, as a constraint rather than a hope.
        para = await insert_medicine(db_session, hospital_id, "PARA")
        rx = await _prescribe(db_session, prescriptions, hospital_id, (para, 10))
        await prescriptions.record_dispensed(rx.items[0], 10)

        with pytest.raises(IntegrityError, match="dispensed_within_prescribed"):
            async with db_session.begin_nested():
                await prescriptions.record_dispensed(rx.items[0], 1)

    async def test_gets_and_lists_are_tenant_scoped(
        self,
        prescriptions: PrescriptionRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        para = await insert_medicine(db_session, hospital_id, "PARA")
        rx = await _prescribe(db_session, prescriptions, hospital_id, (para, 10))

        assert await prescriptions.get_prescription_by_id(other_hospital_id, rx.id) is None
        assert (
            await prescriptions.get_prescription_by_id(other_hospital_id, rx.id, for_update=True)
        ) is None
        assert await prescriptions.list_prescriptions(other_hospital_id) == []
        assert await prescriptions.count_prescriptions(other_hospital_id) == 0
        assert await prescriptions.list_dispenses(other_hospital_id, rx.id) == []
        assert (
            await prescriptions.get_prescription_by_id(hospital_id, rx.id, for_update=True)
        ) is rx

    async def test_list_filters_and_orders(
        self,
        prescriptions: PrescriptionRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        para = await insert_medicine(db_session, hospital_id, "PARA")
        older = await _prescribe(
            db_session,
            prescriptions,
            hospital_id,
            (para, 1),
            prescribed_at=NOW - timedelta(hours=3),
        )
        newer = await _prescribe(db_session, prescriptions, hospital_id, (para, 1))
        done = await _prescribe(
            db_session,
            prescriptions,
            hospital_id,
            (para, 1),
            prescribed_at=NOW - timedelta(hours=1),
        )
        await prescriptions.update_prescription(done, status=PrescriptionStatus.DISPENSED)

        async def ids(**options: Any) -> list[Any]:
            return [p.id for p in await prescriptions.list_prescriptions(hospital_id, **options)]

        assert await ids() == [newer.id, done.id, older.id]
        pending = (PrescriptionStatus.ACTIVE, PrescriptionStatus.PARTIALLY_DISPENSED)
        # The queue: longest-waiting first.
        assert await ids(statuses=pending, oldest_first=True) == [older.id, newer.id]
        assert await prescriptions.count_prescriptions(hospital_id, statuses=pending) == 2
        assert await ids(patient_id=older.patient_id) == [older.id]
        assert await ids(doctor_id=newer.doctor_id) == [newer.id]
        assert await ids(appointment_id=done.appointment_id) == [done.id]
        assert await ids(skip=1, limit=1) == [done.id]

    async def test_a_dispense_round_trips_with_its_lines_and_batches(
        self,
        prescriptions: PrescriptionRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        user = await insert_user(db_session, hospital_id)
        para = await insert_medicine(db_session, hospital_id, "PARA")
        batch = await insert_batch(db_session, para, "B1", 50)
        rx = await _prescribe(db_session, prescriptions, hospital_id, (para, 10))

        dispense = await prescriptions.create_dispense(
            prescription=rx,
            dispensed_at=NOW,
            dispensed_by=user.id,
            total_amount=Decimal("25.00"),
            notes=None,
            lines=[
                {
                    "prescription_item_id": rx.items[0].id,
                    "medicine_id": para.id,
                    "batch_id": batch.id,
                    "quantity": 10,
                    "unit_price": Decimal("2.50"),
                    "total": Decimal("25.00"),
                }
            ],
        )

        [listed] = await prescriptions.list_dispenses(hospital_id, rx.id)
        assert listed.id == dispense.id
        assert listed.hospital_id == hospital_id
        [line] = listed.items
        assert (line.hospital_id, line.quantity, line.total) == (hospital_id, 10, Decimal("25.00"))
        # Loaded with the line, so a response needs no further IO.
        assert (line.batch.batch_number, line.batch.medicine.sku) == ("B1", "PARA")


class TestProcurement:
    async def test_vendor_names_are_unique_per_hospital_and_lookups_scoped(
        self,
        procurement: ProcurementRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        mine = await procurement.create_vendor(hospital_id=hospital_id, name="Acme")
        await procurement.create_vendor(
            hospital_id=hospital_id, name="Zed", contact="z@zed.example"
        )
        theirs = await procurement.create_vendor(hospital_id=other_hospital_id, name="Acme")

        with pytest.raises(IntegrityError, match="uq_vendors_hospital_name"):
            async with db_session.begin_nested():
                await procurement.create_vendor(hospital_id=hospital_id, name="Acme")

        assert await procurement.get_vendor_by_id(hospital_id, theirs.id) is None
        assert (await procurement.get_vendor_by_name(hospital_id, "Acme")) is mine
        assert [v.name for v in await procurement.list_vendors(hospital_id)] == ["Acme", "Zed"]
        assert await procurement.count_vendors(hospital_id) == 2
        await procurement.update_vendor(mine, is_active=False)
        assert [v.name for v in await procurement.list_vendors(hospital_id, is_active=True)] == [
            "Zed"
        ]
        assert await procurement.count_vendors(hospital_id, is_active=False) == 1
        assert [v.name for v in await procurement.list_vendors(hospital_id, skip=1, limit=1)] == [
            "Zed"
        ]

    async def test_purchase_orders_round_trip_and_are_tenant_scoped(
        self,
        procurement: ProcurementRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        vendor = await procurement.create_vendor(hospital_id=hospital_id, name="Acme")
        para = await insert_medicine(db_session, hospital_id, "PARA")
        amox = await insert_medicine(db_session, hospital_id, "AMOX")

        def line(medicine: Any, quantity: int) -> dict[str, Any]:
            return {
                "medicine_id": medicine.id,
                "quantity": quantity,
                "unit_price": Decimal("1.20"),
                "total": Decimal("1.20") * quantity,
            }

        order = await procurement.create_purchase_order(
            hospital_id=hospital_id,
            vendor_id=vendor.id,
            po_number="PO-1",
            items=[line(amox, 200), line(para, 500)],
        )
        second = await procurement.create_purchase_order(
            hospital_id=hospital_id, vendor_id=vendor.id, po_number="PO-2", items=[line(para, 1)]
        )

        assert order.status is PurchaseOrderStatus.DRAFT
        assert [(i.position, i.medicine.sku) for i in order.items] == [(0, "AMOX"), (1, "PARA")]
        assert order.vendor.name == "Acme"
        with pytest.raises(IntegrityError, match="uq_purchase_orders_hospital_number"):
            async with db_session.begin_nested():
                await procurement.create_purchase_order(
                    hospital_id=hospital_id, vendor_id=vendor.id, po_number="PO-1", items=[]
                )
        with pytest.raises(IntegrityError, match="uq_po_items_po_medicine"):
            async with db_session.begin_nested():
                await procurement.create_purchase_order(
                    hospital_id=hospital_id,
                    vendor_id=vendor.id,
                    po_number="PO-3",
                    items=[line(para, 1), line(para, 2)],
                )

        sent = await procurement.update_purchase_order(order, status=PurchaseOrderStatus.SENT)
        assert sent.items[0].medicine.sku == "AMOX"  # still loaded after the update
        assert await procurement.get_purchase_order_by_id(other_hospital_id, order.id) is None
        assert (
            await procurement.get_purchase_order_by_id(other_hospital_id, order.id, for_update=True)
        ) is None
        assert (
            await procurement.get_purchase_order_by_id(hospital_id, order.id, for_update=True)
        ) is order
        assert await procurement.list_purchase_orders(other_hospital_id) == []
        assert await procurement.count_purchase_orders(hospital_id) == 2
        listed = await procurement.list_purchase_orders(
            hospital_id, status=PurchaseOrderStatus.DRAFT
        )
        assert [o.id for o in listed] == [second.id]
        assert await procurement.count_purchase_orders(hospital_id, vendor_id=vendor.id) == 2
