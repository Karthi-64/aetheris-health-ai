"""DTOs for record access grants (§8.3). No endpoint returns them yet."""

from __future__ import annotations

# NOTE: ``date``/``datetime``/``UUID`` must be imported at runtime, not under
# TYPE_CHECKING — Pydantic resolves annotations against the module globals.
from datetime import date, datetime  # noqa: TC003
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, Field

from app.models.patient_consent import (  # noqa: TC001 — Pydantic needs them at runtime
    GranteeType,
    GrantPurposeNote,
    GrantStatus,
    RecordCategory,
)

__all__ = ["AccessGrantView"]


class AccessGrantView(BaseModel):
    """One grant as its grantor sees it. ``status`` is derived at read time."""

    id: UUID
    hospital_id: UUID = Field(description="The source hospital, which holds the records.")
    grantee_type: GranteeType
    grantee_id: UUID
    grantee_hospital_id: UUID
    purpose_note: GrantPurposeNote
    categories: list[RecordCategory]
    records_from: date | None
    records_to: date | None
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    status: GrantStatus
