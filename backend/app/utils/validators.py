"""Shared validation utilities.

Stateless validation functions for common input patterns.
These are pure functions with no framework dependencies — trivially testable.
"""

from __future__ import annotations

import re
from typing import Final

#: RFC 5322 simplified email pattern.
_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$",
)

#: Password minimum requirements.
_PASSWORD_MIN_LENGTH: Final[int] = 12
_PASSWORD_HAS_UPPER: Final[re.Pattern[str]] = re.compile(r"[A-Z]")
_PASSWORD_HAS_LOWER: Final[re.Pattern[str]] = re.compile(r"[a-z]")
_PASSWORD_HAS_DIGIT: Final[re.Pattern[str]] = re.compile(r"\d")
_PASSWORD_HAS_SYMBOL: Final[re.Pattern[str]] = re.compile(
    r"[!@#$%^&*(),.\"':{}|<>?~_\-+=\[\]\\;`/]"
)

#: MRN format pattern.
_MRN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^MRN-\d{4}-\d{5}$")


def is_valid_email(email: str) -> bool:
    """Check if an email address has valid format (RFC 5322 simplified).

    :param email: The email string to validate.
    :returns: ``True`` if the email format is valid.
    """
    if not email or len(email) > 254:
        return False
    return bool(_EMAIL_PATTERN.match(email))


def is_strong_password(password: str) -> bool:
    """Check if a password meets the application's strength requirements.

    Delegates to :func:`password_errors` to keep validation logic in
    one place.

    :param password: The password to validate.
    :returns: ``True`` if the password meets all requirements.
    """
    return len(password_errors(password)) == 0


def password_errors(password: str) -> list[str]:
    """Return a list of human-readable password requirement violations.

    :param password: The password to validate.
    :returns: A list of error messages, empty if the password is valid.
    """
    errors: list[str] = []

    if len(password) < _PASSWORD_MIN_LENGTH:
        errors.append(f"Password must be at least {_PASSWORD_MIN_LENGTH} characters.")

    if not _PASSWORD_HAS_UPPER.search(password):
        errors.append("Password must contain at least one uppercase letter.")

    if not _PASSWORD_HAS_LOWER.search(password):
        errors.append("Password must contain at least one lowercase letter.")

    if not _PASSWORD_HAS_DIGIT.search(password):
        errors.append("Password must contain at least one digit.")

    if not _PASSWORD_HAS_SYMBOL.search(password):
        errors.append("Password must contain at least one symbol.")

    return errors


def is_valid_mrn(mrn: str) -> bool:
    """Check if an MRN string matches the expected format.

    .. code-block:: text

        MRN-{YEAR}-{SEQUENCE}
        e.g.  MRN-2026-00042

    :param mrn: The MRN string to validate.
    :returns: ``True`` if the MRN format is valid.
    """
    return bool(_MRN_PATTERN.match(mrn))


def is_valid_uuid(value: str) -> bool:
    """Check if a string is a valid UUID v4.

    :param value: The string to check.
    :returns: ``True`` if the string is a valid UUID.
    """
    import uuid as _uuid

    try:
        _uuid.UUID(value, version=4)
        return True
    except (ValueError, AttributeError):
        return False


__all__ = [
    "is_strong_password",
    "is_valid_email",
    "is_valid_mrn",
    "is_valid_uuid",
    "password_errors",
]


def normalize_email(email: str) -> str:
    """Return the canonical form of a staff email address.

    Staff identity is the lower-cased address: ``Asha@Hospital.in`` and
    ``asha@hospital.in`` are the same account. Applied at every boundary where
    an address identifies a staff user — login, invite, password reset — and
    on every write to ``users.email``.

    Patient identity is separate and does not use this.

    :param email: An address as typed or stored.
    :returns: The address stripped of surrounding whitespace and lower-cased.
    """
    return email.strip().lower()
