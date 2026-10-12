"""Error code catalog.

The single source of truth for the ``error_code`` field of the failure
envelope (``docs/06-API_STANDARDS.md`` §5.4). Frontend and SDK clients branch
on these values, so a code's string is part of the public API contract:
add codes freely, never rename or repurpose one.

:class:`ErrorCode` is a :class:`~enum.StrEnum`, so members compare equal to and
serialize as their string value.

Usage::

    from app.core.error_codes import ErrorCode, http_status_for

    ErrorCode.VALIDATION_ERROR == "VALIDATION_ERROR"   # True
    http_status_for(ErrorCode.RESOURCE_NOT_FOUND)      # 404
"""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus


class ErrorCode(StrEnum):
    """Application error codes returned in the failure envelope."""

    # ── Client errors ───────────────────────────────────────────────────────
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"
    BUSINESS_RULE_VIOLATION = "BUSINESS_RULE_VIOLATION"
    RATE_LIMITED = "RATE_LIMITED"
    FEATURE_DISABLED = "FEATURE_DISABLED"

    # ── Patient App (docs/modules/15-patient-app.md §27.2) ──────────────────
    OTP_INVALID = "OTP_INVALID"
    OTP_THROTTLED = "OTP_THROTTLED"
    LINK_MRN_REQUIRED = "LINK_MRN_REQUIRED"
    LINK_UNAVAILABLE = "LINK_UNAVAILABLE"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    RECORD_LINK_REQUIRED = "RECORD_LINK_REQUIRED"

    # ── Server errors ───────────────────────────────────────────────────────
    AI_PROVIDER_UNAVAILABLE = "AI_PROVIDER_UNAVAILABLE"
    AI_NOT_CONFIGURED = "AI_NOT_CONFIGURED"
    AI_PROVIDER_TIMEOUT = "AI_PROVIDER_TIMEOUT"
    AI_RESPONSE_INVALID = "AI_RESPONSE_INVALID"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Canonical HTTP status for each error code. Kept beside the catalog so the
#: mapping documented in ``docs/06-API_STANDARDS.md`` §5.4 lives in exactly one
#: place.
ERROR_CODE_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.AUTHENTICATION_REQUIRED: HTTPStatus.UNAUTHORIZED,
    ErrorCode.ACCOUNT_LOCKED: HTTPStatus.FORBIDDEN,
    ErrorCode.ACCOUNT_SUSPENDED: HTTPStatus.FORBIDDEN,
    ErrorCode.PERMISSION_DENIED: HTTPStatus.FORBIDDEN,
    ErrorCode.RESOURCE_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.RESOURCE_CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.BUSINESS_RULE_VIOLATION: HTTPStatus.BAD_REQUEST,
    ErrorCode.RATE_LIMITED: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.FEATURE_DISABLED: HTTPStatus.FORBIDDEN,
    ErrorCode.OTP_INVALID: HTTPStatus.UNAUTHORIZED,
    ErrorCode.OTP_THROTTLED: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.LINK_MRN_REQUIRED: HTTPStatus.CONFLICT,
    ErrorCode.LINK_UNAVAILABLE: HTTPStatus.FORBIDDEN,
    ErrorCode.CONSENT_REQUIRED: HTTPStatus.FORBIDDEN,
    ErrorCode.RECORD_LINK_REQUIRED: HTTPStatus.FORBIDDEN,
    ErrorCode.AI_PROVIDER_UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.AI_NOT_CONFIGURED: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.AI_PROVIDER_TIMEOUT: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.AI_RESPONSE_INVALID: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.SERVICE_UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.CONFIGURATION_ERROR: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCode.INTERNAL_ERROR: HTTPStatus.INTERNAL_SERVER_ERROR,
}


def http_status_for(code: ErrorCode) -> int:
    """Return the canonical HTTP status for an error code.

    :param code: The error code to look up.
    :returns: The HTTP status, or 500 for a code with no explicit mapping.
    """
    return int(ERROR_CODE_HTTP_STATUS.get(code, HTTPStatus.INTERNAL_SERVER_ERROR))
