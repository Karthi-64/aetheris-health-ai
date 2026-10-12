"""Authentication service — login, logout, refresh, password management, MFA.

Implements the business rules from ``docs/modules/01-authentication.md``.
Every rule is enforced here, never in the route layer.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import structlog

from app.core.audit import AuditEvent
from app.core.client_ip import UNKNOWN_SOURCE, source_of
from app.core.config import settings
from app.core.email import email_delivery_configured
from app.core.exceptions import (
    AuthenticationError,
    BusinessRuleError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.core.notifications import NotificationRequest, Notifier, NullNotifier
from app.core.security import (
    MfaEncryptionNotConfiguredError,
    MfaSecretDecryptionError,
    burn_password_verification,
    create_access_token,
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    generate_opaque_token,
    generate_totp_secret,
    get_totp_provisioning_uri,
    hash_password,
    hash_token,
    mfa_secret_needs_reencryption,
    password_needs_rehash,
    validate_password_strength,
    verify_access_token,
    verify_password,
    verify_totp_code,
)
from app.core.tenancy import cross_tenant, tenant_or_platform
from app.models.user import User, UserStatus
from app.services.auth_throttle import Admission, AuthThrottle, Bucket, BucketKind, bucket
from app.utils.validators import normalize_email

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any

    from app.core.audit import AuditSink
    from app.database.unit_of_work import UnitOfWork
    from app.models.auth_throttle import TrustedDevice
    from app.repositories.auth_throttle_repository import TrustedDeviceRepository
    from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
    from app.repositories.refresh_token_repository import RefreshTokenRepository
    from app.repositories.user_repository import UserRepository

logger = structlog.get_logger(__name__)

#: Scope for a user acting on their own account. The id is the authenticated
#: principal's own (or comes from a token only that user holds), so the lookup
#: is by identity rather than by hospital.
_OWN_ACCOUNT = cross_tenant("own account: id of the authenticated principal")

#: Log event for each reason an account is refused at login. The event names
#: predate the shared check and are kept, since dashboards may key on them.
_REJECTION_LOG_EVENTS = {
    "suspended": "login_attempt_suspended_account",
    "hospital_inactive": "login_attempt_inactive_hospital",
    "not_active": "login_attempt_invited_account",
}

#: The one answer for every refusal at the MFA step, whatever the cause. The
#: Hospital frontend recognises this text for its "wrong code" message.
_MFA_REJECTED = "Invalid MFA code."


#: Random extra added to the uniform minimum duration, as a fraction of it.
_UNIFORM_DURATION_JITTER = 0.2


async def _sleep(seconds: float) -> None:
    """Wait. A module-level seam so tests can observe the wait without sleeping."""
    await asyncio.sleep(seconds)


#: Password hashing runs here, not on the event loop. Argon2 is deliberately
#: slow; done inline, every login stalled every other request the worker was
#: serving, and a burst of them stretched responses unevenly enough to be
#: measured from outside. The pool is small on purpose: it is also the limit
#: on how much CPU and memory sign-in attempts can take at once.
_HASH_POOL: Final = ThreadPoolExecutor(max_workers=4, thread_name_prefix="password-hash")


async def _off_loop[T](function: Callable[..., T], *args: object) -> T:
    """Run a password-hashing call in the hashing pool and wait for it."""
    return await asyncio.get_running_loop().run_in_executor(_HASH_POOL, function, *args)


#: How long a browser stays a trusted device without completing a sign-in, and
#: the longest it can stay one at all.
_DEVICE_IDLE: Final = timedelta(days=90)
_DEVICE_LIFETIME: Final = timedelta(days=180)
#: Trusted devices kept per account, and device tokens kept in one cookie.
_DEVICES_PER_ACCOUNT: Final = 10
DEVICE_TOKENS_PER_COOKIE: Final = 8

#: How long a sign-in may take between the password and the code, and the
#: ticket claim that ties it to the password it was issued for.
_MFA_TICKET_LIFETIME: Final = timedelta(minutes=5)
_TICKET_PASSWORD_CLAIM: Final = "pwb"

#: Password-reset emails sent to one account within one token lifetime. When
#: the limit is reached, a link sent earlier is by definition still valid, so
#: the owner's mailbox is never left without a working one.
_RESET_EMAILS_PER_TOKEN_LIFETIME: Final = 3


#: The one message for every MFA encryption failure. It says nothing about the
#: key or the stored value.
_MFA_UNAVAILABLE = "Multi-factor authentication is temporarily unavailable."


class AuthService:
    """Handles all authentication-related business logic.

    :param user_repo: Repository for user data access.
    :param refresh_token_repo: Repository for refresh token data access.
    :param password_reset_repo: Repository for password reset token data access.
    :param audit: Where mutating operations are recorded (CLAUDE.md rule 9).
    :param throttle: Decides whether a credential may be evaluated at all.
        Required: there is no unthrottled way to build this service.
    :param trusted_devices: Browsers that have completed a sign-in.
    :param notifier: Where the password-reset email is requested. Optional; a
        service built without one sends nothing.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        refresh_token_repo: RefreshTokenRepository,
        password_reset_repo: PasswordResetTokenRepository,
        uow: UnitOfWork,
        audit: AuditSink,
        *,
        throttle: AuthThrottle,
        trusted_devices: TrustedDeviceRepository,
        notifier: Notifier | None = None,
    ) -> None:
        self._user_repo = user_repo
        self._refresh_token_repo = refresh_token_repo
        self._password_reset_repo = password_reset_repo
        self._uow = uow
        self._audit = audit
        self._throttle = throttle
        self._trusted_devices = trusted_devices
        self._notifier: Notifier = notifier or NullNotifier()

    # ── Login ────────────────────────────────────────────────────────────────

    async def login(
        self,
        email: str,
        password: str,
        device_info: str | None = None,
        ip_address: str | None = None,
        device_tokens: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Authenticate a user with email and password.

        :param email: The user's email address.
        :param password: The user's plaintext password.
        :param device_info: Optional device/user-agent string.
        :param ip_address: The caller's address, from :func:`app.core.client_ip.client_ip`.
        :param device_tokens: Trusted-device tokens the browser presented.
        :returns: A dict with ``access_token``, ``refresh_token``, ``expires_in``,
            ``user`` and ``device_cookie``, or ``mfa_ticket`` if MFA is required.
        :raises AuthenticationError: For every refusal, with one message.
        """
        started = time.monotonic()
        try:
            return await self._login(email, password, device_info, ip_address, device_tokens)
        except AuthenticationError:
            await self._wait_out_uniform_duration(started)
            raise

    async def _login(
        self,
        email: str,
        password: str,
        device_info: str | None,
        ip_address: str | None,
        device_tokens: Sequence[str],
    ) -> dict[str, Any]:
        """Do the work of :meth:`login`. Every refusal is an ``AuthenticationError``."""
        # Step 1: Who is this, and from where? Plain reads, no locks. A real
        # address and an unknown one go through the same statements.
        normalized = normalize_email(email)
        source = self._usable_source(ip_address)
        user = await self._find_user_by_email(email)
        device = await self._recognise(user, device_tokens)

        # Step 2: May this attempt be evaluated at all? It is charged to its
        # buckets now, before the password is looked at, and the charge is
        # given back only if the password is right. The buckets are keyed on
        # the address as typed, never on whether an account exists.
        admission = await self._throttle.admit(self._password_buckets(normalized, source, device))
        if not admission.admitted:
            logger.info(
                "login_attempt_throttled",
                email_hash=self._email_discriminator(email),
                bucket=str(admission.refused_by),
            )
            raise AuthenticationError("Invalid credentials.")

        if user is None:
            # No account, a soft-deleted one, or an ambiguous identity — all
            # the same to the caller. Do the work of a password check anyway,
            # so this answer takes as long as a wrong password does.
            await _off_loop(burn_password_verification, password)
            # Generic error — don't reveal whether the email exists, and never
            # log the raw address (PII, CLAUDE.md security rule 10 — B3).
            logger.info(
                "login_attempt_nonexistent_email",
                email_hash=self._email_discriminator(email),
            )
            raise AuthenticationError("Invalid credentials.")

        # Step 3: Check account status
        await self._check_account_status(user, attempted_password=password)

        # Step 4: Verify password
        password_hash = user.password_hash
        if not await _off_loop(verify_password, password, password_hash):
            await self._record_failed_attempt(user, "invalid_password", admission)
            raise AuthenticationError("Invalid credentials.")

        # Step 5: The password is right. Give this attempt's charges back.
        # With MFA still to come nothing else is cleared: the origin's backoff
        # goes only once the second factor has been verified (`verify_mfa`).
        if user.mfa_enabled:
            await self._throttle.settle(admission)
            # Under lock, confirm the password just verified is still the
            # account's password, so the ticket is tied to exactly that one.
            user = await self._reread_for_issue(user, password_hash=password_hash)
            if password_needs_rehash(user.password_hash):
                user = await self._user_repo.update(
                    user, password_hash=await _off_loop(hash_password, password)
                )
                logger.info("password_rehashed", user_id=str(user.id))
            await self._user_repo.record_login(user)
            mfa_ticket = self._issue_mfa_ticket(user)
            logger.info("mfa_required", user_id=str(user.id))
            await self._uow.commit()
            return {
                "mfa_ticket": mfa_ticket,
                "expires_in": 300,  # 5 minutes
            }

        await self._throttle.settle(admission, clear=self._own_backoff(device))

        # Step 6: Take the account row under lock and look again. The password
        # was checked against a copy read before the wait above; if it has
        # been changed or the account suspended since, this is not a login.
        # Only a caller who has already proved the password gets this far, so
        # the lock cannot be used to hold anyone else out.
        user = await self._reread_for_issue(user, password_hash=password_hash)
        if user.mfa_enabled:
            # The second factor was switched on while this sign-in was in
            # flight. A password alone no longer signs this account in.
            await self._uow.commit()
            logger.info("login_refused_account_changed", user_id=str(user.id))
            raise AuthenticationError("Invalid credentials.")

        # Step 7: Rehash password if needed (scheme migration)
        if password_needs_rehash(user.password_hash):
            user = await self._user_repo.update(
                user, password_hash=await _off_loop(hash_password, password)
            )
            logger.info("password_rehashed", user_id=str(user.id))

        await self._user_repo.record_login(user)

        # Step 8: Issue tokens
        result = await self._issue_tokens(user, device_info, ip_address)
        await self._audit.record(
            AuditEvent(
                action="auth.login.success",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
            )
        )
        # _issue_tokens already committed, so the audit row needs its own
        # commit — otherwise the session closes with it still pending and the
        # durable trail silently misses every successful login.
        await self._uow.commit()
        result["device_cookie"] = await self._trust_device(user, device_tokens, device)
        return result

    async def verify_mfa(
        self,
        mfa_ticket: str,
        code: str,
        device_info: str | None = None,
        ip_address: str | None = None,
        device_tokens: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Complete MFA verification after login.

        :param mfa_ticket: The MFA ticket from the login response.
        :param code: The 6-digit TOTP code.
        :param device_info: Optional device/user-agent string.
        :param ip_address: The caller's address, from :func:`app.core.client_ip.client_ip`.
        :param device_tokens: Trusted-device tokens the browser presented.
        :returns: Token response dict on success.
        :raises AuthenticationError: If the ticket or code is invalid.
        """
        started = time.monotonic()
        try:
            return await self._verify_mfa(mfa_ticket, code, device_info, ip_address, device_tokens)
        except AuthenticationError:
            await self._wait_out_uniform_duration(started)
            raise

    async def _verify_mfa(
        self,
        mfa_ticket: str,
        code: str,
        device_info: str | None,
        ip_address: str | None,
        device_tokens: Sequence[str],
    ) -> dict[str, Any]:
        """Do the work of :meth:`verify_mfa`. Every refusal is an ``AuthenticationError``."""
        try:
            payload = verify_access_token(mfa_ticket)
        except Exception:
            raise AuthenticationError("Invalid or expired MFA ticket.")  # noqa: B904

        if payload.get("type") != "mfa_ticket":
            raise AuthenticationError("Invalid MFA ticket.")

        try:
            user_id = uuid.UUID(str(payload["sub"]))
        except (KeyError, ValueError):
            raise AuthenticationError("Invalid MFA ticket.") from None

        # The ticket proves the password was right up to five minutes ago. It
        # proves nothing about the account now, so the account is read fresh.
        user = await self._user_repo.get_by_id(
            user_id, cross_tenant("subject of a signed MFA ticket")
        )

        # From here every refusal is the same 401 with the same message — a
        # wrong code, too many codes, an account that has been deleted or
        # suspended since the password step, a deactivated hospital, MFA
        # switched off, a password changed since. Whoever holds the ticket
        # learns only that it did not work.
        if user is None:
            raise AuthenticationError(_MFA_REJECTED)

        ticket_password_hash = user.password_hash
        if not hmac.compare_digest(
            str(payload.get(_TICKET_PASSWORD_CLAIM, "")), self._password_binding(user)
        ):
            # The password this ticket vouches for is no longer the password
            # — changed, reset, or reset by an administrator since. (Or the
            # ticket carries no binding at all.) Sign in again.
            logger.info("mfa_ticket_not_for_current_password", user_id=str(user.id))
            raise AuthenticationError(_MFA_REJECTED)

        if not user.mfa_enabled or not user.mfa_secret:
            # A ticket is only ever issued for an account with MFA on. If it
            # is off now, the ticket is stale: sign in again. Nothing is
            # charged: there is no code to guess.
            raise AuthenticationError(_MFA_REJECTED)

        # A code is charged to the origin's own backoff and to a budget. For
        # a device that has itself passed this account's second factor before,
        # the budget is that device's own; for everything else it is one
        # budget shared by the whole account.
        device = await self._recognise(user, device_tokens)
        origin = self._mfa_origin(user, ip_address, device)
        admission = await self._throttle.admit([origin, self._mfa_budget(user, device)])
        if not admission.admitted:
            logger.info(
                "mfa_attempt_throttled", user_id=str(user.id), bucket=str(admission.refused_by)
            )
            raise AuthenticationError(_MFA_REJECTED)

        await self._check_account_status(user, message=_MFA_REJECTED)

        stored_secret = user.mfa_secret
        try:
            secret = self._decrypt_mfa_secret(user)
        except ServiceUnavailableError:
            # No code was judged, so none is counted.
            await self._throttle.settle(admission)
            raise

        if not verify_totp_code(secret, code):
            logger.info("mfa_verification_failed", user_id=str(user_id))
            await self._record_failed_attempt(user, "invalid_mfa_code", admission)
            raise AuthenticationError(_MFA_REJECTED)

        # Both factors passed. On a trusted device, its own backoffs are
        # cleared, for the code step and the password step. From anywhere
        # else only this attempt's charge is given back: the source may be
        # shared, and clearing its backoff would hand whoever else is
        # guessing from there a fresh allowance.
        own = [origin, *self._own_backoff(device)] if device is not None else []
        await self._throttle.settle(admission, clear=own)

        user = await self._reread_for_issue(
            user,
            password_hash=ticket_password_hash,
            mfa_secret=stored_secret,
            message=_MFA_REJECTED,
        )

        # A secret still encrypted under a retired key is moved to the current
        # one now that the code has proved it is the right secret — the same
        # idea as rehashing a password on login. `_issue_tokens` commits it.
        if mfa_secret_needs_reencryption(stored_secret):
            await self._user_repo.update(user, mfa_secret=self._encrypt_mfa_secret(secret))
            logger.info("mfa_secret_reencrypted", user_id=str(user.id))

        # Record login
        await self._user_repo.record_login(user)

        # Issue tokens
        result = await self._issue_tokens(user, device_info, ip_address)
        await self._audit.record(
            AuditEvent(
                action="auth.login.success",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
            )
        )
        # See login(): the audit row trails an already-committed token issue.
        await self._uow.commit()
        result["device_cookie"] = await self._trust_device(
            user, device_tokens, device, mfa_verified=True
        )
        return result

    # ── Token Management ─────────────────────────────────────────────────────

    async def refresh_token(
        self,
        raw_token: str,
        device_info: str | None = None,
        ip_address: str | None = None,
        device_tokens: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Refresh an access token using a refresh token (rotation pattern).

        Implements refresh token rotation with reuse detection.
        See ``docs/07-SECURITY.md`` §2.3.

        :param raw_token: The opaque refresh token string.
        :param device_info: Optional device/user-agent string.
        :param ip_address: Optional IP address of the client.
        :param device_tokens: Trusted-device tokens the browser presented.
        :returns: Dict with new ``access_token``, ``refresh_token`` and
            ``device_cookie``.
        :raises AuthenticationError: If the token is invalid, expired, or reused.
        """
        token_hash = hash_token(raw_token)
        stored_token = await self._refresh_token_repo.get_by_token_hash(token_hash)

        if stored_token is None:
            raise AuthenticationError("Invalid refresh token.")

        # ── Reuse Detection ────────────────────────────────────────────
        # If the token has already been revoked, this is a potential theft.
        # Invalidate ALL sessions for this user.
        if stored_token.is_revoked:
            logger.warning(
                "refresh_token_reuse_detected",
                token_id=str(stored_token.id),
                user_id=str(stored_token.user_id),
            )
            # Reuse detection needs the hospital for the audit trail. The user
            # row is fetched here because the event carries tenant context that
            # the token row alone does not.
            owner = await self._user_repo.get_by_id(
                stored_token.user_id, cross_tenant("owner of a presented refresh token")
            )
            if owner is not None:
                await self._audit.record(
                    AuditEvent(
                        action="auth.token.reuse_detected",
                        hospital_id=owner.hospital_id,
                        target_type="user",
                        target_id=owner.id,
                        actor_id=None,
                    )
                )
            await self._refresh_token_repo.revoke_all_for_user(stored_token.user_id)
            # Commit before raising. The error below ends the request, and the
            # request-scoped session rolls back whatever is uncommitted when it
            # closes — which used to undo both the revocation and its audit
            # row, leaving every stolen session alive and no trace of the reuse.
            await self._uow.commit()
            raise AuthenticationError("Refresh token has been revoked. All sessions invalidated.")

        # ── Expiry Check ───────────────────────────────────────────────
        if stored_token.is_expired:
            raise AuthenticationError("Refresh token has expired.")

        # ── Fetch User ─────────────────────────────────────────────────
        user = await self._user_repo.get_by_id(
            stored_token.user_id, cross_tenant("owner of a presented refresh token")
        )
        if user is None or user.status != UserStatus.ACTIVE or not user.hospital_is_active:
            raise AuthenticationError("User account is not active.")

        # ── Rotate ─────────────────────────────────────────────────────
        # Generate the new opaque token BEFORE creating the record (avoids
        # a unique-constraint violation window on token_hash).
        raw_new_token, new_token_hash = generate_opaque_token()

        # Create the new refresh token record with the real hash
        new_refresh = await self._refresh_token_repo.create(
            user_id=user.id,
            token_hash=new_token_hash,
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.JWT_REFRESH_TTL_SECONDS),
            device_info=device_info,
            ip_address=ip_address,
        )

        # Revoke old token, linking to the new one
        await self._refresh_token_repo.revoke(stored_token, rotated_by_id=new_refresh.id)

        # Issue new access token
        permissions = await self._get_user_permission_codes(user)
        access_token = create_access_token(
            user_id=user.id,
            hospital_id=user.hospital_id,
            roles=[r.role.name for r in user.user_roles] if user.user_roles else None,
            permissions=permissions,
        )

        logger.info("token_refreshed", user_id=str(user.id), token_id=str(stored_token.id))

        await self._uow.commit()

        # A refresh never makes a browser a trusted device. A session is not a
        # credential check: if it did, whoever held one stolen session could
        # mint device after device, each with password attempts of its own.
        return {
            "access_token": access_token,
            "refresh_token": raw_new_token,
            "expires_in": settings.JWT_ACCESS_TTL_SECONDS,
            "device_cookie": None,
        }

    async def logout(self, raw_token: str) -> None:
        """Logout by revoking the specific refresh token.

        :param raw_token: The opaque refresh token to revoke.
        """
        token_hash = hash_token(raw_token)
        stored_token = await self._refresh_token_repo.get_by_token_hash(token_hash)

        if stored_token is not None and not stored_token.is_revoked:
            await self._refresh_token_repo.revoke(stored_token)
            await self._audit.record(
                AuditEvent(
                    action="auth.logout",
                    hospital_id=stored_token.user.hospital_id,
                    target_type="user",
                    target_id=stored_token.user_id,
                    actor_id=stored_token.user_id,
                )
            )
            logger.info(
                "token_revoked", token_id=str(stored_token.id), user_id=str(stored_token.user_id)
            )

        await self._uow.commit()

    async def logout_all(self, user_id: uuid.UUID, *, forget_devices: bool = True) -> int:
        """Revoke ALL refresh tokens for a user, and forget every trusted device.

        "Sign out everywhere" — also what suspending or reactivating an
        account does — leaves no browser recognised.

        :param user_id: The user's UUID.
        :param forget_devices: ``False`` when sessions are ended only so that
            new ones pick up changed roles: nothing about the account's
            credentials is in doubt, so its browsers stay recognised.
        :returns: The number of revoked tokens.
        """
        count = await self._refresh_token_repo.revoke_all_for_user(user_id)
        if forget_devices:
            await self._trusted_devices.delete_for_user(user_id)
        logger.info("all_tokens_revoked", user_id=str(user_id), count=count)
        await self._uow.commit()
        return count

    # ── Password Management ────────────────────────────────────────────────

    async def forgot_password(self, email: str) -> None:
        """Request a password reset token.

        Always returns success to prevent email enumeration.

        :param email: The email address to send a reset link to.
        """
        started = time.monotonic()
        await self._forgot_password(email)
        # Always, not only when nothing was sent: the answer is the same 200
        # either way, so its timing has to be the same too.
        await self._wait_out_uniform_duration(started)

    async def _forgot_password(self, email: str) -> None:
        """Do the work of :meth:`forgot_password`."""
        # One request per address at a time: a burst for a real account, each
        # of which would write a token and queue an email, must do no more
        # work — and so answer no slower — than a burst for an unknown one.
        if await self._user_repo.claim_authentication_attempt(email) is not True:
            logger.info(
                "password_reset_requested_concurrent",
                email_hash=self._email_discriminator(email),
            )
            return

        # Don't reveal whether the email exists — always return success.
        user = await self._find_user_by_email(email)
        if user is None:
            # Never log the raw address (PII — B3); a stable hash prefix is
            # enough to correlate support cases without storing the email.
            logger.info(
                "password_reset_requested_nonexistent_email",
                email_hash=self._email_discriminator(email),
            )
            return

        # A reset token is a credential, and is issued only to an account
        # that already has a password to reset: active, in an active hospital.
        # An invited account gets its link from an invitation, which an
        # administrator sends and can decline to send again — not from an
        # anonymous request that would keep a lapsed invitation alive for
        # ever. Everyone else gets nothing, and the same success response as
        # everyone else. (A soft-deleted account is not found at all, above.)
        if not self._may_request_reset(user):
            logger.info("password_reset_requested_unusable_account", user_id=str(user.id))
            return

        # The link can only travel by email. With no mail transport there is
        # nowhere to send one, so none is minted.
        if not email_delivery_configured():
            logger.warning("password_reset_not_sent", reason="email_not_configured")
            return

        # A few emails per account per token lifetime, and no more: this
        # endpoint is anonymous, and without a limit it is a way to bury
        # someone's mailbox and fill the token table. When the limit is
        # reached a link sent earlier is still valid, so the owner is never
        # left without one — the limit cannot be used to deny them a reset.
        lifetime = timedelta(minutes=settings.PASSWORD_RESET_TOKEN_TTL_MINUTES)
        now = datetime.now(UTC)
        recent = await self._password_reset_repo.count_issued_since(user.id, now - lifetime)
        if recent >= _RESET_EMAILS_PER_TOKEN_LIFETIME:
            logger.info("password_reset_requested_too_often", user_id=str(user.id))
            return

        # Generate token
        raw_token, token_hash = generate_opaque_token()
        expires_at = now + lifetime

        reset_token = await self._password_reset_repo.create(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )

        await self._audit.record(
            AuditEvent(
                action="auth.password.reset_requested",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
            )
        )
        # Until this ran, the token above was created and then never delivered
        # to anyone. The raw token goes only into the email; the in-app notice
        # just says that a reset was requested.
        # The notifier is contracted not to raise. If one ever does, that must
        # look exactly like "not queued": an error here would happen only for
        # a real account, and would skip the uniform wait.
        try:
            queued = await self._notifier.deliver_credential(
                NotificationRequest(
                    kind="auth.password_reset_requested",
                    hospital_id=user.hospital_id,
                    recipient_user_ids=(user.id,),
                    variables={
                        "expires_in": f"{settings.PASSWORD_RESET_TOKEN_TTL_MINUTES} minutes",
                    },
                    secret_variables={
                        "action_url": (
                            f"{settings.FRONTEND_BASE_URL.rstrip('/')}/reset-password#token={raw_token}"
                        ),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 — see above; nothing from the request is logged
            queued = False
            logger.error("password_reset_email_failed", error_type=type(exc).__name__)
        if queued is not True:
            # No email carries it, so nobody may hold it: the link is dead
            # before the request ends.
            await self._password_reset_repo.mark_as_used(reset_token)
        await self._uow.commit()
        logger.info("password_reset_token_created", user_id=str(user.id), emailed=queued is True)

    async def reset_password(
        self, raw_token: str, new_password: str, device_tokens: Sequence[str] = ()
    ) -> str | None:
        """Complete a password reset, or activate an invitation, using an emailed token.

        :param raw_token: The token from the emailed link.
        :param new_password: The new password.
        :param device_tokens: Trusted-device tokens the browser presented.
        :returns: The trusted-device cookie value to give the browser, if any.
            Completing an emailed link proves control of the mailbox, so the
            browser that does it becomes a trusted device of the account.
        :raises AuthenticationError: If the token is invalid or expired.
        :raises BusinessRuleError: If the password is too weak.
        """
        # Validate password strength
        password_errors = validate_password_strength(new_password)
        if password_errors:
            raise BusinessRuleError(
                "Password does not meet requirements.", detail={"password": password_errors}
            )

        # Whose token is this? A plain read: nothing is decided by it.
        token_hash = hash_token(raw_token)
        candidate = await self._password_reset_repo.get_valid_token(token_hash)
        if candidate is None:
            raise AuthenticationError("Invalid or expired password reset token.")

        # The account first, under lock — the same order in which suspending
        # an account or sending an invitation again takes these rows — so
        # that such a change is seen either wholly before or wholly after,
        # and the two can never wait on each other.
        user = await self._user_repo.lock_for_authentication(candidate.user_id)

        # Then spend the token. One statement checks it is still unused and
        # unexpired and marks it used — so of two requests presenting the
        # same token at the same moment, exactly one gets past this line.
        token = await self._password_reset_repo.consume(token_hash)
        if token is None:
            await self._uow.commit()
            raise AuthenticationError("Invalid or expired password reset token.")

        # A token for an account that has since been deleted, suspended, or
        # whose hospital was deactivated is refused exactly like a bad token:
        # the answer must not say that the account exists or what state it is
        # in. The token stays spent: a link refused once does not come back
        # to life if the account is later reinstated.
        if user is None or not self._may_redeem_reset(user):
            await self._uow.commit()
            raise AuthenticationError("Invalid or expired password reset token.")

        # Update password. An invitation token also flips the account from
        # INVITED to ACTIVE.
        now = datetime.now(UTC)
        new_hash = await _off_loop(hash_password, new_password)
        updates: dict[str, Any] = {
            "password_hash": new_hash,
            "password_changed_at": now,
        }
        if user.status == UserStatus.INVITED:
            updates["status"] = UserStatus.ACTIVE
        await self._user_repo.update(user, **updates)

        # Invalidate all outstanding reset tokens for this user
        await self._password_reset_repo.invalidate_all_for_user(user.id)

        # Revoke all refresh tokens (force re-login)
        await self._refresh_token_repo.revoke_all_for_user(user.id)

        await self._audit.record(
            AuditEvent(
                action="auth.password.reset",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
            )
        )
        await self._uow.commit()
        logger.info("password_reset_completed", user_id=str(user.id))
        return await self._retrust_device(user, device_tokens)

    async def change_password(
        self,
        user_id: uuid.UUID,
        current_password: str,
        new_password: str,
        device_tokens: Sequence[str] = (),
    ) -> str | None:
        """Change password for an authenticated user.

        :param user_id: The user's UUID.
        :param current_password: The current password for verification.
        :param new_password: The new password.
        :param device_tokens: Trusted-device tokens the browser presented.
        :returns: The trusted-device cookie value to give the browser, if any.
        :raises AuthenticationError: If the current password is wrong.
        :raises BusinessRuleError: If the password is too weak.
        """
        user = await self._user_repo.get_by_id(user_id, _OWN_ACCOUNT)
        if user is None:
            raise AuthenticationError("User not found.")

        await self._check_session_password(
            user, current_password, message="Current password is incorrect."
        )

        password_errors = validate_password_strength(new_password)
        if password_errors:
            raise BusinessRuleError(
                "Password does not meet requirements.", detail={"password": password_errors}
            )

        now = datetime.now(UTC)
        new_hash = await _off_loop(hash_password, new_password)
        await self._user_repo.update(
            user,
            password_hash=new_hash,
            password_changed_at=now,
        )

        # Revoke all refresh tokens except the current session
        # (We don't have a "current session" concept, so we revoke all for now.)
        await self._refresh_token_repo.revoke_all_for_user(user.id)

        await self._audit.record(
            AuditEvent(
                action="auth.password.changed",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
            )
        )
        await self._uow.commit()
        logger.info("password_changed", user_id=str(user.id))
        return await self._retrust_device(user, device_tokens)

    async def admin_reset_password(
        self,
        user_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        actor_hospital_id: uuid.UUID | None = None,
    ) -> None:
        """Admin-initiated password reset (sets password_change_required).

        :param user_id: The target user's UUID.
        :param actor_id: UUID of the admin performing the reset, for the audit trail.
        :param actor_hospital_id: The acting admin's hospital. A target user
            from another hospital is treated as not found (B1 — tenant
            isolation on every admin write path).
        :raises NotFoundError: If the user is not found or belongs to another
            hospital. 404 — not 401 — so a cross-tenant UUID is
            indistinguishable from one that does not exist.
        """
        user = await self._user_repo.get_by_id(
            user_id,
            tenant_or_platform(
                actor_hospital_id, reason="password reset by a platform-level administrator"
            ),
        )
        if user is None or (
            actor_hospital_id is not None and user.hospital_id != actor_hospital_id
        ):
            raise NotFoundError("User not found.")

        # Set a random password to invalidate the current one, and mark as change required
        await self._user_repo.update(
            user,
            password_hash=await _off_loop(hash_password, secrets.token_urlsafe(32)),
            password_changed_at=None,
        )

        # Revoke all sessions, and stop recognising any browser: an account an
        # administrator has to reset is one whose past sign-ins are in doubt.
        await self._refresh_token_repo.revoke_all_for_user(user.id)
        await self._trusted_devices.delete_for_user(user.id)

        await self._audit.record(
            AuditEvent(
                action="auth.password.admin_reset",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=actor_id,
            )
        )
        await self._uow.commit()
        logger.info(
            "admin_password_reset",
            user_id=str(user_id),
            actor_id=str(actor_id) if actor_id else None,
        )

    # ── MFA ─────────────────────────────────────────────────────────────────

    async def enroll_mfa(self, user_id: uuid.UUID, password: str) -> dict[str, Any]:
        """Initiate MFA enrollment.

        :param user_id: The user's UUID.
        :param password: Current password for verification.
        :returns: Dict with ``secret``, ``provisioning_uri``. This response is
            the one time the plaintext secret leaves the server: the user has
            to enter it into an authenticator. It is stored only encrypted.
        :raises AuthenticationError: If verification fails.
        :raises ServiceUnavailableError: If no encryption key is configured.
            Nothing is stored in that case — never the plaintext.
        :raises BusinessRuleError: If MFA is already enabled — re-enrolment
            would overwrite the working secret stored on the account before the
            user confirms the new one, and from then on no TOTP code the user
            enters would verify (lockout recoverable only from the database).
            Disabling MFA first is the supported path back to enrolment.
        """
        user = await self._user_repo.get_by_id(user_id, _OWN_ACCOUNT)
        if user is None:
            raise AuthenticationError("User not found.")

        await self._check_session_password(user, password, message="Invalid password.")

        if user.mfa_enabled:
            raise BusinessRuleError(
                "MFA is already enabled for this account. Disable it first if you want to re-enroll."
            )

        secret = generate_totp_secret()
        provisioning_uri = get_totp_provisioning_uri(secret, user.email)

        # Store secret temporarily — user must confirm with a valid TOTP code
        # before we enable MFA. Encrypted even while pending.
        await self._user_repo.update(user, mfa_secret=self._encrypt_mfa_secret(secret))

        await self._audit.record(
            AuditEvent(
                action="auth.mfa.enrollment_initiated",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
            )
        )
        # Persist the temporary secret and the audit row — without this the
        # request-scope session rolls both back when it closes.
        await self._uow.commit()
        logger.info("mfa_enrollment_initiated", user_id=str(user.id))

        return {
            "secret": secret,
            "provisioning_uri": provisioning_uri,
        }

    async def confirm_mfa(self, user_id: uuid.UUID, code: str) -> None:
        """Confirm MFA enrollment by verifying a TOTP code.

        The code is checked against the **pending secret the server stored at
        enrolment** — the only secret this method ever reads. It takes no
        secret from the caller: confirmation used to verify against, and then
        store, a secret sent in the request, which let anyone holding a session
        enable MFA with a secret of their choosing or replace an active one,
        without the password that enrolment demands.

        Nothing is written to ``mfa_secret`` here except, at most, the same
        secret re-encrypted under the current key.

        :param user_id: The user's UUID.
        :param code: The 6-digit TOTP code.
        :raises AuthenticationError: If the code does not match the pending secret.
        :raises BusinessRuleError: If MFA is already enabled — the active
            secret is never replaced through confirmation; disable MFA first —
            or if no enrolment is in progress.
        :raises ServiceUnavailableError: If no encryption key is configured, or
            the pending secret cannot be decrypted.
        """
        user = await self._user_repo.get_by_id(user_id, _OWN_ACCOUNT)
        if user is None:
            raise AuthenticationError("User not found.")

        if user.mfa_enabled:
            raise BusinessRuleError(
                "MFA is already enabled for this account. Disable it first if you want to re-enroll."
            )

        if not user.mfa_secret:
            raise BusinessRuleError("No MFA enrollment is in progress. Start enrollment first.")

        pending = self._decrypt_mfa_secret(user)

        await self._check_session_code(
            user, pending, code, message="Invalid MFA code. Please try again."
        )

        updates: dict[str, Any] = {"mfa_enabled": True}
        if mfa_secret_needs_reencryption(user.mfa_secret):
            updates["mfa_secret"] = self._encrypt_mfa_secret(pending)
        await self._user_repo.update(user, **updates)
        # A new second factor: no device has passed it yet.
        await self._trusted_devices.forget_mfa_for_user(user.id)

        await self._audit.record(
            AuditEvent(
                action="auth.mfa.enrolled",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
            )
        )
        await self._uow.commit()
        logger.info("mfa_enabled", user_id=str(user.id))

    async def disable_mfa(self, user_id: uuid.UUID, password: str, code: str) -> None:
        """Disable MFA for a user.

        :param user_id: The user's UUID.
        :param password: Current password for verification.
        :param code: The 6-digit TOTP code.
        :raises AuthenticationError: If verification fails.
        """
        user = await self._user_repo.get_by_id(user_id, _OWN_ACCOUNT)
        if user is None:
            raise AuthenticationError("User not found.")

        await self._check_session_password(user, password, message="Invalid password.")

        if user.mfa_secret:
            await self._check_session_code(
                user, self._decrypt_mfa_secret(user), code, message="Invalid MFA code."
            )

        await self._user_repo.update(
            user,
            mfa_secret=None,
            mfa_enabled=False,
        )
        await self._trusted_devices.forget_mfa_for_user(user.id)

        await self._audit.record(
            AuditEvent(
                action="auth.mfa.disabled",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
            )
        )
        await self._uow.commit()
        logger.info("mfa_disabled", user_id=str(user.id))

    # ── Internal Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _encrypt_mfa_secret(secret: str) -> str:
        """Encrypt a TOTP secret for storage, or refuse the operation.

        :param secret: The plaintext secret.
        :returns: The value to store in ``users.mfa_secret``.
        :raises ServiceUnavailableError: If no encryption key is configured.
            The plaintext is never stored as a fallback.
        """
        try:
            return encrypt_mfa_secret(secret)
        except MfaEncryptionNotConfiguredError:
            logger.error("mfa_encryption_not_configured")
            raise ServiceUnavailableError(_MFA_UNAVAILABLE) from None

    @staticmethod
    def _decrypt_mfa_secret(user: User) -> str:
        """Decrypt a user's stored TOTP secret, in memory, or refuse the operation.

        Both failures answer 503 rather than "invalid code": the code is not
        the problem, and telling the user to retry would only burn attempts.
        Neither lets the caller through — a secret that cannot be read cannot
        verify anything.

        :param user: A user whose ``mfa_secret`` is set.
        :returns: The plaintext secret. Never log or return it.
        :raises ServiceUnavailableError: If no key is configured, or the
            stored value does not decrypt under any configured key.
        """
        try:
            return decrypt_mfa_secret(user.mfa_secret or "")
        except MfaEncryptionNotConfiguredError:
            logger.error("mfa_encryption_not_configured")
            raise ServiceUnavailableError(_MFA_UNAVAILABLE) from None
        except MfaSecretDecryptionError:
            # The stored value is deliberately absent from this line.
            logger.error("mfa_secret_undecryptable", user_id=str(user.id))
            raise ServiceUnavailableError(_MFA_UNAVAILABLE) from None

    @staticmethod
    def _email_discriminator(email: str) -> str:
        """Return a non-reversible, PII-safe log discriminator for an email.

        The lowercased address is hashed with SHA-256 and only the first 16
        hex chars are kept — enough to correlate a support case, not enough
        to recover the address (CLAUDE.md security rule 10 — B3).

        :param email: The raw email address.
        :returns: A 16-char hex digest.
        """
        return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]

    async def _wait_out_uniform_duration(self, started: float) -> None:
        """Hold the response until the uniform minimum duration has passed.

        Rejections do different amounts of work — none for an unknown email,
        several writes for a real account — and the difference is measurable
        from outside. Padding every one of them to the same floor removes it.

        The transaction is ended first. By this point everything a rejection
        keeps (the audit entry, the failure count) has already been committed
        where it was written, so this writes nothing new; what it does is hand
        the database connection and the lock on the account row back before
        waiting. Otherwise a stream of failed attempts could pin the connection
        pool, or hold a victim's row locked while the attacker's request sleeps.

        :param started: ``time.monotonic()`` taken when the operation began.
        """
        await self._uow.commit()
        floor = settings.AUTH_FAILURE_MIN_SECONDS
        if floor <= 0:
            return
        # The floor hides the work done before it. What happens after it —
        # building and sending the response — still varied by well under a
        # millisecond between a real account and an unknown address when
        # measured. A random extra of up to a fifth of the floor, different
        # for every response, buries that: telling the two apart would take
        # thousands of attempts per address instead of a few dozen.
        jitter = floor * _UNIFORM_DURATION_JITTER * (secrets.randbelow(10_000) / 10_000)
        # If the work itself outran the floor — a loaded server — the answer
        # is held to the next whole multiple of it rather than released at
        # once. Otherwise "slower than the floor" would itself say that real
        # work was done, and a burst sent to make the server slow would turn
        # the floor off.
        elapsed = time.monotonic() - started
        steps = max(1, math.ceil(elapsed / floor))
        remaining = steps * floor + jitter - elapsed
        if remaining > 0:
            await _sleep(remaining)

    async def _find_user_by_email(self, email: str, *, for_update: bool = False) -> User | None:
        """Resolve the one live staff account an email names.

        The address is normalised here, so every caller — login and password
        reset — compares in the same canonical form. The repository returns
        ``None`` for an unknown address, a soft-deleted account, and an
        ambiguous identity alike.

        :param email: The email as submitted, in any case.
        :param for_update: Lock the row for the rest of the transaction (login).
        :returns: The user, or ``None``.
        """
        return await self._user_repo.get_by_email_cross_tenant(
            normalize_email(email), for_update=for_update
        )

    @staticmethod
    def _may_request_reset(user: User) -> bool:
        """Whether a password-reset link may be emailed to this account on request.

        Only an ``ACTIVE`` account in an active hospital. An invited account
        has no password to reset; its link comes from an invitation.

        :param user: A live (not soft-deleted) user.
        """
        return user.status == UserStatus.ACTIVE and user.hospital_is_active

    @staticmethod
    def _may_redeem_reset(user: User) -> bool:
        """Whether an emailed token may be redeemed by this account.

        An allowlist: ``ACTIVE``, or ``INVITED`` (redeeming the invitation is
        how an invited account is activated), in an active hospital. Anything
        else — suspended, or a state this code does not know — is refused.

        :param user: A live (not soft-deleted) user.
        """
        return user.status in (UserStatus.ACTIVE, UserStatus.INVITED) and user.hospital_is_active

    @staticmethod
    def _rejection_reason(user: User) -> str | None:
        """Why this account may not authenticate right now, or ``None``.

        The single definition of "usable account", shared by the password
        step, the MFA step and token issuance. An allowlist: the account must
        be ``ACTIVE`` and in an active hospital. Any other state, including
        one this code does not know, is refused.

        There is no "locked" state. Too many wrong attempts slow further
        attempts down (``app/services/auth_throttle.py``); they never change
        what the account is.

        :param user: The account being authenticated.
        :returns: A short reason for the audit trail, or ``None`` if usable.
        """
        if user.status == UserStatus.SUSPENDED:
            return "suspended"
        if user.status != UserStatus.ACTIVE:
            return "not_active"
        if not user.hospital_is_active:
            return "hospital_inactive"
        return None

    async def _check_account_status(
        self,
        user: User,
        *,
        attempted_password: str | None = None,
        message: str = "Invalid credentials.",
    ) -> None:
        """Refuse an account that may not authenticate.

        Every failure raises the same generic :class:`AuthenticationError` —
        never a distinct ``ACCOUNT_SUSPENDED``/``ACCOUNT_LOCKED`` — so an
        attempt cannot be used to enumerate valid email addresses or learn an
        account's state (docs/07-SECURITY.md rule 10). The real reason is
        recorded only in the audit event and the log.

        :param user: The user to check.
        :param attempted_password: The submitted password, at the password
            step. When given, a rejection first spends the time of a real
            password check, so it is not faster than a wrong password. The MFA
            step passes nothing: the password was already verified.
        :param message: The generic message for this step.
        :raises AuthenticationError: With the generic message, if the account
            is not active or belongs to an inactive hospital.
        """
        reason = self._rejection_reason(user)
        if reason is None:
            return

        if attempted_password is not None:
            await _off_loop(burn_password_verification, attempted_password)

        await self._audit.record(
            AuditEvent(
                action="auth.login.failed",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
                context={"reason": reason},
            )
        )
        # Persist before raising — the exception rolls the request back and
        # this failure must survive in the durable trail.
        await self._uow.commit()
        logger.info(_REJECTION_LOG_EVENTS[reason], user_id=str(user.id))
        raise AuthenticationError(message)

    async def _record_failed_attempt(self, user: User, reason: str, admission: Admission) -> None:
        """Write the audit entry for a wrong password or code, and commit it.

        The attempt was already charged to its buckets when it was admitted;
        nothing is counted here. If that charge started a wait, the entry says
        which bucket and for how long.

        Committed here rather than by the caller, which raises as soon as this
        returns: the entry must survive the failed request.

        :param user: The account the attempt was made on.
        :param reason: ``invalid_password`` or ``invalid_mfa_code``.
        :param admission: What the throttle charged for this attempt.
        """
        context: dict[str, Any] = {"reason": reason}
        wait = admission.wait_started
        if wait is not None:
            context["throttle"] = {"bucket": wait[0].value, "wait_seconds": wait[1]}
            logger.info(
                "auth_throttle_engaged",
                user_id=str(user.id),
                bucket=wait[0].value,
                wait_seconds=wait[1],
            )
        if admission.budgets_exhausted:
            # The record that an account's (or a source's) allowance ran out:
            # from here further attempts are refused without leaving a trace.
            exhausted = [kind.value for kind in admission.budgets_exhausted]
            context["budget_exhausted"] = exhausted
            logger.info("auth_budget_exhausted", user_id=str(user.id), buckets=exhausted)
        await self._audit.record(
            AuditEvent(
                action="auth.login.failed",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
                context=context,
            )
        )
        await self._uow.commit()

    # ── Throttle buckets ────────────────────────────────────────────────────

    @staticmethod
    def _password_buckets(
        normalized_email: str, source: str | None, device: TrustedDevice | None
    ) -> list[Bucket]:
        """The buckets a password attempt on an email is charged to.

        From a trusted device of the account: that device's own buckets, and
        nothing shared. So nothing an outsider does — from one address or ten
        thousand — can use up the allowance of a browser the owner has
        already signed in from.

        From anywhere else: the account's shared budget, and, when the source
        is known, the source's own budget and the backoff for this account
        from this source. With no usable source the source buckets are left
        out rather than shared between every such caller.

        :param normalized_email: The address as typed, normalised.
        :param source: Where the attempt came from, or ``None``.
        :param device: The trusted device it came from, or ``None``.
        """
        if device is not None:
            return [
                bucket(BucketKind.PW_DEVICE, device.id),
                bucket(BucketKind.PW_DEVICE_CAP, device.id),
            ]
        buckets = [bucket(BucketKind.PW_ACCOUNT, normalized_email)]
        if source is not None:
            buckets.append(bucket(BucketKind.PW_SOURCE, source))
            buckets.append(bucket(BucketKind.PW_PAIR, normalized_email, source))
        return buckets

    @staticmethod
    def _own_backoff(device: TrustedDevice | None) -> list[Bucket]:
        """The password backoff a completed sign-in clears: the trusted device's own.

        An attempt from anywhere else clears nothing. Its source may be shared
        — a whole hospital behind one address — so clearing that backoff
        would hand a fresh allowance to whoever else is guessing from there.
        The browser becomes a trusted device at this sign-in and starts on a
        clean bucket of its own.
        """
        return [bucket(BucketKind.PW_DEVICE, device.id)] if device is not None else []

    @staticmethod
    def _usable_source(ip_address: str | None) -> str | None:
        """The source to count an attempt under, or ``None`` if there is none.

        ``None`` — no address, or one that is not an address — means the
        source buckets are left out. They are never shared between every
        caller whose source is unknown.
        """
        source = source_of(ip_address)
        return None if source == UNKNOWN_SOURCE else source

    @staticmethod
    def _mfa_origin(user: User, ip_address: str | None, device: TrustedDevice | None) -> Bucket:
        """The backoff bucket for second-factor codes from one origin."""
        if device is not None:
            return bucket(BucketKind.MFA_ORIGIN, user.id, "device", device.id)
        return bucket(BucketKind.MFA_ORIGIN, user.id, "source", source_of(ip_address))

    @staticmethod
    def _mfa_budget(user: User, device: TrustedDevice | None) -> Bucket:
        """The budget a second-factor code is charged to.

        Reaching this step takes the account's password. If every code drew
        on one budget for the account, anyone who had stolen the password —
        and nothing else — could spend it and keep the owner out for as long
        as they cared to. So a device that has itself completed the second
        factor on this account has a budget of its own, which nobody without
        that device's token can touch. Everything else shares the account's:
        other sources, devices trusted only through an emailed link or a
        session, and every password reset an attacker might repeat to look
        like a new device.
        """
        if device is not None and device.mfa_verified_at is not None:
            return bucket(BucketKind.MFA_DEVICE_CAP, device.id)
        return bucket(BucketKind.MFA_ACCOUNT, user.id)

    @staticmethod
    def _password_binding(user: User) -> str:
        """A value that changes whenever the account's password does.

        A digest of the stored password hash — itself salted and one-way — so
        it reveals nothing about the password, and it is only ever handed to
        someone who has just proved they know that password.
        """
        return hashlib.sha256(
            b"aetheris:mfa-ticket:v1\0" + user.password_hash.encode("utf-8")
        ).hexdigest()[:32]

    def _issue_mfa_ticket(self, user: User) -> str:
        """Issue the ticket that carries a sign-in from the password step to the code step.

        The ticket says "the password was right a moment ago". It is tied to
        the password it vouches for: if that password is changed or reset
        before the code step — by the owner, through an emailed link, or by
        an administrator — the ticket stops working at that instant, not five
        minutes later.

        Built with the token helper's own ``extra_claims``, so it is signed
        and verified exactly as every other token is; only its type, lifetime
        and the binding differ.
        """
        return create_access_token(
            user_id=user.id,
            hospital_id=None,
            extra_claims={
                "type": "mfa_ticket",
                "purpose": "mfa_verification",
                "exp": datetime.now(UTC) + _MFA_TICKET_LIFETIME,
                _TICKET_PASSWORD_CLAIM: self._password_binding(user),
            },
        )

    async def _check_session_password(self, user: User, password: str, *, message: str) -> None:
        """Verify the password of a signed-in user, throttled.

        Changing a password, and switching MFA on or off, ask for the current
        password again. Holding a session must not turn those into an
        unlimited password oracle. Throttled and wrong get the same answer.

        :param user: The signed-in user.
        :param password: The password submitted.
        :param message: The refusal for this endpoint.
        :raises AuthenticationError: If throttled or wrong.
        """
        own = bucket(BucketKind.SESSION_PW, user.id)
        admission = await self._throttle.admit([own])
        if not admission.admitted:
            logger.info("session_password_check_throttled", user_id=str(user.id))
            raise AuthenticationError(message)
        if not await _off_loop(verify_password, password, user.password_hash):
            raise AuthenticationError(message)
        await self._throttle.settle(admission, clear=[own])

    async def _check_session_code(
        self, user: User, secret: str, code: str, *, message: str
    ) -> None:
        """Verify a TOTP code for a signed-in user, throttled.

        Charged to the account's second-factor budget as well, so guessing
        codes here and at sign-in draws on the same allowance.

        :param user: The signed-in user.
        :param secret: The decrypted TOTP secret to check against.
        :param code: The code submitted.
        :param message: The refusal for this endpoint.
        :raises AuthenticationError: If throttled or wrong.
        """
        own = bucket(BucketKind.SESSION_CODE, user.id)
        admission = await self._throttle.admit([own, bucket(BucketKind.MFA_ACCOUNT, user.id)])
        if not admission.admitted:
            logger.info("session_code_check_throttled", user_id=str(user.id))
            raise AuthenticationError(message)
        if not verify_totp_code(secret, code):
            raise AuthenticationError(message)
        await self._throttle.settle(admission, clear=[own])

    async def admit_invitation_resend(self, user_id: uuid.UUID) -> bool:
        """Whether another invitation email may be sent for this account now.

        :param user_id: The invited user's UUID.
        :returns: ``False`` when too many have been sent recently.
        """
        admission = await self._throttle.admit([bucket(BucketKind.INVITE_RESEND, user_id)])
        return admission.admitted

    # ── Trusted devices ─────────────────────────────────────────────────────

    async def _recognise(
        self, user: User | None, device_tokens: Sequence[str]
    ) -> TrustedDevice | None:
        """Find the trusted device of ``user`` among the tokens a browser presented.

        Recognition is per account: a token counts only if this user has a
        live row for it. A token trusted for somebody else, or one the server
        never issued, is nothing. With no account the same query runs against
        an id that matches no one, so an unknown address costs what a real
        one does.

        :param user: The account being signed in to, or ``None``.
        :param device_tokens: Tokens from the browser's cookie.
        :returns: The trusted-device row, or ``None``.
        """
        if not device_tokens:
            return None
        return await self._trusted_devices.find(
            user.id if user is not None else uuid.uuid4(),
            [hash_token(token) for token in device_tokens],
            now=datetime.now(UTC),
        )

    async def _trust_device(
        self,
        user: User,
        device_tokens: Sequence[str],
        device: TrustedDevice | None,
        *,
        mfa_verified: bool = False,
    ) -> str | None:
        """Make the browser that just completed a sign-in a trusted device.

        Called only after full authentication, a completed emailed link, or a
        password change by a signed-in user. If the browser is already a
        trusted device of this account its row is refreshed and its token
        kept. Otherwise a new random token is issued for it. A value the
        client supplied is never adopted, so a token planted in someone's
        browser gains nothing when they sign in.

        Failing to record trust never fails the sign-in: the browser is simply
        not recognised next time.

        :param user: The account that was signed in to.
        :param device_tokens: Tokens from the browser's cookie.
        :param device: The row :meth:`_recognise` found, if any.
        :param mfa_verified: Whether the sign-in just completed passed the
            account's second factor on this browser.
        :returns: The cookie value to give the browser, or ``None`` on failure.
        """
        # Read now: the rollback below expires `user`, and touching an expired
        # attribute afterwards is a database read that fails the request.
        user_id = user.id
        try:
            now = datetime.now(UTC)
            minted: list[str] = []
            if device is not None:
                expires_at = min(now + _DEVICE_IDLE, device.created_at + _DEVICE_LIFETIME)
                await self._trusted_devices.touch(
                    device, now=now, expires_at=expires_at, mfa_verified=mfa_verified
                )
            else:
                raw_token = secrets.token_urlsafe(32)
                # Room is made first, so the browser being trusted now is
                # never the one that gets dropped.
                await self._trusted_devices.prune(user_id, keep=_DEVICES_PER_ACCOUNT - 1, now=now)
                await self._trusted_devices.create(
                    user_id,
                    hash_token(raw_token),
                    now=now,
                    expires_at=now + _DEVICE_IDLE,
                    mfa_verified=mfa_verified,
                )
                minted.append(raw_token)
                await self._audit.record(
                    AuditEvent(
                        action="auth.device.trusted",
                        hospital_id=user.hospital_id,
                        target_type="user",
                        target_id=user_id,
                        actor_id=user_id,
                    )
                )

            # The cookie carries one token per account that has signed in on
            # this browser. Tokens that no longer name a live row are dropped.
            live = await self._trusted_devices.live_hashes(
                [hash_token(token) for token in device_tokens], now=now
            )
            kept = [token for token in device_tokens if hash_token(token) in live]
            if device is not None:
                # The token just used goes to the end, so that when the
                # cookie is full it is the least recently used that drops out.
                used = [token for token in kept if hash_token(token) == device.token_hash]
                kept = [token for token in kept if token not in used] + used
            await self._uow.commit()
        except Exception as exc:  # noqa: BLE001 — recognition is a convenience; sign-in already succeeded
            await self._uow.rollback()
            logger.warning(
                "trusted_device_not_recorded", user_id=str(user_id), error_type=type(exc).__name__
            )
            return None
        return ".".join((kept + minted)[-DEVICE_TOKENS_PER_COOKIE:]) or None

    async def _retrust_device(self, user: User, device_tokens: Sequence[str]) -> str | None:
        """Give this browser a fresh trusted-device token for the account.

        After an emailed link is completed or a password is changed. Any trust
        this browser already held for the account is replaced: if its old
        token had been copied, the copy stops working here. The account's
        other devices are left alone — forgetting them would mean that using
        the recovery path costs the owner every other device.

        :param user: The account.
        :param device_tokens: Tokens from the browser's cookie.
        :returns: The cookie value to give the browser, or ``None`` on failure.
        """
        user_id = user.id  # see _trust_device: `user` is expired by a rollback
        try:
            await self._trusted_devices.delete_tokens(
                user_id, [hash_token(token) for token in device_tokens]
            )
        except Exception:  # noqa: BLE001 — see _trust_device
            await self._uow.rollback()
            logger.warning("trusted_device_not_recorded", user_id=str(user_id))
            return None
        return await self._trust_device(user, device_tokens, None)

    async def _reread_for_issue(
        self,
        user: User,
        *,
        password_hash: str | None = None,
        mfa_secret: str | None = None,
        message: str = "Invalid credentials.",
    ) -> User:
        """Lock the account row and confirm it is still what was just verified.

        The credential was checked against a copy of the account read before
        the throttle's commits. Between then and now the password may have
        been changed, the second factor replaced or the account suspended —
        in which case a session issued here would be one that the change was
        meant to prevent, and that "revoke every session" has already missed.

        :param user: The account as read earlier.
        :param password_hash: The hash the password was verified against.
        :param mfa_secret: The stored secret the code was verified against.
        :param message: The generic refusal for this step.
        :returns: The account as it is now, locked until the next commit.
        :raises AuthenticationError: If it is gone or has changed.
        """
        current = await self._user_repo.lock_for_authentication(user.id)
        unchanged = (
            current is not None
            and (password_hash is None or current.password_hash == password_hash)
            and (mfa_secret is None or (current.mfa_enabled and current.mfa_secret == mfa_secret))
            and self._rejection_reason(current) is None
        )
        if current is None or not unchanged:
            await self._uow.commit()
            logger.info("login_refused_account_changed", user_id=str(user.id))
            raise AuthenticationError(message)
        return current

    async def _issue_tokens(
        self, user: User, device_info: str | None = None, ip_address: str | None = None
    ) -> dict[str, Any]:
        """Issue a new access token and refresh token pair.

        :param user: The authenticated user.
        :param device_info: Optional device/user-agent string.
        :param ip_address: Optional IP address.
        :returns: Dict with access_token, refresh_token, expires_in, and user profile.
        """
        # Last line of defence: no session is ever minted for an account that
        # may not authenticate, whatever path led here.
        if self._rejection_reason(user) is not None:
            logger.error("token_issue_refused_unusable_account", user_id=str(user.id))
            raise AuthenticationError("Invalid credentials.")

        # Generate refresh token
        raw_refresh_token, refresh_token_hash = generate_opaque_token()

        await self._refresh_token_repo.create(
            user_id=user.id,
            token_hash=refresh_token_hash,
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.JWT_REFRESH_TTL_SECONDS),
            device_info=device_info,
            ip_address=ip_address,
        )

        # The refresh token must be durable before it is handed to the client.
        # Returning a token that was never committed is what made every refresh
        # fail with "Invalid refresh token" on first use.
        await self._uow.commit()

        # Get permissions
        permissions = await self._get_user_permission_codes(user)

        # Create access token
        roles = [r.role.name for r in user.user_roles] if user.user_roles else []
        force_password_change = user.password_changed_at is None

        access_token = create_access_token(
            user_id=user.id,
            hospital_id=user.hospital_id,
            roles=roles,
            permissions=permissions,
            force_password_change=force_password_change,
        )

        return {
            "access_token": access_token,
            "refresh_token": raw_refresh_token,
            "expires_in": settings.JWT_ACCESS_TTL_SECONDS,
            "user": {
                "id": user.id,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "name": f"{user.first_name} {user.last_name}".strip(),
                "phone": user.phone,
                "roles": roles,
                "status": user.status.value,
                "mfa_enabled": user.mfa_enabled,
                "password_change_required": force_password_change,
                # The SPA derives every navigation/route gate from this list
                # (frontend RBAC is a UX affordance; the backend remains the
                # security boundary). Mirrors the codes embedded in the token.
                "permissions": permissions,
            },
        }

    async def _get_user_permission_codes(self, user: User) -> list[str]:
        """Get all permission codes for a user (union of all role permissions).

        :param user: The user to get permissions for.
        :returns: List of permission code strings.
        """
        permissions: list[str] = []
        for user_role in user.user_roles or []:
            role = user_role.role
            if role and role.role_permissions:
                for rp in role.role_permissions:
                    if rp.permission and rp.permission.code not in permissions:
                        permissions.append(rp.permission.code)
        return permissions
