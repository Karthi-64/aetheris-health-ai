"""Repository tests for the appointment aggregate.

Real PostgreSQL (``docs/11-TESTING_STRATEGY.md`` §2.2). What is under test is
the SQL and the constraints: module spec §16 asks specifically for the overlap
constraint and the state-history queries, and AC-2 is a *database* guarantee —
asserting it anywhere else would prove nothing.

Every query method has at least one test proving it filters by ``hospital_id``,
with one documented exception: the no-show sweeper is deliberately untenanted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.appointment import AppointmentStatus, AppointmentType
from app.repositories.appointment_repository import AppointmentRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.appointment import Appointment

pytestmark = pytest.mark.database

BASE = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)


@pytest.fixture
def repository(db_session: AsyncSession) -> AppointmentRepository:
    """A repository bound to the rolled-back test session."""
    return AppointmentRepository(db_session)


async def _fixtures(session: AsyncSession, hospital_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a patient and a doctor, returning their ids."""
    from app.models.doctor import Doctor
    from app.models.patient import Gender, Patient
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"appt-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Asha",
        last_name="Menon",
    )
    session.add(user)
    await session.flush()

    doctor = Doctor(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        user_id=user.id,
        specialization="Cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:8]}",
    )
    patient = Patient(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        mrn=f"MRN-{uuid.uuid4().hex[:8]}",
        first_name="Ananya",
        last_name="Rao",
        date_of_birth=datetime(1990, 1, 1).date(),
        gender=Gender.FEMALE,
    )
    session.add_all([doctor, patient])
    await session.flush()
    return patient.id, doctor.id


async def _book(
    repository: AppointmentRepository,
    hospital_id: uuid.UUID,
    patient_id: uuid.UUID,
    doctor_id: uuid.UUID,
    *,
    offset_minutes: int = 0,
    minutes: int = 15,
    **overrides: Any,
) -> Appointment:
    """Insert an appointment relative to :data:`BASE`."""
    start = BASE + timedelta(minutes=offset_minutes)
    values: dict[str, Any] = {
        "scheduled_start": start,
        "scheduled_end": start + timedelta(minutes=minutes),
        "appointment_type": AppointmentType.NEW,
    }
    values.update(overrides)
    return await repository.create_appointment(
        hospital_id=hospital_id, patient_id=patient_id, doctor_id=doctor_id, **values
    )


