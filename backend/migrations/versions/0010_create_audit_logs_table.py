"""create audit_logs table

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30 10:00:00.000000

Creates the immutable compliance trail described in
``docs/05-DATABASE_DESIGN.md`` §2.21 and ``docs/modules/12-audit-logs.md``:

- ``audit_logs`` — append-only record of every auditable action
- indexes for the tenant timeline, actor lookups and target lookups
- INSERT/SELECT-only privileges for the application role (§8 of the module
  spec): the app writes this table but must never update or delete from it.

Two columns are additive to §2.21's table listing:

- ``context`` JSONB — ``AuditEvent`` carries a non-PII context dict (failure
  reasons, result counts) that the spec's ``before``/``after`` pair has no home
  for. Dropping it would lose the "why" of entries such as
  ``auth.login.failed``.
- ``ix_audit_action`` — the audit search page filters on ``action`` (§12), and
  §2.21's three indexes do not cover it.

``actor_user_id`` uses ``ON DELETE SET NULL``: if a user row is ever hard
deleted the trail must survive with a null actor rather than lose the event
entirely — a compliance record is worse to lose than an attribution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
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
            nullable=True,
            comment="Tenant; NULL for platform events.",
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment="Acting user; NULL for system actions.",
        ),
        sa.Column(
            "actor_type",
            sa.String(20),
            nullable=False,
            server_default="user",
            comment="user / system / ai.",
        ),
        sa.Column(
            "action",
            sa.String(100),
            nullable=False,
            comment="Dotted action name, e.g. patient.created.",
        ),
        sa.Column("target_type", sa.String(50), nullable=True, comment="Entity type acted on."),
        sa.Column(
            "target_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="UUID of the entity acted on.",
        ),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Non-PII context (reason, counts) carried by the AuditEvent.",
        ),
        sa.Column("ip_address", postgresql.INET, nullable=True, comment="Client address."),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Correlates with request logs.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="When the action occurred.",
        ),
    )

    # §8: tenant timeline (newest first), actor, target. Action backs the
    # audit search page's primary filter (§12).
    op.create_index(
        "ix_audit_hospital_created",
        "audit_logs",
        ["hospital_id", "created_at"],
    )
    op.create_index("ix_audit_actor", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_target", "audit_logs", ["target_type", "target_id"])
    op.create_index("ix_audit_action", "audit_logs", ["action"])

    # §8: the application role may append and read the trail, never rewrite it.
    # Guarded on role existence so a database without the deployed `aetheris_app`
    # role (local dev, CI) still migrates cleanly.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aetheris_app') THEN
                REVOKE UPDATE, DELETE ON audit_logs FROM aetheris_app;
                GRANT INSERT, SELECT ON audit_logs TO aetheris_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_audit_action", table_name="audit_logs")
    op.drop_index("ix_audit_target", table_name="audit_logs")
    op.drop_index("ix_audit_actor", table_name="audit_logs")
    op.drop_index("ix_audit_hospital_created", table_name="audit_logs")
    op.drop_table("audit_logs")
