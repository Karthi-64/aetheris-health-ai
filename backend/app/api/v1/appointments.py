"""Appointment Management API routes.

Implements the endpoint set in ``docs/modules/05-appointment-management.md`` §9.
Routes parse input, delegate to
:class:`~app.services.appointment_service.AppointmentService`, and wrap the
result in the standard envelope. No business logic, no database access.

**Tenancy.** ``hospital_id`` always comes from the authenticated user.

**Permissions.** Every endpoint declares one from §10. The lifecycle
transitions each carry their own code, so a nurse who may check patients in
cannot also complete a consultation.

**Idempotency.** ``POST /appointments`` requires an ``Idempotency-Key`` header
(business rule 8). A retry with the same key returns the original appointment
and ``200`` rather than creating a second booking.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status

from app.api.dependencies.auth import require_permission
from app.api.dependencies.services import get_appointment_service
from app.core.exceptions import BusinessRuleError
from app.models.appointment import AppointmentStatus, AppointmentType
from app.models.user import User
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
from app.schemas.common import (
    MetadataWithPagination,
    PaginatedResponse,
    PaginationMeta,
    PaginationParams,
    SuccessResponse,
)
from app.services.appointment_service import AppointmentService

router = APIRouter(prefix="/appointments", tags=["Appointments"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    422: {"description": "Request failed validation."},
}

_NOT_FOUND_RESPONSE: dict[int | str, dict[str, str]] = {
    404: {"description": "Appointment not found in this hospital."},
}

_TRANSITION_RESPONSES: dict[int | str, dict[str, str]] = {
    400: {"description": "The state machine does not allow this transition."},
    **_NOT_FOUND_RESPONSE,
    **_COMMON_RESPONSES,
}


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the request acts within.

    A Super Admin has no ``hospital_id``, so there is no tenant to scope
    appointments to. Rejected rather than silently querying across tenants.

    :param current_user: The authenticated user.
    :returns: The hospital UUID to scope every query by.
    :raises BusinessRuleError: If the user belongs to no hospital.
    """
    if current_user.hospital_id is None:
        msg = "This account is not scoped to a hospital, so appointments cannot be accessed."
        raise BusinessRuleError(msg)
    return current_user.hospital_id


def _has_permission(user: User, code: str) -> bool:
    """Check whether a user holds a permission code.

    Used for the *optional* override in business rule 4: booking outside
    availability is allowed, but only for a caller who also holds
    ``appointment.book_override``. That cannot be a route dependency, because
    lacking it must not refuse the request — it only narrows what is permitted.

    :param user: The authenticated user.
    :param code: Permission code to look for.
    :returns: ``True`` if the user holds it.
    """
    # A Super Admin has every permission implicitly, matching require_permission.
    if user.hospital_id is None:
        return True
    for user_role in user.user_roles or []:
        role = user_role.role
        if role and role.role_permissions:
            for mapping in role.role_permissions:
                if mapping.permission and mapping.permission.code == code:
                    return True
    return False


