"""Medicine catalog, batch and stock API routes.

Implements the catalog and stock part of ``docs/modules/08-pharmacy.md`` §9.
Routes parse input, delegate to
:class:`~app.services.pharmacy_catalog_service.PharmacyCatalogService`, and
wrap the result in the standard envelope. No business logic, no database
access.

**Tenancy.** ``hospital_id`` always comes from the authenticated user.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.dependencies.auth import require_permission
from app.api.dependencies.services import get_pharmacy_catalog_service
from app.core.exceptions import BusinessRuleError
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
    AdjustStockRequest,
    BatchResponse,
    CreateMedicineRequest,
    MedicineResponse,
    MedicineStockResponse,
    ReceiveBatchRequest,
    UpdateBatchRequest,
    UpdateMedicineRequest,
)
from app.services.pharmacy_catalog_service import PharmacyCatalogService

router = APIRouter(prefix="/medicines", tags=["Pharmacy — Medicines and stock"])

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
    404: {"description": "Medicine or batch not found in this hospital."},
}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[MedicineResponse],
    summary="Add a medicine to the catalog",
    description=(
        "Create a medicine in the caller's hospital. `sku` is uppercased and "
        "must be unique within the hospital. `unit_price` is the selling price "
        "per unit, as a decimal string."
    ),
    responses={
        201: {"description": "Medicine created."},
        409: {"description": "A medicine with this SKU already exists."},
        **_COMMON_RESPONSES,
    },
)
async def create_medicine(
    payload: CreateMedicineRequest,
    current_user: User = Depends(require_permission("pharmacy.medicine.create")),
    service: PharmacyCatalogService = Depends(get_pharmacy_catalog_service),
) -> SuccessResponse[MedicineResponse]:
    """Create a medicine (module spec §9)."""
    created = await service.create_medicine(
        _tenant_of(current_user), payload, actor_id=current_user.id
    )
    return SuccessResponse[MedicineResponse](message="Medicine created.", data=created)


@router.get(
    "",
    response_model=PaginatedResponse[MedicineResponse],
    summary="List medicines",
    description=(
        "Return a page of medicines, ordered by name. `q` matches a prefix of "
        "the name or generic name case-insensitively, or an exact SKU."
    ),
    responses={200: {"description": "Page of medicines returned."}, **_COMMON_RESPONSES},
)
async def list_medicines(
    q: str | None = Query(None, max_length=200, description="Name prefix or exact SKU."),
    is_active: bool | None = Query(None, description="Filter on the active flag."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("pharmacy.medicine.read")),
    service: PharmacyCatalogService = Depends(get_pharmacy_catalog_service),
) -> PaginatedResponse[MedicineResponse]:
    """List medicines (module spec §9)."""
    result = await service.list_medicines(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        term=q,
        is_active=is_active,
    )
    return PaginatedResponse[MedicineResponse](
        message="Medicines retrieved.", data=result.items, metadata=_pagination(result)
    )


@router.get(
    "/{medicine_id}",
    response_model=SuccessResponse[MedicineResponse],
    summary="Get a medicine",
    description="Return one medicine.",
    responses={200: {"description": "Medicine returned."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def get_medicine(
    medicine_id: uuid.UUID = Path(description="Medicine UUID."),
    current_user: User = Depends(require_permission("pharmacy.medicine.read")),
    service: PharmacyCatalogService = Depends(get_pharmacy_catalog_service),
) -> SuccessResponse[MedicineResponse]:
    """Retrieve one medicine."""
    medicine = await service.get_medicine(_tenant_of(current_user), medicine_id)
    return SuccessResponse[MedicineResponse](message="Medicine retrieved.", data=medicine)


@router.patch(
    "/{medicine_id}",
    response_model=SuccessResponse[MedicineResponse],
    summary="Update a medicine",
    description=(
        "Apply a partial update. `sku` cannot be changed. Retire a medicine "
        "with `is_active: false`. A new price applies to future dispenses only."
    ),
    responses={200: {"description": "Medicine updated."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def update_medicine(
    payload: UpdateMedicineRequest,
    medicine_id: uuid.UUID = Path(description="Medicine UUID."),
    current_user: User = Depends(require_permission("pharmacy.medicine.update")),
    service: PharmacyCatalogService = Depends(get_pharmacy_catalog_service),
) -> SuccessResponse[MedicineResponse]:
    """Update a medicine."""
    updated = await service.update_medicine(
        _tenant_of(current_user), medicine_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[MedicineResponse](message="Medicine updated.", data=updated)


@router.get(
    "/{medicine_id}/stock",
    response_model=SuccessResponse[MedicineStockResponse],
    summary="Get a medicine's stock position",
    description=(
        "Return how much of a medicine is held, how much of that can be "
        "dispensed today, and how much is expiring soon, expired or recalled, "
        "with every batch."
    ),
    responses={200: {"description": "Stock returned."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def get_stock(
    medicine_id: uuid.UUID = Path(description="Medicine UUID."),
    current_user: User = Depends(require_permission("pharmacy.batch.read")),
    service: PharmacyCatalogService = Depends(get_pharmacy_catalog_service),
) -> SuccessResponse[MedicineStockResponse]:
    """Return a medicine's stock position (module spec §9)."""
    stock = await service.get_stock(_tenant_of(current_user), medicine_id)
    return SuccessResponse[MedicineStockResponse](message="Stock retrieved.", data=stock)


