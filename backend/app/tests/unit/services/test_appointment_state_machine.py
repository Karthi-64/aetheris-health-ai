"""Unit tests for the appointment state machine.

Module spec §16 asks for the transition rules to be tested directly, and AC-3
requires that status can only follow the defined machine. These assert against
:data:`~app.services.appointment_service.ALLOWED_TRANSITIONS` and the service
method that reads it, with no database.

Every ordered pair of states is covered: the parametrised test below enumerates
all 36 combinations and checks each against the spec's diagram, so a transition
silently added or removed fails here rather than in production.
"""

from __future__ import annotations

import itertools
import uuid
from unittest.mock import AsyncMock

import pytest

from app.models.appointment import TERMINAL_STATUSES, Appointment, AppointmentStatus
from app.services.appointment_service import (
    ALLOWED_TRANSITIONS,
    AppointmentService,
    InvalidTransitionError,
    NullInvoiceDraftSink,
)
from app.tests.conftest import FakeSession, RecordingAuditSink
from app.tests.factories import build_appointment_model, build_cancel_request

HOSPITAL_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()

#: The transitions module spec §5.1 draws, written out independently of the
#: implementation. Comparing the two catches a diagram/code drift that a test
#: importing ALLOWED_TRANSITIONS for both sides would miss.
SPEC_TRANSITIONS: set[tuple[AppointmentStatus, AppointmentStatus]] = {
    (AppointmentStatus.BOOKED, AppointmentStatus.CHECKED_IN),
    (AppointmentStatus.BOOKED, AppointmentStatus.IN_PROGRESS),
    (AppointmentStatus.BOOKED, AppointmentStatus.CANCELLED),
    (AppointmentStatus.BOOKED, AppointmentStatus.NO_SHOW),
    (AppointmentStatus.CHECKED_IN, AppointmentStatus.IN_PROGRESS),
    (AppointmentStatus.CHECKED_IN, AppointmentStatus.COMPLETED),
    (AppointmentStatus.CHECKED_IN, AppointmentStatus.CANCELLED),
    (AppointmentStatus.CHECKED_IN, AppointmentStatus.NO_SHOW),
    (AppointmentStatus.IN_PROGRESS, AppointmentStatus.COMPLETED),
}


def _attach(appointment: Appointment) -> Appointment:
    """Give a detached Appointment the relationships the DTO reads.

    ``AppointmentResponse.from_model`` reads ``patient`` and ``doctor``; on an
    instance that was never loaded from the database they are unset.
    """
    patient = AsyncMock()
    patient.full_name = "Ananya Rao"
    doctor = AsyncMock()
    doctor.user.first_name = "Asha"
    doctor.user.last_name = "Menon"
    appointment.patient = patient
    appointment.doctor = doctor
    return appointment


def _make_service(repo: AsyncMock) -> tuple[AppointmentService, FakeSession, RecordingAuditSink]:
    """Assemble a service over mocked collaborators."""
    session = FakeSession()
    audit = RecordingAuditSink()
    service = AppointmentService(
        repo,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        session,  # type: ignore[arg-type]
        audit,
        NullInvoiceDraftSink(),
    )
    return service, session, audit


class TestTransitionTable:
    """The declared machine matches the specification."""

    def test_table_matches_the_spec_diagram(self) -> None:
        implemented = {
            (source, target)
            for source, targets in ALLOWED_TRANSITIONS.items()
            for target in targets
        }
        assert implemented == SPEC_TRANSITIONS

    def test_every_status_has_an_entry(self) -> None:
        """A status missing from the table would raise KeyError mid-transition."""
        assert set(ALLOWED_TRANSITIONS) == set(AppointmentStatus)

    @pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
    def test_terminal_states_allow_nothing(self, status: AppointmentStatus) -> None:
        """Business rule 5: a cancelled appointment is never reactivated."""
        assert ALLOWED_TRANSITIONS[status] == frozenset()

    def test_no_state_transitions_to_itself_except_via_reschedule(self) -> None:
        """Self-transitions are not part of the machine.

        A reschedule records ``booked -> booked`` in the *history*, but it goes
        through the reschedule path, not through the transition table.
        """
        for source, targets in ALLOWED_TRANSITIONS.items():
            assert source not in targets

    @pytest.mark.parametrize(
        ("source", "target"), list(itertools.product(AppointmentStatus, AppointmentStatus))
    )
    def test_every_pair_is_classified(
        self, source: AppointmentStatus, target: AppointmentStatus
    ) -> None:
        """All 36 ordered pairs agree with the spec — legal or not."""
        allowed = target in ALLOWED_TRANSITIONS[source]
        assert allowed is ((source, target) in SPEC_TRANSITIONS)


