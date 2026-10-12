"""Unit tests for :class:`~app.ai.services.ai_service.AIService`.

Fake providers, no HTTP. What matters here: one provider call per request (no
retry, no fallback), a total deadline, a concurrency cap that refuses rather
than queues, typed errors that carry nothing along, and exactly one
``ai_interaction`` log line on every path.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from app.ai import constants
from app.ai.constants import DEFAULT_HINT_MAPPING, ModelHint
from app.ai.errors import (
    AIError,
    AIModelUnavailableError,
    AINotConfiguredError,
    AIProviderAuthError,
    AIProviderRateLimitedError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    AIResponseInvalidError,
)
from app.ai.prompts.registry import PromptRegistry
from app.ai.providers import AIProviderRegistry
from app.ai.providers.base import (
    AIChunk,
    AIProvider,
    AIResponse,
    Message,
    ResponseSchema,
    ToolDefinition,
)
from app.ai.services.ai_service import AIService
from app.core.exceptions import ServiceUnavailableError
from app.tests.ai_fakes import FAST_MODEL, RecordingLogger, contains_any, install_loggers

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

PROMPT_MARKER = "PROMPT-MARKER-2c41"
REPLY_MARKER = "REPLY-MARKER-aa07"
SECRET_MARKER = "SECRET-MARKER-66f0"

MESSAGES = [Message(role="user", content=f"hello {PROMPT_MARKER}")]
SCHEMA = ResponseSchema(name="s", schema={"type": "object"})
HOSPITAL_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()


class FakeProvider(AIProvider):
    """A provider double that records its calls and answers from a callable."""

    name = "groq"

    def __init__(self, behaviour: Callable[[], Awaitable[Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._behaviour = behaviour

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
        """Record the call and run the configured behaviour."""
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "tools": tools,
                "stream": stream,
                "response_schema": response_schema,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._behaviour is not None:
            result: AIResponse = await self._behaviour()
            return result
        return AIResponse(
            content=f"answer {REPLY_MARKER}",
            model="returned-model",
            input_tokens=120,
            output_tokens=80,
        )

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Not used."""
        raise NotImplementedError


def _service(provider: AIProvider | None, **kwargs: Any) -> AIService:
    """A service over a private registry holding at most one provider."""
    registry = AIProviderRegistry()
    if provider is not None:
        registry.register("groq", provider)
    return AIService(registry, PromptRegistry(), **kwargs)


def _raiser(exc: BaseException) -> Callable[[], Awaitable[Any]]:
    async def _raise() -> Any:
        raise exc

    return _raise


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    """Record what the service logs."""
    return install_loggers(monkeypatch, ["app.ai.services.ai_service"])


def _one_interaction(log: RecordingLogger) -> dict[str, Any]:
    """Exactly one ``ai_interaction`` line per call, on every path."""
    entries = log.events("ai_interaction")
    assert len(entries) == 1
    return entries[0]


