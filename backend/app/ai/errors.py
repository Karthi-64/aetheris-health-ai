"""Typed errors of the AI runtime.

Every failure on an AI path leaves the runtime as one of these, so a caller —
and the global exception handler — can tell "not configured", "provider
unavailable", "timed out" and "answer rejected" apart without parsing text.

Two rules keep secrets and model text out of logs and responses:

- **Messages are static.** ``default_message`` is returned to the browser and
  logged by the global handler, so it never contains anything dynamic and no
  ``detail`` is ever attached. The variable part of a failure is ``kind``, a
  short machine reason that goes to the logs only.
- **Raise outside ``except``.** An exception raised inside an ``except`` block
  keeps the original attached as ``__context__``. An httpx exception carries
  the request (with its ``Authorization`` header), a ``JSONDecodeError`` keeps
  the document, a Pydantic error renders its input. Callers therefore record
  the failure in a local, leave the block, and raise the typed error after it.
"""

from __future__ import annotations

from typing import ClassVar

from app.core.error_codes import ErrorCode
from app.core.exceptions import AetherisError

__all__ = [
    "AIError",
    "AIModelUnavailableError",
    "AINotConfiguredError",
    "AIProviderAuthError",
    "AIProviderRateLimitedError",
    "AIProviderTimeoutError",
    "AIProviderUnavailableError",
    "AIResponseInvalidError",
]


class AIError(AetherisError):
    """Base for every AI runtime failure. Maps to HTTP 503.

    The class attributes are deliberately not named ``message`` and
    ``error_code``: the base constructor assigns those as instance variables,
    and a ``ClassVar`` may not override an instance variable.

    :param kind: Stable machine reason, for logs. Never shown to a user.
    """

    default_message: ClassVar[str] = "The AI service is unavailable right now."
    code: ClassVar[ErrorCode] = ErrorCode.AI_PROVIDER_UNAVAILABLE

    def __init__(self, kind: str) -> None:
        super().__init__(
            message=self.default_message,
            detail=None,
            status_code=503,
            error_code=self.code,
        )
        self.kind = kind


class AINotConfiguredError(AIError):
    """AI is not configured on this server (no key, or the kill switch is off)."""

    default_message = "AI suggestions are not configured on this server."
    code = ErrorCode.AI_NOT_CONFIGURED


class AIProviderUnavailableError(AIError):
    """The provider could not be used: unreachable, or it answered with an error.

    Because this covers both cases, no message for it may claim the provider
    was unreachable.
    """

    default_message = "The AI service is unavailable right now. Choose a slot manually."
    code = ErrorCode.AI_PROVIDER_UNAVAILABLE


class AIProviderAuthError(AIProviderUnavailableError):
    """The provider rejected the credentials."""


class AIProviderRateLimitedError(AIProviderUnavailableError):
    """The provider rate-limited the call, or this process is at its own cap."""


class AIModelUnavailableError(AIProviderUnavailableError):
    """The provider does not serve the requested model.

    :param kind: Stable machine reason.
    :param model: The model id that was requested.
    """

    def __init__(self, kind: str, *, model: str) -> None:
        super().__init__(kind)
        self.model = model


class AIProviderTimeoutError(AIError):
    """The provider did not answer within the deadline."""

    default_message = "The AI service took too long to respond. Choose a slot manually."
    code = ErrorCode.AI_PROVIDER_TIMEOUT


class AIResponseInvalidError(AIError):
    """The provider answered, but the answer could not be used."""

    default_message = "The AI suggestion could not be used. Choose a slot manually or try again."
    code = ErrorCode.AI_RESPONSE_INVALID
