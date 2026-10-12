"""Inventory models — items, locations, stock, the movement ledger, and
purchase orders for non-pharmacy consumables.

Columns follow ``docs/modules/09-inventory.md`` §8. Where the schema departs
from the spec's sketch, the reasons are given once, in migration 0016.

**Stock is a ledger plus a running total**, exactly as in Pharmacy. Every
change to a stock row is an :class:`InventoryMovement`; the row's ``quantity``
is the sum of its movements (AC-4), kept on the row so the database can refuse
to let it go negative. The two are only ever written together, by the
repository.

**A stock row is one batch of one item at one location** (business rule 1).
For an item that is not batch-tracked there is one row per location, with no
batch number.

Quantities are ``NUMERIC(12, 2)`` — some consumables are issued in fractions
of a unit of measure — and money is ``NUMERIC(15, 2)`` (CLAUDE.md rule 6).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for Mapped[uuid.UUID] resolution
from datetime import date, datetime  # noqa: TC003 — needed at runtime for Mapped[...]
from decimal import Decimal  # noqa: TC003 — needed at runtime for Mapped[Decimal] resolution
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CommonColumnsMixin, UUIDPrimaryKeyMixin
from app.models.pharmacy import PurchaseOrderStatus, Vendor

__all__ = [
    "InventoryItem",
    "InventoryLocation",
    "InventoryLocationKind",
    "InventoryMovement",
    "InventoryMovementReason",
    "InventoryPurchaseOrder",
    "InventoryPurchaseOrderItem",
    "InventoryStock",
]


class InventoryLocationKind(StrEnum):
    """What kind of place stock is kept in — ``inventory_location_kind``."""

    WARD = "ward"
    OT = "ot"
    ICU = "icu"
    STORE = "store"


class InventoryMovementReason(StrEnum):
    """Why stock changed — the ``inventory_movement_reason`` enum."""

    RECEIVED = "received"
    CONSUMED = "consumed"
    TRANSFERRED_IN = "transferred_in"
    TRANSFERRED_OUT = "transferred_out"
    ADJUSTED = "adjusted"
    EXPIRED = "expired"


def _enum(enum_class: type[StrEnum], name: str) -> SQLEnum:
    """Map a ``StrEnum`` onto an existing Postgres enum by its values."""
    return SQLEnum(
        enum_class,
        name=name,
        create_type=False,
        values_callable=lambda e: [m.value for m in e],
    )


def _tenant() -> Mapped[uuid.UUID]:
    """The ``hospital_id`` column every inventory table carries."""
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="UUID of the hospital (tenant) this row belongs to.",
    )


def _ref(
    target: str, comment: str, *, nullable: bool = False, ondelete: str = "RESTRICT"
) -> Mapped[uuid.UUID]:
    """A UUID foreign key column."""
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        comment=comment,
    )


class InventoryItem(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One consumable or small piece of equipment the hospital stocks."""

    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint("hospital_id", "sku", name="uq_inventory_items_hospital_sku"),
        CheckConstraint(
            "reorder_point IS NULL OR reorder_point >= 0", name="reorder_point_non_negative"
        ),
        CheckConstraint(
            "target_stock IS NULL OR target_stock >= 0", name="target_stock_non_negative"
        ),
        Index("ix_inventory_items_hospital_name", "hospital_id", "name"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    sku: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Stock-keeping code, unique per hospital."
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="Display name.")
    category: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="Free-form grouping, e.g. 'Disposables'."
    )
    unit_of_measure: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="unit",
        server_default=text("'unit'"),
        comment="What one unit is, e.g. 'box of 100'.",
    )
    is_batch_tracked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="Whether stock is kept per batch with an expiry (FR-2).",
    )
    reorder_point: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Hospital-wide stock at or below which it is low."
    )
    target_stock: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Stock level a reorder should bring it back to."
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Inactive items cannot be received, ordered or consumed.",
    )

    def __repr__(self) -> str:
        return f"<InventoryItem id={self.id!s:.8} sku={self.sku!r}>"


