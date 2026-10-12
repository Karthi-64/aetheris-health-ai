"""create patient consent and access grant tables

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-07 10:20:00.000000

Adds ``patient_consent_records`` and ``patient_access_grants``, and the
``patient_record_category`` enum type the grants use
(``docs/modules/15-patient-app.md`` §8).

They answer two questions the record link (migration 0023) does not:

* **Purpose consent** — has the patient agreed to this specific use, under
  this version of the policy text? One row per account, purpose, hospital (or
  none) and policy version. A new version is a new row; a row is never
  overwritten, only ended with ``withdrawn_at``.
* **Access grant** — has the patient let a named recipient read named
  categories of one record? A grant always has an expiry, and its status
  (active / expired / revoked) is *derived* from its timestamps and never
  stored, so the two cannot disagree. Grants are never deleted.

**Tenancy.**

* ``patient_consent_records.hospital_id`` is nullable. A consent to a hospital
  purpose (linking a record, registering one) belongs to that hospital; a
  consent to a platform policy belongs to none. ``hospital_scope`` ties the
  column to the purpose so a hospital purpose can never be stored without its
  hospital, and uniqueness is split into two partial indexes because a
  NULL never equals a NULL.
* ``patient_access_grants.hospital_id`` is the *source* hospital — the one
  that holds the records. ``grantee_hospital_id`` is the recipient's.

Categories are an array of an enum, not free text and not implied by one
another. ``documents`` is part of the type but cannot be granted yet: the
service refuses it, because no document source exists.

``downgrade`` drops both tables and the enum type. Consent evidence and
grants are lost; nothing else is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECORD_CATEGORIES = (
    "identity",
    "medical_history",
    "appointments",
    "prescriptions",
    "lab_results",
    "documents",
)


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    """Create the consent and grant tables."""
    op.create_table(
        "patient_consent_records",
        _uuid_pk(),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=40), nullable=False),
        sa.Column("hospital_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("policy_version", sa.String(length=40), nullable=False),
        _timestamp("granted_at"),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_patient_consent_records"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["patient_accounts.id"],
            name="fk_patient_consent_records_account_id_patient_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["hospital_id"],
            ["hospitals.id"],
            name="fk_patient_consent_records_hospital_id_hospitals",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "purpose IN ('terms_of_service', 'privacy_notice', "
            "'hospital_record_link', 'hospital_registration')",
            name=op.f("ck_patient_consent_records_purpose"),
        ),
        sa.CheckConstraint(
            "(purpose IN ('hospital_record_link', 'hospital_registration')) "
            "= (hospital_id IS NOT NULL)",
            name=op.f("ck_patient_consent_records_hospital_scope"),
        ),
    )
    op.create_index(
        "uq_patient_consent_records_active_hospital",
        "patient_consent_records",
        ["account_id", "purpose", "hospital_id", "policy_version"],
        unique=True,
        postgresql_where=sa.text("withdrawn_at IS NULL AND hospital_id IS NOT NULL"),
    )
    op.create_index(
        "uq_patient_consent_records_active_platform",
        "patient_consent_records",
        ["account_id", "purpose", "policy_version"],
        unique=True,
        postgresql_where=sa.text("withdrawn_at IS NULL AND hospital_id IS NULL"),
    )
    op.create_index(
        "ix_patient_consent_records_account_id", "patient_consent_records", ["account_id"]
    )
    op.create_index(
        "ix_patient_consent_records_hospital_id", "patient_consent_records", ["hospital_id"]
    )

    category = postgresql.ENUM(*_RECORD_CATEGORIES, name="patient_record_category")
    category.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "patient_access_grants",
        _uuid_pk(),
        sa.Column("hospital_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grantor_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grantee_type", sa.String(length=16), nullable=False),
        sa.Column("grantee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grantee_hospital_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_appointment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("purpose_note", sa.String(length=32), nullable=False),
        sa.Column(
            "categories",
            postgresql.ARRAY(
                postgresql.ENUM(
                    *_RECORD_CATEGORIES, name="patient_record_category", create_type=False
                )
            ),
            nullable=False,
        ),
        sa.Column("records_from", sa.Date(), nullable=True),
        sa.Column("records_to", sa.Date(), nullable=True),
        _timestamp("granted_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=200), nullable=True),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_patient_access_grants"),
        sa.ForeignKeyConstraint(
            ["hospital_id"],
            ["hospitals.id"],
            name="fk_patient_access_grants_hospital_id_hospitals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patients.id"],
            name="fk_patient_access_grants_patient_id_patients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["grantor_account_id"],
            ["patient_accounts.id"],
            name="fk_patient_access_grants_grantor_account_id_patient_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["grantee_hospital_id"],
            ["hospitals.id"],
            name="fk_patient_access_grants_grantee_hospital_id_hospitals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["context_appointment_id"],
            ["appointments.id"],
            name="fk_patient_access_grants_context_appointment_id_appointments",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_account_id"],
            ["patient_accounts.id"],
            name="fk_patient_access_grants_revoked_by_account_id_patient_accounts",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "grantee_type IN ('hospital', 'doctor')",
            name=op.f("ck_patient_access_grants_grantee_type"),
        ),
        sa.CheckConstraint(
            "purpose_note IN ('consultation', 'second_opinion', 'continuity_of_care')",
            name=op.f("ck_patient_access_grants_purpose_note"),
        ),
        sa.CheckConstraint(
            "cardinality(categories) >= 1",
            name=op.f("ck_patient_access_grants_categories_not_empty"),
        ),
        sa.CheckConstraint("expires_at > granted_at", name=op.f("ck_patient_access_grants_expiry")),
        sa.CheckConstraint(
            "records_from IS NULL OR records_to IS NULL OR records_from <= records_to",
            name=op.f("ck_patient_access_grants_record_window"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_account_id IS NULL)",
            name=op.f("ck_patient_access_grants_revocation"),
        ),
    )
    op.create_index(
        "ix_patient_access_grants_hospital_patient",
        "patient_access_grants",
        ["hospital_id", "patient_id"],
    )
    op.create_index(
        "ix_patient_access_grants_grantee",
        "patient_access_grants",
        ["grantee_hospital_id", "grantee_id"],
    )
    op.create_index(
        "ix_patient_access_grants_grantor", "patient_access_grants", ["grantor_account_id"]
    )


def downgrade() -> None:
    """Drop the consent and grant tables and the category type."""
    op.drop_index("ix_patient_access_grants_grantor", table_name="patient_access_grants")
    op.drop_index("ix_patient_access_grants_grantee", table_name="patient_access_grants")
    op.drop_index("ix_patient_access_grants_hospital_patient", table_name="patient_access_grants")
    op.drop_table("patient_access_grants")
    postgresql.ENUM(name="patient_record_category").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_patient_consent_records_hospital_id", table_name="patient_consent_records")
    op.drop_index("ix_patient_consent_records_account_id", table_name="patient_consent_records")
    op.drop_index(
        "uq_patient_consent_records_active_platform", table_name="patient_consent_records"
    )
    op.drop_index(
        "uq_patient_consent_records_active_hospital", table_name="patient_consent_records"
    )
    op.drop_table("patient_consent_records")
