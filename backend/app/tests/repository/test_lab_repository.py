"""Repository tests for the lab test catalog and lab orders.

Real Postgres, rolled back per test. Every read method is checked for tenant
isolation (``backend/CLAUDE.md``: "every repository method has at least one
test that verifies ``hospital_id`` filtering").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.lab import LabOrderPriority, LabOrderStatus, LabResultFlag, LabResultType
from app.repositories.lab_order_repository import LabOrderRepository
from app.repositories.lab_test_repository import LabTestRepository
from app.tests.billing_helpers import insert_appointment, insert_doctor, insert_patient, insert_user

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.lab import LabOrder, LabTest

pytestmark = pytest.mark.database

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
RANGES = [{"sex": "any", "low": "3.5", "high": "5.1"}]


@pytest.fixture
def tests(db_session: AsyncSession) -> LabTestRepository:
    """A catalog repository bound to the rolled-back test session."""
    return LabTestRepository(db_session)


@pytest.fixture
def orders(db_session: AsyncSession) -> LabOrderRepository:
    """An order repository bound to the rolled-back test session."""
    return LabOrderRepository(db_session)


async def _add_test(
    tests: LabTestRepository, hospital_id: uuid.UUID, code: str, **fields: Any
) -> LabTest:
    """Insert a catalog test with sensible defaults."""
    values: dict[str, Any] = {
        "name": code.title(),
        "price": Decimal("300.00"),
        "reference_ranges": RANGES,
    }
    values.update(fields)
    return await tests.create_test(hospital_id=hospital_id, code=code, **values)


async def _add_order(
    session: AsyncSession,
    orders: LabOrderRepository,
    hospital_id: uuid.UUID,
    *tests: LabTest,
    ordered_at: datetime = NOW,
    **fields: Any,
) -> LabOrder:
    """Insert an order for a fresh patient and doctor."""
    patient = await insert_patient(session, hospital_id)
    doctor = await insert_doctor(session, hospital_id)
    appointment = await insert_appointment(
        session, hospital_id, patient_id=patient.id, doctor_id=doctor.id
    )
    return await orders.create_order(
        hospital_id=hospital_id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_id=appointment.id,
        ordered_at=ordered_at,
        items=[
            {
                "test_id": test.id,
                "test_code": test.code,
                "test_name": test.name,
                "result_type": test.result_type,
                "price": test.price,
            }
            for test in tests
        ],
        **fields,
    )


class TestCatalog:
    async def test_persists_with_defaults(
        self, tests: LabTestRepository, hospital_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        test = await _add_test(tests, hospital_id, "K", created_by=actor_id)

        assert test.result_type is LabResultType.NUMERIC
        assert test.is_active is True
        assert test.reference_ranges == RANGES
        assert test.price == Decimal("300.00")
        assert test.created_by == actor_id

    async def test_code_is_unique_per_hospital_only(
        self,
        tests: LabTestRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        await _add_test(tests, hospital_id, "K")
        await _add_test(tests, other_hospital_id, "K")

        with pytest.raises(IntegrityError, match="uq_tests_catalog_hospital_code"):
            async with db_session.begin_nested():
                await _add_test(tests, hospital_id, "K")

    async def test_a_negative_price_is_refused_by_the_database(
        self, tests: LabTestRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        with pytest.raises(IntegrityError, match="price_non_negative"):
            async with db_session.begin_nested():
                await _add_test(tests, hospital_id, "K", price=Decimal("-1.00"))

    async def test_every_lookup_is_tenant_scoped(
        self, tests: LabTestRepository, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
    ) -> None:
        mine = await _add_test(tests, hospital_id, "K")
        theirs = await _add_test(tests, other_hospital_id, "NA")

        assert await tests.get_test_by_id(hospital_id, theirs.id) is None
        assert await tests.get_test_by_code(hospital_id, "NA") is None
        assert await tests.get_tests_by_ids(hospital_id, [mine.id, theirs.id]) == [mine]
        assert await tests.get_tests_by_ids(hospital_id, []) == []
        assert [t.id for t in await tests.list_tests(hospital_id)] == [mine.id]
        assert await tests.count_tests(hospital_id) == 1
        found = await tests.get_test_by_id(hospital_id, mine.id)
        assert found is not None
        assert (await tests.get_test_by_code(hospital_id, "K")) is found

    async def test_list_filters_orders_and_pages(
        self, tests: LabTestRepository, hospital_id: uuid.UUID
    ) -> None:
        await _add_test(tests, hospital_id, "NA", name="Sodium", category="Biochemistry")
        await _add_test(tests, hospital_id, "K", name="Potassium", category="Biochemistry")
        await _add_test(tests, hospital_id, "HB", name="Haemoglobin", category="Haematology")
        await _add_test(tests, hospital_id, "OLD", name="Retired 100%_", is_active=False)

        async def names(**filters: Any) -> list[str]:
            return [t.name for t in await tests.list_tests(hospital_id, **filters)]

        assert await names(is_active=True) == ["Haemoglobin", "Potassium", "Sodium"]
        assert await names(category="Biochemistry") == ["Potassium", "Sodium"]
        assert await names(term="so") == ["Sodium"]
        assert await names(term="hb") == ["Haemoglobin"]  # exact code, any case
        # % and _ in a term are literal, not wildcards.
        assert await names(term="%") == []
        assert await names(term="retired 100%_") == ["Retired 100%_"]
        assert await names(is_active=True, skip=1, limit=1) == ["Potassium"]
        assert await tests.count_tests(hospital_id, category="Biochemistry") == 2

    async def test_update(
        self, tests: LabTestRepository, hospital_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        test = await _add_test(tests, hospital_id, "K")

        updated = await tests.update_test(test, updated_by=actor_id, is_active=False)

        assert updated.is_active is False
        assert updated.updated_by == actor_id


class TestOrders:
    async def test_create_persists_the_order_with_its_items_in_position(
        self,
        tests: LabTestRepository,
        orders: LabOrderRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        potassium = await _add_test(tests, hospital_id, "K")
        sodium = await _add_test(tests, hospital_id, "NA")

        order = await _add_order(
            db_session, orders, hospital_id, sodium, potassium, priority=LabOrderPriority.STAT
        )

        assert order.status is LabOrderStatus.ORDERED
        assert order.priority is LabOrderPriority.STAT
        assert order.ordered_at == NOW
        assert [(i.position, i.test_code) for i in order.items] == [(0, "NA"), (1, "K")]
        assert {i.hospital_id for i in order.items} == {hospital_id}
        # Loaded with the order, so a response can be built without more IO.
        assert order.patient.full_name
        assert order.doctor.user.first_name

    async def test_a_test_cannot_be_on_an_order_twice(
        self,
        tests: LabTestRepository,
        orders: LabOrderRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        potassium = await _add_test(tests, hospital_id, "K")

        with pytest.raises(IntegrityError, match="uq_lab_order_items_order_test"):
            async with db_session.begin_nested():
                await _add_order(db_session, orders, hospital_id, potassium, potassium)

    async def test_gets_are_tenant_scoped(
        self,
        tests: LabTestRepository,
        orders: LabOrderRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        order = await _add_order(
            db_session, orders, hospital_id, await _add_test(tests, hospital_id, "K")
        )

        assert await orders.get_order_by_id(other_hospital_id, order.id) is None
        assert await orders.get_order_for_update(other_hospital_id, order.id) is None
        assert (await orders.get_order_by_id(hospital_id, order.id)) is order
        assert (await orders.get_order_for_update(hospital_id, order.id)) is order

    async def test_list_is_tenant_scoped_newest_first_and_filterable(
        self,
        tests: LabTestRepository,
        orders: LabOrderRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        potassium = await _add_test(tests, hospital_id, "K")
        older = await _add_order(
            db_session, orders, hospital_id, potassium, ordered_at=NOW - timedelta(hours=2)
        )
        newer = await _add_order(
            db_session, orders, hospital_id, potassium, priority=LabOrderPriority.URGENT
        )
        await orders.update_order(older, status=LabOrderStatus.COLLECTED)
        await _add_order(
            db_session, orders, other_hospital_id, await _add_test(tests, other_hospital_id, "K")
        )

        async def ids(**filters: Any) -> list[Any]:
            return [o.id for o in await orders.list_orders(hospital_id, **filters)]

        assert await ids() == [newer.id, older.id]
        assert await orders.count_orders(hospital_id) == 2
        assert await ids(status=LabOrderStatus.COLLECTED) == [older.id]
        assert await ids(priority=LabOrderPriority.URGENT) == [newer.id]
        assert await ids(patient_id=older.patient_id) == [older.id]
        assert await ids(doctor_id=newer.doctor_id) == [newer.id]
        assert await ids(appointment_id=older.appointment_id) == [older.id]
        assert await ids(skip=1, limit=5) == [older.id]
        assert await orders.count_orders(hospital_id, status=LabOrderStatus.ORDERED) == 1
        # Another hospital's patient is not found through this hospital.
        assert await ids(patient_id=older.patient_id) == [older.id]
        assert await orders.list_orders(other_hospital_id, patient_id=older.patient_id) == []

    async def test_rule_5_a_sample_id_is_unique_per_hospital(
        self,
        tests: LabTestRepository,
        orders: LabOrderRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        potassium = await _add_test(tests, hospital_id, "K")
        first = await _add_order(db_session, orders, hospital_id, potassium)
        second = await _add_order(db_session, orders, hospital_id, potassium)
        elsewhere = await _add_order(
            db_session, orders, other_hospital_id, await _add_test(tests, other_hospital_id, "K")
        )
        await orders.update_item(first.items[0], sample_id="BC-001", sample_collected_at=NOW)

        # Another hospital may print the same barcode...
        await orders.update_item(elsewhere.items[0], sample_id="BC-001")
        # ...this one may not.
        with pytest.raises(IntegrityError, match="uq_lab_order_items_hospital_sample"):
            async with db_session.begin_nested():
                await orders.update_item(second.items[0], sample_id="BC-001")

    async def test_update_item_and_amendment_round_trip(
        self,
        tests: LabTestRepository,
        orders: LabOrderRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        user = await insert_user(db_session, hospital_id)
        order = await _add_order(
            db_session, orders, hospital_id, await _add_test(tests, hospital_id, "K")
        )
        item = order.items[0]
        await orders.update_item(
            item,
            updated_by=user.id,
            result_value="4.2",
            result_flag=LabResultFlag.NORMAL,
            reference_low=Decimal("3.5"),
            reference_high=Decimal("5.1"),
        )

        amendment = await orders.add_amendment(
            item,
            previous_value="4.2",
            new_value="5.9",
            previous_flag=LabResultFlag.NORMAL,
            new_flag=LabResultFlag.HIGH,
            reason="Transcription error",
            amended_by=user.id,
            amended_at=NOW,
        )
        order_id, user_id, amendment_id = order.id, user.id, amendment.id
        db_session.expire_all()
        reloaded = await orders.get_order_by_id(hospital_id, order_id)

        assert reloaded is not None
        stored = reloaded.items[0]
        assert stored.result_flag is LabResultFlag.NORMAL
        assert stored.reference_low == Decimal("3.5000")
        assert stored.updated_by == user_id
        assert [a.id for a in stored.amendments] == [amendment_id]
        assert stored.amendments[0].hospital_id == hospital_id
        assert stored.amendments[0].new_flag is LabResultFlag.HIGH

    async def test_the_database_refuses_a_release_without_a_time_or_a_cancel_without_a_reason(
        self,
        tests: LabTestRepository,
        orders: LabOrderRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        order = await _add_order(
            db_session, orders, hospital_id, await _add_test(tests, hospital_id, "K")
        )

        with pytest.raises(IntegrityError, match="released_has_time"):
            async with db_session.begin_nested():
                await orders.update_order(order, status=LabOrderStatus.RELEASED)
        await db_session.refresh(order)
        with pytest.raises(IntegrityError, match="cancelled_has_reason"):
            async with db_session.begin_nested():
                await orders.update_order(order, status=LabOrderStatus.CANCELLED)
