"""Repository for the :class:`User` model and :class:`UserRole` join table.

Users belong to a hospital and support soft delete. All queries
automatically filter out soft-deleted records.
"""

from __future__ import annotations

import hashlib
import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, or_, select

from app.models.permission import Permission
from app.models.role import RolePermission
from app.models.user import User, UserRole, UserStatus
from app.repositories.base import BaseRepository
from app.utils.validators import normalize_email

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import ColumnElement


logger = structlog.get_logger(__name__)


class UserRepository(BaseRepository[User]):
    """Repository for user CRUD operations.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(User, session)

    @staticmethod
    def _search_predicate(search: str | None) -> ColumnElement[bool] | None:
        """Build the shared name/email search predicate, or ``None``.

        One helper drives both :meth:`list_by_hospital` and
        :meth:`count_by_hospital` so the two queries can never drift
        (Week 1 handoff B4).

        :param search: Optional search term.
        :returns: A case-insensitive ``ilike`` predicate over first/last
            name and email, or ``None`` when no search is requested.
        """
        if not search:
            return None
        pattern = f"%{search}%"
        return (
            User.first_name.ilike(pattern)
            | User.last_name.ilike(pattern)
            | User.email.ilike(pattern)
        )

    async def create(  # type: ignore[override]
        self,
        hospital_id: uuid.UUID,
        email: str,
        password_hash: str,
        first_name: str,
        last_name: str,
        **kwargs: object,
    ) -> User:
        """Create a new user.

        :param hospital_id: UUID of the parent hospital.
        :param email: Login email address (unique per hospital).
        :param password_hash: Argon2id hash of the user's password.
        :param first_name: User's given name.
        :param last_name: User's family name.
        :param kwargs: Additional optional fields (phone, status, etc.).
        :returns: The created user instance.
        """
        return await super().create(
            hospital_id=hospital_id,
            email=email,
            password_hash=password_hash,
            first_name=first_name,
            last_name=last_name,
            **kwargs,
        )

    @staticmethod
    def _email_hash(normalized_email: str) -> str:
        """A short, non-reversible tag for an address, for log lines (never the address)."""
        return hashlib.sha256(normalized_email.encode("utf-8")).hexdigest()[:16]

    async def get_by_email(self, hospital_id: uuid.UUID, email: str) -> User | None:
        """Retrieve the one live user with this email in a specific hospital.

        Compared in canonical (lower-cased) form. Fails closed like
        :meth:`get_by_email_cross_tenant`: if more than one live row matches,
        none is returned — never the first, never the newest.

        Not for "is this address free?" checks; use :meth:`email_is_taken`.

        :param hospital_id: The hospital's UUID.
        :param email: The user's email address, in any case.
        :returns: The single matching live user, or ``None``.
        """
        normalized = normalize_email(email)
        stmt = (
            self._query()
            .where(User.hospital_id == hospital_id, func.lower(User.email) == normalized)
            .limit(2)
        )
        result = await self._session.execute(stmt)
        matches = list(result.unique().scalars().all())
        if len(matches) > 1:
            logger.error("staff_identity_ambiguous", email_hash=self._email_hash(normalized))
            return None
        return matches[0] if matches else None

    async def get_by_email_cross_tenant(
        self, email: str, *, for_update: bool = False
    ) -> User | None:
        """Resolve the one live staff account an email names (login, password reset).

        Not hospital-scoped: a login request carries an email and nothing else,
        so the hospital is not known until the account is found.

        Fails closed. Soft-deleted users are never returned. If the address
        unexpectedly names more than one live account — which the unique index
        from migration 0019 forbids, but which must not be trusted blindly —
        nobody is returned and the anomaly is logged. Authentication must
        never pick one of several candidates, and must never raise.

        :param email: The email address, in any case.
        :param for_update: Lock the matched row until the transaction ends.
        :returns: The single matching live user, or ``None`` if there is none
            or the identity is ambiguous.
        """
        normalized = normalize_email(email)
        stmt = self._query().where(func.lower(User.email) == normalized).limit(2)
        if for_update:
            # `of=User`: the hospital is outer-joined for eager loading and
            # must not be locked. `populate_existing`: read the row as it is
            # now that the lock is held, not a copy cached earlier.
            stmt = stmt.with_for_update(of=User).execution_options(populate_existing=True)
        result = await self._session.execute(stmt)
        matches = list(result.unique().scalars().all())
        if len(matches) > 1:
            # A hash, never the address (PII), and never to the caller.
            logger.error("staff_identity_ambiguous", email_hash=self._email_hash(normalized))
            return None
        return matches[0] if matches else None

    @staticmethod
    def authentication_claim_key(email: str) -> int:
        """Return the advisory-lock key for one email address.

        Derived from the canonical address alone — never from whether an
        account exists — so the claim behaves identically for a real address
        and an unknown one. The prefix keeps it clear of any other use of
        advisory locks in this database.

        :param email: The email address, in any case.
        :returns: A signed 64-bit integer.
        """
        digest = hashlib.sha256(b"aetheris:staff-auth:" + normalize_email(email).encode("utf-8"))
        return int.from_bytes(digest.digest()[:8], "big", signed=True)

    async def claim_authentication_attempt(self, email: str) -> bool:
        """Claim the right to handle a password-reset request for an email.

        At most one reset request per address is handled at a time. The claim
        is a transaction-scoped advisory lock taken with *try* semantics: it
        never waits. If another request for the same address holds it, this
        returns ``False`` at once and the caller answers as it always does.

        It exists so that a burst of requests for a real account — each of
        which would write a token, an audit entry and an email — does no more
        work, and so takes no longer, than a burst for an unknown address.
        Sign-in does not use it: there, exclusivity would itself be a way to
        keep an account's owner out.

        It reads and writes no row, and is released when the transaction ends.

        :param email: The email address, in any case.
        :returns: ``True`` if this request may proceed.
        """
        result = await self._session.execute(
            select(func.pg_try_advisory_xact_lock(self.authentication_claim_key(email)))
        )
        return result.scalar_one() is True

    async def lock_for_authentication(self, user_id: uuid.UUID) -> User | None:
        """Load a live user by id and lock the row until the transaction ends.

        Taken only once a credential has been verified — before a session is
        issued, and when an emailed token is redeemed — so that a concurrent
        password change, suspension or re-sent invitation is seen either
        wholly before or wholly after. The id always comes from a server-side
        record. Soft-deleted users are never returned.

        Not hospital-scoped: the hospital is read from the row.

        :param user_id: The user's UUID.
        :returns: The locked, freshly-read user, or ``None``.
        """
        stmt = (
            self._query()
            .where(User.id == user_id)
            .with_for_update(of=User)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def email_is_taken(self, email: str, *, hospital_id: uuid.UUID) -> bool:
        """Whether a new staff account may **not** use this email.

        An address is unavailable when:

        * any live user on the platform — in any hospital — already has it
          (the platform-wide identity rule, migration 0019); or
        * any user of ``hospital_id``, including a soft-deleted one, has it —
          the older per-hospital constraint still covers deleted rows.

        The caller must run this under an explicit ``TenantScope.system(...)``:
        inside a staff request the session is confined to that staff member's
        hospital, and the whole point here is to see the others. It returns a
        boolean and nothing about *where* the address is in use.

        :param email: The email address, in any case.
        :param hospital_id: The hospital the new account would belong to.
        :returns: ``True`` if the address cannot be used.
        """
        normalized = normalize_email(email)
        stmt = (
            select(func.count())
            .select_from(User)
            .where(
                func.lower(User.email) == normalized,
                or_(User.deleted_at.is_(None), User.hospital_id == hospital_id),
            )
        )
        result = await self._session.execute(stmt)
        return bool(result.scalar_one())

    async def list_with_mfa_secret_cross_tenant(self) -> list[User]:
        """List every user that has a stored MFA secret, in every hospital.

        For MFA key rotation only (``app/services/mfa_key_rotation.py``): a
        retired key can be dropped from configuration only once no row is
        still encrypted under it, wherever that row is. Soft-deleted users are
        included for the same reason — their secret is still on disk.

        Must be run under an explicit ``TenantScope.system(...)``; it is never
        called from a request.

        :returns: Users whose ``mfa_secret`` is not ``NULL``.
        """
        stmt = select(User).where(User.mfa_secret.is_not(None)).order_by(User.id)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def list_by_hospital(
        self,
        hospital_id: uuid.UUID,
        *,
        skip: int = 0,
        limit: int = 100,
        status: UserStatus | None = None,
        search: str | None = None,
    ) -> list[User]:
        """List users belonging to a hospital, with optional filters.

        :param hospital_id: The hospital's UUID.
        :param skip: Number of records to skip.
        :param limit: Maximum records to return.
        :param status: Optional status filter.
        :param search: Optional search query matching name or email (case-insensitive).
        :returns: List of user instances.
        """
        stmt = self._query().where(User.hospital_id == hospital_id)
        if status is not None:
            stmt = stmt.where(User.status == status)
        predicate = self._search_predicate(search)
        if predicate is not None:
            stmt = stmt.where(predicate)
        stmt = self._apply_pagination(stmt, skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_by_hospital(
        self,
        hospital_id: uuid.UUID,
        status: UserStatus | None = None,
        search: str | None = None,
    ) -> int:
        """Count users in a hospital, optionally filtered by status and search.

        Applies the identical search predicate as :meth:`list_by_hospital` so
        ``total`` can never disagree with the rows on a filtered page (Week 1
        handoff B4).

        :param hospital_id: The hospital's UUID.
        :param status: Optional status filter.
        :param search: Optional search term (same match as the list).
        :returns: The user count.
        """
        stmt = select(User).where(User.hospital_id == hospital_id)
        if status is not None:
            stmt = stmt.where(User.status == status)
        predicate = self._search_predicate(search)
        if predicate is not None:
            stmt = stmt.where(predicate)
        return await self.count(stmt)

    async def record_login(self, user: User) -> User:
        """Update a user's last_login_at timestamp.

        :param user: The user instance to update.
        :returns: The updated user instance.
        """
        return await self.update(user, last_login_at=datetime.now(UTC))

    # ── UserRole Management ────────────────────────────────────────────────

    async def refresh(self, user: User) -> User:
        """Re-load a user row (and its selectin-loaded relationships) from the DB.

        ``user_roles`` is ``lazy="selectin"``: it loads when the instance is
        first fetched, so rows inserted later in the same session (role
        assignment during an invite) do not appear on the stale in-memory
        collection. Refreshing re-runs the eager loads so the instance the
        API serializes tells the truth.

        :param user: The user instance to re-load.
        :returns: The same instance, refreshed.
        """
        await self._session.refresh(user)
        return user

    async def has_role(self, user_id: uuid.UUID, role_id: uuid.UUID) -> bool:
        """Check if a user already has a specific role assigned.

        :param user_id: The user's UUID.
        :param role_id: The role's UUID.
        :returns: ``True`` if the user has the role.
        """
        stmt = select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none() is not None

    async def add_role(
        self, user_id: uuid.UUID, role_id: uuid.UUID, assigned_by: uuid.UUID | None = None
    ) -> UserRole:
        """Assign a role to a user.

        :param user_id: The user's UUID.
        :param role_id: The role's UUID.
        :param assigned_by: Optional UUID of the assigning user.
        :returns: The created UserRole instance.
        """
        user_role = UserRole(
            user_id=user_id,
            role_id=role_id,
            assigned_by=assigned_by,
        )
        self._session.add(user_role)
        await self._session.flush()
        return user_role

    async def remove_role(self, user_id: uuid.UUID, role_id: uuid.UUID) -> bool:
        """Remove a role from a user.

        :param user_id: The user's UUID.
        :param role_id: The role's UUID.
        :returns: ``True`` if a role was removed, ``False`` if it wasn't assigned.
        """
        stmt = select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
        )
        result = await self._session.execute(stmt)
        user_role = result.unique().scalar_one_or_none()
        if user_role is None:
            return False
        await self._session.delete(user_role)
        await self._session.flush()
        return True

    async def count_other_active_holders(
        self,
        hospital_id: uuid.UUID,
        role_id: uuid.UUID,
        exclude_user_id: uuid.UUID,
    ) -> int:
        """Count the hospital's *other* active users holding a given role.

        Backs the administrative-lockout guard (``02-user-management.md`` §14):
        before a role is stripped, the service asks whether anyone else would
        still hold it. ``invited`` and ``suspended`` users are not counted —
        neither can log in, so neither can unlock the hospital.

        :param hospital_id: The hospital to scope the count to.
        :param role_id: The role being given up.
        :param exclude_user_id: The user losing the role.
        :returns: Number of other active users in the hospital with that role.
        """
        stmt = (
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .where(
                User.hospital_id == hospital_id,
                User.status == UserStatus.ACTIVE,
                User.id != exclude_user_id,
                UserRole.role_id == role_id,
            )
        )
        return await self.count(stmt)

    async def list_active_recipients(
        self,
        hospital_id: uuid.UUID,
        *,
        permission_code: str | None = None,
        role_id: uuid.UUID | None = None,
    ) -> list[User]:
        """List a hospital's active users, optionally narrowed by role or permission.

        Backs the Notifications module's "who should hear about this" question
        (``docs/modules/11-notifications.md`` §5, FR-6): everyone, everyone
        holding a role, or everyone holding a permission through any role.
        Only ``active`` users are returned — an invited or suspended account
        cannot act on a notification.

        :param hospital_id: The hospital to scope to.
        :param permission_code: Only users holding this permission code.
        :param role_id: Only users holding this role.
        :returns: Matching users, ordered by id for a stable result.
        """
        stmt = self._query().where(
            User.hospital_id == hospital_id, User.status == UserStatus.ACTIVE
        )
        if role_id is not None:
            stmt = stmt.where(
                User.id.in_(select(UserRole.user_id).where(UserRole.role_id == role_id))
            )
        if permission_code is not None:
            holders = (
                select(UserRole.user_id)
                .join(RolePermission, RolePermission.role_id == UserRole.role_id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(Permission.code == permission_code)
            )
            stmt = stmt.where(User.id.in_(holders))
        result = await self._session.execute(stmt.order_by(User.id.asc()))
        return list(result.unique().scalars().all())
