"""AI use-case services — summarization, extraction, recommendation, Q&A.

``AIService`` is the single entry point other modules call. It resolves a
capability hint to a provider and model, makes one bounded call, and writes
one structured ``ai_interaction`` log line. Per-hospital budgets and the
``ai_interactions`` table are planned and not built.
"""

from __future__ import annotations

from app.ai.services.ai_service import AIService, BudgetExceededError

__all__ = [
    "AIService",
    "BudgetExceededError",
]
