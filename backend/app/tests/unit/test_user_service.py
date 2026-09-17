"""Unit tests for :class:`UserService`.

Tests business logic with mocked repositories.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.models.user import User, UserStatus


@pytest.fixture
def mock_user_repo() -> AsyncMock:
    """Create a mock UserRepository."""
    return AsyncMock()


@pytest.fixture
def mock_role_repo() -> AsyncMock:
    """Create a mock RoleRepository."""
    return AsyncMock()


@pytest.fixture
def mock_permission_repo() -> AsyncMock:
    """Create a mock PermissionRepository."""
    return AsyncMock()


@pytest.fixture
def mock_auth_service() -> AsyncMock:
    """Create a mock AuthService."""
    return AsyncMock()


@pytest.fixture
def mock_password_reset_repo() -> AsyncMock:
    """Create a mock PasswordResetTokenRepository."""
    return AsyncMock()


@pytest.fixture
def user_service(
    mock_user_repo: AsyncMock,
    mock_role_repo: AsyncMock,
    mock_permission_repo: AsyncMock,
    mock_auth_service: AsyncMock,
    mock_uow: AsyncMock,
    audit_sink: Any,
    mock_password_reset_repo: AsyncMock,
) -> Any:
    """Create a UserService with mocked dependencies."""
    from app.services.user_service import UserService

    return UserService(
        user_repo=mock_user_repo,
        role_repo=mock_role_repo,
        permission_repo=mock_permission_repo,
        auth_service=mock_auth_service,
        uow=mock_uow,
        audit=audit_sink,
        password_reset_repo=mock_password_reset_repo,
    )


def _make_user(overrides: dict[str, Any] | None = None) -> User:
    """Create a test user with sensible defaults."""
    user_id = uuid.uuid4()
    hospital_id = uuid.uuid4()

    user = MagicMock(spec=User)
    user.id = user_id
    user.hospital_id = hospital_id
    user.email = "test@hospital.test"
    user.first_name = "Test"
    user.last_name = "User"
    user.phone = "+911234567890"
    user.status = UserStatus.ACTIVE
    user.mfa_enabled = False
    user.user_roles = []
    user.created_at = MagicMock()
    user.updated_at = MagicMock()

    if overrides:
        for key, value in overrides.items():
            if hasattr(user, key):
                setattr(user, key, value)

    return user


# ── Read Tests ─────────────────────────────────────────────────────────────


class TestGetUser:
    """Tests for retrieving users."""

    async def test_get_user_success(self: Any, user_service: Any, mock_user_repo: Any) -> None:
        """Existing user is returned."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        result = await user_service.get_user(user_id=user.id)
        assert result.id == user.id
        assert result.email == "test@hospital.test"

    async def test_get_user_not_found(self: Any, user_service: Any, mock_user_repo: Any) -> None:
        """Non-existent user raises NotFoundError."""
        mock_user_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.get_user(user_id=uuid.uuid4())

    async def test_get_user_cross_hospital_blocked(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """User from different hospital is not visible."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        other_hospital_id = uuid.uuid4()
        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.get_user(user_id=user.id, actor_hospital_id=other_hospital_id)


# ── Invite Tests ──────────────────────────────────────────────────────────


class TestInviteUser:
    """Tests for inviting users."""

    async def test_invite_user_success(self: Any, user_service: Any, mock_user_repo: Any) -> None:
        """User is created with invited status."""
        hospital_id = uuid.uuid4()
        mock_user_repo.get_by_email.return_value = None
        mock_user_repo.create.return_value = _make_user(
            {
                "status": UserStatus.INVITED,
                "hospital_id": hospital_id,
            }
        )

        result, invite_token = await user_service.invite_user(
            hospital_id=hospital_id,
            email="newuser@hospital.test",
            first_name="New",
            last_name="User",
            actor_permissions=["user.create"],
        )

        assert result.status == UserStatus.INVITED
        assert invite_token  # B6: an invite token is minted for activation
        mock_user_repo.create.assert_called_once()

    async def test_invite_user_refreshes_roles_into_the_response(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """Roles assigned during the invite must appear on the returned user.

        Regression: ``user_roles`` is a selectin-loaded collection that had
        already loaded (empty) before ``add_role`` inserted the rows, so the
        invite response reported ``roles: []`` for a user that did have roles.
        """
        from unittest.mock import MagicMock

        from app.models.role import Role
        from app.models.user import UserRole

        hospital_id = uuid.uuid4()
        role_id = uuid.uuid4()
        created = _make_user()
        created.status = UserStatus.INVITED
        mock_user_repo.get_by_email.return_value = None
        mock_user_repo.create.return_value = created
        mock_user_repo.has_role.return_value = False

        def _loaded_user_role(user: Any) -> Any:
            role = MagicMock(spec=Role)
            role.id = role_id
            role.name = "Nurse"
            role.description = "Care coordination"
            role.is_system = True
            role.role_permissions = []
            ur = MagicMock(spec=UserRole)
            ur.role = role
            ur.role_id = role_id
            user.user_roles = [ur]
            return user

        mock_user_repo.refresh = AsyncMock(side_effect=_loaded_user_role)

        # The requested role must pass the B5 tenant check: a system role
        # (hospital_id NULL) is visible to every hospital.
        requested_role = MagicMock(spec=Role)
        requested_role.id = role_id
        requested_role.hospital_id = None
        requested_role.is_system = True
        mock_role_repo.get_by_id.return_value = requested_role

        result, _ = await user_service.invite_user(
            hospital_id=hospital_id,
            email="newuser@hospital.test",
            first_name="New",
            last_name="User",
            role_ids=[role_id],
            actor_permissions=["user.create"],
        )

        mock_user_repo.refresh.assert_awaited_once_with(result)
        assert [r.role.name for r in result.user_roles] == ["Nurse"]

    async def test_invite_user_duplicate_email(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """Duplicate email raises ConflictError (HTTP 409 per the contract)."""
        hospital_id = uuid.uuid4()
        mock_user_repo.get_by_email.return_value = _make_user()

        with pytest.raises(ConflictError, match="already exists"):
            await user_service.invite_user(
                hospital_id=hospital_id,
                email="existing@hospital.test",
                first_name="Existing",
                last_name="User",
                actor_permissions=["user.create"],
            )

    async def test_invite_user_without_permission(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """Missing permission raises error."""
        # Ensure get_by_email returns None so it won't interfere
        mock_user_repo.get_by_email.return_value = None

        with pytest.raises(PermissionDeniedError, match="do not have permission"):
            await user_service.invite_user(
                hospital_id=uuid.uuid4(),
                email="test@hospital.test",
                first_name="Test",
                last_name="User",
                actor_permissions=[],
            )

    async def test_invite_user_rejects_unknown_role_id(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """B5: an unresolvable role id fails the whole invite (all-or-nothing)."""
        hospital_id = uuid.uuid4()
        mock_user_repo.get_by_email.return_value = None
        mock_role_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError, match="roles were not found"):
            await user_service.invite_user(
                hospital_id=hospital_id,
                email="newuser@hospital.test",
                first_name="New",
                last_name="User",
                role_ids=[uuid.uuid4()],
                actor_permissions=["user.create"],
            )

        # All-or-nothing: the user must not have been created.
        mock_user_repo.create.assert_not_called()

    async def test_invite_user_rejects_foreign_hospital_role(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """B5/B1: a role from another hospital fails the invite too."""
        hospital_id = uuid.uuid4()
        foreign_role = MagicMock(
            id=uuid.uuid4(), name="Other Hospital Role", hospital_id=uuid.uuid4()
        )
        mock_user_repo.get_by_email.return_value = None
        mock_role_repo.get_by_id.return_value = foreign_role

        with pytest.raises(NotFoundError, match="roles were not found"):
            await user_service.invite_user(
                hospital_id=hospital_id,
                email="newuser@hospital.test",
                first_name="New",
                last_name="User",
                role_ids=[foreign_role.id],
                actor_permissions=["user.create"],
            )

        mock_user_repo.create.assert_not_called()


# ── Deactivate Tests ──────────────────────────────────────────────────────


class TestDeactivateUser:
    """Tests for deactivating users."""

    async def test_deactivate_other_user(
        self: Any, user_service: Any, mock_user_repo: Any, mock_auth_service: Any
    ) -> None:
        """Admin can deactivate another user."""
        user = _make_user()
        admin_id = uuid.uuid4()
        mock_user_repo.get_by_id.return_value = user

        # Mock update to modify the user in-place (simulating SQLAlchemy flush)
        async def _update_in_place(instance: Any, **kwargs: Any) -> Any:
            for key, value in kwargs.items():
                setattr(instance, key, value)
            return instance

        mock_user_repo.update.side_effect = _update_in_place

        result = await user_service.deactivate_user(
            user_id=user.id,
            actor_user_id=admin_id,
            actor_hospital_id=user.hospital_id,
            actor_permissions=["user.deactivate"],
        )

        assert result.status == UserStatus.SUSPENDED
        mock_auth_service.logout_all.assert_called_once_with(user.id)

    async def test_deactivate_self_raises_error(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """Deactivating yourself is not allowed."""
        user = _make_user()

        with pytest.raises(BusinessRuleError, match="cannot deactivate yourself"):
            await user_service.deactivate_user(
                user_id=user.id,
                actor_user_id=user.id,
                actor_permissions=["user.deactivate"],
            )

    async def test_deactivate_cross_tenant_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B1: deactivating a user from another hospital is not found."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.deactivate_user(
                user_id=user.id,
                actor_user_id=uuid.uuid4(),
                actor_hospital_id=uuid.uuid4(),  # different hospital
                actor_permissions=["user.deactivate"],
            )

    async def test_deactivate_without_permission_fails_closed(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B2: empty permission list is denied, not skipped."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(PermissionDeniedError):
            await user_service.deactivate_user(
                user_id=user.id,
                actor_user_id=uuid.uuid4(),
                actor_hospital_id=user.hospital_id,
                actor_permissions=[],
            )

        with pytest.raises(PermissionDeniedError):
            await user_service.deactivate_user(
                user_id=user.id,
                actor_user_id=uuid.uuid4(),
                actor_hospital_id=user.hospital_id,
                actor_permissions=None,
            )

    async def test_reactivate_cross_tenant_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B1: reactivating a user from another hospital is not found."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.reactivate_user(
                user_id=user.id,
                actor_hospital_id=uuid.uuid4(),
                actor_permissions=["user.deactivate"],
            )

    async def test_reactivate_without_permission_fails_closed(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B2: reactivate now requires the permission explicitly."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(PermissionDeniedError):
            await user_service.reactivate_user(
                user_id=user.id,
                actor_hospital_id=user.hospital_id,
                actor_permissions=None,
            )


# ── Role Management Tests ──────────────────────────────────────────────────


class TestRoleManagement:
    """Tests for role assignment and removal."""

    async def test_assign_role(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_role_repo: Any,
        mock_auth_service: Any,
    ) -> None:
        """Role is assigned and sessions are revoked."""
        user = _make_user()
        role_id = uuid.uuid4()

        mock_user_repo.get_by_id.return_value = user
        # A system role (hospital_id=None) is assignable from any tenant (B1).
        mock_role_repo.get_by_id.return_value = MagicMock(
            id=role_id, name="Doctor", hospital_id=None
        )
        mock_user_repo.has_role.return_value = False

        await user_service.assign_role(
            user_id=user.id,
            role_id=role_id,
            actor_permissions=["role.assign"],
            actor_hospital_id=user.hospital_id,
        )

        # BR-9: the join row records who granted the role.
        mock_user_repo.add_role.assert_called_once_with(user.id, role_id, assigned_by=None)
        mock_auth_service.logout_all.assert_called_once_with(user.id)

    async def test_assign_role_already_assigned(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_role_repo: Any,
        mock_auth_service: Any,
    ) -> None:
        """Re-assigning same role is idempotent."""
        user = _make_user()
        role_id = uuid.uuid4()

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = MagicMock(
            id=role_id, name="Doctor", hospital_id=None
        )
        mock_user_repo.has_role.return_value = True  # Already assigned

        await user_service.assign_role(
            user_id=user.id,
            role_id=role_id,
            actor_permissions=["role.assign"],
            actor_hospital_id=user.hospital_id,
        )

        mock_user_repo.add_role.assert_not_called()

    async def test_remove_role(
        self: Any, user_service: Any, mock_user_repo: Any, mock_auth_service: Any
    ) -> None:
        """Role is removed and sessions are revoked."""
        user = _make_user()
        role_id = uuid.uuid4()

        mock_user_repo.get_by_id.return_value = user
        mock_user_repo.remove_role.return_value = True  # Successfully removed

        await user_service.remove_role(
            user_id=user.id,
            role_id=role_id,
            actor_permissions=["role.assign"],
            actor_hospital_id=user.hospital_id,
        )

        mock_user_repo.remove_role.assert_called_once_with(user.id, role_id)
        mock_auth_service.logout_all.assert_called_once_with(user.id)

    async def test_assign_role_fails_closed_without_permission(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B2: ``None`` actor_permissions is denied, not skipped."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(PermissionDeniedError):
            await user_service.assign_role(
                user_id=user.id,
                role_id=uuid.uuid4(),
                actor_permissions=None,
            )

    async def test_remove_role_fails_closed_without_permission(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B2: empty permission list is denied, not skipped."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(PermissionDeniedError):
            await user_service.remove_role(
                user_id=user.id,
                role_id=uuid.uuid4(),
                actor_permissions=[],
            )

    async def test_assign_role_cross_tenant_user_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """B1: assigning a role to another hospital's user is not found."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = MagicMock(
            id=uuid.uuid4(), name="Doctor", hospital_id=None
        )

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.assign_role(
                user_id=user.id,
                role_id=uuid.uuid4(),
                actor_permissions=["role.assign"],
                actor_hospital_id=uuid.uuid4(),
            )

    async def test_assign_role_cross_tenant_role_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """B1: a role from another hospital cannot be assigned."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = MagicMock(
            id=uuid.uuid4(), name="Other Hospital Role", hospital_id=uuid.uuid4()
        )

        with pytest.raises(NotFoundError, match="Role not found."):
            await user_service.assign_role(
                user_id=user.id,
                role_id=uuid.uuid4(),
                actor_permissions=["role.assign"],
                actor_hospital_id=user.hospital_id,
            )

    async def test_remove_role_cross_tenant_user_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B1: removing a role from another hospital's user is not found."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.remove_role(
                user_id=user.id,
                role_id=uuid.uuid4(),
                actor_permissions=["role.assign"],
                actor_hospital_id=uuid.uuid4(),
            )

    async def test_list_user_roles_cross_tenant_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B1: listing roles of another hospital's user is not found."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.list_user_roles(user_id=user.id, actor_hospital_id=uuid.uuid4())

    async def test_update_user_fails_closed_without_permission(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B2: update_user denies ``None``/empty actor_permissions."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        permission_cases: list[list[str] | None] = [None, []]
        for permissions in permission_cases:
            with pytest.raises(PermissionDeniedError):
                await user_service.update_user(
                    user_id=user.id,
                    actor_hospital_id=user.hospital_id,
                    actor_permissions=permissions,
                    last_name="Nope",
                )


# ── Privilege Escalation & Lockout Tests ───────────────────────────────────


def _role_granting(*codes: str, name: str = "Lab Technician") -> MagicMock:
    """Build a role mock whose ``role_permissions`` expose the given codes."""
    role = MagicMock()
    role.id = uuid.uuid4()
    role.name = name
    role.hospital_id = None
    role.role_permissions = [MagicMock(permission=MagicMock(code=code)) for code in codes]
    return role


class TestPrivilegeEscalation:
    """BR-8 / FR-3 / AC-3: an actor may only grant permissions they hold."""

    async def test_assign_role_rejects_permissions_the_actor_lacks(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """Assigning a role that outranks the actor is denied."""
        user = _make_user()
        role = _role_granting("lab.create", "lab.update")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = role
        mock_role_repo.get_with_permissions.return_value = role

        with pytest.raises(PermissionDeniedError, match="permissions you do not hold"):
            await user_service.assign_role(
                user_id=user.id,
                role_id=role.id,
                actor_permissions=["role.assign"],
                actor_hospital_id=user.hospital_id,
            )

        mock_user_repo.add_role.assert_not_called()

    async def test_assign_role_reports_exactly_what_is_missing(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """The 403 names the permissions that blocked it, for a usable error."""
        user = _make_user()
        role = _role_granting("lab.create", "lab.update", "patient.read")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = role
        mock_role_repo.get_with_permissions.return_value = role

        with pytest.raises(PermissionDeniedError) as exc_info:
            await user_service.assign_role(
                user_id=user.id,
                role_id=role.id,
                actor_permissions=["role.assign", "patient.read"],
                actor_hospital_id=user.hospital_id,
            )

        assert exc_info.value.detail["missing_permissions"] == ["lab.create", "lab.update"]

    async def test_assign_role_allows_a_role_the_actor_fully_covers(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_role_repo: Any,
        mock_auth_service: Any,
    ) -> None:
        """Holding every permission the role grants is enough — and no more."""
        user = _make_user()
        role = _role_granting("patient.read", "patient.create", name="Receptionist")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = role
        mock_role_repo.get_with_permissions.return_value = role
        mock_user_repo.has_role.return_value = False

        await user_service.assign_role(
            user_id=user.id,
            role_id=role.id,
            actor_permissions=["role.assign", "patient.read", "patient.create"],
            actor_hospital_id=user.hospital_id,
        )

        mock_user_repo.add_role.assert_called_once_with(user.id, role.id, assigned_by=None)
        mock_auth_service.logout_all.assert_called_once_with(user.id)

    async def test_self_assignment_cannot_escalate(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """The reported hole: an admin granting *themselves* a richer role."""
        admin = _make_user()
        role = _role_granting("pharmacy.dispense", name="Pharmacist")

        mock_user_repo.get_by_id.return_value = admin
        mock_role_repo.get_by_id.return_value = role
        mock_role_repo.get_with_permissions.return_value = role

        with pytest.raises(PermissionDeniedError):
            await user_service.assign_role(
                user_id=admin.id,  # self
                role_id=role.id,
                actor_permissions=["role.assign", "user.read"],
                actor_id=admin.id,
                actor_hospital_id=admin.hospital_id,
            )

    async def test_invite_cannot_escalate_through_initial_roles(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """Inviting into a privileged role is the same hole — the token is returned."""
        hospital_id = uuid.uuid4()
        role = _role_granting("pharmacy.dispense", name="Pharmacist")

        mock_user_repo.get_by_email.return_value = None
        mock_role_repo.get_by_id.return_value = role
        mock_role_repo.get_with_permissions.return_value = role

        with pytest.raises(PermissionDeniedError, match="permissions you do not hold"):
            await user_service.invite_user(
                hospital_id=hospital_id,
                email="escalate@hospital.test",
                first_name="Priv",
                last_name="Escalator",
                role_ids=[role.id],
                actor_permissions=["user.create"],
            )

        mock_user_repo.create.assert_not_called()


class TestAdministratorLockout:
    """§14: the last remaining administrator cannot give up the admin role."""

    async def test_removing_last_administrator_role_is_blocked(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """No other active holder of ``role.assign`` → refuse."""
        user = _make_user()
        role = _role_granting("role.assign", "user.read", name="Hospital Admin")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_with_permissions.return_value = role
        mock_user_repo.count_other_active_holders.return_value = 0

        with pytest.raises(BusinessRuleError, match="last administrator"):
            await user_service.remove_role(
                user_id=user.id,
                role_id=role.id,
                actor_permissions=["role.assign"],
                actor_hospital_id=user.hospital_id,
            )

        mock_user_repo.remove_role.assert_not_called()

    async def test_removal_allowed_while_another_administrator_remains(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """A second active admin makes the removal safe."""
        user = _make_user()
        role = _role_granting("role.assign", "user.read", name="Hospital Admin")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_with_permissions.return_value = role
        mock_user_repo.count_other_active_holders.return_value = 1
        mock_user_repo.remove_role.return_value = True

        await user_service.remove_role(
            user_id=user.id,
            role_id=role.id,
            actor_permissions=["role.assign"],
            actor_hospital_id=user.hospital_id,
        )

        mock_user_repo.remove_role.assert_called_once_with(user.id, role.id)

    async def test_non_administrative_role_removal_is_unaffected(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """A clinical role carries no lockout risk, so the count is never taken."""
        user = _make_user()
        role = _role_granting("patient.read", name="Nurse")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_with_permissions.return_value = role
        mock_user_repo.remove_role.return_value = True

        await user_service.remove_role(
            user_id=user.id,
            role_id=role.id,
            actor_permissions=["role.assign"],
            actor_hospital_id=user.hospital_id,
        )

        mock_user_repo.count_other_active_holders.assert_not_called()
        mock_user_repo.remove_role.assert_called_once_with(user.id, role.id)
