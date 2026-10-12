"""The per-hospital booking policy a patient is held to (``docs/modules/15-patient-app.md`` §13.1, rule 7).

Read from ``hospitals.settings`` — the column meant for "feature flags, hours,
policies" — under flat keys, as the Patient App flag is. Each value is used
only when it is an integer within its bounds; anything else, including a
missing key, means the documented default. A hospital cannot switch the policy
off by storing nonsense in it.

Availability (Task 31) reads it to decide which slots are worth showing;
booking (Task 32) reads the same object to decide what may be booked, so the
two can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

__all__ = ["BookingPolicy"]

#: Settings keys, flat in the JSONB object.
HORIZON_KEY: Final = "patient_app.booking_horizon_days"
MIN_LEAD_KEY: Final = "patient_app.min_lead_minutes"
MAX_ACTIVE_KEY: Final = "patient_app.max_active_bookings"
CANCEL_CUTOFF_KEY: Final = "patient_app.cancel_cutoff_minutes"

#: Documented defaults and the bounds a stored value must lie within.
DEFAULT_HORIZON_DAYS: Final = 30
DEFAULT_MIN_LEAD_MINUTES: Final = 60
DEFAULT_MAX_ACTIVE_BOOKINGS: Final = 3
DEFAULT_CANCEL_CUTOFF_MINUTES: Final = 120
HORIZON_BOUNDS: Final = (1, 365)
MIN_LEAD_BOUNDS: Final = (0, 7 * 24 * 60)
MAX_ACTIVE_BOUNDS: Final = (1, 50)
CANCEL_CUTOFF_BOUNDS: Final = (0, 7 * 24 * 60)


@dataclass(frozen=True, slots=True)
class BookingPolicy:
    """What a hospital lets a patient book.

    :param horizon_days: The furthest bookable date, in days from today.
    :param min_lead_minutes: The earliest bookable start, in minutes from now.
    :param max_active_bookings: How many upcoming ``booked`` appointments one
        patient may hold at the hospital.
    :param cancel_cutoff_minutes: How long before its start a patient may
        still cancel an appointment themself.
    """

    horizon_days: int = DEFAULT_HORIZON_DAYS
    min_lead_minutes: int = DEFAULT_MIN_LEAD_MINUTES
    max_active_bookings: int = DEFAULT_MAX_ACTIVE_BOOKINGS
    cancel_cutoff_minutes: int = DEFAULT_CANCEL_CUTOFF_MINUTES

    @classmethod
    def from_settings(cls, settings: object) -> BookingPolicy:
        """The policy a stored settings value expresses.

        :param settings: The hospital's stored settings, whatever they hold.
        :returns: The policy, with the default for every value that is not an
            integer within bounds.
        """
        stored = settings if isinstance(settings, dict) else {}
        return cls(
            horizon_days=_bounded(stored.get(HORIZON_KEY), HORIZON_BOUNDS, DEFAULT_HORIZON_DAYS),
            min_lead_minutes=_bounded(
                stored.get(MIN_LEAD_KEY), MIN_LEAD_BOUNDS, DEFAULT_MIN_LEAD_MINUTES
            ),
            max_active_bookings=_bounded(
                stored.get(MAX_ACTIVE_KEY), MAX_ACTIVE_BOUNDS, DEFAULT_MAX_ACTIVE_BOOKINGS
            ),
            cancel_cutoff_minutes=_bounded(
                stored.get(CANCEL_CUTOFF_KEY), CANCEL_CUTOFF_BOUNDS, DEFAULT_CANCEL_CUTOFF_MINUTES
            ),
        )


def _bounded(value: Any, bounds: tuple[int, int], default: int) -> int:
    """``value`` if it is an integer within ``bounds``; ``default`` otherwise."""
    # ``bool`` is an ``int`` in Python; a flag is not a number of days.
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    low, high = bounds
    return value if low <= value <= high else default
