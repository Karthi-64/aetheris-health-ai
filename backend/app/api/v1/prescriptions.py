"""Prescription and dispensing API routes.

Implements the dispensing part of ``docs/modules/08-pharmacy.md`` §9. Routes
parse input, delegate to
:class:`~app.services.dispensing_service.DispensingService`, and wrap the
result in the standard envelope. No business logic, no database access.

**Tenancy.** ``hospital_id`` always comes from the authenticated user.

**Not in the spec's list.** ``POST /prescriptions``, ``GET /prescriptions``,
``GET /prescriptions/{id}``, the cancel route and the dispense history exist
because there is no Consultation module to write or show a prescription.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Path, Query, status

from app.api.dependencies.auth import require_permission
from app.api.dependencies.services import get_dispensing_service
from app.core.exceptions import BusinessRuleError
from app.models.pharmacy import PrescriptionStatus
from app.models.user import User
from app.schemas.common import (
    MetadataWithPagination,
    Page,
    PaginatedResponse,
    PaginationMeta,
    PaginationParams,
    SuccessResponse,
)
from app.schemas.pharmacy import (
    CancelPrescriptionRequest,
    CreatePrescriptionRequest,
    DispenseRequest,
    DispenseResponse,
    PrescriptionResponse,
)
from app.services.dispensing_service import DispensingService

router = APIRouter(prefix="/prescriptions", tags=["Pharmacy — Prescriptions and dispensing"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    422: {"description": "Request failed validation."},
}


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the request acts within.

    :param current_user: The authenticated user.
    :returns: The hospital UUID to scope every query by.
    :raises BusinessRuleError: If the user belongs to no hospital.
    """
    if current_user.hospital_id is None:
        msg = "This account is not scoped to a hospital, so the pharmacy cannot be accessed."
        raise BusinessRuleError(msg)
    return current_user.hospital_id


def _pagination(page: Page[Any]) -> MetadataWithPagination:
    """Build the pagination block of a list response."""
    return MetadataWithPagination(
        pagination=PaginationMeta(
            page=page.page,
            page_size=page.page_size,
            total_records=page.total_records,
            total_pages=page.total_pages,
        ),
    )


_NOT_FOUND: dict[int | str, dict[str, str]] = {
    404: {"description": "Prescription not found in this hospital."},
}

_READ = "pharmacy.prescription.read"
_WRITE = "pharmacy.prescription.create"


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[PrescriptionResponse],
    summary="Write a prescription",
    description=(
        "Prescribe medicines for the patient of a visit. The patient and the "
        "prescribing doctor are taken from the appointment.\n\n"
        "Each line is either a catalog `medicine_id` or a free-text "
        "`medicine_name` for something the pharmacy does not stock. A "
        "free-text line is recorded but cannot be dispensed here."
    ),
    responses={201: {"description": "Prescription written."}, **_COMMON_RESPONSES},
)
async def create_prescription(
    payload: CreatePrescriptionRequest,
    current_user: User = Depends(require_permission(_WRITE)),
    service: DispensingService = Depends(get_dispensing_service),
) -> SuccessResponse[PrescriptionResponse]:
    """Write a prescription."""
    created = await service.create_prescription(
        _tenant_of(current_user), payload, actor_id=current_user.id
    )
    return SuccessResponse[PrescriptionResponse](message="Prescription written.", data=created)


@router.get(
    "/pending",
    response_model=PaginatedResponse[PrescriptionResponse],
    summary="List prescriptions waiting to be dispensed",
    description=(
        "The dispensing queue: prescriptions with something left to dispense, "
        "longest-waiting first. Each line carries `available_quantity`, the "
        "units in stock that can be dispensed today."
    ),
    responses={200: {"description": "Queue returned."}, **_COMMON_RESPONSES},
)
async def list_pending(
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission(_READ)),
    service: DispensingService = Depends(get_dispensing_service),
) -> PaginatedResponse[PrescriptionResponse]:
    """List the dispensing queue (module spec §9)."""
    result = await service.list_prescriptions(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        pending_only=True,
    )
    return PaginatedResponse[PrescriptionResponse](
        message="Pending prescriptions retrieved.", data=result.items, metadata=_pagination(result)
    )


