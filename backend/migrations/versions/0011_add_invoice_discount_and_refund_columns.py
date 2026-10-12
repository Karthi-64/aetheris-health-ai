"""add invoice discount-approval and refund columns

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-03 20:00:00.000000

Adds the two invoice columns the discount and refund workflows need
(``docs/modules/06-billing.md`` §5.2, §5.5):

- ``discount_pending_approval`` — spec §5.2 names this flag but
  ``docs/05-DATABASE_DESIGN.md`` §2.17 has no column for it. It is stored
  rather than derived so that the admin's approval queue is a plain indexed
  filter, and so that lowering a hospital's threshold later does not silently
  re-open invoices that were within the rules when they were drafted.
- ``amount_refunded`` — how much of ``amount_paid`` has been given back.
  Kept separate from ``amount_paid`` so that neither figure is ever rewritten:
  what was received and what was returned both stay on the record.

``refunded_within_paid`` is business rule 10 ("refunds cannot exceed the
invoice's paid amount"), held by the database as well as the service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "discount_pending_approval",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "amount_refunded", sa.Numeric(15, 2), nullable=False, server_default=sa.text("0")
        ),
    )
    op.create_check_constraint(
        "refunded_within_paid",
        "invoices",
        sa.text("amount_refunded >= 0 AND amount_refunded <= amount_paid"),
    )
    # The discount approval queue (module spec §12): drafts awaiting an admin.
    op.create_index(
        "ix_invoices_discount_pending",
        "invoices",
        ["hospital_id"],
        postgresql_where=sa.text("discount_pending_approval"),
    )


def downgrade() -> None:
    """Rollback — drop the discount-approval and refund columns."""
    op.drop_index("ix_invoices_discount_pending", table_name="invoices")
    # The short name: the metadata naming convention adds the `ck_invoices_`
    # prefix itself, exactly as it did for create_check_constraint above.
    op.drop_constraint("refunded_within_paid", "invoices", type_="check")
    op.drop_column("invoices", "amount_refunded")
    op.drop_column("invoices", "discount_pending_approval")
