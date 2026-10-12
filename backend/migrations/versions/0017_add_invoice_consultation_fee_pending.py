"""add invoices.consultation_fee_pending

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-05 23:30:00.000000

Fixes the consultation fee being dropped when a lab test is ordered or a
medicine dispensed before the visit is completed.

Since migration 0014 a visit's draft invoice can be raised by another module's
charge (``BillingService.add_charges``) while the consultation is still going
on. Completing the visit then found the appointment "already invoiced" and
skipped the consultation fee (``docs/modules/06-billing.md`` §5.1), exactly as
it rightly does for an invoice billing staff raised by hand. Nothing on the row
told the two apart.

``consultation_fee_pending`` is that difference: set on a draft a charge raised
for a visit, cleared when completion adds the fee. An invoice raised by hand
never has it, so completion still leaves those alone.

Existing rows get ``false``. A draft a charge raised before this migration
therefore still lacks its fee if the visit completes afterwards; the row does
not record how it came about, so it cannot be backfilled from here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "consultation_fee_pending",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    """Rollback — drop the pending-fee marker."""
    op.drop_column("invoices", "consultation_fee_pending")
