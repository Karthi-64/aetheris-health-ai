"""AI service — the single entry point every business module calls.

Modules never call providers directly. They call one of the use-case services
in this module (summarization, extraction, recommendation, QA) or the generic
:class:`AIService` for ad-hoc completions.

Every AI interaction produces exactly one ``ai_interaction`` structured log
line — on success and on every failure path — for observability and cost
tracking. There is no ``ai_interactions`` table yet; the log line is the
record. It never contains prompt text, model output or exception text.

One call to :meth:`AIService.complete` invokes a provider at most once: there
is no retry, no fallback provider and no tool loop.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time as _time
import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from app.ai.constants import MAX_TOKENS, TEMPERATURE, ModelHint, model_info
from app.ai.errors import (
    AIError,
    AINotConfiguredError,
    AIProviderRateLimitedError,
    AIProviderTimeoutError,
    AIResponseInvalidError,
)
from app.ai.prompts import PromptRegistry
from app.ai.providers import AIProviderRegistry, ProviderNotRegisteredError
from app.ai.providers.base import (
    AIProvider,
    AIResponse,
    Message,
    ResponseSchema,
    ToolDefinition,
)
from app.core.exceptions import ServiceUnavailableError

#: Fallbacks when a model hint has no entry in the lookup tables.
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.3

logger = structlog.get_logger(__name__)


class BudgetExceededError(ServiceUnavailableError):
    """Raised when the AI budget for a hospital or user is exceeded."""

    def __init__(
        self,
        message: str = "AI budget exceeded.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class AIInteractionLog:
    """Record of a single AI interaction, to be persisted to ``ai_interactions``.

    This is a domain DTO — the actual persistence is handled by the
    ``AIInteractionRepository`` when it exists.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        hospital_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
        module: str,
        use_case: str,
        prompt_id: str,
        prompt_version: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        status: str,
        error_message: str | None = None,
        cost_estimate_usd: Decimal = Decimal("0"),
        request_id: str | None = None,
    ) -> None:
        self.hospital_id = hospital_id
        self.user_id = user_id
        self.module = module
        self.use_case = use_case
        self.prompt_id = prompt_id
        self.prompt_version = prompt_version
        self.provider = provider
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.status = status
        self.error_message = error_message
        self.cost_estimate_usd = cost_estimate_usd
        self.request_id = request_id
        self.created_at = datetime.now(UTC)