class TestResolutionAndForwarding:
    """Hint → (provider, model), and what reaches the provider."""

    async def test_fast_resolves_to_the_repository_mapping(self, log: RecordingLogger) -> None:
        provider = FakeProvider()

        response = await _service(provider).complete(messages=MESSAGES, hint="fast")

        assert DEFAULT_HINT_MAPPING[ModelHint.FAST] == ("groq", "openai/gpt-oss-20b")
        assert provider.calls[0]["model"] == FAST_MODEL == "openai/gpt-oss-20b"
        assert response.provider == "groq"
        assert response.model == "returned-model"

    async def test_register_hint_override_is_honoured(self, log: RecordingLogger) -> None:
        provider = FakeProvider()
        registry = AIProviderRegistry()
        registry.register("groq", provider)
        registry.register_hint(ModelHint.FAST, "groq", "another/model-id")

        await AIService(registry, PromptRegistry()).complete(messages=MESSAGES, hint="fast")

        assert provider.calls[0]["model"] == "another/model-id"
        assert _one_interaction(log)["model"] == "another/model-id"

    async def test_schema_timeout_and_sampling_are_forwarded(self, log: RecordingLogger) -> None:
        provider = FakeProvider()

        await _service(provider, default_timeout_seconds=6.0).complete(
            messages=MESSAGES,
            max_tokens=1024,
            temperature=0.0,
            response_schema=SCHEMA,
            timeout_seconds=2.5,
        )

        call = provider.calls[0]
        assert call["response_schema"] is SCHEMA
        assert call["timeout_seconds"] == 2.5
        assert call["max_tokens"] == 1024
        assert call["temperature"] == 0.0
        assert call["stream"] is False
        assert call["tools"] is None
        assert _one_interaction(log)["structured_output"] == "schema"

    async def test_default_deadline_and_hint_defaults_apply(self, log: RecordingLogger) -> None:
        provider = FakeProvider()

        await _service(provider, default_timeout_seconds=6.0).complete(messages=MESSAGES)

        call = provider.calls[0]
        assert call["timeout_seconds"] == 6.0
        assert call["max_tokens"] == 4096
        assert call["temperature"] == 0.3
        assert _one_interaction(log)["structured_output"] == "none"

    async def test_unknown_hint_is_a_programming_error(self, log: RecordingLogger) -> None:
        provider = FakeProvider()

        with pytest.raises(ValueError, match="turbo"):
            await _service(provider).complete(messages=MESSAGES, hint="turbo")

        assert provider.calls == []
        entry = _one_interaction(log)
        assert entry["status"] == "error"
        assert entry["error_kind"] == "unresolved"
        assert entry["error_type"] == "ValueError"
        assert entry["provider"] is None

    async def test_model_override_nothing_serves_is_logged_once_and_passed_on(
        self, log: RecordingLogger
    ) -> None:
        provider = FakeProvider()

        with pytest.raises(ServiceUnavailableError):
            await _service(provider).complete(messages=MESSAGES, model="no-such/model")

        assert provider.calls == []
        entry = _one_interaction(log)
        assert entry["status"] == "error"
        assert entry["error_kind"] == "unresolved"
        assert entry["error_type"] == "ServiceUnavailableError"
        assert entry["model"] == "no-such/model"


class TestNotConfigured:
    """No provider registered for the hint."""

    @pytest.mark.parametrize("hint", ["fast", "deep", "cheap", "local"])
    async def test_unregistered_provider_is_not_configured(
        self, log: RecordingLogger, hint: str
    ) -> None:
        # With only groq registered, the other hints have nowhere to go either.
        provider = FakeProvider() if hint != "fast" else None

        with pytest.raises(AINotConfiguredError) as caught:
            await _service(provider).complete(messages=MESSAGES, hint=hint)

        exc = caught.value
        assert exc.kind == "not_configured"
        assert exc.error_code == "AI_NOT_CONFIGURED"
        assert exc.status_code == 503
        assert exc.message == "AI suggestions are not configured on this server."
        # The registry's own exception (which names the provider) is not attached.
        assert exc.__context__ is None
        assert exc.__cause__ is None
        entry = _one_interaction(log)
        assert entry["status"] == "not_configured"
        assert entry["level"] == "warning"
        assert entry["provider"] is None
        if provider is not None:
            assert provider.calls == []


class TestDeadline:
    """The total deadline is enforced here, whatever the adapter does."""

    async def test_slow_provider_times_out(self, log: RecordingLogger) -> None:
        async def _sleep() -> Any:
            await asyncio.sleep(5)

        provider = FakeProvider(_sleep)

        with pytest.raises(AIProviderTimeoutError) as caught:
            await _service(provider).complete(messages=MESSAGES, timeout_seconds=0.01)

        exc = caught.value
        assert exc.kind == "deadline"
        assert exc.error_code == "AI_PROVIDER_TIMEOUT"
        assert exc.__context__ is None
        assert exc.__cause__ is None
        assert len(provider.calls) == 1
        entry = _one_interaction(log)
        assert entry["status"] == "timeout"
        assert entry["error_kind"] == "deadline"


