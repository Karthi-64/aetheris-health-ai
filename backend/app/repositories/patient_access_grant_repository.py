"""Data access for patient record access grants.

``patient_access_grants`` is tenant data: ``hospital_id`` is the *source*
hospital, the one that holds the records a grant shares. Every method takes
it, or a row a scoped read already returned. Grants are inserted and revoked;
none is deleted, and no status is stored.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import date, datetime  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.patient_consent import PatientAccessGrant
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.patient_consent import RecordCategory

__all__ = ["PatientAccessGrantRepository"]


class PatientAccessGrantRepository(BaseRepository[PatientAccessGrant]):
    """Reads and writes access grants.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(PatientAccessGrant, session)

    async def create_grant(
        self,
        hospital_id: uuid.UUID,
        *,
        patient_id: uuid.UUID,
        grantor_account_id: uuid.UUID,
        grantee_type: str,
        grantee_id: uuid.UUID,
        grantee_hospital_id: uuid.UUID,
        purpose_note: str,
        categories: Sequence[RecordCategory],
        records_from: date | None,
        records_to: date | None,
        granted_at: datetime,
        expires_at: datetime,
    ) -> PatientAccessGrant:
        """Insert one grant.

        :param hospital_id: The source hospital, which holds the records.
        :param patient_id: The record being shared, resolved inside it.
        :param grantor_account_id: The account giving the grant.
        :param grantee_type: ``hospital`` or ``doctor``.
        :param grantee_id: The recipient hospital or doctor.
        :param grantee_hospital_id: The hospital the recipient belongs to.
        :param purpose_note: Why, from the fixed list.
        :param categories: The record categories shared.
        :param records_from: Earliest record date shared, if bounded.
        :param records_to: Latest record date shared, if bounded.
        :param granted_at: When the grant was given.
        :param expires_at: When it dies.
        :returns: The new grant.
        """
        return await super().create(
            hospital_id=hospital_id,
            patient_id=patient_id,
            grantor_account_id=grantor_account_id,
            grantee_type=grantee_type,
            grantee_id=grantee_id,
            grantee_hospital_id=grantee_hospital_id,
            purpose_note=purpose_note,
            categories=list(categories),
            records_from=records_from,
            records_to=records_to,
            granted_at=granted_at,
            expires_at=expires_at,
        )

    async def get_for_grantor(
        self, hospital_id: uuid.UUID, grant_id: uuid.UUID, *, grantor_account_id: uuid.UUID
    ) -> PatientAccessGrant | None:
        """One grant, only if this account gave it at this hospital.

        :param hospital_id: The source hospital.
        :param grant_id: The grant.
        :param grantor_account_id: The account that must have given it.
        :returns: The grant, or ``None``.
        """
        result = await self._session.execute(
            select(PatientAccessGrant)
            .where(
                PatientAccessGrant.hospital_id == hospital_id,
                PatientAccessGrant.id == grant_id,
                PatientAccessGrant.grantor_account_id == grantor_account_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def list_for_grantor(
        self, hospital_id: uuid.UUID, *, grantor_account_id: uuid.UUID
    ) -> list[PatientAccessGrant]:
        """Every grant an account has given at one hospital, newest first.

        :param hospital_id: The source hospital.
        :param grantor_account_id: The account.
        :returns: Its grants there, whatever their status.
        """
        result = await self._session.execute(
            select(PatientAccessGrant)
            .where(
                PatientAccessGrant.hospital_id == hospital_id,
                PatientAccessGrant.grantor_account_id == grantor_account_id,
            )
            .order_by(PatientAccessGrant.granted_at.desc(), PatientAccessGrant.id)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def list_unrevoked_for_recipient(
        self,
        hospital_id: uuid.UUID,
        *,
        patient_id: uuid.UUID,
        grantee_hospital_id: uuid.UUID,
    ) -> list[PatientAccessGrant]:
        """The grants on one record that name a recipient hospital and are not revoked.

        Candidates only: expiry, the recipient, the category, the record
        window and the grantor's link are all judged by the service, against
        each row, at the time of the read.

        :param hospital_id: The source hospital.
        :param patient_id: The record.
        :param grantee_hospital_id: The hospital the reader belongs to.
        :returns: The candidate grants, newest first.
        """
        result = await self._session.execute(
            select(PatientAccessGrant)
            .where(
                PatientAccessGrant.hospital_id == hospital_id,
                PatientAccessGrant.patient_id == patient_id,
                PatientAccessGrant.grantee_hospital_id == grantee_hospital_id,
                PatientAccessGrant.revoked_at.is_(None),
            )
            .order_by(PatientAccessGrant.granted_at.desc(), PatientAccessGrant.id)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def revoke(
        self,
        grant: PatientAccessGrant,
        *,
        now: datetime,
        account_id: uuid.UUID,
        reason: str | None,
    ) -> None:
        """Revoke a grant. The row and its history stay.

        :param grant: A grant a scoped read returned.
        :param now: When it was revoked.
        :param account_id: The account revoking it.
        :param reason: Why, if the patient said.
        """
        grant.revoked_at = now
        grant.revoked_by_account_id = account_id
        grant.revoke_reason = reason
        await self._session.flush()
