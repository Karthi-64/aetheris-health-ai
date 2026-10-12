"""Unit tests for booking, idempotency, and the no-show sweeper.

Repositories are mocked; no database. The overlap *constraint* is tested
against real Postgres in the repository suite — here we test that the service
checks first, surfaces a useful 409, and never writes when validation fails.

Module spec §16 calls out idempotency and no-overlap logic specifically.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.ai.errors import (
    AIError,
    AIModelUnavailableError,
    AINotConfiguredError,
    AIProviderAuthError,
    AIProviderRateLimitedError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    AIResponseInvalidError,
)
from app.core.exceptions import FeatureDisabledError, ValidationError
from app.models.appointment import AppointmentStatus, AppointmentType
from app.schemas.appointment import SlotRecommendationRequest, SlotRecommendationStatus
from app.services.appointment_service import (
    DEFAULT_NO_SHOW_GRACE_MINUTES,
    AppointmentService,
    DaySlot,
    DoubleBookingError,
    NullInvoiceDraftSink,
    OutsideAvailabilityError,
    SlotChoice,
)
from app.tests.ai_fakes import RecordingLogger, install_loggers
from app.tests.conftest import FakeSession, RecordingAuditSink
from app.tests.factories import build_appointment_model, build_book_request, future_window

HOSPITAL_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()
KEY = "idem-key-0001"


def _attach(appointment: Any) -> Any:
    """Give a detached Appointment the relationships the DTO reads."""
    patient = AsyncMock()
    patient.full_name = "Ananya Rao"
    doctor = AsyncMock()
    doctor.user.first_name = "Asha"
    doctor.user.last_name = "Menon"
    appointment.patient = patient
    appointment.doctor = doctor
    return appointment


def _hospital(settings: dict[str, Any] | None = None) -> AsyncMock:
    """A hospital double with a timezone and settings blob."""
    hospital = AsyncMock()
    hospital.timezone = "UTC"
    hospital.settings = settings if settings is not None else {}
    return hospital


def _make_service(
    repo: AsyncMock,
    *,
    patients: AsyncMock | None = None,
    doctors: AsyncMock | None = None,
    hospitals: AsyncMock | None = None,
    slot_ranker: Any = None,
) -> tuple[AppointmentService, FakeSession, RecordingAuditSink]:
    """Assemble a service over mocked collaborators, defaulting to a valid world."""
    session = FakeSession()
    audit = RecordingAuditSink()

    if patients is None:
        patients = AsyncMock()
        patients.get_patient_by_id.return_value = AsyncMock()
    if doctors is None:
        doctors = AsyncMock()
        doctors.get_doctor_by_id.return_value = AsyncMock()
        doctors.get_availability.return_value = []
        doctors.list_leaves.return_value = []
    if hospitals is None:
        hospitals = AsyncMock()
        hospitals.get_by_id.return_value = _hospital()

    service = AppointmentService(
        repo,
        patients,
        doctors,
        hospitals,
        session,  # type: ignore[arg-type]
        audit,
        NullInvoiceDraftSink(),
        slot_ranker,
    )
    return service, session, audit


@pytest.fixture
def repo() -> AsyncMock:
    """A mocked appointment repository with a clear calendar."""
    mock = AsyncMock()
    mock.get_by_idempotency_key.return_value = None
    mock.find_overlapping.return_value = []
    return mock


class TestIdempotency:
    """Business rule 8 and FR-8."""

    async def test_first_booking_creates(self, repo: AsyncMock) -> None:
        created = _attach(build_appointment_model(hospital_id=HOSPITAL_ID))
        repo.create_appointment.return_value = created
        service, session, audit = _make_service(repo)

        _, was_created = await service.book_appointment(
            HOSPITAL_ID,
            build_book_request(),
            idempotency_key=KEY,
            actor_id=ACTOR_ID,
            allow_override=True,
        )

        assert was_created is True
        assert session.commits == 1
        assert audit.actions() == ["appointment.booked"]

    async def test_replay_returns_the_original_without_writing(self, repo: AsyncMock) -> None:
        """A retry must return the first booking, not make a second."""
        existing = _attach(build_appointment_model(hospital_id=HOSPITAL_ID))
        repo.get_by_idempotency_key.return_value = existing
        service, session, audit = _make_service(repo)

        result, was_created = await service.book_appointment(
            HOSPITAL_ID,
            build_book_request(),
            idempotency_key=KEY,
            actor_id=ACTOR_ID,
            allow_override=True,
        )

        assert was_created is False
        assert result.id == existing.id
        repo.create_appointment.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_key_is_stored_on_the_appointment(self, repo: AsyncMock) -> None:
        repo.create_appointment.return_value = _attach(
            build_appointment_model(hospital_id=HOSPITAL_ID)
        )
        service, _, _ = _make_service(repo)

        await service.book_appointment(
            HOSPITAL_ID, build_book_request(), idempotency_key=KEY, allow_override=True
        )

        assert repo.create_appointment.await_args.kwargs["idempotency_key"] == KEY


class TestBookingValidation:
    """Business rules 1-4."""

    async def test_unknown_patient_is_rejected(self, repo: AsyncMock) -> None:
        patients = AsyncMock()
        patients.get_patient_by_id.return_value = None
        service, session, _ = _make_service(repo, patients=patients)

        with pytest.raises(ValidationError) as exc:
            await service.book_appointment(
                HOSPITAL_ID, build_book_request(), idempotency_key=KEY, allow_override=True
            )

        assert exc.value.detail["errors"][0]["field"] == "patient_id"
        repo.create_appointment.assert_not_awaited()
        assert session.commits == 0

    async def test_unknown_doctor_is_rejected(self, repo: AsyncMock) -> None:
        doctors = AsyncMock()
        doctors.get_doctor_by_id.return_value = None
        service, session, _ = _make_service(repo, doctors=doctors)

        with pytest.raises(ValidationError) as exc:
            await service.book_appointment(
                HOSPITAL_ID, build_book_request(), idempotency_key=KEY, allow_override=True
            )

        assert exc.value.detail["errors"][0]["field"] == "doctor_id"
        assert session.commits == 0

    async def test_booking_in_the_past_is_rejected(self, repo: AsyncMock) -> None:
        service, session, _ = _make_service(repo)
        past = datetime.now(UTC) - timedelta(days=1)

        with pytest.raises(ValidationError):
            await service.book_appointment(
                HOSPITAL_ID,
                build_book_request(
                    scheduled_start=past.isoformat(),
                    scheduled_end=(past + timedelta(minutes=15)).isoformat(),
                ),
                idempotency_key=KEY,
                allow_override=True,
            )

        assert session.commits == 0

    async def test_walk_in_gets_a_backdate_grace(self, repo: AsyncMock) -> None:
        """Module spec §11: a walk-in is recorded once the patient has arrived."""
        repo.create_appointment.return_value = _attach(
            build_appointment_model(hospital_id=HOSPITAL_ID)
        )
        service, session, _ = _make_service(repo)
        just_past = datetime.now(UTC) - timedelta(minutes=5)

        await service.book_appointment(
            HOSPITAL_ID,
            build_book_request(
                type="walk_in",
                scheduled_start=just_past.isoformat(),
                scheduled_end=(just_past + timedelta(minutes=15)).isoformat(),
            ),
            idempotency_key=KEY,
            allow_override=True,
        )

        assert session.commits == 1

    async def test_overlap_is_rejected_with_the_clash_attached(self, repo: AsyncMock) -> None:
        """FR-2. The 409 carries what clashed so reception can re-pick."""
        clash = build_appointment_model(hospital_id=HOSPITAL_ID)
        repo.find_overlapping.return_value = [clash]
        service, session, audit = _make_service(repo)

        with pytest.raises(DoubleBookingError) as exc:
            await service.book_appointment(
                HOSPITAL_ID, build_book_request(), idempotency_key=KEY, allow_override=True
            )

        assert exc.value.status_code == 409
        assert exc.value.detail["conflicting_appointments"][0]["appointment_id"] == str(clash.id)
        repo.create_appointment.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_outside_availability_is_rejected_without_override(self, repo: AsyncMock) -> None:
        """Business rule 4."""
        doctors = AsyncMock()
        doctors.get_doctor_by_id.return_value = AsyncMock()
        doctors.get_availability.return_value = []  # no published windows
        service, session, _ = _make_service(repo, doctors=doctors)

        with pytest.raises(OutsideAvailabilityError):
            await service.book_appointment(
                HOSPITAL_ID, build_book_request(), idempotency_key=KEY, allow_override=False
            )

        assert session.commits == 0

    async def test_override_permits_booking_outside_availability(self, repo: AsyncMock) -> None:
        repo.create_appointment.return_value = _attach(
            build_appointment_model(hospital_id=HOSPITAL_ID)
        )
        doctors = AsyncMock()
        doctors.get_doctor_by_id.return_value = AsyncMock()
        doctors.get_availability.return_value = []
        service, session, audit = _make_service(repo, doctors=doctors)

        await service.book_appointment(
            HOSPITAL_ID, build_book_request(), idempotency_key=KEY, allow_override=True
        )

        assert session.commits == 1
        assert audit.last().context["override_used"] is True

    async def test_booking_inside_availability_passes_without_override(
        self, repo: AsyncMock
    ) -> None:
        """The happy path for rule 4: a published window contains the booking."""
        start, end = future_window()
        window = AsyncMock()
        window.day_of_week = start.weekday()
        window.start_time = start.time()
        window.end_time = end.time()

        doctors = AsyncMock()
        doctors.get_doctor_by_id.return_value = AsyncMock()
        doctors.get_availability.return_value = [window]

        repo.create_appointment.return_value = _attach(
            build_appointment_model(hospital_id=HOSPITAL_ID)
        )
        service, session, _ = _make_service(repo, doctors=doctors)

        await service.book_appointment(
            HOSPITAL_ID, build_book_request(), idempotency_key=KEY, allow_override=False
        )

        assert session.commits == 1


class TestNoShowSweeper:
    """Module spec §5.7, FR-7, AC-5."""

    async def test_sweeps_an_overdue_appointment(self, repo: AsyncMock) -> None:
        now = datetime(2030, 1, 7, 12, 0, tzinfo=UTC)
        overdue = build_appointment_model(
            hospital_id=HOSPITAL_ID,
            scheduled_end=now - timedelta(minutes=45),
            status=AppointmentStatus.BOOKED,
        )
        repo.find_no_show_candidates.return_value = [overdue]
        service, session, audit = _make_service(repo)

        swept = await service.sweep_no_shows(now=now)

        assert swept == 1
        assert repo.update_appointment.await_args.kwargs["status"] is AppointmentStatus.NO_SHOW
        assert session.commits == 1
        assert audit.actions() == ["appointment.no_show"]

    async def test_sweeper_records_a_null_actor(self, repo: AsyncMock) -> None:
        """The system has no acting user — that is why changed_by is nullable."""
        now = datetime(2030, 1, 7, 12, 0, tzinfo=UTC)
        repo.find_no_show_candidates.return_value = [
            build_appointment_model(
                hospital_id=HOSPITAL_ID, scheduled_end=now - timedelta(minutes=45)
            )
        ]
        service, _, audit = _make_service(repo)

        await service.sweep_no_shows(now=now)

        assert repo.record_transition.await_args.kwargs["changed_by"] is None
        assert repo.record_transition.await_args.kwargs["reason"] == "no_show_sweeper"
        assert audit.last().actor_id is None

    async def test_inside_the_grace_window_is_left_alone(self, repo: AsyncMock) -> None:
        """AC-5 turns on the boundary, so it is asserted rather than approximated."""
        now = datetime(2030, 1, 7, 12, 0, tzinfo=UTC)
        just_inside = build_appointment_model(
            hospital_id=HOSPITAL_ID,
            scheduled_end=now - timedelta(minutes=DEFAULT_NO_SHOW_GRACE_MINUTES - 1),
        )
        repo.find_no_show_candidates.return_value = [just_inside]
        service, session, _ = _make_service(repo)

        assert await service.sweep_no_shows(now=now) == 0
        repo.update_appointment.assert_not_awaited()
        assert session.commits == 0

    async def test_per_hospital_grace_override_is_honoured(self, repo: AsyncMock) -> None:
        """§5.7: grace is a per-hospital setting, read from hospitals.settings."""
        now = datetime(2030, 1, 7, 12, 0, tzinfo=UTC)
        # 45 minutes overdue: swept under the 30-minute default, but this
        # hospital allows 90.
        appointment = build_appointment_model(
            hospital_id=HOSPITAL_ID, scheduled_end=now - timedelta(minutes=45)
        )
        repo.find_no_show_candidates.return_value = [appointment]
        hospitals = AsyncMock()
        hospitals.get_by_id.return_value = _hospital({"no_show_grace_minutes": 90})
        service, session, _ = _make_service(repo, hospitals=hospitals)

        assert await service.sweep_no_shows(now=now) == 0
        assert session.commits == 0

    async def test_empty_sweep_does_not_commit(self, repo: AsyncMock) -> None:
        repo.find_no_show_candidates.return_value = []
        service, session, audit = _make_service(repo)

        assert await service.sweep_no_shows(now=datetime.now(UTC)) == 0
        assert session.commits == 0
        assert audit.events == []


class TestWalkInQueue:
    """Module spec §5.8."""

    async def test_queue_delegates_to_the_repository(self, repo: AsyncMock) -> None:
        repo.list_walk_in_queue.return_value = [
            _attach(build_appointment_model(hospital_id=HOSPITAL_ID, type=AppointmentType.WALK_IN))
        ]
        service, _, _ = _make_service(repo)

        queue = await service.get_walk_in_queue(HOSPITAL_ID)

        assert len(queue) == 1
        assert queue[0].type is AppointmentType.WALK_IN


#: The recommendation tests use a fixed far-future Monday and a pinned clock,
#: so none of them depends on the hour or the day they are run.
REC_DATE = date(2030, 1, 7)
REC_ZONE = "Asia/Kolkata"
REC_NOW = datetime(2030, 1, 1, tzinfo=UTC)
FLAG = "feature.ai.slot_recommendation"
DOCTOR_ID = uuid.uuid4()
PATIENT_ID = uuid.uuid4()


def _window(start: time, end: time, minutes: int = 30, day_of_week: int = 0) -> SimpleNamespace:
    """An availability row double."""
    return SimpleNamespace(
        day_of_week=day_of_week, start_time=start, end_time=end, slot_duration_minutes=minutes
    )


#: The seeded clinic week's Monday: 09:00-13:00 and 14:00-17:00 in 30-minute slots.
CLINIC_DAY = [_window(time(9, 0), time(13, 0)), _window(time(14, 0), time(17, 0))]


def _local(hour: int, minute: int = 0) -> datetime:
    """A wall-clock time on the recommendation date, in the hospital's zone."""
    return datetime(2030, 1, 7, hour, minute, tzinfo=ZoneInfo(REC_ZONE))


