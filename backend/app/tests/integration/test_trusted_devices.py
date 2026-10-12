"""Trusted devices, attacked (P3b).

A trusted device is a browser that has completed a sign-in to an account. It
is not a credential; it only decides which throttle counters a password
attempt draws on (``app/services/auth_throttle.py``). That is still worth
attacking: whoever can make the server take *their* browser for a trusted
device of somebody else's account steps round the account's shared budget, and
whoever can make the server forget the owner's device brings the lockout back.

Every test drives the real application over HTTP against a real PostgreSQL.

**"Recognised", as an attacker would observe it.** With an account's shared
budget exhausted (twenty wrong passwords from twenty addresses), the right
password signs in only from a trusted device of that account. So "this cookie
is recognised for this account" is asserted as: budget exhausted, cookie
presented, right password → 200; and "not recognised" as → 401.

Addresses are varied through ``X-Forwarded-For`` with
``RATE_LIMIT_TRUST_PROXY_HEADER`` on and a trusted socket peer, i.e. through
the real resolver (see ``test_auth_throttle_attacks.py``). Time is moved only
through the throttle's clock or by editing ``trusted_devices`` rows.

**What makes a browser a trusted device.** A completed sign-in (both factors
where the account has two), a completed emailed link, a password change by a
signed-in user. Nothing else — in particular not a session refresh: a session
is not a credential check, and a stolen one must not be able to mint devices.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pyotp
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies.db import get_db_session
from app.core import security as security_module
from app.core.config import settings
from app.core.security import (
    create_access_token,
    encrypt_mfa_secret,
    generate_opaque_token,
    generate_totp_secret,
    hash_password,
    hash_token,
)
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.auth_throttle import AuthThrottleBucket, TrustedDevice
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.role import Role
from app.models.user import User, UserStatus
from app.repositories.auth_throttle_repository import (
    AuthThrottleRepository,
    TrustedDeviceRepository,
)
from app.services import auth_service as auth_service_module
from app.services.auth_throttle import Bucket, BucketKind, bucket
from app.tests.conftest import grant_permissions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

PASSWORD = "Str0ng!Passw0rd123"  # noqa: S105 — a test credential, not a real one
WRONG_PASSWORD = "Wr0ng!Passw0rd999"  # noqa: S105
NEW_PASSWORD = "An0ther!Passw0rd456"  # noqa: S105
AUTH = "/api/v1/auth"
USERS = "/api/v1/users"
DEV_COOKIE = "aetheris-device"
PRODUCTION_COOKIE = "__Host-aetheris-device"

# Documented numbers, restated on purpose (see test_auth_throttle_attacks.py).
ACCOUNT_BURST = 20
ACCOUNT_REFILL = timedelta(minutes=15)
DEVICE_FREE = 5
DEVICES_PER_ACCOUNT = 10
TOKENS_PER_COOKIE = 8
TOKENS_READ_PER_REQUEST = 64
MFA_ACCOUNT_BURST = 10
MFA_REFILL = timedelta(minutes=30)

_PROXY_PEER = ("127.0.0.1", 40000)
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _email(prefix: str = "staff") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@hospital.example"


def _source() -> str:
    return f"203.{secrets.randbelow(256)}.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}"


def _planted_token() -> str:
    """A well-formed device token the server never issued."""
    return secrets.token_urlsafe(32)


async def _make_user(session: AsyncSession, hospital_id: uuid.UUID, **overrides: Any) -> User:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "hospital_id": hospital_id,
        "email": _email(),
        "password_hash": hash_password(PASSWORD),
        "first_name": "Device",
        "last_name": "Tester",
        "password_changed_at": datetime.now(UTC),
    }
    values.update(overrides)
    user = User(**values)
    session.add(user)
    await session.flush()
    await session.refresh(user)
    # Committed (to the test's outer, rolled-back transaction) so that a
    # rollback inside the code under test cannot take the fixture rows with it.
    await session.commit()
    return user


async def _make_admin(session: AsyncSession, hospital_id: uuid.UUID) -> User:
    admin = await _make_user(session, hospital_id, email=_email("admin"))
    await grant_permissions(
        session,
        hospital_id=hospital_id,
        user_id=admin.id,
        codes=["user.deactivate", "user.reset_password", "user.read"],
    )
    await session.commit()
    return admin


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(user_id=user.id, hospital_id=user.hospital_id)
    return {"Authorization": f"Bearer {token}"}


async def _login(
    client: AsyncClient, email: str, password: str = PASSWORD, *, cookie: str | None = None
) -> Response:
    """POST /auth/login. ``cookie`` is a raw ``Cookie`` header, for a client with an empty jar."""
    headers = {"Cookie": cookie} if cookie is not None else None
    return await client.post(
        f"{AUTH}/login", json={"email": email, "password": password}, headers=headers
    )


def _set_cookies(response: Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _issued(response: Response, name: str = DEV_COOKIE) -> str | None:
    """The device-cookie value this response set, if it set one."""
    for header in _set_cookies(response):
        key, _, value = header.split(";", 1)[0].partition("=")
        if key.strip() == name:
            return value.strip().strip('"')
    return None


async def _devices(session: AsyncSession, user: User) -> list[TrustedDevice]:
    result = await session.execute(
        select(TrustedDevice)
        .where(TrustedDevice.user_id == user.id)
        .order_by(TrustedDevice.created_at, TrustedDevice.id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _hashes(session: AsyncSession, user: User) -> set[str]:
    return {device.token_hash for device in await _devices(session, user)}


async def _sessions(session: AsyncSession, user: User) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.is_revoked.is_(False))
    )
    return int(result.scalar_one())


async def _trust_events(session: AsyncSession, user: User) -> int:
    """How many times the audit trail says a new browser was trusted for this account."""
    result = await session.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.target_id == user.id, AuditLog.action == "auth.device.trusted")
    )
    return int(result.scalar_one())


async def _reset_token(session: AsyncSession, user: User) -> str:
    """A reset token as the emailed link would carry it (no mail transport in tests)."""
    raw, token_hash = generate_opaque_token()
    session.add(
        PasswordResetToken(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    )
    await session.flush()
    await session.commit()
    return raw


async def _make_mfa_user(session: AsyncSession, hospital_id: uuid.UUID) -> tuple[User, str]:
    secret = generate_totp_secret()
    user = await _make_user(
        session, hospital_id, mfa_enabled=True, mfa_secret=encrypt_mfa_secret(secret)
    )
    return user, secret


def _wrong_code(secret: str) -> str:
    """A six-digit code that no clock skew makes valid for this secret."""
    totp = pyotp.TOTP(secret)
    now = datetime.now(UTC)
    valid = {totp.at(now + timedelta(seconds=30 * step)) for step in range(-3, 4)}
    return next(code for code in ("000000", "111111", "222222", "333333") if code not in valid)


async def _right_code(secret: str) -> str:
    """The code that is right at this moment.

    A code is good for one 30-second step. If the step is about to end, wait
    for the next one, so that the request made with the code cannot straddle
    the boundary. (This is the authenticator's clock, not the throttle's.)
    """
    totp = pyotp.TOTP(secret)
    left = totp.interval - time.time() % totp.interval
    if left < 1.5:
        await asyncio.sleep(left + 0.05)
    return totp.now()


async def _code_step(
    client: AsyncClient,
    user: User,
    secret: str,
    password: str = PASSWORD,
    *,
    wrong: bool = False,
    cookie: str | None = None,
) -> Response:
    """The password step and then one code — the right one unless ``wrong`` — on one client."""
    headers = {"Cookie": cookie} if cookie is not None else None
    password_step = await _login(client, user.email, password, cookie=cookie)
    assert password_step.status_code == 200, password_step.text
    code = _wrong_code(secret) if wrong else await _right_code(secret)
    return await client.post(
        f"{AUTH}/mfa/verify",
        json={"mfa_ticket": password_step.json()["data"]["mfa_ticket"], "code": code},
        headers=headers,
    )


async def _mfa_trusted_browser(net: _Net, user: User, secret: str) -> AsyncClient:
    """A browser that has completed both factors and holds the device cookie for it."""
    browser = net.browser()
    response = await _code_step(browser, user, secret)
    assert response.status_code == 200, response.text
    assert browser.cookies.get(DEV_COOKIE), "the sign-in did not set a device cookie"
    return browser


async def _budget_spent(
    session: AsyncSession, target: Bucket, refill: timedelta = MFA_REFILL
) -> int:
    """Units of a budget bucket in use right now (whole refill periods, rounded up)."""
    row = (
        await session.execute(
            select(AuthThrottleBucket)
            .where(AuthThrottleBucket.key_hash == target.key_hash)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or row.drains_at is None:
        return 0
    now = await AuthThrottleRepository(session).now()
    return max(0, -((now - row.drains_at) // refill))


class _Net:
    """Hands out browsers: HTTP clients with their own cookie jar and address."""

    def __init__(self, application: FastAPI) -> None:
        self._application = application
        self._clients: list[AsyncClient] = []

    def browser(self, source: str | None = None, *, https: bool = False) -> AsyncClient:
        client = AsyncClient(
            transport=ASGITransport(app=self._application, client=_PROXY_PEER),
            base_url="https://test" if https else "http://test",
            headers={"X-Forwarded-For": source or _source()},
        )
        self._clients.append(client)
        return client

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()


class _Clock:
    """Moves the throttle's clock — and nothing else — forward."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.offset = timedelta(0)
        real_now = AuthThrottleRepository.now
        clock = self

        async def _now(repository: AuthThrottleRepository) -> datetime:
            return await real_now(repository) + clock.offset

        monkeypatch.setattr(AuthThrottleRepository, "now", _now)

    def advance(self, **delta: float) -> None:
        self.offset += timedelta(**delta)


