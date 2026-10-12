"""Unit tests for :class:`AuthService`.

Tests business logic with mocked repositories.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.core.exceptions import (
    AuthenticationError,
    BusinessRuleError,
    NotFoundError,
)
from app.core.security import hash_password, hash_token, verify_password
from app.models.user import User, UserStatus
from app.services.auth_throttle import BucketKind
from app.tests.conftest import AdmitAllThrottle


def _known(repo: AsyncMock, user: User) -> User:
    """Make ``user`` the account every lookup on ``repo`` finds.

    A sign-in reads the account once without a lock, and — only after the
    credential has been verified — re-reads it under lock
    (``lock_for_authentication``) before a session is issued.
    """
    repo.get_by_email_cross_tenant.return_value = user
    repo.get_by_id.return_value = user
    repo.lock_for_authentication.return_value = user
    return user


@pytest.fixture
def mock_user_repo() -> AsyncMock:
    """Create a mock UserRepository."""
    repo = AsyncMock()
    # No other reset request for the same address is in flight (the per-email
    # claim the service takes before handling a forgot-password request).
    repo.claim_authentication_attempt.return_value = True
    return repo


@pytest.fixture
def mock_refresh_token_repo() -> AsyncMock:
    """Create a mock RefreshTokenRepository."""
    return AsyncMock()


@pytest.fixture
def mock_password_reset_repo() -> AsyncMock:
    """Create a mock PasswordResetTokenRepository."""
    repo = AsyncMock()
    # No reset email has been sent to the account within the token lifetime.
    repo.count_issued_since.return_value = 0
    return repo


@pytest.fixture
def throttle() -> AdmitAllThrottle:
    """The test double for the authentication throttle: admits, and remembers."""
    return AdmitAllThrottle()


@pytest.fixture
def mock_notifier() -> AsyncMock:
    """Where the service asks for the password-reset email to be sent.

    ``deliver_credential`` answers ``True``: the email carrying the link was
    queued. (A bare ``AsyncMock`` would answer with a mock, which is not
    ``True`` — and the service would rightly kill the token.)
    """
    notifier = AsyncMock()
    notifier.deliver_credential.return_value = True
    return notifier


@pytest.fixture
def email_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mail transport exists, so a reset link has somewhere to go."""
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.hospital.test")


@pytest.fixture
def auth_service(
    mock_user_repo: AsyncMock,
    mock_refresh_token_repo: AsyncMock,
    mock_password_reset_repo: AsyncMock,
    mock_uow: AsyncMock,
    audit_sink: Any,
    throttle: AdmitAllThrottle,
    mock_notifier: AsyncMock,
) -> Any:
    """Create an AuthService with mocked repositories."""
    from app.services.auth_service import AuthService

    return AuthService(
        user_repo=mock_user_repo,
        refresh_token_repo=mock_refresh_token_repo,
        password_reset_repo=mock_password_reset_repo,
        uow=mock_uow,
        audit=audit_sink,
        throttle=throttle,  # type: ignore[arg-type]
        trusted_devices=AsyncMock(),
        notifier=mock_notifier,
    )


def _redeemable(repo: AsyncMock, user_id: uuid.UUID) -> MagicMock:
    """Make ``repo`` hold one valid emailed token for ``user_id``.

    Redemption reads the token (``get_valid_token``) to learn whose it is,
    locks that account, and only then spends it (``consume``).
    """
    from app.models.password_reset_token import PasswordResetToken

    token = MagicMock(spec=PasswordResetToken)
    token.id = uuid.uuid4()
    token.user_id = user_id
    repo.get_valid_token.return_value = token
    repo.consume.return_value = token
    return token


def _make_user(overrides: dict[str, Any] | None = None) -> User:
    """Create a test user with sensible defaults."""
    user_id = uuid.uuid4()
    hospital_id = uuid.uuid4()
    role_id = uuid.uuid4()
    perm_id = uuid.uuid4()

    # Build a minimal user with relationships
    from app.models.permission import Permission
    from app.models.role import Role, RolePermission
    from app.models.user import UserRole

    role = MagicMock(spec=Role)
    role.id = role_id
    role.name = "Hospital Admin"
    role.description = "Test role"
    role.is_system = False
    role.hospital_id = hospital_id

    perm = MagicMock(spec=Permission)
    perm.id = perm_id
    perm.code = "user.read"
    perm.module = "auth"

    rp = MagicMock(spec=RolePermission)
    rp.permission = perm

    role.role_permissions = [rp]

    ur = MagicMock(spec=UserRole)
    ur.role = role
    ur.user_id = user_id
    ur.role_id = role_id

    user = MagicMock(spec=User)
    user.id = user_id
    user.hospital_id = hospital_id
    user.email = overrides.get("email", "test@hospital.test") if overrides else "test@hospital.test"
    user.password_hash = hash_password("TestPass@123")
    user.first_name = "Test"
    user.last_name = "User"
    user.phone = "+911234567890"
    user.status = UserStatus.ACTIVE
    user.hospital_is_active = True
    user.mfa_enabled = False
    user.mfa_secret = None
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = None
    user.password_changed_at = datetime.now(UTC)
    user.user_roles = [ur]
    user.created_at = datetime.now(UTC)
    user.updated_at = datetime.now(UTC)

    if overrides:
        for key, value in overrides.items():
            if hasattr(user, key):
                setattr(user, key, value)

    return user


