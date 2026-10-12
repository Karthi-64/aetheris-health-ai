"""Unit tests for the Groq adapter.

Outbound HTTP is :class:`httpx.MockTransport` throughout — nothing here can
reach a network, and the key is the sentinel from ``ai_fakes``.

Two things are pinned beyond the happy path: the mapping from every kind of
failure to a typed error, and that neither the key, nor the prompt, nor the
provider's own text can leave the adapter in a log record or an exception.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
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
from app.ai.providers.base import AIResponse, Message, ResponseSchema, ToolDefinition
from app.ai.providers.groq import GroqProvider
from app.tests import conftest
from app.tests.ai_fakes import (
    CHAT_URL,
    FAKE_GROQ_KEY,
    FAST_MODEL,
    RecordingHandler,
    RecordingLogger,
    contains_any,
    groq_error,
    groq_reply,
    install_loggers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

PROMPT_MARKER = "PROMPT-MARKER-7f3a"
REPLY_MARKER = "REPLY-MARKER-91bc"
PROVIDER_TEXT_MARKER = "PROVIDER-TEXT-MARKER-5d2e"

MESSAGES = [
    Message(role="system", content=f"system text {PROMPT_MARKER}"),
    Message(role="user", content=f"user text {PROMPT_MARKER}"),
]
SCHEMA = ResponseSchema(
    name="slot_recommendation",
    schema={
        "type": "object",
        "properties": {
            "slot_id": {"type": "string", "enum": ["S1", "S2"]},
            "reason": {"type": "string"},
        },
        "required": ["slot_id", "reason"],
        "additionalProperties": False,
    },
)
VALID_CONTENT = json.dumps({"slot_id": "S1", "reason": REPLY_MARKER})

#: The two bodies Groq really returned for the repository's previous model ids.
DECOMMISSIONED = {
    "error": {
        "message": f"The model has been decommissioned. {PROVIDER_TEXT_MARKER}",
        "type": "invalid_request_error",
        "code": "model_decommissioned",
    }
}
NOT_FOUND = {
    "error": {
        "message": f"The model does not exist. {PROVIDER_TEXT_MARKER}",
        "type": "invalid_request_error",
        "code": "model_not_found",
    }
}


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    """Record what the adapter logs."""
    return install_loggers(monkeypatch, ["app.ai.providers.groq"])


@pytest.fixture
async def make_provider() -> AsyncGenerator[Callable[..., GroqProvider]]:
    """Build providers on a fake transport and close them afterwards."""
    built: list[GroqProvider] = []

    def _make(handler: Any, **kwargs: Any) -> GroqProvider:
        provider = GroqProvider(
            api_key=SecretStr(FAKE_GROQ_KEY),
            transport=httpx.MockTransport(handler),
            **kwargs,
        )
        built.append(provider)
        return provider

    yield _make
    for provider in built:
        await provider.aclose()


async def _complete(provider: GroqProvider, **kwargs: Any) -> AIResponse:
    """Call the adapter the way ``AIService`` does."""
    result = await provider.complete(
        messages=MESSAGES,
        model=FAST_MODEL,
        max_tokens=1024,
        temperature=0.0,
        response_schema=kwargs.pop("response_schema", SCHEMA),
        **kwargs,
    )
    assert isinstance(result, AIResponse)
    return result


async def _failure(provider: GroqProvider, **kwargs: Any) -> AIError:
    """Run a call that must fail with a typed error, and return the error."""
    with pytest.raises(AIError) as caught:
        await _complete(provider, **kwargs)
    return caught.value


def _assert_no_leak(exc: AIError, log: RecordingLogger) -> None:
    """The leak check: nothing secret or model-written left the adapter."""
    assert log.entries, "the failure must be logged, or this check proves nothing"
    haystacks = [str(exc), repr(exc), repr(exc.detail), exc.message, log.text()]
    leaked = contains_any(
        haystacks, [FAKE_GROQ_KEY, "Bearer", PROMPT_MARKER, REPLY_MARKER, PROVIDER_TEXT_MARKER]
    )
    assert leaked is False
    # No httpx exception (with its request and headers), JSONDecodeError (with
    # the body) or anything else rides along on the typed error.
    assert exc.__context__ is None
    assert exc.__cause__ is None
    assert exc.detail == {}


class TestSuccess:
    """One request, the documented body, the documented fields read back."""

    async def test_sends_one_post_with_the_bearer_key_and_generic_body(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        handler = RecordingHandler(lambda _request: groq_reply(VALID_CONTENT))
        provider = make_provider(handler)

        response = await _complete(provider)

        assert len(handler.requests) == 1
        request = handler.requests[0]
        assert request.method == "POST"
        assert str(request.url) == CHAT_URL
        # Reduced to a boolean first, so a mismatch never prints the header.
        header_ok = request.headers.get("authorization") == f"Bearer {FAKE_GROQ_KEY}"
        assert header_ok
        assert request.headers["accept"] == "application/json"
        assert request.headers["user-agent"] == "aetheris-health-ai"
        key_in_url = FAKE_GROQ_KEY in str(request.url)
        assert key_in_url is False

        body = handler.last_body()
        assert set(body) == {
            "model",
            "messages",
            "temperature",
            "max_completion_tokens",
            "stream",
            "response_format",
        }
        assert body["model"] == FAST_MODEL
        assert body["temperature"] == 0.0
        assert body["max_completion_tokens"] == 1024
        assert body["stream"] is False
        assert body["messages"] == [
            {"role": "system", "content": MESSAGES[0].content},
            {"role": "user", "content": MESSAGES[1].content},
        ]
        # Nothing model-specific and no way for the model to act.
        for forbidden in ("tools", "tool_choice", "functions", "user", "reasoning_effort"):
            assert forbidden not in body

        assert response.content == VALID_CONTENT
        assert response.model == FAST_MODEL
        assert response.input_tokens == 211
        assert response.output_tokens == 84
        assert response.finish_reason == "stop"
        assert response.tool_calls == []

    async def test_strict_schema_is_the_default_response_format(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        """By default the model is structurally limited to the candidate ids."""
        handler = RecordingHandler(lambda _request: groq_reply(VALID_CONTENT))

        await _complete(make_provider(handler))

        assert handler.last_body()["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "slot_recommendation",
                "strict": True,
                "schema": SCHEMA.schema,
            },
        }

    async def test_json_mode_when_strict_schema_is_turned_off(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        handler = RecordingHandler(lambda _request: groq_reply(VALID_CONTENT))

        await _complete(make_provider(handler, strict_json_schema=False))

        assert handler.last_body()["response_format"] == {"type": "json_object"}

    @pytest.mark.parametrize("strict", [True, False])
    async def test_no_schema_means_no_response_format(
        self, make_provider: Callable[..., GroqProvider], strict: bool
    ) -> None:
        handler = RecordingHandler(lambda _request: groq_reply("plain text"))

        await _complete(make_provider(handler, strict_json_schema=strict), response_schema=None)

        assert "response_format" not in handler.last_body()

    async def test_timeout_argument_reaches_the_request(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        handler = RecordingHandler(lambda _request: groq_reply(VALID_CONTENT))
        provider = make_provider(handler, timeout_seconds=8.0)

        await _complete(provider, timeout_seconds=5.0)
        await _complete(provider, timeout_seconds=1.5)
        await _complete(provider)

        timeouts = [request.extensions["timeout"] for request in handler.requests]
        assert timeouts[0] == {"connect": 3.0, "read": 5.0, "write": 5.0, "pool": 3.0}
        assert timeouts[1] == {"connect": 1.5, "read": 1.5, "write": 1.5, "pool": 1.5}
        assert timeouts[2] == {"connect": 3.0, "read": 8.0, "write": 8.0, "pool": 3.0}

    @pytest.mark.parametrize("returned", [None, "", "   ", "bad model id!", 17])
    async def test_blank_or_odd_response_model_falls_back_to_the_requested_one(
        self, make_provider: Callable[..., GroqProvider], returned: Any
    ) -> None:
        provider = make_provider(lambda _request: groq_reply(VALID_CONTENT, model=returned))

        response = await _complete(provider)

        assert response.model == FAST_MODEL

    async def test_odd_usage_and_missing_finish_reason_get_safe_defaults(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "some/other-model",
                    "choices": [{"message": {"content": VALID_CONTENT}}],
                    "usage": {"prompt_tokens": -4, "completion_tokens": "many"},
                },
            )

        response = await _complete(make_provider(handler))

        assert response.model == "some/other-model"
        assert response.input_tokens == 0
        assert response.output_tokens == 0
        assert response.finish_reason == "stop"

    @pytest.mark.parametrize(
        "reason",
        [
            "stop\nINJECTED level=error " + "A" * 5000,
            "has spaces",
            "x" * 65,
            f"{REPLY_MARKER} said so",
        ],
        ids=["newline-and-long", "spaces", "too-long", "free-text"],
    )
    async def test_finish_reason_that_is_not_a_plain_token_becomes_unknown(
        self, make_provider: Callable[..., GroqProvider], reason: str
    ) -> None:
        """It is the one response string that reaches the interaction log."""
        provider = make_provider(lambda _request: groq_reply(VALID_CONTENT, finish_reason=reason))

        response = await _complete(provider)

        assert response.finish_reason == "unknown"
        assert response.content == VALID_CONTENT

    @pytest.mark.parametrize("reason", ["stop", "length", "content_filter", "tool_calls"])
    async def test_ordinary_finish_reasons_are_kept(
        self, make_provider: Callable[..., GroqProvider], reason: str
    ) -> None:
        provider = make_provider(lambda _request: groq_reply(VALID_CONTENT, finish_reason=reason))

        response = await _complete(provider)

        assert response.finish_reason == reason

    async def test_reasoning_field_of_a_reasoning_model_is_never_read(
        self, make_provider: Callable[..., GroqProvider], log: RecordingLogger
    ) -> None:
        """The answer is ``content``; ``reasoning`` is ignored entirely."""
        provider = make_provider(
            lambda _request: groq_reply(VALID_CONTENT, reasoning=f"thinking {REPLY_MARKER}-R")
        )

        response = await _complete(provider)

        assert response.content == VALID_CONTENT
        leaked = contains_any([repr(response), log.text()], [f"{REPLY_MARKER}-R"])
        assert leaked is False


class TestTimeouts:
    """Every httpx timeout is a timeout error — not "connection"."""

    @pytest.mark.parametrize(
        ("raised", "kind"),
        [
            (httpx.ReadTimeout, "read_timeout"),
            (httpx.ConnectTimeout, "connect_timeout"),
            (httpx.WriteTimeout, "timeout"),
            (httpx.PoolTimeout, "timeout"),
        ],
    )
    async def test_timeouts_map_to_the_timeout_error(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        raised: type[httpx.TimeoutException],
        kind: str,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise raised(f"timed out {PROVIDER_TEXT_MARKER}", request=request)

        exc = await _failure(make_provider(handler))

        # Pins the order of the except clauses: every httpx timeout is also a
        # TransportError, which would otherwise win.
        assert type(exc) is AIProviderTimeoutError
        assert not isinstance(exc, AIProviderUnavailableError)
        assert exc.kind == kind
        assert exc.error_code == "AI_PROVIDER_TIMEOUT"
        entry = log.events("ai_provider_error")[0]
        assert entry["kind"] == kind
        assert entry["error_type"] == raised.__name__
        assert entry["provider_status"] is None
        _assert_no_leak(exc, log)


class TestConnectionFailures:
    """The provider could not be reached at all."""

    @pytest.mark.parametrize(
        ("raised", "text"),
        [
            (httpx.ConnectError, "[Errno 61] Connection refused"),
            (httpx.ConnectError, "[Errno 8] nodename nor servname provided, or not known"),
            (httpx.ReadError, "connection reset"),
        ],
    )
    async def test_transport_errors_map_to_connection(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        raised: type[httpx.TransportError],
        text: str,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise raised(f"{text} {PROVIDER_TEXT_MARKER}", request=request)

        exc = await _failure(make_provider(handler))

        assert type(exc) is AIProviderUnavailableError
        assert exc.kind == "connection"
        assert exc.error_code == "AI_PROVIDER_UNAVAILABLE"
        assert log.events("ai_provider_error")[0]["error_type"] == raised.__name__
        _assert_no_leak(exc, log)

    async def test_any_other_exception_from_the_call_is_request_error(
        self, make_provider: Callable[..., GroqProvider], log: RecordingLogger
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise RuntimeError(f"boom {PROVIDER_TEXT_MARKER} Bearer {FAKE_GROQ_KEY}")

        exc = await _failure(make_provider(handler))

        assert type(exc) is AIProviderUnavailableError
        assert exc.kind == "request_error"
        assert log.events("ai_provider_error")[0]["error_type"] == "RuntimeError"
        _assert_no_leak(exc, log)


class TestHttpErrors:
    """A response other than 200, classified by error code first, then status."""

    @pytest.mark.parametrize(
        ("status", "body", "code"),
        [
            (400, DECOMMISSIONED, "model_decommissioned"),
            (404, NOT_FOUND, "model_not_found"),
        ],
    )
    async def test_retired_and_unknown_models_are_model_unavailable(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        status: int,
        body: dict[str, Any],
        code: str,
    ) -> None:
        """The exact bodies Groq returned for the repository's previous model ids."""
        exc = await _failure(make_provider(lambda _request: httpx.Response(status, json=body)))

        assert type(exc) is AIModelUnavailableError
        assert exc.kind == "model_unavailable"
        assert exc.model == FAST_MODEL
        assert exc.error_code == "AI_PROVIDER_UNAVAILABLE"
        entry = log.events("ai_provider_error")[0]
        assert entry["level"] == "error"
        assert entry["model"] == FAST_MODEL
        assert entry["provider_status"] == status
        assert entry["provider_error_code"] == code
        assert entry["provider_error_type"] == "invalid_request_error"
        assert "AI_FAST_MODEL" in entry["hint"]
        _assert_no_leak(exc, log)

    async def test_403_with_a_model_code_is_model_unavailable(
        self, make_provider: Callable[..., GroqProvider], log: RecordingLogger
    ) -> None:
        provider = make_provider(lambda _request: groq_error(403, "model_permission_blocked_org"))

        exc = await _failure(provider)

        assert type(exc) is AIModelUnavailableError
        assert log.events("ai_provider_error")[0]["provider_error_code"] == (
            "model_permission_blocked_org"
        )
        _assert_no_leak(exc, log)

    async def test_json_validate_failed_is_an_invalid_response(
        self, make_provider: Callable[..., GroqProvider], log: RecordingLogger
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Failed to generate JSON",
                        "type": "invalid_request_error",
                        "code": "json_validate_failed",
                        "failed_generation": f"half an answer {REPLY_MARKER}",
                    }
                },
            )

        exc = await _failure(make_provider(handler))

        assert type(exc) is AIResponseInvalidError
        assert exc.kind == "json_generation_failed"
        assert exc.error_code == "AI_RESPONSE_INVALID"
        _assert_no_leak(exc, log)

    @pytest.mark.parametrize(
        "response",
        [
            groq_error(401, "invalid_api_key", message=f"Invalid API Key {PROVIDER_TEXT_MARKER}"),
            groq_error(403, None),
            httpx.Response(403, text="Forbidden"),
        ],
        ids=["401", "403-no-code", "403-not-json"],
    )
    async def test_rejected_credentials_are_an_auth_error(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        response: httpx.Response,
    ) -> None:
        exc = await _failure(make_provider(lambda _request: response))

        assert type(exc) is AIProviderAuthError
        assert exc.kind == "auth"
        assert exc.error_code == "AI_PROVIDER_UNAVAILABLE"
        entry = log.events("ai_provider_error")[0]
        assert entry["level"] == "error"
        assert entry["hint"] == "check GROQ_API_KEY"
        assert "provider_error_code" in entry
        _assert_no_leak(exc, log)

    async def test_429_is_rate_limited(
        self, make_provider: Callable[..., GroqProvider], log: RecordingLogger
    ) -> None:
        provider = make_provider(
            lambda _request: groq_error(429, "rate_limit_exceeded", error_type="tokens")
        )

        exc = await _failure(provider)

        assert type(exc) is AIProviderRateLimitedError
        assert exc.kind == "rate_limited"
        entry = log.events("ai_provider_error")[0]
        assert entry["level"] == "warning"
        assert entry["provider_error_code"] == "rate_limit_exceeded"
        assert entry["provider_error_type"] == "tokens"
        _assert_no_leak(exc, log)

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(404, text="Not Found"),
            groq_error(404, None),
            groq_error(404, "unknown_url"),
        ],
        ids=["bare", "no-code", "unrelated-code"],
    )
    async def test_other_404_points_at_the_base_url(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        response: httpx.Response,
    ) -> None:
        """A wrong path in GROQ_BASE_URL is not mistaken for a missing model."""
        exc = await _failure(make_provider(lambda _request: response))

        assert type(exc) is AIProviderUnavailableError
        assert exc.kind == "not_found"
        entry = log.events("ai_provider_error")[0]
        assert entry["level"] == "error"
        assert "GROQ_BASE_URL" in entry["hint"]
        assert "provider_error_code" in entry
        _assert_no_leak(exc, log)

    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (400, "bad_request"),
            (413, "bad_request"),
            (422, "bad_request"),
            (500, "http_5xx"),
            (502, "http_5xx"),
            (503, "http_5xx"),
            (302, "http_error"),
            (418, "http_error"),
        ],
    )
    async def test_remaining_statuses(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        status: int,
        kind: str,
    ) -> None:
        handler = RecordingHandler(
            lambda _request: httpx.Response(
                status,
                json={"error": {"message": PROVIDER_TEXT_MARKER, "code": "some_code"}},
                headers={"location": "https://elsewhere.example/steal"} if status == 302 else None,
            )
        )

        exc = await _failure(make_provider(handler))

        assert type(exc) is AIProviderUnavailableError
        assert exc.kind == kind
        entry = log.events("ai_provider_error")[0]
        assert entry["provider_status"] == status
        assert entry["provider_error_code"] == "some_code"
        # One request: no retry, and a redirect is not followed.
        assert len(handler.requests) == 1
        _assert_no_leak(exc, log)

    async def test_bad_request_with_a_strict_schema_points_at_the_setting(
        self, make_provider: Callable[..., GroqProvider], log: RecordingLogger
    ) -> None:
        """A model that rejects strict schemas: the operator is told which setting."""
        exc = await _failure(make_provider(lambda _request: groq_error(400, "some_code")))

        assert type(exc) is AIProviderUnavailableError
        assert exc.kind == "bad_request"
        entry = log.events("ai_provider_error")[0]
        assert entry["level"] == "warning"
        assert "GROQ_STRICT_JSON_SCHEMA=false" in entry["hint"]
        _assert_no_leak(exc, log)

    @pytest.mark.parametrize(
        ("provider_kwargs", "call_kwargs"),
        [
            ({"strict_json_schema": False}, {}),
            ({}, {"response_schema": None}),
        ],
        ids=["json-mode", "no-schema"],
    )
    async def test_bad_request_without_a_strict_schema_has_no_hint(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        provider_kwargs: dict[str, Any],
        call_kwargs: dict[str, Any],
    ) -> None:
        provider = make_provider(lambda _request: groq_error(400, "some_code"), **provider_kwargs)

        exc = await _failure(provider, **call_kwargs)

        assert exc.kind == "bad_request"
        assert "hint" not in log.events("ai_provider_error")[0]

    @pytest.mark.parametrize(
        ("status", "error_cls", "kind"),
        [
            (200, AIResponseInvalidError, "provider_body_malformed"),
            (500, AIProviderUnavailableError, "http_5xx"),
            (404, AIProviderUnavailableError, "not_found"),
        ],
    )
    async def test_body_nested_too_deep_to_decode_is_still_a_typed_error(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        status: int,
        error_cls: type[AIError],
        kind: str,
    ) -> None:
        """The decoder's RecursionError must not escape as an untyped 500."""
        depth = 200_000
        deep = b'{"choices": ' + b"[" * depth + b"]" * depth + b"}"
        response = httpx.Response(
            status, content=deep, headers={"content-type": "application/json"}
        )

        exc = await _failure(make_provider(lambda _request: response))

        assert type(exc) is error_cls
        assert exc.kind == kind
        entry = log.events("ai_provider_error")[0]
        assert entry["provider_status"] == status
        assert entry["provider_error_code"] is None
        _assert_no_leak(exc, log)

    async def test_provider_message_is_never_surfaced_and_odd_codes_are_dropped(
        self, make_provider: Callable[..., GroqProvider], log: RecordingLogger
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": f"org_123 said {PROVIDER_TEXT_MARKER}",
                        "type": f"type with spaces {PROVIDER_TEXT_MARKER}",
                        "code": f"code\n{PROVIDER_TEXT_MARKER}",
                    }
                },
            )

        exc = await _failure(make_provider(handler))

        entry = log.events("ai_provider_error")[0]
        assert entry["provider_error_code"] is None
        assert entry["provider_error_type"] is None
        assert exc.message == "The AI service is unavailable right now. Choose a slot manually."
        _assert_no_leak(exc, log)


