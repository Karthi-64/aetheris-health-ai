"""AI-backed implementation of the appointment slot ranker.

Satisfies :class:`~app.services.appointment_service.SlotRanker` by calling the
AI platform layer. Kept out of ``appointment_service.py`` on purpose: prompt
selection, provider routing and response parsing belong to the AI stack, and a
clinical service should not care which model answered
(``docs/08-AI_ARCHITECTURE.md``; ``backend/CLAUDE.md``, "AI Module Usage" —
never call a provider SDK outside ``app/ai/providers/``).

**What the model is shown.** One doctor's day as a list of clock times: free
slots under opaque ids (``S1``, ``S2``, …), the rest marked unavailable, plus
the date and its weekday. No patient, doctor, hospital or user data, no free
text from anyone, and no tool.

**What is accepted back.** One JSON object naming one of the offered ids, with
a reason. The reply is parsed strictly and validated here whatever the
provider was asked to enforce; nothing is repaired. A reply that is truncated,
not JSON, the wrong shape, or names an id that was not offered is rejected with
a typed error. The model chooses; it never invents.
"""

from __future__ import annotations

import json
import re
import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from app.ai.errors import AIResponseInvalidError
from app.ai.providers.base import Message, ResponseSchema
from app.core.logging import get_logger
from app.services.appointment_service import DaySlot, SlotChoice

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from app.ai.prompts.registry import PromptRegistry
    from app.ai.providers.base import AIResponse
    from app.ai.services.ai_service import AIService

logger = get_logger(__name__)

__all__ = ["MAX_REASON_LENGTH", "MAX_TOKENS", "PROMPT_ID", "AISlotRanker"]

#: Prompt template backing this ranker. Versioned on disk at
#: ``app/ai/prompts/templates/appointment/recommend_slot.yaml``.
PROMPT_ID = "appointment.recommend_slot"

#: Completion-token budget for one suggestion. The answer itself is a short
#: JSON object, but the default model is a reasoning model whose completion
#: tokens include its reasoning, so the budget has to leave room for both.
MAX_TOKENS = 1024

#: Longest reason passed on to a client.
MAX_REASON_LENGTH = 200

#: Name the schema travels under when a provider takes a named schema.
_SCHEMA_NAME = "slot_recommendation"

#: English weekday names indexed by ``date.weekday()``. A fixed tuple rather
#: than ``strftime("%A")``, which depends on the process locale.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_WHITESPACE_RUN = re.compile(r"\s+")


class _ModelChoice(BaseModel):
    """The shape the model's reply must have. The server-side schema check."""

    model_config = ConfigDict(extra="forbid", strict=True)

    slot_id: str = Field(pattern=r"^S[1-9][0-9]{0,2}$")
    reason: str = Field(max_length=2000)


def _reject_constant(_name: str) -> NoReturn:
    """Refuse ``NaN`` / ``Infinity``, which ``json.loads`` accepts by default."""
    msg = "Non-standard JSON constant."
    raise ValueError(msg)


#: A candidate id (``S1``, ``S12``) appearing in model-written text.
_CANDIDATE_ID_MENTION = re.compile(r"\bS\d{1,3}\b")


def _clean_reason(text: str) -> str | None:
    """Make model-written text safe to pass on as plain text.

    Non-printable characters become spaces, whitespace runs collapse to one
    space, and the result is cut to :data:`MAX_REASON_LENGTH`. A reason that
    cites a candidate id is withheld altogether. This changes presentation
    only; it never alters which slot was chosen.

    :param text: The model's reason.
    :returns: The cleaned reason, or ``None`` when nothing is left.
    """
    printable = "".join(char if char.isprintable() else " " for char in text)
    collapsed = _WHITESPACE_RUN.sub(" ", printable).strip()
    if not collapsed:
        return None
    if _CANDIDATE_ID_MENTION.search(collapsed):
        # The ids exist only between this class and the model. A reason that
        # cites one ("Choose S5 because…") means nothing to the person
        # reading it, so it is withheld; the chosen slot is unaffected.
        return None
    return collapsed[:MAX_REASON_LENGTH].rstrip()


def _response_schema(candidate_ids: list[str]) -> ResponseSchema:
    """Build the JSON Schema for one request's candidate ids.

    The ``enum`` is rebuilt per request, so a provider that enforces the
    schema structurally limits the model to the ids it was offered. There is
    no ``maxLength``: the schema is kept portable and the cap is applied here.
    """
    return ResponseSchema(
        name=_SCHEMA_NAME,
        schema={
            "type": "object",
            "properties": {
                "slot_id": {"type": "string", "enum": candidate_ids},
                "reason": {"type": "string"},
            },
            "required": ["slot_id", "reason"],
            "additionalProperties": False,
        },
    )


