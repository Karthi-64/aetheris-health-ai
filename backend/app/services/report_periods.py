"""Period arithmetic for Reports and Dashboards.

Pure functions, no I/O: defaults and validation of a report's date range,
calendar buckets, and the UTC instants a hospital-local date range covers.
Everything a report says about "today", "this week" or "October" is decided
here, in the hospital's timezone, so that the SQL only ever receives two UTC
instants and a zone name (``docs/modules/10-reports-dashboard.md`` §4, rule 3).

**Weeks start on Monday** (ISO), matching PostgreSQL's ``date_trunc('week')``
and the doctor availability model's ``day_of_week 0 = Monday``.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from app.core.exceptions import ValidationError

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

__all__ = [
    "DEFAULT_GRANULARITY",
    "DEFAULT_RANGE_DAYS",
    "GRANULARITIES",
    "MAX_RANGE_MONTHS",
    "MAX_REPORT_DATE",
    "MIN_REPORT_DATE",
    "BucketSpan",
    "ResolvedPeriod",
    "add_months",
    "build_buckets",
    "field_error",
    "local_bounds",
    "month_end",
    "month_start",
    "resolve_period",
    "week_start",
]

#: The bucket sizes a period report accepts, as PostgreSQL ``date_trunc`` units.
GRANULARITIES: tuple[str, ...] = ("day", "week", "month")

DEFAULT_GRANULARITY = "day"

#: A report with no ``from`` covers this many days, ending on ``to``.
DEFAULT_RANGE_DAYS = 30

#: The longest range one report may cover, in calendar months.
MAX_RANGE_MONTHS = 12

#: The earliest and latest dates a report accepts. Python's calendar stops at
#: years 1 and 9999, so a range touching either end cannot be shifted by a day,
#: a month or a UTC offset; refusing such dates up front keeps every later step
#: free of overflow. No hospital has data anywhere near either bound.
MIN_REPORT_DATE = date(1900, 1, 1)
MAX_REPORT_DATE = date(2999, 12, 31)


@dataclass(frozen=True, slots=True)
class ResolvedPeriod:
    """A validated report period, with defaults applied.

    :param from_: First hospital-local date, inclusive.
    :param to: Last hospital-local date, inclusive.
    :param granularity: ``day``, ``week`` or ``month``.
    """

    from_: date
    to: date
    granularity: str


@dataclass(frozen=True, slots=True)
class BucketSpan:
    """One calendar bucket of a report.

    :param start: The day, the Monday, or the 1st of the month. May fall
        before the report's ``from``.
    :param end: The day, the Sunday, or the last day of the month. May fall
        after the report's ``to``.
    :param partial: Whether the bucket reaches outside the report's range, so
        its figures cover only part of the calendar bucket.
    """

    start: date
    end: date
    partial: bool


def field_error(field: str, message: str) -> ValidationError:
    """Build a 422 naming one offending parameter, in the standard error shape.

    :param field: The query parameter at fault.
    :param message: The exact text shown to the caller.
    :returns: The error to raise.
    """
    return ValidationError(
        message=message, detail={"errors": [{"field": field, "message": message}]}
    )


def add_months(day: date, months: int) -> date:
    """Return ``day`` moved forward by whole calendar months.

    The day of the month is kept where the target month has it and clamped to
    that month's last day otherwise (31 January + 1 month = 28 or 29 February).

    :param day: The starting date.
    :param months: How many months to add. Not negative.
    :returns: The shifted date.
    """
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def week_start(day: date) -> date:
    """Return the Monday of the ISO week ``day`` falls in."""
    return day - timedelta(days=day.weekday())


def month_start(day: date) -> date:
    """Return the first day of ``day``'s month."""
    return day.replace(day=1)


