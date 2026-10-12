"""Repository for the doctor aggregate — profile, availability, and leaves.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it, including the child tables — which is why migration 0006 carries
``hospital_id`` on them (CLAUDE.md rules 4 and 5).

One repository for all three tables because they are a single aggregate rooted
at :class:`~app.models.doctor.Doctor`. Availability and leave rows are never
addressed except through their doctor, so splitting them into sibling
repositories would only tempt a service into composing what is really one
consistency boundary.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, and_, delete, exists, func, or_, select

from app.models.department import Department
from app.models.doctor import Doctor, DoctorAvailability, DoctorLeave
from app.models.user import User, UserStatus
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Row
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import ColumnElement


@dataclass(frozen=True, slots=True)
class DoctorDirectoryEntry:
    """A doctor as a directory may describe one: columns, not the entity.

    No user id, e-mail, phone, licence number, fee or audit column is read.

    :param id: The doctor's UUID.
    :param first_name: Given name, from the doctor's user row.
    :param last_name: Family name, from the doctor's user row.
    :param specialization: Clinical specialization.
    :param qualifications: The stored qualifications value, as stored.
    :param languages: The stored languages value, as stored.
    :param bio: The stored biography, if any.
    :param department_id: The doctor's active department, if any.
    :param department_name: That department's name.
    """

    id: uuid.UUID
    first_name: str
    last_name: str
    specialization: str
    qualifications: Any
    languages: Any
    bio: str | None
    department_id: uuid.UUID | None
    department_name: str | None


@dataclass(frozen=True, slots=True)
class DirectoryDepartment:
    """A department that has a doctor in the directory.

    :param id: The department's UUID.
    :param name: Its name.
    :param description: Its description, if any.
    """

    id: uuid.UUID
    name: str
    description: str | None


class DoctorRepository(BaseRepository[Doctor]):
    """Persistence for doctors and their schedule.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Doctor, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _scoped(
        self, hospital_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Select[tuple[Doctor]]:
        """Return a base SELECT filtered to one hospital.

        :param hospital_id: The tenant to scope to.
        :param include_deleted: Include soft-deleted doctors.
        :returns: A statement filtered by ``hospital_id``.
        """
        stmt = select(Doctor) if include_deleted else self._query()
        return stmt.where(Doctor.hospital_id == hospital_id)

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[Doctor]],
        *,
        term: str | None = None,
        specialization: str | None = None,
        department_id: uuid.UUID | None = None,
    ) -> Select[tuple[Doctor]]:
        """Apply the filters shared by list, search, and count (module spec §9).

        ``term`` prefix-matches the doctor's first and last name
        case-insensitively — which lives on ``users``, hence the join — and
        exact-matches the licence number. Prefix rather than substring so the
        name indexes stay usable; exact on licence because a partial licence is
        not a meaningful query.

        :param stmt: The statement to extend.
        :param term: Free-text term.
        :param specialization: Exact specialization filter.
        :param department_id: Exact department filter.
        :returns: The statement with predicates applied.
        """
        if term:
            # An explicit join, not a reliance on the `lazy="joined"` eager load:
            # the eager load's alias is not addressable in a WHERE clause.
            stmt = stmt.join(User, User.id == Doctor.user_id)
            needle = term.lower()
            prefix = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stmt = stmt.where(
                or_(
                    func.lower(User.first_name).like(prefix, escape="\\"),
                    func.lower(User.last_name).like(prefix, escape="\\"),
                    Doctor.license_number == term,
                )
            )
        if specialization:
            stmt = stmt.where(func.lower(Doctor.specialization) == specialization.lower())
        if department_id is not None:
            stmt = stmt.where(Doctor.department_id == department_id)
        return stmt

    @staticmethod
    def _ordered(stmt: Select[tuple[Doctor]]) -> Select[tuple[Doctor]]:
        """Order by specialization then id.

        The ``id`` tiebreak keeps pagination stable: without it two doctors in
        the same specialization can swap places between page requests, showing
        one twice and skipping another.
        """
        return stmt.order_by(Doctor.specialization.asc(), Doctor.id.asc())

    # ── Directory ────────────────────────────────────────────────────────────
    # The doctors of one hospital that may be shown to somebody outside it: not
    # deactivated, backed by an active user of that same hospital, and with at
    # least one availability window. Read as columns, so nothing but what a
    # directory shows ever leaves the database.

    @staticmethod
    def _in_directory(hospital_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
        """The predicates of the directory, over ``doctors`` joined to ``users``."""
        return (
            Doctor.hospital_id == hospital_id,
            Doctor.deleted_at.is_(None),
            User.hospital_id == hospital_id,
            User.deleted_at.is_(None),
            User.status == UserStatus.ACTIVE,
            exists().where(
                DoctorAvailability.doctor_id == Doctor.id,
                DoctorAvailability.hospital_id == hospital_id,
            ),
        )

    @staticmethod
    def _directory_select(hospital_id: uuid.UUID) -> Select[Any]:
        """The directory's columns, with the doctor's department if it is active."""
        return (
            select(
                Doctor.id,
                User.first_name,
                User.last_name,
                Doctor.specialization,
                Doctor.qualifications,
                Doctor.languages,
                Doctor.bio,
                Department.id.label("department_id"),
                Department.name.label("department_name"),
            )
            .select_from(Doctor)
            .join(User, User.id == Doctor.user_id)
            .outerjoin(
                Department,
                and_(
                    Department.id == Doctor.department_id,
                    Department.hospital_id == hospital_id,
                    Department.deleted_at.is_(None),
                ),
            )
            .where(*DoctorRepository._in_directory(hospital_id))
        )

    @staticmethod
    def _directory_entry(row: Row[Any]) -> DoctorDirectoryEntry:
        """Build an entry from a row of :meth:`_directory_select`."""
        return DoctorDirectoryEntry(
            id=row.id,
            first_name=row.first_name,
            last_name=row.last_name,
            specialization=row.specialization,
            qualifications=row.qualifications,
            languages=row.languages,
            bio=row.bio,
            department_id=row.department_id,
            department_name=row.department_name,
        )

    async def list_directory(
        self,
        hospital_id: uuid.UUID,
        *,
        search: str | None = None,
        department_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[DoctorDirectoryEntry], int]:
        """One page of the doctors of a hospital that a directory may show.

        Ordered by family name then given name without regard to case, then by
        id — which is unique, so a page never repeats or skips a doctor.

        :param hospital_id: The tenant to scope to.
        :param search: Text the full name or the specialization must contain,
            case-insensitively. Matched literally: ``%``, ``_`` and ``\\`` are
            not wildcards.
        :param department_id: The active department the doctor must be in.
        :param skip: Number of rows to skip.
        :param limit: Maximum rows to return.
        :returns: The page, and how many doctors match in all.
        """
        stmt = self._directory_select(hospital_id)
        if search:
            literal = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{literal}%"
            full_name = User.first_name + " " + User.last_name
            stmt = stmt.where(
                or_(
                    full_name.ilike(pattern, escape="\\"),
                    Doctor.specialization.ilike(pattern, escape="\\"),
                )
            )
        if department_id is not None:
            stmt = stmt.where(Department.id == department_id)

        total = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.order_by(func.lower(User.last_name), func.lower(User.first_name), Doctor.id)
            .offset(skip)
            .limit(limit)
        )
        return [self._directory_entry(row) for row in rows], total.scalar_one()

    async def get_directory_entry(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID
    ) -> DoctorDirectoryEntry | None:
        """Read one doctor of a hospital, if a directory may show them.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor's UUID.
        :returns: The entry, or ``None`` — also for a doctor of another hospital.
        """
        row = (
            await self._session.execute(
                self._directory_select(hospital_id).where(Doctor.id == doctor_id)
            )
        ).one_or_none()
        return None if row is None else self._directory_entry(row)

    async def list_directory_departments(
        self, hospital_id: uuid.UUID, *, limit: int
    ) -> list[DirectoryDepartment]:
        """The active departments of a hospital that have a doctor in the directory.

        :param hospital_id: The tenant to scope to.
        :param limit: Maximum number of departments to return.
        :returns: The departments, by name then id.
        """
        has_listed_doctor = (
            exists()
            .where(Doctor.department_id == Department.id, User.id == Doctor.user_id)
            .where(*self._in_directory(hospital_id))
        )
        rows = await self._session.execute(
            select(Department.id, Department.name, Department.description)
            .where(
                Department.hospital_id == hospital_id,
                Department.deleted_at.is_(None),
                has_listed_doctor,
            )
            .order_by(func.lower(Department.name), Department.id)
            .limit(limit)
        )
        return [
            DirectoryDepartment(id=row.id, name=row.name, description=row.description)
            for row in rows
        ]

    # ── Doctor commands ───────────────────────────────────────────────────────

    async def create_doctor(
        self,
        *,
        hospital_id: uuid.UUID,
        user_id: uuid.UUID,
        specialization: str,
        license_number: str,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> Doctor:
        """Insert a new doctor. Does not commit — the service owns the transaction.

        :param hospital_id: Owning tenant.
        :param user_id: The user this doctor profile belongs to.
        :param specialization: Clinical specialization.
        :param license_number: Medical council licence number.
        :param created_by: UUID of the acting user.
        :param optional_fields: Any remaining doctor columns.
        :returns: The persisted doctor.
        """
        return await super().create(
            hospital_id=hospital_id,
            user_id=user_id,
            specialization=specialization,
            license_number=license_number,
            created_by=created_by,
            **optional_fields,
        )

    async def update_doctor(
        self, doctor: Doctor, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> Doctor:
        """Apply field updates to an existing doctor.

        :param doctor: The attached ORM instance to modify.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated doctor.
        """
        return await self.update(doctor, updated_by=updated_by, **fields)

    async def delete_doctor(self, doctor: Doctor, *, deleted_by: uuid.UUID | None = None) -> Doctor:
        """Soft-delete a doctor and record who did it.

        Doctors are never hard-deleted: appointments, consultations, and
        invoices reference them (module spec §4, rule 7).

        :param doctor: The doctor to deactivate.
        :param deleted_by: UUID of the acting user.
        :returns: The soft-deleted doctor.
        """
        doctor.deleted_by = deleted_by
        return await self.soft_delete(doctor)

    async def restore_doctor(
        self, doctor: Doctor, *, updated_by: uuid.UUID | None = None
    ) -> Doctor:
        """Clear the soft delete on a doctor.

        :param doctor: The soft-deleted doctor to restore.
        :param updated_by: UUID of the acting user.
        :returns: The reactivated doctor.
        """
        return await self.update(doctor, deleted_at=None, deleted_by=None, updated_by=updated_by)

    # ── Doctor queries ────────────────────────────────────────────────────────

    async def get_doctor_by_id(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> Doctor | None:
        """Retrieve one doctor by UUID within a hospital.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor UUID.
        :param include_deleted: Include soft-deleted records.
        :returns: The doctor, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(
            Doctor.id == doctor_id
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_doctor_by_user_id(
        self,
        hospital_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        include_deleted: bool = True,
    ) -> Doctor | None:
        """Retrieve the doctor profile attached to a user.

        Defaults to including soft-deleted rows: ``uq_doctors_user_id`` applies
        regardless of deletion state, so a duplicate check that ignored
        deactivated doctors would report a user as free when the unique index
        will reject it.

        :param hospital_id: The tenant to scope to.
        :param user_id: The user UUID.
        :param include_deleted: Include soft-deleted records.
        :returns: The doctor, or ``None``.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(
            Doctor.user_id == user_id
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_doctors(
        self,
        hospital_id: uuid.UUID,
        *,
        term: str | None = None,
        specialization: str | None = None,
        department_id: uuid.UUID | None = None,
        include_deleted: bool = False,
        skip: int = 0,
        limit: int = 25,
    ) -> list[Doctor]:
        """List or search doctors in a hospital.

        :param hospital_id: The tenant to scope to.
        :param term: Free-text term (name prefix or exact licence).
        :param specialization: Exact specialization filter.
        :param department_id: Exact department filter.
        :param include_deleted: Include deactivated doctors.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :returns: A page of doctors.
        """
        stmt = self._apply_filters(
            self._scoped(hospital_id, include_deleted=include_deleted),
            term=term,
            specialization=specialization,
            department_id=department_id,
        )
        stmt = self._apply_pagination(self._ordered(stmt), skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_doctors(
        self,
        hospital_id: uuid.UUID,
        *,
        term: str | None = None,
        specialization: str | None = None,
        department_id: uuid.UUID | None = None,
        include_deleted: bool = False,
    ) -> int:
        """Count doctors matching the same filters :meth:`list_doctors` uses.

        Takes identical filter arguments so a paginated total can never
        disagree with the rows on the page.

        :param hospital_id: The tenant to scope to.
        :param term: Free-text term.
        :param specialization: Exact specialization filter.
        :param department_id: Exact department filter.
        :param include_deleted: Include deactivated doctors.
        :returns: The number of matching doctors.
        """
        stmt = self._apply_filters(
            self._scoped(hospital_id, include_deleted=include_deleted),
            term=term,
            specialization=specialization,
            department_id=department_id,
        )
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one()

    async def doctor_exists(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> bool:
        """Check whether a doctor exists in a hospital.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor UUID.
        :param include_deleted: Count deactivated doctors as existing.
        :returns: ``True`` if the doctor exists in this tenant.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(
            Doctor.id == doctor_id
        )
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one() > 0

    async def count_active_by_department(
        self, hospital_id: uuid.UUID, department_id: uuid.UUID
    ) -> int:
        """Count active doctors assigned to a department.

        This is the query behind
        :class:`~app.services.department_service.DepartmentUsageSource`, which
        the Department module has been depending on through a null
        implementation since it shipped. A plain ``COUNT`` rather than loading
        ``Department.doctors``: the caller only needs to know whether the number
        is zero.

        :param hospital_id: The tenant to scope to.
        :param department_id: The department being checked.
        :returns: The number of active doctors assigned.
        """
        stmt = select(func.count()).select_from(
            self._scoped(hospital_id).where(Doctor.department_id == department_id).subquery()
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    # ── Availability ──────────────────────────────────────────────────────────

    async def get_availability(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID
    ) -> list[DoctorAvailability]:
        """Return a doctor's weekly availability, ordered for display.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor whose schedule to read.
        :returns: Availability rows ordered by day then start time.
        """
        stmt = (
            select(DoctorAvailability)
            .where(
                DoctorAvailability.hospital_id == hospital_id,
                DoctorAvailability.doctor_id == doctor_id,
                DoctorAvailability.deleted_at.is_(None),
            )
            .order_by(
                DoctorAvailability.day_of_week.asc(),
                DoctorAvailability.start_time.asc(),
            )
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def replace_availability(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        entries: list[dict[str, Any]],
        *,
        actor_id: uuid.UUID | None = None,
    ) -> list[DoctorAvailability]:
        """Replace a doctor's entire weekly availability atomically.

        Module spec §5.2 step 3: old rows are cleared and new ones inserted in
        one transaction, so a reader never observes a half-applied schedule.

        A hard ``DELETE`` rather than a soft delete, deliberately: availability
        is configuration, not a business record. Soft-deleting would accumulate
        a row per edit forever and force every read to filter them out.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor whose schedule to replace.
        :param entries: New availability rows as column dicts.
        :param actor_id: UUID of the acting user.
        :returns: The newly persisted availability, ordered.
        """
        await self._session.execute(
            delete(DoctorAvailability).where(
                DoctorAvailability.hospital_id == hospital_id,
                DoctorAvailability.doctor_id == doctor_id,
            )
        )

        for entry in entries:
            self._session.add(
                DoctorAvailability(
                    hospital_id=hospital_id,
                    doctor_id=doctor_id,
                    created_by=actor_id,
                    updated_by=actor_id,
                    **entry,
                )
            )
        await self._session.flush()

        return await self.get_availability(hospital_id, doctor_id)

    # ── Leaves ────────────────────────────────────────────────────────────────

    async def create_leave(
        self,
        *,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        starts_at: datetime,
        ends_at: datetime,
        reason: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> DoctorLeave:
        """Insert a leave interval.

        :param hospital_id: Owning tenant.
        :param doctor_id: The doctor taking leave.
        :param starts_at: Inclusive start (UTC).
        :param ends_at: Exclusive end (UTC).
        :param reason: Free-text reason.
        :param created_by: UUID of the acting user.
        :returns: The persisted leave.
        """
        leave = DoctorLeave(
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=reason,
            created_by=created_by,
        )
        self._session.add(leave)
        await self._session.flush()
        await self._session.refresh(leave)
        return leave

    async def get_leave_by_id(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID, leave_id: uuid.UUID
    ) -> DoctorLeave | None:
        """Retrieve one leave belonging to a specific doctor.

        Scoped by doctor as well as hospital so a leave id from another doctor
        cannot be deleted by guessing.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The owning doctor.
        :param leave_id: The leave UUID.
        :returns: The leave, or ``None``.
        """
        stmt = select(DoctorLeave).where(
            DoctorLeave.hospital_id == hospital_id,
            DoctorLeave.doctor_id == doctor_id,
            DoctorLeave.id == leave_id,
            DoctorLeave.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_leaves(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        *,
        starts_before: datetime | None = None,
        ends_after: datetime | None = None,
    ) -> list[DoctorLeave]:
        """List a doctor's leaves, optionally bounded to an interval.

        Passing both bounds returns every leave *overlapping* the window, which
        is what slot generation needs — a leave that starts before the day and
        ends after it still blocks the whole day.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor whose leaves to read.
        :param starts_before: Only leaves starting strictly before this instant.
        :param ends_after: Only leaves ending strictly after this instant.
        :returns: Matching leaves, earliest first.
        """
        stmt = select(DoctorLeave).where(
            DoctorLeave.hospital_id == hospital_id,
            DoctorLeave.doctor_id == doctor_id,
            DoctorLeave.deleted_at.is_(None),
        )
        if starts_before is not None:
            stmt = stmt.where(DoctorLeave.starts_at < starts_before)
        if ends_after is not None:
            stmt = stmt.where(DoctorLeave.ends_at > ends_after)
        stmt = stmt.order_by(DoctorLeave.starts_at.asc(), DoctorLeave.id.asc())
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def find_overlapping_leaves(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        starts_at: datetime,
        ends_at: datetime,
        *,
        exclude_leave_id: uuid.UUID | None = None,
    ) -> list[DoctorLeave]:
        """Find existing leaves that overlap a proposed interval.

        Two half-open intervals overlap when ``existing.starts_at < new.ends_at``
        and ``existing.ends_at > new.starts_at``. Stated that way, back-to-back
        leaves — one ending exactly when the next begins — correctly do *not*
        overlap.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor to check.
        :param starts_at: Proposed start (UTC).
        :param ends_at: Proposed end (UTC).
        :param exclude_leave_id: A leave to ignore, when re-checking an edit.
        :returns: Overlapping leaves, earliest first.
        """
        stmt = select(DoctorLeave).where(
            DoctorLeave.hospital_id == hospital_id,
            DoctorLeave.doctor_id == doctor_id,
            DoctorLeave.deleted_at.is_(None),
            DoctorLeave.starts_at < ends_at,
            DoctorLeave.ends_at > starts_at,
        )
        if exclude_leave_id is not None:
            stmt = stmt.where(DoctorLeave.id != exclude_leave_id)
        stmt = stmt.order_by(DoctorLeave.starts_at.asc())
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def delete_leave(
        self, leave: DoctorLeave, *, deleted_by: uuid.UUID | None = None
    ) -> DoctorLeave:
        """Soft-delete a leave.

        Soft rather than hard, unlike availability: cancelling a leave is a
        business event worth keeping an audit trail of.

        :param leave: The leave to cancel.
        :param deleted_by: UUID of the acting user.
        :returns: The cancelled leave.
        """
        leave.deleted_by = deleted_by
        leave.deleted_at = func.now()
        await self._session.flush()
        await self._session.refresh(leave)
        return leave
