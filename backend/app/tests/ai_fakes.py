"""Test doubles for the AI runtime.

Nothing here can reach a network or see a real key:

- outbound HTTP is :class:`httpx.MockTransport` only;
- every :class:`~app.core.config.Settings` is built with ``_env_file=None``
  and the fake key below, never from ``backend/.env`` or the singleton.

Log assertions use :class:`RecordingLogger`, swapped in for a module's
``logger`` with ``monkeypatch.setattr``. ``structlog.testing.capture_logs`` is
not used: once anything imports ``app.main``, logging is configured with
``cache_logger_on_first_use=True`` and an already-bound logger stops honouring
it — negative "the key is not in the logs" assertions would pass vacuously.
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any

import httpx

from app.ai.constants import DEFAULT_HINT_MAPPING, ModelHint
from app.ai.runtime import AIRuntime, build_ai_runtime
from app.core.config import Settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    import pytest

    GroqHandler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]

__all__ = [
    "AI_LOGGER_MODULES",
    "CHAT_URL",
    "FAKE_GROQ_KEY",
    "FAST_MODEL",
    "RecordingHandler",
    "RecordingLogger",
    "ai_settings",
    "contains_any",
    "groq_error",
    "groq_reply",
    "install_loggers",
    "make_runtime",
    "mock_transport",
]

#: A sentinel, not a credential. Tests assert it never appears in a log
#: record, an exception or a response body.
FAKE_GROQ_KEY = "test-groq-key-not-real"

#: The model the ``fast`` hint resolves to by default.
FAST_MODEL = DEFAULT_HINT_MAPPING[ModelHint.FAST][1]

#: Where the adapter posts by default.
CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

#: Every module on the AI path that logs through a module-level ``logger``.
AI_LOGGER_MODULES = (
    "app.ai.providers.groq",
    "app.ai.runtime",
    "app.ai.services.ai_service",
    "app.services.slot_ranker",
    "app.services.appointment_service",
)


class RecordingLogger:
    """A logger double that keeps every call."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def _record(self, level: str, event: str, kwargs: dict[str, Any]) -> None:
        self.entries.append({"event": event, "level": level, **kwargs})

    def debug(self, event: str, **kwargs: Any) -> None:
        """Record a debug call."""
        self._record("debug", event, kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        """Record an info call."""
        self._record("info", event, kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        """Record a warning call."""
        self._record("warning", event, kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        """Record an error call."""
        self._record("error", event, kwargs)

    def exception(self, event: str, **kwargs: Any) -> None:
        """Record an exception call."""
        self._record("exception", event, kwargs)

    def events(self, name: str) -> list[dict[str, Any]]:
        """Return the entries logged under one event name."""
        return [entry for entry in self.entries if entry["event"] == name]

    def text(self) -> str:
        """Everything recorded, as one string, for leak checks."""
        return repr(self.entries)


def install_loggers(
    monkeypatch: pytest.MonkeyPatch, modules: Iterable[str] = AI_LOGGER_MODULES
) -> RecordingLogger:
    """Swap the module-level ``logger`` of each AI module for one recorder."""
    recorder = RecordingLogger()
    for module in modules:
        monkeypatch.setattr(f"{module}.logger", recorder)
    return recorder


def contains_any(haystacks: Iterable[str], needles: Iterable[str]) -> bool:
    """Tell whether any needle occurs in any haystack.

    Returns a bare boolean so a failing assertion prints ``False is True``
    rather than the strings — one of which may be a secret-shaped value.
    """
    needle_list = [needle for needle in needles if needle]
    return any(needle in haystack for haystack in haystacks for needle in needle_list)


def groq_reply(
    content: str | None,
    *,
    model: str | None = FAST_MODEL,
    finish_reason: str | None = "stop",
    prompt_tokens: int = 211,
    completion_tokens: int = 84,
    reasoning: str | None = None,
) -> httpx.Response:
    """Build a 200 chat-completion response in Groq's OpenAI-compatible shape."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )


def groq_error(
    status: int,
    code: str | None = None,
    *,
    error_type: str | None = "invalid_request_error",
    message: str = "provider error text that must never be surfaced",
) -> httpx.Response:
    """Build an OpenAI-style error response: ``{"error": {message, type, code}}``."""
    error: dict[str, Any] = {"message": message}
    if error_type is not None:
        error["type"] = error_type
    if code is not None:
        error["code"] = code
    return httpx.Response(status, json={"error": error})


class RecordingHandler:
    """A ``MockTransport`` handler that keeps every request it is given."""

    def __init__(self, handler: GroqHandler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the request, then answer with the wrapped handler."""
        self.requests.append(request)
        result = self._handler(request)
        if inspect.isawaitable(result):
            return await result
        return result

    def bodies(self) -> list[dict[str, Any]]:
        """The JSON body of each recorded request."""
        return [json.loads(request.content) for request in self.requests]

    def last_body(self) -> dict[str, Any]:
        """The JSON body of the most recent request."""
        return self.bodies()[-1]


def ai_settings(**overrides: Any) -> Settings:
    """Build test settings that cannot see ``backend/.env`` or a real key."""
    values: dict[str, Any] = {"GROQ_API_KEY": FAKE_GROQ_KEY, **overrides}
    # `_env_file` is a pydantic-settings init argument, not a declared field.
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def mock_transport(handler: GroqHandler) -> httpx.MockTransport:
    """Wrap a sync or async handler as a ``MockTransport``."""

    async def _handle(request: httpx.Request) -> httpx.Response:
        result = handler(request)
        if inspect.isawaitable(result):
            return await result
        return result

    return httpx.MockTransport(_handle)


def make_runtime(handler: GroqHandler, **settings_overrides: Any) -> AIRuntime:
    """Build an AI runtime whose provider talks to ``handler`` and nothing else."""
    return build_ai_runtime(ai_settings(**settings_overrides), transport=mock_transport(handler))