class TestErrors:
    """Typed errors pass through; our own bugs are not disguised."""

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (AIProviderUnavailableError("connection"), "error"),
            (AIProviderAuthError("auth"), "error"),
            (AIProviderRateLimitedError("rate_limited"), "error"),
            (AIModelUnavailableError("model_unavailable", model="m"), "error"),
            (AIProviderTimeoutError("read_timeout"), "timeout"),
            (AIResponseInvalidError("empty_content"), "error"),
        ],
    )
    async def test_typed_error_passes_through_unchanged_with_no_retry(
        self, log: RecordingLogger, error: AIError, status: str
    ) -> None:
        provider = FakeProvider(_raiser(error))

        with pytest.raises(AIError) as caught:
            await _service(provider).complete(messages=MESSAGES)

        assert caught.value is error
        assert len(provider.calls) == 1
        entry = _one_interaction(log)
        assert entry["status"] == status
        assert entry["error_kind"] == error.kind
        assert entry["error_type"] is None
        assert entry["level"] == "warning"

    async def test_unexpected_exception_propagates_and_only_its_class_is_logged(
        self, log: RecordingLogger
    ) -> None:
        boom = RuntimeError(f"bug with {SECRET_MARKER}")
        provider = FakeProvider(_raiser(boom))

        with pytest.raises(RuntimeError) as caught:
            await _service(provider).complete(messages=MESSAGES)

        # Not turned into "provider unavailable": it is our bug and must be a 500.
        assert caught.value is boom
        assert len(provider.calls) == 1
        entry = _one_interaction(log)
        assert entry["status"] == "error"
        assert entry["error_type"] == "RuntimeError"
        assert entry["error_kind"] is None
        assert log.entries
        leaked = contains_any([log.text()], [SECRET_MARKER, PROMPT_MARKER])
        assert leaked is False

    async def test_cancellation_is_logged_once_and_releases_the_slot(
        self, log: RecordingLogger
    ) -> None:
        started = asyncio.Event()

        async def _hang() -> Any:
            started.set()
            await asyncio.Event().wait()

        hanging = FakeProvider(_hang)
        registry = AIProviderRegistry()
        registry.register("groq", hanging)
        service = AIService(registry, PromptRegistry(), max_concurrent_calls=1)

        task = asyncio.create_task(service.complete(messages=MESSAGES, timeout_seconds=30))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Cancelled, not timed out, and not swallowed.
        entry = _one_interaction(log)
        assert entry["status"] == "error"
        assert entry["error_kind"] == "cancelled"
        assert entry["error_type"] == "CancelledError"
        # The in-flight slot came back: with a cap of one, a refused call
        # would log `local_concurrency_limit` instead of reaching the provider.
        started.clear()
        task = asyncio.create_task(service.complete(messages=MESSAGES, timeout_seconds=30))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(hanging.calls) == 2

    async def test_non_response_result_is_a_contract_error(self, log: RecordingLogger) -> None:
        async def _wrong() -> Any:
            return {"content": "not an AIResponse"}

        with pytest.raises(AIResponseInvalidError) as caught:
            await _service(FakeProvider(_wrong)).complete(messages=MESSAGES)

        assert caught.value.kind == "provider_contract"
        assert caught.value.__context__ is None
        assert _one_interaction(log)["error_kind"] == "provider_contract"

    async def test_streaming_is_rejected_before_the_provider_is_called(
        self, log: RecordingLogger
    ) -> None:
        provider = FakeProvider()

        with pytest.raises(ValueError, match="Streaming is not supported"):
            await _service(provider).complete(messages=MESSAGES, stream=True)

        assert provider.calls == []


class TestConcurrencyCap:
    """Above the cap a call is refused at once; the provider is not called."""

    async def test_second_call_is_refused_while_the_first_is_in_flight(
        self, log: RecordingLogger
    ) -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def _blocked() -> Any:
            started.set()
            await release.wait()
            return AIResponse(content="ok", model="m")

        provider = FakeProvider(_blocked)
        service = _service(provider, max_concurrent_calls=1)

        first = asyncio.create_task(service.complete(messages=MESSAGES))
        await started.wait()

        with pytest.raises(AIProviderRateLimitedError) as caught:
            await service.complete(messages=MESSAGES)

        exc = caught.value
        assert exc.kind == "local_concurrency_limit"
        assert exc.error_code == "AI_PROVIDER_UNAVAILABLE"
        assert exc.__context__ is None
        assert len(provider.calls) == 1
        refused = [e for e in log.events("ai_interaction") if e["status"] == "error"]
        assert len(refused) == 1
        assert refused[0]["error_kind"] == "local_concurrency_limit"
        assert refused[0]["latency_ms"] == 0

        release.set()
        assert (await first).content == "ok"

        # The slot is released when the first call finishes.
        await service.complete(messages=MESSAGES)
        assert len(provider.calls) == 2

    @pytest.mark.parametrize(
        "error",
        [AIProviderUnavailableError("connection"), RuntimeError("bug")],
        ids=["typed", "unexpected"],
    )
    async def test_slot_is_released_after_a_call_that_raised(
        self, log: RecordingLogger, error: Exception
    ) -> None:
        outcomes: list[Exception | None] = [error, None]

        async def _behaviour() -> Any:
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome
            return AIResponse(content="ok", model="m")

        provider = FakeProvider(_behaviour)
        service = _service(provider, max_concurrent_calls=1)

        with pytest.raises(type(error)):
            await service.complete(messages=MESSAGES)
        assert service._in_flight == 0

        assert (await service.complete(messages=MESSAGES)).content == "ok"

    async def test_slot_is_released_after_a_timeout(self, log: RecordingLogger) -> None:
        async def _sleep() -> Any:
            await asyncio.sleep(5)

        service = _service(FakeProvider(_sleep), max_concurrent_calls=1)

        with pytest.raises(AIProviderTimeoutError):
            await service.complete(messages=MESSAGES, timeout_seconds=0.01)

        assert service._in_flight == 0


