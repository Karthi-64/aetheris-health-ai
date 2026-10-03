"""Unit tests for the authorization dependencies in ``app/api/dependencies/auth.py``.

Covers the six cases the Week 1 handoff lists explicitly: no header, malformed
header, expired token, valid token missing the permission, valid token with the
permission, and a suspended user holding a still-valid token.

``docs/07-SECURITY.md`` rules 3 and 4 make these dependencies the only gate
every protected endpoint has, so a regression here is a regression everywhere.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from app.api.dependencies.auth import (
    get_current_user,
    require_any_permission,
    require_permission,
    user_has_permission,
    user_permission_codes,
)
from app.core.config import settings
from app.core.security import create_access_token
from app.models.user import User, UserStatus


def _make_user(overrides: dict[str, Any] | None = None) -> MagicMock:
    """Build a user with a role granting exactly ``user.read``.

    :param overrides: Attribute overrides, e.g. ``{"status": ...}``.
    :returns: A mocked :class:`User` with a populated permission graph.
    """
    hospital_id = uuid.uuid4()

    from app.models.permission import Permission
    from app.models.role import Role, RolePermission
    from app.models.user import UserRole

    perm = MagicMock(spec=Permission)
    perm.code = "user.read"

    rp = MagicMock(spec=RolePermission)
    rp.permission = perm

    role = MagicMock(spec=Role)
    role.role_permissions = [rp]

    ur = MagicMock(spec=UserRole)
    ur.role = role

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.hospital_id = hospital_id
    user.status = UserStatus.ACTIVE
    user.user_roles = [ur]

    if overrides:
        for key, value in overrides.items():
            setattr(user, key, value)
    return user


def _auth_credentials(token: str) -> Any:
    """Wrap a token in the HTTPBearer credentials object the dependency reads."""
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class TestGetCurrentUser:
    """The authentication gate shared by every protected endpoint."""

    async def test_no_header_returns_401(self: Any) -> None:
        """A missing Authorization header is rejected with 401."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None, user_repo=AsyncMock())
        assert exc_info.value.status_code == 401

    async def test_malformed_header_returns_401(self: Any) -> None:
        """A token that is not a valid JWT is rejected with 401."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                credentials=_auth_credentials("not-a-jwt"),
                user_repo=AsyncMock(),
            )
        assert exc_info.value.status_code == 401

    async def test_expired_token_returns_401(self: Any) -> None:
        """An expired JWT is rejected with 401, not 500."""
        # Mint a token in the past using the same signing key and issuer the
        # app uses, so only the expiry is wrong.
        payload: dict[str, Any] = {
            "sub": str(uuid.uuid4()),
            "iss": settings.JWT_ISSUER,
            "iat": datetime.now(UTC) - timedelta(hours=1),
            "exp": datetime.now(UTC) - timedelta(minutes=30),
            "type": "access",
            "hospital_id": str(uuid.uuid4()),
        }
        expired = pyjwt.encode(
            payload,
            settings.APP_SECRET_KEY,
            algorithm="HS256",
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                credentials=_auth_credentials(expired),
                user_repo=AsyncMock(),
            )
        assert exc_info.value.status_code == 401

    async def test_valid_token_with_unknown_user_returns_401(self: Any) -> None:
        """A token whose subject no longer exists is rejected."""
        user_repo = AsyncMock()
        user_repo.get_by_id.return_value = None

        token = create_access_token(user_id=uuid.uuid4(), hospital_id=uuid.uuid4())

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                credentials=_auth_credentials(token),
                user_repo=user_repo,
            )
        assert exc_info.value.status_code == 401

    async def test_valid_token_for_suspended_user_returns_403(self: Any) -> None:
        """A suspended user with a still-valid token is rejected with 403.

        Suspension must win over a live token — the account was disabled after
        the token was issued (module spec §5.2).
        """
        user_repo = AsyncMock()
        user_repo.get_by_id.return_value = _make_user({"status": UserStatus.SUSPENDED})

        token = create_access_token(user_id=uuid.uuid4(), hospital_id=uuid.uuid4())

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                credentials=_auth_credentials(token),
                user_repo=user_repo,
            )
        assert exc_info.value.status_code == 403

    async def test_valid_token_returns_the_user(self: Any) -> None:
        """A valid token resolves to the active user."""
        user = _make_user()
        user_repo = AsyncMock()
        user_repo.get_by_id.return_value = user

        token = create_access_token(
            user_id=user.id,
            hospital_id=user.hospital_id,
        )

        resolved = await get_current_user(
            credentials=_auth_credentials(token),
            user_repo=user_repo,
        )
        assert resolved.id == user.id


class TestRequirePermission:
    """The authorization gate that maps a permission code to a 403."""

    async def test_valid_token_missing_the_permission_returns_403(self: Any) -> None:
        """An authenticated user without the code gets 403 PERMISSION_DENIED."""
        user = _make_user()  # grants only user.read
        checker = require_permission("patient.read")

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=user)
        assert exc_info.value.status_code == 403
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error_code"] == "PERMISSION_DENIED"

    async def test_valid_token_with_the_permission_passes(self: Any) -> None:
        """An authenticated user holding the code is admitted."""
        user = _make_user()  # grants user.read
        checker = require_permission("user.read")

        resolved = await checker(current_user=user)
        assert resolved.id == user.id

    async def test_super_admin_bypasses_the_check(self: Any) -> None:
        """A user with no hospital_id is a Super Admin and passes any code.

        ``docs/modules/02-user-management.md`` §3: Super Admin acts across all
        hospitals and carries every permission implicitly.
        """
        user = _make_user({"hospital_id": None})
        checker = require_permission("anything.at.all")

        resolved = await checker(current_user=user)
        assert resolved.id == user.id


class TestRequireAnyPermission:
    """The gate for endpoints that a full permission and a narrower one both open."""

    async def test_holding_one_of_the_codes_passes(self: Any) -> None:
        """A user holding any listed code is admitted."""
        user = _make_user()  # grants only user.read
        checker = require_any_permission("patient.read", "user.read")

        resolved = await checker(current_user=user)
        assert resolved.id == user.id

    async def test_holding_none_of_the_codes_returns_403(self: Any) -> None:
        """A user holding none of them gets 403, naming what would have sufficed."""
        user = _make_user()  # grants only user.read
        checker = require_any_permission("invoice.read", "invoice.read.own")

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=user)
        assert exc_info.value.status_code == 403
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error_code"] == "PERMISSION_DENIED"
        assert "invoice.read, invoice.read.own" in detail["message"]

    async def test_a_user_with_no_roles_returns_403(self: Any) -> None:
        """No roles means no permissions, not an error."""
        user = _make_user({"user_roles": None})
        checker = require_any_permission("user.read")

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=user)
        assert exc_info.value.status_code == 403

    async def test_super_admin_bypasses_the_check(self: Any) -> None:
        """A Super Admin passes, exactly as with :func:`require_permission`."""
        user = _make_user({"hospital_id": None})
        checker = require_any_permission("anything.at.all")

        resolved = await checker(current_user=user)
        assert resolved.id == user.id

    def test_naming_no_codes_is_a_programming_error(self: Any) -> None:
        """An endpoint that requires "any of nothing" would admit no one silently."""
        with pytest.raises(ValueError, match="at least one permission code"):
            require_any_permission()


class TestUserHasPermission:
    """The non-refusing check used to tell a full permission from a narrow one."""

    def test_reports_held_and_missing_codes(self: Any) -> None:
        user = _make_user()  # grants only user.read

        assert user_has_permission(user, "user.read") is True
        assert user_has_permission(user, "invoice.read") is False
        assert user_permission_codes(user) == {"user.read"}

    def test_super_admin_holds_everything(self: Any) -> None:
        user = _make_user({"hospital_id": None})

        assert user_has_permission(user, "anything.at.all") is True

    async def test_agrees_with_require_permission(self: Any) -> None:
        # The two must never disagree, or a route could admit a caller the
        # narrowing check then treats as holding the wider code (or the reverse).
        user = _make_user()

        for code in ("user.read", "patient.read"):
            checker = require_permission(code)
            try:
                await checker(current_user=user)
                admitted = True
            except HTTPException:
                admitted = False
            assert user_has_permission(user, code) is admitted
