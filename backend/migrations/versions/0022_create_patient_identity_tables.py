"""create patient identity tables

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-07 10:00:00.000000

Adds the Patient App's login identity (``docs/modules/15-patient-app.md`` §4
and §5): ``patient_accounts``, ``patient_otp_challenges``,
``patient_refresh_tokens`` and ``patient_devices``.

A patient is a different kind of principal from a staff user. An account is
keyed by a phone number proven with a one-time code; it is never a ``users``
row, a role or a permission, and none of these tables references ``users``.

**No ``hospital_id`` on any of the four**, each for its own reason:

* ``patient_accounts`` — a login identity exists before it is linked to any
  hospital, and one person can be a patient at several. Putting a hospital on
  the account would make that impossible. What an account may see is decided
  by ``patient_account_links`` (migration 0023), which *is* tenant data.
* ``patient_otp_challenges`` — a code is requested for a phone number before
  any account exists, and requesting or verifying one never touches a
  hospital's data. The row holds a keyed hash of the code (HMAC-SHA-256 under
  ``PATIENT_OTP_SECRET``), never the code.
* ``patient_refresh_tokens`` — a session belongs to one account and is reached
  only through it, exactly as ``refresh_tokens`` belongs to a staff user. Only
  the SHA-256 of the token is stored.
* ``patient_devices`` — a browser that has completed a sign-in to an account;
  the counterpart of ``trusted_devices`` (migration 0021). It decides only
  which throttle bucket a request for a code is charged to. Only the SHA-256
  of the cookie token is stored.

This is the same position as ``permissions``, ``refresh_tokens``,
``auth_throttle_buckets`` and ``trusted_devices``: platform-level identity
data, not tenant data.

``downgrade`` drops the four tables. Every patient account, session and
recognised device is lost with them; no hospital record is touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    """Create the four patient identity tables."""
    op.create_table(
        "patient_accounts",
        _uuid_pk(),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_patient_accounts"),
        sa.UniqueConstraint("phone", name="uq_patient_accounts_phone"),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'closed')",
            name=op.f("ck_patient_accounts_status"),
        ),
    )

    op.create_table(
        "patient_otp_challenges",
        _uuid_pk(),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_patient_otp_challenges"),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_patient_otp_challenges_attempts")),
    )
    op.create_index(
        "ix_patient_otp_challenges_expires_at", "patient_otp_challenges", ["expires_at"]
    )

    op.create_table(
        "patient_refresh_tokens",
        _uuid_pk(),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_by_token_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("device_info", sa.String(length=255), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_patient_refresh_tokens"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["patient_accounts.id"],
            name="fk_patient_refresh_tokens_account_id_patient_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rotated_by_token_id"],
            ["patient_refresh_tokens.id"],
            # Shortened: the conventional name is over PostgreSQL's 63 characters.
            name="fk_patient_refresh_tokens_rotated_by_token_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("token_hash", name="uq_patient_refresh_tokens_token_hash"),
    )
    op.create_index(
        "ix_patient_refresh_tokens_account_id", "patient_refresh_tokens", ["account_id"]
    )
    op.create_index(
        "ix_patient_refresh_tokens_expires_at", "patient_refresh_tokens", ["expires_at"]
    )

    op.create_table(
        "patient_devices",
        _uuid_pk(),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        _timestamp("created_at"),
        _timestamp("last_used_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_patient_devices"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["patient_accounts.id"],
            name="fk_patient_devices_account_id_patient_accounts",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="uq_patient_devices_token_hash"),
    )
    op.create_index("ix_patient_devices_account_id", "patient_devices", ["account_id"])
    op.create_index("ix_patient_devices_expires_at", "patient_devices", ["expires_at"])


def downgrade() -> None:
    """Drop the four patient identity tables."""
    op.drop_index("ix_patient_devices_expires_at", table_name="patient_devices")
    op.drop_index("ix_patient_devices_account_id", table_name="patient_devices")
    op.drop_table("patient_devices")
    op.drop_index("ix_patient_refresh_tokens_expires_at", table_name="patient_refresh_tokens")
    op.drop_index("ix_patient_refresh_tokens_account_id", table_name="patient_refresh_tokens")
    op.drop_table("patient_refresh_tokens")
    op.drop_index("ix_patient_otp_challenges_expires_at", table_name="patient_otp_challenges")
    op.drop_table("patient_otp_challenges")
    op.drop_table("patient_accounts")
