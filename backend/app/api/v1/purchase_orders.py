"""Purchase order API routes.

Implements the purchase-order part of ``docs/modules/08-pharmacy.md`` §9.
Routes parse input, delegate to
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
from app.models.pharmacy import PurchaseOrderStatus
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
    CreatePurchaseOrderRequest,
    PurchaseOrderResponse,
    ReceivePurchaseOrderRequest,
)
from app.services.procurement_service import ProcurementService

router = APIRouter(prefix="/purchase-orders", tags=["Pharmacy — Purchase orders"])

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
    404: {"description": "Purchase order not found in this hospital."},
}

_STATE: dict[int | str, dict[str, str]] = {
    400: {"description": "The order's status does not allow this step."},
}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[PurchaseOrderResponse],
    summary="Draft a purchase order",
    description=(
        "Create a purchase order as a draft. The order number is generated. "
        "The vendor and every medicine must be active."
    ),
    responses={201: {"description": "Purchase order drafted."}, **_COMMON_RESPONSES},
)
async def create_purchase_order(
    payload: CreatePurchaseOrderRequest,
    current_user: User = Depends(require_permission("pharmacy.po.create")),
    service: ProcurementService = Depends(get_procurement_service),
) -> SuccessResponse[PurchaseOrderResponse]:
    """Draft a purchase order (module spec §9)."""
    created = await service.create_purchase_order(
        _tenant_of(current_user), payload, actor_id=current_user.id
    )
    return SuccessResponse[PurchaseOrderResponse](message="Purchase order drafted.", data=created)


@router.get(
    "",
    response_model=PaginatedResponse[PurchaseOrderResponse],
    summary="List purchase orders",
    description="Return a page of purchase orders, newest first.",
    responses={200: {"description": "Page of orders returned."}, **_COMMON_RESPONSES},
)
async def list_purchase_orders(
    order_status: PurchaseOrderStatus | None = Query(
        None, alias="status", description="Only orders in this status."
    ),
    vendor_id: uuid.UUID | None = Query(None, description="Only this vendor's orders."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("pharmacy.po.read")),
    service: ProcurementService = Depends(get_procurement_service),
) -> PaginatedResponse[PurchaseOrderResponse]:
    """List purchase orders (module spec §9)."""
    result = await service.list_purchase_orders(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        status=order_status,
        vendor_id=vendor_id,
    )
    return PaginatedResponse[PurchaseOrderResponse](
        message="Purchase orders retrieved.", data=result.items, metadata=_pagination(result)
    )


@router.get(
    "/{order_id}",
    response_model=SuccessResponse[PurchaseOrderResponse],
    summary="Get a purchase order",
    description="Return one purchase order with its items.",
    responses={200: {"description": "Order returned."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def get_purchase_order(
    order_id: uuid.UUID = Path(description="Purchase order UUID."),
    current_user: User = Depends(require_permission("pharmacy.po.read")),
    service: ProcurementService = Depends(get_procurement_service),
) -> SuccessResponse[PurchaseOrderResponse]:
    """Retrieve one purchase order."""
    order = await service.get_purchase_order(_tenant_of(current_user), order_id)
    return SuccessResponse[PurchaseOrderResponse](message="Purchase order retrieved.", data=order)


@router.post(
    "/{order_id}/send",
    response_model=SuccessResponse[PurchaseOrderResponse],
    summary="Mark a purchase order as sent",
    description="Move a draft to `sent`. Only a sent order can be received.",
    responses={200: {"description": "Order sent."}, **_STATE, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def send_purchase_order(
    order_id: uuid.UUID = Path(description="Purchase order UUID."),
    current_user: User = Depends(require_permission("pharmacy.po.update")),
    service: ProcurementService = Depends(get_procurement_service),
) -> SuccessResponse[PurchaseOrderResponse]:
    """Mark a purchase order as sent."""
    order = await service.send_purchase_order(
        _tenant_of(current_user), order_id, actor_id=current_user.id
    )
    return SuccessResponse[PurchaseOrderResponse](message="Purchase order sent.", data=order)


@router.post(
    "/{order_id}/cancel",
    response_model=SuccessResponse[PurchaseOrderResponse],
    summary="Cancel a purchase order",
    description="Cancel a draft or sent order. A received order cannot be cancelled.",
    responses={
        200: {"description": "Order cancelled."},
        **_STATE,
        **_NOT_FOUND,
        **_COMMON_RESPONSES,
    },
)
async def cancel_purchase_order(
    order_id: uuid.UUID = Path(description="Purchase order UUID."),
    current_user: User = Depends(require_permission("pharmacy.po.update")),
    service: ProcurementService = Depends(get_procurement_service),
) -> SuccessResponse[PurchaseOrderResponse]:
    """Cancel a purchase order."""
    order = await service.cancel_purchase_order(
        _tenant_of(current_user), order_id, actor_id=current_user.id
    )
    return SuccessResponse[PurchaseOrderResponse](message="Purchase order cancelled.", data=order)


@router.post(
    "/{order_id}/receive",
    response_model=SuccessResponse[PurchaseOrderResponse],
    summary="Receive a purchase order",
    description=(
        "Take in the goods of a sent order. Each line names an order item and "
        "a batch with its expiry date and quantity; a line may be split "
        "across several batches, and what arrived may differ from what was "
        "ordered. Every batch becomes stock, and the order moves to "
        "`received`. A batch that has already expired is refused."
    ),
    responses={
        200: {"description": "Goods received."},
        **_STATE,
        **_NOT_FOUND,
        **_COMMON_RESPONSES,
    },
)
async def receive_purchase_order(
    payload: ReceivePurchaseOrderRequest,
    order_id: uuid.UUID = Path(description="Purchase order UUID."),
    current_user: User = Depends(require_permission("pharmacy.po.receive")),
    service: ProcurementService = Depends(get_procurement_service),
) -> SuccessResponse[PurchaseOrderResponse]:
    """Receive a purchase order (module spec §5.2)."""
    order = await service.receive_purchase_order(
        _tenant_of(current_user), order_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[PurchaseOrderResponse](message="Goods received.", data=order)
