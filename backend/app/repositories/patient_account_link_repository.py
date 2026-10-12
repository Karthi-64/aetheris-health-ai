"""Data access for patient record links.

``patient_account_links`` is tenant data: a link names a patient record, so
every method here takes the hospital that owns it, an explicit
:func:`~app.core.tenancy.cross_tenant` marker, or a row a scoped read already
returned. The one deliberately cross-hospital read is "every active link of
this account" — an account's own links are the only way to learn which
hospitals it is linked at.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import datetime  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.models.patient_account import LinkRelationship, PatientAccountLink
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.tenancy import CrossTenant

__all__ = ["PatientAccountLinkRepository"]


class PatientAccountLinkRepository(BaseRepository[PatientAccountLink]):
    """Reads and writes record links.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(PatientAccountLink, session)

    async def lock_account_in_hospital(self, hospital_id: uuid.UUID, account_id: uuid.UUID) -> None:
        """Serialise link and registration attempts of one account at one hospital.

        A transaction-scoped advisory lock keyed on the pair: a second attempt
        waits here until the first has committed, and then sees what it did.
        It reads and writes no row, and is released when the transaction ends.

        :param hospital_id: The hospital the attempt is made at.
        :param account_id: The account making it.
        """
        key = f"patient-link:{hospital_id}:{account_id}"
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
        )

    async def create_link(
        self,
        hospital_id: uuid.UUID,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        verified_via: str,
        now: datetime,
    ) -> PatientAccountLink:
        """Insert an active ``self`` link.

        The partial unique indexes refuse a second active link for the same
        record, or a second active ``self`` link for the same account here.

        :param hospital_id: The hospital that owns the record.
        :param account_id: The account being linked.
        :param patient_id: The record, already resolved inside ``hospital_id``.
        :param verified_via: How the link was established.
        :param now: When it was.
        :returns: The new link.
        """
        return await super().create(
            hospital_id=hospital_id,
            account_id=account_id,
            patient_id=patient_id,
            relationship=LinkRelationship.SELF.value,
            verified_via=verified_via,
            linked_at=now,
        )

    async def get_active_self(
        self, hospital_id: uuid.UUID, account_id: uuid.UUID
    ) -> PatientAccountLink | None:
        """The account's active ``self`` link at one hospital, if it has one.

        :param hospital_id: The hospital to look in.
        :param account_id: The account.
        :returns: The link, or ``None``.
        """
        result = await self._session.execute(
            select(PatientAccountLink)
            .where(
                PatientAccountLink.hospital_id == hospital_id,
                PatientAccountLink.account_id == account_id,
                PatientAccountLink.relationship == LinkRelationship.SELF.value,
                PatientAccountLink.unlinked_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_active_for_patient(
        self, hospital_id: uuid.UUID, patient_id: uuid.UUID
    ) -> PatientAccountLink | None:
        """The active link to one patient record, whichever account holds it.

        :param hospital_id: The hospital that owns the record.
        :param patient_id: The record.
        :returns: The link, or ``None``.
        """
        result = await self._session.execute(
            select(PatientAccountLink)
            .where(
                PatientAccountLink.hospital_id == hospital_id,
                PatientAccountLink.patient_id == patient_id,
                PatientAccountLink.unlinked_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def list_active_for_account(
        self, account_id: uuid.UUID, scope: uuid.UUID | CrossTenant
    ) -> list[PatientAccountLink]:
        """Every active link of one account, oldest first.

        :param account_id: The account, from a verified patient token.
        :param scope: A hospital id, or a ``cross_tenant(...)`` marker for the
            account's own cross-hospital list.
        :returns: The account's active links.
        """
        stmt = self._confine_to_tenant(
            select(PatientAccountLink).where(
                PatientAccountLink.account_id == account_id,
                PatientAccountLink.unlinked_at.is_(None),
            ),
            scope,
            "list_active_for_account",
        ).order_by(PatientAccountLink.linked_at, PatientAccountLink.id)
        result = await self._session.execute(stmt.execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def end_link(self, link: PatientAccountLink, *, now: datetime, reason: str) -> None:
        """End a link. The row stays as history.

        :param link: A link a scoped read returned.
        :param now: When it ended.
        :param reason: Why, as a short fixed word.
        """
        link.unlinked_at = now
        link.unlink_reason = reason
        await self._session.flush()
