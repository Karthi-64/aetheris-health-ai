"""Authentication and authorization dependencies.

Provides ``get_current_user`` and ``require_permission(...)`` — the two
dependencies every non-public endpoint declares (``docs/07-SECURITY.md``,
rules 3 and 4).

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
