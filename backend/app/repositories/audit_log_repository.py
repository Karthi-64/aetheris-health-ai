"""Repository for the :class:`~app.models.audit_log.AuditLog` model.

The audit trail is append-only (``docs/modules/12-audit-logs.md``): this
repository deliberately exposes no update or delete methods, and migration
0010 revokes UPDATE/DELETE from the application role. Reads are always scoped
to one hospital (CLAUDE.md rule 5).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.models.audit_log import AuditLog
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class AuditLogRepository(BaseRepository[AuditLog]):
    """Append-only persistence for audit entries.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(AuditLog, session)

    async def add_entry(
        self,
        *,
        hospital_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        actor_type: str,
        action: str,
        target_type: str | None,
        target_id: uuid.UUID | None,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        context: dict[str, Any] | None,
        request_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        patient_account_id: uuid.UUID | None = None,
    ) -> AuditLog:
        """Append one audit entry.

        The row is flushed but not committed: it joins the caller's
        transaction so an audit entry and the change it describes commit — or
        roll back — together.

        :returns: The pending :class:`AuditLog` instance.
        """
        entry = AuditLog(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            patient_account_id=patient_account_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before=before,
            after=after,
            context=context,
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def search(
        self,
        hospital_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: uuid.UUID | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        q: str | None = None,
        skip: int = 0,
        limit: int = 25,
    ) -> list[AuditLog]:
        """Search one hospital's trail, newest first.

        :param hospital_id: Tenant to scope to — always the caller's.
        :param actor_id: Exact actor match.
        :param action: Exact dotted action match, e.g. ``user.invited``.
        :param target_type: Exact target type match.
        :param target_id: Exact target id match.
        :param start: Inclusive lower bound on ``created_at``.
        :param end: Inclusive upper bound on ``created_at``.
        :param q: Free-text match on action or target type (≥3 chars, §11).
        :param skip: Rows to skip (offset).
        :param limit: Maximum rows to return.
        :returns: Matching entries ordered by ``created_at`` descending.
        """
        stmt = select(AuditLog).where(AuditLog.hospital_id == hospital_id)

        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_user_id == actor_id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if target_type is not None:
            stmt = stmt.where(AuditLog.target_type == target_type)
        if target_id is not None:
            stmt = stmt.where(AuditLog.target_id == target_id)
        if start is not None:
            stmt = stmt.where(AuditLog.created_at >= start)
        if end is not None:
            stmt = stmt.where(AuditLog.created_at <= end)
        if q is not None:
            pattern = f"%{q.lower()}%"
            stmt = stmt.where(
                func.lower(AuditLog.action).like(pattern)
                | func.lower(AuditLog.target_type).like(pattern)
            )

        stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        stmt = self._apply_pagination(stmt, skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_search(
        self,
        hospital_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: uuid.UUID | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        q: str | None = None,
    ) -> int:
        """Count entries matching the same filters as :meth:`search`."""
        stmt = select(func.count()).select_from(AuditLog).where(AuditLog.hospital_id == hospital_id)

        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_user_id == actor_id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if target_type is not None:
            stmt = stmt.where(AuditLog.target_type == target_type)
        if target_id is not None:
            stmt = stmt.where(AuditLog.target_id == target_id)
        if start is not None:
            stmt = stmt.where(AuditLog.created_at >= start)
        if end is not None:
            stmt = stmt.where(AuditLog.created_at <= end)
        if q is not None:
            pattern = f"%{q.lower()}%"
            stmt = stmt.where(
                func.lower(AuditLog.action).like(pattern)
                | func.lower(AuditLog.target_type).like(pattern)
            )

        result = await self._session.execute(stmt)
        return int(result.scalar_one())
