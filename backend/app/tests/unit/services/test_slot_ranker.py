"""Unit tests for :class:`~app.services.slot_ranker.AISlotRanker`.

A fake ``AIService``; no provider and no HTTP. The ranker's job is to show the
model only a schedule and to accept back only an id it offered — so most of
this file is the ways a reply is rejected.
"""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import app.ai.prompts as prompts_package
from app.ai.errors import AIProviderUnavailableError, AIResponseInvalidError
from app.ai.prompts.registry import PromptRegistry
from app.ai.providers.base import AIResponse, Message, ResponseSchema
from app.services.appointment_service import DaySlot, SlotChoice, SlotRanker
from app.services.slot_ranker import MAX_TOKENS, PROMPT_ID, AISlotRanker
from app.tests.ai_fakes import RecordingLogger, contains_any, install_loggers

KOLKATA = ZoneInfo("Asia/Kolkata")
TARGET = date(2030, 1, 7)  # a Monday
HOSPITAL_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()
REPLY_MARKER = "REPLY-MARKER-e19d"


def _slot(hour: int, minute: int, slot_id: str | None) -> DaySlot:
    start = datetime(2030, 1, 7, hour, minute, tzinfo=KOLKATA)
    return DaySlot(start=start, end=start + timedelta(minutes=30), slot_id=slot_id)


#: The day from contract section 6.4: three free slots around two taken ones.
DAY = [
    _slot(9, 0, "S1"),
    _slot(9, 30, None),
    _slot(10, 0, None),
    _slot(10, 30, "S2"),
    _slot(11, 0, "S3"),
]


