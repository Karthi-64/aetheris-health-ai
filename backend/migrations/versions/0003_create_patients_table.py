"""create patients table

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-27 10:00:00.000000

Creates the patient record schema (``docs/05-DATABASE_DESIGN.md`` §2.7):

- ``gender_enum`` enum type
- ``patients`` — demographics, contact, and structured medical history

Note there is no ``status`` column. Patient lifecycle state is derived from
``deleted_at`` (``docs/modules/03-patient-management.md`` §4, rule 4 — soft
delete only). See :mod:`app.models.patient` for the rationale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Enums ──────────────────────────────────────────────────────────────────
    # Created explicitly rather than inline so that the type is dropped cleanly
    # on downgrade and so a re-run after a partial failure is not blocked.
    gender_enum = postgresql.ENUM(
        "male",
        "female",
        "other",
        "unspecified",
        name="gender_enum",
        create_type=False,
    )
    gender_enum.create(op.get_bind(), checkfirst=True)

    # ── Patients ───────────────────────────────────────────────────────────────
    op.create_table(
        "patients",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "hospital_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hospitals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("mrn", sa.String(30), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("date_of_birth", sa.Date, nullable=False),
        sa.Column("gender", gender_enum, nullable=False),
        sa.Column("blood_group", sa.String(5), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("address", postgresql.JSONB, nullable=True),
        sa.Column("emergency_contact", postgresql.JSONB, nullable=True),
        sa.Column("marital_status", sa.String(20), nullable=True),
        sa.Column("occupation", sa.String(100), nullable=True),
        sa.Column("allergies", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("chronic_conditions", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("current_medications", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "deleted_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # MRN is unique per hospital (module spec §4, rule 1). Enforced by the
    # database so that two concurrent registrations cannot both win — the
    # service retries on the resulting IntegrityError (module spec §14).
    op.create_unique_constraint("uq_patients_hospital_mrn", "patients", ["hospital_id", "mrn"])

    # Search indexes (docs/05-DATABASE_DESIGN.md §2.7).
    op.create_index("ix_patients_phone", "patients", ["phone"])
    op.create_index("ix_patients_name", "patients", ["hospital_id", "last_name", "first_name"])
    op.create_index("ix_patients_dob", "patients", ["hospital_id", "date_of_birth"])

    # Partial index for the common case: listing live patients in one hospital.
    # docs/05-DATABASE_DESIGN.md §1.7 calls for soft-delete-aware partial indexes.
    op.create_index(
        "ix_patients_hospital_active",
        "patients",
        ["hospital_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # Case-insensitive prefix search on name (module spec §4, rule 6). Without
    # these, `lower(last_name) LIKE 'rao%'` cannot use ix_patients_name.
    op.create_index(
        "ix_patients_last_name_lower",
        "patients",
        ["hospital_id", sa.text("lower(last_name) varchar_pattern_ops")],
    )
    op.create_index(
        "ix_patients_first_name_lower",
        "patients",
        ["hospital_id", sa.text("lower(first_name) varchar_pattern_ops")],
    )


def downgrade() -> None:
    """Rollback — drop the patients table and the gender_enum type."""
    op.drop_index("ix_patients_first_name_lower", table_name="patients")
    op.drop_index("ix_patients_last_name_lower", table_name="patients")
    op.drop_index("ix_patients_hospital_active", table_name="patients")
    op.drop_index("ix_patients_dob", table_name="patients")
    op.drop_index("ix_patients_name", table_name="patients")
    op.drop_index("ix_patients_phone", table_name="patients")
    op.drop_table("patients")

    op.execute("DROP TYPE IF EXISTS gender_enum")
