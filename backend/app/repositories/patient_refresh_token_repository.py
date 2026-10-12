"""Data access for patient sessions.

``patient_refresh_tokens`` hangs off a patient account and carries no hospital
(see ``app/models/patient_account.py``). A token is found only by the hash of
the opaque value a browser presented, and everything else is keyed by the
account that token — or a verified access token — named.
"""

from __future__ import annotations

import uuid
from datetime import datetime  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from app.models.patient_account import PatientRefreshToken

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["PatientRefreshTokenRepository"]


class PatientRefreshTokenRepository:
    """Reads and writes patient refresh tokens.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        account_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        device_info: str | None,
        ip_address: str | None,
    ) -> PatientRefreshToken:
        """Store a new session token.

        :param account_id: The account the session belongs to.
        :param token_hash: SHA-256 of the opaque token. Never the token.
        :param expires_at: When the session ends if it is not refreshed.
        :param device_info: User agent at issuance.
        :param ip_address: Address at issuance.
        :returns: The new row.
        """
        token = PatientRefreshToken(
            id=uuid.uuid4(),
            account_id=account_id,
            token_hash=token_hash,
            expires_at=expires_at,
            is_revoked=False,
            device_info=device_info[:255] if device_info else None,
            ip_address=ip_address,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def account_id_for_token_hash(self, token_hash: str) -> uuid.UUID | None:
        """Which account a presented token belongs to. Takes no lock.

        Only so the caller can lock that account *before* it locks the token:
        the account of a token never changes, and everything else about the
        token is read again under :meth:`lock_by_token_hash`.

        :param token_hash: SHA-256 of the presented token.
        :returns: The account id, or ``None`` if no such token exists.
        """
        result = await self._session.execute(
            select(PatientRefreshToken.account_id).where(
                PatientRefreshToken.token_hash == token_hash
            )
        )
        return result.scalar_one_or_none()

    async def lock_by_token_hash(self, token_hash: str) -> PatientRefreshToken | None:
        """Find a token by its hash and lock it until the transaction ends.

        Two refreshes presenting the same token at once wait here in turn, so
        the second one sees that the first already rotated it — which is what
        makes reuse detection exact.

        :param token_hash: SHA-256 of the presented token.
        :returns: The row as it is now, under lock, or ``None``.
        """
        result = await self._session.execute(
            select(PatientRefreshToken)
            .where(PatientRefreshToken.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def revoke(
        self,
        token: PatientRefreshToken,
        *,
        now: datetime,
        rotated_by_id: uuid.UUID | None = None,
    ) -> None:
        """Revoke one token.

        :param token: The token to revoke.
        :param now: When it was revoked.
        :param rotated_by_id: The token that replaced it, on a rotation.
        """
        token.is_revoked = True
        token.revoked_at = now
        if rotated_by_id is not None:
            token.rotated_by_token_id = rotated_by_id
        await self._session.flush()

    async def revoke_all_for_account(self, account_id: uuid.UUID, *, now: datetime) -> int:
        """End every session of an account.

        :param account_id: The account.
        :param now: When they were ended.
        :returns: How many live tokens were revoked.
        """
        result = await self._session.execute(
            update(PatientRefreshToken)
            .where(
                PatientRefreshToken.account_id == account_id,
                PatientRefreshToken.is_revoked.is_(False),
            )
            .values(is_revoked=True, revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)
