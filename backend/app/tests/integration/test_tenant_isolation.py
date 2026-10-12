"""Tenant isolation end to end: real requests, real jobs, real PostgreSQL.

``app/tests/repository/test_tenant_enforcement.py`` proves the two enforcement
layers in isolation. This module proves they are actually wired in:

* an authenticated request is confined to the principal's hospital, and that
  hospital comes from the user row — not from anything the client sends;
* the scheduled jobs handle each row inside that row's own hospital;
* a platform-level principal still gets an explicit, unconfined scope.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.dependencies.auth import get_current_user
from app.api.dependencies.db import get_db_session
from app.core.security import create_access_token
from app.core.tenancy import (
    CrossTenant,
    CrossTenantAccessError,
    TenantScope,
    current_tenant_scope,
    tenant_scope,
)
from app.main import create_app
from app.models.appointment import Appointment, AppointmentStatus, AppointmentType
from app.models.doctor import Doctor
from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
)
from app.models.patient import Gender, Patient
from app.models.user import User, UserStatus
from app.repositories.appointment_repository import AppointmentRepository
from app.repositories.doctor_repository import DoctorRepository
from app.repositories.hospital_repository import HospitalRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.user_repository import UserRepository
from app.services.appointment_service import AppointmentService, NullInvoiceDraftSink
from app.services.notification_service import NotificationService
from app.tests.conftest import RecordingAuditSink, grant_permissions
from app.tests.factories import build_appointment_payload

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.email import EmailMessage

pytestmark = pytest.mark.database

PATIENTS = "/api/v1/patients"
APPOINTMENTS = "/api/v1/appointments"
USERS = "/api/v1/users"

STAFF_PERMISSIONS = [
    "patient.read",
    "patient.create",
    "patient.update",
    "patient.delete",
    "appointment.read",
    "appointment.book",
    "appointment.book_override",
    "appointment.cancel",
    "user.read",
    "user.update",
]

#: Well in the past, so both appointments are overdue for any grace period.
OVERDUE_START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
SWEEP_AT = OVERDUE_START + timedelta(hours=6)


# ── World building ───────────────────────────────────────────────────────────


async def _staff(session: AsyncSession, hospital_id: uuid.UUID) -> User:
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"iso-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Iso",
        last_name="Staff",
    )
    session.add(user)
    await session.flush()
    await grant_permissions(
        session, hospital_id=hospital_id, user_id=user.id, codes=STAFF_PERMISSIONS
    )
    return user


async def _patient(session: AsyncSession, hospital_id: uuid.UUID, last_name: str) -> Patient:
    patient = Patient(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        mrn=f"MRN-{uuid.uuid4().hex[:8]}",
        first_name="Patient",
        last_name=last_name,
        date_of_birth=date(1990, 1, 1),
        gender=Gender.FEMALE,
    )
    session.add(patient)
    await session.flush()
    return patient


async def _doctor(session: AsyncSession, hospital_id: uuid.UUID) -> Doctor:
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"iso-doc-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Iso",
        last_name="Doctor",
    )
    session.add(user)
    await session.flush()
    doctor = Doctor(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        user_id=user.id,
        specialization="General Medicine",
        license_number=f"LIC-{uuid.uuid4().hex[:8]}",
    )
    session.add(doctor)
    await session.flush()
    return doctor


async def _overdue_appointment(session: AsyncSession, hospital_id: uuid.UUID) -> Appointment:
    patient = await _patient(session, hospital_id, "Overdue")
    doctor = await _doctor(session, hospital_id)
    appointment = Appointment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        scheduled_start=OVERDUE_START,
        scheduled_end=OVERDUE_START + timedelta(minutes=15),
        status=AppointmentStatus.BOOKED,
        type=AppointmentType.NEW,
    )
    session.add(appointment)
    await session.flush()
    return appointment


async def _queued_email(
    session: AsyncSession, hospital_id: uuid.UUID, address: str
) -> NotificationDelivery:
    user = await _staff(session, hospital_id)
    notification = Notification(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        recipient_user_id=user.id,
        kind="tenancy.test",
        title="Test",
        body="Test body",
    )
    session.add(notification)
    await session.flush()
    delivery = NotificationDelivery(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        notification_id=notification.id,
        channel=NotificationChannel.EMAIL,
        status=DeliveryStatus.QUEUED,
        to_address=address,
        subject="Test",
        body="Test body",
        next_attempt_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(delivery)
    await session.flush()
    return delivery


def _bearer(user: User, *, claimed_hospital: uuid.UUID | None = None) -> dict[str, str]:
    """Mint a Bearer header. ``claimed_hospital`` lets a test lie in the claim."""
    token = create_access_token(user_id=user.id, hospital_id=claimed_hospital or user.hospital_id)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """An HTTP client whose requests share the test's rolled-back session."""
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def staff_a(db_session: AsyncSession, hospital_id: uuid.UUID) -> User:
    """A fully permitted staff member of hospital A."""
    return await _staff(db_session, hospital_id)


