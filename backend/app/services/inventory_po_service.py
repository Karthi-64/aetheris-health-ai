"""Business logic for inventory purchase orders.

The procurement part of ``docs/modules/09-inventory.md`` (§5, FR-4). An order
moves one way::

    draft ──send──▶ sent ──receive──▶ received
      └──────── cancel ────────┘

Receiving is where stock is created (business rule 5): each receipt line
becomes a ``received`` movement into a stock row at the chosen location,
pointing back at the order. The whole receipt is validated before any of it
is put on the shelf.

Vendors are Pharmacy's ``vendors`` table, shared by design (§20); this service
only reads them.

Returns DTOs, never ORM models, and records an audit event per mutation
(CLAUDE.md rule 9).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.core.audit import AuditEvent
from app.core.exceptions import BusinessRuleError, NotFoundError
from app.core.logging import get_logger
from app.models.inventory import InventoryMovementReason
from app.models.pharmacy import PurchaseOrderStatus
from app.schemas.common import Page, PaginationParams
from app.schemas.inventory import (
    CreateInventoryPurchaseOrderRequest,
    InventoryPurchaseOrderResponse,
    ReceiveInventoryPurchaseOrderRequest,
)
from app.services.pharmacy_common import field_error, hospital_today

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.inventory import InventoryPurchaseOrder
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.inventory_po_repository import InventoryPurchaseOrderRepository
    from app.repositories.inventory_repository import InventoryRepository
    from app.repositories.procurement_repository import ProcurementRepository

logger = get_logger(__name__)

__all__ = [
    "InventoryPurchaseOrderNotFoundError",
    "InventoryPurchaseOrderService",
    "InventoryPurchaseOrderStateError",
]


class InventoryPurchaseOrderNotFoundError(NotFoundError):
    """Raised when a purchase order is absent from the requested hospital."""

    def __init__(self, order_id: uuid.UUID) -> None:
        super().__init__(
            message="Purchase order not found.", detail={"purchase_order_id": str(order_id)}
        )


class InventoryPurchaseOrderStateError(BusinessRuleError):
    """Raised when a step is not allowed from the order's status."""

    def __init__(self, action: str, status: PurchaseOrderStatus) -> None:
        super().__init__(
            message=f"Cannot {action} a purchase order that is {status.value}.",
            detail={"status": status.value},
        )


#: Money is kept to two places (CLAUDE.md rule 6); a fractional quantity
#: times a price can have more.
_CENT = Decimal("0.01")


def _new_po_number(now: datetime) -> str:
    """Return a new order number: ``IPO-<year>-<8 hex>``.

    The ``I`` keeps inventory orders visibly apart from Pharmacy's ``PO-``
    numbers on paperwork. No counter is needed — nothing requires these to be
    sequential — and the per-hospital unique constraint catches a collision.
    """
    return f"IPO-{now.year}-{uuid.uuid4().hex[:8].upper()}"


