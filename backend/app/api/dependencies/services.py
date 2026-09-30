"""Dependency injection wiring for every service.

One provider per service, each composing the repositories it needs. Routes
depend on these — never on repositories directly.

Usage::

    from typing import Annotated

    from fastapi import Depends

    from app.api.dependencies.services import get_auth_service
    from app.services.appointment_service import (
    AppointmentBookedIntervalSource,
    AppointmentService,
    InvoiceDraftSink,
    NullInvoiceDraftSink,
    SlotRanker,
)
from app.services.auth_service import AuthService

    async def handler(
        auth: Annotated[AuthService, Depends(get_auth_service)],
    ):
        ...
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.db import get_db_session

# ── Repository DI ────────────────────────────────────────────────────────────
# Re-exported here so routes and services import from a single dependencies
# module rather than picking between ``dependencies/`` sub-modules.
from app.api.dependencies.repositories import (  # noqa: F401
    DbSession,
    get_appointment_repository,
    get_audit_log_repository,
    get_department_repository,
    get_doctor_repository,
    get_hospital_repository,
    get_mrn_sequence_repository,
    get_password_reset_token_repository,
    get_patient_repository,
    get_permission_repository,
    get_refresh_token_repository,
    get_role_repository,
    get_user_repository,
)
from app.core.audit import AuditSink
from app.database.unit_of_work import UnitOfWork
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
from app.services.appointment_service import (
    AppointmentBookedIntervalSource,
    AppointmentService,
    InvoiceDraftSink,
    NullInvoiceDraftSink,
    SlotRanker,
)
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.department_service import DepartmentService, DepartmentUsageSource
from app.services.doctor_service import (
    BookedIntervalSource,
    DoctorDepartmentUsageSource,
    DoctorService,
)
from app.services.hospital_service import HospitalService
from app.services.mrn_service import MRNService
from app.services.patient_service import PatientService
from app.services.role_service import RoleService
from app.services.user_service import UserService

# ── Dependency type aliases ──────────────────────────────────────────────────
_Session = Annotated[AsyncSession, Depends(get_db_session)]


def get_unit_of_work(session: AsyncSession = Depends(get_db_session)) -> UnitOfWork:
    """Provide a :class:`UnitOfWork` over the request-scoped session.

    Services own the transaction boundary (``docs/03-ARCHITECTURE.md`` §9), so
    every service that writes takes one of these and commits through it.
    """
    return UnitOfWork(session)


# ── Auth service ────────────────────────────────────────────────────────────
def get_audit_sink(
    session: AsyncSession = Depends(get_db_session),
    audit_repo: AuditLogRepository = Depends(get_audit_log_repository),
    user_repo: UserRepository = Depends(get_user_repository),
) -> AuditSink:
    """Provide the audit sink every service records mutations to.

    Now that ``docs/modules/12-audit-logs.md`` ships, this is the
    database-backed :class:`~app.services.audit_service.AuditService` — which
    still emits the structlog line, so observability is unchanged and no
    service had to change (the seam in :mod:`app.core.audit` did its job).
    """
    return AuditService(session, audit_repo, user_repo)


def get_audit_service(
    session: AsyncSession = Depends(get_db_session),
    audit_repo: AuditLogRepository = Depends(get_audit_log_repository),
    user_repo: UserRepository = Depends(get_user_repository),
) -> AuditService:
    """Provide an :class:`AuditService` for the audit read endpoints."""
    return AuditService(session, audit_repo, user_repo)


# ── Hospital settings module ─────────────────────────────────────────────────
def get_hospital_service(
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    uow: UnitOfWork = Depends(get_unit_of_work),
    audit: AuditSink = Depends(get_audit_sink),
) -> HospitalService:
    """Provide a :class:`HospitalService` composed with its dependencies."""
    return HospitalService(hospitals=hospitals, uow=uow, audit=audit)


def get_auth_service(
    user_repo: UserRepository = Depends(get_user_repository),
    refresh_token_repo: RefreshTokenRepository = Depends(get_refresh_token_repository),
    password_reset_repo: PasswordResetTokenRepository = Depends(
        get_password_reset_token_repository
    ),
    uow: UnitOfWork = Depends(get_unit_of_work),
    audit: AuditSink = Depends(get_audit_sink),
) -> AuthService:
    """Provide an :class:`AuthService` composed with its repository dependencies."""
    return AuthService(
        user_repo=user_repo,
        refresh_token_repo=refresh_token_repo,
        password_reset_repo=password_reset_repo,
        uow=uow,
        audit=audit,
    )


# ── User service ────────────────────────────────────────────────────────────
def get_user_service(
    user_repo: UserRepository = Depends(get_user_repository),
    role_repo: RoleRepository = Depends(get_role_repository),
    permission_repo: PermissionRepository = Depends(get_permission_repository),
    auth_service: AuthService = Depends(get_auth_service),
    uow: UnitOfWork = Depends(get_unit_of_work),
    audit: AuditSink = Depends(get_audit_sink),
    password_reset_repo: PasswordResetTokenRepository = Depends(
        get_password_reset_token_repository
    ),
) -> UserService:
    """Provide a :class:`UserService` composed with its repository dependencies."""
    return UserService(
        user_repo=user_repo,
        role_repo=role_repo,
        permission_repo=permission_repo,
        auth_service=auth_service,
        uow=uow,
        audit=audit,
        password_reset_repo=password_reset_repo,
    )


# ── Roles & Permissions module ────────────────────────────────────────────────
def get_role_service(
    role_repo: RoleRepository = Depends(get_role_repository),
    permission_repo: PermissionRepository = Depends(get_permission_repository),
) -> RoleService:
    """Provide a :class:`RoleService` bound to the request session.

    Read-only in the MVP (``docs/modules/02-user-management.md`` §9), so no
    session or audit sink is needed — the two repositories share the
    request-scoped session already.
    """
    return RoleService(role_repo, permission_repo)


# ── Patient module ──────────────────────────────────────────────────────────
def get_mrn_service(
    sequences: MrnSequenceRepository = Depends(get_mrn_sequence_repository),
) -> MRNService:
    """Provide an :class:`MRNService` bound to the request session."""
    return MRNService(sequences)


def get_patient_service(
    patients: PatientRepository = Depends(get_patient_repository),
    mrn_service: MRNService = Depends(get_mrn_service),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
) -> PatientService:
    """Provide a :class:`PatientService` bound to the request session.

    All four collaborators share the same request-scoped session, so the
    service's ``commit()`` covers every write made through them.
    """
    return PatientService(patients, mrn_service, session, audit)


# ── Department module ───────────────────────────────────────────────────────
def get_department_usage_source(
    doctors: DoctorRepository = Depends(get_doctor_repository),
) -> DepartmentUsageSource:
    """Provide the source that answers how many doctors a department has.

    This is the swap the Department module was built for. It shipped against
    ``NullDepartmentUsageSource``, which reported zero because no ``doctors``
    table existed. Now that Doctor Management provides one, returning the real
    adapter activates business rule 13 — "a department cannot be deactivated
    while active doctors are assigned to it" — with **no change to any
    department module code**. The guard's tests were written against both
    branches when it shipped, so they cover this without modification.
    """
    return DoctorDepartmentUsageSource(doctors)


def get_department_service(
    departments: DepartmentRepository = Depends(get_department_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
    usage: DepartmentUsageSource = Depends(get_department_usage_source),
) -> DepartmentService:
    """Provide a :class:`DepartmentService` bound to the request session.

    The repository and the service share the same request-scoped session, so
    the service's ``commit()`` covers every write made through it.
    """
    return DepartmentService(departments, session, audit, usage)


# ── Doctor module ───────────────────────────────────────────────────────────
def get_booked_interval_source(
    appointments: AppointmentRepository = Depends(get_appointment_repository),
) -> BookedIntervalSource:
    """Provide the source of appointment facts slot generation needs.

    This is the swap Doctor Management was built for. It shipped against
    ``NullBookedIntervalSource``, which reported an empty calendar because no
    ``appointments`` table existed. Returning the real adapter now activates
    two things at once — slots reporting ``booked`` with their appointment id,
    and the FR-5 guard refusing to deactivate a doctor with future
    appointments — with **no change to any doctor module code**.
    """
    return AppointmentBookedIntervalSource(appointments)


def get_doctor_service(
    doctors: DoctorRepository = Depends(get_doctor_repository),
    users: UserRepository = Depends(get_user_repository),
    departments: DepartmentRepository = Depends(get_department_repository),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
    booked: BookedIntervalSource = Depends(get_booked_interval_source),
) -> DoctorService:
    """Provide a :class:`DoctorService` bound to the request session.

    Every repository shares the request-scoped session, so the service's
    ``commit()`` covers all writes. The user, department, and hospital
    repositories are here because the service orchestrates across aggregates —
    validating the linked user, the assigned department, and reading the
    hospital's timezone for slot generation. Repositories never call each
    other; the service composes (``backend/CLAUDE.md``, "The Layer Rule").
    """
    return DoctorService(doctors, users, departments, hospitals, session, audit, booked)


# ── Appointment module ──────────────────────────────────────────────────────
def get_invoice_draft_sink() -> InvoiceDraftSink:
    """Provide the sink completed appointments are handed to for invoicing.

    Returns the interim null implementation, which logs rather than drafts,
    because ``docs/modules/06-billing.md`` has not shipped. Billing swaps this
    one provider and :class:`AppointmentService` does not change.
    """
    return NullInvoiceDraftSink()


def get_slot_ranker() -> SlotRanker | None:
    """Provide the AI slot ranker, when the AI stack is configured.

    Returns ``None`` when the AI platform is unavailable, which makes
    ``POST /appointments/recommend-slot`` return an empty list instead of
    failing. Booking by hand must never depend on the AI stack being up
    (module spec §18 gates the feature behind a flag for the same reason).
    """
    try:
        from app.ai.prompts.registry import PromptRegistry
        from app.ai.providers import registry as provider_registry
        from app.ai.services.ai_service import AIService
        from app.services.slot_ranker import AISlotRanker

        prompts = PromptRegistry()
        prompts.load_all("app/ai/prompts/templates")
        return AISlotRanker(AIService(provider_registry, prompts), prompts)
    except Exception:  # noqa: BLE001 — an unconfigured AI stack is not an error here
        # No provider credentials, no templates on disk, or the AI package is
        # mid-refactor: none of that should stop a receptionist booking.
        return None


def get_appointment_service(
    appointments: AppointmentRepository = Depends(get_appointment_repository),
    patients: PatientRepository = Depends(get_patient_repository),
    doctors: DoctorRepository = Depends(get_doctor_repository),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
    invoices: InvoiceDraftSink = Depends(get_invoice_draft_sink),
    slot_ranker: SlotRanker | None = Depends(get_slot_ranker),
) -> AppointmentService:
    """Provide an :class:`AppointmentService` bound to the request session.

    Every repository shares the request-scoped session, so the service's
    ``commit()`` covers the appointment row and its status-history row together
    — which business rule 7 requires.
    """
    return AppointmentService(
        appointments, patients, doctors, hospitals, session, audit, invoices, slot_ranker
    )


__all__ = [
    # Re-exports from repositories
    "DbSession",
    "get_appointment_repository",
    "get_audit_log_repository",
    "get_department_repository",
    "get_doctor_repository",
    "get_hospital_repository",
    "get_password_reset_token_repository",
    "get_permission_repository",
    "get_refresh_token_repository",
    "get_role_repository",
    "get_user_repository",
    # Service providers
    "get_auth_service",
    "get_unit_of_work",
    "get_user_service",
    # Audit Logs module
    "get_audit_service",
    # Hospital settings module
    "get_hospital_service",
    # Roles & Permissions module
    "get_role_service",
    # Patient module
    "get_audit_sink",
    "get_mrn_service",
    "get_patient_service",
    # Department module
    "get_department_service",
    "get_department_usage_source",
    # Appointment module
    "get_appointment_service",
    "get_invoice_draft_sink",
    "get_slot_ranker",
    # Doctor module
    "get_booked_interval_source",
    "get_doctor_service",
]
