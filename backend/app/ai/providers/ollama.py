"""Ollama (self-hosted) provider implementation.

This provider adapter wraps the Ollama REST API for self-hosted LLMs.
Implementation requires:
- Running an Ollama server locally or on the network
- Configuring ``OLLAMA_BASE_URL`` in environment variables
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


class OllamaProvider(AIProvider):
    """Ollama (self-hosted) provider.

    .. caution::

        Not yet connected to the Ollama REST API. The :meth:`complete` and
        :meth:`embed` methods raise :class:`NotImplementedError` until the
        Ollama client is wired in a future sprint.
    """

    name = "ollama"

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
        """Send a completion to the local Ollama server.

        :raises NotImplementedError: Always — provider not yet wired.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.complete() is not yet implemented. "
            "Run an Ollama server and wire the provider "
            "in the application startup lifecycle."
        )

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Generate embeddings via Ollama's API.

        :raises NotImplementedError: Always — provider not yet wired.
        """
        raise NotImplementedError(f"{type(self).__name__}.embed() is not yet implemented.")


__all__ = ["OllamaProvider"]
