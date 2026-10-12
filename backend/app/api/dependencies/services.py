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
from app.services.auth_throttle import AuthThrottle

    async def handler(
        auth: Annotated[AuthService, Depends(get_auth_service)],
    ):
        ...
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.runtime import AIRuntime
from app.api.dependencies.ai import provide_ai_runtime
from app.api.dependencies.db import get_db_session

# ── Repository DI ────────────────────────────────────────────────────────────
# Re-exported here so routes and services import from a single dependencies
# module rather than picking between ``dependencies/`` sub-modules.
from app.api.dependencies.repositories import (  # noqa: F401
    DbSession,
    get_appointment_repository,
    get_audit_log_repository,
    get_auth_throttle_repository,
    get_department_repository,
    get_doctor_repository,
    get_hospital_repository,
    get_inventory_po_repository,
    get_inventory_repository,
    get_invoice_number_sequence_repository,
    get_invoice_repository,
    get_lab_order_repository,
    get_lab_test_repository,
    get_medicine_repository,
    get_mrn_sequence_repository,
    get_notification_repository,
    get_password_reset_token_repository,
    get_patient_repository,
    get_permission_repository,
    get_prescription_repository,
    get_procurement_repository,
    get_refresh_token_repository,
    get_report_repository,
    get_role_repository,
    get_service_catalog_repository,
    get_trusted_device_repository,
    get_user_repository,
)
from app.core.audit import AuditSink
from app.core.charges import ChargeSink
from app.core.notifications import Notifier
from app.database.unit_of_work import UnitOfWork
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
from app.services.appointment_service import (
    AppointmentBookedIntervalSource,
    AppointmentService,
    InvoiceDraftSink,
    SlotRanker,
)
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.auth_throttle import AuthThrottle
from app.services.billing_service import BillingInvoiceDraftSink, BillingService
from app.services.department_service import DepartmentService, DepartmentUsageSource
from app.services.dispensing_service import DispensingService
from app.services.doctor_service import (
    BookedIntervalSource,
    DoctorDepartmentUsageSource,
    DoctorService,
)
from app.services.hospital_service import HospitalService
from app.services.inventory_po_service import InventoryPurchaseOrderService
from app.services.inventory_service import InventoryService
from app.services.lab_catalog_service import LabCatalogService
from app.services.lab_service import LabService
from app.services.mrn_service import MRNService
from app.services.notification_service import NotificationService
from app.services.patient_service import PatientService
from app.services.pharmacy_catalog_service import PharmacyCatalogService
from app.services.procurement_service import ProcurementService
from app.services.report_service import ReportService
from app.services.role_service import RoleService
from app.services.service_catalog_service import ServiceCatalogService
from app.services.slot_ranker import AISlotRanker
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


# ── Notifications module ─────────────────────────────────────────────────────
def get_notification_service(
    notifications: NotificationRepository = Depends(get_notification_repository),
    users: UserRepository = Depends(get_user_repository),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
) -> NotificationService:
    """Provide a :class:`NotificationService` bound to the request session.

    It shares the request-scoped session with every other service, which is
    what makes a notification transactional with the event that caused it:
    both are written, or neither is.
    """
    return NotificationService(notifications, users, hospitals, session, audit)


def get_notifier(
    service: NotificationService = Depends(get_notification_service),
) -> Notifier:
    """Provide the :class:`~app.core.notifications.Notifier` other modules emit to.

    Other modules depend on the protocol, not on the Notifications module, so
    this provider is the only place the two are joined.
    """
    return service


# ── Hospital settings module ─────────────────────────────────────────────────
def get_hospital_service(
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    uow: UnitOfWork = Depends(get_unit_of_work),
    audit: AuditSink = Depends(get_audit_sink),
    runtime: AIRuntime = Depends(provide_ai_runtime),
) -> HospitalService:
    """Provide a :class:`HospitalService` composed with its dependencies."""
    return HospitalService(
        hospitals=hospitals, uow=uow, audit=audit, ai_configured=runtime.status.configured
    )


