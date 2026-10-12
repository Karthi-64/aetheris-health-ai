"""Unit tests for the AI runtime: what "configured" means and what is logged.

Every ``Settings`` here is built with ``_env_file=None`` and the fake key (see
``ai_fakes.ai_settings``), so nothing can read ``backend/.env``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from app.ai import runtime as runtime_module
from app.ai.errors import AINotConfiguredError
from app.ai.prompts.registry import PromptRegistry
from app.ai.providers.base import Message
from app.ai.runtime import (
    AIRuntime,
    build_ai_runtime,
    close_ai_runtime,
    get_ai_runtime,
    reset_ai_runtime,
    set_ai_runtime,
)
from app.tests.ai_fakes import (
    FAKE_GROQ_KEY,
    FAST_MODEL,
    RecordingHandler,
    RecordingLogger,
    ai_settings,
    contains_any,
    groq_reply,
    install_loggers,
    make_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    """Record what the runtime logs."""
    return install_loggers(monkeypatch, ["app.ai.runtime"])


def _registered(runtime: AIRuntime) -> list[str]:
    """Names of the providers the runtime's service can reach."""
    return runtime.service._provider_registry.available_providers


def _assert_key_not_logged(log: RecordingLogger) -> None:
    assert log.entries, "the status must be logged, or this check proves nothing"
    leaked = contains_any([log.text()], [FAKE_GROQ_KEY, "Bearer", str(len(FAKE_GROQ_KEY))])
    assert leaked is False


class TestDecisionTable:
    """Contract section 3.2, row by row."""

    def test_no_key_is_not_configured(self, log: RecordingLogger) -> None:
        runtime = build_ai_runtime(ai_settings(GROQ_API_KEY=None))

        assert runtime.status.configured is False
        assert runtime.status.reason == "no_api_key"
        assert runtime.status.provider is None
        assert runtime.status.fast_model is None
        assert runtime.provider is None
        assert _registered(runtime) == []
        entry = log.events("ai_runtime_disabled")[0]
        assert entry["reason"] == "no_api_key"
        assert entry["level"] == "info"

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    def test_blank_key_is_no_key(self, log: RecordingLogger, blank: str) -> None:
        runtime = build_ai_runtime(ai_settings(GROQ_API_KEY=blank))

        assert runtime.status.reason == "no_api_key"

    def test_kill_switch_wins_over_a_key(self, log: RecordingLogger) -> None:
        runtime = build_ai_runtime(ai_settings(AI_ENABLED=False))

        assert runtime.status.configured is False
        assert runtime.status.reason == "disabled_by_setting"
        assert runtime.provider is None
        assert _registered(runtime) == []
        _assert_key_not_logged(log)

    def test_enabled_without_a_key_is_still_off_and_warns(self, log: RecordingLogger) -> None:
        runtime = build_ai_runtime(ai_settings(GROQ_API_KEY=None, AI_ENABLED=True))

        assert runtime.status.configured is False
        assert runtime.status.reason == "no_api_key"
        assert log.events("ai_runtime_disabled")[0]["level"] == "warning"

    @pytest.mark.parametrize(
        "bad_key", ["has a space", "line\nbreak", "tab\there", "café", "x" * 513]
    )
    def test_malformed_key_disables_ai_without_revealing_it(
        self, log: RecordingLogger, bad_key: str
    ) -> None:
        runtime = build_ai_runtime(ai_settings(GROQ_API_KEY=bad_key))

        assert runtime.status.configured is False
        assert runtime.status.reason == "api_key_malformed"
        assert runtime.provider is None
        entry = log.events("ai_runtime_disabled")[0]
        assert entry["level"] == "error"
        leaked = contains_any([log.text()], [bad_key, bad_key[:6]])
        assert leaked is False

    def test_key_present_is_configured_with_exactly_one_provider(
        self, log: RecordingLogger
    ) -> None:
        runtime = build_ai_runtime(ai_settings())

        assert runtime.status.configured is True
        assert runtime.status.reason == "ok"
        assert runtime.status.provider == "groq"
        assert runtime.status.fast_model == FAST_MODEL == "openai/gpt-oss-20b"
        assert runtime.provider is not None
        assert _registered(runtime) == ["groq"]
        entry = log.events("ai_runtime_configured")[0]
        assert entry["level"] == "info"
        assert entry["provider"] == "groq"
        assert entry["model"] == FAST_MODEL
        assert entry["model_source"] == "default"
        assert entry["base_url_host"] == "api.groq.com"
        assert entry["timeout_seconds"] == 8.0
        assert entry["strict_json_schema"] is True
        assert entry["max_concurrent_calls"] == 4
        assert entry["prompt_templates"] >= 1
        _assert_key_not_logged(log)

    def test_fast_model_override(self, log: RecordingLogger) -> None:
        runtime = build_ai_runtime(ai_settings(AI_FAST_MODEL="vendor/other-model"))

        assert runtime.status.fast_model == "vendor/other-model"
        _provider, model = runtime.service._provider_registry.resolve("fast")
        assert model == "vendor/other-model"
        entry = log.events("ai_runtime_configured")[0]
        assert entry["model"] == "vendor/other-model"
        assert entry["model_source"] == "AI_FAST_MODEL"

    def test_settings_reach_the_provider_and_the_service(self, log: RecordingLogger) -> None:
        runtime = build_ai_runtime(
            ai_settings(
                GROQ_BASE_URL="http://localhost:8080/v1/",
                AI_REQUEST_TIMEOUT_SECONDS=3.5,
                GROQ_STRICT_JSON_SCHEMA=False,
                AI_MAX_CONCURRENT_CALLS=2,
            )
        )

        assert runtime.provider is not None
        assert runtime.provider._url == "http://localhost:8080/v1/chat/completions"
        assert runtime.provider._timeout_seconds == 3.5
        assert runtime.provider._strict_json_schema is False
        assert runtime.service._default_timeout_seconds == 3.5
        assert runtime.service._max_concurrent_calls == 2
        entry = log.events("ai_runtime_configured")[0]
        assert entry["base_url_host"] == "localhost"
        assert entry["strict_json_schema"] is False

    def test_only_the_fast_hint_is_served(self, log: RecordingLogger) -> None:
        """Anthropic, OpenAI and Ollama stay unregistered stubs."""
        from app.ai.providers import ProviderNotRegisteredError

        registry = build_ai_runtime(ai_settings()).service._provider_registry

        for hint in ("deep", "cheap", "local"):
            with pytest.raises(ProviderNotRegisteredError):
                registry.resolve(hint)


