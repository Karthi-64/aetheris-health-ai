"""Unit tests for booking, idempotency, and the no-show sweeper.

Repositories are mocked; no database. The overlap *constraint* is tested
against real Postgres in the repository suite — here we test that the service
checks first, surfaces a useful 409, and never writes when validation fails.

Module spec §16 calls out idempotency and no-overlap logic specifically.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ValidationError
from app.models.appointment import AppointmentStatus, AppointmentType
from app.services.appointment_service import (
    DEFAULT_NO_SHOW_GRACE_MINUTES,
    AppointmentService,
    DoubleBookingError,
    NullInvoiceDraftSink,
    OutsideAvailabilityError,
)
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


class TestSlotRecommendation:
    """Module spec §5.9 — degradation matters more than the ranking itself."""

    async def test_disabled_flag_returns_empty(self, repo: AsyncMock) -> None:
        from app.schemas.appointment import SlotRecommendationRequest

        service, _, _ = _make_service(repo)  # settings {} => flag off

        result = await service.recommend_slots(
            HOSPITAL_ID, SlotRecommendationRequest(patient_id=uuid.uuid4())
        )

        assert result.recommendations == []

    async def test_ai_failure_degrades_instead_of_raising(self, repo: AsyncMock) -> None:
        """An AI outage must never stop reception booking by hand."""
        from app.schemas.appointment import SlotRecommendationRequest

        hospitals = AsyncMock()
        hospitals.get_by_id.return_value = _hospital({"feature.ai.slot_recommendation": True})
        doctors = AsyncMock()
        doctors.get_doctor_by_id.return_value = AsyncMock()
        window = AsyncMock()
        window.day_of_week = future_window()[0].weekday()
        window.start_time = future_window()[0].time()
        window.slot_duration_minutes = 15
        doctors.get_availability.return_value = [window]
        repo.booked_intervals_for_doctor.return_value = []

        ranker = AsyncMock()
        ranker.rank_slots.side_effect = RuntimeError("provider down")

        session = FakeSession()
        service = AppointmentService(
            repo,
            AsyncMock(),
            doctors,
            hospitals,
            session,  # type: ignore[arg-type]
            RecordingAuditSink(),
            NullInvoiceDraftSink(),
            ranker,
        )

        result = await service.recommend_slots(
            HOSPITAL_ID,
            SlotRecommendationRequest(
                patient_id=uuid.uuid4(),
                doctor_id=uuid.uuid4(),
                preferred_window_start=future_window()[0],
                preferred_window_end=future_window()[0] + timedelta(days=1),
            ),
        )

        assert result.recommendations == []


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
