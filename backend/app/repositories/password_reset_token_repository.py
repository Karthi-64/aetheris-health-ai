"""Repository for the :class:`PasswordResetToken` model.

Supports creation, lookup by hash, consumption (mark-as-used), and
expired-token cleanup. No business logic — just data access.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update

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

    async def consume(self, token_hash: str) -> PasswordResetToken | None:
        """Spend a token: find it, check it is usable, and mark it used — atomically.

        One ``UPDATE ... RETURNING``. Two requests presenting the same token at
        the same moment cannot both succeed: the second waits for the first,
        then finds ``used_at`` set and matches nothing. A token is single-use
        because of this statement, not because callers check first.

        :param token_hash: SHA-256 hash of the token presented.
        :returns: The token, now marked used, or ``None`` if it is unknown,
            already used or expired.
        """
        now = datetime.now(UTC)
        result = await self._session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.token_hash == token_hash,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
            .values(used_at=now)
            .returning(PasswordResetToken)
            .execution_options(populate_existing=True)
        )
        return result.unique().scalar_one_or_none()

    async def count_issued_since(self, user_id: uuid.UUID, since: datetime) -> int:
        """Count the tokens issued to a user since a moment that still work.

        Only links that can still be redeemed are counted — unused and
        unexpired. A limit built on this therefore never refuses a new link
        unless working ones are already in the user's mailbox.

        :param user_id: The user's UUID.
        :param since: Only tokens created at or after this are counted.
        :returns: The number of tokens.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.created_at >= since,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > datetime.now(UTC),
            )
        )
        return int(result.scalar_one())

    async def invalidate_all_for_user(
        self, user_id: uuid.UUID, *, keep: uuid.UUID | None = None
    ) -> int:
        """Mark all unused tokens for a user as consumed.

        Called when a password is successfully changed, to invalidate
        any outstanding reset requests.

        :param user_id: The user's UUID.
        :param keep: A token id to leave alone — the one just issued, when an
            invitation is sent again and only the older links are to die.
        :returns: The number of tokens invalidated.
        """
        now = datetime.now(UTC)
        stmt = select(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
        if keep is not None:
            stmt = stmt.where(PasswordResetToken.id != keep)
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