class TestSuccessLog:
    """The one record of an interaction — with no content in it."""

    async def test_success_log_fields(self, log: RecordingLogger) -> None:
        provider = FakeProvider()

        await _service(provider).complete(
            messages=MESSAGES,
            use_case="appointment.recommend_slot",
            module="appointment",
            prompt_id="appointment.recommend_slot",
            prompt_version="2.0.0",
            actor_id=ACTOR_ID,
            hospital_id=HOSPITAL_ID,
            request_id="req-1",
        )

        entry = _one_interaction(log)
        assert entry["level"] == "info"
        assert entry["status"] == "success"
        assert entry["module"] == "appointment"
        assert entry["use_case"] == "appointment.recommend_slot"
        assert entry["prompt_id"] == "appointment.recommend_slot"
        assert entry["prompt_version"] == "2.0.0"
        assert entry["provider"] == "groq"
        assert entry["model"] == FAST_MODEL
        assert entry["response_model"] == "returned-model"
        assert entry["input_tokens"] == 120
        assert entry["output_tokens"] == 80
        assert entry["finish_reason"] == "stop"
        assert isinstance(entry["latency_ms"], int)
        assert entry["latency_ms"] >= 0
        assert entry["error_kind"] is None
        assert entry["error_type"] is None
        assert entry["request_id"] == "req-1"
        # Strings, not UUID / Decimal objects: the JSON renderer would emit repr().
        assert entry["actor_id"] == str(ACTOR_ID)
        assert entry["hospital_id"] == str(HOSPITAL_ID)
        # No price is recorded for this model, so the cost is unknown — reported
        # as null, never as a zero that would read as "free".
        assert entry["cost_estimate_usd"] is None
        assert entry["cost_known"] is False

        leaked = contains_any([log.text()], [PROMPT_MARKER, REPLY_MARKER])
        assert leaked is False

    async def test_cost_is_reported_as_known_for_a_priced_model(
        self, log: RecordingLogger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No Groq model has a recorded price, so one is given to a test-only id.
        monkeypatch.setitem(
            constants.MODEL_CATALOG["groq"],
            "test/priced-model",
            constants.ModelInfo(Decimal("0.001"), Decimal("0.002")),
        )
        monkeypatch.setitem(
            constants.COST_PER_1K_INPUT["groq"], "test/priced-model", Decimal("0.001")
        )
        monkeypatch.setitem(
            constants.COST_PER_1K_OUTPUT["groq"], "test/priced-model", Decimal("0.002")
        )
        provider = FakeProvider()
        registry = AIProviderRegistry()
        registry.register("groq", provider)
        registry.register_hint(ModelHint.FAST, "groq", "test/priced-model")

        await AIService(registry, PromptRegistry()).complete(messages=MESSAGES)

        entry = _one_interaction(log)
        assert entry["cost_known"] is True
        # 120 prompt and 80 completion tokens at the rates above.
        assert Decimal(entry["cost_estimate_usd"]) == Decimal("0.000280")

    def test_ai_path_does_not_import_the_tool_layer(self) -> None:
        """The model gets no tool: nothing on this path knows the tool registry."""
        import inspect

        import app.ai.providers.groq as groq_module
        import app.ai.runtime as runtime_module
        import app.ai.services.ai_service as service_module
        import app.services.slot_ranker as ranker_module

        for module in (groq_module, runtime_module, service_module, ranker_module):
            source = inspect.getsource(module)
            assert "app.ai.tools" not in source, module.__name__
            assert "app.mcp" not in source, module.__name__
