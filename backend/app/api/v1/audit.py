"""Audit Logs API routes.

Implements ``docs/modules/12-audit-logs.md`` §9:

- ``GET /audit-logs``             — filtered, paginated search (``audit.read``)
- ``GET /audit-logs/{id}``        — one entry (``audit.read``)
- ``GET /audit-logs/export``      — CSV/JSON download (``audit.export``)

Reads are tenant-scoped: a caller only ever sees entries from their own
hospital (CLAUDE.md rule 5). Validation follows §11 — free-text search at
least 3 characters, date range at most one year per call.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.api.dependencies.auth import require_permission
from app.api.dependencies.services import get_audit_service
from app.core.exceptions import ValidationError
from app.models.user import User
from app.schemas.audit import AuditLogResponse
from app.schemas.common import (
    MetadataWithPagination,
    PaginatedResponse,
    PaginationMeta,
    PaginationParams,
    SuccessResponse,
)
from app.services.audit_service import AuditService

router = APIRouter(prefix="/audit-logs", tags=["Audit Logs"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    422: {"description": "Request failed validation."},
}

#: §11: one call may never span more than a year.
_MAX_RANGE_DAYS = 366

#: §11: free-text search needs at least 3 characters (enforced by Query too;
#: this assert keeps the two rules in one place for the export path).
_MIN_SEARCH_CHARS = 3

_EXPORT_ROW_LIMIT = 1000


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the trail is scoped to.

    :raises ValidationError: If the user belongs to no hospital (a platform
        account has no tenant trail to read).
    """
    if current_user.hospital_id is None:
        msg = "This account is not scoped to a hospital, so the audit trail cannot be read."
        raise ValidationError(msg)
    return current_user.hospital_id


