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
from sqlalchemy import func, select

from app.api.dependencies.db import get_db_session
from app.core.security import create_access_token, hash_password
from app.main import create_app
from app.models.user import User, UserRole
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
        """The invite path grants permissions too — and returns the token."""
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
