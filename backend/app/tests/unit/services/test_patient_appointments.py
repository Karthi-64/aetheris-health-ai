"""Unit tests for :class:`PatientAppointmentsService` — every collaborator mocked.

What is under test: that ownership is the account's own record pairs and
nothing else, what the cancel precondition decides on the locked row, how the
lifecycle's refusals become the patient's, and what a patient is shown.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.core.feature_flags import PATIENT_APP_ENABLED
from app.core.tenancy import CrossTenant, current_tenant_scope
from app.models.appointment import AppointmentStatus, AppointmentType
from app.repositories.hospital_repository import HospitalDirectoryEntry
from app.schemas.patient_app.appointments import CancelAppointment
from app.services.appointment_service import AppointmentNotFoundError, InvalidTransitionError
from app.services.patient_app import appointments_service
from app.services.patient_app.appointments_service import PatientAppointmentsService
from app.services.patient_app.booking_policy import BookingPolicy
from app.services.patient_app.errors import ConsentRequiredError
from app.services.patient_app.patient_authorization import PatientContext

NOW = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)  # 09:00 in Kolkata
START = NOW + timedelta(hours=5)
CANCEL: Any = CancelAppointment.model_validate(
    {"reason_code": "feeling_better", "reason_text": "  all fine "}
)


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appointments_service, "utc_now", lambda: NOW)


class _Mine:
    def __init__(self, *, settings: dict[str, Any] | None = None, hospitals: int = 1) -> None:
        self.account: Any = SimpleNamespace(id=uuid.uuid4())
        self.entries = [
            HospitalDirectoryEntry(
                id=uuid.uuid4(),
                name=f"Hospital {n}",
                slug=f"hospital-{n}",
                settings={PATIENT_APP_ENABLED: True, **(settings or {})},
                address={},
                phone=None,
                logo_url=None,
                timezone="Asia/Kolkata",
            )
            for n in range(hospitals)
        ]
        self.contexts = [
            PatientContext(account_id=self.account.id, hospital_id=e.id, patient_id=uuid.uuid4())
            for e in self.entries
        ]
        self.authorization = AsyncMock()
        self.authorization.own_contexts.return_value = self.contexts
        self.gate = AsyncMock()
        self.gate.describe.side_effect = lambda ref: next(
            (e for e in self.entries if str(e.id) == ref), None
        )
        self.appointments = AsyncMock()
        self.lifecycle = AsyncMock()
        self.scopes: list[Any] = []
        self.service = PatientAppointmentsService(
            self.authorization, self.gate, self.appointments, self.lifecycle
        )

    @property
    def pairs(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        return [(c.hospital_id, c.patient_id) for c in self.contexts]

    def row(self, index: int = 0, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "hospital_id": self.contexts[index].hospital_id,
            "patient_id": self.contexts[index].patient_id,
            "status": AppointmentStatus.BOOKED,
            "type": AppointmentType.NEW,
            "scheduled_start": START,
            "scheduled_end": START + timedelta(minutes=15),
            "reason": None,
            "doctor": SimpleNamespace(
                id=uuid.uuid4(),
                specialization=" Cardiology ",
                user=SimpleNamespace(first_name=" Asha", last_name="Menon "),
            ),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def lifecycle_runs_precondition_on(self, locked: Any) -> None:
        async def _cancel(
            hospital_id: uuid.UUID, appointment_id: uuid.UUID, request: Any, **kw: Any
        ) -> None:
            self.scopes.append(current_tenant_scope())
            kw["precondition"](locked)
            locked.status = AppointmentStatus.CANCELLED

        self.lifecycle.cancel.side_effect = _cancel


class TestOwnership:
    async def test_the_list_is_asked_for_the_accounts_own_record_pairs_only(self) -> None:
        mine = _Mine(hospitals=2)
        rows = [mine.row(1), mine.row(0)]
        mine.appointments.list_for_patient_records.return_value = (rows, 7)

        page = await mine.service.list_appointments(
            mine.account, upcoming=False, page=3, page_size=2
        )

        call = mine.appointments.list_for_patient_records.await_args
        assert call.args[0] == mine.pairs
        assert isinstance(call.args[1], CrossTenant)
        assert call.kwargs == {"upcoming": False, "now": NOW, "skip": 4, "limit": 2}
        assert (page.total_records, [i.ref for i in page.items]) == (7, [str(r.id) for r in rows])
        assert [i.hospital.ref for i in page.items] == ["hospital-1", "hospital-0"]

    async def test_a_hospital_the_gate_no_longer_describes_contributes_no_pair(self) -> None:
        mine = _Mine(hospitals=2)
        closed = mine.entries.pop(1)
        mine.appointments.list_for_patient_records.return_value = ([], 0)

        await mine.service.list_appointments(mine.account, upcoming=True, page=1, page_size=20)

        pairs = mine.appointments.list_for_patient_records.await_args.args[0]
        assert [hospital for hospital, _ in pairs] == [mine.entries[0].id]
        assert closed.id not in {hospital for hospital, _ in pairs}

    async def test_a_pending_policy_is_refused_before_anything_is_read(self) -> None:
        mine = _Mine()
        mine.authorization.own_contexts.side_effect = ConsentRequiredError

        for call in (
            mine.service.list_appointments(mine.account, upcoming=True, page=1, page_size=20),
            mine.service.get_appointment(mine.account, str(uuid.uuid4())),
            mine.service.cancel_appointment(mine.account, str(uuid.uuid4()), CANCEL),
        ):
            with pytest.raises(ConsentRequiredError):
                await call
        assert mine.appointments.mock_calls == [] and mine.lifecycle.mock_calls == []

    @pytest.mark.parametrize("reference", ["", "x", "' OR 1=1", "a" * 4000, uuid.uuid4().hex])
    async def test_a_reference_that_is_not_one_reads_nothing(self, reference: str) -> None:
        mine = _Mine()

        for call in (
            mine.service.get_appointment(mine.account, reference or " "),
            mine.service.cancel_appointment(mine.account, reference or " ", CANCEL),
        ):
            with pytest.raises(NotFoundError) as refusal:
                await call
            assert refusal.value.message == "Not Found"
        assert mine.appointments.mock_calls == [] and mine.lifecycle.mock_calls == []

    async def test_an_appointment_outside_the_pairs_is_not_found_and_never_cancelled(self) -> None:
        mine = _Mine()
        mine.appointments.get_for_patient_records.return_value = None
        ref = uuid.uuid4()

        with pytest.raises(NotFoundError):
            await mine.service.get_appointment(mine.account, str(ref))
        with pytest.raises(NotFoundError):
            await mine.service.cancel_appointment(mine.account, str(ref), CANCEL)

        call = mine.appointments.get_for_patient_records.await_args
        assert (call.args[0], call.args[2]) == (mine.pairs, ref)
        assert mine.lifecycle.mock_calls == []


class TestWhatIsShown:
    async def test_the_patient_shape_and_the_servers_cancel_decision(self) -> None:
        mine = _Mine()
        row = mine.row(reason="Cough")
        mine.appointments.get_for_patient_records.return_value = row

        shown = (
            await mine.service.get_appointment(mine.account, f" {str(row.id).upper()} ")
        ).model_dump()

        assert set(shown) == {
            "ref", "status", "type", "start", "end", "timezone", "hospital", "doctor",
            "reason", "can_cancel", "cancel_until",
        }  # fmt: skip
        assert shown["doctor"] == {
            "ref": str(row.doctor.id),
            "name": "Asha Menon",
            "specialization": "Cardiology",
        }
        assert shown["start"].utcoffset() == timedelta(hours=5, minutes=30)
        assert (shown["can_cancel"], shown["cancel_until"]) == (
            True,
            START - timedelta(minutes=120),
        )

    @pytest.mark.parametrize(
        ("status", "minutes_before_start", "cutoff", "can", "until"),
        [
            (AppointmentStatus.BOOKED, 121, 120, True, True),
            (AppointmentStatus.BOOKED, 120, 120, True, True),
            (AppointmentStatus.BOOKED, 119, 120, False, True),
            (AppointmentStatus.BOOKED, 1, 0, True, True),
            (AppointmentStatus.BOOKED, -1, 0, False, True),
            (AppointmentStatus.CHECKED_IN, 600, 120, False, False),
            (AppointmentStatus.IN_PROGRESS, 600, 120, False, False),
            (AppointmentStatus.COMPLETED, 600, 120, False, False),
            (AppointmentStatus.CANCELLED, 600, 120, False, False),
            (AppointmentStatus.NO_SHOW, 600, 120, False, False),
        ],
    )
    async def test_can_cancel_is_booked_and_before_the_hospitals_cut_off(
        self,
        status: AppointmentStatus,
        minutes_before_start: int,
        cutoff: int,
        can: bool,
        until: bool,
    ) -> None:
        mine = _Mine(settings={"patient_app.cancel_cutoff_minutes": cutoff})
        start = NOW + timedelta(minutes=minutes_before_start)
        mine.appointments.get_for_patient_records.return_value = mine.row(
            status=status, scheduled_start=start, scheduled_end=start + timedelta(minutes=15)
        )

        shown = await mine.service.get_appointment(mine.account, str(uuid.uuid4()))

        assert shown.can_cancel is can
        assert (shown.cancel_until is not None) is until


class TestCancel:
    async def test_it_is_the_hospitals_own_cancel_with_a_patient_precondition_and_audit(
        self,
    ) -> None:
        mine = _Mine()
        row = mine.row()
        mine.appointments.get_for_patient_records.return_value = row
        mine.lifecycle_runs_precondition_on(row)

        shown = await mine.service.cancel_appointment(mine.account, str(row.id), CANCEL)

        call = mine.lifecycle.cancel.await_args
        assert call.args[:2] == (row.hospital_id, row.id)
        assert (
            call.args[2].reason == "Cancelled by the patient in the app (feeling_better): all fine"
        )
        assert call.kwargs["actor_id"] is None
        assert [scope.hospital_id for scope in mine.scopes] == [row.hospital_id]
        assert current_tenant_scope() is None
        assert (shown.status, shown.can_cancel, shown.cancel_until) == (
            AppointmentStatus.CANCELLED,
            False,
            None,
        )
        event = call.kwargs["audit_event"](row, AppointmentStatus.BOOKED)
        assert event.action == "patient.appointment.cancelled"
        assert (event.actor_type, event.patient_account_id, event.actor_id) == (
            "patient",
            mine.account.id,
            None,
        )
        assert (event.hospital_id, event.target_id) == (row.hospital_id, row.id)
        assert event.context == {"reason_code": "feeling_better", "from_status": "booked"}

    @pytest.mark.parametrize(
        ("change", "error", "message"),
        [
            (
                {"status": AppointmentStatus.CHECKED_IN},
                ConflictError,
                "This appointment can no longer be cancelled.",
            ),
            (
                {"status": AppointmentStatus.IN_PROGRESS},
                ConflictError,
                "This appointment can no longer be cancelled.",
            ),
            (
                {"status": AppointmentStatus.COMPLETED},
                ConflictError,
                "This appointment can no longer be cancelled.",
            ),
            (
                {"status": AppointmentStatus.NO_SHOW},
                ConflictError,
                "This appointment can no longer be cancelled.",
            ),
            (
                {"scheduled_start": NOW + timedelta(minutes=119)},
                BusinessRuleError,
                "It is too late",
            ),
            ({"scheduled_start": NOW - timedelta(minutes=1)}, BusinessRuleError, "It is too late"),
            ({"patient_id": uuid.uuid4()}, NotFoundError, "Not Found"),
        ],
    )
    async def test_the_precondition_decides_on_the_locked_row_not_the_one_read_before(
        self, change: dict[str, Any], error: type[Exception], message: str
    ) -> None:
        """What was read a moment ago was cancellable; what the lock returns is not."""
        mine = _Mine()
        read = mine.row()
        locked = mine.row(**{"id": read.id, **change})
        mine.appointments.get_for_patient_records.return_value = read
        mine.lifecycle_runs_precondition_on(locked)

        with pytest.raises(error) as refusal:
            await mine.service.cancel_appointment(mine.account, str(read.id), CANCEL)

        assert refusal.value.message.startswith(message)  # type: ignore[attr-defined]
        assert not refusal.value.detail  # type: ignore[attr-defined]
        assert locked.status is not AppointmentStatus.CANCELLED

    async def test_an_appointment_already_cancelled_is_answered_as_it_is(self) -> None:
        mine = _Mine()
        row = mine.row(status=AppointmentStatus.CANCELLED)
        mine.appointments.get_for_patient_records.return_value = row

        async def _cancel(*args: Any, **kw: Any) -> None:
            kw["precondition"](row)
            raise AssertionError("the precondition must have stopped it")

        mine.lifecycle.cancel.side_effect = _cancel

        shown = await mine.service.cancel_appointment(mine.account, str(row.id), CANCEL)

        assert (shown.status, shown.can_cancel) == (AppointmentStatus.CANCELLED, False)

    @pytest.mark.parametrize(
        ("raised", "error"),
        [
            (
                InvalidTransitionError(AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED),
                ConflictError,
            ),
            (AppointmentNotFoundError(uuid.uuid4()), NotFoundError),
        ],
    )
    async def test_the_lifecycles_own_refusals_become_the_patients(
        self, raised: Exception, error: type[Exception]
    ) -> None:
        mine = _Mine()
        mine.appointments.get_for_patient_records.return_value = mine.row()
        mine.lifecycle.cancel.side_effect = raised

        with pytest.raises(error) as refusal:
            await mine.service.cancel_appointment(mine.account, str(uuid.uuid4()), CANCEL)

        assert not refusal.value.detail  # type: ignore[attr-defined]
        assert "allowed_transitions" not in str(refusal.value.__dict__)


class TestPolicyAndBody:
    def test_the_cut_off_is_read_like_the_rest_of_the_policy(self) -> None:
        key = "patient_app.cancel_cutoff_minutes"
        assert BookingPolicy().cancel_cutoff_minutes == 120
        assert BookingPolicy.from_settings({key: 0}).cancel_cutoff_minutes == 0
        assert BookingPolicy.from_settings({key: 10080}).cancel_cutoff_minutes == 10080
        for bad in (-1, 10081, "60", 60.0, True, None):
            assert BookingPolicy.from_settings({key: bad}).cancel_cutoff_minutes == 120

    @pytest.mark.parametrize(
        "body",
        [{}, {"reason_code": "because"}, {"reason_code": "other", "reason_text": "t" * 201},
         {"reason_code": "other", "patient_id": "x"}, {"reason_code": "other", "reason_text": "a\x00"}],
    )  # fmt: skip
    def test_what_the_cancel_body_refuses(self, body: dict[str, Any]) -> None:
        with pytest.raises(Exception, match="validation error"):
            CancelAppointment.model_validate(body)

    def test_blank_text_is_absent(self) -> None:
        assert (
            CancelAppointment.model_validate(
                {"reason_code": "other", "reason_text": "   "}
            ).reason_text
            is None
        )