def get_auth_service(
    user_repo: UserRepository = Depends(get_user_repository),
    refresh_token_repo: RefreshTokenRepository = Depends(get_refresh_token_repository),
    password_reset_repo: PasswordResetTokenRepository = Depends(
        get_password_reset_token_repository
    ),
    uow: UnitOfWork = Depends(get_unit_of_work),
    audit: AuditSink = Depends(get_audit_sink),
    notifier: Notifier = Depends(get_notifier),
    throttle_buckets: AuthThrottleRepository = Depends(get_auth_throttle_repository),
    trusted_devices: TrustedDeviceRepository = Depends(get_trusted_device_repository),
) -> AuthService:
    """Provide an :class:`AuthService` composed with its repository dependencies."""
    return AuthService(
        user_repo=user_repo,
        refresh_token_repo=refresh_token_repo,
        password_reset_repo=password_reset_repo,
        uow=uow,
        audit=audit,
        throttle=AuthThrottle(throttle_buckets, uow),
        trusted_devices=trusted_devices,
        notifier=notifier,
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
    notifier: Notifier = Depends(get_notifier),
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
        notifier=notifier,
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


# ── Billing module ──────────────────────────────────────────────────────────
def get_service_catalog_service(
    services: ServiceCatalogRepository = Depends(get_service_catalog_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
) -> ServiceCatalogService:
    """Provide a :class:`ServiceCatalogService` bound to the request session."""
    return ServiceCatalogService(services, session, audit)


def get_billing_service(
    invoices: InvoiceRepository = Depends(get_invoice_repository),
    sequences: InvoiceNumberSequenceRepository = Depends(get_invoice_number_sequence_repository),
    catalog: ServiceCatalogRepository = Depends(get_service_catalog_repository),
    patients: PatientRepository = Depends(get_patient_repository),
    appointments: AppointmentRepository = Depends(get_appointment_repository),
    doctors: DoctorRepository = Depends(get_doctor_repository),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
    notifier: Notifier = Depends(get_notifier),
) -> BillingService:
    """Provide a :class:`BillingService` bound to the request session.

    Every repository shares the request-scoped session, so the service's
    ``commit()`` covers a payment row and the invoice balance it changes
    together, and the invoice-number counter advances in the same transaction
    as the issue it numbers — which is what keeps the series gap-free.
    """
    return BillingService(
        invoices,
        sequences,
        catalog,
        patients,
        appointments,
        doctors,
        hospitals,
        session,
        audit,
        notifier=notifier,
    )


def get_charge_sink(billing: BillingService = Depends(get_billing_service)) -> ChargeSink:
    """Provide the :class:`~app.core.charges.ChargeSink` other modules charge through.

    Laboratory and Pharmacy depend on the protocol, not on Billing, so they
    can be built and tested without it. Billing shares the request session, so
    a charge commits or rolls back with the order or dispense that raised it.
    """
    return billing


# ── Laboratory module ───────────────────────────────────────────────────────
def get_lab_catalog_service(
    tests: LabTestRepository = Depends(get_lab_test_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
) -> LabCatalogService:
    """Provide a :class:`LabCatalogService` bound to the request session."""
    return LabCatalogService(tests, session, audit)


def get_lab_service(
    orders: LabOrderRepository = Depends(get_lab_order_repository),
    tests: LabTestRepository = Depends(get_lab_test_repository),
    appointments: AppointmentRepository = Depends(get_appointment_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
    charges: ChargeSink = Depends(get_charge_sink),
    notifier: Notifier = Depends(get_notifier),
) -> LabService:
    """Provide a :class:`LabService` bound to the request session."""
    return LabService(
        orders, tests, appointments, session, audit, charges=charges, notifier=notifier
    )


# ── Pharmacy module ─────────────────────────────────────────────────────────
def get_pharmacy_catalog_service(
    medicines: MedicineRepository = Depends(get_medicine_repository),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
) -> PharmacyCatalogService:
    """Provide a :class:`PharmacyCatalogService` bound to the request session."""
    return PharmacyCatalogService(medicines, hospitals, session, audit)


def get_dispensing_service(
    prescriptions: PrescriptionRepository = Depends(get_prescription_repository),
    medicines: MedicineRepository = Depends(get_medicine_repository),
    appointments: AppointmentRepository = Depends(get_appointment_repository),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
    charges: ChargeSink = Depends(get_charge_sink),
) -> DispensingService:
    """Provide a :class:`DispensingService` bound to the request session.

    Billing shares the session through the charge sink, so the stock
    movements, the dispense and its bill line commit or roll back together
    (module spec §5.1 step 3).
    """
    return DispensingService(
        prescriptions, medicines, appointments, hospitals, session, audit, charges=charges
    )


def get_procurement_service(
    procurement: ProcurementRepository = Depends(get_procurement_repository),
    medicines: MedicineRepository = Depends(get_medicine_repository),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
) -> ProcurementService:
    """Provide a :class:`ProcurementService` bound to the request session."""
    return ProcurementService(procurement, medicines, hospitals, session, audit)


# ── Inventory module ────────────────────────────────────────────────────────
def get_inventory_service(
    inventory: InventoryRepository = Depends(get_inventory_repository),
    departments: DepartmentRepository = Depends(get_department_repository),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
    notifier: Notifier = Depends(get_notifier),
) -> InventoryService:
    """Provide an :class:`InventoryService` bound to the request session."""
    return InventoryService(inventory, departments, hospitals, session, audit, notifier=notifier)


def get_inventory_po_service(
    orders: InventoryPurchaseOrderRepository = Depends(get_inventory_po_repository),
    inventory: InventoryRepository = Depends(get_inventory_repository),
    vendors: ProcurementRepository = Depends(get_procurement_repository),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
) -> InventoryPurchaseOrderService:
    """Provide an :class:`InventoryPurchaseOrderService` bound to the request session.

    Vendors come from Pharmacy's repository: the table is shared by design
    (``docs/modules/09-inventory.md`` §20).
    """
    return InventoryPurchaseOrderService(orders, inventory, vendors, hospitals, session, audit)


# ── Reports module ──────────────────────────────────────────────────────────
def get_report_service(
    reports: ReportRepository = Depends(get_report_repository),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    doctors: DoctorRepository = Depends(get_doctor_repository),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditSink = Depends(get_audit_sink),
) -> ReportService:
    """Provide a :class:`ReportService` bound to the request session.

    The session and audit sink are there for one thing: an export records an
    audit entry and commits it. Every other report method only reads.
    """
    return ReportService(reports, hospitals, doctors, session, audit)


# ── Appointment module ──────────────────────────────────────────────────────
def get_invoice_draft_sink(
    billing: BillingService = Depends(get_billing_service),
) -> InvoiceDraftSink:
    """Provide the sink completed appointments are handed to for invoicing.

    Backed by :class:`BillingService` now that ``docs/modules/06-billing.md``
    has shipped. This provider is the only thing that changed:
    :class:`AppointmentService` still depends on the ``InvoiceDraftSink``
    protocol and knows nothing about Billing.
    """
    return BillingInvoiceDraftSink(billing)


def get_slot_ranker(
    runtime: AIRuntime = Depends(provide_ai_runtime),
) -> SlotRanker | None:
    """Provide the AI slot ranker, or ``None`` when AI is not configured on this server.

    With ``None``, ``POST /appointments/recommend-slot`` answers
    ``AI_NOT_CONFIGURED`` and booking by hand is unaffected. Building the
    ranker is two attribute assignments: no I/O happens per request.
    """
    if not runtime.status.configured:
        return None
    return AISlotRanker(runtime.service, runtime.prompts)


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
    # Notifications module
    "get_notification_service",
    "get_notifier",
    # Billing module
    "get_billing_service",
    "get_charge_sink",
    # Inventory module
    "get_inventory_po_service",
    "get_inventory_service",
    # Laboratory module
    "get_lab_catalog_service",
    "get_lab_service",
    # Pharmacy module
    "get_dispensing_service",
    "get_pharmacy_catalog_service",
    "get_procurement_service",
    "get_service_catalog_service",
    # Reports module
    "get_report_service",
    # Appointment module
    "get_appointment_service",
    "get_invoice_draft_sink",
    "get_slot_ranker",
    # Doctor module
    "get_booked_interval_source",
    "get_doctor_service",
]
