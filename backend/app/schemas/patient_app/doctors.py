"""DTOs for doctor discovery (``docs/modules/15-patient-app.md`` §9.1, §12).

An allow-list: a doctor is described to a patient by the fields below and by
nothing else. There is no user id, e-mail, phone, licence number or audit
column, and no consultation fee (§9.1, U-3). There is no rating, review,
experience, availability or distance either — the platform holds none.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, Field

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE",
    "MAX_PAGE_SIZE",
    "PatientDepartment",
    "PatientDepartments",
    "PatientDoctor",
    "PatientDoctorDepartment",
    "PatientDoctorQualification",
]

#: Discovery pages are small, and the list cannot be walked without end.
DEFAULT_PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 50
MAX_PAGE: Final = 1000


class PatientDoctorQualification(BaseModel):
    """One qualification. Exactly these three fields."""

    degree: str
    institution: str | None
    year: int | None


class PatientDoctorDepartment(BaseModel):
    """The department a doctor works in."""

    ref: str = Field(description="The department's reference, as the department filter takes it.")
    name: str


class PatientDoctor(BaseModel):
    """A doctor as a patient may see one."""

    ref: str = Field(description="The doctor's public reference within the hospital.")
    name: str = Field(description="The doctor's name, as stored.")
    specialization: str
    department: PatientDoctorDepartment | None = Field(
        description="The doctor's department, or null if none is assigned."
    )
    qualifications: list[PatientDoctorQualification]
    languages: list[str]
    bio: str | None


class PatientDepartment(BaseModel):
    """A department that has a listed doctor: an option of the department filter."""

    ref: str
    name: str
    description: str | None


class PatientDepartments(BaseModel):
    """``GET /patient/hospitals/{hospital_ref}/departments``."""

    departments: list[PatientDepartment] = Field(
        description="Departments that have a listed doctor, by name."
    )
