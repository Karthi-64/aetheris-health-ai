"""Patient consent records and record access grants.

See ``docs/modules/15-patient-app.md`` §8. Three mechanisms answer three
different questions and none is a single global boolean:

* ownership — "is this the patient's own record?" — is the record link
  (:class:`~app.models.patient_account.PatientAccountLink`);
* purpose consent — "has the patient agreed to this specific use, under this
  policy version?" — is :class:`PatientConsentRecord`;
* access grant — "has the patient let someone else see specific categories of
  their records?" — is :class:`PatientAccessGrant`.

**Tenancy.** ``patient_consent_records.hospital_id`` is nullable: a consent to
a hospital purpose belongs to that hospital, a consent to a platform policy
(terms of service, privacy notice) belongs to none. A check constraint ties
the two together so a hospital purpose can never be stored without its
hospital. ``patient_access_grants.hospital_id`` is the *source* hospital — the
one that holds the records being shared.

Rows in both tables are never updated in place beyond their own end
timestamps (``withdrawn_at``, ``revoked_at``) and never deleted: a new policy
version is a new row, and a grant's status is derived from its timestamps.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for Mapped[uuid.UUID] resolution
from datetime import date, datetime  # noqa: TC003 — needed at runtime for Mapped[...] resolution
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import ARRAY, INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin

__all__ = [
    "ConsentPurpose",
    "GrantPurposeNote",
    "GrantStatus",
    "GranteeType",
    "PatientAccessGrant",
    "PatientConsentRecord",
    "RecordCategory",
]


class ConsentPurpose(StrEnum):
    """What a consent is for (§8.2). The first two are platform policies."""

    TERMS_OF_SERVICE = "terms_of_service"
    PRIVACY_NOTICE = "privacy_notice"
    HOSPITAL_RECORD_LINK = "hospital_record_link"
    HOSPITAL_REGISTRATION = "hospital_registration"


class GranteeType(StrEnum):
    """Who receives an access grant (§8.3)."""

    HOSPITAL = "hospital"
    DOCTOR = "doctor"


class GrantPurposeNote(StrEnum):
    """Why a grant was given — a fixed list, never free text."""

    CONSULTATION = "consultation"
    SECOND_OPINION = "second_opinion"
    CONTINUITY_OF_CARE = "continuity_of_care"


class RecordCategory(StrEnum):
    """A category of a patient's records — the ``patient_record_category`` type.

    No category is implied by another. ``DOCUMENTS`` is defined but cannot be
    selected in V1: no document source exists.
    """

    IDENTITY = "identity"
    MEDICAL_HISTORY = "medical_history"
    APPOINTMENTS = "appointments"
    PRESCRIPTIONS = "prescriptions"
    LAB_RESULTS = "lab_results"
    DOCUMENTS = "documents"


class GrantStatus(StrEnum):
    """Derived state of a grant. Never stored — see :meth:`PatientAccessGrant.status_at`."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class PatientConsentRecord(UUIDPrimaryKeyMixin, Base):
    """One consent given by an account: a purpose, under one policy version."""

    __tablename__ = "patient_consent_records"

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('terms_of_service', 'privacy_notice', "
            "'hospital_record_link', 'hospital_registration')",
            name="purpose",
        ),
        # A hospital purpose always names its hospital; a platform policy never does.
        CheckConstraint(
            "(purpose IN ('hospital_record_link', 'hospital_registration')) "
            "= (hospital_id IS NOT NULL)",
            name="hospital_scope",
        ),
        Index(
            "uq_patient_consent_records_active_hospital",
            "account_id",
            "purpose",
            "hospital_id",
            "policy_version",
            unique=True,
            postgresql_where=text("withdrawn_at IS NULL AND hospital_id IS NOT NULL"),
        ),
        Index(
            "uq_patient_consent_records_active_platform",
            "account_id",
            "purpose",
            "policy_version",
            unique=True,
            postgresql_where=text("withdrawn_at IS NULL AND hospital_id IS NULL"),
        ),
        Index("ix_patient_consent_records_account_id", "account_id"),
        Index("ix_patient_consent_records_hospital_id", "hospital_id"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patient_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Who consented.",
    )
    purpose: Mapped[str] = mapped_column(
        String(40), nullable=False, comment="What the consent is for."
    )
    hospital_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=True,
        comment="The hospital, for a hospital purpose. NULL for a platform policy.",
    )
    policy_version: Mapped[str] = mapped_column(
        String(40), nullable=False, comment="The exact version of the text that was shown."
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When it was withdrawn. NULL: in force."
    )
    ip_address: Mapped[str | None] = mapped_column(
        INET, nullable=True, comment="Evidence of the act: the address it came from."
    )
    user_agent: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Evidence of the act: the client that sent it."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PatientAccessGrant(UUIDPrimaryKeyMixin, Base):
    """A patient's permission for a named recipient to read named categories of one record.

    ``hospital_id`` is the source hospital, which holds the records;
    ``grantee_hospital_id`` is the hospital the recipient belongs to. They
    differ for cross-hospital sharing. ``grantee_id`` is a hospital id for a
    ``hospital`` grant and a doctor id for a ``doctor`` grant.
    """

    __tablename__ = "patient_access_grants"

    __table_args__ = (
        CheckConstraint("grantee_type IN ('hospital', 'doctor')", name="grantee_type"),
        CheckConstraint(
            "purpose_note IN ('consultation', 'second_opinion', 'continuity_of_care')",
            name="purpose_note",
        ),
        CheckConstraint("cardinality(categories) >= 1", name="categories_not_empty"),
        CheckConstraint("expires_at > granted_at", name="expiry"),
        CheckConstraint(
            "records_from IS NULL OR records_to IS NULL OR records_from <= records_to",
            name="record_window",
        ),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_account_id IS NULL)",
            name="revocation",
        ),
        Index("ix_patient_access_grants_hospital_patient", "hospital_id", "patient_id"),
        Index("ix_patient_access_grants_grantee", "grantee_hospital_id", "grantee_id"),
        Index("ix_patient_access_grants_grantor", "grantor_account_id"),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="The source hospital (tenant): the one that holds the records.",
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="RESTRICT"),
        nullable=False,
        comment="The patient record being shared.",
    )
    grantor_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patient_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        comment="The account that gave the grant.",
    )
    grantee_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="hospital / doctor."
    )
    grantee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="The recipient: a hospital id or a doctor id, by grantee_type.",
    )
    grantee_hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="The hospital the recipient belongs to.",
    )
    context_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="SET NULL"),
        nullable=True,
        comment="The appointment the grant was given for, if any.",
    )
    purpose_note: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="Why the grant was given, from a fixed list."
    )
    categories: Mapped[list[RecordCategory]] = mapped_column(
        ARRAY(
            SQLEnum(
                RecordCategory,
                name="patient_record_category",
                create_type=False,
                values_callable=lambda e: [m.value for m in e],
            )
        ),
        nullable=False,
        comment="The record categories shared. None implies another.",
    )
    records_from: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="Earliest record date shared. NULL: no lower bound."
    )
    records_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="Latest record date shared. NULL: no upper bound."
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="The grant is dead from this instant."
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When it was revoked. NULL: not revoked."
    )
    revoked_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patient_accounts.id", ondelete="RESTRICT"),
        nullable=True,
        comment="The account that revoked it.",
    )
    revoke_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def status_at(self, now: datetime) -> GrantStatus:
        """Derive the grant's status at an instant.

        Derived, never stored, so that it cannot disagree with the timestamps:
        revoked if ``revoked_at`` is set; otherwise expired once ``expires_at``
        has been reached; otherwise active.

        :param now: The instant to judge at (timezone-aware).
        """
        if self.revoked_at is not None:
            return GrantStatus.REVOKED
        if self.expires_at <= now:
            return GrantStatus.EXPIRED
        return GrantStatus.ACTIVE
