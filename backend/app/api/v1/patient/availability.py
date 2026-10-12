"""Patient App doctor availability route — when a doctor can be booked.

See ``docs/modules/15-patient-app.md`` §9.1 and §13.

``hospital_ref`` and ``doctor_ref`` are read exactly as doctor discovery reads
them. The dates are validated here only for their form; what the policy
allows — today, the horizon, the length of a range — is decided by the service.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies.patient import get_doctor_availability_service, get_patient_account
from app.core.envelope import success_envelope
from app.models.patient_account import PatientAccount
from app.schemas.common import SuccessResponse
from app.schemas.patient_app.availability import (
    MAX_RANGE_DAYS,
    IsoDate,
    PatientDoctorAvailability,
)
from app.services.patient_app.availability_service import DoctorAvailabilityService

router = APIRouter(tags=["Patient App — Doctor availability"])


@router.get(
    "/hospitals/{hospital_ref}/doctors/{doctor_ref}/availability",
    response_model=SuccessResponse[PatientDoctorAvailability],
    summary="When a doctor can be booked",
    description=(
        "The doctor's bookable slots for a range of dates at the hospital, in the hospital's "
        f"timezone. The range defaults to the next week and may cover at most {MAX_RANGE_DAYS} "
        "days, between today and the hospital's booking horizon. Only slots a patient could "
        "book right now are returned; seeing one does not reserve it."
    ),
    responses={
        200: {"description": "Availability returned."},
        400: {"description": "A date lies outside the bookable window (BUSINESS_RULE_VIOLATION)."},
        401: {"description": "Authentication required."},
        403: {"description": "A required policy has not been accepted (CONSENT_REQUIRED)."},
        404: {"description": "No such hospital, or no such doctor at it."},
        422: {"description": "A malformed date, a backwards range, or one that is too long."},
    },
)
async def get_doctor_availability(
    hospital_ref: Annotated[str, Path(description="The hospital's code.")],
    doctor_ref: Annotated[str, Path(description="The doctor's reference.")],
    start_date: Annotated[
        IsoDate | None,
        Query(description="First date, YYYY-MM-DD. Today at the hospital if absent."),
    ] = None,
    end_date: Annotated[
        IsoDate | None, Query(description="Last date, YYYY-MM-DD. A week from the first if absent.")
    ] = None,
    account: PatientAccount = Depends(get_patient_account),
    availability: DoctorAvailabilityService = Depends(get_doctor_availability_service),
) -> dict[str, Any]:
    """Read a doctor's bookable slots."""
    result = await availability.get_availability(
        account, hospital_ref, doctor_ref, start_date=start_date, end_date=end_date
    )
    return success_envelope("Availability retrieved.", data=result.model_dump(mode="json"))
