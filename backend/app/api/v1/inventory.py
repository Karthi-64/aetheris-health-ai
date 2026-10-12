"""Inventory API routes.

Implements ``docs/modules/09-inventory.md`` §9. Routes parse input, delegate
to :class:`~app.services.inventory_service.InventoryService` or
:class:`~app.services.inventory_po_service.InventoryPurchaseOrderService`, and
wrap the result in the standard envelope. No business logic, no database
access.

**Tenancy.** ``hospital_id`` always comes from the authenticated user.

**Not here yet.** ``GET /inventory/forecast`` (AI reorder recommendations) is
not built; calling it is a 404.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.dependencies.auth import require_permission
from app.api.dependencies.services import get_inventory_po_service, get_inventory_service
from app.core.exceptions import BusinessRuleError
from app.models.inventory import InventoryMovementReason
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
from app.schemas.inventory import (
    AdjustStockRequest,
    ConsumeRequest,
    CreateInventoryItemRequest,
    CreateInventoryPurchaseOrderRequest,
    CreateLocationRequest,
    InventoryItemResponse,
    InventoryPurchaseOrderResponse,
    ItemStockSummaryResponse,
    LocationResponse,
    MovementResponse,
    ReceiveInventoryPurchaseOrderRequest,
    StockChangeResponse,
    StockRowResponse,
    TransferRequest,
    UpdateInventoryItemRequest,
    UpdateLocationRequest,
)
from app.services.inventory_po_service import InventoryPurchaseOrderService
from app.services.inventory_service import InventoryService

router = APIRouter(prefix="/inventory", tags=["Inventory"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    422: {"description": "Request failed validation."},
}

_NOT_FOUND: dict[int | str, dict[str, str]] = {
    404: {"description": "Not found in this hospital."},
}

_SHORT: dict[int | str, dict[str, str]] = {
    409: {"description": "Not enough usable stock. Nothing was changed."},
}

_STATE: dict[int | str, dict[str, str]] = {
    400: {"description": "The order's status does not allow this step."},
}


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the request acts within.

    :param current_user: The authenticated user.
    :returns: The hospital UUID to scope every query by.
    :raises BusinessRuleError: If the user belongs to no hospital.
    """
    if current_user.hospital_id is None:
        msg = "This account is not scoped to a hospital, so inventory cannot be accessed."
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


# ── Items ───────────────────────────────────────────────────────────────────


@router.post(
    "/items",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[InventoryItemResponse],
    summary="Add an inventory item",
    description=(
        "Create an item in the caller's hospital. `sku` is uppercased and must "
        "be unique within the hospital. `is_batch_tracked` cannot be changed later."
    ),
    responses={
        201: {"description": "Item created."},
        409: {"description": "An item with this SKU already exists."},
        **_COMMON_RESPONSES,
    },
)
async def create_item(
    payload: CreateInventoryItemRequest,
    current_user: User = Depends(require_permission("inventory.item.create")),
    service: InventoryService = Depends(get_inventory_service),
) -> SuccessResponse[InventoryItemResponse]:
    """Create an item (module spec §9)."""
    created = await service.create_item(_tenant_of(current_user), payload, actor_id=current_user.id)
    return SuccessResponse[InventoryItemResponse](message="Item created.", data=created)


@router.get(
    "/items",
    response_model=PaginatedResponse[InventoryItemResponse],
    summary="List inventory items",
    description="Return a page of items, ordered by name. `q` is a name prefix or an exact SKU.",
    responses={200: {"description": "Page of items returned."}, **_COMMON_RESPONSES},
)
async def list_items(
    q: str | None = Query(None, max_length=200, description="Name prefix or exact SKU."),
    category: str | None = Query(None, max_length=100, description="Exact category."),
    is_active: bool | None = Query(None, description="Filter on the active flag."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("inventory.item.read")),
    service: InventoryService = Depends(get_inventory_service),
) -> PaginatedResponse[InventoryItemResponse]:
    """List items (module spec §9)."""
    result = await service.list_items(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        term=q,
        category=category,
        is_active=is_active,
    )
    return PaginatedResponse[InventoryItemResponse](
        message="Items retrieved.", data=result.items, metadata=_pagination(result)
    )