class FakeAIService:
    """Stands in for ``AIService``: records the call, answers with a response."""

    def __init__(self, response: AIResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> AIResponse:
        """Record the keyword arguments and return the canned response."""
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _prompts() -> PromptRegistry:
    registry = PromptRegistry()
    registry.load_all(Path(prompts_package.__file__).resolve().parent / "templates")
    return registry


def _reply(content: str, *, finish_reason: str = "stop") -> AIResponse:
    return AIResponse(
        content=content, finish_reason=finish_reason, model="returned-model", provider="groq"
    )


def _valid(slot_id: str = "S2", reason: str = "It sits next to an unavailable slot.") -> str:
    return json.dumps({"slot_id": slot_id, "reason": reason})


async def _choose(ai: FakeAIService) -> SlotChoice:
    ranker = AISlotRanker(ai, _prompts())  # type: ignore[arg-type]
    return await ranker.choose_slot(
        hospital_id=HOSPITAL_ID,
        actor_id=ACTOR_ID,
        request_id="req-9",
        target_date=TARGET,
        day=DAY,
    )


async def _rejection(content: str, **reply_kwargs: Any) -> AIResponseInvalidError:
    with pytest.raises(AIResponseInvalidError) as caught:
        await _choose(FakeAIService(_reply(content, **reply_kwargs)))
    return caught.value


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    """Record what the ranker logs."""
    return install_loggers(monkeypatch, ["app.services.slot_ranker"])


class TestValidReply:
    async def test_returns_the_choice_with_provider_and_model(self, log: RecordingLogger) -> None:
        choice = await _choose(FakeAIService(_reply(_valid("S2"))))

        assert choice == SlotChoice(
            slot_id="S2",
            reason="It sits next to an unavailable slot.",
            provider="groq",
            model="returned-model",
        )
        assert log.events("appointment.slot_recommendation_rejected") == []

    async def test_surrounding_whitespace_is_tolerated(self, log: RecordingLogger) -> None:
        choice = await _choose(FakeAIService(_reply(f"\n  {_valid('S1')}\n")))

        assert choice.slot_id == "S1"

    async def test_the_call_made_to_the_ai_service(self, log: RecordingLogger) -> None:
        ai = FakeAIService(_reply(_valid()))

        await _choose(ai)

        assert len(ai.calls) == 1
        call = ai.calls[0]
        assert call["hint"] == "fast"
        # Room for a reasoning model's reasoning tokens as well as the answer.
        assert call["max_tokens"] == MAX_TOKENS
        assert MAX_TOKENS >= 1024
        assert call["temperature"] == 0.0
        assert call["prompt_id"] == PROMPT_ID == "appointment.recommend_slot"
        assert call["prompt_version"] == "2.0.1"
        assert call["use_case"] == "appointment.recommend_slot"
        assert call["module"] == "appointment"
        assert call["hospital_id"] == HOSPITAL_ID
        assert call["actor_id"] == ACTOR_ID
        assert call["request_id"] == "req-9"
        # No tool, no stream, no model pinned by the caller.
        for absent in ("tools", "stream", "model"):
            assert absent not in call

        schema = call["response_schema"]
        assert isinstance(schema, ResponseSchema)
        assert schema.name == "slot_recommendation"
        assert schema.schema == {
            "type": "object",
            "properties": {
                "slot_id": {"type": "string", "enum": ["S1", "S2", "S3"]},
                "reason": {"type": "string"},
            },
            "required": ["slot_id", "reason"],
            "additionalProperties": False,
        }

    async def test_the_prompt_is_the_schedule_and_nothing_else(self, log: RecordingLogger) -> None:
        ai = FakeAIService(_reply(_valid()))

        await _choose(ai)

        messages: list[Message] = ai.calls[0]["messages"]
        assert [message.role for message in messages] == ["system", "user"]
        user = messages[1].content
        assert user == (
            "Date: Monday 2030-01-07. All times are local hospital time on a 24-hour clock.\n"
            "\n"
            "The doctor's day has 3 free slots. A line that starts with an id is free. "
            'A line marked "unavailable" cannot be chosen.\n'
            "S1 09:00-09:30\n"
            "-- 09:30-10:00 unavailable\n"
            "-- 10:00-10:30 unavailable\n"
            "S2 10:30-11:00\n"
            "S3 11:00-11:30\n"
            "\n"
            "Reply with the JSON object only.\n"
        )
        everything = "\n".join(message.content for message in messages)
        assert "{{" not in everything
        # No identity of any kind, no zone name, no urgency line.
        leaked = contains_any(
            [everything], [str(HOSPITAL_ID), str(ACTOR_ID), "req-9", "Asia/Kolkata", "+05:30"]
        )
        assert leaked is False
        assert not any(line.startswith("Urgency") for line in everything.splitlines())

    def test_the_protocol_carries_no_patient_and_no_urgency(self) -> None:
        for function in (AISlotRanker.choose_slot, SlotRanker.choose_slot):
            parameters = set(inspect.signature(function).parameters) - {"self"}
            assert parameters == {"hospital_id", "actor_id", "request_id", "target_date", "day"}

    async def test_typed_ai_errors_propagate(self, log: RecordingLogger) -> None:
        error = AIProviderUnavailableError("connection")

        with pytest.raises(AIProviderUnavailableError) as caught:
            await _choose(FakeAIService(error))

        assert caught.value is error


class TestRejectedReplies:
    """Nothing is repaired: a reply is exactly right or it is rejected."""

    @pytest.mark.parametrize(
        "content",
        [
            "I would choose S2 because it keeps the day compact.",
            f"```json\n{_valid()}\n```",
            f"{_valid()} Hope that helps!",
            f"Here you go: {_valid()}",
            '{"slot_id": "S2", "reason": NaN}',
            '{"slot_id": "S2", "reason": Infinity}',
            '{"slot_id": "S2", "reason": "unterminated',
            "",
            "[" * 5000,
        ],
        ids=[
            "prose",
            "fenced",
            "trailing-text",
            "leading-text",
            "nan",
            "infinity",
            "cut-off",
            "empty",
            "deeply-nested",
        ],
    )
    async def test_not_json(self, log: RecordingLogger, content: str) -> None:
        exc = await _rejection(content)

        assert exc.kind == "not_json"
        # The JSONDecodeError, which keeps the whole reply, is not attached.
        assert exc.__context__ is None
        assert exc.__cause__ is None

    @pytest.mark.parametrize(
        "content",
        [
            "[]",
            json.dumps([{"slot_id": "S2", "reason": "x"}]),
            '"S2"',
            "42",
            "null",
            "{}",
            json.dumps({"slot_id": "S2"}),
            json.dumps({"reason": "x"}),
            json.dumps({"slot": "S2", "reason": "x"}),
            json.dumps({"slot_id": "S2", "reason": "x", "score": 0.9}),
            json.dumps({"slot_id": 2, "reason": "x"}),
            json.dumps({"slot_id": "S2", "reason": ["a", "b"]}),
            json.dumps({"slot_id": "S2", "reason": None}),
            json.dumps({"slot_id": "S2", "reason": "x" * 2001}),
            json.dumps({"slot_id": "s1", "reason": "x"}),
            json.dumps({"slot_id": " S1", "reason": "x"}),
            json.dumps({"slot_id": "S1 ", "reason": "x"}),
            json.dumps({"slot_id": "S01", "reason": "x"}),
            json.dumps({"slot_id": "S0", "reason": "x"}),
            json.dumps({"slot_id": "10:30", "reason": "x"}),
            json.dumps({"slot_id": "2030-01-07T10:30:00+05:30", "reason": "x"}),
            json.dumps({"slot_id": "S1\n", "reason": "x"}),
        ],
    )
    async def test_schema_invalid(self, log: RecordingLogger, content: str) -> None:
        exc = await _rejection(content)

        assert exc.kind == "schema_invalid"
        # The Pydantic error, which renders its input, is not attached.
        assert exc.__context__ is None
        assert exc.__cause__ is None

    @pytest.mark.parametrize("slot_id", ["S9", "S4", "S99", "S999"])
    async def test_well_formed_id_that_was_not_offered(
        self, log: RecordingLogger, slot_id: str
    ) -> None:
        exc = await _rejection(_valid(slot_id))

        assert exc.kind == "unknown_candidate"
        assert exc.__context__ is None

    async def test_an_unavailable_slot_cannot_be_named(self, log: RecordingLogger) -> None:
        """Unavailable slots have no id, so there is nothing the model can say to pick one."""
        offered = {slot.slot_id for slot in DAY if slot.slot_id}
        assert offered == {"S1", "S2", "S3"}

        for attempt in ("--", "09:30-10:00", "S2b"):
            exc = await _rejection(_valid(attempt))
            assert exc.kind in {"schema_invalid", "unknown_candidate"}

    async def test_truncated_reply_is_rejected_even_if_it_parses(
        self, log: RecordingLogger
    ) -> None:
        exc = await _rejection(_valid("S2"), finish_reason="length")

        assert exc.kind == "truncated"

    async def test_rejection_is_an_invalid_ai_response_with_a_static_message(
        self, log: RecordingLogger
    ) -> None:
        exc = await _rejection(f"not json {REPLY_MARKER}")

        assert exc.error_code == "AI_RESPONSE_INVALID"
        assert exc.status_code == 503
        assert exc.message == (
            "The AI suggestion could not be used. Choose a slot manually or try again."
        )
        assert exc.detail == {}

    @pytest.mark.parametrize(
        "content",
        [
            f"prose with {REPLY_MARKER}",
            json.dumps({"slot_id": "S2", "reason": REPLY_MARKER, "extra": REPLY_MARKER}),
            json.dumps({"slot_id": "S77", "reason": REPLY_MARKER}),
        ],
        ids=["not-json", "schema-invalid", "unknown-candidate"],
    )
    async def test_rejection_log_has_kind_and_lengths_only(
        self, log: RecordingLogger, content: str
    ) -> None:
        exc = await _rejection(content)

        entries = log.events("appointment.slot_recommendation_rejected")
        assert len(entries) == 1
        entry = entries[0]
        assert entry["level"] == "warning"
        assert entry["kind"] == exc.kind
        assert entry["candidate_count"] == 3
        assert entry["content_length"] == len(content)
        assert entry["prompt_id"] == "appointment.recommend_slot"
        assert entry["prompt_version"] == "2.0.1"
        assert entry["provider"] == "groq"
        assert entry["model"] == "returned-model"
        assert entry["hospital_id"] == str(HOSPITAL_ID)
        assert entry["request_id"] == "req-9"
        # Never the content, never the offending id.
        leaked = contains_any(
            [log.text(), str(exc), repr(exc), repr(exc.detail)], [REPLY_MARKER, "S77"]
        )
        assert leaked is False


class TestReasonCleaning:
    """The reason is untrusted model text: made plain, bounded, never trusted."""

    async def _reason(self, reason: str) -> str | None:
        choice = await _choose(FakeAIService(_reply(_valid("S1", reason))))
        assert choice.slot_id == "S1"  # cleaning never changes the chosen slot
        return choice.reason

    async def test_control_characters_and_newlines_collapse_to_spaces(
        self, log: RecordingLogger
    ) -> None:
        cleaned = await self._reason("Line one\n\nline\ttwo\x00\x1b[31m red​ end  ")

        assert cleaned == "Line one line two [31m red end"

    async def test_long_reason_is_cut_to_200_characters(self, log: RecordingLogger) -> None:
        cleaned = await self._reason("word " * 100)

        assert cleaned is not None
        assert len(cleaned) <= 200
        assert cleaned == ("word " * 100)[:200].rstrip()

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t\r", "\x00\x01"])
    async def test_empty_reason_becomes_none(self, log: RecordingLogger, blank: str) -> None:
        assert await self._reason(blank) is None

    async def test_markup_is_passed_on_as_plain_text_not_interpreted(
        self, log: RecordingLogger
    ) -> None:
        text = "<img src=x onerror=alert(1)> see http://example.test"

        assert await self._reason(text) == text


class TestReasonNeverCitesACandidateId:
    """The ids are private to the ranker and the model; a reader sees times."""

    @pytest.mark.parametrize(
        "text",
        [
            "Choose S5 because it is directly before the unavailable slot.",
            "S12 keeps the morning compact.",
            "Earliest free slot (S1).",
        ],
    )
    def test_a_reason_that_cites_an_id_is_withheld(self, text: str) -> None:
        from app.services.slot_ranker import _clean_reason

        assert _clean_reason(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "11:00 sits directly before an unavailable slot, keeping the day compact.",
            "Slots after lunch are free; 14:00 is the earliest of them.",
        ],
    )
    def test_a_reason_about_times_is_kept(self, text: str) -> None:
        from app.services.slot_ranker import _clean_reason

        assert _clean_reason(text) == text
