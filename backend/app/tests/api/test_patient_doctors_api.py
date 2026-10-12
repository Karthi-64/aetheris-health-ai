"""API and adversarial tests for Patient App doctor discovery (Task 30).

``docs/modules/15-patient-app.md`` §9.1 and §12. Over HTTP, through the real
application and a real PostgreSQL. What is under test: which doctors of which
hospital a patient can see, that nothing but the public fields is told, and
that every refusal is one and the same.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from fastapi import Depends
from sqlalchemy import func, select, update

from app.api.dependencies.patient import get_consent_service
from app.api.dependencies.repositories import (
    get_patient_account_link_repository,
    get_patient_consent_repository,
)
from app.api.dependencies.services import get_audit_sink, get_unit_of_work
from app.core.audit import AuditSink  # noqa: TC001 — FastAPI resolves the override at runtime
from app.core.config import settings
from app.core.security import create_access_token
from app.database.unit_of_work import UnitOfWork  # noqa: TC001 — as above
from app.models.audit_log import AuditLog
from app.models.department import Department
from app.models.doctor import Doctor, DoctorAvailability
from app.models.hospital import Hospital
from app.models.patient_consent import ConsentPurpose
from app.models.user import User
from app.repositories import (  # noqa: TC001 — as above
    PatientAccountLinkRepository,
    PatientConsentRepository,
)
from app.services.patient_app.consent_service import ConsentService
from app.services.patient_app.policies import Policy
from app.tests.patient_app_helpers import (
    PATIENT,
    FakeSmsSender,
    bearer,
    build_patient_application,
    insert_department,
    insert_doctor,
    insert_hospital,
    new_phone,
    open_hospital,
    patient_client,
    sign_in,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

HOSPITALS = f"{PATIENT}/hospitals"
DOCTOR_FIELDS = {
    "ref",
    "name",
    "specialization",
    "department",
    "qualifications",
    "languages",
    "bio",
}
NOT_FOUND = (404, "RESOURCE_NOT_FOUND", "Not Found")
#: Words that must never reach a patient, whatever the endpoint.
INTERNAL = [
    "staff-secret.test",
    "LICCANARY",
    "license",
    "consultation_fee",
    "user_id",
    "hospital_id",
    "password",
    "email",
    "phone",
    "created_",
    "updated_",
    "deleted_",
    "FEECANARY",
    "+919000000001",
]


@pytest.fixture(autouse=True)
def _patient_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_HOSPITAL_PER_MIN", 1_000_000)


@pytest.fixture
def sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def tag() -> str:
    """A word no other doctor's name or specialization contains."""
    return f"q{uuid.uuid4().hex[:11]}"


