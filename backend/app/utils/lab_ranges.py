"""Reference-range selection and abnormal flagging for lab results.

Pure functions, no I/O (``docs/modules/07-laboratory.md`` business rule 2,
FR-3). Kept apart from the service so the rule that decides whether a result
is abnormal can be tested exhaustively on its own.

A test's ``reference_ranges`` is a list of entries::

    {"sex": "female", "age_min": 18, "age_max": 120,
     "low": "12.0", "high": "15.5", "critical_low": "7.0", "critical_high": "20.0"}

``sex`` is ``male``, ``female`` or ``any``. Ages are whole years, inclusive at
both ends; a missing bound is open. ``low`` / ``high`` may each be absent for a
one-sided range, and the critical bounds are optional.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from app.models.lab import LabResultFlag

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

__all__ = [
    "ANY_SEX",
    "ReferenceRange",
    "age_in_years",
    "flag_value",
    "parse_numeric",
    "select_range",
]

#: The ``sex`` value of a range that applies to everyone.
ANY_SEX = "any"


@dataclass(frozen=True, slots=True)
class ReferenceRange:
    """The bounds one result is judged against.

    :param low: Below this is ``low``. ``None`` for no lower bound.
    :param high: Above this is ``high``. ``None`` for no upper bound.
    :param critical_low: Below this is ``critical``.
    :param critical_high: Above this is ``critical``.
    """

    low: Decimal | None = None
    high: Decimal | None = None
    critical_low: Decimal | None = None
    critical_high: Decimal | None = None


def parse_numeric(value: object) -> Decimal | None:
    """Read a finite decimal out of a stored or submitted value.

    :param value: A number, a numeric string, or anything else.
    :returns: The decimal, or ``None`` if it is not a finite number. Booleans
        are not numbers here, although Python would accept them.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def age_in_years(date_of_birth: date, on: date) -> int:
    """Return a person's age in completed years on a given day.

    :param date_of_birth: Their date of birth.
    :param on: The day to measure at — the sample collection date (§14).
    :returns: Completed years, never negative.
    """
    years = on.year - date_of_birth.year
    if (on.month, on.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return max(years, 0)


def _age_matches(entry: dict[str, Any], age: int) -> bool:
    """Whether an age falls inside an entry's inclusive, possibly open, bounds."""
    age_min = parse_numeric(entry.get("age_min"))
    age_max = parse_numeric(entry.get("age_max"))
    return (age_min is None or age >= age_min) and (age_max is None or age <= age_max)


def select_range(
    ranges: Sequence[dict[str, Any]] | None, *, sex: str, age: int
) -> ReferenceRange | None:
    """Pick the reference range that applies to a patient (FR-1).

    A range written for the patient's sex wins over one for ``any``; within
    each, the first entry whose age bounds fit is used. A patient whose sex is
    recorded as neither male nor female is matched only against ``any`` ranges
    — guessing a sex-specific range would be worse than reporting none.

    :param ranges: The test's ``reference_ranges``.
    :param sex: The patient's recorded sex.
    :param age: Their age in years at sample collection.
    :returns: The applicable bounds, or ``None`` if no entry fits.
    """
    candidates = [entry for entry in ranges or [] if isinstance(entry, dict)]
    for wanted in (sex, ANY_SEX):
        for entry in candidates:
            if str(entry.get("sex") or ANY_SEX).lower() != wanted:
                continue
            if _age_matches(entry, age):
                return ReferenceRange(
                    low=parse_numeric(entry.get("low")),
                    high=parse_numeric(entry.get("high")),
                    critical_low=parse_numeric(entry.get("critical_low")),
                    critical_high=parse_numeric(entry.get("critical_high")),
                )
    return None


def flag_value(value: Decimal, bounds: ReferenceRange | None) -> LabResultFlag | None:
    """Judge a numeric result against its reference range (FR-3).

    The bounds themselves are in range: a value equal to ``low`` or ``high``
    is ``normal``, and one equal to a critical bound is not yet ``critical``.

    :param value: The result.
    :param bounds: The applicable range, or ``None``.
    :returns: The flag, or ``None`` when there is no range to judge against —
        "no range" is reported as such rather than passed off as normal.
    """
    if bounds is None:
        return None
    if bounds.critical_low is not None and value < bounds.critical_low:
        return LabResultFlag.CRITICAL
    if bounds.critical_high is not None and value > bounds.critical_high:
        return LabResultFlag.CRITICAL
    if bounds.low is not None and value < bounds.low:
        return LabResultFlag.LOW
    if bounds.high is not None and value > bounds.high:
        return LabResultFlag.HIGH
    return LabResultFlag.NORMAL
