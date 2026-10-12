"""Patient App identity: the account, its sign-in state, and its record links.

See ``docs/modules/15-patient-app.md`` §4 and §5. Three things are kept apart
on purpose:

* a **patient record** is a row in ``patients`` — owned by one hospital;
* a **patient account** (:class:`PatientAccount`) is a login identity keyed by
  a verified phone number — platform-wide, and never a ``users`` row, a role
  or a permission;
* a **record link** (:class:`PatientAccountLink`) is the verified statement
  "this account may act as this patient record" — inside one hospital.

**Four tables here carry no ``hospital_id``**, and that is the point rather
than an omission. :class:`PatientAccount`, :class:`PatientOtpChallenge`,
:class:`PatientRefreshToken` and :class:`PatientDevice` hold platform-level
identity data: an account exists before it is linked to any hospital, a code
is requested before any account exists, and a session or a recognised device
hangs off the account exactly as ``refresh_tokens`` and ``trusted_devices``
hang off a staff user. None of them holds anything a hospital recorded.
:class:`PatientAccountLink` is where an account meets tenant data, and it is
tenant-scoped like every other business table.

None of these tables uses the audit mixins: ``created_by`` / ``updated_by``
are foreign keys to ``users``, and no staff user ever writes these rows.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for Mapped[uuid.UUID] resolution
from datetime import datetime  # noqa: TC003 — needed at runtime for Mapped[datetime] resolution
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin

__all__ = [
    "LinkRelationship",
    "LinkVerification",
    "PatientAccount",
    "PatientAccountLink",
    "PatientAccountStatus",
    "PatientDevice",
    "PatientOtpChallenge",
    "PatientRefreshToken",
]


class PatientAccountStatus(StrEnum):
    """Lifecycle state of a patient account."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class LinkRelationship(StrEnum):
    """Whom a record link lets the account act for. V1 accepts only ``self``."""

    SELF = "self"


class LinkVerification(StrEnum):
    """How a record link was established (``docs/modules/15-patient-app.md`` §4.5)."""

    PHONE_DOB = "phone_dob"
    PHONE_DOB_MRN = "phone_dob_mrn"
    SELF_REGISTRATION = "self_registration"


class PatientAccount(UUIDPrimaryKeyMixin, Base):
    """A Patient App login identity, keyed by a verified phone number.

    Created only by a successful one-time-code verification. Carries no
    hospital: which records it may see is decided by its links.
    """

    __tablename__ = "patient_accounts"

    __table_args__ = (
        UniqueConstraint("phone", name="uq_patient_accounts_phone"),
        CheckConstraint("status IN ('active', 'suspended', 'closed')", name="status"),
    )

    phone: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="Verified phone number in E.164 form."
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PatientAccountStatus.ACTIVE.value,
        server_default=PatientAccountStatus.ACTIVE.value,
        comment="active / suspended / closed.",
    )
    phone_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the phone number was last proven by a one-time code.",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the account last signed in."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @property
    def is_active(self) -> bool:
        """True if the account may sign in and be used."""
        return self.status == PatientAccountStatus.ACTIVE.value

    def __repr__(self) -> str:
        # The phone number is PII and is deliberately absent.
        return f"<PatientAccount id={self.id!s:.8} status={self.status}>"


