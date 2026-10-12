"""API tests for the user management endpoints.

Focused on the two contract failures that unit tests could not see: writes that
reported success without persisting, and a list response whose shape did not
match ``docs/06-API_STANDARDS.md`` §5.2.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.api.dependencies.db import get_db_session
from app.core.config import settings
from app.core.security import create_access_token, hash_password
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User, UserRole, UserStatus
from app.repositories.notification_repository import NotificationRepository
from app.tests.conftest import grant_permissions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

USER_PERMISSIONS = ["user.read", "user.create", "user.update", "user.deactivate"]


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """HTTP client sharing the test's rolled-back session."""
    application = create_app()

    async def _override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _override
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, Any]:
    """An admin holding the user-management permissions."""
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"admin-{uuid.uuid4().hex[:12]}@hospital.example",
        password_hash=hash_password("Str0ng!Passw0rd123"),
        first_name="Admin",
        last_name="User",
    )
    db_session.add(user)
    await db_session.flush()
    await grant_permissions(
        db_session, hospital_id=hospital_id, user_id=user.id, codes=USER_PERMISSIONS
    )
    token = create_access_token(user_id=user.id, hospital_id=hospital_id)
    return {"id": user.id, "headers": {"Authorization": f"Bearer {token}"}}


class TestInviteUserPersistence:
    """``POST /api/v1/users``."""

    async def test_inviting_a_user_actually_persists_the_row(
        self, api: AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
    ) -> None:
        # Regression: this returned 201 Created with a generated id for a user
        # that was never written, because no service in the module committed.
        email = f"invitee-{uuid.uuid4().hex[:12]}@hospital.example"

        response = await api.post(
            "/api/v1/users",
            json={"email": email, "first_name": "New", "last_name": "Nurse"},
            headers=admin["headers"],
        )

        assert response.status_code == 201, response.text
        stored = await db_session.execute(
            select(func.count()).select_from(User).where(User.email == email)
        )
        assert stored.scalar_one() == 1, "API reported success but no row was written"

    async def test_the_returned_id_refers_to_a_real_row(
        self, api: AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
    ) -> None:
        response = await api.post(
            "/api/v1/users",
            json={
                "email": f"invitee-{uuid.uuid4().hex[:12]}@hospital.example",
                "first_name": "New",
                "last_name": "Nurse",
            },
            headers=admin["headers"],
        )

        new_id = uuid.UUID(response.json()["data"]["id"])
        found = await db_session.execute(select(User).where(User.id == new_id))
        assert found.unique().scalar_one_or_none() is not None

    async def test_inviting_without_permission_returns_403(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        plain = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"plain-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Plain",
            last_name="User",
        )
        db_session.add(plain)
        await db_session.flush()
        await grant_permissions(
            db_session, hospital_id=hospital_id, user_id=plain.id, codes=["user.read"]
        )
        token = create_access_token(user_id=plain.id, hospital_id=hospital_id)

        response = await api.post(
            "/api/v1/users",
            json={
                "email": "x@hospital.example",
                "first_name": "X",
                "last_name": "Y",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403

    async def test_inviting_without_a_token_returns_401(self, api: AsyncClient) -> None:
        response = await api.post(
            "/api/v1/users",
            json={"email": "x@hospital.example", "first_name": "X", "last_name": "Y"},
        )

        assert response.status_code == 401


class TestInvitationContract:
    """``POST /api/v1/users`` and ``POST /api/v1/users/{id}/invitation``.

    The activation link is a credential for the invited person's account. The
    API tells the administrator what became of the email and nothing else.
    The attacks on this are in ``integration/test_invitation_delivery.py``;
    these pin the response shape the frontend is written against.
    """

    @pytest.fixture(autouse=True)
    def _mail_transport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.hospital.example")

    async def _invite(self, api: AsyncClient, admin: dict[str, Any]) -> dict[str, Any]:
        response = await api.post(
            "/api/v1/users",
            json={
                "email": f"invitee-{uuid.uuid4().hex[:12]}@hospital.example",
                "first_name": "New",
                "last_name": "Nurse",
            },
            headers=admin["headers"],
        )
        assert response.status_code == 201, response.text
        data: dict[str, Any] = response.json()["data"]
        return data

    async def test_the_invite_response_has_a_delivery_state_and_no_token(
        self, api: AsyncClient, admin: dict[str, Any]
    ) -> None:
        data = await self._invite(api, admin)

        assert "invite_token" not in data, "the activation token is back in the response"
        assert not [key for key in data if "token" in key.lower()]
        assert data["invitation"] == {"delivery": "queued"}
        assert data["status"] == "invited"

    async def test_without_a_mail_transport_the_invite_says_unavailable(
        self, api: AsyncClient, admin: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "SMTP_HOST", None)

        data = await self._invite(api, admin)

        assert data["invitation"] == {"delivery": "unavailable"}
        assert "invite_token" not in data
        assert data["status"] == "invited"

    async def test_resending_returns_only_the_delivery_state(
        self, api: AsyncClient, admin: dict[str, Any]
    ) -> None:
        invited = await self._invite(api, admin)

        response = await api.post(
            f"/api/v1/users/{invited['id']}/invitation", headers=admin["headers"]
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"delivery": "queued"}
        assert response.json()["message"] == "Invitation queued."

    async def test_resending_is_audited_with_the_actor_and_without_the_link(
        self, api: AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
    ) -> None:
        invited = await self._invite(api, admin)

        await api.post(f"/api/v1/users/{invited['id']}/invitation", headers=admin["headers"])

        rows = await db_session.execute(
            select(AuditLog).where(
                AuditLog.target_id == uuid.UUID(invited["id"]),
                AuditLog.action == "user.invitation_resent",
            )
        )
        [entry] = rows.scalars().all()
        assert entry.actor_user_id == admin["id"]
        assert entry.target_type == "user"
        assert not entry.before
        assert not entry.after
        assert not entry.context

    @staticmethod
    def _break_the_email_queue(patch: pytest.MonkeyPatch) -> None:
        """Make the insert into the email queue fail, as an outage would."""

        async def _insert_fails(self: NotificationRepository, **values: Any) -> None:
            msg = "the queue is unavailable"
            raise RuntimeError(msg)

        patch.setattr(NotificationRepository, "create_email_delivery", _insert_fails)

    @staticmethod
    async def _live_tokens(db_session: AsyncSession, user_id: str) -> int:
        result = await db_session.execute(
            select(func.count())
            .select_from(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == uuid.UUID(user_id),
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > func.now(),
            )
        )
        return int(result.scalar_one())

    async def test_an_invite_whose_email_could_not_be_queued_says_unavailable(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: the administrator is told "queued" for an email that does not exist.

        Email is configured but the queue refused the message. The account is
        created, the answer is ``unavailable``, and no link is left alive.
        """
        self._break_the_email_queue(monkeypatch)

        data = await self._invite(api, admin)

        assert data["invitation"] == {"delivery": "unavailable"}
        assert data["status"] == "invited"
        assert not [key for key in data if "token" in key.lower()]
        assert await self._live_tokens(db_session, data["id"]) == 0

    async def test_a_resend_whose_email_could_not_be_queued_says_unavailable(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same honesty on a resend: 200, ``unavailable``, and nothing audited as sent."""
        invited = await self._invite(api, admin)
        self._break_the_email_queue(monkeypatch)

        response = await api.post(
            f"/api/v1/users/{invited['id']}/invitation", headers=admin["headers"]
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"delivery": "unavailable"}
        assert response.json()["message"] != "Invitation queued."
        assert response.json()["message"].startswith("No invitation was sent")
        # The link already in the invited person's mailbox is the one left alive.
        assert await self._live_tokens(db_session, invited["id"]) == 1
        resent = await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.target_id == uuid.UUID(invited["id"]),
                AuditLog.action == "user.invitation_resent",
            )
        )
        assert resent.scalar_one() == 0

    async def test_resending_without_user_create_is_403(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        admin: dict[str, Any],
    ) -> None:
        invited = await self._invite(api, admin)
        reader = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"reader-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash="test-placeholder-not-a-hash",
            first_name="Read",
            last_name="Only",
        )
        db_session.add(reader)
        await db_session.flush()
        await grant_permissions(
            db_session, hospital_id=hospital_id, user_id=reader.id, codes=["user.read"]
        )
        token = create_access_token(user_id=reader.id, hospital_id=hospital_id)

        response = await api.post(
            f"/api/v1/users/{invited['id']}/invitation",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403

    async def test_resending_to_a_user_who_is_not_invited_is_409(
        self, api: AsyncClient, admin: dict[str, Any]
    ) -> None:
        # The admin is an active account: there is no invitation to send again.
        response = await api.post(
            f"/api/v1/users/{admin['id']}/invitation", headers=admin["headers"]
        )

        assert response.status_code == 409, response.text

    async def test_reactivating_a_never_activated_user_reports_invited(
        self, api: AsyncClient, admin: dict[str, Any]
    ) -> None:
        # Not "active": nobody has set a password on this account yet.
        invited = await self._invite(api, admin)
        await api.post(f"/api/v1/users/{invited['id']}/deactivate", headers=admin["headers"])

        response = await api.post(
            f"/api/v1/users/{invited['id']}/reactivate", headers=admin["headers"]
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "invited"

    @pytest.mark.parametrize(
        ("endpoint", "action"),
        [("deactivate", "user.deactivated"), ("reactivate", "user.reactivated")],
    )
    async def test_suspending_and_reactivating_each_leave_no_live_link_and_one_audit_entry(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, Any],
        endpoint: str,
        action: str,
    ) -> None:
        """Attack: a link issued before the change still works after it.

        Both operations kill every outstanding link themselves, and record
        who did it, in the same transaction as the status change.
        """
        invited = await self._invite(api, admin)
        user_id = uuid.UUID(invited["id"])
        if endpoint == "reactivate":
            await db_session.execute(
                update(User).where(User.id == user_id).values(status=UserStatus.SUSPENDED)
            )
        assert await self._live_tokens(db_session, invited["id"]) == 1, "premise: a live link"

        response = await api.post(
            f"/api/v1/users/{invited['id']}/{endpoint}", headers=admin["headers"]
        )

        assert response.status_code == 200, response.text
        assert await self._live_tokens(db_session, invited["id"]) == 0
        rows = await db_session.execute(
            select(AuditLog).where(AuditLog.target_id == user_id, AuditLog.action == action)
        )
        [entry] = rows.scalars().all()
        assert entry.actor_user_id == admin["id"]


class TestListUsersEnvelope:
    """``GET /api/v1/users`` — ``docs/06-API_STANDARDS.md`` §5.2."""

    async def test_data_is_the_array_of_records(
        self, api: AsyncClient, admin: dict[str, Any]
    ) -> None:
        # Regression: `data` was the whole result dict, so the records were
        # nested under data.items and the counts appeared twice.
        response = await api.get("/api/v1/users", headers=admin["headers"])

        body = response.json()
        assert response.status_code == 200
        assert isinstance(body["data"], list), "data must be the array of records"

    async def test_pagination_lives_in_metadata(
        self, api: AsyncClient, admin: dict[str, Any]
    ) -> None:
        response = await api.get("/api/v1/users", headers=admin["headers"])

        pagination = response.json()["metadata"]["pagination"]
        assert set(pagination) == {"page", "page_size", "total_records", "total_pages"}
        assert pagination["total_records"] >= 1

    async def test_unauthenticated_errors_use_the_standard_envelope(self, api: AsyncClient) -> None:
        response = await api.get("/api/v1/users")

        body = response.json()
        assert response.status_code == 401
        assert body["success"] is False
        assert body["error_code"] == "AUTHENTICATION_REQUIRED"
        assert "detail" not in body


class TestCrossTenantIsolation:
    """B1: every admin write endpoint 404s on another hospital's user UUID.

    The handoff demands a test per endpoint asserting a 404 for a cross-tenant
    UUID — an admin at Hospital A must not deactivate, reset, or re-role a
    user at Hospital B, even with a known UUID.
    """

    ALL_PERMISSIONS = [
        "user.read",
        "user.create",
        "user.update",
        "user.deactivate",
        "user.reset_password",
        "role.assign",
    ]

    @pytest_asyncio.fixture
    async def admin(self, db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, Any]:
        """An admin holding every permission the endpoints below check."""
        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"admin-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Admin",
            last_name="User",
        )
        db_session.add(user)
        await db_session.flush()
        await grant_permissions(
            db_session, hospital_id=hospital_id, user_id=user.id, codes=self.ALL_PERMISSIONS
        )
        token = create_access_token(user_id=user.id, hospital_id=hospital_id)
        return {"id": user.id, "headers": {"Authorization": f"Bearer {token}"}}

    @pytest_asyncio.fixture
    async def foreign_user(
        self, db_session: AsyncSession, other_hospital_id: uuid.UUID
    ) -> uuid.UUID:
        """A user who belongs to a hospital the admin has no access to.

        ``other_hospital_id`` is a real hospital row — the ``users.hospital_id``
        foreign key is NOT NULL with ``ON DELETE RESTRICT``, so a random UUID
        would raise an IntegrityError instead of exercising the tenant check.
        """
        user = User(
            id=uuid.uuid4(),
            hospital_id=other_hospital_id,
            email=f"foreign-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Foreign",
            last_name="User",
        )
        db_session.add(user)
        await db_session.flush()
        return user.id

    @pytest_asyncio.fixture
    async def any_role(self, db_session: AsyncSession, hospital_id: uuid.UUID) -> uuid.UUID:
        """A role id for the role endpoints (the user 404 must fire first)."""
        from app.models.role import Role

        role = Role(id=uuid.uuid4(), hospital_id=hospital_id, name=f"Role {uuid.uuid4().hex[:8]}")
        db_session.add(role)
        await db_session.flush()
        return role.id

    async def test_get_user_foreign_uuid_is_404(
        self, api: AsyncClient, admin: dict[str, Any], foreign_user: uuid.UUID
    ) -> None:
        response = await api.get(f"/api/v1/users/{foreign_user}", headers=admin["headers"])
        assert response.status_code == 404

    async def test_deactivate_foreign_uuid_is_404(
        self, api: AsyncClient, admin: dict[str, Any], foreign_user: uuid.UUID
    ) -> None:
        response = await api.post(
            f"/api/v1/users/{foreign_user}/deactivate", headers=admin["headers"]
        )
        assert response.status_code == 404

    async def test_reactivate_foreign_uuid_is_404(
        self, api: AsyncClient, admin: dict[str, Any], foreign_user: uuid.UUID
    ) -> None:
        response = await api.post(
            f"/api/v1/users/{foreign_user}/reactivate", headers=admin["headers"]
        )
        assert response.status_code == 404

    async def test_resend_invitation_foreign_uuid_is_404(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, Any],
        foreign_user: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.hospital.example")
        foreign = await db_session.get(User, foreign_user)
        assert foreign is not None
        foreign.status = UserStatus.INVITED
        await db_session.flush()

        response = await api.post(
            f"/api/v1/users/{foreign_user}/invitation", headers=admin["headers"]
        )

        assert response.status_code == 404
        minted = await db_session.execute(
            select(func.count())
            .select_from(PasswordResetToken)
            .where(PasswordResetToken.user_id == foreign_user)
        )
        assert minted.scalar_one() == 0

    async def test_admin_reset_foreign_uuid_is_404(
        self, api: AsyncClient, admin: dict[str, Any], foreign_user: uuid.UUID
    ) -> None:
        response = await api.post(
            f"/api/v1/users/{foreign_user}/reset-password", headers=admin["headers"]
        )
        assert response.status_code == 404

    async def test_get_roles_foreign_uuid_is_404(
        self, api: AsyncClient, admin: dict[str, Any], foreign_user: uuid.UUID
    ) -> None:
        response = await api.get(f"/api/v1/users/{foreign_user}/roles", headers=admin["headers"])
        assert response.status_code == 404

    async def test_assign_role_foreign_uuid_is_404(
        self,
        api: AsyncClient,
        admin: dict[str, Any],
        foreign_user: uuid.UUID,
        any_role: uuid.UUID,
    ) -> None:
        response = await api.post(
            f"/api/v1/users/{foreign_user}/roles",
            json={"role_id": str(any_role)},
            headers=admin["headers"],
        )
        assert response.status_code == 404

    async def test_remove_role_foreign_uuid_is_404(
        self,
        api: AsyncClient,
        admin: dict[str, Any],
        foreign_user: uuid.UUID,
        any_role: uuid.UUID,
    ) -> None:
        response = await api.delete(
            f"/api/v1/users/{foreign_user}/roles/{any_role}", headers=admin["headers"]
        )
        assert response.status_code == 404

    async def test_assign_foreign_hospitals_role_is_404(
        self,
        api: AsyncClient,
        admin: dict[str, Any],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """A role belonging to another hospital is invisible to this tenant."""
        from app.models.role import Role

        foreign_role = Role(
            id=uuid.uuid4(), hospital_id=other_hospital_id, name=f"Theirs {uuid.uuid4().hex[:8]}"
        )
        db_session.add(foreign_role)

        invitee = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"invitee-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Local",
            last_name="Invitee",
        )
        db_session.add(invitee)
        await db_session.flush()

        response = await api.post(
            f"/api/v1/users/{invitee.id}/roles",
            json={"role_id": str(foreign_role.id)},
            headers=admin["headers"],
        )
        assert response.status_code == 404

    async def test_assign_system_role_succeeds(
        self,
        api: AsyncClient,
        admin: dict[str, Any],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        """The allowed case: a system role (tenantless) can be assigned."""
        from app.models.role import Role

        system_role = Role(
            id=uuid.uuid4(),
            hospital_id=None,
            name=f"System {uuid.uuid4().hex[:8]}",
            is_system=True,
        )
        db_session.add(system_role)

        invitee = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"invitee-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Local",
            last_name="Invitee",
        )
        db_session.add(invitee)
        await db_session.flush()

        response = await api.post(
            f"/api/v1/users/{invitee.id}/roles",
            json={"role_id": str(system_role.id)},
            headers=admin["headers"],
        )
        assert response.status_code == 200, response.text


