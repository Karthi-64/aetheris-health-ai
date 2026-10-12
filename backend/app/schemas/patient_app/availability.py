"""DTOs for doctor availability (``docs/modules/15-patient-app.md`` §9.1, §13).

A patient is told when a doctor can be booked and nothing else about the
doctor's day: no booked or on-leave slots, no appointment ids, no counts, no
reasons. A slot is its start and its end, in the hospital's timezone — that
pair is what booking takes, so no slot id exists.
"""

from __future__ import annotations

import re
from datetime import date as DateType  # noqa: TC003, N812 — `date` is also a field name below
from datetime import datetime  # noqa: TC003
from typing import Annotated, Final

from pydantic import BaseModel, BeforeValidator, Field

__all__ = [
    "DEFAULT_RANGE_DAYS",
    "IsoDate",
    "MAX_RANGE_DAYS",
    "PatientAvailabilityDay",
    "PatientDoctorAvailability",
    "PatientSlot",
]

#: The range answered when none is asked for, and the most that may be asked for.
DEFAULT_RANGE_DAYS: Final = 7
MAX_RANGE_DAYS: Final = 14


_DATE_TEXT: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _calendar_date(value: object) -> object:
    """Accept a date only in the one form the API documents, ``YYYY-MM-DD``."""
    if isinstance(value, str) and not _DATE_TEXT.fullmatch(value):
        msg = "Use the form YYYY-MM-DD."
        raise ValueError(msg)
    return value


#: A calendar date as a query parameter: exactly ``YYYY-MM-DD``, nothing looser.
IsoDate = Annotated[DateType, BeforeValidator(_calendar_date)]


class PatientSlot(BaseModel):
    """One bookable slot. Exactly its bounds."""

    start: datetime = Field(description="Slot start, with the hospital's UTC offset.")
    end: datetime = Field(description="Slot end, with the hospital's UTC offset.")


class PatientAvailabilityDay(BaseModel):
    """One hospital-local day of the requested range."""

    date: DateType
    slots: list[PatientSlot] = Field(
        description="The bookable slots of the day, in order. Empty when there are none."
    )


class PatientDoctorAvailability(BaseModel):
    """``GET /patient/hospitals/{hospital_ref}/doctors/{doctor_ref}/availability``."""

    timezone: str = Field(description="IANA timezone every date and time here is expressed in.")
    today: DateType = Field(description="Today's date at the hospital.")
    horizon_end: DateType = Field(description="The last date a patient may book.")
    min_lead_minutes: int = Field(description="A slot must start at least this long from now.")
    start_date: DateType = Field(description="First date of the range answered.")
    end_date: DateType = Field(description="Last date of the range answered.")
    days: list[PatientAvailabilityDay] = Field(
        description="Every date of the range, in order, with its bookable slots."
    )
