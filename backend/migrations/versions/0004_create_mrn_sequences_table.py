"""create mrn_sequences table

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-27 10:05:00.000000

Creates the per-hospital MRN counter
(``docs/modules/03-patient-management.md`` §8).

One row per hospital. The counter is advanced under ``SELECT ... FOR UPDATE``
inside the patient-registration transaction, so concurrent registrations in the
same hospital serialize on this row rather than racing for the same MRN.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mrn_sequences",
        sa.Column(
            "hospital_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hospitals.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("current_value", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "format_template", sa.String(50), nullable=False, server_default="MRN-{year}-{seq:05d}"
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Rollback — drop the MRN counter table."""
    op.drop_table("mrn_sequences")