class AIService:
    """Central AI orchestration service.

    Every business module that needs AI capabilities goes through this service
    (or one of the specialised services in this package).

    :param provider_registry: The provider registry for model resolution.
    :param prompt_registry: The prompt template registry.
    :param default_timeout_seconds: Total deadline for one provider call when
        the caller gives none.
    :param max_concurrent_calls: Provider calls this instance may have in
        flight at once. A call above the cap is refused immediately.
    """

    def __init__(
        self,
        provider_registry: AIProviderRegistry,
        prompt_registry: PromptRegistry,
        *,
        default_timeout_seconds: float = 8.0,
        max_concurrent_calls: int = 4,
    ) -> None:
        self._provider_registry = provider_registry
        self._prompt_registry = prompt_registry
        self._default_timeout_seconds = default_timeout_seconds
        self._max_concurrent_calls = max_concurrent_calls
        # A plain counter, not an asyncio.Semaphore: the check and the
        # increment happen with no await between them on one event loop, and
        # the service may be built on a threadpool thread before any loop runs.
        self._in_flight: int = 0

    async def complete(
        self,
        *,
        messages: list[Message],
        hint: str = "fast",
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[ToolDefinition] | None = None,
        stream: bool = False,
        use_case: str = "generic.complete",
        module: str = "ai",
        actor_id: uuid.UUID | None = None,
        hospital_id: uuid.UUID | None = None,
        request_id: str | None = None,
        response_schema: ResponseSchema | None = None,
        timeout_seconds: float | None = None,
        prompt_id: str = "direct",
        prompt_version: str = "",
    ) -> AIResponse:
        """Send a completion request through the AI provider layer.

        This is the lowest-level method in the AI service. The provider is
        called at most once, under a total deadline, and the outcome is logged
        once whatever happens.

        :param messages: The conversation messages.
        :param hint: Model capability hint (``fast``, ``deep``, ``cheap``, ``local``).
        :param model: Concrete model override (bypasses hint resolution).
        :param max_tokens: Override default max tokens.
        :param temperature: Override default temperature.
        :param tools: Tool definitions the model may call.
        :param stream: Not supported; must be ``False``.
        :param use_case: Identifier for cost tracking and observability.
        :param module: Module name for observability.
        :param actor_id: User ID for attribution.
        :param hospital_id: Hospital ID for attribution.
        :param request_id: Correlation ID for observability.
        :param response_schema: JSON Schema the reply must satisfy, passed
            through to the provider. The caller still validates the reply.
        :param timeout_seconds: Total deadline for the call. ``None`` uses the
            service default.
        :param prompt_id: Prompt template id, recorded in the interaction log.
        :param prompt_version: Prompt template version, recorded likewise.
        :returns: The provider's response, with ``provider`` filled in.
        :raises ValueError: If ``stream`` is requested or the hint is unknown.
        :raises AINotConfiguredError: If no provider is registered for the hint.
        :raises AIProviderRateLimitedError: If this service is at its
            concurrency cap, or the provider rate-limited the call.
        :raises AIProviderTimeoutError: If the deadline passed.
        :raises AIProviderUnavailableError: If the provider failed.
        :raises AIResponseInvalidError: If the provider's answer was unusable.
        """
        if stream:
            msg = "Streaming is not supported by the AI runtime."
            raise ValueError(msg)

        log_fields: dict[str, Any] = {
            "module": module,
            "use_case": use_case,
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "structured_output": "schema" if response_schema is not None else "none",
            "actor_id": str(actor_id) if actor_id is not None else None,
            "hospital_id": str(hospital_id) if hospital_id is not None else None,
            "request_id": request_id,
        }

        # Resolve provider and model. The typed error is raised after the
        # except block so the registry's exception is not attached to it.
        resolved: tuple[AIProvider, str] | None = None
        try:
            if model is not None:
                resolved = self._resolve_provider_for_model(model)
            else:
                resolved = self._provider_registry.resolve(hint)
        except ProviderNotRegisteredError:
            resolved = None
        except (ValueError, ServiceUnavailableError) as exc:
            # An unknown hint, or a model override nothing serves: a caller's
            # mistake, passed on unchanged — but still logged once.
            self._log_interaction(
                **log_fields,
                provider=None,
                model=model,
                status="error",
                error_kind="unresolved",
                error_type=type(exc).__name__,
            )
            raise
        if resolved is None:
            self._log_interaction(
                **log_fields,
                provider=None,
                model=model,
                status="not_configured",
                error_kind="not_configured",
            )
            raise AINotConfiguredError("not_configured")
        provider, resolved_model = resolved
        log_fields["provider"] = provider.name
        log_fields["model"] = resolved_model

        # Apply defaults from hint if not overridden. The defaults are
        # supplied to .get() as well: a valid ModelHint that is absent from the
        # lookup table would otherwise resolve to None.
        hint_enum: ModelHint | None = None
        try:
            hint_enum = ModelHint(hint)
        except ValueError:
            hint_enum = None
        actual_max_tokens: int = (
            max_tokens
            if max_tokens is not None
            else (
                MAX_TOKENS.get(hint_enum, DEFAULT_MAX_TOKENS) if hint_enum else DEFAULT_MAX_TOKENS
            )
        )
        actual_temperature: float = (
            temperature
            if temperature is not None
            else (
                TEMPERATURE.get(hint_enum, DEFAULT_TEMPERATURE)
                if hint_enum
                else DEFAULT_TEMPERATURE
            )
        )
        deadline = timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds

        # Concurrency cap: refuse at once, never queue. No await separates the
        # check from the increment.
        if self._in_flight >= self._max_concurrent_calls:
            self._log_interaction(
                **log_fields, status="error", error_kind="local_concurrency_limit", latency_ms=0
            )
            raise AIProviderRateLimitedError("local_concurrency_limit")

        start_ns = _time.perf_counter_ns()
        result: object = None
        deadline_passed = False
        self._in_flight += 1
        try:
            async with asyncio.timeout(deadline):
                result = await provider.complete(
                    messages=messages,
                    model=resolved_model,
                    max_tokens=int(actual_max_tokens),
                    temperature=float(actual_temperature),
                    tools=tools,
                    stream=False,
                    response_schema=response_schema,
                    timeout_seconds=deadline,
                )
        except TimeoutError:
            # Raised below, outside the block.
            deadline_passed = True
        except asyncio.CancelledError:
            # The caller went away (client disconnect, shutdown). The deadline
            # is not this: asyncio.timeout turns its own cancellation into the
            # TimeoutError above before it reaches here.
            self._log_interaction(
                **log_fields,
                status="error",
                error_kind="cancelled",
                error_type="CancelledError",
                latency_ms=_elapsed_ms(start_ns),
            )
            raise
        except AIError as exc:
            # Already typed by the adapter: log and pass it on unchanged.
            self._log_interaction(
                **log_fields,
                status="timeout" if isinstance(exc, AIProviderTimeoutError) else "error",
                error_kind=exc.kind,
                latency_ms=_elapsed_ms(start_ns),
            )
            raise
        except Exception as exc:
            # A bug in our own code. It must surface as a 500, not be dressed
            # up as "provider unavailable". Only the class name is logged.
            self._log_interaction(
                **log_fields,
                status="error",
                error_type=type(exc).__name__,
                latency_ms=_elapsed_ms(start_ns),
            )
            raise
        finally:
            self._in_flight -= 1

        latency_ms = _elapsed_ms(start_ns)
        if deadline_passed:
            self._log_interaction(
                **log_fields, status="timeout", error_kind="deadline", latency_ms=latency_ms
            )
            raise AIProviderTimeoutError("deadline")
        if not isinstance(result, AIResponse):
            self._log_interaction(
                **log_fields, status="error", error_kind="provider_contract", latency_ms=latency_ms
            )
            raise AIResponseInvalidError("provider_contract")

        # A cost is reported only for a model whose price is recorded. An
        # unpriced model logs "unknown" (null), never a zero that reads as free.
        info = model_info(provider.name, resolved_model)
        cost_known = info is not None and info.priced
        cost = (
            provider.estimate_cost(result.input_tokens, result.output_tokens, resolved_model)
            if cost_known
            else None
        )
        self._log_interaction(
            **log_fields,
            status="success",
            response_model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            finish_reason=result.finish_reason,
            latency_ms=latency_ms,
            cost_estimate_usd=None if cost is None else str(cost),
            cost_known=cost_known,
        )
        return dataclasses.replace(result, provider=provider.name)

    def _resolve_provider_for_model(self, model: str) -> tuple[AIProvider, str]:
        """Find a registered provider that can serve the given model.

        :param model: The model identifier.
        :returns: A (provider, model) tuple.
        :raises ServiceUnavailableError: If no provider can serve the model.
        """
        return self._provider_registry.resolve_model(model)

    def _log_interaction(self, *, status: str, **fields: Any) -> None:
        """Emit the one ``ai_interaction`` log line for a call.

        Every line carries the same keys, so a log query never has to guess
        whether a field exists. INFO on success, WARNING otherwise. When an
        ``AIInteractionRepository`` exists this is also where the row will be
        written.

        :param status: ``success``, ``error``, ``timeout`` or ``not_configured``.
        :param fields: Values overriding the defaults below.
        """
        entry: dict[str, Any] = {
            "module": None,
            "use_case": None,
            "prompt_id": None,
            "prompt_version": None,
            "provider": None,
            "model": None,
            "response_model": None,
            "structured_output": "none",
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": 0,
            "status": status,
            "error_kind": None,
            "error_type": None,
            "finish_reason": None,
            "cost_estimate_usd": None,
            "cost_known": False,
            "actor_id": None,
            "hospital_id": None,
            "request_id": None,
        }
        entry.update(fields)
        if status == "success":
            logger.info("ai_interaction", **entry)
        else:
            logger.warning("ai_interaction", **entry)


def _elapsed_ms(start_ns: int) -> int:
    """Whole milliseconds since ``start_ns`` on the monotonic clock."""
    return int((_time.perf_counter_ns() - start_ns) / 1_000_000)


__all__ = [
    "AIInteractionLog",
    "AIService",
    "BudgetExceededError",
]
