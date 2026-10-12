"""Helpers shared by the Patient App API and integration tests.

The Patient App is driven the way a patient's browser drives it: over HTTP,
through the real application, the real PostgreSQL throttle and the real
cookies. Two things are stood in for:

* the SMS transport — :class:`FakeSmsSender` keeps what it was asked to send
  in memory, injected through the ``get_sms_sender`` dependency. The
  development sender is never used in a test;
* the database session — every request shares the test's rolled-back session.

Rows that belong to another module (hospitals, patient records) are inserted
directly rather than through the owning services, so that a Patient App test
does not fail because staff registration validation changed.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.dependencies.db import get_db_session
from app.api.dependencies.patient import get_sms_sender
from app.core.feature_flags import PATIENT_APP_ENABLED
from app.core.sms import SmsDeliveryError, SmsMessage
from app.main import create_app
from app.models.appointment import Appointment, AppointmentStatus, AppointmentType
from app.models.audit_log import AuditLog
from app.models.department import Department
from app.models.doctor import Doctor, DoctorAvailability, DoctorLeave
from app.models.hospital import Hospital
from app.models.patient import Gender, Patient
from app.models.user import User, UserStatus
from app.repositories.auth_throttle_repository import AuthThrottleRepository
from app.services.patient_app.policies import DRAFT_VERSION

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import pytest
    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CSRF",
    "DEVICE_COOKIE",
    "PATIENT",
    "POLICY",
    "REFRESH_COOKIE",
    "FakeSmsSender",
    "ThrottleClock",
    "audit_rows",
    "bearer",
    "build_patient_application",
    "insert_appointment",
    "insert_department",
    "insert_doctor",
    "insert_hospital",
    "insert_leave",
    "insert_patient_record",
    "new_phone",
    "open_hospital",
    "set_windows",
    "patient_client",
    "set_cookie_headers",
    "sign_in",
]

PATIENT = "/api/v1/patient"
#: The header the cookie-authenticated endpoints require.
CSRF = {"X-Atheris-Patient": "1"}
#: Cookie names as sent without ``Secure`` (the suite talks plain HTTP).
REFRESH_COOKIE = "atheris-patient-refresh"
DEVICE_COOKIE = "atheris-patient-device"
#: The current version of every policy.
POLICY = DRAFT_VERSION

_CODE = re.compile(r"\b(\d{6})\b")


class FakeSmsSender:
    """An in-memory :class:`~app.core.sms.SmsSender`.

    :ivar sent: Every message it was asked to send, in order.
    :ivar fail: When set, the next sends raise :class:`SmsDeliveryError`.
    """

    def __init__(self) -> None:
        self.sent: list[SmsMessage] = []
        self.fail = False

    async def send(self, message: SmsMessage) -> None:
        """Record the message, or fail as a provider would."""
        if self.fail:
            raise SmsDeliveryError
        self.sent.append(message)

    def code_for(self, phone: str) -> str:
        """The code in the most recent message sent to a number."""
        for message in reversed(self.sent):
            if message.to == phone:
                match = _CODE.search(message.body)
                assert match is not None, "the message carries no six-digit code"
                return match.group(1)
        msg = f"no message was sent to {phone}"
        raise AssertionError(msg)


class ThrottleClock:
    """Moves the throttle's clock — which also times one-time codes — forward."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.offset = timedelta(0)
        real_now = AuthThrottleRepository.now
        clock = self

        async def _now(repository: AuthThrottleRepository) -> datetime:
            return await real_now(repository) + clock.offset

        monkeypatch.setattr(AuthThrottleRepository, "now", _now)

    def advance(self, **delta: float) -> None:
        """Move time forward."""
        self.offset += timedelta(**delta)


def new_phone() -> str:
    """A random, well-formed Indian mobile number in E.164 form."""
    return f"+91{6 + secrets.randbelow(4)}{secrets.randbelow(10**9):09d}"


def build_patient_application(session: AsyncSession, sms: FakeSmsSender | None) -> FastAPI:
    """The real application on the test session, with the SMS transport stood in.

    :param session: The test's rolled-back session.
    :param sms: The fake sender, or ``None`` for "SMS is not configured".
    """
    application = create_app()

    async def _session() -> AsyncGenerator[AsyncSession]:
        yield session

    application.dependency_overrides[get_db_session] = _session
    application.dependency_overrides[get_sms_sender] = lambda: sms
    return application


