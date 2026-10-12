"""create pharmacy tables

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-05 16:00:00.000000

Creates the Pharmacy module's tables (``docs/modules/08-pharmacy.md`` §8):
``vendors``, ``medicines``, ``medicine_batches``, ``stock_movements``,
``prescriptions``, ``prescription_items``, ``dispenses``, ``dispense_items``,
``purchase_orders`` and ``po_items``. ``drug_interactions`` is not created:
interaction warnings are out of this slice.

Where this departs from the spec's sketch, and why:

1. **``hospital_id`` on every table.** The spec lists it on four. CLAUDE.md
   rules 4 and 5 require every tenant table to carry it and be filtered on it
   directly.
2. **Prescriptions live here and hang off the appointment.** The spec says
   they are "already in Consultation"; there is no Consultation module. A
   prescription is a header plus items (the database design's single
   ``prescriptions`` table is one row per medicine with no quantity, which
   cannot express "dispense quantity ≤ prescribed quantity", §11). A
   ``consultation_id`` is added when that module exists.
3. **``medicines.sku`` and ``medicines.unit_price``.** §2 says the catalog has
   a SKU, and a dispense line has a unit price that must come from somewhere.
4. **``medicine_batches.quantity_on_hand``** with a ``>= 0`` check. The spec
   computes stock as the sum of movements and, for two pharmacists dispensing
   the last unit at once, says a "DB constraint on stock movement sum wins
   one" (§14). A check constraint cannot span rows, so the running sum is kept
   on the batch — written only together with a movement — and that column
   carries the constraint. ``stock_movements`` stays the ledger of record.
5. **``medicine_batches.is_recalled``**, so a recalled batch can be blocked
   (§14). No ``location_id``: there is no locations table yet.
6. **``prescription_items.quantity`` / ``quantity_dispensed``** and
   ``prescriptions.status``, for partial dispensing (rule 5).
7. **``dispenses.invoice_id``**: the draft invoice the dispense was charged to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS: dict[str, tuple[str, ...]] = {
    "stock_movement_reason": ("received", "dispensed", "adjusted", "expired"),
    "prescription_status": ("active", "partially_dispensed", "dispensed", "cancelled"),
    "purchase_order_status": ("draft", "sent", "received", "cancelled"),
}

_TABLES = (
    "po_items",
    "purchase_orders",
    "dispense_items",
    "dispenses",
    "prescription_items",
    "prescriptions",
    "stock_movements",
    "medicine_batches",
    "medicines",
    "vendors",
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


def _money(name: str, *, nullable: bool = False) -> sa.Column[object]:
    """Return a ``NUMERIC(15, 2)`` money column (CLAUDE.md rule 6)."""
    return sa.Column(name, sa.Numeric(15, 2), nullable=nullable)


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

    movement_reason = postgresql.ENUM(name="stock_movement_reason", create_type=False)
    prescription_status = postgresql.ENUM(name="prescription_status", create_type=False)
    po_status = postgresql.ENUM(name="purchase_order_status", create_type=False)

    # ── vendors ──────────────────────────────────────────────────────────────
    op.create_table(
        "vendors",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("contact", sa.String(200), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("tax_id", sa.String(50), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_audit_columns(),
        sa.UniqueConstraint("hospital_id", "name", name="uq_vendors_hospital_name"),
    )

    # ── medicines ────────────────────────────────────────────────────────────
    op.create_table(
        "medicines",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        sa.Column("sku", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("generic_name", sa.String(200), nullable=True),
        sa.Column("strength", sa.String(50), nullable=True),
        sa.Column("form", sa.String(50), nullable=True),
        sa.Column("atc_code", sa.String(20), nullable=True),
        _money("unit_price"),
        sa.Column(
            "requires_prescription", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_audit_columns(),
        sa.UniqueConstraint("hospital_id", "sku", name="uq_medicines_hospital_sku"),
    )
    op.create_check_constraint("unit_price_non_negative", "medicines", sa.text("unit_price >= 0"))
    op.create_index("ix_medicines_hospital_name", "medicines", ["hospital_id", "name"])

    # ── medicine_batches ─────────────────────────────────────────────────────
    op.create_table(
        "medicine_batches",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("medicine_id", "medicines.id"),
        sa.Column("batch_number", sa.String(50), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        _money("cost_per_unit"),
        sa.Column("initial_quantity", sa.Integer(), nullable=False),
        sa.Column("quantity_on_hand", sa.Integer(), nullable=False),
        sa.Column("is_recalled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        *_audit_columns(),
        sa.UniqueConstraint(
            "medicine_id", "batch_number", name="uq_medicine_batches_medicine_batch"
        ),
    )
    op.create_check_constraint(
        "quantity_on_hand_non_negative", "medicine_batches", sa.text("quantity_on_hand >= 0")
    )
    op.create_check_constraint(
        "initial_quantity_non_negative", "medicine_batches", sa.text("initial_quantity >= 0")
    )
    op.create_check_constraint(
        "cost_non_negative", "medicine_batches", sa.text("cost_per_unit >= 0")
    )
    # FIFO by expiry: the dispense query walks this index.
    op.create_index(
        "ix_medicine_batches_fifo",
        "medicine_batches",
        ["hospital_id", "medicine_id", "expiry_date"],
    )

    # ── stock_movements ──────────────────────────────────────────────────────
    op.create_table(
        "stock_movements",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("batch_id", "medicine_batches.id"),
        sa.Column("quantity_change", sa.Integer(), nullable=False),
        sa.Column("reason", movement_reason, nullable=False),
        sa.Column("reference_type", sa.String(50), nullable=True),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("moved_at", sa.DateTime(timezone=True), nullable=False),
        _fk("moved_by", "users.id", nullable=True),
        *_audit_columns(),
    )
    op.create_check_constraint(
        "quantity_change_non_zero", "stock_movements", sa.text("quantity_change <> 0")
    )
    op.create_index("ix_stock_movements_batch", "stock_movements", ["batch_id", "moved_at"])
    op.create_index(
        "ix_stock_movements_reference", "stock_movements", ["reference_type", "reference_id"]
    )

    # ── prescriptions ────────────────────────────────────────────────────────
    op.create_table(
        "prescriptions",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("appointment_id", "appointments.id"),
        _fk("patient_id", "patients.id"),
        _fk("doctor_id", "doctors.id"),
        sa.Column("status", prescription_status, nullable=False, server_default="active"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("prescribed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(500), nullable=True),
        *_audit_columns(),
    )
    op.create_index(
        "ix_prescriptions_hospital_status",
        "prescriptions",
        ["hospital_id", "status", "prescribed_at"],
    )
    op.create_index(
        "ix_prescriptions_patient", "prescriptions", ["hospital_id", "patient_id", "prescribed_at"]
    )
    op.create_index("ix_prescriptions_appointment", "prescriptions", ["appointment_id"])

    op.create_table(
        "prescription_items",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("prescription_id", "prescriptions.id", ondelete="CASCADE"),
        # NULL for a free-text line the pharmacy does not stock.
        _fk("medicine_id", "medicines.id", nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("medicine_name", sa.String(200), nullable=False),
        sa.Column("dosage", sa.String(100), nullable=False),
        sa.Column("frequency", sa.String(100), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("quantity_dispensed", sa.Integer(), nullable=False, server_default="0"),
        *_audit_columns(),
    )
    op.create_check_constraint("quantity_positive", "prescription_items", sa.text("quantity > 0"))
    op.create_check_constraint(
        "dispensed_within_prescribed",
        "prescription_items",
        sa.text("quantity_dispensed >= 0 AND quantity_dispensed <= quantity"),
    )
    op.create_index(
        "ix_prescription_items_prescription", "prescription_items", ["prescription_id", "position"]
    )

    # ── dispenses ────────────────────────────────────────────────────────────
    op.create_table(
        "dispenses",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("prescription_id", "prescriptions.id"),
        sa.Column("dispensed_at", sa.DateTime(timezone=True), nullable=False),
        _fk("dispensed_by", "users.id", nullable=True),
        _money("total_amount"),
        sa.Column("notes", sa.Text(), nullable=True),
        _fk("invoice_id", "invoices.id", nullable=True, ondelete="SET NULL"),
        *_audit_columns(),
    )
    op.create_index("ix_dispenses_prescription", "dispenses", ["prescription_id", "dispensed_at"])

    op.create_table(
        "dispense_items",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("dispense_id", "dispenses.id", ondelete="CASCADE"),
        _fk("prescription_item_id", "prescription_items.id"),
        _fk("medicine_id", "medicines.id"),
        _fk("batch_id", "medicine_batches.id"),
        sa.Column("quantity", sa.Integer(), nullable=False),
        _money("unit_price"),
        _money("total"),
        *_audit_columns(),
    )
    op.create_check_constraint("quantity_positive", "dispense_items", sa.text("quantity > 0"))
    op.create_index("ix_dispense_items_dispense", "dispense_items", ["dispense_id"])

    # ── purchase_orders ──────────────────────────────────────────────────────
    op.create_table(
        "purchase_orders",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("vendor_id", "vendors.id"),
        sa.Column("po_number", sa.String(50), nullable=False),
        sa.Column("status", po_status, nullable=False, server_default="draft"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        _fk("received_by", "users.id", nullable=True),
        *_audit_columns(),
        sa.UniqueConstraint("hospital_id", "po_number", name="uq_purchase_orders_hospital_number"),
    )
    op.create_index(
        "ix_purchase_orders_hospital_status", "purchase_orders", ["hospital_id", "status"]
    )

    op.create_table(
        "po_items",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("po_id", "purchase_orders.id", ondelete="CASCADE"),
        _fk("medicine_id", "medicines.id"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quantity", sa.Integer(), nullable=False),
        _money("unit_price"),
        _money("total"),
        *_audit_columns(),
        sa.UniqueConstraint("po_id", "medicine_id", name="uq_po_items_po_medicine"),
    )
    op.create_check_constraint("quantity_positive", "po_items", sa.text("quantity > 0"))
    op.create_index("ix_po_items_po", "po_items", ["po_id", "position"])


def downgrade() -> None:
    """Rollback — drop the pharmacy tables and their enum types."""
    for table in _TABLES:
        op.drop_table(table)
    bind = op.get_bind()
    for name in reversed(_ENUMS):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
