"""Business logic for the Doctor Management module.

Owns every rule in ``docs/modules/04-doctor-management.md`` §4, the transaction
boundary for doctor writes (``docs/03-ARCHITECTURE.md`` §9), and the audit
record for every mutation (CLAUDE.md rule 9). Returns Pydantic DTOs, never ORM
models.

**Slot generation is a pure function.** :func:`generate_slots` takes availability,
leaves, booked intervals and a timezone, and returns slots. It touches no
database and no clock, which is what makes the DST case in module spec §14 and
AC-2 testable without fixtures. The service's job is to gather those four
inputs and hand them over.

**The appointments seam.** Business rule 4 subtracts booked appointments from
generated slots, and FR-5 blocks deleting a doctor with future appointments —
both need a module that does not exist yet. :class:`BookedIntervalSource` is the
interface; :class:`NullBookedIntervalSource` answers "nothing booked" until
Appointment Management supplies a real one. Identical in shape to the
``DepartmentUsageSource`` this module now *implements* for Departments, and to
:class:`~app.core.audit.AuditSink`.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.audit import AuditEvent
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.doctor import SlotStatus
from app.schemas.common import Page, PaginationParams
from app.schemas.doctor import (
    AvailabilityResponse,
    CreateDoctorRequest,
    CreateLeaveRequest,
    DaySlotsResponse,
    DoctorResponse,
    DoctorSummaryResponse,
    LeaveResponse,
    SetAvailabilityRequest,
    SlotResponse,
    UpdateDoctorRequest,
)

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.doctor import Doctor
    from app.repositories.department_repository import DepartmentRepository
    from app.repositories.doctor_repository import DoctorRepository
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.user_repository import UserRepository

logger = get_logger(__name__)

__all__ = [
    "BookedInterval",
    "BookedIntervalSource",
    "DoctorHasAppointmentsError",
    "DoctorNotFoundError",
    "DoctorService",
    "DuplicateDoctorProfileError",
    "LeaveNotFoundError",
    "NullBookedIntervalSource",
    "OverlappingLeaveError",
    "generate_slots",
]

#: Local midnight — the anchor for turning a local date into the pair of UTC
#: instants that bound it.
_MIDNIGHT = time(0, 0)

#: Columns a client may write through create/update. Anything outside this set
#: is dropped before it reaches the repository, so a widened DTO cannot
#: silently widen what is writable.
_WRITABLE_COLUMNS = frozenset(
    {
        "specialization",
        "license_number",
        "consultation_fee",
        "department_id",
        "qualifications",
        "languages",
        "bio",
    }
)


# ── The appointments seam ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BookedInterval:
    """One booked appointment, reduced to what slot generation needs.

    :param starts_at: Appointment start (UTC).
    :param ends_at: Appointment end (UTC).
    :param appointment_id: UUID echoed back on the booked slot.
    """

    starts_at: datetime
    ends_at: datetime
    appointment_id: uuid.UUID


@runtime_checkable
class BookedIntervalSource(Protocol):
    """Supplies the appointment facts this module needs but does not own.

    Implemented today by :class:`NullBookedIntervalSource` and, once
    ``docs/modules/05-appointment-management.md`` ships, by an adapter over the
    appointment repository. Depending on this protocol keeps the dependency
    pointing one way: Appointments knows about Doctors, not the reverse.
    """

    async def booked_intervals(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> list[BookedInterval]:
        """Return appointments overlapping a window.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor whose calendar to read.
        :param window_start: Window start (UTC).
        :param window_end: Window end (UTC).
        :returns: Overlapping booked intervals.
        """
        ...

    async def has_future_appointments(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID, *, after: datetime
    ) -> int:
        """Count a doctor's appointments starting after an instant.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor to check.
        :param after: Only appointments starting after this instant (UTC).
        :returns: The number of future appointments.
        """
        ...


class NullBookedIntervalSource:
    """Interim :class:`BookedIntervalSource` reporting an empty calendar.

    Correct rather than merely convenient: no ``appointments`` table exists, so
    no doctor has any. When Appointment Management lands, its adapter replaces
    this one in ``app/api/dependencies/services.py`` and neither
    :class:`DoctorService` nor :func:`generate_slots` changes.
    """

    async def booked_intervals(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> list[BookedInterval]:
        """Return no bookings.

        :returns: An empty list.
        """
        return []

    async def has_future_appointments(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID, *, after: datetime
    ) -> int:
        """Return zero future appointments.

        :returns: Always ``0``.
        """
        return 0


# ── The departments seam, now implemented ───────────────────────────────────


class DoctorDepartmentUsageSource:
    """Real :class:`~app.services.department_service.DepartmentUsageSource`.

    The Department module shipped with a null implementation of this protocol
    because ``doctors`` did not exist. This is the adapter it was waiting for:
    wiring it in ``app/api/dependencies/services.py`` activates the rule-13
    guard — "a department cannot be deactivated while active doctors are
    assigned" — without a single change to department code.

    :param doctors: Doctor data access.
    """

    def __init__(self, doctors: DoctorRepository) -> None:
        self._doctors = doctors

    async def active_doctor_count(self, hospital_id: uuid.UUID, department_id: uuid.UUID) -> int:
        """Count active doctors assigned to a department.

        :param hospital_id: The tenant to scope to.
        :param department_id: The department being checked.
        :returns: The number of active doctors assigned.
        """
        return await self._doctors.count_active_by_department(hospital_id, department_id)


# ── Module exceptions ───────────────────────────────────────────────────────


class DoctorNotFoundError(NotFoundError):
    """Raised when a doctor is absent from the requested hospital.

    Also raised for a doctor in a *different* hospital: a cross-tenant lookup
    must be indistinguishable from a miss, or the 404/403 difference leaks the
    existence of another tenant's records.
    """

    def __init__(self, doctor_id: uuid.UUID) -> None:
        super().__init__(message="Doctor not found.", detail={"doctor_id": str(doctor_id)})


class LeaveNotFoundError(NotFoundError):
    """Raised when a leave does not belong to the given doctor and hospital."""

    def __init__(self, leave_id: uuid.UUID) -> None:
        super().__init__(message="Leave not found.", detail={"leave_id": str(leave_id)})


class DuplicateDoctorProfileError(ConflictError):
    """Raised when a user already has a doctor profile (module spec §4, rule 1)."""

    def __init__(self, user_id: uuid.UUID) -> None:
        super().__init__(
            message="This user already has a doctor profile.",
            detail={"user_id": str(user_id)},
        )


class OverlappingLeaveError(ConflictError):
    """Raised when a proposed leave overlaps an existing one.

    MVP behaviour per module spec §14: reject. Merging overlapping leaves is
    v2.1.
    """

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(
            message="This leave overlaps an existing leave.",
            detail={"conflicting_leaves": conflicts},
        )


class DoctorHasAppointmentsError(ConflictError):
    """Raised when deactivating a doctor who still has future appointments.

    Module spec §14 and FR-5. The count travels with the error so the caller
    can tell the user how much reassignment stands in the way.
    """

    def __init__(self, doctor_id: uuid.UUID, appointment_count: int) -> None:
        super().__init__(
            message=(
                "Doctor cannot be deactivated while future appointments exist. "
                "Cancel or reassign them first."
            ),
            detail={"doctor_id": str(doctor_id), "future_appointments": appointment_count},
        )


class DoctorAlreadyActiveError(BusinessRuleError):
    """Raised when reactivating a doctor who was never deactivated."""

    def __init__(self, doctor_id: uuid.UUID) -> None:
        super().__init__(message="Doctor is already active.", detail={"doctor_id": str(doctor_id)})


class DoctorNotDeactivatedError(BusinessRuleError):
    """Raised when deactivating a doctor who is already deactivated."""

    def __init__(self, doctor_id: uuid.UUID) -> None:
        super().__init__(
            message="Doctor is already deactivated.", detail={"doctor_id": str(doctor_id)}
        )


# ── Slot generation ─────────────────────────────────────────────────────────


def generate_slots(
    *,
    target_date: date,
    availability: list[tuple[time, time, int]],
    leaves: list[tuple[datetime, datetime]],
    booked: list[BookedInterval],
    timezone: str,
) -> list[SlotResponse]:
    """Compute one day's slots for a doctor (module spec §5.4).

    A pure function: no database, no clock, no ``self``. Everything it needs
    arrives as an argument, which is what lets the DST and boundary cases be
    tested exhaustively without fixtures.

    The algorithm, per business rule 4:

    1. Take each availability window for the target date's local weekday.
    2. Walk it in ``slot_duration`` steps, building each boundary as a
       *wall-clock* time in ``timezone`` and letting the zone resolve it to an
       instant.
    3. Drop a trailing partial slot — a 20-minute remainder in a 30-minute
       schedule is not bookable.
    4. Mark the slot ``on_leave`` if it intersects a leave, ``booked`` if it
       intersects an appointment, otherwise ``available``.

    **Why wall-clock rather than adding timedeltas to an instant.** On a DST
    transition the two differ. Adding 30 minutes repeatedly to a UTC instant
    walks straight through the discontinuity and produces slots at local times
    the clinic never has. Constructing each boundary as a local wall-clock time
    instead means the day has the number of real hours the wall clock says it
    has — which is what a receptionist reading the schedule expects
    (module spec §14).

    The two DST directions behave differently, both deliberately:

    *Spring forward* — an hour of wall-clock does not exist. In Europe/London
    on 2026-03-29 the clock goes 01:00 GMT straight to 02:00 BST, so a nominal
    01:00–02:00 slot spans zero real seconds. Those slots are dropped: they
    cannot host an appointment, and offering one would let reception book into
    a minute that never happens.

    *Fall back* — an hour of wall-clock happens twice. On 2026-10-25 the
    nominal 01:00–02:00 slot spans 120 real minutes, because the wall clock
    covers both passes. It is emitted as a single slot rather than split in
    two. That is a deliberate MVP simplification: it never double-books (one
    slot still takes one appointment), it just leaves the doctor with slack.
    Splitting the ambiguous hour into two distinct bookable slots needs
    ``fold``-aware handling and is deferred — see the note in the module spec
    §14 follow-ups.

    Leave and booking overlap use half-open intervals: a slot ending exactly
    when a leave begins is unaffected.

    :param target_date: The local date to generate for.
    :param availability: ``(start_time, end_time, slot_duration_minutes)``
        windows for this weekday, as wall-clock times.
    :param leaves: ``(starts_at, ends_at)`` leave intervals, timezone-aware.
    :param booked: Booked appointment intervals.
    :param timezone: IANA timezone the hospital operates in.
    :returns: Slots in chronological order.
    :raises ValidationError: If ``timezone`` is not a known IANA zone.
    """
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        msg = f"Hospital timezone {timezone!r} is not a valid IANA timezone."
        raise ValidationError(message=msg) from exc

    slots: list[SlotResponse] = []

    for start_time, end_time, duration_minutes in sorted(availability):
        step = timedelta(minutes=duration_minutes)

        # Window bounds as local wall-clock instants.
        window_start = datetime.combine(target_date, start_time, tzinfo=zone)
        window_end = datetime.combine(target_date, end_time, tzinfo=zone)

        cursor = window_start
        while True:
            slot_end = cursor + step
            # A trailing partial slot is not bookable, so the window ends here.
            if slot_end > window_end:
                break

            # Drop slots that occupy no real time. On a spring-forward
            # transition an hour of wall-clock does not exist — in
            # Europe/London on 2026-03-29 the clock goes 01:00 GMT straight to
            # 02:00 BST — so the 01:00-02:00 local slot spans zero actual
            # seconds. Wall-clock arithmetic is still right for every other
            # slot that day; this one simply cannot host an appointment, and
            # offering it would let reception book into a minute that never
            # happens.
            if slot_end.astimezone(UTC) <= cursor.astimezone(UTC):
                cursor = slot_end
                continue

            # The mirror case needs no branch: on fall-back the repeated hour
            # falls through as one 120-minute slot. Splitting it into two
            # 60-minute slots would mean threading fold-aware handling through
            # every downstream consumer — booking UI, check-in, calendar,
            # appointment records — to reclaim one bookable hour per timezone
            # per year. The test pin at [60, 120, 60, 60] keeps any change to
            # that trade-off a deliberate one.

            status, appointment_id = _classify_slot(cursor, slot_end, leaves, booked)
            slots.append(
                SlotResponse(
                    start=cursor,
                    end=slot_end,
                    status=status,
                    appointment_id=appointment_id,
                )
            )
            cursor = slot_end

    slots.sort(key=lambda slot: slot.start)
    return slots


def _classify_slot(
    slot_start: datetime,
    slot_end: datetime,
    leaves: list[tuple[datetime, datetime]],
    booked: list[BookedInterval],
) -> tuple[SlotStatus, uuid.UUID | None]:
    """Decide whether a slot is on leave, booked, or available.

    Leave wins over booked: if a doctor is away, the slot is not bookable
    regardless of what the calendar still holds — and surfacing it as
    ``on_leave`` is what tells reception the appointment needs reassigning
    (module spec §5.3 step 3).

    :param slot_start: Slot start instant.
    :param slot_end: Slot end instant.
    :param leaves: Leave intervals.
    :param booked: Booked appointment intervals.
    :returns: ``(status, appointment_id)``; the id is set only when booked.
    """
    for leave_start, leave_end in leaves:
        if _overlaps(slot_start, slot_end, leave_start, leave_end):
            return SlotStatus.ON_LEAVE, None

    for interval in booked:
        if _overlaps(slot_start, slot_end, interval.starts_at, interval.ends_at):
            return SlotStatus.BOOKED, interval.appointment_id

    return SlotStatus.AVAILABLE, None


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Test whether two half-open intervals intersect.

    Half-open on purpose: touching intervals — one ending exactly when the next
    begins — do not overlap, so a leave starting at 13:00 leaves the 12:30–13:00
    slot bookable.

    :returns: ``True`` if the intervals share any instant.
    """
    return a_start < b_end and a_end > b_start