@router.get(
    "",
    response_model=PaginatedResponse[PrescriptionResponse],
    summary="List prescriptions",
    description="Return a page of prescriptions, newest first.",
    responses={200: {"description": "Page of prescriptions returned."}, **_COMMON_RESPONSES},
)
async def list_prescriptions(
    prescription_status: PrescriptionStatus | None = Query(
        None, alias="status", description="Only prescriptions in this status."
    ),
    patient_id: uuid.UUID | None = Query(None, description="Only this patient's."),
    doctor_id: uuid.UUID | None = Query(None, description="Only those by this doctor."),
    appointment_id: uuid.UUID | None = Query(None, description="Only those from this visit."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission(_READ)),
    service: DispensingService = Depends(get_dispensing_service),
) -> PaginatedResponse[PrescriptionResponse]:
    """List prescriptions."""
    result = await service.list_prescriptions(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        status=prescription_status,
        patient_id=patient_id,
        doctor_id=doctor_id,
        appointment_id=appointment_id,
    )
    return PaginatedResponse[PrescriptionResponse](
        message="Prescriptions retrieved.", data=result.items, metadata=_pagination(result)
    )


@router.get(
    "/{prescription_id}",
    response_model=SuccessResponse[PrescriptionResponse],
    summary="Get a prescription",
    description="Return one prescription, with what is in stock for each line.",
    responses={200: {"description": "Prescription returned."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def get_prescription(
    prescription_id: uuid.UUID = Path(description="Prescription UUID."),
    current_user: User = Depends(require_permission(_READ)),
    service: DispensingService = Depends(get_dispensing_service),
) -> SuccessResponse[PrescriptionResponse]:
    """Retrieve one prescription."""
    prescription = await service.get_prescription(_tenant_of(current_user), prescription_id)
    return SuccessResponse[PrescriptionResponse](
        message="Prescription retrieved.", data=prescription
    )


@router.post(
    "/{prescription_id}/cancel",
    response_model=SuccessResponse[PrescriptionResponse],
    summary="Cancel a prescription",
    description="Cancel a prescription nothing has been dispensed from, with a reason.",
    responses={
        200: {"description": "Prescription cancelled."},
        400: {"description": "Some of it has been dispensed, or it is already cancelled."},
        **_NOT_FOUND,
        **_COMMON_RESPONSES,
    },
)
async def cancel_prescription(
    payload: CancelPrescriptionRequest,
    prescription_id: uuid.UUID = Path(description="Prescription UUID."),
    current_user: User = Depends(require_permission(_WRITE)),
    service: DispensingService = Depends(get_dispensing_service),
) -> SuccessResponse[PrescriptionResponse]:
    """Cancel a prescription."""
    cancelled = await service.cancel_prescription(
        _tenant_of(current_user), prescription_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[PrescriptionResponse](message="Prescription cancelled.", data=cancelled)


@router.post(
    "/{prescription_id}/dispense",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[DispenseResponse],
    summary="Dispense a prescription",
    description=(
        "Dispense a prescription. Send no body to dispense everything "
        "outstanding, or name lines and quantities for a partial dispense, "
        "which needs `notes` saying why.\n\n"
        "The server takes stock first-expiry-first, never from an expired or "
        "recalled batch. **It is all or nothing:** if any medicine is short "
        "the response is `409`, listing the shortages, and nothing is "
        "dispensed. Each dispense is charged to a draft invoice."
    ),
    responses={
        201: {"description": "Dispensed."},
        400: {"description": "The prescription cannot be dispensed in its current status."},
        409: {"description": "Not enough stock. Nothing was dispensed."},
        **_NOT_FOUND,
        **_COMMON_RESPONSES,
    },
)
async def dispense(
    prescription_id: uuid.UUID = Path(description="Prescription UUID."),
    payload: DispenseRequest | None = Body(default=None),
    current_user: User = Depends(require_permission("pharmacy.dispense.execute")),
    service: DispensingService = Depends(get_dispensing_service),
) -> SuccessResponse[DispenseResponse]:
    """Dispense a prescription (module spec §5.1)."""
    result = await service.dispense(
        _tenant_of(current_user), prescription_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[DispenseResponse](message="Dispensed.", data=result)


@router.get(
    "/{prescription_id}/dispenses",
    response_model=SuccessResponse[list[DispenseResponse]],
    summary="List a prescription's dispenses",
    description="Return every dispense made against a prescription, oldest first.",
    responses={200: {"description": "Dispenses returned."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def list_dispenses(
    prescription_id: uuid.UUID = Path(description="Prescription UUID."),
    current_user: User = Depends(require_permission(_READ)),
    service: DispensingService = Depends(get_dispensing_service),
) -> SuccessResponse[list[DispenseResponse]]:
    """List a prescription's dispenses."""
    dispenses = await service.list_dispenses(_tenant_of(current_user), prescription_id)
    return SuccessResponse[list[DispenseResponse]](message="Dispenses retrieved.", data=dispenses)
