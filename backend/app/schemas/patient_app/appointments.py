"""DTOs for patient appointment booking (``docs/modules/15-patient-app.md`` §9.1, §13).

The request names a slot and nothing else: there is no patient, hospital or
doctor field to supply, and any field not listed here is refused. The response
is what the patient may see of their own appointment — never reception notes,
``created_by``, or anything of another patient.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models.appointment import AppointmentStatus  # noqa: TC001
from app.schemas.appointment import MAX_REASON_LENGTH

__all__ = [
    "IDEMPOTENCY_KEY_PATTERN",
    "MAX_CANCEL_TEXT",
    "AppointmentScope",
    "BookAppointment",
    "CancelAppointment",
    "CancelReason",
    "PatientAppointmentDetail",
    "PatientAppointment",
    "PatientAppointmentDoctor",
    "PatientAppointmentHospital",
    "PatientAppointmentType",
]

#: What a client may send as ``Idempotency-Key``: opaque, URL-safe, bounded.
IDEMPOTENCY_KEY_PATTERN: Final = r"^[A-Za-z0-9_-]{16,64}$"


class PatientAppointmentType(StrEnum):
    """The appointment types a patient may book. Never ``walk_in`` or ``emergency``."""

    NEW = "new"
    FOLLOW_UP = "follow_up"


class BookAppointment(BaseModel):
    """Body of ``POST /patient/hospitals/{hospital_ref}/doctors/{doctor_ref}/appointments``."""

    model_config = ConfigDict(extra="forbid")

    start: datetime = Field(description="Slot start, ISO 8601 with an offset.")
    end: datetime = Field(description="Slot end, ISO 8601 with an offset.")
    type: PatientAppointmentType = Field(default=PatientAppointmentType.NEW)
    reason: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_REASON_LENGTH)]
        | None
    ) = Field(default=None, description="Why the patient wants to be seen. Plain text.")

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        """A slot is an instant: a time without an offset names none."""
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "Must carry a UTC offset."
            raise ValueError(msg)
        return value

    @field_validator("reason")
    @classmethod
    def _storable(cls, value: str | None) -> str | None:
        """Blank is absent; the one character PostgreSQL text cannot hold is refused."""
        if value is not None and "\x00" in value:
            msg = "Contains a character that is not allowed."
            raise ValueError(msg)
        return value or None

    def same_request(self, start: datetime, end: datetime) -> bool:
        """Whether a stored booking is for the slot this request names."""
        return self.start == start and self.end == end


class PatientAppointmentHospital(BaseModel):
    """The hospital of an appointment."""

    ref: str
    name: str


class PatientAppointmentDoctor(BaseModel):
    """The doctor of an appointment."""

    ref: str
    name: str
    specialization: str


class PatientAppointment(BaseModel):
    """A patient's own appointment, as they may see it."""

    ref: str = Field(description="The appointment's reference.")
    status: AppointmentStatus
    type: str
    start: datetime = Field(description="Start, with the hospital's UTC offset.")
    end: datetime = Field(description="End, with the hospital's UTC offset.")
    timezone: str = Field(description="IANA timezone the times are expressed in.")
    hospital: PatientAppointmentHospital
    doctor: PatientAppointmentDoctor
    reason: str | None = Field(description="The patient's own reason, if they gave one.")

    @classmethod
    def build(cls, **fields: object) -> Self:
        """Construct from named fields (a seam for the service; no ORM access here)."""
        return cls.model_validate(fields)


class AppointmentScope(StrEnum):
    """Which of a patient's two appointment lists."""

    UPCOMING = "upcoming"
    PAST = "past"


class CancelReason(StrEnum):
    """Why a patient cancels: a fixed list (§14), so nothing free-form is required."""

    SCHEDULE_CONFLICT = "schedule_conflict"
    FEELING_BETTER = "feeling_better"
    BOOKED_BY_MISTAKE = "booked_by_mistake"
    OTHER = "other"


#: The longest free text a patient may add to a cancellation.
MAX_CANCEL_TEXT: Final = 200


class CancelAppointment(BaseModel):
    """Body of ``POST /patient/appointments/{appointment_ref}/cancel``."""

    model_config = ConfigDict(extra="forbid")

    reason_code: CancelReason
    reason_text: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_CANCEL_TEXT)] | None
    ) = Field(default=None, description="Optional detail, plain text.")

    @field_validator("reason_text")
    @classmethod
    def _storable(cls, value: str | None) -> str | None:
        """Blank is absent; the one character PostgreSQL text cannot hold is refused."""
        if value is not None and "\x00" in value:
            msg = "Contains a character that is not allowed."
            raise ValueError(msg)
        return value or None


class PatientAppointmentDetail(PatientAppointment):
    """A patient's own appointment, with what they may still do about it.

    ``can_cancel`` is the server's decision — status, ownership and the
    hospital's cut-off, as of this answer. A client shows a cancel action when
    it is true and never works it out for itself.
    """

    can_cancel: bool = Field(description="Whether the patient may cancel it themself right now.")
    cancel_until: datetime | None = Field(
        description="The last instant self-cancellation is allowed; null unless it is booked."
    )
