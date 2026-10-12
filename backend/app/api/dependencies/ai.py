"""Dependency injection wiring for the AI platform layer.

Every AI call in the application goes through the AI runtime's ``AIService`` —
never a raw provider SDK (``docs/08-AI_ARCHITECTURE.md``). This module exposes
that runtime to the rest of the composition root.
"""

from __future__ import annotations

from app.ai.runtime import AIRuntime, get_ai_runtime

__all__ = ["provide_ai_runtime"]


def provide_ai_runtime() -> AIRuntime:
    """Provide the process-wide AI runtime.

    Built lazily on first use and cached. It never raises and never touches
    the network; when AI is not configured its status says so.

    :returns: The AI runtime.
    """
    return get_ai_runtime()
