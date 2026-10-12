"""Errors only the Patient App raises.

Each has one fixed message. Several of them stand for more than one cause on
purpose — a wrong code, an expired code and a used code are one answer — so a
message here must never be made more specific at the call site.
"""

from __future__ import annotations

from typing import Final

from app.core.error_codes import ErrorCode
from app.core.exceptions import AetherisError

__all__ = [
    "OTP_THROTTLE_RETRY_AFTER_SECONDS",
    "ConsentRequiredError",
    "LinkMrnRequiredError",
    "LinkUnavailableError",
    "OtpInvalidError",
    "OtpThrottledError",
    "RecordLinkRequiredError",
]

#: The ``Retry-After`` sent with every OTP throttle refusal. One fixed value,
#: whichever limit was reached: the real wait would say which one it was.
OTP_THROTTLE_RETRY_AFTER_SECONDS: Final = 600


class OtpInvalidError(AetherisError):
    """A one-time code was not accepted — for any reason at all.

    Unknown, expired or used challenge, wrong code, attempts exhausted, a
    throttled source, a suspended account: one status, one code, one message.
    Maps to HTTP 401.
    """

    def __init__(self) -> None:
        super().__init__(
            message="The code is incorrect or has expired.",
            status_code=401,
            error_code=ErrorCode.OTP_INVALID,
        )


class OtpThrottledError(AetherisError):
    """A limit on requesting one-time codes was reached.

    Says nothing about which limit. Maps to HTTP 429 with a fixed
    ``Retry-After``.
    """

    def __init__(self) -> None:
        super().__init__(
            message="Too many requests. Please try again later.",
            status_code=429,
            error_code=ErrorCode.OTP_THROTTLED,
        )
        #: Response headers; the exception handlers pass them through.
        self.headers: dict[str, str] = {"Retry-After": str(OTP_THROTTLE_RETRY_AFTER_SECONDS)}


class LinkMrnRequiredError(AetherisError):
    """More than one record matches; the MRN is needed to tell them apart.

    Says only that an MRN is needed — never how many records there are.
    Maps to HTTP 409.
    """

    def __init__(self) -> None:
        super().__init__(
            message="More information is needed to find your record. Please enter your MRN.",
            status_code=409,
            error_code=ErrorCode.LINK_MRN_REQUIRED,
        )


class LinkUnavailableError(AetherisError):
    """Linking cannot be completed in the app. Maps to HTTP 403."""

    def __init__(self) -> None:
        super().__init__(
            message="Please contact the hospital.",
            status_code=403,
            error_code=ErrorCode.LINK_UNAVAILABLE,
        )


class ConsentRequiredError(AetherisError):
    """A required policy or purpose consent has not been given. Maps to HTTP 403."""

    def __init__(self) -> None:
        super().__init__(
            message="Please review and accept the current policies to continue.",
            status_code=403,
            error_code=ErrorCode.CONSENT_REQUIRED,
        )


class RecordLinkRequiredError(AetherisError):
    """The action needs a linked record at this hospital. Maps to HTTP 403."""

    def __init__(self) -> None:
        super().__init__(
            message="Link your record at this hospital to continue.",
            status_code=403,
            error_code=ErrorCode.RECORD_LINK_REQUIRED,
        )
