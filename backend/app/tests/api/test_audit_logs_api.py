"""API tests for the Audit Logs endpoints (docs/modules/12-audit-logs.md §9).

Beyond the per-endpoint contract (§24 of the API standards), the load-bearing
test here is ``test_mutation_is_visible_in_trail``: it proves the sink swap in
``get_audit_sink`` actually persists entries end to end — a unit test with a
recording sink could not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.dependencies.db import get_db_session
from app.core.security import create_access_token
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.user import User
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
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"audit-api-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Audit",
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
async def reader(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Auth header for a user holding ``audit.read``."""
    user_id = await _make_user(db_session, hospital_id, ["audit.read"])
    return _auth_header(user_id, hospital_id)


@pytest_asyncio.fixture
async def exporter(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Auth header for a user holding ``audit.read`` + ``audit.export``."""
    user_id = await _make_user(db_session, hospital_id, ["audit.read", "audit.export"])
    return _auth_header(user_id, hospital_id)


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Auth header for a settings.update holder (drives the mutation under test)."""
    user_id = await _make_user(db_session, hospital_id, ["settings.read", "settings.update"])
    return _auth_header(user_id, hospital_id)


@pytest_asyncio.fixture
async def no_audit(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Auth header for an active user with no audit permissions."""
    user_id = await _make_user(db_session, hospital_id, [])
    return _auth_header(user_id, hospital_id)


async def _seed_entry(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
    action: str = "user.invited",
    created_at: datetime | None = None,
    target_type: str | None = "user",
) -> AuditLog:
    """Insert one audit row directly and flush it."""
    entry = AuditLog(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        actor_user_id=actor_id,
        actor_type="user" if actor_id else "system",
        action=action,
        target_type=target_type,
        target_id=uuid.uuid4(),
        before={"status": {"before": None, "after": "invited"}},
        after={"status": {"before": None, "after": "invited"}},
        context={"reason": "seed"},
    )
    if created_at is not None:
        entry.created_at = created_at
    session.add(entry)
    await session.flush()
    return entry


class TestListAuditLogs:
    """GET /audit-logs — audit.read."""

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        response = await api.get("/api/v1/audit-logs")
        assert response.status_code == 401

    async def test_permission_denied_without_audit_read(
        self, api: AsyncClient, no_audit: dict[str, str]
    ) -> None:
        response = await api.get("/api/v1/audit-logs", headers=no_audit)
        assert response.status_code == 403
        assert response.json()["error_code"] == "PERMISSION_DENIED"

    async def test_returns_entries_newest_first_with_actor_name(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        reader: dict[str, str],
    ) -> None:
        actor = await _make_user(db_session, hospital_id, [])
        old = await _seed_entry(
            db_session,
            hospital_id,
            actor_id=actor,
            action="user.updated",
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
        new = await _seed_entry(db_session, hospital_id, actor_id=actor, action="user.invited")

        response = await api.get("/api/v1/audit-logs", headers=reader)
        assert response.status_code == 200
        body = response.json()
        ids = [row["id"] for row in body["data"]]
        assert str(new.id) in ids and str(old.id) in ids
        # Newest first.
        assert ids.index(str(new.id)) < ids.index(str(old.id))

        row = next(r for r in body["data"] if r["id"] == str(new.id))
        assert row["actor_name"] == "Audit Tester"
        assert row["actor_email"].startswith("audit-api-")
        assert row["action"] == "user.invited"
        assert row["after"]["status"]["after"] == "invited"
        assert row["context"] == {"reason": "seed"}
        assert body["metadata"]["pagination"]["total_records"] >= 2

    async def test_action_filter_is_exact(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        reader: dict[str, str],
    ) -> None:
        await _seed_entry(db_session, hospital_id, action="user.invited")
        await _seed_entry(db_session, hospital_id, action="user.deactivated")

        response = await api.get(
            "/api/v1/audit-logs", headers=reader, params={"action": "user.deactivated"}
        )
        assert response.status_code == 200
        actions = {row["action"] for row in response.json()["data"]}
        assert actions == {"user.deactivated"}

    async def test_q_requires_at_least_three_characters(
        self, api: AsyncClient, reader: dict[str, str]
    ) -> None:
        response = await api.get("/api/v1/audit-logs", headers=reader, params={"q": "us"})
        assert response.status_code == 422

    async def test_q_matches_action_text(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        reader: dict[str, str],
    ) -> None:
        await _seed_entry(db_session, hospital_id, action="patient.created", target_type="patient")
        await _seed_entry(db_session, hospital_id, action="user.invited", target_type="user")

        response = await api.get("/api/v1/audit-logs", headers=reader, params={"q": "patient"})
        assert response.status_code == 200
        actions = {row["action"] for row in response.json()["data"]}
        assert actions == {"patient.created"}

    async def test_date_range_may_not_exceed_one_year(
        self, api: AsyncClient, reader: dict[str, str]
    ) -> None:
        response = await api.get(
            "/api/v1/audit-logs",
            headers=reader,
            params={
                "from": (datetime.now(UTC) - timedelta(days=400)).isoformat(),
                "to": datetime.now(UTC).isoformat(),
            },
        )
        assert response.status_code == 422

    async def test_inverted_date_range_is_rejected(
        self, api: AsyncClient, reader: dict[str, str]
    ) -> None:
        now = datetime.now(UTC)
        response = await api.get(
            "/api/v1/audit-logs",
            headers=reader,
            params={
                "from": now.isoformat(),
                "to": (now - timedelta(days=3)).isoformat(),
            },
        )
        assert response.status_code == 422

    async def test_other_tenants_entries_are_invisible(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        reader: dict[str, str],
    ) -> None:
        """Tenant isolation — the one bug class that would be catastrophic."""
        mine = await _seed_entry(db_session, hospital_id, action="user.invited")
        foreign = await _seed_entry(db_session, other_hospital_id, action="user.invited")

        response = await api.get("/api/v1/audit-logs", headers=reader)
        assert response.status_code == 200
        ids = {row["id"] for row in response.json()["data"]}
        assert str(mine.id) in ids
        assert str(foreign.id) not in ids


class TestGetAuditLog:
    """GET /audit-logs/{id} — audit.read."""

    async def test_returns_own_entry(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        reader: dict[str, str],
    ) -> None:
        entry = await _seed_entry(db_session, hospital_id)
        response = await api.get(f"/api/v1/audit-logs/{entry.id}", headers=reader)
        assert response.status_code == 200
        assert response.json()["data"]["id"] == str(entry.id)
        assert response.json()["data"]["before"]["status"]["before"] is None

    async def test_foreign_entry_reads_as_404(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        other_hospital_id: uuid.UUID,
        reader: dict[str, str],
    ) -> None:
        """A miss and a foreign row must look identical (§ rule 5)."""
        foreign = await _seed_entry(db_session, other_hospital_id)
        response = await api.get(f"/api/v1/audit-logs/{foreign.id}", headers=reader)
        assert response.status_code == 404

    async def test_unknown_id_is_404(self, api: AsyncClient, reader: dict[str, str]) -> None:
        response = await api.get(f"/api/v1/audit-logs/{uuid.uuid4()}", headers=reader)
        assert response.status_code == 404

    async def test_permission_denied_without_audit_read(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID, no_audit
    ) -> None:
        entry = await _seed_entry(db_session, hospital_id)
        response = await api.get(f"/api/v1/audit-logs/{entry.id}", headers=no_audit)
        assert response.status_code == 403


class TestExport:
    """GET /audit-logs/export — audit.export."""

    async def test_requires_export_permission(
        self, api: AsyncClient, reader: dict[str, str]
    ) -> None:
        """audit.read alone must not allow bulk extraction (§10)."""
        response = await api.get("/api/v1/audit-logs/export", headers=reader)
        assert response.status_code == 403

    async def test_csv_export_downloads(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        exporter: dict[str, str],
    ) -> None:
        entry = await _seed_entry(db_session, hospital_id, action="user.invited")
        response = await api.get(
            "/api/v1/audit-logs/export", headers=exporter, params={"format": "csv"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        assert "user.invited" in response.text
        assert str(entry.id) in response.text

    async def test_json_export_downloads(self, api: AsyncClient, exporter: dict[str, str]) -> None:
        response = await api.get(
            "/api/v1/audit-logs/export", headers=exporter, params={"format": "json"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert isinstance(response.json(), list)

    async def test_unknown_format_is_rejected(
        self, api: AsyncClient, exporter: dict[str, str]
    ) -> None:
        response = await api.get(
            "/api/v1/audit-logs/export", headers=exporter, params={"format": "xml"}
        )
        assert response.status_code == 422


class TestEndToEndAudit:
    """The sink swap: a real mutation must appear in the durable trail."""

    async def test_mutation_is_visible_in_trail(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        reader: dict[str, str],
        admin: dict[str, str],
    ) -> None:
        # settings.update holder performs a real, audited mutation.
        response = await api.patch(
            "/api/v1/hospitals/current",
            headers=admin,
            json={"name": "Audited Hospital"},
        )
        assert response.status_code == 200

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.hospital_id == hospital_id,
                AuditLog.action == "settings.hospital_updated",
            )
        )
        entry = result.scalars().one()
        assert entry.actor_user_id is not None

        # And the read path surfaces it with the actor resolved.
        listing = await api.get("/api/v1/audit-logs", headers=reader)
        assert listing.status_code == 200
        actions = [row["action"] for row in listing.json()["data"]]
        assert "settings.hospital_updated" in actions
