"""DTOs for the patient's own account and record links (§27.3, §27.4)."""

from __future__ import annotations

# NOTE: ``date``/``datetime``/``UUID`` must be imported at runtime, not under
# TYPE_CHECKING — Pydantic resolves annotations against the module globals.
from datetime import date, datetime  # noqa: TC003
from typing import Annotated
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models.patient import Gender  # noqa: TC001 — Pydantic needs it at runtime
from app.schemas.patient import MAX_PATIENT_AGE_YEARS
from app.schemas.patient_app.auth import (  # noqa: TC001 — Pydantic needs them at runtime
    PatientAccountSummary,
    PendingPolicy,
)
from app.utils.datetime import age, utc_today

__all__ = [
    "LinkRecordRequest",
    "PatientLink",
    "PatientMeResponse",
    "PatientProfile",
    "RegisterRecordRequest",
    "RegisterRecordResponse",
]

#: A name as a patient types it: trimmed, and not blank once trimmed.
_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
_PolicyVersion = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)
]


class PatientLink(BaseModel):
    """One of the account's record links. Says nothing about the record itself."""

    hospital_id: UUID = Field(description="The hospital the record is at.")
    hospital_ref: str | None = Field(
        description=(
            "That hospital's public reference (its code), as hospital discovery uses it. "
            "Null if the hospital has no code a reference can spell."
        )
    )
    hospital_name: str = Field(description="That hospital's name.")
    linked_at: datetime = Field(description="When the link was made (UTC).")
    suspended: bool = Field(
        description="True when the link is not honoured right now and must be made again."
    )


class PatientMeResponse(BaseModel):
    """``GET /patient/me``."""

    account: PatientAccountSummary
    links: list[PatientLink]
    pending_policies: list[PendingPolicy]


class LinkRecordRequest(BaseModel):
    """Payload for ``POST /patient/hospitals/{hospital_ref}/link``.

    The phone number is not here and cannot be: it is the one the account
    proved at sign-in.
    """

    model_config = ConfigDict(extra="forbid")

    date_of_birth: date = Field(description="The patient's date of birth.")
    mrn: str | None = Field(
        default=None, max_length=30, description="Medical Record Number, when asked for."
    )
    consent_policy_version: _PolicyVersion = Field(
        description="The version of the record-link consent text that was shown."
    )

    @field_validator("mrn")
    @classmethod
    def _blank_mrn_is_absent(cls, value: str | None) -> str | None:
        """Trim the MRN and treat a blank one as not supplied."""
        if value is None:
            return None
        return value.strip() or None


class RegisterRecordRequest(BaseModel):
    """Payload for ``POST /patient/hospitals/{hospital_ref}/register``."""

    model_config = ConfigDict(extra="forbid")

    first_name: _Name = Field(description="Given name.")
    last_name: _Name = Field(description="Family name.")
    date_of_birth: date = Field(description="Date of birth.")
    gender: Gender = Field(description="male, female, other, or unspecified.")
    consent_policy_version: _PolicyVersion = Field(
        description="The version of the registration consent text that was shown."
    )

    @field_validator("date_of_birth")
    @classmethod
    def _check_date_of_birth(cls, value: date) -> date:
        """Hold a new record to the patient rules: not in the future, not implausibly old."""
        today = utc_today()
        if value > today:
            msg = "Date of birth cannot be in the future."
            raise ValueError(msg)
        if age(value, today) > MAX_PATIENT_AGE_YEARS:
            msg = f"Date of birth cannot be more than {MAX_PATIENT_AGE_YEARS} years ago."
            raise ValueError(msg)
        return value


class PatientProfile(BaseModel):
    """The patient's own record, as far as registration shows it."""

    mrn: str = Field(description="Medical Record Number at this hospital.")
    first_name: str
    last_name: str
    date_of_birth: date
    gender: Gender


class RegisterRecordResponse(BaseModel):
    """A newly registered record and the link to it."""

    link: PatientLink
    profile: PatientProfile
