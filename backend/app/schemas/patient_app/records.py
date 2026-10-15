"""DTOs for a patient's own prescriptions and lab results (§9.1, §15, §16).

An allow-list, like every other patient DTO: a record is described to the
patient by exactly the fields below and by nothing else. Nothing internal —
no live stock quantity, no batch, invoice or price, no technician notes or
doctor's notes (unresolved U-3: not decided, so not returned), no sample
identifier, no staff ids, no previous result values — and no lab order that
is anything but ``released``.

A prescription of every status is shown, labelled (§15). A result amended
after release shows only its current value with when it was amended (§16) —
never the previous one.
"""

from __future__ import annotations

from datetime import date, datetime  # noqa: TC003
from decimal import Decimal  # noqa: TC003

from pydantic import BaseModel, Field

from app.models.lab import LabOrderStatus, LabResultFlag  # noqa: TC001
from app.models.pharmacy import PrescriptionStatus  # noqa: TC001

__all__ = [
    "PatientLabItem",
    "PatientLabResult",
    "PatientPrescription",
    "PatientPrescriptionDoctor",
    "PatientRecordHospital",
    "PatientPrescriptionItem",
]


class PatientRecordHospital(BaseModel):
    """The hospital that wrote the prescription, or holds the lab result."""

    ref: str = Field(description="The hospital's public reference.")
    name: str


class PatientPrescriptionDoctor(BaseModel):
    """The prescriber, or the doctor who ordered tests. The display name is all
    of the doctor's user row a patient sees."""

    ref: str = Field(description="The doctor's reference.")
    name: str
    specialization: str


class PatientPrescriptionItem(BaseModel):
    """One medicine line as the patient may see it (§9.1)."""

    medicine_name: str = Field(description="Name as prescribed.")
    dosage: str
    frequency: str
    duration_days: int | None = Field(description="How many days to take it, or null.")
    instructions: str | None = Field(description="e.g. 'after food', or null.")
    quantity: int = Field(description="Units prescribed.")
    quantity_dispensed: int = Field(description="Units dispensed so far.")


class PatientPrescription(BaseModel):
    """A patient's own prescription (§15): read-only, every status, labelled."""

    ref: str = Field(description="The prescription's reference.")
    status: PrescriptionStatus = Field(
        description="active, partially_dispensed, dispensed or cancelled."
    )
    prescribed_at: datetime = Field(description="When it was written, with the hospital's offset.")
    timezone: str = Field(description="IANA timezone the time is expressed in.")
    hospital: PatientRecordHospital
    doctor: PatientPrescriptionDoctor
    items: list[PatientPrescriptionItem] = Field(
        description="The medicine lines, in display order.", min_length=1
    )


class PatientLabItem(BaseModel):
    """One test's released result as the patient may see it (§9.1)."""

    test_name: str = Field(description="Test as recorded when it was ordered.")
    value: str = Field(description="The result, numeric or text, as released (or amended).")
    unit: str | None = Field(description="Unit the result is reported in, or null.")
    flag: LabResultFlag | None = Field(
        description="normal, low, high or critical, as recorded; null when no range applied."
    )
    reference_low: Decimal | None = Field(
        description="Lower bound the flag was judged against, or null."
    )
    reference_high: Decimal | None = Field(
        description="Upper bound the flag was judged against, or null."
    )
    released_at: datetime = Field(description="When this result was released, hospital time.")
    amended_on: date | None = Field(
        description=(
            "The day the released result was last corrected, when it was. Only "
            "the current value is shown; never the previous one."
        )
    )


class PatientLabResult(BaseModel):
    """One released lab order of the patient (§16). Unreleased orders do not exist."""

    ref: str = Field(description="The lab order's reference.")
    status: LabOrderStatus = Field(description="Always 'released'.")
    released_at: datetime = Field(description="When the order was released, hospital time.")
    timezone: str = Field(description="IANA timezone the times are expressed in.")
    hospital: PatientRecordHospital
    doctor: PatientPrescriptionDoctor = Field(description="The doctor who ordered the tests.")
    items: list[PatientLabItem] = Field(
        description="The released results, in display order.", min_length=1
    )
