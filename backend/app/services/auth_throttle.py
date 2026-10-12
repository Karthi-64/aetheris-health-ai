"""Authentication throttle — slows guessing down without locking anyone out.

This replaces the account lockout (five wrong passwords locked the account for
everyone for thirty minutes), which let anybody who knew a staff email keep
its owner out for as long as they liked.

**What is counted.** Every attempt is charged to several buckets at once:

==================  =========  ====================================================
bucket              shape      counts
==================  =========  ====================================================
``pw_source``       budget     password attempts from one source, any account
``pw_pair``         backoff    password attempts on one account from one source
``pw_account``      budget     password attempts on one account from every source
                               that is not a trusted device of it
``pw_device``       backoff    password attempts on one account from one of its
                               trusted devices
``pw_device_cap``   budget     the same, as a ceiling a success cannot reset
``mfa_origin``      backoff    wrong second-factor codes from one source or device
``mfa_account``     budget     second-factor attempts on one account from everywhere
                               except a device that has itself passed the second
                               factor before
``mfa_device_cap``  budget     second-factor attempts from one such device
``session_pw``      backoff    password re-checks inside a signed-in session
``session_code``    backoff    code re-checks inside a signed-in session
``invite_resend``   budget     invitation emails sent again for one account
==================  =========  ====================================================

The Patient App (``docs/modules/15-patient-app.md`` §5) counts in the same
table, with kinds of its own that only the patient services use:

========================  ======  ============================================
bucket                    shape   counts
========================  ======  ============================================
``pt_otp_send_source``    budget  codes requested from one source, any number
``pt_otp_send_pair``      budget  codes requested for one number from one source
``pt_otp_send_phone``     budget  codes requested for one number from every
                                  source that is not a recognised device of it;
                                  and, in a key space of its own, codes
                                  requested from every recognised device of
                                  one account together
``pt_otp_send_device``    budget  codes requested for one number from one of
                                  its recognised devices
``pt_otp_send_global``    budget  codes sent by the whole platform
``pt_otp_verify_source``  budget  code verifications from one source
``pt_link_attempt``       budget  record link or registration attempts by one
                                  account at one hospital, per hour
``pt_link_daily``         budget  the same attempts, per day
========================  ======  ============================================

A *backoff* bucket lets a few attempts through at once and then makes each
further one wait twice as long as the last, up to a cap. A *budget* is an
allowance that refills at a steady rate. Nothing is ever locked: when the wait
is over, the next attempt is judged on its merits.

**Why both an account and a source.** Counting by source alone does nothing
against guessing from many addresses. Counting by account alone is the old
lockout. So an attacker's own source runs out first (``pw_pair``,
``pw_source``); guessing from many sources runs into the account's shared
budget (``pw_account``); and a browser the owner has already signed in from
(a *trusted device*) draws on a bucket of its own that an outsider cannot
reach, so the owner keeps getting in while the shared budget is exhausted.

**Charge first.** An attempt is charged *before* its credential is looked at,
in one short transaction that takes the bucket rows under lock, and the charge
is given back if the credential turns out to be right. Under parallel requests
the buckets therefore admit exactly as many attempts as they allow and no
more; an attempt that crashes half-way stays counted.

**What a success clears.** Only the backoff of the origin that succeeded. A
shared budget is given back the one unit that attempt took and is otherwise
refilled by time alone — so somebody else signing in never hands an attacker a
fresh allowance.

The numbers are constants, not settings: configuration cannot switch this off.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from app.database.unit_of_work import UnitOfWork
    from app.models.auth_throttle import AuthThrottleBucket
    from app.repositories.auth_throttle_repository import AuthThrottleRepository

__all__ = [
    "POLICIES",
    "Admission",
    "AuthThrottle",
    "Backoff",
    "Bucket",
    "BucketKind",
    "Budget",
    "bucket",
]

logger = structlog.get_logger(__name__)


class BucketKind(StrEnum):
    """The counters. Declaration order is the order they are locked in."""

    PW_SOURCE = "pw_source"
    PW_PAIR = "pw_pair"
    PW_DEVICE = "pw_device"
    PW_DEVICE_CAP = "pw_device_cap"
    PW_ACCOUNT = "pw_account"
    MFA_ORIGIN = "mfa_origin"
    MFA_DEVICE_CAP = "mfa_device_cap"
    MFA_ACCOUNT = "mfa_account"
    SESSION_PW = "session_pw"
    SESSION_CODE = "session_code"
    INVITE_RESEND = "invite_resend"
    # Patient App. Appended, so the lock order of the kinds above is unchanged.
    PT_OTP_SEND_SOURCE = "pt_otp_send_source"
    PT_OTP_SEND_PAIR = "pt_otp_send_pair"
    PT_OTP_SEND_PHONE = "pt_otp_send_phone"
    PT_OTP_SEND_DEVICE = "pt_otp_send_device"
    PT_OTP_SEND_GLOBAL = "pt_otp_send_global"
    PT_OTP_VERIFY_SOURCE = "pt_otp_verify_source"
    PT_LINK_ATTEMPT = "pt_link_attempt"
    PT_LINK_DAILY = "pt_link_daily"


@dataclass(frozen=True, slots=True)
class Backoff:
    """A few attempts at once, then exponentially longer waits.

    :param free: Attempts evaluated without any wait. The last of them starts
        the first wait.
    :param base: The first wait.
    :param cap: The longest wait.
    :param quiet: With no attempt for this long, the bucket starts over. It
        must exceed ``cap``, or a bucket at the cap would forget itself
        between attempts.
    """

    free: int
    base: timedelta
    cap: timedelta
    quiet: timedelta


@dataclass(frozen=True, slots=True)
class Budget:
    """An allowance of ``burst`` attempts that refills one every ``refill``."""

    burst: int
    refill: timedelta


_MINUTE = timedelta(minutes=1)
_HOUR = timedelta(hours=1)

#: The whole policy. See the module docstring for what each bucket counts.
POLICIES: Final[dict[BucketKind, Backoff | Budget]] = {
    # One source, any account. Sized for a hospital behind a single address at
    # shift change, not for one person: it exists to cap spraying, and the
    # per-account buckets do the rest.
    BucketKind.PW_SOURCE: Budget(burst=300, refill=timedelta(seconds=15)),
    BucketKind.PW_PAIR: Backoff(free=5, base=_MINUTE, cap=30 * _MINUTE, quiet=2 * _HOUR),
    # Every source that is not a trusted device, together: 20 guesses, then
    # 96 a day, however many addresses are used.
    BucketKind.PW_ACCOUNT: Budget(burst=20, refill=15 * _MINUTE),
    BucketKind.PW_DEVICE: Backoff(free=5, base=_MINUTE, cap=30 * _MINUTE, quiet=2 * _HOUR),
    BucketKind.PW_DEVICE_CAP: Budget(burst=10, refill=30 * _MINUTE),
    BucketKind.MFA_ORIGIN: Backoff(free=5, base=_MINUTE, cap=_HOUR, quiet=4 * _HOUR),
    # A six-digit code: 10 guesses, then 48 a day, across every source,
    # session and password reset — everything except a device that has
    # already passed the second factor on this account, which has the same
    # allowance to itself. Somebody who has only stolen the password can
    # therefore use up the shared one without touching the owner's devices.
    BucketKind.MFA_ACCOUNT: Budget(burst=10, refill=30 * _MINUTE),
    BucketKind.MFA_DEVICE_CAP: Budget(burst=10, refill=30 * _MINUTE),
    BucketKind.SESSION_PW: Backoff(free=5, base=_MINUTE, cap=30 * _MINUTE, quiet=2 * _HOUR),
    BucketKind.SESSION_CODE: Backoff(free=5, base=_MINUTE, cap=_HOUR, quiet=4 * _HOUR),
    BucketKind.INVITE_RESEND: Budget(burst=3, refill=10 * _MINUTE),
    # ── Patient App ──────────────────────────────────────────────────────
    # Every code that is sent costs money, so sends are budgets and a
    # successful sign-in gives nothing back. One source asking for codes for
    # many numbers: sized for a clinic's waiting room behind one address.
    BucketKind.PT_OTP_SEND_SOURCE: Budget(burst=30, refill=_MINUTE),
    # One number from one source: three codes, then one every ten minutes.
    BucketKind.PT_OTP_SEND_PAIR: Budget(burst=3, refill=10 * _MINUTE),
    # One number from every source that is not a recognised device of it:
    # what bounds a flood of texts to somebody's phone from many addresses.
    BucketKind.PT_OTP_SEND_PHONE: Budget(burst=6, refill=5 * _MINUTE),
    # One number from one browser its owner has signed in from before. Drawn
    # on *instead of* the pair and phone budgets above, so that somebody who
    # only knows the number cannot use up the owner's own allowance. Every
    # recognised device of one account also shares a budget under the
    # ``PT_OTP_SEND_PHONE`` policy, keyed by the account id, so that minting
    # device rows mints no allowance; and the source budget still applies.
    BucketKind.PT_OTP_SEND_DEVICE: Budget(burst=3, refill=10 * _MINUTE),
    # The whole platform: the ceiling on what SMS pumping can cost.
    BucketKind.PT_OTP_SEND_GLOBAL: Budget(burst=600, refill=timedelta(seconds=1)),
    # Verifications from one source. A single challenge already dies after
    # five attempts; this bounds one source working through many challenges.
    BucketKind.PT_OTP_VERIFY_SOURCE: Budget(burst=60, refill=timedelta(seconds=30)),
    # Attempts to link to, or register, a record: five, then five an hour,
    # per account and hospital.
    BucketKind.PT_LINK_ATTEMPT: Budget(burst=5, refill=12 * _MINUTE),
    # The same attempts, over a day: ten, then ten a day. Charged together
    # with the hourly allowance, so both must have room
    # (``docs/modules/15-patient-app.md`` §4.5: 5 per hour *and* 10 per day).
    BucketKind.PT_LINK_DAILY: Budget(burst=10, refill=144 * _MINUTE),
}

_LOCK_ORDER: Final[dict[BucketKind, int]] = {kind: index for index, kind in enumerate(BucketKind)}

#: The largest exponent applied to a backoff's base. Far past any cap; it only
#: keeps the arithmetic small.
_MAX_DOUBLINGS: Final = 20

#: Expired rows deleted per sweep, and how often an admission sweeps (1 in N).
_PURGE_BATCH: Final = 200
_PURGE_ONE_IN: Final = 32

_KEY_PREFIX: Final = b"aetheris:auth-throttle:v1"


@dataclass(frozen=True, slots=True)
class Bucket:
    """One counter: what kind it is and which row it lives in."""

    kind: BucketKind
    key_hash: str

    @property
    def policy(self) -> Backoff | Budget:
        """The rule this bucket follows."""
        return POLICIES[self.kind]


def bucket(kind: BucketKind, *parts: str | uuid.UUID) -> Bucket:
    """Name a bucket.

    The key is a SHA-256 over the kind and the parts, each length-prefixed so
    that no two different lists of parts can collide. It is a hash, not a
    secret: it depends on nothing that differs between processes or
    restarts, so every worker counts in the same row.

    :param kind: Which counter.
    :param parts: What it counts — a normalised email, a source, a user id.
    :returns: The bucket.
    """
    digest = hashlib.sha256(_KEY_PREFIX)
    for part in (kind.value, *(str(p) for p in parts)):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return Bucket(kind=kind, key_hash=digest.hexdigest())


@dataclass(frozen=True, slots=True)
class _Charge:
    """What one admitted attempt did to one bucket, so it can be undone."""

    bucket: Bucket
    failures_after: int = 0
    blocked_after: datetime | None = None
    last_charged_before: datetime | None = None
    #: Seconds of wait this charge started on a backoff bucket, if any.
    wait_started: int = 0
    #: Whether this charge took the last of a budget.
    exhausted: bool = False


@dataclass(frozen=True, slots=True)
class Admission:
    """The outcome of asking whether an attempt may be evaluated.

    :param admitted: Whether to look at the credential at all.
    :param refused_by: The bucket kind that said no, for the log.
    :param charges: What was charged, for :meth:`AuthThrottle.settle`.
    """

    admitted: bool
    refused_by: BucketKind | None = None
    charges: tuple[_Charge, ...] = ()

    @property
    def wait_started(self) -> tuple[BucketKind, int] | None:
        """The longest wait this attempt started, if it fails: ``(kind, seconds)``."""
        waits = [(c.bucket.kind, c.wait_started) for c in self.charges if c.wait_started]
        return max(waits, key=lambda item: item[1]) if waits else None

    @property
    def budgets_exhausted(self) -> tuple[BucketKind, ...]:
        """The budgets this attempt used the last of, if it fails."""
        return tuple(c.bucket.kind for c in self.charges if c.exhausted)


def _is_open(row: AuthThrottleBucket, policy: Backoff | Budget, now: datetime) -> bool:
    """Whether a bucket would let one more attempt through right now."""
    if isinstance(policy, Backoff):
        return row.blocked_until is None or now >= row.blocked_until
    drained = max(row.drains_at, now) if row.drains_at is not None else now
    return drained + policy.refill <= now + policy.burst * policy.refill


def _charge(row: AuthThrottleBucket, target: Bucket, now: datetime) -> _Charge:
    """Charge one attempt to an open bucket, as if it will fail."""
    policy = target.policy
    if isinstance(policy, Budget):
        drained = max(row.drains_at, now) if row.drains_at is not None else now
        row.drains_at = drained + policy.refill
        row.expires_at = row.drains_at
        return _Charge(bucket=target, exhausted=not _is_open(row, policy, now))

    last_before = row.last_charged_at
    failures = row.failures
    if last_before is None or now - last_before >= policy.quiet:
        failures = 0
    failures += 1
    wait = timedelta(0)
    if failures >= policy.free:
        doublings = min(failures - policy.free, _MAX_DOUBLINGS)
        wait = min(policy.cap, policy.base * (2**doublings))
    row.failures = failures
    row.last_charged_at = now
    row.blocked_until = now + wait if wait else None
    row.expires_at = now + policy.quiet
    return _Charge(
        bucket=target,
        failures_after=failures,
        blocked_after=row.blocked_until,
        last_charged_before=last_before,
        wait_started=int(wait.total_seconds()),
    )


def _refund(row: AuthThrottleBucket, charge: _Charge, now: datetime) -> None:
    """Give back what one attempt was charged, and nothing more."""
    policy = charge.bucket.policy
    if isinstance(policy, Budget):
        if row.drains_at is not None:
            row.drains_at = row.drains_at - policy.refill
            row.expires_at = max(row.drains_at, now)
        return

    untouched = row.failures == charge.failures_after and row.blocked_until == charge.blocked_after
    row.failures = max(row.failures - 1, 0)
    if untouched:
        # Nobody else has been charged since: put the bucket back exactly as
        # it was, including the wait this attempt started.
        row.blocked_until = None
        row.last_charged_at = charge.last_charged_before
    if row.failures < policy.free:
        # A wait exists only because of the attempts that are still counted.
        # With fewer of those than the free allowance there is nothing to
        # wait for — whichever of several simultaneous attempts started it.
        row.blocked_until = None
    if row.failures == 0:
        row.last_charged_at = None
        row.expires_at = now


def _clear(row: AuthThrottleBucket, now: datetime) -> None:
    """Return a backoff bucket to its starting state."""
    row.failures = 0
    row.blocked_until = None
    row.last_charged_at = None
    row.expires_at = now


class AuthThrottle:
    """Decides whether an authentication attempt may be evaluated.

    :param buckets: Bucket data access.
    :param uow: The request's unit of work. Admission and settlement each end
        with a commit, so no bucket lock outlives the decision it protects.
    """

    def __init__(self, buckets: AuthThrottleRepository, uow: UnitOfWork) -> None:
        self._buckets = buckets
        self._uow = uow

    async def now(self) -> datetime:
        """The clock every throttle decision is timed by (the database's)."""
        return await self._buckets.now()

    async def admit(self, targets: Sequence[Bucket]) -> Admission:
        """Charge an attempt to its buckets, if every one of them is open.

        The buckets are locked one at a time in a fixed order and checked as
        they are locked. At the first one that is closed the attempt is
        refused and nothing is charged — a refusal never uses up allowance and
        never lengthens a wait, so hammering a closed bucket achieves nothing.
        If all are open, all are charged.

        Commits before returning, in both cases.

        :param targets: The buckets this attempt draws on.
        :returns: Whether to evaluate the credential, and what was charged.
        """
        ordered = sorted(targets, key=lambda t: (_LOCK_ORDER[t.kind], t.key_hash))
        now = await self._buckets.now()

        rows: list[AuthThrottleBucket] = []
        refused_by: BucketKind | None = None
        for target in ordered:
            row = await self._buckets.lock(target.kind.value, target.key_hash, now=now)
            if not _is_open(row, target.policy, now):
                refused_by = target.kind
                break
            rows.append(row)

        if refused_by is not None:
            await self._uow.commit()
            # Rows created for a refused attempt hold nothing and have already
            # expired; refusals sweep too, so a flood of them cannot fill the
            # table faster than it is emptied.
            await self._sweep(now)
            return Admission(admitted=False, refused_by=refused_by)

        charges = tuple(
            _charge(row, target, now) for row, target in zip(rows, ordered, strict=True)
        )
        await self._buckets.flush()
        await self._uow.commit()
        await self._sweep(now)
        return Admission(admitted=True, charges=charges)

    async def settle(self, admission: Admission, *, clear: Sequence[Bucket] = ()) -> None:
        """Record that an admitted attempt presented a correct credential.

        Every charge the attempt made is given back, and the backoff buckets
        named in ``clear`` are returned to their starting state. Shared
        budgets cannot be cleared this way: a bucket in ``clear`` that is not
        a backoff bucket is ignored.

        Commits before returning.

        :param admission: What :meth:`admit` returned.
        :param clear: The succeeding origin's own backoff buckets.
        """
        refunds = {charge.bucket.key_hash: charge for charge in admission.charges}
        clears = {target.key_hash: target for target in clear if isinstance(target.policy, Backoff)}
        touched = {charge.bucket for charge in admission.charges} | set(clears.values())
        if not touched:
            return

        now = await self._buckets.now()
        for target in sorted(touched, key=lambda t: (_LOCK_ORDER[t.kind], t.key_hash)):
            row = await self._buckets.lock(target.kind.value, target.key_hash, now=now)
            if target.key_hash in clears:
                _clear(row, now)
            else:
                _refund(row, refunds[target.key_hash], now)
        await self._buckets.flush()
        await self._uow.commit()

    async def _sweep(self, now: datetime) -> None:
        """Now and then, delete a batch of rows that hold nothing any more."""
        if secrets.randbelow(_PURGE_ONE_IN) != 0:
            return
        deleted = await self._buckets.purge_expired(now=now, limit=_PURGE_BATCH)
        await self._uow.commit()
        if deleted:
            logger.info("auth_throttle_purged", rows=deleted)
