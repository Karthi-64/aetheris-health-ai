"""Security utilities — JWT token creation/verification, password hashing, and token generation.

This module is the single place where:
- JWTs are signed and verified
- Passwords are hashed and verified (Argon2id)
- Opaque tokens are generated
- MFA/TOTP codes are verified
- MFA/TOTP secrets are encrypted and decrypted for storage

**Never** reimplement any of these operations outside this module.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt as pyjwt
import pyotp
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from passlib.context import CryptContext
from passlib.exc import PasswordSizeError, UnknownHashError

from app.core.config import settings

if TYPE_CHECKING:
    import uuid

# ── Password Hashing (Argon2id) ─────────────────────────────────────────────
# CryptContext manages scheme deprecation and migration transparently.
# We use Argon2id as the primary scheme with bcrypt as a fallback for
# legacy hashes during algorithm migration.
_pwd_context = CryptContext(
    schemes=["argon2", "bcrypt"],
    default="argon2",
    argon2__time_cost=2,  # iterations
    argon2__memory_cost=19456,  # 19 MB (KB)
    argon2__parallelism=1,
    argon2__type="ID",  # Argon2id — hybrid resistant to both side-channel and GPU attacks
    bcrypt__rounds=12,
    deprecated=["auto"],  # auto-deprecate non-argon2 hashes; rehash on verify
)


def hash_password(password: str) -> str:
    """Hash a password using Argon2id.

    :param password: The plaintext password.
    :returns: The password hash string (includes algorithm, salt, and parameters).
    """
    return str(_pwd_context.hash(password))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a stored hash.

    Automatically detects the algorithm used in ``hashed_password``.
    If the hash uses a deprecated scheme, it will be rehashed with Argon2id
    on next login (caller should check :func:`password_needs_rehash`).

    Never raises on bad input. A password the hasher refuses to process
    (oversized) or a stored value it cannot parse is simply not a match: an
    exception here would turn into a 500 for real accounts only, which tells
    a caller that the account exists.

    :param plain_password: The plaintext password to verify.
    :param hashed_password: The stored password hash.
    :returns: ``True`` if the password matches; ``False`` otherwise, including
        when the input cannot be verified at all.
    """
    try:
        return bool(_pwd_context.verify(plain_password, hashed_password))
    except (PasswordSizeError, UnknownHashError, ValueError, TypeError):
        return False


#: A real Argon2id hash of a random value nobody knows, made once per process.
#: It is what a rejected login is checked against when there is no account
#: whose hash can be checked — see :func:`burn_password_verification`.
_DUMMY_PASSWORD_HASH: str = str(_pwd_context.hash(secrets.token_urlsafe(32)))


def burn_password_verification(password: str) -> None:
    """Spend the time a real password check takes, and discard the result.

    Login refuses some attempts before it reaches the password — the email is
    unknown, or the account is suspended, locked, or in an inactive hospital.
    Answering those at once while a wrong password takes a full Argon2id
    verification lets a caller tell the cases apart with a stopwatch, even
    though the response body is identical. Calling this on every early
    rejection makes each of them do the same work as a wrong password.

    Nothing can pass: the hash is of a random value that is never stored.

    :param password: The password that was submitted. Hashed, never logged.
    """
    verify_password(password, _DUMMY_PASSWORD_HASH)


def password_needs_rehash(hashed_password: str) -> bool:
    """Check if a password hash needs to be rehashed with the current scheme.

    :param hashed_password: The stored password hash.
    :returns: ``True`` if the hash was created with a deprecated scheme.
    """
    return bool(_pwd_context.needs_update(hashed_password))


# ── JWT Token Management ────────────────────────────────────────────────────


def _get_jwt_algorithm() -> str:
    """Return the JWT signing algorithm based on configured keys.

    :returns: ``"RS256"`` if RSA keys are configured, otherwise ``"HS256"``.
    """
    if settings.JWT_PRIVATE_KEY and settings.JWT_PUBLIC_KEY:
        return "RS256"
    return "HS256"


def _get_jwt_signing_key() -> str:
    """Return the key used for signing JWTs.

    :returns: The RSA private key if configured, otherwise the app secret key.
    """
    if settings.JWT_PRIVATE_KEY:
        return settings.JWT_PRIVATE_KEY
    return settings.APP_SECRET_KEY.get_secret_value()


