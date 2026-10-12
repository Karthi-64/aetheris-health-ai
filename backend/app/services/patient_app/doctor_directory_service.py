"""Doctor discovery: which doctors of a hospital a patient can find, and what they are told.

``docs/modules/15-patient-app.md`` §9.1 and §12. Reference data, as hospital
discovery is: no record link is needed and nothing is audited, but it is
answered to a signed-in patient only, once the required policies are accepted.

**Which hospital.** The one the path names, resolved by
:class:`~app.services.patient_app.hospital_gate.PatientHospitalGate` exactly as
hospital discovery resolves it. A hospital that hospital discovery would not
show has no doctors here.

**Which doctors.** Those of that hospital that the directory query returns:
not deactivated, backed by an active user, with at least one availability
window. Every read is made inside that hospital's tenant scope and names the
hospital, so a doctor of another hospital is simply not there.

**One refusal.** An unknown or closed hospital, an unknown, hidden or foreign
doctor, and a reference that is not one at all are the same ``404``.
"""

from __future__ import annotations

import re
import uuid
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

from app.core.exceptions import NotFoundError
from app.core.tenancy import TenantScope, tenant_scope
from app.schemas.common import Page
from app.schemas.patient_app.doctors import (
    PatientDepartment,
    PatientDepartments,
    PatientDoctor,
    PatientDoctorDepartment,
    PatientDoctorQualification,
)

if TYPE_CHECKING:
    from app.models.patient_account import PatientAccount
    from app.repositories.doctor_repository import DoctorDirectoryEntry, DoctorRepository
    from app.repositories.hospital_repository import HospitalSummary
    from app.services.patient_app.consent_service import ConsentService
    from app.services.patient_app.hospital_gate import PatientHospitalGate

__all__ = ["MAX_DEPARTMENTS", "HospitalDoctorDirectoryService"]

#: The most departments the filter is ever offered.
MAX_DEPARTMENTS: Final = 200

#: The one message for everything that cannot be shown — the framework's own
#: answer for a path that matches no route, as hospital discovery uses.
_NOT_FOUND: Final = HTTPStatus.NOT_FOUND.phrase

#: A doctor reference exactly as ``str(uuid)`` writes it.
_UUID_TEXT: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

#: Bounds on what is shown of stored free-form values.
_TEXT_MAX: Final = 200
_BIO_MAX: Final = 4000
_LIST_MAX: Final = 20


