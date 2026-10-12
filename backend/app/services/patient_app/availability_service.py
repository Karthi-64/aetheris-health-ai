"""Doctor availability: when a patient can book a doctor, and nothing else about the doctor's day.

``docs/modules/15-patient-app.md`` §9.1 (Slot: start and end only) and §13.

**One slot engine.** The slots are the ones the hospital's own slot picker and
booking path compute: the same weekly windows, the same leaves, the same
appointments (every status that occupies the doctor's time — a cancelled or
no-show appointment frees its slot, as the booking exclusion constraint says),
fed to the same :func:`~app.services.doctor_service.generate_slots`. Nothing
here invents a slot, and booking (Task 32) recomputes them before it writes.

**What a patient is shown.** Only slots that are ``available`` *and* start no
earlier than the hospital's minimum lead time from now, on dates no earlier
than today and no later than the booking horizon — today and the horizon
being reckoned in the hospital's timezone, which every date and time in the
answer is expressed in. A booked or on-leave slot is simply absent.

**Who.** A signed-in patient, past the policy gate; the hospital the path
names if hospital discovery would show it; the doctor if doctor discovery
would list them at that hospital. Every other case is one ``404``.

Reading availability writes nothing, reserves nothing, and is not audited.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from http import HTTPStatus
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.core.exceptions import (
    BusinessRuleError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.tenancy import TenantScope, tenant_scope
from app.models.doctor import SlotStatus
from app.schemas.patient_app.availability import (
    DEFAULT_RANGE_DAYS,
    MAX_RANGE_DAYS,
    PatientAvailabilityDay,
    PatientDoctorAvailability,
    PatientSlot,
)
from app.services.doctor_service import BookedInterval, generate_slots
from app.services.patient_app.booking_policy import BookingPolicy
from app.utils.datetime import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.models.patient_account import PatientAccount
    from app.repositories.appointment_repository import AppointmentRepository
    from app.repositories.doctor_repository import DoctorRepository
    from app.repositories.hospital_repository import HospitalDirectoryEntry
    from app.schemas.doctor import SlotResponse
    from app.services.patient_app.consent_service import ConsentService
    from app.services.patient_app.hospital_gate import PatientHospitalGate

__all__ = ["DoctorAvailabilityService", "SlotState"]

logger = structlog.get_logger(__name__)

#: The one message for everything that cannot be shown, as discovery uses it.
_NOT_FOUND: Final = HTTPStatus.NOT_FOUND.phrase
#: The one message for a date a patient may not ask about.
_OUTSIDE_WINDOW: Final = "Outside the bookable window."

#: A doctor reference exactly as ``str(uuid)`` writes it.
_UUID_TEXT: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class SlotState(StrEnum):
    """What a requested slot is, to a patient, at this moment."""

    BOOKABLE = "bookable"
    TAKEN = "taken"
    NOT_A_SLOT = "not_a_slot"


class DoctorAvailabilityService:
    """Answers when a doctor of a hospital can be booked by a patient.

    :param gate: Which hospitals are open to patients.
    :param doctors: Doctor data access — the directory, windows and leaves.
    :param appointments: Appointment data access — the booked intervals.
    :param consent: The policy gate.
    :param clock: The current instant, UTC. Defaults to the real clock.
    """

    def __init__(
        self,
        gate: PatientHospitalGate,
        doctors: DoctorRepository,
        appointments: AppointmentRepository,
        consent: ConsentService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gate = gate
        self._doctors = doctors
        self._appointments = appointments
        self._consent = consent
        self._clock = clock or utc_now

    async def get_availability(
        self,
        account: PatientAccount,
        hospital_ref: str,
        doctor_ref: str,
        *,
        start_date: date | None,
        end_date: date | None,
    ) -> PatientDoctorAvailability:
        """The bookable slots of one doctor over a bounded range of hospital-local dates.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's code or id, from the path.
        :param doctor_ref: The doctor's reference, from the path.
        :param start_date: First date asked for; today at the hospital if absent.
        :param end_date: Last date asked for; a week from the first, or the
            horizon if that comes sooner, if absent.
        :returns: Every date of the range with its bookable slots.
        :raises ConsentRequiredError: If a required policy is pending.
        :raises NotFoundError: If the hospital or the doctor cannot be shown.
        :raises ValidationError: If the range is backwards or too long.
        :raises BusinessRuleError: If any date lies outside the bookable window.
        :raises ServiceUnavailableError: If the hospital's timezone cannot be used.
        """
        await self._consent.ensure_policies_accepted(account.id)
        hospital = await self._gate.describe(hospital_ref)
        reference = doctor_ref.strip().lower()
        if hospital is None or not _UUID_TEXT.fullmatch(reference):
            raise NotFoundError(_NOT_FOUND)
        doctor_id = uuid.UUID(reference)

        with tenant_scope(TenantScope.hospital(hospital.id)):
            doctor = await self._doctors.get_directory_entry(hospital.id, doctor_id)
            if doctor is None:
                raise NotFoundError(_NOT_FOUND)

            zone = _zone_of(hospital.timezone, hospital.id)
            now = self._clock()
            policy = BookingPolicy.from_settings(hospital.settings)
            today = now.astimezone(zone).date()
            horizon_end = today + timedelta(days=policy.horizon_days)
            first, last = _bounded_range(start_date, end_date, today=today, horizon_end=horizon_end)

            by_day = await self._engine_slots(hospital, doctor.id, first, last, zone)

        earliest = now + timedelta(minutes=policy.min_lead_minutes)
        days = [
            PatientAvailabilityDay(
                date=day,
                slots=[
                    PatientSlot(start=start, end=end)
                    for status, start, end in _real(slots, zone)
                    if status is SlotStatus.AVAILABLE and start >= earliest
                ],
            )
            for day, slots in by_day.items()
        ]

        return PatientDoctorAvailability(
            timezone=hospital.timezone,
            today=today,
            horizon_end=horizon_end,
            min_lead_minutes=policy.min_lead_minutes,
            start_date=first,
            end_date=last,
            days=days,
        )

    async def slot_state(
        self,
        hospital: HospitalDirectoryEntry,
        doctor_id: uuid.UUID,
        start: datetime,
        end: datetime,
        *,
        now: datetime,
    ) -> SlotState:
        """Whether a patient may book exactly this slot, right now.

        The question booking asks before it writes, answered by the same code
        that lists the slots: the day is recomputed from current rows, and the
        requested bounds must be one of its real slots. Must be called inside
        the hospital's tenant scope.

        :param hospital: The hospital, as the gate described it.
        :param doctor_id: A doctor the directory lists at that hospital.
        :param start: Requested start, timezone-aware.
        :param end: Requested end, timezone-aware.
        :param now: The current instant.
        :returns: ``BOOKABLE``; ``TAKEN`` if it is a slot but booked or on
            leave; ``NOT_A_SLOT`` for anything else — no such slot, a past
            one, one inside the lead time or beyond the horizon.
        :raises ServiceUnavailableError: If the hospital's timezone cannot be used.
        """
        zone = _zone_of(hospital.timezone, hospital.id)
        policy = BookingPolicy.from_settings(hospital.settings)
        today = now.astimezone(zone).date()
        day = start.astimezone(zone).date()
        if not today <= day <= today + timedelta(days=policy.horizon_days):
            return SlotState.NOT_A_SLOT
        if start < now + timedelta(minutes=policy.min_lead_minutes):
            return SlotState.NOT_A_SLOT

        by_day = await self._engine_slots(hospital, doctor_id, day, day, zone)
        for status, slot_start, slot_end in _real(by_day[day], zone):
            if slot_start == start and slot_end == end:
                return SlotState.BOOKABLE if status is SlotStatus.AVAILABLE else SlotState.TAKEN
        return SlotState.NOT_A_SLOT

    async def _engine_slots(
        self,
        hospital: HospitalDirectoryEntry,
        doctor_id: uuid.UUID,
        first: date,
        last: date,
        zone: ZoneInfo,
    ) -> dict[date, list[SlotResponse]]:
        """The hospital slot engine's answer for each date of a range.

        The whole range is read at once: one query for the weekly windows, one
        for the leaves and one for the appointments that could touch any of
        its days. The number of days changes nothing about the reads.
        """
        window_start = datetime.combine(first, time.min, tzinfo=zone)
        window_end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=zone)
        windows = await self._doctors.get_availability(hospital.id, doctor_id)
        leave_rows = await self._doctors.list_leaves(
            hospital.id, doctor_id, starts_before=window_end, ends_after=window_start
        )
        booked_rows = await self._appointments.booked_intervals_for_doctor(
            hospital.id, doctor_id, window_start, window_end
        )
        leaves = [(row.starts_at, row.ends_at) for row in leave_rows]
        booked = [
            BookedInterval(
                starts_at=row.scheduled_start, ends_at=row.scheduled_end, appointment_id=row.id
            )
            for row in booked_rows
        ]
        return {
            day: generate_slots(
                target_date=day,
                availability=[
                    (row.start_time, row.end_time, row.slot_duration_minutes)
                    for row in windows
                    if row.day_of_week == day.weekday()
                ],
                leaves=leaves,
                booked=booked,
                timezone=hospital.timezone,
            )
            for day in (first + timedelta(days=n) for n in range((last - first).days + 1))
        }


def _real(slots: list[SlotResponse], zone: ZoneInfo) -> list[tuple[SlotStatus, datetime, datetime]]:
    """The engine's slots that name real time, each instant once, as the clock shows them.

    On a spring-forward day the engine labels slots inside the skipped hour
    with wall-clock times that never happen (02:00 when the clock went from
    01:59 straight to 03:00); such a slot names the same instants as a later,
    real one. A slot whose start is not a time the zone's clock shows is left
    out, no instant appears twice — as the slot ranker already does for the
    same engine — and every bound is written as the clock really shows it.
    """
    real: list[tuple[SlotStatus, datetime, datetime]] = []
    seen: set[datetime] = set()
    for slot in slots:
        start, end = _as_shown(slot.start, zone), _as_shown(slot.end, zone)
        if start.replace(tzinfo=None) != slot.start.replace(tzinfo=None) or start in seen:
            continue
        seen.add(start)
        real.append((slot.status, start, end))
    return real


def _as_shown(moment: datetime, zone: ZoneInfo) -> datetime:
    """An instant as the zone's clock really shows it.

    Through UTC on purpose: ``astimezone`` returns a value unchanged when it
    already carries the same zone, and a time the clock skipped would then
    pass as real.
    """
    return moment.astimezone(UTC).astimezone(zone)


def _zone_of(timezone: str, hospital_id: uuid.UUID) -> ZoneInfo:
    """The hospital's zone — or a generic refusal, never the stored text, if it is not one."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("patient_availability_timezone_invalid", hospital_id=str(hospital_id))
        raise ServiceUnavailableError from None


def _bounded_range(
    start_date: date | None, end_date: date | None, *, today: date, horizon_end: date
) -> tuple[date, date]:
    """The dates to answer for, or a refusal.

    A backwards or over-long range is a malformed request (``422``); a range
    that reaches outside the bookable window is a request the policy refuses
    (``400``). Both are decided before any slot is computed.
    """
    first = start_date or today
    # A week, cut at the horizon — but never before the first day itself, so a
    # start beyond the horizon is refused as outside the window, not as backwards.
    last = end_date or max(first, min(first + timedelta(days=DEFAULT_RANGE_DAYS - 1), horizon_end))
    if last < first:
        raise ValidationError(message="end_date must not be before start_date.")
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise ValidationError(message=f"The range may cover at most {MAX_RANGE_DAYS} days.")
    if first < today or last > horizon_end:
        raise BusinessRuleError(message=_OUTSIDE_WINDOW)
    return first, last
