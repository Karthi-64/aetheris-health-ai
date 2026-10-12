"""Vendor API routes.

Implements the vendor part of ``docs/modules/08-pharmacy.md`` §9. Routes parse
input, delegate to
:class:`~app.services.procurement_service.ProcurementService`, and wrap the
result in the standard envelope. No business logic, no database access.

**Tenancy.** ``hospital_id`` always comes from the authenticated user.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.dependencies.auth import require_permission
from app.api.dependencies.services import get_procurement_service
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
    CreateVendorRequest,
    UpdateVendorRequest,
    VendorResponse,
)
from app.services.procurement_service import ProcurementService

router = APIRouter(prefix="/vendors", tags=["Pharmacy — Vendors"])

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
    404: {"description": "Vendor not found in this hospital."},
}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[VendorResponse],
    summary="Add a vendor",
    description="Create a vendor. The name must be unique within the hospital.",
    responses={
        201: {"description": "Vendor created."},
        409: {"description": "A vendor with this name already exists."},
        **_COMMON_RESPONSES,
    },
)
async def create_vendor(
    payload: CreateVendorRequest,
    current_user: User = Depends(require_permission("pharmacy.vendor.create")),
    service: ProcurementService = Depends(get_procurement_service),
) -> SuccessResponse[VendorResponse]:
    """Create a vendor."""
    created = await service.create_vendor(
        _tenant_of(current_user), payload, actor_id=current_user.id
    )
    return SuccessResponse[VendorResponse](message="Vendor created.", data=created)


@router.get(
    "",
    response_model=PaginatedResponse[VendorResponse],
    summary="List vendors",
    description="Return a page of vendors, ordered by name.",
    responses={200: {"description": "Page of vendors returned."}, **_COMMON_RESPONSES},
)
async def list_vendors(
    is_active: bool | None = Query(None, description="Filter on the active flag."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("pharmacy.vendor.read")),
    service: ProcurementService = Depends(get_procurement_service),
) -> PaginatedResponse[VendorResponse]:
    """List vendors."""
    result = await service.list_vendors(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        is_active=is_active,
    )
    return PaginatedResponse[VendorResponse](
        message="Vendors retrieved.", data=result.items, metadata=_pagination(result)
    )


@router.get(
    "/{vendor_id}",
    response_model=SuccessResponse[VendorResponse],
    summary="Get a vendor",
    description="Return one vendor.",
    responses={200: {"description": "Vendor returned."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def get_vendor(
    vendor_id: uuid.UUID = Path(description="Vendor UUID."),
    current_user: User = Depends(require_permission("pharmacy.vendor.read")),
    service: ProcurementService = Depends(get_procurement_service),
) -> SuccessResponse[VendorResponse]:
    """Retrieve one vendor."""
    vendor = await service.get_vendor(_tenant_of(current_user), vendor_id)
    return SuccessResponse[VendorResponse](message="Vendor retrieved.", data=vendor)


@router.patch(
    "/{vendor_id}",
    response_model=SuccessResponse[VendorResponse],
    summary="Update a vendor",
    description="Apply a partial update. Retire a vendor with `is_active: false`.",
    responses={
        200: {"description": "Vendor updated."},
        409: {"description": "A vendor with this name already exists."},
        **_NOT_FOUND,
        **_COMMON_RESPONSES,
    },
)
async def update_vendor(
    payload: UpdateVendorRequest,
    vendor_id: uuid.UUID = Path(description="Vendor UUID."),
    current_user: User = Depends(require_permission("pharmacy.vendor.update")),
    service: ProcurementService = Depends(get_procurement_service),
) -> SuccessResponse[VendorResponse]:
    """Update a vendor."""
    updated = await service.update_vendor(
        _tenant_of(current_user), vendor_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[VendorResponse](message="Vendor updated.", data=updated)
