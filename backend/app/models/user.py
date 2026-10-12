"""User model, user status enum, and the user-to-role join table.

Every user belongs to exactly one hospital. Users are soft-deleted rather than
permanently removed. Column conventions live in :mod:`app.models.base`.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for Mapped[uuid.UUID] resolution
from datetime import datetime  # noqa: TC003 — needed at runtime for Mapped[datetime] resolution
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.models.base import Base, CommonColumnsMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.utils.validators import normalize_email

if TYPE_CHECKING:
    from app.models.hospital import Hospital
    from app.models.refresh_token import RefreshToken
    from app.models.role import Role


class UserStatus(StrEnum):
    """Valid states for a user account."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    INVITED = "invited"


class User(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """A user who can authenticate and interact with the system.

    Every user belongs to exactly one hospital (Super Admin is a future
    consideration). Users are soft-deleted rather than permanently removed.
    """

    __tablename__ = "users"

    __table_args__ = (
        UniqueConstraint("hospital_id", "email", name="uq_users_hospital_email"),
        # The effective identity rule (migration 0019): one live staff account
        # per lower-cased email across the whole platform. Login takes an email
        # and no hospital, so an address must name exactly one account.
        Index(
            "uq_users_email_normalized_active",
            func.lower(text("email")),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_users_phone", "phone"),
        Index("ix_users_hospital_status", "hospital_id", "status"),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="UUID of the hospital this user belongs to. NULL only for Super Admin.",
    )
    email: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Login email address, lower-cased. Unique across the platform among live users.",
    )
    phone: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="Phone number for contact and SMS notifications."
    )
    password_hash: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Argon2id password hash."
    )
    first_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="User's given name."
    )
    last_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="User's family name."
    )
    status: Mapped[UserStatus] = mapped_column(
        SQLEnum(
            UserStatus,
            name="user_status",
            create_type=True,
            # Persist the member *values* ('active'), not the member names
            # ('ACTIVE'). The user_status type is created with lowercase labels
            # in migration 0001, so without this every INSERT fails with
            # InvalidTextRepresentationError and every SELECT fails with
            # LookupError. Same pattern as Patient.gender.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=UserStatus.ACTIVE,
        comment="Current account status (active, suspended, invited).",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp of last successful login.",
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp of last password change.",
    )
    # LEGACY, UNUSED. The next two columns belonged to the account lockout that
    # the authentication throttle replaced (app/services/auth_throttle.py,
    # migration 0020). Nothing reads or writes them; an account cannot be
    # "locked". They stay only so that the schema does not change under a
    # rollback, and are to be dropped in a later migration.
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Consecutive failed login attempts.",
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Account is locked until this timestamp.",
    )
    mfa_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether MFA/TOTP is enabled.",
    )
    mfa_secret: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted TOTP secret. NULL if MFA is not set up.",
    )

    # ── Relationships ───────────────────────────────────────────────────
    # Every relationship touching `users` must name its join column: the audit
    # mixins add created_by/updated_by/deleted_by FKs to users.id on nearly
    # every table, so SQLAlchemy sees multiple FK paths and cannot guess.
    hospital: Mapped[Hospital] = relationship(
        "Hospital",
        back_populates="users",
        foreign_keys="User.hospital_id",
        lazy="joined",
    )
    user_roles: Mapped[list[UserRole]] = relationship(
        "UserRole",
        back_populates="user",
        foreign_keys="UserRole.user_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken",
        back_populates="user",
        foreign_keys="RefreshToken.user_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @validates("email")
    def _normalize_email(self, _key: str, value: str) -> str:
        """Store every staff email in its canonical, lower-cased form."""
        return normalize_email(value)

    @property
    def hospital_is_active(self) -> bool:
        """Whether this user's hospital is allowed to authenticate. Fails closed.

        ``True`` only when the hospital row is present and its ``is_active``
        is exactly ``True``. A user whose hospital cannot be seen — not
        loaded, filtered out, missing — is treated as belonging to an
        inactive hospital: doubt about hospital state denies.

        A principal with no ``hospital_id`` at all has no hospital to
        deactivate. No such row can exist today (the column is ``NOT NULL``).

        ``hospital`` is loaded with the user (``lazy="joined"``), so reading
        this issues no query.
        """
        if self.hospital_id is None:
            return True
        hospital = self.hospital
        return hospital is not None and hospital.is_active is True

    @property
    def full_name(self) -> str:
        """Full display name."""
        return f"{self.first_name} {self.last_name}"

    def __repr__(self) -> str:
        return f"<User id={self.id!s:.8} email={self.email!r} hospital={self.hospital_id!s:.8}>"


class UserRole(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Many-to-many join between :class:`User` and :class:`~app.models.role.Role`.

    Records which roles are assigned to which users, including
    who performed the assignment.
    """

    __tablename__ = "user_roles"

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_role"),
        Index("ix_user_roles_role_id", "role_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="UUID of the user being assigned a role.",
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
        comment="UUID of the role being assigned.",
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="UUID of the user who performed this assignment. NULL if system-assigned.",
    )

    # ── Relationships ───────────────────────────────────────────────────
    # `user_id` is the assignment target; `assigned_by` (and the audit columns)
    # also point at users.id, hence the explicit join column.
    user: Mapped[User] = relationship(
        "User",
        back_populates="user_roles",
        foreign_keys="UserRole.user_id",
        lazy="joined",
    )
    role: Mapped[Role] = relationship(
        "Role",
        back_populates="user_roles",
        foreign_keys="UserRole.role_id",
        lazy="joined",
    )

    def __repr__(self) -> str:
        return f"<UserRole user={self.user_id!s:.8} role={self.role_id!s:.8}>"