class TestTemplates:
    """The prompt the workflow needs is part of "configured"."""

    def test_templates_load_from_an_absolute_path(
        self, log: RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)

        runtime = build_ai_runtime(ai_settings())

        assert "appointment.recommend_slot" in runtime.prompts.registered_ids
        assert runtime.status.configured is True

    def test_missing_template_means_not_configured(
        self, log: RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(runtime_module, "_TEMPLATES_DIR", tmp_path)

        runtime = build_ai_runtime(ai_settings())

        assert runtime.status.configured is False
        assert runtime.status.reason == "prompt_template_missing"
        assert runtime.provider is None
        assert _registered(runtime) == []
        entry = log.events("ai_runtime_disabled")[0]
        assert entry["level"] == "error"
        assert entry["reason"] == "prompt_template_missing"


class TestNoNetworkAtBuild:
    """Configured is not reachable: nothing calls the provider at startup."""

    async def test_build_sends_no_request(self, log: RecordingLogger) -> None:
        handler = RecordingHandler(lambda _request: groq_reply("x"))

        runtime = make_runtime(handler)

        assert runtime.status.configured is True
        assert handler.requests == []
        assert runtime.provider is not None
        assert runtime.provider._client is None

    async def test_unconfigured_service_raises_not_configured(self, log: RecordingLogger) -> None:
        runtime = build_ai_runtime(ai_settings(GROQ_API_KEY=None))

        with pytest.raises(AINotConfiguredError):
            await runtime.service.complete(messages=[Message(role="user", content="hi")])


class TestProcessSingleton:
    """get / set / reset / close."""

    def test_get_builds_from_the_settings_singleton_and_caches(self, log: RecordingLogger) -> None:
        # The autouse fixture pinned the singleton to "no key".
        first = get_ai_runtime()

        assert first.status.configured is False
        assert first.status.reason == "no_api_key"
        assert get_ai_runtime() is first
        assert len(log.events("ai_runtime_disabled")) == 1

    def test_reset_drops_the_cache(self, log: RecordingLogger) -> None:
        first = get_ai_runtime()

        reset_ai_runtime()

        assert get_ai_runtime() is not first

    def test_set_installs_a_runtime(self, log: RecordingLogger) -> None:
        installed = make_runtime(lambda _request: groq_reply("x"))

        set_ai_runtime(installed)

        assert get_ai_runtime() is installed

    def test_a_failing_build_yields_a_disabled_runtime_and_never_raises(
        self, log: RecordingLogger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _explode(self: PromptRegistry, templates_dir: Any) -> int:
            msg = f"template exploded {FAKE_GROQ_KEY}"
            raise RuntimeError(msg)

        monkeypatch.setattr(PromptRegistry, "load_all", _explode)

        runtime = get_ai_runtime()

        assert runtime.status.configured is False
        assert runtime.status.reason == "build_failed"
        assert runtime.provider is None
        entry = log.events("ai_runtime_disabled")[0]
        assert entry["level"] == "error"
        assert entry["reason"] == "build_failed"
        assert entry["error_type"] == "RuntimeError"
        # Only the class name: the exception text is not logged.
        _assert_key_not_logged(log)
        assert get_ai_runtime() is runtime

    async def test_close_closes_the_provider_and_drops_the_cache(
        self, log: RecordingLogger
    ) -> None:
        handler = RecordingHandler(lambda _request: groq_reply("hello"))
        installed = make_runtime(handler)
        set_ai_runtime(installed)
        await installed.service.complete(messages=[Message(role="user", content="hi")])
        assert installed.provider is not None
        assert installed.provider._client is not None

        await close_ai_runtime()

        assert installed.provider._client is None
        assert get_ai_runtime() is not installed
        await close_ai_runtime()  # nothing configured now: still safe
