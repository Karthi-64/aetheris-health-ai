"""Pydantic DTOs for the Audit Logs module (docs/modules/12-audit-logs.md)."""

from __future__ import annotations

# NOTE: ``datetime``/``UUID`` must be imported at runtime, not under
# TYPE_CHECKING — Pydantic resolves annotations against the module globals.
from datetime import datetime  # noqa: TC003
from typing import Any  # noqa: TC003
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AuditLogResponse"]


class AuditLogResponse(BaseModel):
    """One audit entry as returned by ``GET /api/v1/audit-logs``."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Entry UUID.")
    action: str = Field(description="Dotted action, e.g. user.invited.")
    actor_id: UUID | None = Field(default=None, description="Acting user; NULL for system.")
    actor_name: str | None = Field(default=None, description="Resolved display name of the actor.")
    actor_email: str | None = Field(
        default=None, description="Resolved email of the actor (admin visibility only)."
    )
    actor_type: str = Field(description="user / system / ai.")
    target_type: str | None = Field(default=None, description="Entity type acted on.")
    target_id: UUID | None = Field(default=None, description="Entity acted on.")
    before: dict[str, Any] | None = Field(
        default=None, description="Field values before the change."
    )
    after: dict[str, Any] | None = Field(default=None, description="Field values after the change.")
    context: dict[str, Any] | None = Field(
        default=None, description="Non-PII context (reason, counts)."
    )
    created_at: datetime = Field(description="When the action occurred.")
