"""The AI runtime — one provider, built once per process from settings.

This is the composition root of the AI layer: it reads the AI settings,
decides whether AI is configured, registers the one provider that is, and
hands out the :class:`~app.ai.services.ai_service.AIService` everything else
calls.

**Configured is not reachable.** Building the runtime performs no network
I/O; nothing validates the key against the provider at startup. A wrong key or
a retired model is discovered on the first real call and reported as a typed
error.

**AI off is a normal state.** With no key, or with the kill switch off, the
runtime is built anyway with an empty provider registry: the application
starts, every non-AI module works, and an AI call raises
:class:`~app.ai.errors.AINotConfiguredError`.

Adding a provider means one adapter module, its settings, and two lines in
:func:`build_ai_runtime`. Nothing in ``AIService`` or its callers changes.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import structlog

from app.ai.constants import DEFAULT_HINT_MAPPING, ModelHint
from app.ai.prompts.registry import PromptRegistry
from app.ai.providers import AIProviderRegistry
from app.ai.providers.groq import GroqProvider
from app.ai.services.ai_service import AIService
from app.core.config import Settings

logger = structlog.get_logger(__name__)

__all__ = [
    "SLOT_PROMPT_ID",
    "AIRuntime",
    "AIRuntimeStatus",
    "build_ai_runtime",
    "close_ai_runtime",
    "get_ai_runtime",
    "reset_ai_runtime",
    "set_ai_runtime",
]

#: The prompt the one AI workflow needs. A runtime without it is not configured.
SLOT_PROMPT_ID = "appointment.recommend_slot"

#: Where the versioned prompt templates live. Absolute, so the working
#: directory the server was started from does not matter.
_TEMPLATES_DIR = Path(__file__).resolve().parent / "prompts" / "templates"

#: A usable API key: printable ASCII, no spaces or control characters. A key
#: with a newline would be an illegal header value.
_API_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,512}$")


@dataclass(frozen=True, slots=True)
class AIRuntimeStatus:
    """Whether AI is configured in this process, and why not when it is not.

    :param configured: ``True`` when a provider is registered and usable.
    :param reason: ``ok``, ``no_api_key``, ``disabled_by_setting``,
        ``api_key_malformed``, ``prompt_template_missing`` or ``build_failed``.
    :param provider: ``"groq"`` when configured, else ``None``.
    :param fast_model: Model the ``fast`` hint resolves to, else ``None``.
    """

    configured: bool
    reason: str
    provider: str | None
    fast_model: str | None


@dataclass(frozen=True, slots=True)
class AIRuntime:
    """The process-wide AI objects.

    :param status: Whether AI is configured.
    :param service: The AI service. Always present; with nothing configured
        every call raises :class:`~app.ai.errors.AINotConfiguredError`.
    :param prompts: The loaded prompt templates.
    :param provider: The provider, kept so it can be closed on shutdown.
    """

    status: AIRuntimeStatus
    service: AIService
    prompts: PromptRegistry
    provider: GroqProvider | None


def _disabled_reason(config: Settings, prompts: PromptRegistry) -> str | None:
    """Return why AI is off, or ``None`` when it is configured.

    The first matching row wins: kill switch, missing key, malformed key,
    missing prompt template.
    """
    if config.AI_ENABLED is False:
        return "disabled_by_setting"
    if config.GROQ_API_KEY is None:
        return "no_api_key"
    if _API_KEY_PATTERN.fullmatch(config.GROQ_API_KEY.get_secret_value()) is None:
        return "api_key_malformed"
    if SLOT_PROMPT_ID not in prompts.registered_ids:
        return "prompt_template_missing"
    return None


def build_ai_runtime(
    config: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> AIRuntime:
    """Build the AI runtime from settings. Performs no network I/O.

    :param config: The application settings.
    :param transport: HTTP transport for the provider. Tests pass an
        :class:`httpx.MockTransport`; production leaves it ``None``.
    :returns: The runtime, configured or not.
    """
    prompts = PromptRegistry()
    template_count = prompts.load_all(_TEMPLATES_DIR)

    # A private registry: the module-level singleton in app.ai.providers has
    # no way to unregister, so using it would leak a provider between tests.
    registry = AIProviderRegistry()
    service = AIService(
        registry,
        prompts,
        default_timeout_seconds=config.AI_REQUEST_TIMEOUT_SECONDS,
        max_concurrent_calls=config.AI_MAX_CONCURRENT_CALLS,
    )

    reason = _disabled_reason(config, prompts)
    if reason is not None or config.GROQ_API_KEY is None:
        reason = reason or "no_api_key"
        fields = {"reason": reason}
        if reason in {"api_key_malformed", "prompt_template_missing"}:
            logger.error("ai_runtime_disabled", **fields)
        elif reason == "no_api_key" and config.AI_ENABLED is True:
            logger.warning("ai_runtime_disabled", **fields)
        else:
            logger.info("ai_runtime_disabled", **fields)
        return AIRuntime(
            status=AIRuntimeStatus(configured=False, reason=reason, provider=None, fast_model=None),
            service=service,
            prompts=prompts,
            provider=None,
        )

    provider = GroqProvider(
        api_key=config.GROQ_API_KEY,
        base_url=config.GROQ_BASE_URL,
        timeout_seconds=config.AI_REQUEST_TIMEOUT_SECONDS,
        strict_json_schema=config.GROQ_STRICT_JSON_SCHEMA,
        transport=transport,
    )
    registry.register(provider.name, provider)
    fast_model = DEFAULT_HINT_MAPPING[ModelHint.FAST][1]
    model_source = "default"
    if config.AI_FAST_MODEL is not None:
        registry.register_hint(ModelHint.FAST, provider.name, config.AI_FAST_MODEL)
        fast_model = config.AI_FAST_MODEL
        model_source = "AI_FAST_MODEL"

    logger.info(
        "ai_runtime_configured",
        provider=provider.name,
        model=fast_model,
        model_source=model_source,
        # Host only: never the path or query, and never the key.
        base_url_host=urlsplit(config.GROQ_BASE_URL).hostname,
        timeout_seconds=config.AI_REQUEST_TIMEOUT_SECONDS,
        strict_json_schema=config.GROQ_STRICT_JSON_SCHEMA,
        max_concurrent_calls=config.AI_MAX_CONCURRENT_CALLS,
        prompt_templates=template_count,
    )
    return AIRuntime(
        status=AIRuntimeStatus(
            configured=True, reason="ok", provider=provider.name, fast_model=fast_model
        ),
        service=service,
        prompts=prompts,
        provider=provider,
    )


def _disabled_runtime(reason: str) -> AIRuntime:
    """A runtime with nothing registered, for when building one failed."""
    prompts = PromptRegistry()
    return AIRuntime(
        status=AIRuntimeStatus(configured=False, reason=reason, provider=None, fast_model=None),
        service=AIService(AIProviderRegistry(), prompts),
        prompts=prompts,
        provider=None,
    )


_lock = threading.Lock()
_runtime: AIRuntime | None = None


def get_ai_runtime() -> AIRuntime:
    """Return the process-wide runtime, building it on first use.

    Never raises: an AI problem must not break a non-AI request. A failed
    build is logged with the exception's class name only and cached as a
    disabled runtime. Safe to call from a threadpool thread or the event loop.

    :returns: The runtime.
    """
    global _runtime  # noqa: PLW0603 — a lazy process singleton
    runtime = _runtime
    if runtime is not None:
        return runtime
    with _lock:
        if _runtime is None:
            from app.core.config import settings

            try:
                _runtime = build_ai_runtime(settings)
            except Exception as exc:  # noqa: BLE001 — see the docstring
                logger.error(
                    "ai_runtime_disabled", reason="build_failed", error_type=type(exc).__name__
                )
                _runtime = _disabled_runtime("build_failed")
        return _runtime


def set_ai_runtime(runtime: AIRuntime | None) -> None:
    """Install a runtime. A test hook: tests install one built on a fake transport.

    :param runtime: The runtime to serve, or ``None`` to clear the cache.
    """
    global _runtime  # noqa: PLW0603
    with _lock:
        _runtime = runtime


def reset_ai_runtime() -> None:
    """Drop the cached runtime without closing it. A test hook."""
    set_ai_runtime(None)


async def close_ai_runtime() -> None:
    """Close the provider's HTTP client and drop the cache. Called on shutdown."""
    global _runtime  # noqa: PLW0603
    with _lock:
        runtime, _runtime = _runtime, None
    if runtime is not None and runtime.provider is not None:
        await runtime.provider.aclose()
