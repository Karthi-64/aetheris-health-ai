"""Repository for the :class:`PasswordResetToken` model.

Supports creation, lookup by hash, consumption (mark-as-used), and
expired-token cleanup. No business logic — just data access.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.password_reset_token import PasswordResetToken
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PasswordResetTokenRepository(BaseRepository[PasswordResetToken]):
    """Repository for password reset token lifecycle.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(PasswordResetToken, session)

    async def create(  # type: ignore[override]
        self,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        **kwargs: object,
    ) -> PasswordResetToken:
        """Create a new password reset token.

        :param user_id: UUID of the user requesting the reset.
        :param token_hash: SHA-256 hash of the opaque reset token.
        :param expires_at: Token expiration timestamp (UTC).
        :param kwargs: Additional optional fields.
        :returns: The created token instance.
        """
        return await super().create(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            **kwargs,
        )

    async def get_by_token_hash(self, token_hash: str) -> PasswordResetToken | None:
        """Retrieve a token by its SHA-256 hash.

        :param token_hash: The token hash to look up.
        :returns: The token instance, or ``None``.
        """
        stmt = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def mark_as_used(self, token: PasswordResetToken) -> PasswordResetToken:
        """Mark a token as used.

        :param token: The token instance to mark.
        :returns: The updated token instance.
        """
        return await self.update(token, used_at=datetime.now(UTC))

    async def get_valid_token(self, token_hash: str) -> PasswordResetToken | None:
        """Retrieve a token that is not yet used and not expired.

        :param token_hash: The token hash to look up.
        :returns: The token instance, or ``None``.
        """
        now = datetime.now(UTC)
        stmt = select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def invalidate_all_for_user(self, user_id: uuid.UUID) -> int:
        """Mark all unused tokens for a user as consumed.

        Called when a password is successfully changed, to invalidate
        any outstanding reset requests.

        :param user_id: The user's UUID.
        :returns: The number of tokens invalidated.
        """
        now = datetime.now(UTC)
        stmt = select(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
        result = await self._session.execute(stmt)
        tokens = list(result.unique().scalars().all())
        for token in tokens:
            token.used_at = now
        await self._session.flush()
        return len(tokens)

    async def delete_expired(self) -> int:
        """Delete all expired password reset tokens.

        :returns: The number of deleted tokens.
        """
        now = datetime.now(UTC)
        stmt = select(PasswordResetToken).where(PasswordResetToken.expires_at < now)
        result = await self._session.execute(stmt)
        tokens = list(result.unique().scalars().all())
        for token in tokens:
            await self._session.delete(token)
        await self._session.flush()
        return len(tokens)
