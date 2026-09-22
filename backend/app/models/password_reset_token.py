"""PasswordResetToken model — single-use password reset tokens.

Backs the password-reset-via-email flow described in
``docs/modules/01-authentication.md`` §5.6–5.7.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for Mapped[uuid.UUID] resolution
from datetime import datetime  # noqa: TC003 — needed at runtime for Mapped[datetime] resolution
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class PasswordResetToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single-use password reset token.

    Tokens are generated on request and validated on use.
    They expire after a configurable TTL (default 30 minutes).
    """

    __tablename__ = "password_reset_tokens"

    __table_args__ = (
        Index("ix_password_reset_tokens_user_id_used_at", "user_id", "used_at"),
        Index("ix_password_reset_tokens_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="UUID of the user requesting the password reset.",
    )
    token_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="SHA-256 hash of the opaque reset token. Used for O(1) lookup.",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Token expiration timestamp (UTC). Default 30 minutes from issuance.",
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when this token was used. NULL = not yet used.",
    )

    # ── Relationships ───────────────────────────────────────────────────
    user: Mapped[User] = relationship(
        "User",
        foreign_keys="PasswordResetToken.user_id",
        lazy="joined",
    )

    @property
    def is_expired(self) -> bool:
        """True if the token has passed its expiration time."""
        from datetime import UTC

        return datetime.now(UTC) > self.expires_at

    @property
    def is_used(self) -> bool:
        """True if this token has already been consumed."""
        return self.used_at is not None

    @property
    def is_valid(self) -> bool:
        """True if the token is not used and not expired."""
        return not self.is_used and not self.is_expired

    def __repr__(self) -> str:
        return (
            f"<PasswordResetToken id={self.id!s:.8} "
            f"user={self.user_id!s:.8} "
            f"used={self.is_used} "
            f"expired={self.is_expired}>"
        )
