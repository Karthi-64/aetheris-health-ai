"""Authentication API routes — login, logout, refresh, password management, MFA.

See ``docs/modules/01-authentication.md`` §9 for the full API contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.dependencies.auth import get_current_user
from app.api.dependencies.services import get_auth_service
from app.core.envelope import success_envelope
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    MfaConfirmRequest,
    MfaDisableRequest,
    MfaEnrollRequest,
    MfaVerifyRequest,
    RefreshTokenRequest,
    ResetPasswordRequest,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _get_device_info(request: Request) -> str | None:
    """Extract user-agent from the request headers."""
    return request.headers.get("user-agent")


def _get_ip_address(request: Request) -> str | None:
    """Extract client IP from the request."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post(
    "/login",
    summary="Login with email and password",
    description="Authenticate a user and return access + refresh tokens. If MFA is enabled, returns an MFA ticket instead.",
    responses={
        200: {"description": "Login successful (with or without MFA required)."},
        401: {"description": "Invalid credentials."},
        429: {"description": "Rate limited."},
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Authenticate with email and password."""
    result = await auth_service.login(
        email=payload.email,
        password=payload.password,
        device_info=_get_device_info(request),
        ip_address=_get_ip_address(request),
    )

    if "mfa_ticket" in result:
        return success_envelope(
            "MFA required.",
            data={
                "mfa_ticket": result["mfa_ticket"],
                "expires_in": result["expires_in"],
            },
        )

    return success_envelope(
        "Logged in.",
        data={
            "access_token": result["access_token"],
            "refresh_token": result["refresh_token"],
            "expires_in": result["expires_in"],
            "user": result["user"],
        },
    )


@router.post(
    "/mfa/verify",
    summary="Verify MFA code",
    description="Complete MFA verification and receive access + refresh tokens.",
    responses={
        200: {"description": "MFA verification successful."},
        401: {"description": "Invalid MFA ticket or code."},
    },
)
async def verify_mfa(
    payload: MfaVerifyRequest,
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Complete MFA verification after login."""
    result = await auth_service.verify_mfa(
        mfa_ticket=payload.mfa_ticket,
        code=payload.code,
        device_info=_get_device_info(request),
        ip_address=_get_ip_address(request),
    )

    return success_envelope(
        "MFA verified. Logged in.",
        data={
            "access_token": result["access_token"],
            "refresh_token": result["refresh_token"],
            "expires_in": result["expires_in"],
            "user": result["user"],
        },
    )


@router.post(
    "/refresh",
    summary="Refresh access token",
    description="Exchange a refresh token for a new access + refresh token pair (rotation).",
    responses={
        200: {"description": "Token refresh successful."},
        401: {"description": "Invalid or expired refresh token."},
    },
)
async def refresh(
    payload: RefreshTokenRequest,
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Refresh an access token using a refresh token (rotation pattern)."""
    result = await auth_service.refresh_token(
        raw_token=payload.refresh_token,
        device_info=_get_device_info(request),
        ip_address=_get_ip_address(request),
    )

    return success_envelope(
        "Token refreshed.",
        data={
            "access_token": result["access_token"],
            "refresh_token": result["refresh_token"],
            "expires_in": result["expires_in"],
        },
    )


@router.post(
    "/logout",
    summary="Logout",
    description="Revoke the specified refresh token.",
    responses={200: {"description": "Logged out successfully."}},
)
async def logout(
    payload: LogoutRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Logout by revoking the specific refresh token."""
    await auth_service.logout(raw_token=payload.refresh_token)
    return success_envelope("Logged out.")


@router.post(
    "/logout-all",
    summary="Logout all devices",
    description="Revoke ALL refresh tokens for the authenticated user.",
    responses={200: {"description": "All sessions terminated."}},
)
async def logout_all(
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Revoke all refresh tokens for the current user."""
    count = await auth_service.logout_all(current_user.id)
    return success_envelope(f"Logged out of all devices. {count} session(s) terminated.")


@router.post(
    "/password/forgot",
    summary="Request password reset",
    description="Request a password reset token. Generic success response regardless of whether the email exists.",
    responses={200: {"description": "If the email exists, a reset link has been sent."}},
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Request a password reset token (always returns success)."""
    await auth_service.forgot_password(email=payload.email)
    return success_envelope(
        "If an account with that email exists, a password reset link has been sent.",
    )


@router.post(
    "/password/reset",
    summary="Complete password reset",
    description="Complete a password reset using a valid reset token.",
    responses={
        200: {"description": "Password has been reset."},
        400: {"description": "Password does not meet requirements."},
        401: {"description": "Invalid or expired reset token."},
    },
)
async def reset_password(
    payload: ResetPasswordRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Complete a password reset using a reset token."""
    await auth_service.reset_password(
        raw_token=payload.token,
        new_password=payload.new_password,
    )
    return success_envelope("Password has been reset successfully.")


@router.post(
    "/password/change",
    summary="Change password",
    description="Change the password for the authenticated user.",
    responses={
        200: {"description": "Password changed."},
        400: {"description": "Password does not meet requirements."},
        401: {"description": "Current password is incorrect."},
    },
)
async def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Change the current user's password."""
    await auth_service.change_password(
        user_id=current_user.id,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return success_envelope("Password changed successfully.")


@router.post(
    "/mfa/enroll",
    summary="Enroll in MFA",
    description="Initiate MFA enrollment. Returns a TOTP secret and provisioning URI.",
    responses={
        200: {"description": "MFA enrollment initiated."},
        401: {"description": "Invalid password."},
    },
)
async def enroll_mfa(
    payload: MfaEnrollRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Initiate MFA enrollment."""
    result = await auth_service.enroll_mfa(
        user_id=current_user.id,
        password=payload.password,
    )

    return success_envelope(
        "MFA enrollment initiated. Scan the QR code with your authenticator app.",
        data={
            "secret": result["secret"],
            "provisioning_uri": result["provisioning_uri"],
        },
    )


@router.post(
    "/mfa/confirm",
    summary="Confirm MFA enrollment",
    description="Confirm MFA enrollment by verifying a TOTP code.",
    responses={
        200: {"description": "MFA has been enabled."},
        401: {"description": "Invalid MFA code."},
    },
)
async def confirm_mfa(
    payload: MfaConfirmRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Confirm MFA enrollment by verifying a TOTP code."""
    await auth_service.confirm_mfa(
        user_id=current_user.id,
        secret=payload.secret,
        code=payload.code,
    )
    return success_envelope("MFA has been enabled successfully.")


@router.post(
    "/mfa/disable",
    summary="Disable MFA",
    description="Disable MFA for the authenticated user.",
    responses={
        200: {"description": "MFA disabled."},
        401: {"description": "Invalid password or MFA code."},
    },
)
async def disable_mfa(
    payload: MfaDisableRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Disable MFA."""
    await auth_service.disable_mfa(
        user_id=current_user.id,
        password=payload.password,
        code=payload.code,
    )
    return success_envelope("MFA has been disabled.")
