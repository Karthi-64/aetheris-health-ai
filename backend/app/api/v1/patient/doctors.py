"""Patient App doctor discovery routes — the doctors of one hospital.

See ``docs/modules/15-patient-app.md`` §9.1 and §12.

``hospital_ref`` is read exactly as hospital discovery reads it. ``doctor_ref``
is deliberately not validated here: a reference that is malformed, unknown,
hidden or another hospital's gets the same ``404``, not a ``422`` of its own.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies.patient import get_doctor_directory_service, get_patient_account
from app.core.envelope import paginated_envelope, success_envelope
from app.models.patient_account import PatientAccount
from app.schemas.common import PaginatedResponse, SuccessResponse
from app.schemas.patient_app.doctors import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE,
    MAX_PAGE_SIZE,
    PatientDepartments,
    PatientDoctor,
)
from app.schemas.patient_app.hospitals import HospitalFilterText
from app.services.patient_app.doctor_directory_service import HospitalDoctorDirectoryService

router = APIRouter(tags=["Patient App — Doctor discovery"])

_COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Authentication required."},
    403: {"description": "A required policy has not been accepted (CONSENT_REQUIRED)."},
    404: {"description": "No such hospital, or no such doctor at it."},
}

HospitalRef = Annotated[str, Path(description="The hospital's code.")]


@router.get(
    "/hospitals/{hospital_ref}/doctors",
    response_model=PaginatedResponse[PatientDoctor],
    summary="Discover a hospital's doctors",
    description=(
        "A page of the hospital's doctors, in name order. `search` matches any part of the "
        "name or the specialization, without regard to case and literally (`%` and `_` are "
        "not wildcards). `department` is a department reference from the departments "
        "endpoint. Any other query parameter is ignored."
    ),
    responses={200: {"description": "Page of doctors returned."}, **_COMMON_RESPONSES},
)
async def discover_doctors(
    hospital_ref: HospitalRef,
    search: Annotated[
        HospitalFilterText | None,
        Query(description="Text the doctor's name or specialization contains."),
    ] = None,
    department: Annotated[
        uuid.UUID | None, Query(description="A department reference to filter by.")
    ] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE, description="1-based page number."),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Doctors per page."
    ),
    account: PatientAccount = Depends(get_patient_account),
    directory: HospitalDoctorDirectoryService = Depends(get_doctor_directory_service),
) -> dict[str, Any]:
    """List or search the doctors of a hospital open to patients."""
    result = await directory.discover(
        account, hospital_ref, search=search, department=department, page=page, page_size=page_size
    )
    return paginated_envelope(
        "Doctors retrieved.",
        data=[doctor.model_dump(mode="json") for doctor in result.items],
        page=result.page,
        page_size=result.page_size,
        total_records=result.total_records,
    )


@router.get(
    "/hospitals/{hospital_ref}/departments",
    response_model=SuccessResponse[PatientDepartments],
    summary="Departments to filter a hospital's doctors by",
    description="The departments of the hospital that have a listed doctor, by name.",
    responses={200: {"description": "Departments returned."}, **_COMMON_RESPONSES},
)
async def list_departments(
    hospital_ref: HospitalRef,
    account: PatientAccount = Depends(get_patient_account),
    directory: HospitalDoctorDirectoryService = Depends(get_doctor_directory_service),
) -> dict[str, Any]:
    """List the options of the department filter."""
    result = await directory.list_departments(account, hospital_ref)
    return success_envelope("Departments retrieved.", data=result.model_dump(mode="json"))


@router.get(
    "/hospitals/{hospital_ref}/doctors/{doctor_ref}",
    response_model=SuccessResponse[PatientDoctor],
    summary="One doctor",
    description=(
        "One doctor of the hospital. A doctor that is unknown, cannot be shown, or belongs "
        "to another hospital is the same 404."
    ),
    responses={200: {"description": "Doctor returned."}, **_COMMON_RESPONSES},
)
async def get_doctor(
    hospital_ref: HospitalRef,
    doctor_ref: Annotated[str, Path(description="The doctor's reference.")],
    account: PatientAccount = Depends(get_patient_account),
    directory: HospitalDoctorDirectoryService = Depends(get_doctor_directory_service),
) -> dict[str, Any]:
    """Describe one doctor of a hospital open to patients."""
    result = await directory.get_doctor(account, hospital_ref, doctor_ref)
    return success_envelope("Doctor retrieved.", data=result.model_dump(mode="json"))
