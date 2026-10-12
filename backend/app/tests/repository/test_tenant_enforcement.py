"""Tenant isolation against a real PostgreSQL — both enforcement layers.

Layer 1 is the explicit scope every base-repository primitive takes. Layer 2
is the ambient scope the session itself enforces (``app/core/tenancy.py``).
Each is tested on its own, so that a regression in one cannot hide behind the
other.

Two hospitals, ``A`` and ``B``, each with the same shape of data. Every test
asks one question: can something acting for ``A`` reach a row of ``B``?
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update

from app.core.tenancy import (
    CrossTenantAccessError,
    TenantScope,
    TenantScopeRequiredError,
    cross_tenant,
    tenant_scope,
)
from app.models.appointment import Appointment, AppointmentStatus, AppointmentType
from app.models.audit_log import AuditLog
from app.models.doctor import Doctor
from app.models.hospital import Hospital
from app.models.patient import Gender, Patient
from app.models.role import Role
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.hospital_repository import HospitalRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.role_repository import RoleRepository
from app.repositories.user_repository import UserRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database


@dataclass(frozen=True)
class _Tenant:
    """One hospital's worth of rows."""

    hospital_id: uuid.UUID
    user_id: uuid.UUID
    doctor_id: uuid.UUID
    patient_id: uuid.UUID
    appointment_id: uuid.UUID
    role_id: uuid.UUID
    audit_id: uuid.UUID


async def _build_tenant(session: AsyncSession, hospital_id: uuid.UUID, label: str) -> _Tenant:
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"tenancy-{label}-{uuid.uuid4().hex[:8]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Staff",
        last_name=label,
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
    patient = Patient(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        mrn=f"MRN-{uuid.uuid4().hex[:8]}",
        first_name="Patient",
        last_name=label,
        date_of_birth=date(1990, 1, 1),
        gender=Gender.FEMALE,
    )
    role = Role(id=uuid.uuid4(), hospital_id=hospital_id, name=f"role-{label}")
    audit = AuditLog(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        actor_user_id=user.id,
        actor_type="user",
        action="tenancy.test",
    )
    session.add_all([doctor, patient, role, audit])
    await session.flush()

    start = datetime(2031, 1, 6, 9, 0, tzinfo=UTC)
    appointment = Appointment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=15),
        status=AppointmentStatus.BOOKED,
        type=AppointmentType.NEW,
    )
    session.add(appointment)
    await session.flush()

    return _Tenant(
        hospital_id=hospital_id,
        user_id=user.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_id=appointment.id,
        role_id=role.id,
        audit_id=audit.id,
    )


@pytest_asyncio.fixture
async def a(db_session: AsyncSession, hospital_id: uuid.UUID) -> _Tenant:
    """Hospital A."""
    return await _build_tenant(db_session, hospital_id, "a")