def patient_client(application: FastAPI, **kwargs: Any) -> AsyncClient:
    """A browser: an HTTP client with its own cookie jar."""
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test", **kwargs)


def bearer(token: str) -> dict[str, str]:
    """An ``Authorization`` header carrying an access token."""
    return {"Authorization": f"Bearer {token}"}


def set_cookie_headers(response: Response) -> list[str]:
    """Every ``Set-Cookie`` header of a response."""
    return response.headers.get_list("set-cookie")


async def sign_in(client: AsyncClient, sms: FakeSmsSender, phone: str) -> dict[str, Any]:
    """Request a code, verify it, and return the session data of the response.

    The client's cookie jar keeps the refresh and device cookies.
    """
    requested = await client.post(f"{PATIENT}/auth/otp/request", json={"phone": phone})
    assert requested.status_code == 202, requested.text
    challenge_id = requested.json()["data"]["challenge_id"]
    verified = await client.post(
        f"{PATIENT}/auth/otp/verify",
        json={"challenge_id": challenge_id, "code": sms.code_for(phone)},
    )
    assert verified.status_code == 200, verified.text
    data: dict[str, Any] = verified.json()["data"]
    return data


async def insert_hospital(
    session: AsyncSession,
    *,
    name: str,
    slug: str | None = None,
    address: Any = None,
    enabled: Any = True,
    is_active: bool = True,
    settings: dict[str, Any] | None = None,
    **columns: Any,
) -> Hospital:
    """Insert a hospital as the platform would have set it up, for discovery tests.

    :param enabled: The stored value of the Patient App flag. ``True`` opens
        the hospital; anything else — including ``"true"`` and ``1`` — is
        stored as given. ``None`` leaves the flag out altogether.
    :param settings: Other settings to store next to the flag.
    :param columns: Any other column (``phone``, ``logo_url``, ``tax_id``, …).
    """
    stored = dict(settings or {})
    if enabled is not None:
        stored[PATIENT_APP_ENABLED] = enabled
    hospital = Hospital(
        id=uuid.uuid4(),
        name=name,
        slug=slug or f"h-{uuid.uuid4().hex[:16]}",
        address={"line1": "1 Test Road", "city": "Hyderabad"} if address is None else address,
        settings=stored,
        is_active=is_active,
        **columns,
    )
    session.add(hospital)
    await session.flush()
    await session.commit()
    return hospital


async def insert_doctor(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    first_name: str = "Asha",
    last_name: str = "Menon",
    specialization: str = "Cardiology",
    department_id: uuid.UUID | None = None,
    available: bool = True,
    deleted: bool = False,
    user_status: str = "active",
    user_deleted: bool = False,
    **columns: Any,
) -> Doctor:
    """Insert a doctor as hospital staff would have set one up, for discovery tests.

    :param available: Whether the doctor has an availability window.
    :param deleted: Whether the doctor is deactivated.
    :param user_status: The status of the user row behind the doctor.
    :param user_deleted: Whether that user row is soft-deleted.
    :param columns: Any other doctor column (``bio``, ``languages``, ``qualifications``, …).
    """
    tag = uuid.uuid4().hex[:12]
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"doctor-{tag}@staff-secret.test",
        phone="+919000000001",
        password_hash="test-placeholder-not-a-hash",
        first_name=first_name,
        last_name=last_name,
        status=UserStatus(user_status),
    )
    if user_deleted:
        user.deleted_at = datetime.now(UTC)
    session.add(user)
    await session.flush()
    doctor = Doctor(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        user_id=user.id,
        specialization=specialization,
        department_id=department_id,
        license_number=f"LICCANARY-{tag}",
        **columns,
    )
    if deleted:
        doctor.deleted_at = datetime.now(UTC)
    session.add(doctor)
    await session.flush()
    if available:
        session.add(
            DoctorAvailability(
                id=uuid.uuid4(),
                doctor_id=doctor.id,
                hospital_id=hospital_id,
                day_of_week=0,
                start_time=time(9, 0),
                end_time=time(12, 0),
                slot_duration_minutes=15,
            )
        )
        await session.flush()
    await session.commit()
    return doctor


