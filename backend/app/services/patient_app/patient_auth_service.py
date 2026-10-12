"""Patient authentication: one-time codes by SMS, and the sessions they start.

Implements ``docs/modules/15-patient-app.md`` §5 as amended by the owner: no
Redis and no per-phone lock. Guessing and cost are both bounded by the
PostgreSQL throttle the staff sign-in already uses
(:mod:`app.services.auth_throttle`), with bucket kinds of its own.

**Requesting a code** never looks an account up, never touches a hospital's
data and answers identically for every number. What it is charged to:

==========================  ====================================================
bucket                      counts
==========================  ====================================================
``pt_otp_send_source``      codes one source asks for, for any number
``pt_otp_send_pair``        codes for one number from one source
``pt_otp_send_phone``       codes for one number from every source that is not
                            a recognised device of it
``pt_otp_send_device``      codes for one number from one recognised device —
                            *instead of* the pair and phone budgets above
``pt_otp_send_global``      every code the platform sends
==========================  ====================================================

A recognised device is also charged to a budget shared by every recognised
device of its account (the ``pt_otp_send_phone`` policy, keyed by the account
id) and to its source's budget: minting device after device never yields more
codes than one account's allowance.

The recognised device exists for one reason: without it, anybody who knows a
phone number could use up that number's allowance and keep its owner from
signing in. A browser the owner has signed in from holds a random token in an
``HttpOnly`` cookie and draws on a bucket of its own that an outsider cannot
reach. It is not a credential and changes nothing else.

A send is never refunded: the code was sent and paid for whether or not it is
ever used.

**Verifying a code** has one failure, :class:`OtpInvalidError`, for every
cause, padded to the same minimum duration as a failed staff sign-in. There is
no lock on a phone number: a challenge dies after five attempts, and the send
budgets bound how many challenges there can be.

**Sessions** follow the staff rules — an opaque refresh token stored as a
hash, rotated on every use, and a revoked token presented again ends every
session of the account — with one difference: the token is locked while it is
rotated, so two simultaneous refreshes cannot both succeed.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Final

import structlog

from app.core.client_ip import UNKNOWN_SOURCE, source_of
from app.core.config import settings
from app.core.exceptions import AuthenticationError, ServiceUnavailableError
from app.core.security import (
    PATIENT_ACCESS_TTL_SECONDS,
    create_patient_access_token,
    generate_opaque_token,
    hash_token,
)
from app.core.sms import SmsMessage
from app.schemas.patient_app.auth import (
    OtpRequestResponse,
    PatientAccountSummary,
    PendingPolicy,
)
from app.services.auth_throttle import Bucket, BucketKind, bucket
from app.services.patient_app.common import (
    ClientContext,
    normalize_patient_phone,
    patient_event,
    phone_reference,
)
from app.services.patient_app.errors import OtpInvalidError, OtpThrottledError
from app.services.patient_app.patient_account_service import account_summary

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from datetime import datetime

    from app.core.audit import AuditSink
    from app.core.sms import SmsSender
    from app.database.unit_of_work import UnitOfWork
    from app.models.patient_account import PatientAccount, PatientDevice
    from app.repositories.patient_account_repository import PatientAccountRepository
    from app.repositories.patient_device_repository import PatientDeviceRepository
    from app.repositories.patient_refresh_token_repository import PatientRefreshTokenRepository
    from app.services.auth_throttle import AuthThrottle
    from app.services.patient_app.consent_service import ConsentService
    from app.services.patient_app.otp_service import OtpService

__all__ = [
    "DEVICE_TOKENS_PER_COOKIE",
    "OTP_RESEND_AFTER_SECONDS",
    "REFRESH_TOKEN_LIFETIME",
    "PatientAuthService",
    "PatientSession",
]

logger = structlog.get_logger(__name__)

#: What the client is told to wait before asking for another code.
OTP_RESEND_AFTER_SECONDS: Final = 60
#: How long a patient session lasts without being refreshed (security rule 2).
REFRESH_TOKEN_LIFETIME: Final = timedelta(days=7)

#: How long a browser stays recognised without completing a sign-in, and the
#: longest it can stay recognised at all.
_DEVICE_IDLE: Final = timedelta(days=90)
_DEVICE_LIFETIME: Final = timedelta(days=180)
#: Recognised devices kept per account, and device tokens kept in one cookie
#: (a shared family phone signs in to more than one account).
_DEVICES_PER_ACCOUNT: Final = 5
DEVICE_TOKENS_PER_COOKIE: Final = 4

#: Key space of the send budget shared by every recognised device of one
#: account. A phone number never spells this, so it cannot collide with the
#: number-keyed bucket of the same kind.
_RECOGNISED: Final = "recognised"

#: Expired challenges deleted per sweep, and how often a request sweeps (1 in N).
_PURGE_BATCH: Final = 200
_PURGE_ONE_IN: Final = 32

#: Random extra added to the uniform minimum duration, as a fraction of it.
_UNIFORM_DURATION_JITTER: Final = 0.2

#: The one answer for every refusal on the session endpoints.
_SESSION_REJECTED: Final = "Authentication required."
_SMS_UNAVAILABLE: Final = "Sign-in by SMS is temporarily unavailable."


async def _sleep(seconds: float) -> None:
    """Wait. A module-level seam so tests can observe the wait without sleeping."""
    await asyncio.sleep(seconds)


def _otp_message(phone: str, code: str) -> SmsMessage:
    """The text a code is sent in: the code and its lifetime, and nothing else."""
    return SmsMessage(
        to=phone,
        body=f"{code} is your Aetheris verification code. It expires in 5 minutes.",
        purpose="otp",
    )


@dataclass(frozen=True, slots=True)
class PatientSession:
    """A started or rotated session, on its way to the route.

    The refresh token and the device cookie value are for cookies only: the
    route never puts either in a response body. Both are kept out of ``repr``.

    :param access_token: The patient access token.
    :param expires_in: Seconds it is valid for.
    :param refresh_token: The opaque refresh token, for the refresh cookie.
    :param account: The signed-in account, as the app may show it.
    :param pending_policies: Policies still to accept.
    :param device_cookie: The device cookie value to set, if any.
    """

    access_token: str = field(repr=False)
    expires_in: int
    refresh_token: str = field(repr=False)
    account: PatientAccountSummary
    pending_policies: list[PendingPolicy] = field(default_factory=list)
    device_cookie: str | None = field(default=None, repr=False)


class PatientAuthService:
    """One-time-code sign-in and session lifecycle for patient accounts.

    :param accounts: Patient accounts.
    :param refresh_tokens: Patient sessions.
    :param devices: Recognised devices.
    :param otp: Issues and checks the codes.
    :param consent: Which policies the account still has to accept.
    :param throttle: Decides whether a request may proceed at all. Required:
        there is no unthrottled way to build this service.
    :param uow: The request's unit of work.
    :param audit: Where sign-in events are recorded.
    :param sms: The SMS transport, or ``None`` when SMS is off — in which case
        no code is ever issued.
    """

    def __init__(
        self,
        accounts: PatientAccountRepository,
        refresh_tokens: PatientRefreshTokenRepository,
        devices: PatientDeviceRepository,
        otp: OtpService,
        consent: ConsentService,
        *,
        throttle: AuthThrottle,
        uow: UnitOfWork,
        audit: AuditSink,
        sms: SmsSender | None,
    ) -> None:
        self._accounts = accounts
        self._refresh_tokens = refresh_tokens
        self._devices = devices
        self._otp = otp
        self._consent = consent
        self._throttle = throttle
        self._uow = uow
        self._audit = audit
        self._sms = sms

    # ── Requesting a code ────────────────────────────────────────────────────

    async def request_otp(
        self, phone: str, client: ClientContext, device_tokens: Sequence[str] = ()
    ) -> OtpRequestResponse:
        """Send a sign-in code to a phone number.

        The answer is the same whether or not an account exists for the
        number: no account is looked up to produce it.

        :param phone: The number as the patient typed it.
        :param client: Where the request came from.
        :param device_tokens: Device tokens the browser presented.
        :returns: The challenge id and how long the code lives.
        :raises ValidationError: If the number is not one a code may be sent to.
        :raises ServiceUnavailableError: If SMS is off, or the send failed. No
            challenge exists afterwards in either case.
        :raises OtpThrottledError: If any limit was reached. Never says which.
        """
        normalized = normalize_patient_phone(phone)
        if self._sms is None:
            # Never pretend a code was sent. Nothing is created or charged.
            logger.warning("patient_otp_unavailable", reason="no_sms_sender")
            raise ServiceUnavailableError(_SMS_UNAVAILABLE)

        now = await self._throttle.now()
        reference = phone_reference(normalized)
        device = await self._devices.find_for_phone(
            normalized, [hash_token(token) for token in device_tokens], now=now
        )

        admission = await self._throttle.admit(
            self._send_buckets(normalized, self._usable_source(client.ip_address), device)
        )
        if not admission.admitted:
            self._log_send_refusal(admission.refused_by, reference)
            raise OtpThrottledError
        if BucketKind.PT_OTP_SEND_GLOBAL in admission.budgets_exhausted:
            logger.error("patient_otp_global_ceiling_reached", phone_ref=reference)

        # The challenge is made durable before the code leaves, so a code
        # that arrives can always be verified; if it does not leave, the
        # challenge is removed again. No transaction is open during the send.
        issued = await self._otp.issue(normalized, now=now, ip_address=client.ip_address)
        await self._uow.commit()
        try:
            await asyncio.wait_for(
                self._sms.send(_otp_message(normalized, issued.code)),
                timeout=settings.SMS_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — any failure to send is one outcome
            await self._otp.discard(issued.challenge_id)
            await self._uow.commit()
            # The exception's own text is not logged: a provider can quote
            # the message it rejected, and the message is the code.
            logger.error(
                "patient_otp_send_failed", phone_ref=reference, error_type=type(exc).__name__
            )
            raise ServiceUnavailableError(_SMS_UNAVAILABLE) from None

        await self._audit.record(
            patient_event(
                "patient.auth.otp_requested",
                target_type="patient_otp_challenge",
                target_id=issued.challenge_id,
                context={"phone_ref": reference, "recognised_device": device is not None},
                client=client,
            )
        )
        await self._uow.commit()
        await self._sweep(now)
        return OtpRequestResponse(
            challenge_id=issued.challenge_id,
            expires_in=issued.expires_in,
            resend_after=OTP_RESEND_AFTER_SECONDS,
        )

    # ── Verifying a code ─────────────────────────────────────────────────────

    async def verify_otp(
        self,
        challenge_id: uuid.UUID,
        code: str,
        client: ClientContext,
        device_tokens: Sequence[str] = (),
    ) -> PatientSession:
        """Verify a sign-in code and start a session.

        :param challenge_id: The challenge the code was sent for.
        :param code: The code as typed.
        :param client: Where the request came from.
        :param device_tokens: Device tokens the browser presented.
        :returns: The session.
        :raises OtpInvalidError: For every refusal, with one message, after
            the uniform minimum duration.
        """
        started = time.monotonic()
        try:
            return await self._verify_otp(challenge_id, code, client, device_tokens)
        except OtpInvalidError:
            await self._wait_out_uniform_duration(started)
            raise

    async def _verify_otp(
        self,
        challenge_id: uuid.UUID,
        code: str,
        client: ClientContext,
        device_tokens: Sequence[str],
    ) -> PatientSession:
        """Do the work of :meth:`verify_otp`. Every refusal is an ``OtpInvalidError``."""
        # Step 1: may this source try a code at all? Charged before the code
        # is looked at, and given back only if the code is right.
        source = self._usable_source(client.ip_address)
        admission = None
        if source is not None:
            admission = await self._throttle.admit(
                [bucket(BucketKind.PT_OTP_VERIFY_SOURCE, source)]
            )
            if not admission.admitted:
                logger.info("patient_otp_verify_throttled", bucket=str(admission.refused_by))
                raise OtpInvalidError

        # Step 2: count the attempt, then compare. The count is committed at
        # once, so it stands whatever happens to the rest of this request.
        now = await self._throttle.now()
        check = await self._otp.check(challenge_id, code, now=now)
        await self._uow.commit()
        if check.outcome == "dead" or check.challenge_id is None or check.phone is None:
            logger.info("patient_otp_rejected", reason="no_live_challenge")
            raise OtpInvalidError
        reference = phone_reference(check.phone)
        if check.outcome == "wrong":
            await self._audit.record(
                patient_event(
                    "patient.auth.otp_failed",
                    target_type="patient_otp_challenge",
                    target_id=check.challenge_id,
                    context={
                        "phone_ref": reference,
                        "reason": "wrong_code",
                        "attempts": check.attempts,
                    },
                    client=client,
                )
            )
            await self._uow.commit()
            raise OtpInvalidError

        # Step 3: the code is right. Use the challenge up — exactly one of any
        # number of simultaneous requests gets past this line.
        if not await self._otp.consume(check.challenge_id, now=now):
            await self._uow.commit()
            logger.info("patient_otp_rejected", reason="already_consumed")
            raise OtpInvalidError

        # Step 4: the number is proven. Find its account, or make one.
        account, created = await self._accounts.lock_or_create_by_phone(check.phone, now=now)
        if not account.is_active:
            # The code stays used up. A suspended or closed account gets the
            # same answer as a wrong code.
            await self._uow.commit()
            logger.info(
                "patient_login_refused", account_id=str(account.id), reason="account_not_active"
            )
            raise OtpInvalidError
        await self._accounts.record_login(account, now=now)

        # Step 5: start the session. The consumed challenge, the account and
        # the refresh token commit together.
        raw_refresh_token = await self._issue_refresh_token(account.id, client, now)
        if created:
            await self._audit.record(
                patient_event(
                    "patient.auth.account_created",
                    target_type="patient_account",
                    target_id=account.id,
                    account_id=account.id,
                    context={"phone_ref": reference},
                    client=client,
                )
            )
        await self._audit.record(
            patient_event(
                "patient.auth.login",
                target_type="patient_account",
                target_id=account.id,
                account_id=account.id,
                context={"phone_ref": reference},
                client=client,
            )
        )
        summary = account_summary(account)
        await self._uow.commit()

        if admission is not None:
            await self._throttle.settle(admission)
        pending = await self._consent.pending_policies(summary.id)
        device_cookie = await self._recognise_device(summary.id, device_tokens)
        logger.info("patient_login", account_id=str(summary.id), account_created=created)
        return PatientSession(
            access_token=create_patient_access_token(summary.id),
            expires_in=PATIENT_ACCESS_TTL_SECONDS,
            refresh_token=raw_refresh_token,
            account=summary,
            pending_policies=pending,
            device_cookie=device_cookie,
        )

    # ── Sessions ─────────────────────────────────────────────────────────────

    async def refresh(self, raw_token: str, client: ClientContext) -> PatientSession:
        """Rotate a session: a new access token and a new refresh token.

        The account and the presented token are locked while this runs. If
        the token has already been revoked — rotated away, or signed out —
        presenting it again is taken as theft and every session of the
        account is ended.

        :param raw_token: The opaque refresh token from the cookie.
        :param client: Where the request came from.
        :returns: The rotated session.
        :raises AuthenticationError: For every refusal, with one message.
        """
        now = await self._throttle.now()
        token_hash = hash_token(raw_token)
        # Lock order, the same in every path that holds both: the account
        # row first, then its token rows. "End every session" (log out
        # everywhere, reuse detection) holds the account and then updates
        # every token of it, so a rotation that took its token first and the
        # account second could deadlock against it. The account lock is what
        # serialises the three: an "end every session" either commits before
        # this rotation reads the token — and the token is then seen revoked
        # below — or waits for it and ends the replacement too.
        owner_id = await self._refresh_tokens.account_id_for_token_hash(token_hash)
        account = await self._accounts.lock_by_id(owner_id) if owner_id is not None else None
        stored = (
            await self._refresh_tokens.lock_by_token_hash(token_hash)
            if account is not None
            else None
        )
        if stored is None or account is None:
            await self._uow.rollback()
            raise AuthenticationError(_SESSION_REJECTED)

        if stored.is_revoked:
            account_id = stored.account_id
            logger.warning(
                "patient_refresh_token_reuse_detected",
                token_id=str(stored.id),
                account_id=str(account_id),
            )
            # Under the account lock taken above, so a sibling token that was
            # being rotated has either committed its replacement (ended here)
            # or has not started (and will find its token revoked).
            revoked = await self._refresh_tokens.revoke_all_for_account(account_id, now=now)
            await self._audit.record(
                patient_event(
                    "patient.auth.refresh_reuse_detected",
                    target_type="patient_account",
                    target_id=account_id,
                    account_id=account_id,
                    context={"sessions_ended": revoked},
                    client=client,
                )
            )
            # Committed before raising: the request's session rolls back
            # whatever is uncommitted when it closes, which would undo both
            # the revocation and its audit row.
            await self._uow.commit()
            raise AuthenticationError(_SESSION_REJECTED)

        if stored.expires_at <= now or not account.is_active:
            await self._uow.rollback()
            raise AuthenticationError(_SESSION_REJECTED)

        summary = account_summary(account)
        raw_new_token, new_token_hash = generate_opaque_token()
        replacement = await self._refresh_tokens.create(
            account_id=summary.id,
            token_hash=new_token_hash,
            expires_at=now + REFRESH_TOKEN_LIFETIME,
            device_info=client.user_agent,
            ip_address=client.ip_address,
        )
        await self._refresh_tokens.revoke(stored, now=now, rotated_by_id=replacement.id)
        await self._uow.commit()
        logger.info("patient_token_refreshed", account_id=str(summary.id))

        # A refresh never makes a browser a recognised device: a session is
        # not a proof of the phone number.
        return PatientSession(
            access_token=create_patient_access_token(summary.id),
            expires_in=PATIENT_ACCESS_TTL_SECONDS,
            refresh_token=raw_new_token,
            account=summary,
            pending_policies=await self._consent.pending_policies(summary.id),
        )

    async def logout(self, raw_token: str | None, client: ClientContext) -> None:
        """End one session. Never fails: an unknown or missing token ends nothing.

        :param raw_token: The opaque refresh token from the cookie, if any.
        :param client: Where the request came from.
        """
        if not raw_token:
            return
        now = await self._throttle.now()
        stored = await self._refresh_tokens.lock_by_token_hash(hash_token(raw_token))
        if stored is None or stored.is_revoked:
            await self._uow.rollback()
            return
        account_id = stored.account_id
        await self._refresh_tokens.revoke(stored, now=now)
        await self._audit.record(
            patient_event(
                "patient.auth.logout",
                target_type="patient_account",
                target_id=account_id,
                account_id=account_id,
                client=client,
            )
        )
        await self._uow.commit()
        logger.info("patient_logout", account_id=str(account_id))

    async def logout_all(self, account: PatientAccount, client: ClientContext) -> int:
        """End every session of an account, and forget every recognised device.

        :param account: The authenticated account.
        :param client: Where the request came from.
        :returns: How many sessions were ended.
        """
        account_id = account.id
        now = await self._throttle.now()
        # Waits for any rotation of this account's tokens that is in flight,
        # so the replacement it is about to commit is ended with the rest.
        # Account first, then its tokens: the order ``refresh`` uses.
        await self._accounts.lock_by_id(account_id)
        revoked = await self._refresh_tokens.revoke_all_for_account(account_id, now=now)
        await self._devices.delete_for_account(account_id)
        await self._audit.record(
            patient_event(
                "patient.auth.logout_all",
                target_type="patient_account",
                target_id=account_id,
                account_id=account_id,
                context={"sessions_ended": revoked},
                client=client,
            )
        )
        await self._uow.commit()
        logger.info("patient_logout_all", account_id=str(account_id), count=revoked)
        return revoked

    # ── Internals ────────────────────────────────────────────────────────────

    async def _issue_refresh_token(
        self, account_id: uuid.UUID, client: ClientContext, now: datetime
    ) -> str:
        """Create a session row and return the opaque token for it. Does not commit."""
        raw_token, token_hash = generate_opaque_token()
        await self._refresh_tokens.create(
            account_id=account_id,
            token_hash=token_hash,
            expires_at=now + REFRESH_TOKEN_LIFETIME,
            device_info=client.user_agent,
            ip_address=client.ip_address,
        )
        return raw_token

    @staticmethod
    def _usable_source(ip_address: str | None) -> str | None:
        """The source to count a request under, or ``None`` if there is none.

        ``None`` means the source buckets are left out. They are never shared
        between every caller whose source is unknown.
        """
        source = source_of(ip_address)
        return None if source == UNKNOWN_SOURCE else source

    @staticmethod
    def _send_buckets(phone: str, source: str | None, device: PatientDevice | None) -> list[Bucket]:
        """The buckets a request for a code is charged to.

        From a recognised device of the number's account: that device's own
        bucket, and one budget shared by every recognised device of the
        account. Neither can be reached by somebody who only knows the number
        — from one address or ten thousand — so the allowance of the browser
        the owner signs in from cannot be used up from outside.

        The account-wide budget is what keeps device rows from being a
        currency: a client that signs in without its cookie is given a new
        device row each time, and without a budget keyed by the account each
        new row would bring three more codes. It is keyed by the account id —
        never by the device row — under the ``pt_otp_send_phone`` policy, in a
        key space ("recognised") of its own, so it is independent of the
        number's budget for unrecognised callers.

        From anywhere else: the number's shared budget, and, when the source
        is known, the one for this number from this source.

        The platform ceiling and, when the source is known, the source's own
        budget apply either way.

        :param phone: The number, normalised.
        :param source: Where the request came from, or ``None``.
        :param device: The recognised device it came from, or ``None``.
        """
        platform = bucket(BucketKind.PT_OTP_SEND_GLOBAL)
        if device is not None:
            buckets = [
                bucket(BucketKind.PT_OTP_SEND_DEVICE, device.id),
                bucket(BucketKind.PT_OTP_SEND_PHONE, _RECOGNISED, device.account_id),
                platform,
            ]
            if source is not None:
                buckets.append(bucket(BucketKind.PT_OTP_SEND_SOURCE, source))
            return buckets
        buckets = [bucket(BucketKind.PT_OTP_SEND_PHONE, phone), platform]
        if source is not None:
            buckets.append(bucket(BucketKind.PT_OTP_SEND_SOURCE, source))
            buckets.append(bucket(BucketKind.PT_OTP_SEND_PAIR, phone, source))
        return buckets

    @staticmethod
    def _log_send_refusal(refused_by: BucketKind | None, reference: str) -> None:
        """Log which limit refused a code. The platform ceiling is an operational alert."""
        if refused_by is BucketKind.PT_OTP_SEND_GLOBAL:
            logger.error("patient_otp_global_ceiling_reached", phone_ref=reference)
        else:
            logger.info("patient_otp_send_throttled", phone_ref=reference, bucket=str(refused_by))

    async def _recognise_device(
        self, account_id: uuid.UUID, device_tokens: Sequence[str]
    ) -> str | None:
        """Make the browser that just completed a sign-in a recognised device.

        Called only after a code was verified. If the browser is already a
        recognised device of this account its row is refreshed and its token
        kept; otherwise a new random token is issued for it. A value the
        client supplied is never adopted, so a token planted in someone's
        browser gains nothing when they sign in.

        Failing to record this never fails the sign-in: the browser is simply
        not recognised next time.

        :param account_id: The account that was signed in to.
        :param device_tokens: Tokens from the browser's cookie.
        :returns: The cookie value to give the browser, or ``None`` on failure.
        """
        try:
            now = await self._throttle.now()
            hashes = [hash_token(token) for token in device_tokens]
            device = await self._devices.find(account_id, hashes, now=now)
            minted: list[str] = []
            if device is not None:
                expires_at = min(now + _DEVICE_IDLE, device.created_at + _DEVICE_LIFETIME)
                await self._devices.touch(device, now=now, expires_at=expires_at)
            else:
                raw_token = secrets.token_urlsafe(32)
                # Room is made first, so the browser being recognised now is
                # never the one that gets dropped.
                await self._devices.prune(account_id, keep=_DEVICES_PER_ACCOUNT - 1, now=now)
                await self._devices.create(
                    account_id, hash_token(raw_token), now=now, expires_at=now + _DEVICE_IDLE
                )
                minted.append(raw_token)

            # The cookie carries one token per account that has signed in on
            # this browser. Tokens that no longer name a live row are dropped.
            live = await self._devices.live_hashes(hashes, now=now)
            kept = [token for token in device_tokens if hash_token(token) in live]
            if device is not None:
                used = [token for token in kept if hash_token(token) == device.token_hash]
                kept = [token for token in kept if token not in used] + used
            await self._uow.commit()
        except Exception as exc:  # noqa: BLE001 — recognition is a convenience; sign-in already succeeded
            await self._uow.rollback()
            logger.warning(
                "patient_device_not_recorded",
                account_id=str(account_id),
                error_type=type(exc).__name__,
            )
            return None
        return ".".join((kept + minted)[-DEVICE_TOKENS_PER_COOKIE:]) or None

    async def _sweep(self, now: datetime) -> None:
        """Now and then, delete a batch of challenges that died long ago."""
        if secrets.randbelow(_PURGE_ONE_IN) != 0:
            return
        deleted = await self._otp.purge(now=now, limit=_PURGE_BATCH)
        await self._uow.commit()
        if deleted:
            logger.info("patient_otp_challenges_purged", rows=deleted)

    async def _wait_out_uniform_duration(self, started: float) -> None:
        """Hold a refusal until the uniform minimum duration has passed.

        The patient-side equivalent of the staff sign-in's floor, with the
        same setting and the same shape. A dead challenge is refused after one
        statement and a wrong code after several; padding both to one floor —
        plus a random extra, and rounded up to a whole multiple when the work
        itself ran long — keeps the difference from being measured.

        The transaction is ended first, so nothing is held while waiting.

        :param started: ``time.monotonic()`` taken when the operation began.
        """
        await self._uow.commit()
        floor = settings.AUTH_FAILURE_MIN_SECONDS
        if floor <= 0:
            return
        jitter = floor * _UNIFORM_DURATION_JITTER * (secrets.randbelow(10_000) / 10_000)
        elapsed = time.monotonic() - started
        steps = max(1, math.ceil(elapsed / floor))
        remaining = steps * floor + jitter - elapsed
        if remaining > 0:
            await _sleep(remaining)
