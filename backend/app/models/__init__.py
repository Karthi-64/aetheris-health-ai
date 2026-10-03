"""SQLAlchemy ORM models — one module per aggregate.

Importing this package registers every model with :attr:`Base.metadata`.
Alembic's ``migrations/env.py`` and any code that needs the full metadata
should import from here rather than from individual modules, so that no
mapper is left unconfigured.

Placement rule (``docs/09-PROJECT_STRUCTURE.md``): a new model goes in
``app/models/<domain>.py`` and is re-exported below.
"""

from app.models.appointment import (
    Appointment,
    AppointmentStatus,
    AppointmentStatusHistory,
    AppointmentType,
)
from app.models.audit_log import AuditLog
from app.models.base import (
    Base,
    CommonColumnsMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.models.billing import (
    Invoice,
    InvoiceItem,
    InvoiceNumberSequence,
    InvoiceStatus,
    Payment,
    PaymentMethod,
    Service,
)
from app.models.department import Department, DepartmentStatus
from app.models.doctor import (
    Doctor,
    DoctorAvailability,
    DoctorLeave,
    DoctorStatus,
    SlotStatus,
)
from app.models.hospital import Hospital
from app.models.password_reset_token import PasswordResetToken
from app.models.patient import BloodGroup, Gender, MrnSequence, Patient, PatientStatus
from app.models.permission import Permission
from app.models.refresh_token import RefreshToken
from app.models.role import Role, RolePermission
from app.models.user import User, UserRole, UserStatus

__all__ = [
    # Audit
    "AuditLog",
    # Base + mixins
    "Base",
    "CommonColumnsMixin",
    "SoftDeleteMixin",
    "TenantMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    # Identity
    "Hospital",
    "PasswordResetToken",
    "Permission",
    "RefreshToken",
    "Role",
    "RolePermission",
    "User",
    "UserRole",
    "UserStatus",
    # Patient
    "BloodGroup",
    "Gender",
    "MrnSequence",
    "Patient",
    "PatientStatus",
    # Appointment
    "Appointment",
    "AppointmentStatus",
    "AppointmentStatusHistory",
    "AppointmentType",
    # Billing
    "Invoice",
    "InvoiceItem",
    "InvoiceNumberSequence",
    "InvoiceStatus",
    "Payment",
    "PaymentMethod",
    "Service",
    # Department
    "Department",
    "DepartmentStatus",
    # Doctor
    "Doctor",
    "DoctorAvailability",
    "DoctorLeave",
    "DoctorStatus",
    "SlotStatus",
]
