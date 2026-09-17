"""User management service — create, read, update, deactivate users, manage roles.

Implements the business rules from ``docs/modules/02-user-management.md``.
Every rule is enforced here, never in the route layer.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from app.core.audit import AuditEvent
from app.core.config import settings
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.security import generate_opaque_token, hash_password
from app.models.user import User, UserStatus

if TYPE_CHECKING:
    from app.core.audit import AuditSink
    from app.database.unit_of_work import UnitOfWork
    from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
    from app.repositories.permission_repository import PermissionRepository
    from app.repositories.role_repository import RoleRepository
    from app.repositories.user_repository import UserRepository
    from app.services.auth_service import AuthService

logger = structlog.get_logger(__name__)


class UserService:
    """Handles user lifecycle and role management.

    :param user_repo: Repository for user data access.
    :param role_repo: Repository for role data access.
    :param permission_repo: Repository for permission data access.
    :param auth_service: Auth service for token revocation.
    :param audit: Where mutating operations are recorded (CLAUDE.md rule 9).
    """

    def __init__(
        self,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        permission_repo: PermissionRepository,
        auth_service: AuthService,
        uow: UnitOfWork,
        audit: AuditSink,
        password_reset_repo: PasswordResetTokenRepository,
    ) -> None:
        self._user_repo = user_repo
        self._role_repo = role_repo
        self._permission_repo = permission_repo
        self._auth_service = auth_service
        self._uow = uow
        self._audit = audit
        self._password_reset_repo = password_reset_repo

    # ── Read ─────────────────────────────────────────────────────────────────

    async def get_user(
        self, user_id: uuid.UUID, actor_hospital_id: uuid.UUID | None = None
    ) -> User:
        """Retrieve a single user by ID.

        :param user_id: The user's UUID.
        :param actor_hospital_id: The hospital ID of the requesting user for isolation.
        :returns: The user instance.
        :raises NotFoundError: If the user doesn't exist or is soft-deleted.
        :raises PermissionDeniedError: If cross-hospital access is attempted.
        """
        user = await self._user_repo.get_by_id(user_id)
        if user is None:
            raise NotFoundError("User not found.")

        # Multi-tenant isolation: ensure user belongs to the same hospital
        if actor_hospital_id is not None and user.hospital_id != actor_hospital_id:
            raise NotFoundError("User not found.")

        return user

    async def list_users(
        self,
        hospital_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 25,
        status: UserStatus | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """List users in a hospital with pagination and optional filters.

        :param hospital_id: The hospital UUID for tenant isolation.
        :param page: 1-based page number.
        :param page_size: Records per page (max 100).
        :param status: Optional status filter.
        :param search: Optional search query for name/email.
        :returns: Dict with ``items``, ``total``, ``page``, ``page_size``, ``total_pages``.
        """
        limit = min(page_size, 100)
        offset = (page - 1) * limit

        # Get users with filters
        users = await self._user_repo.list_by_hospital(
            hospital_id,
            skip=offset,
            limit=limit,
            status=status,
            search=search,
        )

        total = await self._user_repo.count_by_hospital(hospital_id, status=status, search=search)

        # Enrich with roles
        user_list = []
        for u in users:
            roles = [
                {
                    "id": r.role.id,
                    "name": r.role.name,
                    "description": r.role.description,
                    "is_system": r.role.is_system,
                }
                for r in (u.user_roles or [])
            ]
            user_list.append(
                {
                    "id": u.id,
                    "email": u.email,
                    "first_name": u.first_name,
                    "last_name": u.last_name,
                    "phone": u.phone,
                    "status": u.status.value,
                    "hospital_id": u.hospital_id,
                    "roles": roles,
                    "mfa_enabled": u.mfa_enabled,
                    "last_login_at": u.last_login_at,
                    "password_changed_at": u.password_changed_at,
                    "created_at": u.created_at,
                    "updated_at": u.updated_at,
                }
            )

        return {
            "items": user_list,
            "total": total,
            "page": page,
            "page_size": limit,
            "total_pages": math.ceil(total / limit) if limit > 0 else 0,
        }

    async def get_current_user_profile(self, user_id: uuid.UUID) -> User:
        """Get the current user's full profile.

        :param user_id: The user's UUID.
        :returns: The user instance.
        :raises NotFoundError: If the user doesn't exist.
        """
        user = await self._user_repo.get_by_id(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        return user

    # ── Create / Invite ──────────────────────────────────────────────────────

    async def invite_user(
        self,
        hospital_id: uuid.UUID,
        email: str,
        first_name: str,
        last_name: str,
        phone: str | None = None,
        role_ids: list[uuid.UUID] | None = None,
        actor_permissions: list[str] | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> tuple[User, str]:
        """Invite a new user to the system.

        Creates the user with ``status=invited`` and mints a single-use
        invitation token (Week 1 handoff B6) that ``reset_password`` accepts
        to transition the account ``INVITED -> ACTIVE``. The raw token is
        returned so the Notifications module can deliver it; email delivery
        itself is out of scope.

        :param hospital_id: The hospital UUID.
        :param email: The user's email address.
        :param first_name: The user's given name.
        :param last_name: The user's family name.
        :param phone: Optional phone number.
        :param role_ids: Optional list of initial role UUIDs.
        :param actor_permissions: Permissions of the actor performing the invite.
        :param actor_id: UUID of the acting user, for the audit trail.
        :returns: A ``(user, invite_token)`` pair. The token is single-use and
            expires after ``INVITE_TOKEN_TTL_HOURS``.
        :raises BusinessRuleError: If the email already exists in the hospital.
        :raises PermissionDeniedError: If the actor doesn't have ``user.create``.
        :raises NotFoundError: If any requested role id does not exist or
            belongs to another hospital (all-or-nothing, B5).
        """
        # Check permission
        if not actor_permissions or "user.create" not in actor_permissions:
            raise PermissionDeniedError("You do not have permission to create users.")

        # Check for duplicate email — a 409, not a 400: the request is well-formed
        # but conflicts with current state (docs/06-API_STANDARDS.md §14; the
        # endpoint contract documents 409 for this case).
        existing = await self._user_repo.get_by_email(hospital_id, email)
        if existing is not None:
            raise ConflictError("A user with this email already exists in this hospital.")

        # B5: validate every requested role BEFORE creating the user — the
        # invite is all-or-nothing. Unknown ids and another hospital's roles
        # both fail the whole operation instead of being silently skipped.
        validated_roles: list[uuid.UUID] = []
        if role_ids:
            unknown_ids: list[str] = []
            for role_id in role_ids:
                role = await self._role_repo.get_by_id(role_id)
                if role is None:
                    unknown_ids.append(str(role_id))
                    continue
                # System roles (hospital_id NULL) are visible to every tenant;
                # hospital-scoped roles must belong to this hospital.
                if role.hospital_id is not None and role.hospital_id != hospital_id:
                    unknown_ids.append(str(role_id))
                    continue
                validated_roles.append(role_id)
            if unknown_ids:
                raise NotFoundError(
                    "One or more roles were not found in this hospital.",
                    detail={"role_ids": unknown_ids},
                )

            # BR-8: an invite grants permissions just as an assignment does, and
            # the caller receives the invite token — so the same escalation
            # guard applies before the user row is written.
            for role_id in validated_roles:
                await self._assert_grantable(role_id, actor_permissions, hospital_id)

        # Create user without a password (they'll set it via the invite link)
        user = await self._user_repo.create(
            hospital_id=hospital_id,
            email=email,
            password_hash=hash_password(uuid.uuid4().hex),  # placeholder password
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            status=UserStatus.INVITED,
        )

        # Assign validated roles (all-or-nothing enforced above)
        for role_id in validated_roles:
            if not await self._user_repo.has_role(user.id, role_id):
                # BR-9: record who granted the role.
                await self._user_repo.add_role(user.id, role_id, assigned_by=actor_id)

        # The ``user_roles`` collection was eager-loaded when the row was
        # created — before any role was inserted — so the instance this method
        # returns would otherwise report ``roles: []`` even though the rows
        # exist. Refresh so the API response reflects reality.
        if validated_roles:
            await self._user_repo.refresh(user)

        # B6: mint the single-use invitation token (stored hashed) and hand
        # the raw token to the caller for the Notifications module to deliver.
        raw_token, token_hash = generate_opaque_token()
        expires_at = datetime.now(UTC) + timedelta(hours=settings.INVITE_TOKEN_TTL_HOURS)
        await self._password_reset_repo.create(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )

        await self._audit.record(
            AuditEvent(
                action="user.invited",
                hospital_id=hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=actor_id,
            )
        )
        await self._uow.commit()
        logger.info("user_invited", user_id=str(user.id), hospital_id=str(hospital_id))
        return user, raw_token

    # ── Update ───────────────────────────────────────────────────────────────

    async def update_user(
        self,
        user_id: uuid.UUID,
        actor_hospital_id: uuid.UUID | None = None,
        actor_permissions: list[str] | None = None,
        actor_id: uuid.UUID | None = None,
        **updates: Any,
    ) -> User:
        """Update a user's profile fields.

        :param user_id: The user's UUID.
        :param actor_hospital_id: The hospital ID of the requesting user.
        :param actor_permissions: Permissions of the actor.
        :param actor_id: UUID of the acting user, for the audit trail.
        :param updates: Fields to update (first_name, last_name, phone).
        :returns: The updated user instance.
        :raises NotFoundError: If the user doesn't exist.
        :raises PermissionDeniedError: If the actor doesn't have ``user.update``.
        """
        user = await self.get_user(user_id, actor_hospital_id)

        if not actor_permissions or "user.update" not in actor_permissions:
            raise PermissionDeniedError("You do not have permission to update users.")

        # Only allow updating allowed fields
        allowed_fields = {"first_name", "last_name", "phone"}
        safe_updates = {k: v for k, v in updates.items() if k in allowed_fields and v is not None}

        if safe_updates:
            user = await self._user_repo.update(user, **safe_updates)
            await self._audit.record(
                AuditEvent(
                    action="user.updated",
                    hospital_id=user.hospital_id,
                    target_type="user",
                    target_id=user.id,
                    actor_id=actor_id,
                    # Field *names* only — never values, per the audit PII rule
                    # (docs/07-SECURITY.md, rule 10).
                    changes={field: {"before": None, "after": None} for field in safe_updates},
                )
            )
            await self._uow.commit()
            logger.info("user_updated", user_id=str(user.id), fields=list(safe_updates.keys()))

        return user

    async def update_own_profile(self, user_id: uuid.UUID, **updates: Any) -> User:
        """Update the current user's own profile.

        :param user_id: The user's UUID.
        :param updates: Fields to update (first_name, last_name, phone).
        :returns: The updated user instance.
        """
        user = await self.get_user(user_id)

        allowed_fields = {"first_name", "last_name", "phone"}
        safe_updates = {k: v for k, v in updates.items() if k in allowed_fields and v is not None}

        if safe_updates:
            user = await self._user_repo.update(user, **safe_updates)
            await self._audit.record(
                AuditEvent(
                    action="user.updated",
                    hospital_id=user.hospital_id,
                    target_type="user",
                    target_id=user.id,
                    actor_id=user_id,
                    changes={field: {"before": None, "after": None} for field in safe_updates},
                )
            )
            await self._uow.commit()
            logger.info("own_profile_updated", user_id=str(user.id))

        return user

    # ── Deactivate / Reactivate ──────────────────────────────────────────────

    async def deactivate_user(
        self,
        user_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        actor_hospital_id: uuid.UUID | None = None,
        actor_permissions: list[str] | None = None,
    ) -> User:
        """Deactivate (suspend) a user.

        :param user_id: The user's UUID.
        :param actor_user_id: The UUID of the user performing the action.
        :param actor_hospital_id: The acting user's hospital for tenant isolation.
        :param actor_permissions: Permissions of the actor.
        :returns: The updated user instance.
        :raises BusinessRuleError: If trying to deactivate oneself.
        :raises PermissionDeniedError: If the actor doesn't have ``user.deactivate``.
        :raises NotFoundError: If the user doesn't exist or is outside the actor's hospital.
        """
        if not actor_permissions or "user.deactivate" not in actor_permissions:
            raise PermissionDeniedError("You do not have permission to deactivate users.")

        if user_id == actor_user_id:
            raise BusinessRuleError("You cannot deactivate yourself.")

        user = await self.get_user(user_id, actor_hospital_id)

        await self._user_repo.update(user, status=UserStatus.SUSPENDED)

        # Revoke all sessions
        await self._auth_service.logout_all(user_id)

        await self._audit.record(
            AuditEvent(
                action="user.deactivated",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=actor_user_id,
            )
        )
        await self._uow.commit()
        logger.info("user_deactivated", user_id=str(user_id), actor_id=str(actor_user_id))
        return user

    async def reactivate_user(
        self,
        user_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        actor_hospital_id: uuid.UUID | None = None,
        actor_permissions: list[str] | None = None,
    ) -> User:
        """Reactivate a suspended user.

        :param user_id: The user's UUID.
        :param actor_id: UUID of the acting user, for the audit trail.
        :param actor_hospital_id: The acting user's hospital for tenant isolation.
        :param actor_permissions: Permissions of the actor.
        :returns: The updated user instance.
        :raises PermissionDeniedError: If the actor doesn't have ``user.deactivate``.
        :raises NotFoundError: If the user doesn't exist or is outside the actor's hospital.
        """
        if not actor_permissions or "user.deactivate" not in actor_permissions:
            raise PermissionDeniedError("You do not have permission to reactivate users.")

        user = await self.get_user(user_id, actor_hospital_id)

        await self._user_repo.update(
            user, status=UserStatus.ACTIVE, failed_login_attempts=0, locked_until=None
        )

        await self._audit.record(
            AuditEvent(
                action="user.reactivated",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=actor_id,
            )
        )
        await self._uow.commit()
        logger.info("user_reactivated", user_id=str(user_id))
        return user

    # ── Role Management ──────────────────────────────────────────────────────

    async def _assert_grantable(
        self,
        role_id: uuid.UUID,
        actor_permissions: list[str] | None,
        actor_hospital_id: uuid.UUID | None,
    ) -> None:
        """Refuse to hand out permissions the actor does not already hold.

        Business rule 8 of ``docs/modules/02-user-management.md``: "Users cannot
        grant themselves permissions they don't already have (no privilege
        escalation)", restated by §5.3 step 2, FR-3 and AC-3. Without this an
        actor holding only ``role.assign`` can grant *themselves* any seeded
        role and inherit its whole permission set — and because ``POST /users``
        returns the invite token, the same trick works by inviting a new user
        into a privileged role and then claiming that account.

        The check covers both entry points (invite and assign), so the role
        catalog is the only thing that bounds an admin's reach.

        :param role_id: The role about to be granted.
        :param actor_permissions: Permission codes the actor currently holds.
        :param actor_hospital_id: The actor's hospital, for tenant scoping.
        :raises NotFoundError: If the role is not visible to the actor's tenant.
        :raises PermissionDeniedError: If the role grants anything the actor lacks.
        """
        role = await self._role_repo.get_with_permissions(role_id, hospital_id=actor_hospital_id)
        if role is None:
            raise NotFoundError("Role not found.")

        granted = {rp.permission.code for rp in role.role_permissions if rp.permission is not None}
        missing = sorted(granted - set(actor_permissions or []))
        if missing:
            logger.warning(
                "privilege_escalation_blocked",
                role_id=str(role_id),
                missing_permissions=missing,
            )
            raise PermissionDeniedError(
                "You cannot grant a role that includes permissions you do not hold.",
                detail={"role": role.name, "missing_permissions": missing},
            )

    async def assign_role(
        self,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        actor_permissions: list[str] | None = None,
        actor_id: uuid.UUID | None = None,
        actor_hospital_id: uuid.UUID | None = None,
    ) -> User:
        """Assign a role to a user.

        :param user_id: The user's UUID.
        :param role_id: The role's UUID.
        :param actor_permissions: Permissions of the actor.
        :param actor_id: UUID of the acting user, for the audit trail.
        :param actor_hospital_id: The acting user's hospital for tenant isolation.
        :returns: The updated user instance.
        :raises PermissionDeniedError: If the actor doesn't have ``role.assign``.
        :raises NotFoundError: If the user or role doesn't exist, or either
            belongs to another hospital (B1).
        """
        if not actor_permissions or "role.assign" not in actor_permissions:
            raise PermissionDeniedError("You do not have permission to assign roles.")

        user = await self.get_user(user_id, actor_hospital_id)

        role = await self._role_repo.get_by_id(role_id)
        if role is None:
            raise NotFoundError("Role not found.")

        # B1: the role must belong to the actor's hospital or be a system role
        # (system roles have a NULL hospital_id and are visible to every tenant).
        # A tenantless actor (actor_hospital_id is None, e.g. a system admin or
        # internal caller) is not constrained — mirroring ``get_user``.
        if (
            actor_hospital_id is not None
            and role.hospital_id is not None
            and role.hospital_id != actor_hospital_id
        ):
            raise NotFoundError("Role not found.")

        # BR-8 / FR-3 / AC-3: the actor may only hand out permissions they
        # already hold, so ``role.assign`` is not a route to more power.
        await self._assert_grantable(role_id, actor_permissions, actor_hospital_id)

        # Check if already assigned
        if await self._user_repo.has_role(user_id, role_id):
            logger.info("role_already_assigned", user_id=str(user_id), role_id=str(role_id))
            return user

        # BR-9: the join row records who granted the role and when.
        await self._user_repo.add_role(user_id, role_id, assigned_by=actor_id)

        # Revoke refresh tokens to force re-login with new claims
        await self._auth_service.logout_all(user_id)

        await self._audit.record(
            AuditEvent(
                action="role.assigned",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=actor_id,
                changes={"role_id": {"before": None, "after": str(role_id)}},
            )
        )
        await self._uow.commit()
        logger.info("role_assigned", user_id=str(user_id), role_id=str(role_id))
        return user

    async def _assert_not_last_administrator(self, user: User, role_id: uuid.UUID) -> None:
        """Block a role removal that would leave the hospital unadministrable.

        ``docs/modules/02-user-management.md`` §14: "Last remaining Hospital
        Admin tries to remove admin role → blocked". The guard keys on the
        capability rather than the role's name, because what actually locks a
        hospital out is losing every active holder of ``role.assign`` — after
        that nobody can grant the role back and the tenant needs operator
        intervention.

        Only active users count: an invited or suspended account cannot log in,
        so it cannot undo the lockout either.

        :param user: The user about to lose the role.
        :param role_id: The role being removed.
        :raises BusinessRuleError: If no other active administrator would remain.
        """
        if user.hospital_id is None:
            return  # Tenantless (Super Admin) — no hospital to lock out.

        role = await self._role_repo.get_with_permissions(role_id, hospital_id=user.hospital_id)
        if role is None:
            return  # Not visible here; ``remove_role`` will simply find nothing.

        grants_administration = any(
            rp.permission is not None and rp.permission.code == "role.assign"
            for rp in role.role_permissions
        )
        if not grants_administration:
            return

        others = await self._user_repo.count_other_active_holders(
            user.hospital_id, role_id, user.id
        )
        if others == 0:
            raise BusinessRuleError(
                "This is the hospital's last administrator — assign the role to "
                "someone else before removing it.",
                detail={"role": role.name},
            )

    async def remove_role(
        self,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        actor_permissions: list[str] | None = None,
        actor_id: uuid.UUID | None = None,
        actor_hospital_id: uuid.UUID | None = None,
    ) -> User:
        """Remove a role from a user.

        :param user_id: The user's UUID.
        :param role_id: The role's UUID.
        :param actor_permissions: Permissions of the actor.
        :param actor_id: UUID of the acting user, for the audit trail.
        :param actor_hospital_id: The acting user's hospital for tenant isolation.
        :returns: The updated user instance.
        :raises PermissionDeniedError: If the actor doesn't have ``role.assign``.
        :raises NotFoundError: If the user doesn't exist or is outside the actor's hospital.
        :raises BusinessRuleError: If this would strip the hospital's last
            administrator of the ability to administer roles (§14).
        """
        if not actor_permissions or "role.assign" not in actor_permissions:
            raise PermissionDeniedError("You do not have permission to remove roles.")

        user = await self.get_user(user_id, actor_hospital_id)

        await self._assert_not_last_administrator(user, role_id)

        removed = await self._user_repo.remove_role(user_id, role_id)
        if removed:
            await self._auth_service.logout_all(user_id)
            await self._audit.record(
                AuditEvent(
                    action="role.removed",
                    hospital_id=user.hospital_id,
                    target_type="user",
                    target_id=user.id,
                    actor_id=actor_id,
                    changes={"role_id": {"before": str(role_id), "after": None}},
                )
            )
            await self._uow.commit()
            logger.info("role_removed", user_id=str(user_id), role_id=str(role_id))

        return user

    async def list_user_roles(
        self, user_id: uuid.UUID, actor_hospital_id: uuid.UUID | None = None
    ) -> list[dict[str, Any]]:
        """List all roles assigned to a user.

        :param user_id: The user's UUID.
        :param actor_hospital_id: The acting user's hospital for tenant isolation.
        :returns: List of role dicts.
        :raises NotFoundError: If the user doesn't exist or is outside the actor's hospital.
        """
        user = await self.get_user(user_id, actor_hospital_id)

        return [
            {
                "id": r.role.id,
                "name": r.role.name,
                "description": r.role.description,
                "is_system": r.role.is_system,
            }
            for r in (user.user_roles or [])
        ]
