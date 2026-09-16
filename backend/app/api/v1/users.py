"""User management API routes — CRUD, invite, deactivate, role assignment.

See ``docs/modules/02-user-management.md`` §9 for the full API contract.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.dependencies.auth import get_current_user, require_permission
from app.api.dependencies.services import get_auth_service, get_user_service
from app.core.envelope import paginated_envelope, success_envelope
from app.models.user import User, UserStatus
from app.schemas.user import (
    AssignRoleRequest,
    UserCreateRequest,
    UserProfileUpdateRequest,
    UserUpdateRequest,
)
from app.services.auth_service import AuthService
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["Users"])


@router.get(
    "/me",
    summary="Get own profile",
    description="Return the authenticated user's profile.",
    responses={200: {"description": "User profile returned."}},
)
async def get_own_profile(
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """Get the current user's profile."""
    user = await user_service.get_current_user_profile(current_user.id)
    roles = [
        {
            "id": r.role.id,
            "name": r.role.name,
            "description": r.role.description,
            "is_system": r.role.is_system,
        }
        for r in (user.user_roles or [])
    ]

    return success_envelope(
        "User profile retrieved.",
        data=_user_to_dict(user, roles),
    )


@router.patch(
    "/me",
    summary="Update own profile",
    description="Update limited profile fields (name, phone).",
    responses={200: {"description": "Profile updated."}},
)
async def update_own_profile(
    payload: UserProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """Update the current user's own profile."""
    user = await user_service.update_own_profile(
        user_id=current_user.id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        phone=payload.phone,
    )
    roles = [
        {
            "id": r.role.id,
            "name": r.role.name,
            "description": r.role.description,
            "is_system": r.role.is_system,
        }
        for r in (user.user_roles or [])
    ]

    return success_envelope(
        "Profile updated.",
        data=_user_to_dict(user, roles),
    )


@router.get(
    "",
    summary="List users",
    description="List all users in the hospital with pagination and filters.",
    responses={200: {"description": "User list returned."}},
)
async def list_users(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=100, description="Records per page"),
    status: UserStatus | None = Query(None, description="Filter by status"),
    search: str | None = Query(None, description="Search query"),
    current_user: User = Depends(require_permission("user.read")),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """List users in the hospital (requires user.read permission)."""
    result = await user_service.list_users(
        hospital_id=current_user.hospital_id,
        page=page,
        page_size=page_size,
        status=status,
        search=search,
    )

    # docs/06-API_STANDARDS.md §5.2: `data` is the array of records and the
    # counts live in metadata.pagination. Passing the whole result dict as
    # `data` nested the array under data.items and duplicated the counts.
    return paginated_envelope(
        "Users retrieved.",
        data=result["items"],
        page=result["page"],
        page_size=result["page_size"],
        total_records=result["total"],
    )


@router.post(
    "",
    summary="Invite a user",
    description="Create a new user with an invited status.",
    status_code=201,
    responses={
        201: {
            "description": "User invited.",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "message": "User invited.",
                        "data": {
                            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "email": "nurse@hospital.test",
                            "first_name": "Priya",
                            "last_name": "Nair",
                            "phone": None,
                            "status": "invited",
                            "hospital_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "roles": [],
                            "mfa_enabled": False,
                            "last_login_at": None,
                            "password_changed_at": None,
                            "created_at": "2026-08-11T10:00:00Z",
                            "updated_at": "2026-08-11T10:00:00Z",
                            "invite_token": "<single-use credential - returned exactly once, deliver to the invitee, never log or echo>",
                        },
                        "metadata": {"request_id": None},
                    }
                }
            },
        },
        403: {"description": "Permission denied."},
        409: {"description": "Email already exists."},
    },
)
async def invite_user(
    payload: UserCreateRequest,
    current_user: User = Depends(require_permission("user.create")),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """Invite a new user.

    .. note::

        ``data.invite_token`` in the response is a **single-use credential**
        minted for the invited user. It is returned exactly once so the
        Notifications module can deliver it to the invitee's email — it must
        never be logged, stored, or echoed anywhere else
        (``docs/07-SECURITY.md``).
    """
    # Collect actor's permissions
    actor_permissions = _get_user_permission_codes(current_user)

    user, invite_token = await user_service.invite_user(
        hospital_id=current_user.hospital_id,
        email=payload.email,
        first_name=payload.first_name,
        last_name=payload.last_name,
        phone=payload.phone,
        role_ids=payload.role_ids,
        actor_permissions=actor_permissions,
        actor_id=current_user.id,
    )
    roles = [
        {
            "id": r.role.id,
            "name": r.role.name,
            "description": r.role.description,
            "is_system": r.role.is_system,
        }
        for r in (user.user_roles or [])
    ]

    data = _user_to_dict(user, roles)
    # B6 seam: the single-use invite token is handed back here so the
    # Notifications module can deliver it. Email delivery is out of scope.
    data["invite_token"] = invite_token

    return success_envelope(
        "User invited.",
        data=data,
    )


@router.get(
    "/{user_id}",
    summary="Get user by ID",
    description="Retrieve a specific user's profile.",
    responses={
        200: {"description": "User found."},
        404: {"description": "User not found."},
    },
)
async def get_user(
    user_id: str,
    current_user: User = Depends(require_permission("user.read")),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """Get a specific user's profile."""
    user = await user_service.get_user(
        user_id=uuid.UUID(user_id),
        actor_hospital_id=current_user.hospital_id,
    )
    roles = [
        {
            "id": r.role.id,
            "name": r.role.name,
            "description": r.role.description,
            "is_system": r.role.is_system,
        }
        for r in (user.user_roles or [])
    ]

    return success_envelope(
        "User retrieved.",
        data=_user_to_dict(user, roles),
    )


@router.patch(
    "/{user_id}",
    summary="Update user",
    description="Update a user's profile fields.",
    responses={
        200: {"description": "User updated."},
        403: {"description": "Permission denied."},
        404: {"description": "User not found."},
    },
)
async def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    current_user: User = Depends(require_permission("user.update")),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """Update a user's profile."""
    actor_permissions = _get_user_permission_codes(current_user)

    user = await user_service.update_user(
        user_id=uuid.UUID(user_id),
        actor_hospital_id=current_user.hospital_id,
        actor_permissions=actor_permissions,
        actor_id=current_user.id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        phone=payload.phone,
    )
    roles = [
        {
            "id": r.role.id,
            "name": r.role.name,
            "description": r.role.description,
            "is_system": r.role.is_system,
        }
        for r in (user.user_roles or [])
    ]

    return success_envelope(
        "User updated.",
        data=_user_to_dict(user, roles),
    )


@router.post(
    "/{user_id}/deactivate",
    summary="Deactivate a user",
    description="Suspend a user account and revoke all sessions.",
    responses={
        200: {"description": "User deactivated."},
        403: {"description": "Permission denied."},
        400: {"description": "Cannot deactivate yourself."},
    },
)
async def deactivate_user(
    user_id: str,
    current_user: User = Depends(require_permission("user.deactivate")),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """Deactivate a user."""
    user = await user_service.deactivate_user(
        user_id=uuid.UUID(user_id),
        actor_user_id=current_user.id,
        actor_hospital_id=current_user.hospital_id,
        actor_permissions=_get_user_permission_codes(current_user),
    )
    roles = [
        {
            "id": r.role.id,
            "name": r.role.name,
            "description": r.role.description,
            "is_system": r.role.is_system,
        }
        for r in (user.user_roles or [])
    ]

    return success_envelope(
        "User deactivated.",
        data=_user_to_dict(user, roles),
    )


@router.post(
    "/{user_id}/reactivate",
    summary="Reactivate a user",
    description="Reactivate a suspended user account.",
    responses={
        200: {"description": "User reactivated."},
        403: {"description": "Permission denied."},
    },
)
async def reactivate_user(
    user_id: str,
    current_user: User = Depends(require_permission("user.deactivate")),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """Reactivate a user."""
    user = await user_service.reactivate_user(
        user_id=uuid.UUID(user_id),
        actor_id=current_user.id,
        actor_hospital_id=current_user.hospital_id,
        actor_permissions=_get_user_permission_codes(current_user),
    )
    roles = [
        {
            "id": r.role.id,
            "name": r.role.name,
            "description": r.role.description,
            "is_system": r.role.is_system,
        }
        for r in (user.user_roles or [])
    ]

    return success_envelope(
        "User reactivated.",
        data=_user_to_dict(user, roles),
    )


@router.post(
    "/{user_id}/reset-password",
    summary="Admin reset user password",
    description="Admin-initiated password reset. Forces the user to change password on next login.",
    responses={
        200: {"description": "Password reset initiated."},
        403: {"description": "Permission denied."},
    },
)
async def admin_reset_password(
    user_id: str,
    current_user: User = Depends(require_permission("user.reset_password")),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """Admin-initiated password reset."""
    await auth_service.admin_reset_password(
        user_id=uuid.UUID(user_id),
        actor_id=current_user.id,
        actor_hospital_id=current_user.hospital_id,
    )
    return success_envelope(
        "Password reset initiated. The user will be required to set a new password on next login."
    )


@router.get(
    "/{user_id}/roles",
    summary="Get user roles",
    description="List all roles assigned to a user.",
    responses={200: {"description": "Roles retrieved."}},
)
async def get_user_roles(
    user_id: str,
    current_user: User = Depends(require_permission("user.read")),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """List roles assigned to a user."""
    roles = await user_service.list_user_roles(
        user_id=uuid.UUID(user_id),
        actor_hospital_id=current_user.hospital_id,
    )
    return success_envelope(
        "User roles retrieved.",
        data=roles,
    )


@router.post(
    "/{user_id}/roles",
    summary="Assign role to user",
    description="Assign a role to a user. Revokes all sessions to force re-login with new claims.",
    responses={
        200: {"description": "Role assigned."},
        403: {"description": "Permission denied."},
    },
)
async def assign_role(
    user_id: str,
    payload: AssignRoleRequest,
    current_user: User = Depends(require_permission("role.assign")),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """Assign a role to a user."""
    actor_permissions = _get_user_permission_codes(current_user)

    await user_service.assign_role(
        user_id=uuid.UUID(user_id),
        role_id=payload.role_id,
        actor_permissions=actor_permissions,
        actor_id=current_user.id,
        actor_hospital_id=current_user.hospital_id,
    )
    return success_envelope("Role assigned.")


@router.delete(
    "/{user_id}/roles/{role_id}",
    summary="Remove role from user",
    description="Remove a role from a user. Revokes all sessions to force re-login.",
    responses={
        200: {"description": "Role removed."},
        403: {"description": "Permission denied."},
    },
)
async def remove_role(
    user_id: str,
    role_id: str,
    current_user: User = Depends(require_permission("role.assign")),
    user_service: UserService = Depends(get_user_service),
) -> dict[str, Any]:
    """Remove a role from a user."""
    actor_permissions = _get_user_permission_codes(current_user)

    await user_service.remove_role(
        user_id=uuid.UUID(user_id),
        role_id=uuid.UUID(role_id),
        actor_permissions=actor_permissions,
        actor_id=current_user.id,
        actor_hospital_id=current_user.hospital_id,
    )
    return success_envelope("Role removed.")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _user_to_dict(user: User, roles: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert a User model to a response dict."""
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "status": user.status.value if user.status else "active",
        "hospital_id": user.hospital_id,
        "roles": roles,
        "mfa_enabled": user.mfa_enabled,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "password_changed_at": user.password_changed_at.isoformat()
        if user.password_changed_at
        else None,
        "created_at": user.created_at.isoformat()
        if hasattr(user, "created_at") and user.created_at
        else None,
        "updated_at": user.updated_at.isoformat()
        if hasattr(user, "updated_at") and user.updated_at
        else None,
    }


def _get_user_permission_codes(user: User) -> list[str]:
    """Get all permission codes for a user."""
    permissions: list[str] = []
    for user_role in user.user_roles or []:
        role = user_role.role
        if role and role.role_permissions:
            for rp in role.role_permissions:
                if rp.permission and rp.permission.code not in permissions:
                    permissions.append(rp.permission.code)
    return permissions