def _get_jwt_verification_key() -> str:
    """Return the key used for verifying JWTs.

    :returns: The RSA public key if configured, otherwise the app secret key.
    """
    if settings.JWT_PUBLIC_KEY:
        return settings.JWT_PUBLIC_KEY
    return settings.APP_SECRET_KEY.get_secret_value()


def create_access_token(
    user_id: uuid.UUID,
    hospital_id: uuid.UUID | None,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
    *,
    force_password_change: bool = False,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a short-lived JWT access token.

    :param user_id: The user's UUID.
    :param hospital_id: The user's hospital UUID (``None`` for Super Admin).
    :param roles: List of role names assigned to the user.
    :param permissions: List of permission codes the user has.
    :param force_password_change: If ``True``, includes a claim that forces a password change.
    :param extra_claims: Additional claims to include in the token payload.
    :returns: A signed JWT string.
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iss": settings.JWT_ISSUER,
        "iat": now,
        "exp": now + timedelta(seconds=settings.JWT_ACCESS_TTL_SECONDS),
        "type": "access",
        "hospital_id": str(hospital_id) if hospital_id else None,
    }

    if roles:
        payload["roles"] = roles
    if permissions:
        payload["permissions"] = permissions
    if force_password_change:
        payload["force_password_change"] = True
    if extra_claims:
        payload.update(extra_claims)

    return pyjwt.encode(
        payload,
        _get_jwt_signing_key(),
        algorithm=_get_jwt_algorithm(),
    )


def verify_access_token(token: str) -> dict[str, Any]:
    """Verify and decode a JWT access token.

    :param token: The JWT string to verify.
    :returns: The decoded payload.
    :raises jwt.ExpiredSignatureError: If the token has expired.
    :raises jwt.InvalidTokenError: If the token is invalid.
    """
    return pyjwt.decode(
        token,
        _get_jwt_verification_key(),
        algorithms=[_get_jwt_algorithm()],
        issuer=settings.JWT_ISSUER,
        leeway=settings.JWT_LEEWAY_SECONDS,
        options={
            "require": ["sub", "iss", "iat", "exp", "type"],
        },
    )


def create_mfa_ticket(user_id: uuid.UUID) -> str:
    """Create a short-lived MFA verification ticket (JWT).

    Issued during login when the user has MFA enabled. The client
    presents this ticket plus the TOTP code to complete authentication.

    :param user_id: The user's UUID.
    :returns: A signed JWT string (5-minute TTL).
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iss": settings.JWT_ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "type": "mfa_ticket",
        "purpose": "mfa_verification",
    }

    return pyjwt.encode(
        payload,
        _get_jwt_signing_key(),
        algorithm=_get_jwt_algorithm(),
    )


# ── Patient access tokens ───────────────────────────────────────────────────
# A patient is a different kind of principal from a staff user
# (``docs/modules/15-patient-app.md`` §5.6), and the two kinds of token must
# never be accepted in each other's place. A patient token therefore carries
# an audience and its own type, and carries no hospital, role or permission:
#
# * :func:`verify_access_token` supplies no audience, and PyJWT refuses a
#   token that has one — so no staff verifier accepts a patient token;
# * :func:`verify_patient_access_token` requires the audience — so it refuses
#   every staff token and MFA ticket, which have none.

#: The ``aud`` claim of every patient access token.
PATIENT_TOKEN_AUDIENCE = "atheris-patient"  # noqa: S105 — a claim value, not a credential
#: The ``type`` claim of every patient access token.
PATIENT_TOKEN_TYPE = "patient_access"  # noqa: S105 — a claim value, not a credential
#: Lifetime of a patient access token. A constant, not a setting: configuration
#: cannot lengthen it.
PATIENT_ACCESS_TTL_SECONDS = 15 * 60


def create_patient_access_token(account_id: uuid.UUID) -> str:
    """Create a short-lived JWT access token for a patient account.

    The token names the account and nothing else: no hospital, no role and no
    permission. Which hospital a request concerns is resolved on the server,
    per request, from the account's record links.

    :param account_id: The patient account's UUID (``patient_accounts.id``).
    :returns: A signed JWT string (15-minute TTL).
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(account_id),
        "iss": settings.JWT_ISSUER,
        "aud": PATIENT_TOKEN_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(seconds=PATIENT_ACCESS_TTL_SECONDS),
        "type": PATIENT_TOKEN_TYPE,
    }

    return pyjwt.encode(
        payload,
        _get_jwt_signing_key(),
        algorithm=_get_jwt_algorithm(),
    )


