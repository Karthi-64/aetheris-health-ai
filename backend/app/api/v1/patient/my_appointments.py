"""Patient App routes for the patient's own appointments.

See ``docs/modules/15-patient-app.md`` §14.

No route names a patient or a hospital: whose appointments these are is the
authenticated account's own record links, resolved by the service.
``appointment_ref`` is deliberately not validated here — a malformed,
unknown, or somebody else's reference is one and the same ``404``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies.patient import (
    get_client_context,
    get_patient_account,
    get_patient_appointments_service,
)
from app.core.envelope import paginated_envelope, success_envelope
from app.models.patient_account import PatientAccount
from app.schemas.common import PaginatedResponse, SuccessResponse
from app.schemas.patient_app.appointments import (
    AppointmentScope,
    CancelAppointment,
    PatientAppointmentDetail,
)
from app.schemas.patient_app.hospitals import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE
from app.services.patient_app.appointments_service import PatientAppointmentsService
from app.services.patient_app.common import ClientContext

router = APIRouter(tags=["Patient App — My appointments"])

_COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Authentication required."},
    403: {"description": "A required policy has not been accepted (CONSENT_REQUIRED)."},
}
AppointmentRef = Annotated[str, Path(description="The appointment's reference.")]


@router.get(
    "/appointments",
    response_model=PaginatedResponse[PatientAppointmentDetail],
    summary="My appointments",
    description=(
        "The signed-in patient's own appointments, at every hospital where their record "
        "link is honoured. `scope=upcoming` (the default) lists those still ahead, soonest "
        "first; `scope=past` lists the rest — ended, completed, cancelled or missed — most "
        "recent first."
    ),
    responses={200: {"description": "Page of appointments returned."}, **_COMMON_RESPONSES},
)
async def list_my_appointments(
    scope: AppointmentScope = Query(AppointmentScope.UPCOMING, description="upcoming or past."),
    page: int = Query(1, ge=1, le=MAX_PAGE, description="1-based page number."),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Appointments per page."
    ),
    account: PatientAccount = Depends(get_patient_account),
    appointments: PatientAppointmentsService = Depends(get_patient_appointments_service),
) -> dict[str, Any]:
    """List the caller's own appointments."""
    result = await appointments.list_appointments(
        account, upcoming=scope is AppointmentScope.UPCOMING, page=page, page_size=page_size
    )
    return paginated_envelope(
        "Appointments retrieved.",
        data=[item.model_dump(mode="json") for item in result.items],
        page=result.page,
        page_size=result.page_size,
        total_records=result.total_records,
    )


@router.get(
    "/appointments/{appointment_ref}",
    response_model=SuccessResponse[PatientAppointmentDetail],
    summary="One of my appointments",
    description="One appointment of the signed-in patient. Anybody else's is the same 404.",
    responses={
        200: {"description": "Appointment returned."},
        404: {"description": "No such appointment."},
        **_COMMON_RESPONSES,
    },
)
async def get_my_appointment(
    appointment_ref: AppointmentRef,
    account: PatientAccount = Depends(get_patient_account),
    appointments: PatientAppointmentsService = Depends(get_patient_appointments_service),
) -> dict[str, Any]:
    """Read one of the caller's own appointments."""
    result = await appointments.get_appointment(account, appointment_ref)
    return success_envelope("Appointment retrieved.", data=result.model_dump(mode="json"))


@router.post(
    "/appointments/{appointment_ref}/cancel",
    response_model=SuccessResponse[PatientAppointmentDetail],
    summary="Cancel one of my appointments",
    description=(
        "Cancel a booked appointment of the signed-in patient, before the hospital's cut-off. "
        "Repeating the request for an appointment that is already cancelled changes nothing "
        "and returns it as it is."
    ),
    responses={
        200: {"description": "The appointment, now cancelled."},
        400: {"description": "The hospital's cut-off for self-cancellation has passed."},
        404: {"description": "No such appointment."},
        409: {"description": "The appointment is no longer booked."},
        422: {"description": "A malformed body."},
        **_COMMON_RESPONSES,
    },
)
async def cancel_my_appointment(
    payload: CancelAppointment,
    appointment_ref: AppointmentRef,
    account: PatientAccount = Depends(get_patient_account),
    client: ClientContext = Depends(get_client_context),
    appointments: PatientAppointmentsService = Depends(get_patient_appointments_service),
) -> dict[str, Any]:
    """Cancel one of the caller's own appointments."""
    result = await appointments.cancel_appointment(account, appointment_ref, payload, client=client)
    return success_envelope("Appointment cancelled.", data=result.model_dump(mode="json"))