@router.get(
    "/items/{item_id}",
    response_model=SuccessResponse[InventoryItemResponse],
    summary="Get an inventory item",
    description="Return one item.",
    responses={200: {"description": "Item returned."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def get_item(
    item_id: uuid.UUID = Path(description="Item UUID."),
    current_user: User = Depends(require_permission("inventory.item.read")),
    service: InventoryService = Depends(get_inventory_service),
) -> SuccessResponse[InventoryItemResponse]:
    """Retrieve one item."""
    item = await service.get_item(_tenant_of(current_user), item_id)
    return SuccessResponse[InventoryItemResponse](message="Item retrieved.", data=item)


@router.patch(
    "/items/{item_id}",
    response_model=SuccessResponse[InventoryItemResponse],
    summary="Update an inventory item",
    description=(
        "Apply a partial update, including the reorder point and target stock. "
        "`sku` and `is_batch_tracked` cannot be changed."
    ),
    responses={200: {"description": "Item updated."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def update_item(
    payload: UpdateInventoryItemRequest,
    item_id: uuid.UUID = Path(description="Item UUID."),
    current_user: User = Depends(require_permission("inventory.item.update")),
    service: InventoryService = Depends(get_inventory_service),
) -> SuccessResponse[InventoryItemResponse]:
    """Update an item."""
    updated = await service.update_item(
        _tenant_of(current_user), item_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[InventoryItemResponse](message="Item updated.", data=updated)


# ── Locations ───────────────────────────────────────────────────────────────


@router.post(
    "/locations",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[LocationResponse],
    summary="Add a stock location",
    description="Create a ward, theatre, ICU or store that holds stock. `code` is unique.",
    responses={
        201: {"description": "Location created."},
        409: {"description": "A location with this code already exists."},
        **_COMMON_RESPONSES,
    },
)
async def create_location(
    payload: CreateLocationRequest,
    current_user: User = Depends(require_permission("inventory.location.create")),
    service: InventoryService = Depends(get_inventory_service),
) -> SuccessResponse[LocationResponse]:
    """Create a location (module spec §9)."""
    created = await service.create_location(
        _tenant_of(current_user), payload, actor_id=current_user.id
    )
    return SuccessResponse[LocationResponse](message="Location created.", data=created)


@router.get(
    "/locations",
    response_model=SuccessResponse[list[LocationResponse]],
    summary="List stock locations",
    description="Return the hospital's stock locations, ordered by name.",
    responses={200: {"description": "Locations returned."}, **_COMMON_RESPONSES},
)
async def list_locations(
    is_active: bool | None = Query(None, description="Filter on the active flag."),
    current_user: User = Depends(require_permission("inventory.location.read")),
    service: InventoryService = Depends(get_inventory_service),
) -> SuccessResponse[list[LocationResponse]]:
    """List locations (module spec §9)."""
    locations = await service.list_locations(_tenant_of(current_user), is_active=is_active)
    return SuccessResponse[list[LocationResponse]](message="Locations retrieved.", data=locations)


@router.patch(
    "/locations/{location_id}",
    response_model=SuccessResponse[LocationResponse],
    summary="Update a stock location",
    description="Apply a partial update. `code` cannot be changed.",
    responses={200: {"description": "Location updated."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def update_location(
    payload: UpdateLocationRequest,
    location_id: uuid.UUID = Path(description="Location UUID."),
    current_user: User = Depends(require_permission("inventory.location.update")),
    service: InventoryService = Depends(get_inventory_service),
) -> SuccessResponse[LocationResponse]:
    """Update a location."""
    updated = await service.update_location(
        _tenant_of(current_user), location_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[LocationResponse](message="Location updated.", data=updated)


# ── Stock ───────────────────────────────────────────────────────────────────


@router.get(
    "/stock",
    response_model=PaginatedResponse[StockRowResponse],
    summary="List stock",
    description=(
        "Return stock rows — one per batch of an item at a location — ordered "
        "by item, location and expiry. Rows holding nothing are left out "
        "unless `in_stock_only=false`."
    ),
    responses={200: {"description": "Page of stock rows returned."}, **_COMMON_RESPONSES},
)
async def list_stock(
    item_id: uuid.UUID | None = Query(None, description="Only this item."),
    location_id: uuid.UUID | None = Query(None, description="Only this location."),
    in_stock_only: bool = Query(True, description="Leave out rows holding nothing."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("inventory.stock.read")),
    service: InventoryService = Depends(get_inventory_service),
) -> PaginatedResponse[StockRowResponse]:
    """List stock rows (module spec §9)."""
    result = await service.list_stock(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        item_id=item_id,
        location_id=location_id,
        in_stock_only=in_stock_only,
    )
    return PaginatedResponse[StockRowResponse](
        message="Stock retrieved.", data=result.items, metadata=_pagination(result)
    )


@router.get(
    "/stock/summary",
    response_model=PaginatedResponse[ItemStockSummaryResponse],
    summary="Summarise stock per item",
    description=(
        "Return each active item's hospital-wide stock against its reorder "
        "point. `low_stock=true` returns only items at or below their reorder "
        "point — the reorder alerts panel — with a suggested order quantity."
    ),
    responses={200: {"description": "Page of summaries returned."}, **_COMMON_RESPONSES},
)
async def stock_summary(
    low_stock: bool = Query(False, description="Only items at or below their reorder point."),
    q: str | None = Query(None, max_length=200, description="Name prefix or exact SKU."),
    category: str | None = Query(None, max_length=100, description="Exact category."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("inventory.stock.read")),
    service: InventoryService = Depends(get_inventory_service),
) -> PaginatedResponse[ItemStockSummaryResponse]:
    """Summarise stock per item (FR-3)."""
    result = await service.stock_summary(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        low_stock_only=low_stock,
        term=q,
        category=category,
    )
    return PaginatedResponse[ItemStockSummaryResponse](
        message="Stock summary retrieved.", data=result.items, metadata=_pagination(result)
    )


@router.get(
    "/movements",
    response_model=PaginatedResponse[MovementResponse],
    summary="List stock movements",
    description="Return the stock ledger, newest first. Every change to stock is one entry.",
    responses={200: {"description": "Page of movements returned."}, **_COMMON_RESPONSES},
)
async def list_movements(
    item_id: uuid.UUID | None = Query(None, description="Only this item."),
    location_id: uuid.UUID | None = Query(None, description="Only this location."),
    reason: InventoryMovementReason | None = Query(None, description="Only this reason."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("inventory.stock.read")),
    service: InventoryService = Depends(get_inventory_service),
) -> PaginatedResponse[MovementResponse]:
    """List the stock ledger (AC-4)."""
    result = await service.list_movements(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        item_id=item_id,
        location_id=location_id,
        reason=reason,
    )
    return PaginatedResponse[MovementResponse](
        message="Movements retrieved.", data=result.items, metadata=_pagination(result)
    )


@router.post(
    "/consume",
    response_model=SuccessResponse[StockChangeResponse],
    summary="Record stock used",
    description=(
        "Record units of an item used at a location. Stock is taken earliest "
        "expiry first and never from an expired batch. If the location does "
        "not hold enough the response is `409` and nothing changes. When the "
        "item's stock crosses its reorder point, the people who can raise a "
        "purchase order are notified."
    ),
    responses={200: {"description": "Consumption recorded."}, **_SHORT, **_COMMON_RESPONSES},
)
async def consume(
    payload: ConsumeRequest,
    current_user: User = Depends(require_permission("inventory.consume")),
    service: InventoryService = Depends(get_inventory_service),
) -> SuccessResponse[StockChangeResponse]:
    """Record consumption (module spec §5)."""
    result = await service.consume(_tenant_of(current_user), payload, actor_id=current_user.id)
    return SuccessResponse[StockChangeResponse](message="Consumption recorded.", data=result)


@router.post(
    "/transfer",
    response_model=SuccessResponse[StockChangeResponse],
    summary="Transfer stock between locations",
    description=(
        "Move units of an item from one location to another. Batches keep "
        "their number and expiry. If the source does not hold enough the "
        "response is `409` and nothing changes."
    ),
    responses={200: {"description": "Stock transferred."}, **_SHORT, **_COMMON_RESPONSES},
)
async def transfer(
    payload: TransferRequest,
    current_user: User = Depends(require_permission("inventory.transfer")),
    service: InventoryService = Depends(get_inventory_service),
) -> SuccessResponse[StockChangeResponse]:
    """Transfer stock (module spec §9)."""
    result = await service.transfer(_tenant_of(current_user), payload, actor_id=current_user.id)
    return SuccessResponse[StockChangeResponse](message="Stock transferred.", data=result)


@router.post(
    "/adjust",
    response_model=SuccessResponse[StockChangeResponse],
    summary="Adjust stock",
    description=(
        "Correct a count or write off expired stock. `note` is required. A "
        "positive adjustment may create the stock row, which is how an "
        "opening balance is entered. Stock cannot be taken below zero."
    ),
    responses={
        200: {"description": "Stock adjusted."},
        400: {"description": "The location does not hold that many units."},
        **_COMMON_RESPONSES,
    },
)
async def adjust(
    payload: AdjustStockRequest,
    current_user: User = Depends(require_permission("inventory.adjust")),
    service: InventoryService = Depends(get_inventory_service),
) -> SuccessResponse[StockChangeResponse]:
    """Adjust stock (module spec §9)."""
    result = await service.adjust(_tenant_of(current_user), payload, actor_id=current_user.id)
    return SuccessResponse[StockChangeResponse](message="Stock adjusted.", data=result)


# ── Purchase orders ─────────────────────────────────────────────────────────


@router.post(
    "/purchase-orders",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[InventoryPurchaseOrderResponse],
    summary="Draft an inventory purchase order",
    description="Create a purchase order as a draft. The order number is generated.",
    responses={201: {"description": "Purchase order drafted."}, **_COMMON_RESPONSES},
)
async def create_purchase_order(
    payload: CreateInventoryPurchaseOrderRequest,
    current_user: User = Depends(require_permission("inventory.po.create")),
    service: InventoryPurchaseOrderService = Depends(get_inventory_po_service),
) -> SuccessResponse[InventoryPurchaseOrderResponse]:
    """Draft a purchase order (module spec §9)."""
    created = await service.create_purchase_order(
        _tenant_of(current_user), payload, actor_id=current_user.id
    )
    return SuccessResponse[InventoryPurchaseOrderResponse](
        message="Purchase order drafted.", data=created
    )


@router.get(
    "/purchase-orders",
    response_model=PaginatedResponse[InventoryPurchaseOrderResponse],
    summary="List inventory purchase orders",
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
    current_user: User = Depends(require_permission("inventory.po.read")),
    service: InventoryPurchaseOrderService = Depends(get_inventory_po_service),
) -> PaginatedResponse[InventoryPurchaseOrderResponse]:
    """List purchase orders (module spec §9)."""
    result = await service.list_purchase_orders(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        status=order_status,
        vendor_id=vendor_id,
    )
    return PaginatedResponse[InventoryPurchaseOrderResponse](
        message="Purchase orders retrieved.", data=result.items, metadata=_pagination(result)
    )


@router.get(
    "/purchase-orders/{order_id}",
    response_model=SuccessResponse[InventoryPurchaseOrderResponse],
    summary="Get an inventory purchase order",
    description="Return one purchase order with its items.",
    responses={200: {"description": "Order returned."}, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def get_purchase_order(
    order_id: uuid.UUID = Path(description="Purchase order UUID."),
    current_user: User = Depends(require_permission("inventory.po.read")),
    service: InventoryPurchaseOrderService = Depends(get_inventory_po_service),
) -> SuccessResponse[InventoryPurchaseOrderResponse]:
    """Retrieve one purchase order."""
    order = await service.get_purchase_order(_tenant_of(current_user), order_id)
    return SuccessResponse[InventoryPurchaseOrderResponse](
        message="Purchase order retrieved.", data=order
    )


@router.post(
    "/purchase-orders/{order_id}/send",
    response_model=SuccessResponse[InventoryPurchaseOrderResponse],
    summary="Mark an inventory purchase order as sent",
    description="Move a draft to `sent`. Only a sent order can be received.",
    responses={200: {"description": "Order sent."}, **_STATE, **_NOT_FOUND, **_COMMON_RESPONSES},
)
async def send_purchase_order(
    order_id: uuid.UUID = Path(description="Purchase order UUID."),
    current_user: User = Depends(require_permission("inventory.po.update")),
    service: InventoryPurchaseOrderService = Depends(get_inventory_po_service),
) -> SuccessResponse[InventoryPurchaseOrderResponse]:
    """Mark a purchase order as sent."""
    order = await service.send_purchase_order(
        _tenant_of(current_user), order_id, actor_id=current_user.id
    )
    return SuccessResponse[InventoryPurchaseOrderResponse](
        message="Purchase order sent.", data=order
    )


@router.post(
    "/purchase-orders/{order_id}/cancel",
    response_model=SuccessResponse[InventoryPurchaseOrderResponse],
    summary="Cancel an inventory purchase order",
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
    current_user: User = Depends(require_permission("inventory.po.update")),
    service: InventoryPurchaseOrderService = Depends(get_inventory_po_service),
) -> SuccessResponse[InventoryPurchaseOrderResponse]:
    """Cancel a purchase order."""
    order = await service.cancel_purchase_order(
        _tenant_of(current_user), order_id, actor_id=current_user.id
    )
    return SuccessResponse[InventoryPurchaseOrderResponse](
        message="Purchase order cancelled.", data=order
    )


@router.post(
    "/purchase-orders/{order_id}/receive",
    response_model=SuccessResponse[InventoryPurchaseOrderResponse],
    summary="Receive an inventory purchase order",
    description=(
        "Take in the goods of a sent order into `location_id`. Each line names "
        "an order item and the quantity that arrived; a batch-tracked item "
        "also needs `batch_number`. Everything becomes stock at once and the "
        "order moves to `received`."
    ),
    responses={
        200: {"description": "Goods received."},
        **_STATE,
        **_NOT_FOUND,
        **_COMMON_RESPONSES,
    },
)
async def receive_purchase_order(
    payload: ReceiveInventoryPurchaseOrderRequest,
    order_id: uuid.UUID = Path(description="Purchase order UUID."),
    current_user: User = Depends(require_permission("inventory.po.receive")),
    service: InventoryPurchaseOrderService = Depends(get_inventory_po_service),
) -> SuccessResponse[InventoryPurchaseOrderResponse]:
    """Receive a purchase order (business rule 5)."""
    order = await service.receive_purchase_order(
        _tenant_of(current_user), order_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[InventoryPurchaseOrderResponse](message="Goods received.", data=order)