class TestMalformedSuccessBodies:
    """A 200 that cannot be used is an invalid response, never a success."""

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, text=f"<html>{REPLY_MARKER}</html>"),
            httpx.Response(200, content=b"\xff\xfe not utf-8"),
            httpx.Response(200, json=[]),
            httpx.Response(200, json={}),
            httpx.Response(200, json={"choices": []}),
            httpx.Response(200, json={"choices": "nope"}),
            httpx.Response(200, json={"choices": [{"finish_reason": "stop"}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": ["a", "b"]}}]}),
        ],
        ids=[
            "html",
            "not-utf8",
            "array",
            "empty-object",
            "no-choices",
            "choices-not-a-list",
            "no-message",
            "content-not-a-string",
        ],
    )
    async def test_unreadable_bodies(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        response: httpx.Response,
    ) -> None:
        exc = await _failure(make_provider(lambda _request: response))

        assert type(exc) is AIResponseInvalidError
        assert exc.kind == "provider_body_malformed"
        assert exc.error_code == "AI_RESPONSE_INVALID"
        _assert_no_leak(exc, log)

    @pytest.mark.parametrize("content", [None, "", "   \n\t "])
    async def test_empty_content(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        content: str | None,
    ) -> None:
        exc = await _failure(make_provider(lambda _request: groq_reply(content)))

        assert type(exc) is AIResponseInvalidError
        assert exc.kind == "empty_content"
        _assert_no_leak(exc, log)

    @pytest.mark.parametrize("content", [None, ""])
    async def test_reasoning_model_that_ran_out_of_tokens_before_answering(
        self,
        make_provider: Callable[..., GroqProvider],
        log: RecordingLogger,
        content: str | None,
    ) -> None:
        """No content with finish_reason "length": the budget went on reasoning."""
        provider = make_provider(
            lambda _request: groq_reply(
                content, finish_reason="length", reasoning=f"long thought {REPLY_MARKER}"
            )
        )

        exc = await _failure(provider)

        assert type(exc) is AIResponseInvalidError
        assert exc.kind == "truncated"
        _assert_no_leak(exc, log)

    async def test_finish_reason_length_with_content_is_passed_up_for_the_caller_to_reject(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        provider = make_provider(
            lambda _request: groq_reply('{"slot_id": "S1", "rea', finish_reason="length")
        )

        response = await _complete(provider)

        assert response.finish_reason == "length"


class TestRefusals:
    """What the adapter will not do, decided before any request is built."""

    async def test_tools_are_refused_and_nothing_is_sent(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        handler = RecordingHandler(lambda _request: groq_reply(VALID_CONTENT))
        tool = ToolDefinition(name="book", description="Book it", input_schema={"type": "object"})

        with pytest.raises(NotImplementedError, match="does not support tools"):
            await _complete(make_provider(handler), tools=[tool])

        assert handler.requests == []

    async def test_streaming_is_refused_and_nothing_is_sent(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        handler = RecordingHandler(lambda _request: groq_reply(VALID_CONTENT))

        with pytest.raises(NotImplementedError, match="does not support streaming"):
            await _complete(make_provider(handler), stream=True)

        assert handler.requests == []

    async def test_tool_role_messages_are_refused(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        handler = RecordingHandler(lambda _request: groq_reply(VALID_CONTENT))
        provider = make_provider(handler)

        with pytest.raises(ValueError, match="only system, user and assistant"):
            await provider.complete(
                messages=[Message(role="tool", content="result")], model=FAST_MODEL
            )

        assert handler.requests == []

    async def test_embeddings_are_not_implemented(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        with pytest.raises(NotImplementedError):
            await make_provider(lambda _request: groq_reply("x")).embed(["text"])


class TestLifecycleAndSecrecy:
    """Client lifetime, and where the key can and cannot be seen."""

    async def test_client_is_created_lazily_and_close_is_idempotent(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        provider = make_provider(lambda _request: groq_reply(VALID_CONTENT))
        assert provider._client is None

        await provider.aclose()  # nothing to close yet
        await _complete(provider)
        assert provider._client is not None

        await provider.aclose()
        await provider.aclose()
        assert provider._client is None

    async def test_client_ignores_the_environment_and_does_not_follow_redirects(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        provider = make_provider(lambda _request: groq_reply(VALID_CONTENT))
        await _complete(provider)

        client = provider._client
        assert client is not None
        assert client.trust_env is False
        assert client.follow_redirects is False
        # The key is a per-request header, not a default on the client.
        assert "authorization" not in client.headers

    def test_repr_and_attributes_do_not_expose_the_key(self) -> None:
        provider = GroqProvider(api_key=SecretStr(FAKE_GROQ_KEY))

        exposed = contains_any(
            [repr(provider), str(provider), repr(vars(provider))], [FAKE_GROQ_KEY]
        )

        assert exposed is False

    async def test_trailing_slash_on_the_base_url_is_tolerated(
        self, make_provider: Callable[..., GroqProvider]
    ) -> None:
        handler = RecordingHandler(lambda _request: groq_reply(VALID_CONTENT))

        await _complete(make_provider(handler, base_url="http://localhost:8080/v1/"))

        assert str(handler.requests[0].url) == "http://localhost:8080/v1/chat/completions"

    async def test_the_network_guard_stops_a_provider_with_a_real_transport(
        self, log: RecordingLogger
    ) -> None:
        """A provider built without a fake transport still cannot reach Groq."""
        provider = GroqProvider(api_key=SecretStr(FAKE_GROQ_KEY))
        try:
            exc = await _failure(provider)
        finally:
            await provider.aclose()

        assert type(exc) is AIProviderUnavailableError
        assert exc.kind == "request_error"
        assert conftest.REAL_NETWORK_ATTEMPTS == ["api.groq.com"]
        _assert_no_leak(exc, log)
        # Cleared here so this test's own teardown check passes: the attempt
        # was the point.
        conftest.REAL_NETWORK_ATTEMPTS.clear()
