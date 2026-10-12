"""Re-encrypt stored MFA secrets under the current key.

The maintenance step of an MFA key rotation (``app/core/security.py``):

1. Generate a new Fernet key.
2. Set it as ``MFA_ENCRYPTION_KEY`` and move the old key to
   ``MFA_ENCRYPTION_PREVIOUS_KEYS``. Deploy. Both keys now decrypt; new
   secrets are written under the new one.
3. Run this module (``make rotate-mfa-keys``). Every stored secret is
   re-encrypted under the new key.
4. When it reports nothing left under a retired key, remove the old key from
   ``MFA_ENCRYPTION_PREVIOUS_KEYS`` and deploy again.

Secrets are also moved to the current key one at a time as users sign in
(``AuthService.verify_mfa``), but that never reaches a user who does not log
in, so step 3 is what makes step 4 safe.

The plaintext exists only in memory, inside :mod:`app.core.security`, for the
moment between decrypting and encrypting one row. Nothing here logs a secret.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.core.security import (
    MfaSecretDecryptionError,
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    mfa_secret_needs_reencryption,
)
from app.core.tenancy import TenantScope, tenant_scope
from app.repositories.user_repository import UserRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

__all__ = ["RotationReport", "reencrypt_mfa_secrets"]


@dataclass(frozen=True, slots=True)
class RotationReport:
    """What one rotation run did.

    :param total: Stored secrets examined.
    :param reencrypted: Secrets moved from a retired key to the current key.
    :param already_current: Secrets that were already under the current key.
    :param undecryptable: Secrets that no configured key decrypts. These users
        cannot pass MFA until an administrator resets it; a retired key must
        not be assumed safe to remove while this is non-zero and unexplained.
    """

    total: int
    reencrypted: int
    already_current: int
    undecryptable: int


async def reencrypt_mfa_secrets(session: AsyncSession) -> RotationReport:
    """Re-encrypt every stored MFA secret that is under a retired key.

    Commits once at the end, so a failure part-way leaves every row as it was.

    :param session: A session with no tenant bound; this spans hospitals.
    :returns: Counts of what was done.
    :raises MfaEncryptionNotConfiguredError: If no key is configured.
    """
    users = UserRepository(session)
    reencrypted = already_current = undecryptable = 0

    with tenant_scope(TenantScope.system("MFA key rotation: every hospital's stored secrets")):
        enrolled = await users.list_with_mfa_secret_cross_tenant()

    for user in enrolled:
        stored = user.mfa_secret or ""
        try:
            secret = decrypt_mfa_secret(stored)
        except MfaSecretDecryptionError:
            undecryptable += 1
            # The stored value is deliberately absent from this line.
            logger.error("mfa_secret_undecryptable", user_id=str(user.id))
            continue

        if not mfa_secret_needs_reencryption(stored):
            already_current += 1
            continue

        # Each row is written under its own hospital, like any scheduled job.
        with tenant_scope(TenantScope.hospital(user.hospital_id)):
            await users.update(user, mfa_secret=encrypt_mfa_secret(secret))
        reencrypted += 1

    await session.commit()
    report = RotationReport(
        total=len(enrolled),
        reencrypted=reencrypted,
        already_current=already_current,
        undecryptable=undecryptable,
    )
    logger.info(
        "mfa_key_rotation_completed",
        total=report.total,
        reencrypted=report.reencrypted,
        already_current=report.already_current,
        undecryptable=report.undecryptable,
    )
    return report


async def _main() -> RotationReport:
    from app.core.config import settings
    from app.core.logging import configure_logging
    from app.database import create_session_factory, initialize_database

    configure_logging()
    initialize_database(database_url=settings.DATABASE_URL)
    factory = create_session_factory()
    async with factory() as session:
        return await reencrypt_mfa_secrets(session)


if __name__ == "__main__":
    asyncio.run(_main())
