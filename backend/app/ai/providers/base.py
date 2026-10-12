"""AI provider abstract base.

Every LLM provider (Anthropic, OpenAI, Groq, Ollama) implements this interface.
The rest of the codebase never imports a vendor SDK directly — it goes through
:class:`AIProvider`.

This pattern enables provider-agnostic service code and trivial failover.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Message:
    """A message in a chat conversation.

    :param role: One of ``system``, ``user``, ``assistant``, ``tool``.
    :param content: The text content of the message.
    :param tool_calls: Tool invocations from the assistant (from function calling).
    :param tool_call_id: ID of the tool call this message is responding to (for tool results).
    """

    role: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    """Schema for a tool/function the model can call.

    :param name: Unique tool name.
    :param description: Natural-language description of what the tool does.
    :param input_schema: JSON Schema object describing the tool arguments.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model.

    :param id: Unique ID for this invocation (used to correlate results).
    :param name: The tool being called.
    :param arguments: Parsed JSON arguments for the tool.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ResponseSchema:
    """A JSON Schema the model's reply must satisfy, passed through to adapters.

    An adapter uses it however its provider supports structured output (a
    strict schema, a JSON mode, or not at all). The caller still validates the
    reply itself: the schema narrows what a model can say, it is not the check.

    :param name: Short identifier for the schema (``^[A-Za-z0-9_-]{1,64}$``).
    :param schema: The JSON Schema object.
    """

    name: str
    schema: dict[str, Any]


@dataclass(frozen=True)
class AIResponse:
    """Normalised response from an AI provider.

    :param content: The text content of the assistant's response.
    :param tool_calls: Any tool invocations the model requested.
    :param finish_reason: Why the model stopped (``stop``, ``tool_calls``, ``length``, etc.).
    :param input_tokens: Token count of the prompt.
    :param output_tokens: Token count of the response.
    :param model: The model that produced this response.
    :param cost_estimate_usd: Estimated cost in USD.
    :param provider: Name of the provider that answered. Filled in by
        :class:`~app.ai.services.ai_service.AIService`, not by adapters.
    """

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cost_estimate_usd: Decimal = Decimal("0")
    provider: str = ""


@dataclass(frozen=True)
class AIChunk:
    """A streaming chunk from an AI provider.

    :param delta: The incremental text content.
    :param finish_reason: Set on the final chunk.
    :param input_tokens: Set on the final chunk.
    :param output_tokens: Set on the final chunk.
    :param model: The model producing this response.
    :param cost_estimate_usd: Set on the final chunk.
    """

    delta: str = ""
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cost_estimate_usd: Decimal = Decimal("0")


class AIProvider(ABC):
    """Abstract interface every LLM provider implements.

    :param name: Human-readable provider name (``anthropic``, ``openai``, etc.).
    """

    name: str = ""

    @abstractmethod
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
        """Send a completion request and return the response.

        :param messages: The conversation history.
        :param model: The model identifier (e.g. ``claude-sonnet-4-20250514``).
        :param max_tokens: Maximum tokens to generate.
        :param temperature: Sampling temperature (0.0–1.0).
        :param tools: Tool definitions the model may call.
        :param stream: If ``True``, returns an async iterator of chunks.
        :param response_schema: JSON Schema the reply must satisfy, if any.
        :param timeout_seconds: Deadline for this call. ``None`` = adapter default.
        :returns: A complete response, or an async iterator of chunks for streaming.
        """
        ...

    @abstractmethod
    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Generate embeddings for the given texts.

        :param texts: List of text strings to embed.
        :param model: Optional model override. ``None`` = provider default.
        :returns: List of embedding vectors, one per input text.
        """
        ...

    def estimate_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str,
    ) -> Decimal:
        """Estimate the cost of a completion in USD.

        :param input_tokens: Number of prompt tokens.
        :param output_tokens: Number of generated tokens.
        :param model: Model identifier for rate lookup.
        :returns: Estimated cost as a ``Decimal``.
        """
        from app.ai.constants import COST_PER_1K_INPUT, COST_PER_1K_OUTPUT

        provider_rates_in = COST_PER_1K_INPUT.get(self.name, {})
        provider_rates_out = COST_PER_1K_OUTPUT.get(self.name, {})

        rate_in = provider_rates_in.get(model, Decimal("0"))
        rate_out = provider_rates_out.get(model, Decimal("0"))

        cost = (Decimal(str(input_tokens)) / 1000 * rate_in) + (
            Decimal(str(output_tokens)) / 1000 * rate_out
        )
        return cost


__all__ = [
    "AIChunk",
    "AIProvider",
    "AIResponse",
    "Message",
    "ResponseSchema",
    "ToolCall",
    "ToolDefinition",
]