@pytest_asyncio.fixture
async def b(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> _Tenant:
    """Hospital B."""
    return await _build_tenant(db_session, other_hospital_id, "b")


@pytest_asyncio.fixture
async def clean_session(db_session: AsyncSession, a: _Tenant, b: _Tenant) -> AsyncSession:
    """The test session with nothing cached, so every read really hits the database."""
    db_session.expunge_all()
    return db_session


# ── Layer 1: explicit scope on the base repository ───────────────────────────


class TestExplicitScopeRead:
    async def test_a_row_is_found_inside_its_own_hospital(
        self, clean_session: AsyncSession, a: _Tenant
    ) -> None:
        found = await UserRepository(clean_session).get_by_id(a.user_id, a.hospital_id)
        assert found is not None
        assert found.id == a.user_id

    async def test_another_hospitals_row_is_not_found_by_id(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        assert await UserRepository(clean_session).get_by_id(b.user_id, a.hospital_id) is None
        assert await PatientRepository(clean_session).get_by_id(b.patient_id, a.hospital_id) is None
        assert await AuditLogRepository(clean_session).get_by_id(b.audit_id, a.hospital_id) is None

    async def test_get_by_ids_drops_another_hospitals_rows(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        found = await UserRepository(clean_session).get_by_ids(
            [a.user_id, b.user_id], a.hospital_id
        )
        assert [user.id for user in found] == [a.user_id]

    async def test_list_returns_only_the_hospitals_own_rows(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        listed = await PatientRepository(clean_session).list(scope=a.hospital_id)
        assert {patient.id for patient in listed} == {a.patient_id}

    async def test_count_and_exists_are_confined(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        repository = PatientRepository(clean_session)
        assert await repository.count(scope=a.hospital_id) == 1
        assert await repository.exists(a.patient_id, a.hospital_id) is True
        assert await repository.exists(b.patient_id, a.hospital_id) is False

    async def test_a_hospital_sees_shared_roles_but_never_another_hospitals(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        shared = Role(id=uuid.uuid4(), hospital_id=None, name="shared-role", is_system=True)
        clean_session.add(shared)
        await clean_session.flush()
        clean_session.expunge_all()

        repository = RoleRepository(clean_session)
        assert await repository.get_by_id(a.role_id, a.hospital_id) is not None
        assert await repository.get_by_id(shared.id, a.hospital_id) is not None
        assert await repository.get_by_id(b.role_id, a.hospital_id) is None


class TestExplicitScopeWrite:
    async def test_update_by_pk_cannot_touch_another_hospitals_row(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        repository = PatientRepository(clean_session)

        assert await repository.update_by_pk(b.patient_id, a.hospital_id, occupation="x") is None

        clean_session.expunge_all()
        untouched = await repository.get_by_id(b.patient_id, b.hospital_id)
        assert untouched is not None
        assert untouched.occupation is None

    async def test_update_by_pk_works_inside_the_hospital(
        self, clean_session: AsyncSession, a: _Tenant
    ) -> None:
        updated = await PatientRepository(clean_session).update_by_pk(
            a.patient_id, a.hospital_id, occupation="Teacher"
        )
        assert updated is not None
        assert updated.occupation == "Teacher"

    async def test_the_tenant_column_cannot_be_rewritten(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        repository = PatientRepository(clean_session)
        patient = await repository.get_by_id(a.patient_id, a.hospital_id)
        assert patient is not None

        with pytest.raises(CrossTenantAccessError):
            await repository.update(patient, hospital_id=b.hospital_id)
        with pytest.raises(CrossTenantAccessError):
            await repository.update_by_pk(a.patient_id, a.hospital_id, hospital_id=b.hospital_id)

    async def test_a_tenant_row_cannot_be_created_without_a_hospital(
        self, clean_session: AsyncSession
    ) -> None:
        with pytest.raises(TenantScopeRequiredError):
            await PatientRepository(clean_session).create(
                mrn="MRN-NONE",
                first_name="No",
                last_name="Tenant",
                date_of_birth=date(1990, 1, 1),
                gender=Gender.MALE,
            )


class TestMissingTenantFailsSafely:
    """A missing tenant is an error. It never becomes an unrestricted query."""

    async def test_every_read_primitive_refuses_to_run_unscoped(
        self, clean_session: AsyncSession, a: _Tenant
    ) -> None:
        repository = PatientRepository(clean_session)

        with pytest.raises(TenantScopeRequiredError):
            await repository.get_by_id(a.patient_id)
        with pytest.raises(TenantScopeRequiredError):
            await repository.get_by_ids([a.patient_id])
        with pytest.raises(TenantScopeRequiredError):
            await repository.list()
        with pytest.raises(TenantScopeRequiredError):
            await repository.count()
        with pytest.raises(TenantScopeRequiredError):
            await repository.exists(a.patient_id)

    async def test_update_by_pk_refuses_to_run_unscoped(
        self, clean_session: AsyncSession, a: _Tenant
    ) -> None:
        with pytest.raises(TenantScopeRequiredError):
            await PatientRepository(clean_session).update_by_pk(a.patient_id, occupation="x")

    async def test_role_lookups_refuse_to_run_unscoped(
        self, clean_session: AsyncSession, a: _Tenant
    ) -> None:
        repository = RoleRepository(clean_session)
        with pytest.raises(TenantScopeRequiredError):
            await repository.get_by_id(a.role_id)
        with pytest.raises(TenantScopeRequiredError):
            await repository.get_with_permissions(a.role_id)

    async def test_models_with_no_tenant_column_need_no_scope(
        self, clean_session: AsyncSession, a: _Tenant
    ) -> None:
        """Tokens, permissions and the hospital table itself are not tenant-scoped."""
        assert await RefreshTokenRepository(clean_session).get_by_id(uuid.uuid4()) is None
        hospital = await HospitalRepository(clean_session).get_by_id(a.hospital_id)
        assert hospital is not None


class TestLegitimateCrossTenantPaths:
    """Platform-level and system work still functions — when it is asked for by name."""

    async def test_identity_resolution_finds_a_user_in_any_hospital(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        repository = UserRepository(clean_session)
        marker = cross_tenant("resolve the principal named by a verified token")

        assert (await repository.get_by_id(a.user_id, marker)) is not None
        assert (await repository.get_by_id(b.user_id, marker)) is not None

    async def test_a_platform_administrator_can_read_any_hospitals_role(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        repository = RoleRepository(clean_session)
        marker = cross_tenant("platform-level administrator")

        assert await repository.get_by_id(a.role_id, marker) is not None
        assert await repository.get_with_permissions(b.role_id, hospital_id=marker) is not None

    async def test_a_system_scope_sees_every_hospital(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        with tenant_scope(TenantScope.system("platform maintenance")):
            rows = await clean_session.execute(
                select(Patient.id).where(Patient.id.in_([a.patient_id, b.patient_id]))
            )
        assert set(rows.scalars()) == {a.patient_id, b.patient_id}

    async def test_login_lookup_by_email_still_crosses_hospitals(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        user_b = await clean_session.get(User, b.user_id)
        assert user_b is not None
        found = await UserRepository(clean_session).get_by_email_cross_tenant(user_b.email)
        assert found is not None
        assert found.hospital_id == b.hospital_id

    async def test_a_cross_tenant_marker_does_not_lift_a_bound_hospital(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        """Inside hospital A's request, even an explicit cross-tenant read stays in A."""
        repository = UserRepository(clean_session)
        marker = cross_tenant("identity lookup")

        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            assert await repository.get_by_id(a.user_id, marker) is not None
            assert await repository.get_by_id(b.user_id, marker) is None


# ── Layer 2: the ambient scope, with no help from the repository ─────────────


class TestAmbientScopeRead:
    """Statements written with **no** tenant filter are still confined."""

    async def test_an_unfiltered_entity_query_returns_only_the_bound_hospital(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        stmt = select(Patient).where(Patient.id.in_([a.patient_id, b.patient_id]))

        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            rows = (await clean_session.execute(stmt)).scalars().all()

        assert [patient.id for patient in rows] == [a.patient_id]

    async def test_the_same_statement_is_refiltered_for_each_hospital(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        """A cached statement must not carry the previous hospital's filter."""
        stmt = select(Patient.id).where(Patient.id.in_([a.patient_id, b.patient_id]))

        seen: dict[uuid.UUID, list[uuid.UUID]] = {}
        for tenant in (a, b, a, b):
            with tenant_scope(TenantScope.hospital(tenant.hospital_id)):
                seen[tenant.hospital_id] = list((await clean_session.execute(stmt)).scalars())

        assert seen == {a.hospital_id: [a.patient_id], b.hospital_id: [b.patient_id]}

    async def test_a_lookup_that_passes_the_wrong_hospital_finds_nothing(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        """The client-chosen-hospital case: the bound scope wins over the argument."""
        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            found = await PatientRepository(clean_session).get_patient_by_id(
                b.hospital_id, b.patient_id
            )
        assert found is None

    async def test_column_aggregate_and_subquery_reads_are_confined(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        ids = [a.patient_id, b.patient_id]
        subquery = select(Patient).where(Patient.id.in_(ids)).subquery()

        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            names = (
                await clean_session.execute(select(Patient.last_name).where(Patient.id.in_(ids)))
            ).scalars()
            direct = await clean_session.scalar(
                select(func.count(Patient.id)).where(Patient.id.in_(ids))
            )
            wrapped = await clean_session.scalar(select(func.count()).select_from(subquery))

            assert list(names) == ["a"]
            assert direct == 1
            assert wrapped == 1

    async def test_session_get_does_not_return_another_hospitals_row(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            assert await clean_session.get(Patient, b.patient_id) is None
            assert await clean_session.get(Patient, a.patient_id) is not None

    async def test_the_hospital_table_is_confined_to_the_bound_hospital(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            rows = await clean_session.execute(
                select(Hospital.id).where(Hospital.id.in_([a.hospital_id, b.hospital_id]))
            )
            assert list(rows.scalars()) == [a.hospital_id]

    async def test_rows_with_no_hospital_stay_visible(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        """A shared role belongs to no hospital, so it is nobody else's row."""
        shared = Role(id=uuid.uuid4(), hospital_id=None, name="shared-ambient", is_system=True)
        clean_session.add(shared)
        await clean_session.flush()
        clean_session.expunge_all()

        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            rows = await clean_session.execute(
                select(Role.id).where(Role.id.in_([shared.id, a.role_id, b.role_id]))
            )
            assert set(rows.scalars()) == {shared.id, a.role_id}

    async def test_with_nothing_bound_the_session_is_not_filtered(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        """Unbound, the explicit repository scope is the control (Layer 1)."""
        rows = await clean_session.execute(
            select(Patient.id).where(Patient.id.in_([a.patient_id, b.patient_id]))
        )
        assert set(rows.scalars()) == {a.patient_id, b.patient_id}


class TestAmbientScopeNestedRelationships:
    async def test_a_join_cannot_reach_into_another_hospital(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        """An appointment joined to its patient and doctor never crosses the boundary."""
        stmt = (
            select(Appointment.id, Patient.id, Doctor.id)
            .join(Patient, Patient.id == Appointment.patient_id)
            .join(Doctor, Doctor.id == Appointment.doctor_id)
            .where(Appointment.id.in_([a.appointment_id, b.appointment_id]))
        )

        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            rows = (await clean_session.execute(stmt)).all()

        assert [tuple(row) for row in rows] == [(a.appointment_id, a.patient_id, a.doctor_id)]

    async def test_a_related_row_of_another_hospital_is_not_loaded(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        """A foreign key pointing across hospitals yields nothing on the far side.

        The row below is deliberately corrupt — hospital A's appointment naming
        hospital B's patient — which the application never writes. If it ever
        existed, loading it for A must not hand back B's patient.
        """
        start = datetime(2031, 1, 7, 9, 0, tzinfo=UTC)
        crossed = Appointment(
            id=uuid.uuid4(),
            hospital_id=a.hospital_id,
            patient_id=b.patient_id,
            doctor_id=a.doctor_id,
            scheduled_start=start,
            scheduled_end=start + timedelta(minutes=15),
            status=AppointmentStatus.BOOKED,
            type=AppointmentType.NEW,
        )
        clean_session.add(crossed)
        await clean_session.flush()
        clean_session.expunge_all()

        stmt = (
            select(Patient.id)
            .join(Appointment, Appointment.patient_id == Patient.id)
            .where(Appointment.id == crossed.id)
        )
        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            assert (await clean_session.execute(stmt)).all() == []


class TestAmbientScopeWrite:
    async def test_a_bulk_update_cannot_touch_another_hospital(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        stmt = (
            update(Patient)
            .where(Patient.id.in_([a.patient_id, b.patient_id]))
            .values(occupation="changed")
        )
        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            await clean_session.execute(stmt)

        clean_session.expunge_all()
        rows = await clean_session.execute(
            select(Patient.id, Patient.occupation).where(
                Patient.id.in_([a.patient_id, b.patient_id])
            )
        )
        occupations = {patient_id: occupation for patient_id, occupation in rows.all()}
        assert occupations == {a.patient_id: "changed", b.patient_id: None}

    async def test_a_bulk_delete_cannot_touch_another_hospital(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        stmt = delete(AuditLog).where(AuditLog.id.in_([a.audit_id, b.audit_id]))
        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            await clean_session.execute(stmt)

        remaining = await clean_session.execute(
            select(AuditLog.id).where(AuditLog.id.in_([a.audit_id, b.audit_id]))
        )
        assert list(remaining.scalars()) == [b.audit_id]

    async def test_a_new_row_for_another_hospital_is_refused(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            clean_session.add(
                Patient(
                    id=uuid.uuid4(),
                    hospital_id=b.hospital_id,
                    mrn="MRN-CROSS",
                    first_name="Wrong",
                    last_name="Hospital",
                    date_of_birth=date(1990, 1, 1),
                    gender=Gender.MALE,
                )
            )
            with pytest.raises(CrossTenantAccessError):
                await clean_session.flush()
        clean_session.expunge_all()

    async def test_changing_another_hospitals_loaded_row_is_refused(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        """A row loaded before the scope was bound still cannot be written under it."""
        foreign = await clean_session.get(Patient, b.patient_id)
        assert foreign is not None

        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            foreign.occupation = "tampered"
            with pytest.raises(CrossTenantAccessError):
                await clean_session.flush()
        clean_session.expunge_all()

    async def test_soft_deleting_another_hospitals_row_is_refused(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        foreign = await clean_session.get(Patient, b.patient_id)
        assert foreign is not None

        with (
            tenant_scope(TenantScope.hospital(a.hospital_id)),
            pytest.raises(CrossTenantAccessError),
        ):
            await PatientRepository(clean_session).delete_patient(foreign, deleted_by=a.user_id)
        clean_session.expunge_all()

        still_there = await PatientRepository(clean_session).get_by_id(b.patient_id, b.hospital_id)
        assert still_there is not None

    async def test_a_row_can_never_be_moved_between_hospitals(
        self, clean_session: AsyncSession, a: _Tenant, b: _Tenant
    ) -> None:
        """True in every scope — bound, system, or none at all."""
        patient = await clean_session.get(Patient, a.patient_id)
        assert patient is not None

        patient.hospital_id = b.hospital_id
        with pytest.raises(CrossTenantAccessError):
            await clean_session.flush()
        clean_session.expunge_all()

    async def test_writes_inside_the_bound_hospital_still_work(
        self, clean_session: AsyncSession, a: _Tenant
    ) -> None:
        with tenant_scope(TenantScope.hospital(a.hospital_id)):
            repository = PatientRepository(clean_session)
            patient = await repository.get_patient_by_id(a.hospital_id, a.patient_id)
            assert patient is not None
            await repository.update_patient(patient, updated_by=a.user_id, occupation="Nurse")
            await repository.delete_patient(patient, deleted_by=a.user_id)

        clean_session.expunge_all()
        assert await PatientRepository(clean_session).get_by_id(a.patient_id, a.hospital_id) is None
