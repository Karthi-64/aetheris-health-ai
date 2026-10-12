"""Groq provider adapter.

Talks to Groq's OpenAI-compatible HTTP API (``POST {base_url}/chat/completions``)
over httpx. There is no vendor SDK: the request and response shapes used here
are the generic OpenAI-compatible ones, so nothing model-specific is sent.

The API key comes from the ``GROQ_API_KEY`` setting and is held only as the
:class:`~pydantic.SecretStr` it arrived as. It is placed in the
``Authorization`` header of each request and nowhere else — never in a URL, a
log field, an exception message or a ``repr``.

**What is read from a reply.** Only ``choices[0].message.content``,
``choices[0].finish_reason`` (kept only when it is a short plain token, else
``"unknown"``), ``model`` and the two token counts. A reasoning
model may also return ``message.reasoning``; it is never read. From an error
body only ``error.code`` and ``error.type`` are read, and only when they look
like identifiers — ``error.message`` can carry organisation ids or model text.

**How failures are raised.** Every failure becomes a typed error from
:mod:`app.ai.errors`, classified inside the ``except`` block and raised after
it, so the original exception (which holds the request and its headers) is not
attached as ``__context__``.

See ``docs/08-AI_ARCHITECTURE.md`` for the provider interface contract.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog
from pydantic import SecretStr

from app.ai.errors import (
    AIError,
    AIModelUnavailableError,
    AIProviderAuthError,
    AIProviderRateLimitedError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    AIResponseInvalidError,
)
from app.ai.providers.base import (
    AIChunk,
    AIProvider,
    AIResponse,
    Message,
    ResponseSchema,
    ToolDefinition,
)
from app.core.config import MODEL_ID_PATTERN

logger = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

#: Roles the chat-completions request may carry. Tool messages are not sent.
_ALLOWED_ROLES = frozenset({"system", "user", "assistant"})

#: Shape an ``error.code`` / ``error.type`` must have to be kept and logged.
_ERROR_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

#: ``error.code`` values meaning the requested model is not served. Groq
#: answers 404 ``model_not_found`` for an unknown id and 400
#: ``model_decommissioned`` for a retired one.
_MODEL_GONE_CODES = frozenset({"model_not_found", "model_decommissioned"})

#: Longest the connect and pool phases may take, whatever the read deadline.
_CONNECT_TIMEOUT_CAP_SECONDS = 3.0

#: Failure kinds logged at ERROR: they need an operator, not a retry.
_OPERATOR_KINDS = frozenset({"auth", "model_unavailable", "not_found"})

_HINT_MODEL = "set AI_FAST_MODEL to a model id Groq currently serves"
_HINT_KEY = "check GROQ_API_KEY"
_HINT_BASE_URL = "check GROQ_BASE_URL, then AI_FAST_MODEL"
_HINT_STRICT_SCHEMA = (
    "if the model rejects strict JSON Schema output, set GROQ_STRICT_JSON_SCHEMA=false"
)

#: ``finish_reason`` reported when the provider sent one that is not a plain token.
_UNKNOWN_FINISH_REASON = "unknown"


def _error_token(value: object) -> str | None:
    """Keep an error identifier only when it is a short, plain token."""
    if isinstance(value, str) and _ERROR_TOKEN_PATTERN.fullmatch(value) is not None:
        return value
    return None


def _token_count(value: object) -> int:
    """Return a token count, or 0 when the provider sent something else."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _classify_http_error(status: int, code: str | None) -> tuple[type[AIError], str, str | None]:
    """Map a non-200 response to a typed error.

    ``error.code`` is consulted before the status, so a wrong base URL (a bare
    404) and a missing model (404 ``model_not_found``) are not confused.

    :param status: The HTTP status.
    :param code: The sanitised ``error.code``, if any.
    :returns: ``(error class, kind, operator hint or None)``.
    """
    if code in _MODEL_GONE_CODES:
        return AIModelUnavailableError, "model_unavailable", _HINT_MODEL
    if status == 403 and code is not None and code.startswith("model_"):
        return AIModelUnavailableError, "model_unavailable", _HINT_MODEL
    if status == 400 and code == "json_validate_failed":
        return AIResponseInvalidError, "json_generation_failed", None
    if status in (401, 403):
        return AIProviderAuthError, "auth", _HINT_KEY
    if status == 429:
        return AIProviderRateLimitedError, "rate_limited", None
    if status == 404:
        return AIProviderUnavailableError, "not_found", _HINT_BASE_URL
    if status in (400, 413, 422):
        return AIProviderUnavailableError, "bad_request", None
    if 500 <= status <= 599:
        return AIProviderUnavailableError, "http_5xx", None
    return AIProviderUnavailableError, "http_error", None


