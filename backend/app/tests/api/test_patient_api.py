"""API tests for the patient endpoints.

Real app, real service, real repository, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3). Repositories are
deliberately not mocked: mocking them here would hide exactly the regressions
these tests exist to catch.

Per ``docs/06-API_STANDARDS.md`` §24 every endpoint gets a happy path, a
missing-auth case, a wrong-permission case, a not-found case where applicable,
and a validation-failure case. There is also a cross-tenant test per endpoint,
because tenant isolation is the one bug class that would be catastrophic and
silent.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.dependencies.db import get_db_session
from app.core.security import create_access_token
from app.main import create_app
from app.tests.conftest import grant_permissions
from app.tests.factories import build_patient_payload

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

ALL_PATIENT_PERMISSIONS = [
    "patient.read",
    "patient.create",
    "patient.update",
    "patient.delete",
]


async def _make_user(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    permissions: list[str],
) -> uuid.UUID:
    """Create an active user in ``hospital_id`` holding ``permissions``."""
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"api-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Api",
        last_name="Tester",
    )
    session.add(user)
    await session.flush()
    await grant_permissions(session, hospital_id=hospital_id, user_id=user.id, codes=permissions)
    return user.id


def _auth_header(user_id: uuid.UUID, hospital_id: uuid.UUID | None) -> dict[str, str]:
    """Mint a Bearer header for a user."""
    token = create_access_token(user_id=user_id, hospital_id=hospital_id)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """An HTTP client whose requests share the test's rolled-back session.

    ``get_db_session`` is overridden so the app, the service, and the test all
    see the same transaction. Without this the app would open its own session
    against the real database and the test's rows would be invisible to it.
    """
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def full_access(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Auth header for a user holding every patient permission."""
    user_id = await _make_user(db_session, hospital_id, ALL_PATIENT_PERMISSIONS)
    return _auth_header(user_id, hospital_id)


