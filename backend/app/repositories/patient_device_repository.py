"""Data access for recognised patient devices.

``patient_devices`` hangs off a patient account and carries no hospital (see
``app/models/patient_account.py``). Recognition is always **per account**: a
token counts only where a live row ties it to the account in question. A token
recognised for somebody else, or one the server never issued, is nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from app.models.patient_account import PatientAccount, PatientDevice

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["PatientDeviceRepository"]


class PatientDeviceRepository:
    """Reads and writes recognised-device rows.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_for_phone(
        self, phone: str, token_hashes: Sequence[str], *, now: datetime
    ) -> PatientDevice | None:
        """Find the live row that makes one of these tokens a recognised device of a number.

        One statement, joined through the account that owns the number, and
        the same statement whether or not any account does: a number nobody
        has signed in with simply matches no row.

        :param phone: The number a code is being requested for, in E.164 form.
        :param token_hashes: Hashes of the tokens the browser presented.
        :param now: Rows that expired before this do not count.
        :returns: The row, or ``None`` if the browser is not recognised for
            the account that owns this number.
        """
        if not token_hashes:
            return None
        result = await self._session.execute(
            select(PatientDevice)
            .join(PatientAccount, PatientAccount.id == PatientDevice.account_id)
            .where(
                PatientAccount.phone == phone,
                PatientDevice.token_hash.in_(list(token_hashes)),
                PatientDevice.expires_at > now,
            )
            .order_by(PatientDevice.last_used_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def find(
        self, account_id: uuid.UUID, token_hashes: Sequence[str], *, now: datetime
    ) -> PatientDevice | None:
        """Find the live row that makes one of these tokens a recognised device of an account.

        :param account_id: The account that just signed in.
        :param token_hashes: Hashes of the tokens the browser presented.
        :param now: Rows that expired before this do not count.
        :returns: The row, or ``None``.
        """
        if not token_hashes:
            return None
        result = await self._session.execute(
            select(PatientDevice)
            .where(
                PatientDevice.account_id == account_id,
                PatientDevice.token_hash.in_(list(token_hashes)),
                PatientDevice.expires_at > now,
            )
            .order_by(PatientDevice.last_used_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def live_hashes(self, token_hashes: Sequence[str], *, now: datetime) -> set[str]:
        """Which of these token hashes still belong to a live row, of any account.

        Used only to drop dead tokens from the cookie when it is rewritten.

        :param token_hashes: Hashes of the tokens the browser presented.
        :param now: Rows that expired before this do not count.
        :returns: The subset that is still live.
        """
        if not token_hashes:
            return set()
        result = await self._session.execute(
            select(PatientDevice.token_hash).where(
                PatientDevice.token_hash.in_(list(token_hashes)),
                PatientDevice.expires_at > now,
            )
        )
        return set(result.scalars().all())

    async def create(
        self, account_id: uuid.UUID, token_hash: str, *, now: datetime, expires_at: datetime
    ) -> PatientDevice:
        """Record a browser as recognised for an account.

        :param account_id: The account.
        :param token_hash: SHA-256 of the token given to the browser.
        :param now: When the sign-in completed.
        :param expires_at: When recognition lapses.
        :returns: The new row.
        """
        device = PatientDevice(
            id=uuid.uuid4(),
            account_id=account_id,
            token_hash=token_hash,
            created_at=now,
            last_used_at=now,
            expires_at=expires_at,
        )
        self._session.add(device)
        await self._session.flush()
        return device

    async def touch(self, device: PatientDevice, *, now: datetime, expires_at: datetime) -> None:
        """Note a completed sign-in from a recognised device.

        :param device: The row.
        :param now: When the sign-in completed.
        :param expires_at: The new expiry.
        """
        device.last_used_at = now
        device.expires_at = expires_at
        await self._session.flush()

    async def delete_for_account(self, account_id: uuid.UUID) -> int:
        """Forget every recognised device of an account.

        :param account_id: The account.
        :returns: How many rows were deleted.
        """
        result = await self._session.execute(
            delete(PatientDevice)
            .where(PatientDevice.account_id == account_id)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def prune(self, account_id: uuid.UUID, *, keep: int, now: datetime) -> int:
        """Keep an account's most established devices and drop the rest.

        Expired rows go first. Among live ones, a browser that has come back
        and signed in again outranks one seen only once, and after that the
        most recently used wins — so a client that never keeps its cookie
        displaces only its own earlier rows, not the browsers the owner uses.

        :param account_id: The account.
        :param keep: How many live rows to keep.
        :param now: Rows that expired before this are dropped regardless.
        :returns: How many rows were deleted.
        """
        keepers = (
            select(PatientDevice.id)
            .where(PatientDevice.account_id == account_id, PatientDevice.expires_at > now)
            .order_by(
                (PatientDevice.last_used_at > PatientDevice.created_at).desc(),
                PatientDevice.last_used_at.desc(),
                PatientDevice.id,
            )
            .limit(keep)
        )
        result = await self._session.execute(
            delete(PatientDevice)
            .where(PatientDevice.account_id == account_id, PatientDevice.id.not_in(keepers))
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)
