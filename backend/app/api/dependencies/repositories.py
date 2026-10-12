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
    AuthThrottleRepository,
    DepartmentRepository,
    DoctorRepository,
    HospitalRepository,
    InventoryPurchaseOrderRepository,
    InventoryRepository,
    InvoiceNumberSequenceRepository,
    InvoiceRepository,
    LabOrderRepository,
    LabTestRepository,
    MedicineRepository,
    MrnSequenceRepository,
    NotificationRepository,
    PasswordResetTokenRepository,
    PatientAccessGrantRepository,
    PatientAccountLinkRepository,
    PatientAccountRepository,
    PatientConsentRepository,
    PatientDeviceRepository,
    PatientOtpChallengeRepository,
    PatientRefreshTokenRepository,
    PatientRepository,
    PermissionRepository,
    PrescriptionRepository,
    ProcurementRepository,
    RefreshTokenRepository,
    ReportRepository,
    RoleRepository,
    ServiceCatalogRepository,
    TrustedDeviceRepository,
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


def get_auth_throttle_repository(session: DbSession) -> AuthThrottleRepository:
    """Provide an :class:`AuthThrottleRepository` bound to the request session."""
    return AuthThrottleRepository(session)


def get_trusted_device_repository(session: DbSession) -> TrustedDeviceRepository:
    """Provide a :class:`TrustedDeviceRepository` bound to the request session."""
    return TrustedDeviceRepository(session)


def get_refresh_token_repository(session: DbSession) -> RefreshTokenRepository:
    """Provide a :class:`RefreshTokenRepository` bound to the request session."""
    return RefreshTokenRepository(session)


def get_patient_repository(session: DbSession) -> PatientRepository:
    """Provide a :class:`PatientRepository` bound to the request session."""
    return PatientRepository(session)


def get_patient_account_repository(session: DbSession) -> PatientAccountRepository:
    """Provide a :class:`PatientAccountRepository` bound to the request session."""
    return PatientAccountRepository(session)


def get_patient_otp_challenge_repository(session: DbSession) -> PatientOtpChallengeRepository:
    """Provide a :class:`PatientOtpChallengeRepository` bound to the request session."""
    return PatientOtpChallengeRepository(session)


def get_patient_refresh_token_repository(session: DbSession) -> PatientRefreshTokenRepository:
    """Provide a :class:`PatientRefreshTokenRepository` bound to the request session."""
    return PatientRefreshTokenRepository(session)


def get_patient_device_repository(session: DbSession) -> PatientDeviceRepository:
    """Provide a :class:`PatientDeviceRepository` bound to the request session."""
    return PatientDeviceRepository(session)


def get_patient_account_link_repository(session: DbSession) -> PatientAccountLinkRepository:
    """Provide a :class:`PatientAccountLinkRepository` bound to the request session."""
    return PatientAccountLinkRepository(session)


def get_patient_consent_repository(session: DbSession) -> PatientConsentRepository:
    """Provide a :class:`PatientConsentRepository` bound to the request session."""
    return PatientConsentRepository(session)


def get_patient_access_grant_repository(session: DbSession) -> PatientAccessGrantRepository:
    """Provide a :class:`PatientAccessGrantRepository` bound to the request session."""
    return PatientAccessGrantRepository(session)


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


def get_service_catalog_repository(session: DbSession) -> ServiceCatalogRepository:
    """Provide a :class:`ServiceCatalogRepository` bound to the request session."""
    return ServiceCatalogRepository(session)


def get_invoice_repository(session: DbSession) -> InvoiceRepository:
    """Provide an :class:`InvoiceRepository` bound to the request session."""
    return InvoiceRepository(session)


def get_invoice_number_sequence_repository(
    session: DbSession,
) -> InvoiceNumberSequenceRepository:
    """Provide an :class:`InvoiceNumberSequenceRepository` bound to the request session."""
    return InvoiceNumberSequenceRepository(session)


def get_lab_test_repository(session: DbSession) -> LabTestRepository:
    """Provide a :class:`LabTestRepository` bound to the request session."""
    return LabTestRepository(session)


def get_lab_order_repository(session: DbSession) -> LabOrderRepository:
    """Provide a :class:`LabOrderRepository` bound to the request session."""
    return LabOrderRepository(session)


def get_medicine_repository(session: DbSession) -> MedicineRepository:
    """Provide a :class:`MedicineRepository` bound to the request session."""
    return MedicineRepository(session)


def get_prescription_repository(session: DbSession) -> PrescriptionRepository:
    """Provide a :class:`PrescriptionRepository` bound to the request session."""
    return PrescriptionRepository(session)


def get_procurement_repository(session: DbSession) -> ProcurementRepository:
    """Provide a :class:`ProcurementRepository` bound to the request session."""
    return ProcurementRepository(session)


def get_inventory_repository(session: DbSession) -> InventoryRepository:
    """Provide an :class:`InventoryRepository` bound to the request session."""
    return InventoryRepository(session)


def get_inventory_po_repository(session: DbSession) -> InventoryPurchaseOrderRepository:
    """Provide an :class:`InventoryPurchaseOrderRepository` bound to the request session."""
    return InventoryPurchaseOrderRepository(session)


def get_report_repository(session: DbSession) -> ReportRepository:
    """Provide a :class:`ReportRepository` bound to the request session."""
    return ReportRepository(session)


def get_notification_repository(session: DbSession) -> NotificationRepository:
    """Provide a :class:`NotificationRepository` bound to the request session."""
    return NotificationRepository(session)