class DoctorService:
    """Doctor profiles, availability, leaves, and computed slots.

    :param doctors: Doctor data access.
    :param users: User lookups, for validating the linked account.
    :param departments: Department lookups, for validating assignment.
    :param hospitals: Hospital lookups, for the timezone slot generation needs.
    :param session: The request-scoped session, held only to own the
        transaction boundary.
    :param audit: Where audit events are recorded.
    :param booked: Supplies appointment facts this module does not own.
    """

    def __init__(
        self,
        doctors: DoctorRepository,
        users: UserRepository,
        departments: DepartmentRepository,
        hospitals: HospitalRepository,
        session: AsyncSession,
        audit: AuditSink,
        booked: BookedIntervalSource,
    ) -> None:
        self._doctors = doctors
        self._users = users
        self._departments = departments
        self._hospitals = hospitals
        self._session = session
        self._audit = audit
        self._booked = booked

    # ── Doctor commands ───────────────────────────────────────────────────────

    async def create_doctor(
        self,
        hospital_id: uuid.UUID,
        payload: CreateDoctorRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> DoctorResponse:
        """Onboard a doctor against an existing user (module spec §5.1).

        :param hospital_id: The hospital onboarding the doctor.
        :param payload: Validated creation data.
        :param actor_id: UUID of the acting user.
        :returns: The created doctor.
        :raises ValidationError: If the user or department is unusable.
        :raises DuplicateDoctorProfileError: If the user already has a profile.
        """
        await self._assert_user_available(hospital_id, payload.user_id)
        await self._assert_department_valid(hospital_id, payload.department_id)

        values = self._writable_values(payload.model_dump(mode="json"))
        # ``mode="json"`` renders Decimal as a string and UUID as text; the
        # database columns need the Python objects back.
        values["consultation_fee"] = payload.consultation_fee
        values["department_id"] = payload.department_id

        doctor = await self._doctors.create_doctor(
            hospital_id=hospital_id,
            user_id=payload.user_id,
            created_by=actor_id,
            **values,
        )

        await self._audit.record(
            AuditEvent(
                action="doctor.created",
                hospital_id=hospital_id,
                target_type="doctor",
                target_id=doctor.id,
                actor_id=actor_id,
                changes={name: {"before": None, "after": value} for name, value in values.items()},
            )
        )
        await self._session.commit()

        logger.info(
            "doctor.created",
            hospital_id=str(hospital_id),
            doctor_id=str(doctor.id),
            actor_id=str(actor_id) if actor_id else None,
        )
        return DoctorResponse.from_model(doctor)

    async def update_doctor(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        payload: UpdateDoctorRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> DoctorResponse:
        """Apply a partial update to a doctor.

        :param hospital_id: The hospital the doctor belongs to.
        :param doctor_id: The doctor to update.
        :param payload: Partial update data.
        :param actor_id: UUID of the acting user.
        :returns: The updated doctor.
        :raises DoctorNotFoundError: If absent from this tenant.
        """
        doctor = await self._get_or_raise(hospital_id, doctor_id)

        changes = self._writable_values(payload.changed_fields())
        if "consultation_fee" in changes:
            changes["consultation_fee"] = payload.consultation_fee
        if "department_id" in changes:
            changes["department_id"] = payload.department_id
            await self._assert_department_valid(hospital_id, payload.department_id)

        diff = self._diff(doctor, changes)
        if not diff:
            logger.info(
                "doctor.update_noop", hospital_id=str(hospital_id), doctor_id=str(doctor_id)
            )
            return DoctorResponse.from_model(doctor)

        doctor = await self._doctors.update_doctor(doctor, updated_by=actor_id, **changes)

        await self._audit.record(
            AuditEvent(
                action="doctor.updated",
                hospital_id=hospital_id,
                target_type="doctor",
                target_id=doctor.id,
                actor_id=actor_id,
                changes=diff,
            )
        )
        await self._session.commit()

        logger.info(
            "doctor.updated",
            hospital_id=str(hospital_id),
            doctor_id=str(doctor.id),
            actor_id=str(actor_id) if actor_id else None,
            changed_fields=sorted(diff),
        )
        return DoctorResponse.from_model(doctor)

    async def deactivate_doctor(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> DoctorResponse:
        """Deactivate a doctor by soft-deleting the record.

        Refused while future appointments exist (module spec §4 rule 7, FR-5).

        :param hospital_id: The hospital the doctor belongs to.
        :param doctor_id: The doctor to deactivate.
        :param actor_id: UUID of the acting user.
        :returns: The deactivated doctor.
        :raises DoctorNotFoundError: If absent from this tenant.
        :raises DoctorNotDeactivatedError: If already deactivated.
        :raises DoctorHasAppointmentsError: If future appointments exist.
        """
        doctor = await self._doctors.get_doctor_by_id(hospital_id, doctor_id, include_deleted=True)
        if doctor is None:
            raise DoctorNotFoundError(doctor_id)
        if doctor.deleted_at is not None:
            raise DoctorNotDeactivatedError(doctor_id)

        upcoming = await self._booked.has_future_appointments(
            hospital_id, doctor_id, after=datetime.now(UTC)
        )
        if upcoming > 0:
            logger.info(
                "doctor.deactivate_blocked",
                hospital_id=str(hospital_id),
                doctor_id=str(doctor_id),
                future_appointments=upcoming,
            )
            raise DoctorHasAppointmentsError(doctor_id, upcoming)

        doctor = await self._doctors.delete_doctor(doctor, deleted_by=actor_id)

        await self._audit.record(
            AuditEvent(
                action="doctor.deactivated",
                hospital_id=hospital_id,
                target_type="doctor",
                target_id=doctor.id,
                actor_id=actor_id,
            )
        )
        await self._session.commit()

        logger.info(
            "doctor.deactivated",
            hospital_id=str(hospital_id),
            doctor_id=str(doctor.id),
            actor_id=str(actor_id) if actor_id else None,
        )
        return DoctorResponse.from_model(doctor)

    async def activate_doctor(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> DoctorResponse:
        """Reactivate a previously deactivated doctor.

        :param hospital_id: The hospital the doctor belongs to.
        :param doctor_id: The doctor to reactivate.
        :param actor_id: UUID of the acting user.
        :returns: The reactivated doctor.
        :raises DoctorNotFoundError: If absent from this tenant.
        :raises DoctorAlreadyActiveError: If never deactivated.
        """
        doctor = await self._doctors.get_doctor_by_id(hospital_id, doctor_id, include_deleted=True)
        if doctor is None:
            raise DoctorNotFoundError(doctor_id)
        if doctor.deleted_at is None:
            raise DoctorAlreadyActiveError(doctor_id)

        doctor = await self._doctors.restore_doctor(doctor, updated_by=actor_id)

        await self._audit.record(
            AuditEvent(
                action="doctor.activated",
                hospital_id=hospital_id,
                target_type="doctor",
                target_id=doctor.id,
                actor_id=actor_id,
            )
        )
        await self._session.commit()

        logger.info("doctor.activated", hospital_id=str(hospital_id), doctor_id=str(doctor.id))
        return DoctorResponse.from_model(doctor)

    # ── Doctor queries ────────────────────────────────────────────────────────

    async def get_doctor_details(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID, *, include_inactive: bool = False
    ) -> DoctorResponse:
        """Retrieve one doctor's full record.

        :param hospital_id: The hospital the doctor belongs to.
        :param doctor_id: The doctor UUID.
        :param include_inactive: Return the record even if deactivated.
        :returns: The doctor.
        :raises DoctorNotFoundError: If absent from this tenant.
        """
        doctor = await self._doctors.get_doctor_by_id(
            hospital_id, doctor_id, include_deleted=include_inactive
        )
        if doctor is None:
            raise DoctorNotFoundError(doctor_id)
        return DoctorResponse.from_model(doctor)

    async def list_doctors(
        self,
        hospital_id: uuid.UUID,
        *,
        term: str | None = None,
        specialization: str | None = None,
        department_id: uuid.UUID | None = None,
        include_inactive: bool = False,
        pagination: PaginationParams | None = None,
    ) -> Page[DoctorSummaryResponse]:
        """List or search doctors (module spec §9).

        :param hospital_id: The hospital to search.
        :param term: Free-text term (name prefix or exact licence).
        :param specialization: Exact specialization filter.
        :param department_id: Exact department filter.
        :param include_inactive: Include deactivated doctors.
        :param pagination: Page and page size. Defaults to page 1.
        :returns: One page of summaries plus the total match count.
        """
        page_params = pagination or PaginationParams()
        criteria: dict[str, Any] = {
            "term": term,
            "specialization": specialization,
            "department_id": department_id,
            "include_deleted": include_inactive,
        }

        rows = await self._doctors.list_doctors(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **criteria
        )
        total = await self._doctors.count_doctors(hospital_id, **criteria)

        return Page[DoctorSummaryResponse](
            items=[DoctorSummaryResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Availability ──────────────────────────────────────────────────────────

    async def get_availability(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID
    ) -> list[AvailabilityResponse]:
        """Return a doctor's weekly availability.

        :param hospital_id: The hospital the doctor belongs to.
        :param doctor_id: The doctor whose schedule to read.
        :returns: Availability windows, ordered by day then start time.
        :raises DoctorNotFoundError: If the doctor is absent from this tenant.
        """
        await self._get_or_raise(hospital_id, doctor_id)
        rows = await self._doctors.get_availability(hospital_id, doctor_id)
        return [AvailabilityResponse.from_model(row) for row in rows]

    async def set_availability(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        payload: SetAvailabilityRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> list[AvailabilityResponse]:
        """Replace a doctor's weekly availability atomically (module spec §5.2).

        Overlap is rejected by :class:`~app.schemas.doctor.SetAvailabilityRequest`
        and re-asserted here, because this service is also reachable from seed
        scripts and background jobs that never cross the API boundary.

        :param hospital_id: The hospital the doctor belongs to.
        :param doctor_id: The doctor whose schedule to replace.
        :param payload: The complete weekly schedule.
        :param actor_id: UUID of the acting user.
        :returns: The persisted availability.
        :raises DoctorNotFoundError: If the doctor is absent from this tenant.
        :raises ValidationError: If entries overlap within a day.
        """
        await self._get_or_raise(hospital_id, doctor_id)
        self._assert_no_overlap(payload)

        entries = [
            {
                "day_of_week": entry.day_of_week,
                "start_time": entry.start_time,
                "end_time": entry.end_time,
                "slot_duration_minutes": entry.slot_duration_minutes,
            }
            for entry in payload.entries
        ]

        rows = await self._doctors.replace_availability(
            hospital_id, doctor_id, entries, actor_id=actor_id
        )

        await self._audit.record(
            AuditEvent(
                action="doctor.availability_updated",
                hospital_id=hospital_id,
                target_type="doctor",
                target_id=doctor_id,
                actor_id=actor_id,
                context={"window_count": len(entries)},
            )
        )
        await self._session.commit()

        logger.info(
            "doctor.availability_updated",
            hospital_id=str(hospital_id),
            doctor_id=str(doctor_id),
            window_count=len(entries),
        )
        return [AvailabilityResponse.from_model(row) for row in rows]

    # ── Leaves ────────────────────────────────────────────────────────────────

    async def list_leaves(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID
    ) -> list[LeaveResponse]:
        """List a doctor's leaves.

        :param hospital_id: The hospital the doctor belongs to.
        :param doctor_id: The doctor whose leaves to read.
        :returns: Leaves, earliest first.
        :raises DoctorNotFoundError: If the doctor is absent from this tenant.
        """
        await self._get_or_raise(hospital_id, doctor_id)
        rows = await self._doctors.list_leaves(hospital_id, doctor_id)
        return [LeaveResponse.from_model(row) for row in rows]

    async def create_leave(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        payload: CreateLeaveRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> tuple[LeaveResponse, list[dict[str, Any]]]:
        """Record a leave, auto-approved (module spec §5.3).

        Returns the appointments that fall inside the leave alongside the leave
        itself, so the caller can drive reassignment (§5.3 step 3). Today that
        list is always empty — see :class:`NullBookedIntervalSource`.

        :param hospital_id: The hospital the doctor belongs to.
        :param doctor_id: The doctor taking leave.
        :param payload: Validated leave data.
        :param actor_id: UUID of the acting user.
        :returns: ``(leave, affected_appointments)``.
        :raises DoctorNotFoundError: If the doctor is absent from this tenant.
        :raises OverlappingLeaveError: If it overlaps an existing leave.
        """
        await self._get_or_raise(hospital_id, doctor_id)

        starts_at = payload.starts_at.astimezone(UTC)
        ends_at = payload.ends_at.astimezone(UTC)

        conflicts = await self._doctors.find_overlapping_leaves(
            hospital_id, doctor_id, starts_at, ends_at
        )
        if conflicts:
            raise OverlappingLeaveError(
                [
                    {
                        "leave_id": str(conflict.id),
                        "starts_at": conflict.starts_at.isoformat(),
                        "ends_at": conflict.ends_at.isoformat(),
                    }
                    for conflict in conflicts
                ]
            )

        leave = await self._doctors.create_leave(
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=payload.reason,
            created_by=actor_id,
        )

        affected = await self._booked.booked_intervals(hospital_id, doctor_id, starts_at, ends_at)

        await self._audit.record(
            AuditEvent(
                action="doctor.leave_created",
                hospital_id=hospital_id,
                target_type="doctor",
                target_id=doctor_id,
                actor_id=actor_id,
                context={"leave_id": str(leave.id), "affected_appointments": len(affected)},
            )
        )
        await self._session.commit()

        logger.info(
            "doctor.leave_created",
            hospital_id=str(hospital_id),
            doctor_id=str(doctor_id),
            affected_appointments=len(affected),
        )
        return (
            LeaveResponse.from_model(leave),
            [
                {
                    "appointment_id": str(interval.appointment_id),
                    "starts_at": interval.starts_at.isoformat(),
                    "ends_at": interval.ends_at.isoformat(),
                }
                for interval in affected
            ],
        )

    async def delete_leave(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        leave_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> None:
        """Cancel a leave by soft-deleting it.

        :param hospital_id: The hospital the doctor belongs to.
        :param doctor_id: The doctor the leave belongs to.
        :param leave_id: The leave to cancel.
        :param actor_id: UUID of the acting user.
        :raises DoctorNotFoundError: If the doctor is absent from this tenant.
        :raises LeaveNotFoundError: If the leave is absent or another doctor's.
        """
        await self._get_or_raise(hospital_id, doctor_id)

        leave = await self._doctors.get_leave_by_id(hospital_id, doctor_id, leave_id)
        if leave is None:
            raise LeaveNotFoundError(leave_id)

        await self._doctors.delete_leave(leave, deleted_by=actor_id)

        await self._audit.record(
            AuditEvent(
                action="doctor.leave_deleted",
                hospital_id=hospital_id,
                target_type="doctor",
                target_id=doctor_id,
                actor_id=actor_id,
                context={"leave_id": str(leave_id)},
            )
        )
        await self._session.commit()

        logger.info(
            "doctor.leave_deleted",
            hospital_id=str(hospital_id),
            doctor_id=str(doctor_id),
            leave_id=str(leave_id),
        )

    # ── Slots ─────────────────────────────────────────────────────────────────

    async def get_slots(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID, target_date: date
    ) -> DaySlotsResponse:
        """Compute a doctor's slots for one date (module spec §5.4).

        Gathers the four inputs :func:`generate_slots` needs — availability for
        the local weekday, overlapping leaves, booked intervals, and the
        hospital timezone — then hands off. The service does no arithmetic on
        times itself.

        :param hospital_id: The hospital the doctor belongs to.
        :param doctor_id: The doctor whose calendar to compute.
        :param target_date: The local date to generate for.
        :returns: The day's slots.
        :raises DoctorNotFoundError: If the doctor is absent from this tenant.
        :raises ValidationError: If the hospital's timezone is invalid.
        """
        await self._get_or_raise(hospital_id, doctor_id)

        hospital = await self._hospitals.get_by_id(hospital_id)
        timezone = hospital.timezone if hospital else "UTC"

        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"Hospital timezone {timezone!r} is not a valid IANA timezone."
            raise ValidationError(message=msg) from exc

        # The local day, converted to the UTC instants that bound it. Used to
        # fetch only the leaves and appointments that could touch this day.
        day_start = datetime.combine(target_date, _MIDNIGHT, tzinfo=zone)
        day_end = day_start + timedelta(days=1)

        rows = await self._doctors.get_availability(hospital_id, doctor_id)
        # `weekday()` is 0=Monday, matching the day_of_week column (§2.9).
        weekday = target_date.weekday()
        availability = [
            (row.start_time, row.end_time, row.slot_duration_minutes)
            for row in rows
            if row.day_of_week == weekday
        ]

        leave_rows = await self._doctors.list_leaves(
            hospital_id, doctor_id, starts_before=day_end, ends_after=day_start
        )
        leaves = [(row.starts_at, row.ends_at) for row in leave_rows]

        booked = await self._booked.booked_intervals(hospital_id, doctor_id, day_start, day_end)

        slots = generate_slots(
            target_date=target_date,
            availability=availability,
            leaves=leaves,
            booked=booked,
            timezone=timezone,
        )

        logger.info(
            "doctor.slots_computed",
            hospital_id=str(hospital_id),
            doctor_id=str(doctor_id),
            date=target_date.isoformat(),
            slot_count=len(slots),
        )
        return DaySlotsResponse(
            date=target_date, doctor_id=doctor_id, timezone=timezone, slots=slots
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _get_or_raise(self, hospital_id: uuid.UUID, doctor_id: uuid.UUID) -> Doctor:
        """Fetch an active doctor or raise :class:`DoctorNotFoundError`."""
        doctor = await self._doctors.get_doctor_by_id(hospital_id, doctor_id)
        if doctor is None:
            raise DoctorNotFoundError(doctor_id)
        return doctor

    async def _assert_user_available(self, hospital_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Check the user exists in this tenant and has no doctor profile yet.

        :raises ValidationError: If the user is absent or in another tenant.
        :raises DuplicateDoctorProfileError: If they already have a profile.
        """
        user = await self._users.get_by_id(user_id, hospital_id)
        if user is None or user.hospital_id != hospital_id:
            # Same message either way: confirming that a user exists in another
            # hospital would leak across the tenant boundary.
            raise ValidationError(
                message="User not found in this hospital.",
                detail={"errors": [{"field": "user_id", "message": "Unknown user."}]},
            )

        existing = await self._doctors.get_doctor_by_user_id(hospital_id, user_id)
        if existing is not None:
            raise DuplicateDoctorProfileError(user_id)

    async def _assert_department_valid(
        self, hospital_id: uuid.UUID, department_id: uuid.UUID | None
    ) -> None:
        """Check a department exists in this tenant, when one was supplied.

        :raises ValidationError: If the department is absent or in another tenant.
        """
        if department_id is None:
            return
        department = await self._departments.get_department_by_id(hospital_id, department_id)
        if department is None:
            raise ValidationError(
                message="Department not found in this hospital.",
                detail={"errors": [{"field": "department_id", "message": "Unknown department."}]},
            )

    @staticmethod
    def _assert_no_overlap(payload: SetAvailabilityRequest) -> None:
        """Re-assert the overlap rule for non-HTTP callers (module spec §11).

        :raises ValidationError: If two windows on the same day overlap.
        """
        by_day: dict[int, list[tuple[time, time]]] = {}
        for entry in payload.entries:
            by_day.setdefault(entry.day_of_week, []).append((entry.start_time, entry.end_time))

        errors: list[dict[str, str]] = []
        for day, windows in sorted(by_day.items()):
            ordered = sorted(windows)
            for (_, earlier_end), (later_start, _) in zip(ordered, ordered[1:], strict=False):
                if later_start < earlier_end:
                    errors.append(
                        {
                            "field": "entries",
                            "message": f"Overlapping availability on day {day}.",
                        }
                    )
                    break

        if errors:
            logger.warning("doctor.availability_overlap", days=[e["message"] for e in errors])
            raise ValidationError(
                message="Availability entries overlap.", detail={"errors": errors}
            )

    @staticmethod
    def _writable_values(values: dict[str, Any]) -> dict[str, Any]:
        """Keep only the columns a client is allowed to write."""
        return {name: value for name, value in values.items() if name in _WRITABLE_COLUMNS}

    @staticmethod
    def _diff(doctor: Doctor, changes: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Build a before/after diff, dropping fields whose value is unchanged."""
        diff: dict[str, dict[str, Any]] = {}
        for name, new_value in changes.items():
            current = getattr(doctor, name, None)
            if current != new_value:
                diff[name] = {"before": current, "after": new_value}
        return diff
