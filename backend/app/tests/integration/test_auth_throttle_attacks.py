"""The authentication throttle, attacked (P3b).

Every test here is an attack on ``app/services/auth_throttle.py`` as wired
into ``AuthService``, replayed through real HTTP against the application and a
real PostgreSQL. Each one states the attack and asserts what the attacker must
not get.

**How the source address is varied.** ``RATE_LIMIT_TRUST_PROXY_HEADER`` is
switched on and the ASGI transport's socket peer is ``127.0.0.1`` — a trusted
proxy under the default ``RATE_LIMIT_TRUSTED_PROXY_CIDRS`` — so the request's
``X-Forwarded-For`` is resolved by the real ``app.core.client_ip.client_ip``,
exactly as behind a load balancer. Nothing in the resolver is patched.

**How time is moved.** Only through the throttle's clock
(``AuthThrottleRepository.now``), which is offset; nothing sleeps.

**What "evaluated" means.** A password attempt is evaluated when
``verify_password`` (or, for an unknown address, ``burn_password_verification``)
runs; a code is evaluated when ``verify_totp_code`` runs. They are counted.

**What the shared session can and cannot prove.** Most tests use the suite's
``db_session``: one connection inside one rolled-back transaction, on which
requests can only run one after another. That proves the arithmetic, not the
locking. ``TestConcurrentAttempts`` therefore uses a pool of real connections
with real commits and fires its requests at once, and deletes what it wrote.

The anonymous per-address request limit (``RATE_LIMIT_ANON_PER_MIN``, a
different layer with its own tests) is raised for these tests, because several
of them send more than sixty requests from one address.
"""

from __future__ import annotations

import asyncio
import math
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
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
    hash_token,
    verify_password,
)
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.auth_throttle import AuthThrottleBucket, TrustedDevice
from app.models.hospital import Hospital
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.auth_throttle_repository import AuthThrottleRepository
from app.services import auth_service as auth_service_module
from app.services.auth_throttle import (
    POLICIES,
    Admission,
    AuthThrottle,
    Backoff,
    Bucket,
    BucketKind,
    Budget,
    bucket,
)
from app.tests.conftest import grant_permissions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.database

PASSWORD = "Str0ng!Passw0rd123"  # noqa: S105 — a test credential, not a real one
WRONG_PASSWORD = "Wr0ng!Passw0rd999"  # noqa: S105
NEW_PASSWORD = "An0ther!Passw0rd456"  # noqa: S105
AUTH = "/api/v1/auth"
USERS = "/api/v1/users"
GENERIC_LOGIN_FAILURE = {
    "success": False,
    "message": "Invalid credentials.",
    "error_code": "AUTHENTICATION_REQUIRED",
    "errors": None,
}
GENERIC_MFA_FAILURE = {**GENERIC_LOGIN_FAILURE, "message": "Invalid MFA code."}
DEVICE_COOKIE = "aetheris-device"

# The documented numbers (the module docstring and POLICIES of
# app/services/auth_throttle.py), restated here on purpose: if somebody loosens
# the policy, these tests must fail rather than follow it.
PAIR_FREE = 5
PAIR_WAITS = (60, 120, 240, 480, 960, 1800, 1800, 1800)
ACCOUNT_BURST = 20
ACCOUNT_REFILL = timedelta(minutes=15)
DEVICE_FREE = 5
DEVICE_CAP_BURST = 10
MFA_FREE = 5
MFA_ACCOUNT_BURST = 10
MFA_ACCOUNT_REFILL = timedelta(minutes=30)
MFA_WAITS = (60, 120, 240, 480)
MFA_DEVICE_BURST = 10
MFA_DEVICE_REFILL = timedelta(minutes=30)

#: The socket peer of every test request: a proxy we "operate".
_PROXY_PEER = ("127.0.0.1", 40000)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _body(response: Response) -> dict[str, Any]:
    """The response envelope without its per-request metadata."""
    payload: dict[str, Any] = response.json()
    payload.pop("metadata", None)
    return payload


def _email(prefix: str = "staff") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@hospital.example"


def _source() -> str:
    """A public IPv4 address nobody else in this run is using."""
    return f"203.{secrets.randbelow(256)}.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}"


def _new_user(hospital_id: uuid.UUID, **overrides: Any) -> User:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "hospital_id": hospital_id,
        "email": _email(),
        "password_hash": hash_password(PASSWORD),
        "first_name": "Throttle",
        "last_name": "Tester",
        "password_changed_at": datetime.now(UTC),
    }
    values.update(overrides)
    return User(**values)


async def _make_user(session: AsyncSession, hospital_id: uuid.UUID, **overrides: Any) -> User:
    user = _new_user(hospital_id, **overrides)
    session.add(user)
    await session.flush()
    await session.refresh(user)
    # Committed (to the test's outer, rolled-back transaction) so that a
    # rollback inside the code under test cannot take the fixture rows with it.
    await session.commit()
    return user


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
    the boundary. (This is the authenticator's clock, not the throttle's:
    throttle time is only ever moved through ``_Clock``.)
    """
    totp = pyotp.TOTP(secret)
    left = totp.interval - time.time() % totp.interval
    if left < 1.5:
        await asyncio.sleep(left + 0.05)
    return totp.now()


async def _login(
    client: AsyncClient, email: str, password: str = PASSWORD, *, source: str | None = None
) -> Response:
    """POST /auth/login. ``source`` overrides the client's own address for one request."""
    headers = {"X-Forwarded-For": source} if source is not None else None
    return await client.post(
        f"{AUTH}/login", json={"email": email, "password": password}, headers=headers
    )


async def _verify(client: AsyncClient, ticket: str, code: str) -> Response:
    return await client.post(f"{AUTH}/mfa/verify", json={"mfa_ticket": ticket, "code": code})


async def _ticket(client: AsyncClient, user: User, password: str = PASSWORD) -> str:
    response = await _login(client, user.email, password)
    assert response.status_code == 200, response.text
    ticket: str = response.json()["data"]["mfa_ticket"]
    return ticket


async def _both_factors(client: AsyncClient, user: User, secret: str) -> Response:
    """A whole sign-in to an MFA account: the password, then the right code."""
    return await _verify(client, await _ticket(client, user), await _right_code(secret))


def _issued(response: Response) -> str | None:
    """The device-cookie value this response set, if it set one."""
    for header in response.headers.get_list("set-cookie"):
        key, _, value = header.split(";", 1)[0].partition("=")
        if key.strip() == DEVICE_COOKIE:
            return value.strip().strip('"')
    return None


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(user_id=user.id, hospital_id=user.hospital_id)
    return {"Authorization": f"Bearer {token}"}


async def _sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id)
    )
    return int(result.scalar_one())


