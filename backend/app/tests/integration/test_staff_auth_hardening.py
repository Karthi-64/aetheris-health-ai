"""Staff authentication boundaries — attacks that must no longer work (P3).

Each class replays one finding of the P3 audit against the real application
and a real PostgreSQL, and asserts the attack fails closed:

- F1  one email, two hospitals: login broke, and any admin could cause it
- F3  a rejected login answered faster than a wrong password
- F4  MFA codes could be guessed without limit — now bounded by the
      authentication throttle (``app/services/auth_throttle.py``), which slows
      guessing down and never locks an account
- F5  the MFA step did not re-check the account
- F6  a soft-deleted user still received tokens
- F7  a deactivated hospital kept authenticating
- F8  email case created duplicate identities and failed logins
- an oversized password answered 500 for real accounts only

(F2, the signing key, is in ``app/tests/unit/core/test_secret_key_config.py``.)

Throttle time is never slept through: :class:`_Clock` moves the one clock the
throttle reads (the database's, via ``AuthThrottleRepository.now``).
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pyotp
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.api.dependencies.db import get_db_session
from app.core import security as security_module
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_mfa_ticket,
    encrypt_mfa_secret,
    generate_opaque_token,
    generate_totp_secret,
    hash_password,
)
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.auth_throttle import AuthThrottleBucket, TrustedDevice
from app.models.hospital import Hospital
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserStatus
from app.repositories import user_repository as user_repository_module
from app.repositories.auth_throttle_repository import AuthThrottleRepository
from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import MAX_PASSWORD_INPUT_LENGTH
from app.services import auth_service as auth_service_module
from app.services.auth_service import AuthService
from app.services.auth_throttle import POLICIES, Backoff, Bucket, BucketKind, Budget, bucket
from app.services.notification_service import NotificationService
from app.tests.ai_fakes import RecordingLogger
from app.tests.conftest import grant_permissions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.notifications import NotificationRequest

pytestmark = pytest.mark.database

PASSWORD = "Str0ng!Passw0rd123"  # noqa: S105 — a test credential, not a real one
WRONG_PASSWORD = "Wr0ng!Passw0rd999"  # noqa: S105
AUTH = "/api/v1/auth"
USERS = "/api/v1/users"
GENERIC_LOGIN_FAILURE = {
    "success": False,
    "message": "Invalid credentials.",
    "error_code": "AUTHENTICATION_REQUIRED",
    "errors": None,
}
GENERIC_MFA_FAILURE = {**GENERIC_LOGIN_FAILURE, "message": "Invalid MFA code."}
FORGOT_RESPONSE = "If an account with that email exists, a password reset link has been sent."
NEW_PASSWORD = "An0ther!Passw0rd456"  # noqa: S105 — a test credential

#: The throttle's own numbers, read from the policy rather than repeated here.
MFA_ORIGIN = cast("Backoff", POLICIES[BucketKind.MFA_ORIGIN])
MFA_ACCOUNT = cast("Budget", POLICIES[BucketKind.MFA_ACCOUNT])
MFA_DEVICE_CAP = cast("Budget", POLICIES[BucketKind.MFA_DEVICE_CAP])
PW_PAIR = cast("Backoff", POLICIES[BucketKind.PW_PAIR])

#: Addresses "other people" connect from (documentation ranges, never routed).
SOURCE_B = "203.0.113.20"
SOURCE_C = "203.0.113.30"
SOURCE_D = "198.51.100.40"
SOURCE_E = "198.51.100.50"
LOCAL_SOURCE = "127.0.0.1"  # where the ``api`` client connects from


# ── Helpers ──────────────────────────────────────────────────────────────────


def _body(response: Response) -> dict[str, Any]:
    """The response envelope without its per-request metadata."""
    payload: dict[str, Any] = response.json()
    payload.pop("metadata", None)
    return payload


def _email(prefix: str = "staff") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@hospital.example"


async def _make_user(
    session: AsyncSession, hospital_id: uuid.UUID, *, email: str | None = None, **overrides: Any
) -> User:
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=email or _email(),
        password_hash=hash_password(PASSWORD),
        first_name="Auth",
        last_name="Tester",
        password_changed_at=datetime.now(UTC),
        **overrides,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def _make_mfa_user(session: AsyncSession, hospital_id: uuid.UUID) -> tuple[User, str]:
    secret = generate_totp_secret()
    user = await _make_user(
        session, hospital_id, mfa_enabled=True, mfa_secret=encrypt_mfa_secret(secret)
    )
    return user, secret


async def _make_admin(session: AsyncSession, hospital_id: uuid.UUID) -> User:
    user = await _make_user(session, hospital_id, email=_email("admin"))
    await grant_permissions(
        session, hospital_id=hospital_id, user_id=user.id, codes=["user.create", "user.read"]
    )
    return user


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(user_id=user.id, hospital_id=user.hospital_id)
    return {"Authorization": f"Bearer {token}"}


async def _login(api: AsyncClient, email: str, password: str = PASSWORD) -> Response:
    return await api.post(f"{AUTH}/login", json={"email": email, "password": password})


async def _ticket(api: AsyncClient, user: User, password: str = PASSWORD) -> str:
    """Pass the password step for real and return the ticket it issues."""
    response = await _login(api, user.email, password)
    assert response.status_code == 200, response.text
    ticket: str = response.json()["data"]["mfa_ticket"]
    return ticket


async def _sign_in(
    browser: AsyncClient, user: User, secret: str, password: str = PASSWORD
) -> Response:
    """Both steps, with the right password and the right code."""
    return await _verify(browser, await _ticket(browser, user, password), await _code(secret))


async def _verify(api: AsyncClient, ticket: str, code: str) -> Response:
    return await api.post(f"{AUTH}/mfa/verify", json={"mfa_ticket": ticket, "code": code})


async def _code(secret: str) -> str:
    """The code that is right at this moment.

    A code is good for one 30-second step. If the step is about to end, wait
    for the next one, so a request made with the code cannot straddle the
    boundary. (This is the authenticator's clock, not the throttle's.)
    """
    totp = pyotp.TOTP(secret)
    left = totp.interval - time.time() % totp.interval
    if left < 1.5:
        await asyncio.sleep(left + 0.05)
    return totp.now()


def _wrong_code(secret: str) -> str:
    """A code that is wrong now and in the steps either side of now."""
    totp = pyotp.TOTP(secret)
    near = {totp.at(int(time.time()) + offset) for offset in (-30, 0, 30, 60)}
    return next(
        code for code in ("000000", "111111", "222222", "333333", "444444") if code not in near
    )


async def _set_hospital_active(session: AsyncSession, hospital_id: uuid.UUID, active: bool) -> None:
    hospital = await session.get(Hospital, hospital_id)
    assert hospital is not None
    hospital.is_active = active
    await session.flush()


async def _count(session: AsyncSession, model: Any, **filters: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    return int((await session.execute(stmt)).scalar_one())


async def _audit_actions(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        select(AuditLog.action).where(AuditLog.target_id == user_id).order_by(AuditLog.created_at)
    )
    return list(rows.scalars())


async def _failure_reasons(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        select(AuditLog.context)
        .where(AuditLog.target_id == user_id, AuditLog.action == "auth.login.failed")
        .order_by(AuditLog.created_at)
    )
    return [str((context or {}).get("reason")) for context in rows.scalars()]


async def _fresh(session: AsyncSession, user: User) -> User:
    await session.refresh(user)
    return user


async def _saved(session: AsyncSession) -> None:
    """Commit what the test has set up, as production would have long before.

    Some refusals roll the request's transaction back. Rows a test has only
    flushed would go with it; in production they were committed by whichever
    earlier request wrote them.
    """
    await session.commit()


async def _reset_token(session: AsyncSession, user: User, *, minutes: int = 30) -> str:
    """Give ``user`` a valid emailed-link token and return its raw value."""
    raw, token_hash = generate_opaque_token()
    await PasswordResetTokenRepository(session).create(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
    )
    await _saved(session)
    return raw


@pytest.fixture(autouse=True)
def mail_transport_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs as a deployment that can send email.

    Forgot-password mints nothing without a mail transport, so with none
    configured every "no token was issued" assertion below would pass for the
    wrong reason. The host is never contacted: a request only queues the
    email, and nothing here drains the queue.
    """
    monkeypatch.setattr(settings, "SMTP_HOST", "mail.invalid")