def _slot_lines(day: Sequence[DaySlot]) -> str:
    """Render the day as one line per slot, in the order given."""
    lines: list[str] = []
    for slot in day:
        span = f"{slot.start:%H:%M}-{slot.end:%H:%M}"
        lines.append(f"{slot.slot_id} {span}" if slot.slot_id else f"-- {span} unavailable")
    return "\n".join(lines)


class AISlotRanker:
    """Chooses a slot with the AI platform layer.

    :param ai: The shared AI service. All provider access goes through it.
    :param prompts: Prompt registry holding the versioned template.
    """

    def __init__(self, ai: AIService, prompts: PromptRegistry) -> None:
        self._ai = ai
        self._prompts = prompts

    async def choose_slot(
        self,
        *,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        request_id: str | None,
        target_date: date,
        day: Sequence[DaySlot],
    ) -> SlotChoice:
        """Ask the model to choose one of the day's free slots.

        Makes at most one model call. Every value rendered into the prompt is
        produced by the server: the date, its weekday, the number of free
        slots and the slot lines.

        :param hospital_id: Tenant, for log attribution only.
        :param actor_id: Acting user, for log attribution only.
        :param request_id: Correlation id of the HTTP request.
        :param target_date: The hospital-local day ``day`` describes.
        :param day: The day's slots in chronological order, times in the
            hospital's zone.
        :returns: The chosen id (always one from ``day``) and a cleaned reason.
        :raises AIError: If the provider failed.
        :raises AIResponseInvalidError: If the reply was rejected.
        """
        candidate_ids = [slot.slot_id for slot in day if slot.slot_id is not None]
        template = self._prompts.get(PROMPT_ID)
        rendered = template.render(
            weekday=_WEEKDAYS[target_date.weekday()],
            date=target_date.isoformat(),
            candidate_count=len(candidate_ids),
            slot_lines=_slot_lines(day),
        )

        response = await self._ai.complete(
            messages=[
                Message(role=message["role"], content=message["content"])
                for message in rendered.messages
            ],
            hint=template.model_hint,
            max_tokens=MAX_TOKENS,
            temperature=0.0,
            response_schema=_response_schema(candidate_ids),
            use_case=PROMPT_ID,
            module="appointment",
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.prompt_version,
            actor_id=actor_id,
            hospital_id=hospital_id,
            request_id=request_id,
        )

        choice, rejection = self._validate(response, candidate_ids)
        if choice is None:
            logger.warning(
                "appointment.slot_recommendation_rejected",
                kind=rejection,
                candidate_count=len(candidate_ids),
                content_length=len(response.content),
                prompt_id=rendered.prompt_id,
                prompt_version=rendered.prompt_version,
                provider=response.provider,
                model=response.model,
                hospital_id=str(hospital_id),
                request_id=request_id,
            )
            raise AIResponseInvalidError(rejection)

        return SlotChoice(
            slot_id=choice.slot_id,
            reason=_clean_reason(choice.reason),
            provider=response.provider,
            model=response.model,
        )

    @staticmethod
    def _validate(
        response: AIResponse, candidate_ids: list[str]
    ) -> tuple[_ModelChoice | None, str]:
        """Check the model's reply. Nothing is repaired.

        Returns the failure kind instead of raising, so the typed error is
        raised by the caller outside any ``except`` block: a
        ``JSONDecodeError`` keeps the whole reply in ``.doc`` and a Pydantic
        error renders its input, and neither may ride along as
        ``__context__``.

        :param response: The provider's response.
        :param candidate_ids: The ids that were offered.
        :returns: ``(choice, "")`` when valid, else ``(None, kind)``.
        """
        if response.finish_reason == "length":
            return None, "truncated"

        parsed: Any = None
        not_json = False
        try:
            # No fence stripping and no brace hunting: the reply is JSON or it
            # is rejected.
            parsed = json.loads(response.content.strip(), parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            not_json = True
        if not_json:
            return None, "not_json"

        choice: _ModelChoice | None = None
        try:
            choice = _ModelChoice.model_validate(parsed)
        except PydanticValidationError:
            choice = None
        if choice is None:
            return None, "schema_invalid"

        # Exact string equality: no trimming, no case folding.
        if choice.slot_id not in candidate_ids:
            return None, "unknown_candidate"
        return choice, ""
