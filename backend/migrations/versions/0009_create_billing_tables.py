"""create billing tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01 10:00:00.000000

Creates the billing schema (``docs/05-DATABASE_DESIGN.md`` §2.16–2.19,
``docs/modules/06-billing.md`` §8):

- ``invoice_status`` / ``payment_method`` enum types
- ``services`` — the catalog of billable items
- ``invoice_number_sequences`` — the per-hospital, gap-free invoice counter
- ``invoices`` and ``invoice_items`` — the invoice and its lines
- ``payments`` — money received against an invoice

Five points where this departs from the database design, each to satisfy a
rule the docs also state.

**``invoices.invoice_number`` is nullable.** §2.17 says ``NOT NULL``, but module
spec §5.1 says a draft has no number — it is allocated at issue so that an
abandoned draft cannot leave a gap in a series that must be gap-free (business
rule 2). ``number_required_once_issued`` keeps the stricter rule where it
applies: every non-draft invoice has a number.

**``invoice_items`` and ``payments`` carry ``hospital_id``.** Neither does in
§2.18–2.19, but CLAUDE.md rule 4 requires it on every table holding tenant
data, and rule 5 requires every query to filter on it. Same reasoning as
``appointment_status_history`` in migration 0008.

**Payment idempotency is unique per hospital**, not globally as §2.19 and the
spec's ``uq_payments_idempotency`` have it. A global index would let one
tenant's key collide with another's, and would make "is this key taken" a
cross-tenant signal. Matches ``uq_appointments_hospital_idempotency_key``.

**``invoices.void_reason`` and ``invoice_items.position`` are additions.** Spec
§5.6 requires a reason on void and the design has nowhere to keep it; and an
invoice's lines need a stable order that survives an edit, which ``id`` alone
does not give.

**One live invoice per appointment.** ``uq_invoices_live_appointment`` is what
makes the draft-on-completion hook idempotent: a second completion event, or a
manual draft racing the automatic one, cannot produce two invoices for one
visit. Void invoices are excluded so "void and re-issue" (business rule 3) still
works.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[object]:
    """Return the standard UUID primary-key column."""
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _hospital_fk() -> sa.Column[object]:
    """Return the standard tenant column."""
    return sa.Column(
        "hospital_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
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
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "deleted_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    ]


def upgrade() -> None:
    # ── Enums ──────────────────────────────────────────────────────────────────
    # Created explicitly rather than inline so they drop cleanly on downgrade
    # and a re-run after a partial failure is not blocked.
    invoice_status = postgresql.ENUM(
        "draft",
        "issued",
        "partially_paid",
        "paid",
        "void",
        "refunded",
        name="invoice_status",
        create_type=False,
    )
    invoice_status.create(op.get_bind(), checkfirst=True)

    payment_method = postgresql.ENUM(
        "cash",
        "card",
        "upi",
        "bank_transfer",
        "insurance",
        name="payment_method",
        create_type=False,
    )
    payment_method.create(op.get_bind(), checkfirst=True)

    # ── services ───────────────────────────────────────────────────────────────
    op.create_table(
        "services",
        _uuid_pk(),
        _hospital_fk(),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("price", sa.Numeric(15, 2), nullable=False),
        sa.Column("taxable", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        *_audit_columns(),
    )
    op.create_unique_constraint("uq_services_hospital_code", "services", ["hospital_id", "code"])
    op.create_check_constraint("price_non_negative", "services", sa.text("price >= 0"))
    op.create_index("ix_services_hospital_category", "services", ["hospital_id", "category"])

    # ── invoice_number_sequences ───────────────────────────────────────────────
    # One row per hospital, advanced under SELECT ... FOR UPDATE inside the
    # issue transaction (module spec §8) — the same shape as mrn_sequences.
    op.create_table(
        "invoice_number_sequences",
        sa.Column(
            "hospital_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hospitals.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("current_value", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "format_template",
            sa.String(50),
            nullable=False,
            server_default="INV-{year}-{seq:06d}",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ── invoices ───────────────────────────────────────────────────────────────
    op.create_table(
        "invoices",
        _uuid_pk(),
        _hospital_fk(),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("patients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "appointment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("appointments.id", ondelete="RESTRICT"),
            # NULL for ad-hoc billing not tied to a visit (§2.17).
            nullable=True,
        ),
        # NULL while draft — see the module docstring.
        sa.Column("invoice_number", sa.String(30), nullable=True),
        sa.Column("subtotal", sa.Numeric(15, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("tax_amount", sa.Numeric(15, 2), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "discount_amount", sa.Numeric(15, 2), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("discount_reason", sa.String(200), nullable=True),
        sa.Column(
            "discount_approved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("total", sa.Numeric(15, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("amount_paid", sa.Numeric(15, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("status", invoice_status, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("void_reason", sa.String(500), nullable=True),
        *_audit_columns(),
    )

    # The money rules, held by the database as well as the service (module spec
    # §4 rules 7, 9; §11). A bug in the service must not be able to store an
    # invoice that has been paid more than it is worth.
    op.create_check_constraint(
        "amounts_non_negative",
        "invoices",
        sa.text(
            "subtotal >= 0 AND tax_amount >= 0 AND discount_amount >= 0 "
            "AND total >= 0 AND amount_paid >= 0"
        ),
    )
    op.create_check_constraint(
        "discount_within_subtotal", "invoices", sa.text("discount_amount <= subtotal")
    )
    op.create_check_constraint("paid_within_total", "invoices", sa.text("amount_paid <= total"))
    op.create_check_constraint(
        "number_required_once_issued",
        "invoices",
        sa.text("status = 'draft' OR invoice_number IS NOT NULL"),
    )

    # Business rule 2 / AC-2. Partial, so the many unnumbered drafts do not
    # collide with each other.
    op.create_index(
        "uq_invoices_hospital_number",
        "invoices",
        ["hospital_id", "invoice_number"],
        unique=True,
        postgresql_where=sa.text("invoice_number IS NOT NULL"),
    )
    op.create_index(
        "uq_invoices_live_appointment",
        "invoices",
        ["appointment_id"],
        unique=True,
        postgresql_where=sa.text(
            "appointment_id IS NOT NULL AND status <> 'void' AND deleted_at IS NULL"
        ),
    )
    # Read paths (module spec §8).
    op.create_index("ix_invoices_status", "invoices", ["hospital_id", "status", "issued_at"])
    op.create_index("ix_invoices_patient", "invoices", ["patient_id", sa.text("issued_at DESC")])

    # ── invoice_items ──────────────────────────────────────────────────────────
    # No audit columns (§2.18): a line has no life apart from its invoice, and
    # the invoice's own audit columns and audit events cover every edit.
    op.create_table(
        "invoice_items",
        _uuid_pk(),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _hospital_fk(),
        sa.Column(
            "service_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("services.id", ondelete="RESTRICT"),
            # NULL for an ad-hoc line that is not in the catalog.
            nullable=True,
        ),
        # Denormalized (§2.18): renaming a catalog service must not rewrite the
        # invoices already raised against it.
        sa.Column("description", sa.String(200), nullable=False),
        sa.Column("quantity", sa.Numeric(10, 2), nullable=False, server_default=sa.text("1")),
        sa.Column("unit_price", sa.Numeric(15, 2), nullable=False),
        sa.Column("tax_rate", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("line_total", sa.Numeric(15, 2), nullable=False),
        sa.Column("position", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint("quantity_positive", "invoice_items", sa.text("quantity > 0"))
    op.create_check_constraint(
        "unit_price_non_negative", "invoice_items", sa.text("unit_price >= 0")
    )
    op.create_check_constraint(
        "tax_rate_range", "invoice_items", sa.text("tax_rate >= 0 AND tax_rate <= 100")
    )
    op.create_check_constraint(
        "line_total_non_negative", "invoice_items", sa.text("line_total >= 0")
    )
    op.create_index("ix_invoice_items_invoice", "invoice_items", ["invoice_id", "position"])

    # ── payments ───────────────────────────────────────────────────────────────
    op.create_table(
        "payments",
        _uuid_pk(),
        _hospital_fk(),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            # RESTRICT, not CASCADE: money received is never deleted as a side
            # effect of removing the thing it was paid against.
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("method", payment_method, nullable=False),
        sa.Column("reference", sa.String(100), nullable=True),
        sa.Column(
            "received_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        *_audit_columns(),
    )
    op.create_check_constraint("amount_positive", "payments", sa.text("amount > 0"))
    # Business rule 6 / AC-3 — see the module docstring for the tenant scoping.
    op.create_index(
        "uq_payments_hospital_idempotency_key",
        "payments",
        ["hospital_id", "idempotency_key"],
        unique=True,
    )
    op.create_index("ix_payments_invoice", "payments", ["invoice_id", "received_at"])


def downgrade() -> None:
    """Rollback — drop the billing tables and their enum types."""
    op.drop_index("ix_payments_invoice", table_name="payments")
    op.drop_index("uq_payments_hospital_idempotency_key", table_name="payments")
    op.drop_table("payments")

    op.drop_index("ix_invoice_items_invoice", table_name="invoice_items")
    op.drop_table("invoice_items")

    op.drop_index("ix_invoices_patient", table_name="invoices")
    op.drop_index("ix_invoices_status", table_name="invoices")
    op.drop_index("uq_invoices_live_appointment", table_name="invoices")
    op.drop_index("uq_invoices_hospital_number", table_name="invoices")
    op.drop_table("invoices")

    op.drop_table("invoice_number_sequences")

    op.drop_index("ix_services_hospital_category", table_name="services")
    op.drop_table("services")

    op.execute("DROP TYPE IF EXISTS payment_method")
    op.execute("DROP TYPE IF EXISTS invoice_status")