class InventoryLocation(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """A place stock is kept: a ward, a theatre, an ICU, a store (FR-1)."""

    __tablename__ = "inventory_locations"
    __table_args__ = (
        UniqueConstraint("hospital_id", "code", name="uq_inventory_locations_hospital_code"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="Display name.")
    code: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Short code, unique per hospital."
    )
    kind: Mapped[InventoryLocationKind] = mapped_column(
        _enum(InventoryLocationKind, "inventory_location_kind"),
        nullable=False,
        default=InventoryLocationKind.STORE,
        comment="ward, ot, icu or store.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Inactive locations cannot receive stock.",
    )

    def __repr__(self) -> str:
        return f"<InventoryLocation id={self.id!s:.8} code={self.code!r}>"


class InventoryStock(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """How much of one batch of one item is at one location (rule 1)."""

    __tablename__ = "inventory_stock"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        Index(
            "uq_inventory_stock_item_location_batch",
            "item_id",
            "location_id",
            text("coalesce(batch_number, '')"),
            unique=True,
        ),
        Index("ix_inventory_stock_hospital_location", "hospital_id", "location_id"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    item_id: Mapped[uuid.UUID] = _ref("inventory_items.id", "Item held.")
    location_id: Mapped[uuid.UUID] = _ref("inventory_locations.id", "Where it is held.")
    batch_number: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Batch number; NULL for an untracked item."
    )
    expiry_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="Last day the batch may be used."
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0"),
        server_default=text("0"),
        comment="Units held: the sum of the row's movements.",
    )

    item: Mapped[InventoryItem] = relationship(
        "InventoryItem", foreign_keys="InventoryStock.item_id", lazy="joined"
    )
    location: Mapped[InventoryLocation] = relationship(
        "InventoryLocation", foreign_keys="InventoryStock.location_id", lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<InventoryStock id={self.id!s:.8} quantity={self.quantity}>"


class InventoryMovement(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One change to a stock row. Append-only: the ledger of record (AC-4)."""

    __tablename__ = "inventory_movements"
    __table_args__ = (
        CheckConstraint("quantity_change <> 0", name="quantity_change_non_zero"),
        Index("ix_inventory_movements_stock", "stock_id", "moved_at"),
        Index("ix_inventory_movements_item", "hospital_id", "item_id", "moved_at"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    item_id: Mapped[uuid.UUID] = _ref("inventory_items.id", "Item moved.")
    location_id: Mapped[uuid.UUID] = _ref("inventory_locations.id", "Where it moved.")
    stock_id: Mapped[uuid.UUID] = _ref("inventory_stock.id", "Stock row that changed.")
    quantity_change: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, comment="Units added (positive) or removed (negative)."
    )
    reason: Mapped[InventoryMovementReason] = mapped_column(
        _enum(InventoryMovementReason, "inventory_movement_reason"),
        nullable=False,
        comment="Why the stock changed.",
    )
    department_id: Mapped[uuid.UUID | None] = _ref(
        "departments.id", "Department that consumed it (rule 2).", nullable=True
    )
    reference_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="What caused it, e.g. 'purchase_order'."
    )
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="UUID of what caused it."
    )
    note: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Why, for an adjustment."
    )
    moved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When the stock changed (UTC)."
    )
    moved_by: Mapped[uuid.UUID | None] = _ref(
        "users.id", "User who caused the change.", nullable=True
    )

    stock: Mapped[InventoryStock] = relationship(
        "InventoryStock", foreign_keys="InventoryMovement.stock_id", lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<InventoryMovement id={self.id!s:.8} change={self.quantity_change:+}>"


class InventoryPurchaseOrder(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """An order placed with a vendor for inventory items (FR-4)."""

    __tablename__ = "inventory_purchase_orders"
    __table_args__ = (
        UniqueConstraint(
            "hospital_id", "po_number", name="uq_inventory_purchase_orders_hospital_number"
        ),
        Index("ix_inventory_purchase_orders_hospital_status", "hospital_id", "status"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    vendor_id: Mapped[uuid.UUID] = _ref("vendors.id", "Vendor the order is placed with.")
    po_number: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Order number, unique per hospital."
    )
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        _enum(PurchaseOrderStatus, "purchase_order_status"),
        nullable=False,
        default=PurchaseOrderStatus.DRAFT,
        comment="draft, sent, received or cancelled.",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Notes to the vendor.")
    ordered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When it was sent (UTC)."
    )
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the goods were received (UTC)."
    )
    received_by: Mapped[uuid.UUID | None] = _ref(
        "users.id", "User who received the goods.", nullable=True
    )
    received_location_id: Mapped[uuid.UUID | None] = _ref(
        "inventory_locations.id", "Where the goods were received into.", nullable=True
    )

    items: Mapped[list[InventoryPurchaseOrderItem]] = relationship(
        "InventoryPurchaseOrderItem",
        foreign_keys="InventoryPurchaseOrderItem.po_id",
        order_by="InventoryPurchaseOrderItem.position",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    vendor: Mapped[Vendor] = relationship(
        Vendor, foreign_keys="InventoryPurchaseOrder.vendor_id", lazy="joined"
    )

    def __repr__(self) -> str:
        return (
            f"<InventoryPurchaseOrder id={self.id!s:.8} number={self.po_number!r} "
            f"status={self.status}>"
        )


class InventoryPurchaseOrderItem(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One item on an inventory purchase order."""

    __tablename__ = "inventory_po_items"
    __table_args__ = (
        UniqueConstraint("po_id", "item_id", name="uq_inventory_po_items_po_item"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_inventory_po_items_po", "po_id", "position"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    po_id: Mapped[uuid.UUID] = _ref(
        "inventory_purchase_orders.id", "Order this line belongs to.", ondelete="CASCADE"
    )
    item_id: Mapped[uuid.UUID] = _ref("inventory_items.id", "Item ordered.")
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="Display order."
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, comment="Units ordered."
    )
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Agreed purchase price per unit."
    )
    total: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="quantity x unit_price."
    )

    item: Mapped[InventoryItem] = relationship(
        "InventoryItem", foreign_keys="InventoryPurchaseOrderItem.item_id", lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<InventoryPurchaseOrderItem id={self.id!s:.8} quantity={self.quantity}>"