class TestRoleAssignmentAuthorization:
    """Authorization edges around role assignment (module spec §4 rule 8, AC-3).

    The dependency already gates on ``role.assign``; these tests pin the
    allowed/denied matrix end-to-end so a future refactor cannot quietly swap
    the gate for a role-name check.
    """

    @pytest_asyncio.fixture
    async def invitee(self, db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, Any]:
        """A role-less invited user to operate on."""
        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"invitee-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Invited",
            last_name="User",
        )
        db_session.add(user)
        await db_session.flush()
        return {"id": user.id}

    @pytest_asyncio.fixture
    async def system_role(self, db_session: AsyncSession) -> uuid.UUID:
        """A system role whose id can be assigned by an authorized actor."""
        from app.models.role import Role

        role = Role(
            id=uuid.uuid4(), hospital_id=None, name=f"System {uuid.uuid4().hex[:8]}", is_system=True
        )
        db_session.add(role)
        await db_session.flush()
        return role.id

    async def test_actor_with_role_assign_can_assign(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        invitee: dict[str, Any],
        system_role: uuid.UUID,
    ) -> None:
        """Allowed: an actor holding ``role.assign`` may assign a visible role."""
        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"assigner-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Role",
            last_name="Assigner",
        )
        db_session.add(user)
        await db_session.flush()
        await grant_permissions(
            db_session, hospital_id=hospital_id, user_id=user.id, codes=["role.assign"]
        )
        token = create_access_token(user_id=user.id, hospital_id=hospital_id)

        response = await api.post(
            f"/api/v1/users/{invitee['id']}/roles",
            json={"role_id": str(system_role)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text

        rows = await db_session.execute(
            select(UserRole).where(
                UserRole.user_id == invitee["id"], UserRole.role_id == system_role
            )
        )
        assert rows.unique().scalar_one_or_none() is not None

    async def test_actor_without_role_assign_is_403(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        invitee: dict[str, Any],
        system_role: uuid.UUID,
    ) -> None:
        """Denied: full user-management rights minus ``role.assign`` still 403."""
        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"noassign-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Cannot",
            last_name="Assign",
        )
        db_session.add(user)
        await db_session.flush()
        await grant_permissions(
            db_session,
            hospital_id=hospital_id,
            user_id=user.id,
            codes=["user.read", "user.update"],
        )
        token = create_access_token(user_id=user.id, hospital_id=hospital_id)

        response = await api.post(
            f"/api/v1/users/{invitee['id']}/roles",
            json={"role_id": str(system_role)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_unauthenticated_assignment_is_401(
        self, api: AsyncClient, invitee: dict[str, Any], system_role: uuid.UUID
    ) -> None:
        response = await api.post(
            f"/api/v1/users/{invitee['id']}/roles", json={"role_id": str(system_role)}
        )
        assert response.status_code == 401


class TestLoginPayloadCarriesPermissions:
    """The SPA builds its RBAC navigation from the login response's user object."""

    async def test_login_user_payload_includes_permission_codes(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        """``user.permissions`` mirrors the codes enforced by require_permission."""
        from httpx import ASGITransport

        from app.main import create_app

        password = "Str0ng!Passw0rd123"
        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"login-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password(password),
            first_name="Permy",
            last_name="User",
            password_changed_at=datetime.now(UTC),
        )
        db_session.add(user)
        await db_session.flush()
        await grant_permissions(
            db_session,
            hospital_id=hospital_id,
            user_id=user.id,
            codes=["patient.read", "user.read"],
        )

        application = create_app()

        async def _override() -> AsyncGenerator[AsyncSession]:
            yield db_session

        application.dependency_overrides[get_db_session] = _override
        client = AsyncClient(transport=ASGITransport(app=application), base_url="http://test")
        try:
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": user.email, "password": password},
            )
        finally:
            await client.aclose()
            application.dependency_overrides.clear()

        assert login.status_code == 200, login.text
        profile = login.json()["data"]["user"]
        assert set(profile["permissions"]) == {"patient.read", "user.read"}
        assert profile["name"] == "Permy User"


class TestPrivilegeEscalationOverHttp:
    """BR-8 / FR-3 / AC-3 at the HTTP boundary.

    The service-level rule is unit-tested; these pin the status codes and the
    persistence outcome, because the reported hole was reachable with nothing
    but a valid admin token.
    """

    @pytest_asyncio.fixture
    async def privileged_role(self, db_session: AsyncSession) -> uuid.UUID:
        """A system role granting a permission the test actors do not hold."""
        from app.models.permission import Permission
        from app.models.role import Role, RolePermission

        existing = await db_session.execute(
            select(Permission).where(Permission.code == "pharmacy.dispense")
        )
        permission = existing.unique().scalar_one_or_none()
        if permission is None:
            permission = Permission(
                id=uuid.uuid4(),
                code="pharmacy.dispense",
                module="pharmacy",
                description="Dispense medication",
            )
            db_session.add(permission)
            await db_session.flush()

        role = Role(
            id=uuid.uuid4(),
            hospital_id=None,
            name=f"Pharmacist {uuid.uuid4().hex[:8]}",
            is_system=True,
        )
        db_session.add(role)
        await db_session.flush()
        db_session.add(
            RolePermission(id=uuid.uuid4(), role_id=role.id, permission_id=permission.id)
        )
        await db_session.flush()
        return role.id

    @pytest_asyncio.fixture
    async def assigner(self, db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, Any]:
        """An actor holding ``role.assign`` but no clinical permissions."""
        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"assigner-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Limited",
            last_name="Assigner",
        )
        db_session.add(user)
        await db_session.flush()
        await grant_permissions(
            db_session,
            hospital_id=hospital_id,
            user_id=user.id,
            codes=["role.assign", "user.create", "user.read"],
        )
        token = create_access_token(user_id=user.id, hospital_id=hospital_id)
        return {"id": user.id, "headers": {"Authorization": f"Bearer {token}"}}

    async def test_self_assignment_of_a_richer_role_is_403(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        assigner: dict[str, Any],
        privileged_role: uuid.UUID,
    ) -> None:
        """The reported hole: granting yourself permissions you lack."""
        response = await api.post(
            f"/api/v1/users/{assigner['id']}/roles",
            json={"role_id": str(privileged_role)},
            headers=assigner["headers"],
        )
        assert response.status_code == 403, response.text

        rows = await db_session.execute(
            select(UserRole).where(
                UserRole.user_id == assigner["id"], UserRole.role_id == privileged_role
            )
        )
        assert rows.unique().scalar_one_or_none() is None, "the role must not have been written"

    async def test_inviting_into_a_richer_role_is_403(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        assigner: dict[str, Any],
        privileged_role: uuid.UUID,
    ) -> None:
        """The invite path grants permissions too.

        (It used to return the new account's activation token as well, which
        made this the quickest route to a privileged account. It no longer
        does — see ``TestInvitationContract`` — but the grant check stands on
        its own.)
        """
        email = f"escalate-{uuid.uuid4().hex[:12]}@hospital.example"
        response = await api.post(
            "/api/v1/users",
            json={
                "email": email,
                "first_name": "Priv",
                "last_name": "Escalator",
                "role_ids": [str(privileged_role)],
            },
            headers=assigner["headers"],
        )
        assert response.status_code == 403, response.text

        count = await db_session.execute(
            select(func.count()).select_from(User).where(User.email == email)
        )
        assert count.scalar_one() == 0, "the invite must be all-or-nothing"

    async def test_assignment_records_who_granted_the_role(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        assigner: dict[str, Any],
    ) -> None:
        """BR-9: ``user_roles`` records who granted the role, and when.

        The spec calls the timestamp ``assigned_at``; the table serves it from
        the ``TimestampMixin``'s ``created_at``, since the row exists only to
        record the assignment.
        """
        from app.models.role import Role

        target = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"target-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Grant",
            last_name="Target",
        )
        db_session.add(target)
        role = Role(
            id=uuid.uuid4(),
            hospital_id=None,
            name=f"Empty {uuid.uuid4().hex[:8]}",
            is_system=True,
        )
        db_session.add(role)
        await db_session.flush()

        response = await api.post(
            f"/api/v1/users/{target.id}/roles",
            json={"role_id": str(role.id)},
            headers=assigner["headers"],
        )
        assert response.status_code == 200, response.text

        rows = await db_session.execute(
            select(UserRole).where(UserRole.user_id == target.id, UserRole.role_id == role.id)
        )
        user_role = rows.unique().scalar_one()
        assert user_role.assigned_by == assigner["id"]
        assert user_role.created_at is not None