class GroqProvider(AIProvider):
    """Groq over its OpenAI-compatible HTTP API.

    The HTTP client is created on the first :meth:`complete` call, never in
    the constructor: the runtime may be built on a threadpool thread, and a
    client belongs to the event loop that uses it.

    :param api_key: The Groq API key.
    :param base_url: Base URL of the OpenAI-compatible API, no trailing slash.
    :param timeout_seconds: Default read deadline for one call.
    :param strict_json_schema: Send a strict JSON Schema as ``response_format``
        when a schema is given; otherwise plain JSON mode is requested.
    :param transport: HTTP transport override. Tests inject
        :class:`httpx.MockTransport`; production leaves it ``None``.
    """

    name = "groq"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 8.0,
        strict_json_schema: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout_seconds = timeout_seconds
        self._strict_json_schema = strict_json_schema
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def complete(
        self,
        messages: list[Message],
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        tools: list[ToolDefinition] | None = None,
        stream: bool = False,
        *,
        response_schema: ResponseSchema | None = None,
        timeout_seconds: float | None = None,
    ) -> AIResponse | AsyncIterator[AIChunk]:
        """Send one chat completion to Groq.

        Makes exactly one HTTP request: no retry, no redirect, no tool call.

        :param messages: The conversation; roles ``system``, ``user``, ``assistant``.
        :param model: The model id to request.
        :param max_tokens: Completion-token cap. For a reasoning model this
            includes reasoning tokens, so it must leave room for the answer.
        :param temperature: Sampling temperature.
        :param tools: Not supported; must be empty.
        :param stream: Not supported; must be ``False``.
        :param response_schema: JSON Schema the reply must satisfy, if any.
        :param timeout_seconds: Read deadline for this call. ``None`` uses the
            instance default.
        :returns: The normalised response. ``content`` is never blank.
        :raises NotImplementedError: If ``stream`` or ``tools`` is requested.
        :raises ValueError: If a message has an unsupported role.
        :raises AIProviderTimeoutError: The call timed out.
        :raises AIProviderUnavailableError: Groq was unreachable or answered
            with an error (including its auth, rate-limit and model subclasses).
        :raises AIResponseInvalidError: Groq answered, but not with a usable body.
        """
        if stream:
            msg = "GroqProvider does not support streaming."
            raise NotImplementedError(msg)
        if tools:
            msg = "GroqProvider does not support tools."
            raise NotImplementedError(msg)

        body = self._build_body(messages, model, max_tokens, temperature, response_schema)
        deadline = timeout_seconds if timeout_seconds is not None else self._timeout_seconds
        short = min(_CONNECT_TIMEOUT_CAP_SECONDS, deadline)
        timeout = httpx.Timeout(connect=short, read=deadline, write=deadline, pool=short)
        client = self._get_client()

        # Classified inside the except blocks, raised after them — see the
        # module docstring. Every httpx timeout is a TransportError, so the
        # timeout clauses must come first.
        failure: tuple[type[AIError], str, str] | None = None
        response: httpx.Response | None = None
        try:
            response = await client.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                timeout=timeout,
            )
        except httpx.ConnectTimeout as exc:
            failure = (AIProviderTimeoutError, "connect_timeout", type(exc).__name__)
        except httpx.ReadTimeout as exc:
            failure = (AIProviderTimeoutError, "read_timeout", type(exc).__name__)
        except httpx.TimeoutException as exc:
            failure = (AIProviderTimeoutError, "timeout", type(exc).__name__)
        except httpx.TransportError as exc:
            failure = (AIProviderUnavailableError, "connection", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 — anything from the HTTP call is "unavailable"
            failure = (AIProviderUnavailableError, "request_error", type(exc).__name__)

        if failure is not None or response is None:
            error_cls, kind, error_type = failure or (
                AIProviderUnavailableError,
                "request_error",
                "NoResponse",
            )
            raise self._failure(error_cls, kind, model=model, error_type=error_type)

        if response.status_code != httpx.codes.OK:
            code, error_kind = self._read_error_tokens(response)
            error_cls, kind, hint = _classify_http_error(response.status_code, code)
            if kind == "bad_request" and "response_format" in body and self._strict_json_schema:
                # The one request-shape choice an operator can undo by setting.
                hint = _HINT_STRICT_SCHEMA
            raise self._failure(
                error_cls,
                kind,
                model=model,
                status=response.status_code,
                provider_error_code=code,
                provider_error_type=error_kind,
                hint=hint,
            )

        return self._parse_success(response, model)

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Generate embeddings via Groq's API.

        :raises NotImplementedError: Always — embeddings are not wired.
        """
        raise NotImplementedError(f"{type(self).__name__}.embed() is not yet implemented.")

    async def aclose(self) -> None:
        """Close the HTTP client, if one was created. Safe to call twice."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating it on first use.

        ``trust_env=False`` keeps proxy variables and ``~/.netrc`` from
        changing where the key is sent or which credentials are used.
        Redirects are not followed, so the key cannot be replayed elsewhere.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
                headers={"Accept": "application/json", "User-Agent": "aetheris-health-ai"},
            )
        return self._client

    def _build_body(
        self,
        messages: list[Message],
        model: str,
        max_tokens: int,
        temperature: float,
        response_schema: ResponseSchema | None,
    ) -> dict[str, Any]:
        """Build the request body: generic OpenAI-compatible keys only."""
        serialised: list[dict[str, str]] = []
        for message in messages:
            if message.role not in _ALLOWED_ROLES:
                msg = "GroqProvider accepts only system, user and assistant messages."
                raise ValueError(msg)
            serialised.append({"role": message.role, "content": message.content})

        body: dict[str, Any] = {
            "model": model,
            "messages": serialised,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
            "stream": False,
        }
        if response_schema is not None:
            if self._strict_json_schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_schema.name,
                        "strict": True,
                        "schema": response_schema.schema,
                    },
                }
            else:
                body["response_format"] = {"type": "json_object"}
        return body

    @staticmethod
    def _read_error_tokens(response: httpx.Response) -> tuple[str | None, str | None]:
        """Read ``error.code`` and ``error.type`` from an error body.

        Nothing else is read. ``error.message`` is deliberately ignored.

        :returns: ``(code, type)``, each ``None`` unless a plain token.
        """
        payload: object = None
        try:
            payload = response.json()
        except (ValueError, RecursionError):
            # RecursionError: a body nested deeper than the decoder allows.
            payload = None
        if not isinstance(payload, dict):
            return None, None
        error = payload.get("error")
        if not isinstance(error, dict):
            return None, None
        return _error_token(error.get("code")), _error_token(error.get("type"))

    def _parse_success(self, response: httpx.Response, model: str) -> AIResponse:
        """Turn a 200 body into an :class:`AIResponse`, or raise a typed error."""
        payload: object = None
        unreadable = False
        try:
            payload = response.json()
        except (ValueError, RecursionError):
            # Not raised here: a JSONDecodeError keeps the whole body in `.doc`.
            # RecursionError: a body nested deeper than the decoder allows.
            unreadable = True

        message: object = None
        finish_reason = "stop"
        if not unreadable and isinstance(payload, dict):
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                reason = choices[0].get("finish_reason")
                if isinstance(reason, str) and reason:
                    # It reaches the interaction log, so it is kept only when
                    # it is a short plain token, like the error identifiers.
                    finish_reason = _error_token(reason) or _UNKNOWN_FINISH_REASON

        if not isinstance(payload, dict) or not isinstance(message, dict):
            raise self._failure(
                AIResponseInvalidError, "provider_body_malformed", model=model, status=200
            )

        # Only `content` is read. A reasoning model's `reasoning` is ignored.
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise self._failure(
                AIResponseInvalidError, "provider_body_malformed", model=model, status=200
            )
        if content is None or not content.strip():
            # A reasoning model that spends the whole budget thinking returns
            # no content with finish_reason "length": report that as such.
            kind = "truncated" if finish_reason == "length" else "empty_content"
            raise self._failure(AIResponseInvalidError, kind, model=model, status=200)

        returned_model = payload.get("model")
        usage = payload.get("usage")
        usage_map: dict[str, Any] = usage if isinstance(usage, dict) else {}
        return AIResponse(
            content=content,
            finish_reason=finish_reason,
            input_tokens=_token_count(usage_map.get("prompt_tokens")),
            output_tokens=_token_count(usage_map.get("completion_tokens")),
            model=(
                returned_model
                if isinstance(returned_model, str)
                and MODEL_ID_PATTERN.fullmatch(returned_model) is not None
                else model
            ),
        )

    def _failure(
        self,
        error_cls: type[AIError],
        kind: str,
        *,
        model: str,
        status: int | None = None,
        provider_error_code: str | None = None,
        provider_error_type: str | None = None,
        error_type: str | None = None,
        hint: str | None = None,
    ) -> AIError:
        """Log one ``ai_provider_error`` event and build the typed error.

        The caller raises the returned error, outside any ``except`` block.
        Nothing from the request, the response body or the original exception
        is logged — only the class name of a transport failure.
        """
        fields: dict[str, Any] = {
            "provider": self.name,
            "model": model,
            "kind": kind,
            "provider_status": status,
            "provider_error_code": provider_error_code,
            "provider_error_type": provider_error_type,
            "error_type": error_type,
        }
        if hint is not None:
            fields["hint"] = hint
        if kind in _OPERATOR_KINDS:
            logger.error("ai_provider_error", **fields)
        else:
            logger.warning("ai_provider_error", **fields)

        if issubclass(error_cls, AIModelUnavailableError):
            return error_cls(kind, model=model)
        return error_cls(kind)


__all__ = ["GroqProvider"]