@pytest_asyncio.fixture
async def staff_b(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> User:
    """A fully permitted staff member of hospital B."""
    return await _staff(db_session, other_hospital_id)


@pytest_asyncio.fixture
async def patient_a(db_session: AsyncSession, hospital_id: uuid.UUID) -> Patient:
    """A patient of hospital A."""
    return await _patient(db_session, hospital_id, "Alpha")


@pytest_asyncio.fixture
async def patient_b(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> Patient:
    """A patient of hospital B."""
    return await _patient(db_session, other_hospital_id, "Bravo")


# ── 1–4: read, update, list, delete across hospitals, over HTTP ──────────────


class TestCrossHospitalRequests:
    async def test_a_resource_of_another_hospital_cannot_be_read_by_id(
        self, api: AsyncClient, staff_a: User, patient_a: Patient, patient_b: Patient
    ) -> None:
        own = await api.get(f"{PATIENTS}/{patient_a.id}", headers=_bearer(staff_a))
        foreign = await api.get(f"{PATIENTS}/{patient_b.id}", headers=_bearer(staff_a))

        assert own.status_code == 200, own.text
        assert foreign.status_code == 404

    async def test_a_resource_of_another_hospital_cannot_be_updated(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        staff_a: User,
        patient_b: Patient,
    ) -> None:
        response = await api.patch(
            f"{PATIENTS}/{patient_b.id}",
            headers=_bearer(staff_a),
            json={"occupation": "tampered"},
        )

        assert response.status_code == 404
        await db_session.refresh(patient_b)
        assert patient_b.occupation is None

    async def test_a_list_never_includes_another_hospitals_records(
        self, api: AsyncClient, staff_a: User, patient_a: Patient, patient_b: Patient
    ) -> None:
        response = await api.get(PATIENTS, headers=_bearer(staff_a))

        assert response.status_code == 200, response.text
        listed = {row["id"] for row in response.json()["data"]}
        assert str(patient_a.id) in listed
        assert str(patient_b.id) not in listed

    async def test_a_resource_of_another_hospital_cannot_be_deactivated(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        staff_a: User,
        patient_b: Patient,
    ) -> None:
        response = await api.delete(f"{PATIENTS}/{patient_b.id}", headers=_bearer(staff_a))

        assert response.status_code == 404
        await db_session.refresh(patient_b)
        assert patient_b.deleted_at is None

    async def test_a_user_of_another_hospital_cannot_be_read(
        self, api: AsyncClient, staff_a: User, staff_b: User
    ) -> None:
        response = await api.get(f"{USERS}/{staff_b.id}", headers=_bearer(staff_a))
        assert response.status_code == 404

    async def test_each_hospital_still_reads_its_own_data(
        self,
        api: AsyncClient,
        staff_a: User,
        staff_b: User,
        patient_a: Patient,
        patient_b: Patient,
    ) -> None:
        """The boundary is symmetric and does not get in the way of normal use."""
        for staff, own in ((staff_a, patient_a), (staff_b, patient_b), (staff_a, patient_a)):
            response = await api.get(f"{PATIENTS}/{own.id}", headers=_bearer(staff))
            assert response.status_code == 200, response.text
            assert response.json()["data"]["id"] == str(own.id)


class TestTheHospitalComesFromThePrincipal:
    async def test_a_token_claiming_another_hospital_changes_nothing(
        self, api: AsyncClient, staff_a: User, staff_b: User, patient_a: Patient, patient_b: Patient
    ) -> None:
        """The claim is ignored: the hospital is read from the verified user's row."""
        lying = _bearer(staff_a, claimed_hospital=staff_b.hospital_id)

        listing = await api.get(PATIENTS, headers=lying)
        foreign = await api.get(f"{PATIENTS}/{patient_b.id}", headers=lying)

        assert listing.status_code == 200, listing.text
        listed = {row["id"] for row in listing.json()["data"]}
        assert str(patient_a.id) in listed
        assert str(patient_b.id) not in listed
        assert foreign.status_code == 404

    async def test_a_hospital_named_in_the_query_string_is_not_honoured(
        self, api: AsyncClient, staff_a: User, staff_b: User, patient_b: Patient
    ) -> None:
        response = await api.get(
            PATIENTS, headers=_bearer(staff_a), params={"hospital_id": str(staff_b.hospital_id)}
        )

        # Either the unknown parameter is ignored or it is rejected; in neither
        # case does hospital B's patient come back.
        if response.status_code == 200:
            assert str(patient_b.id) not in {row["id"] for row in response.json()["data"]}
        else:
            assert response.status_code == 422

    async def test_the_request_scope_does_not_leak_out_of_the_request(
        self, api: AsyncClient, staff_a: User, patient_a: Patient
    ) -> None:
        response = await api.get(f"{PATIENTS}/{patient_a.id}", headers=_bearer(staff_a))

        assert response.status_code == 200
        assert current_tenant_scope() is None


# ── 5: nested relationships ──────────────────────────────────────────────────


class TestNestedRelationships:
    async def test_an_appointment_cannot_name_another_hospitals_patient(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        staff_a: User,
        patient_b: Patient,
        hospital_id: uuid.UUID,
    ) -> None:
        doctor_a = await _doctor(db_session, hospital_id)

        response = await api.post(
            APPOINTMENTS,
            headers={**_bearer(staff_a), "Idempotency-Key": uuid.uuid4().hex},
            json=build_appointment_payload(
                patient_id=str(patient_b.id), doctor_id=str(doctor_a.id)
            ),
        )

        assert response.status_code in {400, 404, 422}, response.text
        booked = await db_session.execute(
            select(Appointment.id).where(Appointment.patient_id == patient_b.id)
        )
        assert booked.all() == []

    async def test_an_appointment_cannot_name_another_hospitals_doctor(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        staff_a: User,
        patient_a: Patient,
        other_hospital_id: uuid.UUID,
    ) -> None:
        doctor_b = await _doctor(db_session, other_hospital_id)

        response = await api.post(
            APPOINTMENTS,
            headers={**_bearer(staff_a), "Idempotency-Key": uuid.uuid4().hex},
            json=build_appointment_payload(
                patient_id=str(patient_a.id), doctor_id=str(doctor_b.id)
            ),
        )

        assert response.status_code in {400, 404, 422}, response.text
        booked = await db_session.execute(
            select(Appointment.id).where(Appointment.doctor_id == doctor_b.id)
        )
        assert booked.all() == []

    async def test_another_hospitals_appointments_are_not_listed_for_a_patient(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        staff_a: User,
        other_hospital_id: uuid.UUID,
    ) -> None:
        foreign = await _overdue_appointment(db_session, other_hospital_id)

        response = await api.get(
            APPOINTMENTS,
            headers=_bearer(staff_a),
            params={"patient_id": str(foreign.patient_id)},
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"] == []


# ── 6: background jobs ───────────────────────────────────────────────────────


def _appointment_service(session: AsyncSession) -> AppointmentService:
    return AppointmentService(
        AppointmentRepository(session),
        PatientRepository(session),
        DoctorRepository(session),
        HospitalRepository(session),
        session,
        RecordingAuditSink(),
        NullInvoiceDraftSink(),
    )


class TestNoShowSweeper:
    async def test_each_appointment_is_changed_inside_its_own_hospital(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        in_a = await _overdue_appointment(db_session, hospital_id)
        in_b = await _overdue_appointment(db_session, other_hospital_id)

        bound_during_update: dict[uuid.UUID, TenantScope | None] = {}
        original = AppointmentRepository.update_appointment

        async def _spy(self: AppointmentRepository, appointment: Appointment, **fields: Any) -> Any:
            bound_during_update[appointment.id] = current_tenant_scope()
            return await original(self, appointment, **fields)

        monkeypatch.setattr(AppointmentRepository, "update_appointment", _spy)

        swept = await _appointment_service(db_session).sweep_no_shows(now=SWEEP_AT)

        assert swept >= 2
        assert bound_during_update[in_a.id] == TenantScope.hospital(hospital_id)
        assert bound_during_update[in_b.id] == TenantScope.hospital(other_hospital_id)
        # Nothing stays bound once the job returns.
        assert current_tenant_scope() is None

        for appointment in (in_a, in_b):
            await db_session.refresh(appointment)
            assert appointment.status is AppointmentStatus.NO_SHOW

    async def test_one_hospitals_unit_of_work_cannot_change_another_hospitals_row(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """If the job ever handled a row under the wrong hospital, it is refused."""
        in_b = await _overdue_appointment(db_session, other_hospital_id)
        service = _appointment_service(db_session)

        with (
            tenant_scope(TenantScope.hospital(hospital_id)),
            pytest.raises(CrossTenantAccessError),
        ):
            await service._sweep_one(in_b, SWEEP_AT)  # noqa: SLF001 — the unit of work under test
        db_session.expunge_all()

        reloaded = await db_session.get(Appointment, in_b.id)
        assert reloaded is not None
        assert reloaded.status is AppointmentStatus.BOOKED

    async def test_the_discovery_query_cannot_mix_hospitals_inside_a_hospital_scope(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        in_a = await _overdue_appointment(db_session, hospital_id)
        in_b = await _overdue_appointment(db_session, other_hospital_id)
        db_session.expunge_all()
        repository = AppointmentRepository(db_session)

        with tenant_scope(TenantScope.hospital(hospital_id)):
            confined = await repository.find_no_show_candidates(cutoff=SWEEP_AT)
        confined_ids = {appointment.id for appointment in confined}

        assert in_a.id in confined_ids
        assert in_b.id not in confined_ids


class _ScopeRecordingOutbox:
    """A mail transport that notes which hospital was bound for each send."""

    def __init__(self) -> None:
        self.bound: dict[str, TenantScope | None] = {}

    async def send(self, message: EmailMessage) -> None:
        self.bound[message.to] = current_tenant_scope()


class TestEmailQueue:
    async def test_each_delivery_is_handled_inside_its_own_hospital(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        to_a = await _queued_email(db_session, hospital_id, "a@hospital-a.test")
        to_b = await _queued_email(db_session, other_hospital_id, "b@hospital-b.test")

        users = UserRepository(db_session)
        service = NotificationService(
            NotificationRepository(db_session),
            users,
            HospitalRepository(db_session),
            db_session,
            RecordingAuditSink(),
        )
        outbox = _ScopeRecordingOutbox()

        report = await service.deliver_due_emails(
            outbox, now=datetime(2026, 6, 1, tzinfo=UTC), limit=500
        )

        assert report.sent >= 2
        assert outbox.bound["a@hospital-a.test"] == TenantScope.hospital(hospital_id)
        assert outbox.bound["b@hospital-b.test"] == TenantScope.hospital(other_hospital_id)
        assert current_tenant_scope() is None

        for delivery in (to_a, to_b):
            await db_session.refresh(delivery)
            assert delivery.status is DeliveryStatus.SENT


# ── 7: platform-level principals ─────────────────────────────────────────────


def _token_for(user_id: uuid.UUID) -> HTTPAuthorizationCredentials:
    token = create_access_token(user_id=user_id, hospital_id=None)
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def _scope_after_authenticating(user: Any) -> tuple[TenantScope | None, Any]:
    """Run ``get_current_user`` in its own task, as a request would, and report."""
    repository = AsyncMock()
    repository.get_by_id.return_value = user

    async def _request() -> TenantScope | None:
        await get_current_user(credentials=_token_for(user.id), user_repo=repository)
        return current_tenant_scope()

    return await asyncio.create_task(_request()), repository


class TestPrincipalScopeBinding:
    async def test_a_staff_principal_is_confined_to_its_hospital(self) -> None:
        staff = MagicMock(id=uuid.uuid4(), hospital_id=uuid.uuid4(), status=UserStatus.ACTIVE)

        scope, repository = await _scope_after_authenticating(staff)

        assert scope == TenantScope.hospital(staff.hospital_id)
        # Identity resolution is the explicit, named cross-tenant lookup.
        lookup_scope = repository.get_by_id.await_args.args[1]
        assert isinstance(lookup_scope, CrossTenant)

    async def test_a_platform_administrator_keeps_cross_hospital_access(self) -> None:
        """A principal with no hospital gets an explicit system scope, not a filter."""
        super_admin = MagicMock(id=uuid.uuid4(), hospital_id=None, status=UserStatus.ACTIVE)

        scope, _ = await _scope_after_authenticating(super_admin)

        assert scope is not None
        assert scope.is_system

    async def test_a_rejected_principal_binds_nothing(self) -> None:
        suspended = MagicMock(
            id=uuid.uuid4(), hospital_id=uuid.uuid4(), status=UserStatus.SUSPENDED
        )
        repository = AsyncMock()
        repository.get_by_id.return_value = suspended

        async def _request() -> TenantScope | None:
            from fastapi import HTTPException

            with pytest.raises(HTTPException):
                await get_current_user(credentials=_token_for(suspended.id), user_repo=repository)
            return current_tenant_scope()

        assert await asyncio.create_task(_request()) is None
