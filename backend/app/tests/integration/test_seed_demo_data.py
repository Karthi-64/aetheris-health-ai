"""Integration tests for the demo data seed.

The seed is the frontend team's development database and the Saturday demo
script, so two properties matter and are checked here: what it produces is
valid and complete, and running it twice changes nothing
(``docs/11-TESTING_STRATEGY.md`` §4.3 — each test runs in a rolled-back
transaction, so these never leave rows behind).

:func:`~app.seeds.demo_data.seed_demo_data` is called directly rather than
through :func:`~app.seeds.seed.seed_database`: the latter builds its own engine
and session factory and commits, which would escape the test transaction. The
permissions/roles half of the seed is already exercised by the identity tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.appointment import (
    Appointment,
    AppointmentStatus,
    AppointmentStatusHistory,
    AppointmentType,
)
from app.models.department import Department
from app.models.doctor import Doctor, DoctorAvailability, DoctorLeave
from app.models.hospital import Hospital
from app.models.patient import MrnSequence, Patient
from app.seeds.demo_data import (
    CLINIC_TZ,
    DEPARTMENTS,
    DOCTORS,
    PATIENTS,
    seed_demo_data,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database


@pytest_asyncio.fixture
async def hospital(db_session: AsyncSession, hospital_id: uuid.UUID) -> Hospital:
    """The tenant the seed writes into."""
    result = await db_session.execute(select(Hospital).where(Hospital.id == hospital_id))
    return result.unique().scalar_one()


async def _count(session: AsyncSession, model: type[Any], hospital_id: uuid.UUID) -> int:
    """Count rows of ``model`` belonging to a hospital.

    ``type[Any]`` rather than a tighter bound: the models share
    ``hospital_id`` through :class:`~app.models.base.TenantMixin`, which is a
    ``declared_attr`` and so invisible to a ``type[DeclarativeBase]``
    annotation.
    """
    result = await session.execute(
        select(func.count()).select_from(model).where(model.hospital_id == hospital_id)
    )
    return int(result.scalar_one())


async def _counts(session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, int]:
    """Snapshot the row counts the seed is responsible for."""
    return {
        "departments": await _count(session, Department, hospital_id),
        "doctors": await _count(session, Doctor, hospital_id),
        "availability": await _count(session, DoctorAvailability, hospital_id),
        "leaves": await _count(session, DoctorLeave, hospital_id),
        "patients": await _count(session, Patient, hospital_id),
        "appointments": await _count(session, Appointment, hospital_id),
    }


class TestSeededData:
    """What one run of the seed produces."""

    async def test_seeds_the_full_catalogue(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        counts = await _counts(db_session, hospital.id)
        assert counts["departments"] == len(DEPARTMENTS)
        assert counts["doctors"] == len(DOCTORS)
        assert counts["patients"] == len(PATIENTS)
        # The sprint asks for at least 2–3 doctors and 10 patients; the seed is
        # above both, and this asserts the floor rather than the exact figure so
        # adding a fixture does not fail the suite.
        assert counts["doctors"] >= 3
        assert counts["patients"] >= 10
        assert counts["availability"] > 0
        assert counts["leaves"] == 1

    async def test_every_appointment_status_is_represented(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        # A demo that cannot show a cancellation or a no-show cannot show the
        # lifecycle, and the frontend has no fixture for those states.
        await seed_demo_data(db_session, hospital, {})

        result = await db_session.execute(
            select(Appointment.status).where(Appointment.hospital_id == hospital.id).distinct()
        )
        assert set(result.scalars().all()) == set(AppointmentStatus)

    async def test_walk_ins_exist_for_the_queue(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        result = await db_session.execute(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.hospital_id == hospital.id,
                Appointment.type == AppointmentType.WALK_IN,
                Appointment.status == AppointmentStatus.CHECKED_IN,
            )
        )
        assert int(result.scalar_one()) >= 1

    async def test_future_bookings_land_on_working_days(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        # Seeding on a Friday used to put "tomorrow's" bookings on a Saturday,
        # where no doctor publishes availability — so the appointment existed
        # but never appeared as `booked` in the slots read model, which is the
        # join the frontend most needs to see working.
        await seed_demo_data(db_session, hospital, {})

        result = await db_session.execute(
            select(Appointment.scheduled_start).where(
                Appointment.hospital_id == hospital.id,
                Appointment.status == AppointmentStatus.BOOKED,
            )
        )
        for start in result.scalars().all():
            assert start.astimezone(CLINIC_TZ).weekday() < 5

    async def test_patients_get_unique_sequential_mrns(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        result = await db_session.execute(
            select(Patient.mrn).where(Patient.hospital_id == hospital.id)
        )
        mrns = list(result.scalars().all())
        assert len(mrns) == len(PATIENTS)
        assert len(set(mrns)) == len(mrns)
        assert all(mrn.startswith("MRN-") for mrn in mrns)

        # The counter must agree with the rows, or the next patient registered
        # through the API collides with a seeded one.
        sequence = await db_session.execute(
            select(MrnSequence.current_value).where(MrnSequence.hospital_id == hospital.id)
        )
        assert sequence.scalar_one() == len(PATIENTS)

    async def test_demographics_span_the_gender_enum_and_a_wide_age_range(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        # Search, gender filters and age filters are the things the frontend
        # demos, and none of them are demonstrable on uniform data.
        await seed_demo_data(db_session, hospital, {})

        result = await db_session.execute(select(Patient).where(Patient.hospital_id == hospital.id))
        patients = list(result.unique().scalars().all())

        assert len({patient.gender for patient in patients}) >= 3
        ages = sorted(patient.date_of_birth for patient in patients)
        assert (ages[-1] - ages[0]).days > 365 * 50

    async def test_includes_a_deactivated_patient_and_doctor(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        # `include_inactive` is part of the contract; without an inactive row it
        # cannot be demonstrated or regression-tested against real data.
        await seed_demo_data(db_session, hospital, {})

        inactive_patients = await db_session.execute(
            select(func.count())
            .select_from(Patient)
            .where(Patient.hospital_id == hospital.id, Patient.deleted_at.is_not(None))
        )
        inactive_doctors = await db_session.execute(
            select(func.count())
            .select_from(Doctor)
            .where(Doctor.hospital_id == hospital.id, Doctor.deleted_at.is_not(None))
        )
        assert int(inactive_patients.scalar_one()) == 1
        assert int(inactive_doctors.scalar_one()) == 1


class TestRelationships:
    """Every reference the seed creates points somewhere valid and in-tenant."""

    async def test_doctors_are_attached_to_users_and_departments_in_the_same_hospital(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        result = await db_session.execute(select(Doctor).where(Doctor.hospital_id == hospital.id))
        doctors = list(result.unique().scalars().all())

        department_ids = await db_session.execute(
            select(Department.id).where(Department.hospital_id == hospital.id)
        )
        valid_departments = set(department_ids.scalars().all())

        assert doctors
        for doctor in doctors:
            assert doctor.user_id is not None
            assert doctor.department_id in valid_departments

    async def test_appointments_reference_seeded_patients_and_doctors(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        patient_ids = await db_session.execute(
            select(Patient.id).where(Patient.hospital_id == hospital.id)
        )
        doctor_ids = await db_session.execute(
            select(Doctor.id).where(Doctor.hospital_id == hospital.id)
        )
        valid_patients = set(patient_ids.scalars().all())
        valid_doctors = set(doctor_ids.scalars().all())

        result = await db_session.execute(
            select(Appointment).where(Appointment.hospital_id == hospital.id)
        )
        for appointment in result.unique().scalars().all():
            assert appointment.patient_id in valid_patients
            assert appointment.doctor_id in valid_doctors
            assert appointment.scheduled_end > appointment.scheduled_start

    async def test_every_appointment_carries_its_status_history(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        result = await db_session.execute(
            select(Appointment).where(Appointment.hospital_id == hospital.id)
        )
        # Queried rather than walked through the relationship: it is
        # ``lazy="raise"``, so a stray N+1 in production code fails loudly.
        for appointment in result.unique().scalars().all():
            rows = await db_session.execute(
                select(AppointmentStatusHistory)
                .where(AppointmentStatusHistory.appointment_id == appointment.id)
                .order_by(AppointmentStatusHistory.changed_at)
            )
            history = list(rows.unique().scalars().all())
            assert history, "an appointment with no history has no auditable trail"
            assert history[0].from_status is None
            assert history[0].to_status is AppointmentStatus.BOOKED
            assert history[-1].to_status is appointment.status


class TestIdempotency:
    """Running the seed again must not duplicate, renumber, or re-point anything."""

    async def test_second_run_creates_nothing(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        first = await _counts(db_session, hospital.id)

        await seed_demo_data(db_session, hospital, {})
        second = await _counts(db_session, hospital.id)

        assert second == first

    async def test_second_run_does_not_advance_the_mrn_counter(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        # Re-running must not burn MRNs: the series is per-hospital and a gap in
        # it is indistinguishable from a lost patient record.
        await seed_demo_data(db_session, hospital, {})
        before = await db_session.execute(
            select(MrnSequence.current_value).where(MrnSequence.hospital_id == hospital.id)
        )
        first_value = before.scalar_one()

        await seed_demo_data(db_session, hospital, {})
        after = await db_session.execute(
            select(MrnSequence.current_value).where(MrnSequence.hospital_id == hospital.id)
        )

        assert after.scalar_one() == first_value

    async def test_second_run_keeps_the_same_row_identities(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        # Stable ids matter beyond duplicate counts: a bookmarked patient URL or
        # an open appointment must survive a re-seed.
        await seed_demo_data(db_session, hospital, {})
        before = await db_session.execute(
            select(Patient.id, Patient.mrn).where(Patient.hospital_id == hospital.id)
        )
        first = set(before.all())

        await seed_demo_data(db_session, hospital, {})
        after = await db_session.execute(
            select(Patient.id, Patient.mrn).where(Patient.hospital_id == hospital.id)
        )

        assert set(after.all()) == first

    async def test_seeding_a_second_hospital_does_not_touch_the_first(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital_id: uuid.UUID,
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        first = await _counts(db_session, hospital.id)

        other = await db_session.execute(select(Hospital).where(Hospital.id == other_hospital_id))
        await seed_demo_data(db_session, other.unique().scalar_one(), {})

        assert await _counts(db_session, hospital.id) == first
        assert await _counts(db_session, other_hospital_id) == first
