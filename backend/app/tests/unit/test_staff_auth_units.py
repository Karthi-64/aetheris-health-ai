"""Small, database-free checks for the P3 staff-authentication hardening."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pyotp
import pytest

from app.core.exceptions import AuthenticationError
from app.core.security import (
    create_access_token,
    create_mfa_ticket,
    encrypt_mfa_secret,
    hash_token,
    verify_access_token,
)
from app.models.auth_throttle import TrustedDevice
from app.models.user import User, UserStatus
from app.services import auth_service as auth_service_module
from app.services.auth_service import AuthService
from app.services.auth_throttle import Admission, Bucket, BucketKind, bucket
from app.tests.conftest import AdmitAllThrottle
from app.utils.validators import normalize_email

#: A legacy lock that would still be "in force" whenever the test happens to
#: run. Parametrised values are built at collection time, minutes before a
#: long run reaches them.
_FAR_FUTURE = datetime.now(UTC) + timedelta(days=365)

EMAIL = "a@hospital.example"
SOURCE = "93.184.216.34"
DEVICE_TOKEN = "d" * 43


def _service(**repos: Any) -> AuthService:
    return AuthService(
        user_repo=repos.get("user_repo", AsyncMock()),
        refresh_token_repo=repos.get("refresh_token_repo", AsyncMock()),
        password_reset_repo=repos.get("password_reset_repo", AsyncMock()),
        uow=repos.get("uow", AsyncMock()),
        audit=repos.get("audit", AsyncMock()),
        throttle=repos.get("throttle", AdmitAllThrottle()),
        trusted_devices=repos.get("trusted_devices", AsyncMock()),
    )


def _user(**overrides: Any) -> MagicMock:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.hospital_id = uuid.uuid4()
    user.status = UserStatus.ACTIVE
    user.locked_until = None
    user.hospital_is_active = True
    user.mfa_enabled = False
    user.mfa_secret = None
    user.failed_login_attempts = 0
    user.user_roles = []
    user.password_hash = "stored-hash"
    user.password_changed_at = datetime.now(UTC) - timedelta(days=30)
    user.email = EMAIL
    user.first_name = "A"
    user.last_name = "B"
    user.phone = None
    for name, value in overrides.items():
        setattr(user, name, value)
    return user


def _users(user: MagicMock | None) -> AsyncMock:
    """A user repository in which ``user`` is the account every lookup finds."""
    users = AsyncMock()
    users.get_by_email_cross_tenant.return_value = user
    users.get_by_id.return_value = user
    users.lock_for_authentication.return_value = user
    users.claim_authentication_attempt.return_value = True
    return users


def _device(user: MagicMock, *, mfa_verified: bool = False) -> MagicMock:
    """A trusted device of ``user``.

    :param mfa_verified: Whether this browser has itself completed the
        account's second factor before (``trusted_devices.mfa_verified_at``).
    """
    device = MagicMock(spec=TrustedDevice)
    device.id = uuid.uuid4()
    device.user_id = user.id
    device.created_at = datetime.now(UTC) - timedelta(days=1)
    device.mfa_verified_at = datetime.now(UTC) - timedelta(hours=1) if mfa_verified else None
    return device


def _ticket(user: MagicMock) -> str:
    """The MFA ticket a correct password step issues for ``user`` as it is right now.

    Bound to the account's current password hash, exactly as ``login`` binds
    it. A ticket from ``core.security.create_mfa_ticket`` carries no binding
    and is refused before anything else happens, so a test that used one
    would pass for the wrong reason.
    """
    return _service()._issue_mfa_ticket(user)  # noqa: SLF001


def _devices(device: MagicMock | None = None) -> AsyncMock:
    """A trusted-device store that recognises ``device`` (or nothing)."""
    devices = AsyncMock()
    devices.find.return_value = device
    devices.live_hashes.return_value = {hash_token(DEVICE_TOKEN)} if device is not None else set()
    return devices


def _kinds(buckets: list[Bucket]) -> list[BucketKind]:
    return [target.kind for target in buckets]


class _Work:
    """Counts credential evaluations, and decides their outcome."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, correct: bool) -> None:
        self.passwords: list[str] = []
        self.burns: list[str] = []
        self.codes: list[str] = []

        def _verify(password: str, _hashed: str) -> bool:
            self.passwords.append(password)
            return correct

        def _burn(password: str) -> None:
            self.burns.append(password)

        def _totp(_secret: str, code: str) -> bool:
            self.codes.append(code)
            return correct

        monkeypatch.setattr(auth_service_module, "verify_password", _verify)
        monkeypatch.setattr(auth_service_module, "burn_password_verification", _burn)
        monkeypatch.setattr(auth_service_module, "verify_totp_code", _totp)
        monkeypatch.setattr(auth_service_module, "password_needs_rehash", lambda _hash: False)

    @property
    def evaluations(self) -> int:
        return len(self.passwords) + len(self.burns) + len(self.codes)


class TestNormalizeEmail:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Asha@Hospital.in", "asha@hospital.in"),
            ("  asha@hospital.in  ", "asha@hospital.in"),
            ("ASHA.RAO+Clinic@HOSPITAL.IN", "asha.rao+clinic@hospital.in"),
            ("asha@hospital.in", "asha@hospital.in"),
        ],
    )
    def test_canonical_form(self, raw: str, expected: str) -> None:
        assert normalize_email(raw) == expected

    def test_it_is_idempotent(self) -> None:
        once = normalize_email("  MiXed@Case.Example ")
        assert normalize_email(once) == once

    def test_every_write_to_a_user_is_normalised(self) -> None:
        user = User(email="  MiXed@Case.Example ")
        assert user.email == "mixed@case.example"

        user.email = "CHANGED@Case.Example"
        assert user.email == "changed@case.example"


class TestUsableAccountIsAnAllowlist:
    """Only ACTIVE accounts in an active hospital may authenticate."""

    def test_an_active_account_is_usable(self) -> None:
        assert AuthService._rejection_reason(_user()) is None  # noqa: SLF001

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"status": UserStatus.SUSPENDED}, "suspended"),
            ({"status": UserStatus.INVITED}, "not_active"),
            ({"status": "some-future-state"}, "not_active"),
            ({"status": None}, "not_active"),
            ({"hospital_is_active": False}, "hospital_inactive"),
            ({"status": UserStatus.SUSPENDED, "hospital_is_active": False}, "suspended"),
        ],
    )
    def test_everything_else_is_refused(self, overrides: dict[str, Any], reason: str) -> None:
        assert AuthService._rejection_reason(_user(**overrides)) == reason  # noqa: SLF001

    @pytest.mark.parametrize(
        "legacy",
        [
            {"locked_until": _FAR_FUTURE},
            {"failed_login_attempts": 10_000},
            {"locked_until": _FAR_FUTURE, "failed_login_attempts": 10_000},
            {"locked_until": datetime.now(UTC) - timedelta(seconds=1)},
        ],
    )
    def test_there_is_no_locked_state(self, legacy: dict[str, Any]) -> None:
        """Attack: lock a colleague out by failing their password.

        "Locked" was a state anyone could put an account in. It is gone: the
        legacy columns are not consulted, so nothing an outsider does changes
        what an account *is*.
        """
        assert AuthService._rejection_reason(_user(**legacy)) is None  # noqa: SLF001

    def test_no_refusal_is_ever_reported_as_locked(self) -> None:
        assert "locked" not in auth_service_module._REJECTION_LOG_EVENTS  # noqa: SLF001
        assert not hasattr(AuthService, "_handle_failed_login")
        assert not hasattr(AuthService, "_may_reset_password")

    @pytest.mark.parametrize(
        ("status", "hospital_active", "allowed"),
        [
            (UserStatus.ACTIVE, True, True),
            (UserStatus.INVITED, True, False),
            (UserStatus.SUSPENDED, True, False),
            ("some-future-state", True, False),
            (None, True, False),
            (UserStatus.ACTIVE, False, False),
            (UserStatus.INVITED, False, False),
            (UserStatus.SUSPENDED, False, False),
        ],
    )
    def test_requesting_a_reset_link_is_for_active_accounts_only(
        self, status: Any, hospital_active: bool, allowed: bool
    ) -> None:
        """Attack: keep a lapsed invitation alive for ever through forgot-password.

        An anonymous request mints a credential only for an account that
        already has a password: ACTIVE, in an active hospital. An invited
        account gets its link from an administrator's invitation.
        """
        user = _user(status=status, hospital_is_active=hospital_active)
        assert AuthService._may_request_reset(user) is allowed  # noqa: SLF001

    @pytest.mark.parametrize(
        ("status", "hospital_active", "allowed"),
        [
            (UserStatus.ACTIVE, True, True),
            (UserStatus.INVITED, True, True),
            (UserStatus.SUSPENDED, True, False),
            ("some-future-state", True, False),
            (None, True, False),
            (UserStatus.ACTIVE, False, False),
            (UserStatus.INVITED, False, False),
            (UserStatus.SUSPENDED, False, False),
        ],
    )
    def test_redeeming_an_emailed_token_is_an_allowlist_too(
        self, status: Any, hospital_active: bool, allowed: bool
    ) -> None:
        """Redeeming is how an invitation is accepted, so INVITED is allowed — and nothing else new."""
        user = _user(status=status, hospital_is_active=hospital_active)
        assert AuthService._may_redeem_reset(user) is allowed  # noqa: SLF001

    def test_whoever_may_request_may_redeem_but_not_the_reverse(self) -> None:
        """The two lists differ by exactly one state: INVITED can redeem, never request."""
        states = [UserStatus.ACTIVE, UserStatus.INVITED, UserStatus.SUSPENDED, "unknown", None]
        only_redeem = []
        for status in states:
            for hospital_active in (True, False):
                user = _user(status=status, hospital_is_active=hospital_active)
                request = AuthService._may_request_reset(user)  # noqa: SLF001
                redeem = AuthService._may_redeem_reset(user)  # noqa: SLF001
                assert redeem or not request
                if redeem and not request:
                    only_redeem.append((status, hospital_active))

        assert only_redeem == [(UserStatus.INVITED, True)]