# ── Login Tests ────────────────────────────────────────────────────────────


class TestLogin:
    """Tests for the login flow."""

    async def test_login_success(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """Happy path: valid credentials return tokens."""
        user = _known(mock_user_repo, _make_user())
        mock_refresh_token_repo.create.return_value = MagicMock(id=uuid.uuid4())

        result = await auth_service.login(
            email="test@hospital.test",
            password="TestPass@123",
        )

        assert "access_token" in result
        assert "refresh_token" in result
        assert "user" in result
        assert result["user"]["email"] == "test@hospital.test"
        mock_user_repo.record_login.assert_called_once()
        # The session is issued for the row as re-read under lock, by id.
        mock_user_repo.lock_for_authentication.assert_awaited_once_with(user.id)

    async def test_login_gives_the_browser_a_device_token_it_did_not_choose(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """Attack: plant a device token in a victim's browser before they sign in.

        The cookie value returned after a sign-in is a token the server
        minted. A value the client presented that names no live device of
        this account is never adopted.
        """
        _known(mock_user_repo, _make_user())
        mock_refresh_token_repo.create.return_value = MagicMock(id=uuid.uuid4())
        planted = "p" * 43
        devices = auth_service._trusted_devices
        devices.find.return_value = None
        devices.live_hashes.return_value = set()

        result = await auth_service.login(
            email="test@hospital.test", password="TestPass@123", device_tokens=(planted,)
        )

        cookie = result["device_cookie"]
        assert cookie
        assert planted not in cookie
        stored_hash = devices.create.await_args.args[1]
        assert stored_hash == hash_token(cookie)
        assert stored_hash != hash_token(planted)

    async def test_login_is_charged_to_the_throttle_before_the_password_is_checked(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        throttle: AdmitAllThrottle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Charge first: by the time the hash is touched, the attempt is already counted."""
        from app.services import auth_service as module

        _known(mock_user_repo, _make_user())
        admitted_when_verified: list[int] = []

        def _verify(password: str, hashed: str) -> bool:
            admitted_when_verified.append(len(throttle.admitted))
            return verify_password(password, hashed)

        monkeypatch.setattr(module, "verify_password", _verify)

        with pytest.raises(AuthenticationError):
            await auth_service.login(
                email="Test@Hospital.Test", password="WrongPass@123", ip_address="203.0.113.7"
            )

        assert admitted_when_verified == [1]
        assert [b.kind for b in throttle.admitted[0]] == [
            BucketKind.PW_ACCOUNT,
            BucketKind.PW_SOURCE,
            BucketKind.PW_PAIR,
        ]
        # A wrong password keeps its charge: nothing is settled.
        assert throttle.settled == []

    async def test_login_user_payload_includes_permissions_and_display_name(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """The SPA builds its RBAC nav from ``user.permissions`` and displays ``user.name``."""
        _known(mock_user_repo, _make_user())
        mock_refresh_token_repo.create.return_value = MagicMock(id=uuid.uuid4())

        result = await auth_service.login(
            email="test@hospital.test",
            password="TestPass@123",
        )

        profile = result["user"]
        assert profile["name"] == "Test User"
        assert profile["permissions"] == ["user.read"]

    async def test_login_user_permissions_are_empty_without_roles(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """A role-less user gets no permissions and no fabricated access."""
        user = _make_user()
        user.user_roles = []
        _known(mock_user_repo, user)
        mock_refresh_token_repo.create.return_value = MagicMock(id=uuid.uuid4())

        result = await auth_service.login(
            email="test@hospital.test",
            password="TestPass@123",
        )

        assert result["user"]["permissions"] == []
        mock_user_repo.record_login.assert_called_once()

    async def test_login_invalid_credentials(
        self: Any, auth_service: Any, mock_user_repo: Any
    ) -> None:
        """Invalid password returns AuthenticationError."""
        user = _make_user()
        mock_user_repo.get_by_email_cross_tenant.return_value = user

        with pytest.raises(AuthenticationError, match="Invalid credentials."):
            await auth_service.login(
                email="test@hospital.test",
                password="WrongPass@123",
            )

    async def test_login_nonexistent_email(
        self: Any, auth_service: Any, mock_user_repo: Any
    ) -> None:
        """Non-existent email returns generic error."""
        mock_user_repo.get_by_email_cross_tenant.return_value = None

        with pytest.raises(AuthenticationError, match="Invalid credentials."):
            await auth_service.login(
                email="nonexistent@test.test",
                password="SomePass@123",
            )

    async def test_login_suspended_account(
        self: Any, auth_service: Any, mock_user_repo: Any
    ) -> None:
        """Suspended account login is indistinguishable from invalid credentials.

        Anti-enumeration (docs/07-SECURITY.md rule 10): the response must not
        reveal that the email belongs to a real, suspended account.
        """
        user = _make_user({"status": UserStatus.SUSPENDED})
        mock_user_repo.get_by_email_cross_tenant.return_value = user

        with pytest.raises(AuthenticationError, match="Invalid credentials."):
            await auth_service.login(
                email="test@hospital.test",
                password="TestPass@123",
            )

    async def test_a_legacy_lock_on_the_row_does_not_keep_the_owner_out(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """There is no "locked" state any more.

        The old lockout let anyone who knew a staff email keep its owner out
        by failing the password five times. ``locked_until`` and
        ``failed_login_attempts`` are legacy columns: a row that still carries
        them — set by the old code, or by hand — signs in normally with the
        right password, and nothing writes to them.
        """
        user = _make_user(
            {"locked_until": datetime.now(UTC) + timedelta(hours=1), "failed_login_attempts": 99}
        )
        _known(mock_user_repo, user)
        mock_refresh_token_repo.create.return_value = MagicMock(id=uuid.uuid4())

        result = await auth_service.login(email="test@hospital.test", password="TestPass@123")

        assert result["access_token"]
        assert user.failed_login_attempts == 99
        for call in mock_user_repo.update.await_args_list:
            assert not {"locked_until", "failed_login_attempts"} & set(call.kwargs)

    async def test_a_wrong_password_writes_nothing_to_the_account(
        self: Any, auth_service: Any, mock_user_repo: Any, audit_sink: Any
    ) -> None:
        """A failure is counted in the throttle's buckets and audited — never on the user row."""
        user = _known(mock_user_repo, _make_user())

        with pytest.raises(AuthenticationError, match="Invalid credentials."):
            await auth_service.login(email="test@hospital.test", password="WrongPass@123")

        mock_user_repo.update.assert_not_awaited()
        mock_user_repo.lock_for_authentication.assert_not_awaited()
        assert user.locked_until is None
        assert audit_sink.last().action == "auth.login.failed"
        assert audit_sink.last().context == {"reason": "invalid_password"}

    async def test_a_throttled_login_is_the_generic_error_and_evaluates_nothing(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        throttle: AdmitAllThrottle,
        audit_sink: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: keep guessing once the allowance is spent.

        A refused admission must not reach the password hash — not even with
        the *correct* password — and must look exactly like a wrong one.
        """
        from app.services import auth_service as module

        evaluations: list[str] = []

        def _verify(_password: str, _hashed: str) -> bool:
            evaluations.append("verify")
            return True

        def _burn(_password: str) -> None:
            evaluations.append("burn")

        monkeypatch.setattr(module, "verify_password", _verify)
        monkeypatch.setattr(module, "burn_password_verification", _burn)
        _known(mock_user_repo, _make_user())
        throttle.refuse = True

        with pytest.raises(AuthenticationError) as refused:
            await auth_service.login(email="test@hospital.test", password="TestPass@123")

        assert str(refused.value) == "Invalid credentials."
        assert evaluations == []
        assert throttle.settled == []
        assert audit_sink.events == []
        mock_user_repo.lock_for_authentication.assert_not_awaited()
        mock_user_repo.record_login.assert_not_awaited()


# ── Token Refresh Tests ────────────────────────────────────────────────────


class TestRefreshToken:
    """Tests for refresh token rotation."""

    async def test_refresh_success(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """Valid refresh token returns new token pair."""
        from app.models.refresh_token import RefreshToken

        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        token = MagicMock(spec=RefreshToken)
        token.id = uuid.uuid4()
        token.user_id = user.id
        token.is_revoked = False
        token.is_expired = False
        token.is_valid = True

        mock_refresh_token_repo.get_by_token_hash.return_value = token
        mock_refresh_token_repo.create.return_value = MagicMock(id=uuid.uuid4())

        result = await auth_service.refresh_token(raw_token="some-valid-token")

        assert result["access_token"] is not None
        assert result["refresh_token"] is not None
        assert result["expires_in"] > 0

    async def test_refresh_reuse_detection(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """Revoked token triggers reuse detection."""
        from app.models.refresh_token import RefreshToken

        user_id = uuid.uuid4()
        token = MagicMock(spec=RefreshToken)
        token.id = uuid.uuid4()
        token.user_id = user_id
        token.is_revoked = True
        token.is_expired = False

        mock_refresh_token_repo.get_by_token_hash.return_value = token

        with pytest.raises(AuthenticationError, match="has been revoked"):
            await auth_service.refresh_token(raw_token="stolen-token")

        mock_refresh_token_repo.revoke_all_for_user.assert_called_once_with(user_id)

    async def test_refresh_reuse_revocation_is_committed_before_the_error(
        self: Any,
        auth_service: Any,
        mock_refresh_token_repo: Any,
        mock_uow: Any,
    ) -> None:
        """The revocation must be durable, not undone when the request ends.

        The reuse branch ends in an exception, and the request-scoped session
        rolls back anything uncommitted when it closes. Without a commit before
        the raise, every session of the compromised account stayed alive and
        the audit row vanished (PR #29 re-review).
        """
        from app.models.refresh_token import RefreshToken

        calls: list[str] = []
        mock_refresh_token_repo.revoke_all_for_user.side_effect = lambda *_: calls.append("revoke")
        mock_uow.commit.side_effect = lambda: calls.append("commit")

        token = MagicMock(spec=RefreshToken)
        token.id = uuid.uuid4()
        token.user_id = uuid.uuid4()
        token.is_revoked = True
        token.is_expired = False
        mock_refresh_token_repo.get_by_token_hash.return_value = token

        with pytest.raises(AuthenticationError, match="has been revoked"):
            await auth_service.refresh_token(raw_token="stolen-token")

        assert calls == ["revoke", "commit"]


# ── Password Reset Tests ───────────────────────────────────────────────────


class TestPasswordReset:
    """Tests for password reset flow."""

    @pytest.mark.usefixtures("email_configured")
    async def test_forgot_password_existing_user(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_password_reset_repo: Any
    ) -> None:
        """Existing user gets a reset token created."""
        user = _make_user()
        mock_user_repo.get_by_email_cross_tenant.return_value = user

        await auth_service.forgot_password(email="test@hospital.test")

        mock_password_reset_repo.create.assert_called_once()

    @pytest.mark.usefixtures("email_configured")
    async def test_the_reset_token_leaves_only_inside_the_emailed_link(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_notifier: AsyncMock,
        audit_sink: Any,
    ) -> None:
        """Attack: read a reset token from somewhere other than the owner's mailbox.

        The raw token is stored only as a hash, travels only in the secret
        part of the notification, and sits in the URL *fragment* — which a
        browser never sends to a server, so it reaches no access log and no
        ``Referer`` header.
        """
        user = _make_user()
        mock_user_repo.get_by_email_cross_tenant.return_value = user

        await auth_service.forgot_password(email="test@hospital.test")

        # One request, through the channel that reports whether it was queued;
        # the fire-and-forget channel never carries a credential.
        mock_notifier.deliver_credential.assert_awaited_once()
        mock_notifier.notify.assert_not_awaited()
        request = mock_notifier.deliver_credential.await_args.args[0]
        url = request.secret_variables["action_url"]
        base = settings.FRONTEND_BASE_URL.rstrip("/")
        assert url.startswith(f"{base}/reset-password#token=")
        assert "?" not in url
        raw_token = url.split("#token=", 1)[1]
        assert len(raw_token) >= 32

        stored = mock_password_reset_repo.create.await_args.kwargs
        assert stored["token_hash"] == hash_token(raw_token)
        assert raw_token not in repr(stored)
        assert raw_token not in repr(request.variables)
        assert raw_token not in repr(audit_sink.events)
        assert request.recipient_user_ids == (user.id,)
        # Queued, so the link in the owner's mailbox stays alive.
        mock_password_reset_repo.mark_as_used.assert_not_awaited()

    @pytest.mark.usefixtures("email_configured")
    @pytest.mark.parametrize("answer", [False, None, 0, 1, "queued", MagicMock()])
    async def test_a_reset_token_whose_email_was_not_queued_is_dead_before_the_request_ends(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_notifier: AsyncMock,
        mock_uow: Any,
        answer: Any,
    ) -> None:
        """Attack: collect a live reset token that no email ever carried.

        If the email could not be queued, nobody may hold a working link: the
        token just minted is marked used in the same transaction. Fail closed
        — only a definite ``True`` from the notifier keeps it alive.
        """
        mock_user_repo.get_by_email_cross_tenant.return_value = _make_user()
        minted = MagicMock(name="minted-token")
        mock_password_reset_repo.create.return_value = minted
        mock_notifier.deliver_credential.return_value = answer
        order: list[str] = []
        mock_password_reset_repo.mark_as_used.side_effect = lambda *_: order.append("kill")
        mock_uow.commit.side_effect = lambda: order.append("commit")

        await auth_service.forgot_password(email="test@hospital.test")

        mock_password_reset_repo.mark_as_used.assert_awaited_once_with(minted)
        # Killed before the commit that would have made a live token durable.
        assert order[:2] == ["kill", "commit"]
        # No other link of the account is touched: one already in the mailbox still works.
        mock_password_reset_repo.invalidate_all_for_user.assert_not_awaited()

    @pytest.mark.usefixtures("email_configured")
    async def test_an_unqueued_reset_email_reports_nothing_different_to_the_caller(
        self: Any,
        mock_user_repo: Any,
        mock_refresh_token_repo: Any,
        mock_uow: Any,
        audit_sink: Any,
        throttle: AdmitAllThrottle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: tell "mail is broken for this real account" from any other answer.

        Queued, not queued, and an address nobody has: the same ``None``, no
        exception, and the same single wait to the uniform duration.
        """
        from app.services import auth_service as module
        from app.services.auth_service import AuthService

        monkeypatch.setattr(settings, "AUTH_FAILURE_MIN_SECONDS", 0.5)
        outcomes: list[tuple[Any, int]] = []
        for queued, user in ((True, _make_user()), (False, _make_user()), (True, None)):
            waits: list[float] = []

            async def _record(seconds: float, waits: list[float] = waits) -> None:
                waits.append(seconds)

            monkeypatch.setattr(module, "_sleep", _record)
            mock_user_repo.get_by_email_cross_tenant.return_value = user
            tokens, notifier = AsyncMock(), AsyncMock()
            tokens.count_issued_since.return_value = 0
            notifier.deliver_credential.return_value = queued
            service: Any = AuthService(
                user_repo=mock_user_repo,
                refresh_token_repo=mock_refresh_token_repo,
                password_reset_repo=tokens,
                uow=mock_uow,
                audit=audit_sink,
                throttle=throttle,  # type: ignore[arg-type]
                trusted_devices=AsyncMock(),
                notifier=notifier,
            )

            result = await service.forgot_password(email="test@hospital.test")

            outcomes.append((result, len(waits)))
            assert all(0.4 < wait <= 0.5 * 1.2 for wait in waits)
            assert tokens.mark_as_used.await_count == (1 if user is not None and not queued else 0)

        assert outcomes == [(None, 1)] * 3

    async def test_no_token_is_minted_when_there_is_no_way_to_deliver_it(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_notifier: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A valid credential nobody can receive is only a liability: none is created."""
        monkeypatch.setattr(settings, "SMTP_HOST", None)
        mock_user_repo.get_by_email_cross_tenant.return_value = _make_user()

        await auth_service.forgot_password(email="test@hospital.test")

        mock_password_reset_repo.create.assert_not_awaited()
        mock_notifier.deliver_credential.assert_not_awaited()
        mock_notifier.notify.assert_not_awaited()

    @pytest.mark.usefixtures("email_configured")
    @pytest.mark.parametrize("already_sent", [3, 4, 50])
    async def test_an_anonymous_caller_cannot_bury_a_mailbox_in_reset_emails(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_notifier: AsyncMock,
        already_sent: int,
    ) -> None:
        """Attack: request a reset for someone else, over and over.

        Three per account per token lifetime, and then nothing more is
        written or sent — while the response stays the same success.
        """
        mock_user_repo.get_by_email_cross_tenant.return_value = _make_user()
        mock_password_reset_repo.count_issued_since.return_value = already_sent

        await auth_service.forgot_password(email="test@hospital.test")

        mock_password_reset_repo.create.assert_not_awaited()
        mock_notifier.deliver_credential.assert_not_awaited()
        mock_notifier.notify.assert_not_awaited()

    @pytest.mark.usefixtures("email_configured")
    async def test_the_reset_email_limit_counts_one_token_lifetime_back(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_password_reset_repo: Any
    ) -> None:
        """Two already sent is still under the limit; the window is the token's lifetime."""
        user = _make_user()
        mock_user_repo.get_by_email_cross_tenant.return_value = user
        mock_password_reset_repo.count_issued_since.return_value = 2
        before = datetime.now(UTC)

        await auth_service.forgot_password(email="test@hospital.test")

        mock_password_reset_repo.create.assert_awaited_once()
        counted_user, since = mock_password_reset_repo.count_issued_since.await_args.args
        lifetime = timedelta(minutes=settings.PASSWORD_RESET_TOKEN_TTL_MINUTES)
        assert counted_user == user.id
        assert abs((before - lifetime) - since) < timedelta(seconds=5)

    @pytest.mark.usefixtures("email_configured")
    @pytest.mark.parametrize(
        "overrides",
        [
            {"status": UserStatus.INVITED},
            {"status": UserStatus.SUSPENDED},
            {"hospital_is_active": False},
        ],
    )
    async def test_forgot_password_gives_nothing_to_an_account_that_is_not_active(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_notifier: AsyncMock,
        overrides: dict[str, Any],
    ) -> None:
        """Attack: keep a lapsed invitation alive, or revive a suspended account, anonymously.

        An invited account gets its link from an administrator's invitation
        and nowhere else. The response is the same success either way.
        """
        mock_user_repo.get_by_email_cross_tenant.return_value = _make_user(overrides)

        assert await auth_service.forgot_password(email="test@hospital.test") is None

        mock_password_reset_repo.create.assert_not_awaited()
        mock_password_reset_repo.count_issued_since.assert_not_awaited()
        mock_notifier.deliver_credential.assert_not_awaited()
        mock_notifier.notify.assert_not_awaited()

    async def test_forgot_password_nonexistent_user(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_password_reset_repo: Any
    ) -> None:
        """Non-existent user still returns success (no reveal)."""
        mock_user_repo.get_by_email_cross_tenant.return_value = None

        await auth_service.forgot_password(email="nonexistent@test.test")

        mock_password_reset_repo.create.assert_not_called()

    async def test_reset_password_weak_password(
        self: Any, auth_service: Any, mock_password_reset_repo: Any
    ) -> None:
        """Weak password raises BusinessRuleError."""
        with pytest.raises(BusinessRuleError, match="Password does not meet requirements"):
            await auth_service.reset_password(
                raw_token="valid-token",
                new_password="weak",
            )

    async def test_reset_password_activates_an_invited_user(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
    ) -> None:
        """B6: consuming an invite token flips INVITED -> ACTIVE."""
        user = _known(mock_user_repo, _make_user({"status": UserStatus.INVITED}))

        token = _redeemable(mock_password_reset_repo, user.id)

        async def _update_in_place(instance: Any, **kwargs: Any) -> Any:
            for key, value in kwargs.items():
                setattr(instance, key, value)
            return instance

        mock_user_repo.update.side_effect = _update_in_place

        await auth_service.reset_password(
            raw_token="invite-token", new_password="Str0ng!Passw0rd123"
        )

        assert token.user_id == user.id
        assert user.status == UserStatus.ACTIVE
        assert user.password_changed_at is not None
        # Looked up, then spent in one atomic statement — both keyed by the
        # hash of what was presented, never by the raw token.
        mock_password_reset_repo.get_valid_token.assert_awaited_once_with(
            hash_token("invite-token")
        )
        mock_password_reset_repo.consume.assert_awaited_once_with(hash_token("invite-token"))
        mock_user_repo.lock_for_authentication.assert_awaited_once_with(user.id)
        mock_password_reset_repo.invalidate_all_for_user.assert_awaited_once_with(user.id)

    async def test_reset_password_keeps_active_status_active(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_password_reset_repo: Any
    ) -> None:
        """B6: a plain reset on an ACTIVE user leaves the status unchanged."""
        user = _known(mock_user_repo, _make_user({"status": UserStatus.ACTIVE}))
        _redeemable(mock_password_reset_repo, user.id)

        async def _update_in_place(instance: Any, **kwargs: Any) -> Any:
            for key, value in kwargs.items():
                setattr(instance, key, value)
            return instance

        mock_user_repo.update.side_effect = _update_in_place

        await auth_service.reset_password(
            raw_token="reset-token", new_password="Str0ng!Passw0rd123"
        )

        assert user.status == UserStatus.ACTIVE

    async def test_a_token_that_is_not_valid_reaches_no_account(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_refresh_token_repo: Any,
    ) -> None:
        """Attack: present made-up, used or expired tokens to take row locks on accounts.

        A token the lookup does not find names no account: no user row is
        locked, nothing is spent, nothing is written.
        """
        mock_password_reset_repo.get_valid_token.return_value = None

        with pytest.raises(AuthenticationError, match="Invalid or expired password reset token."):
            await auth_service.reset_password(
                raw_token="used-token", new_password="Str0ng!Passw0rd123"
            )

        mock_password_reset_repo.get_valid_token.assert_awaited_once_with(hash_token("used-token"))
        mock_user_repo.lock_for_authentication.assert_not_awaited()
        mock_password_reset_repo.consume.assert_not_awaited()
        mock_user_repo.update.assert_not_awaited()
        mock_refresh_token_repo.revoke_all_for_user.assert_not_awaited()
        auth_service._trusted_devices.create.assert_not_awaited()

    async def test_the_loser_of_a_race_for_one_token_changes_nothing(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_refresh_token_repo: Any,
        mock_uow: Any,
    ) -> None:
        """Attack: replay a token, or race two requests with the same one.

        The lookup is only a read — both racers pass it. ``consume`` is the
        single atomic gate: the request it yields nothing to gets the ordinary
        refusal, sets no password, revokes nothing, trusts no device, and
        releases the account row it had locked.
        """
        user = _known(mock_user_repo, _make_user())
        _redeemable(mock_password_reset_repo, user.id)
        mock_password_reset_repo.consume.return_value = None

        with pytest.raises(AuthenticationError, match="Invalid or expired password reset token."):
            await auth_service.reset_password(
                raw_token="raced-token", new_password="Str0ng!Passw0rd123"
            )

        mock_password_reset_repo.consume.assert_awaited_once_with(hash_token("raced-token"))
        mock_user_repo.update.assert_not_awaited()
        mock_refresh_token_repo.revoke_all_for_user.assert_not_awaited()
        mock_password_reset_repo.invalidate_all_for_user.assert_not_awaited()
        auth_service._trusted_devices.create.assert_not_awaited()
        auth_service._trusted_devices.delete_tokens.assert_not_awaited()
        # The row lock taken on the account is given back at once.
        mock_uow.commit.assert_awaited()

    async def test_the_account_is_locked_before_the_token_is_spent(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_uow: Any,
    ) -> None:
        """Attack: race an activation against an invitation resend to deadlock both.

        Suspending an account and sending an invitation again take the user
        row and then its tokens. Redemption must take them in that same order
        — plain lookup, user row, then the atomic spend — or the two wait on
        each other. And the old check-then-mark is not what makes a token
        single-use: ``mark_as_used`` is never called here.
        """
        user = _known(mock_user_repo, _make_user())
        token = _redeemable(mock_password_reset_repo, user.id)
        order: list[str] = []

        async def _lookup(_hash: str) -> Any:
            order.append("lookup")
            return token

        async def _lock(_user_id: uuid.UUID) -> Any:
            order.append("lock-user")
            return user

        async def _consume(_hash: str) -> Any:
            order.append("consume-token")
            return token

        mock_password_reset_repo.get_valid_token.side_effect = _lookup
        mock_user_repo.lock_for_authentication.side_effect = _lock
        mock_password_reset_repo.consume.side_effect = _consume
        mock_uow.commit.side_effect = lambda: order.append("commit")

        await auth_service.reset_password(raw_token="t", new_password="Str0ng!Passw0rd123")

        assert order[:3] == ["lookup", "lock-user", "consume-token"]
        # Nothing is committed (no lock released) between taking the row and spending the token.
        assert "commit" not in order[: order.index("consume-token")]
        mock_user_repo.lock_for_authentication.assert_awaited_once_with(user.id)
        mock_password_reset_repo.mark_as_used.assert_not_awaited()

    async def test_the_account_locked_is_the_tokens_own_never_one_the_caller_names(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_password_reset_repo: Any
    ) -> None:
        """The row to lock comes from the server's token record."""
        owner = _known(mock_user_repo, _make_user())
        _redeemable(mock_password_reset_repo, owner.id)

        await auth_service.reset_password(raw_token="t", new_password="Str0ng!Passw0rd123")

        mock_user_repo.lock_for_authentication.assert_awaited_once_with(owner.id)
        mock_user_repo.get_by_id.assert_not_awaited()
        mock_user_repo.get_by_email_cross_tenant.assert_not_awaited()

    @pytest.mark.parametrize(
        "overrides",
        [
            {"status": UserStatus.SUSPENDED},
            {"hospital_is_active": False},
            {"status": "some-future-state"},
            None,
        ],
    )
    async def test_a_valid_token_for_an_unusable_account_is_refused_and_stays_spent(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_refresh_token_repo: Any,
        mock_uow: Any,
        overrides: dict[str, Any] | None,
    ) -> None:
        """Attack: get back into a suspended account with a link emailed before the suspension.

        Refused with the very message a bad token gets (the account's state is
        not revealed) and nothing is written to the account. The refusal is
        *committed*, not rolled back: the token was spent by ``consume`` and
        stays spent, so the same link does not come back to life if the
        account is reinstated later.
        """
        user = _make_user(overrides) if overrides is not None else None
        mock_user_repo.lock_for_authentication.return_value = user
        token = _redeemable(mock_password_reset_repo, uuid.uuid4())
        order: list[str] = []

        async def _consume(_hash: str) -> Any:
            order.append("consume-token")
            return token

        mock_password_reset_repo.consume.side_effect = _consume
        mock_uow.commit.side_effect = lambda: order.append("commit")

        with pytest.raises(AuthenticationError, match="Invalid or expired password reset token."):
            await auth_service.reset_password(raw_token="t", new_password="Str0ng!Passw0rd123")

        assert order == ["consume-token", "commit"]
        mock_uow.rollback.assert_not_awaited()
        mock_user_repo.update.assert_not_awaited()
        mock_refresh_token_repo.revoke_all_for_user.assert_not_awaited()
        auth_service._trusted_devices.create.assert_not_awaited()

    async def test_a_reset_revokes_every_session_and_trusts_only_this_browser_anew(
        self: Any,
        auth_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_refresh_token_repo: Any,
    ) -> None:
        """Attack: keep a stolen session, or a copied device token, alive across a reset."""
        user = _known(mock_user_repo, _make_user())
        _redeemable(mock_password_reset_repo, user.id)
        presented = "d" * 43
        devices = auth_service._trusted_devices
        devices.live_hashes.return_value = set()

        cookie = await auth_service.reset_password(
            raw_token="t", new_password="Str0ng!Passw0rd123", device_tokens=(presented,)
        )

        mock_refresh_token_repo.revoke_all_for_user.assert_awaited_once_with(user.id)
        # The trust this browser held is replaced, not kept: a copy of the old token dies here.
        devices.delete_tokens.assert_awaited_once_with(user.id, [hash_token(presented)])
        assert cookie
        assert presented not in cookie
        assert devices.create.await_args.args[:2] == (user.id, hash_token(cookie))
        # An emailed link proves control of the mailbox, not of the second
        # factor: the browser gets no MFA budget of its own from it.
        assert devices.create.await_args.kwargs["mfa_verified"] is False

    async def test_admin_reset_cross_tenant_is_not_found(
        self: Any, auth_service: Any, mock_user_repo: Any
    ) -> None:
        """B1: an admin cannot reset another hospital's user (404, like every
        other cross-tenant admin write — never 401, which would leak that the
        user exists elsewhere)."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await auth_service.admin_reset_password(
                user_id=user.id,
                actor_hospital_id=uuid.uuid4(),  # different hospital
            )

    async def test_admin_reset_same_hospital_succeeds(
        self: Any, auth_service: Any, mock_user_repo: Any, mock_refresh_token_repo: Any
    ) -> None:
        """B1: same-hospital admin reset still works."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        await auth_service.admin_reset_password(
            user_id=user.id,
            actor_hospital_id=user.hospital_id,
        )

        mock_refresh_token_repo.revoke_all_for_user.assert_called_once_with(user.id)
        # An account an administrator had to reset keeps no recognised browser.
        auth_service._trusted_devices.delete_for_user.assert_awaited_once_with(user.id)

    async def test_admin_reset_of_another_hospitals_user_forgets_no_devices(
        self: Any, auth_service: Any, mock_user_repo: Any
    ) -> None:
        """Attack: an admin of hospital A wiping the trusted devices of hospital B's staff."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError):
            await auth_service.admin_reset_password(user_id=user.id, actor_hospital_id=uuid.uuid4())

        auth_service._trusted_devices.delete_for_user.assert_not_awaited()

    async def test_sign_out_everywhere_forgets_every_trusted_device(
        self: Any, auth_service: Any, mock_refresh_token_repo: Any
    ) -> None:
        user_id = uuid.uuid4()
        mock_refresh_token_repo.revoke_all_for_user.return_value = 3

        assert await auth_service.logout_all(user_id) == 3

        auth_service._trusted_devices.delete_for_user.assert_awaited_once_with(user_id)


# ── MFA Tests ──────────────────────────────────────────────────────────────


class TestMFA:
    """Tests for MFA enrollment and verification."""

    async def test_enroll_mfa(self: Any, auth_service: Any, mock_user_repo: Any) -> None:
        """MFA enrollment returns a secret."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        result = await auth_service.enroll_mfa(
            user_id=user.id,
            password="TestPass@123",
        )

        assert "secret" in result
        assert "provisioning_uri" in result
        mock_user_repo.update.assert_called_once()

    async def test_re_enrolment_is_refused_when_mfa_already_enabled(
        self: Any, auth_service: Any, mock_user_repo: Any
    ) -> None:
        """PR #29 review finding 2: re-enrolling must not overwrite a live secret.

        Before the fix the new secret was written and committed immediately, so
        a user who started — but did not confirm — a second enrolment lost the
        only secret their authenticator knew and could never log in again
        (disable_mfa needs a valid code too). The guard must leave the stored
        secret untouched and refuse with a 4xx-worthy BusinessRuleError.
        """
        existing_secret = "JBSWY3DPEHPK3PXP"
        user = _make_user({"mfa_enabled": True, "mfa_secret": existing_secret})
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(BusinessRuleError, match="MFA is already enabled"):
            await auth_service.enroll_mfa(
                user_id=user.id,
                password="TestPass@123",
            )

        # The working secret survives and nothing was persisted.
        assert user.mfa_secret == existing_secret
        mock_user_repo.update.assert_not_called()


# ── PII / Logging Tests ────────────────────────────────────────────────────


class TestLogDiscriminator:
    """B3: raw email addresses never reach the logs."""

    async def test_discriminator_is_stable_and_non_reversible(self: Any, auth_service: Any) -> None:
        """Same email hashes identically; different emails differ."""
        assert auth_service._email_discriminator(
            "User@Example.com"
        ) == auth_service._email_discriminator("user@example.com")
        assert auth_service._email_discriminator("a@b.co") != auth_service._email_discriminator(
            "c@d.co"
        )

    async def test_discriminator_contains_no_email_parts(self: Any, auth_service: Any) -> None:
        """The hash prefix cannot leak any part of the address."""
        email = "patient.one@hospital.example"
        digest = auth_service._email_discriminator(email)

        assert len(digest) == 16
        assert all(c in "0123456789abcdef" for c in digest)
        assert "patient" not in digest
        assert "hospital" not in digest
