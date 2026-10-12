"""OpenAI provider implementation.

This provider adapter wraps the official OpenAI Python SDK.
Implementation requires:
- Installing the ``openai`` package
- Configuring ``OPENAI_API_KEY`` in environment variables
- Registering with :data:`app.ai.providers.registry` during application startup

See ``docs/08-AI_ARCHITECTURE.md`` for the provider interface contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.ai.providers.base import (
    AIChunk,
    AIProvider,
    AIResponse,
    Message,
    ResponseSchema,
    ToolDefinition,
)


class OpenAIProvider(AIProvider):
    """OpenAI GPT provider.

    .. caution::

        Not yet connected to the OpenAI SDK. The :meth:`complete` and
        :meth:`embed` methods raise :class:`NotImplementedError` until the
        ``openai`` package is installed and wired in a future sprint.
    """

    name = "openai"

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
        """Send a completion to OpenAI's API.

        :raises NotImplementedError: Always — provider not yet wired.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.complete() is not yet implemented. "
            "Install the ``openai`` package and wire the provider "
            "in the application startup lifecycle."
        )

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Generate embeddings via OpenAI's API.

        :raises NotImplementedError: Always — provider not yet wired.
        """
        raise NotImplementedError(f"{type(self).__name__}.embed() is not yet implemented.")


__all__ = ["OpenAIProvider"]