async def _bucket_row(session: AsyncSession, target: Bucket) -> AuthThrottleBucket | None:
    result = await session.execute(
        select(AuthThrottleBucket)
        .where(AuthThrottleBucket.key_hash == target.key_hash)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _failures(session: AsyncSession, target: Bucket) -> int:
    """Attempts a backoff bucket is currently holding against its origin."""
    row = await _bucket_row(session, target)
    return 0 if row is None else row.failures


async def _budget_used(
    session: AsyncSession, target: Bucket, *, clock: _Clock | None = None
) -> int:
    """How many attempts a budget bucket is currently short of full."""
    row = await _bucket_row(session, target)
    if row is None or row.drains_at is None:
        return 0
    now: datetime = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    if clock is not None:
        now += clock.offset
    refill = cast("Budget", target.policy).refill
    return max(0, math.ceil((row.drains_at - now) / refill))


def _mfa_origin(user: User, ip: str) -> Bucket:
    """The backoff bucket for codes sent for ``user`` from an unrecognised source."""
    return AuthService._mfa_origin(user, ip, None)  # noqa: SLF001


def _mfa_account(user: User) -> Bucket:
    return bucket(BucketKind.MFA_ACCOUNT, user.id)


def _mfa_device_cap(device: TrustedDevice) -> Bucket:
    """The second-factor budget of one device that has passed the second factor."""
    return bucket(BucketKind.MFA_DEVICE_CAP, device.id)


async def _devices(session: AsyncSession, user_id: uuid.UUID) -> list[TrustedDevice]:
    """An account's trusted devices as they are stored now, oldest first."""
    rows = await session.execute(
        select(TrustedDevice)
        .where(TrustedDevice.user_id == user_id)
        .order_by(TrustedDevice.created_at, TrustedDevice.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


async def _only_device(session: AsyncSession, user_id: uuid.UUID) -> TrustedDevice:
    (device,) = await _devices(session, user_id)
    return device


async def _spend_shared_mfa_budget(
    application: FastAPI, user: User, secret: str, password: str = PASSWORD
) -> None:
    """What somebody holding only the password can do to the second factor.

    Wrong codes from as many addresses as it takes, never enough from any one
    of them to make that address wait, until the account's shared allowance
    is gone. Every one of those codes is evaluated.
    """
    per_source = MFA_ORIGIN.free - 1
    sent = 0
    for index in range(math.ceil(MFA_ACCOUNT.burst / per_source)):
        async with _browser(application, f"203.0.113.{100 + index}") as attacker:
            ticket = await _ticket(attacker, user, password)
            for _ in range(min(per_source, MFA_ACCOUNT.burst - sent)):
                response = await _verify(attacker, ticket, _wrong_code(secret))
                assert _body(response) == GENERIC_MFA_FAILURE
                sent += 1
    assert sent == MFA_ACCOUNT.burst


class _Clock:
    """Moves the throttle's clock. Nothing in these tests sleeps through a wait."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.offset = timedelta(0)
        real_now = AuthThrottleRepository.now
        clock = self

        async def _now(repository: AuthThrottleRepository) -> datetime:
            return await real_now(repository) + clock.offset

        monkeypatch.setattr(AuthThrottleRepository, "now", _now)

    def advance(self, delta: timedelta) -> None:
        self.offset += delta


class _CodeChecks:
    """Counts the second-factor codes that were actually evaluated."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real_check = security_module.verify_totp_code

        def _check(secret: str, code: str) -> bool:
            self.count += 1
            return real_check(secret, code)

        monkeypatch.setattr(auth_service_module, "verify_totp_code", _check)


@pytest.fixture(autouse=True)
def only_the_throttle_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the per-address request limit (60 a minute) for this module.

    It is a separate control with its own tests. Lifted, whatever refuses an
    attempt here is the authentication throttle and nothing else — it has to
    hold on its own.
    """
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 100_000)


@pytest_asyncio.fixture
async def application(db_session: AsyncSession) -> AsyncGenerator[FastAPI]:
    """The real application, on the test's rolled-back session."""
    instance = create_app()

    async def _override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    instance.dependency_overrides[get_db_session] = _override
    yield instance
    instance.dependency_overrides.clear()


@pytest_asyncio.fixture
async def api(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    """A browser connecting from ``LOCAL_SOURCE``. It keeps cookies, as a browser does."""
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client


@asynccontextmanager
async def _browser(application: FastAPI, ip: str) -> AsyncIterator[AsyncClient]:
    """Another browser, with no cookies, connecting from ``ip``."""
    transport = ASGITransport(app=application, client=(ip, 40_000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def user(db_session: AsyncSession, hospital_id: uuid.UUID) -> User:
    """An active staff member of hospital A."""
    return await _make_user(db_session, hospital_id)


class _Work:
    """Counts password-hash verifications made while serving a request.

    ``auth_service`` hands these calls to its hashing pool (``_off_loop``) and
    looks the functions up by name when it does, so the spies go on the names
    ``auth_service`` uses. Each call also notes which thread it ran on.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.real = 0
        self.dummy = 0
        self.threads: set[str] = set()
        real_verify = security_module.verify_password
        real_burn = security_module.burn_password_verification

        def _verify(password: str, hashed: str) -> bool:
            self.real += 1
            self.threads.add(threading.current_thread().name)
            return real_verify(password, hashed)

        def _burn(password: str) -> None:
            self.dummy += 1
            self.threads.add(threading.current_thread().name)
            real_burn(password)

        monkeypatch.setattr(auth_service_module, "verify_password", _verify)
        monkeypatch.setattr(auth_service_module, "burn_password_verification", _burn)

    def reset(self) -> None:
        self.real = self.dummy = 0
        self.threads.clear()

    @property
    def total(self) -> int:
        return self.real + self.dummy


# ── F1: one email must name exactly one account ──────────────────────────────


class TestOneEmailOneAccount:
    async def test_the_database_refuses_the_same_email_in_a_second_hospital(
        self, db_session: AsyncSession, user: User, other_hospital_id: uuid.UUID
    ) -> None:
        with pytest.raises(IntegrityError, match="uq_users_email_normalized_active"):
            await _make_user(db_session, other_hospital_id, email=user.email)
        await db_session.rollback()

    async def test_the_database_refuses_a_case_variant_in_a_second_hospital(
        self, db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
    ) -> None:
        """Checked with raw SQL, so the application's own lower-casing cannot help."""
        for index, hid in enumerate((hospital_id, other_hospital_id)):
            statement = text(
                "INSERT INTO users (id, hospital_id, email, password_hash, first_name, last_name) "
                "VALUES (:id, :hid, :email, 'x', 'A', 'B')"
            )
            params = {
                "id": uuid.uuid4(),
                "hid": hid,
                "email": "Case.Twin@hospital.example" if index else "case.twin@hospital.example",
            }
            if index == 0:
                await db_session.execute(statement, params)
            else:
                with pytest.raises(IntegrityError, match="uq_users_email_normalized_active"):
                    await db_session.execute(statement, params)
        await db_session.rollback()

    async def test_an_admin_cannot_invite_another_hospitals_email(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """The original attack: hospital B's admin invites hospital A's address."""
        admin_b = await _make_admin(db_session, other_hospital_id)

        response = await api.post(
            USERS,
            headers=_bearer(admin_b),
            json={"email": user.email, "first_name": "Dup", "last_name": "Licate"},
        )

        assert response.status_code == 409, response.text
        assert await _count(db_session, User, email=user.email) == 1
        # ... and the victim is untouched: they still sign in.
        assert (await _login(api, user.email)).status_code == 200

    async def test_the_refusal_says_nothing_about_other_hospitals(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Same body whether the address is taken here or in another hospital."""
        admin_a = await _make_admin(db_session, hospital_id)
        admin_b = await _make_admin(db_session, other_hospital_id)
        payload = {"email": user.email, "first_name": "Dup", "last_name": "Licate"}

        same_hospital = await api.post(USERS, headers=_bearer(admin_a), json=payload)
        other_hospital = await api.post(USERS, headers=_bearer(admin_b), json=payload)

        assert same_hospital.status_code == other_hospital.status_code == 409
        assert _body(same_hospital) == _body(other_hospital)
        assert "hospital" not in other_hospital.json()["message"].lower()
        assert str(hospital_id) not in other_hospital.text

    async def test_login_fails_closed_when_an_email_names_two_accounts(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        other_hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even if the constraint is somehow absent, ambiguity never authenticates.

        The unique index is dropped inside this test's transaction (rolled back
        afterwards) to recreate the pre-migration state.
        """
        await db_session.execute(text("DROP INDEX uq_users_email_normalized_active"))
        twin = await _make_user(db_session, other_hospital_id, email=user.email)
        recorder = RecordingLogger()
        monkeypatch.setattr(user_repository_module, "logger", recorder)

        right_password = await _login(api, user.email)
        wrong_password = await _login(api, user.email, WRONG_PASSWORD)

        # Not a 500, not a login as either account, and nothing about why.
        for response in (right_password, wrong_password):
            assert response.status_code == 401, response.text
            assert _body(response) == GENERIC_LOGIN_FAILURE
        assert await _count(db_session, RefreshToken, user_id=user.id) == 0
        assert await _count(db_session, RefreshToken, user_id=twin.id) == 0
        # The anomaly is recorded internally, without the address.
        events = [
            entry for entry in recorder.entries if entry["event"] == "staff_identity_ambiguous"
        ]
        assert len(events) == 2
        assert user.email not in json.dumps(recorder.entries, default=str)

    async def test_password_reset_fails_closed_when_an_email_names_two_accounts(
        self, api: AsyncClient, db_session: AsyncSession, user: User, other_hospital_id: uuid.UUID
    ) -> None:
        await db_session.execute(text("DROP INDEX uq_users_email_normalized_active"))
        twin = await _make_user(db_session, other_hospital_id, email=user.email)

        response = await api.post(f"{AUTH}/password/forgot", json={"email": user.email})

        assert response.status_code == 200
        assert response.json()["message"] == FORGOT_RESPONSE
        assert await _count(db_session, PasswordResetToken, user_id=user.id) == 0
        assert await _count(db_session, PasswordResetToken, user_id=twin.id) == 0

    async def test_the_per_hospital_lookup_never_picks_one_of_several(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Two rows differing only in case, inserted behind the application's back."""
        await db_session.execute(text("DROP INDEX uq_users_email_normalized_active"))
        for address in ("twin@hospital.example", "Twin@hospital.example"):
            await db_session.execute(
                text(
                    "INSERT INTO users (id, hospital_id, email, password_hash, first_name, "
                    "last_name) VALUES (:id, :hid, :email, 'x', 'A', 'B')"
                ),
                {"id": uuid.uuid4(), "hid": hospital_id, "email": address},
            )

        repository = UserRepository(db_session)

        assert await repository.get_by_email(hospital_id, "twin@hospital.example") is None
        assert await repository.get_by_email_cross_tenant("TWIN@hospital.example") is None

    async def test_a_deleted_account_elsewhere_does_not_block_a_new_identity(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        other_hospital_id: uuid.UUID,
        hospital_id: uuid.UUID,
    ) -> None:
        gone = await _make_user(db_session, hospital_id, deleted_at=datetime.now(UTC))
        admin_b = await _make_admin(db_session, other_hospital_id)

        response = await api.post(
            USERS,
            headers=_bearer(admin_b),
            json={"email": gone.email, "first_name": "New", "last_name": "Person"},
        )

        assert response.status_code == 201, response.text

    async def test_a_deleted_account_in_the_same_hospital_is_a_clean_conflict(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The older per-hospital constraint still covers deleted rows: 409, not 500."""
        gone = await _make_user(db_session, hospital_id, deleted_at=datetime.now(UTC))
        admin_a = await _make_admin(db_session, hospital_id)

        response = await api.post(
            USERS,
            headers=_bearer(admin_a),
            json={"email": gone.email, "first_name": "New", "last_name": "Person"},
        )

        assert response.status_code == 409, response.text
        assert response.json()["message"] == "This email address cannot be used for a new user."


# ── F8: case is not identity ─────────────────────────────────────────────────


class TestEmailCase:
    async def test_an_address_is_stored_lower_cased_whatever_was_supplied(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        created = await _make_user(db_session, hospital_id, email="  MiXed.Case@Hospital.Example ")

        stored = await db_session.execute(
            text("SELECT email FROM users WHERE id = :id"), {"id": created.id}
        )
        assert stored.scalar_one() == "mixed.case@hospital.example"

    @pytest.mark.parametrize("transform", [str.upper, str.title, str.swapcase])
    async def test_login_succeeds_in_any_case(
        self, api: AsyncClient, user: User, transform: Any
    ) -> None:
        local, _, domain = user.email.partition("@")

        response = await _login(api, f"{transform(local)}@{domain}")

        assert response.status_code == 200, response.text
        assert response.json()["data"]["user"]["email"] == user.email

    async def test_a_case_variant_cannot_be_invited_into_the_same_hospital(
        self, api: AsyncClient, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        admin = await _make_admin(db_session, hospital_id)

        response = await api.post(
            USERS,
            headers=_bearer(admin),
            json={"email": user.email.upper(), "first_name": "Case", "last_name": "Twin"},
        )

        assert response.status_code == 409, response.text
        assert await _count(db_session, User, email=user.email) == 1

    async def test_a_case_variant_cannot_be_invited_into_another_hospital(
        self, api: AsyncClient, db_session: AsyncSession, user: User, other_hospital_id: uuid.UUID
    ) -> None:
        admin_b = await _make_admin(db_session, other_hospital_id)

        response = await api.post(
            USERS,
            headers=_bearer(admin_b),
            json={"email": user.email.title(), "first_name": "Case", "last_name": "Twin"},
        )

        assert response.status_code == 409, response.text

    async def test_an_invited_address_is_stored_lower_cased(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        admin = await _make_admin(db_session, hospital_id)
        address = f"New.Person-{uuid.uuid4().hex[:8]}@Hospital.Example"

        response = await api.post(
            USERS,
            headers=_bearer(admin),
            json={"email": address, "first_name": "New", "last_name": "Person"},
        )

        assert response.status_code == 201, response.text
        assert response.json()["data"]["email"] == address.lower()

    async def test_forgot_and_reset_work_with_mixed_case_input(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        forgot = await api.post(f"{AUTH}/password/forgot", json={"email": user.email.upper()})

        assert forgot.status_code == 200
        assert await _count(db_session, PasswordResetToken, user_id=user.id) == 1

        # Redeem a token for the same account, then sign in with yet another case.
        raw = await _reset_token(db_session, user)
        reset = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
        )
        assert reset.status_code == 200, reset.text

        local, _, domain = user.email.partition("@")
        assert (await _login(api, f"{local.title()}@{domain}", NEW_PASSWORD)).status_code == 200


# ── F3: every rejected login does the same work and says the same thing ──────


class TestUniformLoginFailure:
    async def _cases(
        self, db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
    ) -> dict[str, tuple[str, str]]:
        """Build one account per failure mode; return ``{name: (email, password)}``."""
        active = await _make_user(db_session, hospital_id)
        suspended = await _make_user(db_session, hospital_id, status=UserStatus.SUSPENDED)
        deleted = await _make_user(db_session, hospital_id, deleted_at=datetime.now(UTC))
        invited = await _make_user(db_session, hospital_id, status=UserStatus.INVITED)
        in_closed_hospital = await _make_user(db_session, other_hospital_id)
        await _set_hospital_active(db_session, other_hospital_id, active=False)
        return {
            "unknown email": (_email("nobody"), PASSWORD),
            "wrong password": (active.email, WRONG_PASSWORD),
            "suspended, right password": (suspended.email, PASSWORD),
            "soft-deleted, right password": (deleted.email, PASSWORD),
            "invited, right password": (invited.email, PASSWORD),
            "inactive hospital, right password": (in_closed_hospital.email, PASSWORD),
        }

    async def test_every_rejection_has_the_same_status_and_body(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        cases = await self._cases(db_session, hospital_id, other_hospital_id)

        for name, (email, password) in cases.items():
            response = await _login(api, email, password)
            assert response.status_code == 401, name
            assert _body(response) == GENERIC_LOGIN_FAILURE, name
            assert "access_token" not in response.text, name
            assert "mfa_ticket" not in response.text, name

    async def test_every_rejection_performs_exactly_one_password_verification(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No path is cheaper than a wrong password, so none is faster."""
        cases = await self._cases(db_session, hospital_id, other_hospital_id)
        work = _Work(monkeypatch)

        for name, (email, password) in cases.items():
            work.reset()
            await _login(api, email, password)
            assert work.total == 1, f"{name}: {work.real} real + {work.dummy} dummy"

    async def test_the_dummy_path_runs_where_no_real_check_can(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cases = await self._cases(db_session, hospital_id, other_hospital_id)
        work = _Work(monkeypatch)

        for name, (email, password) in cases.items():
            work.reset()
            await _login(api, email, password)
            expected_dummy = 0 if name == "wrong password" else 1
            assert work.dummy == expected_dummy, name

    async def test_no_password_is_hashed_on_the_event_loop(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A burst of logins must not stall every other request on the worker
        (and, by stretching responses unevenly, leak which addresses did real
        work). Real and dummy verifications alike run in the hashing pool."""
        cases = await self._cases(db_session, hospital_id, other_hospital_id)
        work = _Work(monkeypatch)
        loop_thread = threading.current_thread().name

        for email, password in cases.values():
            await _login(api, email, password)

        assert work.total == len(cases)
        assert loop_thread not in work.threads
        assert all(name.startswith("password-hash") for name in work.threads), work.threads

    async def test_a_throttled_attempt_says_nothing_about_whether_the_address_exists(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Enumeration through the throttle: guess until refused, compare.

        The counters are keyed on the address as typed, so a real address and
        an unknown one are refused after the same number of attempts, with the
        same answer, and neither refusal evaluates anything.
        """
        unknown = _email("nobody")
        work = _Work(monkeypatch)
        outcomes: dict[str, list[tuple[int, str, int]]] = {user.email: [], unknown: []}

        for address, seen in outcomes.items():
            for _ in range(PW_PAIR.free + 2):
                work.reset()
                response = await _login(api, address, WRONG_PASSWORD)
                seen.append(
                    (response.status_code, json.dumps(_body(response), sort_keys=True), work.total)
                )

        assert outcomes[user.email] == outcomes[unknown]
        generic = json.dumps(GENERIC_LOGIN_FAILURE, sort_keys=True)
        evaluated = [(401, generic, 1)] * PW_PAIR.free
        refused_unevaluated = [(401, generic, 0)] * 2
        assert outcomes[unknown] == evaluated + refused_unevaluated
        # ... and the real account was told nothing either: no session, and the
        # right password is refused while its origin is waiting.
        assert _body(await _login(api, user.email)) == GENERIC_LOGIN_FAILURE
        assert await _count(db_session, RefreshToken, user_id=user.id) == 0

    async def test_the_dummy_check_is_a_real_argon2id_verification(self) -> None:
        from app.core import security

        assert security._DUMMY_PASSWORD_HASH.startswith("$argon2id$")  # noqa: SLF001
        assert security.verify_password(PASSWORD, security._DUMMY_PASSWORD_HASH) is False  # noqa: SLF001
        security.burn_password_verification(PASSWORD)  # returns nothing, raises nothing

    async def test_no_rejection_issues_a_session_or_a_success_audit(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        cases = await self._cases(db_session, hospital_id, other_hospital_id)
        before = await _count(db_session, RefreshToken)

        for email, password in cases.values():
            await _login(api, email, password)

        assert await _count(db_session, RefreshToken) == before
        successes = await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "auth.login.success")
        )
        assert successes.scalar_one() == 0


class TestOversizedPassword:
    """A password the hasher refuses used to be a 500 for real accounts only."""

    async def test_known_and_unknown_emails_get_the_same_answer(
        self, api: AsyncClient, user: User
    ) -> None:
        oversized = "a" * 5000

        known = await _login(api, user.email, oversized)
        unknown = await _login(api, _email("nobody"), oversized)

        assert known.status_code == unknown.status_code == 422
        assert _body(known) == _body(unknown)

    async def test_the_longest_accepted_password_is_an_ordinary_failure(
        self, api: AsyncClient, user: User
    ) -> None:
        longest = "a" * MAX_PASSWORD_INPUT_LENGTH

        known = await _login(api, user.email, longest)
        unknown = await _login(api, _email("nobody"), longest)

        assert known.status_code == unknown.status_code == 401
        assert _body(known) == _body(unknown) == GENERIC_LOGIN_FAILURE

    async def test_the_hasher_wrapper_never_raises(self) -> None:
        from app.core import security

        stored = hash_password(PASSWORD)

        assert security.verify_password("a" * 100_000, stored) is False
        assert security.verify_password(PASSWORD, "not-a-hash") is False
        security.burn_password_verification("a" * 100_000)


# ── F6: a soft-deleted account ───────────────────────────────────────────────


class TestSoftDeletedUser:
    async def test_cannot_log_in_and_receives_nothing(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        gone = await _make_user(db_session, hospital_id, deleted_at=datetime.now(UTC))

        response = await _login(api, gone.email)

        assert response.status_code == 401
        assert _body(response) == GENERIC_LOGIN_FAILURE
        assert await _count(db_session, RefreshToken, user_id=gone.id) == 0
        assert "auth.login.success" not in await _audit_actions(db_session, gone.id)

    async def test_looks_exactly_like_an_unknown_email(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        gone = await _make_user(db_session, hospital_id, deleted_at=datetime.now(UTC))

        for password in (PASSWORD, WRONG_PASSWORD):
            deleted = await _login(api, gone.email, password)
            unknown = await _login(api, _email("nobody"), password)
            assert deleted.status_code == unknown.status_code == 401
            assert _body(deleted) == _body(unknown)

    async def test_cannot_obtain_a_password_reset_token(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        gone = await _make_user(db_session, hospital_id, deleted_at=datetime.now(UTC))

        response = await api.post(f"{AUTH}/password/forgot", json={"email": gone.email})

        assert response.status_code == 200
        assert response.json()["message"] == FORGOT_RESPONSE
        assert await _count(db_session, PasswordResetToken, user_id=gone.id) == 0

    async def test_cannot_redeem_a_token_issued_before_deletion(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        raw = await _reset_token(db_session, user)
        user_id, original_hash = user.id, user.password_hash
        user.deleted_at = datetime.now(UTC)
        await _saved(db_session)

        redeemed = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
        )
        bogus = await api.post(
            f"{AUTH}/password/reset",
            json={"token": "not-a-real-token", "new_password": NEW_PASSWORD},
        )

        # Indistinguishable from a token that never existed.
        assert redeemed.status_code == bogus.status_code == 401
        assert _body(redeemed) == _body(bogus)
        assert "set-cookie" not in redeemed.headers
        stored = await db_session.execute(
            text("SELECT password_hash FROM users WHERE id = :id"), {"id": user_id}
        )
        assert stored.scalar_one() == original_hash
        assert await _count(db_session, RefreshToken, user_id=user_id) == 0


# ── F7: a deactivated hospital ───────────────────────────────────────────────


class TestInactiveHospital:
    async def test_login_fails(
        self, api: AsyncClient, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        await _set_hospital_active(db_session, hospital_id, active=False)

        response = await _login(api, user.email)

        assert response.status_code == 401
        assert _body(response) == GENERIC_LOGIN_FAILURE
        assert await _count(db_session, RefreshToken, user_id=user.id) == 0
        assert await _failure_reasons(db_session, user.id) == ["hospital_inactive"]

    async def test_refresh_fails(
        self, api: AsyncClient, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        session_data = (await _login(api, user.email)).json()["data"]
        await _set_hospital_active(db_session, hospital_id, active=False)

        response = await api.post(
            f"{AUTH}/refresh", json={"refresh_token": session_data["refresh_token"]}
        )

        assert response.status_code == 401
        assert "access_token" not in response.text

    async def test_an_existing_access_token_stops_working(
        self, api: AsyncClient, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        token = (await _login(api, user.email)).json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert (await api.get(f"{USERS}/me", headers=headers)).status_code == 200

        await _set_hospital_active(db_session, hospital_id, active=False)
        response = await api.get(f"{USERS}/me", headers=headers)

        assert response.status_code == 403
        assert response.json()["message"] == "Account is not active."

    async def test_the_mfa_step_fails(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)
        await _set_hospital_active(db_session, hospital_id, active=False)

        response = await _verify(api, ticket, await _code(secret))

        assert response.status_code == 401
        assert _body(response) == GENERIC_MFA_FAILURE
        assert await _count(db_session, RefreshToken, user_id=mfa_user.id) == 0

    async def test_no_password_reset_token_is_issued_or_redeemable(
        self, api: AsyncClient, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        raw = await _reset_token(db_session, user)
        user_id, email, original_hash = user.id, user.email, user.password_hash
        await _set_hospital_active(db_session, hospital_id, active=False)
        await _saved(db_session)

        forgot = await api.post(f"{AUTH}/password/forgot", json={"email": email})
        redeemed = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
        )

        assert forgot.status_code == 200
        assert forgot.json()["message"] == FORGOT_RESPONSE
        assert await _count(db_session, PasswordResetToken, user_id=user_id) == 1  # only ours
        assert redeemed.status_code == 401
        assert redeemed.json()["message"] == "Invalid or expired password reset token."
        stored = await db_session.execute(
            text("SELECT password_hash FROM users WHERE id = :id"), {"id": user_id}
        )
        assert stored.scalar_one() == original_hash

    async def test_a_reactivated_hospital_works_again(
        self, api: AsyncClient, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        token = (await _login(api, user.email)).json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        await _set_hospital_active(db_session, hospital_id, active=False)
        assert (await _login(api, user.email)).status_code == 401
        assert (await api.get(f"{USERS}/me", headers=headers)).status_code == 403

        await _set_hospital_active(db_session, hospital_id, active=True)

        assert (await _login(api, user.email)).status_code == 200
        assert (await api.get(f"{USERS}/me", headers=headers)).status_code == 200

    async def test_another_hospital_is_unaffected(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        colleague_elsewhere = await _make_user(db_session, other_hospital_id)
        await _set_hospital_active(db_session, hospital_id, active=False)

        assert (await _login(api, colleague_elsewhere.email)).status_code == 200
        assert (await _login(api, user.email)).status_code == 401

    async def test_a_user_whose_hospital_cannot_be_seen_is_denied(self) -> None:
        """Doubt about hospital state denies; it does not default to active."""
        orphan = User(hospital_id=uuid.uuid4(), email="orphan@hospital.example")
        orphan.hospital = None  # type: ignore[assignment]
        assert orphan.hospital_is_active is False

        closed = User(hospital_id=uuid.uuid4(), email="closed@hospital.example")
        closed.hospital = Hospital(is_active=False)
        assert closed.hospital_is_active is False

        unset = User(hospital_id=uuid.uuid4(), email="unset@hospital.example")
        unset.hospital = Hospital()  # is_active not loaded / None
        assert unset.hospital_is_active is False

        open_ = User(hospital_id=uuid.uuid4(), email="open@hospital.example")
        open_.hospital = Hospital(is_active=True)
        assert open_.hospital_is_active is True


# ── F4: MFA codes cannot be guessed without limit ────────────────────────────


async def _throttle_notes(session: AsyncSession, user_id: uuid.UUID) -> list[dict[str, Any]]:
    """What the audit trail says about waits started by this account's failures."""
    rows = await session.execute(
        select(AuditLog.context).where(
            AuditLog.target_id == user_id, AuditLog.action == "auth.login.failed"
        )
    )
    return [context["throttle"] for context in rows.scalars() if context and "throttle" in context]


class TestMfaGuessingIsThrottled:
    """A six-digit code is one guess in a million; unlimited guesses find it.

    The old answer counted failures on the account and locked it, which let
    anybody keep the owner out. Now every origin gets a few codes and then
    exponentially longer waits (``mfa_origin``), and the account as a whole
    gets a small allowance that only time refills (``mfa_account``). An
    attempt that is not admitted is not evaluated at all.

    The one origin that does not draw on the account's allowance is a device
    that has itself passed this account's second factor before; see
    :class:`TestTheOwnersVerifiedDeviceKeepsItsSecondFactor`.
    """

    def test_the_allowances_are_as_small_as_these_tests_assume(self) -> None:
        """The tests below read the throttle's numbers from its policy, so a
        policy loosened to uselessness would still pass them. This one states
        the ceiling: a guesser gets at most 10 codes at once and 48 a day
        after that — under 6 chances in 100,000 a day against a six-digit code."""
        assert MFA_ACCOUNT.burst <= 10
        assert MFA_ACCOUNT.refill >= timedelta(minutes=30)
        # A device that has passed the second factor has an allowance of its
        # own. It is no larger than the shared one: holding such a device's
        # token buys no extra guesses.
        assert MFA_DEVICE_CAP.burst <= MFA_ACCOUNT.burst
        assert MFA_DEVICE_CAP.refill >= MFA_ACCOUNT.refill
        assert MFA_ORIGIN.free <= 5
        assert MFA_ORIGIN.base >= timedelta(minutes=1)
        assert MFA_ORIGIN.quiet > MFA_ORIGIN.cap >= timedelta(hours=1)
        assert PW_PAIR.free <= 5
        assert PW_PAIR.base >= timedelta(minutes=1)
        assert PW_PAIR.quiet > PW_PAIR.cap
        account_budget = cast("Budget", POLICIES[BucketKind.PW_ACCOUNT])
        assert account_budget.burst <= 20
        assert account_budget.refill >= timedelta(minutes=15)

    async def test_every_wrong_code_is_charged_and_audited(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)
        origin = _mfa_origin(mfa_user, LOCAL_SOURCE)

        for attempt in range(1, 4):
            response = await _verify(api, ticket, _wrong_code(secret))
            assert response.status_code == 401
            assert _body(response) == GENERIC_MFA_FAILURE
            # Charged to the origin's backoff and to the account's budget.
            assert await _failures(db_session, origin) == attempt
            assert await _budget_used(db_session, _mfa_account(mfa_user)) == attempt

        assert await _failure_reasons(db_session, mfa_user.id) == ["invalid_mfa_code"] * 3
        assert await _count(db_session, RefreshToken, user_id=mfa_user.id) == 0

    async def test_after_the_free_codes_the_right_code_is_refused_without_being_looked_at(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The guesser's next code may be the right one. It must not be tried."""
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)
        origin = _mfa_origin(mfa_user, LOCAL_SOURCE)
        checks = _CodeChecks(monkeypatch)

        for _ in range(MFA_ORIGIN.free):
            await _verify(api, ticket, _wrong_code(secret))
        assert checks.count == MFA_ORIGIN.free
        row = await _bucket_row(db_session, origin)
        assert row is not None
        blocked_until = row.blocked_until
        assert blocked_until is not None
        used = await _budget_used(db_session, _mfa_account(mfa_user))

        responses = [await _verify(api, ticket, await _code(secret)) for _ in range(3)]

        for response in responses:
            assert response.status_code == 401
            assert _body(response) == GENERIC_MFA_FAILURE
            assert "access_token" not in response.text
            assert "set-cookie" not in response.headers
        assert checks.count == MFA_ORIGIN.free  # the right code was never evaluated
        assert await _count(db_session, RefreshToken, user_id=mfa_user.id) == 0
        assert "auth.login.success" not in await _audit_actions(db_session, mfa_user.id)
        # A refusal charges nothing and lengthens nothing: hammering a closed
        # origin neither extends the owner's wait nor drains the account.
        row = await _bucket_row(db_session, origin)
        assert row is not None
        assert (row.failures, row.blocked_until) == (MFA_ORIGIN.free, blocked_until)
        assert await _budget_used(db_session, _mfa_account(mfa_user)) == used
        # The failure that started the wait says so in the audit trail.
        assert await _throttle_notes(db_session, mfa_user.id) == [
            {"bucket": "mfa_origin", "wait_seconds": int(MFA_ORIGIN.base.total_seconds())}
        ]

    async def test_when_the_wait_has_passed_the_right_code_works(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing is locked: the owner is delayed, never shut out."""
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)
        clock = _Clock(monkeypatch)
        for _ in range(MFA_ORIGIN.free):
            await _verify(api, ticket, _wrong_code(secret))

        clock.advance(MFA_ORIGIN.base - timedelta(seconds=5))
        too_early = await _verify(api, ticket, await _code(secret))
        clock.advance(timedelta(seconds=10))
        in_time = await _verify(api, ticket, await _code(secret))

        assert too_early.status_code == 401
        assert _body(too_early) == GENERIC_MFA_FAILURE
        assert in_time.status_code == 200, in_time.text
        assert in_time.json()["data"]["access_token"]
        assert "auth.login.success" in await _audit_actions(db_session, mfa_user.id)

    async def test_waiting_out_one_block_does_not_start_the_guesser_afresh(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One guess per wait, and each wait twice as long as the last."""
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)
        clock = _Clock(monkeypatch)
        checks = _CodeChecks(monkeypatch)
        for _ in range(MFA_ORIGIN.free):
            await _verify(api, ticket, _wrong_code(secret))

        clock.advance(MFA_ORIGIN.base + timedelta(seconds=5))
        await _verify(api, ticket, _wrong_code(secret))  # evaluated: one guess
        await _verify(api, ticket, _wrong_code(secret))  # refused: no second one
        assert checks.count == MFA_ORIGIN.free + 1

        # The first wait again is no longer enough ...
        clock.advance(MFA_ORIGIN.base + timedelta(seconds=5))
        still_waiting = await _verify(api, ticket, await _code(secret))
        # ... the second is twice the first.
        clock.advance(MFA_ORIGIN.base)
        admitted = await _verify(api, ticket, await _code(secret))

        assert still_waiting.status_code == 401
        assert checks.count == MFA_ORIGIN.free + 2  # only the admitted one was evaluated
        assert admitted.status_code == 200, admitted.text
        assert {"bucket": "mfa_origin", "wait_seconds": 120} in await _throttle_notes(
            db_session, mfa_user.id
        )

    async def test_the_account_budget_bounds_guessing_from_any_number_of_origins(
        self,
        application: FastAPI,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Spreading guesses over many addresses, so that no single origin
        ever has to wait.

        The account's own allowance is shared by every origin that has not
        itself passed the second factor. When it is used up the next code is
        not evaluated from any of them, and only time gives it back.
        """
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        account = _mfa_account(mfa_user)
        clock = _Clock(monkeypatch)
        checks = _CodeChecks(monkeypatch)
        per_origin = MFA_ORIGIN.free - 1  # never enough to make an origin wait
        assert 3 * per_origin >= MFA_ACCOUNT.burst

        async with (
            _browser(application, SOURCE_B) as first,
            _browser(application, SOURCE_C) as second,
            _browser(application, SOURCE_D) as third,
            _browser(application, SOURCE_E) as fresh,
        ):
            guesses = 0
            for browser in (first, second, third):
                ticket = await _ticket(browser, mfa_user)
                for _ in range(per_origin):
                    if guesses == MFA_ACCOUNT.burst:
                        break
                    response = await _verify(browser, ticket, _wrong_code(secret))
                    assert _body(response) == GENERIC_MFA_FAILURE
                    guesses += 1

            # Every one of them was evaluated: no origin was made to wait, and
            # they really were three different origins.
            assert guesses == checks.count == MFA_ACCOUNT.burst
            assert await _failures(db_session, _mfa_origin(mfa_user, SOURCE_B)) == per_origin
            assert await _failures(db_session, _mfa_origin(mfa_user, SOURCE_C)) == per_origin
            assert await _failures(db_session, _mfa_origin(mfa_user, SOURCE_D)) == (
                MFA_ACCOUNT.burst - 2 * per_origin
            )
            assert await _budget_used(db_session, account) == MFA_ACCOUNT.burst

            # The next code is not evaluated — not from an address never seen
            # before, and not from one that has guessed least — even though it
            # is the right one.
            fresh_ticket = await _ticket(fresh, mfa_user)
            from_new_address = await _verify(fresh, fresh_ticket, await _code(secret))
            from_a_used_one = await _verify(
                third, await _ticket(third, mfa_user), await _code(secret)
            )
            for response in (from_new_address, from_a_used_one):
                assert response.status_code == 401
                assert _body(response) == GENERIC_MFA_FAILURE
                assert "set-cookie" not in response.headers
            assert checks.count == MFA_ACCOUNT.burst
            assert await _count(db_session, RefreshToken, user_id=mfa_user.id) == 0
            assert await _devices(db_session, mfa_user.id) == []

            # Only time gives the allowance back, one code per refill period.
            clock.advance(MFA_ACCOUNT.refill + timedelta(seconds=5))
            await _verify(fresh, fresh_ticket, _wrong_code(secret))
            assert checks.count == MFA_ACCOUNT.burst + 1
            spent_again = await _verify(fresh, fresh_ticket, await _code(secret))
            assert spent_again.status_code == 401
            assert checks.count == MFA_ACCOUNT.burst + 1

            clock.advance(MFA_ACCOUNT.refill + timedelta(seconds=5))
            finally_admitted = await _verify(fresh, fresh_ticket, await _code(secret))
            assert finally_admitted.status_code == 200, finally_admitted.text

    async def test_signing_in_again_with_the_password_refills_nothing(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bypass: holding the password, start a fresh login before each guess.

        A correct password gives back only what that password attempt was
        charged. It never touches what wrong codes have been charged.
        """
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        checks = _CodeChecks(monkeypatch)

        for _ in range(MFA_ORIGIN.free + 3):
            login = await _login(api, mfa_user.email)
            assert login.status_code == 200  # the password is right every time
            await _verify(api, login.json()["data"]["mfa_ticket"], _wrong_code(secret))

        assert checks.count == MFA_ORIGIN.free
        assert await _failures(db_session, _mfa_origin(mfa_user, LOCAL_SOURCE)) == MFA_ORIGIN.free
        assert await _budget_used(db_session, _mfa_account(mfa_user)) == MFA_ORIGIN.free
        # A brand-new ticket and the right code do not get through either.
        response = await _verify(api, await _ticket(api, mfa_user), await _code(secret))
        assert response.status_code == 401
        assert checks.count == MFA_ORIGIN.free
        assert await _count(db_session, RefreshToken, user_id=mfa_user.id) == 0

    async def test_a_success_from_an_unrecognised_origin_clears_nothing(
        self,
        application: FastAPI,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Somebody signing in must not hand a guesser a fresh allowance.

        An address may be a whole hospital's. If one person completing a
        sign-in from it cleared its backoff, a guesser behind the same address
        would get the free codes all over again each time a colleague signed
        in. So a success from an origin that is not a trusted device gives
        back its own charge and nothing else: not the address's backoff, and
        not what wrong codes took from the account's budget.
        """
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        origin = _mfa_origin(mfa_user, LOCAL_SOURCE)
        account = _mfa_account(mfa_user)
        clock = _Clock(monkeypatch)
        ticket = await _ticket(api, mfa_user)
        for _ in range(MFA_ORIGIN.free):
            await _verify(api, ticket, _wrong_code(secret))
        clock.advance(MFA_ORIGIN.base + timedelta(seconds=5))

        success = await _verify(api, ticket, await _code(secret))

        assert success.status_code == 200, success.text
        # The address still holds every wrong code that was sent from it ...
        assert await _failures(db_session, origin) == MFA_ORIGIN.free
        # ... and the account's budget holds them too. Only the successful
        # attempt's own charge came back.
        assert await _budget_used(db_session, account, clock=clock) == MFA_ORIGIN.free

        # So a guesser at that same address (another browser: no device
        # cookie) does not start afresh. One code is heard, and it starts a
        # wait twice as long as the first.
        checks = _CodeChecks(monkeypatch)
        async with _browser(application, LOCAL_SOURCE) as guesser:
            guess_ticket = await _ticket(guesser, mfa_user)
            await _verify(guesser, guess_ticket, _wrong_code(secret))
            refused = await _verify(guesser, guess_ticket, await _code(secret))
        assert checks.count == 1
        assert refused.status_code == 401
        assert _body(refused) == GENERIC_MFA_FAILURE
        assert await _failures(db_session, origin) == MFA_ORIGIN.free + 1
        assert {
            "bucket": "mfa_origin",
            "wait_seconds": 2 * int(MFA_ORIGIN.base.total_seconds()),
        } in await _throttle_notes(db_session, mfa_user.id)

        # And the account has only what was left, from origins never used before.
        checks.count = 0
        left = MFA_ACCOUNT.burst - MFA_ORIGIN.free - 1
        async with (
            _browser(application, SOURCE_B) as second,
            _browser(application, SOURCE_C) as third,
        ):
            guess_ticket = await _ticket(second, mfa_user)
            for _ in range(left):
                await _verify(second, guess_ticket, _wrong_code(secret))
            assert checks.count == left
            refused = await _verify(third, await _ticket(third, mfa_user), await _code(secret))

        assert refused.status_code == 401
        assert _body(refused) == GENERIC_MFA_FAILURE
        assert checks.count == left

    async def test_a_success_on_a_trusted_device_clears_that_devices_backoffs_and_no_budget(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The owner mistypes on their own browser, then gets it right.

        A trusted device's backoffs are its own — nobody else can be guessing
        through them — so its success clears them. Its budget is a ceiling a
        success cannot reset: whoever holds the device's token and the
        password still gets no more than the budget's worth of codes.
        """
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        assert (await _sign_in(api, mfa_user, secret)).status_code == 200  # ``api`` is trusted
        device = await _only_device(db_session, mfa_user.id)
        code_backoff = AuthService._mfa_origin(mfa_user, LOCAL_SOURCE, device)  # noqa: SLF001
        password_backoff = bucket(BucketKind.PW_DEVICE, device.id)
        mistakes = MFA_ORIGIN.free - 1

        for _ in range(2):
            assert (await _login(api, mfa_user.email, WRONG_PASSWORD)).status_code == 401
        ticket = await _ticket(api, mfa_user)
        for _ in range(mistakes):
            await _verify(api, ticket, _wrong_code(secret))
        assert await _failures(db_session, code_backoff) == mistakes
        assert await _failures(db_session, password_backoff) == 2
        # Charged to the device's own budget, not the account's shared one.
        assert await _budget_used(db_session, _mfa_device_cap(device)) == mistakes
        assert await _budget_used(db_session, _mfa_account(mfa_user)) == 0

        success = await _verify(api, ticket, await _code(secret))

        assert success.status_code == 200, success.text
        for backoff in (code_backoff, password_backoff):
            row = await _bucket_row(db_session, backoff)
            assert row is not None
            assert (row.failures, row.blocked_until, row.last_charged_at) == (0, None, None)
        assert await _budget_used(db_session, _mfa_device_cap(device)) == mistakes

        # The ceiling. Getting the code right now and then keeps the device's
        # backoff clear, so nothing ever makes it wait — and the wrong codes
        # in between are counted against its budget all the same. When that
        # is gone not even the right code is heard.
        checks = _CodeChecks(monkeypatch)
        wrong, right = mistakes, 0
        while wrong < MFA_DEVICE_CAP.burst:
            batch = min(mistakes, MFA_DEVICE_CAP.burst - wrong)
            ticket = await _ticket(api, mfa_user)
            for _ in range(batch):
                await _verify(api, ticket, _wrong_code(secret))
            wrong += batch
            if wrong < MFA_DEVICE_CAP.burst:
                assert (await _verify(api, ticket, await _code(secret))).status_code == 200
                right += 1
        assert right >= 1
        assert checks.count == MFA_DEVICE_CAP.burst - mistakes + right
        assert await _failures(db_session, code_backoff) < MFA_ORIGIN.free  # nothing is waiting
        assert await _budget_used(db_session, _mfa_device_cap(device)) == MFA_DEVICE_CAP.burst
        sessions = await _count(db_session, RefreshToken, user_id=mfa_user.id)

        refused = await _verify(api, await _ticket(api, mfa_user), await _code(secret))

        assert refused.status_code == 401
        assert _body(refused) == GENERIC_MFA_FAILURE
        assert checks.count == MFA_DEVICE_CAP.burst - mistakes + right
        assert await _count(db_session, RefreshToken, user_id=mfa_user.id) == sessions

    async def test_a_throttled_attempt_is_indistinguishable_from_a_wrong_code(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        """The guesser cannot tell when it stopped being listened to, nor
        whether the code it sent while refused was the right one."""
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)

        evaluated = [
            await _verify(api, ticket, _wrong_code(secret)) for _ in range(MFA_ORIGIN.free)
        ]
        throttled = [await _verify(api, ticket, _wrong_code(secret)) for _ in range(3)]
        throttled.append(await _verify(api, ticket, await _code(secret)))

        reference = _wire(evaluated[0])
        for response in (*evaluated, *throttled):
            assert _wire(response) == reference
        assert reference[0] == 401
        assert json.loads(reference[1]) == GENERIC_MFA_FAILURE
        for header in ("set-cookie", "retry-after", "www-authenticate"):
            assert header not in reference[2]


def _wire(response: Response) -> tuple[int, str, tuple[str, ...], str | None, int]:
    """Everything a client can see of a response, bar what differs per request.

    The body is compared byte for byte once the per-request metadata (the
    request id) is taken out; its length is compared with the metadata in.
    Header *names* are compared; the values that differ on every response (a
    request id, a rate-limit counter, a timing) say nothing about the outcome.
    """
    return (
        response.status_code,
        json.dumps(_body(response), sort_keys=True),
        tuple(sorted(response.headers.keys())),
        response.headers.get("content-type"),
        len(response.content),
    )


# ── An MFA ticket vouches for one password, and dies with it ─────────────────


async def _rewind_password_change(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Make the account's last password change look an hour old.

    A ticket used to be compared with the *time* of the last change, to the
    second, so one issued in the same second as the change — or before an
    administrator's reset, which records no time at all — went on working.
    With the recorded time moved well before the ticket, only a ticket that
    is tied to the password itself is still refused.
    """
    await session.execute(
        text("UPDATE users SET password_changed_at = now() - interval '1 hour' WHERE id = :id"),
        {"id": user_id},
    )
    await _saved(session)


class TestMfaTicketIsBoundToThePassword:
    """A thief has the password and is at the code step, ticket in hand, when
    the password is replaced. The ticket says "the password was right": that
    password no longer exists, so the ticket is worth nothing from that
    instant — not five minutes later, and not one second later.
    """

    async def _assert_dead(
        self,
        thief: AsyncClient,
        db_session: AsyncSession,
        user_id: uuid.UUID,
        stolen_ticket: str,
        secret: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ticket fails even with the right code, and the code is not looked at."""
        checks = _CodeChecks(monkeypatch)
        sessions = await _count(db_session, RefreshToken, user_id=user_id, is_revoked=False)

        responses = [
            await _verify(thief, stolen_ticket, await _code(secret)),
            await _verify(thief, stolen_ticket, _wrong_code(secret)),
        ]

        for response in responses:
            assert response.status_code == 401
            assert _body(response) == GENERIC_MFA_FAILURE
            assert "access_token" not in response.text
            assert "set-cookie" not in response.headers
        assert checks.count == 0
        assert await _count(db_session, RefreshToken, user_id=user_id, is_revoked=False) == sessions
        assert "auth.login.success" not in await _audit_actions(db_session, user_id)

    async def _assert_a_new_sign_in_works(
        self, browser: AsyncClient, user: User, secret: str, old_ticket: str
    ) -> None:
        """The old password opens nothing; the new one, with a new ticket, does."""
        assert (await _login(browser, user.email, PASSWORD)).status_code == 401
        new_ticket = await _ticket(browser, user, NEW_PASSWORD)
        assert new_ticket != old_ticket
        completed = await _verify(browser, new_ticket, await _code(secret))
        assert completed.status_code == 200, completed.text
        assert completed.json()["data"]["access_token"]

    async def test_a_ticket_issued_before_an_emailed_reset_is_refused_at_once(
        self,
        application: FastAPI,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The owner resets a stolen password through their mailbox."""
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        stolen_ticket = await _ticket(api, mfa_user)
        raw = await _reset_token(db_session, mfa_user)
        user_id = mfa_user.id

        async with _browser(application, SOURCE_B) as owner:
            reset = await owner.post(
                f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
            )
            assert reset.status_code == 200, reset.text
            await _rewind_password_change(db_session, user_id)

            await self._assert_dead(api, db_session, user_id, stolen_ticket, secret, monkeypatch)
            await self._assert_a_new_sign_in_works(owner, mfa_user, secret, stolen_ticket)
        # ... and the stolen one stays dead after the owner has signed in.
        again = await _verify(api, stolen_ticket, await _code(secret))
        assert _body(again) == GENERIC_MFA_FAILURE

    async def test_a_ticket_issued_before_a_self_service_change_is_refused_at_once(
        self,
        application: FastAPI,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The owner, signed in elsewhere, changes the password the thief has."""
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        stolen_ticket = await _ticket(api, mfa_user)
        user_id = mfa_user.id

        async with _browser(application, SOURCE_B) as owner:
            changed = await owner.post(
                f"{AUTH}/password/change",
                headers=_bearer(mfa_user),
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            )
            assert changed.status_code == 200, changed.text
            await _rewind_password_change(db_session, user_id)

            await self._assert_dead(api, db_session, user_id, stolen_ticket, secret, monkeypatch)
            await self._assert_a_new_sign_in_works(owner, mfa_user, secret, stolen_ticket)

    async def test_a_ticket_issued_before_an_administrator_reset_is_refused_at_once(
        self,
        application: FastAPI,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An administrator resets an account believed compromised.

        That reset records no time of change at all, so a comparison of times
        had nothing to refuse the thief's ticket with: for the five minutes
        it lived, the reset did not stop a sign-in already under way.
        """
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        admin = await _make_user(db_session, hospital_id, email=_email("admin"))
        await grant_permissions(
            db_session, hospital_id=hospital_id, user_id=admin.id, codes=["user.reset_password"]
        )
        stolen_ticket = await _ticket(api, mfa_user)
        user_id = mfa_user.id

        async with _browser(application, SOURCE_B) as office:
            reset = await office.post(f"{USERS}/{user_id}/reset-password", headers=_bearer(admin))
            assert reset.status_code == 200, reset.text
            stored = await db_session.execute(
                text("SELECT password_changed_at FROM users WHERE id = :id"), {"id": user_id}
            )
            assert stored.scalar_one() is None  # nothing for a comparison of times to use

            await self._assert_dead(api, db_session, user_id, stolen_ticket, secret, monkeypatch)

            # The owner sets a password through the emailed link and signs in:
            # a ticket issued for the password that exists now is good.
            raw = await _reset_token(db_session, mfa_user)
            chosen = await office.post(
                f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
            )
            assert chosen.status_code == 200, chosen.text
            await self._assert_a_new_sign_in_works(office, mfa_user, secret, stolen_ticket)
        again = await _verify(api, stolen_ticket, await _code(secret))
        assert _body(again) == GENERIC_MFA_FAILURE

    async def test_a_ticket_that_names_no_password_is_refused_like_a_wrong_code(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A correctly signed ticket of the right type that is tied to nothing
        — what the old helper issued, and what any code path that forgets the
        binding would issue. Absence of the binding must not read as "matches".
        """
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        reference = _wire(await _verify(api, await _ticket(api, mfa_user), _wrong_code(secret)))
        charged = (
            await _failures(db_session, _mfa_origin(mfa_user, LOCAL_SOURCE)),
            await _budget_used(db_session, _mfa_account(mfa_user)),
        )
        assert charged == (1, 1)
        checks = _CodeChecks(monkeypatch)
        unbound = create_mfa_ticket(mfa_user.id)

        responses = [
            await _verify(api, unbound, await _code(secret)),
            await _verify(api, unbound, _wrong_code(secret)),
        ]

        for response in responses:
            assert _wire(response) == reference
            assert _body(response) == GENERIC_MFA_FAILURE
        assert checks.count == 0
        # Nothing was charged either: there was never a code to judge.
        assert (
            await _failures(db_session, _mfa_origin(mfa_user, LOCAL_SOURCE)),
            await _budget_used(db_session, _mfa_account(mfa_user)),
        ) == charged
        assert await _count(db_session, RefreshToken, user_id=mfa_user.id) == 0

    async def test_a_ticket_bound_to_another_accounts_password_is_refused(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The binding is to this account's password, not to "some password"."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        attacker_account, _ = await _make_mfa_user(db_session, hospital_id)
        checks = _CodeChecks(monkeypatch)
        forged = create_access_token(
            user_id=victim.id,
            hospital_id=None,
            extra_claims={
                "type": "mfa_ticket",
                "purpose": "mfa_verification",
                "pwb": AuthService._password_binding(attacker_account),  # noqa: SLF001
            },
        )

        response = await _verify(api, forged, await _code(secret))

        assert response.status_code == 401
        assert _body(response) == GENERIC_MFA_FAILURE
        assert checks.count == 0
        assert await _count(db_session, RefreshToken, user_id=victim.id) == 0


# ── The owner's own device is not shut out of the second factor ──────────────


class TestTheOwnersVerifiedDeviceKeepsItsSecondFactor:
    """Reaching the code step takes only the password. If every code drew on
    one budget for the account, somebody who had stolen the password — and
    nothing else — could spend that budget and keep the owner out for as long
    as they cared to: the old lockout, one step later.

    So a browser that has itself passed this account's second factor has a
    budget of its own. Every other origin — including a browser trusted only
    through an emailed link or a live session — shares the account's.
    """

    async def test_the_owner_signs_in_while_an_attacker_with_the_password_exhausts_the_budget(
        self,
        application: FastAPI,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        account = _mfa_account(mfa_user)

        async with _browser(application, SOURCE_B) as owner:
            assert (await _sign_in(owner, mfa_user, secret)).status_code == 200
            device = await _only_device(db_session, mfa_user.id)
            assert device.mfa_verified_at is not None
            checks = _CodeChecks(monkeypatch)

            await _spend_shared_mfa_budget(application, mfa_user, secret)

            # The shared budget really is gone: the right code, from an
            # address never seen before, is not even looked at.
            assert checks.count == MFA_ACCOUNT.burst
            assert await _budget_used(db_session, account) == MFA_ACCOUNT.burst
            async with _browser(application, SOURCE_E) as newcomer:
                shut_out = await _verify(
                    newcomer, await _ticket(newcomer, mfa_user), await _code(secret)
                )
            assert shut_out.status_code == 401
            assert _body(shut_out) == GENERIC_MFA_FAILURE
            assert checks.count == MFA_ACCOUNT.burst

            # The owner, on the browser that has passed the second factor
            # before, signs in — and keeps doing so while the attack goes on.
            rounds = 3
            for index in range(rounds):
                completed = await _sign_in(owner, mfa_user, secret)
                assert completed.status_code == 200, completed.text
                assert completed.json()["data"]["access_token"]

                async with _browser(application, f"198.51.100.{60 + index}") as attacker:
                    still_trying = await _verify(
                        attacker, await _ticket(attacker, mfa_user), await _code(secret)
                    )
                assert still_trying.status_code == 401
                assert _body(still_trying) == GENERIC_MFA_FAILURE

            # Only the owner's codes were evaluated after the budget ran out.
            assert checks.count == MFA_ACCOUNT.burst + rounds
            assert await _count(db_session, RefreshToken, user_id=mfa_user.id) == 1 + rounds
            # The attacker's refusals charged nothing; the owner's sign-ins
            # never touched the shared budget and left their own one full.
            assert await _budget_used(db_session, account) == MFA_ACCOUNT.burst
            assert await _budget_used(db_session, _mfa_device_cap(device)) == 0
            assert [d.id for d in await _devices(db_session, mfa_user.id)] == [device.id]

    async def test_a_device_trusted_only_through_an_emailed_reset_draws_on_the_shared_budget(
        self,
        application: FastAPI,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whoever can read the mailbox can complete a reset as often as they
        like. If that made a browser "the owner's device" for the second
        factor too, each reset would buy a private budget of fresh guesses.
        It does not: such a device has passed no second factor.
        """
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        raw = await _reset_token(db_session, mfa_user)

        async with _browser(application, SOURCE_B) as browser:
            reset = await browser.post(
                f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
            )
            assert reset.status_code == 200, reset.text
            device = await _only_device(db_session, mfa_user.id)
            assert device.mfa_verified_at is None

            # It is recognised — its codes are counted under the device, not
            # the address — and they are charged to the account's budget.
            ticket = await _ticket(browser, mfa_user, NEW_PASSWORD)
            await _verify(browser, ticket, _wrong_code(secret))
            as_device = AuthService._mfa_origin(mfa_user, SOURCE_B, device)  # noqa: SLF001
            assert await _failures(db_session, as_device) == 1
            assert await _failures(db_session, _mfa_origin(mfa_user, SOURCE_B)) == 0
            assert await _budget_used(db_session, _mfa_account(mfa_user)) == 1
            assert await _bucket_row(db_session, _mfa_device_cap(device)) is None

            # So once the shared budget is spent, from anywhere, this device
            # is refused like everybody else, right code or not.
            await db_session.execute(
                text("DELETE FROM auth_throttle_buckets WHERE key_hash = :key"),
                {"key": _mfa_account(mfa_user).key_hash},
            )
            await _saved(db_session)
            await _spend_shared_mfa_budget(application, mfa_user, secret, NEW_PASSWORD)
            checks = _CodeChecks(monkeypatch)

            refused = await _verify(browser, ticket, await _code(secret))

            assert refused.status_code == 401
            assert _body(refused) == GENERIC_MFA_FAILURE
            assert checks.count == 0
            assert (
                await _count(db_session, RefreshToken, user_id=mfa_user.id, is_revoked=False) == 0
            )
            assert (await _only_device(db_session, mfa_user.id)).mfa_verified_at is None

    async def test_a_replayed_refresh_token_buys_no_device_and_no_budget(
        self,
        application: FastAPI,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        """A stolen refresh token, replayed from the thief's browser. A
        session is not a credential check: it must not make that browser a
        trusted device — still less one that has passed the second factor —
        or each stolen session would mint devices with allowances of their own.
        """
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)

        async with (
            _browser(application, SOURCE_B) as owner,
            _browser(application, SOURCE_C) as thief,
        ):
            signed_in = await _sign_in(owner, mfa_user, secret)
            assert signed_in.status_code == 200, signed_in.text
            owners_device = await _only_device(db_session, mfa_user.id)

            refreshed = await thief.post(
                f"{AUTH}/refresh",
                json={"refresh_token": signed_in.json()["data"]["refresh_token"]},
            )

            assert refreshed.status_code == 200, refreshed.text
            assert "set-cookie" not in refreshed.headers
            assert not thief.cookies
            assert [d.id for d in await _devices(db_session, mfa_user.id)] == [owners_device.id]

            # With the password as well, the thief's codes are an outsider's:
            # counted under the address and charged to the shared budget.
            await _verify(thief, await _ticket(thief, mfa_user), _wrong_code(secret))

            assert await _failures(db_session, _mfa_origin(mfa_user, SOURCE_C)) == 1
            assert await _budget_used(db_session, _mfa_account(mfa_user)) == 1
            assert await _budget_used(db_session, _mfa_device_cap(owners_device)) == 0
            buckets = await db_session.execute(
                select(func.count())
                .select_from(AuthThrottleBucket)
                .where(AuthThrottleBucket.kind == BucketKind.MFA_DEVICE_CAP.value)
            )
            assert buckets.scalar_one() <= 1  # at most the owner's own

    async def test_disabling_mfa_ends_every_devices_verified_standing(
        self,
        application: FastAPI,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        """What a device proved about the old second factor says nothing about
        the next one. A device verified against a secret that has since been
        thrown away must earn its own budget again — against the new secret.
        """
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)

        async with _browser(application, SOURCE_B) as owner:
            signed_in = await _sign_in(owner, mfa_user, secret)
            assert signed_in.status_code == 200, signed_in.text
            session = {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"}
            device = await _only_device(db_session, mfa_user.id)
            assert device.mfa_verified_at is not None

            disabled = await owner.post(
                f"{AUTH}/mfa/disable",
                headers=session,
                json={"password": PASSWORD, "code": await _code(secret)},
            )

            assert disabled.status_code == 200, disabled.text
            device = await _only_device(db_session, mfa_user.id)  # still trusted for passwords
            assert device.mfa_verified_at is None

            # A new second factor is enrolled and confirmed; the device has
            # not passed *it*, so its codes draw on the shared budget ...
            enrolled = await owner.post(
                f"{AUTH}/mfa/enroll", headers=session, json={"password": PASSWORD}
            )
            assert enrolled.status_code == 200, enrolled.text
            new_secret = enrolled.json()["data"]["secret"]
            confirmed = await owner.post(
                f"{AUTH}/mfa/confirm", headers=session, json={"code": await _code(new_secret)}
            )
            assert confirmed.status_code == 200, confirmed.text
            assert (await _only_device(db_session, mfa_user.id)).mfa_verified_at is None

            ticket = await _ticket(owner, mfa_user)
            await _verify(owner, ticket, _wrong_code(new_secret))
            assert await _budget_used(db_session, _mfa_account(mfa_user)) == 1
            assert await _bucket_row(db_session, _mfa_device_cap(device)) is None

            # ... until it passes the new one.
            completed = await _verify(owner, ticket, await _code(new_secret))
            assert completed.status_code == 200, completed.text
            assert (await _only_device(db_session, mfa_user.id)).mfa_verified_at is not None

    async def test_confirming_a_second_factor_ends_every_devices_verified_standing(
        self,
        application: FastAPI,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        """Confirmation is where a new second factor starts to count, however
        the old one went away. A device still marked as verified at that
        moment was verified against something else."""
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)

        async with _browser(application, SOURCE_B) as owner:
            signed_in = await _sign_in(owner, mfa_user, secret)
            assert signed_in.status_code == 200, signed_in.text
            session = {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"}
            device = await _only_device(db_session, mfa_user.id)
            assert device.mfa_verified_at is not None
            # The second factor is switched off behind the service's back
            # (an operator, a data fix), leaving an enrolment to confirm.
            await db_session.execute(
                text("UPDATE users SET mfa_enabled = false WHERE id = :id"), {"id": mfa_user.id}
            )
            await _saved(db_session)
            await db_session.refresh(mfa_user)

            confirmed = await owner.post(
                f"{AUTH}/mfa/confirm", headers=session, json={"code": await _code(secret)}
            )

            assert confirmed.status_code == 200, confirmed.text
            assert (await _only_device(db_session, mfa_user.id)).mfa_verified_at is None
            await _verify(owner, await _ticket(owner, mfa_user), _wrong_code(secret))
            assert await _budget_used(db_session, _mfa_account(mfa_user)) == 1
            assert await _bucket_row(db_session, _mfa_device_cap(device)) is None


# ── There is no "locked" account any more ────────────────────────────────────


class TestNobodyIsLockedOut:
    """Five wrong passwords used to lock an account for everyone for thirty
    minutes, so knowing a staff email was enough to keep its owner out.

    ``users.failed_login_attempts`` and ``users.locked_until`` are still in
    the table. Nothing reads them and nothing writes them.
    """

    @staticmethod
    async def _marked_locked(
        session: AsyncSession, hospital_id: uuid.UUID, **overrides: Any
    ) -> User:
        """A row as the old lockout left it: "locked" until tomorrow, 99 failures."""
        return await _make_user(
            session,
            hospital_id,
            locked_until=datetime.now(UTC) + timedelta(days=1),
            failed_login_attempts=99,
            **overrides,
        )

    async def test_a_row_still_marked_locked_signs_in_normally(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        legacy = await self._marked_locked(db_session, hospital_id)

        response = await _login(api, legacy.email)

        assert response.status_code == 200, response.text
        token = response.json()["data"]["access_token"]
        me = await api.get(f"{USERS}/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert await _count(db_session, RefreshToken, user_id=legacy.id) == 1
        assert "auth.login.success" in await _audit_actions(db_session, legacy.id)

    async def test_a_row_still_marked_locked_completes_mfa(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        secret = generate_totp_secret()
        legacy = await self._marked_locked(
            db_session, hospital_id, mfa_enabled=True, mfa_secret=encrypt_mfa_secret(secret)
        )

        response = await _verify(api, await _ticket(api, legacy), await _code(secret))

        assert response.status_code == 200, response.text
        assert response.json()["data"]["access_token"]

    async def test_a_row_still_marked_locked_is_not_let_in_with_a_wrong_password(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Ignoring the old columns must not mean ignoring the password."""
        legacy = await self._marked_locked(db_session, hospital_id)

        response = await _login(api, legacy.email, WRONG_PASSWORD)

        assert response.status_code == 401
        assert _body(response) == GENERIC_LOGIN_FAILURE
        assert await _failure_reasons(db_session, legacy.id) == ["invalid_password"]

    async def test_failed_attempts_never_write_the_old_lockout_columns(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        plain = await _make_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)

        for _ in range(PW_PAIR.free + 3):
            await _login(api, plain.email, WRONG_PASSWORD)
        for _ in range(MFA_ORIGIN.free + 3):
            await _verify(api, ticket, _wrong_code(secret))

        rows = await db_session.execute(
            text("SELECT failed_login_attempts, locked_until FROM users WHERE id IN (:a, :b)"),
            {"a": plain.id, "b": mfa_user.id},
        )
        assert [tuple(row) for row in rows] == [(0, None), (0, None)]

    async def test_a_strangers_wrong_passwords_do_not_keep_the_owner_out(
        self,
        application: FastAPI,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
    ) -> None:
        """The lockout attack itself: hammer a known address, deny its owner."""
        async with _browser(application, SOURCE_B) as stranger:
            for _ in range(PW_PAIR.free + 10):
                assert (await _login(stranger, user.email, WRONG_PASSWORD)).status_code == 401
            # The stranger has slowed nobody down but themselves ...
            strangers_lucky_guess = await _login(stranger, user.email)

        owner = await _login(api, user.email)

        assert _body(strangers_lucky_guess) == GENERIC_LOGIN_FAILURE
        assert owner.status_code == 200, owner.text
        assert await _count(db_session, RefreshToken, user_id=user.id) == 1

    async def test_guessing_from_many_addresses_cannot_shut_out_a_browser_the_owner_has_used(
        self,
        application: FastAPI,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Guessing from many addresses is bounded by the account's shared
        budget — and exhausting that budget must not be the old lockout again.

        A browser the owner has already signed in from draws on a bucket of
        its own, which no outsider can reach.
        """
        account_budget = cast("Budget", POLICIES[BucketKind.PW_ACCOUNT])
        assert (await _login(api, user.email)).status_code == 200  # ``api`` is now trusted
        work = _Work(monkeypatch)
        per_source = PW_PAIR.free - 1  # no single address is ever made to wait
        sources = math.ceil(account_budget.burst / per_source) + 1

        for index in range(sources):
            async with _browser(application, f"203.0.113.{100 + index}") as attacker:
                for _ in range(per_source):
                    refused = await _login(attacker, user.email, WRONG_PASSWORD)
                    assert _body(refused) == GENERIC_LOGIN_FAILURE

        # The bound held: exactly the budget was evaluated, however many
        # addresses were used, and a newcomer with the right password is not
        # evaluated either.
        assert work.real == account_budget.burst
        async with _browser(application, SOURCE_E) as newcomer:
            assert (await _login(newcomer, user.email)).status_code == 401
        assert work.real == account_budget.burst

        # The owner, on the browser they have used before, is not affected.
        owner = await _login(api, user.email)
        assert owner.status_code == 200, owner.text
        assert await _count(db_session, RefreshToken, user_id=user.id) == 2


# ── F5: the MFA step re-checks the account ───────────────────────────────────


class TestMfaStepRechecksTheAccount:
    async def _assert_refused(
        self, api: AsyncClient, db_session: AsyncSession, user: User, ticket: str, secret: str
    ) -> None:
        response = await _verify(api, ticket, await _code(secret))

        assert response.status_code == 401
        assert _body(response) == GENERIC_MFA_FAILURE
        assert "access_token" not in response.text
        assert await _count(db_session, RefreshToken, user_id=user.id) == 0
        assert "auth.login.success" not in await _audit_actions(db_session, user.id)

    async def test_an_account_suspended_after_the_password_step_gets_no_session(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)
        mfa_user.status = UserStatus.SUSPENDED
        await db_session.flush()

        await self._assert_refused(api, db_session, mfa_user, ticket, secret)
        assert await _failure_reasons(db_session, mfa_user.id) == ["suspended"]

    async def test_an_account_deleted_after_the_password_step_gets_no_session(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)
        mfa_user.deleted_at = datetime.now(UTC)
        await db_session.flush()

        await self._assert_refused(api, db_session, mfa_user, ticket, secret)

    async def test_a_ticket_is_useless_once_mfa_is_switched_off(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """... and it charges nothing: with no second factor there is no
        code to guess, so a stale ticket must not be a way to spend the
        account's allowance or to start a wait for the address it is sent from.
        """
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)
        mfa_user.mfa_enabled = False
        await db_session.flush()

        for _ in range(MFA_ORIGIN.free + 1):
            assert _body(await _verify(api, ticket, _wrong_code(secret))) == GENERIC_MFA_FAILURE
        await self._assert_refused(api, db_session, mfa_user, ticket, secret)

        # Not charged, and not even counted: the buckets were never opened.
        assert await _bucket_row(db_session, _mfa_origin(mfa_user, LOCAL_SOURCE)) is None
        assert await _bucket_row(db_session, _mfa_account(mfa_user)) is None
        assert await _failure_reasons(db_session, mfa_user.id) == []
        # Switched back on, the same ticket and the right code work at once:
        # the stale attempts left no wait behind.
        mfa_user.mfa_enabled = True
        await db_session.flush()
        assert (await _verify(api, ticket, await _code(secret))).status_code == 200

    async def test_every_refusal_at_the_mfa_step_is_identical(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Wrong code, suspended, deleted, MFA off: one status, one body."""
        bodies: list[dict[str, Any]] = []

        plain, secret = await _make_mfa_user(db_session, hospital_id)
        bodies.append(_body(await _verify(api, await _ticket(api, plain), _wrong_code(secret))))

        suspended, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, suspended)
        suspended.status = UserStatus.SUSPENDED
        await db_session.flush()
        bodies.append(_body(await _verify(api, ticket, await _code(secret))))

        deleted, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, deleted)
        deleted.deleted_at = datetime.now(UTC)
        await db_session.flush()
        bodies.append(_body(await _verify(api, ticket, await _code(secret))))

        switched_off, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, switched_off)
        switched_off.mfa_enabled = False
        await db_session.flush()
        bodies.append(_body(await _verify(api, ticket, await _code(secret))))

        assert all(body == GENERIC_MFA_FAILURE for body in bodies)

    async def test_an_untouched_account_still_completes_mfa(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)

        response = await _verify(api, await _ticket(api, mfa_user), await _code(secret))

        assert response.status_code == 200, response.text
        assert response.json()["data"]["access_token"]
        assert "auth.login.success" in await _audit_actions(db_session, mfa_user.id)

    async def test_an_access_token_is_not_accepted_as_an_mfa_ticket(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        access_token = create_access_token(user_id=mfa_user.id, hospital_id=hospital_id)

        response = await _verify(api, access_token, await _code(secret))

        assert response.status_code == 401
        assert "access_token" not in response.text


# ── No session for an unusable account, by any route ─────────────────────────


class TestNoCredentialsForUnusableAccounts:
    async def test_an_invited_account_with_a_known_password_cannot_sign_in(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Login requires ``ACTIVE``; it does not merely exclude ``SUSPENDED``."""
        invited = await _make_user(db_session, hospital_id, status=UserStatus.INVITED)

        response = await _login(api, invited.email)

        assert response.status_code == 401
        assert _body(response) == GENERIC_LOGIN_FAILURE
        assert await _count(db_session, RefreshToken, user_id=invited.id) == 0

    async def test_a_suspended_account_gets_no_reset_token_and_cannot_redeem_one(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        suspended = await _make_user(db_session, hospital_id, status=UserStatus.SUSPENDED)
        raw = await _reset_token(db_session, suspended)
        user_id, email, original_hash = suspended.id, suspended.email, suspended.password_hash

        forgot = await api.post(f"{AUTH}/password/forgot", json={"email": email})
        redeemed = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
        )

        assert forgot.status_code == 200
        assert forgot.json()["message"] == FORGOT_RESPONSE
        assert await _count(db_session, PasswordResetToken, user_id=user_id) == 1  # ours
        assert redeemed.status_code == 401
        assert redeemed.json()["message"] == "Invalid or expired password reset token."
        stored = await db_session.execute(
            text("SELECT password_hash, status FROM users WHERE id = :id"), {"id": user_id}
        )
        assert tuple(stored.one()) == (original_hash, "suspended")

        # The refused link stays spent — durably, not only until the request's
        # transaction is thrown away — so it does not come back to life when
        # the account is reinstated.
        await db_session.rollback()  # whatever the refusal did not commit is gone
        spent = await db_session.execute(
            select(PasswordResetToken.used_at).where(PasswordResetToken.user_id == user_id)
        )
        assert [used_at is not None for used_at in spent.scalars()] == [True]
        await db_session.execute(
            text("UPDATE users SET status = 'active' WHERE id = :id"), {"id": user_id}
        )
        await _saved(db_session)
        after_reinstatement = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
        )
        assert after_reinstatement.status_code == 401
        assert _body(after_reinstatement) == _body(redeemed)
        stored = await db_session.execute(
            text("SELECT password_hash FROM users WHERE id = :id"), {"id": user_id}
        )
        assert stored.scalar_one() == original_hash
        assert (await _login(api, email)).status_code == 200  # the old password still stands

    async def test_redeeming_a_link_takes_the_account_row_before_it_spends_the_token(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Suspending an account and sending an invitation again both take
        the account row and then its tokens. Redemption used to take them the
        other way round, so an administrator pressing "resend" while the
        invitee activated could leave the two waiting on each other until the
        database killed one. Account first, everywhere; and the token is
        still spent by the one atomic statement, after the account is held.
        """
        invited = await _make_user(db_session, hospital_id, status=UserStatus.INVITED)
        raw = await _reset_token(db_session, invited, minutes=60)
        order: list[str] = []
        real_lookup = PasswordResetTokenRepository.get_valid_token
        real_consume = PasswordResetTokenRepository.consume
        real_lock = UserRepository.lock_for_authentication

        async def _lookup(
            repository: PasswordResetTokenRepository, token_hash: str
        ) -> PasswordResetToken | None:
            order.append("read token")
            return await real_lookup(repository, token_hash)

        async def _consume(
            repository: PasswordResetTokenRepository, token_hash: str
        ) -> PasswordResetToken | None:
            order.append("spend token")
            return await real_consume(repository, token_hash)

        async def _lock(repository: UserRepository, user_id: uuid.UUID) -> User | None:
            order.append("lock account")
            return await real_lock(repository, user_id)

        monkeypatch.setattr(PasswordResetTokenRepository, "get_valid_token", _lookup)
        monkeypatch.setattr(PasswordResetTokenRepository, "consume", _consume)
        monkeypatch.setattr(UserRepository, "lock_for_authentication", _lock)

        first = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
        )
        second = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": PASSWORD}
        )

        assert first.status_code == 200, first.text
        assert order[:3] == ["read token", "lock account", "spend token"]
        # The plain read decides nothing: a spent token is refused, and no
        # account row is locked on the strength of it.
        assert second.status_code == 401
        assert order[3:] == ["read token"]
        assert (await _login(api, invited.email, NEW_PASSWORD)).status_code == 200

    async def test_an_invited_account_gets_nothing_from_forgot_password(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Keeping a lapsed invitation alive for ever, anonymously.

        An invited account's link comes from an invitation, which an
        administrator sends and can decline to send again. Anyone who knows
        the address must not be able to mint a fresh activation link for it.
        """
        invited = await _make_user(db_session, hospital_id, status=UserStatus.INVITED)
        active = await _make_user(db_session, hospital_id)

        for _ in range(3):
            answer = await api.post(f"{AUTH}/password/forgot", json={"email": invited.email})
            control = await api.post(f"{AUTH}/password/forgot", json={"email": active.email})
            assert answer.status_code == control.status_code == 200
            assert _body(answer) == _body(control)

        assert await _count(db_session, PasswordResetToken, user_id=invited.id) == 0
        assert "auth.password.reset_requested" not in await _audit_actions(db_session, invited.id)
        # The control shows the request would have issued one had it been allowed.
        assert await _count(db_session, PasswordResetToken, user_id=active.id) == 3

    async def test_without_a_mail_transport_no_reset_token_is_minted(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A token nobody can be sent is a credential lying in the database."""
        monkeypatch.setattr(settings, "SMTP_HOST", None)

        response = await api.post(f"{AUTH}/password/forgot", json={"email": user.email})

        assert response.status_code == 200
        assert response.json()["message"] == FORGOT_RESPONSE
        assert await _count(db_session, PasswordResetToken, user_id=user.id) == 0

    async def test_the_reset_token_travels_only_in_the_fragment_of_the_emailed_link(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A token in a query string ends up in access logs, proxies and Referer
        headers. In the fragment it is never sent to any server."""
        requests: list[NotificationRequest] = []
        outcomes: list[bool] = []
        real_deliver = NotificationService.deliver_credential

        async def _deliver(service: NotificationService, request: NotificationRequest) -> bool:
            requests.append(request)
            outcomes.append(await real_deliver(service, request))
            return outcomes[-1]

        async def _notify(service: NotificationService, request: NotificationRequest) -> None:
            pytest.fail("a credential was sent through the channel that reports no outcome")

        monkeypatch.setattr(NotificationService, "deliver_credential", _deliver)
        monkeypatch.setattr(NotificationService, "notify", _notify)

        response = await api.post(f"{AUTH}/password/forgot", json={"email": user.email})

        assert response.status_code == 200
        (request,) = requests
        assert outcomes == [True]  # the email really was queued
        link = request.secret_variables["action_url"]
        page, separator, raw = link.partition("#token=")
        assert separator == "#token="
        assert page.endswith("/reset-password")
        assert "?" not in link
        # The token is in the email-only variables and nowhere else ...
        assert raw not in json.dumps(request.variables)
        assert raw not in (request.link or "")
        assert raw not in response.text
        # ... only its hash is stored, and it is the real thing: it redeems.
        stored = await db_session.execute(
            select(PasswordResetToken.token_hash).where(PasswordResetToken.user_id == user.id)
        )
        assert stored.scalars().all() == [security_module.hash_token(raw)]
        redeemed = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
        )
        assert redeemed.status_code == 200, redeemed.text

    @pytest.mark.parametrize("outcome", [False, None, "queued", 1])
    async def test_a_reset_link_no_email_carries_is_dead_before_the_request_ends(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
        outcome: object,
    ) -> None:
        """A valid credential that nobody was sent is one only an attacker
        with database or log access can use. If the email was not queued —
        or the notifier did not positively say that it was — the link is
        spent on the spot, and the caller is told nothing different.
        """
        requests: list[NotificationRequest] = []

        async def _deliver(service: NotificationService, request: NotificationRequest) -> object:
            requests.append(request)
            return outcome

        monkeypatch.setattr(NotificationService, "deliver_credential", _deliver)
        user_id, email, original_hash = user.id, user.email, user.password_hash
        await _saved(db_session)

        response = await api.post(f"{AUTH}/password/forgot", json={"email": email})
        unknown = await api.post(f"{AUTH}/password/forgot", json={"email": _email("nobody")})

        assert response.status_code == 200
        assert _body(response) == _body(unknown)
        (request,) = requests
        raw = request.secret_variables["action_url"].partition("#token=")[2]
        assert raw
        await db_session.rollback()  # only what the request committed remains
        spent = await db_session.execute(
            select(PasswordResetToken.used_at).where(PasswordResetToken.user_id == user_id)
        )
        assert [used_at is not None for used_at in spent.scalars()] == [True]
        redeemed = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
        )
        assert redeemed.status_code == 401
        assert redeemed.json()["message"] == "Invalid or expired password reset token."
        stored = await db_session.execute(
            text("SELECT password_hash FROM users WHERE id = :id"), {"id": user_id}
        )
        assert stored.scalar_one() == original_hash

    async def test_forgot_password_answers_every_account_state_identically(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        addresses = [
            _email("nobody"),
            (await _make_user(db_session, hospital_id)).email,
            (await _make_user(db_session, hospital_id, status=UserStatus.SUSPENDED)).email,
            (await _make_user(db_session, hospital_id, status=UserStatus.INVITED)).email,
            (await _make_user(db_session, hospital_id, deleted_at=datetime.now(UTC))).email,
            (await _make_user(db_session, other_hospital_id)).email,
        ]
        await _set_hospital_active(db_session, other_hospital_id, active=False)

        responses = [
            await api.post(f"{AUTH}/password/forgot", json={"email": address})
            for address in addresses
        ]

        assert {response.status_code for response in responses} == {200}
        assert len({json.dumps(_body(response), sort_keys=True) for response in responses}) == 1

    async def test_an_invited_account_can_still_be_activated_by_reset(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Redeeming an invitation still activates the account — and only once."""
        invited = await _make_user(db_session, hospital_id, status=UserStatus.INVITED)
        raw = await _reset_token(db_session, invited, minutes=60)

        response = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}
        )
        again = await api.post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": PASSWORD}
        )

        assert response.status_code == 200, response.text
        assert again.status_code == 401
        assert (await _fresh(db_session, invited)).status is UserStatus.ACTIVE
        assert (await _login(api, invited.email, NEW_PASSWORD)).status_code == 200


class TestNothingSensitiveInLogsOrResponses:
    async def test_failed_logins_log_no_password_email_or_token(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorder = RecordingLogger()
        monkeypatch.setattr(auth_service_module, "logger", recorder)
        monkeypatch.setattr(user_repository_module, "logger", recorder)

        await _login(api, user.email, WRONG_PASSWORD)
        await _login(api, _email("nobody"), WRONG_PASSWORD)
        session_data = (await _login(api, user.email)).json()["data"]

        logged = json.dumps(recorder.entries, default=str)
        assert recorder.entries
        for secret_value in (
            PASSWORD,
            WRONG_PASSWORD,
            user.email,
            user.password_hash,
            session_data["access_token"],
            session_data["refresh_token"],
            settings.APP_SECRET_KEY.get_secret_value(),
        ):
            assert secret_value not in logged

    async def test_a_login_response_carries_no_hash_secret_or_key(
        self, api: AsyncClient, user: User
    ) -> None:
        response = await _login(api, user.email)

        assert response.status_code == 200
        for forbidden in (
            user.password_hash,
            "password_hash",
            "mfa_secret",
            settings.APP_SECRET_KEY.get_secret_value(),
        ):
            assert forbidden not in response.text


# ── F3, second half: every rejection also takes the same time ────────────────


class _Waits:
    """Records each uniform-duration wait instead of actually sleeping."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, session: AsyncSession, floor: float
    ) -> None:
        self.durations: list[float] = []
        self.in_transaction: list[bool] = []
        monkeypatch.setattr(settings, "AUTH_FAILURE_MIN_SECONDS", floor)

        async def _sleep(seconds: float) -> None:
            self.durations.append(seconds)
            self.in_transaction.append(session.in_transaction())

        monkeypatch.setattr(auth_service_module, "_sleep", _sleep)

    def reset(self) -> None:
        self.durations.clear()
        self.in_transaction.clear()


class TestUniformFailureDuration:
    """The dummy hash equalises the hashing; this equalises everything else.

    Measured before this existed: an unknown email answered in about 22 ms and
    a wrong password for a real account in about 39 ms, because the real
    account's failure also writes an audit row. For forgot-password the gap
    was 3 ms against 43 ms.
    """

    FLOOR = 30.0  # far above any real work, so every padded path must wait

    async def test_every_rejected_login_waits_out_the_floor(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cases = await TestUniformLoginFailure()._cases(db_session, hospital_id, other_hospital_id)  # noqa: SLF001
        waits = _Waits(monkeypatch, db_session, self.FLOOR)

        for name, (email, password) in cases.items():
            waits.reset()
            response = await _login(api, email, password)
            assert response.status_code == 401, name
            assert len(waits.durations) == 1, name
            # work + wait == floor, so the total is the same for every case.
            assert 0 < waits.durations[0] <= self.FLOOR * 1.2, name

    async def test_a_successful_login_is_not_delayed(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        waits = _Waits(monkeypatch, db_session, self.FLOOR)

        assert (await _login(api, user.email)).status_code == 200
        assert waits.durations == []

    async def test_forgot_password_takes_the_same_time_for_everyone(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        suspended = await _make_user(db_session, hospital_id, status=UserStatus.SUSPENDED)
        deleted = await _make_user(db_session, hospital_id, deleted_at=datetime.now(UTC))
        waits = _Waits(monkeypatch, db_session, self.FLOOR)

        for address in (user.email, _email("nobody"), suspended.email, deleted.email):
            waits.reset()
            response = await api.post(f"{AUTH}/password/forgot", json={"email": address})
            assert response.status_code == 200
            assert len(waits.durations) == 1
            assert 0 < waits.durations[0] <= self.FLOOR * 1.2

    async def test_every_rejected_mfa_attempt_waits_out_the_floor(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)
        waits = _Waits(monkeypatch, db_session, self.FLOOR)

        wrong = await _verify(api, ticket, _wrong_code(secret))
        bad_ticket = await _verify(api, "not-a-ticket", "123456")
        mfa_user.status = UserStatus.SUSPENDED
        await db_session.flush()
        suspended = await _verify(api, ticket, await _code(secret))

        assert [r.status_code for r in (wrong, bad_ticket, suspended)] == [401, 401, 401]
        assert len(waits.durations) == 3
        assert all(0 < duration <= self.FLOOR * 1.2 for duration in waits.durations)

    async def test_nothing_is_held_while_waiting(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The transaction — and with it every lock on a throttle counter — is
        over before the wait starts, so a stream of failed attempts cannot pin
        pooled connections, or hold the counters of somebody else's address
        locked, while the attacker's own requests sleep."""
        waits = _Waits(monkeypatch, db_session, self.FLOOR)

        await _login(api, user.email, WRONG_PASSWORD)
        await _login(api, _email("nobody"), WRONG_PASSWORD)
        for _ in range(PW_PAIR.free):  # ... the last of these is throttled
            await _login(api, user.email, WRONG_PASSWORD)
        await api.post(f"{AUTH}/password/forgot", json={"email": user.email})

        assert waits.in_transaction == [False] * (PW_PAIR.free + 3)

    async def test_what_a_rejection_must_keep_is_already_saved_before_the_wait(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A client that hangs up during the wait must not get its attempt
        forgotten: the charge to the throttle and the audit entry are both
        committed before the wait begins."""
        waits = _Waits(monkeypatch, db_session, self.FLOOR)
        user_id, email = user.id, user.email
        pair = bucket(BucketKind.PW_PAIR, email, LOCAL_SOURCE)
        account = bucket(BucketKind.PW_ACCOUNT, email)
        seen_at_the_wait: list[tuple[int, int, list[str]]] = []
        recording_sleep = auth_service_module._sleep  # noqa: SLF001 — the spy _Waits installed

        async def _sleep(seconds: float) -> None:
            await recording_sleep(seconds)
            await db_session.rollback()  # whatever was not committed is gone
            seen_at_the_wait.append(
                (
                    await _failures(db_session, pair),
                    await _budget_used(db_session, account),
                    await _failure_reasons(db_session, user_id),
                )
            )

        monkeypatch.setattr(auth_service_module, "_sleep", _sleep)

        await _login(api, email, WRONG_PASSWORD)

        assert waits.in_transaction == [False]
        assert seen_at_the_wait == [(1, 1, ["invalid_password"])]
        assert await _failure_reasons(db_session, user_id) == ["invalid_password"]

    async def test_a_throttled_attempt_waits_exactly_like_an_evaluated_one(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refusal that skips the password hash and the code check would
        otherwise answer at once — telling the guesser, by the clock, that it
        is no longer being listened to."""
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        ticket = await _ticket(api, mfa_user)
        work = _Work(monkeypatch)
        checks = _CodeChecks(monkeypatch)
        for _ in range(PW_PAIR.free):
            await _login(api, user.email, WRONG_PASSWORD)
        for _ in range(MFA_ORIGIN.free):
            await _verify(api, ticket, _wrong_code(secret))
        work.reset()
        checks.count = 0
        waits = _Waits(monkeypatch, db_session, self.FLOOR)

        throttled_login = await _login(api, user.email)
        throttled_code = await _verify(api, ticket, await _code(secret))

        assert [throttled_login.status_code, throttled_code.status_code] == [401, 401]
        assert (work.total, checks.count) == (0, 0)  # neither was evaluated
        assert len(waits.durations) == 2
        assert all(0 < duration <= self.FLOOR * 1.2 for duration in waits.durations)
        assert waits.in_transaction == [False, False]

    async def test_work_that_outruns_the_floor_is_held_to_the_next_multiple_of_it(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Making the server slow must not switch the padding off.

        If an answer that took longer than the floor were released at once,
        "slower than the floor" would itself say that real work was done — and
        a burst sent to load the server would be the way to see it. Such an
        answer is held to the next whole multiple of the floor instead.
        """
        floor = 2.0
        waits = _Waits(monkeypatch, db_session, floor)
        commits: list[bool] = []

        async def _commit() -> None:
            commits.append(True)

        service = cast("AuthService", SimpleNamespace(_uow=SimpleNamespace(commit=_commit)))
        took = 2.25 * floor  # the work ran past two floors

        await AuthService._wait_out_uniform_duration(service, time.monotonic() - took)  # noqa: SLF001

        assert commits == [True]  # the transaction is ended before waiting
        (wait,) = waits.durations
        total = took + wait
        jitter = floor * 0.2
        assert 3 * floor - 0.1 <= total <= 3 * floor + jitter
        # Just short of a multiple, the answer still goes out at that multiple.
        waits.reset()
        await AuthService._wait_out_uniform_duration(service, time.monotonic() - 0.9 * floor)  # noqa: SLF001
        (wait,) = waits.durations
        assert floor - 0.1 <= 0.9 * floor + wait <= floor + jitter

    async def test_each_wait_carries_a_different_random_extra(
        self, api: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identical requests are not padded to identical totals."""
        waits = _Waits(monkeypatch, db_session, self.FLOOR)

        for _ in range(12):
            await _login(api, _email("nobody"), WRONG_PASSWORD)

        assert len(waits.durations) == 12
        assert max(waits.durations) - min(waits.durations) > self.FLOOR * 0.02
        assert max(waits.durations) <= self.FLOOR * 1.2

    async def test_with_the_floor_at_zero_nothing_waits(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        waits = _Waits(monkeypatch, db_session, 0.0)

        await _login(api, user.email, WRONG_PASSWORD)

        assert waits.durations == []

    async def test_the_default_floor_is_on(self) -> None:
        from app.core.config import Settings

        field = Settings.model_fields["AUTH_FAILURE_MIN_SECONDS"]
        assert field.default == 0.5


# ── Nothing an attempt holds can be used against anyone else ─────────────────


@pytest_asyncio.fixture
async def other_connection(db_engine: Any) -> AsyncGenerator[Any]:
    """A second, independent database connection — "another request"."""
    async with db_engine.connect() as connection:
        yield connection


async def _hold_claim(connection: Any, email: str) -> None:
    """Hold, from another connection, the per-address claim a request would take."""
    key = UserRepository.authentication_claim_key(email)
    await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})


async def _release_claim(connection: Any, email: str) -> None:
    key = UserRepository.authentication_claim_key(email)
    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


class _AccountAccess:
    """Records how requests reach for an account while they are served."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.claims = 0
        self.lookups = 0
        self.locking_lookups = 0
        self.row_locks = 0
        real_claim = UserRepository.claim_authentication_attempt
        real_lookup = UserRepository.get_by_email_cross_tenant
        real_lock = UserRepository.lock_for_authentication

        async def _claim(repository: UserRepository, email: str) -> bool:
            self.claims += 1
            return await real_claim(repository, email)

        async def _lookup(
            repository: UserRepository, email: str, *, for_update: bool = False
        ) -> User | None:
            self.lookups += 1
            self.locking_lookups += int(for_update)
            return await real_lookup(repository, email, for_update=for_update)

        async def _lock(repository: UserRepository, user_id: uuid.UUID) -> User | None:
            self.row_locks += 1
            return await real_lock(repository, user_id)

        monkeypatch.setattr(UserRepository, "claim_authentication_attempt", _claim)
        monkeypatch.setattr(UserRepository, "get_by_email_cross_tenant", _lookup)
        monkeypatch.setattr(UserRepository, "lock_for_authentication", _lock)


class TestLoginHasNoInFlightExclusivity:
    """Sign-in used to allow one attempt per address at a time and refuse the
    rest. Anyone could therefore keep an account's owner out simply by keeping
    a request for their address in flight — no password needed.

    Now an attempt is never refused because another one exists, and an attempt
    that has not proved the password takes no claim on the address and no lock
    on the account row for anyone else to queue behind.
    """

    async def test_the_owner_signs_in_while_another_request_holds_their_address(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        other_connection: Any,
    ) -> None:
        await _hold_claim(other_connection, user.email)
        try:
            response = await _login(api, user.email)
        finally:
            await _release_claim(other_connection, user.email)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["access_token"]
        assert await _count(db_session, RefreshToken, user_id=user.id) == 1

    async def test_an_attempt_made_meanwhile_is_evaluated_not_waved_away(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        other_connection: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not being exclusive must not mean not being counted: the attempt is
        judged on its password, charged, and audited like any other."""
        work = _Work(monkeypatch)
        unknown = _email("nobody")
        for address in (user.email, unknown):
            await _hold_claim(other_connection, address)
        try:
            real = await _login(api, user.email, WRONG_PASSWORD)
            ghost = await _login(api, unknown, WRONG_PASSWORD)
        finally:
            for address in (user.email, unknown):
                await _release_claim(other_connection, address)

        assert real.status_code == ghost.status_code == 401
        assert _body(real) == _body(ghost) == GENERIC_LOGIN_FAILURE
        assert (work.real, work.dummy) == (1, 1)
        assert await _failure_reasons(db_session, user.id) == ["invalid_password"]
        pair = bucket(BucketKind.PW_PAIR, user.email, LOCAL_SOURCE)
        assert await _failures(db_session, pair) == 1

    async def test_the_mfa_step_is_not_exclusive_either(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_connection: Any,
    ) -> None:
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        await _hold_claim(other_connection, mfa_user.email)
        try:
            ticket = await _ticket(api, mfa_user)
            response = await _verify(api, ticket, await _code(secret))
        finally:
            await _release_claim(other_connection, mfa_user.email)

        assert response.status_code == 200, response.text

    async def test_an_unproven_attempt_claims_nothing_and_locks_no_account_row(
        self,
        application: FastAPI,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whatever a request locks, the next request for that account waits
        behind. So nothing is locked until the caller has proved a credential:
        wrong passwords, unknown addresses, unusable accounts, wrong codes and
        throttled attempts — of either kind — lock nothing."""
        mfa_user, secret = await _make_mfa_user(db_session, hospital_id)
        suspended = await _make_user(db_session, hospital_id, status=UserStatus.SUSPENDED)
        ticket = await _ticket(api, mfa_user)
        access = _AccountAccess(monkeypatch)

        for _ in range(PW_PAIR.free + 2):  # the last two are throttled
            await _login(api, user.email, WRONG_PASSWORD)
        await _login(api, _email("nobody"), WRONG_PASSWORD)
        await _login(api, suspended.email)
        await _login(api, mfa_user.email, WRONG_PASSWORD)
        for _ in range(MFA_ORIGIN.free + 2):  # the last two are throttled
            await _verify(api, ticket, _wrong_code(secret))
        throttled_but_right = await _verify(api, ticket, await _code(secret))

        assert throttled_but_right.status_code == 401
        assert access.lookups == PW_PAIR.free + 2 + 3
        assert (access.claims, access.locking_lookups, access.row_locks) == (0, 0, 0)

        # Only a caller who has proved the password reaches the row lock —
        # after the fact, and once: to issue the session, or the MFA ticket,
        # against the account as it is at that moment.
        async with _browser(application, SOURCE_B) as owner:
            assert (await _login(owner, user.email)).status_code == 200
            assert (access.claims, access.locking_lookups, access.row_locks) == (0, 0, 1)
            proven = await _login(owner, mfa_user.email)
            assert proven.status_code == 200
            assert "mfa_ticket" in proven.json()["data"]
            assert (access.claims, access.locking_lookups, access.row_locks) == (0, 0, 2)
            # ... and a wrong code sent with that ticket locks nothing more.
            await _verify(owner, proven.json()["data"]["mfa_ticket"], _wrong_code(secret))
        assert (access.claims, access.locking_lookups, access.row_locks) == (0, 0, 2)


class TestForgotPasswordUnderABurst:
    """A burst of reset requests used to answer slower for a real account than
    for an unknown email: each one for a real account wrote a token, an audit
    entry and an email. Measured with 40 parallel requests: 0.56 s against
    1.86 s. A real address must cost no more than an unknown one.
    """

    @staticmethod
    def _notifications(monkeypatch: pytest.MonkeyPatch) -> list[NotificationRequest]:
        """Every credential email requested, through the one channel allowed
        to carry a credential (``deliver_credential``)."""
        seen: list[NotificationRequest] = []
        real_deliver = NotificationService.deliver_credential

        async def _deliver(service: NotificationService, request: NotificationRequest) -> bool:
            seen.append(request)
            return await real_deliver(service, request)

        monkeypatch.setattr(NotificationService, "deliver_credential", _deliver)
        return seen

    async def test_a_simultaneous_request_does_no_work_for_a_real_address_or_an_unknown_one(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        other_connection: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        unknown = _email("nobody")
        access = _AccountAccess(monkeypatch)
        notified = self._notifications(monkeypatch)
        answers = []
        for address in (user.email, unknown):
            await _hold_claim(other_connection, address)
            try:
                answers.append(await api.post(f"{AUTH}/password/forgot", json={"email": address}))
            finally:
                await _release_claim(other_connection, address)

        assert [answer.status_code for answer in answers] == [200, 200]
        assert _body(answers[0]) == _body(answers[1])
        assert answers[0].json()["message"] == FORGOT_RESPONSE
        # Neither request so much as looked the address up.
        assert (access.claims, access.lookups) == (2, 0)
        assert notified == []
        assert await _count(db_session, PasswordResetToken, user_id=user.id) == 0
        assert "auth.password.reset_requested" not in await _audit_actions(db_session, user.id)

    async def test_the_claim_is_per_address_and_ignores_case(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        hospital_id: uuid.UUID,
        other_connection: Any,
    ) -> None:
        """Typing the address in another case must not get a second request in,
        and holding one address must not stop anybody else's reset."""
        colleague = await _make_user(db_session, hospital_id)
        await _hold_claim(other_connection, user.email.upper())
        try:
            await api.post(f"{AUTH}/password/forgot", json={"email": user.email})
            await api.post(f"{AUTH}/password/forgot", json={"email": user.email.title()})
            await api.post(f"{AUTH}/password/forgot", json={"email": colleague.email})
        finally:
            await _release_claim(other_connection, user.email.upper())

        assert await _count(db_session, PasswordResetToken, user_id=user.id) == 0
        assert await _count(db_session, PasswordResetToken, user_id=colleague.id) == 1

    async def test_the_address_is_served_again_once_the_other_request_has_ended(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        other_connection: Any,
    ) -> None:
        """The claim is not a way to deny someone their reset for good."""
        await _hold_claim(other_connection, user.email)
        await api.post(f"{AUTH}/password/forgot", json={"email": user.email})
        await _release_claim(other_connection, user.email)
        assert await _count(db_session, PasswordResetToken, user_id=user.id) == 0

        await api.post(f"{AUTH}/password/forgot", json={"email": user.email})

        assert await _count(db_session, PasswordResetToken, user_id=user.id) == 1

    async def test_a_burst_for_a_real_address_mints_a_bounded_number_of_links(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Burying a mailbox, and filling the token table, through an anonymous
        endpoint. After a few links per token lifetime a request for a real
        address writes nothing at all — exactly like one for an unknown address
        — and the answers never differ."""
        limit = 3  # stated here, not read from the code: raising it must fail this test
        unknown = _email("nobody")
        notified = self._notifications(monkeypatch)

        real = [
            await api.post(f"{AUTH}/password/forgot", json={"email": user.email})
            for _ in range(limit + 7)
        ]
        ghost = [
            await api.post(f"{AUTH}/password/forgot", json={"email": unknown})
            for _ in range(limit + 7)
        ]

        assert {response.status_code for response in (*real, *ghost)} == {200}
        assert len({json.dumps(_body(r), sort_keys=True) for r in (*real, *ghost)}) == 1
        assert await _count(db_session, PasswordResetToken, user_id=user.id) == limit
        assert len(notified) == limit
        requested = [
            action
            for action in await _audit_actions(db_session, user.id)
            if action == "auth.password.reset_requested"
        ]
        assert len(requested) == limit
        # The links already sent still work: the limit denies nobody a reset.
        assert await _count(db_session, PasswordResetToken, user_id=user.id, used_at=None) == limit

    # That a request releases its claim when it ends cannot be shown here: the
    # claim is transaction-scoped, and this suite runs each test inside one
    # outer transaction that only ends at teardown. It is checked against a
    # running server instead (live verification), where every request is its
    # own transaction.
