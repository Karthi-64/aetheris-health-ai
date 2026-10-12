"""add patient actor to audit logs

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-07 10:30:00.000000

Adds ``audit_logs.patient_account_id``.

Until now an audit row could name only a staff user (``actor_user_id``) or
nobody, and an event with no user was recorded as the system. A Patient App
action is neither: a patient is not a ``users`` row and must not be recorded
as the system, or "who did this" could not be answered for any of them
(``docs/modules/15-patient-app.md`` §20.2). Such a row now has
``actor_type = 'patient'`` — the column is free text and needs no change —
and names the patient account here.

The column is nullable and is NULL on every existing row and on every staff
and system event: nothing about them changes. Like ``actor_user_id`` it is not
a foreign key: the trail is append-only and must outlive what it describes.

``audit_logs.hospital_id`` was already nullable (platform events), which is
what a patient signing in before any hospital is involved needs.

``downgrade`` drops the index and the column. Patient events stay in the
table but no longer say which account acted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``audit_logs.patient_account_id`` and its index."""
    op.add_column(
        "audit_logs",
        sa.Column("patient_account_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_audit_patient_account", "audit_logs", ["patient_account_id"])


def downgrade() -> None:
    """Drop the index and the column."""
    op.drop_index("ix_audit_patient_account", table_name="audit_logs")
    op.drop_column("audit_logs", "patient_account_id")