class _Work:
    """Counts real password verifications (they run in a thread pool, hence the lock)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._lock = threading.Lock()
        self.real = 0
        real_verify = security_module.verify_password

        def _verify(password: str, hashed: str) -> bool:
            with self._lock:
                self.real += 1
            return real_verify(password, hashed)

        monkeypatch.setattr(auth_service_module, "verify_password", _verify)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def behind_one_trusted_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve addresses as in production behind one proxy we operate."""
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    return _Clock(monkeypatch)


@pytest.fixture
def work(monkeypatch: pytest.MonkeyPatch) -> _Work:
    return _Work(monkeypatch)


@pytest_asyncio.fixture
async def net(db_session: AsyncSession) -> AsyncGenerator[_Net]:
    application = create_app()

    async def _override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _override
    network = _Net(application)
    try:
        yield network
    finally:
        await network.aclose()
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def user(db_session: AsyncSession, hospital_id: uuid.UUID) -> User:
    """The owner: an active staff member of hospital A."""
    return await _make_user(db_session, hospital_id)


async def _trusted_browser(net: _Net, email: str, password: str = PASSWORD) -> AsyncClient:
    """A browser that has completed a sign-in and holds the device cookie for it."""
    browser = net.browser()
    response = await _login(browser, email, password)
    assert response.status_code == 200, response.text
    assert browser.cookies.get(DEV_COOKIE), "the sign-in did not set a device cookie"
    return browser


def _held(browser: AsyncClient) -> str:
    value = browser.cookies.get(DEV_COOKIE)
    assert value is not None
    return value


async def _exhaust_account(net: _Net, email: str, password: str = PASSWORD) -> None:
    """Use up the account's shared budget: from here only a trusted device gets in.

    :param password: The account's current (right) password, for the guard.
    """
    for _ in range(ACCOUNT_BURST):
        assert (await _login(net.browser(), email, WRONG_PASSWORD)).status_code == 401
    # The guard every "recognised" assertion rests on: a stranger with the
    # right password is refused now.
    assert (await _login(net.browser(), email, password)).status_code == 401


async def _presenting(
    net: _Net, email: str, tokens: str, password: str = PASSWORD, *, name: str = DEV_COOKIE
) -> Response:
    """Sign in from a new address with exactly this cookie value (a copied or planted cookie)."""
    return await _login(net.browser(), email, password, cookie=f"{name}={tokens}")


# ── The cookie itself ────────────────────────────────────────────────────────


