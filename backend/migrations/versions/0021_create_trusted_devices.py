"""create trusted devices

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-06 23:40:00.000000

Adds ``trusted_devices``: browsers that have completed a full sign-in to an
account.

The authentication throttle (migration 0020) has to bound guessing that comes
from many addresses at once, which means one budget per account shared by
every source it does not recognise. On its own that budget would let an
attacker with a handful of addresses keep an account's owner out. A browser
the owner has already signed in from is told apart by a random token in an
``HttpOnly`` cookie, and draws on a counter of its own that nobody else can
reach.

A trusted device changes only which counters an attempt is charged to. It is
not a credential and never replaces the password or the second factor.

**No ``hospital_id``**, following ``refresh_tokens`` and
``password_reset_tokens``: a row belongs to one user and is reached only
through that user. Only the SHA-256 of the token is stored.

``downgrade`` drops the table. Every browser becomes unrecognised; nothing
else is lost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``trusted_devices``."""
    op.create_table(
        "trusted_devices",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_trusted_devices"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_trusted_devices_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_trusted_devices_token_hash"),
    )
    op.create_index("ix_trusted_devices_user_id", "trusted_devices", ["user_id"])
    op.create_index("ix_trusted_devices_expires_at", "trusted_devices", ["expires_at"])


def downgrade() -> None:
    """Drop ``trusted_devices``."""
    op.drop_index("ix_trusted_devices_expires_at", table_name="trusted_devices")
    op.drop_index("ix_trusted_devices_user_id", table_name="trusted_devices")
    op.drop_table("trusted_devices")
