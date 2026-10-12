"""Data access for patient accounts.

``patient_accounts`` is platform-level identity data, not tenant data (see
``app/models/patient_account.py``), so this repository takes no hospital. An
account is found by its phone number — which the caller has just proven with a
one-time code — or by an id that came from a verified patient token or a
server-side record. Never by anything a request merely claimed.
"""

from __future__ import annotations

import uuid
from datetime import datetime  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.patient_account import PatientAccount, PatientAccountStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["PatientAccountRepository"]


class PatientAccountRepository:
    """Reads and writes patient accounts.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, account_id: uuid.UUID) -> PatientAccount | None:
        """Read one account as it is now.

        :param account_id: The account's UUID.
        :returns: The account, or ``None``.
        """
        result = await self._session.execute(
            select(PatientAccount)
            .where(PatientAccount.id == account_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def lock_by_id(self, account_id: uuid.UUID) -> PatientAccount | None:
        """Read one account and lock its row until the transaction ends.

        What serialises every change to an account's sessions: a rotation, a
        "log out everywhere" and reuse detection each take this lock, so none
        of them can miss what another is in the middle of writing.

        :param account_id: The account's UUID.
        :returns: The account as it is now, under lock, or ``None``.
        """
        result = await self._session.execute(
            select(PatientAccount)
            .where(PatientAccount.id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def lock_or_create_by_phone(
        self, phone: str, *, now: datetime
    ) -> tuple[PatientAccount, bool]:
        """Return the account for a phone number, locked, creating it if there is none.

        Called only once the number has been proven by a one-time code. Two
        first sign-ins for the same number at once both insert; the unique
        index lets one through, and both then lock and read the same row.

        :param phone: The verified number, in E.164 form.
        :param now: When the number was verified.
        :returns: The account under lock, and whether this call created it.
        """
        inserted = await self._session.execute(
            insert(PatientAccount)
            .values(
                id=uuid.uuid4(),
                phone=phone,
                status=PatientAccountStatus.ACTIVE.value,
                phone_verified_at=now,
            )
            .on_conflict_do_nothing(index_elements=[PatientAccount.phone])
            .returning(PatientAccount.id)
        )
        created = inserted.scalar_one_or_none() is not None
        result = await self._session.execute(
            select(PatientAccount)
            .where(PatientAccount.phone == phone)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one(), created

    async def record_login(self, account: PatientAccount, *, now: datetime) -> None:
        """Note a completed sign-in: the phone was just verified again.

        :param account: The account that signed in.
        :param now: When it did.
        """
        account.phone_verified_at = now
        account.last_login_at = now
        await self._session.flush()
