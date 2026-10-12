"""The model catalog is the one place that says which provider serves a model.

Covers the registry's explicit-model resolution, the catalog's consistency
with the hint mapping, the ``AI_FAST_MODEL`` override, and cost reporting.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.ai import constants
from app.ai.constants import (
    COST_PER_1K_INPUT,
    COST_PER_1K_OUTPUT,
    DEFAULT_HINT_MAPPING,
    MODEL_CATALOG,
    ModelHint,
    ModelInfo,
    model_info,
    providers_serving,
)
from app.ai.prompts.registry import PromptRegistry
from app.ai.providers import AIProviderRegistry
from app.ai.providers.base import Message
from app.ai.services.ai_service import AIService
from app.core.exceptions import ServiceUnavailableError
from app.tests.ai_fakes import FAST_MODEL, RecordingLogger, install_loggers
from app.tests.unit.ai.test_ai_service import FakeProvider

RETIRED = ("llama-3.1-70b-versatile", "llama-3.3-70b-versatile", "mixtral-8x7b-32768")


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    return install_loggers(monkeypatch)


def _registry() -> tuple[AIProviderRegistry, FakeProvider]:
    provider = FakeProvider()
    registry = AIProviderRegistry()
    registry.register("groq", provider)
    return registry, provider


class TestCatalog:
    def test_every_hint_names_a_model_its_provider_lists(self) -> None:
        for hint, (provider, model) in DEFAULT_HINT_MAPPING.items():
            assert model_info(provider, model) is not None, hint

    def test_the_fast_model_is_a_groq_model(self) -> None:
        assert DEFAULT_HINT_MAPPING[ModelHint.FAST] == ("groq", "openai/gpt-oss-20b")
        assert FAST_MODEL == "openai/gpt-oss-20b"
        assert providers_serving("openai/gpt-oss-20b") == ["groq"]

    def test_its_price_is_recorded_as_unavailable_not_as_zero(self) -> None:
        info = model_info("groq", "openai/gpt-oss-20b")
        assert info == ModelInfo()
        assert info is not None and info.priced is False
        assert "openai/gpt-oss-20b" not in COST_PER_1K_INPUT["groq"]
        assert "openai/gpt-oss-20b" not in COST_PER_1K_OUTPUT["groq"]

    @pytest.mark.parametrize("model", RETIRED)
    def test_a_retired_model_is_nowhere_in_the_active_configuration(self, model: str) -> None:
        assert providers_serving(model) == []
        assert all(model not in models for models in MODEL_CATALOG.values())
        assert all(model not in rates for rates in COST_PER_1K_INPUT.values())
        assert all(model not in rates for rates in COST_PER_1K_OUTPUT.values())
        assert all(mapped != model for _, mapped in DEFAULT_HINT_MAPPING.values())

    def test_the_price_tables_hold_exactly_the_priced_catalog_models(self) -> None:
        for provider, models in MODEL_CATALOG.items():
            priced = {name for name, info in models.items() if info.priced}
            assert set(COST_PER_1K_INPUT[provider]) == priced
            assert set(COST_PER_1K_OUTPUT[provider]) == priced
            for name in priced:
                assert COST_PER_1K_INPUT[provider][name] == models[name].input_per_1k_usd
                assert COST_PER_1K_OUTPUT[provider][name] == models[name].output_per_1k_usd


class TestExplicitModelResolution:
    def test_the_current_model_resolves_to_groq_although_it_has_no_price(self) -> None:
        registry, provider = _registry()

        assert registry.resolve_model("openai/gpt-oss-20b") == (provider, "openai/gpt-oss-20b")

    async def test_complete_with_an_explicit_model_calls_that_model(
        self, log: RecordingLogger
    ) -> None:
        registry, provider = _registry()

        await AIService(registry, PromptRegistry()).complete(
            messages=[Message(role="user", content="hello")], model="openai/gpt-oss-20b"
        )

        assert [call["model"] for call in provider.calls] == ["openai/gpt-oss-20b"]
        entry = log.events("ai_interaction")[0]
        assert (entry["provider"], entry["model"], entry["status"]) == (
            "groq",
            "openai/gpt-oss-20b",
            "success",
        )
        assert entry["cost_estimate_usd"] is None
        assert entry["cost_known"] is False

    @pytest.mark.parametrize("model", [*RETIRED, "no-such-model"])
    async def test_a_retired_or_unknown_model_is_refused_without_a_call(
        self, model: str, log: RecordingLogger
    ) -> None:
        registry, provider = _registry()

        with pytest.raises(ServiceUnavailableError, match="No registered provider can serve"):
            await AIService(registry, PromptRegistry()).complete(
                messages=[Message(role="user", content="hello")], model=model
            )

        assert provider.calls == []

    def test_a_model_of_an_unregistered_provider_does_not_resolve(self) -> None:
        registry, _ = _registry()

        with pytest.raises(ServiceUnavailableError):
            registry.resolve_model("gpt-4o-mini")

    def test_a_model_set_by_environment_override_resolves_by_name_too(self) -> None:
        """``AI_FAST_MODEL`` may name a model the catalog has never heard of."""
        registry, provider = _registry()
        registry.register_hint(ModelHint.FAST, "groq", "vendor/new-model")

        assert registry.resolve(ModelHint.FAST) == (provider, "vendor/new-model")
        assert registry.resolve_model("vendor/new-model") == (provider, "vendor/new-model")
        # The repository default stays resolvable by name: the catalog lists it.
        assert registry.resolve_model("openai/gpt-oss-20b") == (provider, "openai/gpt-oss-20b")


class TestCostAccounting:
    async def test_a_priced_model_reports_its_cost(
        self, log: RecordingLogger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(
            constants.MODEL_CATALOG["groq"],
            "test/priced-model",
            ModelInfo(Decimal("0.001"), Decimal("0.002")),
        )
        monkeypatch.setitem(COST_PER_1K_INPUT["groq"], "test/priced-model", Decimal("0.001"))
        monkeypatch.setitem(COST_PER_1K_OUTPUT["groq"], "test/priced-model", Decimal("0.002"))
        registry, _ = _registry()

        await AIService(registry, PromptRegistry()).complete(
            messages=[Message(role="user", content="hello")], model="test/priced-model"
        )

        entry = log.events("ai_interaction")[0]
        assert entry["cost_known"] is True
        assert Decimal(entry["cost_estimate_usd"]) == Decimal("0.000280")

    async def test_an_override_model_with_no_catalog_entry_reports_unknown_cost(
        self, log: RecordingLogger
    ) -> None:
        registry, _ = _registry()
        registry.register_hint(ModelHint.FAST, "groq", "vendor/new-model")

        await AIService(registry, PromptRegistry()).complete(
            messages=[Message(role="user", content="hello")]
        )

        entry = log.events("ai_interaction")[0]
        assert entry["model"] == "vendor/new-model"
        assert entry["cost_estimate_usd"] is None
        assert entry["cost_known"] is False