def verify_patient_access_token(token: str) -> dict[str, Any]:
    """Verify and decode a patient access token.

    Accepts only a token that carries the patient audience **and** the patient
    type. A staff access token or an MFA ticket has no audience and is refused
    here; a token with the audience but another type is refused too.

    :param token: The JWT string to verify.
    :returns: The decoded payload.
    :raises jwt.ExpiredSignatureError: If the token has expired.
    :raises jwt.InvalidTokenError: If the token is invalid, is not addressed
        to the patient audience, or is not a patient access token.
    """
    payload: dict[str, Any] = pyjwt.decode(
        token,
        _get_jwt_verification_key(),
        algorithms=[_get_jwt_algorithm()],
        issuer=settings.JWT_ISSUER,
        audience=PATIENT_TOKEN_AUDIENCE,
        leeway=settings.JWT_LEEWAY_SECONDS,
        options={
            "require": ["sub", "iss", "aud", "iat", "exp", "type"],
        },
    )
    if payload.get("type") != PATIENT_TOKEN_TYPE:
        msg = "Not a patient access token."
        raise pyjwt.InvalidTokenError(msg)
    return payload


# ── Opaque Token Generation ─────────────────────────────────────────────────


def generate_opaque_token() -> tuple[str, str]:
    """Generate an opaque token and its SHA-256 hash.

    The raw token is returned to the client (once). The hash is stored
    server-side for O(1) lookup and verification.

    :returns: A tuple of ``(raw_token, token_hash)``.
    """
    raw_token = secrets.token_urlsafe(48)  # 48 bytes → 64 chars base64url
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash


def hash_token(token: str) -> str:
    """Return the SHA-256 hash of a token string.

    :param token: The raw token to hash.
    :returns: The hex-encoded SHA-256 digest.
    """
    return hashlib.sha256(token.encode()).hexdigest()


# ── MFA / TOTP ──────────────────────────────────────────────────────────────


def generate_totp_secret() -> str:
    """Generate a new TOTP secret key.

    :returns: A base32-encoded secret string.
    """
    return pyotp.random_base32()


def get_totp_provisioning_uri(secret: str, email: str, issuer: str | None = None) -> str:
    """Generate the ``otpauth://`` URI for QR code provisioning.

    :param secret: The TOTP secret.
    :param email: The user's email (used as the label).
    :param issuer: The issuer name (defaults to app name).
    :returns: The provisioning URI string.
    """
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(
        name=email,
        issuer_name=issuer or settings.APP_NAME,
    )


def verify_totp_code(secret: str, code: str) -> bool:
    """Verify a TOTP code against the stored secret.

    :param secret: The user's TOTP secret.
    :param code: The 6-digit code to verify.
    :returns: ``True`` if the code is valid.
    """
    totp = pyotp.TOTP(secret)
    return totp.verify(code)


# ── MFA secret encryption at rest ───────────────────────────────────────────
# A TOTP secret is a long-lived shared key: whoever reads it can produce valid
# codes for that account for as long as MFA stays enrolled. It is therefore
# stored encrypted, and decrypted only in memory, only here, only to check a
# code.
#
# Fernet (``cryptography``) is used as-is: AES-128-CBC with an HMAC-SHA256 tag
# over a versioned, timestamped token. It is authenticated — a token that was
# altered, truncated or produced under another key fails to decrypt rather
# than yielding garbage — and it has a standard, documented format, so nothing
# about the stored value is specific to this codebase.


class MfaEncryptionNotConfiguredError(RuntimeError):
    """No ``MFA_ENCRYPTION_KEY`` is configured, so a secret cannot be stored or read."""


class MfaSecretDecryptionError(RuntimeError):
    """A stored MFA secret could not be decrypted with any configured key.

    Raised for a value encrypted under an unknown key, a corrupted or tampered
    value, and a value that is not ciphertext at all. The message never
    contains the stored value.
    """


#: Every Fernet token starts with these characters: the version byte ``0x80``
#: followed by the high bytes of a 64-bit timestamp, base64-encoded. A base32
#: TOTP secret is upper-case letters and the digits 2-7, so it can never start
#: this way — which is what lets the data migration tell the two apart.
_FERNET_TOKEN_PREFIX = "gAAAAA"  # noqa: S105 — a format marker, not a credential


