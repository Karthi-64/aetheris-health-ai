"""Database seed data — permissions catalog, system roles, and demo data.

This module is called via ``make seed`` to populate a fresh database with:

1. The permissions catalog (global, read-only in MVP)
2. System roles with their permission mappings
3. A demo hospital with demo users (for development)
4. Realistic clinical demo data — departments, doctors, patients and
   appointments — from :mod:`app.seeds.demo_data`

All operations are idempotent — safe to run multiple times. Every entity is
looked up by a stable natural key before it is created, so a second run adds
nothing and changes nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from app.core.security import hash_password
from app.database import create_session_factory, initialize_database
from app.models.hospital import Hospital
from app.models.permission import Permission
from app.models.role import Role, RolePermission
from app.models.user import User, UserRole, UserStatus
from app.seeds.demo_data import seed_demo_data

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
    ("billing.read", "billing", "View invoices and payments"),
    ("billing.create", "billing", "Create invoices"),
    ("billing.void", "billing", "Void invoices"),
    ("billing.approve_discount", "billing", "Approve discounts above threshold"),
    ("billing.record_payment", "billing", "Record payments"),
    # Laboratory
    ("lab.read", "lab", "View lab orders and results"),
    ("lab.create", "lab", "Create lab orders"),
    ("lab.update", "lab", "Update lab results"),
    # Pharmacy
    ("pharmacy.read", "pharmacy", "View prescriptions and inventory"),
    ("pharmacy.dispense", "pharmacy", "Dispense medications"),
    # Inventory
    ("inventory.read", "inventory", "View inventory"),
    ("inventory.create", "inventory", "Create inventory items"),
    ("inventory.update", "inventory", "Update inventory"),
    # Reports
    ("report.read", "reports", "View reports and dashboards"),
    ("report.export", "reports", "Export data"),
    # Settings
    ("settings.read", "settings", "View hospital settings"),
    ("settings.update", "settings", "Update hospital settings"),
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
            "billing.read",
            "billing.create",
            "billing.void",
            "billing.approve_discount",
            "billing.record_payment",
            "lab.read",
            "lab.create",
            "lab.update",
            "pharmacy.read",
            "pharmacy.dispense",
            "inventory.read",
            "inventory.create",
            "inventory.update",
            "report.read",
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
        ],
    ),
    (
        "Hospital Admin",
        "Full access within a single hospital.",
        [
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
            "billing.read",
            "billing.create",
            "billing.void",
            "billing.approve_discount",
            "billing.record_payment",
            "lab.read",
            # The Hospital Admin is "full access within a single hospital", so
            # its grant must cover every hospital-scoped role. Role assignment
            # enforces BR-8 (an admin may only hand out permissions they hold),
            # and without these five the admin could not invite a Doctor, Lab
            # Technician, Pharmacist or Inventory Manager at all.
            "lab.create",
            "lab.update",
            "pharmacy.read",
            "pharmacy.dispense",
            "inventory.read",
            "inventory.create",
            "inventory.update",
            "report.read",
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
        ],
    ),
    (
        "Doctor",
        "Clinical access — own patients, appointments, lab results.",
        [
            "patient.read",
            "patient.create",
            "patient.update",
            "appointment.read",
            "appointment.read.own",
            "appointment.start",
            "appointment.complete",
            "appointment.check_in",
            "lab.read",
            "lab.create",
            "report.read",
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
            "patient.read",
            "patient.update",
            "appointment.read",
            "appointment.check_in",
            "lab.read",
            "department.read",
            "doctor.read",
            "doctor.availability.read",
        ],
    ),
    (
        "Receptionist",
        "Front desk — patient registration, appointment booking.",
        [
            "patient.read",
            "patient.create",
            "appointment.read",
            "appointment.book",
            "appointment.reschedule",
            "appointment.cancel",
            "appointment.check_in",
            "appointment.recommend_slot",
            "department.read",
            "doctor.read",
            "doctor.availability.read",
        ],
    ),
    (
        "Billing Staff",
        "Financial operations — invoices, payments, insurance.",
        [
            "patient.read",
            "billing.read",
            "billing.create",
            "billing.void",
            "billing.record_payment",
            "report.read",
            "department.read",
            "doctor.read",
        ],
    ),
    (
        "Lab Technician",
        "Lab operations — receive orders, enter results.",
        [
            "lab.read",
            "lab.create",
            "lab.update",
            "department.read",
        ],
    ),
    (
        "Pharmacist",
        "Pharmacy operations — dispensing, inventory.",
        [
            "pharmacy.read",
            "pharmacy.dispense",
            "inventory.read",
            "department.read",
        ],
    ),
    (
        "Inventory Manager",
        "Supply chain — stock management, purchase orders.",
        [
            "inventory.read",
            "inventory.create",
            "inventory.update",
            "department.read",
        ],
    ),
]


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

        # ── 3. Create Demo Hospital ──────────────────────────────────────────
        hospital_stmt = select(Hospital).where(Hospital.slug == "demo-hospital")
        hospital_result = await session.execute(hospital_stmt)
        hospital = hospital_result.unique().scalar_one_or_none()

        if hospital is None:
            hospital = Hospital(
                name="Demo Hospital & Clinic",
                slug="demo-hospital",
                address={
                    "street": "123 Healthcare Avenue",
                    "city": "Bangalore",
                    "state": "Karnataka",
                    "zip": "560001",
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

        # ── 7. Demo Clinical Data ────────────────────────────────────────────
        # Departments, doctors, patients and appointments, so the frontend has
        # real data to build against. Same transaction, same idempotency rules.
        await seed_demo_data(session, hospital, role_map, actor_id=admin_user.id)

        # ── Commit ──────────────────────────────────────────────────────────
        await session.commit()
        logger.info("database_seeded_successfully")
        logger.info(
            "demo_credentials", admin=admin_email, doctor=doctor_email, reception=receptionist_email
        )


async def main() -> None:
    """Entry point for ``make seed``."""
    from app.core.config import settings

    await seed_database(database_url=settings.DATABASE_URL)


if __name__ == "__main__":
    asyncio.run(main())
