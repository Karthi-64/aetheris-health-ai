"""API tests for the services catalog endpoints.

Real app, real service, real repository, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.dependencies.db import get_db_session
from app.api.dependencies.services import get_audit_sink
from app.main import create_app
from app.tests.billing_helpers import auth_headers, insert_user_with_permissions
from app.tests.conftest import RecordingAuditSink
from app.tests.factories import build_service_payload

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

URL = "/api/v1/services"
ALL_SERVICE_PERMISSIONS = ["service.read", "service.create", "service.update"]


@pytest.fixture
def audit() -> RecordingAuditSink:
    """The sink the app records to for the duration of a test."""
    return RecordingAuditSink()


@pytest_asyncio.fixture
async def api(db_session: AsyncSession, audit: RecordingAuditSink) -> AsyncGenerator[AsyncClient]:
    """An HTTP client sharing the test's rolled-back session and audit sink."""
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override
    application.dependency_overrides[get_audit_sink] = lambda: audit
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """A user holding every catalog permission."""
    user = await insert_user_with_permissions(db_session, hospital_id, ALL_SERVICE_PERMISSIONS)
    return auth_headers(user.id, hospital_id)


@pytest_asyncio.fixture
async def reader(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """A user holding only ``service.read``."""
    user = await insert_user_with_permissions(db_session, hospital_id, ["service.read"])
    return auth_headers(user.id, hospital_id)


@pytest_asyncio.fixture
async def other_tenant(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> dict[str, str]:
    """A fully-permissioned user in a different hospital."""
    user = await insert_user_with_permissions(
        db_session, other_hospital_id, ALL_SERVICE_PERMISSIONS
    )
    return auth_headers(user.id, other_hospital_id)


async def _create(api: AsyncClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    """Create a service through the API and return the data block."""
    response = await api.post(URL, json=build_service_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


class TestCreate:
    async def test_returns_201_with_the_service(
        self, api: AsyncClient, admin: dict[str, str], audit: RecordingAuditSink
    ) -> None:
        response = await api.post(
            URL, json=build_service_payload(code="ecg", price="450.00"), headers=admin
        )

        assert response.status_code == 201
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        assert data["code"] == "ECG"
        # Money is a decimal string, never a JSON number.
        assert data["price"] == "450.00"
        assert data["is_active"] is True
        assert "hospital_id" not in data
        assert audit.actions() == ["service.created"]
        assert str(audit.last().target_id) == data["id"]

    async def test_duplicate_code_returns_409(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        await _create(api, admin, code="ECG")

        response = await api.post(URL, json=build_service_payload(code="ecg"), headers=admin)

        assert response.status_code == 409
        assert response.json()["error_code"] == "RESOURCE_CONFLICT"

    @pytest.mark.parametrize(
        "overrides",
        [{"price": "-1.00"}, {"price": "1.999"}, {"code": "has space"}, {"name": "  "}],
    )
    async def test_invalid_body_returns_422(
        self, api: AsyncClient, admin: dict[str, str], overrides: dict[str, Any]
    ) -> None:
        response = await api.post(URL, json=build_service_payload(**overrides), headers=admin)

        assert response.status_code == 422
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    async def test_hospital_id_cannot_be_supplied(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await api.post(
            URL, json=build_service_payload(hospital_id=str(uuid.uuid4())), headers=admin
        )

        assert response.status_code == 422


class TestRead:
    async def test_get_returns_the_service(self, api: AsyncClient, admin: dict[str, str]) -> None:
        created = await _create(api, admin)

        response = await api.get(f"{URL}/{created['id']}", headers=admin)

        assert response.status_code == 200
        assert response.json()["data"] == created

    async def test_get_unknown_returns_404(self, api: AsyncClient, admin: dict[str, str]) -> None:
        response = await api.get(f"{URL}/{uuid.uuid4()}", headers=admin)

        assert response.status_code == 404
        assert response.json()["error_code"] == "RESOURCE_NOT_FOUND"

    async def test_list_is_paginated_and_ordered_by_name(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        await _create(api, admin, code="XRAY", name="X-ray")
        await _create(api, admin, code="ECG", name="ECG")
        await _create(api, admin, code="CONS", name="Consultation")

        response = await api.get(URL, params={"page_size": 2}, headers=admin)

        assert response.status_code == 200
        body = response.json()
        assert [item["name"] for item in body["data"]] == ["Consultation", "ECG"]
        assert body["metadata"]["pagination"] == {
            "page": 1,
            "page_size": 2,
            "total_records": 3,
            "total_pages": 2,
        }

    async def test_list_filters(self, api: AsyncClient, admin: dict[str, str]) -> None:
        await _create(api, admin, code="CONS", name="Consultation", category="Consultation")
        ecg = await _create(api, admin, code="ECG", name="ECG", category="Diagnostics")
        await api.patch(f"{URL}/{ecg['id']}", json={"is_active": False}, headers=admin)

        by_term = await api.get(URL, params={"q": "cons"}, headers=admin)
        by_category = await api.get(URL, params={"category": "Diagnostics"}, headers=admin)
        active = await api.get(URL, params={"is_active": "true"}, headers=admin)

        assert [item["code"] for item in by_term.json()["data"]] == ["CONS"]
        assert [item["code"] for item in by_category.json()["data"]] == ["ECG"]
        assert [item["code"] for item in active.json()["data"]] == ["CONS"]


class TestUpdate:
    async def test_patch_applies_the_change_and_audits(
        self, api: AsyncClient, admin: dict[str, str], audit: RecordingAuditSink
    ) -> None:
        created = await _create(api, admin, price="500.00")

        response = await api.patch(
            f"{URL}/{created['id']}", json={"price": "650.00"}, headers=admin
        )

        assert response.status_code == 200
        assert response.json()["data"]["price"] == "650.00"
        assert audit.actions() == ["service.created", "service.updated"]
        assert audit.last().changes == {"price": {"before": "500.00", "after": "650.00"}}

    async def test_code_cannot_be_changed(self, api: AsyncClient, admin: dict[str, str]) -> None:
        created = await _create(api, admin)

        response = await api.patch(f"{URL}/{created['id']}", json={"code": "NEW"}, headers=admin)

        assert response.status_code == 422

    async def test_patch_unknown_returns_404(self, api: AsyncClient, admin: dict[str, str]) -> None:
        response = await api.patch(f"{URL}/{uuid.uuid4()}", json={"price": "1.00"}, headers=admin)

        assert response.status_code == 404


class TestAuthorization:
    async def test_no_token_returns_401(self, api: AsyncClient) -> None:
        assert (await api.get(URL)).status_code == 401
        assert (await api.post(URL, json=build_service_payload())).status_code == 401

    async def test_reader_can_read_but_not_write(
        self, api: AsyncClient, admin: dict[str, str], reader: dict[str, str]
    ) -> None:
        created = await _create(api, admin)

        assert (await api.get(URL, headers=reader)).status_code == 200
        assert (await api.get(f"{URL}/{created['id']}", headers=reader)).status_code == 200

        create = await api.post(URL, json=build_service_payload(code="NEW"), headers=reader)
        update = await api.patch(f"{URL}/{created['id']}", json={"price": "1.00"}, headers=reader)

        assert create.status_code == 403
        assert update.status_code == 403
        assert create.json()["error_code"] == "PERMISSION_DENIED"

    async def test_a_user_with_no_permissions_cannot_read(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        user = await insert_user_with_permissions(db_session, hospital_id, [])

        response = await api.get(URL, headers=auth_headers(user.id, hospital_id))

        assert response.status_code == 403


class TestTenantIsolation:
    async def test_another_hospital_cannot_see_or_change_a_service(
        self, api: AsyncClient, admin: dict[str, str], other_tenant: dict[str, str]
    ) -> None:
        created = await _create(api, admin, code="ECG")

        get = await api.get(f"{URL}/{created['id']}", headers=other_tenant)
        patch = await api.patch(
            f"{URL}/{created['id']}", json={"price": "1.00"}, headers=other_tenant
        )
        listed = await api.get(URL, headers=other_tenant)

        # 404, not 403: a cross-tenant lookup must look exactly like a miss.
        assert get.status_code == 404
        assert patch.status_code == 404
        assert listed.json()["data"] == []
        # And it really was not changed.
        mine = await api.get(f"{URL}/{created['id']}", headers=admin)
        assert mine.json()["data"]["price"] == "500.00"

    async def test_another_hospital_may_reuse_a_code(
        self, api: AsyncClient, admin: dict[str, str], other_tenant: dict[str, str]
    ) -> None:
        await _create(api, admin, code="ECG")

        response = await api.post(URL, json=build_service_payload(code="ECG"), headers=other_tenant)

        assert response.status_code == 201