class TestTokenIssuanceIsTheLastLineOfDefence:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"status": UserStatus.SUSPENDED},
            {"status": UserStatus.INVITED},
            {"status": "some-future-state"},
            {"hospital_is_active": False},
        ],
    )
    async def test_no_session_is_minted_for_an_unusable_account(
        self, overrides: dict[str, Any]
    ) -> None:
        """Whatever path reaches ``_issue_tokens``, an unusable account gets nothing."""
        refresh_tokens = AsyncMock()
        service = _service(refresh_token_repo=refresh_tokens)

        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            await service._issue_tokens(_user(**overrides))  # noqa: SLF001

        refresh_tokens.create.assert_not_awaited()

    async def test_a_usable_account_still_gets_a_session(self) -> None:
        refresh_tokens = AsyncMock()
        service = _service(refresh_token_repo=refresh_tokens)

        result = await service._issue_tokens(_user())  # noqa: SLF001

        assert result["access_token"]
        assert result["refresh_token"]
        refresh_tokens.create.assert_awaited_once()

    async def test_a_legacy_lock_does_not_stop_a_session_being_issued(self) -> None:
        refresh_tokens = AsyncMock()
        service = _service(refresh_token_repo=refresh_tokens)

        result = await service._issue_tokens(_user(locked_until=_FAR_FUTURE))  # noqa: SLF001

        assert result["refresh_token"]


class TestTheAccountIsReReadBeforeASessionIsIssued:
    """Attack: sign in with a credential that stopped being valid a moment ago.

    The password is verified against a copy of the account read before the
    throttle's commits (and a deliberately slow hash). If the password was
    changed, or the account suspended, in between — by the owner reacting to
    a compromise, say — a session issued from the stale copy is exactly the
    one that "revoke every session" was meant to prevent, and has just missed.
    """

    @pytest.mark.parametrize(
        ("now", "why"),
        [
            ({"password_hash": "a-different-hash"}, "the password was changed"),
            ({"status": UserStatus.SUSPENDED}, "the account was suspended"),
            ({"status": UserStatus.INVITED}, "the account was returned to invited"),
            ({"hospital_is_active": False}, "the hospital was deactivated"),
            (None, "the account was deleted"),
        ],
    )
    async def test_login_is_refused_when_the_account_changed_under_it(
        self, monkeypatch: pytest.MonkeyPatch, now: dict[str, Any] | None, why: str
    ) -> None:
        _Work(monkeypatch, correct=True)
        seen = _user(password_hash="stored-hash")
        users = _users(seen)
        users.lock_for_authentication.return_value = (
            _user(**{"password_hash": "stored-hash", **now}) if now is not None else None
        )
        refresh_tokens, devices, uow = AsyncMock(), _devices(), AsyncMock()
        service = _service(
            user_repo=users, refresh_token_repo=refresh_tokens, trusted_devices=devices, uow=uow
        )

        with pytest.raises(AuthenticationError) as refused:
            await service.login(email=EMAIL, password="x", ip_address=SOURCE)

        assert str(refused.value) == "Invalid credentials.", why
        users.lock_for_authentication.assert_awaited_once_with(seen.id)
        refresh_tokens.create.assert_not_awaited()
        users.record_login.assert_not_awaited()
        devices.create.assert_not_awaited()
        # The row lock is released before the uniform-duration wait.
        assert uow.commit.await_count >= 1

    @pytest.mark.parametrize(
        ("now", "why"),
        [
            ({"password_hash": "a-different-hash"}, "the password was changed"),
            ({"status": UserStatus.SUSPENDED}, "the account was suspended"),
            ({"hospital_is_active": False}, "the hospital was deactivated"),
            (None, "the account was deleted"),
        ],
    )
    async def test_no_mfa_ticket_is_issued_when_the_account_changed_under_the_password_step(
        self, monkeypatch: pytest.MonkeyPatch, now: dict[str, Any] | None, why: str
    ) -> None:
        """Attack: keep signing in with the old password while the owner changes it.

        The ticket is bound to the hash the password was verified against. If
        that is no longer the account's hash by the time the ticket would be
        issued, a ticket built from the *new* hash would vouch for a password
        the caller never presented. So the row is re-read under lock first,
        and no ticket is issued.
        """
        _Work(monkeypatch, correct=True)
        seen = _user(password_hash="stored-hash", mfa_enabled=True, mfa_secret="stored")
        users = _users(seen)
        users.lock_for_authentication.return_value = (
            _user(**{"password_hash": "stored-hash", "mfa_enabled": True, **now})
            if now is not None
            else None
        )
        service = _service(user_repo=users, trusted_devices=_devices())

        with pytest.raises(AuthenticationError) as refused:
            await service.login(email=EMAIL, password="x", ip_address=SOURCE)

        assert str(refused.value) == "Invalid credentials.", why
        users.lock_for_authentication.assert_awaited_once_with(seen.id)
        users.record_login.assert_not_awaited()

    async def test_a_password_alone_does_not_sign_in_once_mfa_was_switched_on_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: race the owner switching MFA on, and get a session with the password only."""
        _Work(monkeypatch, correct=True)
        seen = _user(password_hash="stored-hash", mfa_enabled=False)
        users = _users(seen)
        users.lock_for_authentication.return_value = _user(
            password_hash="stored-hash", mfa_enabled=True, mfa_secret="stored"
        )
        refresh_tokens, devices = AsyncMock(), _devices()
        service = _service(
            user_repo=users, refresh_token_repo=refresh_tokens, trusted_devices=devices
        )

        with pytest.raises(AuthenticationError) as refused:
            await service.login(email=EMAIL, password="x", ip_address=SOURCE)

        assert str(refused.value) == "Invalid credentials."
        refresh_tokens.create.assert_not_awaited()
        users.record_login.assert_not_awaited()
        devices.create.assert_not_awaited()

    async def test_the_session_is_issued_for_the_row_as_it_is_now(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Roles revoked a moment ago must not ride into the token on a stale copy."""
        _Work(monkeypatch, correct=True)
        seen = _user(password_hash="stored-hash", first_name="Stale")
        current = _user(password_hash="stored-hash", first_name="Current")
        current.id = seen.id
        users = _users(seen)
        users.lock_for_authentication.return_value = current
        service = _service(user_repo=users, trusted_devices=_devices())

        result = await service.login(email=EMAIL, password="x")

        assert result["user"]["first_name"] == "Current"
        users.record_login.assert_awaited_once_with(current)

    @pytest.mark.parametrize(
        ("now", "why"),
        [
            ({"mfa_secret": "a-different-secret"}, "the second factor was replaced"),
            ({"mfa_enabled": False}, "MFA was switched off"),
            ({"password_hash": "a-different-hash"}, "the password was changed"),
            ({"status": UserStatus.SUSPENDED}, "the account was suspended"),
            (None, "the account was deleted"),
        ],
    )
    async def test_mfa_is_refused_when_the_account_changed_under_it(
        self, monkeypatch: pytest.MonkeyPatch, now: dict[str, Any] | None, why: str
    ) -> None:
        _Work(monkeypatch, correct=True)
        stored = encrypt_mfa_secret(pyotp.random_base32())
        seen = _user(mfa_enabled=True, mfa_secret=stored)
        users = _users(seen)
        users.lock_for_authentication.return_value = (
            _user(**{"mfa_enabled": True, "mfa_secret": stored, **now}) if now is not None else None
        )
        refresh_tokens, throttle = AsyncMock(), AdmitAllThrottle()
        service = _service(
            user_repo=users,
            refresh_token_repo=refresh_tokens,
            trusted_devices=_devices(),
            throttle=throttle,
        )

        with pytest.raises(AuthenticationError) as refused:
            await service.verify_mfa(_ticket(seen), "123456", ip_address=SOURCE)

        assert str(refused.value) == "Invalid MFA code.", why
        # The refusal came from the locking re-read — after the code was
        # accepted — and not from anything earlier (a stale ticket, say).
        assert len(throttle.admitted) == 1, why
        users.lock_for_authentication.assert_awaited_once_with(seen.id)
        refresh_tokens.create.assert_not_awaited()
        users.record_login.assert_not_awaited()

    async def test_the_reread_itself_checks_every_condition(self) -> None:
        """Directly: unchanged passes and returns the fresh row; anything else raises."""
        seen = _user(password_hash="h", mfa_enabled=True, mfa_secret="s")
        fresh = _user(password_hash="h", mfa_enabled=True, mfa_secret="s")
        users = _users(seen)
        users.lock_for_authentication.return_value = fresh
        service = _service(user_repo=users)

        assert await service._reread_for_issue(seen, password_hash="h") is fresh  # noqa: SLF001
        assert await service._reread_for_issue(seen, mfa_secret="s") is fresh  # noqa: SLF001

        for kwargs in ({"password_hash": "other"}, {"mfa_secret": "other"}):
            with pytest.raises(AuthenticationError, match="Invalid credentials"):
                await service._reread_for_issue(seen, **kwargs)  # noqa: SLF001

        fresh.mfa_enabled = False
        with pytest.raises(AuthenticationError, match="Invalid MFA code"):
            await service._reread_for_issue(  # noqa: SLF001
                seen, mfa_secret="s", message="Invalid MFA code."
            )


