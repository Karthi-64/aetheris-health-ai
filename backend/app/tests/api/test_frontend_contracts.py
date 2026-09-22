"""Contract tests for the endpoints the frontend consumes.

These lock the shapes published in ``docs/18-API_CONTRACTS.md``. The per-module
API tests already cover behaviour; what is asserted here is narrower and
deliberately brittle — the exact field set of each response and the exact names
of the query parameters — because the frontend now hard-codes them. A field
renamed or quietly dropped is a broken integration in another repository, and
the point of these tests is to make that fail here first.

If one of these fails, the fix is either to revert the contract change or to
change the contract *and* the doc *and* tell the frontend team. Do not adjust
the expected sets to match new behaviour without doing all three.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.dependencies.db import get_db_session
from app.core.security import create_access_token
from app.main import create_app
from app.tests.conftest import grant_permissions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

#: Everything the frontend's current targets need, in one user. Real roles are
#: narrower (see the matrix in ``docs/18-API_CONTRACTS.md`` §1.9); the split is
#: covered by the per-module permission tests, not here.
FRONTEND_PERMISSIONS = [
    "patient.read",
    "patient.create",
    "patient.update",
    "patient.delete",
    "department.read",
    "department.create",
    "doctor.read",
    "doctor.create",
    "doctor.availability.read",
    "appointment.read",
    "appointment.book",
    # Booking outside published availability. The seeded doctor below has no
    # schedule, so without this every booking in this module would be a 400.
    "appointment.book_override",
]

#: Far-future Monday: "not in the past" holds, and the date never drifts.
BASE = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)

PATIENT_SUMMARY_FIELDS = {
    "id",
    "mrn",
    "first_name",
    "last_name",
    "full_name",
    "date_of_birth",
    "age",
    "gender",
    "phone",
    "status",
}

DOCTOR_SUMMARY_FIELDS = {
    "id",
    "user_id",
    "full_name",
    "specialization",
    "department_id",
    "department_name",
    "consultation_fee",
    "status",
}

DEPARTMENT_SUMMARY_FIELDS = {"id", "code", "name", "location", "status"}

APPOINTMENT_SUMMARY_FIELDS = {
    "id",
    "patient_id",
    "patient_name",
    "doctor_id",
    "doctor_name",
    "scheduled_start",
    "scheduled_end",
    "status",
    "type",
}


async def _make_user(session: AsyncSession, hospital_id: uuid.UUID) -> uuid.UUID:
    """Create an active user holding :data:`FRONTEND_PERMISSIONS`."""
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"contract-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Contract",
        last_name="Tester",
    )
    session.add(user)
    await session.flush()
    await grant_permissions(
        session, hospital_id=hospital_id, user_id=user.id, codes=FRONTEND_PERMISSIONS
    )
    return user.id


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """An HTTP client sharing the test's rolled-back session."""
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Bearer header for the frontend-capable user."""
    user_id = await _make_user(db_session, hospital_id)
    token = create_access_token(user_id=user_id, hospital_id=hospital_id)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def clinic(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """A department, a doctor attached to it, and a patient."""
    from app.models.department import Department
    from app.models.doctor import Doctor
    from app.models.user import User

    department = Department(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        code=f"C{uuid.uuid4().hex[:3].upper()}",
        name="Cardiology",
        location="Block A, 2nd floor",
    )
    doctor_user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"doc-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Priya",
        last_name="Sharma",
    )
    db_session.add_all([department, doctor_user])
    await db_session.flush()

    doctor = Doctor(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        user_id=doctor_user.id,
        department_id=department.id,
        specialization="Cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(doctor)
    await db_session.flush()

    return {"department_id": department.id, "doctor_id": doctor.id}


async def _register_patient(
    api: AsyncClient, auth: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    """Register a patient through the API and return its data block."""
    payload = {
        "first_name": "Ananya",
        "last_name": "Rao",
        "date_of_birth": "1988-03-14",
        "gender": "female",
        "phone": "+919812345678",
        **overrides,
    }
    response = await api.post("/api/v1/patients", json=payload, headers=auth)
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


class TestPatientContract:
    """``/api/v1/patients`` — ``docs/18-API_CONTRACTS.md`` §2."""

    async def test_list_row_has_exactly_the_documented_fields(
        self, api: AsyncClient, auth: dict[str, str]
    ) -> None:
        await _register_patient(api, auth)

        response = await api.get("/api/v1/patients", headers=auth)

        assert set(response.json()["data"][0]) == PATIENT_SUMMARY_FIELDS

    async def test_list_row_carries_no_doctor_department_or_clinical_status(
        self, api: AsyncClient, auth: dict[str, str]
    ) -> None:
        # The mock frontend displayed all three. None is a patient attribute,
        # and inventing them here is what the mismatch resolution rejected.
        await _register_patient(api, auth)

        row = response_row = (await api.get("/api/v1/patients", headers=auth)).json()["data"][0]

        assert "doctor" not in row
        assert "department" not in row
        assert response_row["status"] in {"active", "inactive"}

    async def test_name_is_two_fields_plus_a_computed_full_name(
        self, api: AsyncClient, auth: dict[str, str]
    ) -> None:
        created = await _register_patient(api, auth, first_name="Ananya", last_name="Rao")

        assert created["full_name"] == "Ananya Rao"
        assert "name" not in created

    async def test_age_is_computed_from_date_of_birth_and_never_accepted(
        self, api: AsyncClient, auth: dict[str, str]
    ) -> None:
        created = await _register_patient(api, auth, date_of_birth="2000-01-01")

        assert created["age"] == _completed_years(2000, 1, 1)

        # Sending an age instead of a DOB is a validation error, not a silently
        # ignored field — the frontend must collect a date.
        response = await api.post(
            "/api/v1/patients",
            json={"first_name": "A", "last_name": "B", "age": 30, "gender": "female"},
            headers=auth,
        )
        assert response.status_code == 422

    async def test_gender_accepts_the_enum_and_rejects_m_and_f(
        self, api: AsyncClient, auth: dict[str, str]
    ) -> None:
        for value in ("male", "female", "other", "unspecified"):
            created = await _register_patient(api, auth, gender=value, last_name=f"G{value}")
            assert created["gender"] == value

        response = await api.post(
            "/api/v1/patients",
            json={
                "first_name": "Ravi",
                "last_name": "Menon",
                "date_of_birth": "1970-05-05",
                "gender": "M",
            },
            headers=auth,
        )
        assert response.status_code == 422

    async def test_mrn_is_server_generated_and_a_supplied_one_is_rejected(
        self, api: AsyncClient, auth: dict[str, str]
    ) -> None:
        created = await _register_patient(api, auth)
        assert created["mrn"].startswith("MRN-")

        response = await api.post(
            "/api/v1/patients",
            json={
                "first_name": "Ravi",
                "last_name": "Menon",
                "date_of_birth": "1970-05-05",
                "gender": "male",
                "mrn": "MRN-2026-99999",
            },
            headers=auth,
        )
        assert response.status_code == 422

    async def test_q_matches_a_name_prefix_but_not_a_substring(
        self, api: AsyncClient, auth: dict[str, str]
    ) -> None:
        # Documented explicitly because it is the one search behaviour a
        # frontend is likely to assume wrongly.
        await _register_patient(api, auth, last_name="Rao")

        prefix = await api.get("/api/v1/patients", params={"q": "rao"}, headers=auth)
        substring = await api.get("/api/v1/patients", params={"q": "ao"}, headers=auth)

        assert prefix.json()["metadata"]["pagination"]["total_records"] == 1
        assert substring.json()["metadata"]["pagination"]["total_records"] == 0

    async def test_the_search_parameter_is_q_not_search(
        self, api: AsyncClient, auth: dict[str, str]
    ) -> None:
        # `?search=` is not an alias: it is ignored, so a frontend sending it
        # gets an unfiltered list rather than an error. Worth pinning.
        await _register_patient(api, auth, last_name="Rao")
        await _register_patient(api, auth, last_name="Sharma")

        response = await api.get("/api/v1/patients", params={"search": "rao"}, headers=auth)

        assert response.json()["metadata"]["pagination"]["total_records"] == 2

    async def test_metadata_is_null_on_a_single_resource_success(
        self, api: AsyncClient, auth: dict[str, str]
    ) -> None:
        # `docs/06-API_STANDARDS.md` §5.1 shows `metadata.request_id` on every
        # response, but only the error handlers and the paginated routers build
        # a metadata block — a success envelope carries `null` and the
        # correlation id travels in the `X-Request-ID` header. Pinned here so
        # the frontend does not dereference it, and flagged in
        # `docs/18-API_CONTRACTS.md` §1.2 as a doc/code divergence to settle.
        created = await _register_patient(api, auth)

        response = await api.get(f"/api/v1/patients/{created['id']}", headers=auth)

        assert response.json()["metadata"] is None
        assert response.headers["X-Request-ID"]

    async def test_pagination_metadata_uses_snake_case_wire_names(
        self, api: AsyncClient, auth: dict[str, str]
    ) -> None:
        await _register_patient(api, auth)

        response = await api.get(
            "/api/v1/patients", params={"page": 1, "page_size": 25}, headers=auth
        )

        pagination = response.json()["metadata"]["pagination"]
        assert set(pagination) == {"page", "page_size", "total_records", "total_pages"}


class TestDepartmentContract:
    """``/api/v1/departments`` — ``docs/18-API_CONTRACTS.md`` §3."""

    async def test_list_row_has_exactly_the_documented_fields(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        response = await api.get("/api/v1/departments", headers=auth)

        assert set(response.json()["data"][0]) == DEPARTMENT_SUMMARY_FIELDS


class TestDoctorContract:
    """``/api/v1/doctors`` — ``docs/18-API_CONTRACTS.md`` §4."""

    async def test_list_row_has_exactly_the_documented_fields(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        response = await api.get("/api/v1/doctors", headers=auth)

        assert set(response.json()["data"][0]) == DOCTOR_SUMMARY_FIELDS

    async def test_department_name_is_denormalized_into_the_row(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        # So a doctor list needs no second request per row.
        response = await api.get("/api/v1/doctors", headers=auth)

        row = response.json()["data"][0]
        assert row["department_id"] == str(clinic["department_id"])
        assert row["department_name"] == "Cardiology"

    async def test_consultation_fee_is_a_decimal_string_not_a_float(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        # Money is NUMERIC(15,2); serializing it as a JSON number would invite
        # binary-float rounding on the client.
        response = await api.get("/api/v1/doctors", headers=auth)

        assert isinstance(response.json()["data"][0]["consultation_fee"], str)

    async def test_the_department_filter_is_named_department_and_takes_a_uuid(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        match = await api.get(
            "/api/v1/doctors",
            params={"department": str(clinic["department_id"])},
            headers=auth,
        )
        other = await api.get(
            "/api/v1/doctors", params={"department": str(uuid.uuid4())}, headers=auth
        )

        assert match.json()["metadata"]["pagination"]["total_records"] == 1
        assert other.json()["metadata"]["pagination"]["total_records"] == 0

    async def test_specialization_filter_is_exact_not_a_prefix(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        exact = await api.get(
            "/api/v1/doctors", params={"specialization": "Cardiology"}, headers=auth
        )
        partial = await api.get(
            "/api/v1/doctors", params={"specialization": "Cardio"}, headers=auth
        )

        assert exact.json()["metadata"]["pagination"]["total_records"] == 1
        assert partial.json()["metadata"]["pagination"]["total_records"] == 0


class TestAppointmentContract:
    """``/api/v1/appointments`` — ``docs/18-API_CONTRACTS.md`` §5."""

    async def _book(
        self,
        api: AsyncClient,
        auth: dict[str, str],
        clinic: dict[str, uuid.UUID],
        patient_id: str,
        *,
        key: str,
        offset_minutes: int = 0,
    ) -> Any:
        """Book one appointment, returning the raw response."""
        start = BASE + timedelta(minutes=offset_minutes)
        return await api.post(
            "/api/v1/appointments",
            json={
                "patient_id": patient_id,
                "doctor_id": str(clinic["doctor_id"]),
                "scheduled_start": start.isoformat(),
                "scheduled_end": (start + timedelta(minutes=30)).isoformat(),
                "type": "new",
                "reason": "Chest pain",
            },
            headers={**auth, "Idempotency-Key": key},
        )

    async def test_booking_requires_an_idempotency_key_header(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        patient = await _register_patient(api, auth)

        response = await api.post(
            "/api/v1/appointments",
            json={
                "patient_id": patient["id"],
                "doctor_id": str(clinic["doctor_id"]),
                "scheduled_start": BASE.isoformat(),
                "scheduled_end": (BASE + timedelta(minutes=30)).isoformat(),
                "type": "new",
            },
            headers=auth,
        )

        assert response.status_code == 422

    async def test_replaying_a_key_returns_200_and_the_original_appointment(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        # The frontend must treat 200 and 201 as the same success, or a retried
        # booking looks like a failure to the user.
        patient = await _register_patient(api, auth)

        first = await self._book(api, auth, clinic, patient["id"], key="contract-key-0001")
        replay = await self._book(api, auth, clinic, patient["id"], key="contract-key-0001")

        assert first.status_code == 201
        assert replay.status_code == 200
        assert replay.json()["data"]["id"] == first.json()["data"]["id"]

    async def test_list_row_has_exactly_the_documented_fields(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        patient = await _register_patient(api, auth)
        await self._book(api, auth, clinic, patient["id"], key="contract-key-0002")

        response = await api.get("/api/v1/appointments", headers=auth)

        assert set(response.json()["data"][0]) == APPOINTMENT_SUMMARY_FIELDS

    async def test_rows_denormalize_patient_and_doctor_names(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        patient = await _register_patient(api, auth)
        await self._book(api, auth, clinic, patient["id"], key="contract-key-0003")

        row = (await api.get("/api/v1/appointments", headers=auth)).json()["data"][0]

        assert row["patient_name"] == "Ananya Rao"
        assert row["doctor_name"] == "Priya Sharma"

    async def test_an_appointment_carries_no_department_and_no_token_number(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        # Both were assumed by the mock UI. Department is reached through the
        # doctor; queue position is the index in the queue response.
        patient = await _register_patient(api, auth)
        booked = await self._book(api, auth, clinic, patient["id"], key="contract-key-0004")

        detail = booked.json()["data"]
        assert "department_id" not in detail
        assert "token" not in detail
        assert "token_number" not in detail

    async def test_status_and_type_filters_use_their_short_names(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        patient = await _register_patient(api, auth)
        await self._book(api, auth, clinic, patient["id"], key="contract-key-0005")

        booked = await api.get(
            "/api/v1/appointments", params={"status": "booked", "type": "new"}, headers=auth
        )
        cancelled = await api.get(
            "/api/v1/appointments", params={"status": "cancelled"}, headers=auth
        )

        assert booked.json()["metadata"]["pagination"]["total_records"] == 1
        assert cancelled.json()["metadata"]["pagination"]["total_records"] == 0

    async def test_the_queue_returns_a_plain_list_without_pagination(
        self, api: AsyncClient, auth: dict[str, str], clinic: dict[str, uuid.UUID]
    ) -> None:
        # `http.getPaginated` would throw on this response; the frontend has to
        # use a plain GET.
        response = await api.get("/api/v1/appointments/queue", headers=auth)

        body = response.json()
        assert response.status_code == 200
        assert isinstance(body["data"], list)
        metadata = body["metadata"]
        assert metadata is None or "pagination" not in metadata


def _completed_years(year: int, month: int, day: int) -> int:
    """Age in completed years today, matching the backend's computation."""
    today = datetime.now(UTC).date()
    return today.year - year - ((today.month, today.day) < (month, day))
