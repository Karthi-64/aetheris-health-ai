"""AI platform constants.

Model hint mappings, cost estimates, and default budget configurations.
Model choice guidance is documented in ``docs/08-AI_ARCHITECTURE.md`` §16.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final


class ModelHint(StrEnum):
    """Capability hints that prompts declare instead of concrete model names.

    The provider registry resolves hints to actual models per environment.
    """

    FAST = "fast"  # Low latency, good for routine tasks
    DEEP = "deep"  # High quality, good for clinical drafting
    CHEAP = "cheap"  # Cost-optimized, high-volume
    LOCAL = "local"  # Self-hosted, data residency


#: Default mapping from model hints to (provider, model) tuples. Every model
#: named here is listed in :data:`MODEL_CATALOG`. Override per environment via
#: configuration (``AI_FAST_MODEL``).
DEFAULT_HINT_MAPPING: Final[dict[ModelHint, tuple[str, str]]] = {
    # Groq decommissioned the id previously mapped here (its API answers 400
    # ``model_decommissioned`` for it). ``openai/gpt-oss-20b`` is a reasoning
    # model: completion tokens include reasoning tokens, so callers must leave room
    # in ``max_tokens``. Override per deployment with the ``AI_FAST_MODEL`` setting.
    ModelHint.FAST: ("groq", "openai/gpt-oss-20b"),
    ModelHint.DEEP: ("anthropic", "claude-sonnet-4-20250514"),
    ModelHint.CHEAP: ("openai", "gpt-4o-mini"),
    ModelHint.LOCAL: ("ollama", "qwen2.5:14b"),
}


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """What the platform knows about one model a provider serves.

    A price is recorded only when it has been taken from the provider's own
    pricing page. ``None`` means "price unavailable": the cost of a call is
    then reported as unknown, never as zero.

    :param input_per_1k_usd: USD per 1K prompt tokens, or ``None``.
    :param output_per_1k_usd: USD per 1K completion tokens, or ``None``.
    """

    input_per_1k_usd: Decimal | None = None
    output_per_1k_usd: Decimal | None = None

    @property
    def priced(self) -> bool:
        """Whether both rates are known."""
        return self.input_per_1k_usd is not None and self.output_per_1k_usd is not None


#: The models each provider serves — the single source of truth for which
#: provider a model id belongs to and what it costs. ``DEFAULT_HINT_MAPPING``
#: must only name models listed here. Retired model ids are removed, not kept.
#: Prices are rough estimates from provider pricing pages; update them there.
MODEL_CATALOG: Final[dict[str, dict[str, ModelInfo]]] = {
    "anthropic": {
        "claude-sonnet-4-20250514": ModelInfo(Decimal("0.003"), Decimal("0.015")),
        "claude-haiku-3-5-20241022": ModelInfo(Decimal("0.0008"), Decimal("0.004")),
    },
    "openai": {
        "gpt-4o-mini": ModelInfo(Decimal("0.00015"), Decimal("0.0006")),
        "gpt-4o": ModelInfo(Decimal("0.0025"), Decimal("0.01")),
    },
    "groq": {
        # Price unavailable: no figure from Groq's pricing page has been
        # recorded for this model, so its calls log ``cost_known: false``.
        "openai/gpt-oss-20b": ModelInfo(),
    },
    "ollama": {
        # Self-hosted: no per-token charge.
        "qwen2.5:14b": ModelInfo(Decimal("0"), Decimal("0")),
    },
}


def model_info(provider: str, model: str) -> ModelInfo | None:
    """Look a model up in the catalog.

    :param provider: Provider name.
    :param model: Model identifier.
    :returns: Its catalog entry, or ``None`` when the provider does not list it.
    """
    return MODEL_CATALOG.get(provider, {}).get(model)


def providers_serving(model: str) -> list[str]:
    """Name the providers whose catalog lists ``model``.

    :param model: Model identifier.
    :returns: Provider names, in catalog order.
    """
    return [provider for provider, models in MODEL_CATALOG.items() if model in models]


#: Cost per 1K input tokens (USD), derived from :data:`MODEL_CATALOG`. Holds
#: only models whose price is known.
COST_PER_1K_INPUT: Final[dict[str, dict[str, Decimal]]] = {
    provider: {
        model: info.input_per_1k_usd
        for model, info in models.items()
        if info.input_per_1k_usd is not None and info.priced
    }
    for provider, models in MODEL_CATALOG.items()
}

#: Cost per 1K output tokens (USD), derived from :data:`MODEL_CATALOG`.
COST_PER_1K_OUTPUT: Final[dict[str, dict[str, Decimal]]] = {
    provider: {
        model: info.output_per_1k_usd
        for model, info in models.items()
        if info.output_per_1k_usd is not None and info.priced
    }
    for provider, models in MODEL_CATALOG.items()
}

#: Default per-hospital monthly AI budget in USD.
DEFAULT_MONTHLY_AI_BUDGET_USD: Final[Decimal] = Decimal("100.00")

#: Default daily AI budget per user in USD.
DEFAULT_DAILY_USER_AI_BUDGET_USD: Final[Decimal] = Decimal("1.00")

#: Max tokens for AI completions by hint.
MAX_TOKENS: Final[dict[ModelHint, int]] = {
    ModelHint.FAST: 4096,
    ModelHint.DEEP: 8192,
    ModelHint.CHEAP: 2048,
    ModelHint.LOCAL: 4096,
}

#: Default temperature for AI completions by hint.
TEMPERATURE: Final[dict[ModelHint, float]] = {
    ModelHint.FAST: 0.3,
    ModelHint.DEEP: 0.1,
    ModelHint.CHEAP: 0.5,
    ModelHint.LOCAL: 0.3,
}

#: AI interaction status values.
AI_STATUS_SUCCESS = "success"
AI_STATUS_ERROR = "error"
AI_STATUS_RATE_LIMITED = "rate_limited"