class TestOverlapConstraint:
    """AC-2: double-booking is prevented at the database level."""

    async def test_overlapping_booking_is_refused_by_postgres(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The exclusion constraint, not the service, is what makes this safe.

        Two receptionists racing cannot be stopped by an application check —
        both read before either writes — so this asserts the database refuses.
        """
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        await _book(repository, hospital_id, patient_id, doctor_id)

        with pytest.raises(IntegrityError, match="no_overlap_per_doctor"):
            await _book(
                repository, hospital_id, patient_id, doctor_id, offset_minutes=10, minutes=15
            )
        await db_session.rollback()

    async def test_adjacent_bookings_are_allowed(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Half-open ranges: 09:00-09:15 and 09:15-09:30 do not overlap.

        Getting this wrong would silently lose every second slot in a day.
        """
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        await _book(repository, hospital_id, patient_id, doctor_id)
        await _book(repository, hospital_id, patient_id, doctor_id, offset_minutes=15)

        assert await repository.count_appointments(hospital_id) == 2

    async def test_cancelled_appointment_frees_its_slot(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The constraint's WHERE clause excludes cancelled and no-show."""
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        first = await _book(repository, hospital_id, patient_id, doctor_id)
        await repository.update_appointment(first, status=AppointmentStatus.CANCELLED)

        # The same slot is now bookable again.
        await _book(repository, hospital_id, patient_id, doctor_id)
        assert await repository.count_appointments(hospital_id) == 2

    async def test_different_doctors_may_share_a_time(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The constraint is per doctor, not per clinic."""
        patient_id, first_doctor = await _fixtures(db_session, hospital_id)
        _, second_doctor = await _fixtures(db_session, hospital_id)

        await _book(repository, hospital_id, patient_id, first_doctor)
        await _book(repository, hospital_id, patient_id, second_doctor)

        assert await repository.count_appointments(hospital_id) == 2

    async def test_end_before_start_is_refused(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """``ck_appointments_time_order`` is the last line of defence."""
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)

        with pytest.raises(IntegrityError):
            await repository.create_appointment(
                hospital_id=hospital_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                scheduled_start=BASE,
                scheduled_end=BASE - timedelta(minutes=15),
                appointment_type=AppointmentType.NEW,
            )
        await db_session.rollback()


class TestIdempotencyIndex:
    """Business rule 8, enforced by a partial unique index."""

    async def test_duplicate_key_in_one_hospital_is_refused(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        await _book(repository, hospital_id, patient_id, doctor_id, idempotency_key="KEY-1")

        with pytest.raises(IntegrityError):
            await _book(
                repository,
                hospital_id,
                patient_id,
                doctor_id,
                offset_minutes=60,
                idempotency_key="KEY-1",
            )
        await db_session.rollback()

    async def test_many_appointments_may_have_no_key(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The index is partial, so NULLs do not collide with each other."""
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        await _book(repository, hospital_id, patient_id, doctor_id, idempotency_key=None)
        await _book(
            repository,
            hospital_id,
            patient_id,
            doctor_id,
            offset_minutes=30,
            idempotency_key=None,
        )

        assert await repository.count_appointments(hospital_id) == 2

    async def test_same_key_in_two_hospitals_is_allowed(
        self,
        repository: AppointmentRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Keys are scoped per tenant, so two clinics cannot collide."""
        mine = await _fixtures(db_session, hospital_id)
        theirs = await _fixtures(db_session, other_hospital_id)

        await _book(repository, hospital_id, *mine, idempotency_key="SHARED")
        await _book(repository, other_hospital_id, *theirs, idempotency_key="SHARED")

        assert await repository.count_appointments(hospital_id) == 1
        assert await repository.count_appointments(other_hospital_id) == 1

    async def test_lookup_finds_the_original(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        booked = await _book(
            repository, hospital_id, patient_id, doctor_id, idempotency_key="KEY-2"
        )

        found = await repository.get_by_idempotency_key(hospital_id, "KEY-2")
        assert found is not None
        assert found.id == booked.id

    async def test_lookup_is_tenant_scoped(
        self,
        repository: AppointmentRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        await _book(repository, hospital_id, patient_id, doctor_id, idempotency_key="KEY-3")

        assert await repository.get_by_idempotency_key(other_hospital_id, "KEY-3") is None


class TestOverlapQuery:
    """The advisory check the service uses before writing."""

    @pytest.mark.parametrize(
        ("offset", "minutes", "expected"),
        [
            (5, 15, True),  # straddles the start
            (0, 15, True),  # exact same slot
            (-5, 15, True),  # straddles the end
            (-30, 60, True),  # encloses
            (15, 15, False),  # starts exactly when the other ends
            (-15, 15, False),  # ends exactly when the other starts
            (60, 15, False),  # unrelated
        ],
    )
    async def test_overlap_boundaries(
        self,
        repository: AppointmentRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        offset: int,
        minutes: int,
        expected: bool,
    ) -> None:
        """Half-open comparison, pinned at every boundary."""
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        await _book(repository, hospital_id, patient_id, doctor_id)

        probe_start = BASE + timedelta(minutes=offset)
        found = await repository.find_overlapping(
            hospital_id, doctor_id, probe_start, probe_start + timedelta(minutes=minutes)
        )
        assert bool(found) is expected

    async def test_cancelled_is_not_a_clash(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        booked = await _book(repository, hospital_id, patient_id, doctor_id)
        await repository.update_appointment(booked, status=AppointmentStatus.CANCELLED)

        assert (
            await repository.find_overlapping(
                hospital_id, doctor_id, BASE, BASE + timedelta(minutes=15)
            )
            == []
        )

    async def test_exclude_lets_an_appointment_move_within_itself(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Rescheduling must not clash with the row being rescheduled."""
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        booked = await _book(repository, hospital_id, patient_id, doctor_id)

        assert (
            await repository.find_overlapping(
                hospital_id,
                doctor_id,
                BASE,
                BASE + timedelta(minutes=15),
                exclude_appointment_id=booked.id,
            )
            == []
        )

    async def test_overlap_is_tenant_scoped(
        self,
        repository: AppointmentRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        await _book(repository, hospital_id, patient_id, doctor_id)

        assert (
            await repository.find_overlapping(
                other_hospital_id, doctor_id, BASE, BASE + timedelta(minutes=15)
            )
            == []
        )


class TestDoctorSeamQueries:
    """The queries Doctor Management depends on through BookedIntervalSource."""

    async def test_booked_intervals_exclude_cancelled(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """A cancelled appointment must not grey out a genuinely free slot."""
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        live = await _book(repository, hospital_id, patient_id, doctor_id)
        dead = await _book(repository, hospital_id, patient_id, doctor_id, offset_minutes=30)
        await repository.update_appointment(dead, status=AppointmentStatus.CANCELLED)

        rows = await repository.booked_intervals_for_doctor(
            hospital_id, doctor_id, BASE, BASE + timedelta(hours=2)
        )
        assert [r.id for r in rows] == [live.id]

    async def test_count_future_ignores_past_and_cancelled(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Backs the doctor deletion guard — only real upcoming work counts."""
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        await _book(repository, hospital_id, patient_id, doctor_id)
        cancelled = await _book(repository, hospital_id, patient_id, doctor_id, offset_minutes=30)
        await repository.update_appointment(cancelled, status=AppointmentStatus.CANCELLED)

        assert (
            await repository.count_future_for_doctor(
                hospital_id, doctor_id, after=BASE - timedelta(days=1)
            )
            == 1
        )
        # Nothing is "future" relative to well after the appointment.
        assert (
            await repository.count_future_for_doctor(
                hospital_id, doctor_id, after=BASE + timedelta(days=1)
            )
            == 0
        )

    async def test_completed_appointment_does_not_block_deactivation(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """FR-5 is about work needing reassignment, and a completed one needs none.

        Regression: this counted ``completed`` as blocking, which left a doctor
        permanently undeactivatable — nothing can move an appointment out of a
        terminal state, so the guard could never clear. Found by driving the
        live API rather than by a unit test, because it only shows up once an
        appointment has been carried all the way through the lifecycle.
        """
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        done = await _book(repository, hospital_id, patient_id, doctor_id)
        await repository.update_appointment(done, status=AppointmentStatus.COMPLETED)

        assert (
            await repository.count_future_for_doctor(
                hospital_id, doctor_id, after=BASE - timedelta(days=1)
            )
            == 0
        )

    async def test_count_future_is_tenant_scoped(
        self,
        repository: AppointmentRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        await _book(repository, hospital_id, patient_id, doctor_id)

        assert (
            await repository.count_future_for_doctor(
                other_hospital_id, doctor_id, after=BASE - timedelta(days=1)
            )
            == 0
        )


class TestStatusHistory:
    """Business rule 7 and AC-6."""

    async def test_history_is_appended_and_ordered(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        appointment = await _book(repository, hospital_id, patient_id, doctor_id)

        await repository.record_transition(
            appointment=appointment,
            from_status=None,
            to_status=AppointmentStatus.BOOKED,
            reason="booked",
        )
        await repository.record_transition(
            appointment=appointment,
            from_status=AppointmentStatus.BOOKED,
            to_status=AppointmentStatus.CHECKED_IN,
        )

        history = await repository.get_status_history(hospital_id, appointment.id)

        # Asserted as a set, not a sequence. ``changed_at`` defaults to
        # Postgres ``now()``, which is *transaction* time — so two rows written
        # in one transaction share a timestamp and the id tiebreak decides
        # order arbitrarily. In production each transition is its own request
        # and therefore its own transaction, so timestamps differ and the
        # ordering is real; this test simply must not depend on an artefact of
        # writing both at once.
        assert {(h.from_status, h.to_status) for h in history} == {
            (None, AppointmentStatus.BOOKED),
            (AppointmentStatus.BOOKED, AppointmentStatus.CHECKED_IN),
        }

    async def test_history_accepts_a_null_actor(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The sweeper has no acting user, which is why the column is nullable."""
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        appointment = await _book(repository, hospital_id, patient_id, doctor_id)

        entry = await repository.record_transition(
            appointment=appointment,
            from_status=AppointmentStatus.BOOKED,
            to_status=AppointmentStatus.NO_SHOW,
            changed_by=None,
            reason="no_show_sweeper",
        )
        assert entry.changed_by is None

    async def test_history_is_tenant_scoped(
        self,
        repository: AppointmentRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        appointment = await _book(repository, hospital_id, patient_id, doctor_id)
        await repository.record_transition(
            appointment=appointment, from_status=None, to_status=AppointmentStatus.BOOKED
        )

        assert await repository.get_status_history(other_hospital_id, appointment.id) == []


class TestListingAndQueue:
    """Filters, the walk-in queue, and the sweeper query."""

    async def test_list_is_tenant_scoped(
        self,
        repository: AppointmentRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        mine = await _fixtures(db_session, hospital_id)
        theirs = await _fixtures(db_session, other_hospital_id)
        await _book(repository, hospital_id, *mine)
        await _book(repository, other_hospital_id, *theirs)

        assert await repository.count_appointments(hospital_id) == 1

    async def test_filters_narrow_correctly(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        await _book(repository, hospital_id, patient_id, doctor_id)
        await _book(
            repository,
            hospital_id,
            patient_id,
            doctor_id,
            offset_minutes=30,
            appointment_type=AppointmentType.WALK_IN,
        )

        assert (
            await repository.count_appointments(
                hospital_id, appointment_type=AppointmentType.WALK_IN
            )
            == 1
        )
        assert await repository.count_appointments(hospital_id, doctor_id=doctor_id) == 2
        assert (
            await repository.count_appointments(
                hospital_id, starts_on_or_after=BASE + timedelta(minutes=20)
            )
            == 1
        )

    async def test_walk_in_queue_orders_by_check_in_time(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Arrival order, not scheduled order — that is what a queue means.

        Both are checked in so the comparison is like-for-like. The one booked
        for a *later* slot arrived *first*, and must head the queue.
        """
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        booked_earlier_slot = await _book(
            repository,
            hospital_id,
            patient_id,
            doctor_id,
            offset_minutes=30,
            appointment_type=AppointmentType.WALK_IN,
        )
        booked_later_slot = await _book(
            repository,
            hospital_id,
            patient_id,
            doctor_id,
            offset_minutes=60,
            appointment_type=AppointmentType.WALK_IN,
        )

        await repository.update_appointment(
            booked_later_slot,
            status=AppointmentStatus.CHECKED_IN,
            checked_in_at=BASE,
        )
        await repository.update_appointment(
            booked_earlier_slot,
            status=AppointmentStatus.CHECKED_IN,
            checked_in_at=BASE + timedelta(minutes=20),
        )

        queue = await repository.list_walk_in_queue(hospital_id)
        assert [a.id for a in queue] == [booked_later_slot.id, booked_earlier_slot.id]

    async def test_walk_in_not_yet_arrived_sorts_by_booking_time(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """``coalesce(checked_in_at, created_at)`` — an un-arrived walk-in still queues.

        A walk-in that has not checked in yet must still appear, ordered by when
        it was booked. Ordering between two such rows is not asserted here:
        ``created_at`` defaults to Postgres ``now()``, which is transaction
        time, so both would share a timestamp in this test even though separate
        requests in production would not.
        """
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        arrived = await _book(
            repository,
            hospital_id,
            patient_id,
            doctor_id,
            appointment_type=AppointmentType.WALK_IN,
        )
        waiting = await _book(
            repository,
            hospital_id,
            patient_id,
            doctor_id,
            offset_minutes=30,
            appointment_type=AppointmentType.WALK_IN,
        )
        # One has arrived, and did so before either was booked.
        await repository.update_appointment(
            arrived,
            status=AppointmentStatus.CHECKED_IN,
            checked_in_at=datetime(2020, 1, 1, tzinfo=UTC),
        )

        queue = await repository.list_walk_in_queue(hospital_id)
        assert [a.id for a in queue] == [arrived.id, waiting.id]

    async def test_walk_in_queue_excludes_finished(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        done = await _book(
            repository,
            hospital_id,
            patient_id,
            doctor_id,
            appointment_type=AppointmentType.WALK_IN,
        )
        await repository.update_appointment(done, status=AppointmentStatus.COMPLETED)

        assert await repository.list_walk_in_queue(hospital_id) == []

    async def test_no_show_candidates_are_overdue_and_unfinished(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The sweeper query is deliberately untenanted (see its docstring)."""
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        overdue = await _book(repository, hospital_id, patient_id, doctor_id)
        finished = await _book(repository, hospital_id, patient_id, doctor_id, offset_minutes=30)
        await repository.update_appointment(finished, status=AppointmentStatus.COMPLETED)

        candidates = await repository.find_no_show_candidates(cutoff=BASE + timedelta(hours=1))
        ids = {a.id for a in candidates}
        assert overdue.id in ids
        assert finished.id not in ids


class TestThePatientsOwnCalendar:
    """The two queries patient booking decides the patient's own rules with."""

    async def test_overlap_and_count_are_scoped_to_the_patient_and_the_hospital(
        self,
        repository: AppointmentRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        other_patient, other_doctor = await _fixtures(db_session, hospital_id)
        foreign_patient, foreign_doctor = await _fixtures(db_session, other_hospital_id)
        mine = await _book(repository, hospital_id, patient_id, doctor_id)
        await _book(repository, hospital_id, other_patient, other_doctor)
        await _book(repository, other_hospital_id, foreign_patient, foreign_doctor)
        window = (BASE, BASE + timedelta(minutes=15))
        before = BASE - timedelta(days=1)

        found = await repository.find_overlapping_for_patient(hospital_id, patient_id, *window)

        assert [row.id for row in found] == [mine.id]
        assert (
            await repository.find_overlapping_for_patient(other_hospital_id, patient_id, *window)
            == []
        )
        assert (
            await repository.find_overlapping_for_patient(hospital_id, foreign_patient, *window)
            == []
        )
        assert (
            await repository.count_upcoming_booked_for_patient(
                hospital_id, patient_id, after=before
            )
            == 1
        )
        assert (
            await repository.count_upcoming_booked_for_patient(
                other_hospital_id, patient_id, after=before
            )
            == 0
        )
        assert (
            await repository.count_upcoming_booked_for_patient(
                hospital_id, foreign_patient, after=before
            )
            == 0
        )

    async def test_overlap_is_half_open_and_ignores_appointments_that_freed_their_slot(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        held = await _book(repository, hospital_id, patient_id, doctor_id)
        end = BASE + timedelta(minutes=15)

        async def overlapping(start: datetime, stop: datetime) -> int:
            return len(
                await repository.find_overlapping_for_patient(hospital_id, patient_id, start, stop)
            )

        assert await overlapping(BASE + timedelta(minutes=10), end + timedelta(minutes=10)) == 1
        assert await overlapping(end, end + timedelta(minutes=15)) == 0
        assert await overlapping(BASE - timedelta(minutes=15), BASE) == 0
        for status in (AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW):
            await repository.update_appointment(held, status=status)
            assert await overlapping(BASE, end) == 0
        for status in (
            AppointmentStatus.CHECKED_IN,
            AppointmentStatus.IN_PROGRESS,
            AppointmentStatus.COMPLETED,
        ):
            await repository.update_appointment(held, status=status)
            assert await overlapping(BASE, end) == 1

    async def test_the_count_is_of_booked_appointments_that_have_not_started(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        first = await _book(repository, hospital_id, patient_id, doctor_id)
        await _book(repository, hospital_id, patient_id, doctor_id, offset_minutes=60)

        async def held(after: datetime) -> int:
            return await repository.count_upcoming_booked_for_patient(
                hospital_id, patient_id, after=after
            )

        assert await held(BASE - timedelta(minutes=1)) == 2
        assert await held(BASE) == 1
        assert await held(BASE + timedelta(hours=2)) == 0
        await repository.update_appointment(first, status=AppointmentStatus.CANCELLED)
        assert await held(BASE - timedelta(minutes=1)) == 1


class TestAPatientsOwnAppointmentsAcrossHospitals:
    """The cross-hospital reads behind "My appointments": owned pairs, or nothing."""

    async def test_only_rows_whose_hospital_and_patient_are_one_pair_are_returned(
        self,
        repository: AppointmentRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        from app.core.tenancy import cross_tenant

        scope = cross_tenant("test: a patient's own appointments")
        patient_a, doctor_a = await _fixtures(db_session, hospital_id)
        patient_b, doctor_b = await _fixtures(db_session, other_hospital_id)
        stranger, stranger_doctor = await _fixtures(db_session, hospital_id)
        mine_a = await _book(repository, hospital_id, patient_a, doctor_a)
        mine_b = await _book(repository, other_hospital_id, patient_b, doctor_b, offset_minutes=30)
        theirs = await _book(repository, hospital_id, stranger, stranger_doctor)
        pairs = [(hospital_id, patient_a), (other_hospital_id, patient_b)]
        before = BASE - timedelta(days=1)

        rows, total = await repository.list_for_patient_records(
            pairs, scope, upcoming=True, now=before
        )

        assert ([row.id for row in rows], total) == ([mine_a.id, mine_b.id], 2)
        # A pair is a pair: the right patient at the wrong hospital owns nothing.
        crossed = [(other_hospital_id, patient_a), (hospital_id, patient_b)]
        assert await repository.list_for_patient_records(
            crossed, scope, upcoming=True, now=before
        ) == ([], 0)
        assert await repository.list_for_patient_records([], scope, upcoming=True, now=before) == (
            [],
            0,
        )
        # Confined to one hospital, the other hospital's row is not there.
        confined, count = await repository.list_for_patient_records(
            pairs, hospital_id, upcoming=True, now=before
        )
        assert ([row.id for row in confined], count) == ([mine_a.id], 1)

        assert (await repository.get_for_patient_records(pairs, scope, mine_b.id)).id == mine_b.id  # type: ignore[union-attr]
        assert await repository.get_for_patient_records(pairs, scope, theirs.id) is None
        assert await repository.get_for_patient_records(crossed, scope, mine_a.id) is None
        assert await repository.get_for_patient_records([], scope, mine_a.id) is None
        assert await repository.get_for_patient_records(pairs, other_hospital_id, mine_a.id) is None

    async def test_upcoming_and_past_split_on_status_and_end_and_sort_opposite_ways(
        self, repository: AppointmentRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        rows = [
            await _book(repository, hospital_id, patient_id, doctor_id, offset_minutes=60 * n)
            for n in range(5)
        ]
        await repository.update_appointment(rows[1], status=AppointmentStatus.CANCELLED)
        await repository.update_appointment(rows[3], status=AppointmentStatus.COMPLETED)
        await repository.update_appointment(rows[4], status=AppointmentStatus.CHECKED_IN)
        pairs = [(hospital_id, patient_id)]
        now = BASE + timedelta(minutes=15)  # the first one has just ended

        async def ids(upcoming: bool, **page: int) -> tuple[list[uuid.UUID], int]:
            found, total = await repository.list_for_patient_records(
                pairs, hospital_id, upcoming=upcoming, now=now, **page
            )
            return [row.id for row in found], total

        assert await ids(True) == ([rows[2].id, rows[4].id], 2)
        assert await ids(False) == ([rows[3].id, rows[1].id, rows[0].id], 3)
        assert await ids(False, skip=1, limit=1) == ([rows[1].id], 3)
        assert (await ids(True, skip=5))[0] == []

    async def test_locking_reads_the_current_row_of_this_hospital_only(
        self,
        repository: AppointmentRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        patient_id, doctor_id = await _fixtures(db_session, hospital_id)
        booked = await _book(repository, hospital_id, patient_id, doctor_id)

        locked = await repository.lock_appointment_by_id(hospital_id, booked.id)

        assert locked is not None and locked.id == booked.id
        assert locked.doctor.id == doctor_id
        assert await repository.lock_appointment_by_id(other_hospital_id, booked.id) is None
        assert await repository.lock_appointment_by_id(hospital_id, uuid.uuid4()) is None
