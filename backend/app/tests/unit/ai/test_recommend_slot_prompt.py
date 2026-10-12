"""Unit tests for the ``appointment.recommend_slot`` prompt template."""

from __future__ import annotations

from pathlib import Path

import app.ai.prompts as prompts_package
from app.ai.prompts.registry import PromptRegistry

TEMPLATES = Path(prompts_package.__file__).resolve().parent / "templates"

SLOT_LINES = "\n".join(
    [
        "S1 09:00-09:30",
        "-- 09:30-10:00 unavailable",
        "-- 10:00-10:30 unavailable",
        "S2 10:30-11:00",
        "S3 11:00-11:30",
    ]
)


def _registry() -> PromptRegistry:
    registry = PromptRegistry()
    registry.load_all(TEMPLATES)
    return registry


def test_template_loads_with_the_expected_identity() -> None:
    template = _registry().get("appointment.recommend_slot")

    assert template.id == "appointment.recommend_slot"
    assert template.version == "2.0.1"
    assert template.model_hint == "fast"


def test_rendered_prompt_is_complete_and_asks_for_json() -> None:
    rendered = _registry().render(
        "appointment.recommend_slot",
        weekday="Monday",
        date="2030-01-07",
        candidate_count=3,
        slot_lines=SLOT_LINES,
    )

    text = "\n".join(message["content"] for message in rendered.messages)
    assert "{{" not in text
    assert "}}" not in text
    assert "JSON" in text
    for expected in ("S1 09:00-09:30", "S2 10:30-11:00", "S3 11:00-11:30"):
        assert expected in text
    assert text.count("unavailable\n") == 2
    assert "Date: Monday 2030-01-07." in text
    assert "has 3 free slots" in text
    assert rendered.prompt_id == "appointment.recommend_slot"
    assert rendered.prompt_version == "2.0.1"


def test_system_message_occurs_exactly_once() -> None:
    rendered = _registry().render(
        "appointment.recommend_slot",
        weekday="Monday",
        date="2030-01-07",
        candidate_count=3,
        slot_lines=SLOT_LINES,
    )

    roles = [message["role"] for message in rendered.messages]
    assert roles == ["system", "user"]
    assert rendered.messages[0]["content"] == rendered.system


def test_prompt_keeps_the_model_advisory_and_away_from_clinical_judgement() -> None:
    system = _registry().get("appointment.recommend_slot").system

    assert "Never invent a time or an id" in system
    assert "no medical or clinical judgement" in system
    assert "You only advise" in system
    # Nothing in the template asks for, or leaves a place for, patient data.
    template = _registry().get("appointment.recommend_slot")
    for word in ("urgency", "Urgency", "patient_", "{{ patient", "{{ doctor", "score"):
        assert word not in template.system + template.user