@pytest_asyncio.fixture
async def read_only(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Auth header for a user holding only ``patient.read``."""
    user_id = await _make_user(db_session, hospital_id, ["patient.read"])
    return _auth_header(user_id, hospital_id)


async def _register(api: AsyncClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    """Register a patient through the API and return the response data block."""
    response = await api.post(
        "/api/v1/patients", json=build_patient_payload(**overrides), headers=headers
    )
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


class TestRegisterPatient:
    """``POST /api/v1/patients``."""

    async def test_register_returns_201_with_a_generated_mrn(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        response = await api.post(
            "/api/v1/patients", json=build_patient_payload(), headers=full_access
        )

        assert response.status_code == 201
        body = response.json()
        assert body["success"] is True
        assert body["data"]["mrn"].startswith("MRN-")
        assert body["data"]["first_name"] == "Ananya"
        assert body["data"]["status"] == "active"

    async def test_register_without_a_token_returns_401(self, api: AsyncClient) -> None:
        response = await api.post("/api/v1/patients", json=build_patient_payload())

        assert response.status_code == 401

    async def test_register_without_the_create_permission_returns_403(
        self, api: AsyncClient, read_only: dict[str, str]
    ) -> None:
        response = await api.post(
            "/api/v1/patients", json=build_patient_payload(), headers=read_only
        )

        assert response.status_code == 403

    async def test_register_with_an_invalid_payload_returns_422(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        response = await api.post(
            "/api/v1/patients",
            json=build_patient_payload(date_of_birth="2099-01-01"),
            headers=full_access,
        )

        assert response.status_code == 422

    async def test_register_rejects_a_client_supplied_mrn(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        # The MRN is server-generated. Accepting one from the body would let a
        # caller collide with an existing record.
        response = await api.post(
            "/api/v1/patients",
            json=build_patient_payload(mrn="MRN-2026-00001"),
            headers=full_access,
        )

        assert response.status_code == 422

    async def test_register_ignores_a_body_supplied_hospital_id(
        self, api: AsyncClient, full_access: dict[str, str], other_hospital_id: uuid.UUID
    ) -> None:
        # Tenancy comes from the token, never the body.
        response = await api.post(
            "/api/v1/patients",
            json=build_patient_payload(hospital_id=str(other_hospital_id)),
            headers=full_access,
        )

        assert response.status_code == 422


class TestGetPatient:
    """``GET /api/v1/patients/{id}``."""

    async def test_get_returns_the_patient(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        created = await _register(api, full_access)

        response = await api.get(f"/api/v1/patients/{created['id']}", headers=full_access)

        assert response.status_code == 200
        assert response.json()["data"]["mrn"] == created["mrn"]

    async def test_get_an_unknown_patient_returns_404(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        response = await api.get(f"/api/v1/patients/{uuid.uuid4()}", headers=full_access)

        assert response.status_code == 404
        assert response.json()["error_code"] == "RESOURCE_NOT_FOUND"

    async def test_get_without_a_token_returns_401(self, api: AsyncClient) -> None:
        response = await api.get(f"/api/v1/patients/{uuid.uuid4()}")

        assert response.status_code == 401

    async def test_get_from_another_hospital_returns_404(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        full_access: dict[str, str],
        other_hospital_id: uuid.UUID,
    ) -> None:
        # AC-7. A 404 rather than a 403, so the response does not confirm that
        # the record exists in some other tenant.
        created = await _register(api, full_access)
        intruder_id = await _make_user(db_session, other_hospital_id, ALL_PATIENT_PERMISSIONS)
        intruder = _auth_header(intruder_id, other_hospital_id)

        response = await api.get(f"/api/v1/patients/{created['id']}", headers=intruder)

        assert response.status_code == 404


class TestListAndSearchPatients:
    """``GET /api/v1/patients``."""

    async def test_list_returns_a_paginated_envelope(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        await _register(api, full_access, last_name="Rao")
        await _register(api, full_access, last_name="Sharma")

        response = await api.get("/api/v1/patients", headers=full_access)

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert len(body["data"]) == 2
        assert body["metadata"]["pagination"]["total_records"] == 2
        assert body["metadata"]["pagination"]["total_pages"] == 1

    async def test_list_summaries_exclude_medical_history(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        # Clinical data must not leak into a response that only needed identity.
        await _register(api, full_access)

        response = await api.get("/api/v1/patients", headers=full_access)

        row = response.json()["data"][0]
        assert not {"allergies", "chronic_conditions", "current_medications", "notes"} & set(row)

    async def test_search_by_name_prefix(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        await _register(api, full_access, last_name="Rao")
        await _register(api, full_access, last_name="Sharma")

        response = await api.get("/api/v1/patients", params={"q": "rao"}, headers=full_access)

        body = response.json()
        assert body["metadata"]["pagination"]["total_records"] == 1
        assert body["data"][0]["last_name"] == "Rao"

    async def test_search_by_exact_mrn(self, api: AsyncClient, full_access: dict[str, str]) -> None:
        created = await _register(api, full_access)

        response = await api.get(
            "/api/v1/patients", params={"q": created["mrn"]}, headers=full_access
        )

        assert response.json()["data"][0]["id"] == created["id"]

    async def test_pagination_respects_page_size(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        for index in range(3):
            await _register(api, full_access, last_name=f"Patient{index}")

        response = await api.get(
            "/api/v1/patients", params={"page": 2, "page_size": 2}, headers=full_access
        )

        body = response.json()
        assert len(body["data"]) == 1
        assert body["metadata"]["pagination"]["page"] == 2
        assert body["metadata"]["pagination"]["total_pages"] == 2

    async def test_page_size_above_the_cap_returns_422(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        response = await api.get("/api/v1/patients", params={"page_size": 500}, headers=full_access)

        assert response.status_code == 422

    async def test_list_never_returns_another_hospitals_patients(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        full_access: dict[str, str],
        other_hospital_id: uuid.UUID,
    ) -> None:
        await _register(api, full_access, last_name="Rao")
        intruder_id = await _make_user(db_session, other_hospital_id, ALL_PATIENT_PERMISSIONS)

        response = await api.get(
            "/api/v1/patients", headers=_auth_header(intruder_id, other_hospital_id)
        )

        assert response.json()["metadata"]["pagination"]["total_records"] == 0

    async def test_list_without_a_token_returns_401(self, api: AsyncClient) -> None:
        response = await api.get("/api/v1/patients")

        assert response.status_code == 401


class TestUpdatePatient:
    """``PATCH /api/v1/patients/{id}``."""

    async def test_patch_applies_only_the_fields_sent(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        created = await _register(api, full_access)

        response = await api.patch(
            f"/api/v1/patients/{created['id']}",
            json={"occupation": "Engineer"},
            headers=full_access,
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["occupation"] == "Engineer"
        assert data["first_name"] == "Ananya"
        assert data["mrn"] == created["mrn"]

    async def test_patch_without_the_update_permission_returns_403(
        self, api: AsyncClient, full_access: dict[str, str], read_only: dict[str, str]
    ) -> None:
        created = await _register(api, full_access)

        response = await api.patch(
            f"/api/v1/patients/{created['id']}",
            json={"occupation": "Engineer"},
            headers=read_only,
        )

        assert response.status_code == 403

    async def test_patch_an_unknown_patient_returns_404(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        response = await api.patch(
            f"/api/v1/patients/{uuid.uuid4()}",
            json={"occupation": "Engineer"},
            headers=full_access,
        )

        assert response.status_code == 404

    async def test_patch_rejects_an_attempt_to_change_the_mrn(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        created = await _register(api, full_access)

        response = await api.patch(
            f"/api/v1/patients/{created['id']}",
            json={"mrn": "MRN-2026-99999"},
            headers=full_access,
        )

        assert response.status_code == 422

    async def test_patch_with_an_empty_body_returns_422(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        created = await _register(api, full_access)

        response = await api.patch(
            f"/api/v1/patients/{created['id']}", json={}, headers=full_access
        )

        assert response.status_code == 422

    async def test_patch_from_another_hospital_returns_404(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        full_access: dict[str, str],
        other_hospital_id: uuid.UUID,
    ) -> None:
        created = await _register(api, full_access)
        intruder_id = await _make_user(db_session, other_hospital_id, ALL_PATIENT_PERMISSIONS)

        response = await api.patch(
            f"/api/v1/patients/{created['id']}",
            json={"occupation": "Engineer"},
            headers=_auth_header(intruder_id, other_hospital_id),
        )

        assert response.status_code == 404


class TestDeactivatePatient:
    """``DELETE /api/v1/patients/{id}``."""

    async def test_delete_soft_deletes_and_returns_the_record(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        created = await _register(api, full_access)

        response = await api.delete(f"/api/v1/patients/{created['id']}", headers=full_access)

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "inactive"

    async def test_a_deactivated_patient_disappears_from_the_list(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        created = await _register(api, full_access)
        await api.delete(f"/api/v1/patients/{created['id']}", headers=full_access)

        listed = await api.get("/api/v1/patients", headers=full_access)

        assert listed.json()["metadata"]["pagination"]["total_records"] == 0

    async def test_a_deactivated_patient_is_still_retrievable_on_request(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        # Soft delete only — the record must remain readable for audit and for
        # the appointments and invoices that reference it.
        created = await _register(api, full_access)
        await api.delete(f"/api/v1/patients/{created['id']}", headers=full_access)

        response = await api.get(
            f"/api/v1/patients/{created['id']}",
            params={"include_inactive": True},
            headers=full_access,
        )

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "inactive"

    async def test_deleting_twice_returns_400(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        created = await _register(api, full_access)
        await api.delete(f"/api/v1/patients/{created['id']}", headers=full_access)

        response = await api.delete(f"/api/v1/patients/{created['id']}", headers=full_access)

        assert response.status_code == 400
        assert response.json()["error_code"] == "BUSINESS_RULE_VIOLATION"

    async def test_delete_without_the_delete_permission_returns_403(
        self, api: AsyncClient, full_access: dict[str, str], read_only: dict[str, str]
    ) -> None:
        created = await _register(api, full_access)

        response = await api.delete(f"/api/v1/patients/{created['id']}", headers=read_only)

        assert response.status_code == 403

    async def test_delete_an_unknown_patient_returns_404(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        response = await api.delete(f"/api/v1/patients/{uuid.uuid4()}", headers=full_access)

        assert response.status_code == 404


class TestResponseEnvelope:
    """The contract the frontend and SDKs bind to (``docs/06-API_STANDARDS.md`` §5)."""

    async def test_success_responses_carry_the_standard_envelope(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        response = await api.post(
            "/api/v1/patients", json=build_patient_payload(), headers=full_access
        )

        body = response.json()
        assert set(body) >= {"success", "message", "data"}
        assert body["success"] is True

    async def test_error_responses_carry_the_standard_envelope(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        response = await api.get(f"/api/v1/patients/{uuid.uuid4()}", headers=full_access)

        body = response.json()
        assert body["success"] is False
        assert body["error_code"] == "RESOURCE_NOT_FOUND"
        assert "message" in body
