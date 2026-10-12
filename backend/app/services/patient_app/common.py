"""Small pieces every patient service shares: who is calling, and how it is audited."""

from __future__ import annotations

import hashlib
import hmac
import uuid  # noqa: TC003 — needed at runtime for dataclass field resolution
from dataclasses import dataclass
from typing import Any

from app.core.audit import AuditEvent
from app.core.config import settings
from app.core.exceptions import ValidationError
from app.utils.phone import E164_PATTERN, IN_PATTERN, mask, normalize

__all__ = [
    "ClientContext",
    "mask_phone",
    "normalize_patient_phone",
    "patient_event",
    "phone_reference",
]

#: Calling code whose numbers are additionally held to the national mobile shape.
_INDIA = "+91"


@dataclass(frozen=True, slots=True)
class ClientContext:
    """Where a patient request came from.

    :param ip_address: The caller's address, from
        :func:`app.core.client_ip.client_ip` — never from what the caller claimed.
    :param user_agent: The caller's user agent, as sent.
    """

    ip_address: str | None = None
    user_agent: str | None = None


def normalize_patient_phone(raw: str) -> str:
    """Turn what a patient typed into an E.164 number a code may be sent to.

    Only numbers under ``PATIENT_OTP_ALLOWED_COUNTRY_CODES`` are accepted:
    every code costs money, and numbers abroad are how SMS-pumping fraud is
    run. An Indian number must also be a mobile number.

    :param raw: The number as typed.
    :returns: The number in E.164 form.
    :raises ValidationError: If it is not a number a code may be sent to. One
        message for every reason.
    """
    # ASCII only, before anything else looks at it. The shared patterns match
    # the digits of every script, so a number spelled with, say, Devanagari
    # digits would pass as a *different* number — with an allowance, a
    # challenge and an account of its own — while a provider that folds
    # digits would deliver it to the same handset.
    if not raw.isascii():
        raise _invalid_phone()
    phone = normalize(raw)
    allowed = next(
        (code for code in settings.PATIENT_OTP_ALLOWED_COUNTRY_CODES if phone.startswith(code)),
        None,
    )
    valid = (
        allowed is not None
        and E164_PATTERN.fullmatch(phone) is not None
        and (allowed != _INDIA or IN_PATTERN.fullmatch(phone[len(_INDIA) :]) is not None)
    )
    if not valid:
        raise _invalid_phone()
    return phone


def _invalid_phone() -> ValidationError:
    """The one refusal for every number a code may not be sent to."""
    return ValidationError(
        message="Enter a valid mobile number.",
        detail={"errors": [{"field": "phone", "message": "Enter a valid mobile number."}]},
    )


def mask_phone(phone: str) -> str:
    """A phone number with everything but its last four digits hidden."""
    return mask(phone)


def phone_reference(phone: str) -> str:
    """A non-reversible stand-in for a phone number, for logs and audit context.

    Keyed with ``PATIENT_OTP_SECRET``: a phone number has too few possible
    values for a plain hash to hide it. Enough to tie two events to the same
    number; not enough to recover it.

    :param phone: The number, in E.164 form.
    :returns: A 16-character hex string.
    """
    key = settings.PATIENT_OTP_SECRET.get_secret_value().encode("utf-8")
    digest = hmac.new(key, b"phone-reference:" + phone.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:16]


def patient_event(
    action: str,
    *,
    target_type: str,
    account_id: uuid.UUID | None = None,
    hospital_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    context: dict[str, Any] | None = None,
    client: ClientContext | None = None,
) -> AuditEvent:
    """Build the audit event for a Patient App action.

    The context must never carry a one-time code, a token, a cookie value, a
    raw phone number, or a date of birth or MRN a patient entered.

    :param action: Dotted action name, ``patient.…``.
    :param target_type: Entity type acted on.
    :param account_id: The acting patient account, once one is known.
    :param hospital_id: The hospital concerned; ``None`` for a platform-level
        event such as signing in.
    :param target_id: UUID of the entity acted on.
    :param context: Non-PII detail.
    :param client: Where the request came from.
    """
    return AuditEvent(
        action=action,
        hospital_id=hospital_id,
        target_type=target_type,
        target_id=target_id,
        context=context or {},
        actor_type="patient",
        patient_account_id=account_id,
        ip_address=client.ip_address if client else None,
        user_agent=client.user_agent if client else None,
    )
