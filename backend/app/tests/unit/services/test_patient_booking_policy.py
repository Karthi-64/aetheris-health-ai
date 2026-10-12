"""Unit tests for :class:`BookingPolicy` — what a stored settings value is read as."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.patient_app.booking_policy import BookingPolicy

HORIZON = "patient_app.booking_horizon_days"
LEAD = "patient_app.min_lead_minutes"


class TestFromSettings:
    def test_the_documented_defaults(self) -> None:
        assert BookingPolicy.from_settings({}) == BookingPolicy(30, 60)
        assert BookingPolicy() == BookingPolicy(30, 60)

    @pytest.mark.parametrize(
        "stored", [None, [], "x", 7, True, {"patient_app": {"booking_horizon_days": 3}}]
    )
    def test_settings_that_are_not_an_object_or_nest_the_keys_mean_the_defaults(
        self, stored: Any
    ) -> None:
        assert BookingPolicy.from_settings(stored) == BookingPolicy()

    @pytest.mark.parametrize(("horizon", "lead"), [(1, 0), (365, 10080), (14, 120)])
    def test_an_integer_within_bounds_is_used(self, horizon: int, lead: int) -> None:
        assert BookingPolicy.from_settings({HORIZON: horizon, LEAD: lead}) == BookingPolicy(
            horizon, lead
        )

    @pytest.mark.parametrize("value", [0, -1, 366, 10**9, "30", "7", 7.0, True, False, None, [7]])
    def test_a_horizon_outside_its_bounds_or_not_an_integer_means_the_default(
        self, value: Any
    ) -> None:
        assert BookingPolicy.from_settings({HORIZON: value, LEAD: 5}) == BookingPolicy(30, 5)

    @pytest.mark.parametrize("value", [-1, 10081, 10**9, "60", 60.0, True, False, None, [60]])
    def test_a_lead_outside_its_bounds_or_not_an_integer_means_the_default(
        self, value: Any
    ) -> None:
        assert BookingPolicy.from_settings({HORIZON: 5, LEAD: value}) == BookingPolicy(5, 60)

    def test_lead_bounds(self) -> None:
        assert BookingPolicy.from_settings({LEAD: 10081}).min_lead_minutes == 60
        assert BookingPolicy.from_settings({LEAD: -1}).min_lead_minutes == 60
