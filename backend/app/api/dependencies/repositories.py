"""Dependency injection wiring for every repository.

One provider per repository. Routes never depend on these directly — services
do. They are declared here so that the composition root stays in one place.

Placement rule (``docs/09-PROJECT_STRUCTURE.md``): every new repository added
under ``app/repositories/`` gets a provider in this module.

Usage::

    from typing import Annotated

    from fastapi import Depends

    from app.api.dependencies import get_user_repository
    from app.repositories import UserRepository

    async def handler(
        users: Annotated[UserRepository, Depends(get_user_repository)],
    ):
        ...
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.db import get_db_session
from app.repositories import (
    AppointmentRepository,
    AuditLogRepository,
    DepartmentRepository,
    DoctorRepository,
    HospitalRepository,
    MrnSequenceRepository,
    PasswordResetTokenRepository,
    PatientRepository,
    PermissionRepository,
    RefreshTokenRepository,
    RoleRepository,
    UserRepository,
)

#: Request-scoped database session, injected into every repository provider.
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_audit_log_repository(session: DbSession) -> AuditLogRepository:
    """Provide an :class:`AuditLogRepository` bound to the request session."""
    return AuditLogRepository(session)


def get_hospital_repository(session: DbSession) -> HospitalRepository:
    """Provide a :class:`HospitalRepository` bound to the request session."""
    return HospitalRepository(session)


def get_user_repository(session: DbSession) -> UserRepository:
    """Provide a :class:`UserRepository` bound to the request session."""
    return UserRepository(session)


def get_role_repository(session: DbSession) -> RoleRepository:
    """Provide a :class:`RoleRepository` bound to the request session."""
    return RoleRepository(session)


def get_permission_repository(session: DbSession) -> PermissionRepository:
    """Provide a :class:`PermissionRepository` bound to the request session."""
    return PermissionRepository(session)


def get_refresh_token_repository(session: DbSession) -> RefreshTokenRepository:
    """Provide a :class:`RefreshTokenRepository` bound to the request session."""
    return RefreshTokenRepository(session)


def get_patient_repository(session: DbSession) -> PatientRepository:
    """Provide a :class:`PatientRepository` bound to the request session."""
    return PatientRepository(session)


def get_mrn_sequence_repository(session: DbSession) -> MrnSequenceRepository:
    """Provide an :class:`MrnSequenceRepository` bound to the request session."""
    return MrnSequenceRepository(session)


def get_password_reset_token_repository(session: DbSession) -> PasswordResetTokenRepository:
    """Provide a :class:`PasswordResetTokenRepository` bound to the request session."""
    return PasswordResetTokenRepository(session)


def get_department_repository(session: DbSession) -> DepartmentRepository:
    """Provide a :class:`DepartmentRepository` bound to the request session."""
    return DepartmentRepository(session)


def get_doctor_repository(session: DbSession) -> DoctorRepository:
    """Provide a :class:`DoctorRepository` bound to the request session."""
    return DoctorRepository(session)


def get_appointment_repository(session: DbSession) -> AppointmentRepository:
    """Provide an :class:`AppointmentRepository` bound to the request session."""
    return AppointmentRepository(session)
