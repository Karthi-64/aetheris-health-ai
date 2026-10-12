"""DTOs for Patient App sign-in (§27.3)."""

from __future__ import annotations

# NOTE: ``UUID`` must be imported at runtime, not under TYPE_CHECKING —
# Pydantic resolves annotations against the module globals.
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "OtpRequest",
    "OtpRequestResponse",
    "OtpVerifyRequest",
    "PatientAccountSummary",
    "PendingPolicy",
    "PatientRefreshResponse",
    "PatientSessionResponse",
]


class OtpRequest(BaseModel):
    """Payload for ``POST /patient/auth/otp/request``."""

    model_config = ConfigDict(extra="forbid")

    phone: str = Field(min_length=4, max_length=32, description="Mobile number to send a code to.")


class OtpRequestResponse(BaseModel):
    """What a request for a code answers — identical for every number."""

    challenge_id: UUID = Field(description="Names this request when the code is verified.")
    expires_in: int = Field(description="Seconds the code is valid for.")
    resend_after: int = Field(description="Seconds to wait before asking for another code.")


class OtpVerifyRequest(BaseModel):
    """Payload for ``POST /patient/auth/otp/verify``."""

    model_config = ConfigDict(extra="forbid")

    challenge_id: UUID = Field(description="The challenge the code was sent for.")
    code: str = Field(pattern=r"^\d{6}$", description="The six-digit code.")


class PatientAccountSummary(BaseModel):
    """The signed-in account. The phone number is never returned in full."""

    id: UUID = Field(description="Account UUID.")
    phone_masked: str = Field(description="The account's phone number, last four digits only.")
    status: str = Field(description="active / suspended / closed.")


class PendingPolicy(BaseModel):
    """A policy the account must accept before using the app."""

    purpose: str = Field(description="The policy.")
    version: str = Field(description="The version that must be accepted.")


class PatientSessionResponse(BaseModel):
    """A started session. The refresh token travels in a cookie, never here."""

    access_token: str = Field(description="Patient access token (bearer).")
    expires_in: int = Field(description="Seconds the access token is valid for.")
    account: PatientAccountSummary
    pending_policies: list[PendingPolicy] = Field(
        description="Policies still to accept. Empty when there are none."
    )


class PatientRefreshResponse(BaseModel):
    """A rotated session."""

    access_token: str = Field(description="Patient access token (bearer).")
    expires_in: int = Field(description="Seconds the access token is valid for.")
