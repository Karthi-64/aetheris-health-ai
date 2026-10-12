"""Application-wide exception hierarchy.

Every custom exception inherits from :class:`AetherisError`.
The global exception handler in the middleware layer maps these
to the standard HTTP response envelope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.error_codes import ErrorCode


def _to_iso_utc(value: datetime) -> str:
    """Render a datetime as an ISO 8601 UTC string with a ``Z`` suffix.

    ``docs/06-API_STANDARDS.md`` §17: no naive datetimes cross the API
    boundary. A value without tzinfo is assumed to be UTC, matching how every
    timestamp is stored.

    :param value: The datetime to render.
    :returns: e.g. ``2026-07-29T13:20:29Z``.
    """
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class AetherisError(Exception):
    """Base exception for all application-level errors.

    Every custom exception in the codebase inherits from this.
    The global exception handler catches these and maps them to
    structured HTTP responses.

    :param message: Human-readable error description.
    :param detail: Machine-readable details (field errors, context).
    :param status_code: HTTP status code for the error response.
    :param error_code: Application error code for the response envelope.
    """

    def __init__(
        self,
        message: str = "An unexpected error occurred.",
        detail: dict[str, Any] | None = None,
        status_code: int = 500,
        error_code: ErrorCode = ErrorCode.INTERNAL_ERROR,
    ) -> None:
        self.message = message
        self.detail = detail or {}
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(self.message)


class ConfigurationError(AetherisError):
    """Raised when the application is misconfigured.

    This is a startup-time error. It should never surface in production
    because configuration validation happens before the server accepts
    traffic.
    """

    def __init__(
        self,
        message: str = "Application configuration error.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            detail=detail,
            status_code=500,
            error_code=ErrorCode.CONFIGURATION_ERROR,
        )


class NotFoundError(AetherisError):
    """Raised when a requested resource does not exist.

    Maps to HTTP 404.
    """

    def __init__(
        self, message: str = "Resource not found.", detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message=message, detail=detail, status_code=404, error_code=ErrorCode.RESOURCE_NOT_FOUND
        )


class ValidationError(AetherisError):
    """Raised when input validation fails at the business-rule level.

    Maps to HTTP 422. (Pydantic validation errors are handled separately
    by FastAPI's built-in exception handler.)
    """

    def __init__(
        self, message: str = "Validation failed.", detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message=message, detail=detail, status_code=422, error_code=ErrorCode.VALIDATION_ERROR
        )


class BusinessRuleError(AetherisError):
    """Raised when a business rule is violated.

    Maps to HTTP 400. Examples:
    - Attempting to book an appointment in the past.
    - Dispensing more medication than prescribed.
    - Applying a discount above the allowed threshold.
    """

    def __init__(
        self, message: str = "Business rule violation.", detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message=message,
            detail=detail,
            status_code=400,
            error_code=ErrorCode.BUSINESS_RULE_VIOLATION,
        )


class PermissionDeniedError(AetherisError):
    """Raised when the authenticated user lacks the required permission.

    Maps to HTTP 403.
    """

    def __init__(
        self, message: str = "Permission denied.", detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message=message, detail=detail, status_code=403, error_code=ErrorCode.PERMISSION_DENIED
        )


class FeatureDisabledError(AetherisError):
    """Raised when a hospital has not been given a feature.

    Maps to HTTP 403.
    """

    def __init__(
        self,
        message: str = "This feature is not enabled for this hospital.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message, detail=detail, status_code=403, error_code=ErrorCode.FEATURE_DISABLED
        )


class AuthenticationError(AetherisError):
    """Raised when authentication fails.

    Maps to HTTP 401. Used by the auth module (Sprint 2+).
    """

    def __init__(
        self, message: str = "Authentication required.", detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message=message,
            detail=detail,
            status_code=401,
            error_code=ErrorCode.AUTHENTICATION_REQUIRED,
        )


class AccountLockedError(AetherisError):
    """Raised when login is attempted against a temporarily locked account.

    .. note::

       Reserved for non-login flows. The login path intentionally raises the
       generic :class:`AuthenticationError` instead so a failed login cannot
       be used to enumerate valid email addresses (anti-enumeration) — do not
       re-raise this from ``AuthService._check_account_status``.

    If raised, maps to HTTP 403 with ``ACCOUNT_LOCKED`` (lockout is
    time-limited, so the unlock instant lets the client tell the user when to
    retry). Note the login contract has moved to a generic 401; the 403 form
    applies only to non-login callers.

    :param unlock_at: When the lock expires. Serialized into ``errors`` as an
        ISO 8601 UTC timestamp (``docs/06-API_STANDARDS.md`` §17).
    :param message: Human-readable description.
    """

    def __init__(
        self,
        unlock_at: datetime,
        message: str = "Account is locked.",
    ) -> None:
        super().__init__(
            message=message,
            detail={"unlock_at": _to_iso_utc(unlock_at)},
            status_code=403,
            error_code=ErrorCode.ACCOUNT_LOCKED,
        )


class AccountSuspendedError(AetherisError):
    """Raised when login is attempted against a suspended account.

    .. note::

       Reserved for non-login flows. The login path intentionally raises the
       generic :class:`AuthenticationError` instead so a failed login cannot
       be used to enumerate valid email addresses (anti-enumeration) — do not
       re-raise this from ``AuthService._check_account_status``.

    If raised, maps to HTTP 403 with ``ACCOUNT_SUSPENDED``. Unlike a lockout
    this carries no unlock time: suspension is lifted by an administrator, not
    by the clock. The login contract has moved to a generic 401; the 403 form
    applies only to non-login callers.
    """

    def __init__(self, message: str = "Account is suspended.") -> None:
        super().__init__(
            message=message,
            detail=None,
            status_code=403,
            error_code=ErrorCode.ACCOUNT_SUSPENDED,
        )


class ConflictError(AetherisError):
    """Raised when the request conflicts with the current state.

    Maps to HTTP 409. Examples:
    - Duplicate MRN.
    - Appointment time slot already booked.
    - Duplicate invoice number.
    """

    def __init__(
        self, message: str = "Resource conflict.", detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message=message, detail=detail, status_code=409, error_code=ErrorCode.RESOURCE_CONFLICT
        )


class RateLimitError(AetherisError):
    """Raised when the rate limit is exceeded.

    Maps to HTTP 429.
    """

    def __init__(
        self,
        message: str = "Rate limit exceeded. Try again later.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message, detail=detail, status_code=429, error_code=ErrorCode.RATE_LIMITED
        )


class ServiceUnavailableError(AetherisError):
    """Raised when a downstream dependency is unavailable.

    Maps to HTTP 503.
    """

    def __init__(
        self,
        message: str = "Service temporarily unavailable.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            detail=detail,
            status_code=503,
            error_code=ErrorCode.SERVICE_UNAVAILABLE,
        )
