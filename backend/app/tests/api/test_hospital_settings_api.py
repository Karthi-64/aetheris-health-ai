"""API tests for the Hospital Settings endpoints.

Real app, real service, real repository, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3).

Covers ``docs/modules/14-hospital-settings.md`` §9 plus the security
properties that matter most: who may read, who may write, that restricted
fields are refused, and that every write lands in the audit trail.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

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
        email=f"hosp-api-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Settings",
        last_name="Tester",
    )
    session.add(user)
    await session.flush()
    if permissions:
        await grant_permissions(
            session, hospital_id=hospital_id, user_id=user.id, codes=permissions
        )
    return user.id


def _auth_header(user_id: uuid.UUID, hospital_id: uuid.UUID | None) -> dict[str, str]:
    """Mint a Bearer header for a user."""
    token = create_access_token(user_id=user_id, hospital_id=hospital_id)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """An HTTP client whose requests share the test's rolled-back session."""
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Auth header for a user holding settings.read + settings.update."""
    user_id = await _make_user(db_session, hospital_id, ["settings.read", "settings.update"])
    return _auth_header(user_id, hospital_id)


@pytest_asyncio.fixture
async def reader(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Auth header for a user holding only settings.read."""
    user_id = await _make_user(db_session, hospital_id, ["settings.read"])
    return _auth_header(user_id, hospital_id)


@pytest_asyncio.fixture
async def no_settings(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Auth header for an active user with no settings permissions at all."""
    user_id = await _make_user(db_session, hospital_id, [])
    return _auth_header(user_id, hospital_id)


class TestGetCurrentHospital:
    """GET /hospitals/current — public fields for any authenticated user."""

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        response = await api.get("/api/v1/hospitals/current")
        assert response.status_code == 401

    async def test_any_active_user_can_read_public_fields(
        self, api: AsyncClient, no_settings: dict[str, str]
    ) -> None:
        response = await api.get("/api/v1/hospitals/current", headers=no_settings)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["name"].startswith("Test Hospital")
        assert data["slug"]
        assert "timezone" in data and "currency" in data
        # The public view must not leak internal configuration.
        assert "settings" not in data
        assert "tax_id" not in data

    async def test_platform_user_without_tenant_is_rejected(
        self, api: AsyncClient, db_session: AsyncSession
    ) -> None:
        """`users.hospital_id` is NOT NULL, so every account has a tenant.

        The schema forbids the platform-account shape entirely, which is
        stronger than a runtime guard: there is no user for whom `_tenant_of`
        can fire. Kept as a schema regression check — if the column ever
        becomes nullable, the tenant guard starts doing real work again.
        """
        from sqlalchemy import text

        result = await db_session.execute(
            text(
                "SELECT attnotnull FROM pg_attribute "
                "WHERE attrelid = 'users'::regclass AND attname = 'hospital_id'"
            )
        )
        assert result.scalar_one() is True


class TestGetFullSettings:
    """GET /hospitals/current/full — settings.read."""

    async def test_settings_read_holder_gets_every_field(
        self, api: AsyncClient, reader: dict[str, str]
    ) -> None:
        response = await api.get("/api/v1/hospitals/current/full", headers=reader)
        assert response.status_code == 200
        data = response.json()["data"]
        for key in ("name", "slug", "address", "timezone", "currency", "locale", "settings"):
            assert key in data

    async def test_user_without_permission_is_denied(
        self, api: AsyncClient, no_settings: dict[str, str]
    ) -> None:
        response = await api.get("/api/v1/hospitals/current/full", headers=no_settings)
        assert response.status_code == 403
        assert response.json()["error_code"] == "PERMISSION_DENIED"

    async def test_missing_auth_is_401(self, api: AsyncClient) -> None:
        response = await api.get("/api/v1/hospitals/current/full")
        assert response.status_code == 401


class TestUpdateSettings:
    """PATCH /hospitals/current — settings.update + audit."""

    async def test_update_persists_and_is_audited(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        admin: dict[str, str],
    ) -> None:
        response = await api.patch(
            "/api/v1/hospitals/current",
            headers=admin,
            json={"name": "Renamed Hospital", "phone": "+911234567890"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["name"] == "Renamed Hospital"
        assert data["phone"] == "+911234567890"

        # Read back through the API — the write is durable within the session.
        read_back = await api.get("/api/v1/hospitals/current/full", headers=admin)
        assert read_back.json()["data"]["name"] == "Renamed Hospital"

        # And it is in the audit trail with before/after values (rule 9).
        from sqlalchemy import select

        from app.models.audit_log import AuditLog

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.hospital_id == hospital_id,
                AuditLog.action == "settings.hospital_updated",
            )
        )
        entry = result.scalars().one()
        assert entry.target_type == "hospital"
        assert entry.target_id == hospital_id
        assert entry.actor_user_id is not None
        # The sink splits the AuditEvent diff into flat before/after maps.
        assert entry.before["name"].startswith("Test Hospital")
        assert entry.after["name"] == "Renamed Hospital"

    async def test_partial_update_leaves_other_fields_untouched(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        before = (await api.get("/api/v1/hospitals/current/full", headers=admin)).json()["data"]
        response = await api.patch(
            "/api/v1/hospitals/current", headers=admin, json={"phone": "+919999999999"}
        )
        after = response.json()["data"]
        assert after["phone"] == "+919999999999"
        assert after["name"] == before["name"]
        assert after["address"] == before["address"]

    async def test_restricted_fields_are_rejected_with_422(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        """slug/timezone/currency/tax_id are Superadmin-reserved (§9)."""
        for body in (
            {"timezone": "UTC"},
            {"currency": "USD"},
            {"slug": "new-slug"},
            {"tax_id": "HACKED"},
        ):
            response = await api.patch("/api/v1/hospitals/current", headers=admin, json=body)
            assert response.status_code == 422, body

        # Nothing changed.
        data = (await api.get("/api/v1/hospitals/current/full", headers=admin)).json()["data"]
        assert data["timezone"] != "UTC" or data["slug"] != "new-slug"

    async def test_unknown_fields_are_rejected_with_422(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await api.patch(
            "/api/v1/hospitals/current", headers=admin, json={"is_superuser": True}
        )
        assert response.status_code == 422

    async def test_invalid_values_are_rejected(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await api.patch(
            "/api/v1/hospitals/current", headers=admin, json={"email": "not-an-email"}
        )
        assert response.status_code == 422

    async def test_empty_patch_is_a_noop(self, api: AsyncClient, admin: dict[str, str]) -> None:
        response = await api.patch("/api/v1/hospitals/current", headers=admin, json={})
        assert response.status_code == 200

    async def test_user_without_permission_is_denied(
        self, api: AsyncClient, no_settings: dict[str, str]
    ) -> None:
        response = await api.patch(
            "/api/v1/hospitals/current", headers=no_settings, json={"name": "Nope Hospital"}
        )
        assert response.status_code == 403

    async def test_reader_cannot_write(self, api: AsyncClient, reader: dict[str, str]) -> None:
        response = await api.patch(
            "/api/v1/hospitals/current", headers=reader, json={"name": "Nope Hospital"}
        )
        assert response.status_code == 403

    async def test_missing_auth_is_401(self, api: AsyncClient) -> None:
        response = await api.patch("/api/v1/hospitals/current", json={"name": "Nope Hospital"})
        assert response.status_code == 401
