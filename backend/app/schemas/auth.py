"""Pydantic schemas for authentication module.

Request and response models for the auth endpoints.
See ``docs/modules/01-authentication.md`` §9 for the API contract.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — used in Pydantic field annotations

from pydantic import BaseModel, EmailStr, Field

# ── Request Schemas ─────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    """Login credentials payload."""

    email: EmailStr
    password: str = Field(..., min_length=1, description="The user's password")


class MfaVerifyRequest(BaseModel):
    """MFA verification payload."""

    mfa_ticket: str = Field(..., description="MFA ticket from the login response")
    code: str = Field(
        ..., min_length=6, max_length=6, pattern=r"^\d{6}$", description="6-digit TOTP code"
    )


class RefreshTokenRequest(BaseModel):
    """Refresh token request payload."""

    refresh_token: str = Field(..., description="The opaque refresh token")


class LogoutRequest(BaseModel):
    """Logout payload."""

    refresh_token: str = Field(..., description="The refresh token to invalidate")


class ForgotPasswordRequest(BaseModel):
    """Password reset request payload."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Password reset completion payload."""

    token: str = Field(..., description="The password reset token from the email")
    new_password: str = Field(..., min_length=12, max_length=128, description="The new password")


class ChangePasswordRequest(BaseModel):
    """Authenticated password change payload."""

    current_password: str = Field(..., description="The current password")
    new_password: str = Field(..., min_length=12, max_length=128, description="The new password")


class MfaEnrollRequest(BaseModel):
    """MFA enrollment initiation payload."""

    password: str = Field(..., description="Current password for verification")


class MfaConfirmRequest(BaseModel):
    """MFA enrollment confirmation payload."""

    secret: str = Field(..., description="The TOTP secret from the enroll response")
    code: str = Field(
        ...,
        min_length=6,
        max_length=6,
        pattern=r"^\d{6}$",
        description="6-digit TOTP code to verify setup",
    )


class MfaDisableRequest(BaseModel):
    """MFA disable payload."""

    password: str = Field(..., description="Current password for verification")
    code: str = Field(
        ..., min_length=6, max_length=6, pattern=r"^\d{6}$", description="6-digit TOTP code"
    )


# ── Response Schemas ────────────────────────────────────────────────────────


class UserProfileResponse(BaseModel):
    """Public user profile returned in auth responses."""

    id: uuid.UUID
    email: str
    first_name: str
    last_name: str
    phone: str | None = None
    roles: list[str] = []
    status: str
    mfa_enabled: bool
    password_change_required: bool = False

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    """Successful login response (no MFA)."""

    access_token: str
    refresh_token: str
    expires_in: int
    user: UserProfileResponse


class MfaRequiredResponse(BaseModel):
    """Login response when MFA is required."""

    mfa_ticket: str
    expires_in: int


class TokenRefreshResponse(BaseModel):
    """Successful token refresh response."""

    access_token: str
    refresh_token: str
    expires_in: int


class MfaEnrollResponse(BaseModel):
    """MFA enrollment initiation response."""

    secret: str
    provisioning_uri: str
    qr_code_url: str | None = None


class MfaRecoveryCodesResponse(BaseModel):
    """MFA recovery codes response."""

    recovery_codes: list[str]
    message: str = "Store these codes in a safe place. They will not be shown again."


class MessageResponse(BaseModel):
    """Generic message response."""

    message: str
