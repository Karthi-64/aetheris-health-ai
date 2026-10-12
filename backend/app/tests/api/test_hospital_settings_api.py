"""API tests for the Hospital Settings endpoints.

Real app, real service, real repository, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3).

Covers ``docs/modules/14-hospital-settings.md`` §9 plus the security
properties that matter most: who may read, who may write, that restricted
fields are refused, and that every write lands in the audit trail.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.ai.runtime import set_ai_runtime
from app.api.dependencies.db import get_db_session
from app.core.security import create_access_token
from app.main import create_app
from app.tests.ai_fakes import (
    FAKE_GROQ_KEY,
    FAST_MODEL,
    RecordingHandler,
    contains_any,
    groq_reply,
    make_runtime,
)
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
        assert entry.before is not None
        assert entry.after is not None
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

    async def test_explicit_null_for_required_fields_is_422(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        """PR #29 review finding 4: name/address/locale/settings are NOT NULL.

        ``model_dump(exclude_unset=True)`` keeps explicit nulls, so before the
        validator these reached the database, raised IntegrityError, and the
        caller saw a 500 with the session stuck in a failed transaction.
        """
        for body in (
            {"name": None},
            {"address": None},
            {"locale": None},
            {"settings": None},
        ):
            response = await api.patch("/api/v1/hospitals/current", headers=admin, json=body)
            assert response.status_code == 422, body

    async def test_explicit_null_clears_nullable_fields(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        """phone/email/logo_url are nullable, so null is the clear path (finding 5)."""
        await api.patch(
            "/api/v1/hospitals/current",
            headers=admin,
            json={"phone": "+919999000011", "email": "settings-clear@hospital.test"},
        )
        response = await api.patch(
            "/api/v1/hospitals/current",
            headers=admin,
            json={"phone": None, "email": None},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["phone"] is None
        assert data["email"] is None

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


class TestSettingsObjectIsShared:
    """``hospitals.settings`` is read by several modules, so a PATCH must not
    replace it wholesale, and must not let an admin set a feature flag
    (PR #29 re-review; module spec §4 rule 8, §10).
    """

    async def _settings(self, api: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
        response = await api.get("/api/v1/hospitals/current/full", headers=headers)
        return dict(response.json()["data"]["settings"])

    async def _preload(
        self, db_session: AsyncSession, hospital_id: uuid.UUID, settings: dict[str, Any]
    ) -> None:
        from app.models.hospital import Hospital

        hospital = await db_session.get(Hospital, hospital_id)
        assert hospital is not None
        hospital.settings = settings
        await db_session.flush()

    async def test_patch_merges_and_keeps_keys_it_was_not_sent(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        admin: dict[str, str],
    ) -> None:
        # Keys owned by other modules: appointments' grace period, a platform
        # feature flag, and billing's tax rate.
        await self._preload(
            db_session,
            hospital_id,
            {
                "no_show_grace_minutes": 45,
                "feature.ai.slot_recommendation": True,
                "billing": {"default_tax_rate": "18"},
            },
        )

        response = await api.patch(
            "/api/v1/hospitals/current",
            headers=admin,
            json={"settings": {"working_hours": {"mon": "09:00-17:00"}}},
        )

        assert response.status_code == 200, response.text
        assert await self._settings(api, admin) == {
            "no_show_grace_minutes": 45,
            "feature.ai.slot_recommendation": True,
            "billing": {"default_tax_rate": "18"},
            "working_hours": {"mon": "09:00-17:00"},
        }

    async def test_a_key_sent_as_null_is_removed(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        admin: dict[str, str],
    ) -> None:
        await self._preload(
            db_session, hospital_id, {"no_show_grace_minutes": 45, "working_hours": {"mon": "x"}}
        )

        response = await api.patch(
            "/api/v1/hospitals/current", headers=admin, json={"settings": {"working_hours": None}}
        )

        assert response.status_code == 200, response.text
        assert await self._settings(api, admin) == {"no_show_grace_minutes": 45}

    @pytest.mark.parametrize("value", [True, False, None])
    async def test_an_admin_cannot_set_or_remove_a_feature_flag(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        admin: dict[str, str],
        value: bool | None,
    ) -> None:
        await self._preload(db_session, hospital_id, {"feature.ai.slot_recommendation": False})

        response = await api.patch(
            "/api/v1/hospitals/current",
            headers=admin,
            json={
                "settings": {"feature.ai.slot_recommendation": value, "no_show_grace_minutes": 10}
            },
        )

        assert response.status_code == 422
        assert "platform administrator" in response.text
        # Nothing in the request was applied, including the legitimate key.
        assert await self._settings(api, admin) == {"feature.ai.slot_recommendation": False}


FLAGS_URL = "/api/v1/hospitals/current/feature-flags"
SLOT_FLAG = "feature.ai.slot_recommendation"


async def _store_settings(
    session: AsyncSession, hospital_id: uuid.UUID, settings: dict[str, Any]
) -> None:
    """Write ``hospitals.settings`` with a Core statement and read it back.

    Not an ORM assignment: Python treats ``1 == True``, so swapping one for the
    other through the ORM would look like no change and never reach the row.
    """
    from sqlalchemy import update

    from app.models.hospital import Hospital

    hospital = await session.get(Hospital, hospital_id)
    assert hospital is not None
    await session.execute(
        update(Hospital.__table__)  # type: ignore[arg-type]
        .where(Hospital.__table__.c.id == hospital_id)
        .values(settings=settings)
    )
    await session.refresh(hospital)


@pytest_asyncio.fixture
async def fake_groq() -> AsyncGenerator[RecordingHandler]:
    """Turn AI on over a fake transport, and report anything that reaches it."""
    handler = RecordingHandler(lambda _request: groq_reply("{}"))
    runtime = make_runtime(handler)
    set_ai_runtime(runtime)
    yield handler
    if runtime.provider is not None:
        await runtime.provider.aclose()


class TestFeatureFlags:
    """GET /hospitals/current/feature-flags — what the interface may offer.

    One boolean per known flag: the hospital has the feature *and* the server
    can serve it. It never says which is missing, and never calls a provider.
    """

    async def _flags(self, api: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
        response = await api.get(FLAGS_URL, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        assert body["message"] == "Feature flags retrieved."
        assert set(body["data"]) == {"flags"}
        return dict(body["data"]["flags"])

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        response = await api.get(FLAGS_URL)

        assert response.status_code == 401
        assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"

    async def test_garbage_token_is_rejected(self, api: AsyncClient) -> None:
        response = await api.get(FLAGS_URL, headers={"Authorization": "Bearer garbage"})

        assert response.status_code == 401

    async def test_a_user_with_no_permissions_can_read_it(
        self, api: AsyncClient, no_settings: dict[str, str]
    ) -> None:
        assert await self._flags(api, no_settings) == {SLOT_FLAG: {"available": False}}

    async def test_flag_absent_is_unavailable(
        self, api: AsyncClient, no_settings: dict[str, str], fake_groq: RecordingHandler
    ) -> None:
        """AI is configured on the server, but the hospital was not given the feature."""
        assert await self._flags(api, no_settings) == {SLOT_FLAG: {"available": False}}
        assert fake_groq.requests == []

    async def test_flag_on_without_server_ai_is_unavailable(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        no_settings: dict[str, str],
    ) -> None:
        await _store_settings(db_session, hospital_id, {SLOT_FLAG: True})

        assert await self._flags(api, no_settings) == {SLOT_FLAG: {"available": False}}

    async def test_flag_on_with_the_kill_switch_off_is_unavailable(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        no_settings: dict[str, str],
    ) -> None:
        await _store_settings(db_session, hospital_id, {SLOT_FLAG: True})
        set_ai_runtime(make_runtime(lambda _request: groq_reply("{}"), AI_ENABLED=False))

        assert await self._flags(api, no_settings) == {SLOT_FLAG: {"available": False}}

    async def test_flag_on_with_server_ai_is_available(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        no_settings: dict[str, str],
        fake_groq: RecordingHandler,
    ) -> None:
        await _store_settings(db_session, hospital_id, {SLOT_FLAG: True})

        assert await self._flags(api, no_settings) == {SLOT_FLAG: {"available": True}}
        # Configured is not reachable: reading the capability calls no provider.
        assert fake_groq.requests == []

    @pytest.mark.parametrize("stored", ["true", 1, "on", False, None, [True]])
    async def test_only_an_exact_true_counts(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        no_settings: dict[str, str],
        fake_groq: RecordingHandler,
        stored: Any,
    ) -> None:
        await _store_settings(db_session, hospital_id, {SLOT_FLAG: stored})

        assert await self._flags(api, no_settings) == {SLOT_FLAG: {"available": False}}

    async def test_the_two_unavailable_cases_are_indistinguishable(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        no_settings: dict[str, str],
    ) -> None:
        """A caller cannot tell "not enabled for us" from "server has no AI key"."""
        # Hospital flag on, server AI off.
        await _store_settings(db_session, hospital_id, {SLOT_FLAG: True})
        flag_on_ai_off = await api.get(FLAGS_URL, headers=no_settings)

        # Hospital flag off, server AI on.
        await _store_settings(db_session, hospital_id, {})
        runtime = make_runtime(lambda _request: groq_reply("{}"))
        set_ai_runtime(runtime)
        flag_off_ai_on = await api.get(FLAGS_URL, headers=no_settings)

        assert flag_on_ai_off.json()["data"] == flag_off_ai_on.json()["data"]
        assert set(flag_on_ai_off.json()["data"]["flags"][SLOT_FLAG]) == {"available"}
        assert "enabled" not in flag_on_ai_off.text

    async def test_only_known_flags_are_returned_never_other_settings(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        no_settings: dict[str, str],
        fake_groq: RecordingHandler,
    ) -> None:
        await _store_settings(
            db_session,
            hospital_id,
            {
                SLOT_FLAG: True,
                "billing.tax_rate": "18.00",
                "appointment.no_show_grace_minutes": 45,
                "feature.future.thing": True,
            },
        )

        response = await api.get(FLAGS_URL, headers=no_settings)

        assert response.json()["data"] == {"flags": {SLOT_FLAG: {"available": True}}}
        leaked = contains_any(
            [response.text],
            ["billing", "18.00", "no_show", "future", "groq", FAST_MODEL, FAKE_GROQ_KEY, "api."],
        )
        assert leaked is False

    async def test_another_hospitals_flag_does_not_change_this_ones_answer(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        no_settings: dict[str, str],
        fake_groq: RecordingHandler,
    ) -> None:
        await _store_settings(db_session, other_hospital_id, {SLOT_FLAG: True})
        other_user = await _make_user(db_session, other_hospital_id, [])

        mine = await self._flags(api, no_settings)
        theirs = await self._flags(api, _auth_header(other_user, other_hospital_id))

        assert mine == {SLOT_FLAG: {"available": False}}
        assert theirs == {SLOT_FLAG: {"available": True}}

    async def test_inactive_hospital_is_refused_at_authentication(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        no_settings: dict[str, str],
    ) -> None:
        """Staff of a deactivated hospital no longer authenticate at all.

        This used to reach the endpoint and get its 404. The request is now
        stopped earlier, in ``get_current_user``, exactly as for a deactivated
        account — so a switched-off hospital cannot use any endpoint.
        """
        from app.models.hospital import Hospital

        hospital = await db_session.get(Hospital, hospital_id)
        assert hospital is not None
        hospital.is_active = False
        await db_session.flush()

        response = await api.get(FLAGS_URL, headers=no_settings)

        assert response.status_code == 403
        assert response.json()["message"] == "Account is not active."
