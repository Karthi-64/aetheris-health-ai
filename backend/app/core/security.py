"""Security utilities — JWT token creation/verification, password hashing, and token generation.

This module is the single place where:
- JWTs are signed and verified
- Passwords are hashed and verified (Argon2id)
- Opaque tokens are generated
- MFA/TOTP codes are verified

**Never** reimplement any of these operations outside this module.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt as pyjwt
import pyotp
from passlib.context import CryptContext

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

    :param plain_password: The plaintext password to verify.
    :param hashed_password: The stored password hash.
    :returns: ``True`` if the password matches.
    """
    return bool(_pwd_context.verify(plain_password, hashed_password))


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
    return settings.APP_SECRET_KEY


def _get_jwt_verification_key() -> str:
    """Return the key used for verifying JWTs.

    :returns: The RSA public key if configured, otherwise the app secret key.
    """
    if settings.JWT_PUBLIC_KEY:
        return settings.JWT_PUBLIC_KEY
    return settings.APP_SECRET_KEY


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
