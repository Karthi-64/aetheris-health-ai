"""Pydantic DTOs for the Inventory module.

Request models enforce ``docs/modules/09-inventory.md`` §11 before a service
sees the payload (``docs/07-SECURITY.md``, rule 5). Response models are the
only inventory shapes that cross the API boundary.

Quantities and money go out as decimal strings, never JSON numbers
(CLAUDE.md rule 6): some consumables are issued in fractions of a unit.
"""

from __future__ import annotations

# NOTE: runtime imports, not TYPE_CHECKING — Pydantic resolves field
# annotations against the module's real globals (backend/CLAUDE.md).
import re
from datetime import date, datetime  # noqa: TC003
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal, Self
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.inventory import InventoryLocationKind, InventoryMovementReason
from app.models.pharmacy import PurchaseOrderStatus  # noqa: TC001 — Pydantic needs it at runtime

if TYPE_CHECKING:
    from app.models.inventory import (
        InventoryItem,
        InventoryLocation,
        InventoryMovement,
        InventoryPurchaseOrder,
        InventoryStock,
    )

__all__ = [
    "MAX_LINES",
    "AdjustStockRequest",
    "ConsumeRequest",
    "CreateInventoryItemRequest",
    "CreateInventoryPurchaseOrderRequest",
    "CreateLocationRequest",
    "InventoryItemResponse",
    "InventoryPurchaseOrderResponse",
    "ItemStockSummaryResponse",
    "LocationResponse",
    "MovementResponse",
    "ReceiveInventoryPurchaseOrderRequest",
    "StockChangeResponse",
    "StockRowResponse",
    "TransferRequest",
    "UpdateInventoryItemRequest",
    "UpdateLocationRequest",
]

#: Upper bound on lines per purchase order or receipt.
MAX_LINES = 50

#: SKUs, location codes and batch numbers. Stored uppercased.
_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_./-]*$")

#: A monetary amount on the wire: ``NUMERIC(15, 2)``, never negative.
Money = Annotated[Decimal, Field(max_digits=15, decimal_places=2, ge=0)]

#: A quantity of stock: ``NUMERIC(12, 2)``, strictly positive.
Quantity = Annotated[Decimal, Field(max_digits=12, decimal_places=2, gt=0)]


#: Quantities are ``NUMERIC(12, 2)``; this is that scale.
_TWO_PLACES = Decimal("0.01")


def _strip_required(value: str, label: str) -> str:
    """Trim a required string and reject one that is blank."""
    stripped = value.strip()
    if not stripped:
        msg = f"{label} must not be blank."
        raise ValueError(msg)
    return stripped


def _blank_to_none(value: str | None) -> str | None:
    """Trim optional free text, collapsing blank to ``None``."""
    if value is None:
        return None
    return value.strip() or None


def _code(value: str, label: str) -> str:
    """Uppercase a code and restrict it to label-safe characters."""
    code = value.strip().upper()
    if not _CODE_PATTERN.fullmatch(code):
        msg = f"{label} may contain only letters, digits and the characters - _ . /"
        raise ValueError(msg)
    return code


def _optional_code(value: str | None, label: str) -> str | None:
    """Like :func:`_code`, for an optional field."""
    return _code(value, label) if value is not None else None


def _reject_explicit_nulls(model: BaseModel, required: frozenset[str]) -> None:
    """Refuse ``null`` for a column that cannot hold one."""
    nulled = sorted(
        name for name in model.model_fields_set & required if getattr(model, name) is None
    )
    if nulled:
        msg = f"Cannot be null: {', '.join(nulled)}."
        raise ValueError(msg)


def _check_levels(reorder_point: int | None, target_stock: int | None) -> None:
    """A reorder should aim above the level that triggers it."""
    if reorder_point is not None and target_stock is not None and target_stock < reorder_point:
        msg = "target_stock must not be less than reorder_point."
        raise ValueError(msg)


# ── Items ───────────────────────────────────────────────────────────────────