class TestCookieForm:
    async def test_in_production_form_the_cookie_is_host_bound_secure_and_script_proof(
        self, net: _Net, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attacks closed by attributes: script theft, plain-HTTP leak, cross-site sends,
        and a sibling domain planting or shadowing the cookie (``__Host-``)."""
        monkeypatch.setattr(settings, "AUTH_DEVICE_COOKIE_SECURE", True)
        browser = net.browser(https=True)

        response = await _login(browser, user.email)

        assert response.status_code == 200, response.text
        headers = _set_cookies(response)
        assert len(headers) == 1, headers
        name_value, *attributes = [part.strip() for part in headers[0].split(";")]
        name, _, value = name_value.partition("=")
        assert name == PRODUCTION_COOKIE
        assert name.startswith("__Host-")
        assert _TOKEN.fullmatch(value), value
        lowered = [attribute.lower() for attribute in attributes]
        assert "secure" in lowered
        assert "httponly" in lowered
        assert "samesite=strict" in lowered
        assert "path=/" in lowered
        # __Host- requires that no Domain is given; a Domain would also widen it.
        assert not any(attribute.startswith("domain") for attribute in lowered)
        assert any(attribute.startswith("max-age=") for attribute in lowered)
        # The unprefixed development name is not used at all.
        assert _issued(response, DEV_COOKIE) is None

    async def test_the_production_cookie_is_what_recognition_reads(
        self, net: _Net, db_session: AsyncSession, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: plant the unprefixed name (settable from a sibling domain) in production."""
        monkeypatch.setattr(settings, "AUTH_DEVICE_COOKIE_SECURE", True)
        browser = net.browser(https=True)
        assert (await _login(browser, user.email)).status_code == 200
        token = browser.cookies.get(PRODUCTION_COOKIE)
        assert token is not None
        await _exhaust_account(net, user.email)

        # The genuine cookie under the production name is recognised ...
        genuine = await net.browser(https=True).post(
            f"{AUTH}/login",
            json={"email": user.email, "password": PASSWORD},
            headers={"Cookie": f"{PRODUCTION_COOKIE}={token}"},
        )
        assert genuine.status_code == 200, genuine.text
        # ... and the very same token under the unprefixed name is ignored.
        unprefixed = await _presenting(net, user.email, token, name=DEV_COOKIE)
        assert unprefixed.status_code == 401
        assert len(await _devices(db_session, user)) == 1

    async def test_only_a_hash_of_the_token_is_stored(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: read ``trusted_devices`` (a leaked backup) and replay what is in it."""
        browser = await _trusted_browser(net, user.email)
        token = _held(browser)
        (device,) = await _devices(db_session, user)

        assert _TOKEN.fullmatch(token)
        assert device.token_hash == hash_token(token)
        assert device.token_hash != token
        await _exhaust_account(net, user.email)
        # The stored value presented as a token is not a trusted device.
        assert (await _presenting(net, user.email, device.token_hash)).status_code == 401

    async def test_a_recognised_browser_keeps_its_token_and_gets_no_second_row(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        browser = await _trusted_browser(net, user.email)
        token = _held(browser)

        again = await _login(browser, user.email)

        assert again.status_code == 200
        assert _issued(again) == token
        assert len(await _devices(db_session, user)) == 1


# ── Refusals never touch the cookie ──────────────────────────────────────────


class TestCookieIsNeverSetOnARefusal:
    """If a refusal set, changed or cleared the cookie, the cookie would say
    something about the account — and a throttled attacker might be handed a
    new identity to count under."""

    async def test_no_refused_password_attempt_sets_a_cookie_or_records_a_device(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        suspended = await _make_user(db_session, hospital_id, status=UserStatus.SUSPENDED)
        attacker = net.browser()
        responses = {
            "wrong password": await _login(attacker, user.email, WRONG_PASSWORD),
            "unknown email": await _login(attacker, _email("nobody")),
            "suspended, right password": await _login(attacker, suspended.email),
        }
        for _ in range(DEVICE_FREE):
            await _login(attacker, user.email, WRONG_PASSWORD)
        responses["throttled, right password"] = await _login(attacker, user.email)

        for name, response in responses.items():
            assert response.status_code == 401, name
            assert _set_cookies(response) == [], name
        assert not attacker.cookies
        assert await _devices(db_session, user) == []
        assert await _devices(db_session, suspended) == []

    async def test_a_refusal_with_a_planted_cookie_neither_adopts_nor_clears_it(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        planted = _planted_token()

        response = await _presenting(net, user.email, planted, WRONG_PASSWORD)

        assert response.status_code == 401
        assert _set_cookies(response) == []
        assert await _devices(db_session, user) == []

    async def test_the_password_step_of_an_mfa_account_sets_no_cookie(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Attack: with only the password of an MFA account, become a trusted device of it."""
        secret = generate_totp_secret()
        victim = await _make_user(
            db_session, hospital_id, mfa_enabled=True, mfa_secret=encrypt_mfa_secret(secret)
        )
        attacker = net.browser()

        password_step = await _login(attacker, victim.email)

        assert password_step.status_code == 200
        ticket = password_step.json()["data"]["mfa_ticket"]
        assert _set_cookies(password_step) == []
        assert await _devices(db_session, victim) == []

        # A wrong code: still nothing.
        now = datetime.now(UTC)
        valid = {pyotp.TOTP(secret).at(now + timedelta(seconds=30 * s)) for s in range(-3, 4)}
        wrong = next(code for code in ("000000", "111111", "222222") if code not in valid)
        refused = await attacker.post(
            f"{AUTH}/mfa/verify", json={"mfa_ticket": ticket, "code": wrong}
        )
        assert refused.status_code == 401
        assert _set_cookies(refused) == []
        assert not attacker.cookies
        assert await _devices(db_session, victim) == []

        # Only both factors make the browser a trusted device.
        completed = await attacker.post(
            f"{AUTH}/mfa/verify",
            json={"mfa_ticket": ticket, "code": await _right_code(secret)},
        )
        assert completed.status_code == 200, completed.text
        assert _issued(completed)
        assert len(await _devices(db_session, victim)) == 1

    async def test_forgot_password_sets_no_cookie_for_anyone(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: ask for a reset of somebody's account and get recognised for it."""
        attacker = net.browser()

        known = await attacker.post(f"{AUTH}/password/forgot", json={"email": user.email})
        unknown = await attacker.post(f"{AUTH}/password/forgot", json={"email": _email("nobody")})

        for response in (known, unknown):
            assert response.status_code == 200
            assert _set_cookies(response) == []
        assert not attacker.cookies
        assert await _devices(db_session, user) == []

    async def test_a_refused_reset_or_refresh_sets_no_cookie(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        attacker = net.browser()

        bad_reset = await attacker.post(
            f"{AUTH}/password/reset",
            json={"token": _planted_token(), "new_password": NEW_PASSWORD},
        )
        bad_refresh = await attacker.post(
            f"{AUTH}/refresh", json={"refresh_token": _planted_token()}
        )

        for response in (bad_reset, bad_refresh):
            assert response.status_code == 401
            assert _set_cookies(response) == []
        assert await _devices(db_session, user) == []


# ── Fixation ─────────────────────────────────────────────────────────────────


class TestPlantedTokens:
    async def test_a_planted_token_is_not_adopted_when_the_victim_signs_in(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack (fixation): put a token you know into the victim's browser, wait for
        them to sign in, then present the same token as "their" trusted device."""
        planted = _planted_token()
        victim_browser = net.browser()

        signed_in = await _login(victim_browser, user.email, cookie=f"{DEV_COOKIE}={planted}")

        assert signed_in.status_code == 200, signed_in.text
        issued = _issued(signed_in)
        assert issued is not None
        # The server minted its own token and dropped the planted one.
        assert _TOKEN.fullmatch(issued)
        assert planted not in issued
        assert await _hashes(db_session, user) == {hash_token(issued)}

        await _exhaust_account(net, user.email)
        assert (await _presenting(net, user.email, planted)).status_code == 401
        assert (await _presenting(net, user.email, issued)).status_code == 200

    async def test_planting_the_attackers_own_genuine_token_gains_nothing_either(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        """Attack: the planted token is a real one — the attacker's, for his own account."""
        attacker = await _make_user(db_session, hospital_id)
        attacker_token = _held(await _trusted_browser(net, attacker.email))
        victim_browser = net.browser()

        signed_in = await _login(
            victim_browser, user.email, cookie=f"{DEV_COOKIE}={attacker_token}"
        )

        assert signed_in.status_code == 200
        issued = _issued(signed_in)
        assert issued is not None
        victim_token = issued.split(".")[-1]
        assert victim_token != attacker_token
        # The attacker's token still belongs to the attacker alone.
        assert await _hashes(db_session, user) == {hash_token(victim_token)}
        assert await _hashes(db_session, attacker) == {hash_token(attacker_token)}

        await _exhaust_account(net, user.email)
        assert (await _presenting(net, user.email, attacker_token)).status_code == 401
        assert (await _presenting(net, user.email, victim_token)).status_code == 200

    async def test_garbage_in_the_cookie_is_ignored_not_fatal(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: break sign-in for a victim by planting a cookie the server chokes on."""
        for garbage in (
            "x",
            "",
            "." * 50,
            "a" * 5000,
            "'; DROP TABLE trusted_devices; --",
            ".".join(_planted_token() for _ in range(40)),
            f'"{_planted_token()}"',
            "%00%0d%0a",
        ):
            response = await _presenting(net, user.email, garbage)
            assert response.status_code == 200, (garbage[:30], response.text)
            issued = _issued(response)
            assert issued is not None
            assert _TOKEN.fullmatch(issued), garbage[:30]

        # Each of those sign-ins was an unrecognised browser: one row each.
        assert len(await _devices(db_session, user)) == 8


# ── Recognition is per account ───────────────────────────────────────────────


class TestRecognitionIsPerAccount:
    async def test_a_token_trusted_for_one_user_is_nothing_for_another(
        self,
        net: _Net,
        db_session: AsyncSession,
        user: User,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Attack: use your own trusted-device cookie against a colleague's account,
        in your hospital and in another one."""
        colleague = await _make_user(db_session, hospital_id)
        stranger = await _make_user(db_session, other_hospital_id)
        own_token = _held(await _trusted_browser(net, user.email))

        for victim in (colleague, stranger):
            await _exhaust_account(net, victim.email)
            response = await _presenting(net, victim.email, own_token)
            assert response.status_code == 401
            assert _set_cookies(response) == []
            assert await _devices(db_session, victim) == []
        # For its own account the same cookie is good, budget exhausted or not.
        await _exhaust_account(net, user.email)
        assert (await _presenting(net, user.email, own_token)).status_code == 200

    async def test_a_shared_browser_recognises_both_users_with_separate_tokens(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        """A ward workstation: two people, one browser. Neither's token serves the other,
        and the table cannot be used to link them."""
        colleague = await _make_user(db_session, hospital_id)
        workstation = net.browser()
        assert (await _login(workstation, user.email)).status_code == 200
        assert (await _login(workstation, colleague.email)).status_code == 200

        tokens = _held(workstation).split(".")
        assert len(tokens) == len(set(tokens)) == 2
        user_hashes = await _hashes(db_session, user)
        colleague_hashes = await _hashes(db_session, colleague)
        assert len(user_hashes) == len(colleague_hashes) == 1
        assert user_hashes.isdisjoint(colleague_hashes)
        assert user_hashes | colleague_hashes == {hash_token(token) for token in tokens}

        # Both are recognised on the workstation, with the budgets exhausted.
        await _exhaust_account(net, user.email)
        await _exhaust_account(net, colleague.email)
        assert (await _login(workstation, user.email)).status_code == 200
        assert (await _login(workstation, colleague.email)).status_code == 200
        # Recognition reused each one's row; nothing new was minted.
        assert await _hashes(db_session, user) == user_hashes
        assert await _hashes(db_session, colleague) == colleague_hashes
        assert sorted(_held(workstation).split(".")) == sorted(tokens)

        # Each token alone is good for its own account only.
        by_hash = {hash_token(token): token for token in tokens}
        user_token = by_hash[next(iter(user_hashes))]
        colleague_token = by_hash[next(iter(colleague_hashes))]
        assert (await _presenting(net, user.email, colleague_token)).status_code == 401
        assert (await _presenting(net, colleague.email, user_token)).status_code == 401
        assert (await _presenting(net, user.email, user_token)).status_code == 200

    async def test_no_two_accounts_ever_share_a_token_hash(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Five people through one browser, twice: every row has its own token."""
        people = [await _make_user(db_session, hospital_id) for _ in range(5)]
        workstation = net.browser()
        for _ in range(2):
            for person in people:
                assert (await _login(workstation, person.email)).status_code == 200

        rows = (
            await db_session.execute(
                select(TrustedDevice.user_id, TrustedDevice.token_hash).where(
                    TrustedDevice.user_id.in_([person.id for person in people])
                )
            )
        ).all()
        assert len(rows) == 5
        assert len({token_hash for _, token_hash in rows}) == 5
        assert {user_id for user_id, _ in rows} == {person.id for person in people}

    async def test_the_cookie_never_carries_more_than_eight_tokens(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Attack: grow the cookie without bound through a shared browser."""
        people = [await _make_user(db_session, hospital_id) for _ in range(TOKENS_PER_COOKIE + 2)]
        workstation = net.browser()
        for person in people:
            assert (await _login(workstation, person.email)).status_code == 200

        tokens = _held(workstation).split(".")
        assert len(tokens) == TOKENS_PER_COOKIE
        # The most recent eight are the ones kept.
        latest = people[-TOKENS_PER_COOKIE:]
        expected = set()
        for person in latest:
            expected |= await _hashes(db_session, person)
        assert {hash_token(token) for token in tokens} == expected

    async def test_a_full_cookie_forgets_the_least_recently_used_account_not_the_busiest(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Attack — or an ordinary ward: push somebody's token out of a shared workstation's
        cookie by having eight other people sign in after them. Whoever keeps using the
        workstation must stay in the cookie; the one who has not been back drops out."""
        people = [await _make_user(db_session, hospital_id) for _ in range(TOKENS_PER_COOKIE + 1)]
        workstation = net.browser()
        for person in people[:TOKENS_PER_COOKIE]:
            assert (await _login(workstation, person.email)).status_code == 200
        first, second = people[0], people[1]
        (first_hash,) = await _hashes(db_session, first)
        (second_hash,) = await _hashes(db_session, second)
        assert hash_token(_held(workstation).split(".")[0]) == first_hash

        # The first person comes back: their token moves to the end ...
        assert (await _login(workstation, first.email)).status_code == 200
        carried = [hash_token(token) for token in _held(workstation).split(".")]
        assert len(carried) == TOKENS_PER_COOKIE
        assert carried[-1] == first_hash
        assert carried[0] == second_hash
        # ... so a ninth person's sign-in pushes out the second, not the first.
        assert (await _login(workstation, people[-1].email)).status_code == 200
        carried = [hash_token(token) for token in _held(workstation).split(".")]
        assert len(carried) == TOKENS_PER_COOKIE
        assert first_hash in carried
        assert second_hash not in carried

        await _exhaust_account(net, first.email)
        await _exhaust_account(net, second.email)
        assert (await _login(workstation, first.email)).status_code == 200
        assert (await _login(workstation, second.email)).status_code == 401


class TestASecondCookieCannotHideTheRealOne:
    @pytest.mark.parametrize("planted_first", [True, False], ids=["planted-first", "planted-last"])
    async def test_the_real_token_is_found_beside_a_planted_cookie_of_the_same_name(
        self, net: _Net, db_session: AsyncSession, user: User, planted_first: bool
    ) -> None:
        """Attack: add a second cookie of the same name so the server reads yours, not theirs,
        and the owner's browser stops being a trusted device."""
        real = _held(await _trusted_browser(net, user.email))
        # A full cookie's worth of well-formed tokens the server never issued.
        planted = ".".join(_planted_token() for _ in range(TOKENS_PER_COOKIE))
        pairs = [f"{DEV_COOKIE}={planted}", f"{DEV_COOKIE}={real}"]
        if not planted_first:
            pairs.reverse()
        await _exhaust_account(net, user.email)

        one_header = await _login(net.browser(), user.email, cookie="; ".join(pairs))

        assert one_header.status_code == 200, one_header.text
        # Recognised as the existing device: no new row, and the planted tokens are dropped.
        assert await _hashes(db_session, user) == {hash_token(real)}
        assert _issued(one_header) == real

    async def test_the_real_token_is_found_in_a_second_cookie_header(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        real = _held(await _trusted_browser(net, user.email))
        await _exhaust_account(net, user.email)

        response = await net.browser().post(
            f"{AUTH}/login",
            json={"email": user.email, "password": PASSWORD},
            headers=[
                ("Cookie", f"{DEV_COOKIE}={_planted_token()}"),
                ("Cookie", f"other=1; {DEV_COOKIE}={real}"),
            ],
        )

        assert response.status_code == 200, response.text
        assert await _hashes(db_session, user) == {hash_token(real)}

    @pytest.mark.parametrize("planted", [16, 63], ids=["sixteen", "sixty-three"])
    @pytest.mark.parametrize("layout", ["one-cookie", "a-cookie-each"])
    async def test_sixteen_or_sixty_three_planted_tokens_cannot_hide_the_real_one(
        self, net: _Net, db_session: AsyncSession, user: User, planted: int, layout: str
    ) -> None:
        """Attack: pad the request with well-formed tokens the server never issued, ahead
        of the real one, so that the reader stops before it reaches the owner's — and the
        owner's browser is thrown back on the shared budget an attacker has exhausted.

        Sixteen is two cookies' worth; sixty-three is every place but one of
        what the server reads from a request.
        """
        real = _held(await _trusted_browser(net, user.email))
        junk = [_planted_token() for _ in range(planted)]
        if layout == "one-cookie":
            header = f"{DEV_COOKIE}={'.'.join([*junk, real])}"
        else:
            header = "; ".join(f"{DEV_COOKIE}={token}" for token in [*junk, real])
        await _exhaust_account(net, user.email)

        response = await _login(net.browser(), user.email, cookie=header)

        assert response.status_code == 200, response.text
        # Recognised as the existing device: no new row, and the padding is dropped.
        assert await _hashes(db_session, user) == {hash_token(real)}
        assert _issued(response) == real

    async def test_planted_tokens_cannot_hide_the_real_one_at_the_code_step(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The same padding, where it would cost the owner most: an attacker with the
        password has used up the shared code budget, and only the owner's own
        MFA-verified browser still gets a code looked at."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        real = _held(await _mfa_trusted_browser(net, victim, secret))
        for _ in range(MFA_ACCOUNT_BURST):
            assert (await _code_step(net.browser(), victim, secret, wrong=True)).status_code == 401
        refused = await _code_step(net.browser(), victim, secret)
        assert refused.status_code == 401  # the guard: a stranger's right code is not looked at
        header = f"{DEV_COOKIE}={'.'.join([*(_planted_token() for _ in range(63)), real])}"

        response = await _code_step(net.browser(), victim, secret, cookie=header)

        assert response.status_code == 200, response.text
        assert _issued(response) == real
        assert await _hashes(db_session, victim) == {hash_token(real)}

    async def test_a_flood_of_tokens_costs_the_server_a_bounded_lookup(
        self, net: _Net, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack on availability: send hundreds of tokens so every sign-in hashes and
        looks up hundreds of rows."""
        looked_up: list[int] = []
        real_find = TrustedDeviceRepository.find

        async def _find(
            repository: TrustedDeviceRepository,
            user_id: uuid.UUID,
            token_hashes: Sequence[str],
            *,
            now: datetime,
        ) -> TrustedDevice | None:
            looked_up.append(len(token_hashes))
            return await real_find(repository, user_id, token_hashes, now=now)

        monkeypatch.setattr(TrustedDeviceRepository, "find", _find)
        flood = "; ".join(
            f"{DEV_COOKIE}={'.'.join(_planted_token() for _ in range(50))}" for _ in range(10)
        )

        response = await _login(net.browser(), user.email, cookie=flood)

        assert response.status_code == 200, response.text
        assert looked_up
        assert max(looked_up) <= TOKENS_READ_PER_REQUEST
        issued = _issued(response)
        assert issued is not None
        assert _TOKEN.fullmatch(issued)

    async def test_a_lookalike_cookie_name_is_not_read(self, net: _Net, user: User) -> None:
        """Attack: present the token under a name that merely contains the real one."""
        real = _held(await _trusted_browser(net, user.email))
        await _exhaust_account(net, user.email)

        for name in (f"x{DEV_COOKIE}", f"{DEV_COOKIE}x", DEV_COOKIE.upper(), PRODUCTION_COOKIE):
            response = await _presenting(net, user.email, real, name=name)
            assert response.status_code == 401, name


# ── A stolen token ───────────────────────────────────────────────────────────


class TestAStolenTokenSharesOneBucket:
    """A copied cookie must not multiply the attempts: the bucket is the row, not the bearer."""

    async def test_the_copy_and_the_original_draw_on_the_same_allowance(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work
    ) -> None:
        owner = await _trusted_browser(net, user.email)
        stolen = _held(owner)
        start = work.real

        for _ in range(3):
            assert (await _presenting(net, user.email, stolen, WRONG_PASSWORD)).status_code == 401
        for _ in range(4):
            assert (await _login(owner, user.email, WRONG_PASSWORD)).status_code == 401

        # Five evaluated between them, not five each.
        assert work.real == start + DEVICE_FREE
        (device,) = await _devices(db_session, user)
        row = (
            await db_session.execute(
                select(AuthThrottleBucket)
                .where(
                    AuthThrottleBucket.key_hash == bucket(BucketKind.PW_DEVICE, device.id).key_hash
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.failures == DEVICE_FREE
        assert row.blocked_until is not None

    async def test_re_presenting_the_copy_from_new_browsers_and_addresses_buys_nothing(
        self, net: _Net, user: User, work: _Work
    ) -> None:
        """Attack: a fresh browser and address for every guess, same stolen cookie."""
        stolen = _held(await _trusted_browser(net, user.email))
        start = work.real

        for _ in range(15):
            assert (await _presenting(net, user.email, stolen, WRONG_PASSWORD)).status_code == 401

        assert work.real == start + DEVICE_FREE
        # The right password is refused unevaluated while the device is blocked.
        assert (await _presenting(net, user.email, stolen)).status_code == 401
        assert work.real == start + DEVICE_FREE

    async def test_signing_in_as_someone_else_on_the_copy_buys_nothing(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """Attack: the thief signs in to his own account with the stolen cookie in the jar,
        hoping the server re-issues, re-keys or resets the victim's device."""
        thief = await _make_user(db_session, hospital_id)
        stolen = _held(await _trusted_browser(net, user.email))
        (device_before,) = await _devices(db_session, user)
        for _ in range(DEVICE_FREE):
            await _presenting(net, user.email, stolen, WRONG_PASSWORD)
        start = work.real

        own_sign_in = await _presenting(net, thief.email, stolen)
        assert own_sign_in.status_code == 200
        combined = _issued(own_sign_in)
        assert combined is not None
        assert stolen in combined.split(".")  # still a live token, so it is carried along
        own_work = work.real - start
        assert own_work == 1

        # The victim's device is the same row with the same bucket, still blocked.
        (device_after,) = await _devices(db_session, user)
        assert (device_after.id, device_after.token_hash) == (
            device_before.id,
            device_before.token_hash,
        )
        for cookie in (combined, stolen, ".".join(reversed(combined.split(".")))):
            assert (await _presenting(net, user.email, cookie)).status_code == 401
            assert (await _presenting(net, user.email, cookie, WRONG_PASSWORD)).status_code == 401
        assert work.real == start + own_work

    async def test_refreshing_a_session_on_the_copy_buys_nothing(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """Attack: call /refresh (with the thief's own session) carrying the stolen cookie."""
        thief = await _make_user(db_session, hospital_id)
        thief_browser = net.browser()
        thief_session = await _login(thief_browser, thief.email)
        refresh_token = thief_session.json()["data"]["refresh_token"]
        stolen = _held(await _trusted_browser(net, user.email))
        (device_before,) = await _devices(db_session, user)
        for _ in range(DEVICE_FREE):
            await _presenting(net, user.email, stolen, WRONG_PASSWORD)
        start = work.real

        refreshed = await net.browser().post(
            f"{AUTH}/refresh",
            json={"refresh_token": refresh_token},
            headers={"Cookie": f"{DEV_COOKIE}={stolen}"},
        )
        assert refreshed.status_code == 200, refreshed.text
        # A refresh hands out no device cookie at all, to anybody.
        assert _set_cookies(refreshed) == []
        thief_devices = await _hashes(db_session, thief)
        assert len(thief_devices) == 1  # the one from his own sign-in, and no more

        (device_after,) = await _devices(db_session, user)
        assert (device_after.id, device_after.token_hash) == (
            device_before.id,
            device_before.token_hash,
        )
        assert (await _presenting(net, user.email, stolen)).status_code == 401
        assert (await _presenting(net, user.email, stolen, WRONG_PASSWORD)).status_code == 401
        assert work.real == start

    async def test_the_owner_ends_the_copys_trust_by_changing_the_password(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """What the owner can do about a copied cookie: this browser gets a new token."""
        owner = await _trusted_browser(net, user.email)
        stolen = _held(owner)

        changed = await owner.post(
            f"{AUTH}/password/change",
            headers=_bearer(user),
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        )

        assert changed.status_code == 200, changed.text
        replacement = _held(owner)
        assert replacement != stolen
        assert stolen not in replacement.split(".")
        assert await _hashes(db_session, user) == {hash_token(replacement)}
        await _exhaust_account(net, user.email, NEW_PASSWORD)
        assert (await _presenting(net, user.email, stolen, NEW_PASSWORD)).status_code == 401
        assert (await _login(owner, user.email, NEW_PASSWORD)).status_code == 200


# ── Limits and expiry ────────────────────────────────────────────────────────


class TestTheDeviceCap:
    async def test_the_eleventh_device_evicts_the_least_recently_used(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: pile up trusted devices without bound — and, the other way, push the
        owner's everyday browser out by adding new ones."""
        browsers = [await _trusted_browser(net, user.email) for _ in range(DEVICES_PER_ACCOUNT)]
        tokens = [_held(browser) for browser in browsers]
        assert len(await _devices(db_session, user)) == DEVICES_PER_ACCOUNT

        # The oldest browser is used again, so it is no longer the least recent.
        assert (await _login(browsers[0], user.email)).status_code == 200
        newest = _held(await _trusted_browser(net, user.email))

        hashes = await _hashes(db_session, user)
        assert len(hashes) == DEVICES_PER_ACCOUNT
        assert hash_token(tokens[1]) not in hashes  # least recently used: evicted
        assert hash_token(tokens[0]) in hashes  # oldest but recently used: kept
        assert hash_token(newest) in hashes
        assert {hash_token(token) for token in tokens[2:]} <= hashes

        await _exhaust_account(net, user.email)
        assert (await _presenting(net, user.email, tokens[1])).status_code == 401
        assert (await _presenting(net, user.email, tokens[0])).status_code == 200
        assert (await _presenting(net, user.email, newest)).status_code == 200

    async def test_ten_cookie_less_sign_ins_do_not_evict_a_browser_that_has_come_back(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack — or just a script: something that has the password signs in again and
        again and never keeps its cookie. Each sign-in is "a new device"; ten of them
        fill the account's allowance and push the owner's everyday browser out, and
        with it the owner's protection against an exhausted shared budget.

        A browser that has come back and signed in again outranks any number
        of browsers seen once.
        """
        owner = await _trusted_browser(net, user.email)
        assert (await _login(owner, user.email)).status_code == 200  # it comes back
        token = _held(owner)
        (owners_row,) = await _devices(db_session, user)
        assert owners_row.last_used_at > owners_row.created_at

        first_wave = []
        for _ in range(DEVICES_PER_ACCOUNT):
            response = await _login(net.browser(), user.email)
            assert response.status_code == 200, response.text
            first_wave.append(_issued(response))

        hashes = await _hashes(db_session, user)
        assert len(hashes) == DEVICES_PER_ACCOUNT
        assert hash_token(token) in hashes
        # ... and it goes on: the script only ever displaces its own earlier rows.
        second_wave = []
        for _ in range(DEVICES_PER_ACCOUNT + 5):
            response = await _login(net.browser(), user.email)
            assert response.status_code == 200, response.text
            second_wave.append(_issued(response))

        hashes = await _hashes(db_session, user)
        assert len(hashes) == DEVICES_PER_ACCOUNT
        assert hash_token(token) in hashes
        assert all(issued is not None for issued in [*first_wave, *second_wave])
        assert hashes.isdisjoint({hash_token(issued) for issued in first_wave if issued})
        assert hashes - {hash_token(token)} == {
            hash_token(issued) for issued in second_wave[-(DEVICES_PER_ACCOUNT - 1) :] if issued
        }
        (owners_row_now,) = [d for d in await _devices(db_session, user) if d.id == owners_row.id]
        assert owners_row_now.token_hash == hash_token(token)

        await _exhaust_account(net, user.email)
        assert (await _login(owner, user.email)).status_code == 200
        assert (await _presenting(net, user.email, token)).status_code == 200
        # The script's discarded cookies are worth nothing.
        assert first_wave[0] is not None
        assert (await _presenting(net, user.email, first_wave[0])).status_code == 401

    async def test_a_returning_browsers_mfa_standing_survives_the_same_flood(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The same script against an MFA account, by somebody who can pass both factors
        once per run (a compromised automation, a phished code relay). Evicting the
        owner's browser here would also take away its own code budget."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        owner = await _mfa_trusted_browser(net, victim, secret)
        assert (await _code_step(owner, victim, secret)).status_code == 200
        token = _held(owner)

        for _ in range(DEVICES_PER_ACCOUNT):
            response = await _code_step(net.browser(), victim, secret)
            assert response.status_code == 200, response.text

        rows = await _devices(db_session, victim)
        assert len(rows) == DEVICES_PER_ACCOUNT
        (owners_row,) = [row for row in rows if row.token_hash == hash_token(token)]
        assert owners_row.mfa_verified_at is not None

    async def test_a_new_browser_is_trusted_even_when_ten_established_ones_exist(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """The other edge of "established browsers outrank new ones": a nurse who signs in
        on ten ward workstations every week sits down at an eleventh. If the newcomer
        were the row dropped, she could never add a device again — and under an attack
        on the shared budget the new workstation would stay locked out for good.

        Room is made first: the least recently used established browser goes.
        """
        browsers = [await _trusted_browser(net, user.email) for _ in range(DEVICES_PER_ACCOUNT)]
        for browser in browsers:
            assert (await _login(browser, user.email)).status_code == 200  # each comes back
        tokens = [_held(browser) for browser in browsers]
        rows = await _devices(db_session, user)
        assert len(rows) == DEVICES_PER_ACCOUNT
        assert all(row.last_used_at > row.created_at for row in rows)

        eleventh = await _trusted_browser(net, user.email)

        hashes = await _hashes(db_session, user)
        assert len(hashes) == DEVICES_PER_ACCOUNT
        assert hash_token(_held(eleventh)) in hashes
        assert hash_token(tokens[0]) not in hashes  # least recently used: gone
        assert {hash_token(token) for token in tokens[1:]} <= hashes
        await _exhaust_account(net, user.email)
        assert (await _login(eleventh, user.email)).status_code == 200
        assert (await _presenting(net, user.email, tokens[0])).status_code == 401
        assert (await _login(browsers[-1], user.email)).status_code == 200

    async def test_every_newly_trusted_browser_is_on_the_audit_trail_and_only_those(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """A device an attacker manages to add is a foothold that outlives the session it
        came from; the owner's hospital must be able to see that one was added."""
        browser = await _trusted_browser(net, user.email)
        assert await _trust_events(db_session, user) == 1

        # Coming back, refused attempts and a refresh add no device, so no entry.
        signed_in = await _login(browser, user.email)
        assert signed_in.status_code == 200
        assert (await _login(browser, user.email, WRONG_PASSWORD)).status_code == 401
        assert (await _login(net.browser(), user.email, WRONG_PASSWORD)).status_code == 401
        refreshed = await net.browser().post(
            f"{AUTH}/refresh", json={"refresh_token": signed_in.json()["data"]["refresh_token"]}
        )
        assert refreshed.status_code == 200
        assert await _trust_events(db_session, user) == 1

        # A second browser, and a token replaced through an emailed link: one each.
        await _trusted_browser(net, user.email)
        assert await _trust_events(db_session, user) == 2
        reset = await browser.post(
            f"{AUTH}/password/reset",
            json={"token": await _reset_token(db_session, user), "new_password": NEW_PASSWORD},
        )
        assert reset.status_code == 200, reset.text
        assert await _trust_events(db_session, user) == 3
        entry = (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.target_id == user.id, AuditLog.action == "auth.device.trusted")
                .limit(1)
            )
        ).scalar_one()
        assert entry.hospital_id == user.hospital_id
        assert entry.actor_user_id == user.id
        assert entry.target_type == "user"
        # The trail names the account, never the token or its hash.
        recorded = f"{entry.before} {entry.after} {entry.context}"
        for token in _held(browser).split("."):
            assert token not in recorded
            assert hash_token(token) not in recorded

    async def test_one_accounts_devices_never_evict_anothers(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        colleague = await _make_user(db_session, hospital_id)
        colleague_hashes_before = await _hashes(db_session, colleague)
        colleague_token = _held(await _trusted_browser(net, colleague.email))

        for _ in range(DEVICES_PER_ACCOUNT + 3):
            await _trusted_browser(net, user.email)

        assert colleague_hashes_before == set()
        assert await _hashes(db_session, colleague) == {hash_token(colleague_token)}
        assert len(await _devices(db_session, user)) == DEVICES_PER_ACCOUNT


class TestExpiry:
    async def test_an_expired_device_is_not_recognised(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: a cookie lifted from a decommissioned machine, months later."""
        old = _held(await _trusted_browser(net, user.email))
        (device,) = await _devices(db_session, user)
        device.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.flush()
        await db_session.commit()
        await _exhaust_account(net, user.email)

        response = await _presenting(net, user.email, old)

        assert response.status_code == 401
        assert _set_cookies(response) == []

    async def test_a_sign_in_after_expiry_replaces_the_dead_token(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        browser = await _trusted_browser(net, user.email)
        old = _held(browser)
        (device,) = await _devices(db_session, user)
        device.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.flush()
        await db_session.commit()

        assert (await _login(browser, user.email)).status_code == 200

        replacement = _held(browser)
        assert old not in replacement.split(".")
        # The expired row is gone and the new one is the only one.
        assert await _hashes(db_session, user) == {hash_token(replacement)}

    async def test_use_cannot_extend_a_device_past_its_absolute_lifetime(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: keep a stolen cookie alive for ever by using it."""
        browser = await _trusted_browser(net, user.email)
        (device,) = await _devices(db_session, user)
        created = datetime.now(UTC) - timedelta(days=179)
        device.created_at = created
        await db_session.flush()
        await db_session.commit()

        assert (await _login(browser, user.email)).status_code == 200

        (device,) = await _devices(db_session, user)
        assert device.expires_at <= created + timedelta(days=180)
        assert device.expires_at > datetime.now(UTC)


# ── Which devices have passed the second factor ──────────────────────────────


class TestOnlyTheSecondFactorMakesADeviceMfaVerified:
    """An MFA-verified device has a code budget of its own, out of reach of
    whoever has only the password. If anything short of a completed second
    factor could make a browser one, that budget is one more for an attacker."""

    async def test_no_path_but_a_completed_code_step_marks_a_device(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Attack: become "MFA-verified" through an emailed link, a session refresh or a
        password change — each of which makes a browser a trusted device."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        browser = net.browser()

        # An emailed reset: trusted, not MFA-verified.
        reset = await browser.post(
            f"{AUTH}/password/reset",
            json={"token": await _reset_token(db_session, victim), "new_password": NEW_PASSWORD},
        )
        assert reset.status_code == 200, reset.text
        (after_reset,) = await _devices(db_session, victim)
        assert after_reset.mfa_verified_at is None

        # The password step alone changes nothing; a wrong code changes nothing.
        refused = await _code_step(browser, victim, secret, NEW_PASSWORD, wrong=True)
        assert refused.status_code == 401
        (still,) = await _devices(db_session, victim)
        assert (still.id, still.mfa_verified_at) == (after_reset.id, None)

        # Both factors: the same row, now MFA-verified.
        completed = await _code_step(browser, victim, secret, NEW_PASSWORD)
        assert completed.status_code == 200, completed.text
        (verified,) = await _devices(db_session, victim)
        assert verified.id == after_reset.id
        assert verified.mfa_verified_at is not None

        # A refresh of that session — from the browser itself, or from one
        # without the cookie — trusts nothing and marks nothing.
        refresh_token = completed.json()["data"]["refresh_token"]
        for client in (net.browser(), browser):
            refreshed = await client.post(f"{AUTH}/refresh", json={"refresh_token": refresh_token})
            assert refreshed.status_code == 200, refreshed.text
            assert _set_cookies(refreshed) == []
            refresh_token = refreshed.json()["data"]["refresh_token"]
        (unchanged,) = await _devices(db_session, victim)
        assert (unchanged.id, unchanged.mfa_verified_at) == (verified.id, verified.mfa_verified_at)

        # A password change in the verified browser replaces its token; the
        # replacement has proved a password, not a code.
        changed = await browser.post(
            f"{AUTH}/password/change",
            headers=_bearer(victim),
            json={"current_password": NEW_PASSWORD, "new_password": PASSWORD},
        )
        assert changed.status_code == 200, changed.text
        (replacement,) = await _devices(db_session, victim)
        assert replacement.token_hash != verified.token_hash
        assert replacement.mfa_verified_at is None

    async def test_a_password_only_sign_in_never_marks_a_device(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        browser = await _trusted_browser(net, user.email)
        assert (await _login(browser, user.email)).status_code == 200

        (device,) = await _devices(db_session, user)
        assert device.mfa_verified_at is None

    async def test_one_users_code_step_marks_only_that_users_device_on_a_shared_browser(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        """Attack: sign in to your own (non-MFA) account on a workstation where a colleague
        has passed their second factor — or the reverse — and inherit the standing."""
        colleague, secret = await _make_mfa_user(db_session, hospital_id)
        workstation = net.browser()
        assert (await _login(workstation, user.email)).status_code == 200
        completed = await _code_step(workstation, colleague, secret)
        assert completed.status_code == 200, completed.text
        assert (await _login(workstation, user.email)).status_code == 200

        (own,) = await _devices(db_session, user)
        (theirs,) = await _devices(db_session, colleague)
        assert own.mfa_verified_at is None
        assert theirs.mfa_verified_at is not None

    async def test_turning_mfa_off_forgets_which_devices_passed_it(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """What a device proved about the old second factor says nothing about the next."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        first = await _mfa_trusted_browser(net, victim, secret)
        await _mfa_trusted_browser(net, victim, secret)
        assert [d.mfa_verified_at is not None for d in await _devices(db_session, victim)] == [
            True,
            True,
        ]

        disabled = await first.post(
            f"{AUTH}/mfa/disable",
            headers=_bearer(victim),
            json={"password": PASSWORD, "code": await _right_code(secret)},
        )

        assert disabled.status_code == 200, disabled.text
        rows = await _devices(db_session, victim)
        assert len(rows) == 2  # still trusted devices for the password step
        assert [row.mfa_verified_at for row in rows] == [None, None]

    async def test_a_refused_disable_forgets_nothing(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Attack: a stolen session strips the owner's devices of their own code budget
        without the code, so that the shared one (which the thief can exhaust) is all
        the owner has left."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        browser = await _mfa_trusted_browser(net, victim, secret)
        (before,) = await _devices(db_session, victim)

        for payload in (
            {"password": PASSWORD, "code": _wrong_code(secret)},
            {"password": WRONG_PASSWORD, "code": await _right_code(secret)},
        ):
            refused = await browser.post(
                f"{AUTH}/mfa/disable", headers=_bearer(victim), json=payload
            )
            assert refused.status_code == 401

        (after,) = await _devices(db_session, victim)
        assert after.mfa_verified_at == before.mfa_verified_at is not None

    async def test_a_copied_cookie_has_no_code_budget_of_its_own_against_a_new_second_factor(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: a device row that says "passed the second factor" exists before the
        account's (new) second factor does — a copied cookie from before the owner
        re-enrolled, a restored backup. Enrolment must not let it stand: its guesses at
        the new code belong on the shared budget."""
        owner = await _trusted_browser(net, user.email)
        stolen = _held(owner)
        (device,) = await _devices(db_session, user)
        device.mfa_verified_at = datetime.now(UTC) - timedelta(days=1)
        await db_session.flush()
        await db_session.commit()

        enrolled = await owner.post(
            f"{AUTH}/mfa/enroll", headers=_bearer(user), json={"password": PASSWORD}
        )
        assert enrolled.status_code == 200, enrolled.text
        secret = enrolled.json()["data"]["secret"]
        # Enrolment alone (unconfirmed) decides nothing yet.
        confirmed = await owner.post(
            f"{AUTH}/mfa/confirm", headers=_bearer(user), json={"code": await _right_code(secret)}
        )
        assert confirmed.status_code == 200, confirmed.text

        (device,) = await _devices(db_session, user)
        assert device.mfa_verified_at is None
        # The copy's guesses are charged to the account, like anybody's ...
        shared = bucket(BucketKind.MFA_ACCOUNT, user.id)
        own = bucket(BucketKind.MFA_DEVICE_CAP, device.id)
        for _ in range(3):
            refused = await _code_step(
                net.browser(), user, secret, wrong=True, cookie=f"{DEV_COOKIE}={stolen}"
            )
            assert refused.status_code == 401
        assert await _budget_spent(db_session, shared) == 3
        assert await _budget_spent(db_session, own) == 0
        # ... so once that budget is gone the copy gets nothing, right code or not.
        for _ in range(MFA_ACCOUNT_BURST - 3):
            assert (await _code_step(net.browser(), user, secret, wrong=True)).status_code == 401
        assert await _budget_spent(db_session, shared) == MFA_ACCOUNT_BURST
        with_copy = await _code_step(net.browser(), user, secret, cookie=f"{DEV_COOKIE}={stolen}")
        assert with_copy.status_code == 401
        assert await _budget_spent(db_session, own) == 0
        (device,) = await _devices(db_session, user)
        assert device.mfa_verified_at is None


# ── Forgetting devices ───────────────────────────────────────────────────────


class TestForgettingDevices:
    async def test_logout_all_forgets_every_device(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """ "Sign out everywhere" after a suspected compromise must leave no browser trusted."""
        first = await _trusted_browser(net, user.email)
        second = await _trusted_browser(net, user.email)
        tokens = [_held(first), _held(second)]

        response = await first.post(f"{AUTH}/logout-all", headers=_bearer(user))

        assert response.status_code == 200, response.text
        assert _set_cookies(response) == []
        assert await _devices(db_session, user) == []
        assert await _sessions(db_session, user) == 0
        await _exhaust_account(net, user.email)
        for token in tokens:
            assert (await _presenting(net, user.email, token)).status_code == 401

    async def test_an_admin_reset_forgets_every_device(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        admin = await _make_admin(db_session, hospital_id)
        await _trusted_browser(net, user.email)
        await _trusted_browser(net, user.email)

        response = await net.browser().post(
            f"{USERS}/{user.id}/reset-password", headers=_bearer(admin)
        )

        assert response.status_code == 200, response.text
        assert await _devices(db_session, user) == []
        assert await _sessions(db_session, user) == 0
        # The admin's own devices are untouched (none were ever created here).
        assert await _devices(db_session, admin) == []

    async def test_suspending_forgets_every_device(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        admin = await _make_admin(db_session, hospital_id)
        token = _held(await _trusted_browser(net, user.email))

        response = await net.browser().post(f"{USERS}/{user.id}/deactivate", headers=_bearer(admin))

        assert response.status_code == 200, response.text
        assert await _devices(db_session, user) == []
        assert await _sessions(db_session, user) == 0
        assert (await _presenting(net, user.email, token)).status_code == 401

    async def test_reactivating_forgets_every_device(
        self,
        net: _Net,
        db_session: AsyncSession,
        user: User,
        hospital_id: uuid.UUID,
        clock: _Clock,
    ) -> None:
        """A device row that outlived the suspension (restored backup, a race) must not
        come back to life with the account."""
        admin = await _make_admin(db_session, hospital_id)
        browser = await _trusted_browser(net, user.email)
        token = _held(browser)
        suspended = await net.browser().post(
            f"{USERS}/{user.id}/deactivate", headers=_bearer(admin)
        )
        assert suspended.status_code == 200, suspended.text
        now = datetime.now(UTC)
        await TrustedDeviceRepository(db_session).create(
            user.id, hash_token(token), now=now, expires_at=now + timedelta(days=30)
        )
        await db_session.commit()
        assert len(await _devices(db_session, user)) == 1

        response = await net.browser().post(f"{USERS}/{user.id}/reactivate", headers=_bearer(admin))

        assert response.status_code == 200, response.text
        assert await _devices(db_session, user) == []
        await _exhaust_account(net, user.email)
        assert (await _presenting(net, user.email, token)).status_code == 401
        # The account itself works again (once the budget has refilled), from
        # a browser that earns trust anew.
        clock.advance(hours=6)
        assert (await _login(net.browser(), user.email)).status_code == 200

    async def test_a_role_change_ends_sessions_but_forgets_no_device(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        """Attack: anyone who may assign roles strips a colleague of every trusted device
        (and so of the protection against an exhausted shared budget) by giving and
        taking a role. Nothing about the account's credentials is in doubt: its
        sessions end so new ones carry the new roles; its browsers stay recognised."""
        manager = await _make_user(db_session, hospital_id, email=_email("manager"))
        await grant_permissions(
            db_session, hospital_id=hospital_id, user_id=manager.id, codes=["role.assign"]
        )
        role = Role(id=uuid.uuid4(), hospital_id=hospital_id, name=f"ward-{uuid.uuid4().hex[:8]}")
        db_session.add(role)
        await db_session.flush()
        await db_session.commit()
        role_id = role.id
        browser = await _trusted_browser(net, user.email)
        token = _held(browser)
        assert await _sessions(db_session, user) == 1

        assigned = await net.browser().post(
            f"{USERS}/{user.id}/roles", headers=_bearer(manager), json={"role_id": str(role_id)}
        )
        assert assigned.status_code == 200, assigned.text
        assert await _sessions(db_session, user) == 0
        assert await _hashes(db_session, user) == {hash_token(token)}

        assert (await _login(browser, user.email)).status_code == 200
        removed = await net.browser().delete(
            f"{USERS}/{user.id}/roles/{role_id}", headers=_bearer(manager)
        )
        assert removed.status_code == 200, removed.text
        assert await _sessions(db_session, user) == 0
        assert await _hashes(db_session, user) == {hash_token(token)}

        await _exhaust_account(net, user.email)
        assert (await _login(browser, user.email)).status_code == 200

    async def test_another_hospitals_admin_cannot_make_an_account_forget_its_devices(
        self,
        net: _Net,
        db_session: AsyncSession,
        user: User,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Attack: hospital B's admin strips hospital A's user of trusted devices (and so
        of the protection against a distributed attack on the shared budget)."""
        admin_b = await _make_admin(db_session, other_hospital_id)
        token = _held(await _trusted_browser(net, user.email))

        for action in ("reset-password", "deactivate", "reactivate"):
            response = await net.browser().post(
                f"{USERS}/{user.id}/{action}", headers=_bearer(admin_b)
            )
            assert response.status_code == 404, (action, response.text)

        assert await _hashes(db_session, user) == {hash_token(token)}
        await _exhaust_account(net, user.email)
        assert (await _presenting(net, user.email, token)).status_code == 200

    async def test_one_users_logout_all_forgets_nobody_elses_devices(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        colleague = await _make_user(db_session, hospital_id)
        workstation = net.browser()
        assert (await _login(workstation, user.email)).status_code == 200
        assert (await _login(workstation, colleague.email)).status_code == 200
        colleague_hashes = await _hashes(db_session, colleague)

        response = await workstation.post(f"{AUTH}/logout-all", headers=_bearer(user))

        assert response.status_code == 200
        assert await _devices(db_session, user) == []
        assert await _hashes(db_session, colleague) == colleague_hashes
        await _exhaust_account(net, colleague.email)
        assert (await _login(workstation, colleague.email)).status_code == 200


# ── Recovery keeps the other devices ─────────────────────────────────────────


class TestRecoveryKeepsOtherDevices:
    async def test_an_emailed_reset_replaces_this_browsers_token_and_keeps_the_others(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Using the recovery path must not cost the owner every other device — and must
        end any copy of the token this browser held."""
        here = await _trusted_browser(net, user.email)
        elsewhere = await _trusted_browser(net, user.email)
        old_here, kept_elsewhere = _held(here), _held(elsewhere)
        token = await _reset_token(db_session, user)

        reset = await here.post(
            f"{AUTH}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
        )

        assert reset.status_code == 200, reset.text
        new_here = _held(here)
        assert new_here != old_here
        assert old_here not in new_here.split(".")
        assert await _hashes(db_session, user) == {
            hash_token(new_here),
            hash_token(kept_elsewhere),
        }
        await _exhaust_account(net, user.email, NEW_PASSWORD)
        assert (await _presenting(net, user.email, old_here, NEW_PASSWORD)).status_code == 401
        assert (await _login(elsewhere, user.email, NEW_PASSWORD)).status_code == 200
        assert (await _login(here, user.email, NEW_PASSWORD)).status_code == 200

    async def test_a_password_change_replaces_this_browsers_token_and_keeps_the_others(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        here = await _trusted_browser(net, user.email)
        elsewhere = await _trusted_browser(net, user.email)
        old_here, kept_elsewhere = _held(here), _held(elsewhere)

        changed = await here.post(
            f"{AUTH}/password/change",
            headers=_bearer(user),
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        )

        assert changed.status_code == 200, changed.text
        new_here = _held(here)
        assert old_here not in new_here.split(".")
        assert await _hashes(db_session, user) == {
            hash_token(new_here),
            hash_token(kept_elsewhere),
        }
        await _exhaust_account(net, user.email, NEW_PASSWORD)
        assert (await _presenting(net, user.email, old_here, NEW_PASSWORD)).status_code == 401
        assert (await _login(elsewhere, user.email, NEW_PASSWORD)).status_code == 200
        assert (await _login(here, user.email, NEW_PASSWORD)).status_code == 200

    async def test_a_refused_password_change_changes_no_device(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: a stolen session tries to rotate the device token without the password."""
        browser = await _trusted_browser(net, user.email)
        before = await _hashes(db_session, user)

        refused = await browser.post(
            f"{AUTH}/password/change",
            headers=_bearer(user),
            json={"current_password": WRONG_PASSWORD, "new_password": NEW_PASSWORD},
        )

        assert refused.status_code == 401
        assert _set_cookies(refused) == []
        assert await _hashes(db_session, user) == before

    async def test_a_reset_on_a_shared_browser_leaves_the_other_users_token_alone(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        """Attack: redeem your own reset link on a shared browser to knock a colleague's
        trusted device out."""
        colleague = await _make_user(db_session, hospital_id)
        workstation = net.browser()
        assert (await _login(workstation, colleague.email)).status_code == 200
        assert (await _login(workstation, user.email)).status_code == 200
        colleague_hashes = await _hashes(db_session, colleague)
        token = await _reset_token(db_session, user)

        reset = await workstation.post(
            f"{AUTH}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
        )

        assert reset.status_code == 200, reset.text
        assert await _hashes(db_session, colleague) == colleague_hashes
        await _exhaust_account(net, colleague.email)
        assert (await _login(workstation, colleague.email)).status_code == 200

    async def test_a_reset_makes_the_browser_trusted_for_the_tokens_account_only(
        self, net: _Net, db_session: AsyncSession, user: User, hospital_id: uuid.UUID
    ) -> None:
        """Attack: redeem your own reset link while carrying a victim's planted context,
        to come out trusted for the victim."""
        victim = await _make_user(db_session, hospital_id)
        token = await _reset_token(db_session, user)
        attacker_browser = net.browser()

        reset = await attacker_browser.post(
            f"{AUTH}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
        )

        assert reset.status_code == 200
        issued = _held(attacker_browser)
        assert await _hashes(db_session, user) == {hash_token(issued)}
        assert await _devices(db_session, victim) == []
        await _exhaust_account(net, victim.email)
        assert (await _login(attacker_browser, victim.email)).status_code == 401


# ── Refresh ──────────────────────────────────────────────────────────────────


class TestARefreshNeverTrustsABrowser:
    """A live session is not a credential check. If refreshing one made the
    browser a trusted device, whoever held one stolen session could mint
    device after device — each with password attempts of its own, out of reach
    of the account's shared budget, and each surviving the session's end."""

    async def test_a_refresh_from_an_unrecognised_browser_trusts_nothing(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: a stolen refresh token, used from the thief's own browser."""
        signed_in = await _login(net.browser(), user.email)
        refresh_token = signed_in.json()["data"]["refresh_token"]
        before = await _hashes(db_session, user)
        events = await _trust_events(db_session, user)
        thief = net.browser()  # holds the session, but no device cookie

        refreshed = await thief.post(f"{AUTH}/refresh", json={"refresh_token": refresh_token})

        assert refreshed.status_code == 200, refreshed.text
        assert _set_cookies(refreshed) == []
        assert not thief.cookies
        assert await _hashes(db_session, user) == before
        assert await _trust_events(db_session, user) == events
        # With the shared budget gone, that browser is a stranger like any other.
        await _exhaust_account(net, user.email)
        assert (await _login(thief, user.email)).status_code == 401

    async def test_a_stolen_session_cannot_mint_devices_by_refreshing_again_and_again(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work
    ) -> None:
        """Attack: rotate the stolen session over and over, from a new browser and address
        each time, collecting a device (five password guesses, plus ten under its
        ceiling) per rotation."""
        signed_in = await _login(net.browser(), user.email)
        refresh_token = signed_in.json()["data"]["refresh_token"]
        before = await _hashes(db_session, user)
        assert len(before) == 1
        thieves = []

        for _ in range(DEVICES_PER_ACCOUNT + 2):
            thief = net.browser()
            refreshed = await thief.post(f"{AUTH}/refresh", json={"refresh_token": refresh_token})
            assert refreshed.status_code == 200, refreshed.text
            assert _set_cookies(refreshed) == []
            refresh_token = refreshed.json()["data"]["refresh_token"]
            thieves.append(thief)

        # Not one row more, and the owner's own device was not pushed out.
        assert await _hashes(db_session, user) == before
        assert await _trust_events(db_session, user) == 1
        # So every guess from those browsers lands on the shared budget.
        start = work.real
        for thief in thieves:
            for _ in range(2):
                assert (await _login(thief, user.email, WRONG_PASSWORD)).status_code == 401
        assert work.real == start + ACCOUNT_BURST
        assert (
            await _budget_spent(
                db_session, bucket(BucketKind.PW_ACCOUNT, user.email), ACCOUNT_REFILL
            )
            == ACCOUNT_BURST
        )

    async def test_a_refresh_from_a_recognised_browser_changes_nothing(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        browser = net.browser()
        signed_in = await _login(browser, user.email)
        refresh_token = signed_in.json()["data"]["refresh_token"]
        before = await _hashes(db_session, user)
        held = _held(browser)
        (row_before,) = await _devices(db_session, user)
        used_before = row_before.last_used_at

        refreshed = await browser.post(f"{AUTH}/refresh", json={"refresh_token": refresh_token})

        assert refreshed.status_code == 200
        assert _set_cookies(refreshed) == []
        assert _held(browser) == held
        assert await _hashes(db_session, user) == before
        # Not even "came back": a refresh is not a sign-in, and must not buy
        # a row the standing of an established browser.
        (row_after,) = await _devices(db_session, user)
        assert row_after.last_used_at == used_before

    async def test_a_refresh_does_not_adopt_a_planted_token(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Fixation through the refresh path."""
        signed_in = await _login(net.browser(), user.email)
        refresh_token = signed_in.json()["data"]["refresh_token"]
        before = await _hashes(db_session, user)
        planted = _planted_token()

        refreshed = await net.browser().post(
            f"{AUTH}/refresh",
            json={"refresh_token": refresh_token},
            headers={"Cookie": f"{DEV_COOKIE}={planted}"},
        )

        assert refreshed.status_code == 200
        assert _set_cookies(refreshed) == []
        assert await _hashes(db_session, user) == before
        await _exhaust_account(net, user.email)
        assert (await _presenting(net, user.email, planted)).status_code == 401


# ── Recognition is a convenience ─────────────────────────────────────────────


class TestAFailedTrustWriteNeverFailsTheSignIn:
    async def test_sign_in_succeeds_when_the_device_cannot_be_recorded(
        self, net: _Net, db_session: AsyncSession, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack on availability: make the trusted-device write fail and see whether
        that keeps staff out."""

        async def _fail(*_args: Any, **_kwargs: Any) -> TrustedDevice:
            raise SQLAlchemyError("trusted_devices is unavailable")

        monkeypatch.setattr(TrustedDeviceRepository, "create", _fail)
        browser = net.browser()

        response = await _login(browser, user.email)

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["access_token"]
        assert data["refresh_token"]
        assert _set_cookies(response) == []
        # The service rolled back on this shared session, which expires the
        # test's own copy of the user; a real request has a session to itself.
        await db_session.refresh(user)
        assert await _devices(db_session, user) == []
        assert await _trust_events(db_session, user) == 0
        # The session it issued is real and durable.
        assert await _sessions(db_session, user) == 1
        refreshed = await browser.post(
            f"{AUTH}/refresh", json={"refresh_token": data["refresh_token"]}
        )
        assert refreshed.status_code == 200, refreshed.text
        assert _set_cookies(refreshed) == []

    async def test_a_real_constraint_violation_in_the_trust_write_is_survived(
        self, net: _Net, db_session: AsyncSession, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same with a genuine database error: the insert collides on ``token_hash``,
        the transaction is rolled back, and the sign-in still stands."""
        first = await _trusted_browser(net, user.email)
        existing = hash_token(_held(first))
        real_create = TrustedDeviceRepository.create
        collisions: list[str] = []

        async def _collide(
            repository: TrustedDeviceRepository,
            user_id: uuid.UUID,
            _token_hash: str,
            *,
            now: datetime,
            expires_at: datetime,
            mfa_verified: bool = False,
        ) -> TrustedDevice:
            collisions.append(existing)
            return await real_create(
                repository,
                user_id,
                existing,
                now=now,
                expires_at=expires_at,
                mfa_verified=mfa_verified,
            )

        monkeypatch.setattr(TrustedDeviceRepository, "create", _collide)
        sessions_before = await _sessions(db_session, user)

        response = await _login(net.browser(), user.email)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["refresh_token"]
        assert _set_cookies(response) == []
        # It was the database that refused the row, not the test double.
        assert collisions == [existing]
        # The service rolled back on this shared session, which expires the
        # test's own copy of the user; a real request has a session to itself.
        await db_session.refresh(user)
        assert await _hashes(db_session, user) == {existing}
        assert await _sessions(db_session, user) == sessions_before + 1
        # Nothing was left broken: the existing device still signs in.
        monkeypatch.setattr(TrustedDeviceRepository, "create", real_create)
        assert (await _login(first, user.email)).status_code == 200

    async def test_a_real_constraint_violation_does_not_fail_a_password_reset(
        self, net: _Net, db_session: AsyncSession, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The emailed-link path, with the same genuine database error."""
        first = await _trusted_browser(net, user.email)
        existing = hash_token(_held(first))
        real_create = TrustedDeviceRepository.create
        collisions: list[str] = []

        async def _collide(
            repository: TrustedDeviceRepository,
            user_id: uuid.UUID,
            _token_hash: str,
            *,
            now: datetime,
            expires_at: datetime,
            mfa_verified: bool = False,
        ) -> TrustedDevice:
            collisions.append(existing)
            return await real_create(
                repository,
                user_id,
                existing,
                now=now,
                expires_at=expires_at,
                mfa_verified=mfa_verified,
            )

        monkeypatch.setattr(TrustedDeviceRepository, "create", _collide)
        token = await _reset_token(db_session, user)

        reset = await net.browser().post(
            f"{AUTH}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
        )

        assert reset.status_code == 200, reset.text
        assert _set_cookies(reset) == []
        assert collisions == [existing]
        await db_session.refresh(user)
        assert await _hashes(db_session, user) == {existing}
        monkeypatch.setattr(TrustedDeviceRepository, "create", real_create)
        assert (await _login(first, user.email, NEW_PASSWORD)).status_code == 200

    async def test_a_failed_trust_write_does_not_fail_a_password_reset(
        self, net: _Net, db_session: AsyncSession, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fail(*_args: Any, **_kwargs: Any) -> TrustedDevice:
            raise SQLAlchemyError("trusted_devices is unavailable")

        monkeypatch.setattr(TrustedDeviceRepository, "create", _fail)
        token = await _reset_token(db_session, user)

        reset = await net.browser().post(
            f"{AUTH}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
        )

        assert reset.status_code == 200, reset.text
        assert _set_cookies(reset) == []
        await db_session.refresh(user)
        assert await _devices(db_session, user) == []
        # The new password is in force and the token is spent.
        monkeypatch.undo()
        assert (await _login(net.browser(), user.email, NEW_PASSWORD)).status_code == 200
        again = await net.browser().post(
            f"{AUTH}/password/reset", json={"token": token, "new_password": "Third!Passw0rd789"}
        )
        assert again.status_code == 401