async def _row(session: AsyncSession, target: Bucket) -> AuthThrottleBucket | None:
    result = await session.execute(
        select(AuthThrottleBucket)
        .where(AuthThrottleBucket.key_hash == target.key_hash)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _spent(session: AsyncSession, target: Bucket) -> int:
    """How many units of a budget bucket are currently used up (by the throttle's clock)."""
    policy = target.policy
    assert isinstance(policy, Budget)
    row = await _row(session, target)
    if row is None or row.drains_at is None:
        return 0
    now = await AuthThrottleRepository(session).now()
    return max(0, math.ceil((row.drains_at - now) / policy.refill))


async def _backoff(session: AsyncSession, target: Bucket) -> tuple[int, datetime | None]:
    """A backoff bucket's ``(failures, blocked_until)``; a row that is gone holds nothing."""
    row = await _row(session, target)
    return (0, None) if row is None else (row.failures, row.blocked_until)


async def _keys_of_kind(session: AsyncSession, kind: BucketKind) -> set[str]:
    """Every bucket of one kind that anything has drawn on, in the whole table."""
    result = await session.execute(
        select(AuthThrottleBucket.key_hash).where(AuthThrottleBucket.kind == kind.value)
    )
    return set(result.scalars().all())


async def _snapshot(session: AsyncSession, target: Bucket) -> tuple[Any, ...] | None:
    row = await _row(session, target)
    if row is None:
        return None
    return (row.failures, row.blocked_until, row.last_charged_at, row.drains_at)


async def _only_device(session: AsyncSession, user: User) -> TrustedDevice:
    """The one trusted-device row of an account that has signed in from one browser."""
    result = await session.execute(select(TrustedDevice).where(TrustedDevice.user_id == user.id))
    return result.scalars().one()


async def _count_all(session: AsyncSession, model: Any) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def _reset_token(session: AsyncSession, user: User) -> str:
    """A reset token as the emailed link would carry it.

    Written straight to the table: the test environment has no mail transport,
    and with none configured the application (correctly) mints no token.
    """
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
    """Counts credential evaluations. Hashing runs in a thread pool, hence the lock."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._lock = threading.Lock()
        self.real = 0
        self.dummy = 0
        self.codes = 0
        real_verify = security_module.verify_password
        real_burn = security_module.burn_password_verification
        real_totp = security_module.verify_totp_code

        def _verify(password: str, hashed: str) -> bool:
            with self._lock:
                self.real += 1
            return real_verify(password, hashed)

        def _burn(password: str) -> None:
            with self._lock:
                self.dummy += 1
            real_burn(password)

        def _totp(secret: str, code: str) -> bool:
            with self._lock:
                self.codes += 1
            return real_totp(secret, code)

        monkeypatch.setattr(auth_service_module, "verify_password", _verify)
        monkeypatch.setattr(auth_service_module, "burn_password_verification", _burn)
        monkeypatch.setattr(auth_service_module, "verify_totp_code", _totp)

    @property
    def passwords(self) -> int:
        return self.real + self.dummy


class _Net:
    """Hands out browsers: HTTP clients with their own cookie jar and address."""

    def __init__(self, application: FastAPI) -> None:
        self._application = application
        self._clients: list[AsyncClient] = []

    def browser(
        self,
        source: str | None = None,
        *,
        peer: tuple[str, int] = _PROXY_PEER,
        cookie: str | None = None,
    ) -> AsyncClient:
        """A new browser with an empty cookie jar, seen as coming from ``source``.

        :param cookie: A device-cookie value this client sends on every request,
            whatever the server answers: a copied cookie in a thief's hands.
        """
        headers = {"X-Forwarded-For": source or _source()}
        if cookie is not None:
            headers["Cookie"] = f"{DEVICE_COOKIE}={cookie}"
        client = AsyncClient(
            transport=ASGITransport(app=self._application, client=peer),
            base_url="http://test",
            headers=headers,
        )
        self._clients.append(client)
        return client

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()


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
    """Browsers talking to the application on the test's rolled-back session."""
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


async def _exhaust_pair(net: _Net, email: str, source: str) -> AsyncClient:
    """One attacker, one address: use up the free attempts on ``email``."""
    attacker = net.browser(source)
    for _ in range(PAIR_FREE):
        assert (await _login(attacker, email, WRONG_PASSWORD)).status_code == 401
    return attacker


async def _exhaust_account(net: _Net, email: str) -> None:
    """A botnet: one wrong password from each of ``ACCOUNT_BURST`` addresses."""
    for _ in range(ACCOUNT_BURST):
        assert (await _login(net.browser(), email, WRONG_PASSWORD)).status_code == 401


async def _trusted_browser(net: _Net, email: str, source: str | None = None) -> AsyncClient:
    """A browser that has completed a sign-in, and so holds a device cookie."""
    browser = net.browser(source)
    response = await _login(browser, email)
    assert response.status_code == 200, response.text
    assert browser.cookies.get(DEVICE_COOKIE), "the sign-in did not set a device cookie"
    return browser


async def _mfa_trusted_browser(
    net: _Net, user: User, secret: str, source: str | None = None
) -> AsyncClient:
    """A browser that has completed both factors, and so holds an MFA-verified device cookie."""
    browser = net.browser(source)
    response = await _both_factors(browser, user, secret)
    assert response.status_code == 200, response.text
    assert browser.cookies.get(DEVICE_COOKIE), "the sign-in did not set a device cookie"
    return browser


def _held(browser: AsyncClient) -> str:
    value = browser.cookies.get(DEVICE_COOKIE)
    assert value is not None
    return value


async def _make_admin(session: AsyncSession, hospital_id: uuid.UUID) -> User:
    admin = await _make_user(session, hospital_id, email=_email("admin"))
    await grant_permissions(
        session, hospital_id=hospital_id, user_id=admin.id, codes=["user.reset_password"]
    )
    await session.commit()
    return admin


# ── 1. Knowing the email is not enough to lock the owner out ─────────────────


class TestOwnerIsNotLockedOut:
    """The old lockout: five wrong passwords kept the owner out for thirty minutes."""

    async def test_the_resolver_really_sees_the_forwarded_address(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Guard for every test below: the varied source is what the throttle counts."""
        source = _source()
        await _login(net.browser(source), user.email, WRONG_PASSWORD)

        pair = await _row(db_session, bucket(BucketKind.PW_PAIR, user.email, source))
        assert pair is not None
        assert pair.failures == 1

    async def test_an_attacker_on_one_source_cannot_keep_the_owner_off_a_trusted_device(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work
    ) -> None:
        """Attack: exhaust the allowance for the owner's email, from the owner's own address.

        The owner's browser sits behind the same address as the attacker (one
        hospital, one NAT) — the worst case for the owner.
        """
        shared_address = _source()
        owner = await _trusted_browser(net, user.email, shared_address)
        attacker = await _exhaust_pair(net, user.email, shared_address)
        # The attacker is now being refused — even the right password.
        evaluated = work.passwords
        assert (await _login(attacker, user.email, PASSWORD)).status_code == 401
        assert work.passwords == evaluated

        for _ in range(3):
            response = await _login(owner, user.email)
            assert response.status_code == 200, response.text
            assert response.json()["data"]["refresh_token"]
            # ... while the attacker carries on and is still refused.
            assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401

    async def test_an_attacker_on_one_source_cannot_keep_the_owner_off_a_fresh_source(
        self, net: _Net, user: User, work: _Work
    ) -> None:
        """Attack: the same, against an owner with no trusted device, somewhere else."""
        attacker = await _exhaust_pair(net, user.email, _source())
        evaluated = work.passwords
        assert (await _login(attacker, user.email, PASSWORD)).status_code == 401
        assert work.passwords == evaluated

        for _ in range(3):
            # A browser with no cookie, at an address that has never failed.
            response = await _login(net.browser(), user.email)
            assert response.status_code == 200, response.text
            assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401

    async def test_the_owner_keeps_getting_in_while_the_attacker_goes_on_for_hours(
        self, net: _Net, db_session: AsyncSession, user: User, clock: _Clock
    ) -> None:
        """Attack: guess as fast as the throttle allows, for six hours.

        One source can never use up the account's shared budget — its own
        backoff is slower than the budget refills — so at every moment the
        owner gets in both from a trusted device and from a new address.
        """
        owner_device = await _trusted_browser(net, user.email)
        attacker = net.browser(_source())
        account = bucket(BucketKind.PW_ACCOUNT, user.email)
        most_spent = 0

        for _ in range(36):  # 36 x 10 minutes
            for _ in range(3):
                assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401
            most_spent = max(most_spent, await _spent(db_session, account))

            trusted = await _login(owner_device, user.email)
            assert trusted.status_code == 200, trusted.text
            elsewhere = await _login(net.browser(), user.email)
            assert elsewhere.status_code == 200, elsewhere.text
            clock.advance(minutes=10)

        assert 0 < most_spent < ACCOUNT_BURST


# ── 2. One source is throttled ───────────────────────────────────────────────


class TestOneSourceIsThrottled:
    async def test_after_the_free_attempts_the_correct_password_is_refused_unevaluated(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work
    ) -> None:
        """Attack: keep guessing from one address; the sixth guess is the right one."""
        attacker = net.browser(_source())
        for _ in range(PAIR_FREE):
            assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401
        assert work.real == PAIR_FREE

        response = await _login(attacker, user.email, PASSWORD)

        assert response.status_code == 401
        assert _body(response) == GENERIC_LOGIN_FAILURE
        # The password was never looked at: no verification ran, real or dummy.
        assert work.real == PAIR_FREE
        assert work.dummy == 0
        assert await _sessions(db_session, user.id) == 0

    async def test_hammering_a_closed_bucket_changes_nothing(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work, clock: _Clock
    ) -> None:
        """Attack: flood while blocked, to stretch the owner's wait or drain the account budget."""
        source = _source()
        attacker = await _exhaust_pair(net, user.email, source)
        pair = bucket(BucketKind.PW_PAIR, user.email, source)
        account = bucket(BucketKind.PW_ACCOUNT, user.email)
        before = (await _snapshot(db_session, pair), await _snapshot(db_session, account))

        for _ in range(25):
            assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401

        assert work.real == PAIR_FREE
        assert (await _snapshot(db_session, pair), await _snapshot(db_session, account)) == before
        assert await _spent(db_session, account) == PAIR_FREE
        # The first wait is still one minute, not one minute after the last refusal.
        clock.advance(seconds=PAIR_WAITS[0] + 1)
        assert (await _login(attacker, user.email, PASSWORD)).status_code == 200

    async def test_the_waits_double_up_to_the_cap(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work, clock: _Clock
    ) -> None:
        """Attack: wait out each block and guess again. Each wait is twice the last, to 30 min."""
        source = _source()
        attacker = await _exhaust_pair(net, user.email, source)
        pair = bucket(BucketKind.PW_PAIR, user.email, source)
        evaluated = PAIR_FREE

        for wait in PAIR_WAITS:
            row = await _row(db_session, pair)
            assert row is not None
            assert row.blocked_until is not None
            assert row.last_charged_at is not None
            assert (row.blocked_until - row.last_charged_at).total_seconds() == wait

            # Just before the wait ends even the right password is refused, unevaluated.
            clock.advance(seconds=wait - 20)
            assert (await _login(attacker, user.email, PASSWORD)).status_code == 401
            assert work.real == evaluated
            # Just after it, exactly one more guess is evaluated ...
            clock.advance(seconds=21)
            assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401
            evaluated += 1
            assert work.real == evaluated
            # ... and the next is refused again at once.
            assert (await _login(attacker, user.email, PASSWORD)).status_code == 401
            assert work.real == evaluated

        assert await _sessions(db_session, user.id) == 0

    async def test_forged_forwarded_for_entries_do_not_buy_a_new_source(
        self, net: _Net, user: User, work: _Work
    ) -> None:
        """Attack: send a different ``X-Forwarded-For`` each time; the proxy appends the real one."""
        real_address = _source()
        attacker = net.browser(real_address)
        for _ in range(12):
            forged = f"{_source()}, {_source()}, {real_address}"
            assert (
                await _login(attacker, user.email, WRONG_PASSWORD, source=forged)
            ).status_code == 401

        assert work.real == PAIR_FREE
        assert (
            await _login(attacker, user.email, PASSWORD, source=f"{_source()}, {real_address}")
        ).status_code == 401
        assert work.real == PAIR_FREE

    async def test_a_direct_caller_cannot_name_its_own_address(
        self, net: _Net, user: User, work: _Work
    ) -> None:
        """Attack: reach the application directly (not through our proxy) and claim addresses."""
        attacker = net.browser(peer=("198.18.7.7", 51000))
        for _ in range(12):
            assert (
                await _login(attacker, user.email, WRONG_PASSWORD, source=_source())
            ).status_code == 401

        assert work.real == PAIR_FREE


# ── 3 and 4. Many sources, one account ───────────────────────────────────────


class TestDistributedAttack:
    async def test_many_sources_are_cut_off_at_the_account_budget(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work
    ) -> None:
        """Attack: one guess from each of sixty addresses. Twenty are evaluated."""
        statuses = [
            (await _login(net.browser(), user.email, WRONG_PASSWORD)).status_code for _ in range(60)
        ]

        assert statuses == [401] * 60
        assert work.real == ACCOUNT_BURST
        # The twenty-first address is refused with the right password too.
        response = await _login(net.browser(), user.email, PASSWORD)
        assert response.status_code == 401
        assert _body(response) == GENERIC_LOGIN_FAILURE
        assert work.real == ACCOUNT_BURST
        assert await _sessions(db_session, user.id) == 0

    async def test_the_attempt_that_empties_the_budget_says_so_on_the_audit_trail(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: a distributed attack that goes quiet in the logs. Once the shared budget
        is gone every further guess is refused unevaluated and leaves no entry — so
        the last evaluated one must record that the allowance ran out."""
        for _ in range(ACCOUNT_BURST + 15):
            assert (await _login(net.browser(), user.email, WRONG_PASSWORD)).status_code == 401

        entries = (
            (
                await db_session.execute(
                    select(AuditLog)
                    .where(AuditLog.target_id == user.id, AuditLog.action == "auth.login.failed")
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(entries) == ACCOUNT_BURST
        flagged = [e for e in entries if (e.context or {}).get("budget_exhausted")]
        assert len(flagged) == 1
        assert flagged[0].context is not None
        assert flagged[0].context["budget_exhausted"] == [BucketKind.PW_ACCOUNT.value]
        assert flagged[0].context["reason"] == "invalid_password"

    async def test_more_sources_buy_nothing_and_only_time_refills_the_budget(
        self, net: _Net, user: User, work: _Work, clock: _Clock
    ) -> None:
        """Attack: add addresses, and guesses per address. The budget is 20, then 4 an hour."""
        for _ in range(30):
            attacker = net.browser()
            for _ in range(3):
                assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401
        assert work.real == ACCOUNT_BURST

        clock.advance(seconds=ACCOUNT_REFILL.total_seconds() + 1)
        for _ in range(10):
            assert (await _login(net.browser(), user.email, WRONG_PASSWORD)).status_code == 401
        assert work.real == ACCOUNT_BURST + 1

        clock.advance(hours=1)
        for _ in range(10):
            assert (await _login(net.browser(), user.email, WRONG_PASSWORD)).status_code == 401
        assert work.real == ACCOUNT_BURST + 1 + 4

    async def test_the_owners_trusted_device_signs_in_during_the_attack(
        self, net: _Net, user: User
    ) -> None:
        """Attack: drain the account's budget from many addresses to keep the owner out."""
        owner = await _trusted_browser(net, user.email)
        await _exhaust_account(net, user.email)

        for _ in range(3):
            response = await _login(owner, user.email)
            assert response.status_code == 200, response.text
            # The attack continues and stays cut off.
            assert (await _login(net.browser(), user.email, WRONG_PASSWORD)).status_code == 401

    async def test_an_untrusted_device_is_refused_until_it_completes_an_emailed_reset(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work
    ) -> None:
        """During the attack: no cookie, no entry — until the mailbox proves who is asking."""
        owner_at_work = await _trusted_browser(net, user.email)
        await _exhaust_account(net, user.email)
        evaluated = work.real
        owner_at_home = net.browser()

        # Right password, unrecognised browser, budget exhausted: refused unevaluated.
        refused = await _login(owner_at_home, user.email)
        assert refused.status_code == 401
        assert _body(refused) == GENERIC_LOGIN_FAILURE
        assert work.real == evaluated
        assert owner_at_home.cookies.get(DEVICE_COOKIE) is None

        # The owner uses the emailed link in that browser.
        token = await _reset_token(db_session, user)
        reset = await owner_at_home.post(
            f"{AUTH}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
        )
        assert reset.status_code == 200, reset.text
        assert owner_at_home.cookies.get(DEVICE_COOKIE)

        # That browser is now a trusted device and signs in, budget still exhausted.
        signed_in = await _login(owner_at_home, user.email, NEW_PASSWORD)
        assert signed_in.status_code == 200, signed_in.text
        # The reset did not reopen the door for everybody else ...
        stranger = await _login(net.browser(), user.email, NEW_PASSWORD)
        assert stranger.status_code == 401
        # ... and did not cost the owner the other device.
        assert (await _login(owner_at_work, user.email, NEW_PASSWORD)).status_code == 200


# ── 5. Throttling ends ───────────────────────────────────────────────────────


class TestThrottlingExpires:
    async def test_the_right_password_works_once_the_source_wait_is_over(
        self, net: _Net, user: User, clock: _Clock
    ) -> None:
        attacker_or_owner = await _exhaust_pair(net, user.email, _source())
        assert (await _login(attacker_or_owner, user.email)).status_code == 401

        clock.advance(seconds=PAIR_WAITS[0] + 1)

        assert (await _login(attacker_or_owner, user.email)).status_code == 200

    async def test_the_right_password_works_once_the_device_wait_is_over(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work, clock: _Clock
    ) -> None:
        """A trusted device is throttled too (a stolen cookie is no free pass), and recovers."""
        device = await _trusted_browser(net, user.email)
        evaluated = work.real
        for _ in range(DEVICE_FREE):
            assert (await _login(device, user.email, WRONG_PASSWORD)).status_code == 401
        assert work.real == evaluated + DEVICE_FREE
        # Charged to the device, not to the account's shared budget.
        assert await _spent(db_session, bucket(BucketKind.PW_ACCOUNT, user.email)) == 0

        assert (await _login(device, user.email)).status_code == 401
        assert work.real == evaluated + DEVICE_FREE

        clock.advance(seconds=61)

        assert (await _login(device, user.email)).status_code == 200

    async def test_the_right_password_works_once_the_account_budget_has_refilled(
        self, net: _Net, user: User, clock: _Clock
    ) -> None:
        await _exhaust_account(net, user.email)
        assert (await _login(net.browser(), user.email)).status_code == 401

        clock.advance(seconds=ACCOUNT_REFILL.total_seconds() + 1)

        assert (await _login(net.browser(), user.email)).status_code == 200

    async def test_a_long_quiet_period_returns_a_source_to_its_full_allowance(
        self, net: _Net, user: User, work: _Work, clock: _Clock
    ) -> None:
        """Nothing is permanent: after the quiet period the bucket starts over."""
        source = _source()
        attacker = await _exhaust_pair(net, user.email, source)
        policy = POLICIES[BucketKind.PW_PAIR]
        assert isinstance(policy, Backoff)

        clock.advance(seconds=policy.quiet.total_seconds() + 1)
        for _ in range(PAIR_FREE - 1):
            assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401

        assert work.real == 2 * PAIR_FREE - 1
        assert (await _login(attacker, user.email)).status_code == 200


# ── 6. The second factor ─────────────────────────────────────────────────────


class TestMfaGuessing:
    """The attacker here already has the password, and guesses six-digit codes."""

    async def test_one_source_gets_five_codes_and_then_the_right_code_is_refused(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        attacker = net.browser(_source())
        ticket = await _ticket(attacker, victim)
        for _ in range(MFA_FREE):
            assert (await _verify(attacker, ticket, _wrong_code(secret))).status_code == 401
        assert work.codes == MFA_FREE

        response = await _verify(attacker, ticket, await _right_code(secret))

        assert response.status_code == 401
        assert _body(response) == GENERIC_MFA_FAILURE
        assert work.codes == MFA_FREE
        assert await _sessions(db_session, victim.id) == 0

    async def test_signing_in_again_does_not_reset_the_code_allowance(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """Attack: get a fresh ticket with the password between guesses."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        attacker = net.browser(_source())
        for _ in range(12):
            ticket = await _ticket(attacker, victim)
            assert (await _verify(attacker, ticket, _wrong_code(secret))).status_code == 401

        assert work.codes == MFA_FREE
        ticket = await _ticket(attacker, victim)
        assert (await _verify(attacker, ticket, await _right_code(secret))).status_code == 401
        assert work.codes == MFA_FREE
        assert await _sessions(db_session, victim.id) == 0

    async def test_many_sources_and_many_sign_ins_share_one_code_budget(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        work: _Work,
        clock: _Clock,
    ) -> None:
        """Attack: a new address and a new ticket for every code. Ten codes, then 2 an hour."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        for _ in range(25):
            attacker = net.browser()
            ticket = await _ticket(attacker, victim)
            assert (await _verify(attacker, ticket, _wrong_code(secret))).status_code == 401
        assert work.codes == MFA_ACCOUNT_BURST

        # Even the right code, from an address that has never guessed, is refused.
        fresh = net.browser()
        refused = await _verify(fresh, await _ticket(fresh, victim), await _right_code(secret))
        assert refused.status_code == 401
        assert _body(refused) == GENERIC_MFA_FAILURE
        assert work.codes == MFA_ACCOUNT_BURST
        assert await _sessions(db_session, victim.id) == 0

        # Time gives back one code per half hour, and no more.
        clock.advance(seconds=MFA_ACCOUNT_REFILL.total_seconds() + 1)
        for _ in range(5):
            attacker = net.browser()
            ticket = await _ticket(attacker, victim)
            assert (await _verify(attacker, ticket, _wrong_code(secret))).status_code == 401
        assert work.codes == MFA_ACCOUNT_BURST + 1

    async def test_the_right_code_works_again_once_the_throttle_has_expired(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        clock: _Clock,
    ) -> None:
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        browser = net.browser(_source())
        ticket = await _ticket(browser, victim)
        for _ in range(MFA_FREE):
            assert (await _verify(browser, ticket, _wrong_code(secret))).status_code == 401
        assert (await _verify(browser, ticket, await _right_code(secret))).status_code == 401

        clock.advance(seconds=61)

        response = await _verify(browser, await _ticket(browser, victim), await _right_code(secret))
        assert response.status_code == 200, response.text
        assert await _sessions(db_session, victim.id) == 1

    async def test_code_guesses_inside_a_session_draw_on_the_same_account_budget(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        work: _Work,
    ) -> None:
        """Attack: a stolen session guesses codes at /mfa/disable to dodge the sign-in budget."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        bearer = {
            "Authorization": "Bearer "
            + create_access_token(user_id=victim.id, hospital_id=victim.hospital_id)
        }
        thief = net.browser()
        for _ in range(8):
            response = await thief.post(
                f"{AUTH}/mfa/disable",
                headers=bearer,
                json={"password": PASSWORD, "code": _wrong_code(secret)},
            )
            assert response.status_code == 401
        in_session = work.codes
        assert in_session == MFA_FREE  # the session's own backoff stopped it first

        # Whatever the session used is gone from the sign-in budget as well.
        for _ in range(MFA_ACCOUNT_BURST):
            attacker = net.browser()
            ticket = await _ticket(attacker, victim)
            assert (await _verify(attacker, ticket, _wrong_code(secret))).status_code == 401

        assert work.codes == MFA_ACCOUNT_BURST
        await db_session.refresh(victim)
        assert victim.mfa_enabled is True


# ── 6b. Whose second-factor budget an attempt draws on ───────────────────────


class TestThePasswordAloneCannotLockTheOwnerOutOfMfa:
    """One shared code budget for the whole account would be the old lockout
    again, one step later: whoever has stolen the password could spend it and
    keep the owner out for as long as they liked."""

    async def test_a_password_thief_cannot_spend_the_owners_device_budget(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        work: _Work,
        clock: _Clock,
    ) -> None:
        """Attack: with the password and no device, burn the account's code budget from
        many addresses, and keep at it, so the owner's right code is never looked at.

        The owner's browser has passed the second factor before. It must
        complete it every time; a brand-new browser must not; and everything
        that is not that browser gets ten codes, then one per half hour.
        """
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        owner = await _mfa_trusted_browser(net, victim, secret)
        device = await _only_device(db_session, victim)
        assert device.mfa_verified_at is not None
        shared = bucket(BucketKind.MFA_ACCOUNT, victim.id)
        own = bucket(BucketKind.MFA_DEVICE_CAP, device.id)
        owner_codes = 1  # the sign-in that made the browser a trusted device
        assert await _spent(db_session, shared) == 0

        async def attack(guesses: int) -> None:
            """A new address and a new ticket for every guess."""
            for _ in range(guesses):
                attacker = net.browser()
                response = await _verify(
                    attacker, await _ticket(attacker, victim), _wrong_code(secret)
                )
                assert response.status_code == 401
                assert _body(response) == GENERIC_MFA_FAILURE

        await attack(25)
        assert work.codes - owner_codes == MFA_ACCOUNT_BURST
        assert await _spent(db_session, shared) == MFA_ACCOUNT_BURST

        for _ in range(3):
            # The owner, on the browser that has passed the second factor before.
            signed_in = await _both_factors(owner, victim, secret)
            assert signed_in.status_code == 200, signed_in.text
            assert signed_in.json()["data"]["refresh_token"]
            owner_codes += 1
            # A brand-new browser with the right password and the right code.
            stranger = net.browser()
            refused = await _both_factors(stranger, victim, secret)
            assert refused.status_code == 401
            assert _body(refused) == GENERIC_MFA_FAILURE
            assert "set-cookie" not in refused.headers
            # The attack goes on, and not one more of its codes is looked at.
            await attack(4)
            assert work.codes - owner_codes == MFA_ACCOUNT_BURST

        # The owner's sign-ins cost the owner's device nothing and gave the
        # shared budget nothing back.
        assert await _spent(db_session, own) == 0
        assert await _spent(db_session, shared) == MFA_ACCOUNT_BURST
        assert await _sessions(db_session, victim.id) == owner_codes
        assert (await _only_device(db_session, victim)).id == device.id

        # Time is the only thing that gives the attacker another code: one per
        # half hour, however many addresses ask.
        for extra in (1, 2):
            clock.advance(seconds=MFA_ACCOUNT_REFILL.total_seconds() + 1)
            await attack(6)
            assert work.codes - owner_codes == MFA_ACCOUNT_BURST + extra
            assert (await _both_factors(owner, victim, secret)).status_code == 200
            owner_codes += 1

    async def test_the_owners_own_mistakes_on_that_device_do_not_touch_the_shared_budget(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """And the other way round: what happens on the device stays on the device."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        owner = await _mfa_trusted_browser(net, victim, secret)
        device = await _only_device(db_session, victim)
        ticket = await _ticket(owner, victim)

        for _ in range(MFA_FREE - 1):
            assert (await _verify(owner, ticket, _wrong_code(secret))).status_code == 401

        assert await _spent(db_session, bucket(BucketKind.MFA_DEVICE_CAP, device.id)) == (
            MFA_FREE - 1
        )
        assert await _spent(db_session, bucket(BucketKind.MFA_ACCOUNT, victim.id)) == 0
        assert await _backoff(
            db_session, bucket(BucketKind.MFA_ORIGIN, victim.id, "device", device.id)
        ) == (MFA_FREE - 1, None)
        # So the owner, on a browser that is not that device, is not held up by them.
        evaluated = work.codes
        assert (await _both_factors(net.browser(), victim, secret)).status_code == 200
        assert work.codes == evaluated + 1


class TestACopiedMfaDeviceCookie:
    """The thief here holds a copy of an MFA-verified device cookie *and* the
    password. The copy is worth exactly what the device is worth: one origin
    backoff and one ceiling, both keyed on the server's row."""

    async def test_no_sequence_of_requests_buys_the_copy_a_second_budget(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        work: _Work,
        clock: _Clock,
    ) -> None:
        """Attack: guess codes on the copy, then try everything that might re-key,
        re-issue or reset the device: new addresses, new tickets, the cookie
        doubled, padded, reordered or split over two cookies, a sign-in, a
        refresh and an emailed reset of the thief's *own* account with the copy
        in the jar."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        accomplice = await _make_user(db_session, hospital_id)
        owner = await _mfa_trusted_browser(net, victim, secret)
        stolen = _held(owner)
        device = await _only_device(db_session, victim)
        origin = bucket(BucketKind.MFA_ORIGIN, victim.id, "device", device.id)
        ceiling = bucket(BucketKind.MFA_DEVICE_CAP, device.id)
        shared = bucket(BucketKind.MFA_ACCOUNT, victim.id)
        start = work.codes

        async def guess(cookie: str, code: str) -> Response:
            thief = net.browser(cookie=cookie)  # a new address every time
            response = await _verify(thief, await _ticket(thief, victim), code)
            assert response.status_code == 401
            assert _body(response) == GENERIC_MFA_FAILURE
            assert "set-cookie" not in response.headers
            return response

        for _ in range(15):
            await guess(stolen, _wrong_code(secret))
        assert work.codes - start == MFA_FREE
        failures, blocked_until = await _backoff(db_session, origin)
        assert failures == MFA_FREE
        assert blocked_until is not None

        # The thief's own account, with the copy in the jar.
        own_sign_in = await _login(net.browser(cookie=stolen), accomplice.email)
        assert own_sign_in.status_code == 200, own_sign_in.text
        after_sign_in = _issued(own_sign_in)
        assert after_sign_in is not None
        refreshed = await net.browser(cookie=stolen).post(
            f"{AUTH}/refresh",
            json={"refresh_token": own_sign_in.json()["data"]["refresh_token"]},
        )
        assert refreshed.status_code == 200, refreshed.text
        # A refresh hands out no device cookie and records no device, for anybody.
        assert "set-cookie" not in refreshed.headers
        own_reset = await net.browser(cookie=stolen).post(
            f"{AUTH}/password/reset",
            json={
                "token": await _reset_token(db_session, accomplice),
                "new_password": NEW_PASSWORD,
            },
        )
        assert own_reset.status_code == 200, own_reset.text
        after_reset = _issued(own_reset)
        assert after_reset is not None

        planted = secrets.token_urlsafe(32)
        presentations = [
            stolen,
            f"{stolen}.{stolen}",
            f"{planted}.{stolen}",
            f"{stolen}.{planted}",
            f"{planted}; {DEVICE_COOKIE}={stolen}",
            after_sign_in,
            ".".join(reversed(after_sign_in.split("."))),
            after_reset,
        ]
        # Every one of those cookies still carries the copy ...
        assert all(stolen in presentation for presentation in presentations)
        for presentation in presentations:
            # ... and is still the same device, still waiting: nothing is evaluated.
            await guess(presentation, await _right_code(secret))
            await guess(presentation, _wrong_code(secret))
        assert work.codes - start == MFA_FREE
        # It is the same row, and the only device budget anybody has drawn on.
        assert (await _only_device(db_session, victim)).id == device.id
        assert await _keys_of_kind(db_session, BucketKind.MFA_DEVICE_CAP) == {ceiling.key_hash}
        assert await _spent(db_session, ceiling) == MFA_FREE
        assert await _spent(db_session, shared) == 0

        # Waiting is all that works, and each wait is twice the last.
        evaluated = MFA_FREE
        for wait in MFA_WAITS:
            row = await _row(db_session, origin)
            assert row is not None
            assert row.blocked_until is not None
            assert row.last_charged_at is not None
            assert (row.blocked_until - row.last_charged_at).total_seconds() == wait
            clock.advance(seconds=wait - 20)
            await guess(stolen, await _right_code(secret))
            assert work.codes - start == evaluated
            clock.advance(seconds=21)
            await guess(stolen, _wrong_code(secret))
            evaluated += 1
            assert work.codes - start == evaluated
            await guess(stolen, await _right_code(secret))
            assert work.codes - start == evaluated
        # A quarter of an hour of that: nine codes, inside the device's ten.
        assert evaluated == MFA_FREE + len(MFA_WAITS) < MFA_DEVICE_BURST

        # Throwing the cookie away leaves the budget everybody shares — the
        # one the password alone buys — and that is the end of it.
        for _ in range(MFA_ACCOUNT_BURST + 5):
            anonymous = net.browser()
            response = await _verify(
                anonymous, await _ticket(anonymous, victim), _wrong_code(secret)
            )
            assert response.status_code == 401
        assert work.codes - start == evaluated + MFA_ACCOUNT_BURST
        await guess(stolen, await _right_code(secret))
        assert work.codes - start == evaluated + MFA_ACCOUNT_BURST
        assert await _keys_of_kind(db_session, BucketKind.MFA_DEVICE_CAP) == {ceiling.key_hash}
        assert await _sessions(db_session, victim.id) == 1  # the owner's, from the start

    async def test_the_owners_successes_do_not_lift_the_ceiling_on_the_copy(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        work: _Work,
        clock: _Clock,
    ) -> None:
        """Attack: guess four codes on the copy, wait for the owner to sign in on the real
        browser (which clears the device's backoff), guess four more, and so on for ever.

        The backoff is the device's and the owner's success clears it; the
        ceiling is the device's too and nothing but time gives it back.
        """
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        owner = await _mfa_trusted_browser(net, victim, secret)
        stolen = _held(owner)
        device = await _only_device(db_session, victim)
        origin = bucket(BucketKind.MFA_ORIGIN, victim.id, "device", device.id)
        ceiling = bucket(BucketKind.MFA_DEVICE_CAP, device.id)
        shared = bucket(BucketKind.MFA_ACCOUNT, victim.id)
        owner_codes = 1

        async def guess(code: str) -> Response:
            thief = net.browser(cookie=stolen)
            response = await _verify(thief, await _ticket(thief, victim), code)
            assert response.status_code == 401
            assert _body(response) == GENERIC_MFA_FAILURE
            return response

        for _ in range(2):
            for _ in range(MFA_FREE - 1):
                await guess(_wrong_code(secret))
            assert await _backoff(db_session, origin) == (MFA_FREE - 1, None)
            assert (await _both_factors(owner, victim, secret)).status_code == 200
            owner_codes += 1
            # The device's backoff went with the owner's success ...
            assert await _backoff(db_session, origin) == (0, None)
        for _ in range(2):
            await guess(_wrong_code(secret))
        # ... so ten guesses were evaluated with no wait at all, and that is the ceiling.
        assert work.codes - owner_codes == MFA_DEVICE_BURST
        assert await _spent(db_session, ceiling) == MFA_DEVICE_BURST
        assert await _backoff(db_session, origin) == (2, None)

        # No backoff is in the way, yet the eleventh guess is not looked at —
        # nor the right code, nor either of them after one more try.
        for _ in range(3):
            await guess(_wrong_code(secret))
            await guess(await _right_code(secret))
        assert work.codes - owner_codes == MFA_DEVICE_BURST
        assert await _backoff(db_session, origin) == (2, None)
        assert await _sessions(db_session, victim.id) == owner_codes

        # The owner is not locked out by it: the copy drew on the device only,
        # so a browser without the cookie completes both factors.
        assert await _spent(db_session, shared) == 0
        elsewhere = await _both_factors(net.browser(), victim, secret)
        assert elsewhere.status_code == 200, elsewhere.text
        owner_codes += 1

        # Half an hour buys the copy exactly one more code.
        clock.advance(seconds=MFA_DEVICE_REFILL.total_seconds() + 1)
        for _ in range(3):
            await guess(_wrong_code(secret))
        assert work.codes - owner_codes == MFA_DEVICE_BURST + 1

    async def test_redeeming_the_victims_reset_on_the_copy_gives_up_the_device_budget(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """Attack: the thief also reads the victim's mail. Block the copy, then redeem an
        emailed reset with it in the jar, hoping to come out as a fresh MFA-verified device."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        owner = await _mfa_trusted_browser(net, victim, secret)
        stolen = _held(owner)
        device = await _only_device(db_session, victim)
        start = work.codes
        thief = net.browser(cookie=stolen)
        ticket = await _ticket(thief, victim)
        for _ in range(MFA_FREE + 2):
            assert (await _verify(thief, ticket, _wrong_code(secret))).status_code == 401
        assert work.codes - start == MFA_FREE

        password = PASSWORD
        cookie = stolen
        for round_ in range(4):
            password = f"Thief!Passw0rd-{round_:02d}"
            reset = await net.browser(cookie=cookie).post(
                f"{AUTH}/password/reset",
                json={"token": await _reset_token(db_session, victim), "new_password": password},
            )
            assert reset.status_code == 200, reset.text
            issued = _issued(reset)
            assert issued is not None
            assert stolen not in issued.split(".")
            cookie = issued
            # The browser is a trusted device again — but not one that has
            # passed the second factor.
            replacement = await _only_device(db_session, victim)
            assert replacement.id != device.id
            assert replacement.mfa_verified_at is None
            fresh = net.browser(cookie=cookie)
            ticket = await _ticket(fresh, victim, password)
            for _ in range(MFA_FREE - 1):
                assert (await _verify(fresh, ticket, _wrong_code(secret))).status_code == 401

        # Sixteen more guesses were sent, each round under a new device's own
        # backoff. Ten were evaluated: the account's shared budget, and no more.
        assert work.codes - start == MFA_FREE + MFA_ACCOUNT_BURST
        assert await _keys_of_kind(db_session, BucketKind.MFA_DEVICE_CAP) == {
            bucket(BucketKind.MFA_DEVICE_CAP, device.id).key_hash
        }
        final = net.browser(cookie=cookie)
        refused = await _verify(
            final, await _ticket(final, victim, password), await _right_code(secret)
        )
        assert refused.status_code == 401
        assert work.codes - start == MFA_FREE + MFA_ACCOUNT_BURST
        assert await _sessions(db_session, victim.id) == 1


class TestTheMailboxAloneDoesNotMultiplyCodes:
    async def test_repeated_emailed_resets_never_exceed_the_shared_code_budget(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """Attack: the attacker controls the victim's mailbox and nothing else. Reset the
        password, become "a new trusted device", guess codes just short of that device's
        backoff, reset again, become another new trusted device ...

        (Reset tokens are written straight to the table here, so the limit on
        reset *emails* is not what stops this.) A device trusted through an
        emailed link has not passed the second factor: its codes are charged
        to the account's shared budget.
        """
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        browser = net.browser()
        browsers: list[AsyncClient] = []
        sent = 0

        for round_ in range(6):
            # Odd rounds reuse the last browser (its token is replaced); even
            # rounds start from an empty one at a new address.
            if round_ % 2 == 0:
                browser = net.browser()
            password = f"Mailb0x!Passw0rd-{round_:02d}"
            reset = await browser.post(
                f"{AUTH}/password/reset",
                json={"token": await _reset_token(db_session, victim), "new_password": password},
            )
            assert reset.status_code == 200, reset.text
            assert _issued(reset), "the reset did not make the browser a trusted device"
            browsers.append(browser)
            ticket = await _ticket(browser, victim, password)
            for _ in range(MFA_FREE - 1):
                assert (await _verify(browser, ticket, _wrong_code(secret))).status_code == 401
                sent += 1

        assert sent == 24
        assert work.codes == MFA_ACCOUNT_BURST
        assert await _spent(db_session, bucket(BucketKind.MFA_ACCOUNT, victim.id)) == (
            MFA_ACCOUNT_BURST
        )
        devices = (
            (
                await db_session.execute(
                    select(TrustedDevice).where(TrustedDevice.user_id == victim.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(devices) == 3
        assert [device.mfa_verified_at for device in devices] == [None] * 3
        # No device budget was ever drawn on, by anybody.
        assert await _keys_of_kind(db_session, BucketKind.MFA_DEVICE_CAP) == set()

        # Even the right code, from the newest "trusted" browser, is not looked at.
        response = await _verify(
            browser, await _ticket(browser, victim, password), await _right_code(secret)
        )
        assert response.status_code == 401
        assert _body(response) == GENERIC_MFA_FAILURE
        assert work.codes == MFA_ACCOUNT_BURST
        assert await _sessions(db_session, victim.id) == 0


# ── 6c. The ticket between the two factors ───────────────────────────────────


class TestTheMfaTicketDiesWithThePassword:
    """A ticket says "the password was right a moment ago". Once that password
    is no longer the password, the ticket must be worth nothing — at once, not
    five minutes later, and whoever replaced it."""

    @pytest.mark.parametrize("replaced_by", ["owner-change", "emailed-reset", "admin-reset"])
    async def test_a_ticket_from_before_the_password_was_replaced_is_refused_unevaluated(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        work: _Work,
        replaced_by: str,
    ) -> None:
        """Attack: the attacker has the password and (say, by phishing) a live code. He
        takes a ticket; the owner notices and replaces the password — or has an
        administrator do it — in the very same second; the attacker finishes the sign-in."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        attacker = net.browser()
        ticket = await _ticket(attacker, victim)

        if replaced_by == "owner-change":
            replaced = await net.browser().post(
                f"{AUTH}/password/change",
                headers=_bearer(victim),
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            )
        elif replaced_by == "emailed-reset":
            replaced = await net.browser().post(
                f"{AUTH}/password/reset",
                json={
                    "token": await _reset_token(db_session, victim),
                    "new_password": NEW_PASSWORD,
                },
            )
        else:
            admin = await _make_admin(db_session, hospital_id)
            replaced = await net.browser().post(
                f"{USERS}/{victim.id}/reset-password", headers=_bearer(admin)
            )
        assert replaced.status_code == 200, replaced.text
        sessions = await _sessions(db_session, victim.id)

        for _ in range(3):
            response = await _verify(attacker, ticket, await _right_code(secret))
            assert response.status_code == 401
            assert _body(response) == GENERIC_MFA_FAILURE
            assert "set-cookie" not in response.headers

        # The code was never looked at and nothing was charged: there was no
        # code step to guess at.
        assert work.codes == 0
        assert await _spent(db_session, bucket(BucketKind.MFA_ACCOUNT, victim.id)) == 0
        assert await _sessions(db_session, victim.id) == sessions
        assert not attacker.cookies

    async def test_a_password_replaced_while_the_code_is_being_checked_refuses_the_sign_in(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: the same, with the timing as bad as it can be. The owner's reset lands
        after the attacker's ticket has been accepted and his (phished) code verified,
        but before the session is issued. The ticket was checked against a copy of
        the account read earlier; the session must not be issued on the strength of it.
        """
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        attacker = net.browser()
        ticket = await _ticket(attacker, victim)
        real_settle = AuthThrottle.settle
        landed: list[str] = []

        async def _settle(
            throttle: AuthThrottle, admission: Admission, *, clear: Sequence[Bucket] = ()
        ) -> None:
            # The code has just been verified. Now the password is replaced.
            if any(c.bucket.kind is BucketKind.MFA_ORIGIN for c in admission.charges):
                await db_session.execute(
                    update(User)
                    .where(User.id == victim.id)
                    .values(password_hash=hash_password(NEW_PASSWORD))
                    .execution_options(synchronize_session=False)
                )
                await db_session.commit()
                landed.append("reset")
            await real_settle(throttle, admission, clear=clear)

        monkeypatch.setattr(AuthThrottle, "settle", _settle)

        response = await _verify(attacker, ticket, await _right_code(secret))

        assert landed == ["reset"]
        assert response.status_code == 401
        assert _body(response) == GENERIC_MFA_FAILURE
        assert "set-cookie" not in response.headers
        assert await _sessions(db_session, victim.id) == 0
        devices = await db_session.execute(
            select(func.count())
            .select_from(TrustedDevice)
            .where(TrustedDevice.user_id == victim.id)
        )
        assert devices.scalar_one() == 0

    async def test_mfa_switched_on_while_a_password_only_sign_in_is_in_flight_refuses_it(
        self,
        net: _Net,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: the attacker has the password of an account whose owner is, at this very
        moment, switching the second factor on. His password was verified against the
        account as it was; a session issued now would be one that MFA exists to stop."""
        attacker = net.browser()
        real_settle = AuthThrottle.settle
        landed: list[str] = []

        async def _settle(
            throttle: AuthThrottle, admission: Admission, *, clear: Sequence[Bucket] = ()
        ) -> None:
            # The password has just been verified. Now MFA is confirmed.
            if not landed:
                await db_session.execute(
                    update(User)
                    .where(User.id == user.id)
                    .values(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(generate_totp_secret()))
                    .execution_options(synchronize_session=False)
                )
                await db_session.commit()
                landed.append("mfa on")
            await real_settle(throttle, admission, clear=clear)

        monkeypatch.setattr(AuthThrottle, "settle", _settle)

        response = await _login(attacker, user.email)

        assert landed == ["mfa on"]
        assert response.status_code == 401
        assert _body(response) == GENERIC_LOGIN_FAILURE
        assert "set-cookie" not in response.headers
        assert await _sessions(db_session, user.id) == 0
        devices = await db_session.execute(
            select(func.count()).select_from(TrustedDevice).where(TrustedDevice.user_id == user.id)
        )
        assert devices.scalar_one() == 0
        # And from now on the password step leads to the code step, not to a session.
        monkeypatch.setattr(AuthThrottle, "settle", real_settle)
        again = await _login(attacker, user.email)
        assert again.status_code == 200
        assert "mfa_ticket" in again.json()["data"]
        assert "refresh_token" not in again.json()["data"]

    async def test_a_ticket_from_the_new_password_works(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """The other half: the owner is not shut out by replacing the password."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        stale = await _ticket(net.browser(), victim)
        owner = net.browser()
        reset = await owner.post(
            f"{AUTH}/password/reset",
            json={"token": await _reset_token(db_session, victim), "new_password": NEW_PASSWORD},
        )
        assert reset.status_code == 200, reset.text

        assert (await _verify(owner, stale, await _right_code(secret))).status_code == 401
        fresh = await _ticket(owner, victim, NEW_PASSWORD)
        assert (await _verify(owner, fresh, await _right_code(secret))).status_code == 200

    async def test_a_validly_signed_ticket_with_no_password_binding_is_refused(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """Attack: present a ticket of the right type, signed with the server's own key,
        that was never tied to a password (what the legacy helper mints)."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        attacker = net.browser()

        response = await _verify(attacker, create_mfa_ticket(victim.id), await _right_code(secret))

        assert response.status_code == 401
        assert _body(response) == GENERIC_MFA_FAILURE
        assert work.codes == 0
        assert await _sessions(db_session, victim.id) == 0


# ── 7. Parallel requests ─────────────────────────────────────────────────────


class _RealDatabase:
    """A pool of real connections to the test database. Everything is committed."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        self.hospital_ids: list[uuid.UUID] = []
        self.users: list[User] = []
        self.sources: set[str] = set()
        application = create_app()
        # The application's own request-scoped dependency then opens one real
        # session (one pooled connection) per request, as it does in service.
        application.state.db_session_factory = self.factory
        self.net = _Net(application)

    def source(self) -> str:
        address = _source()
        self.sources.add(address)
        return address

    async def make_user(self, **overrides: Any) -> User:
        async with self.factory() as session:
            hospital = Hospital(
                id=uuid.uuid4(),
                name="Throttle Concurrency Hospital",
                slug=f"throttle-{uuid.uuid4().hex[:12]}",
                address={"line1": "1 Test Road", "city": "Hyderabad", "country": "IN"},
                settings={},
            )
            session.add(hospital)
            await session.flush()
            self.hospital_ids.append(hospital.id)
            user = _new_user(hospital.id, **overrides)
            session.add(user)
            await session.commit()
        self.users.append(user)
        return user

    async def cleanup(self) -> None:
        """Delete every row this test committed: by key, by user, by hospital."""
        await self.net.aclose()
        user_ids = [user.id for user in self.users]
        async with self.factory() as session:
            keys: set[str] = set()
            for user in self.users:
                keys.add(bucket(BucketKind.PW_ACCOUNT, user.email).key_hash)
                keys.add(bucket(BucketKind.MFA_ACCOUNT, user.id).key_hash)
                for source in self.sources:
                    keys.add(bucket(BucketKind.PW_PAIR, user.email, source).key_hash)
                    keys.add(bucket(BucketKind.MFA_ORIGIN, user.id, "source", source).key_hash)
            for source in self.sources:
                keys.add(bucket(BucketKind.PW_SOURCE, source).key_hash)
            if user_ids:
                devices = await session.execute(
                    select(TrustedDevice.user_id, TrustedDevice.id).where(
                        TrustedDevice.user_id.in_(user_ids)
                    )
                )
                for owner_id, device_id in devices.all():
                    keys.add(bucket(BucketKind.PW_DEVICE, device_id).key_hash)
                    keys.add(bucket(BucketKind.PW_DEVICE_CAP, device_id).key_hash)
                    keys.add(bucket(BucketKind.MFA_DEVICE_CAP, device_id).key_hash)
                    keys.add(bucket(BucketKind.MFA_ORIGIN, owner_id, "device", device_id).key_hash)
            if keys:
                await session.execute(
                    delete(AuthThrottleBucket).where(AuthThrottleBucket.key_hash.in_(keys))
                )
            if self.hospital_ids:
                await session.execute(
                    delete(AuditLog).where(AuditLog.hospital_id.in_(self.hospital_ids))
                )
            if user_ids:
                # Sessions, reset tokens and trusted devices go with the user (CASCADE).
                await session.execute(delete(User).where(User.id.in_(user_ids)))
            if self.hospital_ids:
                await session.execute(delete(Hospital).where(Hospital.id.in_(self.hospital_ids)))
            await session.commit()

    async def count(self, model: Any, *criteria: Any) -> int:
        async with self.factory() as session:
            result = await session.execute(select(func.count()).select_from(model).where(*criteria))
            return int(result.scalar_one())

    async def backoff(self, target: Bucket) -> tuple[int, datetime | None]:
        """A backoff bucket's ``(failures, blocked_until)``, as committed."""
        async with self.factory() as session:
            return await _backoff(session, target)

    async def spent(self, target: Bucket) -> int:
        """Units of a budget bucket in use, as committed."""
        async with self.factory() as session:
            return await _spent(session, target)


@pytest_asyncio.fixture
async def real_db(db_engine: AsyncEngine) -> AsyncGenerator[_RealDatabase]:
    """Independent connections with real commits; cleaned up whatever happens."""
    engine = create_async_engine(db_engine.url, pool_size=6, max_overflow=0, pool_timeout=120)
    database = _RealDatabase(engine)
    try:
        yield database
    finally:
        try:
            await database.cleanup()
        finally:
            await engine.dispose()


class _SettleGate:
    """Makes "at the same moment" exact, for refunds.

    Holds every watched settlement back until ``expected`` attempts have all
    been admitted and verified, then lets them through one at a time in the
    order they were charged — so the attempt whose charge started the wait
    gives its charge back last. That is the order in which a refund that only
    undoes its own charge leaves a wait behind with nobody left to have earned
    it. Nothing of the throttle is replaced: only when ``settle`` runs.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, kind: BucketKind, expected: int) -> None:
        self.kind = kind
        self.expected = expected
        self.armed = False
        self.order: list[int] = []
        self._arrived: list[int] = []
        self._condition = asyncio.Condition()
        real_settle = AuthThrottle.settle
        gate = self

        async def _settle(
            throttle: AuthThrottle, admission: Admission, *, clear: Sequence[Bucket] = ()
        ) -> None:
            rank = gate._rank(admission)
            if not gate.armed or rank is None:
                await real_settle(throttle, admission, clear=clear)
                return
            async with gate._condition:
                gate._arrived.append(rank)
                gate._condition.notify_all()
                await asyncio.wait_for(
                    gate._condition.wait_for(lambda: gate._is_next(rank)), timeout=60
                )
            try:
                await real_settle(throttle, admission, clear=clear)
            finally:
                async with gate._condition:
                    gate.order.append(rank)
                    gate._condition.notify_all()

        monkeypatch.setattr(AuthThrottle, "settle", _settle)

    def _rank(self, admission: Admission) -> int | None:
        """How many failures the watched bucket held once this attempt was charged."""
        for charge in admission.charges:
            if charge.bucket.kind is self.kind:
                return charge.failures_after
        return None

    def _is_next(self, rank: int) -> bool:
        if len(self._arrived) < self.expected:
            return False
        return rank == min(r for r in self._arrived if r not in self.order)


class TestConcurrentAttempts:
    """Requests that arrive at the same moment, each on its own connection.

    The single-connection ``db_session`` used elsewhere cannot show this: on it
    two requests can never be inside the throttle at once, and a ``FOR UPDATE``
    never has anyone to wait for.
    """

    async def _failed_evaluations(self, real_db: _RealDatabase, user: User) -> int:
        return await real_db.count(
            AuditLog,
            AuditLog.target_id == user.id,
            AuditLog.action == "auth.login.failed",
            AuditLog.context["reason"].astext == "invalid_password",
        )

    async def test_parallel_guesses_from_one_source_are_not_evaluated_beyond_the_allowance(
        self, real_db: _RealDatabase, work: _Work
    ) -> None:
        """Attack: fire thirty guesses at once so they all pass the check before any is counted."""
        victim = await real_db.make_user()
        source = real_db.source()
        attackers = [real_db.net.browser(source) for _ in range(30)]

        responses = await asyncio.gather(
            *(_login(attacker, victim.email, WRONG_PASSWORD) for attacker in attackers)
        )

        assert [r.status_code for r in responses] == [401] * 30
        assert all(_body(r) == GENERIC_LOGIN_FAILURE for r in responses)
        assert work.real <= PAIR_FREE
        assert work.real == PAIR_FREE  # and not fewer: every free attempt was usable
        assert await self._failed_evaluations(real_db, victim) == PAIR_FREE
        assert await real_db.count(RefreshToken, RefreshToken.user_id == victim.id) == 0

    async def test_parallel_guesses_from_many_sources_are_not_evaluated_beyond_the_budget(
        self, real_db: _RealDatabase, work: _Work
    ) -> None:
        """Attack: the same race against the account's shared budget, from forty addresses."""
        victim = await real_db.make_user()
        attackers = [real_db.net.browser(real_db.source()) for _ in range(40)]

        responses = await asyncio.gather(
            *(_login(attacker, victim.email, WRONG_PASSWORD) for attacker in attackers)
        )

        assert [r.status_code for r in responses] == [401] * 40
        assert work.real <= ACCOUNT_BURST
        assert work.real == ACCOUNT_BURST
        assert await self._failed_evaluations(real_db, victim) == ACCOUNT_BURST

    async def test_a_second_parallel_wave_finds_the_first_one_counted(
        self, real_db: _RealDatabase, work: _Work
    ) -> None:
        """Attack: repeat the burst. The first wave's charges are durable; nothing is left."""
        victim = await real_db.make_user()
        source = real_db.source()
        for _ in range(2):
            await asyncio.gather(
                *(
                    _login(real_db.net.browser(source), victim.email, WRONG_PASSWORD)
                    for _ in range(15)
                )
            )

        assert work.real == PAIR_FREE
        # The right password, in parallel, during the wait: none is evaluated.
        responses = await asyncio.gather(
            *(_login(real_db.net.browser(source), victim.email, PASSWORD) for _ in range(10))
        )
        assert [r.status_code for r in responses] == [401] * 10
        assert work.real == PAIR_FREE
        assert await real_db.count(RefreshToken, RefreshToken.user_id == victim.id) == 0

    async def test_parallel_code_guesses_are_not_evaluated_beyond_the_allowance(
        self, real_db: _RealDatabase, work: _Work
    ) -> None:
        """Attack: one ticket, twenty-five codes at once from one address."""
        secret = generate_totp_secret()
        victim = await real_db.make_user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(secret))
        source = real_db.source()
        ticket = await _ticket(real_db.net.browser(source), victim)

        responses = await asyncio.gather(
            *(_verify(real_db.net.browser(source), ticket, _wrong_code(secret)) for _ in range(25))
        )

        assert [r.status_code for r in responses] == [401] * 25
        assert work.codes == MFA_FREE
        assert await real_db.count(RefreshToken, RefreshToken.user_id == victim.id) == 0

    async def test_parallel_code_guesses_from_many_sources_stop_at_the_account_budget(
        self, real_db: _RealDatabase, work: _Work
    ) -> None:
        secret = generate_totp_secret()
        victim = await real_db.make_user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(secret))
        ticket = await _ticket(real_db.net.browser(real_db.source()), victim)

        responses = await asyncio.gather(
            *(
                _verify(real_db.net.browser(real_db.source()), ticket, _wrong_code(secret))
                for _ in range(25)
            )
        )

        assert [r.status_code for r in responses] == [401] * 25
        assert work.codes == MFA_ACCOUNT_BURST

    async def test_one_reset_token_redeemed_in_parallel_succeeds_exactly_once(
        self, real_db: _RealDatabase
    ) -> None:
        """Attack: replay an intercepted reset link alongside the owner, many times at once."""
        victim = await real_db.make_user()
        raw, token_hash = generate_opaque_token()
        async with real_db.factory() as session:
            session.add(
                PasswordResetToken(
                    id=uuid.uuid4(),
                    user_id=victim.id,
                    token_hash=token_hash,
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                )
            )
            await session.commit()
        candidates = [f"Parallel!Passw0rd-{index:02d}" for index in range(16)]

        responses = await asyncio.gather(
            *(
                real_db.net.browser(real_db.source()).post(
                    f"{AUTH}/password/reset", json={"token": raw, "new_password": candidate}
                )
                for candidate in candidates
            )
        )

        statuses = [r.status_code for r in responses]
        assert statuses.count(200) == 1, statuses
        assert statuses.count(401) == len(candidates) - 1, statuses
        winner = candidates[statuses.index(200)]
        async with real_db.factory() as session:
            stored = (
                await session.execute(select(User.password_hash).where(User.id == victim.id))
            ).scalar_one()
        # The password is the one from the request that was told it had won.
        assert [c for c in candidates if verify_password(c, stored)] == [winner]
        assert not verify_password(PASSWORD, stored)
        assert (
            await real_db.count(
                AuditLog, AuditLog.target_id == victim.id, AuditLog.action == "auth.password.reset"
            )
            == 1
        )
        # Exactly one browser was made a trusted device by it.
        assert await real_db.count(TrustedDevice, TrustedDevice.user_id == victim.id) == 1
        # And the token is spent for good.
        late = await real_db.net.browser(real_db.source()).post(
            f"{AUTH}/password/reset", json={"token": raw, "new_password": "Late!Passw0rd-999"}
        )
        assert late.status_code == 401

    async def test_two_simultaneous_correct_sign_ins_leave_no_wait_behind(
        self, real_db: _RealDatabase, work: _Work, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner mistypes three times, then submits the right password twice at once
        (a double click, two tabs) from a browser with no cookie.

        Both are charged before either is given back — the second charge is
        the fifth and starts a wait. With both refunded, three failures are
        counted: no wait may remain, or the owner has locked themselves out
        with a correct password. And nothing may have been forgotten either.
        """
        victim = await real_db.make_user()
        source = real_db.source()
        pair = bucket(BucketKind.PW_PAIR, victim.email, source)
        for _ in range(3):
            response = await _login(real_db.net.browser(source), victim.email, WRONG_PASSWORD)
            assert response.status_code == 401
        assert await real_db.backoff(pair) == (3, None)
        gate = _SettleGate(monkeypatch, BucketKind.PW_PAIR, expected=2)

        gate.armed = True
        responses = await asyncio.gather(
            *(_login(real_db.net.browser(source), victim.email) for _ in range(2))
        )
        gate.armed = False

        assert [r.status_code for r in responses] == [200, 200]
        # Both really were in flight together: charged fourth and fifth, and
        # the one that started the wait was given back last.
        assert gate.order == [4, 5]
        assert await real_db.backoff(pair) == (3, None)
        assert await real_db.spent(bucket(BucketKind.PW_ACCOUNT, victim.email)) == 3
        # So the very next correct sign-in from there is evaluated and succeeds ...
        again = await _login(real_db.net.browser(source), victim.email)
        assert again.status_code == 200, again.text
        assert await real_db.count(RefreshToken, RefreshToken.user_id == victim.id) == 3
        # ... and the three real failures still count: two more guesses, then the wait.
        evaluated = work.real
        for _ in range(4):
            response = await _login(real_db.net.browser(source), victim.email, WRONG_PASSWORD)
            assert response.status_code == 401
        assert work.real == evaluated + (PAIR_FREE - 3)
        assert (await _login(real_db.net.browser(source), victim.email)).status_code == 401
        assert work.real == evaluated + (PAIR_FREE - 3)

    async def test_simultaneous_correct_codes_after_a_failure_leave_no_unearned_wait(
        self, real_db: _RealDatabase, work: _Work, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One wrong code, then the right code four times at once from the same address.

        The four are charged second to fifth; the fifth starts a wait. All
        four are right, so one failure is counted and there is nothing to
        wait for — but that one failure is not forgotten, and the shared
        budget gets back four units and not five.
        """
        secret = generate_totp_secret()
        victim = await real_db.make_user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(secret))
        source = real_db.source()
        origin = bucket(BucketKind.MFA_ORIGIN, victim.id, "source", source)
        shared = bucket(BucketKind.MFA_ACCOUNT, victim.id)
        ticket = await _ticket(real_db.net.browser(source), victim)
        wrong = await _verify(real_db.net.browser(source), ticket, _wrong_code(secret))
        assert wrong.status_code == 401
        assert await real_db.backoff(origin) == (1, None)
        simultaneous = MFA_FREE - 1
        gate = _SettleGate(monkeypatch, BucketKind.MFA_ORIGIN, expected=simultaneous)
        code = await _right_code(secret)

        gate.armed = True
        responses = await asyncio.gather(
            *(_verify(real_db.net.browser(source), ticket, code) for _ in range(simultaneous))
        )
        gate.armed = False

        assert [r.status_code for r in responses] == [200] * simultaneous
        assert gate.order == [2, 3, 4, 5]
        assert await real_db.backoff(origin) == (1, None)
        assert await real_db.spent(shared) == 1
        assert await real_db.count(RefreshToken, RefreshToken.user_id == victim.id) == simultaneous
        # The next right code from there is looked at straight away ...
        evaluated = work.codes
        again = await _verify(real_db.net.browser(source), ticket, await _right_code(secret))
        assert again.status_code == 200, again.text
        assert work.codes == evaluated + 1
        # ... and a guesser at that address has four codes left, not five.
        evaluated = work.codes
        for _ in range(MFA_FREE + 2):
            response = await _verify(real_db.net.browser(source), ticket, _wrong_code(secret))
            assert response.status_code == 401
        assert work.codes == evaluated + (MFA_FREE - 1)
        refused = await _verify(real_db.net.browser(source), ticket, await _right_code(secret))
        assert refused.status_code == 401
        assert work.codes == evaluated + (MFA_FREE - 1)


# ── 8. What a success clears ─────────────────────────────────────────────────


class TestSuccessClearsOnlyItsOwnState:
    async def test_a_trusted_devices_backoff_is_cleared_by_its_own_success(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work
    ) -> None:
        """The owner mistypes four times and then gets it right: a clean slate, on that device."""
        device = await _trusted_browser(net, user.email)
        record = await _only_device(db_session, user)
        backoff = bucket(BucketKind.PW_DEVICE, record.id)
        evaluated = work.real

        for _ in range(DEVICE_FREE - 1):
            assert (await _login(device, user.email, WRONG_PASSWORD)).status_code == 401
        assert (await _login(device, user.email)).status_code == 200
        row = await _row(db_session, backoff)
        assert row is not None
        assert (row.failures, row.blocked_until, row.last_charged_at) == (0, None, None)

        # So four more mistakes are again evaluated without a wait.
        for _ in range(DEVICE_FREE - 1):
            assert (await _login(device, user.email, WRONG_PASSWORD)).status_code == 401
        assert work.real == evaluated + 2 * (DEVICE_FREE - 1) + 1
        assert (await _login(device, user.email)).status_code == 200

    async def test_a_devices_ceiling_is_not_reset_by_its_own_success(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work
    ) -> None:
        """Attack: with a stolen cookie *and* the password, sign in between guesses for ever.

        (An attacker who has the password does not need to guess it; the
        ceiling is what stops success-interleaving from making the device
        bucket unlimited for whoever holds the cookie.)
        """
        device = await _trusted_browser(net, user.email)
        record = await _only_device(db_session, user)
        ceiling = bucket(BucketKind.PW_DEVICE_CAP, record.id)
        start = work.real

        # Two rounds of "four guesses, then sign in": eight guesses, no wait at all.
        for _ in range(2):
            for _ in range(DEVICE_FREE - 1):
                assert (await _login(device, user.email, WRONG_PASSWORD)).status_code == 401
            assert (await _login(device, user.email)).status_code == 200
        assert work.real == start + 2 * DEVICE_FREE
        assert await _spent(db_session, ceiling) == DEVICE_CAP_BURST - 2
        # Two more guesses reach the ceiling.
        for _ in range(2):
            assert (await _login(device, user.email, WRONG_PASSWORD)).status_code == 401
        assert await _spent(db_session, ceiling) == DEVICE_CAP_BURST
        evaluated = work.real

        # The backoff is nowhere near a block, yet the ceiling refuses — a
        # further guess and the right password alike, unevaluated.
        row = await _row(db_session, bucket(BucketKind.PW_DEVICE, record.id))
        assert row is not None
        assert (row.failures, row.blocked_until) == (2, None)
        assert (await _login(device, user.email, WRONG_PASSWORD)).status_code == 401
        assert (await _login(device, user.email)).status_code == 401
        assert work.real == evaluated

    async def test_nobodys_success_refills_the_accounts_shared_budget(
        self,
        net: _Net,
        db_session: AsyncSession,
        user: User,
        hospital_id: uuid.UUID,
        work: _Work,
    ) -> None:
        """Attack: wait for the owner (or anyone) to sign in, then resume guessing."""
        owner_device = await _trusted_browser(net, user.email)
        colleague = await _make_user(db_session, hospital_id)
        account = bucket(BucketKind.PW_ACCOUNT, user.email)
        for _ in range(ACCOUNT_BURST - 2):
            assert (await _login(net.browser(), user.email, WRONG_PASSWORD)).status_code == 401
        assert await _spent(db_session, account) == ACCOUNT_BURST - 2
        evaluated = work.real

        # The owner signs in from the trusted device and from two new addresses;
        # a colleague signs in too.
        assert (await _login(owner_device, user.email)).status_code == 200
        assert (await _login(net.browser(), user.email)).status_code == 200
        assert (await _login(net.browser(), user.email)).status_code == 200
        assert (await _login(net.browser(), colleague.email)).status_code == 200
        successes = work.real - evaluated
        assert successes == 4

        # Each success gave back its own unit and nothing more.
        assert await _spent(db_session, account) == ACCOUNT_BURST - 2
        for _ in range(10):
            assert (await _login(net.browser(), user.email, WRONG_PASSWORD)).status_code == 401
        assert work.real == evaluated + successes + 2

    async def test_an_unrecognised_success_gives_a_co_located_attacker_nothing(
        self, net: _Net, db_session: AsyncSession, user: User, work: _Work
    ) -> None:
        """Attack: guess from the owner's address and let the owner's sign-ins reset the count.

        The attacker stops one short of the block; the owner then signs in
        from the same address on browsers with no cookie.
        """
        shared_address = _source()
        attacker = net.browser(shared_address)
        pair = bucket(BucketKind.PW_PAIR, user.email, shared_address)
        for _ in range(PAIR_FREE - 1):
            assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401
        before = await _snapshot(db_session, pair)

        for _ in range(3):
            response = await _login(net.browser(shared_address), user.email)
            assert response.status_code == 200, response.text

        # The pair's backoff is exactly where the attacker left it.
        assert await _snapshot(db_session, pair) == before
        evaluated = work.real
        for _ in range(6):
            assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401
        assert work.real == evaluated + 1  # the one free attempt that was left
        assert (await _login(attacker, user.email, PASSWORD)).status_code == 401

    async def test_a_success_elsewhere_does_not_lift_an_attackers_block(
        self, net: _Net, user: User, work: _Work
    ) -> None:
        """Attack: be blocked, then wait for the owner to sign in successfully somewhere."""
        attacker = await _exhaust_pair(net, user.email, _source())
        owner_device = await _trusted_browser(net, user.email)
        assert (await _login(owner_device, user.email)).status_code == 200
        evaluated = work.real

        assert (await _login(attacker, user.email, PASSWORD)).status_code == 401
        assert work.real == evaluated

    async def test_an_unrecognised_mfa_success_gives_a_co_located_attacker_no_fresh_codes(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """Attack: with the password, guess codes from the owner's address (one hospital,
        one NAT), stop one short of the wait, and let the owner's sign-ins — on browsers
        the server does not recognise — reset the count for that address."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        shared_address = _source()
        origin = bucket(BucketKind.MFA_ORIGIN, victim.id, "source", shared_address)
        attacker = net.browser(shared_address)
        ticket = await _ticket(attacker, victim)
        for _ in range(MFA_FREE - 1):
            assert (await _verify(attacker, ticket, _wrong_code(secret))).status_code == 401
        before = await _snapshot(db_session, origin)
        assert before is not None
        assert before[:2] == (MFA_FREE - 1, None)

        owners: list[AsyncClient] = []
        for _ in range(3):
            owner = net.browser(shared_address)
            response = await _both_factors(owner, victim, secret)
            assert response.status_code == 200, response.text
            owners.append(owner)

        # The address's backoff is exactly where the attacker left it.
        assert await _snapshot(db_session, origin) == before
        assert await _spent(db_session, bucket(BucketKind.MFA_ACCOUNT, victim.id)) == (MFA_FREE - 1)
        evaluated = work.codes
        for _ in range(6):
            fresh = await _ticket(attacker, victim)
            assert (await _verify(attacker, fresh, _wrong_code(secret))).status_code == 401
        assert work.codes == evaluated + 1  # the one free code that was left
        refused = await _verify(
            attacker, await _ticket(attacker, victim), await _right_code(secret)
        )
        assert refused.status_code == 401
        assert work.codes == evaluated + 1
        # The owner's browsers became trusted devices at those sign-ins, and
        # are not held up by what the address has run up since.
        assert (await _both_factors(owners[0], victim, secret)).status_code == 200

    async def test_a_trusted_devices_mfa_success_clears_its_own_two_backoffs_and_no_others(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """The owner mistypes the password and the code four times each on their own
        browser, then gets both right: a clean slate on that device — and only there."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        address = _source()
        owner = await _mfa_trusted_browser(net, victim, secret, address)
        device = await _only_device(db_session, victim)
        own_password = bucket(BucketKind.PW_DEVICE, device.id)
        own_code = bucket(BucketKind.MFA_ORIGIN, victim.id, "device", device.id)
        # Somebody else, at the same address, one code short of a wait.
        neighbour = net.browser(address)
        neighbours_ticket = await _ticket(neighbour, victim)
        for _ in range(MFA_FREE - 1):
            response = await _verify(neighbour, neighbours_ticket, _wrong_code(secret))
            assert response.status_code == 401
        neighbours_origin = bucket(BucketKind.MFA_ORIGIN, victim.id, "source", address)
        neighbours_before = await _snapshot(db_session, neighbours_origin)

        for _ in range(DEVICE_FREE - 1):
            assert (await _login(owner, victim.email, WRONG_PASSWORD)).status_code == 401
        ticket = await _ticket(owner, victim)
        # The right password alone gives back its own charge and clears nothing.
        assert await _backoff(db_session, own_password) == (DEVICE_FREE - 1, None)
        for _ in range(MFA_FREE - 1):
            assert (await _verify(owner, ticket, _wrong_code(secret))).status_code == 401
        assert await _backoff(db_session, own_code) == (MFA_FREE - 1, None)

        completed = await _verify(owner, ticket, await _right_code(secret))

        assert completed.status_code == 200, completed.text
        assert await _backoff(db_session, own_password) == (0, None)
        assert await _backoff(db_session, own_code) == (0, None)
        assert await _snapshot(db_session, neighbours_origin) == neighbours_before
        # So four more mistakes of each kind are again evaluated without a wait.
        passwords, codes = work.real, work.codes
        for _ in range(DEVICE_FREE - 1):
            assert (await _login(owner, victim.email, WRONG_PASSWORD)).status_code == 401
        ticket = await _ticket(owner, victim)
        for _ in range(MFA_FREE - 1):
            assert (await _verify(owner, ticket, _wrong_code(secret))).status_code == 401
        assert work.real == passwords + DEVICE_FREE  # four wrong and the right one
        assert work.codes == codes + MFA_FREE - 1
        assert (await _verify(owner, ticket, await _right_code(secret))).status_code == 200


# ── 9. Tenants ───────────────────────────────────────────────────────────────


class TestNoCrossTenantEffects:
    async def test_exhausting_everything_for_hospital_a_changes_nothing_for_hospital_b(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        work: _Work,
    ) -> None:
        """Attack: burn every counter of a hospital-A account; hospital B must not notice."""
        victim_a, secret_a = await _make_mfa_user(db_session, hospital_id)
        user_b = await _make_user(db_session, other_hospital_id)
        source_b = _source()
        buckets_b = [
            bucket(BucketKind.PW_ACCOUNT, user_b.email),
            bucket(BucketKind.PW_PAIR, user_b.email, source_b),
            bucket(BucketKind.PW_SOURCE, source_b),
            bucket(BucketKind.MFA_ACCOUNT, user_b.id),
        ]
        before = [await _snapshot(db_session, b) for b in buckets_b]

        # Codes first (they need the password step to be open), then passwords.
        for _ in range(MFA_ACCOUNT_BURST + 3):
            attacker = net.browser()
            ticket = await _ticket(attacker, victim_a)
            await _verify(attacker, ticket, _wrong_code(secret_a))
        await _exhaust_pair(net, victim_a.email, _source())
        for _ in range(ACCOUNT_BURST + 5):
            await _login(net.browser(), victim_a.email, WRONG_PASSWORD)
        assert (await _login(net.browser(), victim_a.email)).status_code == 401

        assert [await _snapshot(db_session, b) for b in buckets_b] == before
        # B has its whole allowance: every free attempt is evaluated, then B signs in.
        browser_b = net.browser(source_b)
        evaluated = work.real
        for _ in range(PAIR_FREE - 1):
            assert (await _login(browser_b, user_b.email, WRONG_PASSWORD)).status_code == 401
        assert work.real == evaluated + PAIR_FREE - 1
        assert (await _login(browser_b, user_b.email)).status_code == 200

    async def test_a_hospital_a_insider_gains_no_attempts_against_hospital_b_by_signing_in(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        work: _Work,
    ) -> None:
        """Attack: a real staff member of A guesses a B password, signing in as himself between."""
        insider = await _make_user(db_session, hospital_id)
        victim_b = await _make_user(db_session, other_hospital_id)
        address = _source()
        browser = await _trusted_browser(net, insider.email, address)
        evaluated = work.real

        for _ in range(12):
            assert (await _login(browser, victim_b.email, WRONG_PASSWORD)).status_code == 401
            # His own successful sign-in, with his own trusted-device cookie.
            assert (await _login(browser, insider.email)).status_code == 200

        own_sign_ins = 12
        assert work.real - evaluated - own_sign_ins == PAIR_FREE
        # His device cookie was not taken as a trusted device of the victim:
        # the guesses went to the victim's shared budget like anybody's.
        assert await _spent(db_session, bucket(BucketKind.PW_ACCOUNT, victim_b.email)) == PAIR_FREE
        assert await _sessions(db_session, victim_b.id) == 0
        devices_b = await db_session.execute(
            select(func.count())
            .select_from(TrustedDevice)
            .where(TrustedDevice.user_id == victim_b.id)
        )
        assert devices_b.scalar_one() == 0

    async def test_a_hospital_a_insider_cannot_deny_a_hospital_b_user(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Attack: the insider spends his whole allowance against B's user to keep them out."""
        insider = await _make_user(db_session, hospital_id)
        victim_b = await _make_user(db_session, other_hospital_id)
        victim_device = await _trusted_browser(net, victim_b.email)
        browser = await _trusted_browser(net, insider.email, _source())
        for _ in range(PAIR_FREE + 10):
            assert (await _login(browser, victim_b.email, WRONG_PASSWORD)).status_code == 401
            assert (await _login(browser, insider.email)).status_code == 200

        assert (await _login(browser, victim_b.email)).status_code == 401  # he is blocked
        assert (await _login(victim_device, victim_b.email)).status_code == 200
        assert (await _login(net.browser(), victim_b.email)).status_code == 200

    async def test_an_insiders_device_token_is_no_ones_trusted_device_but_his_own(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Attack: present a genuine device token (issued for an A account) against a B account.

        With B's shared budget exhausted, only a trusted device of B gets in.
        The insider's token must not count as one.
        """
        insider = await _make_user(db_session, hospital_id)
        victim_b = await _make_user(db_session, other_hospital_id)
        browser = await _trusted_browser(net, insider.email)
        await _exhaust_account(net, victim_b.email)

        # Suppose he even has B's password: his cookie still buys nothing.
        response = await _login(browser, victim_b.email, PASSWORD)

        assert response.status_code == 401
        assert _body(response) == GENERIC_LOGIN_FAILURE
        assert await _sessions(db_session, victim_b.id) == 0
        token = browser.cookies.get(DEVICE_COOKIE)
        assert token is not None
        owners = await db_session.execute(
            select(TrustedDevice.user_id).where(TrustedDevice.token_hash == hash_token(token))
        )
        assert owners.scalars().all() == [insider.id]


# ── 10. One answer ───────────────────────────────────────────────────────────


class TestResponsesNeverDiffer:
    @staticmethod
    def _shape(response: Response) -> tuple[int, str, tuple[str, ...]]:
        """Status, body and the set of header names — everything but per-request values."""
        return (
            response.status_code,
            str(sorted(_body(response).items())),
            tuple(sorted(response.headers.keys())),
        )

    async def test_throttled_wrong_and_unknown_are_one_response(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: read the difference between "wrong", "slow down" and "no such account"."""
        unknown = _email("nobody")
        source = _source()
        cases: dict[str, Response] = {}
        sessions_before = await _count_all(db_session, RefreshToken)
        devices_before = await _count_all(db_session, TrustedDevice)
        cases["wrong password"] = await _login(net.browser(), user.email, WRONG_PASSWORD)
        cases["unknown email"] = await _login(net.browser(), unknown, PASSWORD)
        blocked = await _exhaust_pair(net, user.email, source)
        cases["throttled by source, right password"] = await _login(blocked, user.email)
        cases["throttled by source, wrong password"] = await _login(
            blocked, user.email, WRONG_PASSWORD
        )
        blocked_unknown = await _exhaust_pair(net, unknown, _source())
        cases["throttled by source, unknown email"] = await _login(blocked_unknown, unknown)
        await _exhaust_account(net, user.email)
        cases["throttled by account, right password"] = await _login(net.browser(), user.email)
        await _exhaust_account(net, unknown)
        cases["throttled by account, unknown email"] = await _login(net.browser(), unknown)

        shapes = {name: self._shape(response) for name, response in cases.items()}
        assert len(set(shapes.values())) == 1, shapes
        for name, response in cases.items():
            assert response.status_code == 401, name
            assert _body(response) == GENERIC_LOGIN_FAILURE, name
            assert "set-cookie" not in response.headers, name
            assert "retry-after" not in response.headers, name
            assert not response.cookies, name
        assert await _sessions(db_session, user.id) == 0
        # Nobody at all got a session or became a trusted device.
        assert await _count_all(db_session, RefreshToken) == sessions_before
        assert await _count_all(db_session, TrustedDevice) == devices_before

    async def test_a_refusal_does_not_touch_a_cookie_the_browser_already_holds(
        self, net: _Net, user: User
    ) -> None:
        """Attack: learn from a cleared or rewritten cookie that the address is an account."""
        browser = await _trusted_browser(net, user.email)
        held = browser.cookies.get(DEVICE_COOKIE)
        for _ in range(DEVICE_FREE + 2):  # wrong, then throttled
            response = await _login(browser, user.email, WRONG_PASSWORD)
            assert response.status_code == 401
            assert "set-cookie" not in response.headers
        unknown = await _login(browser, _email("nobody"), PASSWORD)
        assert "set-cookie" not in unknown.headers

        assert browser.cookies.get(DEVICE_COOKIE) == held

    async def test_an_unknown_email_is_throttled_exactly_like_a_real_one_per_source(
        self, net: _Net, user: User, work: _Work, clock: _Clock
    ) -> None:
        """Attack: tell real from unknown by how many guesses are evaluated before refusal."""
        counts: dict[str, list[int]] = {}
        for label, address in (("real", user.email), ("unknown", _email("nobody"))):
            attacker = net.browser(_source())
            start = work.passwords
            for _ in range(PAIR_FREE + 6):
                assert (await _login(attacker, address, WRONG_PASSWORD)).status_code == 401
            first = work.passwords - start
            clock.advance(seconds=PAIR_WAITS[0] + 1)
            for _ in range(4):
                assert (await _login(attacker, address, WRONG_PASSWORD)).status_code == 401
            second = work.passwords - start
            clock.advance(seconds=PAIR_WAITS[1] - 20)
            assert (await _login(attacker, address, WRONG_PASSWORD)).status_code == 401
            counts[label] = [first, second, work.passwords - start]

        assert counts["real"] == counts["unknown"] == [PAIR_FREE, PAIR_FREE + 1, PAIR_FREE + 1]
        # One kind of check each, never both and never neither.
        assert work.real == work.dummy == PAIR_FREE + 1

    async def test_an_unknown_email_is_throttled_exactly_like_a_real_one_across_sources(
        self, net: _Net, user: User, work: _Work
    ) -> None:
        counts: dict[str, int] = {}
        for label, address in (("real", user.email), ("unknown", _email("nobody"))):
            start = work.passwords
            for _ in range(ACCOUNT_BURST + 15):
                assert (await _login(net.browser(), address, WRONG_PASSWORD)).status_code == 401
            counts[label] = work.passwords - start

        assert counts == {"real": ACCOUNT_BURST, "unknown": ACCOUNT_BURST}

    async def test_every_mfa_refusal_is_one_response(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        browser = net.browser(_source())
        ticket = await _ticket(browser, victim)
        wrong = await _verify(browser, ticket, _wrong_code(secret))
        for _ in range(MFA_FREE - 1):
            await _verify(browser, ticket, _wrong_code(secret))
        throttled_right = await _verify(browser, ticket, await _right_code(secret))
        throttled_wrong = await _verify(browser, ticket, _wrong_code(secret))

        shapes = {self._shape(r) for r in (wrong, throttled_right, throttled_wrong)}
        assert len(shapes) == 1, shapes
        for response in (wrong, throttled_right, throttled_wrong):
            assert response.status_code == 401
            assert _body(response) == GENERIC_MFA_FAILURE
            assert "set-cookie" not in response.headers
        assert await _sessions(db_session, victim.id) == 0
