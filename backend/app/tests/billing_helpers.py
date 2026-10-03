"""Database row helpers shared by the billing repository, API and integration tests.

Billing sits downstream of patients, doctors and appointments, so almost every
database-backed billing test needs one of each to exist first. These helpers
insert the minimum valid row and return it, so the three suites build the same
world the same way rather than each hand-rolling its own.

They insert rows directly rather than going through the owning services: a
billing test should not fail because appointment booking validation changed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.appointment import Appointment, AppointmentStatus, AppointmentType
from app.models.doctor import Doctor
from app.models.patient import Gender, Patient
from app.models.user import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "auth_headers",
    "insert_appointment",
    "insert_doctor",
    "insert_patient",
    "insert_user",
    "insert_user_with_permissions",
]

#: A fixed past Monday 09:00 UTC, so a "completed" appointment is plausible.
_VISIT_START = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


async def insert_user(session: AsyncSession, hospital_id: uuid.UUID) -> User:
    """Insert an active user with no roles.

    :param session: The test session.
    :param hospital_id: Hospital the user belongs to.
    :returns: The persisted user.
    """
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"billing-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Asha",
        last_name="Menon",
    )
    session.add(user)
    await session.flush()
    return user


async def insert_patient(
    session: AsyncSession, hospital_id: uuid.UUID, *, first_name: str = "Ananya"
) -> Patient:
    """Insert a patient.

    :param session: The test session.
    :param hospital_id: Hospital the patient belongs to.
    :param first_name: Given name, for tests that need to tell patients apart.
    :returns: The persisted patient.
    """
    patient = Patient(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        mrn=f"MRN-{uuid.uuid4().hex[:8]}",
        first_name=first_name,
        last_name="Rao",
        date_of_birth=date(1990, 1, 1),
        gender=Gender.FEMALE,
    )
    session.add(patient)
    await session.flush()
    return patient


async def insert_doctor(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    consultation_fee: str = "800.00",
    user_id: uuid.UUID | None = None,
) -> Doctor:
    """Insert a doctor, with the user account behind it.

    :param session: The test session.
    :param hospital_id: Hospital the doctor belongs to.
    :param consultation_fee: The fee an auto-drafted invoice should bill.
    :param user_id: Attach the profile to this existing user — for tests where
        the doctor has to log in. A fresh user is created when omitted.
    :returns: The persisted doctor.
    """
    if user_id is None:
        user_id = (await insert_user(session, hospital_id)).id
    doctor = Doctor(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        user_id=user_id,
        specialization="Cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:8]}",
        consultation_fee=Decimal(consultation_fee),
    )
    session.add(doctor)
    await session.flush()
    return doctor


async def insert_appointment(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    patient_id: uuid.UUID,
    doctor_id: uuid.UUID,
    status: AppointmentStatus = AppointmentStatus.COMPLETED,
    offset_minutes: int = 0,
) -> Appointment:
    """Insert an appointment in a given status.

    :param session: The test session.
    :param hospital_id: Hospital the appointment belongs to.
    :param patient_id: Patient being seen.
    :param doctor_id: Doctor seeing them.
    :param status: Lifecycle status to insert in. Defaults to ``completed``,
        which is the state billing normally meets an appointment in.
    :param offset_minutes: Shift from the default start, so one test can insert
        several appointments for one doctor without tripping the no-overlap
        constraint.
    :returns: The persisted appointment.
    """
    start = _VISIT_START + timedelta(minutes=offset_minutes)
    appointment = Appointment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=15),
        status=status,
        type=AppointmentType.NEW,
    )
    session.add(appointment)
    await session.flush()
    return appointment


async def insert_user_with_permissions(
    session: AsyncSession, hospital_id: uuid.UUID, permissions: list[str]
) -> User:
    """Insert an active user holding exactly ``permissions``.

    :param session: The test session.
    :param hospital_id: Hospital the user belongs to.
    :param permissions: Permission codes to grant. Empty means none.
    :returns: The persisted user.
    """
    from app.tests.conftest import grant_permissions

    user = await insert_user(session, hospital_id)
    if permissions:
        await grant_permissions(
            session, hospital_id=hospital_id, user_id=user.id, codes=permissions
        )
    return user


def auth_headers(user_id: uuid.UUID, hospital_id: uuid.UUID | None) -> dict[str, str]:
    """Mint a Bearer header for a user.

    :param user_id: The user the token is for.
    :param hospital_id: Their hospital, or ``None`` for a Super Admin.
    :returns: An ``Authorization`` header dict.
    """
    from app.core.security import create_access_token

    token = create_access_token(user_id=user_id, hospital_id=hospital_id)
    return {"Authorization": f"Bearer {token}"}