class PatientOtpChallenge(UUIDPrimaryKeyMixin, Base):
    """One request for a sign-in code.

    The code itself is never stored: ``code_hash`` is an HMAC-SHA-256 over the
    challenge id, the phone and the code, keyed by ``PATIENT_OTP_SECRET``. A
    code therefore verifies only the challenge it was issued for.
    """

    __tablename__ = "patient_otp_challenges"

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts"),
        Index("ix_patient_otp_challenges_expires_at", "expires_at"),
    )

    phone: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="Number the code was sent to, in E.164 form."
    )
    code_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="HMAC-SHA-256 (hex) of challenge id, phone and code."
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="The code is dead after this instant."
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Verification attempts made against this challenge.",
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the right code was presented. A consumed challenge is dead.",
    )
    ip_address: Mapped[str | None] = mapped_column(
        INET, nullable=True, comment="Address the code was requested from."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PatientRefreshToken(UUIDPrimaryKeyMixin, Base):
    """Server-side store of a patient session's rotating refresh token.

    Same shape and rules as ``refresh_tokens``: only the SHA-256 of the opaque
    token is stored, every refresh replaces the row, and presenting a revoked
    token ends every session of the account.
    """

    __tablename__ = "patient_refresh_tokens"

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_patient_refresh_tokens_token_hash"),
        Index("ix_patient_refresh_tokens_account_id", "account_id"),
        Index("ix_patient_refresh_tokens_expires_at", "expires_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patient_accounts.id", ondelete="CASCADE"),
        nullable=False,
        comment="The account this session belongs to.",
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="SHA-256 (hex) of the opaque refresh token."
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Seven days from issuance."
    )
    is_revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_by_token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "patient_refresh_tokens.id",
            ondelete="SET NULL",
            # Named here: the conventional name is over PostgreSQL's 63 characters.
            name="fk_patient_refresh_tokens_rotated_by_token_id",
        ),
        nullable=True,
        comment="The token that replaced this one. NULL: not rotated.",
    )
    device_info: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="User-agent at issuance."
    )
    ip_address: Mapped[str | None] = mapped_column(
        INET, nullable=True, comment="Address at issuance."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PatientDevice(UUIDPrimaryKeyMixin, Base):
    """A browser that has completed a sign-in to one patient account.

    The counterpart of ``trusted_devices``. It is not a credential: it decides
    only which throttle bucket a request for a code is charged to, so that
    somebody who merely knows a phone number cannot use up the allowance of
    the browser its owner signs in from. Only the SHA-256 of the cookie token
    is stored, and the row's ``id`` — never the token — names the bucket.
    """

    __tablename__ = "patient_devices"

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_patient_devices_token_hash"),
        Index("ix_patient_devices_account_id", "account_id"),
        Index("ix_patient_devices_expires_at", "expires_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patient_accounts.id", ondelete="CASCADE"),
        nullable=False,
        comment="The account this browser has signed in to.",
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="SHA-256 (hex) of the device token in the cookie."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="After this the browser is no longer recognised.",
    )


class PatientAccountLink(UUIDPrimaryKeyMixin, Base):
    """The statement that an account may act as one patient record.

    Tenant-scoped: ``hospital_id`` is the hospital that owns the record. A
    link is *active* while ``unlinked_at`` is NULL. Whether an active link is
    *honoured* is decided at read time (the phone-binding rule, §4.4) and is
    never stored.

    Two partial unique indexes carry the cardinality rules under concurrency:
    one active link per patient record, and one active ``self`` link per
    account and hospital.
    """

    __tablename__ = "patient_account_links"

    __table_args__ = (
        CheckConstraint("relationship IN ('self')", name="relationship"),
        CheckConstraint(
            "verified_via IN ('phone_dob', 'phone_dob_mrn', 'self_registration')",
            name="verified_via",
        ),
        Index(
            "uq_patient_account_links_active_patient",
            "patient_id",
            unique=True,
            postgresql_where=text("unlinked_at IS NULL"),
        ),
        Index(
            "uq_patient_account_links_active_self",
            "account_id",
            "hospital_id",
            unique=True,
            postgresql_where=text("unlinked_at IS NULL AND relationship = 'self'"),
        ),
        Index("ix_patient_account_links_account_id", "account_id"),
        Index("ix_patient_account_links_hospital_patient", "hospital_id", "patient_id"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patient_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        comment="The account that may act as the record.",
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="RESTRICT"),
        nullable=False,
        comment="The patient record.",
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="The hospital (tenant) that owns the record.",
    )
    relationship: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=LinkRelationship.SELF.value,
        server_default=LinkRelationship.SELF.value,
        comment="Whom the account acts for. 'self' only in V1.",
    )
    verified_via: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="How the link was established."
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    unlinked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the link ended. NULL: active."
    )
    unlink_reason: Mapped[str | None] = mapped_column(
        String(40), nullable=True, comment="Why the link ended."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @property
    def is_active(self) -> bool:
        """True while the link has not been ended."""
        return self.unlinked_at is None

    def __repr__(self) -> str:
        return (
            f"<PatientAccountLink id={self.id!s:.8} account={self.account_id!s:.8} "
            f"hospital={self.hospital_id!s:.8}>"
        )
