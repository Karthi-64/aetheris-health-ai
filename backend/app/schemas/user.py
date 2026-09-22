"""Pydantic schemas for user management module.

Request and response models for the user management endpoints.
See ``docs/modules/02-user-management.md`` §9 for the API contract.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — used in Pydantic field annotations
from datetime import datetime  # noqa: TC003 — used in Pydantic field annotations

from pydantic import BaseModel, EmailStr, Field

# ── Request Schemas ─────────────────────────────────────────────────────────


class UserCreateRequest(BaseModel):
    """Create/invite a new user payload."""

    email: EmailStr
    first_name: str = Field(..., min_length=1, max_length=100, description="User's given name")
    last_name: str = Field(..., min_length=1, max_length=100, description="User's family name")
    phone: str | None = Field(
        None, pattern=r"^\+?[1-9]\d{1,14}$", description="Phone number in E.164 format"
    )
    role_ids: list[uuid.UUID] | None = Field(None, description="Initial role UUIDs to assign")


class UserUpdateRequest(BaseModel):
    """Update user profile payload."""

    first_name: str | None = Field(
        None, min_length=1, max_length=100, description="User's given name"
    )
    last_name: str | None = Field(
        None, min_length=1, max_length=100, description="User's family name"
    )
    phone: str | None = Field(
        None, pattern=r"^\+?[1-9]\d{1,14}$", description="Phone number in E.164 format"
    )


class UserProfileUpdateRequest(BaseModel):
    """Own profile update payload."""

    first_name: str | None = Field(None, min_length=1, max_length=100)
    last_name: str | None = Field(None, min_length=1, max_length=100)
    phone: str | None = Field(None, pattern=r"^\+?[1-9]\d{1,14}$")


class AssignRoleRequest(BaseModel):
    """Role assignment payload."""

    role_id: uuid.UUID = Field(..., description="UUID of the role to assign")


# ── Response Schemas ────────────────────────────────────────────────────────


class UserResponse(BaseModel):
    """Full user profile returned in user management responses."""

    id: uuid.UUID
    email: str
    first_name: str
    last_name: str
    phone: str | None = None
    status: str
    hospital_id: uuid.UUID | None = None
    roles: list[RoleResponse] = []
    mfa_enabled: bool
    last_login_at: datetime | None = None
    password_changed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RoleResponse(BaseModel):
    """Role summary returned in user responses."""

    id: uuid.UUID
    name: str
    description: str | None = None
    is_system: bool = False

    model_config = {"from_attributes": True}


class UserListResponse(BaseModel):
    """Paginated user list response."""

    items: list[UserResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class PermissionResponse(BaseModel):
    """Permission catalog response."""

    id: uuid.UUID
    code: str
    description: str | None = None
    module: str

    model_config = {"from_attributes": True}
