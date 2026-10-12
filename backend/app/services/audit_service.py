"""Audit service — the durable, database-backed implementation of AuditSink.

`docs/modules/12-audit-logs.md` owns this module. It does two jobs:

**Write path.** ``record()`` persists an :class:`~app.core.audit.AuditEvent`
to ``audit_logs`` inside a SAVEPOINT, so a failure of the audit backend can
never poison the caller's business transaction (the contract in
:class:`~app.core.audit.AuditSink`). The structlog line is still emitted —
audit (compliance) and logs (observability) are different streams
(``docs/03-ARCHITECTURE.md`` §11) and both are wanted.

**Read path.** Tenant-scoped search/export for ``GET /api/v1/audit-logs``,
with actor display names resolved for the audit search page (§12).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Any

import structlog
from pydantic_core import to_jsonable_python

from app.core.audit import AuditEvent, StructlogAuditSink
from app.core.constants import AUDIT_ACTOR_TYPE_PATIENT
from app.core.exceptions import NotFoundError
from app.schemas.audit import AuditLogResponse
from app.schemas.common import Page, PaginationParams

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User
    from app.repositories.audit_log_repository import AuditLogRepository
    from app.repositories.user_repository import UserRepository

__all__ = ["AuditService"]

logger = structlog.get_logger("aetheris.audit")

#: Shared structlog sink — values are redacted there (see its docstring).
_structlog_sink = StructlogAuditSink()


def _jsonable(value: Any) -> Any:
    """Convert ``value`` to something the JSONB columns can serialize.

    Audit diffs carry domain values — ``date`` of birth, ``Decimal``
    consultation fees, ``UUID`` ids — that ``json.dumps`` refuses, and a raw
    insert would raise inside the SAVEPOINT, silently drop the audit row, and
    only leave a warning in the logs. Pydantic's encoder turns dates into ISO
    strings, Decimals into numbers and UUIDs into strings; anything it still
    cannot handle is stringified so the row survives (an audit entry must
    never be lost over a serialization quirk).
    """
    try:
        return to_jsonable_python(value)
    except Exception:  # noqa: BLE001 — a lossy string is better than no audit row
        return str(value)


class AuditService:
    """Persist and query the append-only audit trail.

    :param session: Request session; used only to open the SAVEPOINT.
    :param audit_repo: Append-only audit persistence.
    :param user_repo: Resolves actor ids to display names for the read path.
    """

    def __init__(
        self,
        session: AsyncSession,
        audit_repo: AuditLogRepository,
        user_repo: UserRepository,
    ) -> None:
        self._session = session
        self._audit_repo = audit_repo
        self._user_repo = user_repo

    # ── AuditSink ─────────────────────────────────────────────────────────────

    async def record(self, event: AuditEvent) -> None:
        """Persist one audit event without ever blocking the business action.

        The insert runs in a SAVEPOINT: if it fails (table missing, constraint,
        disk) the savepoint rolls back, the caller's transaction stays usable,
        and the failure is logged rather than raised.
        """
        # Observability line first — it survives even if persistence fails.
        await _structlog_sink.record(event)

        # A patient is a different kind of principal: never a user id, and
        # never recorded as the system (which is what a missing user id means
        # for a staff event).
        is_patient = event.actor_type == "patient"
        if is_patient:
            actor_type = AUDIT_ACTOR_TYPE_PATIENT
        else:
            actor_type = "user" if event.actor_id else "system"

        try:
            async with self._session.begin_nested():
                await self._audit_repo.add_entry(
                    hospital_id=event.hospital_id,
                    actor_user_id=None if is_patient else event.actor_id,
                    actor_type=actor_type,
                    patient_account_id=event.patient_account_id if is_patient else None,
                    ip_address=event.ip_address,
                    user_agent=event.user_agent,
                    action=event.action,
                    target_type=event.target_type,
                    target_id=event.target_id,
                    before=_jsonable({k: v.get("before") for k, v in event.changes.items()})
                    or None,
                    after=_jsonable({k: v.get("after") for k, v in event.changes.items()}) or None,
                    context=_jsonable(event.context) or None,
                )
        except Exception:  # noqa: BLE001 — audit failure must not abort the caller
            logger.warning(
                "audit_persist_failed",
                action=event.action,
                hospital_id=str(event.hospital_id),
                exc_info=True,
            )

    # ── Read path ─────────────────────────────────────────────────────────────

    async def get_log(self, hospital_id: uuid.UUID, log_id: uuid.UUID) -> AuditLogResponse:
        """Return one entry, refusing rows from any other tenant.

        :raises NotFoundError: If the entry does not exist or belongs to a
            different hospital — the tenant check is the security boundary,
            so a miss and a foreign row look identical to the caller.
        """
        row = await self._audit_repo.get_by_id(log_id, hospital_id)
        if row is None or row.hospital_id != hospital_id:
            msg = "Audit entry not found."
            raise NotFoundError(msg)
        actors = await self._resolve_actors([row], hospital_id)
        return self._to_response(row, actors)

    async def list_logs(
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
        pagination: PaginationParams | None = None,
    ) -> Page[AuditLogResponse]:
        """Search one hospital's trail, newest first (module spec §9).

        :returns: A :class:`Page` of entries with actor names resolved.
        """
        pagination = pagination or PaginationParams()

        rows = await self._audit_repo.search(
            hospital_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            start=start,
            end=end,
            q=q,
            skip=pagination.offset,
            limit=pagination.limit,
        )
        total = await self._audit_repo.count_search(
            hospital_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            start=start,
            end=end,
            q=q,
        )
        actors = await self._resolve_actors(rows, hospital_id)
        items = [self._to_response(row, actors) for row in rows]

        return Page[AuditLogResponse](
            items=items,
            page=pagination.page,
            page_size=pagination.page_size,
            total_records=total,
        )

    async def export_logs(
        self,
        hospital_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
        target_type: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        q: str | None = None,
        limit: int = 1000,
    ) -> list[AuditLogResponse]:
        """Return up to ``limit`` entries for the export endpoint (§9).

        Exports are capped synchronously — an async job with email delivery
        (§12) needs a job runner the MVP does not have.
        """
        rows = await self._audit_repo.search(
            hospital_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            start=start,
            end=end,
            q=q,
            skip=0,
            limit=limit,
        )
        actors = await self._resolve_actors(rows, hospital_id)
        return [self._to_response(row, actors) for row in rows]

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _resolve_actors(
        self, rows: list[Any], hospital_id: uuid.UUID
    ) -> dict[uuid.UUID, User]:
        """Batch-load the actors referenced by ``rows`` (one query, not N).

        Confined to ``hospital_id``: an entry's actor is a member of the same
        hospital as the entry, so a user of another hospital is never resolved
        into this hospital's trail.
        """
        actor_ids = {row.actor_user_id for row in rows if row.actor_user_id is not None}
        if not actor_ids:
            return {}
        users = await self._user_repo.get_by_ids(list(actor_ids), hospital_id)
        return {user.id: user for user in users}

    @staticmethod
    def _to_response(row: Any, actors: dict[uuid.UUID, User]) -> AuditLogResponse:
        """Build the API shape for one audit row."""
        actor = actors.get(row.actor_user_id) if row.actor_user_id else None
        actor_name = None
        actor_email = None
        if actor is not None:
            first = (actor.first_name or "").strip()
            last = (actor.last_name or "").strip()
            actor_name = f"{first} {last}".strip() or actor.email
            actor_email = actor.email
        return AuditLogResponse(
            id=row.id,
            action=row.action,
            actor_id=row.actor_user_id,
            actor_name=actor_name,
            actor_email=actor_email,
            actor_type=row.actor_type,
            target_type=row.target_type,
            target_id=row.target_id,
            before=row.before,
            after=row.after,
            context=row.context,
            created_at=row.created_at,
        )
