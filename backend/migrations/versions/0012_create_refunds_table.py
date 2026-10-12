"""create refunds table

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-03 20:05:00.000000

Creates ``refunds`` — money given back against an invoice
(``docs/modules/06-billing.md`` §5.5).

The spec leaves the representation open: "inserts a negative payment or a
refund record". A separate table is used rather than negative rows in
``payments`` because:

- ``payments.amount > 0`` is a database check today, and every reader of that
  table can rely on a row meaning money *received*. A negative row would make
  every sum over payments silently mean something else.
- A refund carries things a payment does not — a required reason — and is
  authorised differently (``invoice.refund``, admin only).

Like payments, refunds are idempotent by a client-supplied key, unique per
hospital: giving money back twice because a request was retried is the same
class of mistake as taking it twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The enum type already exists (migration 0009); reuse it, do not recreate.
    payment_method = postgresql.ENUM(name="payment_method", create_type=False)

    op.create_table(
        "refunds",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "hospital_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hospitals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            # RESTRICT: a record of money returned is never deleted as a side
            # effect of removing the invoice it was returned against.
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(15, 2), nullable=False),
        # How the money went back — the same vocabulary as payments.
        sa.Column("method", payment_method, nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("reference", sa.String(100), nullable=True),
        sa.Column(
            "refunded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
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
    )
    op.create_check_constraint("amount_positive", "refunds", sa.text("amount > 0"))
    op.create_index(
        "uq_refunds_hospital_idempotency_key",
        "refunds",
        ["hospital_id", "idempotency_key"],
        unique=True,
    )
    op.create_index("ix_refunds_invoice", "refunds", ["invoice_id", "refunded_at"])


def downgrade() -> None:
    """Rollback — drop the refunds table. The shared enum type is left alone."""
    op.drop_index("ix_refunds_invoice", table_name="refunds")
    op.drop_index("uq_refunds_hospital_idempotency_key", table_name="refunds")
    op.drop_table("refunds")
