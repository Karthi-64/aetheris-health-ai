"""create auth throttle buckets

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-06 23:30:00.000000

Adds ``auth_throttle_buckets``, the counters behind the authentication
throttle (``app/services/auth_throttle.py``).

They replace the account lockout kept in ``users.failed_login_attempts`` and
``users.locked_until``. Five wrong passwords used to lock an account for
everyone for thirty minutes, so anybody who knew a staff email could keep its
owner out indefinitely. The throttle counts by account *and* by where an
attempt comes from, and slows guessing down instead of locking the account.

The counters live in PostgreSQL because it is the one dependency the
application cannot start without. The request rate limiter keeps its counts in
Redis and deliberately fails open when Redis is away; a brute-force control
must not.

**No ``hospital_id``.** A row is keyed by a SHA-256 hash and holds counters
and timestamps: no email, address, user or patient data. It also has to exist
for an email that names no account at all, before any hospital is known.

The two ``users`` columns are left in place and no longer read or written. An
account that is locked when this is deployed is simply no longer locked.

``downgrade`` drops the table. That discards throttle state and nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``auth_throttle_buckets``."""
    op.create_table(
        "auth_throttle_buckets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_charged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drains_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_auth_throttle_buckets"),
        sa.UniqueConstraint("key_hash", name="uq_auth_throttle_buckets_key_hash"),
    )
    op.create_index("ix_auth_throttle_buckets_expires_at", "auth_throttle_buckets", ["expires_at"])


def downgrade() -> None:
    """Drop ``auth_throttle_buckets``."""
    op.drop_index("ix_auth_throttle_buckets_expires_at", table_name="auth_throttle_buckets")
    op.drop_table("auth_throttle_buckets")
