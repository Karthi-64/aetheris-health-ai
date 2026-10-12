"""Patient App hospital discovery routes — find a hospital, and read about one.

See ``docs/modules/15-patient-app.md`` §11 and §27.6.

``hospital_ref`` is the hospital's code (its slug); its id is accepted too, as
the record-link routes accept it. It is deliberately not length-checked here:
a reference that is too long, malformed or simply unknown gets the same
``404``, not a ``422`` of its own.

The city options live at a sibling path, ``/hospital-cities``, because
``/hospitals/cities`` would be the hospital whose code is "cities".
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies.patient import get_hospital_directory_service, get_patient_account
from app.core.envelope import paginated_envelope, success_envelope
from app.models.patient_account import PatientAccount
from app.schemas.common import PaginatedResponse, SuccessResponse
from app.schemas.patient_app.hospitals import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE,
    MAX_PAGE_SIZE,
    HospitalFilterText,
    PatientHospital,
    PatientHospitalCities,
)
from app.services.patient_app.hospital_directory_service import HospitalDirectoryService

router = APIRouter(tags=["Patient App — Hospital discovery"])

_COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Authentication required."},
    403: {"description": "A required policy has not been accepted (CONSENT_REQUIRED)."},
}


@router.get(
    "/hospitals",
    response_model=PaginatedResponse[PatientHospital],
    summary="Discover hospitals",
    description=(
        "A page of the hospitals that can be used with the Patient App, in name order. "
        "`search` matches any part of the name, without regard to case and literally "
        "(`%` and `_` are not wildcards). `city` matches the whole city name, without "
        "regard to case. Any other query parameter is ignored."
    ),
    responses={200: {"description": "Page of hospitals returned."}, **_COMMON_RESPONSES},
)
async def discover_hospitals(
    search: Annotated[
        HospitalFilterText | None, Query(description="Text the hospital's name contains.")
    ] = None,
    city: Annotated[
        HospitalFilterText | None, Query(description="The hospital's city, in full.")
    ] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE, description="1-based page number."),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Hospitals per page."
    ),
    account: PatientAccount = Depends(get_patient_account),
    directory: HospitalDirectoryService = Depends(get_hospital_directory_service),
) -> dict[str, Any]:
    """List or search the hospitals open to patients."""
    result = await directory.discover(
        account, search=search, city=city, page=page, page_size=page_size
    )
    return paginated_envelope(
        "Hospitals retrieved.",
        data=[hospital.model_dump(mode="json") for hospital in result.items],
        page=result.page,
        page_size=result.page_size,
        total_records=result.total_records,
    )


@router.get(
    "/hospital-cities",
    response_model=SuccessResponse[PatientHospitalCities],
    summary="Cities to filter hospitals by",
    description="The cities that have a hospital in the directory, in alphabetical order.",
    responses={200: {"description": "Cities returned."}, **_COMMON_RESPONSES},
)
async def list_hospital_cities(
    account: PatientAccount = Depends(get_patient_account),
    directory: HospitalDirectoryService = Depends(get_hospital_directory_service),
) -> dict[str, Any]:
    """List the options of the city filter."""
    result = await directory.list_cities(account)
    return success_envelope("Cities retrieved.", data=result.model_dump(mode="json"))


@router.get(
    "/hospitals/{hospital_ref}",
    response_model=SuccessResponse[PatientHospital],
    summary="One hospital",
    description=(
        "One hospital that can be used with the Patient App. A hospital that is unknown, "
        "or cannot be used, is the same 404."
    ),
    responses={
        200: {"description": "Hospital returned."},
        404: {"description": "No such hospital."},
        **_COMMON_RESPONSES,
    },
)
async def get_hospital(
    hospital_ref: Annotated[str, Path(description="The hospital's code.")],
    account: PatientAccount = Depends(get_patient_account),
    directory: HospitalDirectoryService = Depends(get_hospital_directory_service),
) -> dict[str, Any]:
    """Describe one hospital open to patients."""
    result = await directory.get_hospital(account, hospital_ref)
    return success_envelope("Hospital retrieved.", data=result.model_dump(mode="json"))