#: Leading characters a spreadsheet treats as the start of a formula.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Neutralise a cell that a spreadsheet would run as a formula.

    The export is opened in Excel or Sheets by a compliance reviewer, and some
    of its cells are user-controlled — a display name or an email. A value such
    as ``=HYPERLINK(...)`` would execute when the file is opened. Prefixing an
    apostrophe makes the application treat the cell as text (the OWASP CSV
    injection guidance); the apostrophe is not shown in the cell.

    :param value: The cell text.
    :returns: The text, prefixed with ``'`` if it starts with a formula character.
    """
    if value.startswith(_CSV_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _validate_range(start: datetime | None, end: datetime | None) -> None:
    """Enforce §11's one-year window and sane ordering.

    The timezone check runs **first**, for both bounds. A naive datetime cannot
    be compared with an aware one — ``end < start`` or ``start > now()`` would
    raise ``TypeError`` and surface as a 500 — and folding the ``from`` check
    into an ``elif`` skipped it entirely whenever ``to`` was also supplied
    (PR #29 review finding 3). A client mistake is a 422, not a server error.

    :raises ValidationError: If a bound is naive, the window exceeds a year, or
        ``to`` is before ``from``.
    """
    if start is not None and start.tzinfo is None:
        msg = "`from` must include a timezone offset."
        raise ValidationError(msg)
    if end is not None and end.tzinfo is None:
        msg = "`to` must include a timezone offset."
        raise ValidationError(msg)
    if start is not None and end is not None:
        if end < start:
            msg = "`to` must not be before `from`."
            raise ValidationError(msg)
        if (end - start).days > _MAX_RANGE_DAYS:
            msg = "Date range must not exceed one year per call."
            raise ValidationError(msg)
    if start is not None and start > datetime.now(UTC):
        msg = "`from` must not be in the future."
        raise ValidationError(msg)


@router.get(
    "",
    response_model=PaginatedResponse[AuditLogResponse],
    summary="Search audit logs",
    description=(
        "Return a page of audit entries for the caller's hospital, newest "
        "first. Filters: `actor_id`, `action`, `target_type`, `target_id`, "
        "`from`, `to` (≤ 1 year) and `q` (≥ 3 chars, matches action or "
        "target type)."
    ),
    responses={200: {"description": "Page of audit entries."}, **_COMMON_RESPONSES},
)
async def list_audit_logs(
    actor_id: uuid.UUID | None = Query(None, description="Exact actor match."),
    action: str | None = Query(None, max_length=100, description="Exact dotted action."),
    target_type: str | None = Query(None, max_length=50, description="Exact target type."),
    target_id: uuid.UUID | None = Query(None, description="Exact target id."),
    start: datetime | None = Query(None, alias="from", description="Inclusive start."),
    end: datetime | None = Query(None, alias="to", description="Inclusive end."),
    q: str | None = Query(
        None, min_length=_MIN_SEARCH_CHARS, max_length=100, description="Free-text search."
    ),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("audit.read")),
    service: AuditService = Depends(get_audit_service),
) -> PaginatedResponse[AuditLogResponse]:
    """Search the audit trail (module spec §9)."""
    _validate_range(start, end)
    pagination = PaginationParams(page=page, page_size=page_size)

    page_result = await service.list_logs(
        _tenant_of(current_user),
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        start=start,
        end=end,
        q=q,
        pagination=pagination,
    )
    return PaginatedResponse[AuditLogResponse](
        message="Audit entries retrieved.",
        data=page_result.items,
        metadata=MetadataWithPagination(
            pagination=PaginationMeta(
                page=page_result.page,
                page_size=page_result.page_size,
                total_records=page_result.total_records,
                total_pages=page_result.total_pages,
            ),
        ),
    )


@router.get(
    "/export",
    summary="Export audit logs",
    description=(
        "Download up to 1000 matching entries as CSV or JSON "
        "(`?format=csv|json`, default json). Synchronous with a hard cap — "
        "the async job + email flow of §12 needs a job runner."
    ),
    responses={
        200: {"description": "File download (content-disposition attached)."},
        **_COMMON_RESPONSES,
    },
)
async def export_audit_logs(
    format: str = Query("json", pattern="^(csv|json)$", description="Export format."),
    actor_id: uuid.UUID | None = Query(None, description="Exact actor match."),
    action: str | None = Query(None, max_length=100, description="Exact dotted action."),
    target_type: str | None = Query(None, max_length=50, description="Exact target type."),
    start: datetime | None = Query(None, alias="from", description="Inclusive start."),
    end: datetime | None = Query(None, alias="to", description="Inclusive end."),
    q: str | None = Query(
        None, min_length=_MIN_SEARCH_CHARS, max_length=100, description="Free-text search."
    ),
    current_user: User = Depends(require_permission("audit.export")),
    service: AuditService = Depends(get_audit_service),
) -> Response:
    """Export the audit trail (module spec §9)."""
    _validate_range(start, end)
    rows = await service.export_logs(
        _tenant_of(current_user),
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        start=start,
        end=end,
        q=q,
        limit=_EXPORT_ROW_LIMIT,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "id",
                "created_at",
                "action",
                "actor_id",
                "actor_name",
                "actor_email",
                "target_type",
                "target_id",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    str(row.id),
                    row.created_at.isoformat(),
                    _csv_safe(row.action),
                    str(row.actor_id) if row.actor_id else "",
                    _csv_safe(row.actor_name or ""),
                    _csv_safe(row.actor_email or ""),
                    _csv_safe(row.target_type or ""),
                    str(row.target_id) if row.target_id else "",
                ]
            )
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="audit-logs-{stamp}.csv"'},
        )

    payload = json.dumps([row.model_dump(mode="json") for row in rows], indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="audit-logs-{stamp}.json"'},
    )


@router.get(
    "/{log_id}",
    response_model=SuccessResponse[AuditLogResponse],
    summary="Get one audit entry",
    description="Return a single audit entry with its before/after diff.",
    responses={
        200: {"description": "Audit entry returned."},
        404: {"description": "No such entry in this hospital."},
        **_COMMON_RESPONSES,
    },
)
async def get_audit_log(
    log_id: uuid.UUID,
    current_user: User = Depends(require_permission("audit.read")),
    service: AuditService = Depends(get_audit_service),
) -> SuccessResponse[AuditLogResponse]:
    """Retrieve one audit entry by UUID (module spec §9)."""
    entry = await service.get_log(_tenant_of(current_user), log_id)
    return SuccessResponse[AuditLogResponse](message="Audit entry retrieved.", data=entry)
