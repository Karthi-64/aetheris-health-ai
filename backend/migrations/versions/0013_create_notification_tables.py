"""create notification tables

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-04 10:00:00.000000

Creates the notification schema (``docs/05-DATABASE_DESIGN.md`` §2.20,
``docs/modules/11-notifications.md`` §8):

- ``notification_channel`` / ``notification_delivery_status`` enum types
- ``notifications`` — one message for one recipient; this row *is* the in-app
  notification
- ``notification_preferences`` — one row per user
- ``notification_deliveries`` — one row per out-of-band send (email today)

``notification_templates`` from the spec is not created: template management
is v2.1, and the MVP's templates live in code.

Four points where this departs from the docs.

**``notifications.in_app``.** A user may switch the notification centre off for
a kind while keeping its email (FR-3, AC-3). The email still needs a parent
notification row, so the row records whether it belongs in the centre rather
than that being implied by the row existing.

**``notifications`` and ``notification_deliveries`` carry ``hospital_id``.**
Neither does in the design, but CLAUDE.md rule 4 requires it on every table
holding tenant data.

**``notification_deliveries`` has ``next_attempt_at``.** Business rule 3 asks
for exponential backoff over up to five attempts, and "when is this due" has
to be stored somewhere for a worker to query. It is also what lets the queue
drain after an outage (§14) without any in-memory state.

**``notification_deliveries`` has ``to_address``, ``subject`` and ``body``.**
An email is sent later, by a worker, so what to send has to be stored until
then. For invitations and password resets that body contains a single-use
token, which the rest of the system deliberately stores only as a hash. The
body is therefore **cleared as soon as the delivery is sent or finally
fails**, so the token sits in the database only while the email is in flight.
The in-app notification row never contains it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _audit_columns() -> list[sa.Column[object]]:
    """Return the audit and soft-delete columns every business table carries."""
    return [
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
    ]


def upgrade() -> None:
    channel = postgresql.ENUM(
        "in_app", "email", "sms", name="notification_channel", create_type=False
    )
    channel.create(op.get_bind(), checkfirst=True)

    delivery_status = postgresql.ENUM(
        "queued",
        "sent",
        "failed",
        "delivered",
        name="notification_delivery_status",
        create_type=False,
    )
    delivery_status.create(op.get_bind(), checkfirst=True)

    # ── notifications ──────────────────────────────────────────────────────────
    op.create_table(
        "notifications",
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
        sa.Column(
            "recipient_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("link", sa.Text, nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("in_app", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("sent_email", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("sent_sms", sa.Boolean, nullable=False, server_default=sa.text("false")),
        *_audit_columns(),
    )
    # The bell: "my notifications, newest first".
    op.create_index(
        "ix_notifications_recipient_created",
        "notifications",
        ["recipient_user_id", sa.text("created_at DESC")],
    )
    # The badge: "how many unread". Partial, so it stays small as history grows.
    op.create_index(
        "ix_notifications_recipient_unread",
        "notifications",
        ["recipient_user_id"],
        postgresql_where=sa.text("read_at IS NULL AND in_app AND deleted_at IS NULL"),
    )

    # ── notification_preferences ───────────────────────────────────────────────
    op.create_table(
        "notification_preferences",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "preferences",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ── notification_deliveries ────────────────────────────────────────────────
    op.create_table(
        "notification_deliveries",
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
        sa.Column(
            "notification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notifications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", channel, nullable=False),
        sa.Column("status", delivery_status, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("to_address", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(200), nullable=True),
        # NULL once sent or finally failed — see the module docstring.
        sa.Column("body", sa.Text, nullable=True),
        *_audit_columns(),
    )
    op.create_check_constraint(
        "attempts_non_negative", "notification_deliveries", sa.text("attempts >= 0")
    )
    # The worker's query: "what is queued and due". Partial for the same reason
    # as the unread index — almost every row is in a finished state.
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_notification_deliveries_notification",
        "notification_deliveries",
        ["notification_id"],
    )


def downgrade() -> None:
    """Rollback — drop the notification tables and their enum types."""
    op.drop_index("ix_notification_deliveries_notification", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_due", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")

    op.drop_table("notification_preferences")

    op.drop_index("ix_notifications_recipient_unread", table_name="notifications")
    op.drop_index("ix_notifications_recipient_created", table_name="notifications")
    op.drop_table("notifications")

    op.execute("DROP TYPE IF EXISTS notification_delivery_status")
    op.execute("DROP TYPE IF EXISTS notification_channel")
