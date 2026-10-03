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
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
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
    SlotRecommendationRequest,
    SlotRecommendationResponse,
    StatusHistoryEntryResponse,
)
from app.schemas.common import Page, PaginationParams

if TYPE_CHECKING:
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
    "DoubleBookingError",
    "InvalidTransitionError",
    "InvoiceDraftSink",
    "NullInvoiceDraftSink",
    "OutsideAvailabilityError",
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


@runtime_checkable
class SlotRanker(Protocol):
    """Ranks candidate appointment slots (module spec §13).

    A seam rather than a direct :class:`~app.ai.services.ai_service.AIService`
    call, for two reasons. The AI platform layer is owned by another engineer,
    so this module depends on a shape rather than on their internals; and it
    keeps prompt handling, provider selection and cost accounting out of a
    clinical service, which should not care which model answered.
    """

    async def rank_slots(
        self,
        *,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        urgency: str,
        candidates: list[dict[str, Any]],
        limit: int,
    ) -> SlotRecommendationResponse:
        """Return the candidates re-ordered best-first.

        Implementations must only ever return slots drawn from ``candidates``.

        :param hospital_id: Tenant, for budget and audit attribution.
        :param actor_id: Acting user, for cost attribution.
        :param urgency: routine / soon / urgent, supplied not inferred.
        :param candidates: Free slots the model may choose among.
        :param limit: Maximum suggestions to return.
        :returns: Ranked suggestions.
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
    :param slot_ranker: Ranks candidate slots. ``None`` disables the AI
        endpoint, which then returns an empty list rather than failing —
        booking by hand must never depend on the AI stack being configured.
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
            AuditEvent(
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
    ) -> AppointmentResponse:
        """Cancel an appointment (module spec §5.4).

        :param payload: Carries the required reason.
        """
        return await self._transition(
            hospital_id,
            appointment_id,
            AppointmentStatus.CANCELLED,
            actor_id=actor_id,
            action="appointment.cancelled",
            reason=payload.reason,
            extra_fields={"cancelled_reason": payload.reason},
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

        Runs untenanted — see
        :meth:`~app.repositories.appointment_repository.AppointmentRepository.find_no_show_candidates`
        for why — but records each hospital on its own audit event, so the trail
        stays per-tenant.

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
        candidates = await self._appointments.find_no_show_candidates(
            cutoff=reference - timedelta(minutes=DEFAULT_NO_SHOW_GRACE_MINUTES), limit=limit
        )

        swept = 0
        for appointment in candidates:
            grace = await self._no_show_grace_minutes(appointment.hospital_id)
            if appointment.scheduled_end >= reference - timedelta(minutes=grace):
                # Inside this hospital's grace window — not overdue yet.
                continue

            previous = appointment.status
            await self._appointments.update_appointment(
                appointment, status=AppointmentStatus.NO_SHOW
            )
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
            swept += 1

        if swept:
            await self._session.commit()

        logger.info(
            "appointment.no_show_sweep",
            candidates=len(candidates),
            swept=swept,
            reference=reference.isoformat(),
        )
        return swept

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
    ) -> SlotRecommendationResponse:
        """Rank candidate slots for a patient.

        The model **ranks**, it does not invent: candidate slots come from the
        Doctor module's slot generator and the prompt forbids returning
        anything outside that list (§13, "Safety"). Nothing is reserved — a
        suggestion that goes stale between recommendation and booking is caught
        by the ordinary overlap check.

        Degrades to an empty list rather than raising when the feature flag is
        off or the provider is unavailable, because an AI outage must never
        stop reception booking by hand.

        :param hospital_id: The hospital to recommend within.
        :param payload: Patient, urgency, preferred window, and how many.
        :param actor_id: UUID of the acting user, for AI cost attribution.
        :returns: Ranked suggestions, possibly empty.
        """
        if not await self._ai_recommendation_enabled(hospital_id):
            logger.info(
                "appointment.recommend_slot_disabled",
                hospital_id=str(hospital_id),
                reason="feature_flag_off",
            )
            return SlotRecommendationResponse(recommendations=[])

        if self._ai is None:
            logger.info(
                "appointment.recommend_slot_unavailable",
                hospital_id=str(hospital_id),
                reason="ai_service_not_configured",
            )
            return SlotRecommendationResponse(recommendations=[])

        await self._assert_patient_valid(hospital_id, payload.patient_id)

        candidates = await self._candidate_slots(hospital_id, payload)
        if not candidates:
            return SlotRecommendationResponse(recommendations=[])

        try:
            ranked = await self._ai.rank_slots(
                hospital_id=hospital_id,
                actor_id=actor_id,
                urgency=payload.urgency,
                candidates=candidates,
                limit=payload.limit,
            )
        except Exception:  # noqa: BLE001 — an AI failure must not block booking
            logger.warning(
                "appointment.recommend_slot_failed",
                hospital_id=str(hospital_id),
                exc_info=True,
            )
            return SlotRecommendationResponse(recommendations=[])

        logger.info(
            "appointment.recommend_slot",
            hospital_id=str(hospital_id),
            candidate_count=len(candidates),
            returned=len(ranked.recommendations),
        )
        return ranked

    async def _ai_recommendation_enabled(self, hospital_id: uuid.UUID) -> bool:
        """Check the ``feature.ai.slot_recommendation`` flag (module spec §18).

        Read from the ``hospitals.settings`` JSONB, which already exists for
        exactly this ("feature flags, hours, policies"), rather than inventing
        a flag table this module does not own.

        :param hospital_id: The tenant to check.
        :returns: ``True`` when the feature is switched on.
        """
        hospital = await self._hospitals.get_by_id(hospital_id)
        if hospital is None:
            return False
        return bool((hospital.settings or {}).get("feature.ai.slot_recommendation", False))

    async def _candidate_slots(
        self, hospital_id: uuid.UUID, payload: SlotRecommendationRequest
    ) -> list[dict[str, Any]]:
        """Collect the free slots the model is allowed to rank.

        Slot computation belongs to Doctor Management (§2, "Out of Scope"), so
        this reads that module's availability rather than recomputing it, and
        removes anything already booked.

        :param hospital_id: The tenant to scope to.
        :param payload: Carries the doctor and preferred window.
        :returns: Candidate slots as plain dicts for the prompt.
        """
        if payload.doctor_id is None:
            # Choosing across all doctors needs the load-balancing data in §13,
            # which arrives with the Reports module. Until then a doctor must
            # be named, and the endpoint says so by returning nothing.
            return []

        window_start = payload.preferred_window_start or datetime.now(UTC)
        window_end = payload.preferred_window_end or window_start + timedelta(days=7)

        taken = await self._appointments.booked_intervals_for_doctor(
            hospital_id, payload.doctor_id, window_start, window_end
        )
        busy = {(row.scheduled_start, row.scheduled_end) for row in taken}

        windows = await self._doctors.get_availability(hospital_id, payload.doctor_id)
        candidates: list[dict[str, Any]] = []
        for window in windows:
            for day_offset in range((window_end - window_start).days + 1):
                day = (window_start + timedelta(days=day_offset)).date()
                if day.weekday() != window.day_of_week:
                    continue
                slot_start = datetime.combine(day, window.start_time, tzinfo=UTC)
                slot_end = slot_start + timedelta(minutes=window.slot_duration_minutes)
                if slot_start < window_start or slot_end > window_end:
                    continue
                if (slot_start, slot_end) in busy:
                    continue
                candidates.append(
                    {
                        "slot_start": slot_start.isoformat(),
                        "slot_end": slot_end.isoformat(),
                        "doctor_id": str(payload.doctor_id),
                    }
                )
        return candidates[:50]

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
    ) -> AppointmentResponse:
        """Move an appointment along the state machine.

        The single place status changes, so business rule 7 — a history row per
        change — cannot be forgotten at a call site.

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
        appointment = await self._get_or_raise(hospital_id, appointment_id)
        current = appointment.status

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
            AuditEvent(
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
        from zoneinfo import ZoneInfo

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
