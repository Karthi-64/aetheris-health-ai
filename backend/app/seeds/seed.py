"""Database seed data — permissions catalog, system roles, and demo data.

This module is called via ``make seed`` to populate a fresh database with:

1. The permissions catalog (global, read-only in MVP)
2. System roles with their permission mappings
3. A demo hospital with demo users (for development)
4. Realistic demo data — departments, doctors, patients, appointments, and a
   services catalog with invoices and payments — from
   :mod:`app.seeds.demo_data` and :mod:`app.seeds.demo_billing`

All operations are idempotent — safe to run multiple times. Every entity is
looked up by a stable natural key before it is created, so a second run adds
nothing and changes nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from app.core.feature_flags import (
    AI_SLOT_RECOMMENDATION,
    PATIENT_APP_ENABLED,
    with_default_flag,
)
from app.core.security import hash_password
from app.database import create_session_factory, initialize_database
from app.models.hospital import Hospital
from app.models.permission import Permission
from app.models.role import Role, RolePermission
from app.models.user import User, UserRole, UserStatus
from app.seeds.demo_data import seed_demo_data

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# ── Permission Catalog ──────────────────────────────────────────────────────
# Format: (code, module, description)

PERMISSION_DEFINITIONS: list[tuple[str, str, str]] = [
    # Auth
    ("user.read", "auth", "View user profiles"),
    ("user.create", "auth", "Create/invite new users"),
    ("user.update", "auth", "Update user profiles"),
    ("user.deactivate", "auth", "Suspend or reactivate users"),
    ("user.reset_password", "auth", "Reset another user's password"),
    # Roles
    ("role.read", "auth", "View role definitions"),
    ("role.assign", "auth", "Assign or remove roles from users"),
    # Patients
    ("patient.read", "patient", "View patient records"),
    ("patient.create", "patient", "Create new patient records"),
    ("patient.update", "patient", "Update patient records"),
    ("patient.delete", "patient", "Delete patient records"),
    # Appointments
    # docs/modules/05-appointment-management.md §10. The earlier
    # `appointment.create` / `appointment.update` placeholders are replaced by
    # the spec's names: they were seeded before the module existed and no code
    # ever checked them.
    ("appointment.read", "appointment", "View appointments"),
    ("appointment.read.own", "appointment", "View own schedule only"),
    ("appointment.book", "appointment", "Book appointments"),
    ("appointment.reschedule", "appointment", "Reschedule appointments"),
    ("appointment.cancel", "appointment", "Cancel appointments and mark no-shows"),
    ("appointment.check_in", "appointment", "Check in patients"),
    ("appointment.start", "appointment", "Start consultations"),
    ("appointment.complete", "appointment", "Complete consultations"),
    ("appointment.book_override", "appointment", "Book outside doctor availability"),
    ("appointment.recommend_slot", "appointment", "Request AI slot recommendations"),
    # Billing
    # docs/modules/06-billing.md §10. The earlier `billing.*` placeholders are
    # replaced by the spec's names, as was done for appointments: they were
    # seeded before the module existed and no backend code ever checked them.
    # The whole §10 catalog is seeded; the endpoints behind approve_discount,
    # refund, pdf.download and ai_explain are not built yet.
    ("service.read", "billing", "View the services catalog"),
    ("service.create", "billing", "Add services to the catalog"),
    ("service.update", "billing", "Edit and retire catalog services"),
    ("invoice.read", "billing", "View invoices and payments"),
    ("invoice.read.own", "billing", "View invoices for own appointments only"),
    ("invoice.create", "billing", "Create draft invoices"),
    ("invoice.update", "billing", "Edit draft invoices"),
    ("invoice.issue", "billing", "Issue invoices"),
    ("invoice.void", "billing", "Void invoices"),
    ("invoice.approve_discount", "billing", "Approve discounts above threshold"),
    ("invoice.payment.record", "billing", "Record payments"),
    # Not in §10. §3 limits a receptionist to cash, and the spec names no code
    # to express that; this is the narrow sibling of the one above, in the same
    # way `invoice.read.own` is the narrow sibling of `invoice.read`.
    ("invoice.payment.record.cash", "billing", "Record cash payments only"),
    ("invoice.refund", "billing", "Refund payments"),
    ("invoice.pdf.download", "billing", "Download invoice PDFs"),
    ("invoice.ai_explain", "billing", "Request an AI explanation of an invoice"),
    # Notifications (docs/modules/11-notifications.md §10). The template and
    # delivery-log codes guard v2.1 endpoints that are not built yet.
    ("notification.read.own", "notification", "Read own notifications"),
    ("notification.preference.update.own", "notification", "Change own notification preferences"),
    ("notification.broadcast", "notification", "Send an announcement to a role or the hospital"),
    ("notification.template.read", "notification", "View notification templates"),
    ("notification.template.create", "notification", "Create notification templates"),
    ("notification.template.update", "notification", "Edit notification templates"),
    ("notification.delivery.read", "notification", "View the notification delivery log"),
    # Laboratory
    ("lab.test.read", "lab", "View the lab test catalog"),
    ("lab.test.create", "lab", "Add tests to the lab catalog"),
    ("lab.test.update", "lab", "Edit and retire lab tests"),
    ("lab.order.read", "lab", "View lab orders and results"),
    ("lab.order.create", "lab", "Order lab tests"),
    ("lab.order.cancel", "lab", "Cancel a lab order"),
    ("lab.order.collect_sample", "lab", "Record sample collection"),
    ("lab.order.enter_results", "lab", "Enter lab results"),
    ("lab.order.release", "lab", "Release lab results"),
    ("lab.order.amend", "lab", "Correct a released lab result"),
    ("lab.report.download", "lab", "Download lab report PDFs"),
    ("lab.ai_explain", "lab", "Request an AI explanation of lab results"),
    # Pharmacy
    ("pharmacy.medicine.read", "pharmacy", "View the medicine catalog"),
    ("pharmacy.medicine.create", "pharmacy", "Add medicines to the catalog"),
    ("pharmacy.medicine.update", "pharmacy", "Edit and retire medicines"),
    ("pharmacy.batch.read", "pharmacy", "View batches and stock"),
    ("pharmacy.batch.create", "pharmacy", "Receive stock"),
    ("pharmacy.batch.update", "pharmacy", "Recall batches and adjust stock"),
    ("pharmacy.prescription.read", "pharmacy", "View prescriptions and dispenses"),
    ("pharmacy.prescription.create", "pharmacy", "Write and cancel prescriptions"),
    ("pharmacy.dispense.execute", "pharmacy", "Dispense prescriptions"),
    ("pharmacy.interaction.check", "pharmacy", "Check drug interactions"),
    ("pharmacy.po.read", "pharmacy", "View purchase orders"),
    ("pharmacy.po.create", "pharmacy", "Draft purchase orders"),
    ("pharmacy.po.update", "pharmacy", "Send and cancel purchase orders"),
    ("pharmacy.po.receive", "pharmacy", "Receive purchase orders"),
    ("pharmacy.vendor.read", "pharmacy", "View vendors"),
    ("pharmacy.vendor.create", "pharmacy", "Add vendors"),
    ("pharmacy.vendor.update", "pharmacy", "Edit and retire vendors"),
    ("pharmacy.ai_substitute", "pharmacy", "Request an AI substitution suggestion"),
    # Inventory
    ("inventory.item.read", "inventory", "View inventory items"),
    ("inventory.item.create", "inventory", "Add inventory items"),
    ("inventory.item.update", "inventory", "Edit and retire inventory items"),
    ("inventory.location.read", "inventory", "View stock locations"),
    ("inventory.location.create", "inventory", "Add stock locations"),
    ("inventory.location.update", "inventory", "Edit and retire stock locations"),
    ("inventory.stock.read", "inventory", "View stock levels and movements"),
    ("inventory.consume", "inventory", "Record stock used"),
    ("inventory.transfer", "inventory", "Transfer stock between locations"),
    ("inventory.adjust", "inventory", "Adjust stock after a count"),
    ("inventory.po.read", "inventory", "View inventory purchase orders"),
    ("inventory.po.create", "inventory", "Draft inventory purchase orders"),
    ("inventory.po.update", "inventory", "Send and cancel inventory purchase orders"),
    ("inventory.po.receive", "inventory", "Receive inventory purchase orders"),
    ("inventory.forecast.read", "inventory", "View AI reorder recommendations"),
    # Reports
    # docs/modules/10-reports-dashboard.md §10. The earlier `report.read`
    # placeholder is replaced by one read code per role dashboard: it was
    # seeded before the module existed and no code ever checked it.
    ("report.admin.read", "reports", "View the admin dashboard and hospital-wide reports"),
    ("report.doctor.read", "reports", "View the doctor dashboard (own schedule and patients)"),
    ("report.reception.read", "reports", "View the reception dashboard"),
    ("report.billing.read", "reports", "View the billing dashboard and financial reports"),
    ("report.export", "reports", "Export data"),
    # Settings
    ("settings.read", "settings", "View hospital settings"),
    ("settings.update", "settings", "Update hospital settings"),
    # Audit (docs/modules/12-audit-logs.md §10)
    ("audit.read", "audit", "Search and read the audit trail"),
    ("audit.export", "audit", "Export audit trail entries"),
    # Departments (docs/modules/14-hospital-settings.md §10)
    ("department.read", "settings", "List and read departments"),
    ("department.create", "settings", "Create departments"),
    ("department.update", "settings", "Edit and reactivate departments"),
    ("department.delete", "settings", "Deactivate departments"),
    # Doctors (docs/modules/04-doctor-management.md §10)
    ("doctor.read", "doctor", "View doctors"),
    ("doctor.create", "doctor", "Onboard doctors"),
    ("doctor.update", "doctor", "Update doctor profiles"),
    ("doctor.delete", "doctor", "Deactivate doctors"),
    ("doctor.availability.read", "doctor", "View doctor availability and computed slots"),
    ("doctor.availability.update", "doctor", "Set doctor availability"),
    ("doctor.leave.create", "doctor", "Record doctor leave"),
    ("doctor.leave.delete", "doctor", "Cancel doctor leave"),
]

# ── System Role Definitions ─────────────────────────────────────────────────
# Format: (name, description, [permission_codes])

SYSTEM_ROLES: list[tuple[str, str, list[str]]] = [
    (
        "Super Admin",
        "Platform-wide access. Created per-hospital for local management.",
        [
            # Every role reads its own notifications (module spec 11 §3).
            "notification.read.own",
            "notification.preference.update.own",
            "notification.broadcast",
            "notification.template.read",
            "notification.template.create",
            "notification.template.update",
            "notification.delivery.read",
            "user.read",
            "user.create",
            "user.update",
            "user.deactivate",
            "user.reset_password",
            "role.read",
            "role.assign",
            "patient.read",
            "patient.create",
            "patient.update",
            "patient.delete",
            "appointment.read",
            "appointment.read.own",
            "appointment.book",
            "appointment.reschedule",
            "appointment.cancel",
            "appointment.check_in",
            "appointment.start",
            "appointment.complete",
            "appointment.book_override",
            "appointment.recommend_slot",
            "service.read",
            "service.create",
            "service.update",
            "invoice.read",
            "invoice.read.own",
            "invoice.create",
            "invoice.update",
            "invoice.issue",
            "invoice.void",
            "invoice.approve_discount",
            "invoice.payment.record",
            "invoice.payment.record.cash",
            "invoice.refund",
            "invoice.pdf.download",
            "invoice.ai_explain",
            "lab.test.read",
            "lab.test.create",
            "lab.test.update",
            "lab.order.read",
            "lab.order.create",
            "lab.order.cancel",
            "lab.order.collect_sample",
            "lab.order.enter_results",
            "lab.order.release",
            "lab.order.amend",
            "lab.report.download",
            "lab.ai_explain",
            "pharmacy.medicine.read",
            "pharmacy.medicine.create",
            "pharmacy.medicine.update",
            "pharmacy.batch.read",
            "pharmacy.batch.create",
            "pharmacy.batch.update",
            "pharmacy.prescription.read",
            "pharmacy.prescription.create",
            "pharmacy.dispense.execute",
            "pharmacy.interaction.check",
            "pharmacy.po.read",
            "pharmacy.po.create",
            "pharmacy.po.update",
            "pharmacy.po.receive",
            "pharmacy.vendor.read",
            "pharmacy.vendor.create",
            "pharmacy.vendor.update",
            "pharmacy.ai_substitute",
            "inventory.item.read",
            "inventory.item.create",
            "inventory.item.update",
            "inventory.location.read",
            "inventory.location.create",
            "inventory.location.update",
            "inventory.stock.read",
            "inventory.consume",
            "inventory.transfer",
            "inventory.adjust",
            "inventory.po.read",
            "inventory.po.create",
            "inventory.po.update",
            "inventory.po.receive",
            "inventory.forecast.read",
            "report.admin.read",
            "report.doctor.read",
            "report.reception.read",
            "report.billing.read",
            "report.export",
            "settings.read",
            "settings.update",
            "department.read",
            "department.create",
            "department.update",
            "department.delete",
            "doctor.read",
            "doctor.create",
            "doctor.update",
            "doctor.delete",
            "doctor.availability.read",
            "doctor.availability.update",
            "doctor.leave.create",
            "doctor.leave.delete",
            "audit.read",
            "audit.export",
        ],
    ),
    (
        "Hospital Admin",
        "Full access within a single hospital.",
        [
            # Every role reads its own notifications (module spec 11 §3).
            "notification.read.own",
            "notification.preference.update.own",
            "notification.broadcast",
            "notification.template.read",
            "notification.template.create",
            "notification.template.update",
            "notification.delivery.read",
            "user.read",
            "user.create",
            "user.update",
            "user.deactivate",
            "user.reset_password",
            "role.read",
            "role.assign",
            "patient.read",
            "patient.create",
            "patient.update",
            "patient.delete",
            "appointment.read",
            "appointment.read.own",
            "appointment.book",
            "appointment.reschedule",
            "appointment.cancel",
            "appointment.check_in",
            "appointment.start",
            "appointment.complete",
            "appointment.book_override",
            "appointment.recommend_slot",
            "service.read",
            "service.create",
            "service.update",
            "invoice.read",
            "invoice.read.own",
            "invoice.create",
            "invoice.update",
            "invoice.issue",
            "invoice.void",
            "invoice.approve_discount",
            "invoice.payment.record",
            "invoice.payment.record.cash",
            "invoice.refund",
            "invoice.pdf.download",
            "invoice.ai_explain",
            "lab.test.read",
            "lab.test.create",
            "lab.test.update",
            "lab.order.read",
            "lab.order.create",
            "lab.order.cancel",
            "lab.order.collect_sample",
            "lab.order.enter_results",
            "lab.order.release",
            "lab.order.amend",
            "lab.report.download",
            "lab.ai_explain",
            # The Hospital Admin is "full access within a single hospital", so
            # its grant must cover every hospital-scoped role. Role assignment
            # enforces BR-8 (an admin may only hand out permissions they hold),
            # and without these five the admin could not invite a Doctor, Lab
            # Technician, Pharmacist or Inventory Manager at all.
            "pharmacy.medicine.read",
            "pharmacy.medicine.create",
            "pharmacy.medicine.update",
            "pharmacy.batch.read",
            "pharmacy.batch.create",
            "pharmacy.batch.update",
            "pharmacy.prescription.read",
            "pharmacy.prescription.create",
            "pharmacy.dispense.execute",
            "pharmacy.interaction.check",
            "pharmacy.po.read",
            "pharmacy.po.create",
            "pharmacy.po.update",
            "pharmacy.po.receive",
            "pharmacy.vendor.read",
            "pharmacy.vendor.create",
            "pharmacy.vendor.update",
            "pharmacy.ai_substitute",
            "inventory.item.read",
            "inventory.item.create",
            "inventory.item.update",
            "inventory.location.read",
            "inventory.location.create",
            "inventory.location.update",
            "inventory.stock.read",
            "inventory.consume",
            "inventory.transfer",
            "inventory.adjust",
            "inventory.po.read",
            "inventory.po.create",
            "inventory.po.update",
            "inventory.po.receive",
            "inventory.forecast.read",
            "report.admin.read",
            "report.doctor.read",
            "report.reception.read",
            "report.billing.read",
            "report.export",
            "settings.read",
            "settings.update",
            "department.read",
            "department.create",
            "department.update",
            "department.delete",
            "doctor.read",
            "doctor.create",
            "doctor.update",
            "doctor.delete",
            "doctor.availability.read",
            "doctor.availability.update",
            "doctor.leave.create",
            "doctor.leave.delete",
            "audit.read",
        ],
    ),
    (
        "Doctor",
        "Clinical access — own patients, appointments, lab results.",
        [
            # Every role reads its own notifications (module spec 11 §3).
            "notification.read.own",
            "notification.preference.update.own",
            "patient.read",
            "patient.create",
            "patient.update",
            "appointment.read",
            "appointment.read.own",
            "appointment.start",
            "appointment.complete",
            "appointment.check_in",
            # docs/modules/06-billing.md §3: "view invoices for their patients",
            # scoped to appointments where they are the doctor.
            "invoice.read.own",
            "lab.test.read",
            "lab.order.read",
            "lab.order.create",
            "lab.order.cancel",
            "pharmacy.medicine.read",
            "pharmacy.prescription.read",
            "pharmacy.prescription.create",
            "report.doctor.read",
            "department.read",
            "doctor.read",
            "doctor.availability.read",
            "doctor.availability.update",
            "doctor.leave.create",
            "doctor.leave.delete",
        ],
    ),
    (
        "Nurse",
        "Care coordination — assigned patients, vitals, appointments.",
        [
            # Every role reads its own notifications (module spec 11 §3).
            "notification.read.own",
            "notification.preference.update.own",
            "patient.read",
            "patient.update",
            "appointment.read",
            "appointment.check_in",
            "lab.order.read",
            "inventory.item.read",
            "inventory.location.read",
            "inventory.stock.read",
            "inventory.consume",
            "department.read",
            "doctor.read",
            "doctor.availability.read",
        ],
    ),
    (
        "Receptionist",
        "Front desk — patient registration, appointment booking.",
        [
            # Every role reads its own notifications (module spec 11 §3).
            "notification.read.own",
            "notification.preference.update.own",
            "patient.read",
            "patient.create",
            "appointment.read",
            "appointment.book",
            "appointment.reschedule",
            "appointment.cancel",
            "appointment.check_in",
            "appointment.recommend_slot",
            # docs/modules/06-billing.md §3: view invoices, record cash
            # payments. The `.cash` code is what limits the method.
            "service.read",
            "invoice.read",
            "invoice.payment.record.cash",
            # docs/modules/10-reports-dashboard.md §10: the reception dashboard.
            "report.reception.read",
            "department.read",
            "doctor.read",
            "doctor.availability.read",
        ],
    ),
    (
        "Billing Staff",
        "Financial operations — invoices, payments, insurance.",
        [
            # Every role reads its own notifications (module spec 11 §3).
            "notification.read.own",
            "notification.preference.update.own",
            "patient.read",
            # docs/modules/06-billing.md §3. No `invoice.void`: business rule 4
            # makes voiding an admin action.
            "service.read",
            "invoice.read",
            "invoice.create",
            "invoice.update",
            "invoice.issue",
            "invoice.payment.record",
            # docs/modules/10-reports-dashboard.md §10: the billing dashboard,
            # the revenue and outstanding reports, and their export.
            "report.billing.read",
            "report.export",
            "department.read",
            "doctor.read",
        ],
    ),
    (
        "Lab Technician",
        "Lab operations — receive orders, enter results.",
        [
            # Every role reads its own notifications (module spec 11 §3).
            "notification.read.own",
            "notification.preference.update.own",
            "lab.test.read",
            "lab.order.read",
            "lab.order.collect_sample",
            "lab.order.enter_results",
            "department.read",
        ],
    ),
    (
        "Pharmacist",
        "Pharmacy operations — dispensing, inventory.",
        [
            # Every role reads its own notifications (module spec 11 §3).
            "notification.read.own",
            "notification.preference.update.own",
            "pharmacy.medicine.read",
            "pharmacy.batch.read",
            "pharmacy.batch.create",
            "pharmacy.batch.update",
            "pharmacy.prescription.read",
            "pharmacy.dispense.execute",
            "pharmacy.po.read",
            "pharmacy.po.receive",
            "pharmacy.vendor.read",
            "inventory.item.read",
            "inventory.location.read",
            "inventory.stock.read",
            "department.read",
        ],
    ),
    (
        "Inventory Manager",
        "Supply chain — stock management, purchase orders.",
        [
            # Every role reads its own notifications (module spec 11 §3).
            "notification.read.own",
            "notification.preference.update.own",
            "pharmacy.medicine.read",
            "pharmacy.batch.read",
            "pharmacy.po.read",
            "pharmacy.po.create",
            "pharmacy.po.update",
            "pharmacy.po.receive",
            "pharmacy.vendor.read",
            "pharmacy.vendor.create",
            "pharmacy.vendor.update",
            "inventory.item.read",
            "inventory.item.create",
            "inventory.item.update",
            "inventory.location.read",
            "inventory.location.create",
            "inventory.location.update",
            "inventory.stock.read",
            "inventory.consume",
            "inventory.transfer",
            "inventory.adjust",
            "inventory.po.read",
            "inventory.po.create",
            "inventory.po.update",
            "inventory.po.receive",
            "inventory.forecast.read",
            "department.read",
        ],
    ),
]


async def ensure_demo_flags(session: AsyncSession, hospital: Hospital) -> bool:
    """Turn AI slot suggestions on for the demo hospital unless the flag was set explicitly.

    The flag only says the hospital has been given the feature; the server
    still needs an AI key before anything can be suggested.

    :param session: The seed session.
    :param hospital: The demo hospital.
    :returns: ``True`` when the flag was defaulted, ``False`` when an explicit
        value (including ``False``) was left alone.
    """
    # Reassigned rather than mutated in place so SQLAlchemy sees the JSONB change.
    updated = with_default_flag(hospital.settings, AI_SLOT_RECOMMENDATION, True)
    if updated is None:
        return False
    hospital.settings = updated
    await session.flush()
    logger.info("demo_hospital_flag_defaulted", flag=AI_SLOT_RECOMMENDATION)
    return True


async def ensure_demo_patient_app_flag(session: AsyncSession, hospital: Hospital) -> bool:
    """Open the demo hospital to the Patient App unless the flag was set explicitly.

    The flag is off for every hospital unless set, so without this a local
    Patient App could sign in but never link to, or register, a record.

    :param session: The seed session.
    :param hospital: The demo hospital.
    :returns: ``True`` when the flag was defaulted, ``False`` when an explicit
        value (including ``False``) was left alone.
    """
    # Reassigned rather than mutated in place so SQLAlchemy sees the JSONB change.
    updated = with_default_flag(hospital.settings, PATIENT_APP_ENABLED, True)
    if updated is None:
        return False
    hospital.settings = updated
    await session.flush()
    logger.info("demo_hospital_flag_defaulted", flag=PATIENT_APP_ENABLED)
    return True


async def seed_database(database_url: str | None = None) -> None:
    """Seed the database with permissions, roles, and demo data.

    :param database_url: Optional database URL override. Defaults to settings.
    """
    initialize_database(database_url=database_url)
    factory = create_session_factory()

    async with factory() as session:
        # ── 1. Seed Permissions ──────────────────────────────────────────────
        logger.info("seeding_permissions_started")

        permission_map: dict[str, Permission] = {}
        for code, module, description in PERMISSION_DEFINITIONS:
            perm_stmt = select(Permission).where(Permission.code == code)
            perm_result = await session.execute(perm_stmt)
            existing_perm = perm_result.unique().scalar_one_or_none()

            if existing_perm is None:
                permission = Permission(code=code, module=module, description=description)
                session.add(permission)
                await session.flush()
                permission_map[code] = permission
                logger.debug("permission_created", code=code)
            else:
                permission_map[code] = existing_perm

        logger.info("permissions_seeded", count=len(permission_map))

        # ── 2. Seed System Roles ─────────────────────────────────────────────
        logger.info("seeding_roles_started")

        role_map: dict[str, Role] = {}
        for name, description, permission_codes in SYSTEM_ROLES:
            role_stmt = select(Role).where(
                Role.name == name, Role.hospital_id.is_(None), Role.is_system.is_(True)
            )
            role_result = await session.execute(role_stmt)
            existing_role = role_result.unique().scalar_one_or_none()

            if existing_role is None:
                role = Role(
                    name=name,
                    description=description,
                    is_system=True,
                    hospital_id=None,
                )
                session.add(role)
                await session.flush()
                role_map[name] = role
                logger.debug("role_created", name=name)
            else:
                role_map[name] = existing_role

            # Assign permissions to the role
            if permission_codes:
                current_role = role_map.get(name)
                if current_role is None:
                    continue
                for perm_code in permission_codes:
                    perm = permission_map.get(perm_code)
                    if perm is None:
                        continue

                    # Check if already assigned
                    rp_stmt = select(RolePermission).where(
                        RolePermission.role_id == current_role.id,
                        RolePermission.permission_id == perm.id,
                    )
                    rp_result = await session.execute(rp_stmt)
                    existing_rp = rp_result.unique().scalar_one_or_none()

                    if existing_rp is None:
                        rp = RolePermission(role_id=current_role.id, permission_id=perm.id)
                        session.add(rp)

            await session.flush()

        logger.info("roles_seeded", count=len(role_map))

        # Everything below is demonstration data: a hospital, and staff
        # accounts — an administrator among them — whose passwords are
        # written in this file and in the documentation. Outside development
        # they are never created: a deployment seeded with them would have an
        # administrator anyone could sign in as. The permission catalogue and
        # system roles above are reference data and are seeded everywhere.
        from app.core.config import settings

        if not settings.is_development:
            await session.commit()
            logger.warning(
                "demo_data_not_seeded",
                reason="demo accounts have published passwords",
                environment=settings.APP_ENV.value,
            )
            return

        # ── 3. Create Demo Hospital ──────────────────────────────────────────
        hospital_stmt = select(Hospital).where(Hospital.slug == "demo-hospital")
        hospital_result = await session.execute(hospital_stmt)
        hospital = hospital_result.unique().scalar_one_or_none()

        if hospital is None:
            hospital = Hospital(
                name="Demo Hospital & Clinic",
                slug="demo-hospital",
                # Canonical address keys — ``line1``/``postal_code`` match the
                # patient address shape, so settings and patients agree on one
                # key set (PR #29 review finding 6).
                address={
                    "line1": "123 Healthcare Avenue",
                    "city": "Bangalore",
                    "state": "Karnataka",
                    "postal_code": "560001",
                    "country": "India",
                },
                phone="+918012345678",
                email="info@demohospital.com",
                is_active=True,
            )
            session.add(hospital)
            await session.flush()
            logger.info("demo_hospital_created", id=str(hospital.id))
        else:
            logger.info("demo_hospital_exists", id=str(hospital.id))

        # AI slot suggestions are on for the demo hospital unless the flag was
        # set explicitly. It still needs an AI key on the server to be usable.
        await ensure_demo_flags(session, hospital)
        await ensure_demo_patient_app_flag(session, hospital)

        # ── 4. Create Demo Admin User ────────────────────────────────────────
        admin_email = "admin@demohospital.com"
        admin_stmt = select(User).where(User.email == admin_email, User.hospital_id == hospital.id)
        admin_result = await session.execute(admin_stmt)
        admin_user = admin_result.unique().scalar_one_or_none()

        if admin_user is None:
            admin_user = User(
                hospital_id=hospital.id,
                email=admin_email,
                password_hash=hash_password("Admin@1234567"),
                first_name="Admin",
                last_name="User",
                status=UserStatus.ACTIVE,
                password_changed_at=datetime.now(UTC),
            )
            session.add(admin_user)
            await session.flush()

            # Assign Hospital Admin role
            admin_role = role_map.get("Hospital Admin")
            if admin_role:
                ur = UserRole(user_id=admin_user.id, role_id=admin_role.id)
                session.add(ur)

            logger.info("demo_admin_created", email=admin_email)
        else:
            logger.info("demo_admin_exists", email=admin_email)

        # ── 5. Create Demo Doctor User ───────────────────────────────────────
        doctor_email = "doctor@demohospital.com"
        doctor_stmt = select(User).where(
            User.email == doctor_email, User.hospital_id == hospital.id
        )
        doctor_result = await session.execute(doctor_stmt)
        doctor_user = doctor_result.unique().scalar_one_or_none()

        if doctor_user is None:
            doctor_user = User(
                hospital_id=hospital.id,
                email=doctor_email,
                password_hash=hash_password("Doctor@1234567"),
                first_name="Priya",
                last_name="Sharma",
                status=UserStatus.ACTIVE,
                password_changed_at=datetime.now(UTC),
            )
            session.add(doctor_user)
            await session.flush()

            doctor_role = role_map.get("Doctor")
            if doctor_role:
                ur = UserRole(user_id=doctor_user.id, role_id=doctor_role.id)
                session.add(ur)

            logger.info("demo_doctor_created", email=doctor_email)
        else:
            logger.info("demo_doctor_exists", email=doctor_email)

        # ── 6. Create Demo Receptionist User ─────────────────────────────────
        receptionist_email = "reception@demohospital.com"
        receptionist_stmt = select(User).where(
            User.email == receptionist_email, User.hospital_id == hospital.id
        )
        receptionist_result = await session.execute(receptionist_stmt)
        receptionist_user = receptionist_result.unique().scalar_one_or_none()

        if receptionist_user is None:
            receptionist_user = User(
                hospital_id=hospital.id,
                email=receptionist_email,
                password_hash=hash_password("Reception@1234567"),
                first_name="Ananya",
                last_name="Rao",
                status=UserStatus.ACTIVE,
                password_changed_at=datetime.now(UTC),
            )
            session.add(receptionist_user)
            await session.flush()

            receptionist_role = role_map.get("Receptionist")
            if receptionist_role:
                ur = UserRole(user_id=receptionist_user.id, role_id=receptionist_role.id)
                session.add(ur)

            logger.info("demo_receptionist_created", email=receptionist_email)
        else:
            logger.info("demo_receptionist_exists", email=receptionist_email)

        # ── 6b. Create Demo Lab Technician User ──────────────────────────────
        lab_email = "lab@demohospital.com"
        lab_result = await session.execute(
            select(User).where(User.email == lab_email, User.hospital_id == hospital.id)
        )
        lab_user = lab_result.unique().scalar_one_or_none()

        if lab_user is None:
            lab_user = User(
                hospital_id=hospital.id,
                email=lab_email,
                password_hash=hash_password("LabTech@1234567"),
                first_name="Arjun",
                last_name="Pillai",
                status=UserStatus.ACTIVE,
                password_changed_at=datetime.now(UTC),
            )
            session.add(lab_user)
            await session.flush()

            lab_role = role_map.get("Lab Technician")
            if lab_role:
                session.add(UserRole(user_id=lab_user.id, role_id=lab_role.id))

            logger.info("demo_lab_technician_created", email=lab_email)
        else:
            logger.info("demo_lab_technician_exists", email=lab_email)

        # ── 6c. Create Demo Pharmacist User ──────────────────────────────────
        pharmacist_email = "pharmacy@demohospital.com"
        pharmacist_result = await session.execute(
            select(User).where(User.email == pharmacist_email, User.hospital_id == hospital.id)
        )
        pharmacist_user = pharmacist_result.unique().scalar_one_or_none()

        if pharmacist_user is None:
            pharmacist_user = User(
                hospital_id=hospital.id,
                email=pharmacist_email,
                password_hash=hash_password("Pharmacy@1234567"),
                first_name="Kavya",
                last_name="Reddy",
                status=UserStatus.ACTIVE,
                password_changed_at=datetime.now(UTC),
            )
            session.add(pharmacist_user)
            await session.flush()

            pharmacist_role = role_map.get("Pharmacist")
            if pharmacist_role:
                session.add(UserRole(user_id=pharmacist_user.id, role_id=pharmacist_role.id))

            logger.info("demo_pharmacist_created", email=pharmacist_email)
        else:
            logger.info("demo_pharmacist_exists", email=pharmacist_email)

        # ── 6d. Create Demo Inventory Manager User ───────────────────────────
        inventory_email = "inventory@demohospital.com"
        inventory_result = await session.execute(
            select(User).where(User.email == inventory_email, User.hospital_id == hospital.id)
        )
        inventory_user = inventory_result.unique().scalar_one_or_none()

        if inventory_user is None:
            inventory_user = User(
                hospital_id=hospital.id,
                email=inventory_email,
                password_hash=hash_password("Inventory@1234567"),
                first_name="Rohan",
                last_name="Desai",
                status=UserStatus.ACTIVE,
                password_changed_at=datetime.now(UTC),
            )
            session.add(inventory_user)
            await session.flush()

            inventory_role = role_map.get("Inventory Manager")
            if inventory_role:
                session.add(UserRole(user_id=inventory_user.id, role_id=inventory_role.id))

            logger.info("demo_inventory_manager_created", email=inventory_email)
        else:
            logger.info("demo_inventory_manager_exists", email=inventory_email)

        # ── 6e. Create Demo Billing Staff User ───────────────────────────────
        billing_email = "billing@demohospital.com"
        billing_result = await session.execute(
            select(User).where(User.email == billing_email, User.hospital_id == hospital.id)
        )
        billing_user = billing_result.unique().scalar_one_or_none()

        if billing_user is None:
            billing_user = User(
                hospital_id=hospital.id,
                email=billing_email,
                password_hash=hash_password("Billing@1234567"),
                first_name="Neha",
                last_name="Joshi",
                status=UserStatus.ACTIVE,
                password_changed_at=datetime.now(UTC),
            )
            session.add(billing_user)
            await session.flush()

            billing_role = role_map.get("Billing Staff")
            if billing_role:
                session.add(UserRole(user_id=billing_user.id, role_id=billing_role.id))

            logger.info("demo_billing_staff_created", email=billing_email)
        else:
            logger.info("demo_billing_staff_exists", email=billing_email)

        # ── 7. Demo Clinical Data ────────────────────────────────────────────
        # Departments, doctors, patients, appointments and billing, so the
        # frontend has real data to build against. Same transaction, same
        # idempotency rules.
        await seed_demo_data(session, hospital, role_map, actor_id=admin_user.id)

        # ── Commit ──────────────────────────────────────────────────────────
        await session.commit()
        logger.info("database_seeded_successfully")
        logger.info(
            "demo_credentials",
            admin=admin_email,
            doctor=doctor_email,
            reception=receptionist_email,
            lab=lab_email,
            pharmacy=pharmacist_email,
            inventory=inventory_email,
            billing=billing_email,
        )


async def main() -> None:
    """Entry point for ``make seed``."""
    from app.core.config import settings

    await seed_database(database_url=settings.DATABASE_URL)


if __name__ == "__main__":
    asyncio.run(main())
