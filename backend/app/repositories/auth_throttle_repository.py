"""Data access for the authentication throttle and trusted devices.

Neither table is tenant data (see ``app/models/auth_throttle.py``), so these
repositories take no hospital. :class:`TrustedDeviceRepository` is keyed by a
user id that the service has already resolved from server-side records —
never one taken from a request.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import datetime  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.auth_throttle import AuthThrottleBucket, TrustedDevice

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["AuthThrottleRepository", "TrustedDeviceRepository"]

#: Tries at creating-then-locking a bucket before giving up (and refusing).
_LOCK_ATTEMPTS = 3


class AuthThrottleRepository:
    """Reads and writes throttle counters.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def now(self) -> datetime:
        """The database's clock, read at this instant.

        Every decision is timed by one clock — the database's — so two
        application hosts whose clocks disagree cannot disagree about whether
        a block has ended. ``clock_timestamp()``, not ``now()``: the latter is
        frozen at the start of the transaction.

        :returns: The current time, timezone-aware.
        """
        result = await self._session.execute(select(func.clock_timestamp()))
        value: datetime = result.scalar_one()
        return value

    async def lock(self, kind: str, key_hash: str, *, now: datetime) -> AuthThrottleBucket:
        """Return one bucket, locked until the transaction ends, creating it if absent.

        Concurrent callers for the same key wait here and then see each
        other's writes, which is what makes charging exact under parallel
        requests. Callers must lock buckets in one fixed order.

        :param kind: The bucket kind, stored for operators.
        :param key_hash: The bucket's key.
        :param now: Used as the expiry of a row created here; a row that is
            never charged holds nothing and may go at once.
        :returns: The bucket as it is now, under lock.
        """
        # Two statements, so the row can vanish between them: a sweep may
        # delete an expired bucket after the insert found it and before the
        # lock is taken. Then the insert is simply done again — this time the
        # row is this transaction's own, which nothing else can see or delete.
        for _ in range(_LOCK_ATTEMPTS):
            await self._session.execute(
                insert(AuthThrottleBucket)
                .values(id=uuid.uuid4(), key_hash=key_hash, kind=kind, failures=0, expires_at=now)
                .on_conflict_do_nothing(index_elements=[AuthThrottleBucket.key_hash])
            )
            result = await self._session.execute(
                select(AuthThrottleBucket)
                .where(AuthThrottleBucket.key_hash == key_hash)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                return row
        msg = "could not lock an authentication throttle bucket"
        raise RuntimeError(msg)

    async def flush(self) -> None:
        """Write pending bucket changes."""
        await self._session.flush()

    async def purge_expired(self, *, now: datetime, limit: int) -> int:
        """Delete a bounded batch of buckets that hold nothing any more.

        Rows another transaction has locked are skipped, never waited for.

        :param now: Rows that expired before this are eligible.
        :param limit: The most rows to delete in one call.
        :returns: How many were deleted.
        """
        expired = (
            select(AuthThrottleBucket.id)
            .where(AuthThrottleBucket.expires_at < now)
            .order_by(AuthThrottleBucket.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(
            delete(AuthThrottleBucket)
            .where(AuthThrottleBucket.id.in_(expired))
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)


class TrustedDeviceRepository:
    """Reads and writes trusted-device rows.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find(
        self, user_id: uuid.UUID, token_hashes: Sequence[str], *, now: datetime
    ) -> TrustedDevice | None:
        """Find the live row that makes one of these tokens a trusted device of this user.

        :param user_id: The account being signed in to.
        :param token_hashes: Hashes of the tokens the browser presented.
        :param now: Rows that expired before this do not count.
        :returns: The row, or ``None`` if the browser is not recognised for
            this account.
        """
        if not token_hashes:
            return None
        result = await self._session.execute(
            select(TrustedDevice)
            .where(
                TrustedDevice.user_id == user_id,
                TrustedDevice.token_hash.in_(list(token_hashes)),
                TrustedDevice.expires_at > now,
            )
            .order_by(TrustedDevice.last_used_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def live_hashes(self, token_hashes: Sequence[str], *, now: datetime) -> set[str]:
        """Which of these token hashes still belong to a live row, of any user.

        Used only to drop dead tokens from the cookie when it is rewritten.

        :param token_hashes: Hashes of the tokens the browser presented.
        :param now: Rows that expired before this do not count.
        :returns: The subset that is still live.
        """
        if not token_hashes:
            return set()
        result = await self._session.execute(
            select(TrustedDevice.token_hash).where(
                TrustedDevice.token_hash.in_(list(token_hashes)),
                TrustedDevice.expires_at > now,
            )
        )
        return set(result.scalars().all())

    async def create(
        self,
        user_id: uuid.UUID,
        token_hash: str,
        *,
        now: datetime,
        expires_at: datetime,
        mfa_verified: bool = False,
    ) -> TrustedDevice:
        """Record a browser as trusted for an account.

        :param user_id: The account.
        :param token_hash: SHA-256 of the token given to the browser.
        :param now: When the sign-in completed.
        :param expires_at: When recognition lapses.
        :param mfa_verified: Whether that sign-in passed the second factor.
        :returns: The new row.
        """
        device = TrustedDevice(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=token_hash,
            created_at=now,
            last_used_at=now,
            expires_at=expires_at,
            mfa_verified_at=now if mfa_verified else None,
        )
        self._session.add(device)
        await self._session.flush()
        return device

    async def touch(
        self,
        device: TrustedDevice,
        *,
        now: datetime,
        expires_at: datetime,
        mfa_verified: bool = False,
    ) -> None:
        """Note a completed sign-in from a trusted device.

        :param device: The row.
        :param now: When the sign-in completed.
        :param expires_at: The new expiry.
        :param mfa_verified: Whether this sign-in passed the second factor.
        """
        device.last_used_at = now
        device.expires_at = expires_at
        if mfa_verified:
            device.mfa_verified_at = now
        await self._session.flush()

    async def forget_mfa_for_user(self, user_id: uuid.UUID) -> int:
        """Stop treating any of an account's devices as having passed its second factor.

        For when the second factor itself changes: what a device proved about
        the old one says nothing about the new one.

        :param user_id: The account.
        :returns: How many rows were changed.
        """
        result = await self._session.execute(
            update(TrustedDevice)
            .where(TrustedDevice.user_id == user_id, TrustedDevice.mfa_verified_at.is_not(None))
            .values(mfa_verified_at=None)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_for_user(self, user_id: uuid.UUID) -> int:
        """Forget every trusted device of an account.

        :param user_id: The account.
        :returns: How many rows were deleted.
        """
        result = await self._session.execute(
            delete(TrustedDevice)
            .where(TrustedDevice.user_id == user_id)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_tokens(self, user_id: uuid.UUID, token_hashes: Sequence[str]) -> int:
        """Forget this account's trust in the given tokens.

        :param user_id: The account.
        :param token_hashes: Hashes of the tokens to forget.
        :returns: How many rows were deleted.
        """
        if not token_hashes:
            return 0
        result = await self._session.execute(
            delete(TrustedDevice)
            .where(
                TrustedDevice.user_id == user_id,
                TrustedDevice.token_hash.in_(list(token_hashes)),
            )
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def prune(self, user_id: uuid.UUID, *, keep: int, now: datetime) -> int:
        """Keep an account's most established devices and drop the rest.

        Expired rows go first. Among live ones, a browser that has come back
        and signed in again outranks one seen only once, and after that the
        most recently used wins. So a client that never keeps its cookie — a
        script, say — and is therefore "new" every time, displaces only its
        own earlier rows, not the browsers the owner actually uses.

        :param user_id: The account.
        :param keep: How many live rows to keep.
        :param now: Rows that expired before this are dropped regardless.
        :returns: How many rows were deleted.
        """
        keepers = (
            select(TrustedDevice.id)
            .where(TrustedDevice.user_id == user_id, TrustedDevice.expires_at > now)
            .order_by(
                (TrustedDevice.last_used_at > TrustedDevice.created_at).desc(),
                TrustedDevice.last_used_at.desc(),
                TrustedDevice.id,
            )
            .limit(keep)
        )
        result = await self._session.execute(
            delete(TrustedDevice)
            .where(TrustedDevice.user_id == user_id, TrustedDevice.id.not_in(keepers))
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)