def is_encrypted_mfa_secret(stored: str) -> bool:
    """Whether a stored value has the shape of an encrypted MFA secret.

    A shape check only — it does not prove the value decrypts.

    :param stored: The value of ``users.mfa_secret``.
    """
    return stored.startswith(_FERNET_TOKEN_PREFIX)


def _primary_mfa_cipher() -> Fernet:
    """Return the cipher for the current key.

    :raises MfaEncryptionNotConfiguredError: If no key is configured.
    """
    keys = settings.mfa_encryption_keys("MFA_ENCRYPTION_KEY")
    if not keys:
        msg = "MFA_ENCRYPTION_KEY is not configured."
        raise MfaEncryptionNotConfiguredError(msg)
    return Fernet(keys[0])


def _all_mfa_ciphers() -> MultiFernet:
    """Return a cipher that decrypts under the current key or any retired one."""
    retired = [Fernet(key) for key in settings.mfa_encryption_keys("MFA_ENCRYPTION_PREVIOUS_KEYS")]
    return MultiFernet([_primary_mfa_cipher(), *retired])


def encrypt_mfa_secret(secret: str) -> str:
    """Encrypt a TOTP secret for storage, under the current key.

    Each call produces a different token for the same secret (a fresh random
    IV), so equal secrets are not recognisable in the database.

    :param secret: The plaintext base32 TOTP secret.
    :returns: A Fernet token, safe to store in a text column.
    :raises MfaEncryptionNotConfiguredError: If no key is configured. The
        caller must not fall back to storing the plaintext.
    """
    return _primary_mfa_cipher().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_mfa_secret(stored: str) -> str:
    """Decrypt a stored TOTP secret, in memory.

    Tries the current key, then each retired key. The result must not be
    logged, returned from an API, or written anywhere.

    :param stored: The value of ``users.mfa_secret``.
    :returns: The plaintext base32 TOTP secret.
    :raises MfaEncryptionNotConfiguredError: If no key is configured.
    :raises MfaSecretDecryptionError: If the value does not decrypt under any
        configured key. A plaintext value is **not** accepted: after the data
        migration none exists, and accepting one would let anyone who can
        write the column plant a secret of their choosing.
    """
    ciphers = _all_mfa_ciphers()
    try:
        return ciphers.decrypt(stored.encode("utf-8")).decode("utf-8")
    except (InvalidToken, UnicodeError):
        msg = "The stored MFA secret could not be decrypted."
        raise MfaSecretDecryptionError(msg) from None


def mfa_secret_needs_reencryption(stored: str) -> bool:
    """Whether a stored secret is encrypted under a retired key.

    The counterpart of :func:`password_needs_rehash`: after a successful
    verification the caller re-encrypts such a value under the current key, so
    a retired key can eventually be removed from configuration.

    :param stored: A value that :func:`decrypt_mfa_secret` accepts.
    :returns: ``True`` if the current key alone cannot decrypt it.
    :raises MfaEncryptionNotConfiguredError: If no key is configured.
    """
    try:
        _primary_mfa_cipher().decrypt(stored.encode("utf-8"))
    except (InvalidToken, UnicodeError):
        return True
    return False


# ── Password Policy Validation ──────────────────────────────────────────────


def validate_password_strength(password: str) -> list[str]:
    """Validate a password against the project's strength policy.

    Rules:
    - Minimum :attr:`~app.core.config.Settings.PASSWORD_MIN_LENGTH` characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one symbol (non-alphanumeric)

    :param password: The password to validate.
    :returns: A list of violation messages (empty if the password is valid).
    """
    errors: list[str] = []

    if len(password) < settings.PASSWORD_MIN_LENGTH:
        errors.append(f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters long.")

    if not any(c.isupper() for c in password):
        errors.append("Password must contain at least one uppercase letter.")

    if not any(c.islower() for c in password):
        errors.append("Password must contain at least one lowercase letter.")

    if not any(c.isdigit() for c in password):
        errors.append("Password must contain at least one digit.")

    if not any(not c.isalnum() for c in password):
        errors.append("Password must contain at least one symbol (e.g. !@#$%^&*).")

    return errors
