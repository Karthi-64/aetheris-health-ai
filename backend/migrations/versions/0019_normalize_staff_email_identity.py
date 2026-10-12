"""normalize staff email identity

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-06 22:00:00.000000

Makes a staff email name exactly one live account on the platform.

Login takes an email and no hospital, so the lookup is platform-wide — but the
only uniqueness rule was per hospital and case-sensitive
(``uq_users_hospital_email``). The same address could therefore exist at two
hospitals, or twice in one hospital in different case, and login could not
tell which account was meant.

This migration:

1. checks that the change can be made, and stops if it cannot;
2. lower-cases the email of every live (non-deleted) user;
3. adds ``uq_users_email_normalized_active`` — a unique index on
   ``lower(email)`` over live users.

**It refuses to run, changing nothing, if existing data conflicts.** Two kinds
of conflict are detected up front:

- two or more live users whose emails are equal once lower-cased;
- a live user whose lower-cased email is already held by another row of the
  same hospital — in practice a soft-deleted one — which the older
  per-hospital constraint would reject.

It does not pick a winner, merge, rename or delete anyone. Resolving a
conflict is a decision about real people's accounts and belongs to an
operator. The error lists the affected user ids — never the addresses.

The check, the update and the index run in one transaction, so a failure at
any point leaves the database exactly as it was.

``uq_users_hospital_email`` is kept. It is no longer the identity rule, but it
still covers soft-deleted rows, which the new partial index deliberately
leaves out. Soft-deleted users are not touched: their stored address keeps its
original case and does not occupy the platform-wide index.

``downgrade`` drops the index. It does not restore the original letter case —
that information is not kept, and nothing depends on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_users_email_normalized_active"

#: Live users whose emails collide once lower-cased. One row per colliding
#: address, with the ids of every account involved.
_DUPLICATE_LIVE_EMAILS = sa.text(
    """
    SELECT array_agg(id::text ORDER BY created_at, id) AS user_ids
    FROM users
    WHERE deleted_at IS NULL
    GROUP BY lower(email)
    HAVING count(*) > 1
    ORDER BY min(created_at)
    """
)

#: Live users that cannot be lower-cased because another row of the same
#: hospital already holds the lower-cased address (the per-hospital constraint
#: is not partial, so a soft-deleted row counts).
_BLOCKED_BY_HOSPITAL_CONSTRAINT = sa.text(
    """
    SELECT live.id::text AS user_id, other.id::text AS blocking_user_id
    FROM users AS live
    JOIN users AS other
      ON other.hospital_id = live.hospital_id
     AND other.id <> live.id
     AND other.email = lower(live.email)
    WHERE live.deleted_at IS NULL
      AND live.email <> lower(live.email)
      AND other.deleted_at IS NOT NULL
    ORDER BY live.created_at, live.id
    """
)


def find_conflicts(connection: Connection) -> list[str]:
    """Describe every existing row that prevents this migration.

    Reads only.

    :param connection: The migration's connection.
    :returns: One human-readable line per conflict; empty if there are none.
        Lines contain user ids, never email addresses.
    """
    conflicts = [
        "live accounts share one email (ignoring case): " + ", ".join(row.user_ids)
        for row in connection.execute(_DUPLICATE_LIVE_EMAILS)
    ]
    conflicts.extend(
        f"account {row.user_id} cannot be lower-cased: deleted account "
        f"{row.blocking_user_id} in the same hospital already holds that address"
        for row in connection.execute(_BLOCKED_BY_HOSPITAL_CONSTRAINT)
    )
    return conflicts


def refuse_if_conflicts(connection: Connection) -> None:
    """Stop the migration if existing data conflicts with the new rule.

    :param connection: The migration's connection.
    :raises RuntimeError: Listing the affected user ids. Nothing was changed.
    """
    conflicts = find_conflicts(connection)
    if conflicts:
        details = "\n  - ".join(conflicts)
        msg = (
            f"Cannot make staff email unique across the platform: {len(conflicts)} "
            "conflict(s) in existing data. Resolve each one (change an address, or "
            "remove an account) and run the migration again. Nothing was changed.\n  - " + details
        )
        raise RuntimeError(msg)


def normalize_live_emails(connection: Connection) -> int:
    """Lower-case the email of every live user that is not already lower-case.

    :param connection: The migration's connection.
    :returns: How many rows were changed.
    """
    result = connection.execute(
        sa.text(
            "UPDATE users SET email = lower(email) "
            "WHERE deleted_at IS NULL AND email <> lower(email)"
        )
    )
    return int(result.rowcount or 0)


def upgrade() -> None:
    connection = op.get_bind()
    refuse_if_conflicts(connection)
    normalize_live_emails(connection)
    op.create_index(
        INDEX_NAME,
        "users",
        [sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="users")