# ── Booking ─────────────────────────────────────────────────────────────────


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[AppointmentResponse],
    summary="Book an appointment",
    description=(
        "Book an appointment (module spec §5.2).\n\n"
        "An `Idempotency-Key` header is **required**. Retrying with the same "
        "key returns the original appointment with `200` instead of booking a "
        "second one, so a client retry after a timeout is safe.\n\n"
        "Double-booking is prevented by a database exclusion constraint, not "
        "just an application check — two receptionists racing for the same slot "
        "means one gets `409` with the conflicting appointment attached.\n\n"
        "Booking outside the doctor's published availability requires "
        "`appointment.book_override`."
    ),
    responses={
        201: {"description": "Appointment booked."},
        200: {"description": "Idempotent replay — the original appointment."},
        409: {"description": "The doctor already has an overlapping appointment."},
        **_COMMON_RESPONSES,
    },
)
async def book_appointment(
    payload: BookAppointmentRequest,
    response: Response,
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=200,
        description="Client-generated key making retries safe. Required.",
    ),
    current_user: User = Depends(require_permission("appointment.book")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[AppointmentResponse]:
    """Book an appointment, idempotently."""
    appointment, created = await service.book_appointment(
        _tenant_of(current_user),
        payload,
        idempotency_key=idempotency_key,
        actor_id=current_user.id,
        allow_override=_has_permission(current_user, "appointment.book_override"),
    )
    if not created:
        # A replay is not a creation. Returning 201 again would tell the client
        # it made a second booking.
        response.status_code = status.HTTP_200_OK
        return SuccessResponse[AppointmentResponse](
            message="Appointment already booked with this key.", data=appointment
        )
    return SuccessResponse[AppointmentResponse](
        message="Appointment booked successfully.", data=appointment
    )


@router.get(
    "",
    response_model=PaginatedResponse[AppointmentSummaryResponse],
    summary="List appointments",
    description=(
        "Return a page of appointments, earliest first.\n\n"
        "Filters: `patient_id`, `doctor_id`, `date`, `status`, `type`. `date` "
        "selects a single calendar day **in the hospital's own timezone** — "
        "send the clinic's local date and nothing else. The earlier "
        "`tz_offset_hours` parameter is gone; it could only express whole "
        "hours, which is wrong for India (UTC+5:30)."
    ),
    responses={200: {"description": "Page of appointments returned."}, **_COMMON_RESPONSES},
)
async def list_appointments(
    patient_id: uuid.UUID | None = Query(None, description="Filter by patient."),
    doctor_id: uuid.UUID | None = Query(None, description="Filter by doctor."),
    appointment_date: date | None = Query(
        None,
        alias="date",
        description="Single calendar day in the hospital's timezone, YYYY-MM-DD.",
    ),
    appointment_status: AppointmentStatus | None = Query(
        None, alias="status", description="Filter by lifecycle status."
    ),
    appointment_type: AppointmentType | None = Query(
        None, alias="type", description="Filter by appointment type."
    ),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("appointment.read")),
    service: AppointmentService = Depends(get_appointment_service),
) -> PaginatedResponse[AppointmentSummaryResponse]:
    """List appointments with the module spec §9 filters."""
    page_result = await service.list_appointments(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        patient_id=patient_id,
        doctor_id=doctor_id,
        status=appointment_status,
        appointment_type=appointment_type,
        on_date=appointment_date,
    )
    return PaginatedResponse[AppointmentSummaryResponse](
        message="Appointments retrieved.",
        data=page_result.items,
        metadata=MetadataWithPagination(
            pagination=PaginationMeta(
                page=page_result.page,
                page_size=page_result.page_size,
                total_records=page_result.total_records,
                total_pages=page_result.total_pages,
            ),
        ),
    )