class TestARefusedAdmissionEvaluatesNothing:
    """Attack: keep guessing after the allowance is spent.

    When the throttle says no, the credential must not be looked at — not the
    real hash, not the dummy one, not a TOTP code — and the answer must be the
    one a wrong credential gets.
    """

    @pytest.mark.parametrize("account_exists", [True, False])
    async def test_a_refused_login_never_touches_a_password_hash(
        self, monkeypatch: pytest.MonkeyPatch, account_exists: bool
    ) -> None:
        work = _Work(monkeypatch, correct=True)
        throttle = AdmitAllThrottle()
        throttle.refuse = True
        users = _users(_user() if account_exists else None)
        refresh_tokens, audit = AsyncMock(), AsyncMock()
        service = _service(
            user_repo=users, throttle=throttle, refresh_token_repo=refresh_tokens, audit=audit
        )

        with pytest.raises(AuthenticationError) as refused:
            await service.login(email=EMAIL, password="the-right-password", ip_address=SOURCE)

        assert str(refused.value) == "Invalid credentials."
        assert work.evaluations == 0
        assert throttle.settled == []
        users.lock_for_authentication.assert_not_awaited()
        users.record_login.assert_not_awaited()
        refresh_tokens.create.assert_not_awaited()
        audit.record.assert_not_awaited()

    async def test_a_refusal_is_the_same_error_as_a_wrong_password_and_an_unknown_email(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _Work(monkeypatch, correct=False)
        outcomes: list[tuple[type[Exception], str, Any]] = []
        for refuse, user in ((True, _user()), (False, _user()), (False, None), (True, None)):
            throttle = AdmitAllThrottle()
            throttle.refuse = refuse
            service = _service(user_repo=_users(user), throttle=throttle)
            with pytest.raises(AuthenticationError) as raised:
                await service.login(email=EMAIL, password="x", ip_address=SOURCE)
            outcomes.append((type(raised.value), str(raised.value), raised.value.__cause__))

        assert len(set(outcomes)) == 1
        assert outcomes[0][1] == "Invalid credentials."

    async def test_a_refused_mfa_attempt_never_checks_the_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = _Work(monkeypatch, correct=True)
        throttle = AdmitAllThrottle()
        throttle.refuse = True
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        users, refresh_tokens = _users(user), AsyncMock()
        service = _service(user_repo=users, throttle=throttle, refresh_token_repo=refresh_tokens)

        with pytest.raises(AuthenticationError) as refused:
            await service.verify_mfa(_ticket(user), "123456", ip_address=SOURCE)

        assert str(refused.value) == "Invalid MFA code."
        # It was the throttle that said no: the ticket itself was good.
        assert len(throttle.admitted) == 1
        assert work.evaluations == 0
        assert throttle.settled == []
        refresh_tokens.create.assert_not_awaited()

    async def test_a_refused_login_still_waits_out_the_uniform_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: tell "refused unevaluated" from "wrong password" by how fast it comes back."""
        from app.core.config import settings

        _Work(monkeypatch, correct=False)
        monkeypatch.setattr(settings, "AUTH_FAILURE_MIN_SECONDS", 0.5)
        waits: list[float] = []

        async def _record(seconds: float) -> None:
            waits.append(seconds)

        monkeypatch.setattr(auth_service_module, "_sleep", _record)
        throttle = AdmitAllThrottle()
        throttle.refuse = True
        uow = AsyncMock()
        service = _service(user_repo=_users(_user()), throttle=throttle, uow=uow)

        with pytest.raises(AuthenticationError):
            await service.login(email=EMAIL, password="x", ip_address=SOURCE)

        assert len(waits) == 1
        assert 0.4 < waits[0] <= 0.5 * 1.2
        # Nothing is held while waiting: the transaction was ended first.
        assert uow.commit.await_count >= 1


class TestWhichBucketsAnAttemptIsChargedTo:
    """Attack: choose your own counters — or drain somebody else's."""

    async def test_an_unrecognised_attempt_draws_on_account_source_and_pair(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _Work(monkeypatch, correct=False)
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(_user()), throttle=throttle)

        with pytest.raises(AuthenticationError):
            await service.login(email=f"  {EMAIL.upper()} ", password="x", ip_address=SOURCE)

        assert throttle.admitted == [
            [
                bucket(BucketKind.PW_ACCOUNT, EMAIL),
                bucket(BucketKind.PW_SOURCE, SOURCE),
                bucket(BucketKind.PW_PAIR, EMAIL, SOURCE),
            ]
        ]

    async def test_the_buckets_do_not_depend_on_whether_the_account_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: enumerate staff emails by which addresses get throttled, and how."""
        _Work(monkeypatch, correct=False)
        asked: list[list[Bucket]] = []
        for user in (_user(), None):
            throttle = AdmitAllThrottle()
            service = _service(user_repo=_users(user), throttle=throttle)
            with pytest.raises(AuthenticationError):
                await service.login(email=EMAIL, password="x", ip_address=SOURCE)
            asked.extend(throttle.admitted)

        assert asked[0] == asked[1]

    async def test_every_spelling_of_an_address_is_one_account_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: vary the case of the email to get a fresh allowance per spelling."""
        _Work(monkeypatch, correct=False)
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(_user()), throttle=throttle)

        for spelling in (EMAIL, EMAIL.upper(), f" {EMAIL}\t", EMAIL.title()):
            with pytest.raises(AuthenticationError):
                await service.login(email=spelling, password="x", ip_address=SOURCE)

        assert all(asked == throttle.admitted[0] for asked in throttle.admitted)

    async def test_an_ipv6_caller_is_charged_by_its_slash_64(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: a new address from the same /64 for every guess."""
        _Work(monkeypatch, correct=False)
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(_user()), throttle=throttle)

        for host in (":1", ":2", "dead:beef:0:1"):
            with pytest.raises(AuthenticationError):
                await service.login(email=EMAIL, password="x", ip_address=f"2001:db8:1:2:{host}")

        assert all(asked == throttle.admitted[0] for asked in throttle.admitted)
        assert bucket(BucketKind.PW_SOURCE, "2001:db8:1:2::/64") in throttle.admitted[0]

    @pytest.mark.parametrize("no_address", [None, "", "unknown", "not-an-address", "testclient"])
    async def test_an_attempt_with_no_source_is_still_charged_to_the_account(
        self, monkeypatch: pytest.MonkeyPatch, no_address: str | None
    ) -> None:
        """Hiding where an attempt came from must not make it free.

        With no usable address — none, or text that is not an address — there
        is no source bucket to charge, and certainly not one shared by every
        such caller. The account's shared budget is charged all the same.
        """
        _Work(monkeypatch, correct=False)
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(_user()), throttle=throttle)

        with pytest.raises(AuthenticationError):
            await service.login(email=EMAIL, password="x", ip_address=no_address)

        assert throttle.admitted == [[bucket(BucketKind.PW_ACCOUNT, EMAIL)]]

    async def test_a_trusted_device_draws_only_on_its_own_buckets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner's browser is untouched by whatever outsiders do to the shared budget."""
        _Work(monkeypatch, correct=False)
        user = _user()
        device = _device(user)
        devices, throttle = _devices(device), AdmitAllThrottle()
        service = _service(user_repo=_users(user), throttle=throttle, trusted_devices=devices)

        with pytest.raises(AuthenticationError):
            await service.login(
                email=EMAIL, password="x", ip_address=SOURCE, device_tokens=(DEVICE_TOKEN,)
            )

        assert throttle.admitted == [
            [
                bucket(BucketKind.PW_DEVICE, device.id),
                bucket(BucketKind.PW_DEVICE_CAP, device.id),
            ]
        ]
        # Recognition is by the hash of the token, for THIS account only.
        user_id, hashes = devices.find.await_args.args
        assert user_id == user.id
        assert hashes == [hash_token(DEVICE_TOKEN)]

    async def test_the_device_bucket_is_named_by_the_row_never_by_the_token(self) -> None:
        """Attack: mint fresh buckets by presenting fresh (or altered) cookie values."""
        user = _user()
        device = _device(user)

        named = AuthService._password_buckets(EMAIL, SOURCE, device)  # noqa: SLF001

        assert named == [
            bucket(BucketKind.PW_DEVICE, device.id),
            bucket(BucketKind.PW_DEVICE_CAP, device.id),
        ]
        assert bucket(BucketKind.PW_DEVICE, DEVICE_TOKEN) not in named
        assert bucket(BucketKind.PW_DEVICE, hash_token(DEVICE_TOKEN)) not in named

    async def test_a_token_that_is_nobodys_device_gets_the_ordinary_buckets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: present a made-up device cookie to escape the shared account budget."""
        _Work(monkeypatch, correct=False)
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(_user()), throttle=throttle, trusted_devices=_devices())

        with pytest.raises(AuthenticationError):
            await service.login(
                email=EMAIL, password="x", ip_address=SOURCE, device_tokens=("f" * 43,)
            )

        assert _kinds(throttle.admitted[0]) == [
            BucketKind.PW_ACCOUNT,
            BucketKind.PW_SOURCE,
            BucketKind.PW_PAIR,
        ]

    async def test_an_unknown_email_is_looked_up_against_no_ones_devices(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same query runs for an unknown address, against an id that matches no one."""
        _Work(monkeypatch, correct=False)
        devices = _devices()
        service = _service(user_repo=_users(None), trusted_devices=devices)

        with pytest.raises(AuthenticationError):
            await service.login(email=EMAIL, password="x", device_tokens=(DEVICE_TOKEN,))

        devices.find.assert_awaited_once()
        assert isinstance(devices.find.await_args.args[0], uuid.UUID)

    async def test_mfa_codes_are_charged_to_the_origin_and_to_the_whole_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: guess codes from many addresses; the account budget is shared by all of them."""
        _Work(monkeypatch, correct=False)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(user), throttle=throttle)

        for source in (SOURCE, "151.101.1.69"):
            with pytest.raises(AuthenticationError, match="Invalid MFA code"):
                await service.verify_mfa(_ticket(user), "000000", ip_address=source)

        first, second = throttle.admitted
        assert first == [
            bucket(BucketKind.MFA_ORIGIN, user.id, "source", SOURCE),
            bucket(BucketKind.MFA_ACCOUNT, user.id),
        ]
        assert first[0] != second[0]
        assert first[1] == second[1]
        assert throttle.settled == []

    async def test_a_sourceless_mfa_attempt_shares_the_unknown_origin(self) -> None:
        user = _user()

        origin = AuthService._mfa_origin(user, None, None)  # noqa: SLF001

        assert origin == bucket(BucketKind.MFA_ORIGIN, user.id, "source", "unknown")


class TestWhatACorrectCredentialGivesBack:
    """Attack: turn a success into a fresh allowance — for yourself or for someone else."""

    async def test_with_mfa_pending_the_password_charge_is_refunded_and_nothing_is_cleared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: a stolen password, used to wipe a device's backoff before the second factor.

        The right password proves one factor. The charge it took is given
        back; no backoff is cleared until the code has been verified too.
        """
        _Work(monkeypatch, correct=True)
        user = _user(mfa_enabled=True, mfa_secret="stored")
        device = _device(user)
        throttle, refresh_tokens, devices = AdmitAllThrottle(), AsyncMock(), _devices(device)
        service = _service(
            user_repo=_users(user),
            throttle=throttle,
            trusted_devices=devices,
            refresh_token_repo=refresh_tokens,
        )

        result = await service.login(email=EMAIL, password="x", device_tokens=(DEVICE_TOKEN,))

        assert set(result) == {"mfa_ticket", "expires_in"}
        assert len(throttle.settled) == 1
        admission, cleared = throttle.settled[0]
        assert isinstance(admission, Admission)
        assert admission.admitted is True
        assert cleared == []
        # No session and no device trust at the password step.
        refresh_tokens.create.assert_not_awaited()
        devices.create.assert_not_awaited()
        devices.touch.assert_not_awaited()
        assert "device_cookie" not in result
        # The ticket is tied to the password it vouches for.
        claims = verify_access_token(result["mfa_ticket"])
        assert claims["type"] == "mfa_ticket"
        assert claims["sub"] == str(user.id)
        assert claims["pwb"] == AuthService._password_binding(user)  # noqa: SLF001

    async def test_a_full_success_from_a_trusted_device_clears_that_devices_backoff_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _Work(monkeypatch, correct=True)
        user = _user()
        device = _device(user)
        throttle, devices = AdmitAllThrottle(), _devices(device)
        service = _service(user_repo=_users(user), throttle=throttle, trusted_devices=devices)

        result = await service.login(
            email=EMAIL, password="x", ip_address=SOURCE, device_tokens=(DEVICE_TOKEN,)
        )

        assert result["access_token"]
        [(admission, cleared)] = throttle.settled
        assert admission.admitted is True
        assert cleared == [bucket(BucketKind.PW_DEVICE, device.id)]
        # Not the ceiling, and nothing shared.
        assert bucket(BucketKind.PW_DEVICE_CAP, device.id) not in cleared
        # An already-trusted browser keeps its token; no new one is minted.
        assert result["device_cookie"] == DEVICE_TOKEN
        devices.create.assert_not_awaited()
        devices.touch.assert_awaited_once()

    async def test_an_unrecognised_success_clears_nothing_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: guess from the hospital's shared address and let a colleague's login reset you.

        A source may be a whole hospital behind one address. A success from it
        refunds its own charge and clears no backoff — not the pair, not the
        source, not the account.
        """
        _Work(monkeypatch, correct=True)
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(_user()), throttle=throttle, trusted_devices=_devices())

        await service.login(email=EMAIL, password="x", ip_address=SOURCE)

        [(admission, cleared)] = throttle.settled
        assert admission.admitted is True
        assert cleared == []

    async def test_a_wrong_password_is_never_settled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _Work(monkeypatch, correct=False)
        user = _user()
        throttle = AdmitAllThrottle()
        service = _service(
            user_repo=_users(user), throttle=throttle, trusted_devices=_devices(_device(user))
        )

        with pytest.raises(AuthenticationError):
            await service.login(email=EMAIL, password="x", device_tokens=(DEVICE_TOKEN,))

        assert len(throttle.admitted) == 1
        assert throttle.settled == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"status": UserStatus.SUSPENDED},
            {"status": UserStatus.INVITED},
            {"hospital_is_active": False},
        ],
    )
    async def test_an_attempt_on_an_unusable_account_keeps_its_charge(
        self, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
    ) -> None:
        """Even the right password on a suspended account is a failed attempt, and costs one."""
        work = _Work(monkeypatch, correct=True)
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(_user(**overrides)), throttle=throttle)

        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            await service.login(email=EMAIL, password="x", ip_address=SOURCE)

        assert throttle.settled == []
        # The time of one password check is spent, on the dummy hash.
        assert (len(work.passwords), len(work.burns)) == (0, 1)

    @pytest.mark.parametrize("mfa_verified", [True, False])
    async def test_both_factors_from_a_trusted_device_clear_that_devices_backoffs_only(
        self, monkeypatch: pytest.MonkeyPatch, mfa_verified: bool
    ) -> None:
        """A device's own backoffs — code step and password step — and never a budget."""
        _Work(monkeypatch, correct=True)
        stored = encrypt_mfa_secret(pyotp.random_base32())
        user = _user(mfa_enabled=True, mfa_secret=stored)
        device = _device(user, mfa_verified=mfa_verified)
        throttle = AdmitAllThrottle()
        service = _service(
            user_repo=_users(user), throttle=throttle, trusted_devices=_devices(device)
        )

        result = await service.verify_mfa(
            _ticket(user), "123456", ip_address=SOURCE, device_tokens=(DEVICE_TOKEN,)
        )

        assert result["access_token"]
        origin = bucket(BucketKind.MFA_ORIGIN, user.id, "device", device.id)
        [(_, cleared)] = throttle.settled
        assert cleared == [origin, bucket(BucketKind.PW_DEVICE, device.id)]
        # Neither code budget is ever among the things a success clears.
        assert all(
            target.kind not in (BucketKind.MFA_ACCOUNT, BucketKind.MFA_DEVICE_CAP)
            for target in cleared
        )

    async def test_both_factors_from_an_unrecognised_origin_clear_nothing_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: guess codes from the hospital's shared address; let a colleague's sign-in reset you.

        The origin's code backoff is keyed by the source, and a source may be
        a whole hospital behind one address. A completed sign-in from it gets
        its own charge back and clears no backoff — exactly as at the
        password step.
        """
        _Work(monkeypatch, correct=True)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(user), throttle=throttle, trusted_devices=_devices())

        result = await service.verify_mfa(_ticket(user), "123456", ip_address=SOURCE)

        assert result["access_token"]
        assert throttle.admitted == [
            [
                bucket(BucketKind.MFA_ORIGIN, user.id, "source", SOURCE),
                bucket(BucketKind.MFA_ACCOUNT, user.id),
            ]
        ]
        [(admission, cleared)] = throttle.settled
        assert admission.admitted is True
        assert cleared == []

    async def test_a_made_up_device_cookie_does_not_turn_a_success_into_a_clear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: present any cookie at all to be treated as a device and have a backoff wiped."""
        _Work(monkeypatch, correct=True)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(user), throttle=throttle, trusted_devices=_devices())

        await service.verify_mfa(
            _ticket(user), "123456", ip_address=SOURCE, device_tokens=("f" * 43,)
        )

        [(_, cleared)] = throttle.settled
        assert cleared == []

    async def test_no_success_ever_names_a_shared_budget_to_clear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Across every success path: only backoff buckets are ever asked to be cleared."""
        from app.services.auth_throttle import Backoff

        _Work(monkeypatch, correct=True)
        stored = encrypt_mfa_secret(pyotp.random_base32())
        user = _user(mfa_enabled=True, mfa_secret=stored)
        plain = _user()
        throttle = AdmitAllThrottle()
        verified = _service(
            user_repo=_users(user),
            throttle=throttle,
            trusted_devices=_devices(_device(user, mfa_verified=True)),
        )
        unverified = _service(
            user_repo=_users(user), throttle=throttle, trusted_devices=_devices(_device(user))
        )
        simple = _service(
            user_repo=_users(plain), throttle=throttle, trusted_devices=_devices(_device(plain))
        )

        # Tickets come from the password step itself, as a browser gets them.
        first = await verified.login(email=EMAIL, password="x", device_tokens=(DEVICE_TOKEN,))
        await verified.verify_mfa(first["mfa_ticket"], "1", device_tokens=(DEVICE_TOKEN,))
        second = await unverified.login(email=EMAIL, password="x", device_tokens=(DEVICE_TOKEN,))
        await unverified.verify_mfa(second["mfa_ticket"], "1", device_tokens=(DEVICE_TOKEN,))
        third = await verified.login(email=EMAIL, password="x", ip_address=SOURCE)
        await verified.verify_mfa(third["mfa_ticket"], "1", ip_address=SOURCE)
        await simple.login(email=EMAIL, password="x", device_tokens=(DEVICE_TOKEN,))
        await simple.login(email=EMAIL, password="x", ip_address=SOURCE)
        await simple.change_password(plain.id, "x", "Str0ng!Passw0rd123")
        await verified.disable_mfa(user.id, "x", "1")

        assert len(throttle.settled) == 11
        cleared = [target for _, targets in throttle.settled for target in targets]
        assert cleared
        assert all(isinstance(target.policy, Backoff) for target in cleared)
        assert {target.kind for target in cleared} == {
            BucketKind.PW_DEVICE,
            BucketKind.MFA_ORIGIN,
            BucketKind.SESSION_PW,
            BucketKind.SESSION_CODE,
        }


class TestWhichBudgetASecondFactorCodeDrawsOn:
    """Attack: with only the stolen password, spend the code budget and keep the owner out.

    Reaching the code step takes the password and nothing else. If every code
    drew on one budget for the account, whoever had the password could drain
    it for as long as they liked. So a browser that has *itself* passed this
    account's second factor has a budget of its own; everything else shares
    the account's.
    """

    def test_a_device_that_passed_the_second_factor_has_its_own_budget(self) -> None:
        user = _user()
        device = _device(user, mfa_verified=True)

        chosen = AuthService._mfa_budget(user, device)  # noqa: SLF001

        assert chosen == bucket(BucketKind.MFA_DEVICE_CAP, device.id)
        assert chosen != bucket(BucketKind.MFA_ACCOUNT, user.id)

    def test_a_device_that_never_passed_it_shares_the_accounts(self) -> None:
        """Attack: become "a device" by an emailed reset, a refresh or a password-only sign-in.

        Trust won without the second factor buys no code budget: such a
        device is charged to the shared one, like any stranger.
        """
        user = _user()
        device = _device(user, mfa_verified=False)

        assert AuthService._mfa_budget(user, device) == bucket(  # noqa: SLF001
            BucketKind.MFA_ACCOUNT, user.id
        )

    def test_no_device_at_all_shares_the_accounts(self) -> None:
        user = _user()

        assert AuthService._mfa_budget(user, None) == bucket(  # noqa: SLF001
            BucketKind.MFA_ACCOUNT, user.id
        )

    def test_the_device_budget_is_named_by_the_row_and_is_one_per_device(self) -> None:
        """Attack: mint a fresh code budget by presenting a different cookie value."""
        user = _user()
        first, second = _device(user, mfa_verified=True), _device(user, mfa_verified=True)

        named = AuthService._mfa_budget(user, first)  # noqa: SLF001

        assert named != AuthService._mfa_budget(user, second)  # noqa: SLF001
        assert named != bucket(BucketKind.MFA_DEVICE_CAP, user.id)
        assert named != bucket(BucketKind.MFA_DEVICE_CAP, DEVICE_TOKEN)
        assert named != bucket(BucketKind.MFA_DEVICE_CAP, hash_token(DEVICE_TOKEN))
        # And it is a budget — a ceiling no success can clear — not a backoff.
        from app.services.auth_throttle import Budget

        assert isinstance(named.policy, Budget)

    async def test_the_owners_verified_device_never_touches_the_shared_code_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So an attacker who has drained ``mfa_account`` has not touched this browser."""
        _Work(monkeypatch, correct=False)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        device = _device(user, mfa_verified=True)
        throttle = AdmitAllThrottle()
        service = _service(
            user_repo=_users(user), throttle=throttle, trusted_devices=_devices(device)
        )

        with pytest.raises(AuthenticationError, match="Invalid MFA code"):
            await service.verify_mfa(
                _ticket(user), "000000", ip_address=SOURCE, device_tokens=(DEVICE_TOKEN,)
            )

        assert throttle.admitted == [
            [
                bucket(BucketKind.MFA_ORIGIN, user.id, "device", device.id),
                bucket(BucketKind.MFA_DEVICE_CAP, device.id),
            ]
        ]
        assert bucket(BucketKind.MFA_ACCOUNT, user.id) not in throttle.admitted[0]

    async def test_an_unverified_device_is_charged_to_the_shared_code_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: reset the password by email again and again to look like a new device each time."""
        _Work(monkeypatch, correct=False)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        device = _device(user, mfa_verified=False)
        throttle = AdmitAllThrottle()
        service = _service(
            user_repo=_users(user), throttle=throttle, trusted_devices=_devices(device)
        )

        with pytest.raises(AuthenticationError, match="Invalid MFA code"):
            await service.verify_mfa(
                _ticket(user), "000000", ip_address=SOURCE, device_tokens=(DEVICE_TOKEN,)
            )

        assert throttle.admitted == [
            [
                bucket(BucketKind.MFA_ORIGIN, user.id, "device", device.id),
                bucket(BucketKind.MFA_ACCOUNT, user.id),
            ]
        ]

    @pytest.mark.parametrize("recognised", [True, False])
    async def test_only_a_completed_second_factor_marks_a_device_as_having_passed_it(
        self, monkeypatch: pytest.MonkeyPatch, recognised: bool
    ) -> None:
        _Work(monkeypatch, correct=True)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        device = _device(user) if recognised else None
        devices = _devices(device)
        service = _service(user_repo=_users(user), trusted_devices=devices)

        await service.verify_mfa(
            _ticket(user),
            "123456",
            ip_address=SOURCE,
            device_tokens=(DEVICE_TOKEN,) if recognised else (),
        )

        recorded = devices.touch if recognised else devices.create
        recorded.assert_awaited_once()
        assert recorded.await_args.kwargs["mfa_verified"] is True

    @pytest.mark.parametrize("recognised", [True, False])
    async def test_a_password_only_sign_in_does_not(
        self, monkeypatch: pytest.MonkeyPatch, recognised: bool
    ) -> None:
        """An account without MFA today may switch it on tomorrow; this browser proved nothing about it."""
        _Work(monkeypatch, correct=True)
        user = _user()
        device = _device(user) if recognised else None
        devices = _devices(device)
        service = _service(user_repo=_users(user), trusted_devices=devices)

        await service.login(
            email=EMAIL,
            password="x",
            ip_address=SOURCE,
            device_tokens=(DEVICE_TOKEN,) if recognised else (),
        )

        recorded = devices.touch if recognised else devices.create
        recorded.assert_awaited_once()
        assert recorded.await_args.kwargs["mfa_verified"] is False

    async def test_a_password_change_does_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _Work(monkeypatch, correct=True)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        devices = _devices(_device(user, mfa_verified=True))
        service = _service(user_repo=_users(user), trusted_devices=devices)

        await service.change_password(
            user.id, "x", "Str0ng!Passw0rd123", device_tokens=(DEVICE_TOKEN,)
        )

        # The old trust is replaced by a fresh row that has passed nothing.
        devices.delete_tokens.assert_awaited_once_with(user.id, [hash_token(DEVICE_TOKEN)])
        assert devices.create.await_args.kwargs["mfa_verified"] is False
        devices.touch.assert_not_awaited()

    @pytest.mark.parametrize("presented", [(), (DEVICE_TOKEN,)])
    async def test_a_refreshed_session_never_makes_a_browser_a_device(
        self, presented: tuple[str, ...]
    ) -> None:
        """Attack: with one stolen session, mint device after device — each with its own allowance.

        A refresh is not a credential check. It creates no trusted device,
        touches none, marks none as having passed the second factor, and
        hands back no device cookie.
        """
        user = _user(mfa_enabled=True, mfa_secret="stored")
        stored = MagicMock()
        stored.id = uuid.uuid4()
        stored.user_id = user.id
        stored.is_revoked = False
        stored.is_expired = False
        refresh_tokens, devices = AsyncMock(), _devices(_device(user))
        refresh_tokens.get_by_token_hash.return_value = stored
        refresh_tokens.create.return_value = MagicMock(id=uuid.uuid4())
        service = _service(
            user_repo=_users(user), refresh_token_repo=refresh_tokens, trusted_devices=devices
        )

        result = await service.refresh_token("a-refresh-token", device_tokens=presented)

        assert result["access_token"]
        assert result["device_cookie"] is None
        devices.create.assert_not_awaited()
        devices.touch.assert_not_awaited()
        devices.prune.assert_not_awaited()

    @pytest.mark.parametrize("operation", ["confirm_mfa", "disable_mfa"])
    async def test_changing_the_second_factor_forgets_which_devices_passed_the_old_one(
        self, monkeypatch: pytest.MonkeyPatch, operation: str
    ) -> None:
        """Attack: keep a private code budget for a second factor the device never passed.

        What a browser proved about the old authenticator says nothing about
        the new one (or about none at all).
        """
        _Work(monkeypatch, correct=True)
        user = _user(
            mfa_enabled=operation == "disable_mfa",
            mfa_secret=encrypt_mfa_secret(pyotp.random_base32()),
        )
        devices = _devices()
        service = _service(user_repo=_users(user), trusted_devices=devices)

        if operation == "confirm_mfa":
            await service.confirm_mfa(user.id, "123456")
        else:
            await service.disable_mfa(user.id, "the-password", "123456")

        devices.forget_mfa_for_user.assert_awaited_once_with(user.id)
        # The devices themselves stay trusted for the password step.
        devices.delete_for_user.assert_not_awaited()

    @pytest.mark.parametrize("operation", ["confirm_mfa", "disable_mfa"])
    async def test_a_wrong_code_forgets_nothing(
        self, monkeypatch: pytest.MonkeyPatch, operation: str
    ) -> None:
        """Attack: from a stolen session, strip the owner's devices of their budget with a bad code."""
        _Work(monkeypatch, correct=False)
        monkeypatch.setattr(auth_service_module, "verify_password", lambda *_: True)
        user = _user(
            mfa_enabled=operation == "disable_mfa",
            mfa_secret=encrypt_mfa_secret(pyotp.random_base32()),
        )
        devices = _devices()
        service = _service(user_repo=_users(user), trusted_devices=devices)

        with pytest.raises(AuthenticationError):
            if operation == "confirm_mfa":
                await service.confirm_mfa(user.id, "000000")
            else:
                await service.disable_mfa(user.id, "the-password", "000000")

        devices.forget_mfa_for_user.assert_not_awaited()


