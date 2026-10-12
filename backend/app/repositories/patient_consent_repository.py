"""Data access for patient consent records.

A consent to a hospital purpose belongs to that hospital; a consent to a
platform policy belongs to none. Every method therefore takes ``hospital_id``
— ``None`` meaning "a platform policy", stated explicitly, never "any
hospital" — or a row an earlier read returned. Rows are only ever inserted and
ended; none is overwritten or deleted.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import datetime  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING

from sqlalchemy import Select, select

from app.models.patient_consent import PatientConsentRecord
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["PatientConsentRepository"]


def _in_hospital(
    stmt: Select[tuple[PatientConsentRecord]], hospital_id: uuid.UUID | None
) -> Select[tuple[PatientConsentRecord]]:
    """Confine a statement to one hospital's consents, or to platform consents."""
    if hospital_id is None:
        return stmt.where(PatientConsentRecord.hospital_id.is_(None))
    return stmt.where(PatientConsentRecord.hospital_id == hospital_id)


class PatientConsentRepository(BaseRepository[PatientConsentRecord]):
    """Reads and writes consent records.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(PatientConsentRecord, session)

    async def add(
        self,
        hospital_id: uuid.UUID | None,
        *,
        account_id: uuid.UUID,
        purpose: str,
        policy_version: str,
        now: datetime,
        ip_address: str | None,
        user_agent: str | None,
    ) -> PatientConsentRecord:
        """Insert one consent.

        :param hospital_id: The hospital, for a hospital purpose; ``None`` for
            a platform policy.
        :param account_id: Who consented.
        :param purpose: What to.
        :param policy_version: The exact version of the text that was shown.
        :param now: When.
        :param ip_address: Evidence: where the act came from.
        :param user_agent: Evidence: the client that sent it.
        :returns: The new record.
        """
        return await super().create(
            hospital_id=hospital_id,
            account_id=account_id,
            purpose=purpose,
            policy_version=policy_version,
            granted_at=now,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    async def get_active(
        self,
        hospital_id: uuid.UUID | None,
        *,
        account_id: uuid.UUID,
        purpose: str,
        policy_version: str,
    ) -> PatientConsentRecord | None:
        """The consent in force for one purpose under one exact policy version.

        :param hospital_id: The hospital, or ``None`` for a platform policy.
        :param account_id: Who consented.
        :param purpose: What to.
        :param policy_version: The version that must have been accepted.
        :returns: The record, or ``None`` if there is none in force.
        """
        stmt = _in_hospital(
            select(PatientConsentRecord).where(
                PatientConsentRecord.account_id == account_id,
                PatientConsentRecord.purpose == purpose,
                PatientConsentRecord.policy_version == policy_version,
                PatientConsentRecord.withdrawn_at.is_(None),
            ),
            hospital_id,
        )
        result = await self._session.execute(stmt.execution_options(populate_existing=True))
        return result.scalar_one_or_none()

    async def list_active(
        self, hospital_id: uuid.UUID | None, *, account_id: uuid.UUID, purpose: str
    ) -> list[PatientConsentRecord]:
        """Every consent in force for one purpose, whatever its version.

        :param hospital_id: The hospital, or ``None`` for a platform policy.
        :param account_id: Who consented.
        :param purpose: What to.
        :returns: The records in force, oldest first.
        """
        stmt = _in_hospital(
            select(PatientConsentRecord).where(
                PatientConsentRecord.account_id == account_id,
                PatientConsentRecord.purpose == purpose,
                PatientConsentRecord.withdrawn_at.is_(None),
            ),
            hospital_id,
        ).order_by(PatientConsentRecord.granted_at, PatientConsentRecord.id)
        result = await self._session.execute(stmt.execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def has_ever(
        self, hospital_id: uuid.UUID | None, *, account_id: uuid.UUID, purpose: str
    ) -> bool:
        """Whether the account has ever consented to a purpose, withdrawn or not.

        :param hospital_id: The hospital, or ``None`` for a platform policy.
        :param account_id: Who consented.
        :param purpose: What to.
        :returns: ``True`` if any record exists.
        """
        stmt = _in_hospital(
            select(PatientConsentRecord).where(
                PatientConsentRecord.account_id == account_id,
                PatientConsentRecord.purpose == purpose,
            ),
            hospital_id,
        ).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def withdraw(self, record: PatientConsentRecord, *, now: datetime) -> None:
        """End a consent. The row stays as evidence that it was once given.

        :param record: A record an earlier read returned.
        :param now: When it was withdrawn.
        """
        record.withdrawn_at = now
        await self._session.flush()
