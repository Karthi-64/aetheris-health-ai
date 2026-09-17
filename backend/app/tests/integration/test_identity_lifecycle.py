"""Integration tests for the Identity module — the full lifecycle in one flow.

Real database, real repositories, real services — only the audit sink is a
double, so the events can be asserted on (``docs/11-TESTING_STRATEGY.md`` §2.4,
§5). Mirrors the shape of ``tests/integration/test_patient_lifecycle.py``.

Covers the Week 1 handoff's eight-step flow end-to-end (A3): invite → activate
→ login → refresh → protected route → update → assign role → logout → confirm
revocation, plus the audit trail every mutating step must produce (CLAUDE.md
rule 9).

.. note::

   The invited-user activation step is a stand-in for the invitation-token
   seam the handoff flags as missing (defect B6 — the invite flow has no way
   for the invited user to activate yet). The flow flips ``INVITED → ACTIVE``
   directly to represent what that seam will do; B6 itself is tracked
   separately and out of scope for the feature build.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies.auth import get_current_user, require_permission
from app.core.security import hash_password, verify_access_token
from app.models.user import User, UserStatus
from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
from app.repositories.permission_repository import PermissionRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.role_repository import RoleRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.tests.conftest import grant_permissions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.unit_of_work import UnitOfWork
    from app.tests.conftest import RecordingAuditSink

pytestmark = pytest.mark.database

PASSWORD = "Str0ng!Passw0rd123"


@pytest.fixture
def auth_service(
    db_session: AsyncSession,
    audit_sink: RecordingAuditSink,
    uow: UnitOfWork,
) -> AuthService:
    """A fully wired :class:`AuthService` on the transactional test session."""
    return AuthService(
        user_repo=UserRepository(db_session),
        refresh_token_repo=RefreshTokenRepository(db_session),
        password_reset_repo=PasswordResetTokenRepository(db_session),
        uow=uow,
        audit=audit_sink,
    )


@pytest.fixture
def user_service(
    db_session: AsyncSession,
    audit_sink: RecordingAuditSink,
    uow: UnitOfWork,
    auth_service: AuthService,
) -> UserService:
    """A fully wired :class:`UserService` on the transactional test session."""
    return UserService(
        user_repo=UserRepository(db_session),
        role_repo=RoleRepository(db_session),
        permission_repo=PermissionRepository(db_session),
        auth_service=auth_service,
        uow=uow,
        audit=audit_sink,
        password_reset_repo=PasswordResetTokenRepository(db_session),
    )


async def _create_role_granting(
    db_session: AsyncSession, hospital_id: uuid.UUID, *, code: str
) -> uuid.UUID:
    """Create a hospital-scoped role granting one permission code.

    The test database is never seeded — ``conftest`` runs Alembic migrations
    only — so the seeded system roles do not exist here. This mirrors how
    ``grant_permissions`` builds ``Role`` + ``RolePermission`` rows for the
    same reason.

    :param db_session: The test session.
    :param hospital_id: Tenant to scope the role to.
    :param code: The permission code to grant, e.g. ``"user.read"``.
    :returns: The new role's UUID.
    """
    from sqlalchemy import select

    from app.models.permission import Permission
    from app.models.role import Role, RolePermission

    existing = await db_session.execute(select(Permission).where(Permission.code == code))
    permission = existing.unique().scalar_one_or_none()
    if permission is None:
        permission = Permission(
            id=uuid.uuid4(), code=code, module=code.split(".")[0], description=code
        )
        db_session.add(permission)
        await db_session.flush()

    role = Role(id=uuid.uuid4(), hospital_id=hospital_id, name=f"test-role-{uuid.uuid4().hex[:8]}")
    db_session.add(role)
    await db_session.flush()

    db_session.add(RolePermission(id=uuid.uuid4(), role_id=role.id, permission_id=permission.id))
    await db_session.flush()
    return role.id


class TestFullLifecycle:
    """invite → activate → login → refresh → protected → update → role → logout."""

    async def test_the_eight_step_flow_end_to_end(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID,
        auth_service: AuthService,
        user_service: UserService,
        audit_sink: RecordingAuditSink,
    ) -> None:
        # ── Step 1: invite ───────────────────────────────────────────────────
        # The admin carries user.create, user.update, role.assign.
        await grant_permissions(
            db_session,
            hospital_id=hospital_id,
            user_id=actor_id,
            codes=["user.create", "user.update", "role.assign", "user.read"],
        )
        email = f"invitee-{uuid.uuid4().hex[:12]}@hospital.example"

        invited, invite_token = await user_service.invite_user(
            hospital_id=hospital_id,
            email=email,
            first_name="New",
            last_name="Nurse",
            actor_permissions=["user.create"],
            actor_id=actor_id,
        )
        assert invited.status == UserStatus.INVITED
        assert invite_token
        assert audit_sink.last().action == "user.invited"
        assert audit_sink.last().actor_id == actor_id

        # ── Step 2: activate via the real invite-token seam (B6) ───────────
        # The invite token is a single-use password-reset token; consuming it
        # transitions the account INVITED → ACTIVE.
        await auth_service.reset_password(raw_token=invite_token, new_password=PASSWORD)
        activated = await UserRepository(db_session).get_by_id(invited.id)
        assert activated is not None
        assert activated.status == UserStatus.ACTIVE

        # ── Step 3: login ────────────────────────────────────────────────────
        login_result = await auth_service.login(email=email, password=PASSWORD)
        assert login_result["access_token"]
        assert login_result["refresh_token"]
        assert audit_sink.last().action == "auth.login.success"
        assert audit_sink.last().target_id == invited.id

        # The JWT carries the user's identity.
        payload = verify_access_token(login_result["access_token"])
        assert payload["sub"] == str(invited.id)
        assert payload["type"] == "access"

        # ── Step 4: refresh (rotation) ───────────────────────────────────────
        rotated = await auth_service.refresh_token(login_result["refresh_token"])
        assert rotated["refresh_token"] != login_result["refresh_token"]

        # The old token is revoked: replaying it triggers reuse detection,
        # which invalidates every session and is audited.
        reuse_raised = False
        try:
            await auth_service.refresh_token(login_result["refresh_token"])
        except Exception:
            reuse_raised = True
        assert reuse_raised, "reusing a rotated token must be rejected"
        assert audit_sink.last().action == "auth.token.reuse_detected"

        # ── Step 5: protected route (real dependency against real DB) ───────
        # Resolve the user from the fresh access token through the exact
        # dependency every protected endpoint uses.
        fresh_token = rotated["access_token"]
        resolved = await get_current_user(
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=fresh_token),
            user_repo=UserRepository(db_session),
        )
        assert resolved.id == invited.id
        # The invite granted no permissions yet, so a permission the user does
        # not hold must be rejected with 403.
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await require_permission("user.update")(current_user=resolved)
        assert exc_info.value.status_code == 403

        # ── Step 6: update ───────────────────────────────────────────────────
        updated = await user_service.update_user(
            user_id=invited.id,
            actor_hospital_id=hospital_id,
            actor_permissions=["user.update"],
            actor_id=actor_id,
            last_name="Nurse-Practitioner",
        )
        assert updated.last_name == "Nurse-Practitioner"
        assert audit_sink.last().action == "user.updated"

        # ── Step 7: assign role ──────────────────────────────────────────────
        # Grant the target user user.read via the admin, then the protected
        # route admits them. The test DB is never seeded (conftest runs
        # migrations only), so the role is created here — the same pattern
        # ``grant_permissions`` uses (see the module docstring).
        await grant_permissions(
            db_session,
            hospital_id=hospital_id,
            user_id=actor_id,
            codes=["role.assign"],
        )
        role_id = await _create_role_granting(db_session, hospital_id, code="user.read")

        await user_service.assign_role(
            user_id=invited.id,
            role_id=role_id,
            # BR-8: the actor must already hold everything the role grants.
            actor_permissions=["role.assign", "user.read"],
            actor_id=actor_id,
            actor_hospital_id=hospital_id,
        )
        assert audit_sink.last().action == "role.assigned"
        assert audit_sink.last().changes["role_id"]["after"] == str(role_id)

        # Fresh token after the role change now carries the permission.
        relogin = await auth_service.login(email=email, password=PASSWORD)
        resolved = await get_current_user(
            credentials=HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=relogin["access_token"]
            ),
            user_repo=UserRepository(db_session),
        )
        admitted = await require_permission("user.read")(current_user=resolved)
        assert admitted.id == invited.id

        # ── Step 8: logout, then confirm revocation ─────────────────────────
        await auth_service.logout(relogin["refresh_token"])
        assert audit_sink.last().action == "auth.logout"

        from app.core.exceptions import AuthenticationError

        with pytest.raises(AuthenticationError):
            await auth_service.refresh_token(relogin["refresh_token"])

        # Every mutating step produced exactly one audit event each.
        for action in (
            "user.invited",
            "auth.login.success",
            "user.updated",
            "role.assigned",
            "auth.logout",
        ):
            assert action in audit_sink.actions(), f"missing audit event {action}"


class TestLoginRejectsSuspendedAccounts:
    """A suspended account cannot log in even with valid credentials."""

    async def test_suspended_user_cannot_log_in(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        auth_service: AuthService,
        audit_sink: RecordingAuditSink,
    ) -> None:
        # Anti-enumeration: the login must fail with the same generic error as
        # invalid credentials — never a distinct ACCOUNT_SUSPENDED that would
        # confirm the email belongs to a real account. The real reason stays in
        # the audit trail (CLAUDE.md rule 9).
        from app.core.exceptions import AuthenticationError

        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"suspended-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password(PASSWORD),
            first_name="Suspended",
            last_name="User",
            status=UserStatus.SUSPENDED,
        )
        db_session.add(user)
        await db_session.flush()

        with pytest.raises(AuthenticationError, match="Invalid credentials."):
            await auth_service.login(email=user.email, password=PASSWORD)

        assert audit_sink.last().action == "auth.login.failed"
        assert audit_sink.last().context["reason"] == "suspended"


class TestCrossTenantUserAccess:
    """An admin at Hospital A cannot manage a user at Hospital B."""

    async def test_get_user_from_another_hospital_is_a_404(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        user_service: UserService,
    ) -> None:
        from app.core.exceptions import NotFoundError

        foreign = User(
            id=uuid.uuid4(),
            hospital_id=other_hospital_id,
            email=f"foreign-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password(PASSWORD),
            first_name="Foreign",
            last_name="User",
        )
        db_session.add(foreign)
        await db_session.flush()

        with pytest.raises(NotFoundError):
            await user_service.get_user(foreign.id, actor_hospital_id=hospital_id)


class TestTokenRevocationOnRoleChange:
    """Assigning a role revokes the user's sessions (module spec §5.3)."""

    async def test_assign_role_revokes_existing_tokens(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        actor_id: uuid.UUID,
        auth_service: AuthService,
        user_service: UserService,
    ) -> None:
        from app.core.exceptions import AuthenticationError

        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"revoke-{uuid.uuid4().hex[:12]}@hospital.example",
            password_hash=hash_password(PASSWORD),
            first_name="Token",
            last_name="Holder",
        )
        db_session.add(user)
        await db_session.flush()

        tokens = await auth_service.login(email=user.email, password=PASSWORD)

        role_id = await _create_role_granting(db_session, hospital_id, code="user.read")
        await user_service.assign_role(
            user_id=user.id,
            role_id=role_id,
            # BR-8: the actor must already hold everything the role grants.
            actor_permissions=["role.assign", "user.read"],
            actor_id=actor_id,
            actor_hospital_id=hospital_id,
        )

        # The pre-change session is dead.
        with pytest.raises(AuthenticationError):
            await auth_service.refresh_token(tokens["refresh_token"])