class TestTheSourceAnAttemptIsCountedUnder:
    """Attack: arrive with no usable address and share — or dodge — a source bucket."""

    @pytest.mark.parametrize(
        "nothing",
        [None, "", "   ", "unknown", "garbage", "testclient", "1.2.3", "999.1.1.1", "evil.example"],
    )
    def test_no_address_or_a_non_address_is_no_source(self, nothing: str | None) -> None:
        """Never the literal ``unknown``: that would be one bucket for every such caller."""
        assert AuthService._usable_source(nothing) is None  # noqa: SLF001

    @pytest.mark.parametrize(
        ("address", "source"),
        [
            ("93.184.216.34", "93.184.216.34"),
            (" 93.184.216.34 ", "93.184.216.34"),
            ("::ffff:93.184.216.34", "93.184.216.34"),
            ("2001:db8:1:2:dead:beef:0:1", "2001:db8:1:2::/64"),
            ("[2001:db8:1:2::9]:443", "2001:db8:1:2::/64"),
            ("10.0.0.5", "10.0.0.5"),
        ],
    )
    def test_a_real_address_is_its_source(self, address: str, source: str) -> None:
        assert AuthService._usable_source(address) == source  # noqa: SLF001

    def test_an_unusable_source_leaves_the_source_buckets_out_entirely(self) -> None:
        """Without a source: the account's budget, and no bucket named for "nobody"."""
        named = AuthService._password_buckets(  # noqa: SLF001
            EMAIL,
            AuthService._usable_source("not-an-address"),  # noqa: SLF001
            None,
        )

        assert named == [bucket(BucketKind.PW_ACCOUNT, EMAIL)]
        assert bucket(BucketKind.PW_SOURCE, "unknown") not in named
        assert bucket(BucketKind.PW_PAIR, EMAIL, "unknown") not in named