class TestLastAdministratorLockout:
    """Module spec §14: the hospital must keep at least one administrator."""

    async def test_removing_the_only_admin_role_is_blocked(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The sole holder of ``role.assign`` cannot give the role up."""

        admin_user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"sole-admin-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Sole",
            last_name="Admin",
        )
        db_session.add(admin_user)
        await db_session.flush()
        await grant_permissions(
            db_session,
            hospital_id=hospital_id,
            user_id=admin_user.id,
            codes=["role.assign", "user.read"],
        )
        token = create_access_token(user_id=admin_user.id, hospital_id=hospital_id)

        rows = await db_session.execute(select(UserRole).where(UserRole.user_id == admin_user.id))
        admin_role_id = rows.unique().scalars().one().role_id

        response = await api.delete(
            f"/api/v1/users/{admin_user.id}/roles/{admin_role_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400, response.text
        assert "last administrator" in response.text

        still_there = await db_session.execute(
            select(UserRole).where(
                UserRole.user_id == admin_user.id, UserRole.role_id == admin_role_id
            )
        )
        assert still_there.unique().scalar_one_or_none() is not None


class TestClearingThePhoneNumber:
    """``PATCH /api/v1/users/{id}`` and ``PATCH /api/v1/users/me``.

    PR #29 review finding 9: the UI sends ``phone: null`` to clear a number,
    but the service used to drop every ``None``, so the old number stayed and
    the response still said the update succeeded.
    """

    @pytest_asyncio.fixture
    async def member(self, db_session: AsyncSession, hospital_id: uuid.UUID) -> User:
        """A user in the admin's hospital who has a phone number."""
        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"member-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password("Str0ng!Passw0rd123"),
            first_name="Meera",
            last_name="Nair",
            phone="+919812000199",
        )
        db_session.add(user)
        await db_session.flush()
        return user

    async def test_an_explicit_null_clears_the_number(
        self, api: AsyncClient, db_session: AsyncSession, admin: dict[str, Any], member: User
    ) -> None:
        response = await api.patch(
            f"/api/v1/users/{member.id}", json={"phone": None}, headers=admin["headers"]
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["phone"] is None
        await db_session.refresh(member)
        assert member.phone is None

    async def test_an_omitted_phone_is_left_alone(
        self, api: AsyncClient, db_session: AsyncSession, admin: dict[str, Any], member: User
    ) -> None:
        # The other half of the same rule: only an *explicit* null clears.
        response = await api.patch(
            f"/api/v1/users/{member.id}", json={"last_name": "Menon"}, headers=admin["headers"]
        )

        assert response.status_code == 200, response.text
        await db_session.refresh(member)
        assert member.last_name == "Menon"
        assert member.phone == "+919812000199"

    async def test_a_null_name_does_not_blank_a_required_column(
        self, api: AsyncClient, db_session: AsyncSession, admin: dict[str, Any], member: User
    ) -> None:
        response = await api.patch(
            f"/api/v1/users/{member.id}",
            json={"first_name": None, "phone": None},
            headers=admin["headers"],
        )

        assert response.status_code == 200, response.text
        await db_session.refresh(member)
        assert member.first_name == "Meera"
        assert member.phone is None

    async def test_a_user_can_clear_their_own_number(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID, member: User
    ) -> None:
        token = create_access_token(user_id=member.id, hospital_id=hospital_id)

        response = await api.patch(
            "/api/v1/users/me", json={"phone": None}, headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200, response.text
        await db_session.refresh(member)
        assert member.phone is None
