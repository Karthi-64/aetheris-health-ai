"""encrypt mfa secrets at rest

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-06 20:00:00.000000

A data migration — no schema change. ``users.mfa_secret`` stays ``TEXT``; its
contents change from the plaintext base32 TOTP secret to a Fernet token
(``app/core/security.py``, "MFA secret encryption at rest").

**This migration needs the encryption key when, and only when, there is a
plaintext secret to convert.**

- A database with no MFA secrets — every fresh database, and any existing one
  where nobody has enrolled — upgrades without a key.
- A database that holds plaintext secrets refuses to upgrade until
  ``MFA_ENCRYPTION_KEY`` is provided. It does not skip the rows, blank them, or
  write a placeholder: skipping would leave plaintext behind, and blanking
  would silently switch MFA off for those users.

Idempotent: a value that is already a Fernet token is left alone, so the
migration can be re-run and can follow a partial manual conversion.

**``downgrade`` never restores plaintext.** If any encrypted secret exists it
stops with an error and changes nothing; with none, there is nothing to undo
and it succeeds. The previous application version reads the column as
plaintext, so a database holding encrypted secrets cannot be handed back to it
by a schema downgrade. To roll back:

- roll *forward* — deploy a fixed version that still reads encrypted secrets; or
- restore a database backup taken before this migration, with the application
  version that matches it. A backup taken *after* this migration needs the
  encryption key that was in use, and the newer application.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every Fernet token starts this way (version byte + timestamp, base64). A
#: base32 TOTP secret — upper-case letters and 2-7 — never does.
_FERNET_TOKEN_PREFIX = "gAAAAA"  # noqa: S105 — a format marker, not a credential

_users = sa.table(
    "users",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("mfa_secret", sa.Text),
)


def _configured_keys() -> list[bytes]:
    """Return the current key followed by any retired keys, from configuration.

    Read through the application settings so the key comes from the same place
    the running application takes it: the environment, or ``backend/.env`` in
    local development.
    """
    from app.core.config import settings

    return [
        *settings.mfa_encryption_keys("MFA_ENCRYPTION_KEY"),
        *settings.mfa_encryption_keys("MFA_ENCRYPTION_PREVIOUS_KEYS"),
    ]


def _stored_secrets(connection: Connection) -> list[tuple[object, str]]:
    rows = connection.execute(
        sa.select(_users.c.id, _users.c.mfa_secret).where(_users.c.mfa_secret.is_not(None))
    )
    return [(row.id, row.mfa_secret) for row in rows]


def encrypt_plaintext_secrets(connection: Connection, keys: list[bytes]) -> int:
    """Encrypt every plaintext MFA secret under the current key.

    :param connection: The migration's connection.
    :param keys: Configured keys, current key first. May be empty.
    :returns: How many secrets were encrypted.
    :raises RuntimeError: If a plaintext secret exists and no key is configured.
    """
    plaintext = [
        (user_id, secret)
        for user_id, secret in _stored_secrets(connection)
        if not secret.startswith(_FERNET_TOKEN_PREFIX)
    ]
    if not plaintext:
        return 0
    if not keys:
        msg = (
            f"{len(plaintext)} MFA secret(s) are stored in plaintext and MFA_ENCRYPTION_KEY "
            "is not set. Provide the key and run the migration again; nothing was changed."
        )
        raise RuntimeError(msg)

    cipher = Fernet(keys[0])
    for user_id, secret in plaintext:
        connection.execute(
            sa.update(_users)
            .where(_users.c.id == user_id)
            .values(mfa_secret=cipher.encrypt(secret.encode("utf-8")).decode("ascii"))
        )
    return len(plaintext)


def refuse_downgrade_if_encrypted(connection: Connection) -> None:
    """Stop a downgrade that would need plaintext MFA secrets.

    Reads only; writes nothing in any case.

    :param connection: The migration's connection.
    :raises RuntimeError: If any encrypted MFA secret exists.
    """
    encrypted = sum(
        1
        for _user_id, secret in _stored_secrets(connection)
        if secret.startswith(_FERNET_TOKEN_PREFIX)
    )
    if encrypted:
        msg = (
            f"Refusing to downgrade: {encrypted} MFA secret(s) are encrypted, and this "
            "migration never writes them back as plaintext. Roll forward to a compatible "
            "application version, or restore a backup taken before 0018 together with the "
            "application version that matches it. Nothing was changed."
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    encrypt_plaintext_secrets(op.get_bind(), _configured_keys())


def downgrade() -> None:
    refuse_downgrade_if_encrypted(op.get_bind())
