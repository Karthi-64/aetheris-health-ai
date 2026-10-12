"""Business logic for inventory items, locations and stock.

Implements ``docs/modules/09-inventory.md`` §5: what the hospital stocks,
where, and every way stock moves other than a purchase-order receipt — use on
a ward, a transfer between locations, and a correction after a count.

What the service guarantees:

- **Stock is moved, never set.** Every change is a ledger movement, so the
  history reconstructs the current quantity exactly (AC-4).
- **A stock row never goes below zero** (§11). Consume and transfer lock the
  rows they draw on, work out the whole allocation, and only then write; a
  shortage fails the request having changed nothing.
- **What will be wasted soonest is used first.** Stock leaves earliest expiry
  first, and expired stock is never used (§14).
- **Low stock is announced once, when it happens** (FR-3, AC-2). When a
  movement takes an item's usable stock from above its reorder point to at or
  below it, the people who can raise a purchase order are notified.

The reorder point is judged against the item's usable stock across the whole
hospital. The spec's rule 3 speaks of a reorder point "per item per
location", but its schema keeps one per item; a ward running out while the
store is full is a transfer, not a purchase, so hospital-wide is the level
that should trigger buying.

Returns DTOs, never ORM models, and records an audit event per mutation
(CLAUDE.md rule 9).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent
from app.core.config import settings
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.notifications import NotificationRequest, Notifier, NullNotifier
from app.models.inventory import InventoryMovementReason
from app.schemas.common import Page, PaginationParams
from app.schemas.inventory import (
    AdjustStockRequest,
    ConsumeRequest,
    CreateInventoryItemRequest,
    CreateLocationRequest,
    InventoryItemResponse,
    ItemStockSummaryResponse,
    LocationResponse,
    MovementResponse,
    StockChangeResponse,
    StockRowResponse,
    TransferRequest,
    UpdateInventoryItemRequest,
    UpdateLocationRequest,
)

# Generic helpers that happen to live with the Pharmacy services: the local
# date expiry is judged by, the 422 shape, and audit-safe values.
from app.services.pharmacy_common import audit_value, field_error, hospital_today

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.inventory import (
        InventoryItem,
        InventoryLocation,
        InventoryMovement,
        InventoryStock,
    )
    from app.repositories.department_repository import DepartmentRepository
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.inventory_repository import InventoryRepository

logger = get_logger(__name__)

__all__ = [
    "DuplicateInventorySkuError",
    "DuplicateLocationCodeError",
    "InsufficientInventoryError",
    "InventoryItemNotFoundError",
    "InventoryLocationNotFoundError",
    "InventoryService",
]

_ZERO = Decimal("0")
#: The scale quantities are stored and reported at.
_CENTI = Decimal("0.01")
_REASONS = {
    "adjusted": InventoryMovementReason.ADJUSTED,
    "expired": InventoryMovementReason.EXPIRED,
}

#: Permission whose holders are told when an item runs low: they are the
#: people who can do something about it.
_REORDER_PERMISSION = "inventory.po.create"


def _plain(quantity: Decimal) -> str:
    """Render a quantity for a person: ``20``, not ``20.00`` and never ``2E+1``."""
    return format(quantity.normalize(), "f")


class InventoryItemNotFoundError(NotFoundError):
    """Raised when an item is absent from the requested hospital."""

    def __init__(self, item_id: uuid.UUID) -> None:
        super().__init__(message="Inventory item not found.", detail={"item_id": str(item_id)})


class InventoryLocationNotFoundError(NotFoundError):
    """Raised when a location is absent from the requested hospital."""

    def __init__(self, location_id: uuid.UUID) -> None:
        super().__init__(
            message="Inventory location not found.", detail={"location_id": str(location_id)}
        )


class DuplicateInventorySkuError(ConflictError):
    """Raised when a SKU is already in use in the hospital."""

    def __init__(self, sku: str) -> None:
        super().__init__(message=f"An item with SKU '{sku}' already exists.", detail={"sku": sku})


class DuplicateLocationCodeError(ConflictError):
    """Raised when a location code is already in use in the hospital."""

    def __init__(self, code: str) -> None:
        super().__init__(
            message=f"A location with code '{code}' already exists.", detail={"code": code}
        )


class InsufficientInventoryError(ConflictError):
    """Raised when a location does not hold enough usable stock (§11).

    A 409 rather than a 400: the request was valid, and would succeed against
    a fuller shelf. Nothing has been written when this is raised.
    """

    def __init__(self, item_name: str, requested: Decimal, available: Decimal) -> None:
        super().__init__(
            message=(
                f"Not enough {item_name} at this location: {_plain(requested)} requested, "
                f"{_plain(available)} usable. Nothing was changed."
            ),
            detail={
                "requested": str(requested.quantize(_CENTI)),
                "available": str(available.quantize(_CENTI)),
            },
        )


class InventoryService:
    """Items, locations, stock and its movements.

    :param inventory: Item, location, stock and ledger data access.
    :param departments: Department lookups, for consumption by department.
    :param hospitals: Hospital lookups, for the local date expiry is judged by.
    :param session: Request-scoped session, held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    :param notifier: Where low-stock alerts are raised. Optional.
    """

    def __init__(
        self,
        inventory: InventoryRepository,
        departments: DepartmentRepository,
        hospitals: HospitalRepository,
        session: AsyncSession,
        audit: AuditSink,
        notifier: Notifier | None = None,
    ) -> None:
        self._inventory = inventory
        self._departments = departments
        self._hospitals = hospitals
        self._session = session
        self._audit = audit
        self._notifier: Notifier = notifier or NullNotifier()

    # ── Items ─────────────────────────────────────────────────────────────────

    async def create_item(
        self,
        hospital_id: uuid.UUID,
        payload: CreateInventoryItemRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> InventoryItemResponse:
        """Add an item to the catalog.

        :raises DuplicateInventorySkuError: If the SKU is taken.
        """
        if await self._inventory.get_item_by_sku(hospital_id, payload.sku) is not None:
            raise DuplicateInventorySkuError(payload.sku)

        values = payload.model_dump()
        try:
            async with self._session.begin_nested():
                item = await self._inventory.create_item(
                    hospital_id=hospital_id, created_by=actor_id, **values
                )
        except IntegrityError as exc:
            if "uq_inventory_items_hospital_sku" in str(getattr(exc, "orig", exc)):
                raise DuplicateInventorySkuError(payload.sku) from exc
            raise

        await self._record(
            "inventory.item.created",
            hospital_id,
            "inventory_item",
            item.id,
            actor_id,
            changes={
                name: {"before": None, "after": audit_value(value)}
                for name, value in values.items()
            },
        )
        await self._session.commit()
        return InventoryItemResponse.from_model(item)

    async def update_item(
        self,
        hospital_id: uuid.UUID,
        item_id: uuid.UUID,
        payload: UpdateInventoryItemRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> InventoryItemResponse:
        """Apply a partial update to an item.

        :raises InventoryItemNotFoundError: If absent from this tenant.
        :raises ValidationError: If the result would have a target below the
            reorder point.
        """
        item = await self._item_or_raise(hospital_id, item_id)

        requested = payload.model_dump(exclude_unset=True)
        reorder = requested.get("reorder_point", item.reorder_point)
        target = requested.get("target_stock", item.target_stock)
        if reorder is not None and target is not None and target < reorder:
            raise field_error("target_stock", "target_stock must not be less than reorder_point.")

        changes = {
            name: {"before": audit_value(getattr(item, name)), "after": audit_value(value)}
            for name, value in requested.items()
            if getattr(item, name) != value
        }
        if not changes:
            return InventoryItemResponse.from_model(item)

        item = await self._inventory.update_item(
            item, updated_by=actor_id, **{name: requested[name] for name in changes}
        )
        await self._record(
            "inventory.item.updated",
            hospital_id,
            "inventory_item",
            item.id,
            actor_id,
            changes=changes,
        )
        await self._session.commit()
        return InventoryItemResponse.from_model(item)

    async def get_item(self, hospital_id: uuid.UUID, item_id: uuid.UUID) -> InventoryItemResponse:
        """Retrieve one item.

        :raises InventoryItemNotFoundError: If absent from this tenant.
        """
        return InventoryItemResponse.from_model(await self._item_or_raise(hospital_id, item_id))

    async def list_items(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        term: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
    ) -> Page[InventoryItemResponse]:
        """List items, ordered by name."""
        page_params = pagination or PaginationParams()
        filters: dict[str, Any] = {"term": term, "category": category, "is_active": is_active}
        rows = await self._inventory.list_items(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._inventory.count_items(hospital_id, **filters)
        return Page[InventoryItemResponse](
            items=[InventoryItemResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Locations ─────────────────────────────────────────────────────────────

    async def create_location(
        self,
        hospital_id: uuid.UUID,
        payload: CreateLocationRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> LocationResponse:
        """Add a stock location.

        :raises DuplicateLocationCodeError: If the code is taken.
        """
        if await self._inventory.get_location_by_code(hospital_id, payload.code) is not None:
            raise DuplicateLocationCodeError(payload.code)
        try:
            async with self._session.begin_nested():
                location = await self._inventory.create_location(
                    hospital_id=hospital_id,
                    name=payload.name,
                    code=payload.code,
                    kind=payload.kind,
                    created_by=actor_id,
                )
        except IntegrityError as exc:
            if "uq_inventory_locations_hospital_code" in str(getattr(exc, "orig", exc)):
                raise DuplicateLocationCodeError(payload.code) from exc
            raise

        await self._record(
            "inventory.location.created",
            hospital_id,
            "inventory_location",
            location.id,
            actor_id,
            context={"code": location.code, "kind": location.kind.value},
        )
        await self._session.commit()
        return LocationResponse.from_model(location)

    async def update_location(
        self,
        hospital_id: uuid.UUID,
        location_id: uuid.UUID,
        payload: UpdateLocationRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> LocationResponse:
        """Apply a partial update to a location.

        :raises InventoryLocationNotFoundError: If absent from this tenant.
        """
        location = await self._location_or_raise(hospital_id, location_id)
        requested = payload.model_dump(exclude_unset=True)
        if "kind" in requested:
            requested["kind"] = payload.kind
        changes = {
            name: {
                "before": audit_value(
                    getattr(getattr(location, name), "value", getattr(location, name))
                ),
                "after": audit_value(getattr(value, "value", value)),
            }
            for name, value in requested.items()
            if getattr(location, name) != value
        }
        if not changes:
            return LocationResponse.from_model(location)
        location = await self._inventory.update_location(
            location, updated_by=actor_id, **{name: requested[name] for name in changes}
        )
        await self._record(
            "inventory.location.updated",
            hospital_id,
            "inventory_location",
            location.id,
            actor_id,
            changes=changes,
        )
        await self._session.commit()
        return LocationResponse.from_model(location)

    async def list_locations(
        self, hospital_id: uuid.UUID, *, is_active: bool | None = None
    ) -> list[LocationResponse]:
        """List a hospital's stock locations, ordered by name."""
        rows = await self._inventory.list_locations(hospital_id, is_active=is_active)
        return [LocationResponse.from_model(row) for row in rows]

    # ── Stock: reads ──────────────────────────────────────────────────────────

    async def list_stock(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        item_id: uuid.UUID | None = None,
        location_id: uuid.UUID | None = None,
        in_stock_only: bool = True,
    ) -> Page[StockRowResponse]:
        """List stock rows: one per batch of an item at a location (§9)."""
        page_params = pagination or PaginationParams()
        filters: dict[str, Any] = {
            "item_id": item_id,
            "location_id": location_id,
            "in_stock_only": in_stock_only,
        }
        today = await hospital_today(self._hospitals, hospital_id)
        rows = await self._inventory.list_stock(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._inventory.count_stock(hospital_id, **filters)
        return Page[StockRowResponse](
            items=[StockRowResponse.from_model(row, today=today) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    async def stock_summary(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        low_stock_only: bool = False,
        term: str | None = None,
        category: str | None = None,
    ) -> Page[ItemStockSummaryResponse]:
        """Summarise each active item's stock against its reorder point (FR-3).

        An item with no stock at all is included — it is the lowest of all.

        :param low_stock_only: Only items at or below their reorder point: the
            reorder alerts panel (§12).
        """
        page_params = pagination or PaginationParams()
        filters: dict[str, Any] = {"term": term, "category": category, "is_active": True}
        today = await hospital_today(self._hospitals, hospital_id)

        if not low_stock_only:
            items = await self._inventory.list_items(
                hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
            )
            total = await self._inventory.count_items(hospital_id, **filters)
            summaries = await self._summarise(hospital_id, items, today)
        else:
            # Whether an item is low depends on a sum over its stock, so the
            # filter is applied here. A hospital's catalog is small enough
            # for that; it is bounded so it cannot grow without notice.
            everything = await self._inventory.list_items(
                hospital_id, skip=0, limit=5000, **filters
            )
            low = [s for s in await self._summarise(hospital_id, everything, today) if s.is_low]
            total = len(low)
            summaries = low[page_params.offset : page_params.offset + page_params.limit]

        return Page[ItemStockSummaryResponse](
            items=summaries,
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    async def list_movements(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        item_id: uuid.UUID | None = None,
        location_id: uuid.UUID | None = None,
        reason: InventoryMovementReason | None = None,
    ) -> Page[MovementResponse]:
        """List the stock ledger, newest first (AC-4)."""
        page_params = pagination or PaginationParams()
        filters: dict[str, Any] = {"item_id": item_id, "location_id": location_id, "reason": reason}
        rows = await self._inventory.list_movements(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._inventory.count_movements(hospital_id, **filters)
        return Page[MovementResponse](
            items=[MovementResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Stock: movements ──────────────────────────────────────────────────────

    async def consume(
        self,
        hospital_id: uuid.UUID,
        payload: ConsumeRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> StockChangeResponse:
        """Record stock used at a location (module spec §5, FR-6).

        :raises ValidationError: If the item, location or department is
            unknown in this hospital.
        :raises BusinessRuleError: If the item is inactive.
        :raises InsufficientInventoryError: If the location does not hold
            enough usable stock. Nothing is written.
        """
        item = await self._item_for_movement(hospital_id, payload.item_id)
        location = await self._location_for_movement(
            hospital_id, payload.location_id, "location_id"
        )
        if (
            payload.department_id is not None
            and (await self._departments.get_department_by_id(hospital_id, payload.department_id))
            is None
        ):
            raise field_error("department_id", "Department not found in this hospital.")

        today = await hospital_today(self._hospitals, hospital_id)
        before = await self._usable(hospital_id, item, today)
        takes = await self._allocate(
            hospital_id, item, location, payload.quantity, today, payload.batch_number
        )

        now = datetime.now(UTC)
        movements = [
            await self._inventory.apply_movement(
                stock,
                quantity_change=-quantity,
                reason=InventoryMovementReason.CONSUMED,
                moved_at=now,
                moved_by=actor_id,
                department_id=payload.department_id,
                reference_type="consumption",
                note=payload.note,
            )
            for stock, quantity in takes
        ]
        summary = await self._after(hospital_id, item, today, before, actor_id)
        await self._record(
            "inventory.consumed",
            hospital_id,
            "inventory_item",
            item.id,
            actor_id,
            context={
                "location": location.code,
                "quantity": str(payload.quantity),
                "department_id": str(payload.department_id) if payload.department_id else None,
                "usable_after": str(summary.usable_quantity),
            },
        )
        await self._session.commit()
        return StockChangeResponse(
            movements=[MovementResponse.from_model(m) for m in movements], summary=summary
        )

    async def transfer(
        self,
        hospital_id: uuid.UUID,
        payload: TransferRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> StockChangeResponse:
        """Move stock between two locations, keeping each batch's identity.

        One ``transferred_out`` and one ``transferred_in`` movement per batch
        moved, sharing a reference so the pair can be matched.

        :raises ValidationError: If the item or a location is unknown.
        :raises BusinessRuleError: If the item or the destination is inactive.
        :raises InsufficientInventoryError: If the source does not hold enough
            usable stock. Nothing is written.
        """
        item = await self._item_for_movement(hospital_id, payload.item_id)
        source = await self._location_for_movement(
            hospital_id, payload.from_location_id, "from_location_id"
        )
        destination = await self._location_for_movement(
            hospital_id, payload.to_location_id, "to_location_id", must_be_active=True
        )

        today = await hospital_today(self._hospitals, hospital_id)
        takes = await self._allocate(
            hospital_id, item, source, payload.quantity, today, payload.batch_number
        )

        now = datetime.now(UTC)
        transfer_id = uuid.uuid4()
        movements: list[InventoryMovement] = []
        for stock, quantity in takes:
            movements.append(
                await self._inventory.apply_movement(
                    stock,
                    quantity_change=-quantity,
                    reason=InventoryMovementReason.TRANSFERRED_OUT,
                    moved_at=now,
                    moved_by=actor_id,
                    reference_type="transfer",
                    reference_id=transfer_id,
                    note=payload.note,
                )
            )
            target = await self._inventory.get_or_create_stock(
                item=item,
                location=destination,
                batch_number=stock.batch_number,
                expiry_date=stock.expiry_date,
                created_by=actor_id,
            )
            movements.append(
                await self._inventory.apply_movement(
                    target,
                    quantity_change=quantity,
                    reason=InventoryMovementReason.TRANSFERRED_IN,
                    moved_at=now,
                    moved_by=actor_id,
                    reference_type="transfer",
                    reference_id=transfer_id,
                    note=payload.note,
                )
            )

        totals = await self._inventory.totals_by_item(hospital_id, on=today, item_ids=[item.id])
        on_hand, usable = totals.get(item.id, (_ZERO, _ZERO))
        await self._record(
            "inventory.transferred",
            hospital_id,
            "inventory_item",
            item.id,
            actor_id,
            context={
                "from": source.code,
                "to": destination.code,
                "quantity": str(payload.quantity),
                "transfer_id": str(transfer_id),
            },
        )
        await self._session.commit()
        return StockChangeResponse(
            movements=[MovementResponse.from_model(m) for m in movements],
            summary=ItemStockSummaryResponse.build(item, on_hand, usable),
        )

    async def adjust(
        self,
        hospital_id: uuid.UUID,
        payload: AdjustStockRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> StockChangeResponse:
        """Correct a count, or write off expired stock, with a reason (§11).

        Adding stock may create the stock row, which is how an opening balance
        is entered. A batch-tracked item needs a batch number; an untracked
        one must not have one.

        :raises ValidationError: If the item or location is unknown, or the
            batch number does not suit the item.
        :raises BusinessRuleError: If it would take the stock row below zero,
            or there is no such stock to remove from.
        """
        item = await self._item_for_movement(hospital_id, payload.item_id, allow_inactive=True)
        location = await self._location_for_movement(
            hospital_id, payload.location_id, "location_id"
        )
        self._check_batch(item, payload.batch_number)

        today = await hospital_today(self._hospitals, hospital_id)
        before = await self._usable(hospital_id, item, today)
        stock = await self._inventory.get_or_create_stock(
            item=item,
            location=location,
            batch_number=payload.batch_number,
            expiry_date=payload.expiry_date,
            created_by=actor_id,
        )
        if stock.quantity + payload.quantity_change < 0:
            msg = (
                f"{location.name} holds {stock.quantity} of {item.name}; "
                f"{-payload.quantity_change} cannot be removed."
            )
            raise BusinessRuleError(msg, detail={"quantity": str(stock.quantity)})

        held = stock.quantity
        movement = await self._inventory.apply_movement(
            stock,
            quantity_change=payload.quantity_change,
            reason=_REASONS[payload.reason],
            moved_at=datetime.now(UTC),
            moved_by=actor_id,
            reference_type="adjustment",
            note=payload.note,
        )
        summary = await self._after(hospital_id, item, today, before, actor_id)
        await self._record(
            "inventory.adjusted",
            hospital_id,
            "inventory_item",
            item.id,
            actor_id,
            changes={"quantity": {"before": str(held), "after": str(stock.quantity)}},
            context={
                "location": location.code,
                "batch_number": stock.batch_number,
                "reason": payload.reason,
                "note": payload.note,
            },
        )
        await self._session.commit()
        return StockChangeResponse(
            movements=[MovementResponse.from_model(movement)], summary=summary
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _item_or_raise(self, hospital_id: uuid.UUID, item_id: uuid.UUID) -> InventoryItem:
        """Fetch an item or raise :class:`InventoryItemNotFoundError`."""
        item = await self._inventory.get_item_by_id(hospital_id, item_id)
        if item is None:
            raise InventoryItemNotFoundError(item_id)
        return item

    async def _location_or_raise(
        self, hospital_id: uuid.UUID, location_id: uuid.UUID
    ) -> InventoryLocation:
        """Fetch a location or raise :class:`InventoryLocationNotFoundError`."""
        location = await self._inventory.get_location_by_id(hospital_id, location_id)
        if location is None:
            raise InventoryLocationNotFoundError(location_id)
        return location

    async def _item_for_movement(
        self, hospital_id: uuid.UUID, item_id: uuid.UUID, *, allow_inactive: bool = False
    ) -> InventoryItem:
        """Resolve the item a request body names.

        A 422 rather than a 404: the id came in the body, not the path.

        :raises ValidationError: If unknown in this hospital.
        :raises BusinessRuleError: If inactive and that is not allowed.
        """
        item = await self._inventory.get_item_by_id(hospital_id, item_id)
        if item is None:
            raise field_error("item_id", "Inventory item not found in this hospital.")
        if not item.is_active and not allow_inactive:
            msg = f"Item '{item.sku}' is inactive."
            raise BusinessRuleError(msg)
        return item

    async def _location_for_movement(
        self,
        hospital_id: uuid.UUID,
        location_id: uuid.UUID,
        field: str,
        *,
        must_be_active: bool = False,
    ) -> InventoryLocation:
        """Resolve a location a request body names.

        :raises ValidationError: If unknown in this hospital.
        :raises BusinessRuleError: If inactive and it must not be. Stock can
            still be used up or moved out of a closed location, but nothing
            may be sent to one.
        """
        location = await self._inventory.get_location_by_id(hospital_id, location_id)
        if location is None:
            raise field_error(field, "Inventory location not found in this hospital.")
        if must_be_active and not location.is_active:
            msg = f"Location '{location.code}' is inactive and cannot receive stock."
            raise BusinessRuleError(msg)
        return location

    @staticmethod
    def _check_batch(item: InventoryItem, batch_number: str | None) -> None:
        """A tracked item needs a batch number; an untracked one must not have one."""
        if item.is_batch_tracked and batch_number is None:
            raise field_error("batch_number", f"{item.name} is batch-tracked: give a batch number.")
        if not item.is_batch_tracked and batch_number is not None:
            raise field_error("batch_number", f"{item.name} is not batch-tracked.")

    async def _allocate(
        self,
        hospital_id: uuid.UUID,
        item: InventoryItem,
        location: InventoryLocation,
        quantity: Decimal,
        today: date,
        batch_number: str | None,
    ) -> list[tuple[InventoryStock, Decimal]]:
        """Lock a location's usable stock and decide what to take from each row.

        Earliest expiry first. Writes nothing.

        :returns: ``(stock_row, quantity)`` pairs.
        :raises InsufficientInventoryError: If the rows do not hold enough.
        """
        rows = await self._inventory.lock_usable_stock(
            hospital_id, item.id, location.id, on=today, batch_number=batch_number
        )
        takes: list[tuple[InventoryStock, Decimal]] = []
        needed = quantity
        for row in rows:
            if needed == 0:
                break
            taken = min(needed, row.quantity)
            takes.append((row, taken))
            needed -= taken
        if needed > 0:
            raise InsufficientInventoryError(item.name, quantity, quantity - needed)
        return takes

    async def _usable(self, hospital_id: uuid.UUID, item: InventoryItem, today: date) -> Decimal:
        """Return an item's usable stock across the hospital."""
        totals = await self._inventory.totals_by_item(hospital_id, on=today, item_ids=[item.id])
        return totals.get(item.id, (_ZERO, _ZERO))[1]

    async def _summarise(
        self, hospital_id: uuid.UUID, items: list[InventoryItem], today: date
    ) -> list[ItemStockSummaryResponse]:
        """Build the stock summary for a list of items in one totals query."""
        totals = await self._inventory.totals_by_item(
            hospital_id, on=today, item_ids=[item.id for item in items]
        )
        return [
            ItemStockSummaryResponse.build(item, *totals.get(item.id, (_ZERO, _ZERO)))
            for item in items
        ]

    async def _after(
        self,
        hospital_id: uuid.UUID,
        item: InventoryItem,
        today: date,
        usable_before: Decimal,
        actor_id: uuid.UUID | None,
    ) -> ItemStockSummaryResponse:
        """Summarise an item after a movement, and alert if it has just run low.

        AC-2: the alert fires when usable stock crosses the reorder point —
        once, on the movement that crosses it, not on every one after.
        """
        totals = await self._inventory.totals_by_item(hospital_id, on=today, item_ids=[item.id])
        on_hand, usable = totals.get(item.id, (_ZERO, _ZERO))
        summary = ItemStockSummaryResponse.build(item, on_hand, usable)
        if (
            item.reorder_point is not None
            and usable_before > item.reorder_point
            and usable <= item.reorder_point
        ):
            await self._notifier.notify(
                NotificationRequest(
                    kind="inventory.low_stock",
                    hospital_id=hospital_id,
                    recipient_permission=_REORDER_PERMISSION,
                    variables={
                        "item_name": item.name,
                        "quantity": _plain(usable),
                        "unit_of_measure": item.unit_of_measure,
                        "reorder_point": str(item.reorder_point),
                        "action_url": f"{settings.FRONTEND_BASE_URL.rstrip('/')}/inventory",
                    },
                    link="/inventory",
                )
            )
            logger.info(
                "inventory.low_stock",
                hospital_id=str(hospital_id),
                item_id=str(item.id),
                reorder_point=item.reorder_point,
            )
        return summary

    async def _record(
        self,
        action: str,
        hospital_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        *,
        changes: dict[str, dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Record an audit event and a log line for a mutation."""
        await self._audit.record(
            AuditEvent(
                action=action,
                hospital_id=hospital_id,
                target_type=target_type,
                target_id=target_id,
                actor_id=actor_id,
                changes=changes or {},
                context=context or {},
            )
        )
        logger.info(action, hospital_id=str(hospital_id), target_id=str(target_id))