class TestTransitionEnforcement:
    """The service refuses moves the table does not permit (AC-3)."""

    @pytest.fixture
    def repo(self) -> AsyncMock:
        mock = AsyncMock()
        return mock

    async def test_check_in_from_booked_succeeds(self, repo: AsyncMock) -> None:
        appointment = _attach(build_appointment_model(hospital_id=HOSPITAL_ID))
        repo.get_appointment_by_id.return_value = appointment
        repo.lock_appointment_by_id.return_value = appointment
        repo.update_appointment.return_value = appointment
        service, session, audit = _make_service(repo)

        await service.check_in(HOSPITAL_ID, appointment.id, actor_id=ACTOR_ID)

        assert session.commits == 1
        assert audit.actions() == ["appointment.checked_in"]

    async def test_history_row_written_on_every_transition(self, repo: AsyncMock) -> None:
        """Business rule 7 and AC-6."""
        appointment = _attach(build_appointment_model(hospital_id=HOSPITAL_ID))
        repo.get_appointment_by_id.return_value = appointment
        repo.lock_appointment_by_id.return_value = appointment
        repo.update_appointment.return_value = appointment
        service, _, _ = _make_service(repo)

        await service.check_in(HOSPITAL_ID, appointment.id, actor_id=ACTOR_ID)

        repo.record_transition.assert_awaited_once()
        kwargs = repo.record_transition.await_args.kwargs
        assert kwargs["from_status"] is AppointmentStatus.BOOKED
        assert kwargs["to_status"] is AppointmentStatus.CHECKED_IN
        assert kwargs["changed_by"] == ACTOR_ID

    async def test_start_from_completed_is_rejected(self, repo: AsyncMock) -> None:
        appointment = _attach(
            build_appointment_model(hospital_id=HOSPITAL_ID, status=AppointmentStatus.COMPLETED)
        )
        repo.get_appointment_by_id.return_value = appointment
        repo.lock_appointment_by_id.return_value = appointment
        service, session, audit = _make_service(repo)

        with pytest.raises(InvalidTransitionError) as exc:
            await service.start(HOSPITAL_ID, appointment.id, actor_id=ACTOR_ID)

        # Business rule 6: 400, not 409 — the request is wrong, not conflicting.
        assert exc.value.status_code == 400
        assert exc.value.detail["current_status"] == "completed"
        assert exc.value.detail["allowed_transitions"] == []
        repo.update_appointment.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_cancelled_cannot_be_reactivated(self, repo: AsyncMock) -> None:
        """Business rule 5, stated as a test."""
        appointment = _attach(
            build_appointment_model(hospital_id=HOSPITAL_ID, status=AppointmentStatus.CANCELLED)
        )
        repo.get_appointment_by_id.return_value = appointment
        repo.lock_appointment_by_id.return_value = appointment
        service, session, _ = _make_service(repo)

        for attempt in (service.check_in, service.start, service.complete):
            with pytest.raises(InvalidTransitionError):
                await attempt(HOSPITAL_ID, appointment.id, actor_id=ACTOR_ID)

        assert session.commits == 0

    async def test_complete_from_checked_in_is_allowed(self, repo: AsyncMock) -> None:
        """Module spec §14: a doctor may complete without a formal start.

        The skipped ``in_progress`` state stays visible in the history, which is
        why transitions are recorded rather than just the current status.
        """
        appointment = _attach(
            build_appointment_model(hospital_id=HOSPITAL_ID, status=AppointmentStatus.CHECKED_IN)
        )
        repo.get_appointment_by_id.return_value = appointment
        repo.lock_appointment_by_id.return_value = appointment
        repo.update_appointment.return_value = appointment
        service, session, _ = _make_service(repo)

        await service.complete(HOSPITAL_ID, appointment.id, actor_id=ACTOR_ID)

        assert session.commits == 1
        assert repo.record_transition.await_args.kwargs["from_status"] is (
            AppointmentStatus.CHECKED_IN
        )

    async def test_cancel_records_the_reason(self, repo: AsyncMock) -> None:
        appointment = _attach(build_appointment_model(hospital_id=HOSPITAL_ID))
        repo.get_appointment_by_id.return_value = appointment
        repo.lock_appointment_by_id.return_value = appointment
        repo.update_appointment.return_value = appointment
        service, _, _ = _make_service(repo)

        await service.cancel(HOSPITAL_ID, appointment.id, build_cancel_request(), actor_id=ACTOR_ID)

        assert (
            repo.update_appointment.await_args.kwargs["cancelled_reason"]
            == "Patient called to cancel"
        )
        assert repo.record_transition.await_args.kwargs["reason"] == "Patient called to cancel"

    async def test_no_show_from_booked_is_allowed(self, repo: AsyncMock) -> None:
        appointment = _attach(build_appointment_model(hospital_id=HOSPITAL_ID))
        repo.get_appointment_by_id.return_value = appointment
        repo.lock_appointment_by_id.return_value = appointment
        repo.update_appointment.return_value = appointment
        service, session, _ = _make_service(repo)

        await service.mark_no_show(HOSPITAL_ID, appointment.id, actor_id=ACTOR_ID)

        assert session.commits == 1