@pytest_asyncio.fixture
async def application(db_session: AsyncSession, sms: FakeSmsSender) -> AsyncGenerator[FastAPI]:
    app = build_patient_application(db_session, sms)
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def browser(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with patient_client(application) as client:
        yield client


class _Patient:
    """A signed-in patient."""

    def __init__(self, client: AsyncClient, session: dict[str, Any]) -> None:
        self.client = client
        self.account_id = session["account"]["id"]
        self.headers = bearer(session["access_token"])

    async def doctors(self, hospital_ref: object, **params: Any) -> Response:
        return await self.client.get(
            f"{HOSPITALS}/{hospital_ref}/doctors", params=params, headers=self.headers
        )

    async def refs(self, hospital_ref: object, **params: Any) -> list[str]:
        response = await self.doctors(hospital_ref, **{"page_size": 50, **params})
        assert response.status_code == 200, response.text
        return [doctor["ref"] for doctor in response.json()["data"]]

    async def doctor(self, hospital_ref: object, doctor_ref: object) -> Response:
        return await self.client.get(
            f"{HOSPITALS}/{hospital_ref}/doctors/{doctor_ref}", headers=self.headers
        )

    async def departments(self, hospital_ref: object) -> Response:
        return await self.client.get(
            f"{HOSPITALS}/{hospital_ref}/departments", headers=self.headers
        )


@pytest_asyncio.fixture
async def patient(browser: AsyncClient, sms: FakeSmsSender) -> _Patient:
    return _Patient(browser, await sign_in(browser, sms, new_phone()))


@pytest_asyncio.fixture
async def hospital(db_session: AsyncSession, tag: str) -> Hospital:
    """Hospital A, open to patients."""
    return await insert_hospital(db_session, name=f"{tag} General")


@pytest_asyncio.fixture
async def other(db_session: AsyncSession, tag: str) -> Hospital:
    """Hospital B, open to patients."""
    return await insert_hospital(db_session, name=f"{tag} Other")


def _error(response: Response) -> tuple[int, str, str]:
    body = response.json()
    return response.status_code, body["error_code"], body["message"]


def _refusal(response: Response) -> str:
    """A refusal with the per-request id taken out."""
    body = {k: v for k, v in response.json().items() if "request" not in k and k != "metadata"}
    return json.dumps([response.status_code, body], sort_keys=True)


# ── The list ─────────────────────────────────────────────────────────────────


class TestDiscoverDoctors:
    async def test_a_doctor_is_described_by_the_public_fields_and_nothing_else(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        department = await insert_department(
            db_session,
            hospital.id,
            name="Cardiology",
            description="Heart care",
            email="dept@staff-secret.test",
            phone_extension="4411",
        )
        doctor = await insert_doctor(
            db_session,
            hospital.id,
            first_name="Asha",
            last_name="Menon",
            department_id=department.id,
            bio="  Interventional cardiologist.\nSees adults.  ",
            languages=["English", " Telugu ", "English", 7, None],
            qualifications=[
                {"degree": "MBBS", "institution": "Osmania", "year": 2011, "score": "FEECANARY"},
                {"degree": " MD ", "institution": None, "year": "2015"},
                {"institution": "No degree"},
                "FEECANARY",
            ],
            consultation_fee="812.00",
        )

        listed = await patient.doctors(hospital.slug)
        detail = await patient.doctor(hospital.slug, doctor.id)

        assert listed.status_code == 200, listed.text
        assert set(listed.json()) >= {"success", "message", "data", "metadata"}
        [item] = listed.json()["data"]
        assert item == detail.json()["data"]
        assert set(item) == DOCTOR_FIELDS
        assert item == {
            "ref": str(doctor.id),
            "name": "Asha Menon",
            "specialization": "Cardiology",
            "department": {"ref": str(department.id), "name": "Cardiology"},
            "qualifications": [
                {"degree": "MBBS", "institution": "Osmania", "year": 2011},
                {"degree": "MD", "institution": None, "year": None},
            ],
            "languages": ["English", "Telugu"],
            "bio": "Interventional cardiologist.\nSees adults.",
        }
        for raw in (listed.text, detail.text):
            assert not [word for word in INTERNAL if word in raw]
            assert "812" not in raw
            assert str(doctor.user_id) not in raw
            assert str(hospital.id) not in raw

    async def test_missing_details_are_empty_not_absent(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_doctor(db_session, hospital.id, qualifications={"degree": "x"}, languages="en")

        [item] = (await patient.doctors(hospital.slug)).json()["data"]

        assert (item["department"], item["qualifications"], item["languages"], item["bio"]) == (
            None,
            [],
            [],
            None,
        )

    @pytest.mark.parametrize(
        "hidden",
        [
            {"deleted": True},
            {"available": False},
            {"user_status": "suspended"},
            {"user_deleted": True},
        ],
    )
    async def test_a_doctor_who_must_not_be_shown_is_in_no_list_and_has_no_profile(
        self,
        patient: _Patient,
        hospital: Hospital,
        db_session: AsyncSession,
        hidden: dict[str, Any],
    ) -> None:
        """Attack: read a deactivated, suspended or unscheduled doctor by guessing the id."""
        department = await insert_department(db_session, hospital.id, name="Hidden Dept")
        shown = await insert_doctor(db_session, hospital.id, last_name="Shown")
        doctor = await insert_doctor(
            db_session, hospital.id, last_name="Hidden", department_id=department.id, **hidden
        )
        unknown = await patient.doctor(hospital.slug, uuid.uuid4())

        listed = await patient.doctors(hospital.slug, search="Hidden")
        profile = await patient.doctor(hospital.slug, doctor.id)

        assert await patient.refs(hospital.slug) == [str(shown.id)]
        assert listed.json()["data"] == []
        assert listed.json()["metadata"]["pagination"]["total_records"] == 0
        assert _error(profile) == NOT_FOUND
        assert _refusal(profile) == _refusal(unknown)
        # A department with only hidden doctors is not a filter option either.
        assert (await patient.departments(hospital.slug)).json()["data"] == {"departments": []}

    async def test_a_doctor_who_is_hidden_disappears_on_the_next_request(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id)
        assert await patient.refs(hospital.slug) == [str(doctor.id)]

        await db_session.execute(
            update(Doctor).where(Doctor.id == doctor.id).values(deleted_at=func.now())
        )
        await db_session.commit()

        assert await patient.refs(hospital.slug) == []
        assert _error(await patient.doctor(hospital.slug, doctor.id)) == NOT_FOUND

    async def test_search_matches_name_or_specialization_literally_whatever_the_case(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession, tag: str
    ) -> None:
        asha = await insert_doctor(
            db_session, hospital.id, first_name=f"{tag}Asha", last_name="Menon"
        )
        ravi = await insert_doctor(
            db_session,
            hospital.id,
            first_name="Ravi",
            last_name="Iyer",
            specialization=f"{tag}ology",
        )
        odd = await insert_doctor(
            db_session, hospital.id, first_name="100%_", last_name=f"{tag}son"
        )

        assert await patient.refs(hospital.slug, search=f"{tag.upper()}ASHA") == [str(asha.id)]
        assert await patient.refs(hospital.slug, search=f"{tag}asha menon") == [str(asha.id)]
        assert await patient.refs(hospital.slug, search=f"{tag}OLOGY") == [str(ravi.id)]
        assert await patient.refs(hospital.slug, search="100%_") == [str(odd.id)]
        assert await patient.refs(hospital.slug, search="%") == [str(odd.id)]
        assert await patient.refs(hospital.slug, search="_") == [str(odd.id)]
        assert await patient.refs(hospital.slug, search="   ") == [
            str(ravi.id),
            str(asha.id),
            str(odd.id),
        ]

    @pytest.mark.parametrize(
        "needle", ["staff-secret", "LICCANARY", "+919000000001", "doctor-", "812", "bio-word"]
    )
    async def test_search_reads_nothing_but_name_and_specialization(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession, needle: str
    ) -> None:
        """Attack: confirm an e-mail, licence, phone, fee or id by searching for it."""
        doctor = await insert_doctor(
            db_session, hospital.id, bio="bio-word", consultation_fee="812.00"
        )

        assert await patient.refs(hospital.slug, search=needle) == []
        assert await patient.refs(hospital.slug, search=str(doctor.id)) == []
        assert await patient.refs(hospital.slug, search=str(doctor.user_id)) == []

    @pytest.mark.parametrize(
        "text",
        [
            "' OR '1'='1",
            "'; DROP TABLE doctors;--",
            "\\",
            "%%",
            "../../etc/passwd",
            "<script>",
            "é😀",
        ],
    )
    async def test_hostile_search_text_is_only_text(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession, text: str
    ) -> None:
        await insert_doctor(db_session, hospital.id)

        response = await patient.doctors(hospital.slug, search=text)

        assert response.status_code == 200, response.text
        assert response.json()["data"] == []

    async def test_the_department_filter_and_its_options(
        self, patient: _Patient, hospital: Hospital, other: Hospital, db_session: AsyncSession
    ) -> None:
        cardio = await insert_department(
            db_session, hospital.id, name="Cardiology", description="Heart", email="c@x.test"
        )
        neuro = await insert_department(db_session, hospital.id, name="neurology")
        empty = await insert_department(db_session, hospital.id, name="Empty")
        gone = await insert_department(db_session, hospital.id, name="Gone", deleted=True)
        foreign = await insert_department(db_session, other.id, name="Foreign")
        a = await insert_doctor(db_session, hospital.id, last_name="A", department_id=cardio.id)
        b = await insert_doctor(db_session, hospital.id, last_name="B", department_id=neuro.id)
        c = await insert_doctor(db_session, hospital.id, last_name="C", department_id=gone.id)
        await insert_doctor(db_session, other.id, last_name="D", department_id=foreign.id)

        options = (await patient.departments(hospital.slug)).json()["data"]

        assert options == {
            "departments": [
                {"ref": str(cardio.id), "name": "Cardiology", "description": "Heart"},
                {"ref": str(neuro.id), "name": "neurology", "description": None},
            ]
        }
        assert await patient.refs(hospital.slug, department=cardio.id) == [str(a.id)]
        assert await patient.refs(hospital.slug, department=neuro.id) == [str(b.id)]
        assert await patient.refs(hospital.slug, department=cardio.id, search="B") == []
        # Empty, deactivated, another hospital's, unknown: nothing, and no error.
        for department in (empty.id, gone.id, foreign.id, uuid.uuid4()):
            assert await patient.refs(hospital.slug, department=department) == []
        # A doctor whose department was deactivated is still listed, without it.
        listed = {d["ref"]: d for d in (await patient.doctors(hospital.slug)).json()["data"]}
        assert listed[str(c.id)]["department"] is None
        assert "c@x.test" not in json.dumps(options)

    async def test_pages_cover_the_list_once_in_one_order_with_a_truthful_total(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        names = [
            ("b", "Rao"),
            ("A", "rao"),
            ("a", "Rao"),
            ("Z", "Abel"),
            ("m", "RAO"),
            ("B", "rao"),
        ]
        doctors = [
            await insert_doctor(db_session, hospital.id, first_name=first, last_name=last)
            for first, last in names
        ]
        expected = [
            str(doctor.id)
            for doctor, (first, last) in sorted(
                zip(doctors, names, strict=True),
                key=lambda pair: (pair[1][1].lower(), pair[1][0].lower(), str(pair[0].id)),
            )
        ]
        # Ties on the folded name are broken by id, as the query does.
        tied = sorted(expected[1:3], key=uuid.UUID), sorted(expected[3:5], key=uuid.UUID)
        expected = [expected[0], *tied[0], *tied[1], expected[5]]

        for size in (1, 2, 4, 50):
            walked: list[str] = []
            for page in range(1, 9):
                response = await patient.doctors(hospital.slug, page=page, page_size=size)
                assert response.json()["metadata"]["pagination"]["total_records"] == 6
                walked += [doctor["ref"] for doctor in response.json()["data"]]
            assert walked == expected, size

    @pytest.mark.parametrize(
        "params",
        [
            {"page": 0},
            {"page": 1001},
            {"page_size": 0},
            {"page_size": 51},
            {"page": "x"},
            {"search": "a" * 81},
            {"search": "a\x00b"},
            {"department": "not-a-uuid"},
            {"department": "' OR 1=1"},
        ],
    )
    async def test_a_parameter_out_of_bounds_is_refused(
        self, patient: _Patient, hospital: Hospital, params: dict[str, Any]
    ) -> None:
        assert (await patient.doctors(hospital.slug, **params)).status_code == 422

    async def test_a_parameter_it_does_not_define_changes_nothing(
        self, patient: _Patient, hospital: Hospital, other: Hospital, db_session: AsyncSession
    ) -> None:
        """Attack: ask for hidden or foreign doctors with a parameter of one's own."""
        shown = await insert_doctor(db_session, hospital.id)
        await insert_doctor(db_session, hospital.id, deleted=True)
        await insert_doctor(db_session, other.id)

        refs = await patient.refs(
            hospital.slug,
            include_deleted="true",
            hospital_id=str(other.id),
            hospital=other.slug,
            status="inactive",
            all="1",
        )

        assert refs == [str(shown.id)]


# ── Hospital boundaries ──────────────────────────────────────────────────────


class TestHospitalScope:
    async def test_a_doctor_is_only_ever_under_their_own_hospital(
        self, patient: _Patient, hospital: Hospital, other: Hospital, db_session: AsyncSession
    ) -> None:
        """Attack: read Hospital A's doctor through Hospital B's path, by code and by id."""
        mine = await insert_doctor(db_session, hospital.id, last_name="Mine")
        theirs = await insert_doctor(db_session, other.id, last_name="Theirs")
        unknown = await patient.doctor(hospital.slug, uuid.uuid4())

        assert await patient.refs(hospital.slug) == [str(mine.id)]
        assert await patient.refs(other.slug) == [str(theirs.id)]
        assert await patient.refs(hospital.id) == [str(mine.id)]
        assert (await patient.doctor(other.slug, theirs.id)).status_code == 200
        for hospital_ref in (hospital.slug, hospital.id):
            crossed = await patient.doctor(hospital_ref, theirs.id)
            assert _refusal(crossed) == _refusal(unknown)
        assert await patient.refs(hospital.slug, search="Theirs") == []

    async def test_a_user_of_another_hospital_behind_a_doctor_hides_the_doctor(
        self, patient: _Patient, hospital: Hospital, other: Hospital, db_session: AsyncSession
    ) -> None:
        """Bad data: the doctor row says Hospital A, the user row says Hospital B."""
        doctor = await insert_doctor(db_session, hospital.id)
        await db_session.execute(
            update(User).where(User.id == doctor.user_id).values(hospital_id=other.id)
        )
        await db_session.commit()

        assert await patient.refs(hospital.slug) == []
        assert await patient.refs(other.slug) == []

    async def test_an_availability_window_of_another_hospital_does_not_count(
        self, patient: _Patient, hospital: Hospital, other: Hospital, db_session: AsyncSession
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id)
        await db_session.execute(
            update(DoctorAvailability)
            .where(DoctorAvailability.doctor_id == doctor.id)
            .values(hospital_id=other.id)
        )
        await db_session.commit()

        assert await patient.refs(hospital.slug) == []

    @pytest.mark.parametrize(
        "closed", ["inactive", "flag_off", "flag_string", "unknown", "unusable_code"]
    )
    async def test_a_hospital_that_is_not_shown_has_no_doctors(
        self, patient: _Patient, db_session: AsyncSession, tag: str, closed: str
    ) -> None:
        """Attack: reach the doctors of a hospital that hospital discovery refuses."""
        states: dict[str, dict[str, Any]] = {
            "inactive": {"is_active": False},
            "flag_off": {"enabled": False},
            "flag_string": {"enabled": "true"},
            "unknown": {},
            "unusable_code": {"slug": f"{tag}-Upper"},
        }
        made = states[closed]
        hospital = await insert_hospital(db_session, name=f"{tag} Shut", **made)
        doctor = await insert_doctor(db_session, hospital.id)
        refs = [hospital.id] if closed == "unknown" else [hospital.slug, hospital.id]
        if closed == "unknown":
            await db_session.execute(
                update(Hospital).where(Hospital.id == hospital.id).values(is_active=False)
            )
            await db_session.commit()
            refs = [f"{tag}-no-such", uuid.uuid4()]
        reference = await patient.doctors(f"{tag}-nowhere")

        for ref in refs:
            answers = [
                await patient.doctors(ref),
                await patient.doctors(ref, search="x"),
                await patient.doctor(ref, doctor.id),
                await patient.departments(ref),
            ]
            assert {_refusal(answer) for answer in answers} == {_refusal(reference)}
        assert _error(reference) == NOT_FOUND

    async def test_closing_a_hospital_closes_its_doctors_at_once(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id)
        assert (await patient.doctor(hospital.slug, doctor.id)).status_code == 200

        await open_hospital(db_session, hospital.id, enabled=False)

        assert _error(await patient.doctors(hospital.slug)) == NOT_FOUND
        assert _error(await patient.doctor(hospital.slug, doctor.id)) == NOT_FOUND
        assert _error(await patient.departments(hospital.slug)) == NOT_FOUND


# ── References ───────────────────────────────────────────────────────────────

HOSTILE = [
    "x' OR '1'='1",
    "1; DROP TABLE doctors",
    "..%2F..%2Fme",
    "%2E%2E",
    "%00",
    "%25",
    "a" * 4000,
    "0" * 36,
    "not-a-uuid",
    "{doctor}x",
    "{doctor_hex}",
    "{{{doctor}}}",
    "urn:uuid:{doctor}",
    "{user}",
    "{hospital}",
    "{account}",
    "{department}",
    "%E2%84%AA",
]


class TestManipulatedReferences:
    @pytest.mark.parametrize("shape", HOSTILE)
    async def test_a_doctor_reference_that_is_not_one_gets_the_one_404(
        self,
        patient: _Patient,
        hospital: Hospital,
        db_session: AsyncSession,
        shape: str,
    ) -> None:
        department = await insert_department(db_session, hospital.id, name="Dept")
        doctor = await insert_doctor(db_session, hospital.id, department_id=department.id)
        ref = shape.format(
            doctor=doctor.id,
            doctor_hex=doctor.id.hex,
            user=doctor.user_id,
            hospital=hospital.id,
            account=patient.account_id,
            department=department.id,
        )
        unknown = await patient.doctor(hospital.slug, uuid.uuid4())

        answer = await patient.doctor(hospital.slug, ref)

        assert _refusal(answer) == _refusal(unknown)
        assert _error(unknown) == NOT_FOUND

    @pytest.mark.parametrize("shape", ["{doctor}", " {doctor} ", "{doctor_upper}"])
    async def test_a_respelling_of_the_real_reference_finds_only_that_doctor(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession, shape: str
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id)
        ref = shape.format(doctor=doctor.id, doctor_upper=str(doctor.id).upper())

        answer = await patient.doctor(hospital.slug, ref)

        assert answer.status_code == 200, answer.text
        assert answer.json()["data"]["ref"] == str(doctor.id)

    @pytest.mark.parametrize(
        "hospital_ref", ["x' OR '1'='1", "..%2F..%2Fme", "a" * 4000, "%25", "UPPER_case", "%00"]
    )
    async def test_a_hospital_reference_that_is_not_one_gets_the_one_404(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession, hospital_ref: str
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id)
        unknown = await patient.doctors("no-such-hospital")

        for answer in (
            await patient.doctors(hospital_ref),
            await patient.doctor(hospital_ref, doctor.id),
            await patient.departments(hospital_ref),
        ):
            assert _refusal(answer) == _refusal(unknown)

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    async def test_discovery_is_read_only(
        self,
        patient: _Patient,
        hospital: Hospital,
        db_session: AsyncSession,
        method: str,
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id)
        before = (await db_session.execute(select(func.count()).select_from(Doctor))).scalar_one()

        for path in ("doctors", f"doctors/{doctor.id}", "departments"):
            response = await patient.client.request(
                method, f"{HOSPITALS}/{hospital.slug}/{path}", json={}, headers=patient.headers
            )
            assert response.status_code == 405, (method, path)
        after = (await db_session.execute(select(func.count()).select_from(Doctor))).scalar_one()
        assert before == after


# ── Tokens, policy, audit ────────────────────────────────────────────────────


def _with_a_required_policy(application: FastAPI) -> None:
    """Make the application require a policy nobody has accepted."""
    required = (Policy(ConsentPurpose.TERMS_OF_SERVICE, "2027-01", "platform", required=True),)

    def _consent(
        consents: PatientConsentRepository = Depends(get_patient_consent_repository),
        links: PatientAccountLinkRepository = Depends(get_patient_account_link_repository),
        uow: UnitOfWork = Depends(get_unit_of_work),
        audit: AuditSink = Depends(get_audit_sink),
    ) -> ConsentService:
        return ConsentService(consents, links, uow, audit, policies=required)

    application.dependency_overrides[get_consent_service] = _consent


class TestGates:
    async def test_it_needs_a_patient_token(
        self,
        browser: AsyncClient,
        patient: _Patient,
        hospital: Hospital,
        db_session: AsyncSession,
    ) -> None:
        """Attack: read the directory with no token, a broken one, or a staff token."""
        doctor = await insert_doctor(db_session, hospital.id)
        staff = create_access_token(doctor.user_id, hospital.id)
        token = patient.headers["Authorization"]
        credentials = [
            {},
            {"Authorization": "Bearer nonsense"},
            {"Authorization": token[:-2] + ("ab" if not token.endswith("ab") else "cd")},
            {"Authorization": f"Bearer {staff}"},
            {"Cookie": f"access_token={token.removeprefix('Bearer ')}"},
        ]

        for headers in credentials:
            for path in ("doctors", f"doctors/{doctor.id}", "departments", "doctors?search=a"):
                response = await browser.get(f"{HOSPITALS}/{hospital.slug}/{path}", headers=headers)
                assert response.status_code == 401, (headers.keys(), path)
                assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"
                assert "Asha" not in response.text

    async def test_a_patient_token_opens_nothing_on_the_staff_doctor_api(
        self, browser: AsyncClient, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id)

        for path in ("/api/v1/doctors", f"/api/v1/doctors/{doctor.id}", "/api/v1/departments"):
            response = await browser.get(path, headers=patient.headers)
            assert response.status_code == 401, path

    async def test_a_pending_required_policy_closes_doctor_discovery(
        self,
        application: FastAPI,
        patient: _Patient,
        hospital: Hospital,
        db_session: AsyncSession,
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id)
        _with_a_required_policy(application)

        answers = [
            await patient.doctors(hospital.slug),
            await patient.doctor(hospital.slug, doctor.id),
            await patient.departments(hospital.slug),
            await patient.doctors("no-such-hospital"),
            await patient.doctor(hospital.slug, "x' OR 1=1"),
        ]

        assert {answer.status_code for answer in answers} == {403}
        assert {answer.json()["error_code"] for answer in answers} == {"CONSENT_REQUIRED"}
        assert len({_refusal(answer) for answer in answers}) == 1

    async def test_reading_the_directory_writes_no_audit_row(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id)
        count = select(func.count()).select_from(AuditLog)
        before = (await db_session.execute(count)).scalar_one()

        await patient.doctors(hospital.slug, search="a")
        await patient.doctor(hospital.slug, doctor.id)
        await patient.doctor(hospital.slug, uuid.uuid4())
        await patient.departments(hospital.slug)

        assert (await db_session.execute(count)).scalar_one() == before

    async def test_nothing_is_ever_promoted_rated_priced_or_scheduled(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id)
        department = await insert_department(db_session, hospital.id, name="Any")
        await db_session.execute(
            update(Doctor).where(Doctor.id == doctor.id).values(department_id=department.id)
        )
        await db_session.commit()

        bodies = [
            (await patient.doctors(hospital.slug)).text,
            (await patient.doctor(hospital.slug, doctor.id)).text,
            (await patient.departments(hospital.slug)).text,
        ]

        for body in bodies:
            for word in (
                "promoted",
                "listing",
                "rating",
                "review",
                "_fee",
                '"fee',
                "slot",
                "availab",
                "experience",
                "distance",
                "09:00",
            ):
                assert word not in body, word
        assert await db_session.get(Department, department.id) is not None
