"""Unit tests for report period arithmetic. No database, no clock."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.exceptions import ValidationError
from app.services.report_periods import (
    MAX_REPORT_DATE,
    MIN_REPORT_DATE,
    BucketSpan,
    add_months,
    build_buckets,
    local_bounds,
    month_end,
    month_start,
    resolve_period,
    week_start,
)

TODAY = date(2026, 10, 6)  # a Tuesday


def _error(exc: pytest.ExceptionInfo[ValidationError]) -> tuple[str, str, str]:
    """Return ``(message, field, field message)`` of a rule error."""
    detail = exc.value.detail or {}
    first = detail["errors"][0]
    return exc.value.message, first["field"], first["message"]


class TestDefaults:
    def test_no_parameters_gives_the_thirty_days_ending_today_by_day(self) -> None:
        period = resolve_period(None, None, None, TODAY)

        assert (period.from_, period.to, period.granularity) == (
            date(2026, 9, 7),
            TODAY,
            "day",
        )
        assert (period.to - period.from_).days == 29

    def test_only_from_runs_to_today(self) -> None:
        period = resolve_period(date(2026, 10, 1), None, None, TODAY)

        assert (period.from_, period.to) == (date(2026, 10, 1), TODAY)

    def test_only_to_starts_twenty_nine_days_before_it(self) -> None:
        period = resolve_period(None, date(2026, 3, 31), None, TODAY)

        assert (period.from_, period.to) == (date(2026, 3, 2), date(2026, 3, 31))

    def test_a_future_range_is_allowed(self) -> None:
        period = resolve_period(date(2027, 1, 1), date(2027, 1, 31), "week", TODAY)

        assert period.to == date(2027, 1, 31)


class TestOrder:
    def test_a_single_day_passes(self) -> None:
        period = resolve_period(TODAY, TODAY, None, TODAY)

        assert period.from_ == period.to == TODAY

    def test_to_before_from_is_refused_on_the_to_field(self) -> None:
        with pytest.raises(ValidationError) as exc:
            resolve_period(date(2026, 10, 6), date(2026, 10, 5), None, TODAY)

        assert exc.value.status_code == 422
        assert _error(exc) == (
            "`to` must not be before `from`.",
            "to",
            "`to` must not be before `from`.",
        )

    def test_only_from_after_today_is_refused(self) -> None:
        with pytest.raises(ValidationError) as exc:
            resolve_period(TODAY + timedelta(days=1), None, None, TODAY)

        assert _error(exc)[0] == "`to` must not be before `from`."


class TestTwelveMonths:
    @pytest.mark.parametrize(
        ("start", "end"),
        [
            (date(2026, 1, 1), date(2026, 12, 31)),
            (date(2024, 2, 29), date(2025, 2, 27)),
            (date(2025, 3, 31), date(2026, 3, 30)),
        ],
    )
    def test_twelve_calendar_months_inclusive_pass(self, start: date, end: date) -> None:
        assert resolve_period(start, end, None, TODAY).to == end

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            (date(2026, 1, 1), date(2027, 1, 1)),
            (date(2024, 2, 29), date(2025, 2, 28)),
            (date(2025, 3, 31), date(2026, 3, 31)),
        ],
    )
    def test_one_day_more_is_refused(self, start: date, end: date) -> None:
        with pytest.raises(ValidationError) as exc:
            resolve_period(start, end, None, TODAY)

        assert _error(exc) == (
            "Date range must not exceed 12 months.",
            "to",
            "Date range must not exceed 12 months.",
        )

    def test_add_months_clamps_to_the_last_day_of_a_short_month(self) -> None:
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
        assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
        assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


class TestGranularity:
    @pytest.mark.parametrize("granularity", ["day", "week", "month"])
    def test_each_of_the_three_passes(self, granularity: str) -> None:
        assert resolve_period(None, None, granularity, TODAY).granularity == granularity

    @pytest.mark.parametrize("granularity", ["hour", "Day", "", "year", " day"])
    def test_anything_else_is_refused_with_the_exact_message(self, granularity: str) -> None:
        with pytest.raises(ValidationError) as exc:
            resolve_period(None, None, granularity, TODAY)

        assert _error(exc) == (
            "`granularity` must be one of: day, week, month.",
            "granularity",
            "`granularity` must be one of: day, week, month.",
        )

    def test_rules_are_reported_in_a_fixed_order(self) -> None:
        """Order beats length beats granularity, so the message is predictable."""
        with pytest.raises(ValidationError) as order_first:
            resolve_period(date(2026, 10, 6), date(2026, 10, 5), "hour", TODAY)
        with pytest.raises(ValidationError) as length_second:
            resolve_period(date(2025, 1, 1), date(2026, 10, 5), "hour", TODAY)

        assert _error(order_first)[0] == "`to` must not be before `from`."
        assert _error(length_second)[0] == "Date range must not exceed 12 months."


class TestSupportedDates:
    """Dates at the ends of the calendar are refused before any arithmetic."""

    FROM_MESSAGE = "`from` must be between 1900-01-01 and 2999-12-31."
    TO_MESSAGE = "`to` must be between 1900-01-01 and 2999-12-31."

    @pytest.mark.parametrize(
        ("start", "end", "field"),
        [
            (None, date(9999, 12, 31), "to"),
            (date(9999, 12, 1), date(9999, 12, 31), "from"),
            (date(9999, 1, 1), None, "from"),
            (date(1, 1, 1), date(1, 1, 2), "from"),
            (None, date(1, 1, 5), "to"),
            (None, date(1899, 12, 31), "to"),
            (date(3000, 1, 1), None, "from"),
            (date(2999, 12, 1), date(3000, 1, 1), "to"),
            (date(1899, 12, 31), date(1900, 1, 5), "from"),
        ],
    )
    def test_a_date_outside_the_range_is_refused_on_its_own_field(
        self, start: date | None, end: date | None, field: str
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            resolve_period(start, end, None, TODAY)

        message = self.FROM_MESSAGE if field == "from" else self.TO_MESSAGE
        assert exc.value.status_code == 422
        assert _error(exc) == (message, field, message)

    def test_the_bounds_themselves_are_usable_end_to_end(self) -> None:
        """The first and last supported days resolve, bucket and convert to UTC."""
        for zone in (ZoneInfo("Pacific/Kiritimati"), ZoneInfo("Etc/GMT+12")):
            for start, end in (
                (MIN_REPORT_DATE, MIN_REPORT_DATE + timedelta(days=40)),
                (MAX_REPORT_DATE - timedelta(days=40), MAX_REPORT_DATE),
            ):
                for granularity in ("day", "week", "month"):
                    period = resolve_period(start, end, granularity, TODAY)
                    buckets = build_buckets(period.from_, period.to, period.granularity)
                    lo, hi = local_bounds(period.from_, period.to, zone)

                    assert buckets[0].start <= start
                    assert buckets[-1].end >= end
                    assert lo < hi

    def test_only_to_at_the_lower_bound_defaults_from_before_it(self) -> None:
        period = resolve_period(None, MIN_REPORT_DATE, None, TODAY)

        assert period.from_ == date(1899, 12, 3)

    def test_it_is_reported_before_the_order_rule(self) -> None:
        with pytest.raises(ValidationError) as exc:
            resolve_period(date(9999, 12, 31), date(1, 1, 1), "hour", TODAY)

        assert _error(exc)[0] == self.FROM_MESSAGE


class TestBuckets:
    def test_day_gives_one_whole_bucket_per_date(self) -> None:
        buckets = build_buckets(date(2026, 10, 4), date(2026, 10, 6), "day")

        assert buckets == [
            BucketSpan(date(2026, 10, 4), date(2026, 10, 4), False),
            BucketSpan(date(2026, 10, 5), date(2026, 10, 5), False),
            BucketSpan(date(2026, 10, 6), date(2026, 10, 6), False),
        ]

    def test_week_starts_on_monday_and_marks_the_cut_ends_partial(self) -> None:
        # Wednesday 30 Sep .. Tuesday 13 Oct 2026.
        buckets = build_buckets(date(2026, 9, 30), date(2026, 10, 13), "week")

        assert buckets == [
            BucketSpan(date(2026, 9, 28), date(2026, 10, 4), True),
            BucketSpan(date(2026, 10, 5), date(2026, 10, 11), False),
            BucketSpan(date(2026, 10, 12), date(2026, 10, 18), True),
        ]
        assert all(bucket.start.weekday() == 0 for bucket in buckets)
        assert buckets[0].start < date(2026, 9, 30)

    def test_a_range_of_whole_weeks_has_no_partial_bucket(self) -> None:
        buckets = build_buckets(date(2026, 10, 5), date(2026, 10, 18), "week")

        assert [bucket.partial for bucket in buckets] == [False, False]

    def test_month_marks_the_first_and_last_partial(self) -> None:
        buckets = build_buckets(date(2026, 9, 30), date(2026, 11, 6), "month")

        assert buckets == [
            BucketSpan(date(2026, 9, 1), date(2026, 9, 30), True),
            BucketSpan(date(2026, 10, 1), date(2026, 10, 31), False),
            BucketSpan(date(2026, 11, 1), date(2026, 11, 30), True),
        ]

    def test_a_whole_february_in_a_leap_year(self) -> None:
        buckets = build_buckets(date(2024, 2, 1), date(2024, 2, 29), "month")

        assert buckets == [BucketSpan(date(2024, 2, 1), date(2024, 2, 29), False)]

    @pytest.mark.parametrize("granularity", ["week", "month"])
    def test_a_range_inside_one_bucket_gives_exactly_one(self, granularity: str) -> None:
        buckets = build_buckets(date(2026, 10, 6), date(2026, 10, 8), granularity)

        assert len(buckets) == 1
        assert buckets[0].partial is True

    def test_a_year_by_day_is_three_hundred_and_sixty_five_buckets(self) -> None:
        buckets = build_buckets(date(2026, 1, 1), date(2026, 12, 31), "day")

        assert len(buckets) == 365
        assert [bucket.start for bucket in buckets] == sorted(bucket.start for bucket in buckets)

    def test_an_unknown_granularity_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown granularity"):
            build_buckets(TODAY, TODAY, "hour")

    def test_calendar_helpers(self) -> None:
        assert week_start(date(2026, 10, 11)) == date(2026, 10, 5)  # Sunday → Monday
        assert week_start(date(2026, 10, 5)) == date(2026, 10, 5)
        assert month_start(date(2026, 10, 31)) == date(2026, 10, 1)
        assert month_end(date(2026, 2, 3)) == date(2026, 2, 28)


class TestLocalBounds:
    def test_kolkata_midnight_is_half_past_six_the_evening_before_in_utc(self) -> None:
        lo, hi = local_bounds(date(2026, 10, 6), date(2026, 10, 6), ZoneInfo("Asia/Kolkata"))

        assert lo == datetime(2026, 10, 5, 18, 30, tzinfo=UTC)
        assert hi == datetime(2026, 10, 6, 18, 30, tzinfo=UTC)
        assert lo.tzinfo is UTC

    def test_a_range_ends_at_the_midnight_after_its_last_day(self) -> None:
        lo, hi = local_bounds(date(2026, 10, 1), date(2026, 10, 6), ZoneInfo("Asia/Kolkata"))

        assert (lo, hi) == (
            datetime(2026, 9, 30, 18, 30, tzinfo=UTC),
            datetime(2026, 10, 6, 18, 30, tzinfo=UTC),
        )

    def test_the_day_clocks_go_forward_is_twenty_three_hours_long(self) -> None:
        lo, hi = local_bounds(date(2026, 3, 8), date(2026, 3, 8), ZoneInfo("America/New_York"))

        assert hi - lo == timedelta(hours=23)
        assert lo == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)

    def test_the_day_clocks_go_back_is_twenty_five_hours_long(self) -> None:
        lo, hi = local_bounds(date(2026, 11, 1), date(2026, 11, 1), ZoneInfo("America/New_York"))

        assert hi - lo == timedelta(hours=25)
        assert hi == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)

    def test_utc_is_the_calendar_day_itself(self) -> None:
        lo, hi = local_bounds(date(2026, 10, 6), date(2026, 10, 6), ZoneInfo("UTC"))

        assert (lo, hi) == (
            datetime(2026, 10, 6, tzinfo=UTC),
            datetime(2026, 10, 7, tzinfo=UTC),
        )
