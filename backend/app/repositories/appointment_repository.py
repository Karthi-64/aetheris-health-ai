"""Repository for the appointment aggregate.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it, including status history (CLAUDE.md rules 4 and 5).

Covers both tables because ``appointment_status_history`` is never addressed
except through its appointment — they are one consistency boundary, and a
transition plus its history row must be written together (business rule 7).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, and_, func, not_, select, tuple_

from app.models.appointment import (
    SLOT_FREEING_STATUSES,
    TERMINAL_STATUSES,
    Appointment,
    AppointmentStatus,
    AppointmentStatusHistory,
    AppointmentType,
)
from app.repositories.base import BaseRepository

#: Statuses of an appointment that is still to happen or is happening.
_OCCUPYING_AHEAD: frozenset[AppointmentStatus] = frozenset(
    {AppointmentStatus.BOOKED, AppointmentStatus.CHECKED_IN, AppointmentStatus.IN_PROGRESS}
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import ColumnElement

    from app.core.tenancy import CrossTenant


class AppointmentRepository(BaseRepository[Appointment]):
    """Persistence for appointments and their status history.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Appointment, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _scoped(
        self, hospital_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Select[tuple[Appointment]]:
        """Return a base SELECT filtered to one hospital."""
        stmt = select(Appointment) if include_deleted else self._query()
        return stmt.where(Appointment.hospital_id == hospital_id)

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[Appointment]],
        *,
        patient_id: uuid.UUID | None = None,
        doctor_id: uuid.UUID | None = None,
        status: AppointmentStatus | None = None,
        appointment_type: AppointmentType | None = None,
        starts_on_or_after: datetime | None = None,
        starts_before: datetime | None = None,
    ) -> Select[tuple[Appointment]]:
        """Apply the filters shared by list and count (module spec §9).

        The date filter arrives as a half-open UTC interval rather than a naive
        date, because "today" depends on the hospital's timezone and that
        conversion is a business rule, not a query concern.

        :param stmt: The statement to extend.
        :param patient_id: Exact patient filter.
        :param doctor_id: Exact doctor filter.
        :param status: Exact status filter.
        :param appointment_type: Exact type filter.
        :param starts_on_or_after: Lower bound on ``scheduled_start``.
        :param starts_before: Upper bound on ``scheduled_start``, exclusive.
        :returns: The statement with predicates applied.
        """
        if patient_id is not None:
            stmt = stmt.where(Appointment.patient_id == patient_id)
        if doctor_id is not None:
            stmt = stmt.where(Appointment.doctor_id == doctor_id)
        if status is not None:
            stmt = stmt.where(Appointment.status == status)
        if appointment_type is not None:
            stmt = stmt.where(Appointment.type == appointment_type)
        if starts_on_or_after is not None:
            stmt = stmt.where(Appointment.scheduled_start >= starts_on_or_after)
        if starts_before is not None:
            stmt = stmt.where(Appointment.scheduled_start < starts_before)
        return stmt

    @staticmethod
    def _ordered(stmt: Select[tuple[Appointment]]) -> Select[tuple[Appointment]]:
        """Order chronologically, with ``id`` as a stable tiebreaker.

        Two appointments can share a start time across different doctors, so
        without the tiebreak pagination could show one twice and skip another.
        """
        return stmt.order_by(Appointment.scheduled_start.asc(), Appointment.id.asc())

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_appointment(
        self,
        *,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        doctor_id: uuid.UUID,
        scheduled_start: datetime,
        scheduled_end: datetime,
        appointment_type: AppointmentType,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> Appointment:
        """Insert an appointment in the ``booked`` state.

        Does not commit — the service owns the transaction, and the status
        history row must land in the same one (business rule 7).

        :param hospital_id: Owning tenant.
        :param patient_id: Patient being seen.
        :param doctor_id: Doctor seeing them.
        :param scheduled_start: Start instant (UTC).
        :param scheduled_end: End instant (UTC).
        :param appointment_type: new / follow_up / walk_in / emergency.
        :param created_by: UUID of the acting user.
        :param optional_fields: Remaining columns (reason, notes, idempotency_key).
        :returns: The persisted appointment.
        """
        return await super().create(
            hospital_id=hospital_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            type=appointment_type,
            status=AppointmentStatus.BOOKED,
            created_by=created_by,
            **optional_fields,
        )

    async def update_appointment(
        self, appointment: Appointment, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> Appointment:
        """Apply field updates to an existing appointment.

        :param appointment: The attached ORM instance to modify.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated appointment.
        """
        return await self.update(appointment, updated_by=updated_by, **fields)

    async def record_transition(
        self,
        *,
        appointment: Appointment,
        from_status: AppointmentStatus | None,
        to_status: AppointmentStatus,
        changed_by: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> AppointmentStatusHistory:
        """Append an immutable status-history row (business rule 7, AC-6).

        Deliberately does *not* mutate ``appointment.status`` — the service
        does that, so the write and its record are visibly paired at the call
        site rather than hidden in a helper.

        :param appointment: The appointment that changed.
        :param from_status: Previous status. ``None`` when first booked.
        :param to_status: Status after the change.
        :param changed_by: Acting user. ``None`` means the system acted.
        :param reason: Why the change happened.
        :returns: The persisted history row.
        """
        entry = AppointmentStatusHistory(
            appointment_id=appointment.id,
            hospital_id=appointment.hospital_id,
            from_status=from_status,
            to_status=to_status,
            changed_by=changed_by,
            reason=reason,
        )
        self._session.add(entry)
        await self._session.flush()
        await self._session.refresh(entry)
        return entry

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_appointment_by_id(
        self,
        hospital_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> Appointment | None:
        """Retrieve one appointment by UUID within a hospital.

        :param hospital_id: The tenant to scope to.
        :param appointment_id: The appointment UUID.
        :param include_deleted: Include soft-deleted records.
        :returns: The appointment, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(
            Appointment.id == appointment_id
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def lock_appointment_by_id(
        self, hospital_id: uuid.UUID, appointment_id: uuid.UUID
    ) -> Appointment | None:
        """Read one appointment as it is now and lock its row until the transaction ends.

        For a status change: two transitions of one appointment must not both
        read the same starting status. The second waits here, then sees what
        the first committed.

        :param hospital_id: The tenant to scope to.
        :param appointment_id: The appointment to lock.
        :returns: The appointment with its current values, or ``None``.
        """
        stmt = (
            self._scoped(hospital_id)
            .where(Appointment.id == appointment_id)
            # Only the appointment row: the eager-loaded patient and doctor are
            # outer joins, which ``FOR UPDATE`` cannot lock and need not.
            .with_for_update(of=Appointment)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_for_patient_records(
        self,
        records: list[tuple[uuid.UUID, uuid.UUID]],
        scope: uuid.UUID | CrossTenant,
        *,
        upcoming: bool,
        now: datetime,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[Appointment], int]:
        """One page of the appointments of some patient records, and their total.

        For a patient's own list across the hospitals they are linked at: each
        record is named together with its hospital, so a row is returned only
        where both match.

        *Upcoming* is an appointment that still occupies time ahead: not
        finished, cancelled or missed, and not yet ended — soonest first.
        *Past* is every other one, most recent first.

        :param records: ``(hospital_id, patient_id)`` pairs. Empty means none.
        :param scope: A hospital id, or a ``cross_tenant(...)`` marker for a
            list that spans the caller's own hospitals.
        :param upcoming: Which of the two lists.
        :param now: The current instant (UTC).
        :param skip: Rows to skip.
        :param limit: Maximum rows to return.
        :returns: The page, and how many appointments the list holds in all.
        """
        if not records:
            return [], 0
        ahead = and_(
            Appointment.status.in_(tuple(_OCCUPYING_AHEAD)), Appointment.scheduled_end > now
        )
        stmt = self._confine_to_tenant(
            self._query().where(self._owned_by(records), ahead if upcoming else not_(ahead)),
            scope,
            "list_for_patient_records",
        )
        total = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        order = (
            (Appointment.scheduled_start.asc(), Appointment.id.asc())
            if upcoming
            else (Appointment.scheduled_start.desc(), Appointment.id.desc())
        )
        rows = await self._session.execute(stmt.order_by(*order).offset(skip).limit(limit))
        return list(rows.unique().scalars().all()), total.scalar_one()

    async def get_for_patient_records(
        self,
        records: list[tuple[uuid.UUID, uuid.UUID]],
        scope: uuid.UUID | CrossTenant,
        appointment_id: uuid.UUID,
    ) -> Appointment | None:
        """Read one appointment if it belongs to one of some patient records.

        :param records: ``(hospital_id, patient_id)`` pairs. Empty means none.
        :param scope: A hospital id, or a ``cross_tenant(...)`` marker.
        :param appointment_id: The appointment.
        :returns: The appointment, or ``None`` — also when it is somebody else's.
        """
        if not records:
            return None
        stmt = self._confine_to_tenant(
            self._query().where(Appointment.id == appointment_id, self._owned_by(records)),
            scope,
            "get_for_patient_records",
        ).execution_options(populate_existing=True)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    @staticmethod
    def _owned_by(records: list[tuple[uuid.UUID, uuid.UUID]]) -> ColumnElement[bool]:
        """The appointment is at one of the hospitals, for that hospital's record."""
        return tuple_(Appointment.hospital_id, Appointment.patient_id).in_(records)

    async def get_by_idempotency_key(
        self, hospital_id: uuid.UUID, idempotency_key: str
    ) -> Appointment | None:
        """Find an appointment already booked under this key (business rule 8).

        Includes soft-deleted rows: the unique index does, so a key that looks
        free here but is taken there would surface as a 500 rather than a
        replayed response.

        :param hospital_id: The tenant to scope to.
        :param idempotency_key: The client-supplied key.
        :returns: The original appointment, or ``None``.
        """
        stmt = self._scoped(hospital_id, include_deleted=True).where(
            Appointment.idempotency_key == idempotency_key
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_appointments(
        self,
        hospital_id: uuid.UUID,
        *,
        skip: int = 0,
        limit: int = 25,
        **filters: Any,
    ) -> list[Appointment]:
        """List appointments in a hospital, earliest first.

        :param hospital_id: The tenant to scope to.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        :returns: A page of appointments.
        """
        include_deleted = bool(filters.pop("include_deleted", False))
        stmt = self._apply_filters(
            self._scoped(hospital_id, include_deleted=include_deleted), **filters
        )
        stmt = self._apply_pagination(self._ordered(stmt), skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_appointments(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count appointments matching the same filters :meth:`list_appointments` uses.

        :param hospital_id: The tenant to scope to.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        :returns: The number of matching appointments.
        """
        include_deleted = bool(filters.pop("include_deleted", False))
        stmt = self._apply_filters(
            self._scoped(hospital_id, include_deleted=include_deleted), **filters
        )
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one()

    async def find_overlapping(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        scheduled_start: datetime,
        scheduled_end: datetime,
        *,
        exclude_appointment_id: uuid.UUID | None = None,
    ) -> list[Appointment]:
        """Find appointments that would clash with a proposed booking.

        The database's exclusion constraint is the real guarantee; this query
        exists so the common case returns a clear 409 naming the clash instead
        of surfacing a raw IntegrityError. Cancelled and no-show appointments
        are excluded because they no longer hold the slot — matching the
        constraint's own ``WHERE`` clause.

        Half-open comparison, so an appointment ending exactly when the next
        begins is not a clash.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor whose calendar to check.
        :param scheduled_start: Proposed start (UTC).
        :param scheduled_end: Proposed end (UTC).
        :param exclude_appointment_id: Ignore this appointment — used when
            rescheduling, so a booking does not clash with itself.
        :returns: Clashing appointments, earliest first.
        """
        stmt = self._scoped(hospital_id).where(
            Appointment.doctor_id == doctor_id,
            Appointment.status.not_in(tuple(SLOT_FREEING_STATUSES)),
            Appointment.scheduled_start < scheduled_end,
            Appointment.scheduled_end > scheduled_start,
        )
        if exclude_appointment_id is not None:
            stmt = stmt.where(Appointment.id != exclude_appointment_id)
        result = await self._session.execute(self._ordered(stmt))
        return list(result.unique().scalars().all())

    async def find_overlapping_for_patient(
        self,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        scheduled_start: datetime,
        scheduled_end: datetime,
    ) -> list[Appointment]:
        """Find a patient's own appointments that overlap a proposed window.

        The patient-side twin of :meth:`find_overlapping`: the same half-open
        comparison and the same statuses — an appointment that freed its slot
        no longer occupies the patient's time either.

        :param hospital_id: The tenant to scope to.
        :param patient_id: The patient whose calendar to check.
        :param scheduled_start: Proposed start (UTC).
        :param scheduled_end: Proposed end (UTC).
        :returns: Overlapping appointments, earliest first.
        """
        stmt = self._scoped(hospital_id).where(
            Appointment.patient_id == patient_id,
            Appointment.status.not_in(tuple(SLOT_FREEING_STATUSES)),
            Appointment.scheduled_start < scheduled_end,
            Appointment.scheduled_end > scheduled_start,
        )
        result = await self._session.execute(self._ordered(stmt))
        return list(result.unique().scalars().all())

    async def count_upcoming_booked_for_patient(
        self, hospital_id: uuid.UUID, patient_id: uuid.UUID, *, after: datetime
    ) -> int:
        """Count a patient's ``booked`` appointments that have not started yet.

        :param hospital_id: The tenant to scope to.
        :param patient_id: The patient to count for.
        :param after: Only appointments starting after this instant (UTC).
        :returns: The number of upcoming booked appointments.
        """
        stmt = self._scoped(hospital_id).where(
            Appointment.patient_id == patient_id,
            Appointment.status == AppointmentStatus.BOOKED,
            Appointment.scheduled_start > after,
        )
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()

    async def booked_intervals_for_doctor(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> list[Appointment]:
        """Return appointments overlapping a window, for slot generation.

        This backs the ``BookedIntervalSource`` the Doctor module has been
        depending on through a null implementation. Only slot-occupying
        appointments count: a cancelled booking must not grey out a slot that
        is genuinely free.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor whose calendar to read.
        :param window_start: Window start (UTC).
        :param window_end: Window end (UTC).
        :returns: Overlapping appointments, earliest first.
        """
        stmt = self._scoped(hospital_id).where(
            Appointment.doctor_id == doctor_id,
            Appointment.status.not_in(tuple(SLOT_FREEING_STATUSES)),
            Appointment.scheduled_start < window_end,
            Appointment.scheduled_end > window_start,
        )
        result = await self._session.execute(self._ordered(stmt))
        return list(result.unique().scalars().all())

    async def count_future_for_doctor(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID, *, after: datetime
    ) -> int:
        """Count a doctor's upcoming appointments.

        Backs the Doctor module's FR-5 guard — a doctor with *active* future
        appointments cannot be deactivated.

        Excludes every **terminal** status, not just the slot-freeing ones.
        FR-5 is about work that still needs reassigning, and a completed
        consultation needs none: it already happened. Using
        ``SLOT_FREEING_STATUSES`` here would count a completed appointment as
        blocking and leave the doctor undeactivatable forever, since nothing
        can move a completed appointment out of that state.

        (``booked_intervals_for_doctor`` above deliberately keeps the narrower
        set: a completed appointment *did* occupy its slot, so the calendar
        must still show it as taken.)

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor to check.
        :param after: Only appointments starting after this instant (UTC).
        :returns: The number of upcoming appointments needing reassignment.
        """
        stmt = self._scoped(hospital_id).where(
            Appointment.doctor_id == doctor_id,
            Appointment.status.not_in(tuple(TERMINAL_STATUSES)),
            Appointment.scheduled_start > after,
        )
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one()

    async def find_no_show_candidates(
        self, *, cutoff: datetime, limit: int = 500
    ) -> list[Appointment]:
        """Find appointments that should be marked ``no_show`` (module spec §5.7).

        Deliberately **not** scoped to one hospital: the sweeper is a
        platform-wide background job. Every other read path in this repository
        is tenant-scoped; this is the single, documented exception. The caller
        must run it under an explicit ``TenantScope.system(...)`` and then
        handle each returned appointment under that appointment's own hospital
        (see ``AppointmentService.sweep_no_shows``). It must never be called
        from a request.

        :param cutoff: Appointments whose ``scheduled_end`` is before this
            instant are overdue. The caller subtracts the grace period.
        :param limit: Cap per sweep, so one run cannot monopolise the worker.
        :returns: Overdue appointments, earliest first.
        """
        stmt = (
            self._query()
            .where(
                Appointment.status.in_((AppointmentStatus.BOOKED, AppointmentStatus.CHECKED_IN)),
                Appointment.scheduled_end < cutoff,
            )
            .order_by(Appointment.scheduled_end.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def list_walk_in_queue(
        self, hospital_id: uuid.UUID, *, doctor_id: uuid.UUID | None = None
    ) -> list[Appointment]:
        """Return today's unfinished walk-ins, in arrival order (module spec §5.8).

        Arrival order is ``checked_in_at`` when the patient has arrived, and
        ``created_at`` otherwise — a walk-in is created at the desk the moment
        the patient turns up, so that is when they joined the queue.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: Optionally narrow to one doctor.
        :returns: Queued walk-ins in arrival order.
        """
        stmt = self._scoped(hospital_id).where(
            Appointment.type == AppointmentType.WALK_IN,
            Appointment.status.in_((AppointmentStatus.BOOKED, AppointmentStatus.CHECKED_IN)),
        )
        if doctor_id is not None:
            stmt = stmt.where(Appointment.doctor_id == doctor_id)
        stmt = stmt.order_by(
            func.coalesce(Appointment.checked_in_at, Appointment.created_at).asc(),
            Appointment.id.asc(),
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def get_status_history(
        self, hospital_id: uuid.UUID, appointment_id: uuid.UUID
    ) -> list[AppointmentStatusHistory]:
        """Return an appointment's transitions, oldest first.

        :param hospital_id: The tenant to scope to.
        :param appointment_id: The appointment whose history to read.
        :returns: History rows in chronological order.
        """
        stmt = (
            select(AppointmentStatusHistory)
            .where(
                AppointmentStatusHistory.hospital_id == hospital_id,
                AppointmentStatusHistory.appointment_id == appointment_id,
            )
            .order_by(
                AppointmentStatusHistory.changed_at.asc(),
                AppointmentStatusHistory.id.asc(),
            )
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def appointment_exists(self, hospital_id: uuid.UUID, appointment_id: uuid.UUID) -> bool:
        """Check whether an appointment exists in a hospital.

        :param hospital_id: The tenant to scope to.
        :param appointment_id: The appointment UUID.
        :returns: ``True`` if it exists in this tenant.
        """
        stmt = self._scoped(hospital_id).where(Appointment.id == appointment_id)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one() > 0
