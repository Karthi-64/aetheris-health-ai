"""Unit tests for the lab order and lab catalog services.

The repositories are small in-memory fakes rather than mocks: what matters is
the state a lifecycle step leaves behind — statuses, flags, timestamps, the
amendment trail — and that reads more clearly as assertions on the order than
on call arguments. The SQL is covered in the repository suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import BusinessRuleError, ValidationError
from app.models.appointment import AppointmentStatus
from app.models.lab import (
    LabOrder,
    LabOrderItem,
    LabOrderPriority,
    LabOrderStatus,
    LabResultAmendment,
    LabResultFlag,
    LabResultType,
    LabTest,
)
from app.models.patient import Gender
from app.schemas.common import PaginationParams
from app.schemas.lab import (
    AmendResultRequest,
    CancelLabOrderRequest,
    CollectSamplesRequest,
    CreateLabOrderRequest,
    CreateLabTestRequest,
    EnterResultsRequest,
    UpdateLabOrderRequest,
    UpdateLabTestRequest,
)
from app.services.lab_catalog_service import (
    DuplicateLabTestCodeError,
    LabCatalogService,
    LabTestNotFoundError,
)
from app.services.lab_service import (
    DuplicateSampleIdError,
    LabOrderItemNotFoundError,
    LabOrderNotFoundError,
    LabOrderStateError,
    LabService,
)
from app.tests.conftest import FakeSession, RecordingAuditSink

HOSPITAL_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()
DOCTOR_USER_ID = uuid.uuid4()
INVOICE_ID = uuid.uuid4()
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)

POTASSIUM_RANGES = [
    {"sex": "any", "low": "3.5", "high": "5.1", "critical_low": "2.5", "critical_high": "6.5"}
]


def _test(code: str = "K", **overrides: Any) -> LabTest:
    """A detached catalog test."""
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "hospital_id": HOSPITAL_ID,
        "code": code,
        "name": {"K": "Potassium", "HB": "Haemoglobin", "URINE": "Urine microscopy"}.get(
            code, code.title()
        ),
        "category": "Biochemistry",
        "unit": "mmol/L",
        "result_type": LabResultType.NUMERIC,
        "reference_ranges": POTASSIUM_RANGES,
        "turnaround_hours": 4,
        "price": Decimal("300.00"),
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return LabTest(**values)


class FakeTestRepository:
    """In-memory stand-in for ``LabTestRepository``."""

    def __init__(self, *tests: LabTest) -> None:
        self.tests = {test.id: test for test in tests}
        self.raise_on_create: Exception | None = None

    async def get_tests_by_ids(self, hospital_id: uuid.UUID, ids: Any) -> list[LabTest]:
        return [t for t in self.tests.values() if t.id in ids and t.hospital_id == hospital_id]

    async def get_test_by_id(self, hospital_id: uuid.UUID, test_id: uuid.UUID) -> LabTest | None:
        test = self.tests.get(test_id)
        return test if test is not None and test.hospital_id == hospital_id else None

    async def get_test_by_code(self, hospital_id: uuid.UUID, code: str) -> LabTest | None:
        return next(
            (t for t in self.tests.values() if t.code == code and t.hospital_id == hospital_id),
            None,
        )

    async def create_test(self, **fields: Any) -> LabTest:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        fields.pop("created_by", None)
        test = _test(**fields)
        self.tests[test.id] = test
        return test

    async def update_test(self, test: LabTest, *, updated_by: Any = None, **fields: Any) -> LabTest:
        for name, value in fields.items():
            setattr(test, name, value)
        return test

    async def list_tests(self, hospital_id: uuid.UUID, *, skip: int, limit: int, **_: Any) -> Any:
        return list(self.tests.values())[skip : skip + limit]

    async def count_tests(self, hospital_id: uuid.UUID, **_: Any) -> int:
        return len(self.tests)


class FakeOrderRepository:
    """In-memory stand-in for ``LabOrderRepository``."""

    def __init__(self) -> None:
        self.orders: dict[uuid.UUID, LabOrder] = {}
        self.taken_sample_ids: set[str] = set()

    async def create_order(self, *, items: list[dict[str, Any]], **fields: Any) -> LabOrder:
        order = LabOrder(
            id=uuid.uuid4(),
            status=LabOrderStatus.ORDERED,
            created_at=NOW,
            updated_at=NOW,
            items=[
                LabOrderItem(
                    id=uuid.uuid4(),
                    hospital_id=fields["hospital_id"],
                    position=position,
                    amendments=[],
                    **{k: v for k, v in values.items() if k != "created_by"},
                )
                for position, values in enumerate(items)
            ],
            **{k: v for k, v in fields.items() if k != "created_by"},
        )
        patient = MagicMock()
        patient.full_name = "Ananya Rao"
        patient.mrn = "MRN-2026-00001"
        patient.gender = Gender.FEMALE
        patient.date_of_birth = date(1990, 1, 1)
        doctor = MagicMock()
        doctor.user_id = DOCTOR_USER_ID
        doctor.user.first_name = "Meera"
        doctor.user.last_name = "Iyer"
        # Bypass the instrumented relationships: a detached instance never
        # loads them, and a MagicMock cannot be assigned through one.
        order.__dict__["patient"] = patient
        order.__dict__["doctor"] = doctor
        self.orders[order.id] = order
        return order

    async def update_order(self, order: LabOrder, *, updated_by: Any = None, **fields: Any) -> Any:
        for name, value in fields.items():
            setattr(order, name, value)
        return order

    async def update_item(
        self, item: LabOrderItem, *, updated_by: Any = None, **fields: Any
    ) -> None:
        sample_id = fields.get("sample_id")
        if sample_id is not None and sample_id in self.taken_sample_ids:
            raise IntegrityError(
                "INSERT", {}, Exception('violates "uq_lab_order_items_hospital_sample"')
            )
        if sample_id is not None:
            self.taken_sample_ids.add(sample_id)
        for name, value in fields.items():
            setattr(item, name, value)

    async def add_amendment(self, item: LabOrderItem, **fields: Any) -> LabResultAmendment:
        amendment = LabResultAmendment(
            id=uuid.uuid4(), hospital_id=item.hospital_id, item_id=item.id, **fields
        )
        item.amendments.append(amendment)
        return amendment

    async def get_order_by_id(self, hospital_id: uuid.UUID, order_id: uuid.UUID) -> Any:
        order = self.orders.get(order_id)
        return order if order is not None and order.hospital_id == hospital_id else None

    get_order_for_update = get_order_by_id

    async def list_orders(self, hospital_id: uuid.UUID, *, skip: int, limit: int, **f: Any) -> Any:
        rows = [o for o in self.orders.values() if f.get("status") in (None, o.status)]
        return rows[skip : skip + limit]

    async def count_orders(self, hospital_id: uuid.UUID, **f: Any) -> int:
        return len([o for o in self.orders.values() if f.get("status") in (None, o.status)])


class _World:
    """A lab service over fakes, plus the handles a test needs."""

    def __init__(self, *tests: LabTest, visit_status: AppointmentStatus | None = None) -> None:
        self.potassium = _test("K")
        self.haemoglobin = _test(
            "HB",
            unit="g/dL",
            price=Decimal("250.00"),
            reference_ranges=[
                {"sex": "male", "age_min": 18, "low": "13.0", "high": "17.0"},
                {"sex": "female", "age_min": 18, "low": "12.0", "high": "15.5"},
            ],
        )
        self.tests = FakeTestRepository(self.potassium, self.haemoglobin, *tests)
        self.orders = FakeOrderRepository()
        self.session = FakeSession()
        self.audit = RecordingAuditSink()

        self.appointment = MagicMock()
        self.appointment.id = uuid.uuid4()
        self.appointment.patient_id = uuid.uuid4()
        self.appointment.doctor_id = uuid.uuid4()
        self.appointment.status = visit_status or AppointmentStatus.IN_PROGRESS
        self.appointments = AsyncMock()
        self.appointments.get_appointment_by_id.return_value = self.appointment

        self.charges = AsyncMock()
        self.charges.add_charges.return_value = INVOICE_ID
        self.notifier = AsyncMock()

        self.service = LabService(
            self.orders,  # type: ignore[arg-type]
            self.tests,  # type: ignore[arg-type]
            self.appointments,
            self.session,  # type: ignore[arg-type]
            self.audit,
            charges=self.charges,
            notifier=self.notifier,
        )

    async def order(self, *tests: LabTest, **overrides: Any) -> Any:
        chosen = tests or (self.potassium, self.haemoglobin)
        body: dict[str, Any] = {
            "appointment_id": self.appointment.id,
            "test_ids": [test.id for test in chosen],
        }
        body.update(overrides)
        return await self.service.create_order(
            HOSPITAL_ID, CreateLabOrderRequest.model_validate(body), actor_id=ACTOR_ID
        )

    async def collected(self, *tests: LabTest) -> Any:
        order = await self.order(*tests)
        return await self.service.collect_samples(HOSPITAL_ID, order.id, actor_id=ACTOR_ID)

    async def enter(self, order: Any, **values: str) -> Any:
        by_code = {item.test_code: item.id for item in order.items}
        return await self.service.enter_results(
            HOSPITAL_ID,
            order.id,
            EnterResultsRequest.model_validate(
                {
                    "results": [
                        {"item_id": by_code[code], "value": value} for code, value in values.items()
                    ]
                }
            ),
            actor_id=ACTOR_ID,
        )

    async def released(self, **values: str) -> Any:
        order = await self.collected()
        await self.enter(order, **(values or {"K": "4.2", "HB": "13.0"}))
        return await self.service.release_order(HOSPITAL_ID, order.id, actor_id=ACTOR_ID)

    def notices(self) -> list[str]:
        return [call.args[0].kind for call in self.notifier.notify.await_args_list]


def _item(order: Any, code: str) -> Any:
    return next(item for item in order.items if item.test_code == code)


# ── Ordering ────────────────────────────────────────────────────────────────


class TestCreateOrder:
    async def test_the_patient_and_doctor_come_from_the_visit(self) -> None:
        world = _World()

        order = await world.order(priority="urgent", notes="Fasting sample.")

        assert order.status is LabOrderStatus.ORDERED
        assert order.patient_id == world.appointment.patient_id
        assert order.doctor_id == world.appointment.doctor_id
        assert order.appointment_id == world.appointment.id
        assert order.priority is LabOrderPriority.URGENT
        assert order.notes == "Fasting sample."
        assert order.patient_name == "Ananya Rao"
        assert order.doctor_name == "Dr. Meera Iyer"
        assert order.turnaround_minutes is None
        assert (order.has_abnormal, order.has_critical) == (False, False)

    async def test_items_snapshot_the_catalog_in_the_order_requested(self) -> None:
        world = _World()

        order = await world.order(world.haemoglobin, world.potassium)

        assert [item.test_code for item in order.items] == ["HB", "K"]
        assert [item.test_name for item in order.items] == ["Haemoglobin", "Potassium"]
        assert [item.price for item in order.items] == [Decimal("250.00"), Decimal("300.00")]
        assert all(item.sample_id is None and item.result_value is None for item in order.items)

    async def test_each_test_is_charged_in_the_same_transaction(self) -> None:
        world = _World()

        order = await world.order()

        call = world.charges.add_charges.await_args
        assert call.args == (HOSPITAL_ID,)
        assert call.kwargs["patient_id"] == world.appointment.patient_id
        assert call.kwargs["appointment_id"] == world.appointment.id
        assert call.kwargs["source"] == "laboratory"
        assert [(c.description, c.unit_price) for c in call.kwargs["charges"]] == [
            ("Lab test — Potassium", Decimal("300.00")),
            ("Lab test — Haemoglobin", Decimal("250.00")),
        ]
        assert order.invoice_id == INVOICE_ID
        assert world.session.commits == 1

    async def test_it_audits_without_clinical_detail(self) -> None:
        world = _World()

        order = await world.order()

        event = world.audit.last()
        assert event.action == "lab.order.created"
        assert event.target_id == order.id
        assert event.actor_id == ACTOR_ID
        assert event.context["tests"] == ["K", "HB"]
        assert event.context["invoice_id"] == str(INVOICE_ID)

    async def test_with_no_charge_sink_the_order_still_stands(self) -> None:
        world = _World()
        service = LabService(
            world.orders,  # type: ignore[arg-type]
            world.tests,  # type: ignore[arg-type]
            world.appointments,
            world.session,  # type: ignore[arg-type]
            world.audit,
        )

        order = await service.create_order(
            HOSPITAL_ID,
            CreateLabOrderRequest(
                appointment_id=world.appointment.id, test_ids=[world.potassium.id]
            ),
            actor_id=ACTOR_ID,
        )

        assert order.invoice_id is None

    async def test_a_failed_charge_fails_the_order(self) -> None:
        world = _World()
        world.charges.add_charges.side_effect = RuntimeError("billing is down")

        with pytest.raises(RuntimeError):
            await world.order()

        assert world.session.commits == 0
        assert world.audit.events == []

    async def test_an_unknown_appointment_is_a_422(self) -> None:
        world = _World()
        world.appointments.get_appointment_by_id.return_value = None

        with pytest.raises(ValidationError) as excinfo:
            await world.order()

        assert excinfo.value.detail["errors"][0]["field"] == "appointment_id"
        assert world.orders.orders == {}

    @pytest.mark.parametrize("status", [AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW])
    async def test_a_visit_that_did_not_happen_cannot_be_ordered_against(
        self, status: AppointmentStatus
    ) -> None:
        world = _World(visit_status=status)

        with pytest.raises(ValidationError, match=status.value):
            await world.order()

    async def test_an_unknown_test_is_a_422_naming_its_position(self) -> None:
        world = _World()

        with pytest.raises(ValidationError) as excinfo:
            await world.order(test_ids=[world.potassium.id, uuid.uuid4()])

        assert excinfo.value.detail["errors"][0]["field"] == "test_ids.1"
        world.charges.add_charges.assert_not_awaited()

    async def test_another_hospitals_test_is_unknown(self) -> None:
        foreign = _test("FOREIGN", hospital_id=uuid.uuid4())
        world = _World(foreign)

        with pytest.raises(ValidationError, match="not found"):
            await world.order(foreign)

    async def test_an_inactive_test_cannot_be_ordered(self) -> None:
        retired = _test("OLD", is_active=False)
        world = _World(retired)

        with pytest.raises(ValidationError, match="inactive"):
            await world.order(retired)


class TestUpdateOrder:
    async def test_changes_priority_and_audits_the_change(self) -> None:
        world = _World()
        order = await world.order()

        updated = await world.service.update_order(
            HOSPITAL_ID,
            order.id,
            UpdateLabOrderRequest(priority=LabOrderPriority.STAT),
            actor_id=ACTOR_ID,
        )

        assert updated.priority is LabOrderPriority.STAT
        assert world.audit.last().action == "lab.order.updated"
        assert world.audit.last().changes == {"priority": {"before": "routine", "after": "stat"}}

    async def test_a_no_op_writes_and_audits_nothing(self) -> None:
        world = _World()
        order = await world.order()
        commits = world.session.commits

        await world.service.update_order(
            HOSPITAL_ID,
            order.id,
            UpdateLabOrderRequest(priority=LabOrderPriority.ROUTINE),
            actor_id=ACTOR_ID,
        )

        assert world.session.commits == commits
        assert world.audit.actions() == ["lab.order.created"]

    async def test_a_released_order_cannot_be_edited(self) -> None:
        world = _World()
        order = await world.released()

        with pytest.raises(LabOrderStateError, match="released"):
            await world.service.update_order(
                HOSPITAL_ID, order.id, UpdateLabOrderRequest(notes="late"), actor_id=ACTOR_ID
            )

    async def test_an_unknown_order_is_a_404(self) -> None:
        with pytest.raises(LabOrderNotFoundError):
            await _World().service.update_order(
                HOSPITAL_ID, uuid.uuid4(), UpdateLabOrderRequest(notes="x")
            )


# ── Collection ──────────────────────────────────────────────────────────────


class TestCollectSamples:
    async def test_with_no_body_every_sample_is_collected_with_a_generated_id(self) -> None:
        world = _World()

        order = await world.collected()

        assert order.status is LabOrderStatus.COLLECTED
        assert order.collected_at is not None
        sample_ids = [item.sample_id for item in order.items]
        assert all(sid is not None and sid.startswith("S-") for sid in sample_ids)
        assert len(set(sample_ids)) == 2
        assert all(item.sample_collected_at is not None for item in order.items)
        assert world.audit.last().action == "lab.order.samples_collected"
        assert world.audit.last().context["status"] == "collected"

    async def test_collecting_some_leaves_the_order_ordered(self) -> None:
        world = _World()
        order = await world.order()
        potassium = _item(order, "K")

        partial = await world.service.collect_samples(
            HOSPITAL_ID,
            order.id,
            CollectSamplesRequest.model_validate(
                {"items": [{"item_id": str(potassium.id), "sample_id": "bc-001"}]}
            ),
            actor_id=ACTOR_ID,
        )

        assert partial.status is LabOrderStatus.ORDERED
        assert _item(partial, "K").sample_id == "BC-001"
        assert _item(partial, "HB").sample_id is None

        # The rest follows, and only then is the order collected.
        done = await world.service.collect_samples(HOSPITAL_ID, order.id, actor_id=ACTOR_ID)
        assert done.status is LabOrderStatus.COLLECTED
        assert _item(done, "K").sample_id == "BC-001"

    async def test_rule_5_a_sample_id_in_use_is_a_409(self) -> None:
        world = _World()
        order = await world.order()
        world.orders.taken_sample_ids.add("BC-001")

        with pytest.raises(DuplicateSampleIdError):
            await world.service.collect_samples(
                HOSPITAL_ID,
                order.id,
                CollectSamplesRequest.model_validate(
                    {"items": [{"item_id": str(order.items[0].id), "sample_id": "BC-001"}]}
                ),
                actor_id=ACTOR_ID,
            )

        assert world.session.savepoints_rolled_back == 1

    async def test_an_unrelated_integrity_error_is_not_swallowed(self) -> None:
        world = _World()
        order = await world.order()

        async def explode(*_: Any, **__: Any) -> None:
            raise IntegrityError("UPDATE", {}, Exception("some other constraint"))

        world.orders.update_item = explode  # type: ignore[method-assign]

        with pytest.raises(IntegrityError):
            await world.service.collect_samples(HOSPITAL_ID, order.id, actor_id=ACTOR_ID)

    async def test_an_item_from_another_order_is_a_404(self) -> None:
        world = _World()
        order = await world.order()

        with pytest.raises(LabOrderItemNotFoundError):
            await world.service.collect_samples(
                HOSPITAL_ID,
                order.id,
                CollectSamplesRequest.model_validate({"items": [{"item_id": str(uuid.uuid4())}]}),
            )

    async def test_a_sample_cannot_be_collected_twice(self) -> None:
        world = _World()
        order = await world.order()
        entry = CollectSamplesRequest.model_validate(
            {"items": [{"item_id": str(order.items[0].id)}]}
        )
        await world.service.collect_samples(HOSPITAL_ID, order.id, entry, actor_id=ACTOR_ID)

        with pytest.raises(BusinessRuleError, match="already been collected"):
            await world.service.collect_samples(HOSPITAL_ID, order.id, entry, actor_id=ACTOR_ID)

    async def test_a_collected_order_cannot_be_collected_again(self) -> None:
        world = _World()
        order = await world.collected()

        with pytest.raises(LabOrderStateError):
            await world.service.collect_samples(HOSPITAL_ID, order.id)


# ── Results ─────────────────────────────────────────────────────────────────


class TestEnterResults:
    async def test_ac2_out_of_range_values_are_flagged_automatically(self) -> None:
        world = _World()
        order = await world.collected()

        result = await world.enter(order, K="5.6", HB="11.2")

        potassium, haemoglobin = _item(result, "K"), _item(result, "HB")
        assert potassium.result_flag is LabResultFlag.HIGH
        assert (potassium.reference_low, potassium.reference_high) == (
            Decimal("3.5"),
            Decimal("5.1"),
        )
        assert potassium.result_unit == "mmol/L"
        # The patient is a woman of 36: the female range, 12.0–15.5, applies.
        assert haemoglobin.result_flag is LabResultFlag.LOW
        assert haemoglobin.reference_low == Decimal("12.0")
        assert (result.has_abnormal, result.has_critical) == (True, False)
        assert result.status is LabOrderStatus.RESULTS_ENTERED
        assert result.results_entered_at is not None

    async def test_a_normal_result_is_flagged_normal(self) -> None:
        world = _World()
        order = await world.collected()

        result = await world.enter(order, K="4.2", HB="13.0")

        assert {item.result_flag for item in result.items} == {LabResultFlag.NORMAL}
        assert result.has_abnormal is False

    async def test_entering_some_results_leaves_the_order_in_progress(self) -> None:
        world = _World()
        order = await world.collected()

        partial = await world.enter(order, K="4.2")

        assert partial.status is LabOrderStatus.IN_PROGRESS
        assert partial.results_entered_at is None
        assert _item(partial, "HB").result_value is None

        done = await world.enter(order, HB="13.0")
        assert done.status is LabOrderStatus.RESULTS_ENTERED

    async def test_a_result_can_be_re_entered_before_release(self) -> None:
        world = _World()
        order = await world.collected()
        await world.enter(order, K="5.6", HB="13.0")

        corrected = await world.enter(order, K="4.6")

        assert _item(corrected, "K").result_value == "4.6"
        assert _item(corrected, "K").result_flag is LabResultFlag.NORMAL
        assert _item(corrected, "K").amendments == []
        assert world.audit.last().changes == {"K": {"before": "5.6", "after": "4.6"}}

    async def test_a_critical_value_notifies_the_doctor_at_once(self) -> None:
        # §14: "potassium > 6.5 → immediate notification to doctor".
        world = _World()
        order = await world.collected()

        result = await world.enter(order, K="6.8", HB="13.0")

        assert _item(result, "K").result_flag is LabResultFlag.CRITICAL
        assert result.has_critical is True
        [call] = world.notifier.notify.await_args_list
        request = call.args[0]
        assert request.kind == "lab.critical_result"
        assert request.recipient_user_ids == (DOCTOR_USER_ID,)
        assert request.hospital_id == HOSPITAL_ID
        assert request.variables == {
            "patient_name": "Ananya Rao",
            "test_name": "Potassium",
            "result": "6.8 mmol/L",
        }

    async def test_re_entering_a_still_critical_value_does_not_notify_twice(self) -> None:
        world = _World()
        order = await world.collected()
        await world.enter(order, K="6.8")

        await world.enter(order, K="7.0")

        assert world.notices() == ["lab.critical_result"]

    async def test_a_text_test_takes_any_text_and_has_no_flag(self) -> None:
        urine = _test("URINE", result_type=LabResultType.TEXT, reference_ranges=[], unit=None)
        world = _World(urine)
        order = await world.collected(urine)

        result = await world.enter(order, URINE="No casts seen")

        assert result.items[0].result_value == "No casts seen"
        assert result.items[0].result_flag is None
        assert result.has_abnormal is False

    async def test_a_numeric_test_rejects_text_and_writes_nothing(self) -> None:
        world = _World()
        order = await world.collected()

        with pytest.raises(ValidationError) as excinfo:
            await world.enter(order, HB="13.0", K="high")

        assert excinfo.value.detail["errors"][0]["field"] == "results.1.value"
        stored = world.orders.orders[order.id]
        assert all(item.result_value is None for item in stored.items)

    async def test_no_applicable_range_means_no_flag(self) -> None:
        men_only = _test(
            "PSA", reference_ranges=[{"sex": "male", "low": "0", "high": "4"}], unit="ng/mL"
        )
        world = _World(men_only)
        order = await world.collected(men_only)

        result = await world.enter(order, PSA="9.0")

        # The patient is female; no range applies, so nothing is claimed.
        assert result.items[0].result_flag is None
        assert result.items[0].reference_high is None

    async def test_a_test_retired_from_the_catalog_keeps_its_unit_and_gets_no_flag(self) -> None:
        world = _World()
        order = await world.collected(world.potassium)
        del world.tests.tests[world.potassium.id]

        result = await world.enter(order, K="9.9")

        assert result.items[0].result_flag is None

    async def test_results_need_the_samples_first(self) -> None:
        world = _World()
        order = await world.order()

        with pytest.raises(LabOrderStateError, match="ordered"):
            await world.enter(order, K="4.2")

    async def test_an_item_from_another_order_is_a_404(self) -> None:
        world = _World()
        order = await world.collected()

        with pytest.raises(LabOrderItemNotFoundError):
            await world.service.enter_results(
                HOSPITAL_ID,
                order.id,
                EnterResultsRequest.model_validate(
                    {"results": [{"item_id": str(uuid.uuid4()), "value": "1"}]}
                ),
            )

    async def test_the_audit_names_abnormal_tests_by_code(self) -> None:
        world = _World()
        order = await world.collected()

        await world.enter(order, K="5.6", HB="13.0")

        event = world.audit.last()
        assert event.action == "lab.order.results_entered"
        assert event.context == {"status": "results_entered", "abnormal": ["K"]}


# ── Release, cancel, amend ──────────────────────────────────────────────────


class TestReleaseOrder:
    async def test_release_stamps_everything_and_tells_the_doctor(self) -> None:
        world = _World()

        order = await world.released()

        assert order.status is LabOrderStatus.RELEASED
        assert order.released_at is not None
        assert order.released_by == ACTOR_ID
        assert all(item.released_at == order.released_at for item in order.items)
        assert order.turnaround_minutes is not None
        assert order.turnaround_minutes >= 0
        assert world.notices() == ["lab.results_released"]
        request = world.notifier.notify.await_args.args[0]
        assert request.recipient_user_ids == (DOCTOR_USER_ID,)
        assert request.variables["test_names"] == "Potassium, Haemoglobin"
        assert world.audit.last().action == "lab.order.released"

    async def test_release_needs_every_result(self) -> None:
        # §11: "release only after all items have results".
        world = _World()
        order = await world.collected()
        await world.enter(order, K="4.2")

        with pytest.raises(LabOrderStateError, match="in progress"):
            await world.service.release_order(HOSPITAL_ID, order.id, actor_id=ACTOR_ID)

        assert world.notices() == []

    async def test_an_order_is_released_once(self) -> None:
        world = _World()
        order = await world.released()

        with pytest.raises(LabOrderStateError):
            await world.service.release_order(HOSPITAL_ID, order.id, actor_id=ACTOR_ID)


class TestCancelOrder:
    async def test_cancel_records_the_reason_and_points_at_the_invoice(self) -> None:
        world = _World()
        order = await world.collected()

        cancelled = await world.service.cancel_order(
            HOSPITAL_ID,
            order.id,
            CancelLabOrderRequest(reason="Sample haemolysed"),
            actor_id=ACTOR_ID,
        )

        assert cancelled.status is LabOrderStatus.CANCELLED
        assert cancelled.cancel_reason == "Sample haemolysed"
        assert cancelled.cancelled_at is not None
        event = world.audit.last()
        assert event.action == "lab.order.cancelled"
        assert event.changes == {"status": {"before": "collected", "after": "cancelled"}}
        # The charge is not undone here; the audit says where it is.
        assert event.context == {"reason": "Sample haemolysed", "invoice_id": str(INVOICE_ID)}

    async def test_a_released_order_cannot_be_cancelled(self) -> None:
        world = _World()
        order = await world.released()

        with pytest.raises(LabOrderStateError, match="released"):
            await world.service.cancel_order(
                HOSPITAL_ID, order.id, CancelLabOrderRequest(reason="x")
            )

    async def test_an_order_is_cancelled_once(self) -> None:
        world = _World()
        order = await world.order()
        reason = CancelLabOrderRequest(reason="Ordered in error")
        await world.service.cancel_order(HOSPITAL_ID, order.id, reason)

        with pytest.raises(LabOrderStateError, match="cancelled"):
            await world.service.cancel_order(HOSPITAL_ID, order.id, reason)


class TestAmendResult:
    async def _amend(self, world: _World, order: Any, code: str, value: str) -> Any:
        return await world.service.amend_result(
            HOSPITAL_ID,
            order.id,
            _item(order, code).id,
            AmendResultRequest(new_value=value, reason="Transcription error"),
            actor_id=ACTOR_ID,
        )

    async def test_rule_4_the_old_value_is_kept_and_the_result_reflagged(self) -> None:
        world = _World()
        order = await world.released(K="4.2", HB="13.0")

        amended = await self._amend(world, order, "K", "5.9")

        potassium = _item(amended, "K")
        assert potassium.result_value == "5.9"
        assert potassium.result_flag is LabResultFlag.HIGH
        [amendment] = potassium.amendments
        assert (amendment.previous_value, amendment.new_value) == ("4.2", "5.9")
        assert (amendment.previous_flag, amendment.new_flag) == (
            LabResultFlag.NORMAL,
            LabResultFlag.HIGH,
        )
        assert amendment.reason == "Transcription error"
        assert amendment.amended_by == ACTOR_ID
        assert amended.status is LabOrderStatus.RELEASED
        assert amended.has_abnormal is True

    async def test_the_doctor_is_told_and_it_is_audited(self) -> None:
        world = _World()
        order = await world.released(K="4.2", HB="13.0")

        await self._amend(world, order, "K", "5.9")

        assert world.notices() == ["lab.results_released", "lab.result_amended"]
        assert world.notifier.notify.await_args.args[0].variables == {
            "patient_name": "Ananya Rao",
            "test_name": "Potassium",
            "previous_result": "4.2 mmol/L",
            "result": "5.9 mmol/L",
            "reason": "Transcription error",
        }
        event = world.audit.last()
        assert event.action == "lab.order.result_amended"
        assert event.changes == {"K": {"before": "4.2", "after": "5.9"}}

    async def test_a_second_amendment_is_appended(self) -> None:
        world = _World()
        order = await world.released(K="4.2", HB="13.0")
        await self._amend(world, order, "K", "5.9")

        amended = await self._amend(world, order, "K", "4.9")

        assert [a.new_value for a in _item(amended, "K").amendments] == ["5.9", "4.9"]

    async def test_before_release_there_is_nothing_to_amend(self) -> None:
        world = _World()
        order = await world.collected()
        await world.enter(order, K="4.2", HB="13.0")

        with pytest.raises(LabOrderStateError, match="results entered"):
            await self._amend(world, order, "K", "5.9")

    async def test_an_unchanged_value_is_a_422(self) -> None:
        world = _World()
        order = await world.released(K="4.2", HB="13.0")

        with pytest.raises(ValidationError, match="same as the current"):
            await self._amend(world, order, "K", "4.2")

    async def test_a_numeric_test_rejects_text(self) -> None:
        world = _World()
        order = await world.released(K="4.2", HB="13.0")

        with pytest.raises(ValidationError, match="numeric"):
            await self._amend(world, order, "K", "normal")

    async def test_an_item_from_another_order_is_a_404(self) -> None:
        world = _World()
        order = await world.released()

        with pytest.raises(LabOrderItemNotFoundError):
            await world.service.amend_result(
                HOSPITAL_ID,
                order.id,
                uuid.uuid4(),
                AmendResultRequest(new_value="1", reason="x"),
                actor_id=ACTOR_ID,
            )


class TestQueries:
    async def test_get_order(self) -> None:
        world = _World()
        order = await world.order()

        assert (await world.service.get_order(HOSPITAL_ID, order.id)).id == order.id

    async def test_get_order_in_another_hospital_is_a_404(self) -> None:
        world = _World()
        order = await world.order()

        with pytest.raises(LabOrderNotFoundError) as excinfo:
            await world.service.get_order(uuid.uuid4(), order.id)

        assert excinfo.value.status_code == 404

    async def test_list_orders_pages_and_filters(self) -> None:
        world = _World()
        await world.order()
        await world.collected()

        everything = await world.service.list_orders(
            HOSPITAL_ID, pagination=PaginationParams(page=1, page_size=1)
        )
        waiting = await world.service.list_orders(HOSPITAL_ID, status=LabOrderStatus.ORDERED)

        assert (len(everything.items), everything.total_records) == (1, 2)
        assert [o.status for o in waiting.items] == [LabOrderStatus.ORDERED]


# ── Catalog ─────────────────────────────────────────────────────────────────


def _catalog(*tests: LabTest) -> tuple[LabCatalogService, FakeTestRepository, Any, Any]:
    repo = FakeTestRepository(*tests)
    session = FakeSession()
    audit = RecordingAuditSink()
    return LabCatalogService(repo, session, audit), repo, session, audit  # type: ignore[arg-type]


def _new_test(**overrides: Any) -> CreateLabTestRequest:
    values: dict[str, Any] = {
        "code": "na",
        "name": "Sodium",
        "unit": "mmol/L",
        "reference_ranges": [{"low": "135", "high": "145"}],
        "price": "200.00",
    }
    values.update(overrides)
    return CreateLabTestRequest.model_validate(values)


class TestCatalog:
    async def test_create_stores_ranges_as_exact_strings_and_audits(self) -> None:
        service, repo, session, audit = _catalog()

        created = await service.create_test(HOSPITAL_ID, _new_test(), actor_id=ACTOR_ID)

        assert created.code == "NA"
        assert created.price == Decimal("200.00")
        stored = repo.tests[created.id]
        assert stored.reference_ranges[0]["low"] == "135"
        assert stored.reference_ranges[0]["sex"] == "any"
        assert session.commits == 1
        assert audit.last().action == "lab.test.created"
        assert audit.last().changes["price"] == {"before": None, "after": "200.00"}

    async def test_a_duplicate_code_is_a_409(self) -> None:
        service, _, session, _ = _catalog(_test("NA"))

        with pytest.raises(DuplicateLabTestCodeError):
            await service.create_test(HOSPITAL_ID, _new_test())

        assert session.commits == 0

    async def test_a_duplicate_caught_only_by_the_database_is_still_a_409(self) -> None:
        service, repo, _, _ = _catalog()
        repo.raise_on_create = IntegrityError(
            "INSERT", {}, Exception('violates "uq_tests_catalog_hospital_code"')
        )

        with pytest.raises(DuplicateLabTestCodeError):
            await service.create_test(HOSPITAL_ID, _new_test())

    async def test_an_unrelated_integrity_error_is_not_swallowed(self) -> None:
        service, repo, _, _ = _catalog()
        repo.raise_on_create = IntegrityError("INSERT", {}, Exception("something else"))

        with pytest.raises(IntegrityError):
            await service.create_test(HOSPITAL_ID, _new_test())

    async def test_update_applies_only_what_changed(self) -> None:
        test = _test("NA", price=Decimal("200.00"))
        service, _, session, audit = _catalog(test)

        updated = await service.update_test(
            HOSPITAL_ID,
            test.id,
            UpdateLabTestRequest.model_validate({"price": "220.00", "is_active": True}),
            actor_id=ACTOR_ID,
        )

        assert updated.price == Decimal("220.00")
        assert audit.last().changes == {"price": {"before": "200.00", "after": "220.00"}}
        assert session.commits == 1

    async def test_a_no_op_update_writes_and_audits_nothing(self) -> None:
        test = _test("NA")
        service, _, session, audit = _catalog(test)

        await service.update_test(
            HOSPITAL_ID, test.id, UpdateLabTestRequest.model_validate({"is_active": True})
        )

        assert session.commits == 0
        assert audit.events == []

    async def test_a_numeric_test_cannot_lose_its_last_range(self) -> None:
        test = _test("NA")
        service, _, _, _ = _catalog(test)

        with pytest.raises(ValidationError) as excinfo:
            await service.update_test(
                HOSPITAL_ID, test.id, UpdateLabTestRequest.model_validate({"reference_ranges": []})
            )

        assert excinfo.value.detail["errors"][0]["field"] == "reference_ranges"

    async def test_a_text_test_cannot_gain_a_range(self) -> None:
        test = _test("URINE", result_type=LabResultType.TEXT, reference_ranges=[])
        service, _, _, _ = _catalog(test)

        with pytest.raises(ValidationError, match="cannot have reference ranges"):
            await service.update_test(
                HOSPITAL_ID,
                test.id,
                UpdateLabTestRequest.model_validate(
                    {"reference_ranges": [{"low": "1", "high": "2"}]}
                ),
            )

    async def test_ranges_can_be_replaced(self) -> None:
        test = _test("NA")
        service, _, _, _ = _catalog(test)

        updated = await service.update_test(
            HOSPITAL_ID,
            test.id,
            UpdateLabTestRequest.model_validate(
                {"reference_ranges": [{"low": "136", "high": "146"}]}
            ),
        )

        assert updated.reference_ranges[0].low == Decimal(136)

    async def test_get_and_update_in_another_hospital_are_404s(self) -> None:
        test = _test("NA")
        service, _, _, _ = _catalog(test)

        with pytest.raises(LabTestNotFoundError):
            await service.get_test(uuid.uuid4(), test.id)
        with pytest.raises(LabTestNotFoundError):
            await service.update_test(
                uuid.uuid4(), test.id, UpdateLabTestRequest.model_validate({"name": "X"})
            )

    async def test_get_and_list(self) -> None:
        test = _test("NA")
        service, _, _, _ = _catalog(test, _test("K"))

        assert (await service.get_test(HOSPITAL_ID, test.id)).code == "NA"
        page = await service.list_tests(
            HOSPITAL_ID, pagination=PaginationParams(page=1, page_size=1)
        )
        assert (len(page.items), page.total_records) == (1, 2)
