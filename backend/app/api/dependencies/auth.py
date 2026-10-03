"""Authentication and authorization dependencies.

Provides ``get_current_user`` and ``require_permission(...)`` — the two
dependencies every non-public endpoint declares (``docs/07-SECURITY.md``,
rules 3 and 4) — plus ``require_any_permission(...)`` for the few endpoints
that a full permission and a narrower one both open.

Usage::

    from fastapi import APIRouter, Depends
    from app.api.dependencies.auth import get_current_user, require_permission
    from app.models.user import User

    router = APIRouter()

    @router.get("/protected")
    async def protected_endpoint(
        current_user: User = Depends(get_current_user),
    ):
        ...

    @router.get("/patients")
    async def list_patients(
        current_user: User = Depends(require_permission("patient.read")),
    ):
        ...
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

# NOTE: FastAPI and HTTPException must be runtime imports (not TYPE_CHECKING).
# FastAPI resolves dependency signatures against the module's real globals.
import jwt as pyjwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.dependencies.repositories import get_user_repository
from app.core.error_codes import ErrorCode
from app.core.security import verify_access_token
from app.models.user import User, UserStatus
from app.repositories import UserRepository

if TYPE_CHECKING:
    pass

# HTTP Bearer token extractor
_bearer_scheme = HTTPBearer(
    bearerFormat="JWT",
    description="Enter your JWT access token",
    auto_error=False,
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    user_repo: UserRepository = Depends(get_user_repository),
) -> User:
    """Extract and validate the JWT from the Authorization header.

    Returns the authenticated :class:`User` instance.

    :param credentials: The Bearer token from the Authorization header.
    :param user_repo: Repository for user lookups.
    :returns: The authenticated user.
    :raises HTTPException: If the token is missing, invalid, or the user is not found.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "message": "Authentication required.",
                "error_code": ErrorCode.AUTHENTICATION_REQUIRED,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        payload = verify_access_token(token)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "message": "Token has expired.",
                "error_code": ErrorCode.AUTHENTICATION_REQUIRED,
            },
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except pyjwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "message": "Invalid token.",
                "error_code": ErrorCode.AUTHENTICATION_REQUIRED,
            },
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    # Validate token type
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "message": "Invalid token type.",
                "error_code": ErrorCode.AUTHENTICATION_REQUIRED,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Look up the user
    user_id = uuid.UUID(payload["sub"])
    user = await user_repo.get_by_id(user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "message": "User not found.",
                "error_code": ErrorCode.AUTHENTICATION_REQUIRED,
            },
        )

    if user.status != UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Account is not active.",
                "error_code": ErrorCode.PERMISSION_DENIED,
            },
        )

    return user


def require_permission(permission_code: str) -> Any:
    """Dependency factory that checks a specific permission.

    Usage::

        @router.get("/patients")
        async def list_patients(
            current_user: User = Depends(require_permission("patient.read")),
        ):
            ...

    :param permission_code: The permission code to check (e.g. ``"patient.read"``).
    :returns: A FastAPI dependency that resolves to the authenticated :class:`User`.
    :raises HTTPException: If the user lacks the required permission.
    """

    async def _check_permission(
        current_user: User = Depends(get_current_user),
    ) -> User:
        """Check that the user has the required permission.

        :param current_user: The authenticated user.
        :returns: The authenticated user if authorized.
        :raises HTTPException: If the user lacks the permission.
        """
        # Collect all permission codes for the user
        user_permissions: set[str] = set()
        for user_role in current_user.user_roles or []:
            role = user_role.role
            if role and role.role_permissions:
                for rp in role.role_permissions:
                    if rp.permission:
                        user_permissions.add(rp.permission.code)

        # Super Admin bypass (has all permissions implicitly)
        # A user with no hospital_id is a Super Admin
        if current_user.hospital_id is None:
            return current_user

        if permission_code not in user_permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "message": f"Permission denied. Required: {permission_code}.",
                    "error_code": ErrorCode.PERMISSION_DENIED,
                },
            )

        return current_user

    return _check_permission


def user_permission_codes(user: User) -> set[str]:
    """Collect every permission code a user holds through their roles.

    :param user: The authenticated user.
    :returns: The set of permission codes granted by the user's roles.
    """
    codes: set[str] = set()
    for user_role in user.user_roles or []:
        role = user_role.role
        if role and role.role_permissions:
            for rp in role.role_permissions:
                if rp.permission:
                    codes.add(rp.permission.code)
    return codes


def user_has_permission(user: User, permission_code: str) -> bool:
    """Check whether a user holds a permission, without refusing the request.

    For routes where a permission *widens* what a caller may do rather than
    gating the endpoint — e.g. ``invoice.read`` against ``invoice.read.own``.
    That cannot be a route dependency, because lacking the wider code must not
    refuse the request; it only narrows it.

    :param user: The authenticated user.
    :param permission_code: The permission code to look for.
    :returns: ``True`` if the user holds it. A Super Admin holds every
        permission implicitly, matching :func:`require_permission`.
    """
    if user.hospital_id is None:
        return True
    return permission_code in user_permission_codes(user)


def require_any_permission(*permission_codes: str) -> Any:
    """Dependency factory that passes a user holding **any** of the given codes.

    For endpoints that have a full permission and a narrower one — a billing
    clerk's ``invoice.read`` and a doctor's ``invoice.read.own`` both open
    ``GET /invoices``, and the route then narrows the result for the latter.
    An endpoint with a single permission keeps using :func:`require_permission`.

    Usage::

        @router.get("/invoices")
        async def list_invoices(
            current_user: User = Depends(
                require_any_permission("invoice.read", "invoice.read.own")
            ),
        ):
            ...

    :param permission_codes: The permission codes, any one of which suffices.
    :returns: A FastAPI dependency that resolves to the authenticated :class:`User`.
    :raises ValueError: If no codes are given — an endpoint must name what it requires.
    :raises HTTPException: If the user holds none of them.
    """
    if not permission_codes:
        msg = "require_any_permission needs at least one permission code."
        raise ValueError(msg)

    async def _check_any_permission(
        current_user: User = Depends(get_current_user),
    ) -> User:
        """Check that the user holds at least one of the permissions.

        :param current_user: The authenticated user.
        :returns: The authenticated user if authorized.
        :raises HTTPException: If the user holds none of the permissions.
        """
        if any(user_has_permission(current_user, code) for code in permission_codes):
            return current_user

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": f"Permission denied. Required one of: {', '.join(permission_codes)}.",
                "error_code": ErrorCode.PERMISSION_DENIED,
            },
        )

    return _check_any_permission
