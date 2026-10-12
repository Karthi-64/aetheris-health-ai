"""Unit tests for the feature-flag helpers."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.feature_flags import (
    AI_SLOT_RECOMMENDATION,
    KNOWN_FLAGS,
    flag_is_on,
    with_default_flag,
)

KEY = AI_SLOT_RECOMMENDATION


def test_the_key_and_the_known_flags() -> None:
    assert KEY == "feature.ai.slot_recommendation"
    assert KNOWN_FLAGS == (KEY,)


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({KEY: True}, True),
        ({KEY: False}, False),
        ({}, False),
        (None, False),
        # Only an exact True is on: none of these may switch a gated feature on.
        ({KEY: "true"}, False),
        ({KEY: "false"}, False),
        ({KEY: 1}, False),
        ({KEY: "yes"}, False),
        ({KEY: [True]}, False),
        ({KEY: None}, False),
        ({"feature.other": True}, False),
    ],
)
def test_flag_is_on_only_for_an_exact_true(settings: dict[str, Any] | None, expected: bool) -> None:
    assert flag_is_on(settings, KEY) is expected


def test_with_default_flag_sets_an_absent_key_on_a_new_dict() -> None:
    original = {"billing.tax_rate": "18.00"}

    updated = with_default_flag(original, KEY, True)

    assert updated == {"billing.tax_rate": "18.00", KEY: True}
    assert updated is not original
    assert original == {"billing.tax_rate": "18.00"}


def test_with_default_flag_starts_from_nothing() -> None:
    assert with_default_flag(None, KEY, True) == {KEY: True}
    assert with_default_flag({}, KEY, True) == {KEY: True}


@pytest.mark.parametrize("explicit", [False, True, "false", None])
def test_with_default_flag_leaves_an_explicit_value_alone(explicit: Any) -> None:
    assert with_default_flag({KEY: explicit}, KEY, True) is None
