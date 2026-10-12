"""create patient account links

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-07 10:10:00.000000

Adds ``patient_account_links``: the verified statement that a patient account
(migration 0022) may act as one patient record
(``docs/modules/15-patient-app.md`` §4).

The link is a table of its own, and not a column on either side, because one
person can be a patient at several hospitals (one record each) and — later —
one account will act for dependents. It carries ``relationship`` from day
one; V1 accepts only ``self``, and a check constraint holds it to that.

**Tenant data.** A link names a row in ``patients``, so it carries the
``hospital_id`` of the hospital that owns that record and is confined like
every other business table.

Two rules must hold under concurrent requests, so they are indexes and not
application checks:

* ``uq_patient_account_links_active_patient`` — at most one *active* link per
  patient record: a record cannot be claimed by two accounts at once;
* ``uq_patient_account_links_active_self`` — at most one active ``self`` link
  per account and hospital.

Both are partial (``unlinked_at IS NULL``): an ended link stays as history and
does not block a new one.

``downgrade`` drops the table. Every link is lost; accounts and patient
records are untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``patient_account_links``."""
    op.create_table(
        "patient_account_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hospital_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relationship", sa.String(length=16), server_default="self", nullable=False),
        sa.Column("verified_via", sa.String(length=32), nullable=False),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("unlinked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unlink_reason", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_patient_account_links"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["patient_accounts.id"],
            name="fk_patient_account_links_account_id_patient_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patients.id"],
            name="fk_patient_account_links_patient_id_patients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["hospital_id"],
            ["hospitals.id"],
            name="fk_patient_account_links_hospital_id_hospitals",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "relationship IN ('self')", name=op.f("ck_patient_account_links_relationship")
        ),
        sa.CheckConstraint(
            "verified_via IN ('phone_dob', 'phone_dob_mrn', 'self_registration')",
            name=op.f("ck_patient_account_links_verified_via"),
        ),
    )
    op.create_index(
        "uq_patient_account_links_active_patient",
        "patient_account_links",
        ["patient_id"],
        unique=True,
        postgresql_where=sa.text("unlinked_at IS NULL"),
    )
    op.create_index(
        "uq_patient_account_links_active_self",
        "patient_account_links",
        ["account_id", "hospital_id"],
        unique=True,
        postgresql_where=sa.text("unlinked_at IS NULL AND relationship = 'self'"),
    )
    op.create_index("ix_patient_account_links_account_id", "patient_account_links", ["account_id"])
    op.create_index(
        "ix_patient_account_links_hospital_patient",
        "patient_account_links",
        ["hospital_id", "patient_id"],
    )


def downgrade() -> None:
    """Drop ``patient_account_links``."""
    op.drop_index("ix_patient_account_links_hospital_patient", table_name="patient_account_links")
    op.drop_index("ix_patient_account_links_account_id", table_name="patient_account_links")
    op.drop_index("uq_patient_account_links_active_self", table_name="patient_account_links")
    op.drop_index("uq_patient_account_links_active_patient", table_name="patient_account_links")
    op.drop_table("patient_account_links")
