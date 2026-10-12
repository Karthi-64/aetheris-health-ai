"""Business logic for the Appointment Management module.

Owns the state machine (``docs/modules/05-appointment-management.md`` §5.1),
booking validation (§5.2), the transaction boundary, and an audit record per
mutation (CLAUDE.md rule 9). Returns DTOs, never ORM models.

**The state machine is data, not control flow.** :data:`ALLOWED_TRANSITIONS`
declares every legal move once; :meth:`AppointmentService._transition` is the
only place a status changes, and it always writes the history row business
rule 7 requires. Scattering ``if status == ...`` through six endpoints is how
an illegal transition eventually slips through.

**Overlap is guarded twice, deliberately.** The service checks first so the
common case gets a clear 409 naming the clash; the ``no_overlap_per_doctor``
exclusion constraint then makes the guarantee real. Only the database can
settle two receptionists booking the same slot concurrently (§14), because both
transactions read before either writes.

**Seams.** ``InvoiceDraftSink`` stands in for Billing (§5.6 step 6) until that
module exists — the same pattern ``AuditSink`` and ``DepartmentUsageSource``
use. Notification delivery is explicitly out of scope (§2).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError

from app.ai.errors import AINotConfiguredError, AIResponseInvalidError
from app.core.audit import AuditEvent
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    FeatureDisabledError,
    NotFoundError,
    ValidationError,
)
from app.core.feature_flags import AI_SLOT_RECOMMENDATION, flag_is_on
from app.core.logging import get_logger
from app.core.tenancy import TenantScope, tenant_scope
from app.models.appointment import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
)
from app.schemas.appointment import (
    AppointmentResponse,
    AppointmentSummaryResponse,
    BookAppointmentRequest,
    CancelAppointmentRequest,
    RescheduleAppointmentRequest,
    SlotRecommendation,
    SlotRecommendationRequest,
    SlotRecommendationResponse,
    SlotRecommendationStatus,
    StatusHistoryEntryResponse,
)
from app.schemas.common import Page, PaginationParams

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.repositories.appointment_repository import AppointmentRepository
    from app.repositories.doctor_repository import DoctorRepository
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.patient_repository import PatientRepository

logger = get_logger(__name__)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AppointmentBookedIntervalSource",
    "AppointmentNotFoundError",
    "AppointmentService",
    "DaySlot",
    "DoubleBookingError",
    "InvalidTransitionError",
    "InvoiceDraftSink",
    "NullInvoiceDraftSink",
    "OutsideAvailabilityError",
    "SlotChoice",
    "SlotRanker",
]

#: The state machine from module spec §5.1, declared once.
#:
#: ``no_show`` is reachable from every non-terminal state per the spec's closing
#: note. ``completed`` is reachable from ``checked_in`` as well as
#: ``in_progress`` because §14 allows a doctor to complete without a formal
#: start — the skipped state shows up in the history, which is the point of
#: recording transitions rather than just the current value.
ALLOWED_TRANSITIONS: dict[AppointmentStatus, frozenset[AppointmentStatus]] = {
    AppointmentStatus.BOOKED: frozenset(
        {
            AppointmentStatus.CHECKED_IN,
            AppointmentStatus.IN_PROGRESS,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.NO_SHOW,
        }
    ),
    AppointmentStatus.CHECKED_IN: frozenset(
        {
            AppointmentStatus.IN_PROGRESS,
            AppointmentStatus.COMPLETED,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.NO_SHOW,
        }
    ),
    AppointmentStatus.IN_PROGRESS: frozenset({AppointmentStatus.COMPLETED}),
    # Terminal — business rule 5.
    AppointmentStatus.COMPLETED: frozenset(),
    AppointmentStatus.CANCELLED: frozenset(),
    AppointmentStatus.NO_SHOW: frozenset(),
}

#: Default grace after ``scheduled_end`` before the sweeper marks a no-show
#: (module spec §5.7). Overridable per hospital via ``hospitals.settings``.
DEFAULT_NO_SHOW_GRACE_MINUTES = 30

#: How far in the past a walk-in may be booked (module spec §11). A walk-in is
#: recorded when the patient is already standing at the desk, so a few minutes
#: of backdating is normal; a scheduled appointment gets no such licence.
WALK_IN_BACKDATE_GRACE_MINUTES = 15

#: Most slots of one day that are ever shown to the slot ranker: 24 hours of
#: 10-minute slots, the shortest duration a doctor can publish. Valid
#: availability cannot exceed it; the cap bounds the prompt regardless.
MAX_DAY_SLOTS = 144

#: Longest AI-written reason returned to a client.
MAX_RECOMMENDATION_REASON_LENGTH = 200


def _utc_now() -> datetime:
    """The current instant in UTC.

    A module-level seam so the slot-recommendation tests can move the clock
    without depending on the hour they run at.
    """
    return datetime.now(UTC)


# ── The billing seam ────────────────────────────────────────────────────────


@runtime_checkable
class InvoiceDraftSink(Protocol):
    """Receives completed appointments so Billing can draft an invoice.

    Module spec §5.6 step 6. Implemented today by :class:`NullInvoiceDraftSink`
    and, once ``docs/modules/06-billing.md`` ships, by a ``BillingService``
    adapter — at which point one DI provider changes and this module does not.
    """

    async def draft_invoice_for(
        self, hospital_id: uuid.UUID, appointment_id: uuid.UUID, *, actor_id: uuid.UUID | None
    ) -> None:
        """Draft an invoice for a completed appointment.

        :param hospital_id: The tenant to scope to.
        :param appointment_id: The appointment that just completed.
        :param actor_id: UUID of the acting user.
        """
        ...


class NullInvoiceDraftSink:
    """Interim :class:`InvoiceDraftSink` that records intent and nothing else.

    Completing an appointment must not fail because Billing does not exist yet,
    so this logs and returns. The log line is deliberate: it makes the
    would-be invoices visible before the real sink lands.
    """

    async def draft_invoice_for(
        self, hospital_id: uuid.UUID, appointment_id: uuid.UUID, *, actor_id: uuid.UUID | None
    ) -> None:
        """Log the invoice that Billing will eventually draft."""
        logger.info(
            "billing.invoice_draft_skipped",
            hospital_id=str(hospital_id),
            appointment_id=str(appointment_id),
            reason="billing_module_not_implemented",
        )


# ── The AI seam ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DaySlot:
    """One slot of a doctor's day, as shown to the slot ranker.

    :param start: Slot start, timezone-aware, in the hospital's zone.
    :param end: Slot end, likewise.
    :param slot_id: ``"S1"``..``"Sn"`` when the slot may be chosen; ``None``
        when it cannot (booked, on leave, or already started). Ids mean
        nothing outside the request that produced them.
    """

    start: datetime
    end: datetime
    slot_id: str | None


@dataclass(frozen=True, slots=True)
class SlotChoice:
    """The slot a ranker chose.

    :param slot_id: One of the ids it was offered.
    :param reason: Cleaned, model-written text of at most 200 characters, or
        ``None``. Untrusted: it is displayed, never acted on.
    :param provider: Provider that answered. For logs only.
    :param model: Model that answered. For logs only.
    """

    slot_id: str
    reason: str | None
    provider: str
    model: str


@runtime_checkable
class SlotRanker(Protocol):
    """Chooses one slot from a doctor's day (module spec §13).

    A seam rather than a direct :class:`~app.ai.services.ai_service.AIService`
    call: it keeps prompt handling, provider selection and cost accounting out
    of a clinical service, which should not care which model answered.

    Nothing about the patient, the doctor or the hospital's identity is part
    of this interface beyond the ids used for attribution in logs, so an
    implementation has nothing of the kind to send anywhere.
    """

    async def choose_slot(
        self,
        *,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        request_id: str | None,
        target_date: date,
        day: Sequence[DaySlot],
    ) -> SlotChoice:
        """Choose one of the slots in ``day`` that carries an id.

        Implementations must only ever return an id drawn from ``day``; the
        service checks that again on what comes back.

        :param hospital_id: Tenant, for log attribution only.
        :param actor_id: Acting user, for log attribution only.
        :param request_id: Correlation id of the HTTP request.
        :param target_date: The hospital-local day ``day`` describes.
        :param day: The day's slots in chronological order.
        :returns: The chosen slot id and the reason given.
        :raises AIError: When no usable choice could be obtained.
        """
        ...


# ── The doctors seam, now implemented ───────────────────────────────────────


class AppointmentBookedIntervalSource:
    """Real :class:`~app.services.doctor_service.BookedIntervalSource`.

    Doctor Management shipped against ``NullBookedIntervalSource`` because
    ``appointments`` did not exist, so its slot feed could never show ``booked``
    and its FR-5 deletion guard could never fire. This is the adapter it was
    waiting for: wiring it in ``app/api/dependencies/services.py`` activates
    both, with no change to any doctor module code.

    :param appointments: Appointment data access.
    """

    def __init__(self, appointments: AppointmentRepository) -> None:
        self._appointments = appointments

    async def booked_intervals(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> list[Any]:
        """Return appointments overlapping a window, as booked intervals.

        Imported lazily so this module does not import Doctor Management at
        module scope — the dependency runs Appointments → Doctors, and keeping
        it inside the call makes an accidental cycle impossible.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor whose calendar to read.
        :param window_start: Window start (UTC).
        :param window_end: Window end (UTC).
        :returns: Booked intervals for slot generation.
        """
        from app.services.doctor_service import BookedInterval

        rows = await self._appointments.booked_intervals_for_doctor(
            hospital_id, doctor_id, window_start, window_end
        )
        return [
            BookedInterval(
                starts_at=row.scheduled_start,
                ends_at=row.scheduled_end,
                appointment_id=row.id,
            )
            for row in rows
        ]

    async def has_future_appointments(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID, *, after: datetime
    ) -> int:
        """Count a doctor's upcoming appointments, for the FR-5 guard.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor to check.
        :param after: Only appointments starting after this instant (UTC).
        :returns: The number of upcoming appointments.
        """
        return await self._appointments.count_future_for_doctor(hospital_id, doctor_id, after=after)


# ── Module exceptions ───────────────────────────────────────────────────────


class AppointmentNotFoundError(NotFoundError):
    """Raised when an appointment is absent from the requested hospital.

    Also raised for one in another tenant: a cross-tenant lookup must be
    indistinguishable from a miss.
    """

    def __init__(self, appointment_id: uuid.UUID) -> None:
        super().__init__(
            message="Appointment not found.", detail={"appointment_id": str(appointment_id)}
        )


class InvalidTransitionError(BusinessRuleError):
    """Raised when a status change is not permitted by the state machine.

    Business rule 6 requires 400 rather than 409: the request is malformed
    against the lifecycle, not in conflict with another write.
    """

    def __init__(self, current: AppointmentStatus, requested: AppointmentStatus) -> None:
        allowed = sorted(status.value for status in ALLOWED_TRANSITIONS[current])
        super().__init__(
            message=(f"Cannot move an appointment from '{current.value}' to '{requested.value}'."),
            detail={
                "current_status": current.value,
                "requested_status": requested.value,
                "allowed_transitions": allowed,
            },
        )


class DoubleBookingError(ConflictError):
    """Raised when a booking would overlap the doctor's existing schedule.

    FR-2 and AC-2. Carries the clashing appointments so reception can re-fetch
    slots and pick another (module spec §14).
    """

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(
            message=(
                "The doctor already has an appointment overlapping this time. "
                "Re-fetch available slots and choose another."
            ),
            detail={"conflicting_appointments": conflicts},
        )


class OutsideAvailabilityError(BusinessRuleError):
    """Raised when a booking falls outside the doctor's published availability.

    Business rule 4: overridable by a caller holding
    ``appointment.book_override``.
    """

    def __init__(self, doctor_id: uuid.UUID) -> None:
        super().__init__(
            message=(
                "That time is outside the doctor's availability. Book a published "
                "slot, or retry with the override permission."
            ),
            detail={"doctor_id": str(doctor_id)},
        )


class AppointmentService:
    """Booking, the lifecycle state machine, queues, and the no-show sweep.

    :param appointments: Appointment data access.
    :param patients: Patient lookups, for validating the booking.
    :param doctors: Doctor lookups and availability.
    :param hospitals: Hospital lookups, for timezone and grace settings.
    :param session: Request-scoped session, held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    :param invoices: Receives completed appointments for Billing.
    :param slot_ranker: Chooses a slot for the AI suggestion endpoint.
        ``None`` means AI is not configured on this server: that endpoint then
        answers ``AI_NOT_CONFIGURED`` and everything else — booking by hand
        above all — works exactly as before.
    """

    def __init__(
        self,
        appointments: AppointmentRepository,
        patients: PatientRepository,
        doctors: DoctorRepository,
        hospitals: HospitalRepository,
        session: AsyncSession,
        audit: AuditSink,
        invoices: InvoiceDraftSink,
        slot_ranker: SlotRanker | None = None,
    ) -> None:
        self._appointments = appointments
        self._patients = patients
        self._doctors = doctors
        self._hospitals = hospitals
        self._session = session
        self._audit = audit
        self._invoices = invoices
        self._ai = slot_ranker

    # ── Booking ───────────────────────────────────────────────────────────────

    async def book_appointment(
        self,
        hospital_id: uuid.UUID,
        payload: BookAppointmentRequest,
        *,
        idempotency_key: str,
        actor_id: uuid.UUID | None = None,
        allow_override: bool = False,
        audit_event: Callable[[Appointment], AuditEvent] | None = None,
    ) -> tuple[AppointmentResponse, bool]:
        """Book an appointment (module spec §5.2).

        Returns ``(appointment, created)``. ``created`` is ``False`` when the
        idempotency key matched an existing booking — the caller replays the
        original response rather than double-booking (business rule 8, FR-8).

        :param hospital_id: The hospital booking the appointment.
        :param payload: Validated booking data.
        :param idempotency_key: Client-supplied key; required by rule 8.
        :param actor_id: UUID of the acting user.
        :param allow_override: Caller holds ``appointment.book_override``, so
            availability is advisory rather than binding (rule 4).
        :param audit_event: Builds the audit event recorded for the booking, in
            place of the staff ``appointment.booked`` one. For a caller that is
            not a staff user — the Patient App — so the one audit row of the
            booking names its real actor and commits with the appointment.
        :returns: The appointment and whether it was newly created.
        :raises ValidationError: If patient or doctor is unusable.
        :raises DoubleBookingError: If the doctor is already busy.
        :raises OutsideAvailabilityError: If outside availability without override.
        """
        replayed = await self._appointments.get_by_idempotency_key(hospital_id, idempotency_key)
        if replayed is not None:
            logger.info(
                "appointment.idempotent_replay",
                hospital_id=str(hospital_id),
                appointment_id=str(replayed.id),
            )
            return AppointmentResponse.from_model(replayed), False

        start = payload.scheduled_start.astimezone(UTC)
        end = payload.scheduled_end.astimezone(UTC)

        await self._assert_patient_valid(hospital_id, payload.patient_id)
        await self._assert_doctor_valid(hospital_id, payload.doctor_id)
        self._assert_not_in_past(start, payload.type)
        await self._assert_no_overlap(hospital_id, payload.doctor_id, start, end)

        if not allow_override:
            await self._assert_within_availability(hospital_id, payload.doctor_id, start, end)

        try:
            async with self._session.begin_nested():
                appointment = await self._appointments.create_appointment(
                    hospital_id=hospital_id,
                    patient_id=payload.patient_id,
                    doctor_id=payload.doctor_id,
                    scheduled_start=start,
                    scheduled_end=end,
                    appointment_type=payload.type,
                    created_by=actor_id,
                    reason=payload.reason,
                    notes=payload.notes,
                    idempotency_key=idempotency_key,
                )
                # Business rule 7: the booking itself is a transition, from
                # nothing to `booked`, and gets a history row like any other.
                await self._appointments.record_transition(
                    appointment=appointment,
                    from_status=None,
                    to_status=AppointmentStatus.BOOKED,
                    changed_by=actor_id,
                    reason="booked",
                )
        except IntegrityError as exc:
            # Lost a race: either the exclusion constraint or the idempotency
            # index fired between our check and this write.
            await self._raise_for_integrity(exc, hospital_id, payload, idempotency_key)
            raise

        await self._audit.record(
            audit_event(appointment)
            if audit_event is not None
            else AuditEvent(
                action="appointment.booked",
                hospital_id=hospital_id,
                target_type="appointment",
                target_id=appointment.id,
                actor_id=actor_id,
                context={
                    "doctor_id": str(payload.doctor_id),
                    "type": payload.type.value,
                    "override_used": allow_override,
                },
            )
        )
        await self._session.commit()

        logger.info(
            "appointment.booked",
            hospital_id=str(hospital_id),
            appointment_id=str(appointment.id),
            doctor_id=str(payload.doctor_id),
            actor_id=str(actor_id) if actor_id else None,
        )
        return AppointmentResponse.from_model(appointment), True

    async def reschedule_appointment(
        self,
        hospital_id: uuid.UUID,
        appointment_id: uuid.UUID,
        payload: RescheduleAppointmentRequest,
        *,
        actor_id: uuid.UUID | None = None,
        allow_override: bool = False,
    ) -> AppointmentResponse:
        """Move a booked appointment to a new window (module spec §5.3).

        Only from ``booked``: once a patient has checked in, moving the
        appointment is a cancel-and-rebook, not an edit.

        :param hospital_id: The hospital the appointment belongs to.
        :param appointment_id: The appointment to move.
        :param payload: New window and optional reason.
        :param actor_id: UUID of the acting user.
        :param allow_override: Caller holds ``appointment.book_override``.
        :returns: The rescheduled appointment.
        :raises AppointmentNotFoundError: If absent from this tenant.
        :raises InvalidTransitionError: If it is not in ``booked``.
        :raises DoubleBookingError: If the new window clashes.
        """
        appointment = await self._get_or_raise(hospital_id, appointment_id)

        if appointment.status is not AppointmentStatus.BOOKED:
            raise InvalidTransitionError(appointment.status, AppointmentStatus.BOOKED)

        start = payload.scheduled_start.astimezone(UTC)
        end = payload.scheduled_end.astimezone(UTC)

        self._assert_not_in_past(start, appointment.type)
        await self._assert_no_overlap(
            hospital_id,
            appointment.doctor_id,
            start,
            end,
            exclude_appointment_id=appointment.id,
        )
        if not allow_override:
            await self._assert_within_availability(hospital_id, appointment.doctor_id, start, end)

        previous = (appointment.scheduled_start, appointment.scheduled_end)

        try:
            async with self._session.begin_nested():
                appointment = await self._appointments.update_appointment(
                    appointment,
                    updated_by=actor_id,
                    scheduled_start=start,
                    scheduled_end=end,
                )
                # §5.3 step 5: a reschedule is recorded as booked -> booked so
                # the history shows the move rather than silently rewriting the
                # original time.
                await self._appointments.record_transition(
                    appointment=appointment,
                    from_status=AppointmentStatus.BOOKED,
                    to_status=AppointmentStatus.BOOKED,
                    changed_by=actor_id,
                    reason=payload.reason or "reschedule",
                )
        except IntegrityError as exc:
            await self._raise_for_overlap(exc, hospital_id, appointment.doctor_id, start, end)
            raise

        await self._audit.record(
            AuditEvent(
                action="appointment.rescheduled",
                hospital_id=hospital_id,
                target_type="appointment",
                target_id=appointment.id,
                actor_id=actor_id,
                changes={
                    "scheduled_start": {
                        "before": previous[0].isoformat(),
                        "after": start.isoformat(),
                    },
                    "scheduled_end": {
                        "before": previous[1].isoformat(),
                        "after": end.isoformat(),
                    },
                },
            )
        )
        await self._session.commit()

        logger.info(
            "appointment.rescheduled",
            hospital_id=str(hospital_id),
            appointment_id=str(appointment.id),
        )
        return AppointmentResponse.from_model(appointment)

    # ── Lifecycle transitions ─────────────────────────────────────────────────

    async def check_in(
        self,
        hospital_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> AppointmentResponse:
        """Record the patient's arrival (module spec §5.5)."""
        return await self._transition(
            hospital_id,
            appointment_id,
            AppointmentStatus.CHECKED_IN,
            actor_id=actor_id,
            stamp="checked_in_at",
            action="appointment.checked_in",
        )

    async def start(
        self,
        hospital_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> AppointmentResponse:
        """Begin the consultation (module spec §5.6)."""
        return await self._transition(
            hospital_id,
            appointment_id,
            AppointmentStatus.IN_PROGRESS,
            actor_id=actor_id,
            stamp="started_at",
            action="appointment.started",
        )

    async def complete(
        self,
        hospital_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> AppointmentResponse:
        """Finish the consultation and hand off to Billing (module spec §5.6).

        The invoice draft is requested *after* the commit: a Billing failure
        must not roll back a consultation that genuinely happened.
        """
        result = await self._transition(
            hospital_id,
            appointment_id,
            AppointmentStatus.COMPLETED,
            actor_id=actor_id,
            stamp="completed_at",
            action="appointment.completed",
        )
        await self._invoices.draft_invoice_for(hospital_id, appointment_id, actor_id=actor_id)
        return result

    async def cancel(
        self,
        hospital_id: uuid.UUID,
        appointment_id: uuid.UUID,
        payload: CancelAppointmentRequest,
        *,
        actor_id: uuid.UUID | None = None,
        precondition: Callable[[Appointment], None] | None = None,
        audit_event: Callable[[Appointment, AppointmentStatus], AuditEvent] | None = None,
    ) -> AppointmentResponse:
        """Cancel an appointment (module spec §5.4).

        :param payload: Carries the required reason.
        :param precondition: Called with the locked, current appointment before
            the state machine is consulted; raises to refuse. For a caller with
            rules narrower than the staff state machine — the Patient App may
            cancel only its own ``booked`` appointment, before a cut-off.
        :param audit_event: Builds the audit event from the appointment and the
            status it left, in place of the staff ``appointment.cancelled`` one.
        """
        return await self._transition(
            hospital_id,
            appointment_id,
            AppointmentStatus.CANCELLED,
            actor_id=actor_id,
            action="appointment.cancelled",
            reason=payload.reason,
            extra_fields={"cancelled_reason": payload.reason},
            precondition=precondition,
            audit_event=audit_event,
        )

    async def mark_no_show(
        self,
        hospital_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        reason: str = "no_show",
    ) -> AppointmentResponse:
        """Mark an appointment as a no-show."""
        return await self._transition(
            hospital_id,
            appointment_id,
            AppointmentStatus.NO_SHOW,
            actor_id=actor_id,
            action="appointment.no_show",
            reason=reason,
        )

    # ── The no-show sweep (module spec §5.7, FR-7, AC-5) ──────────────────────

    async def sweep_no_shows(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """Mark overdue appointments as no-shows.

        Called by the Arq worker every five minutes. Split from the job itself
        so the rule is testable without a Redis broker or a running scheduler.

        A scheduled job has no user and no single hospital, so tenancy is
        explicit here (``app/core/tenancy.py``):

        * *finding* the overdue appointments is the one cross-hospital step,
          and runs under a named system scope — see
          :meth:`~app.repositories.appointment_repository.AppointmentRepository.find_no_show_candidates`;
        * *changing* each one runs under that appointment's own hospital, read
          off the row. While it is bound, the session can neither read nor
          write another hospital's rows, so one appointment's update cannot
          spill into a different hospital.

        Each hospital is recorded on its own audit event, so the trail stays
        per-tenant.

        The grace period is read per hospital from ``hospitals.settings``
        (§5.7), falling back to :data:`DEFAULT_NO_SHOW_GRACE_MINUTES`.

        :param now: Reference instant. Injectable so tests can sit exactly on
            the grace boundary instead of sleeping.
        :param limit: Maximum appointments to sweep in one run.
        :returns: How many were marked.
        """
        reference = now or datetime.now(UTC)

        # Widest possible grace, so the query returns every plausible candidate;
        # each is then re-checked against its own hospital's setting below.
        with tenant_scope(TenantScope.system("no-show sweeper: find overdue appointments")):
            candidates = await self._appointments.find_no_show_candidates(
                cutoff=reference - timedelta(minutes=DEFAULT_NO_SHOW_GRACE_MINUTES), limit=limit
            )

        swept = 0
        for appointment in candidates:
            with tenant_scope(TenantScope.hospital(appointment.hospital_id)):
                swept += await self._sweep_one(appointment, reference)

        if swept:
            await self._session.commit()

        logger.info(
            "appointment.no_show_sweep",
            candidates=len(candidates),
            swept=swept,
            reference=reference.isoformat(),
        )
        return swept

    async def _sweep_one(self, appointment: Appointment, reference: datetime) -> int:
        """Mark one overdue appointment as a no-show, inside its own hospital.

        The caller has bound the appointment's hospital as the tenant scope.

        :param appointment: A sweep candidate.
        :param reference: The instant the sweep is measured against.
        :returns: ``1`` if it was marked, ``0`` if it is still within its
            hospital's grace window.
        """
        grace = await self._no_show_grace_minutes(appointment.hospital_id)
        if appointment.scheduled_end >= reference - timedelta(minutes=grace):
            # Inside this hospital's grace window — not overdue yet.
            return 0

        previous = appointment.status
        await self._appointments.update_appointment(appointment, status=AppointmentStatus.NO_SHOW)
        await self._appointments.record_transition(
            appointment=appointment,
            from_status=previous,
            to_status=AppointmentStatus.NO_SHOW,
            # NULL actor: the system did this, not a user.
            changed_by=None,
            reason="no_show_sweeper",
        )
        await self._audit.record(
            AuditEvent(
                action="appointment.no_show",
                hospital_id=appointment.hospital_id,
                target_type="appointment",
                target_id=appointment.id,
                actor_id=None,
                context={"swept_by": "no_show_sweeper", "from_status": previous.value},
            )
        )
        return 1

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_appointment(
        self, hospital_id: uuid.UUID, appointment_id: uuid.UUID
    ) -> AppointmentResponse:
        """Retrieve one appointment.

        :raises AppointmentNotFoundError: If absent from this tenant.
        """
        return AppointmentResponse.from_model(await self._get_or_raise(hospital_id, appointment_id))

    async def list_appointments(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        on_date: date | None = None,
        **filters: Any,
    ) -> Page[AppointmentSummaryResponse]:
        """List appointments (module spec §9).

        ``on_date`` is a calendar day in the **hospital's own timezone**. A
        receptionist asking for "today's appointments" means the clinic's
        today, and only the server knows the clinic's zone exactly: a client
        can send a whole-hour offset at best, which is wrong by thirty minutes
        for India (UTC+5:30) and put appointments near midnight on the wrong
        day.

        :param hospital_id: The hospital to list.
        :param pagination: Page and page size. Defaults to page 1.
        :param on_date: Only appointments starting on this local calendar day.
        :param filters: patient_id, doctor_id, status, appointment_type, and the
            ``starts_on_or_after`` / ``starts_before`` window.
        :returns: One page of summaries plus the total count.
        """
        page_params = pagination or PaginationParams()

        if on_date is not None:
            starts_on_or_after, starts_before = await self._local_day_bounds(hospital_id, on_date)
            filters["starts_on_or_after"] = starts_on_or_after
            filters["starts_before"] = starts_before

        rows = await self._appointments.list_appointments(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._appointments.count_appointments(hospital_id, **filters)

        return Page[AppointmentSummaryResponse](
            items=[AppointmentSummaryResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    async def get_status_history(
        self, hospital_id: uuid.UUID, appointment_id: uuid.UUID
    ) -> list[StatusHistoryEntryResponse]:
        """Return an appointment's transitions, oldest first (AC-6).

        :raises AppointmentNotFoundError: If absent from this tenant.
        """
        await self._get_or_raise(hospital_id, appointment_id)
        rows = await self._appointments.get_status_history(hospital_id, appointment_id)
        return [StatusHistoryEntryResponse.from_model(row) for row in rows]

    async def get_walk_in_queue(
        self, hospital_id: uuid.UUID, *, doctor_id: uuid.UUID | None = None
    ) -> list[AppointmentSummaryResponse]:
        """Return unfinished walk-ins in arrival order (module spec §5.8).

        :param hospital_id: The hospital to read.
        :param doctor_id: Optionally narrow to one doctor.
        :returns: Queued walk-ins.
        """
        rows = await self._appointments.list_walk_in_queue(hospital_id, doctor_id=doctor_id)
        return [AppointmentSummaryResponse.from_model(row) for row in rows]

    # ── AI slot recommendation (module spec §5.9, §13, FR-6) ─────────────────

    async def recommend_slots(
        self,
        hospital_id: uuid.UUID,
        payload: SlotRecommendationRequest,
        *,
        actor_id: uuid.UUID | None = None,
        request_id: str | None = None,
    ) -> SlotRecommendationResponse:
        """Ask the AI for one suggested slot in a doctor's day.

        **The model chooses; it never invents and never books.** The candidate
        slots are computed here, by the Doctor module's own slot generator —
        the same function behind the slot picker — and handed to the ranker
        under opaque ids. What comes back is an id, mapped to a slot this
        method holds, and re-checked against the database before it is
        returned. Nothing is written and nothing is reserved: the booking that
        may follow is the ordinary one, with all of its own validation.

        Every outcome is explicit. A disabled feature, an unconfigured server,
        a failing provider and a rejected answer each raise a typed error;
        none is turned into an empty result.

        :param hospital_id: The hospital to recommend within.
        :param payload: Patient, doctor and hospital-local date.
        :param actor_id: UUID of the acting user, for log attribution.
        :param request_id: Correlation id of the HTTP request.
        :returns: One recommendation, or ``no_free_slots`` (no model call).
        :raises FeatureDisabledError: If the hospital's flag is not on.
        :raises AINotConfiguredError: If AI is not configured on this server.
        :raises ValidationError: If the patient or doctor is not usable, or
            the hospital's timezone is invalid.
        :raises AIError: If the provider failed or its answer was rejected.
        """
        hospital = await self._hospitals.get_by_id(hospital_id)
        if hospital is None or not flag_is_on(hospital.settings, AI_SLOT_RECOMMENDATION):
            # Checked before anything else, so a hospital without the feature
            # learns exactly that and nothing about the deployment or the ids.
            logger.info(
                "appointment.recommend_slot_disabled",
                hospital_id=str(hospital_id),
                reason="feature_flag_off",
            )
            raise FeatureDisabledError(
                "AI slot suggestions are not enabled for this hospital.",
                detail={"feature": AI_SLOT_RECOMMENDATION},
            )

        if self._ai is None:
            logger.info(
                "appointment.recommend_slot_unavailable",
                hospital_id=str(hospital_id),
                reason="ai_not_configured",
            )
            raise AINotConfiguredError("not_configured")

        await self._assert_patient_valid(hospital_id, payload.patient_id)
        await self._assert_doctor_valid(hospital_id, payload.doctor_id)

        timezone = hospital.timezone
        day, truncated = await self._doctor_day(
            hospital_id, payload.doctor_id, payload.date, timezone
        )
        by_id = {slot.slot_id: slot for slot in day if slot.slot_id is not None}

        if not by_id:
            logger.info(
                "appointment.recommend_slot_no_candidates",
                hospital_id=str(hospital_id),
                doctor_id=str(payload.doctor_id),
                date=payload.date.isoformat(),
            )
            return SlotRecommendationResponse(
                status=SlotRecommendationStatus.NO_FREE_SLOTS,
                recommendation=None,
                date=payload.date,
                timezone=timezone,
                candidate_count=0,
            )

        # Nothing is pending. This ends the read transaction so the pooled
        # connection is not held for the length of the outbound call.
        await self._session.commit()

        # No try/except: a typed AI error is the answer, not something to hide.
        choice = await self._ai.choose_slot(
            hospital_id=hospital_id,
            actor_id=actor_id,
            request_id=request_id,
            target_date=payload.date,
            day=day,
        )

        chosen = by_id.get(choice.slot_id)
        if chosen is None:
            # Candidate-only selection must not depend on one ranker
            # implementation getting it right.
            self._log_rejected(hospital_id, "unknown_candidate", len(by_id), choice, request_id)
            raise AIResponseInvalidError("unknown_candidate")

        if not await self._slot_still_free(hospital_id, payload.doctor_id, chosen):
            self._log_rejected(hospital_id, "slot_no_longer_free", len(by_id), choice, request_id)
            raise AIResponseInvalidError("slot_no_longer_free")

        log_fields: dict[str, Any] = {
            "hospital_id": str(hospital_id),
            "doctor_id": str(payload.doctor_id),
            "date": payload.date.isoformat(),
            "candidate_count": len(by_id),
            "provider": choice.provider,
            "model": choice.model,
            "actor_id": str(actor_id) if actor_id is not None else None,
            "request_id": request_id,
        }
        if truncated:
            log_fields["candidates_truncated"] = True
        logger.info("appointment.recommend_slot", **log_fields)

        # Only `reason` comes from the model; the times and the doctor are ours.
        reason = choice.reason[:MAX_RECOMMENDATION_REASON_LENGTH] if choice.reason else None
        return SlotRecommendationResponse(
            status=SlotRecommendationStatus.RECOMMENDED,
            recommendation=SlotRecommendation(
                slot_start=chosen.start,
                slot_end=chosen.end,
                doctor_id=payload.doctor_id,
                reason=reason,
            ),
            date=payload.date,
            timezone=timezone,
            candidate_count=len(by_id),
        )

    async def _doctor_day(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID, target_date: date, timezone: str
    ) -> tuple[list[DaySlot], bool]:
        """Compute one hospital-local day of a doctor's slots for the ranker.

        Gathers the same four inputs as ``DoctorService.get_slots`` and calls
        the same pure generator, so the candidates are exactly the slot
        picker's available slots that have not yet started. A slot is a
        candidate when it is available and starts in the future; every other
        slot of the day is kept, without an id, so the ranker sees the shape
        of the day.

        :param hospital_id: The tenant to scope every read to.
        :param doctor_id: The doctor whose day to compute.
        :param target_date: The hospital-local date.
        :param timezone: The hospital's IANA timezone.
        :returns: The day's slots in chronological order, and whether the day
            had to be cut to :data:`MAX_DAY_SLOTS`.
        :raises ValidationError: If ``timezone`` is not a known IANA zone.
        """
        # Imported lazily: the dependency runs Appointments → Doctors, and
        # keeping it inside the call makes an accidental cycle impossible.
        from app.models.doctor import SlotStatus
        from app.services.doctor_service import BookedInterval, generate_slots

        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"Hospital timezone {timezone!r} is not a valid IANA timezone."
            raise ValidationError(message=msg) from exc

        day_start = datetime.combine(target_date, time.min, tzinfo=zone)
        day_end = day_start + timedelta(days=1)

        windows = await self._doctors.get_availability(hospital_id, doctor_id)
        weekday = target_date.weekday()
        availability = [
            (window.start_time, window.end_time, window.slot_duration_minutes)
            for window in windows
            if window.day_of_week == weekday
        ]
        leave_rows = await self._doctors.list_leaves(
            hospital_id, doctor_id, starts_before=day_end, ends_after=day_start
        )
        leaves = [(leave.starts_at, leave.ends_at) for leave in leave_rows]
        appointments = await self._appointments.booked_intervals_for_doctor(
            hospital_id, doctor_id, day_start, day_end
        )
        booked = [
            BookedInterval(
                starts_at=row.scheduled_start, ends_at=row.scheduled_end, appointment_id=row.id
            )
            for row in appointments
        ]

        slots = generate_slots(
            target_date=target_date,
            availability=availability,
            leaves=leaves,
            booked=booked,
            timezone=timezone,
        )

        now = _utc_now()
        seen: set[tuple[datetime, datetime]] = set()
        day: list[DaySlot] = []
        truncated = False
        next_id = 1
        for slot in slots:
            key = (slot.start, slot.end)
            if key in seen:
                continue
            if len(day) >= MAX_DAY_SLOTS:
                truncated = True
                break
            seen.add(key)
            if slot.status is SlotStatus.AVAILABLE and slot.start > now:
                day.append(DaySlot(start=slot.start, end=slot.end, slot_id=f"S{next_id}"))
                next_id += 1
            else:
                day.append(DaySlot(start=slot.start, end=slot.end, slot_id=None))
        return day, truncated

    async def _slot_still_free(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID, chosen: DaySlot
    ) -> bool:
        """Re-check, after the model call, that the chosen slot can be booked.

        The decision is made by whether fresh queries return rows — never by
        re-reading rows this session already loaded. The session keeps loaded
        objects un-expired across a commit, so an appointment moved onto the
        slot while the model was answering would look unmoved to a recompute
        of the day; the overlap query's ``WHERE`` clause is evaluated by the
        database against current rows and does see it.

        The booking endpoint's exclusion constraint remains the real guarantee
        against a double booking. This exists so the server never returns, as
        validated, a slot it could have known was taken.

        :param hospital_id: The tenant to scope every read to.
        :param doctor_id: The doctor the slot belongs to.
        :param chosen: The slot the ranker chose.
        :returns: ``True`` when the slot is still in the future, clashes with
            no appointment or leave, and lies within published availability.
        """
        start = chosen.start.astimezone(UTC)
        end = chosen.end.astimezone(UTC)

        if start <= _utc_now():
            return False
        if await self._appointments.find_overlapping(hospital_id, doctor_id, start, end):
            return False
        if await self._doctors.list_leaves(
            hospital_id, doctor_id, starts_before=end, ends_after=start
        ):
            return False
        try:
            await self._assert_within_availability(hospital_id, doctor_id, start, end)
        except OutsideAvailabilityError:
            # Reported by the return value; the caller raises its own error
            # outside this block.
            return False
        return True

    @staticmethod
    def _log_rejected(
        hospital_id: uuid.UUID,
        kind: str,
        candidate_count: int,
        choice: SlotChoice,
        request_id: str | None,
    ) -> None:
        """Log a rejected suggestion: the reason kind, never the id or the text."""
        logger.warning(
            "appointment.slot_recommendation_rejected",
            kind=kind,
            candidate_count=candidate_count,
            provider=choice.provider,
            model=choice.model,
            hospital_id=str(hospital_id),
            request_id=request_id,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _transition(
        self,
        hospital_id: uuid.UUID,
        appointment_id: uuid.UUID,
        target: AppointmentStatus,
        *,
        actor_id: uuid.UUID | None,
        action: str,
        stamp: str | None = None,
        reason: str | None = None,
        extra_fields: dict[str, Any] | None = None,
        precondition: Callable[[Appointment], None] | None = None,
        audit_event: Callable[[Appointment, AppointmentStatus], AuditEvent] | None = None,
    ) -> AppointmentResponse:
        """Move an appointment along the state machine.

        The single place status changes, so business rule 7 — a history row per
        change — cannot be forgotten at a call site.

        The appointment's row is locked for the length of the transaction:
        two transitions of one appointment — a patient cancelling while
        reception checks them in — cannot both start from the same status.
        The second waits, reads what the first committed, and is then judged
        by the state machine against that.

        :param hospital_id: The hospital the appointment belongs to.
        :param appointment_id: The appointment to move.
        :param target: Status to move to.
        :param actor_id: UUID of the acting user.
        :param action: Audit action name.
        :param stamp: Timestamp column to set to now, if any.
        :param reason: Recorded on the history row.
        :param extra_fields: Further columns to write in the same update.
        :returns: The updated appointment.
        :raises AppointmentNotFoundError: If absent from this tenant.
        :raises InvalidTransitionError: If the move is not legal (rule 6).
        """
        locked = await self._appointments.lock_appointment_by_id(hospital_id, appointment_id)
        if locked is None:
            raise AppointmentNotFoundError(appointment_id)
        appointment = locked
        current = appointment.status
        if precondition is not None:
            precondition(appointment)

        if target not in ALLOWED_TRANSITIONS[current]:
            logger.info(
                "appointment.invalid_transition",
                hospital_id=str(hospital_id),
                appointment_id=str(appointment_id),
                current=current.value,
                requested=target.value,
            )
            raise InvalidTransitionError(current, target)

        fields: dict[str, Any] = {"status": target, **(extra_fields or {})}
        if stamp is not None:
            fields[stamp] = datetime.now(UTC)

        appointment = await self._appointments.update_appointment(
            appointment, updated_by=actor_id, **fields
        )
        await self._appointments.record_transition(
            appointment=appointment,
            from_status=current,
            to_status=target,
            changed_by=actor_id,
            reason=reason,
        )

        await self._audit.record(
            audit_event(appointment, current)
            if audit_event is not None
            else AuditEvent(
                action=action,
                hospital_id=hospital_id,
                target_type="appointment",
                target_id=appointment.id,
                actor_id=actor_id,
                changes={"status": {"before": current.value, "after": target.value}},
            )
        )
        await self._session.commit()

        logger.info(
            action,
            hospital_id=str(hospital_id),
            appointment_id=str(appointment.id),
            from_status=current.value,
            to_status=target.value,
        )
        return AppointmentResponse.from_model(appointment)

    async def _get_or_raise(self, hospital_id: uuid.UUID, appointment_id: uuid.UUID) -> Appointment:
        """Fetch an appointment or raise :class:`AppointmentNotFoundError`."""
        appointment = await self._appointments.get_appointment_by_id(hospital_id, appointment_id)
        if appointment is None:
            raise AppointmentNotFoundError(appointment_id)
        return appointment

    async def _assert_patient_valid(self, hospital_id: uuid.UUID, patient_id: uuid.UUID) -> None:
        """Check the patient exists in this tenant (business rule 1)."""
        patient = await self._patients.get_patient_by_id(hospital_id, patient_id)
        if patient is None:
            raise ValidationError(
                message="Patient not found in this hospital.",
                detail={"errors": [{"field": "patient_id", "message": "Unknown patient."}]},
            )

    async def _assert_doctor_valid(self, hospital_id: uuid.UUID, doctor_id: uuid.UUID) -> None:
        """Check the doctor exists and is active in this tenant (rule 1)."""
        doctor = await self._doctors.get_doctor_by_id(hospital_id, doctor_id)
        if doctor is None:
            raise ValidationError(
                message="Doctor not found in this hospital.",
                detail={"errors": [{"field": "doctor_id", "message": "Unknown doctor."}]},
            )

    @staticmethod
    def _assert_not_in_past(start: datetime, appointment_type: AppointmentType) -> None:
        """Reject a start time in the past (module spec §11).

        Walk-ins get a short backdate grace: the patient is already at the desk
        when reception types the booking in.

        :raises ValidationError: If the start is too far in the past.
        """
        grace = (
            timedelta(minutes=WALK_IN_BACKDATE_GRACE_MINUTES)
            if appointment_type is AppointmentType.WALK_IN
            else timedelta(0)
        )
        if start < datetime.now(UTC) - grace:
            raise ValidationError(
                message="Cannot book an appointment in the past.",
                detail={
                    "errors": [{"field": "scheduled_start", "message": "Must be in the future."}]
                },
            )

    async def _assert_no_overlap(
        self,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        start: datetime,
        end: datetime,
        *,
        exclude_appointment_id: uuid.UUID | None = None,
    ) -> None:
        """Reject a booking clashing with the doctor's schedule (FR-2).

        Advisory: the exclusion constraint is the real guarantee. This exists so
        the ordinary case returns a useful 409 instead of an IntegrityError.

        :raises DoubleBookingError: If a clash is found.
        """
        conflicts = await self._appointments.find_overlapping(
            hospital_id,
            doctor_id,
            start,
            end,
            exclude_appointment_id=exclude_appointment_id,
        )
        if conflicts:
            raise DoubleBookingError(
                [
                    {
                        "appointment_id": str(conflict.id),
                        "scheduled_start": conflict.scheduled_start.isoformat(),
                        "scheduled_end": conflict.scheduled_end.isoformat(),
                        "status": conflict.status.value,
                    }
                    for conflict in conflicts
                ]
            )

    async def _assert_within_availability(
        self, hospital_id: uuid.UUID, doctor_id: uuid.UUID, start: datetime, end: datetime
    ) -> None:
        """Reject a booking outside published availability (business rule 4).

        Compared as wall-clock in the hospital's timezone, because that is how
        availability is stored (``doctor_availability`` §2.9). A booking is
        inside availability when some window on that local weekday fully
        contains it.

        :raises OutsideAvailabilityError: If no window contains the booking.
        """
        hospital = await self._hospitals.get_by_id(hospital_id)
        zone = ZoneInfo(hospital.timezone if hospital else "UTC")

        local_start = start.astimezone(zone)
        local_end = end.astimezone(zone)

        windows = await self._doctors.get_availability(hospital_id, doctor_id)
        for window in windows:
            if window.day_of_week != local_start.weekday():
                continue
            if window.start_time <= local_start.time() and local_end.time() <= window.end_time:
                return

        raise OutsideAvailabilityError(doctor_id)

    async def _local_day_bounds(
        self, hospital_id: uuid.UUID, on: date
    ) -> tuple[datetime, datetime]:
        """Return the instants bounding a calendar day in the hospital's timezone.

        Built from local midnight to the next local midnight rather than by
        adding 24 hours, so a day that is 23 or 25 hours long across a DST
        change is still bounded correctly.

        An unknown or missing timezone falls back to UTC rather than failing
        the read: a misconfigured hospital should still be able to see its
        schedule.

        :param hospital_id: The tenant whose timezone applies.
        :param on: The local calendar day.
        :returns: ``(start, end)`` as a half-open interval of aware datetimes.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        hospital = await self._hospitals.get_by_id(hospital_id)
        try:
            zone = ZoneInfo(hospital.timezone if hospital else "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "appointment.hospital_timezone_invalid",
                hospital_id=str(hospital_id),
                timezone=hospital.timezone if hospital else None,
            )
            zone = ZoneInfo("UTC")

        start = datetime.combine(on, time.min, tzinfo=zone)
        end = datetime.combine(on + timedelta(days=1), time.min, tzinfo=zone)
        return start, end

    async def _no_show_grace_minutes(self, hospital_id: uuid.UUID) -> int:
        """Return a hospital's no-show grace period in minutes (module spec §5.7).

        Read from the ``hospitals.settings`` JSONB rather than a dedicated
        column, which is what that column is for ("feature flags, hours,
        policies") and avoids a migration for one tunable.

        :param hospital_id: The tenant to read.
        :returns: The configured grace, or the platform default.
        """
        hospital = await self._hospitals.get_by_id(hospital_id)
        if hospital is None:
            return DEFAULT_NO_SHOW_GRACE_MINUTES
        raw = (hospital.settings or {}).get("no_show_grace_minutes")
        if isinstance(raw, int) and raw >= 0:
            return raw
        return DEFAULT_NO_SHOW_GRACE_MINUTES

    async def _raise_for_integrity(
        self,
        exc: IntegrityError,
        hospital_id: uuid.UUID,
        payload: BookAppointmentRequest,
        idempotency_key: str,
    ) -> None:
        """Translate a booking IntegrityError into the right domain error.

        Two constraints can fire here. The idempotency index means a concurrent
        retry won the race, so the honest answer is the appointment that retry
        created. The exclusion constraint means someone else took the slot.

        :raises DoubleBookingError: On an overlap violation.
        """
        blob = str(getattr(exc, "orig", exc))

        if "uq_appointments_hospital_idempotency_key" in blob:
            existing = await self._appointments.get_by_idempotency_key(hospital_id, idempotency_key)
            if existing is not None:
                logger.info(
                    "appointment.idempotent_race_resolved",
                    hospital_id=str(hospital_id),
                    appointment_id=str(existing.id),
                )
                return

        await self._raise_for_overlap(
            exc,
            hospital_id,
            payload.doctor_id,
            payload.scheduled_start.astimezone(UTC),
            payload.scheduled_end.astimezone(UTC),
        )

    async def _raise_for_overlap(
        self,
        exc: IntegrityError,
        hospital_id: uuid.UUID,
        doctor_id: uuid.UUID,
        start: datetime,
        end: datetime,
    ) -> None:
        """Turn an exclusion-constraint violation into a 409 (module spec §14).

        :raises DoubleBookingError: If the exclusion constraint fired.
        """
        if "no_overlap_per_doctor" not in str(getattr(exc, "orig", exc)):
            return

        logger.info(
            "appointment.double_booking_race",
            hospital_id=str(hospital_id),
            doctor_id=str(doctor_id),
        )
        conflicts = await self._appointments.find_overlapping(hospital_id, doctor_id, start, end)
        raise DoubleBookingError(
            [
                {
                    "appointment_id": str(conflict.id),
                    "scheduled_start": conflict.scheduled_start.isoformat(),
                    "scheduled_end": conflict.scheduled_end.isoformat(),
                    "status": conflict.status.value,
                }
                for conflict in conflicts
            ]
        ) from exc
