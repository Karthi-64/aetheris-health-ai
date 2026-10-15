"""DTOs for record access grants (§8.3) and the patient routes of §27.5."""

from __future__ import annotations

# NOTE: ``date``/``datetime``/``UUID`` must be imported at runtime, not under
# TYPE_CHECKING — Pydantic resolves annotations against the module globals.
from datetime import date, datetime  # noqa: TC003
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field

from app.models.patient_consent import (  # noqa: TC001 — Pydantic needs them at runtime
    GranteeType,
    GrantPurposeNote,
    GrantStatus,
    RecordCategory,
)

__all__ = [
    "MAX_REVOKE_TEXT",
    "AccessGrantView",
    "CreateAccessGrant",
    "RevokeAccessGrant",
]

#: The longest text a patient may add when revoking a grant.
MAX_REVOKE_TEXT = 200


class CreateAccessGrant(BaseModel):
    """Body of ``POST /patient/hospitals/{hospital_ref}/access-grants`` (§27.5).

    There is no patient, hospital or ownership field: whose records are shared
    is the caller's own record at the hospital in the path, resolved by the
    server, and any field not listed here is refused.
    """

    model_config = ConfigDict(extra="forbid")

    grantee_type: GranteeType = Field(description="'hospital' to share with one, 'doctor' for one.")
    grantee_id: UUID = Field(description="The recipient hospital, or the recipient doctor.")
    grantee_hospital_id: UUID = Field(description="The hospital the recipient belongs to.")
    categories: list[RecordCategory] = Field(
        description="What is shared. At least one; no category implies another."
    )
    purpose_note: GrantPurposeNote = Field(
        description="Why, from the fixed list: consultation, second_opinion or continuity_of_care."
    )
    expires_at: datetime | None = Field(
        description="When the grant ends. Thirty days from now if absent; never beyond 365."
    )
    records_from: date | None = Field(
        default=None, description="Earliest record date shared, or null for unbounded."
    )
    records_to: date | None = Field(
        default=None, description="Latest record date shared, or null for unbounded."
    )


class RevokeAccessGrant(BaseModel):
    """Body of ``POST /patient/access-grants/{grant_id}/revoke`` (§27.5)."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(
        default=None,
        max_length=MAX_REVOKE_TEXT,
        description="Why the patient is taking the grant back, or nothing.",
    )


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
