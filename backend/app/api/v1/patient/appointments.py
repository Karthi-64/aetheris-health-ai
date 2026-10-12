"""Patient App appointment booking route.

See ``docs/modules/15-patient-app.md`` §13.

The body names a slot. The patient is the authenticated account, the hospital
and the doctor are the path's — resolved by the service exactly as discovery
resolves them — and no field exists to say otherwise.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Response, status

from app.api.dependencies.patient import (
    get_client_context,
    get_patient_account,
    get_patient_booking_service,
)
from app.core.envelope import success_envelope
from app.models.patient_account import PatientAccount
from app.schemas.common import SuccessResponse
from app.schemas.patient_app.appointments import (
    IDEMPOTENCY_KEY_PATTERN,
    BookAppointment,
    PatientAppointment,
)
from app.services.patient_app.booking_service import PatientBookingService
from app.services.patient_app.common import ClientContext

router = APIRouter(tags=["Patient App — Appointments"])


@router.post(
    "/hospitals/{hospital_ref}/doctors/{doctor_ref}/appointments",
    response_model=SuccessResponse[PatientAppointment],
    status_code=status.HTTP_201_CREATED,
    summary="Book an appointment",
    description=(
        "Book one slot of a doctor for the signed-in patient's own record at the hospital. "
        "The slot must be one the availability endpoint offers; it is checked again against "
        "current data before anything is written. `Idempotency-Key` is required: repeating a "
        "request with the same key returns the same appointment (200) instead of booking twice."
    ),
    responses={
        200: {"description": "The same request was already booked; that appointment is returned."},
        201: {"description": "Appointment booked."},
        400: {"description": "The time cannot be booked, or the patient's own limits forbid it."},
        401: {"description": "Authentication required."},
        403: {"description": "CONSENT_REQUIRED, or RECORD_LINK_REQUIRED at this hospital."},
        404: {"description": "No such hospital, or no such doctor at it."},
        409: {"description": "The time is no longer available, or the key was used before."},
        422: {"description": "A malformed body or Idempotency-Key."},
    },
)
async def book_appointment(
    payload: BookAppointment,
    response: Response,
    hospital_ref: Annotated[str, Path(description="The hospital's code.")],
    doctor_ref: Annotated[str, Path(description="The doctor's reference.")],
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            pattern=IDEMPOTENCY_KEY_PATTERN,
            description="A client-chosen key, 16–64 URL-safe characters, unique per booking.",
        ),
    ],
    account: PatientAccount = Depends(get_patient_account),
    client: ClientContext = Depends(get_client_context),
    booking: PatientBookingService = Depends(get_patient_booking_service),
) -> dict[str, Any]:
    """Book a slot for the authenticated patient."""
    outcome = await booking.book(
        account, hospital_ref, doctor_ref, payload, idempotency_key=idempotency_key, client=client
    )
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return success_envelope(
        "Appointment booked." if outcome.created else "Appointment already booked.",
        data=outcome.appointment.model_dump(mode="json"),
    )