def _booking(start: datetime, minutes: int) -> SimpleNamespace:
    """An appointment row double, stored in UTC like the real ones."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        scheduled_start=start.astimezone(UTC),
        scheduled_end=(start + timedelta(minutes=minutes)).astimezone(UTC),
    )


def _leave(start: datetime, end: datetime) -> SimpleNamespace:
    """A leave row double."""
    return SimpleNamespace(starts_at=start.astimezone(UTC), ends_at=end.astimezone(UTC))


def _choice(slot_id: str, reason: str | None = "Keeps the morning compact.") -> SlotChoice:
    return SlotChoice(slot_id=slot_id, reason=reason, provider="groq", model="returned-model")


def _request(on: date = REC_DATE) -> SlotRecommendationRequest:
    return SlotRecommendationRequest(patient_id=PATIENT_ID, doctor_id=DOCTOR_ID, date=on)


class _World:
    """A recommendation scenario over mocked collaborators."""

    def __init__(
        self,
        repo: AsyncMock,
        *,
        settings: dict[str, Any] | None = None,
        windows: list[SimpleNamespace] | None = None,
        leaves: list[SimpleNamespace] | None = None,
        booked: list[SimpleNamespace] | None = None,
        ranker: Any = "default",
        timezone: str = REC_ZONE,
    ) -> None:
        self.repo = repo
        repo.booked_intervals_for_doctor.return_value = booked or []

        hospital = _hospital({FLAG: True} if settings is None else settings)
        hospital.timezone = timezone
        self.hospitals = AsyncMock()
        self.hospitals.get_by_id.return_value = hospital

        self.patients = AsyncMock()
        self.patients.get_patient_by_id.return_value = AsyncMock()

        self.doctors = AsyncMock()
        self.doctors.get_doctor_by_id.return_value = AsyncMock()
        self.doctors.get_availability.return_value = CLINIC_DAY if windows is None else windows
        self.doctors.list_leaves.return_value = leaves or []

        if ranker == "default":
            ranker = AsyncMock()
            ranker.choose_slot.return_value = _choice("S1")
        self.ranker = ranker

        self.service, self.session, self.audit = _make_service(
            repo,
            patients=self.patients,
            doctors=self.doctors,
            hospitals=self.hospitals,
            slot_ranker=ranker,
        )

    async def recommend(self, on: date = REC_DATE) -> Any:
        return await self.service.recommend_slots(
            HOSPITAL_ID, _request(on), actor_id=ACTOR_ID, request_id="req-7"
        )

    def day(self) -> list[DaySlot]:
        """The day the ranker was shown."""
        return list(self.ranker.choose_slot.await_args.kwargs["day"])

    def assert_nothing_written(self, *, commits: int) -> None:
        """A recommendation reads. It never books, transitions or audits."""
        assert self.audit.events == []
        self.repo.create_appointment.assert_not_awaited()
        self.repo.update_appointment.assert_not_awaited()
        self.repo.record_transition.assert_not_awaited()
        assert self.session.commits == commits
        assert self.session.savepoints_opened == 0


class TestSlotRecommendation:
    """Module spec §5.9 — one advisory suggestion, every outcome explicit."""

    @pytest.fixture(autouse=True)
    def _pinned_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin "now" well before the recommendation date."""
        monkeypatch.setattr("app.services.appointment_service._utc_now", lambda: REC_NOW)

    @pytest.fixture
    def log(self, monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
        return install_loggers(monkeypatch, ["app.services.appointment_service"])

    # ── Gates ────────────────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "settings",
        [{}, {FLAG: False}, {FLAG: "true"}, {FLAG: 1}, {FLAG: "false"}, {"other": True}],
        ids=["absent", "false", "string-true", "one", "string-false", "other-key"],
    )
    async def test_flag_not_exactly_true_is_feature_disabled(
        self, repo: AsyncMock, log: RecordingLogger, settings: dict[str, Any]
    ) -> None:
        world = _World(repo, settings=settings)

        with pytest.raises(FeatureDisabledError) as caught:
            await world.recommend()

        exc = caught.value
        assert exc.status_code == 403
        assert exc.error_code == "FEATURE_DISABLED"
        assert exc.message == "AI slot suggestions are not enabled for this hospital."
        assert exc.detail == {"feature": FLAG}
        world.ranker.choose_slot.assert_not_awaited()
        # The gate precedes every lookup: nothing about the ids is revealed.
        world.patients.get_patient_by_id.assert_not_awaited()
        world.doctors.get_doctor_by_id.assert_not_awaited()
        world.assert_nothing_written(commits=0)
        assert log.events("appointment.recommend_slot_disabled")[0]["reason"] == (
            "feature_flag_off"
        )

    async def test_missing_hospital_is_feature_disabled(self, repo: AsyncMock) -> None:
        world = _World(repo)
        world.hospitals.get_by_id.return_value = None

        with pytest.raises(FeatureDisabledError):
            await world.recommend()

        world.ranker.choose_slot.assert_not_awaited()

    async def test_no_ranker_means_ai_not_configured(
        self, repo: AsyncMock, log: RecordingLogger
    ) -> None:
        """Flag on, but no AI on this server: a clear typed outcome, not an empty list."""
        world = _World(repo, ranker=None)

        with pytest.raises(AINotConfiguredError) as caught:
            await world.recommend()

        exc = caught.value
        assert exc.status_code == 503
        assert exc.error_code == "AI_NOT_CONFIGURED"
        assert exc.message == "AI suggestions are not configured on this server."
        world.assert_nothing_written(commits=0)
        assert log.events("appointment.recommend_slot_unavailable")[0]["reason"] == (
            "ai_not_configured"
        )

    async def test_flag_is_checked_before_configuration(self, repo: AsyncMock) -> None:
        """A hospital without the feature learns nothing about the deployment."""
        world = _World(repo, settings={}, ranker=None)

        with pytest.raises(FeatureDisabledError):
            await world.recommend()

    async def test_unknown_patient_is_rejected_before_any_model_call(self, repo: AsyncMock) -> None:
        world = _World(repo)
        world.patients.get_patient_by_id.return_value = None

        with pytest.raises(ValidationError, match="Patient not found in this hospital"):
            await world.recommend()

        world.ranker.choose_slot.assert_not_awaited()
        world.assert_nothing_written(commits=0)

    async def test_unknown_doctor_is_rejected_before_any_model_call(self, repo: AsyncMock) -> None:
        world = _World(repo)
        world.doctors.get_doctor_by_id.return_value = None

        with pytest.raises(ValidationError, match="Doctor not found in this hospital"):
            await world.recommend()

        world.ranker.choose_slot.assert_not_awaited()
        world.assert_nothing_written(commits=0)

    async def test_lookups_are_scoped_to_the_callers_hospital(self, repo: AsyncMock) -> None:
        world = _World(repo)

        await world.recommend()

        world.hospitals.get_by_id.assert_any_await(HOSPITAL_ID)
        world.patients.get_patient_by_id.assert_awaited_once_with(HOSPITAL_ID, PATIENT_ID)
        world.doctors.get_doctor_by_id.assert_awaited_once_with(HOSPITAL_ID, DOCTOR_ID)
        for call in (
            *world.doctors.get_availability.await_args_list,
            *world.doctors.list_leaves.await_args_list,
            *repo.booked_intervals_for_doctor.await_args_list,
            *repo.find_overlapping.await_args_list,
        ):
            assert call.args[:2] == (HOSPITAL_ID, DOCTOR_ID)

    async def test_invalid_hospital_timezone_is_a_validation_error(self, repo: AsyncMock) -> None:
        world = _World(repo, timezone="Mars/Olympus_Mons")

        with pytest.raises(ValidationError, match="not a valid IANA timezone"):
            await world.recommend()

        world.ranker.choose_slot.assert_not_awaited()

    # ── No free slots: no model call ─────────────────────────────────────────

    async def _assert_no_free_slots(self, world: _World, on: date = REC_DATE) -> None:
        result = await world.recommend(on)

        assert result.status is SlotRecommendationStatus.NO_FREE_SLOTS
        assert result.recommendation is None
        assert result.candidate_count == 0
        assert result.date == on
        assert result.timezone == REC_ZONE
        world.ranker.choose_slot.assert_not_awaited()
        world.assert_nothing_written(commits=0)

    async def test_no_availability_that_weekday(
        self, repo: AsyncMock, log: RecordingLogger
    ) -> None:
        tuesday_only = [_window(time(9, 0), time(13, 0), day_of_week=1)]

        await self._assert_no_free_slots(_World(repo, windows=tuesday_only))

        entry = log.events("appointment.recommend_slot_no_candidates")[0]
        assert entry["doctor_id"] == str(DOCTOR_ID)
        assert entry["date"] == "2030-01-07"

    async def test_every_slot_booked(self, repo: AsyncMock) -> None:
        morning = [_window(time(9, 0), time(10, 0))]
        booked = [_booking(_local(9, 0), 30), _booking(_local(9, 30), 30)]

        await self._assert_no_free_slots(_World(repo, windows=morning, booked=booked))

    async def test_every_slot_on_leave(self, repo: AsyncMock) -> None:
        leaves = [_leave(_local(0, 0), _local(23, 59))]

        await self._assert_no_free_slots(_World(repo, leaves=leaves))

    async def test_a_past_date_inside_the_accepted_range(self, repo: AsyncMock) -> None:
        """2020-01-06 was a Monday with availability; every slot has started."""
        await self._assert_no_free_slots(_World(repo), on=date(2020, 1, 6))

    async def test_a_day_whose_slots_have_all_started(
        self, repo: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evening = _local(17, 0).astimezone(UTC)
        monkeypatch.setattr("app.services.appointment_service._utc_now", lambda: evening)

        await self._assert_no_free_slots(_World(repo))

    # ── Candidates ───────────────────────────────────────────────────────────

    async def test_candidates_in_a_non_utc_hospital(self, repo: AsyncMock) -> None:
        world = _World(repo)

        result = await world.recommend()

        day = world.day()
        assert len(day) == 14
        assert [slot.slot_id for slot in day] == [f"S{n}" for n in range(1, 15)]
        assert day == sorted(day, key=lambda slot: slot.start)
        # Wall-clock in the hospital's zone — not 09:00 stamped as UTC.
        assert day[0].start.isoformat() == "2030-01-07T09:00:00+05:30"
        assert day[0].end.isoformat() == "2030-01-07T09:30:00+05:30"
        assert day[7].end.isoformat() == "2030-01-07T13:00:00+05:30"
        assert day[8].start.isoformat() == "2030-01-07T14:00:00+05:30"
        assert day[-1].end.isoformat() == "2030-01-07T17:00:00+05:30"
        assert result.candidate_count == 14
        assert result.timezone == REC_ZONE
        # The day's bounds are the hospital's local midnight, as UTC instants.
        window_start, window_end = repo.booked_intervals_for_doctor.await_args.args[2:4]
        assert window_start.astimezone(UTC) == datetime(2030, 1, 6, 18, 30, tzinfo=UTC)
        assert window_end - window_start == timedelta(days=1)

    async def test_an_overlapping_booking_of_another_length_removes_what_it_overlaps(
        self, repo: AsyncMock
    ) -> None:
        # 09:45-10:30: overlaps the 09:30 and 10:00 slots, matches neither exactly.
        world = _World(repo, booked=[_booking(_local(9, 45), 45)])

        await world.recommend()

        day = world.day()
        taken = [f"{slot.start:%H:%M}" for slot in day if slot.slot_id is None]
        assert taken == ["09:30", "10:00"]
        assert [slot.slot_id for slot in day if slot.slot_id] == [f"S{n}" for n in range(1, 13)]
        # Ids number the free slots only, in order: 10:30 is the second free one.
        assert next(slot for slot in day if slot.slot_id == "S2").start == _local(10, 30)

    async def test_a_leave_removes_its_slots(self, repo: AsyncMock) -> None:
        world = _World(repo)
        # The afternoon is on leave when the day is computed; the re-check of
        # the chosen morning slot finds no leave on it.
        world.doctors.list_leaves.side_effect = [[_leave(_local(14, 0), _local(17, 0))], []]

        result = await world.recommend()

        day = world.day()
        assert [f"{slot.start:%H:%M}" for slot in day if slot.slot_id is None] == [
            "14:00",
            "14:30",
            "15:00",
            "15:30",
            "16:00",
            "16:30",
        ]
        assert result.candidate_count == 8
        leave_call = world.doctors.list_leaves.await_args_list[0]
        assert leave_call.kwargs["starts_before"] - leave_call.kwargs["ends_after"] == timedelta(
            days=1
        )

    async def test_slots_that_have_started_are_shown_but_cannot_be_chosen(
        self, repo: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 10:00 exactly: the 10:00 slot has started (start > now is required).
        now = _local(10, 0).astimezone(UTC)
        monkeypatch.setattr("app.services.appointment_service._utc_now", lambda: now)
        world = _World(repo)

        result = await world.recommend()

        day = world.day()
        assert [f"{slot.start:%H:%M}" for slot in day if slot.slot_id is None] == [
            "09:00",
            "09:30",
            "10:00",
        ]
        assert next(slot for slot in day if slot.slot_id == "S1").start == _local(10, 30)
        assert result.recommendation is not None
        assert result.recommendation.slot_start == _local(10, 30)

    async def test_duplicate_windows_do_not_duplicate_slots(self, repo: AsyncMock) -> None:
        twice = [_window(time(9, 0), time(10, 0)), _window(time(9, 0), time(10, 0))]
        world = _World(repo, windows=twice)

        await world.recommend()

        assert [slot.slot_id for slot in world.day()] == ["S1", "S2"]

    # ── Success ──────────────────────────────────────────────────────────────

    async def test_success_returns_the_servers_own_slot(
        self, repo: AsyncMock, log: RecordingLogger
    ) -> None:
        ranker = AsyncMock()
        ranker.choose_slot.return_value = _choice("S4", "Right after an unavailable slot.")
        world = _World(repo, booked=[_booking(_local(9, 0), 30)], ranker=ranker)

        result = await world.recommend()

        assert result.status is SlotRecommendationStatus.RECOMMENDED
        recommendation = result.recommendation
        assert recommendation is not None
        # S4 is the fourth *free* slot: 09:00 is booked, so 11:00.
        assert recommendation.slot_start.isoformat() == "2030-01-07T11:00:00+05:30"
        assert recommendation.slot_end.isoformat() == "2030-01-07T11:30:00+05:30"
        assert recommendation.doctor_id == DOCTOR_ID
        assert recommendation.reason == "Right after an unavailable slot."
        assert result.date == REC_DATE
        assert result.candidate_count == 13
        # No score, confidence, provider or model reaches the client.
        dumped = result.model_dump()
        assert set(dumped) == {"status", "recommendation", "date", "timezone", "candidate_count"}
        assert set(dumped["recommendation"]) == {"slot_start", "slot_end", "doctor_id", "reason"}
        for absent in ("score", "provider", "model", "recommendations"):
            assert not hasattr(result, absent)
            assert not hasattr(recommendation, absent)

        entry = log.events("appointment.recommend_slot")[0]
        assert entry["provider"] == "groq"
        assert entry["model"] == "returned-model"
        assert entry["candidate_count"] == 13
        assert entry["hospital_id"] == str(HOSPITAL_ID)
        assert entry["doctor_id"] == str(DOCTOR_ID)
        assert entry["actor_id"] == str(ACTOR_ID)
        assert entry["request_id"] == "req-7"
        assert entry["date"] == "2030-01-07"
        # Neither the patient nor the reason text is logged.
        assert str(PATIENT_ID) not in log.text()
        assert "Right after" not in log.text()

    async def test_reason_may_be_absent_and_is_bounded(self, repo: AsyncMock) -> None:
        ranker = AsyncMock()
        ranker.choose_slot.return_value = _choice("S1", None)
        without = await _World(repo, ranker=ranker).recommend()
        assert without.recommendation is not None
        assert without.recommendation.reason is None

        ranker.choose_slot.return_value = _choice("S1", "x" * 500)
        result = await _World(repo, ranker=ranker).recommend()
        assert result.recommendation is not None
        assert result.recommendation.reason == "x" * 200

    async def test_nothing_about_the_patient_is_passed_to_the_ranker(self, repo: AsyncMock) -> None:
        world = _World(repo)

        await world.recommend()

        world.ranker.choose_slot.assert_awaited_once()
        call = world.ranker.choose_slot.await_args
        assert call.args == ()
        assert set(call.kwargs) == {"hospital_id", "actor_id", "request_id", "target_date", "day"}
        assert call.kwargs["hospital_id"] == HOSPITAL_ID
        assert call.kwargs["actor_id"] == ACTOR_ID
        assert call.kwargs["request_id"] == "req-7"
        assert call.kwargs["target_date"] == REC_DATE
        assert str(PATIENT_ID) not in repr(call)
        assert str(DOCTOR_ID) not in repr(call)

    async def test_a_recommendation_writes_nothing(self, repo: AsyncMock) -> None:
        world = _World(repo)

        await world.recommend()

        # One commit, with nothing pending: it ends the read transaction
        # before the outbound call. No appointment, no history, no audit.
        world.assert_nothing_written(commits=1)

    # ── The server does not trust the ranker ─────────────────────────────────

    @pytest.mark.parametrize("slot_id", ["S15", "S99", "s1", "", "09:00"])
    async def test_an_id_that_was_not_offered_is_rejected(
        self, repo: AsyncMock, log: RecordingLogger, slot_id: str
    ) -> None:
        ranker = AsyncMock()
        ranker.choose_slot.return_value = _choice(slot_id, "REASON-MARKER")
        world = _World(repo, ranker=ranker)

        with pytest.raises(AIResponseInvalidError) as caught:
            await world.recommend()

        assert caught.value.kind == "unknown_candidate"
        assert caught.value.__context__ is None
        entry = log.events("appointment.slot_recommendation_rejected")[0]
        assert entry["kind"] == "unknown_candidate"
        assert entry["candidate_count"] == 14
        assert "REASON-MARKER" not in log.text()
        repo.find_overlapping.assert_not_awaited()
        world.assert_nothing_written(commits=1)

    async def test_an_unavailable_slot_has_no_id_to_choose(self, repo: AsyncMock) -> None:
        """A booked slot is not in the id map, whatever a ranker returns."""
        world = _World(repo, booked=[_booking(_local(9, 0), 30)])

        await world.recommend()

        booked_slot = world.day()[0]
        assert booked_slot.start == _local(9, 0)
        assert booked_slot.slot_id is None

    # ── Re-check after the model call ────────────────────────────────────────

    async def _assert_no_longer_free(self, world: _World, log: RecordingLogger) -> None:
        with pytest.raises(AIResponseInvalidError) as caught:
            await world.recommend()

        exc = caught.value
        assert exc.kind == "slot_no_longer_free"
        assert exc.error_code == "AI_RESPONSE_INVALID"
        assert exc.status_code == 503
        assert exc.__context__ is None
        assert exc.__cause__ is None
        entry = log.events("appointment.slot_recommendation_rejected")[0]
        assert entry["kind"] == "slot_no_longer_free"
        assert log.events("appointment.recommend_slot") == []
        world.assert_nothing_written(commits=1)

    async def test_recheck_finds_an_appointment_on_the_slot(
        self, repo: AsyncMock, log: RecordingLogger
    ) -> None:
        repo.find_overlapping.return_value = [AsyncMock()]

        await self._assert_no_longer_free(_World(repo), log)

    async def test_recheck_finds_a_leave_on_the_slot(
        self, repo: AsyncMock, log: RecordingLogger
    ) -> None:
        world = _World(repo)
        # First call computes the day; the second is the re-check.
        world.doctors.list_leaves.side_effect = [[], [_leave(_local(9, 0), _local(9, 30))]]

        await self._assert_no_longer_free(world, log)

        recheck = world.doctors.list_leaves.await_args_list[1]
        assert recheck.kwargs == {
            "starts_before": _local(9, 30).astimezone(UTC),
            "ends_after": _local(9, 0).astimezone(UTC),
        }

    async def test_recheck_finds_the_slot_outside_availability(
        self, repo: AsyncMock, log: RecordingLogger
    ) -> None:
        world = _World(repo)
        # Availability was replaced while the model was answering.
        world.doctors.get_availability.side_effect = [
            CLINIC_DAY,
            [_window(time(14, 0), time(17, 0))],
        ]

        await self._assert_no_longer_free(world, log)

    async def test_recheck_finds_the_slot_has_started(
        self, repo: AsyncMock, log: RecordingLogger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clock moves past the slot's start while the model is answering."""
        clock = {"now": REC_NOW}
        monkeypatch.setattr("app.services.appointment_service._utc_now", lambda: clock["now"])

        async def _slow_model(**_kwargs: Any) -> SlotChoice:
            clock["now"] = _local(9, 0).astimezone(UTC)
            return _choice("S1")

        ranker = AsyncMock()
        ranker.choose_slot.side_effect = _slow_model
        world = _World(repo, ranker=ranker)

        await self._assert_no_longer_free(world, log)
        repo.find_overlapping.assert_not_awaited()

    async def test_recheck_asks_the_database_and_does_not_recompute_the_day(
        self, repo: AsyncMock
    ) -> None:
        ranker = AsyncMock()
        ranker.choose_slot.return_value = _choice("S3")
        world = _World(repo, ranker=ranker)

        result = await world.recommend()

        assert result.status is SlotRecommendationStatus.RECOMMENDED
        # The chosen slot's own UTC bounds, and no excluded appointment.
        repo.find_overlapping.assert_awaited_once_with(
            HOSPITAL_ID,
            DOCTOR_ID,
            datetime(2030, 1, 7, 4, 30, tzinfo=UTC),
            datetime(2030, 1, 7, 5, 0, tzinfo=UTC),
        )
        # The day is computed once. A second read would return the session's
        # stale objects and could not see an appointment moved in place.
        assert repo.booked_intervals_for_doctor.await_count == 1
        assert world.doctors.list_leaves.await_count == 2

    # ── AI failures are outcomes, not empty results ──────────────────────────

    @pytest.mark.parametrize(
        "error",
        [
            AINotConfiguredError("not_configured"),
            AIProviderUnavailableError("connection"),
            AIProviderAuthError("auth"),
            AIProviderRateLimitedError("rate_limited"),
            AIProviderRateLimitedError("local_concurrency_limit"),
            AIModelUnavailableError("model_unavailable", model="openai/gpt-oss-20b"),
            AIProviderTimeoutError("deadline"),
            AIResponseInvalidError("not_json"),
            AIResponseInvalidError("truncated"),
        ],
        ids=lambda error: f"{type(error).__name__}-{error.kind}",
    )
    async def test_typed_ai_errors_propagate_unchanged(
        self, repo: AsyncMock, error: AIError
    ) -> None:
        ranker = AsyncMock()
        ranker.choose_slot.side_effect = error
        world = _World(repo, ranker=ranker)

        with pytest.raises(AIError) as caught:
            await world.recommend()

        assert caught.value is error
        ranker.choose_slot.assert_awaited_once()  # no retry
        world.assert_nothing_written(commits=1)

    async def test_an_unexpected_ranker_error_is_not_swallowed(self, repo: AsyncMock) -> None:
        ranker = AsyncMock()
        ranker.choose_slot.side_effect = RuntimeError("bug in our own code")
        world = _World(repo, ranker=ranker)

        with pytest.raises(RuntimeError, match="bug in our own code"):
            await world.recommend()


class TestLocalDayBounds:
    """``date`` filters are the hospital's calendar day (PR #29 review finding 7)."""

    async def _bounds(self, repo: AsyncMock, hospital: AsyncMock | None, on: Any) -> Any:
        hospitals = AsyncMock()
        hospitals.get_by_id.return_value = hospital
        service, _, _ = _make_service(repo, hospitals=hospitals)
        return await service._local_day_bounds(HOSPITAL_ID, on)

    async def test_a_half_hour_zone_is_bounded_exactly(self, repo: AsyncMock) -> None:
        hospital = _hospital()
        hospital.timezone = "Asia/Kolkata"

        start, end = await self._bounds(repo, hospital, date(2030, 1, 7))

        # Local midnight in India is 18:30 UTC the day before — not 18:00.
        assert start.astimezone(UTC) == datetime(2030, 1, 6, 18, 30, tzinfo=UTC)
        assert end.astimezone(UTC) == datetime(2030, 1, 7, 18, 30, tzinfo=UTC)

    async def test_a_dst_change_day_is_not_assumed_to_be_24_hours(self, repo: AsyncMock) -> None:
        hospital = _hospital()
        hospital.timezone = "America/New_York"

        # Clocks go forward on 10 March 2030, so the local day is 23 hours long.
        start, end = await self._bounds(repo, hospital, date(2030, 3, 10))

        assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=23)

    async def test_an_unknown_timezone_falls_back_to_utc(self, repo: AsyncMock) -> None:
        hospital = _hospital()
        hospital.timezone = "Mars/Olympus_Mons"

        start, end = await self._bounds(repo, hospital, date(2030, 1, 7))

        assert start == datetime(2030, 1, 7, tzinfo=UTC)
        assert end == datetime(2030, 1, 8, tzinfo=UTC)

    async def test_a_missing_hospital_falls_back_to_utc(self, repo: AsyncMock) -> None:
        start, _ = await self._bounds(repo, None, date(2030, 1, 7))

        assert start == datetime(2030, 1, 7, tzinfo=UTC)

    async def test_list_passes_the_local_window_to_list_and_count(self, repo: AsyncMock) -> None:
        hospital = _hospital()
        hospital.timezone = "Asia/Kolkata"
        hospitals = AsyncMock()
        hospitals.get_by_id.return_value = hospital
        repo.list_appointments.return_value = []
        repo.count_appointments.return_value = 0
        service, _, _ = _make_service(repo, hospitals=hospitals)

        await service.list_appointments(HOSPITAL_ID, on_date=date(2030, 1, 7))

        window = {
            "starts_on_or_after": datetime(2030, 1, 6, 18, 30, tzinfo=UTC),
            "starts_before": datetime(2030, 1, 7, 18, 30, tzinfo=UTC),
        }
        listed = repo.list_appointments.await_args.kwargs
        counted = repo.count_appointments.await_args.kwargs
        assert {k: listed[k].astimezone(UTC) for k in window} == window
        assert {k: counted[k].astimezone(UTC) for k in window} == window
