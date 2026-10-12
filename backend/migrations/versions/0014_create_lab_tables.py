"""create lab tables

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-05 10:00:00.000000

Creates the Laboratory module's tables (``docs/modules/07-laboratory.md`` §8):
``tests_catalog``, ``lab_orders``, ``lab_order_items`` and
``lab_result_amendments``.

Where this departs from the spec's sketch, and why:

1. **``hospital_id`` on items and amendments.** The spec lists it only on the
   catalog and the order. CLAUDE.md rules 4 and 5 require every tenant table
   to carry it and be filtered on it directly — the same reason
   ``invoice_items`` has one.
2. **No ``consultation_id`` on ``lab_orders``.** There is no ``consultations``
   table to reference. Orders hang off the appointment (business rule 1 allows
   either); the column is added when the Consultation module exists.
3. **``tests_catalog.result_type``.** §11 says a result "matches the test's
   expected type (numeric or text)", which needs somewhere to record the type.
4. **Snapshot columns on ``lab_order_items``** — test code, name, unit and the
   reference bounds that applied. A result must keep meaning what it meant
   when it was reported, even if the catalog entry is edited afterwards.
5. **Order-level ``collected_at`` / ``results_entered_at`` / ``released_at`` /
   ``released_by``, ``cancel_reason`` and ``invoice_id``** — turnaround time
   (rule 6) is measured order to release, a cancellation carries a reason, and
   the order remembers the draft invoice its tests were charged to.
6. **``previous_flag`` / ``new_flag`` on amendments**, so a correction that
   moves a value across a range boundary records both sides.

Sample ids are unique per hospital (rule 5) through a partial unique index:
an item has no sample id until its sample is collected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS: dict[str, tuple[str, ...]] = {
    "lab_result_type": ("numeric", "text"),
    "lab_order_priority": ("routine", "urgent", "stat"),
    "lab_order_status": (
        "ordered",
        "collected",
        "in_progress",
        "results_entered",
        "released",
        "cancelled",
    ),
    "lab_result_flag": ("normal", "low", "high", "critical"),
}


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

    result_type = postgresql.ENUM(name="lab_result_type", create_type=False)
    priority = postgresql.ENUM(name="lab_order_priority", create_type=False)
    order_status = postgresql.ENUM(name="lab_order_status", create_type=False)
    result_flag = postgresql.ENUM(name="lab_result_flag", create_type=False)

    # ── tests_catalog ────────────────────────────────────────────────────────
    op.create_table(
        "tests_catalog",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("unit", sa.String(20), nullable=True),
        sa.Column("result_type", result_type, nullable=False, server_default="numeric"),
        # Array of {sex, age_min, age_max, low, high, critical_low, critical_high}.
        sa.Column(
            "reference_ranges",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("turnaround_hours", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(15, 2), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_audit_columns(),
        sa.UniqueConstraint("hospital_id", "code", name="uq_tests_catalog_hospital_code"),
    )
    op.create_check_constraint("price_non_negative", "tests_catalog", sa.text("price >= 0"))
    op.create_check_constraint(
        "turnaround_positive",
        "tests_catalog",
        sa.text("turnaround_hours IS NULL OR turnaround_hours > 0"),
    )
    op.create_index(
        "ix_tests_catalog_hospital_category", "tests_catalog", ["hospital_id", "category"]
    )

    # ── lab_orders ───────────────────────────────────────────────────────────
    op.create_table(
        "lab_orders",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("patient_id", "patients.id"),
        _fk("doctor_id", "doctors.id"),
        _fk("appointment_id", "appointments.id", nullable=True),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("priority", priority, nullable=False, server_default="routine"),
        sa.Column("status", order_status, nullable=False, server_default="ordered"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("results_entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        _fk("released_by", "users.id", nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(500), nullable=True),
        # The draft invoice the tests were charged to. SET NULL: an order
        # outlives the bookkeeping that points at it.
        _fk("invoice_id", "invoices.id", nullable=True, ondelete="SET NULL"),
        *_audit_columns(),
    )
    op.create_check_constraint(
        "released_has_time",
        "lab_orders",
        sa.text("status <> 'released' OR released_at IS NOT NULL"),
    )
    op.create_check_constraint(
        "cancelled_has_reason",
        "lab_orders",
        sa.text("status <> 'cancelled' OR cancel_reason IS NOT NULL"),
    )
    op.create_index(
        "ix_lab_orders_hospital_status", "lab_orders", ["hospital_id", "status", "ordered_at"]
    )
    op.create_index(
        "ix_lab_orders_patient", "lab_orders", ["hospital_id", "patient_id", "ordered_at"]
    )
    op.create_index(
        "ix_lab_orders_doctor", "lab_orders", ["hospital_id", "doctor_id", "ordered_at"]
    )
    op.create_index("ix_lab_orders_appointment", "lab_orders", ["appointment_id"])

    # ── lab_order_items ──────────────────────────────────────────────────────
    op.create_table(
        "lab_order_items",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("lab_order_id", "lab_orders.id", ondelete="CASCADE"),
        _fk("test_id", "tests_catalog.id"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        # Snapshots of the catalog entry at the time of ordering.
        sa.Column("test_code", sa.String(50), nullable=False),
        sa.Column("test_name", sa.String(200), nullable=False),
        sa.Column("result_type", result_type, nullable=False),
        sa.Column("price", sa.Numeric(15, 2), nullable=False),
        sa.Column("sample_id", sa.String(50), nullable=True),
        sa.Column("sample_collected_at", sa.DateTime(timezone=True), nullable=True),
        _fk("sample_collected_by", "users.id", nullable=True),
        sa.Column("result_value", sa.Text(), nullable=True),
        sa.Column("result_unit", sa.String(20), nullable=True),
        sa.Column("result_flag", result_flag, nullable=True),
        # The bounds the flag was computed against, for the patient's sex and
        # their age when the sample was taken (§14).
        sa.Column("reference_low", sa.Numeric(15, 4), nullable=True),
        sa.Column("reference_high", sa.Numeric(15, 4), nullable=True),
        sa.Column("result_entered_at", sa.DateTime(timezone=True), nullable=True),
        _fk("result_entered_by", "users.id", nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        _fk("released_by", "users.id", nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_audit_columns(),
        sa.UniqueConstraint("lab_order_id", "test_id", name="uq_lab_order_items_order_test"),
    )
    op.create_check_constraint("price_non_negative", "lab_order_items", sa.text("price >= 0"))
    op.create_index(
        "uq_lab_order_items_hospital_sample",
        "lab_order_items",
        ["hospital_id", "sample_id"],
        unique=True,
        postgresql_where=sa.text("sample_id IS NOT NULL"),
    )
    op.create_index("ix_lab_order_items_order", "lab_order_items", ["lab_order_id", "position"])

    # ── lab_result_amendments ────────────────────────────────────────────────
    op.create_table(
        "lab_result_amendments",
        _uuid_pk(),
        _fk("hospital_id", "hospitals.id"),
        _fk("item_id", "lab_order_items.id", ondelete="CASCADE"),
        sa.Column("previous_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("previous_flag", result_flag, nullable=True),
        sa.Column("new_flag", result_flag, nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        _fk("amended_by", "users.id"),
        sa.Column("amended_at", sa.DateTime(timezone=True), nullable=False),
        *_audit_columns(),
    )
    op.create_index(
        "ix_lab_result_amendments_item", "lab_result_amendments", ["item_id", "amended_at"]
    )


def downgrade() -> None:
    """Rollback — drop the lab tables and their enum types."""
    op.drop_table("lab_result_amendments")
    op.drop_table("lab_order_items")
    op.drop_table("lab_orders")
    op.drop_table("tests_catalog")
    bind = op.get_bind()
    for name in reversed(_ENUMS):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
