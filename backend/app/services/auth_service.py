"""Authentication service — login, logout, refresh, password management, MFA.

Implements the business rules from ``docs/modules/01-authentication.md``.
Every rule is enforced here, never in the route layer.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from app.core.audit import AuditEvent
from app.core.config import settings
from app.core.exceptions import (
    AuthenticationError,
    BusinessRuleError,
    NotFoundError,
)
from app.core.security import (
    create_access_token,
    create_mfa_ticket,
    generate_opaque_token,
    generate_totp_secret,
    get_totp_provisioning_uri,
    hash_password,
    hash_token,
    password_needs_rehash,
    validate_password_strength,
    verify_access_token,
    verify_password,
    verify_totp_code,
)
from app.models.user import User, UserStatus

if TYPE_CHECKING:
    from typing import Any

    from app.core.audit import AuditSink
    from app.database.unit_of_work import UnitOfWork
    from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
    from app.repositories.refresh_token_repository import RefreshTokenRepository
    from app.repositories.user_repository import UserRepository

logger = structlog.get_logger(__name__)


class AuthService:
    """Handles all authentication-related business logic.

    :param user_repo: Repository for user data access.
    :param refresh_token_repo: Repository for refresh token data access.
    :param password_reset_repo: Repository for password reset token data access.
    :param audit: Where mutating operations are recorded (CLAUDE.md rule 9).
    """

    def __init__(
        self,
        user_repo: UserRepository,
        refresh_token_repo: RefreshTokenRepository,
        password_reset_repo: PasswordResetTokenRepository,
        uow: UnitOfWork,
        audit: AuditSink,
    ) -> None:
        self._user_repo = user_repo
        self._refresh_token_repo = refresh_token_repo
        self._password_reset_repo = password_reset_repo
        self._uow = uow
        self._audit = audit

    # ── Login ────────────────────────────────────────────────────────────────

    async def login(
        self,
        email: str,
        password: str,
        device_info: str | None = None,
        ip_address: str | None = None,
    ) -> dict[str, Any]:
        """Authenticate a user with email and password.

        :param email: The user's email address.
        :param password: The user's plaintext password.
        :param device_info: Optional device/user-agent string.
        :param ip_address: Optional IP address of the client.
        :returns: A dict with ``access_token``, ``refresh_token``, ``expires_in``,
            ``user``, and optionally ``mfa_ticket`` if MFA is required.
        :raises AuthenticationError: If credentials are invalid.
        :raises BusinessRuleError: If the account is locked or suspended.
        """
        # Step 1: Find user by email across all hospitals
        user = await self._find_user_by_email(email)

        if user is None:
            # Generic error — don't reveal whether the email exists, and never
            # log the raw address (PII, CLAUDE.md security rule 10 — B3).
            logger.info(
                "login_attempt_nonexistent_email",
                email_hash=self._email_discriminator(email),
            )
            raise AuthenticationError("Invalid credentials.")

        # Step 2: Check account status
        await self._check_account_status(user)

        # Step 3: Verify password
        if not verify_password(password, user.password_hash):
            await self._audit.record(
                AuditEvent(
                    action="auth.login.failed",
                    hospital_id=user.hospital_id,
                    target_type="user",
                    target_id=user.id,
                    actor_id=user.id,
                    context={"reason": "invalid_password"},
                )
            )
            await self._handle_failed_login(user)
            raise AuthenticationError("Invalid credentials.")

        # Step 4: Rehash password if needed (scheme migration)
        if password_needs_rehash(user.password_hash):
            user = await self._user_repo.update(user, password_hash=hash_password(password))
            logger.info("password_rehashed", user_id=str(user.id))

        # Step 5: Reset failed login count on success
        if user.failed_login_attempts > 0:
            await self._user_repo.reset_failed_logins(user)

        # Step 6: Record login timestamp
        await self._user_repo.record_login(user)

        # Step 7: Check if MFA is required
        if user.mfa_enabled:
            mfa_ticket = create_mfa_ticket(user.id)
            logger.info("mfa_required", user_id=str(user.id))
            # Commit the login timestamp and failed-count reset before
            # returning: this branch does not reach _issue_tokens.
            await self._uow.commit()
            return {
                "mfa_ticket": mfa_ticket,
                "expires_in": 300,  # 5 minutes
            }

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
        return result

    async def verify_mfa(
        self,
        mfa_ticket: str,
        code: str,
        device_info: str | None = None,
        ip_address: str | None = None,
    ) -> dict[str, Any]:
        """Complete MFA verification after login.

        :param mfa_ticket: The MFA ticket from the login response.
        :param code: The 6-digit TOTP code.
        :param device_info: Optional device/user-agent string.
        :param ip_address: Optional IP address of the client.
        :returns: Token response dict on success.
        :raises AuthenticationError: If the ticket or code is invalid.
        """
        try:
            payload = verify_access_token(mfa_ticket)
        except Exception:
            raise AuthenticationError("Invalid or expired MFA ticket.")  # noqa: B904

        if payload.get("type") != "mfa_ticket":
            raise AuthenticationError("Invalid MFA ticket.")

        user_id = uuid.UUID(payload["sub"])
        user = await self._user_repo.get_by_id(user_id)

        if user is None:
            raise AuthenticationError("User not found.")

        if not user.mfa_secret:
            raise AuthenticationError("MFA is not configured for this user.")

        if not verify_totp_code(user.mfa_secret, code):
            await self._audit.record(
                AuditEvent(
                    action="auth.login.failed",
                    hospital_id=user.hospital_id,
                    target_type="user",
                    target_id=user.id,
                    actor_id=user.id,
                    context={"reason": "invalid_mfa_code"},
                )
            )
            logger.info("mfa_verification_failed", user_id=str(user.id))
            raise AuthenticationError("Invalid MFA code.")

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
        return result

    # ── Token Management ─────────────────────────────────────────────────────

    async def refresh_token(
        self, raw_token: str, device_info: str | None = None, ip_address: str | None = None
    ) -> dict[str, Any]:
        """Refresh an access token using a refresh token (rotation pattern).

        Implements refresh token rotation with reuse detection.
        See ``docs/07-SECURITY.md`` §2.3.

        :param raw_token: The opaque refresh token string.
        :param device_info: Optional device/user-agent string.
        :param ip_address: Optional IP address of the client.
        :returns: Dict with new ``access_token`` and ``refresh_token``.
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
            owner = await self._user_repo.get_by_id(stored_token.user_id)
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
            raise AuthenticationError("Refresh token has been revoked. All sessions invalidated.")

        # ── Expiry Check ───────────────────────────────────────────────
        if stored_token.is_expired:
            raise AuthenticationError("Refresh token has expired.")

        # ── Fetch User ─────────────────────────────────────────────────
        user = await self._user_repo.get_by_id(stored_token.user_id)
        if user is None or user.status != UserStatus.ACTIVE:
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

        return {
            "access_token": access_token,
            "refresh_token": raw_new_token,
            "expires_in": settings.JWT_ACCESS_TTL_SECONDS,
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

    async def logout_all(self, user_id: uuid.UUID) -> int:
        """Revoke ALL refresh tokens for a user.

        :param user_id: The user's UUID.
        :returns: The number of revoked tokens.
        """
        count = await self._refresh_token_repo.revoke_all_for_user(user_id)
        logger.info("all_tokens_revoked", user_id=str(user_id), count=count)
        await self._uow.commit()
        return count

    # ── Password Management ────────────────────────────────────────────────

    async def forgot_password(self, email: str) -> None:
        """Request a password reset token.

        Always returns success to prevent email enumeration.

        :param email: The email address to send a reset link to.
        """
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

        # Generate token
        raw_token, token_hash = generate_opaque_token()
        expires_at = datetime.now(UTC) + timedelta(
            minutes=settings.PASSWORD_RESET_TOKEN_TTL_MINUTES
        )

        await self._password_reset_repo.create(
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
        await self._uow.commit()
        logger.info("password_reset_token_created", user_id=str(user.id))

    async def reset_password(self, raw_token: str, new_password: str) -> None:
        """Complete a password reset using a reset token.

        :param raw_token: The password reset token from the email.
        :param new_password: The new password.
        :raises AuthenticationError: If the token is invalid or expired.
        :raises BusinessRuleError: If the password is too weak.
        """
        # Validate password strength
        password_errors = validate_password_strength(new_password)
        if password_errors:
            raise BusinessRuleError(
                "Password does not meet requirements.", detail={"password": password_errors}
            )

        # Find the token
        token_hash = hash_token(raw_token)
        token = await self._password_reset_repo.get_valid_token(token_hash)

        if token is None:
            raise AuthenticationError("Invalid or expired password reset token.")

        # Find the user
        user = await self._user_repo.get_by_id(token.user_id)
        if user is None:
            raise AuthenticationError("User not found.")

        # Update password. An invite token (B6) also flips the account from
        # INVITED to ACTIVE — this is the activation seam the invite flow
        # needs; email delivery itself belongs to the Notifications module.
        now = datetime.now(UTC)
        new_hash = hash_password(new_password)
        updates: dict[str, Any] = {
            "password_hash": new_hash,
            "password_changed_at": now,
        }
        if user.status == UserStatus.INVITED:
            updates["status"] = UserStatus.ACTIVE
        await self._user_repo.update(user, **updates)

        # Mark token as used
        await self._password_reset_repo.mark_as_used(token)

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

    async def change_password(
        self, user_id: uuid.UUID, current_password: str, new_password: str
    ) -> None:
        """Change password for an authenticated user.

        :param user_id: The user's UUID.
        :param current_password: The current password for verification.
        :param new_password: The new password.
        :raises AuthenticationError: If the current password is wrong.
        :raises BusinessRuleError: If the password is too weak.
        """
        user = await self._user_repo.get_by_id(user_id)
        if user is None:
            raise AuthenticationError("User not found.")

        if not verify_password(current_password, user.password_hash):
            raise AuthenticationError("Current password is incorrect.")

        password_errors = validate_password_strength(new_password)
        if password_errors:
            raise BusinessRuleError(
                "Password does not meet requirements.", detail={"password": password_errors}
            )

        now = datetime.now(UTC)
        new_hash = hash_password(new_password)
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
        user = await self._user_repo.get_by_id(user_id)
        if user is None or (
            actor_hospital_id is not None and user.hospital_id != actor_hospital_id
        ):
            raise NotFoundError("User not found.")

        # Set a random password to invalidate the current one, and mark as change required
        await self._user_repo.update(
            user,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            password_changed_at=None,
        )

        # Revoke all sessions
        await self._refresh_token_repo.revoke_all_for_user(user.id)

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
        :returns: Dict with ``secret``, ``provisioning_uri``.
        :raises AuthenticationError: If verification fails.
        """
        user = await self._user_repo.get_by_id(user_id)
        if user is None:
            raise AuthenticationError("User not found.")

        if not verify_password(password, user.password_hash):
            raise AuthenticationError("Invalid password.")

        secret = generate_totp_secret()
        provisioning_uri = get_totp_provisioning_uri(secret, user.email)

        # Store secret temporarily — user must confirm with a valid TOTP code
        # before we enable MFA.
        await self._user_repo.update(user, mfa_secret=secret)

        await self._audit.record(
            AuditEvent(
                action="auth.mfa.enrollment_initiated",
                hospital_id=user.hospital_id,
                target_type="user",
                target_id=user.id,
                actor_id=user.id,
            )
        )
        logger.info("mfa_enrollment_initiated", user_id=str(user.id))

        return {
            "secret": secret,
            "provisioning_uri": provisioning_uri,
        }

    async def confirm_mfa(self, user_id: uuid.UUID, secret: str, code: str) -> None:
        """Confirm MFA enrollment by verifying a TOTP code.

        :param user_id: The user's UUID.
        :param secret: The TOTP secret.
        :param code: The 6-digit TOTP code.
        :raises AuthenticationError: If the code is invalid.
        """
        if not verify_totp_code(secret, code):
            raise AuthenticationError("Invalid MFA code. Please try again.")

        user = await self._user_repo.get_by_id(user_id)
        if user is None:
            raise AuthenticationError("User not found.")

        await self._user_repo.update(
            user,
            mfa_secret=secret,
            mfa_enabled=True,
        )

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
        user = await self._user_repo.get_by_id(user_id)
        if user is None:
            raise AuthenticationError("User not found.")

        if not verify_password(password, user.password_hash):
            raise AuthenticationError("Invalid password.")

        if user.mfa_secret and not verify_totp_code(user.mfa_secret, code):
            raise AuthenticationError("Invalid MFA code.")

        await self._user_repo.update(
            user,
            mfa_secret=None,
            mfa_enabled=False,
        )

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
    def _email_discriminator(email: str) -> str:
        """Return a non-reversible, PII-safe log discriminator for an email.

        The lowercased address is hashed with SHA-256 and only the first 16
        hex chars are kept — enough to correlate a support case, not enough
        to recover the address (CLAUDE.md security rule 10 — B3).

        :param email: The raw email address.
        :returns: A 16-char hex digest.
        """
        return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]

    async def _find_user_by_email(self, email: str) -> User | None:
        """Find a user by email across all hospitals.

        Uses the repository's cross-tenant lookup method.

        :param email: The email to search for.
        :returns: The user, or ``None``.
        """
        return await self._user_repo.get_by_email_cross_tenant(email)

    async def _check_account_status(self, user: User) -> None:
        """Check if the user's account is usable.

        Every failure raises the generic ``AuthenticationError("Invalid
        credentials.")`` — never a distinct ``ACCOUNT_SUSPENDED``/
        ``ACCOUNT_LOCKED`` — so a login attempt cannot be used to enumerate
        valid email addresses (docs/07-SECURITY.md rule 10). The real reason
        is recorded only in the audit event and the log line.

        :param user: The user to check.
        :raises AuthenticationError: Always, with the generic message, whether
            the account is suspended, locked, or has no usable credential.
        """
        if user.status == UserStatus.SUSPENDED:
            await self._audit.record(
                AuditEvent(
                    action="auth.login.failed",
                    hospital_id=user.hospital_id,
                    target_type="user",
                    target_id=user.id,
                    actor_id=user.id,
                    context={"reason": "suspended"},
                )
            )
            logger.info("login_attempt_suspended_account", user_id=str(user.id))
            raise AuthenticationError("Invalid credentials.")

        if user.locked_until and datetime.now(UTC) < user.locked_until:
            await self._audit.record(
                AuditEvent(
                    action="auth.login.failed",
                    hospital_id=user.hospital_id,
                    target_type="user",
                    target_id=user.id,
                    actor_id=user.id,
                    context={"reason": "locked"},
                )
            )
            logger.info(
                "login_attempt_locked_account",
                user_id=str(user.id),
                locked_until=str(user.locked_until),
            )
            raise AuthenticationError("Invalid credentials.")

        if user.status == UserStatus.INVITED and user.password_hash is None:
            logger.info("login_attempt_invited_account", user_id=str(user.id))
            raise AuthenticationError("Invalid credentials.")

    async def _handle_failed_login(self, user: User) -> None:
        """Handle a failed login attempt (increment counter, lock if threshold exceeded).

        :param user: The user who failed to log in.
        """
        attempts = user.failed_login_attempts + 1
        await self._user_repo.update(user, failed_login_attempts=attempts)

        if attempts >= settings.MAX_FAILED_LOGIN_ATTEMPTS:
            locked_until = datetime.now(UTC) + timedelta(minutes=settings.ACCOUNT_LOCKOUT_MINUTES)
            await self._user_repo.lock_account(user, locked_until)
            logger.info(
                "account_locked",
                user_id=str(user.id),
                attempts=attempts,
                locked_until=str(locked_until),
            )

        # Committed here rather than by the caller: login() raises
        # AuthenticationError immediately after this returns, so a commit at the
        # caller's end would never be reached and the attempt counter would be
        # rolled back. Brute-force protection depends on this write surviving a
        # failed request (docs/modules/01-authentication.md §4, rule 4).
        await self._uow.commit()

    async def _issue_tokens(
        self, user: User, device_info: str | None = None, ip_address: str | None = None
    ) -> dict[str, Any]:
        """Issue a new access token and refresh token pair.

        :param user: The authenticated user.
        :param device_info: Optional device/user-agent string.
        :param ip_address: Optional IP address.
        :returns: Dict with access_token, refresh_token, expires_in, and user profile.
        """
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
