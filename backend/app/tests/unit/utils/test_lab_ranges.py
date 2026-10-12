"""Unit tests for reference-range selection and abnormal flagging.

``docs/modules/07-laboratory.md`` §16: "reference range flagging across
demographics". These are the rules that decide whether a clinician is told a
result is abnormal, so the boundaries are tested exactly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.models.lab import LabResultFlag
from app.utils.lab_ranges import (
    ReferenceRange,
    age_in_years,
    flag_value,
    parse_numeric,
    select_range,
)

HAEMOGLOBIN = [
    {"sex": "male", "age_min": 18, "low": "13.0", "high": "17.0", "critical_low": "7.0"},
    {"sex": "female", "age_min": 18, "low": "12.0", "high": "15.5", "critical_low": "7.0"},
    {"sex": "any", "age_min": 0, "age_max": 17, "low": "11.0", "high": "14.0"},
]


class TestParseNumeric:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("11.2", Decimal("11.2")),
            (" 7 ", Decimal(7)),
            (5, Decimal(5)),
            ("-0.5", Decimal("-0.5")),
        ],
    )
    def test_reads_numbers(self, raw: object, expected: Decimal) -> None:
        assert parse_numeric(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "positive", "1,5", "NaN", "Infinity", True, "1e"])
    def test_rejects_everything_else(self, raw: object) -> None:
        assert parse_numeric(raw) is None


class TestAgeInYears:
    @pytest.mark.parametrize(
        ("born", "on", "expected"),
        [
            (date(2000, 10, 5), date(2026, 10, 4), 25),  # the day before the birthday
            (date(2000, 10, 5), date(2026, 10, 5), 26),  # on it
            (date(2000, 2, 29), date(2025, 2, 28), 24),  # leap-day birthday, not yet
            (date(2000, 2, 29), date(2025, 3, 1), 25),
            (date(2026, 10, 1), date(2026, 10, 5), 0),  # a newborn
            (date(2027, 1, 1), date(2026, 10, 5), 0),  # never negative
        ],
    )
    def test_completed_years(self, born: date, on: date, expected: int) -> None:
        assert age_in_years(born, on) == expected


class TestSelectRange:
    def test_picks_the_range_for_the_patients_sex(self) -> None:
        male = select_range(HAEMOGLOBIN, sex="male", age=40)
        female = select_range(HAEMOGLOBIN, sex="female", age=40)

        assert male is not None
        assert female is not None
        assert (male.low, male.high) == (Decimal("13.0"), Decimal("17.0"))
        assert (female.low, female.high) == (Decimal("12.0"), Decimal("15.5"))
        assert male.critical_low == Decimal("7.0")
        assert male.critical_high is None

    def test_a_child_falls_through_to_the_range_for_anyone(self) -> None:
        child = select_range(HAEMOGLOBIN, sex="male", age=9)

        assert child is not None
        assert (child.low, child.high) == (Decimal("11.0"), Decimal("14.0"))

    @pytest.mark.parametrize("age", [0, 17])
    def test_age_bounds_are_inclusive(self, age: int) -> None:
        assert select_range(HAEMOGLOBIN, sex="female", age=age) is not None

    @pytest.mark.parametrize("sex", ["other", "unspecified"])
    def test_an_unrecorded_sex_is_never_given_a_sex_specific_range(self, sex: str) -> None:
        # An adult of unrecorded sex matches neither adult range, and the
        # range for anyone stops at 17: better no range than a guessed one.
        assert select_range(HAEMOGLOBIN, sex=sex, age=40) is None
        assert select_range(HAEMOGLOBIN, sex=sex, age=9) is not None

    def test_a_sex_specific_range_wins_over_one_for_anyone(self) -> None:
        ranges = [
            {"sex": "any", "low": "1", "high": "9"},
            {"sex": "female", "low": "2", "high": "8"},
        ]

        chosen = select_range(ranges, sex="female", age=30)

        assert chosen is not None
        assert chosen.low == Decimal(2)

    def test_a_missing_sex_means_anyone(self) -> None:
        assert select_range([{"low": "1", "high": "9"}], sex="male", age=30) is not None

    @pytest.mark.parametrize("ranges", [None, [], ["nonsense"], [{"sex": "male", "age_min": 50}]])
    def test_nothing_applicable_is_none(self, ranges: object) -> None:
        assert select_range(ranges, sex="male", age=30) is None  # type: ignore[arg-type]


class TestFlagValue:
    BOUNDS = ReferenceRange(
        low=Decimal("3.5"),
        high=Decimal("5.1"),
        critical_low=Decimal("2.5"),
        critical_high=Decimal("6.5"),
    )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2.4", LabResultFlag.CRITICAL),
            ("2.5", LabResultFlag.LOW),  # equal to the critical bound: not yet critical
            ("3.4", LabResultFlag.LOW),
            ("3.5", LabResultFlag.NORMAL),  # the bounds themselves are in range
            ("4.2", LabResultFlag.NORMAL),
            ("5.1", LabResultFlag.NORMAL),
            ("5.2", LabResultFlag.HIGH),
            ("6.5", LabResultFlag.HIGH),
            ("6.6", LabResultFlag.CRITICAL),  # §14: potassium above 6.5
        ],
    )
    def test_each_side_of_every_boundary(self, value: str, expected: LabResultFlag) -> None:
        assert flag_value(Decimal(value), self.BOUNDS) is expected

    def test_no_range_is_reported_as_no_flag_not_as_normal(self) -> None:
        assert flag_value(Decimal(5), None) is None

    def test_a_one_sided_range(self) -> None:
        upper_only = ReferenceRange(high=Decimal(200))

        assert flag_value(Decimal(0), upper_only) is LabResultFlag.NORMAL
        assert flag_value(Decimal(201), upper_only) is LabResultFlag.HIGH

    @given(value=st.decimals(min_value=-1000, max_value=1000, places=3))
    def test_a_value_inside_the_range_is_always_normal_and_outside_never(
        self, value: Decimal
    ) -> None:
        flag = flag_value(value, self.BOUNDS)

        inside = self.BOUNDS.low <= value <= self.BOUNDS.high  # type: ignore[operator]
        assert (flag is LabResultFlag.NORMAL) == inside
