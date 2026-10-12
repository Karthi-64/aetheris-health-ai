"""Authentication throttle state and trusted devices.

Two small tables behind ``app/services/auth_throttle.py``:

* :class:`AuthThrottleBucket` — one counter per thing that can be throttled
  (an account from one source, an account from all unrecognised sources, a
  source, a trusted device, an account's second factor).
* :class:`TrustedDevice` — a browser that has completed a full sign-in to an
  account. It decides only which counters an attempt draws on. It is never a
  credential and never replaces a factor.

Neither carries ``hospital_id``. A bucket is keyed by a hash and holds
counters — no tenant data, and it has to exist before any account (and so any
hospital) is known. A trusted device hangs off a user exactly as a refresh
token does (``refresh_tokens``, ``password_reset_tokens``).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for Mapped[uuid.UUID] resolution
from datetime import datetime  # noqa: TC003 — needed at runtime for Mapped[datetime] resolution

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin

__all__ = ["AuthThrottleBucket", "TrustedDevice"]


class AuthThrottleBucket(UUIDPrimaryKeyMixin, Base):
    """One throttle counter.

    ``key_hash`` is a SHA-256 over the bucket kind and what it counts; the
    email, address or id itself is not stored. Two shapes share the table:

    * *backoff* buckets use ``failures``, ``blocked_until`` and
      ``last_charged_at``;
    * *budget* buckets use ``drains_at`` — the instant the bucket will be
      full again.
    """

    __tablename__ = "auth_throttle_buckets"

    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_auth_throttle_buckets_key_hash"),
        Index("ix_auth_throttle_buckets_expires_at", "expires_at"),
    )

    key_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 (hex) of the bucket kind and the thing it counts.",
    )
    kind: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="Which counter this is. Operational visibility only; the key already includes it.",
    )
    failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Backoff: attempts charged and not refunded since the bucket was last clear.",
    )
    blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Backoff: no attempt is evaluated before this instant.",
    )
    last_charged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Backoff: when the most recent attempt was charged.",
    )
    drains_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Budget: the instant at which the bucket is full again.",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="After this the row holds nothing and may be deleted.",
    )


class TrustedDevice(UUIDPrimaryKeyMixin, Base):
    """A browser that has completed a full sign-in to one account.

    The browser holds a random token in an ``HttpOnly`` cookie; only its
    SHA-256 is stored. Each row has its own token, so rows of different users
    on a shared workstation cannot be linked through this table. The row's
    ``id`` — never the token — names the device's throttle buckets, so
    nothing a client does can obtain a fresh bucket for an existing device.
    """

    __tablename__ = "trusted_devices"

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_trusted_devices_token_hash"),
        Index("ix_trusted_devices_user_id", "user_id"),
        Index("ix_trusted_devices_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="The account this browser has signed in to.",
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 (hex) of the random device token held in the browser's cookie.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When this browser first completed a sign-in to the account.",
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When this browser last completed a sign-in to the account.",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="After this the browser is no longer recognised.",
    )
    mfa_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this browser last passed the account's second factor. NULL: never.",
    )