@router.get(
    "/{medicine_id}/batches",
    response_model=SuccessResponse[list[BatchResponse]],
    summary="List a medicine's batches",
    description="Return a medicine's batches, earliest expiry first.",
    responses={200: {"description": "Batches returned."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def list_batches(
    medicine_id: uuid.UUID = Path(description="Medicine UUID."),
    in_stock_only: bool = Query(False, description="Leave out batches holding nothing."),
    current_user: User = Depends(require_permission("pharmacy.batch.read")),
    service: PharmacyCatalogService = Depends(get_pharmacy_catalog_service),
) -> SuccessResponse[list[BatchResponse]]:
    """List a medicine's batches (module spec §9)."""
    batches = await service.list_batches(
        _tenant_of(current_user), medicine_id, in_stock_only=in_stock_only
    )
    return SuccessResponse[list[BatchResponse]](message="Batches retrieved.", data=batches)


@router.post(
    "/{medicine_id}/batches",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[BatchResponse],
    summary="Receive a batch",
    description=(
        "Take stock in directly, without a purchase order. A batch number the "
        "medicine already has is topped up, provided the expiry date agrees. "
        "A batch that has already expired is refused."
    ),
    responses={201: {"description": "Stock received."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def receive_batch(
    payload: ReceiveBatchRequest,
    medicine_id: uuid.UUID = Path(description="Medicine UUID."),
    current_user: User = Depends(require_permission("pharmacy.batch.create")),
    service: PharmacyCatalogService = Depends(get_pharmacy_catalog_service),
) -> SuccessResponse[BatchResponse]:
    """Receive a batch of a medicine (module spec §9)."""
    batch = await service.receive_batch(
        _tenant_of(current_user), medicine_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[BatchResponse](message="Stock received.", data=batch)


@router.patch(
    "/{medicine_id}/batches/{batch_id}",
    response_model=SuccessResponse[BatchResponse],
    summary="Recall a batch, or lift a recall",
    description="Set `is_recalled`. A recalled batch is never dispensed from.",
    responses={200: {"description": "Batch updated."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def update_batch(
    payload: UpdateBatchRequest,
    medicine_id: uuid.UUID = Path(description="Medicine UUID."),
    batch_id: uuid.UUID = Path(description="Batch UUID."),
    current_user: User = Depends(require_permission("pharmacy.batch.update")),
    service: PharmacyCatalogService = Depends(get_pharmacy_catalog_service),
) -> SuccessResponse[BatchResponse]:
    """Recall or release a batch."""
    batch = await service.update_batch(
        _tenant_of(current_user), medicine_id, batch_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[BatchResponse](message="Batch updated.", data=batch)


@router.post(
    "/{medicine_id}/batches/{batch_id}/adjust",
    response_model=SuccessResponse[BatchResponse],
    summary="Adjust a batch's stock",
    description=(
        "Correct a batch's count or write off expired stock, with a note. The "
        "change is recorded as a stock movement; the count is never "
        "overwritten. A batch cannot be taken below zero."
    ),
    responses={
        200: {"description": "Stock adjusted."},
        400: {"description": "The batch does not hold that many units."},
        **_NOT_FOUND,
        **_COMMON_RESPONSES,
    },
)
async def adjust_stock(
    payload: AdjustStockRequest,
    medicine_id: uuid.UUID = Path(description="Medicine UUID."),
    batch_id: uuid.UUID = Path(description="Batch UUID."),
    current_user: User = Depends(require_permission("pharmacy.batch.update")),
    service: PharmacyCatalogService = Depends(get_pharmacy_catalog_service),
) -> SuccessResponse[BatchResponse]:
    """Adjust a batch's stock."""
    batch = await service.adjust_stock(
        _tenant_of(current_user), medicine_id, batch_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[BatchResponse](message="Stock adjusted.", data=batch)
