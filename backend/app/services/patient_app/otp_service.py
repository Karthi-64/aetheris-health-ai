"""One-time sign-in codes: issue one, check one, use one up.

The rules of ``docs/modules/15-patient-app.md`` §5.2, and nothing about who
may ask or how often — throttling and sessions belong to
:class:`~app.services.patient_app.patient_auth_service.PatientAuthService`.

* Six digits from the operating system's CSPRNG.
* Alive for five minutes, for five verification attempts, and for one success.
* Stored only as ``HMAC-SHA-256(PATIENT_OTP_SECRET, challenge id ‖ phone ‖
  code)``. A plain hash would not do: with a million possible codes, a stolen
  table could be reversed in a second. Binding the challenge id and the phone
  into the hash means a code verifies the challenge it was issued for and no
  other.
* Compared in constant time.

The code exists in plain text in exactly two places: this process's memory,
and the message handed to the SMS sender. It is never logged, audited,
stored, or put in an exception.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from app.core.config import settings

if TYPE_CHECKING:
    from app.repositories.patient_otp_challenge_repository import PatientOtpChallengeRepository

__all__ = [
    "OTP_LENGTH",
    "OTP_LIFETIME",
    "OTP_MAX_ATTEMPTS",
    "IssuedOtp",
    "OtpCheck",
    "OtpService",
]

#: Digits in a code.
OTP_LENGTH: Final = 6
#: How long a code can be used for.
OTP_LIFETIME: Final = timedelta(minutes=5)
#: Verification attempts one challenge allows before it is dead.
OTP_MAX_ATTEMPTS: Final = 5

_HASH_CONTEXT: Final = b"aetheris:patient-otp:v1"


@dataclass(frozen=True, slots=True)
class IssuedOtp:
    """A freshly issued code, on its way to the SMS sender.

    :param challenge_id: The challenge the code belongs to.
    :param code: The code. Kept out of ``repr`` so it cannot be logged by accident.
    :param expires_in: Seconds the code lives for.
    """

    challenge_id: uuid.UUID
    code: str = field(repr=False)
    expires_in: int


@dataclass(frozen=True, slots=True)
class OtpCheck:
    """What presenting a code to a challenge came to.

    :param outcome: ``dead`` — there was no live challenge to try it against
        (unknown, expired, used, or out of attempts); ``wrong``; or ``correct``.
    :param challenge_id: The challenge, when it was alive.
    :param phone: The number the code was sent to, when the challenge was alive.
    :param attempts: Attempts counted against the challenge so far.
    """

    outcome: Literal["dead", "wrong", "correct"]
    challenge_id: uuid.UUID | None = None
    phone: str | None = None
    attempts: int = 0


def _hash_code(challenge_id: uuid.UUID, phone: str, code: str) -> str:
    """The keyed hash a code is stored and compared as.

    Each part is length-prefixed, so no two different triples can produce the
    same input.
    """
    key = settings.PATIENT_OTP_SECRET.get_secret_value().encode("utf-8")
    digest = hmac.new(key, _HASH_CONTEXT, hashlib.sha256)
    for part in (str(challenge_id), phone, code):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


class OtpService:
    """Issues and checks one-time codes.

    :param challenges: Challenge data access.
    """

    def __init__(self, challenges: PatientOtpChallengeRepository) -> None:
        self._challenges = challenges

    async def issue(self, phone: str, *, now: datetime, ip_address: str | None) -> IssuedOtp:
        """Create a challenge for a number and return its code.

        Does not commit, and does not send anything.

        :param phone: The number, in E.164 form.
        :param now: The instant the code's lifetime starts.
        :param ip_address: Where the request came from, if known.
        :returns: The challenge id and the code.
        """
        challenge_id = uuid.uuid4()
        code = f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"
        await self._challenges.create(
            challenge_id=challenge_id,
            phone=phone,
            code_hash=_hash_code(challenge_id, phone, code),
            expires_at=now + OTP_LIFETIME,
            ip_address=ip_address,
        )
        return IssuedOtp(
            challenge_id=challenge_id, code=code, expires_in=int(OTP_LIFETIME.total_seconds())
        )

    async def check(self, challenge_id: uuid.UUID, code: str, *, now: datetime) -> OtpCheck:
        """Count an attempt against a challenge, then compare the code.

        The attempt is counted first, in one atomic statement that also
        refuses a dead challenge. Only then is the code looked at, so no
        number of parallel requests can try more codes than the challenge
        allows. A dead challenge still does one hash comparison, so that it
        does not answer faster than a live one.

        Does not commit: the caller commits the counted attempt.

        :param challenge_id: The challenge the caller named.
        :param code: The code the caller presented.
        :param now: The instant to judge at.
        :returns: The outcome.
        """
        attempt = await self._challenges.register_attempt(
            challenge_id, now=now, max_attempts=OTP_MAX_ATTEMPTS
        )
        if attempt is None:
            hmac.compare_digest(_hash_code(challenge_id, "", code), "0" * 64)
            return OtpCheck(outcome="dead")
        matches = hmac.compare_digest(
            _hash_code(attempt.challenge_id, attempt.phone, code), attempt.code_hash
        )
        return OtpCheck(
            outcome="correct" if matches else "wrong",
            challenge_id=attempt.challenge_id,
            phone=attempt.phone,
            attempts=attempt.attempts,
        )

    async def consume(self, challenge_id: uuid.UUID, *, now: datetime) -> bool:
        """Use a challenge up, if nobody has.

        :param challenge_id: A challenge whose code was just matched.
        :param now: When.
        :returns: ``True`` for exactly one of any number of simultaneous callers.
        """
        return await self._challenges.consume(challenge_id, now=now)

    async def discard(self, challenge_id: uuid.UUID) -> None:
        """Remove a challenge whose code never reached the patient.

        :param challenge_id: The challenge to remove.
        """
        await self._challenges.discard(challenge_id)

    async def purge(self, *, now: datetime, limit: int) -> int:
        """Delete a batch of challenges that have been dead for a day.

        :param now: The current instant.
        :param limit: The most rows to delete.
        :returns: How many were deleted.
        """
        return await self._challenges.purge_expired(before=now - timedelta(days=1), limit=limit)