def month_end(day: date) -> date:
    """Return the last day of ``day``'s month."""
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def resolve_period(
    from_: date | None, to: date | None, granularity: str | None, today: date
) -> ResolvedPeriod:
    """Apply defaults to a report period and validate it.

    ``to`` defaults to today; ``from`` defaults to 29 days before ``to`` (a
    30-day range); ``granularity`` defaults to ``day``. Rules are checked in a
    fixed order, so a request breaking several is always told the same one:
    supported dates (``from``, then ``to``), then order, then length, then
    granularity. The supported-dates rule runs before any date arithmetic.

    :param from_: First date requested, or ``None``.
    :param to: Last date requested, or ``None``.
    :param granularity: Bucket size requested, or ``None``.
    :param today: Today in the hospital's timezone.
    :returns: The resolved period.
    :raises ValidationError: If a date is outside the supported range, ``to``
        is before ``from``, the range is longer than 12 calendar months, or the
        granularity is not one of the three.
    """
    for field, value in (("from", from_), ("to", to)):
        if value is not None and not MIN_REPORT_DATE <= value <= MAX_REPORT_DATE:
            raise field_error(
                field,
                f"`{field}` must be between {MIN_REPORT_DATE.isoformat()} "
                f"and {MAX_REPORT_DATE.isoformat()}.",
            )

    resolved_to = to if to is not None else today
    resolved_from = (
        from_ if from_ is not None else resolved_to - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    )
    resolved_granularity = granularity if granularity is not None else DEFAULT_GRANULARITY

    if resolved_from > resolved_to:
        raise field_error("to", "`to` must not be before `from`.")
    if resolved_to >= add_months(resolved_from, MAX_RANGE_MONTHS):
        raise field_error("to", "Date range must not exceed 12 months.")
    if resolved_granularity not in GRANULARITIES:
        raise field_error("granularity", "`granularity` must be one of: day, week, month.")

    return ResolvedPeriod(from_=resolved_from, to=resolved_to, granularity=resolved_granularity)


def build_buckets(from_: date, to: date, granularity: str) -> list[BucketSpan]:
    """List every calendar bucket that intersects ``[from_, to]``, in order.

    The list is what a report zero-fills against, so a day with no activity
    still appears with zeros rather than leaving a gap in the chart.

    :param from_: First date of the range, inclusive.
    :param to: Last date of the range, inclusive.
    :param granularity: ``day``, ``week`` or ``month``.
    :returns: The buckets, ascending by start date.
    :raises ValueError: If the granularity is not one of the three.
    """
    if granularity not in GRANULARITIES:
        msg = f"Unknown granularity: {granularity!r}"
        raise ValueError(msg)

    buckets: list[BucketSpan] = []
    if granularity == "day":
        cursor = from_
        while cursor <= to:
            buckets.append(BucketSpan(start=cursor, end=cursor, partial=False))
            cursor += timedelta(days=1)
        return buckets

    cursor = week_start(from_) if granularity == "week" else month_start(from_)
    while cursor <= to:
        end = cursor + timedelta(days=6) if granularity == "week" else month_end(cursor)
        buckets.append(BucketSpan(start=cursor, end=end, partial=cursor < from_ or end > to))
        cursor = end + timedelta(days=1)
    return buckets


def local_bounds(from_: date, to: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """Return the half-open UTC interval a hospital-local date range covers.

    The lower bound is local midnight at the start of ``from_``; the upper
    bound is local midnight at the start of the day after ``to``. Across a
    daylight-saving change the interval is 23 or 25 hours per affected day,
    which is the point: it is the local calendar day, not 24 hours.

    :param from_: First local date, inclusive.
    :param to: Last local date, inclusive.
    :param zone: The hospital's timezone.
    :returns: ``(lo, hi)`` as timezone-aware UTC datetimes; use ``>= lo`` and
        ``< hi``.
    """
    lo = datetime.combine(from_, time.min, tzinfo=zone)
    hi = datetime.combine(to + timedelta(days=1), time.min, tzinfo=zone)
    return lo.astimezone(UTC), hi.astimezone(UTC)