@router.get(
    "/queue",
    response_model=SuccessResponse[list[AppointmentSummaryResponse]],
    summary="Walk-in queue",
    description=(
        "Return unfinished walk-ins in arrival order (module spec §5.8).\n\n"
        "Arrival order is check-in time where the patient has arrived, and "
        "booking time otherwise — a walk-in is recorded when the patient "
        "reaches the desk."
    ),
    responses={200: {"description": "Queue returned."}, **_COMMON_RESPONSES},
)
async def walk_in_queue(
    doctor_id: uuid.UUID | None = Query(None, description="Narrow the queue to one doctor."),
    current_user: User = Depends(require_permission("appointment.read")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[list[AppointmentSummaryResponse]]:
    """Return the walk-in queue."""
    queue = await service.get_walk_in_queue(_tenant_of(current_user), doctor_id=doctor_id)
    return SuccessResponse[list[AppointmentSummaryResponse]](
        message="Walk-in queue retrieved.", data=queue
    )


@router.post(
    "/recommend-slot",
    response_model=SuccessResponse[SlotRecommendationResponse],
    summary="Ask the AI to suggest one appointment slot",
    description=(
        "Suggest one free slot in a doctor's day (module spec §5.9).\n\n"
        "The server computes the day's free slots itself — the same ones the "
        "slot picker shows — and asks an AI model to choose one. The model is "
        "shown clock times only: no patient, doctor or hospital data. Its "
        "choice is checked against the server's own list and against the "
        "database before it is returned.\n\n"
        "**Advisory only.** Nothing is booked or reserved; a member of staff "
        "books through `POST /appointments`, which validates everything again. "
        "There is no score or confidence.\n\n"
        "Requires `appointment.recommend_slot` and `doctor.availability.read`, "
        "and the `feature.ai.slot_recommendation` flag on the hospital. Every "
        "failure is explicit — there is no silent empty answer:\n\n"
        "- `200` with `status: recommended` or `status: no_free_slots` (no model call)\n"
        "- `403 FEATURE_DISABLED` — not enabled for this hospital\n"
        "- `503 AI_NOT_CONFIGURED` — no AI configured on this server\n"
        "- `503 AI_PROVIDER_UNAVAILABLE` — the AI service could not be used\n"
        "- `503 AI_PROVIDER_TIMEOUT` — the AI service took too long\n"
        "- `503 AI_RESPONSE_INVALID` — the answer was rejected, or the slot was "
        "taken meanwhile"
    ),
    responses={
        200: {"description": "One suggestion, or `no_free_slots` when the day has none."},
        400: {"description": "The account is not scoped to a hospital."},
        401: {"description": "Missing or invalid access token."},
        403: {
            "description": (
                "Lacking a required permission (`PERMISSION_DENIED`), or the feature "
                "is not enabled for this hospital (`FEATURE_DISABLED`)."
            )
        },
        422: {
            "description": (
                "Request failed validation, or the patient or doctor is not in this hospital."
            )
        },
        429: {"description": "Too many AI requests."},
        503: {
            "description": (
                "`AI_NOT_CONFIGURED`, `AI_PROVIDER_UNAVAILABLE`, `AI_PROVIDER_TIMEOUT` "
                "or `AI_RESPONSE_INVALID`. Choose a slot manually."
            )
        },
    },
)
async def recommend_slot(
    payload: SlotRecommendationRequest,
    request: Request,
    current_user: User = Depends(require_permission("appointment.recommend_slot")),
    _availability_reader: User = Depends(require_permission("doctor.availability.read")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[SlotRecommendationResponse]:
    """Ask the AI service for one suggested slot."""
    result = await service.recommend_slots(
        _tenant_of(current_user),
        payload,
        actor_id=current_user.id,
        request_id=getattr(request.state, "request_id", None),
    )
    return SuccessResponse[SlotRecommendationResponse](
        message="Slot recommendation completed.", data=result
    )


@router.get(
    "/{appointment_id}",
    response_model=SuccessResponse[AppointmentResponse],
    summary="Get an appointment",
    description="Return one appointment's full record.",
    responses={
        200: {"description": "Appointment returned."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def get_appointment(
    appointment_id: uuid.UUID = Path(description="Appointment UUID."),
    current_user: User = Depends(require_permission("appointment.read")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[AppointmentResponse]:
    """Retrieve one appointment by UUID."""
    appointment = await service.get_appointment(_tenant_of(current_user), appointment_id)
    return SuccessResponse[AppointmentResponse](message="Appointment retrieved.", data=appointment)


@router.patch(
    "/{appointment_id}",
    response_model=SuccessResponse[AppointmentResponse],
    summary="Reschedule an appointment",
    description=(
        "Move a booked appointment to a new window (module spec §5.3).\n\n"
        "Allowed only while the status is `booked`. Once a patient has checked "
        "in, moving the appointment is a cancel-and-rebook rather than an edit.\n\n"
        "The move is recorded in the status history as `booked → booked` so the "
        "original time is not silently overwritten."
    ),
    responses={
        200: {"description": "Appointment rescheduled."},
        400: {"description": "Only a booked appointment can be rescheduled."},
        409: {"description": "The new window clashes with another appointment."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def reschedule_appointment(
    payload: RescheduleAppointmentRequest,
    appointment_id: uuid.UUID = Path(description="Appointment UUID."),
    current_user: User = Depends(require_permission("appointment.reschedule")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[AppointmentResponse]:
    """Reschedule a booked appointment."""
    appointment = await service.reschedule_appointment(
        _tenant_of(current_user),
        appointment_id,
        payload,
        actor_id=current_user.id,
        allow_override=_has_permission(current_user, "appointment.book_override"),
    )
    return SuccessResponse[AppointmentResponse](
        message="Appointment rescheduled.", data=appointment
    )


# ── Lifecycle transitions ───────────────────────────────────────────────────


@router.post(
    "/{appointment_id}/check-in",
    response_model=SuccessResponse[AppointmentResponse],
    summary="Check a patient in",
    description="Record the patient's arrival (module spec §5.5). Requires status `booked`.",
    responses={200: {"description": "Patient checked in."}, **_TRANSITION_RESPONSES},
)
async def check_in(
    appointment_id: uuid.UUID = Path(description="Appointment UUID."),
    current_user: User = Depends(require_permission("appointment.check_in")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[AppointmentResponse]:
    """Move an appointment to ``checked_in``."""
    appointment = await service.check_in(
        _tenant_of(current_user), appointment_id, actor_id=current_user.id
    )
    return SuccessResponse[AppointmentResponse](message="Patient checked in.", data=appointment)


@router.post(
    "/{appointment_id}/start",
    response_model=SuccessResponse[AppointmentResponse],
    summary="Start a consultation",
    description="Begin the consultation (module spec §5.6). From `booked` or `checked_in`.",
    responses={200: {"description": "Consultation started."}, **_TRANSITION_RESPONSES},
)
async def start_appointment(
    appointment_id: uuid.UUID = Path(description="Appointment UUID."),
    current_user: User = Depends(require_permission("appointment.start")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[AppointmentResponse]:
    """Move an appointment to ``in_progress``."""
    appointment = await service.start(
        _tenant_of(current_user), appointment_id, actor_id=current_user.id
    )
    return SuccessResponse[AppointmentResponse](message="Consultation started.", data=appointment)


@router.post(
    "/{appointment_id}/complete",
    response_model=SuccessResponse[AppointmentResponse],
    summary="Complete a consultation",
    description=(
        "Finish the consultation (module spec §5.6).\n\n"
        "Completing from `checked_in` without a formal start is allowed at the "
        "doctor's discretion; the skipped state is visible in the status "
        "history.\n\n"
        "Triggers an invoice draft in Billing. That happens after the "
        "appointment is committed, so a billing failure cannot roll back a "
        "consultation that genuinely took place."
    ),
    responses={200: {"description": "Consultation completed."}, **_TRANSITION_RESPONSES},
)
async def complete_appointment(
    appointment_id: uuid.UUID = Path(description="Appointment UUID."),
    current_user: User = Depends(require_permission("appointment.complete")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[AppointmentResponse]:
    """Move an appointment to ``completed``."""
    appointment = await service.complete(
        _tenant_of(current_user), appointment_id, actor_id=current_user.id
    )
    return SuccessResponse[AppointmentResponse](message="Consultation completed.", data=appointment)


@router.post(
    "/{appointment_id}/cancel",
    response_model=SuccessResponse[AppointmentResponse],
    summary="Cancel an appointment",
    description=(
        "Cancel an appointment (module spec §5.4). Allowed from `booked` or "
        "`checked_in`, and a reason is required.\n\n"
        "Cancelling is terminal: a cancelled appointment is never reactivated, "
        "a new one is booked instead. The slot is released immediately."
    ),
    responses={200: {"description": "Appointment cancelled."}, **_TRANSITION_RESPONSES},
)
async def cancel_appointment(
    payload: CancelAppointmentRequest,
    appointment_id: uuid.UUID = Path(description="Appointment UUID."),
    current_user: User = Depends(require_permission("appointment.cancel")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[AppointmentResponse]:
    """Move an appointment to ``cancelled``."""
    appointment = await service.cancel(
        _tenant_of(current_user), appointment_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[AppointmentResponse](message="Appointment cancelled.", data=appointment)


@router.post(
    "/{appointment_id}/no-show",
    response_model=SuccessResponse[AppointmentResponse],
    summary="Mark a no-show",
    description=(
        "Mark an appointment as a no-show.\n\n"
        "The background sweeper does this automatically once the hospital's "
        "grace period has passed (module spec §5.7); this endpoint is the "
        "manual path for reception."
    ),
    responses={200: {"description": "Marked as no-show."}, **_TRANSITION_RESPONSES},
)
async def mark_no_show(
    appointment_id: uuid.UUID = Path(description="Appointment UUID."),
    current_user: User = Depends(require_permission("appointment.cancel")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[AppointmentResponse]:
    """Move an appointment to ``no_show``."""
    appointment = await service.mark_no_show(
        _tenant_of(current_user), appointment_id, actor_id=current_user.id
    )
    return SuccessResponse[AppointmentResponse](message="Marked as no-show.", data=appointment)


@router.get(
    "/{appointment_id}/status-history",
    response_model=SuccessResponse[list[StatusHistoryEntryResponse]],
    summary="Get status history",
    description=(
        "Return every recorded transition, oldest first (module spec §9, AC-6).\n\n"
        "Immutable and append-only. `changed_by` is null when the system acted "
        "— the no-show sweeper has no acting user."
    ),
    responses={
        200: {"description": "History returned."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def get_status_history(
    appointment_id: uuid.UUID = Path(description="Appointment UUID."),
    current_user: User = Depends(require_permission("appointment.read")),
    service: AppointmentService = Depends(get_appointment_service),
) -> SuccessResponse[list[StatusHistoryEntryResponse]]:
    """Return an appointment's transition history."""
    history = await service.get_status_history(_tenant_of(current_user), appointment_id)
    return SuccessResponse[list[StatusHistoryEntryResponse]](
        message="Status history retrieved.", data=history
    )