async def set_windows(
    session: AsyncSession, doctor: Doctor, windows: list[tuple[int, time, time, int]]
) -> None:
    """Replace a doctor's weekly windows: ``(day_of_week, start, end, slot_minutes)`` each."""
    from sqlalchemy import delete

    await session.execute(
        delete(DoctorAvailability).where(DoctorAvailability.doctor_id == doctor.id)
    )
    for day_of_week, start, end, minutes in windows:
        session.add(
            DoctorAvailability(
                id=uuid.uuid4(),
                doctor_id=doctor.id,
                hospital_id=doctor.hospital_id,
                day_of_week=day_of_week,
                start_time=start,
                end_time=end,
                slot_duration_minutes=minutes,
            )
        )
    await session.flush()
    await session.commit()


async def insert_leave(
    session: AsyncSession, doctor: Doctor, starts_at: datetime, ends_at: datetime
) -> DoctorLeave:
    """Put a doctor on leave between two instants."""
    leave = DoctorLeave(
        id=uuid.uuid4(),
        doctor_id=doctor.id,
        hospital_id=doctor.hospital_id,
        starts_at=starts_at,
        ends_at=ends_at,
        reason="LEAVECANARY private reason",
    )
    session.add(leave)
    await session.flush()
    await session.commit()
    return leave


async def insert_appointment(
    session: AsyncSession,
    doctor: Doctor,
    patient_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime,
    *,
    status: AppointmentStatus = AppointmentStatus.BOOKED,
    hospital_id: uuid.UUID | None = None,
) -> Appointment:
    """Book a doctor for another patient, in any lifecycle state."""
    appointment = Appointment(
        id=uuid.uuid4(),
        hospital_id=hospital_id or doctor.hospital_id,
        patient_id=patient_id,
        doctor_id=doctor.id,
        scheduled_start=starts_at,
        scheduled_end=ends_at,
        status=status,
        type=AppointmentType.NEW,
        reason="APPTCANARY another patient's reason",
        notes="NOTESCANARY reception notes",
    )
    session.add(appointment)
    await session.flush()
    await session.commit()
    return appointment


async def insert_department(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    name: str,
    deleted: bool = False,
    **columns: Any,
) -> Department:
    """Insert a department, for discovery tests."""
    department = Department(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        code=f"D{uuid.uuid4().hex[:8].upper()}",
        name=name,
        **columns,
    )
    if deleted:
        department.deleted_at = datetime.now(UTC)
    session.add(department)
    await session.flush()
    await session.commit()
    return department


async def open_hospital(
    session: AsyncSession, hospital_id: uuid.UUID, *, enabled: bool = True
) -> Hospital:
    """Switch the Patient App on (or explicitly off) for a hospital, and return it."""
    result = await session.execute(select(Hospital).where(Hospital.id == hospital_id))
    hospital = result.unique().scalar_one()
    # Reassigned rather than mutated in place so SQLAlchemy sees the JSONB change.
    hospital.settings = {**(hospital.settings or {}), PATIENT_APP_ENABLED: enabled}
    await session.flush()
    await session.commit()
    return hospital


async def insert_patient_record(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    phone: str | None,
    date_of_birth: date = date(1990, 5, 17),
    mrn: str | None = None,
    deleted: bool = False,
    first_name: str = "Asha",
    last_name: str = "Verma",
) -> Patient:
    """Insert a patient record as hospital staff would have registered it."""
    patient = Patient(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        mrn=mrn or f"MRN-T-{uuid.uuid4().hex[:10].upper()}",
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        gender=Gender.FEMALE,
        phone=phone,
    )
    if deleted:
        patient.deleted_at = datetime.now(UTC)
    session.add(patient)
    await session.flush()
    await session.commit()
    return patient


async def audit_rows(session: AsyncSession, action: str) -> list[AuditLog]:
    """Every audit row of one action, oldest first, read fresh from the database."""
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.action == action)
        .order_by(AuditLog.created_at, AuditLog.id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())
