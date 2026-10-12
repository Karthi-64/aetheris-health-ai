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
from app.models.auth_throttle import AuthThrottleBucket, TrustedDevice
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
    Refund,
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
from app.models.inventory import (
    InventoryItem,
    InventoryLocation,
    InventoryLocationKind,
    InventoryMovement,
    InventoryMovementReason,
    InventoryPurchaseOrder,
    InventoryPurchaseOrderItem,
    InventoryStock,
)
from app.models.lab import (
    LabOrder,
    LabOrderItem,
    LabOrderPriority,
    LabOrderStatus,
    LabResultAmendment,
    LabResultFlag,
    LabResultType,
    LabTest,
)
from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationPreference,
)
from app.models.password_reset_token import PasswordResetToken
from app.models.patient import BloodGroup, Gender, MrnSequence, Patient, PatientStatus
from app.models.patient_account import (
    LinkRelationship,
    LinkVerification,
    PatientAccount,
    PatientAccountLink,
    PatientAccountStatus,
    PatientDevice,
    PatientOtpChallenge,
    PatientRefreshToken,
)
from app.models.patient_consent import (
    ConsentPurpose,
    GranteeType,
    GrantPurposeNote,
    GrantStatus,
    PatientAccessGrant,
    PatientConsentRecord,
    RecordCategory,
)
from app.models.permission import Permission
from app.models.pharmacy import (
    Dispense,
    DispenseItem,
    Medicine,
    MedicineBatch,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    StockMovement,
    StockMovementReason,
    Vendor,
)
from app.models.refresh_token import RefreshToken
from app.models.role import Role, RolePermission
from app.models.user import User, UserRole, UserStatus

__all__ = [
    "AuthThrottleBucket",
    "TrustedDevice",
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
    # Patient App
    "ConsentPurpose",
    "GrantPurposeNote",
    "GrantStatus",
    "GranteeType",
    "LinkRelationship",
    "LinkVerification",
    "PatientAccessGrant",
    "PatientAccount",
    "PatientAccountLink",
    "PatientAccountStatus",
    "PatientConsentRecord",
    "PatientDevice",
    "PatientOtpChallenge",
    "PatientRefreshToken",
    "RecordCategory",
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
    "Refund",
    "Service",
    # Inventory
    "InventoryItem",
    "InventoryLocation",
    "InventoryLocationKind",
    "InventoryMovement",
    "InventoryMovementReason",
    "InventoryPurchaseOrder",
    "InventoryPurchaseOrderItem",
    "InventoryStock",
    # Laboratory
    "LabOrder",
    "LabOrderItem",
    "LabOrderPriority",
    "LabOrderStatus",
    "LabResultAmendment",
    "LabResultFlag",
    "LabResultType",
    "LabTest",
    # Pharmacy
    "Dispense",
    "DispenseItem",
    "Medicine",
    "MedicineBatch",
    "Prescription",
    "PrescriptionItem",
    "PrescriptionStatus",
    "PurchaseOrder",
    "PurchaseOrderItem",
    "PurchaseOrderStatus",
    "StockMovement",
    "StockMovementReason",
    "Vendor",
    # Notification
    "DeliveryStatus",
    "Notification",
    "NotificationChannel",
    "NotificationDelivery",
    "NotificationPreference",
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
