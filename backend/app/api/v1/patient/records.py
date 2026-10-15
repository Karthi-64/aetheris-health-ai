"""Patient App record routes — own prescriptions and released lab results.

``docs/modules/15-patient-app.md`` §15, §16 and §27.8. Read-only. No route
names a patient: whose records these are is the authenticated account's own
record links, resolved by the service. A malformed, unknown, unreleased or
somebody else's reference is one and the same ``404``, and no list shows
anything but ``released`` lab orders.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies.patient import get_patient_account, get_records_service
from app.core.envelope import paginated_envelope, success_envelope
from app.models.patient_account import PatientAccount
from app.models.pharmacy import PrescriptionStatus
from app.schemas.common import PaginatedResponse, SuccessResponse
from app.schemas.patient_app.records import (
    PatientLabResult,
    PatientPrescription,
)
from app.services.patient_app.records_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE,
    MAX_PAGE_SIZE,
    PatientRecordsService,
)

router = APIRouter(tags=["Patient App — My records"])

HospitalRef = Annotated[
    str, Path(min_length=1, max_length=100, description="The hospital's id or its code.")
]
RecordRef = Annotated[str, Path(description="The record's reference.")]

#: Reading is not audited (§27.8: no audit column); nothing mutates here.
_READ_RESPONSES: Final[dict[int | str, dict[str, str]]] = {
    200: {"description": "Page returned."},
    401: {"description": "Authentication required."},
    403: {"description": "A required policy has not been accepted (CONSENT_REQUIRED)."},
}
_ONE_RESPONSES: Final[dict[int | str, dict[str, str]]] = {
    200: {"description": "Record returned."},
    401: {"description": "Authentication required."},
    403: {"description": "A required policy has not been accepted (CONSENT_REQUIRED)."},
    404: {"description": "No such record of yours exists here."},
}


@router.get(
    "/prescriptions",
    response_model=PaginatedResponse[PatientPrescription],
    summary="My prescriptions",
    description=(
        "The signed-in patient's own prescriptions, at every hospital where "
        "their record link is honoured, newest first. All statuses are shown, "
        "labelled. This is a record, not an instruction to self-medicate: "
        "follow the doctor's advice."
    ),
    responses={**_READ_RESPONSES},
)
async def list_my_prescriptions(
    prescription_status: PrescriptionStatus | None = Query(
        None, alias="status", description="Only prescriptions in this status."
    ),
    page: int = Query(1, ge=1, le=MAX_PAGE, description="1-based page number."),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Prescriptions per page."
    ),
    account: PatientAccount = Depends(get_patient_account),
    records: PatientRecordsService = Depends(get_records_service),
) -> dict[str, Any]:
    """List the caller's own prescriptions."""
    result = await records.list_prescriptions(
        account, status=prescription_status, page=page, page_size=page_size
    )
    return paginated_envelope(
        "Prescriptions retrieved.",
        data=[item.model_dump(mode="json") for item in result.items],
        page=result.page,
        page_size=result.page_size,
        total_records=result.total_records,
    )


@router.get(
    "/hospitals/{hospital_ref}/prescriptions/{prescription_ref}",
    response_model=SuccessResponse[PatientPrescription],
    summary="One of my prescriptions",
    description=(
        "One prescription of the signed-in patient. Anything else — unknown, "
        "somebody else's, a hospital their link does not reach — is the same 404."
    ),
    responses={**_ONE_RESPONSES},
)
async def get_my_prescription(
    hospital_ref: HospitalRef,
    prescription_ref: RecordRef,
    account: PatientAccount = Depends(get_patient_account),
    records: PatientRecordsService = Depends(get_records_service),
) -> dict[str, Any]:
    """Read one of the caller's own prescriptions."""
    result = await records.get_prescription(account, hospital_ref, prescription_ref)
    return success_envelope("Prescription retrieved.", data=result.model_dump(mode="json"))


@router.get(
    "/lab-results",
    response_model=PaginatedResponse[PatientLabResult],
    summary="My released lab results",
    description=(
        "The signed-in patient's own released lab results, newest first. Only "
        "released orders exist here: nothing pending, entered or cancelled is "
        "returned, and results carry no interpretation."
    ),
    responses={**_READ_RESPONSES},
)
async def list_my_lab_results(
    page: int = Query(1, ge=1, le=MAX_PAGE, description="1-based page number."),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Orders per page."
    ),
    account: PatientAccount = Depends(get_patient_account),
    records: PatientRecordsService = Depends(get_records_service),
) -> dict[str, Any]:
    """List the caller's own released lab results."""
    result = await records.list_lab_results(account, page=page, page_size=page_size)
    return paginated_envelope(
        "Lab results retrieved.",
        data=[item.model_dump(mode="json") for item in result.items],
        page=result.page,
        page_size=result.page_size,
        total_records=result.total_records,
    )


@router.get(
    "/hospitals/{hospital_ref}/lab-results/{order_ref}",
    response_model=SuccessResponse[PatientLabResult],
    summary="One released lab result",
    description=(
        "One released lab order of the signed-in patient. An order that is "
        "not released, is cancelled, or is somebody else's, is the same 404."
    ),
    responses={**_ONE_RESPONSES},
)
async def get_my_lab_result(
    hospital_ref: HospitalRef,
    order_ref: RecordRef,
    account: PatientAccount = Depends(get_patient_account),
    records: PatientRecordsService = Depends(get_records_service),
) -> dict[str, Any]:
    """Read one of the caller's own released lab orders."""
    result = await records.get_lab_result(account, hospital_ref, order_ref)
    return success_envelope("Lab result retrieved.", data=result.model_dump(mode="json"))
