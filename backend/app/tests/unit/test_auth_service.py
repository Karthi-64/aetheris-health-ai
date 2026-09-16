"""Unit tests for :class:`AuthService`.

Tests business logic with mocked repositories.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import (
    AuthenticationError,
    BusinessRuleError,
    NotFoundError,
)
from app.core.security import hash_password
from app.models.user import User, UserStatus


@pytest.fixture
def mock_user_repo() -> AsyncMock:
    """Create a mock UserRepository."""
    return AsyncMock()


@pytest.fixture
def mock_refresh_token_repo() -> AsyncMock:
    """Create a mock RefreshTokenRepository."""
    return AsyncMock()


@pytest.fixture
def mock_password_reset_repo() -> AsyncMock:
    """Create a mock PasswordResetTokenRepository."""
    return AsyncMock()


@pytest.fixture
def auth_service(
    mock_user_repo: AsyncMock,
    mock_refresh_token_repo: AsyncMock,
    mock_password_reset_repo: AsyncMock,
    mock_uow: AsyncMock,
    audit_sink: Any,
) -> Any:
    """Create an AuthService with mocked repositories."""
    from app.services.auth_service import AuthService

    return AuthService(
        user_repo=mock_user_repo,
        refresh_token_repo=mock_refresh_token_repo,
        password_reset_repo=mock_password_reset_repo,
        uow=mock_uow,
        audit=audit_sink,
    )


def _make_user(overrides: dict[str, Any] | None = None) -> User:
    """Create a test user with sensible defaults."""
    user_id = uuid.uuid4()
    hospital_id = uuid.uuid4()
    role_id = uuid.uuid4()
    perm_id = uuid.uuid4()

    # Build a minimal user with relationships
    from app.models.permission import Permission
    from app.models.role import Role, RolePermission
    from app.models.user import UserRole

    role = MagicMock(spec=Role)
    role.id = role_id
    role.name = "Hospital Admin"
    role.description = "Test role"
    role.is_system = False
    role.hospital_id = hospital_id

    perm = MagicMock(spec=Permission)
    perm.id = perm_id
    perm.code = "user.read"
    perm.module = "auth"

    rp = MagicMock(spec=RolePermission)
    rp.permission = perm

    role.role_permissions = [rp]

    ur = MagicMock(spec=UserRole)
    ur.role = role
    ur.user_id = user_id
    ur.role_id = role_id

    user = MagicMock(spec=User)
    user.id = user_id
    user.hospital_id = hospital_id
    user.email = overrides.get("email", "test@hospital.test") if overrides else "test@hospital.test"
    user.password_hash = hash_password("TestPass@123")
    user.first_name = "Test"
    user.last_name = "User"
    user.phone = "+911234567890"
    user.status = UserStatus.ACTIVE
    user.mfa_enabled = False
    user.mfa_secret = None
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = None
    user.password_changed_at = datetime.now(UTC)
    user.user_roles = [ur]
    user.created_at = datetime.now(UTC)
    user.updated_at = datetime.now(UTC)

    if overrides:
        for key, value in overrides.items():
            if hasattr(user, key):
                setattr(user, key, value)

    return user


# ── Login Tests ────────────────────────────────────────────────────────────


class TestLogin:
    """Tests for the login flow."""

    async def test_login_success(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """Happy path: valid credentials return tokens."""
        user = _make_user()
        mock_user_repo.get_by_email_cross_tenant.return_value = user
        mock_refresh_token_repo.create.return_value = MagicMock(id=uuid.uuid4())

        result = await auth_service.login(
            email="test@hospital.test",
            password="TestPass@123",
        )

        assert "access_token" in result
        assert "refresh_token" in result
        assert "user" in result
        assert result["user"]["email"] == "test@hospital.test"
        mock_user_repo.record_login.assert_called_once()

    async def test_login_user_payload_includes_permissions_and_display_name(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """The SPA builds its RBAC nav from ``user.permissions`` and displays ``user.name``."""
        user = _make_user()
        mock_user_repo.get_by_email_cross_tenant.return_value = user
        mock_refresh_token_repo.create.return_value = MagicMock(id=uuid.uuid4())

        result = await auth_service.login(
            email="test@hospital.test",
            password="TestPass@123",
        )

        profile = result["user"]
        assert profile["name"] == "Test User"
        assert profile["permissions"] == ["user.read"]

    async def test_login_user_permissions_are_empty_without_roles(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """A role-less user gets no permissions and no fabricated access."""
        user = _make_user()
        user.user_roles = []
        mock_user_repo.get_by_email_cross_tenant.return_value = user
        mock_refresh_token_repo.create.return_value = MagicMock(id=uuid.uuid4())

        result = await auth_service.login(
            email="test@hospital.test",
            password="TestPass@123",
        )

        assert result["user"]["permissions"] == []
        mock_user_repo.record_login.assert_called_once()

    async def test_login_invalid_credentials(
        self: Any, auth_service: Any, mock_user_repo: Any
    ) -> None:
        """Invalid password returns AuthenticationError."""
        user = _make_user()
        mock_user_repo.get_by_email_cross_tenant.return_value = user

        with pytest.raises(AuthenticationError, match="Invalid credentials."):
            await auth_service.login(
                email="test@hospital.test",
                password="WrongPass@123",
            )

    async def test_login_nonexistent_email(
        self: Any, auth_service: Any, mock_user_repo: Any
    ) -> None:
        """Non-existent email returns generic error."""
        mock_user_repo.get_by_email_cross_tenant.return_value = None

        with pytest.raises(AuthenticationError, match="Invalid credentials."):
            await auth_service.login(
                email="nonexistent@test.test",
                password="SomePass@123",
            )

    async def test_login_suspended_account(
        self: Any, auth_service: Any, mock_user_repo: Any
    ) -> None:
        """Suspended account login is indistinguishable from invalid credentials.

        Anti-enumeration (docs/07-SECURITY.md rule 10): the response must not
        reveal that the email belongs to a real, suspended account.
        """
        user = _make_user({"status": UserStatus.SUSPENDED})
        mock_user_repo.get_by_email_cross_tenant.return_value = user

        with pytest.raises(AuthenticationError, match="Invalid credentials."):
            await auth_service.login(
                email="test@hospital.test",
                password="TestPass@123",
            )

    async def test_login_locked_account(self: Any, auth_service: Any, mock_user_repo: Any) -> None:
        """Locked account login is indistinguishable from invalid credentials.

        Anti-enumeration: no unlock_at detail may leak to the caller.
        """
        user = _make_user({"locked_until": datetime.now(UTC) + timedelta(hours=1)})
        mock_user_repo.get_by_email_cross_tenant.return_value = user

        with pytest.raises(AuthenticationError, match="Invalid credentials."):
            await auth_service.login(
                email="test@hospital.test",
                password="TestPass@123",
            )


# ── Token Refresh Tests ────────────────────────────────────────────────────


class TestRefreshToken:
    """Tests for refresh token rotation."""

    async def test_refresh_success(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """Valid refresh token returns new token pair."""
        from app.models.refresh_token import RefreshToken

        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        token = MagicMock(spec=RefreshToken)
        token.id = uuid.uuid4()
        token.user_id = user.id
        token.is_revoked = False
        token.is_expired = False
        token.is_valid = True

        mock_refresh_token_repo.get_by_token_hash.return_value = token
        mock_refresh_token_repo.create.return_value = MagicMock(id=uuid.uuid4())

        result = await auth_service.refresh_token(raw_token="some-valid-token")

        assert result["access_token"] is not None
        assert result["refresh_token"] is not None
        assert result["expires_in"] > 0

    async def test_refresh_reuse_detection(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """Revoked token triggers reuse detection."""
        from app.models.refresh_token import RefreshToken

        user_id = uuid.uuid4()
        token = MagicMock(spec=RefreshToken)
        token.id = uuid.uuid4()
        token.user_id = user_id
        token.is_revoked = True
        token.is_expired = False

        mock_refresh_token_repo.get_by_token_hash.return_value = token

        with pytest.raises(AuthenticationError, match="has been revoked"):
            await auth_service.refresh_token(raw_token="stolen-token")

        mock_refresh_token_repo.revoke_all_for_user.assert_called_once_with(user_id)


# ── Password Reset Tests ───────────────────────────────────────────────────


class TestPasswordReset:
    """Tests for password reset flow."""

    async def test_forgot_password_existing_user(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_password_reset_repo: Any
    ) -> None:
        """Existing user gets a reset token created."""
        user = _make_user()
        mock_user_repo.get_by_email_cross_tenant.return_value = user

        await auth_service.forgot_password(email="test@hospital.test")

        mock_password_reset_repo.create.assert_called_once()

    async def test_forgot_password_nonexistent_user(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_password_reset_repo: Any
    ) -> None:
        """Non-existent user still returns success (no reveal)."""
        mock_user_repo.get_by_email_cross_tenant.return_value = None

        await auth_service.forgot_password(email="nonexistent@test.test")

        mock_password_reset_repo.create.assert_not_called()

    async def test_reset_password_weak_password(
        self: Any, auth_service: Any, mock_password_reset_repo: Any
    ) -> None:
        """Weak password raises BusinessRuleError."""
        with pytest.raises(BusinessRuleError, match="Password does not meet requirements"):
            await auth_service.reset_password(
                raw_token="valid-token",
                new_password="weak",
            )

    async def test_reset_password_activates_an_invited_user(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
    ) -> None:
        """B6: consuming an invite token flips INVITED -> ACTIVE."""
        from app.models.password_reset_token import PasswordResetToken

        user = _make_user({"status": UserStatus.INVITED})
        mock_user_repo.get_by_id.return_value = user

        token = MagicMock(spec=PasswordResetToken)
        token.id = uuid.uuid4()
        token.user_id = user.id
        mock_password_reset_repo.get_valid_token.return_value = token

        async def _update_in_place(instance: Any, **kwargs: Any) -> Any:
            for key, value in kwargs.items():
                setattr(instance, key, value)
            return instance

        mock_user_repo.update.side_effect = _update_in_place

        await auth_service.reset_password(
            raw_token="invite-token", new_password="Str0ng!Passw0rd123"
        )

        assert user.status == UserStatus.ACTIVE
        assert user.password_changed_at is not None
        mock_password_reset_repo.mark_as_used.assert_called_once_with(token)

    async def test_reset_password_keeps_active_status_active(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_password_reset_repo: Any
    ) -> None:
        """B6: a plain reset on an ACTIVE user leaves the status unchanged."""
        from app.models.password_reset_token import PasswordResetToken

        user = _make_user({"status": UserStatus.ACTIVE})
        mock_user_repo.get_by_id.return_value = user

        token = MagicMock(spec=PasswordResetToken)
        token.id = uuid.uuid4()
        token.user_id = user.id
        mock_password_reset_repo.get_valid_token.return_value = token

        async def _update_in_place(instance: Any, **kwargs: Any) -> Any:
            for key, value in kwargs.items():
                setattr(instance, key, value)
            return instance

        mock_user_repo.update.side_effect = _update_in_place

        await auth_service.reset_password(
            raw_token="reset-token", new_password="Str0ng!Passw0rd123"
        )

        assert user.status == UserStatus.ACTIVE

    async def test_admin_reset_cross_tenant_is_not_found(
        self: Any, auth_service: Any, mock_user_repo: Any
    ) -> None:
        """B1: an admin cannot reset another hospital's user (404, like every
        other cross-tenant admin write — never 401, which would leak that the
        user exists elsewhere)."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await auth_service.admin_reset_password(
                user_id=user.id,
                actor_hospital_id=uuid.uuid4(),  # different hospital
            )

    async def test_admin_reset_same_hospital_succeeds(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """B1: same-hospital admin reset still works."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        await auth_service.admin_reset_password(
            user_id=user.id,
            actor_hospital_id=user.hospital_id,
        )

        mock_refresh_token_repo.revoke_all_for_user.assert_called_once_with(user.id)


# ── MFA Tests ──────────────────────────────────────────────────────────────


class TestMFA:
    """Tests for MFA enrollment and verification."""

    async def test_enroll_mfa(self: Any, auth_service: Any, mock_user_repo: Any) -> None:
        """MFA enrollment returns a secret."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        result = await auth_service.enroll_mfa(
            user_id=user.id,
            password="TestPass@123",
        )

        assert "secret" in result
        assert "provisioning_uri" in result
        mock_user_repo.update.assert_called_once()


# ── PII / Logging Tests ────────────────────────────────────────────────────


class TestLogDiscriminator:
    """B3: raw email addresses never reach the logs."""

    async def test_discriminator_is_stable_and_non_reversible(self: Any, auth_service: Any) -> None:
        """Same email hashes identically; different emails differ."""
        assert auth_service._email_discriminator(
            "User@Example.com"
        ) == auth_service._email_discriminator("user@example.com")
        assert auth_service._email_discriminator("a@b.co") != auth_service._email_discriminator(
            "c@d.co"
        )

    async def test_discriminator_contains_no_email_parts(self: Any, auth_service: Any) -> None:
        """The hash prefix cannot leak any part of the address."""
        email = "patient.one@hospital.example"
        digest = auth_service._email_discriminator(email)

        assert len(digest) == 16
        assert all(c in "0123456789abcdef" for c in digest)
        assert "patient" not in digest
        assert "hospital" not in digest
