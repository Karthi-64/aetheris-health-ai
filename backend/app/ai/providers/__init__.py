"""AI provider registry — resolves model hints to concrete provider instances.

The registry is the **only** place in the codebase that constructs provider objects.
Services ask for a provider by hint (``fast``, ``deep``, ``cheap``, ``local``)
and receive a ready-to-use :class:`AIProvider` and model name.

Failover strategy:
- First attempt: resolve the hint's primary provider.
- On failure: fall through registered fallback providers.
- All providers failing: raise :class:`AllProvidersFailedError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.ai.constants import DEFAULT_HINT_MAPPING, ModelHint, providers_serving
from app.core.exceptions import ServiceUnavailableError

if TYPE_CHECKING:
    from app.ai.providers.base import AIProvider

logger = structlog.get_logger(__name__)


class ProviderNotRegisteredError(ServiceUnavailableError):
    """Raised when a requested provider is not registered."""

    def __init__(self, provider_name: str) -> None:
        super().__init__(
            message=f"AI provider '{provider_name}' is not registered.",
            detail={"provider": provider_name},
        )


class AllProvidersFailedError(ServiceUnavailableError):
    """Raised when all providers fail to handle a request."""

    def __init__(self, hint: str) -> None:
        super().__init__(
            message="All AI providers are currently unavailable.",
            detail={"hint": hint},
        )


class AIProviderRegistry:
    """Registry that maps provider names to provider instances.

    Usage::

        registry = AIProviderRegistry()
        registry.register("anthropic", anthropic_provider)
        registry.register("openai", openai_provider)

        provider, model = registry.resolve(ModelHint.FAST)
        response = await provider.complete(messages=messages, model=model)
    """

    def __init__(self) -> None:
        self._providers: dict[str, AIProvider] = {}
        self._hint_mapping: dict[ModelHint, tuple[str, str]] = dict(DEFAULT_HINT_MAPPING)
        self._fallback_order: list[str] = []

    def register(self, name: str, provider: AIProvider) -> None:
        """Register a provider instance.

        :param name: Provider name (``anthropic``, ``openai``, etc.).
        :param provider: The provider instance.
        """
        self._providers[name] = provider
        logger.info("ai_provider_registered", provider=name)

    def register_hint(self, hint: ModelHint, provider_name: str, model: str) -> None:
        """Override the default mapping for a model hint.

        :param hint: The capability hint.
        :param provider_name: Registered provider name.
        :param model: Concrete model identifier.
        """
        self._hint_mapping[hint] = (provider_name, model)

    def set_fallback_order(self, order: list[str]) -> None:
        """Set the provider fallback order.

        When the primary provider for a hint fails, the registry tries
        each provider in *order* sequentially.

        :param order: List of registered provider names.
        """
        self._fallback_order = order

    def resolve(self, hint: ModelHint | str) -> tuple[AIProvider, str]:
        """Resolve a model hint to a (provider, model) pair.

        :param hint: A :class:`ModelHint` or string value.
        :returns: A tuple of (provider instance, model name).
        :raises ProviderNotRegisteredError: If the hint's provider is not registered.
        """
        hint_enum = ModelHint(hint) if isinstance(hint, str) else hint
        mapping = self._hint_mapping.get(hint_enum)

        if mapping is None:
            msg = f"No provider mapping for hint '{hint_enum}'."
            raise ValueError(msg)

        provider_name, model = mapping
        provider = self._providers.get(provider_name)

        if provider is None:
            raise ProviderNotRegisteredError(provider_name)

        return provider, model

    async def resolve_with_fallback(
        self,
        hint: ModelHint | str,
    ) -> tuple[AIProvider, str]:
        """Resolve a hint with automatic fallback on failure.

        Attempts the primary provider first. If it fails, tries each
        provider in ``fallback_order`` until one succeeds.

        :param hint: The desired capability hint.
        :returns: A (provider, model) pair.
        :raises AllProvidersFailedError: If no provider responds.
        """
        errors: list[str] = []

        # Try the hint's primary provider.
        try:
            return self.resolve(hint)
        except (ProviderNotRegisteredError, ServiceUnavailableError) as exc:
            errors.append(str(exc))

        # Try fallback providers.
        for fallback_name in self._fallback_order:
            provider = self._providers.get(fallback_name)
            if provider is None:
                continue
            # Use the fallback provider's default model for this hint.
            hint_enum = ModelHint(hint) if isinstance(hint, str) else hint
            fallback_model = self._hint_mapping.get(hint_enum, (fallback_name, ""))[1]
            if fallback_model:
                return provider, fallback_model

        raise AllProvidersFailedError(str(hint))

    def resolve_model(self, model: str) -> tuple[AIProvider, str]:
        """Resolve a concrete model id to the registered provider that serves it.

        A model is served by a provider when a hint is currently mapped to
        that pair — which covers a per-environment override such as
        ``AI_FAST_MODEL`` — or when the model catalog lists it for that
        provider. Pricing plays no part: a model with no recorded price
        still resolves.

        :param model: Concrete model identifier.
        :returns: A (provider instance, model name) pair.
        :raises ServiceUnavailableError: If no registered provider serves it.
        """
        candidates = [name for name, mapped in self._hint_mapping.values() if mapped == model]
        candidates.extend(providers_serving(model))
        for provider_name in candidates:
            provider = self._providers.get(provider_name)
            if provider is not None:
                return provider, model

        msg = f"No registered provider can serve model '{model}'."
        raise ServiceUnavailableError(message=msg)

    def get_provider(self, name: str) -> AIProvider | None:
        """Retrieve a registered provider by name.

        :param name: The provider name.
        :returns: The provider instance, or ``None`` if not registered.
        """
        return self._providers.get(name)

    @property
    def available_providers(self) -> list[str]:
        """Return names of all registered providers."""
        return list(self._providers.keys())


# Module-level singleton — import this, don't instantiate yourself.
registry = AIProviderRegistry()


__all__ = [
    "AIProviderRegistry",
    "AllProvidersFailedError",
    "ProviderNotRegisteredError",
    "registry",
]
