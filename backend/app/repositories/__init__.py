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
from app.repositories.auth_throttle_repository import (
    AuthThrottleRepository,
    TrustedDeviceRepository,
)
from app.repositories.base import BaseRepository
from app.repositories.department_repository import DepartmentRepository
from app.repositories.doctor_repository import DoctorRepository
from app.repositories.hospital_repository import HospitalRepository
from app.repositories.inventory_po_repository import InventoryPurchaseOrderRepository
from app.repositories.inventory_repository import InventoryRepository
from app.repositories.invoice_number_sequence_repository import InvoiceNumberSequenceRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.lab_order_repository import LabOrderRepository
from app.repositories.lab_test_repository import LabTestRepository
from app.repositories.medicine_repository import MedicineRepository
from app.repositories.mrn_sequence_repository import MrnSequenceRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
from app.repositories.patient_access_grant_repository import PatientAccessGrantRepository
from app.repositories.patient_account_link_repository import PatientAccountLinkRepository
from app.repositories.patient_account_repository import PatientAccountRepository
from app.repositories.patient_consent_repository import PatientConsentRepository
from app.repositories.patient_device_repository import PatientDeviceRepository
from app.repositories.patient_otp_challenge_repository import PatientOtpChallengeRepository
from app.repositories.patient_refresh_token_repository import PatientRefreshTokenRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.permission_repository import PermissionRepository
from app.repositories.prescription_repository import PrescriptionRepository
from app.repositories.procurement_repository import ProcurementRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.report_repository import ReportRepository
from app.repositories.role_repository import RoleRepository
from app.repositories.service_catalog_repository import ServiceCatalogRepository
from app.repositories.user_repository import UserRepository

__all__ = [
    "AuthThrottleRepository",
    "TrustedDeviceRepository",
    "AppointmentRepository",
    "AuditLogRepository",
    "BaseRepository",
    "DepartmentRepository",
    "DoctorRepository",
    "HospitalRepository",
    "InventoryPurchaseOrderRepository",
    "InventoryRepository",
    "InvoiceNumberSequenceRepository",
    "InvoiceRepository",
    "LabOrderRepository",
    "LabTestRepository",
    "MedicineRepository",
    "MrnSequenceRepository",
    "NotificationRepository",
    "PatientAccessGrantRepository",
    "PatientAccountLinkRepository",
    "PatientAccountRepository",
    "PatientConsentRepository",
    "PatientDeviceRepository",
    "PatientOtpChallengeRepository",
    "PatientRefreshTokenRepository",
    "PatientRepository",
    "PasswordResetTokenRepository",
    "PermissionRepository",
    "PrescriptionRepository",
    "ProcurementRepository",
    "RefreshTokenRepository",
    "ReportRepository",
    "RoleRepository",
    "ServiceCatalogRepository",
    "UserRepository",
]
