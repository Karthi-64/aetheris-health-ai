"""Data access for one-time-code challenges.

``patient_otp_challenges`` is platform-level (see
``app/models/patient_account.py``): a code is requested before any account or
hospital is known, so this repository takes no hospital.

The two operations that decide whether a code is accepted are each **one
statement**, so that they stay exact under parallel requests:

* :meth:`register_attempt` counts an attempt and refuses a dead challenge in
  the same ``UPDATE`` — there is no read-then-write window in which more than
  the allowed number of guesses could be evaluated;
* :meth:`consume` marks the challenge used only if nobody has — so any number
  of simultaneous presentations of the right code produce exactly one winner.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for dataclass field resolution
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update

from app.models.patient_account import PatientOtpChallenge

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["OtpAttempt", "PatientOtpChallengeRepository"]


@dataclass(frozen=True, slots=True)
class OtpAttempt:
    """A live challenge, as it stood when one attempt was counted against it.

    :param challenge_id: The challenge.
    :param phone: The number the code was sent to.
    :param code_hash: The keyed hash the presented code must reproduce.
    :param attempts: Attempts counted so far, this one included.
    """

    challenge_id: uuid.UUID
    phone: str
    code_hash: str
    attempts: int


class PatientOtpChallengeRepository:
    """Reads and writes one-time-code challenges.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        challenge_id: uuid.UUID,
        phone: str,
        code_hash: str,
        expires_at: datetime,
        ip_address: str | None,
    ) -> PatientOtpChallenge:
        """Store a new challenge.

        :param challenge_id: The id the code's hash was computed with.
        :param phone: The number the code is being sent to.
        :param code_hash: Keyed hash of the code. Never the code.
        :param expires_at: When the code dies.
        :param ip_address: Where the request came from, if known.
        :returns: The new row.
        """
        challenge = PatientOtpChallenge(
            id=challenge_id,
            phone=phone,
            code_hash=code_hash,
            expires_at=expires_at,
            attempts=0,
            ip_address=ip_address,
        )
        self._session.add(challenge)
        await self._session.flush()
        return challenge

    async def get(self, challenge_id: uuid.UUID) -> PatientOtpChallenge | None:
        """Read one challenge as it is now.

        :param challenge_id: The challenge.
        :returns: The row, or ``None``.
        """
        result = await self._session.execute(
            select(PatientOtpChallenge)
            .where(PatientOtpChallenge.id == challenge_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def register_attempt(
        self, challenge_id: uuid.UUID, *, now: datetime, max_attempts: int
    ) -> OtpAttempt | None:
        """Count one verification attempt against a challenge, if it is still alive.

        One atomic statement. A challenge that is unknown, expired, already
        used or out of attempts matches no row and nothing is returned — the
        four cases are deliberately indistinguishable to the caller.

        :param challenge_id: The challenge the caller named.
        :param now: The instant to judge expiry at.
        :param max_attempts: Attempts a challenge allows in its lifetime.
        :returns: The challenge with this attempt counted, or ``None``.
        """
        result = await self._session.execute(
            update(PatientOtpChallenge)
            .where(
                PatientOtpChallenge.id == challenge_id,
                PatientOtpChallenge.consumed_at.is_(None),
                PatientOtpChallenge.expires_at > now,
                PatientOtpChallenge.attempts < max_attempts,
            )
            .values(attempts=PatientOtpChallenge.attempts + 1)
            .returning(
                PatientOtpChallenge.id,
                PatientOtpChallenge.phone,
                PatientOtpChallenge.code_hash,
                PatientOtpChallenge.attempts,
            )
            .execution_options(synchronize_session=False)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return OtpAttempt(
            challenge_id=row.id, phone=row.phone, code_hash=row.code_hash, attempts=row.attempts
        )

    async def consume(self, challenge_id: uuid.UUID, *, now: datetime) -> bool:
        """Mark a challenge used, if nobody has used it yet.

        One atomic statement: of any number of callers presenting the right
        code at once, exactly one gets ``True``.

        :param challenge_id: The challenge whose code was just matched.
        :param now: When it was matched.
        :returns: Whether this caller is the one that consumed it.
        """
        result = await self._session.execute(
            update(PatientOtpChallenge)
            .where(
                PatientOtpChallenge.id == challenge_id,
                PatientOtpChallenge.consumed_at.is_(None),
                PatientOtpChallenge.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(PatientOtpChallenge.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None

    async def discard(self, challenge_id: uuid.UUID) -> None:
        """Delete a challenge whose code could not be sent.

        :param challenge_id: The challenge to remove.
        """
        await self._session.execute(
            delete(PatientOtpChallenge)
            .where(PatientOtpChallenge.id == challenge_id)
            .execution_options(synchronize_session=False)
        )

    async def purge_expired(self, *, before: datetime, limit: int) -> int:
        """Delete a bounded batch of challenges that died some time ago.

        Rows another transaction has locked are skipped, never waited for.

        :param before: Challenges that expired before this are eligible.
        :param limit: The most rows to delete in one call.
        :returns: How many were deleted.
        """
        expired = (
            select(PatientOtpChallenge.id)
            .where(PatientOtpChallenge.expires_at < before)
            .order_by(PatientOtpChallenge.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(
            delete(PatientOtpChallenge)
            .where(PatientOtpChallenge.id.in_(expired))
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)
