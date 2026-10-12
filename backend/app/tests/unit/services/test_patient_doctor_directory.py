"""Unit tests for :class:`HospitalDoctorDirectoryService` — mocked gate and repository.

What is under test: the order of the checks, that every read names the
resolved hospital inside its tenant scope, the one refusal, and what the
allow-list lets through of stored free-form values.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError
from app.core.feature_flags import PATIENT_APP_ENABLED
from app.core.tenancy import current_tenant_scope
from app.repositories.doctor_repository import DirectoryDepartment, DoctorDirectoryEntry
from app.repositories.hospital_repository import HospitalSummary
from app.services.patient_app.doctor_directory_service import (
    MAX_DEPARTMENTS,
    HospitalDoctorDirectoryService,
)
from app.services.patient_app.errors import ConsentRequiredError
from app.services.patient_app.hospital_gate import PatientHospitalGate

ACCOUNT: Any = SimpleNamespace(id=uuid.uuid4())


def _hospital(slug: str = "city-care") -> HospitalSummary:
    return HospitalSummary(
        id=uuid.uuid4(), name="City Care", slug=slug, settings={PATIENT_APP_ENABLED: True}
    )


def _entry(**overrides: Any) -> DoctorDirectoryEntry:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "first_name": "Asha",
        "last_name": "Menon",
        "specialization": "Cardiology",
        "qualifications": [],
        "languages": [],
        "bio": None,
        "department_id": None,
        "department_name": None,
    }
    values.update(overrides)
    return DoctorDirectoryEntry(**values)


class _Directory:
    """The service over mocks, recording the tenant scope each read ran under."""

    def __init__(self, hospital: HospitalSummary | None) -> None:
        self.hospital = hospital
        self.scopes: list[Any] = []
        self.gate = MagicMock(spec=PatientHospitalGate)
        self.gate.resolve = AsyncMock(return_value=hospital)
        self.gate.public_ref = PatientHospitalGate.public_ref
        self.doctors = AsyncMock()
        self.consent = AsyncMock()
        self.service = HospitalDoctorDirectoryService(self.gate, self.doctors, self.consent)

    def returning(self, name: str, value: Any) -> None:
        async def _read(*_args: Any, **_kwargs: Any) -> Any:
            self.scopes.append(current_tenant_scope())
            return value

        getattr(self.doctors, name).side_effect = _read


class TestOrderOfChecks:
    async def test_a_pending_policy_is_refused_before_anything_is_looked_up(self) -> None:
        directory = _Directory(_hospital())
        directory.consent.ensure_policies_accepted.side_effect = ConsentRequiredError

        with pytest.raises(ConsentRequiredError):
            await directory.service.get_doctor(ACCOUNT, "city-care", str(uuid.uuid4()))
        with pytest.raises(ConsentRequiredError):
            await directory.service.discover(
                ACCOUNT, "city-care", search=None, department=None, page=1, page_size=20
            )
        with pytest.raises(ConsentRequiredError):
            await directory.service.list_departments(ACCOUNT, "city-care")

        directory.gate.resolve.assert_not_awaited()
        assert directory.doctors.mock_calls == []

    @pytest.mark.parametrize(
        "hospital", [None, _hospital("Upper-Case"), _hospital(str(uuid.uuid4()))]
    )
    async def test_a_hospital_that_is_not_shown_reads_no_doctor(
        self, hospital: HospitalSummary | None
    ) -> None:
        directory = _Directory(hospital)

        for call in (
            directory.service.discover(
                ACCOUNT, "x", search=None, department=None, page=1, page_size=20
            ),
            directory.service.get_doctor(ACCOUNT, "x", str(uuid.uuid4())),
            directory.service.list_departments(ACCOUNT, "x"),
        ):
            with pytest.raises(NotFoundError) as refusal:
                await call
            assert refusal.value.message == "Not Found"
        assert directory.doctors.mock_calls == []

    @pytest.mark.parametrize(
        "reference",
        ["", "x", "' OR 1=1", "../..", "a" * 4000, uuid.uuid4().hex, "{" + str(uuid.uuid4()) + "}"],
    )
    async def test_a_doctor_reference_that_is_not_one_reads_nothing(self, reference: str) -> None:
        directory = _Directory(_hospital())

        with pytest.raises(NotFoundError) as refusal:
            await directory.service.get_doctor(ACCOUNT, "city-care", reference)

        assert refusal.value.message == "Not Found"
        assert directory.doctors.mock_calls == []

    async def test_an_unknown_doctor_is_the_same_refusal(self) -> None:
        directory = _Directory(_hospital())
        directory.returning("get_directory_entry", None)

        with pytest.raises(NotFoundError) as refusal:
            await directory.service.get_doctor(ACCOUNT, "city-care", str(uuid.uuid4()))

        assert refusal.value.message == "Not Found"


class TestEveryReadNamesTheResolvedHospital:
    async def test_the_list(self) -> None:
        hospital = _hospital()
        directory = _Directory(hospital)
        entries = [_entry(), _entry()]
        directory.returning("list_directory", (entries, 41))
        department = uuid.uuid4()

        page = await directory.service.discover(
            ACCOUNT, "city-care", search="  heart ", department=department, page=3, page_size=10
        )

        assert (page.page, page.page_size, page.total_records) == (3, 10, 41)
        assert [doctor.ref for doctor in page.items] == [str(entry.id) for entry in entries]
        call = directory.doctors.list_directory.await_args
        assert call.args == (hospital.id,)
        assert call.kwargs == {
            "search": "heart",
            "department_id": department,
            "skip": 20,
            "limit": 10,
        }
        assert [scope.hospital_id for scope in directory.scopes] == [hospital.id]
        assert current_tenant_scope() is None

    async def test_a_blank_search_is_no_search(self) -> None:
        directory = _Directory(_hospital())
        directory.returning("list_directory", ([], 0))

        await directory.service.discover(
            ACCOUNT, "city-care", search="   ", department=None, page=1, page_size=20
        )

        assert directory.doctors.list_directory.await_args.kwargs["search"] is None

    async def test_one_doctor(self) -> None:
        hospital = _hospital()
        directory = _Directory(hospital)
        entry = _entry()
        directory.returning("get_directory_entry", entry)

        doctor = await directory.service.get_doctor(
            ACCOUNT, "city-care", f"  {str(entry.id).upper()} "
        )

        assert doctor.ref == str(entry.id)
        assert directory.doctors.get_directory_entry.await_args.args == (hospital.id, entry.id)
        assert [scope.hospital_id for scope in directory.scopes] == [hospital.id]

    async def test_the_departments(self) -> None:
        hospital = _hospital()
        directory = _Directory(hospital)
        department = DirectoryDepartment(id=uuid.uuid4(), name=" Cardiology ", description=None)
        directory.returning("list_directory_departments", [department])

        result = await directory.service.list_departments(ACCOUNT, "city-care")

        assert result.model_dump() == {
            "departments": [{"ref": str(department.id), "name": "Cardiology", "description": None}]
        }
        call = directory.doctors.list_directory_departments.await_args
        assert (call.args, call.kwargs) == ((hospital.id,), {"limit": MAX_DEPARTMENTS})
        assert [scope.hospital_id for scope in directory.scopes] == [hospital.id]


class TestWhatIsTold:
    async def _describe(self, **stored: Any) -> dict[str, Any]:
        directory = _Directory(_hospital())
        entry = _entry(**stored)
        directory.returning("get_directory_entry", entry)
        doctor = await directory.service.get_doctor(ACCOUNT, "city-care", str(entry.id))
        return doctor.model_dump()

    async def test_exactly_the_public_fields(self) -> None:
        department = uuid.uuid4()
        told = await self._describe(
            first_name="  Asha ",
            last_name="Menon",
            specialization=" Cardiology ",
            bio=" Sees adults. ",
            department_id=department,
            department_name="Heart",
        )

        assert set(told) == {
            "ref",
            "name",
            "specialization",
            "department",
            "qualifications",
            "languages",
            "bio",
        }
        assert (told["name"], told["specialization"], told["bio"]) == (
            "Asha Menon",
            "Cardiology",
            "Sees adults.",
        )
        assert told["department"] == {"ref": str(department), "name": "Heart"}

    @pytest.mark.parametrize("stored", [None, "MBBS", {"degree": "MBBS"}, 7, [None, 3, "MBBS", []]])
    async def test_qualifications_that_are_not_a_list_of_objects_are_none(
        self, stored: Any
    ) -> None:
        assert (await self._describe(qualifications=stored))["qualifications"] == []

    async def test_a_qualification_is_three_keys_and_needs_a_degree(self) -> None:
        told = await self._describe(
            qualifications=[
                {"degree": " MBBS ", "institution": " Osmania ", "year": 2011, "grade": "A"},
                {"degree": "MD", "institution": 5, "year": "2015"},
                {"degree": "DM", "year": True},
                {"degree": "   ", "institution": "Nowhere"},
                {"institution": "No degree"},
                {"degree": 9},
            ]
        )

        assert told["qualifications"] == [
            {"degree": "MBBS", "institution": "Osmania", "year": 2011},
            {"degree": "MD", "institution": None, "year": None},
            {"degree": "DM", "institution": None, "year": None},
        ]

    async def test_lists_and_texts_are_bounded(self) -> None:
        told = await self._describe(
            qualifications=[{"degree": f"D{index}"} for index in range(50)],
            languages=[f"L{index}" for index in range(50)],
            bio="b" * 9000,
            specialization="s" * 500,
        )

        assert len(told["qualifications"]) == 20
        assert len(told["languages"]) == 20
        assert len(told["bio"]) == 4000
        assert len(told["specialization"]) == 200

    async def test_languages_are_text_trimmed_and_listed_once(self) -> None:
        told = await self._describe(
            languages=[" English ", "English", "", 4, None, ["x"], "Telugu"]
        )

        assert told["languages"] == ["English", "Telugu"]
        assert (await self._describe(languages="English"))["languages"] == []

    async def test_a_department_without_a_name_is_no_department(self) -> None:
        told = await self._describe(department_id=uuid.uuid4(), department_name=None)

        assert told["department"] is None
