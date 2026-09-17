"""Repository for the :class:`User` model and :class:`UserRole` join table.

Users belong to a hospital and support soft delete. All queries
automatically filter out soft-deleted records.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.user import User, UserRole, UserStatus
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import ColumnElement


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

    async def get_by_email(self, hospital_id: uuid.UUID, email: str) -> User | None:
        """Retrieve a user by email within a specific hospital.

        :param hospital_id: The hospital's UUID.
        :param email: The user's email address.
        :returns: The user instance, or ``None``.
        """
        stmt = self._query().where(User.hospital_id == hospital_id, User.email == email)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_by_email_cross_tenant(self, email: str) -> User | None:
        """Retrieve a user by email across all hospitals (login use case).

        :param email: The user's email address.
        :returns: The user instance, or ``None``.
        """
        stmt = select(User).where(User.email == email)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

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

    async def increment_failed_logins(self, user: User) -> User:
        """Increment the failed login attempt counter.

        :param user: The user instance to update.
        :returns: The updated user instance.
        """
        return await self.update(
            user,
            failed_login_attempts=user.failed_login_attempts + 1,
        )

    async def reset_failed_logins(self, user: User) -> User:
        """Reset the failed login attempt counter to zero.

        :param user: The user instance to update.
        :returns: The updated user instance.
        """
        return await self.update(user, failed_login_attempts=0, locked_until=None)

    async def lock_account(self, user: User, until: datetime) -> User:
        """Lock a user's account until the specified time.

        :param user: The user instance to lock.
        :param until: Timestamp until which the account is locked.
        :returns: The updated user instance.
        """
        return await self.update(user, locked_until=until)

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
