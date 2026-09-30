"""Data access layer — one repository per aggregate root.

Repositories run queries and return domain objects. They contain **no**
business logic, and a repository **never** calls another repository — the
service layer composes across aggregates.

All repositories inherit from :class:`BaseRepository`, which provides
type-safe async CRUD with soft-delete awareness and pagination.

Usage::

    from app.repositories import UserRepository

    repo = UserRepository(session)
    user = await repo.get_by_email(hospital_id, "doctor@hospital.test")

Placement rule (``docs/09-PROJECT_STRUCTURE.md``): a new repository goes in
``app/repositories/<domain>_repository.py`` and is wired for DI in
``app/api/dependencies/repositories.py``.
"""

from app.repositories.appointment_repository import AppointmentRepository
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.base import BaseRepository
from app.repositories.department_repository import DepartmentRepository
from app.repositories.doctor_repository import DoctorRepository
from app.repositories.hospital_repository import HospitalRepository
from app.repositories.mrn_sequence_repository import MrnSequenceRepository
from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.permission_repository import PermissionRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.role_repository import RoleRepository
from app.repositories.user_repository import UserRepository

__all__ = [
    "AppointmentRepository",
    "AuditLogRepository",
    "BaseRepository",
    "DepartmentRepository",
    "DoctorRepository",
    "HospitalRepository",
    "MrnSequenceRepository",
    "PatientRepository",
    "PasswordResetTokenRepository",
    "PermissionRepository",
    "RefreshTokenRepository",
    "RoleRepository",
    "UserRepository",
]