class CreateInventoryItemRequest(BaseModel):
    """Payload for ``POST /api/v1/inventory/items``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "sku": "GLOVE-M",
                "name": "Nitrile gloves, medium",
                "category": "Disposables",
                "unit_of_measure": "box of 100",
                "is_batch_tracked": False,
                "reorder_point": 20,
                "target_stock": 80,
            },
        },
    )

    sku: str = Field(min_length=1, max_length=50, description="Stock code. Uppercased.")
    name: str = Field(min_length=1, max_length=200, description="Display name.")
    category: str | None = Field(default=None, max_length=100, description="Free-form grouping.")
    unit_of_measure: str = Field(
        default="unit", min_length=1, max_length=30, description="What one unit is."
    )
    is_batch_tracked: bool = Field(
        default=False, description="Whether stock is kept per batch with an expiry."
    )
    reorder_point: int | None = Field(
        default=None, ge=0, le=10_000_000, description="Stock at or below which it is low."
    )
    target_stock: int | None = Field(
        default=None, ge=0, le=10_000_000, description="Level a reorder should restore."
    )

    @field_validator("sku")
    @classmethod
    def _check_sku(cls, value: str) -> str:
        """Uppercase the SKU and restrict its characters."""
        return _code(value, "SKU")

    @field_validator("name", "unit_of_measure")
    @classmethod
    def _check_required(cls, value: str) -> str:
        """Trim required text and reject a blank value."""
        return _strip_required(value, "Name and unit of measure")

    @field_validator("category")
    @classmethod
    def _trim_category(cls, value: str | None) -> str | None:
        """Trim the category, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Keep the target at or above the reorder point."""
        _check_levels(self.reorder_point, self.target_stock)
        return self


class UpdateInventoryItemRequest(BaseModel):
    """Payload for ``PATCH /api/v1/inventory/items/{id}``.

    ``sku`` and ``is_batch_tracked`` are immutable: existing stock rows were
    recorded with or without batches on the strength of it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200, description="Name.")
    category: str | None = Field(default=None, max_length=100, description="Grouping.")
    unit_of_measure: str | None = Field(
        default=None, min_length=1, max_length=30, description="What one unit is."
    )
    reorder_point: int | None = Field(default=None, ge=0, le=10_000_000, description="Low level.")
    target_stock: int | None = Field(default=None, ge=0, le=10_000_000, description="Target.")
    is_active: bool | None = Field(default=None, description="Whether it can be used.")

    @field_validator("name", "unit_of_measure")
    @classmethod
    def _check_required(cls, value: str | None) -> str | None:
        """Trim required text and reject a blank value."""
        return _strip_required(value, "Name and unit of measure") if value is not None else None

    @field_validator("category")
    @classmethod
    def _trim_category(cls, value: str | None) -> str | None:
        """Trim the category, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_nulls(self) -> Self:
        """Reject ``null`` for the ``NOT NULL`` columns."""
        _reject_explicit_nulls(self, frozenset({"name", "unit_of_measure", "is_active"}))
        return self


class InventoryItemResponse(BaseModel):
    """An inventory item."""

    id: UUID = Field(description="Item UUID.")
    sku: str = Field(description="Stock code, unique per hospital.")
    name: str = Field(description="Display name.")
    category: str | None = Field(description="Free-form grouping.")
    unit_of_measure: str = Field(description="What one unit is.")
    is_batch_tracked: bool = Field(description="Whether stock is kept per batch.")
    reorder_point: int | None = Field(description="Stock at or below which it is low.")
    target_stock: int | None = Field(description="Level a reorder should restore.")
    is_active: bool = Field(description="Whether it can be received, ordered or consumed.")
    created_at: datetime = Field(description="When it was added (UTC).")
    updated_at: datetime = Field(description="When it was last changed (UTC).")

    @classmethod
    def from_model(cls, item: InventoryItem) -> Self:
        """Build the DTO from an :class:`~app.models.inventory.InventoryItem`."""
        return cls(
            id=item.id,
            sku=item.sku,
            name=item.name,
            category=item.category,
            unit_of_measure=item.unit_of_measure,
            is_batch_tracked=item.is_batch_tracked,
            reorder_point=item.reorder_point,
            target_stock=item.target_stock,
            is_active=item.is_active,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


# ── Locations ───────────────────────────────────────────────────────────────


class CreateLocationRequest(BaseModel):
    """Payload for ``POST /api/v1/inventory/locations``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, description="Display name.")
    code: str = Field(min_length=1, max_length=50, description="Short code. Uppercased.")
    kind: InventoryLocationKind = Field(
        default=InventoryLocationKind.STORE, description="ward, ot, icu or store."
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """Trim the name and reject a blank one."""
        return _strip_required(value, "Name")

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        """Uppercase the code and restrict its characters."""
        return _code(value, "Code")


class UpdateLocationRequest(BaseModel):
    """Payload for ``PATCH /api/v1/inventory/locations/{id}``. ``code`` is immutable."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200, description="Name.")
    kind: InventoryLocationKind | None = Field(default=None, description="Kind of location.")
    is_active: bool | None = Field(default=None, description="Whether it can receive stock.")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        """Trim the name and reject a blank one."""
        return _strip_required(value, "Name") if value is not None else None

    @model_validator(mode="after")
    def _check_nulls(self) -> Self:
        """Reject ``null`` for the ``NOT NULL`` columns."""
        _reject_explicit_nulls(self, frozenset({"name", "kind", "is_active"}))
        return self


class LocationResponse(BaseModel):
    """A stock location."""

    id: UUID = Field(description="Location UUID.")
    name: str = Field(description="Display name.")
    code: str = Field(description="Short code, unique per hospital.")
    kind: InventoryLocationKind = Field(description="ward, ot, icu or store.")
    is_active: bool = Field(description="Whether it can receive stock.")

    @classmethod
    def from_model(cls, location: InventoryLocation) -> Self:
        """Build the DTO from an :class:`~app.models.inventory.InventoryLocation`."""
        return cls(
            id=location.id,
            name=location.name,
            code=location.code,
            kind=location.kind,
            is_active=location.is_active,
        )


# ── Stock ───────────────────────────────────────────────────────────────────


class StockRowResponse(BaseModel):
    """One batch of one item at one location."""

    id: UUID = Field(description="Stock row UUID.")
    item_id: UUID = Field(description="Item UUID.")
    item_sku: str = Field(description="Item SKU.")
    item_name: str = Field(description="Item name.")
    unit_of_measure: str = Field(description="What one unit is.")
    location_id: UUID = Field(description="Location UUID.")
    location_code: str = Field(description="Location code.")
    location_name: str = Field(description="Location name.")
    batch_number: str | None = Field(description="Batch number; null for an untracked item.")
    expiry_date: date | None = Field(description="Last day the batch may be used.")
    quantity: Decimal = Field(description="Units held, as a decimal string.")
    is_expired: bool = Field(description="Past its expiry: held but not usable.")

    @classmethod
    def from_model(cls, stock: InventoryStock, *, today: date) -> Self:
        """Build the DTO from an :class:`~app.models.inventory.InventoryStock`.

        :param stock: The ORM instance, item and location loaded.
        :param today: Today in the hospital's timezone.
        """
        return cls(
            id=stock.id,
            item_id=stock.item_id,
            item_sku=stock.item.sku,
            item_name=stock.item.name,
            unit_of_measure=stock.item.unit_of_measure,
            location_id=stock.location_id,
            location_code=stock.location.code,
            location_name=stock.location.name,
            batch_number=stock.batch_number,
            expiry_date=stock.expiry_date,
            quantity=stock.quantity,
            is_expired=stock.expiry_date is not None and stock.expiry_date < today,
        )


class ItemStockSummaryResponse(BaseModel):
    """An item's hospital-wide stock against its reorder point (FR-3)."""

    item: InventoryItemResponse = Field(description="The item.")
    quantity_on_hand: Decimal = Field(description="Units held anywhere, in any state.")
    usable_quantity: Decimal = Field(description="Units held that have not expired.")
    is_low: bool = Field(description="Usable stock is at or below the reorder point.")
    suggested_order_quantity: Decimal = Field(
        description="Units that would bring usable stock back to the target. Zero if not low."
    )

    @classmethod
    def build(cls, item: InventoryItem, on_hand: Decimal, usable: Decimal) -> Self:
        """Build the summary for an item from its totals.

        :param item: The item.
        :param on_hand: Units held in any state.
        :param usable: Units held that have not expired.
        """
        is_low = item.reorder_point is not None and usable <= item.reorder_point
        target = item.target_stock if item.target_stock is not None else item.reorder_point
        suggested = max(Decimal(target or 0) - usable, Decimal("0")) if is_low else Decimal("0")
        # An item with no stock rows has totals that never came from the
        # database; put every quantity on the wire at the same two places.
        return cls(
            item=InventoryItemResponse.from_model(item),
            quantity_on_hand=on_hand.quantize(_TWO_PLACES),
            usable_quantity=usable.quantize(_TWO_PLACES),
            is_low=is_low,
            suggested_order_quantity=suggested.quantize(_TWO_PLACES),
        )


class ConsumeRequest(BaseModel):
    """Payload for ``POST /api/v1/inventory/consume``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "item_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "location_id": "8a6e0804-2bd0-4672-b79d-d97027f9071a",
                "quantity": "2",
            },
        },
    )

    item_id: UUID = Field(description="Item consumed.")
    location_id: UUID = Field(description="Where it was taken from.")
    quantity: Quantity = Field(description="Units consumed.")
    department_id: UUID | None = Field(default=None, description="Department that used it.")
    batch_number: str | None = Field(
        default=None, max_length=50, description="Take from this batch only."
    )
    note: str | None = Field(default=None, max_length=500, description="Optional note.")

    @field_validator("batch_number")
    @classmethod
    def _check_batch(cls, value: str | None) -> str | None:
        """Uppercase the batch number and restrict its characters."""
        return _optional_code(value, "Batch number")

    @field_validator("note")
    @classmethod
    def _trim_note(cls, value: str | None) -> str | None:
        """Trim the note, collapsing blank to ``None``."""
        return _blank_to_none(value)


class TransferRequest(BaseModel):
    """Payload for ``POST /api/v1/inventory/transfer``."""

    model_config = ConfigDict(extra="forbid")

    item_id: UUID = Field(description="Item moved.")
    from_location_id: UUID = Field(description="Where it is taken from.")
    to_location_id: UUID = Field(description="Where it is sent.")
    quantity: Quantity = Field(description="Units moved.")
    batch_number: str | None = Field(
        default=None, max_length=50, description="Move this batch only."
    )
    note: str | None = Field(default=None, max_length=500, description="Optional note.")

    @field_validator("batch_number")
    @classmethod
    def _check_batch(cls, value: str | None) -> str | None:
        """Uppercase the batch number and restrict its characters."""
        return _optional_code(value, "Batch number")

    @field_validator("note")
    @classmethod
    def _trim_note(cls, value: str | None) -> str | None:
        """Trim the note, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_locations(self) -> Self:
        """§11: a transfer has a source and a different destination."""
        if self.from_location_id == self.to_location_id:
            msg = "A transfer needs two different locations."
            raise ValueError(msg)
        return self


class AdjustStockRequest(BaseModel):
    """Payload for ``POST /api/v1/inventory/adjust`` — a stock correction."""

    model_config = ConfigDict(extra="forbid")

    item_id: UUID = Field(description="Item corrected.")
    location_id: UUID = Field(description="Where it is held.")
    quantity_change: Decimal = Field(
        max_digits=12, decimal_places=2, description="Units to add (positive) or remove."
    )
    reason: Literal["adjusted", "expired"] = Field(
        default="adjusted", description="Why the stock is changing."
    )
    note: str = Field(min_length=1, max_length=500, description="Explanation. Required (§11).")
    batch_number: str | None = Field(default=None, max_length=50, description="Batch corrected.")
    expiry_date: date | None = Field(
        default=None, description="Expiry, when the correction creates a new batch."
    )

    @field_validator("batch_number")
    @classmethod
    def _check_batch(cls, value: str | None) -> str | None:
        """Uppercase the batch number and restrict its characters."""
        return _optional_code(value, "Batch number")

    @field_validator("note")
    @classmethod
    def _check_note(cls, value: str) -> str:
        """Trim the note and reject a blank one."""
        return _strip_required(value, "Note")

    @model_validator(mode="after")
    def _check_change(self) -> Self:
        """A correction changes something, and a write-off removes stock."""
        if self.quantity_change == 0:
            msg = "quantity_change must not be zero."
            raise ValueError(msg)
        if self.reason == "expired" and self.quantity_change > 0:
            msg = "Writing off expired stock must remove units."
            raise ValueError(msg)
        return self


class MovementResponse(BaseModel):
    """One entry in the stock ledger."""

    id: UUID = Field(description="Movement UUID.")
    item_id: UUID = Field(description="Item moved.")
    location_id: UUID = Field(description="Where it moved.")
    stock_id: UUID = Field(description="Stock row that changed.")
    batch_number: str | None = Field(description="Batch of that stock row.")
    quantity_change: Decimal = Field(description="Units added (positive) or removed.")
    reason: InventoryMovementReason = Field(description="Why the stock changed.")
    department_id: UUID | None = Field(description="Department that consumed it.")
    reference_type: str | None = Field(description="What caused it.")
    reference_id: UUID | None = Field(description="UUID of what caused it.")
    note: str | None = Field(description="Free-text reason.")
    moved_at: datetime = Field(description="When the stock changed (UTC).")
    moved_by: UUID | None = Field(description="User who caused the change.")

    @classmethod
    def from_model(cls, movement: InventoryMovement) -> Self:
        """Build the DTO from an :class:`~app.models.inventory.InventoryMovement`."""
        return cls(
            id=movement.id,
            item_id=movement.item_id,
            location_id=movement.location_id,
            stock_id=movement.stock_id,
            batch_number=movement.stock.batch_number,
            # A just-written movement still holds the value as submitted;
            # put it on the wire at the column's two places like every other.
            quantity_change=movement.quantity_change.quantize(_TWO_PLACES),
            reason=movement.reason,
            department_id=movement.department_id,
            reference_type=movement.reference_type,
            reference_id=movement.reference_id,
            note=movement.note,
            moved_at=movement.moved_at,
            moved_by=movement.moved_by,
        )


class StockChangeResponse(BaseModel):
    """The result of a consume, transfer or adjustment."""

    movements: list[MovementResponse] = Field(description="Ledger entries this request wrote.")
    summary: ItemStockSummaryResponse = Field(description="The item's stock afterwards.")


# ── Purchase orders ─────────────────────────────────────────────────────────


class InventoryPurchaseOrderLineRequest(BaseModel):
    """One item on a new purchase order."""

    model_config = ConfigDict(extra="forbid")

    item_id: UUID = Field(description="Item to order.")
    quantity: Quantity = Field(description="Units to order.")
    unit_price: Money = Field(description="Agreed purchase price per unit.")


class CreateInventoryPurchaseOrderRequest(BaseModel):
    """Payload for ``POST /api/v1/inventory/purchase-orders``. Created as a draft."""

    model_config = ConfigDict(extra="forbid")

    vendor_id: UUID = Field(description="Vendor to order from.")
    notes: str | None = Field(default=None, max_length=2000, description="Notes to the vendor.")
    items: list[InventoryPurchaseOrderLineRequest] = Field(
        min_length=1, max_length=MAX_LINES, description="Items to order."
    )

    @field_validator("notes")
    @classmethod
    def _trim_notes(cls, value: str | None) -> str | None:
        """Trim notes, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @field_validator("items")
    @classmethod
    def _check_unique(
        cls, value: list[InventoryPurchaseOrderLineRequest]
    ) -> list[InventoryPurchaseOrderLineRequest]:
        """An item appears once per order."""
        ids = [line.item_id for line in value]
        if len(set(ids)) != len(ids):
            msg = "Each item may appear only once in an order."
            raise ValueError(msg)
        return value


class InventoryReceiptLineRequest(BaseModel):
    """Units received against a purchase order line."""

    model_config = ConfigDict(extra="forbid")

    po_item_id: UUID = Field(description="The order line the units are for.")
    quantity: Quantity = Field(description="Units received.")
    batch_number: str | None = Field(
        default=None, max_length=50, description="Batch number. Required for a tracked item."
    )
    expiry_date: date | None = Field(default=None, description="Last day the batch may be used.")

    @field_validator("batch_number")
    @classmethod
    def _check_batch(cls, value: str | None) -> str | None:
        """Uppercase the batch number and restrict its characters."""
        return _optional_code(value, "Batch number")


class ReceiveInventoryPurchaseOrderRequest(BaseModel):
    """Payload for ``POST /api/v1/inventory/purchase-orders/{id}/receive``."""

    model_config = ConfigDict(extra="forbid")

    location_id: UUID = Field(description="Where the goods are received into.")
    items: list[InventoryReceiptLineRequest] = Field(
        min_length=1, max_length=MAX_LINES, description="What arrived."
    )

    @field_validator("items")
    @classmethod
    def _check_unique(
        cls, value: list[InventoryReceiptLineRequest]
    ) -> list[InventoryReceiptLineRequest]:
        """A batch is listed once per order line."""
        keys = [(line.po_item_id, line.batch_number) for line in value]
        if len(set(keys)) != len(keys):
            msg = "Each batch may be listed only once per order line."
            raise ValueError(msg)
        return value


class InventoryPurchaseOrderItemResponse(BaseModel):
    """One item on a purchase order."""

    id: UUID = Field(description="Order item UUID.")
    item_id: UUID = Field(description="Item ordered.")
    item_sku: str = Field(description="Item SKU.")
    item_name: str = Field(description="Item name.")
    quantity: Decimal = Field(description="Units ordered, as a decimal string.")
    unit_price: Decimal = Field(description="Purchase price per unit, as a decimal string.")
    total: Decimal = Field(description="Line total, as a decimal string.")


class InventoryPurchaseOrderResponse(BaseModel):
    """An inventory purchase order with its items."""

    id: UUID = Field(description="Purchase order UUID.")
    po_number: str = Field(description="Order number.")
    vendor_id: UUID = Field(description="Vendor UUID.")
    vendor_name: str = Field(description="Vendor name.")
    status: PurchaseOrderStatus = Field(description="draft, sent, received or cancelled.")
    notes: str | None = Field(description="Notes to the vendor.")
    ordered_at: datetime | None = Field(description="When it was sent (UTC).")
    received_at: datetime | None = Field(description="When the goods were received (UTC).")
    received_location_id: UUID | None = Field(description="Where the goods were received into.")
    total_amount: Decimal = Field(description="Sum of the lines, as a decimal string.")
    created_at: datetime = Field(description="When it was drafted (UTC).")
    items: list[InventoryPurchaseOrderItemResponse] = Field(description="Items ordered.")

    @classmethod
    def from_model(cls, order: InventoryPurchaseOrder) -> Self:
        """Build the DTO from an :class:`~app.models.inventory.InventoryPurchaseOrder`."""
        return cls(
            id=order.id,
            po_number=order.po_number,
            vendor_id=order.vendor_id,
            vendor_name=order.vendor.name,
            status=order.status,
            notes=order.notes,
            ordered_at=order.ordered_at,
            received_at=order.received_at,
            received_location_id=order.received_location_id,
            total_amount=sum((line.total for line in order.items), Decimal("0.00")),
            created_at=order.created_at,
            items=[
                InventoryPurchaseOrderItemResponse(
                    id=line.id,
                    item_id=line.item_id,
                    item_sku=line.item.sku,
                    item_name=line.item.name,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    total=line.total,
                )
                for line in order.items
            ],
        )