class HospitalDoctorDirectoryService:
    """Lists and describes the doctors of a hospital that is open to patients.

    :param gate: Which hospitals are open to patients.
    :param doctors: Doctor data access.
    :param consent: The policy gate.
    """

    def __init__(
        self,
        gate: PatientHospitalGate,
        doctors: DoctorRepository,
        consent: ConsentService,
    ) -> None:
        self._gate = gate
        self._doctors = doctors
        self._consent = consent

    async def discover(
        self,
        account: PatientAccount,
        hospital_ref: str,
        *,
        search: str | None,
        department: uuid.UUID | None,
        page: int,
        page_size: int,
    ) -> Page[PatientDoctor]:
        """One page of a hospital's doctors, in name order.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's code or id, from the path.
        :param search: Text the name or specialization must contain. Blank is absent.
        :param department: The department the doctors must be in, or ``None``.
        :param page: 1-based page number.
        :param page_size: Doctors per page.
        :returns: The page — empty beyond the last one — and the total.
        :raises ConsentRequiredError: If a required policy is pending.
        :raises NotFoundError: If the hospital cannot be shown.
        """
        hospital = await self._open_hospital(account, hospital_ref)
        with tenant_scope(TenantScope.hospital(hospital.id)):
            entries, total = await self._doctors.list_directory(
                hospital.id,
                search=(search or "").strip() or None,
                department_id=department,
                skip=(page - 1) * page_size,
                limit=page_size,
            )
        return Page[PatientDoctor](
            items=[_view(entry) for entry in entries],
            page=page,
            page_size=page_size,
            total_records=total,
        )

    async def get_doctor(
        self, account: PatientAccount, hospital_ref: str, doctor_ref: str
    ) -> PatientDoctor:
        """Describe one doctor of a hospital.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's code or id, from the path.
        :param doctor_ref: The doctor's reference, from the path.
        :returns: The doctor.
        :raises ConsentRequiredError: If a required policy is pending.
        :raises NotFoundError: If the hospital or the doctor cannot be shown,
            whatever the reason.
        """
        hospital = await self._open_hospital(account, hospital_ref)
        reference = doctor_ref.strip().lower()
        if not _UUID_TEXT.fullmatch(reference):
            raise NotFoundError(_NOT_FOUND)
        with tenant_scope(TenantScope.hospital(hospital.id)):
            entry = await self._doctors.get_directory_entry(hospital.id, uuid.UUID(reference))
        if entry is None:
            raise NotFoundError(_NOT_FOUND)
        return _view(entry)

    async def list_departments(
        self, account: PatientAccount, hospital_ref: str
    ) -> PatientDepartments:
        """The departments a patient can filter a hospital's doctors by.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's code or id, from the path.
        :returns: The departments that have a listed doctor.
        :raises ConsentRequiredError: If a required policy is pending.
        :raises NotFoundError: If the hospital cannot be shown.
        """
        hospital = await self._open_hospital(account, hospital_ref)
        with tenant_scope(TenantScope.hospital(hospital.id)):
            departments = await self._doctors.list_directory_departments(
                hospital.id, limit=MAX_DEPARTMENTS
            )
        return PatientDepartments(
            departments=[
                PatientDepartment(
                    ref=str(department.id),
                    name=_text(department.name) or "",
                    description=_text(department.description),
                )
                for department in departments
            ]
        )

    async def _open_hospital(self, account: PatientAccount, hospital_ref: str) -> HospitalSummary:
        """The hospital the path names, if hospital discovery would show it.

        The policy gate comes first, so a pending policy is refused the same
        way whatever the path names.
        """
        await self._consent.ensure_policies_accepted(account.id)
        hospital = await self._gate.resolve(hospital_ref)
        if hospital is None or self._gate.public_ref(hospital) is None:
            raise NotFoundError(_NOT_FOUND)
        return hospital


def _view(entry: DoctorDirectoryEntry) -> PatientDoctor:
    """A doctor as a patient may see one. Every field is set here, by name."""
    department = None
    if entry.department_id is not None and entry.department_name is not None:
        department = PatientDoctorDepartment(
            ref=str(entry.department_id), name=_text(entry.department_name) or ""
        )
    return PatientDoctor(
        ref=str(entry.id),
        name=" ".join(part for part in (_text(entry.first_name), _text(entry.last_name)) if part),
        specialization=_text(entry.specialization) or "",
        department=department,
        qualifications=_qualifications(entry.qualifications),
        languages=_languages(entry.languages),
        bio=_text(entry.bio, limit=_BIO_MAX),
    )


def _text(stored: object, *, limit: int = _TEXT_MAX) -> str | None:
    """A stored value as text to show: trimmed and bounded, or nothing if it is not text."""
    if not isinstance(stored, str):
        return None
    return stored.strip()[:limit].rstrip() or None


def _qualifications(stored: object) -> list[PatientDoctorQualification]:
    """The stored qualifications, read through an allow-list of three keys.

    An entry without a degree is not a qualification and is left out; any
    other key an entry carries is not passed on.
    """
    if not isinstance(stored, list):
        return []
    shown: list[PatientDoctorQualification] = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        degree = _text(item.get("degree"))
        if degree is None:
            continue
        year = item.get("year")
        shown.append(
            PatientDoctorQualification(
                degree=degree,
                institution=_text(item.get("institution")),
                # ``bool`` is an ``int`` in Python; it is not a year.
                year=year if isinstance(year, int) and not isinstance(year, bool) else None,
            )
        )
        if len(shown) == _LIST_MAX:
            break
    return shown


def _languages(stored: object) -> list[str]:
    """The stored languages that are text, trimmed, each once, in stored order."""
    if not isinstance(stored, list):
        return []
    shown: list[str] = []
    for item in stored:
        language = _text(item)
        if language is not None and language not in shown:
            shown.append(language)
        if len(shown) == _LIST_MAX:
            break
    return shown
