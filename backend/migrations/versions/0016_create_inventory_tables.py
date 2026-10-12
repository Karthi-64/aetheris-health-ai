"""create inventory tables

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-05 22:00:00.000000

Creates the Inventory module's tables (``docs/modules/09-inventory.md`` §8):
``inventory_items``, ``inventory_locations``, ``inventory_stock``,
``inventory_movements``, ``inventory_purchase_orders`` and
``inventory_po_items``. Vendors are not created here: the ``vendors`` table
from migration 0015 is shared with Pharmacy by design (§20).

Where this departs from the spec's sketch, and why:

1. **``hospital_id`` on every table.** The spec lists it on two. CLAUDE.md
   rules 4 and 5 require every tenant table to carry it and be filtered on it
   directly.
2. **``inventory_items.is_active``**, so an item can be retired without
   deleting the history that refers to it.
3. **A ``>= 0`` check on ``inventory_stock.quantity``** (§11: "quantity ≥ 0
   after any movement") and one stock row per (item, location, batch). The
   uniqueness is an expression index over ``coalesce(batch_number, '')``,
   because a plain unique constraint treats two NULL batch numbers as
   different and would allow duplicate rows for an item that is not
   batch-tracked.
4. **``inventory_movements.stock_id``** in place of the sketch's ``batch_id``
   (there is no separate batches table: the stock row *is* the batch at a
   location), plus ``department_id`` for consumption by department (rule 2,
   FR-6) and ``note`` for the reason an adjustment requires (§11).
5. **Purchase-order tables of its own.** The spec allows either; Pharmacy's
   order lines point at medicines and its receipts create medicine batches,
   so sharing would mean nullable foreign keys both ways. The
   ``purchase_order_status`` enum from migration 0015 is reused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS: dict[str, tuple[str, ...]] = {
    "inventory_location_kind": ("ward", "ot", "icu", "store"),
    "inventory_movement_reason": (
        "received",
        "consumed",
        "transferred_in",
        "transferred_out",
        "adjusted",
        "expired",
    ),
}

_TABLES = (
    "inventory_po_items",
    "inventory_purchase_orders",
    "inventory_movements",
    "inventory_stock",
    "inventory_locations",
    "inventory_items",
)


def _uuid_pk() -> sa.Column[object]:
    """Return the standard UUID primary key column."""
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _fk(
    name: str, target: str, *, nullable: bool = False, ondelete: str = "RESTRICT"
) -> sa.Column[object]:
    """Return a UUID foreign key column."""
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
    )


def _audit_columns() -> list[sa.Column[object]]:
    """Return the audit and soft-delete columns every business table carries."""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _fk("created_by", "users.id", nullable=True, ondelete="SET NULL"),
        _fk("updated_by", "users.id", nullable=True, ondelete="SET NULL"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        _fk("deleted_by", "users.id", nullable=True, ondelete="SET NULL"),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    location_kind = postgresql.ENUM(name="inventory_location_kind", create_type=False)
    movement_reason = postgresql.ENUM(name="inventory_movement_reason", create_type=False)
    # Created by migration 0015 for Pharmacy; reused, not recreated.
    po_status = postgresql.ENUM(name="purchase_order_status", create_type=False)

    # ── inventory_items ──────────────────────────────────────────────────────
    op.create_table(
        "inventory_items",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        sa.Column("sku", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("unit_of_measure", sa.String(30), nullable=False, server_default="unit"),
        sa.Column(
            "is_batch_tracked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("reorder_point", sa.Integer(), nullable=True),
        sa.Column("target_stock", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_audit_columns(),
        sa.UniqueConstraint("hospital_id", "sku", name="uq_inventory_items_hospital_sku"),
    )
    op.create_check_constraint(
        "reorder_point_non_negative",
        "inventory_items",
        sa.text("reorder_point IS NULL OR reorder_point >= 0"),
    )
    op.create_check_constraint(
        "target_stock_non_negative",
        "inventory_items",
        sa.text("target_stock IS NULL OR target_stock >= 0"),
    )
    op.create_index("ix_inventory_items_hospital_name", "inventory_items", ["hospital_id", "name"])

    # ── inventory_locations ──────────────────────────────────────────────────
    op.create_table(
        "inventory_locations",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("kind", location_kind, nullable=False, server_default="store"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_audit_columns(),
        sa.UniqueConstraint("hospital_id", "code", name="uq_inventory_locations_hospital_code"),
    )

    # ── inventory_stock ──────────────────────────────────────────────────────
    op.create_table(
        "inventory_stock",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("item_id", "inventory_items.id"),
        _fk("location_id", "inventory_locations.id"),
        sa.Column("batch_number", sa.String(50), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False, server_default="0"),
        *_audit_columns(),
    )
    op.create_check_constraint("quantity_non_negative", "inventory_stock", sa.text("quantity >= 0"))
    op.create_index(
        "uq_inventory_stock_item_location_batch",
        "inventory_stock",
        ["item_id", "location_id", sa.text("coalesce(batch_number, '')")],
        unique=True,
    )
    op.create_index(
        "ix_inventory_stock_hospital_location", "inventory_stock", ["hospital_id", "location_id"]
    )

    # ── inventory_movements ──────────────────────────────────────────────────
    op.create_table(
        "inventory_movements",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("item_id", "inventory_items.id"),
        _fk("location_id", "inventory_locations.id"),
        _fk("stock_id", "inventory_stock.id"),
        sa.Column("quantity_change", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", movement_reason, nullable=False),
        _fk("department_id", "departments.id", nullable=True),
        sa.Column("reference_type", sa.String(50), nullable=True),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("moved_at", sa.DateTime(timezone=True), nullable=False),
        _fk("moved_by", "users.id", nullable=True),
        *_audit_columns(),
    )
    op.create_check_constraint(
        "quantity_change_non_zero", "inventory_movements", sa.text("quantity_change <> 0")
    )
    op.create_index("ix_inventory_movements_stock", "inventory_movements", ["stock_id", "moved_at"])
    op.create_index(
        "ix_inventory_movements_item",
        "inventory_movements",
        ["hospital_id", "item_id", "moved_at"],
    )

    # ── inventory_purchase_orders ────────────────────────────────────────────
    op.create_table(
        "inventory_purchase_orders",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("vendor_id", "vendors.id"),
        sa.Column("po_number", sa.String(50), nullable=False),
        sa.Column("status", po_status, nullable=False, server_default="draft"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        _fk("received_by", "users.id", nullable=True),
        _fk("received_location_id", "inventory_locations.id", nullable=True),
        *_audit_columns(),
        sa.UniqueConstraint(
            "hospital_id", "po_number", name="uq_inventory_purchase_orders_hospital_number"
        ),
    )
    op.create_index(
        "ix_inventory_purchase_orders_hospital_status",
        "inventory_purchase_orders",
        ["hospital_id", "status"],
    )

    op.create_table(
        "inventory_po_items",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("po_id", "inventory_purchase_orders.id", ondelete="CASCADE"),
        _fk("item_id", "inventory_items.id"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False),
        sa.Column("unit_price", sa.Numeric(15, 2), nullable=False),
        sa.Column("total", sa.Numeric(15, 2), nullable=False),
        *_audit_columns(),
        sa.UniqueConstraint("po_id", "item_id", name="uq_inventory_po_items_po_item"),
    )
    op.create_check_constraint("quantity_positive", "inventory_po_items", sa.text("quantity > 0"))
    op.create_index("ix_inventory_po_items_po", "inventory_po_items", ["po_id", "position"])


def downgrade() -> None:
    """Rollback — drop the inventory tables and their enum types.

    ``purchase_order_status`` belongs to migration 0015 and is left alone.
    """
    for table in _TABLES:
        op.drop_table(table)
    bind = op.get_bind()
    for name in reversed(_ENUMS):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
