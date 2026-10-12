"""DTOs for hospital discovery (``docs/modules/15-patient-app.md`` §11, §27.6).

Reference data, and still an allow-list: a hospital is described to a patient
by the fields below and by nothing else. There is no internal id, no settings
or feature flag, no tax id, no e-mail, no audit column and no count of
anything. There is no distance either — the platform holds no location to
measure one from.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "FILTER_MAX_LENGTH",
    "MAX_PAGE",
    "MAX_PAGE_SIZE",
    "HospitalFilterText",
    "HospitalListing",
    "PatientHospital",
    "PatientHospitalAddress",
    "PatientHospitalCities",
]

#: The longest search text, and the longest city, a request may carry.
FILTER_MAX_LENGTH: Final = 80
#: Discovery pages are small, and the list cannot be walked without end.
DEFAULT_PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 50
MAX_PAGE: Final = 1000


def _storable(value: str) -> str:
    """Refuse the one character PostgreSQL text cannot hold, and so cannot match."""
    if "\x00" in value:
        msg = "Contains a character that is not allowed."
        raise ValueError(msg)
    return value


#: A search text or a city as a patient types it: trimmed, then bounded.
HospitalFilterText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=FILTER_MAX_LENGTH),
    AfterValidator(_storable),
]


class HospitalListing(StrEnum):
    """How a hospital comes to be in a list.

    ``promoted`` is reserved. Nothing in the data model can promote a
    hospital, so no response carries it today and every hospital is
    ``standard``. It is defined now so that, if promotion is ever built, a
    promoted hospital says so in this field — a client must label it, and the
    order of a list never changes without it.
    """

    STANDARD = "standard"
    PROMOTED = "promoted"


class PatientHospitalAddress(BaseModel):
    """Where a hospital is. Exactly these six fields, each text or absent."""

    line1: str | None
    line2: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None


class PatientHospital(BaseModel):
    """A hospital as a patient may see it."""

    ref: str = Field(description="The hospital's public reference: its code.")
    name: str = Field(description="The hospital's name.")
    address: PatientHospitalAddress
    phone: str | None = Field(description="Primary contact phone.")
    logo_url: str | None = Field(description="An absolute https:// URL of the logo, or null.")
    timezone: str = Field(description="IANA timezone.")
    linked: bool = Field(
        description="True when the signed-in account holds a link here that is honoured right now."
    )
    listing: HospitalListing = Field(
        description="Always 'standard' today. 'promoted' is reserved and never returned."
    )


class PatientHospitalCities(BaseModel):
    """``GET /patient/hospital-cities`` — the options of the city filter."""

    cities: list[str] = Field(description="Cities that have a listed hospital, in order.")