class InventoryPurchaseOrderService:
    """Purchase orders for inventory items.

    :param orders: Purchase-order data access.
    :param inventory: Item, location and stock data access.
    :param vendors: Vendor lookups (the table is shared with Pharmacy).
    :param hospitals: Hospital lookups, for the local date expiry is judged by.
    :param session: Request-scoped session, held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    """

    def __init__(
        self,
        orders: InventoryPurchaseOrderRepository,
        inventory: InventoryRepository,
        vendors: ProcurementRepository,
        hospitals: HospitalRepository,
        session: AsyncSession,
        audit: AuditSink,
    ) -> None:
        self._orders = orders
        self._inventory = inventory
        self._vendors = vendors
        self._hospitals = hospitals
        self._session = session
        self._audit = audit

    async def create_purchase_order(
        self,
        hospital_id: uuid.UUID,
        payload: CreateInventoryPurchaseOrderRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> InventoryPurchaseOrderResponse:
        """Draft a purchase order.

        :raises ValidationError: If the vendor or an item is unknown in this
            hospital, or inactive.
        """
        vendor = await self._vendors.get_vendor_by_id(hospital_id, payload.vendor_id)
        if vendor is None:
            raise field_error("vendor_id", "Vendor not found in this hospital.")
        if not vendor.is_active:
            raise field_error("vendor_id", f"Vendor '{vendor.name}' is inactive.")

        catalog = {
            item.id: item
            for item in await self._inventory.get_items_by_ids(
                hospital_id, [line.item_id for line in payload.items]
            )
        }
        lines: list[dict[str, Any]] = []
        for index, line in enumerate(payload.items):
            item = catalog.get(line.item_id)
            if item is None:
                raise field_error(
                    f"items.{index}.item_id", "Inventory item not found in this hospital."
                )
            if not item.is_active:
                raise field_error(
                    f"items.{index}.item_id",
                    f"Item '{item.sku}' is inactive and cannot be ordered.",
                )
            lines.append(
                {
                    "item_id": item.id,
                    "quantity": line.quantity,
                    "unit_price": line.unit_price,
                    "total": (line.unit_price * line.quantity).quantize(_CENT),
                }
            )

        order = await self._orders.create_purchase_order(
            hospital_id=hospital_id,
            vendor_id=vendor.id,
            po_number=_new_po_number(datetime.now(UTC)),
            notes=payload.notes,
            items=lines,
            created_by=actor_id,
        )
        await self._record(order, "inventory.po.created", actor_id, line_count=len(lines))
        await self._session.commit()
        return InventoryPurchaseOrderResponse.from_model(order)

    async def send_purchase_order(
        self, hospital_id: uuid.UUID, order_id: uuid.UUID, *, actor_id: uuid.UUID | None = None
    ) -> InventoryPurchaseOrderResponse:
        """Mark a draft as sent to the vendor.

        :raises InventoryPurchaseOrderNotFoundError: If absent from this tenant.
        :raises InventoryPurchaseOrderStateError: If it is not a draft.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status is not PurchaseOrderStatus.DRAFT:
            raise InventoryPurchaseOrderStateError("send", order.status)
        order = await self._orders.update_purchase_order(
            order,
            updated_by=actor_id,
            status=PurchaseOrderStatus.SENT,
            ordered_at=datetime.now(UTC),
        )
        await self._record(order, "inventory.po.sent", actor_id, before="draft")
        await self._session.commit()
        return InventoryPurchaseOrderResponse.from_model(order)

    async def cancel_purchase_order(
        self, hospital_id: uuid.UUID, order_id: uuid.UUID, *, actor_id: uuid.UUID | None = None
    ) -> InventoryPurchaseOrderResponse:
        """Cancel an order whose goods have not been received.

        :raises InventoryPurchaseOrderNotFoundError: If absent from this tenant.
        :raises InventoryPurchaseOrderStateError: If received or already cancelled.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status not in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.SENT):
            raise InventoryPurchaseOrderStateError("cancel", order.status)
        before = order.status.value
        order = await self._orders.update_purchase_order(
            order, updated_by=actor_id, status=PurchaseOrderStatus.CANCELLED
        )
        await self._record(order, "inventory.po.cancelled", actor_id, before=before)
        await self._session.commit()
        return InventoryPurchaseOrderResponse.from_model(order)

    async def receive_purchase_order(
        self,
        hospital_id: uuid.UUID,
        order_id: uuid.UUID,
        payload: ReceiveInventoryPurchaseOrderRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> InventoryPurchaseOrderResponse:
        """Take in the goods of a sent order (business rule 5).

        Each receipt line names an order line and what arrived. What arrived
        may differ from what was ordered; a batch-tracked item needs a batch
        number on every line and an untracked one must not have one.

        :raises InventoryPurchaseOrderNotFoundError: If absent from this tenant.
        :raises InventoryPurchaseOrderStateError: If it has not been sent, or
            is already received or cancelled.
        :raises ValidationError: If the location is unknown, a line is not on
            the order, a batch number does not suit its item, a batch has
            already expired, or its expiry disagrees with the stock already
            held under that batch number.
        :raises BusinessRuleError: If the location is inactive.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status is not PurchaseOrderStatus.SENT:
            raise InventoryPurchaseOrderStateError("receive", order.status)

        location = await self._inventory.get_location_by_id(hospital_id, payload.location_id)
        if location is None:
            raise field_error("location_id", "Inventory location not found in this hospital.")
        if not location.is_active:
            msg = f"Location '{location.code}' is inactive and cannot receive stock."
            raise BusinessRuleError(msg)

        today = await hospital_today(self._hospitals, hospital_id)
        by_id = {line.id: line for line in order.items}
        # Validate the whole receipt before any of it is put on the shelf.
        for index, line in enumerate(payload.items):
            ordered = by_id.get(line.po_item_id)
            if ordered is None:
                raise field_error(f"items.{index}.po_item_id", "Not an item on this order.")
            item = ordered.item
            if item.is_batch_tracked and line.batch_number is None:
                raise field_error(
                    f"items.{index}.batch_number",
                    f"{item.name} is batch-tracked: give a batch number.",
                )
            if not item.is_batch_tracked and line.batch_number is not None:
                raise field_error(
                    f"items.{index}.batch_number", f"{item.name} is not batch-tracked."
                )
            if line.expiry_date is not None and line.expiry_date < today:
                raise field_error(
                    f"items.{index}.expiry_date",
                    "A batch that has already expired cannot be received.",
                )

        now = datetime.now(UTC)
        received: list[dict[str, Any]] = []
        for index, line in enumerate(payload.items):
            item = by_id[line.po_item_id].item
            stock = await self._inventory.get_or_create_stock(
                item=item,
                location=location,
                batch_number=line.batch_number,
                expiry_date=line.expiry_date,
                created_by=actor_id,
            )
            if (
                line.batch_number is not None
                and line.expiry_date is not None
                and stock.expiry_date != line.expiry_date
            ):
                # The row existed already, under a different expiry. Raising
                # here abandons the request's transaction, so nothing above
                # is kept.
                raise field_error(
                    f"items.{index}.expiry_date",
                    f"Batch {stock.batch_number} is already recorded as expiring "
                    f"{stock.expiry_date.isoformat() if stock.expiry_date else 'never'}.",
                )
            await self._inventory.apply_movement(
                stock,
                quantity_change=line.quantity,
                reason=InventoryMovementReason.RECEIVED,
                moved_at=now,
                moved_by=actor_id,
                reference_type="purchase_order",
                reference_id=order.id,
            )
            received.append(
                {"item": item.sku, "batch": line.batch_number, "quantity": str(line.quantity)}
            )

        order = await self._orders.update_purchase_order(
            order,
            updated_by=actor_id,
            status=PurchaseOrderStatus.RECEIVED,
            received_at=now,
            received_by=actor_id,
            received_location_id=location.id,
        )
        await self._record(
            order,
            "inventory.po.received",
            actor_id,
            before="sent",
            location=location.code,
            received=received,
        )
        await self._session.commit()
        return InventoryPurchaseOrderResponse.from_model(order)

    async def get_purchase_order(
        self, hospital_id: uuid.UUID, order_id: uuid.UUID
    ) -> InventoryPurchaseOrderResponse:
        """Retrieve one purchase order.

        :raises InventoryPurchaseOrderNotFoundError: If absent from this tenant.
        """
        order = await self._orders.get_purchase_order_by_id(hospital_id, order_id)
        if order is None:
            raise InventoryPurchaseOrderNotFoundError(order_id)
        return InventoryPurchaseOrderResponse.from_model(order)

    async def list_purchase_orders(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        status: PurchaseOrderStatus | None = None,
        vendor_id: uuid.UUID | None = None,
    ) -> Page[InventoryPurchaseOrderResponse]:
        """List purchase orders, newest first."""
        page_params = pagination or PaginationParams()
        filters: dict[str, Any] = {"status": status, "vendor_id": vendor_id}
        rows = await self._orders.list_purchase_orders(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._orders.count_purchase_orders(hospital_id, **filters)
        return Page[InventoryPurchaseOrderResponse](
            items=[InventoryPurchaseOrderResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _lock_or_raise(
        self, hospital_id: uuid.UUID, order_id: uuid.UUID
    ) -> InventoryPurchaseOrder:
        """Lock an order or raise :class:`InventoryPurchaseOrderNotFoundError`."""
        order = await self._orders.get_purchase_order_by_id(hospital_id, order_id, for_update=True)
        if order is None:
            raise InventoryPurchaseOrderNotFoundError(order_id)
        return order

    async def _record(
        self,
        order: InventoryPurchaseOrder,
        action: str,
        actor_id: uuid.UUID | None,
        *,
        before: str | None = None,
        **context: Any,
    ) -> None:
        """Record an audit event and a log line for a purchase-order step."""
        await self._audit.record(
            AuditEvent(
                action=action,
                hospital_id=order.hospital_id,
                target_type="inventory_purchase_order",
                target_id=order.id,
                actor_id=actor_id,
                changes=(
                    {"status": {"before": before, "after": order.status.value}} if before else {}
                ),
                context={"po_number": order.po_number, **context},
            )
        )
        logger.info(
            action,
            hospital_id=str(order.hospital_id),
            purchase_order_id=str(order.id),
            status=order.status.value,
        )