class TestStaleMfaTickets:
    """Attack: keep a ticket obtained with a password that is no longer the password.

    A ticket says "the password was right a moment ago". It is bound to the
    stored password hash it was issued for, so it dies the instant that hash
    is replaced — by the owner, by an emailed reset or by an administrator —
    with no window in which the old one still works.
    """

    @staticmethod
    async def _apply(instance: Any, **changes: Any) -> Any:
        for name, value in changes.items():
            setattr(instance, name, value)
        return instance

    def _stale_setup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[_Work, MagicMock, AsyncMock, AsyncMock, AdmitAllThrottle]:
        work = _Work(monkeypatch, correct=True)
        user = _user(
            mfa_enabled=True,
            mfa_secret=encrypt_mfa_secret(pyotp.random_base32()),
            password_hash="old-hash",
        )
        users = _users(user)
        users.update.side_effect = self._apply
        return work, user, users, AsyncMock(), AdmitAllThrottle()

    async def _assert_dead(
        self,
        service: AuthService,
        ticket: str,
        work: _Work,
        throttle: AdmitAllThrottle,
        refresh_tokens: AsyncMock,
    ) -> None:
        """The ticket is refused generically, before any code is charged or checked."""
        refresh_tokens.reset_mock()
        throttle.admitted.clear()
        throttle.settled.clear()
        codes_before = len(work.codes)

        with pytest.raises(AuthenticationError) as refused:
            await service.verify_mfa(ticket, "123456", ip_address=SOURCE)

        assert str(refused.value) == "Invalid MFA code."
        assert len(work.codes) == codes_before
        assert throttle.admitted == []
        assert throttle.settled == []
        refresh_tokens.create.assert_not_awaited()

    @pytest.mark.parametrize(
        "changed_at",
        [
            "unchanged",
            None,
            datetime.now(UTC) - timedelta(days=30),
            datetime.now(UTC) - timedelta(seconds=1),
            datetime.now(UTC) + timedelta(seconds=5),
        ],
    )
    async def test_a_ticket_dies_the_instant_the_password_hash_is_replaced(
        self, monkeypatch: pytest.MonkeyPatch, changed_at: Any
    ) -> None:
        """Attack: hold a ticket from the stolen password while the owner changes it.

        Only the hash decides. Whatever ``password_changed_at`` says — not
        touched, cleared (an administrator's reset), in the past, the same
        second as the ticket — the ticket for the old hash is dead.
        """
        work, user, users, refresh_tokens, throttle = self._stale_setup(monkeypatch)
        service = _service(user_repo=users, throttle=throttle, refresh_token_repo=refresh_tokens)
        ticket = _ticket(user)

        user.password_hash = "new-hash"
        if changed_at != "unchanged":
            user.password_changed_at = changed_at

        await self._assert_dead(service, ticket, work, throttle, refresh_tokens)

    async def test_a_ticket_issued_before_a_self_service_change_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work, user, users, refresh_tokens, throttle = self._stale_setup(monkeypatch)
        service = _service(
            user_repo=users,
            throttle=throttle,
            refresh_token_repo=refresh_tokens,
            trusted_devices=_devices(),
        )
        ticket = (await service.login(email=EMAIL, password="x", ip_address=SOURCE))["mfa_ticket"]

        await service.change_password(user.id, "x", "Str0ng!Passw0rd123")
        assert user.password_hash != "old-hash"

        await self._assert_dead(service, ticket, work, throttle, refresh_tokens)

    async def test_a_ticket_issued_before_an_emailed_reset_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner notices the compromise and resets by email; the thief's ticket dies too."""
        work, user, users, refresh_tokens, throttle = self._stale_setup(monkeypatch)
        tokens = AsyncMock()
        emailed = MagicMock()
        emailed.user_id = user.id
        tokens.get_valid_token.return_value = emailed
        tokens.consume.return_value = emailed
        service = _service(
            user_repo=users,
            throttle=throttle,
            refresh_token_repo=refresh_tokens,
            password_reset_repo=tokens,
            trusted_devices=_devices(),
        )
        ticket = (await service.login(email=EMAIL, password="x", ip_address=SOURCE))["mfa_ticket"]

        await service.reset_password("emailed-token", "Str0ng!Passw0rd123")
        assert user.password_hash != "old-hash"

        await self._assert_dead(service, ticket, work, throttle, refresh_tokens)

    async def test_a_ticket_issued_before_an_administrators_reset_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: hold a ticket from the stolen password while an admin resets the account.

        An administrator resets a password because the account is in doubt.
        The reset replaces the hash, clears ``password_changed_at`` and
        revokes sessions — and a ticket issued minutes earlier for the *old*
        password dies with it.
        """
        work, user, users, refresh_tokens, throttle = self._stale_setup(monkeypatch)
        service = _service(
            user_repo=users,
            throttle=throttle,
            refresh_token_repo=refresh_tokens,
            trusted_devices=_devices(),
        )
        ticket = (await service.login(email=EMAIL, password="x", ip_address=SOURCE))["mfa_ticket"]

        await service.admin_reset_password(user.id, actor_hospital_id=user.hospital_id)
        assert user.password_hash != "old-hash"
        assert user.password_changed_at is None

        await self._assert_dead(service, ticket, work, throttle, refresh_tokens)

    @pytest.mark.parametrize(
        "changed_at", [None, datetime.now(UTC) - timedelta(seconds=30), datetime.now(UTC)]
    )
    async def test_a_ticket_for_the_current_password_works(
        self, monkeypatch: pytest.MonkeyPatch, changed_at: datetime | None
    ) -> None:
        """Including on an account an administrator reset (``password_changed_at`` cleared)."""
        _Work(monkeypatch, correct=True)
        stored = encrypt_mfa_secret(pyotp.random_base32())
        user = _user(mfa_enabled=True, mfa_secret=stored)
        user.password_changed_at = changed_at
        service = _service(user_repo=_users(user), trusted_devices=_devices())

        result = await service.verify_mfa(_ticket(user), "123456", ip_address=SOURCE)

        assert result["access_token"]

    async def test_a_ticket_with_no_binding_at_all_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: a ticket from any code path that forgot to bind it — fail closed.

        ``core.security.create_mfa_ticket`` signs a perfectly valid ticket
        with no ``pwb`` claim. It must open nothing.
        """
        work, user, users, refresh_tokens, throttle = self._stale_setup(monkeypatch)
        service = _service(user_repo=users, throttle=throttle, refresh_token_repo=refresh_tokens)

        await self._assert_dead(service, create_mfa_ticket(user.id), work, throttle, refresh_tokens)

    @pytest.mark.parametrize(
        "binding",
        ["", "0" * 32, "old-hash", None, 0, True, ["x"], {"pwb": "x"}],
    )
    async def test_a_ticket_with_a_wrong_or_malformed_binding_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, binding: Any
    ) -> None:
        """Whatever sits in the claim — empty, the raw hash, the wrong type — it is not a match."""
        work, user, users, refresh_tokens, throttle = self._stale_setup(monkeypatch)
        service = _service(user_repo=users, throttle=throttle, refresh_token_repo=refresh_tokens)
        ticket = create_access_token(
            user_id=user.id,
            hospital_id=None,
            extra_claims={"type": "mfa_ticket", "purpose": "mfa_verification", "pwb": binding},
        )

        await self._assert_dead(service, ticket, work, throttle, refresh_tokens)

    async def test_one_accounts_binding_does_not_open_another(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work, user, users, refresh_tokens, throttle = self._stale_setup(monkeypatch)
        other = _user(password_hash="somebody-elses-hash")
        service = _service(user_repo=users, throttle=throttle, refresh_token_repo=refresh_tokens)
        ticket = create_access_token(
            user_id=user.id,
            hospital_id=None,
            extra_claims={
                "type": "mfa_ticket",
                "purpose": "mfa_verification",
                "pwb": AuthService._password_binding(other),  # noqa: SLF001
            },
        )

        await self._assert_dead(service, ticket, work, throttle, refresh_tokens)

    def test_the_binding_follows_the_hash_and_gives_nothing_of_it_away(self) -> None:
        """The ticket is handed to the browser: it must not carry the password hash."""
        user = _user(password_hash="$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo")
        binding = AuthService._password_binding(user)  # noqa: SLF001

        assert len(binding) == 32
        assert set(binding) <= set("0123456789abcdef")
        assert binding == AuthService._password_binding(user)  # noqa: SLF001
        assert binding != AuthService._password_binding(  # noqa: SLF001
            _user(password_hash=user.password_hash + "x")
        )
        ticket = _ticket(user)
        claims = verify_access_token(ticket)
        assert claims["pwb"] == binding
        for part in (user.password_hash, "c2FsdHNhbHQ", "aGFzaGhhc2hoYXNo", "argon2"):
            assert part not in repr(claims)
        # Five minutes and no more.
        assert 0 < claims["exp"] - claims["iat"] <= 300

    @pytest.mark.parametrize(
        "overrides",
        [{"mfa_enabled": False}, {"mfa_secret": None}, {"mfa_enabled": False, "mfa_secret": None}],
    )
    async def test_a_ticket_for_an_account_with_mfa_now_off_charges_nothing(
        self, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
    ) -> None:
        """Attack: replay a ticket after MFA was switched off, to burn the account's code budget.

        There is no code to guess, so nothing is charged — to the origin or
        to any budget — and nothing is checked. The ticket is otherwise
        perfectly good (right account, right password).
        """
        work = _Work(monkeypatch, correct=True)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        ticket = _ticket(user)
        for name, value in overrides.items():
            setattr(user, name, value)
        throttle, refresh_tokens = AdmitAllThrottle(), AsyncMock()
        service = _service(
            user_repo=_users(user),
            throttle=throttle,
            refresh_token_repo=refresh_tokens,
            trusted_devices=_devices(_device(user, mfa_verified=True)),
        )

        with pytest.raises(AuthenticationError) as refused:
            await service.verify_mfa(
                ticket, "123456", ip_address=SOURCE, device_tokens=(DEVICE_TOKEN,)
            )

        assert str(refused.value) == "Invalid MFA code."
        assert throttle.admitted == []
        assert throttle.settled == []
        assert work.evaluations == 0
        refresh_tokens.create.assert_not_awaited()

    @pytest.mark.parametrize(
        "ticket",
        ["", "not-a-jwt", "a.b.c"],
    )
    async def test_a_forged_ticket_reaches_no_account_and_no_bucket(self, ticket: str) -> None:
        """Attack: charge someone's MFA budget (or probe for accounts) without a real ticket."""
        users, throttle = AsyncMock(), AdmitAllThrottle()
        service = _service(user_repo=users, throttle=throttle)

        with pytest.raises(AuthenticationError):
            await service.verify_mfa(ticket, "123456", ip_address=SOURCE)

        users.get_by_id.assert_not_awaited()
        assert throttle.admitted == []

    async def test_an_access_token_is_not_an_mfa_ticket(self) -> None:
        """Attack: present your own access token as a ticket for your own account."""
        user = _user(mfa_enabled=True, mfa_secret="stored")
        users, throttle = _users(user), AdmitAllThrottle()
        service = _service(user_repo=users, throttle=throttle)
        token = create_access_token(user_id=user.id, hospital_id=user.hospital_id)

        with pytest.raises(AuthenticationError):
            await service.verify_mfa(token, "123456")

        assert throttle.admitted == []


class TestInSessionReAuthentication:
    """Attack: use a stolen session as an unlimited oracle for the password or the code.

    Changing a password and switching MFA on or off ask for the current
    password (and code) again. Those checks go through the throttle, and a
    throttled check answers exactly as a wrong one does.
    """

    NEW_PASSWORD = "Str0ng!Passw0rd123"

    async def _refusal(self, call: Any) -> tuple[type[Exception], str]:
        with pytest.raises(AuthenticationError) as raised:
            await call
        return type(raised.value), str(raised.value)

    @pytest.mark.parametrize(
        ("operation", "message"),
        [
            ("change_password", "Current password is incorrect."),
            ("enroll_mfa", "Invalid password."),
            ("disable_mfa", "Invalid password."),
        ],
    )
    async def test_a_password_recheck_is_throttled_and_says_the_same_as_a_wrong_one(
        self, monkeypatch: pytest.MonkeyPatch, operation: str, message: str
    ) -> None:
        user = _user(mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))

        def _call(service: AuthService) -> Any:
            if operation == "change_password":
                return service.change_password(user.id, "guess", self.NEW_PASSWORD)
            if operation == "enroll_mfa":
                return service.enroll_mfa(user.id, "guess")
            return service.disable_mfa(user.id, "guess", "123456")

        # Throttled: the right password, never evaluated.
        work = _Work(monkeypatch, correct=True)
        refusing = AdmitAllThrottle()
        refusing.refuse = True
        users = _users(user)
        throttled = await self._refusal(_call(_service(user_repo=users, throttle=refusing)))
        assert work.evaluations == 0
        assert refusing.admitted == [[bucket(BucketKind.SESSION_PW, user.id)]]
        assert refusing.settled == []
        users.update.assert_not_awaited()

        # Wrong: evaluated once, charged, not settled.
        work = _Work(monkeypatch, correct=False)
        admitting = AdmitAllThrottle()
        users = _users(user)
        wrong = await self._refusal(_call(_service(user_repo=users, throttle=admitting)))
        assert work.passwords == ["guess"]
        assert admitting.admitted == [[bucket(BucketKind.SESSION_PW, user.id)]]
        assert admitting.settled == []
        users.update.assert_not_awaited()

        assert throttled == wrong == (AuthenticationError, message)

    @pytest.mark.parametrize(
        ("operation", "message"),
        [
            ("confirm_mfa", "Invalid MFA code. Please try again."),
            ("disable_mfa", "Invalid MFA code."),
        ],
    )
    async def test_a_code_recheck_is_throttled_and_says_the_same_as_a_wrong_one(
        self, monkeypatch: pytest.MonkeyPatch, operation: str, message: str
    ) -> None:
        user = _user(
            mfa_enabled=operation == "disable_mfa",
            mfa_secret=encrypt_mfa_secret(pyotp.random_base32()),
        )
        code_buckets = [
            bucket(BucketKind.SESSION_CODE, user.id),
            bucket(BucketKind.MFA_ACCOUNT, user.id),
        ]

        class _RefuseCodes(AdmitAllThrottle):
            """Admits the password re-check and refuses the code check."""

            async def admit(self, targets: Any) -> Any:
                self.refuse = any(t.kind is BucketKind.SESSION_CODE for t in targets)
                return await super().admit(targets)

        def _call(service: AuthService) -> Any:
            if operation == "confirm_mfa":
                return service.confirm_mfa(user.id, "000000")
            return service.disable_mfa(user.id, "the-password", "000000")

        # Throttled: no code is checked, even though it would have been right.
        work = _Work(monkeypatch, correct=True)
        refusing = _RefuseCodes()
        users = _users(user)
        throttled = await self._refusal(_call(_service(user_repo=users, throttle=refusing)))
        assert work.codes == []
        assert refusing.admitted[-1] == code_buckets
        users.update.assert_not_awaited()

        # Wrong: checked once; both charges stay.
        work = _Work(monkeypatch, correct=False)
        monkeypatch.setattr(auth_service_module, "verify_password", lambda *_: True)
        admitting = AdmitAllThrottle()
        users = _users(user)
        wrong = await self._refusal(_call(_service(user_repo=users, throttle=admitting)))
        assert work.codes == ["000000"]
        assert admitting.admitted[-1] == code_buckets
        assert all(
            BucketKind.SESSION_CODE not in _kinds(cleared) for _, cleared in admitting.settled
        )
        users.update.assert_not_awaited()

        assert throttled == wrong == (AuthenticationError, message)

    async def test_code_guesses_in_a_session_draw_on_the_sign_in_code_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: guess the TOTP code through disable-MFA to dodge the sign-in limit.

        The account-wide second-factor bucket is literally the same bucket at
        both doors.
        """
        _Work(monkeypatch, correct=False)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        throttle = AdmitAllThrottle()
        service = _service(user_repo=_users(user), throttle=throttle)

        with pytest.raises(AuthenticationError):
            await service._check_session_code(  # noqa: SLF001
                user, pyotp.random_base32(), "000000", message="Invalid MFA code."
            )
        with pytest.raises(AuthenticationError):
            await service.verify_mfa(_ticket(user), "000000", ip_address=SOURCE)

        in_session, at_sign_in = throttle.admitted
        shared = bucket(BucketKind.MFA_ACCOUNT, user.id)
        assert shared in in_session
        assert shared in at_sign_in

    async def test_a_correct_recheck_clears_only_its_own_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _Work(monkeypatch, correct=True)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        throttle = AdmitAllThrottle()
        users = _users(user)
        service = _service(user_repo=users, throttle=throttle, trusted_devices=_devices())

        await service.disable_mfa(user.id, "the-password", "123456")

        assert [cleared for _, cleared in throttle.settled] == [
            [bucket(BucketKind.SESSION_PW, user.id)],
            [bucket(BucketKind.SESSION_CODE, user.id)],
        ]
        users.update.assert_awaited_once_with(user, mfa_secret=None, mfa_enabled=False)

    async def test_one_users_session_checks_never_touch_another_users_buckets(self) -> None:
        first, second = _user(), _user()

        assert bucket(BucketKind.SESSION_PW, first.id) != bucket(BucketKind.SESSION_PW, second.id)
        assert bucket(BucketKind.SESSION_CODE, first.id) != bucket(
            BucketKind.SESSION_CODE, second.id
        )


class TestSignInTakesNoExclusiveClaim:
    """Attack: keep an account's owner out by holding its sign-in exclusively.

    The per-email advisory claim and the ``FOR UPDATE`` on the users row made
    sign-in exclusive per address: a stream of slow attempts from an attacker
    refused or stalled the owner's. Sign-in now takes neither; the row is
    locked only by someone who has already proved the credential.
    """

    @pytest.mark.parametrize("claim_answer", [True, False, None])
    async def test_login_does_not_ask_for_or_depend_on_the_claim(
        self, monkeypatch: pytest.MonkeyPatch, claim_answer: bool | None
    ) -> None:
        _Work(monkeypatch, correct=True)
        users = _users(_user())
        users.claim_authentication_attempt.return_value = claim_answer
        service = _service(user_repo=users, trusted_devices=_devices())

        result = await service.login(email=EMAIL, password="x", ip_address=SOURCE)

        assert result["access_token"]
        users.claim_authentication_attempt.assert_not_awaited()

    async def test_login_reads_the_account_without_locking_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _Work(monkeypatch, correct=False)
        users = _users(_user())
        service = _service(user_repo=users)

        with pytest.raises(AuthenticationError):
            await service.login(email="  Someone@Hospital.Example ", password="x")

        users.get_by_email_cross_tenant.assert_awaited_once_with(
            "someone@hospital.example", for_update=False
        )
        # A wrong password never reaches the locking re-read.
        users.lock_for_authentication.assert_not_awaited()

    async def test_an_unknown_email_locks_nothing_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = _Work(monkeypatch, correct=False)
        users = _users(None)
        service = _service(user_repo=users)

        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            await service.login(email=EMAIL, password="x")

        users.lock_for_authentication.assert_not_awaited()
        users.claim_authentication_attempt.assert_not_awaited()
        # ...and still spends the time of one password check.
        assert work.burns == ["x"]

    async def test_the_mfa_step_takes_no_claim_and_locks_only_after_the_code_is_right(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = _Work(monkeypatch, correct=False)
        user = _user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(pyotp.random_base32()))
        users = _users(user)
        service = _service(user_repo=users)

        with pytest.raises(AuthenticationError, match="Invalid MFA code"):
            await service.verify_mfa(_ticket(user), "000000", ip_address=SOURCE)

        # The code really was judged (and found wrong) — with no lock held.
        assert work.codes == ["000000"]
        users.claim_authentication_attempt.assert_not_awaited()
        users.lock_for_authentication.assert_not_awaited()


class TestOneResetRequestPerAddressAtATime:
    """The claim that remains: forgot-password only."""

    @pytest.mark.parametrize("answer", [False, None, 0, 1, "t"])
    async def test_anything_but_a_definite_claim_does_nothing(self, answer: Any) -> None:
        """Fail closed: only an explicit ``True`` lets a reset request proceed."""
        users, tokens = AsyncMock(), AsyncMock()
        users.claim_authentication_attempt.return_value = answer
        service = _service(user_repo=users, password_reset_repo=tokens)

        await service.forgot_password(EMAIL)

        users.get_by_email_cross_tenant.assert_not_awaited()
        tokens.create.assert_not_awaited()

    async def test_a_reset_request_does_nothing_while_another_is_in_flight(self) -> None:
        users, tokens = AsyncMock(), AsyncMock()
        users.claim_authentication_attempt.return_value = False
        service = _service(user_repo=users, password_reset_repo=tokens)

        await service.forgot_password("a@hospital.example")

        users.get_by_email_cross_tenant.assert_not_awaited()
        tokens.create.assert_not_awaited()

    def test_the_claim_key_depends_only_on_the_canonical_address(self) -> None:
        from app.repositories.user_repository import UserRepository

        key = UserRepository.authentication_claim_key

        assert key("Asha@Hospital.Example") == key("  asha@hospital.example ")
        assert key("asha@hospital.example") != key("ravi@hospital.example")
        assert -(2**63) <= key("asha@hospital.example") < 2**63
