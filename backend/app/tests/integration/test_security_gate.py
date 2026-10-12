"""The security gate: attacks the other suites left unproven.

Staff sign-in hardening is attacked in four neighbouring files
(``test_auth_throttle_attacks``, ``test_trusted_devices``,
``test_staff_auth_hardening``, ``test_invitation_delivery``). This file holds
only what those do not already prove, each replayed through real HTTP against
the application and a real PostgreSQL:

- one source spraying many accounts, up to the source's own budget;
- forged ``X-Forwarded-For`` headers used to *frame* somebody else's address,
  in every proxy configuration;
- storms of invitation resends and activations on separate connections;
- a second-factor guesser racing the owner's own verified device;
- an activation or reset link leaking through a refusal, an audit record, a
  log line, a database error or a failing mail queue;
- the forgot-password cap, the reactivation guard and the audit entries that
  say an allowance ran out or a browser became trusted.

**How the source address is varied.** As in ``test_auth_throttle_attacks``:
``RATE_LIMIT_TRUST_PROXY_HEADER`` is on and the ASGI transport's socket peer
is ``127.0.0.1``, a proxy we operate, so ``X-Forwarded-For`` is resolved by
the real ``app.core.client_ip``. The tests about spoofing change exactly that.

**How time is moved.** Only through the throttle's clock
(``AuthThrottleRepository.now``). Where a test counts units of a budget the
clock is *stopped*, so a slow machine cannot refill one behind its back.
Nothing sleeps for throttle time.

**Real concurrency.** The classes named ``...InParallel`` use a pool of
independent connections with real commits, and delete every row they wrote in
``finally``. Everything else runs on the suite's rolled-back session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
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
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

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
    verify_password,
)
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.auth_throttle import AuthThrottleBucket, TrustedDevice
from app.models.hospital import Hospital
from app.models.notification import Notification, NotificationDelivery
from app.models.password_reset_token import PasswordResetToken
from app.models.permission import Permission
from app.models.refresh_token import RefreshToken
from app.models.role import Role
from app.models.user import User, UserStatus
from app.repositories.auth_throttle_repository import AuthThrottleRepository
from app.services import auth_service as auth_service_module
from app.services.auth_service import AuthService
from app.services.auth_throttle import Bucket, BucketKind, Budget, bucket
from app.services.notification_service import NotificationService
from app.tests.conftest import every_log_line_reaches_the_root, grant_permissions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.core.notifications import NotificationRequest

pytestmark = pytest.mark.database

PASSWORD = "Str0ng!Passw0rd123"  # noqa: S105 — a test credential, not a real one
WRONG_PASSWORD = "Wr0ng!Passw0rd999"  # noqa: S105
NEW_PASSWORD = "An0ther!Passw0rd456"  # noqa: S105
AUTH = "/api/v1/auth"
USERS = "/api/v1/users"
NOTIFICATIONS = "/api/v1/notifications"
AUDIT = "/api/v1/audit-logs"
GENERIC_LOGIN_FAILURE = {
    "success": False,
    "message": "Invalid credentials.",
    "error_code": "AUTHENTICATION_REQUIRED",
    "errors": None,
}
GENERIC_MFA_FAILURE = {**GENERIC_LOGIN_FAILURE, "message": "Invalid MFA code."}
GENERIC_SERVER_ERROR = {
    "success": False,
    "message": "An unexpected error occurred.",
    "error_code": "INTERNAL_ERROR",
    "errors": None,
}
DEVICE_COOKIE = "aetheris-device"

# The documented numbers (POLICIES of app/services/auth_throttle.py and
# _RESET_EMAILS_PER_TOKEN_LIFETIME of app/services/auth_service.py), restated
# on purpose: if somebody loosens the policy these tests must fail, not follow.
SOURCE_BURST = 300
SOURCE_REFILL = timedelta(seconds=15)
PAIR_FREE = 5
MFA_ACCOUNT_BURST = 10
RESEND_BURST = 3
RESET_LINKS_AT_ONCE = 3

#: The socket peer of an ordinary test request: a proxy we "operate".
_PROXY_PEER = ("127.0.0.1", 40000)

#: Everything an administrator may do to, or read about, an account.
ADMIN = [
    "user.read",
    "user.create",
    "user.update",
    "user.deactivate",
    "user.reset_password",
    "role.assign",
    "audit.read",
    "audit.export",
    "notification.read.own",
    "notification.preference.update.own",
]
OWN_NOTIFICATIONS = ["notification.read.own", "notification.preference.update.own"]

_LINK = re.compile(r"/reset-password#token=([A-Za-z0-9_\-]+)")


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
        "first_name": "Gate",
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


async def _make_staff(session: AsyncSession, hospital_id: uuid.UUID, codes: list[str]) -> User:
    """An active account holding exactly ``codes``."""
    staff = await _make_user(session, hospital_id, email=_email("admin"))
    await grant_permissions(session, hospital_id=hospital_id, user_id=staff.id, codes=codes)
    await session.commit()
    return staff


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

    If the authenticator's 30-second step is about to end, wait for the next
    one so the request cannot straddle the boundary. (This is the
    authenticator's clock, not the throttle's.)
    """
    totp = pyotp.TOTP(secret)
    left = totp.interval - time.time() % totp.interval
    if left < 1.5:
        await asyncio.sleep(left + 0.05)
    return totp.now()


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(user_id=user.id, hospital_id=user.hospital_id)
    return {"Authorization": f"Bearer {token}"}


async def _login(
    client: AsyncClient, email: str, password: str = PASSWORD, *, forwarded: str | None = None
) -> Response:
    """POST /auth/login. ``forwarded`` replaces the client's ``X-Forwarded-For`` once."""
    headers = {"X-Forwarded-For": forwarded} if forwarded is not None else None
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


async def _forgot(client: AsyncClient, email: str) -> Response:
    return await client.post(f"{AUTH}/password/forgot", json={"email": email})


async def _redeem(client: AsyncClient, token: str, password: str = NEW_PASSWORD) -> Response:
    return await client.post(
        f"{AUTH}/password/reset", json={"token": token, "new_password": password}
    )


async def _invite(client: AsyncClient, headers: dict[str, str], email: str) -> Response:
    return await client.post(
        USERS,
        json={"email": email, "first_name": "Nisha", "last_name": "Nair"},
        headers=headers,
    )


async def _resend(client: AsyncClient, headers: dict[str, str], user_id: uuid.UUID) -> Response:
    return await client.post(f"{USERS}/{user_id}/invitation", headers=headers)


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


async def _backoff(session: AsyncSession, target: Bucket) -> tuple[int, bool]:
    """A backoff bucket's ``(failures, is a wait running)``; a missing row holds nothing."""
    row = await _row(session, target)
    return (0, False) if row is None else (row.failures, row.blocked_until is not None)


async def _count(session: AsyncSession, model: Any, *criteria: Any) -> int:
    result = await session.execute(select(func.count()).select_from(model).where(*criteria))
    return int(result.scalar_one())


async def _sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    return await _count(session, RefreshToken, RefreshToken.user_id == user_id)


async def _device_hashes(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    rows = await session.execute(
        select(TrustedDevice.token_hash).where(TrustedDevice.user_id == user_id)
    )
    return set(rows.scalars().all())


async def _audit_rows(session: AsyncSession, *criteria: Any) -> list[AuditLog]:
    rows = await session.execute(
        select(AuditLog).where(*criteria).execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def _failed_attempts(session: AsyncSession, user_id: uuid.UUID) -> list[AuditLog]:
    return await _audit_rows(
        session, AuditLog.target_id == user_id, AuditLog.action == "auth.login.failed"
    )


def _exhausted(entries: list[AuditLog]) -> list[list[str]]:
    """The ``budget_exhausted`` note of every entry that carries one."""
    return [
        list(entry.context["budget_exhausted"])
        for entry in entries
        if entry.context and entry.context.get("budget_exhausted")
    ]


async def _emailed_tokens(session: AsyncSession, address: str) -> set[str]:
    """Every link token in an email queued for ``address``: the mailbox owner's view."""
    rows = await session.execute(
        select(NotificationDelivery.body).where(NotificationDelivery.to_address == address)
    )
    found: set[str] = set()
    for body in rows.scalars().all():
        found.update(_LINK.findall(body or ""))
    return found


async def _emailed_token(session: AsyncSession, address: str) -> str:
    tokens = await _emailed_tokens(session, address)
    assert len(tokens) == 1, f"expected exactly one emailed link, found {len(tokens)}"
    return next(iter(tokens))


async def _live_token_hashes(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """The stored hashes of a user's tokens that could still be redeemed."""
    rows = await session.execute(
        select(PasswordResetToken.token_hash).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > func.now(),
        )
    )
    return set(rows.scalars().all())


async def _plant_token(session: AsyncSession, user_id: uuid.UUID, *, dead: bool = False) -> str:
    """Write a reset token for an account straight to the table; return the raw token.

    :param dead: The token has already been used (redeemed or withdrawn).
    """
    raw, token_hash = generate_opaque_token()
    session.add(
        PasswordResetToken(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            used_at=datetime.now(UTC) if dead else None,
        )
    )
    await session.flush()
    await session.commit()
    return raw


def _everything_sent(response: Response) -> str:
    """The whole response as the caller receives it: status line, headers, body."""
    headers = "\n".join(f"{name}: {value}" for name, value in response.headers.multi_items())
    return f"{response.status_code}\n{headers}\n\n{response.text}"


def _assert_carries_no_link(response: Response, *tokens: str) -> None:
    """The response gives away no token, nothing derived from one, and no link."""
    sent = _everything_sent(response)
    for token in tokens:
        assert token not in sent, f"a raw link token is in the {response.status_code} response"
        assert hash_token(token) not in sent, "a stored token hash is in the response"
    assert "token=" not in sent, "a link is in the response"
    assert "reset-password" not in sent, "a link is in the response"


def _assert_never_written(text: str, where: str, *tokens: str) -> None:
    for token in tokens:
        assert token not in text, f"a raw link token is in {where}"
        assert hash_token(token) not in text, f"a token hash is in {where}"
    assert "token=" not in text, f"a link is in {where}"
    assert "reset-password#" not in text, f"a link is in {where}"


def _wire_shape(response: Response) -> tuple[int, str, tuple[str, ...]]:
    """What a caller can compare between two answers: status, body, header names."""
    return (
        response.status_code,
        json.dumps(_body(response), sort_keys=True),
        tuple(sorted(name.lower() for name in response.headers)),
    )


class _Clock:
    """The throttle's clock, stopped when the test first reads it and moved only by the test.

    Stopped rather than merely offset: a test that counts units of a budget
    refilling every fifteen seconds must not depend on how long it takes.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.offset = timedelta(0)
        self._started: datetime | None = None
        real_now = AuthThrottleRepository.now
        clock = self

        async def _now(repository: AuthThrottleRepository) -> datetime:
            if clock._started is None:
                clock._started = await real_now(repository)
            return clock._started + clock.offset

        monkeypatch.setattr(AuthThrottleRepository, "now", _now)

    def advance(self, **delta: float) -> None:
        self.offset += timedelta(**delta)


class _Work:
    """Counts credential evaluations. Hashing runs in a thread pool, hence the lock.

    ``dummy`` counts the verification burnt for an address that names no
    account. It is counted but not performed: several tests here send hundreds
    of those, and what they assert is how many were *allowed*, not how long
    one takes (``test_staff_auth_hardening`` proves the burn is a real one).
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._lock = threading.Lock()
        self.real = 0
        self.dummy = 0
        self.codes = 0
        real_verify = security_module.verify_password
        real_totp = security_module.verify_totp_code

        def _verify(password: str, hashed: str) -> bool:
            with self._lock:
                self.real += 1
            return real_verify(password, hashed)

        def _burn(password: str) -> None:
            with self._lock:
                self.dummy += 1

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
        self.application = application
        self._clients: list[AsyncClient] = []

    def browser(
        self, forwarded: str | None = None, *, peer: tuple[str, int] = _PROXY_PEER
    ) -> AsyncClient:
        """A new browser with an empty cookie jar.

        :param forwarded: The ``X-Forwarded-For`` it sends (a fresh public
            address when omitted): behind our proxy, the address it is seen at.
        :param peer: The socket peer the application sees.
        """
        client = AsyncClient(
            # An unexpected error must come back as the 500 the application
            # would send, so a test can assert that there was none.
            transport=ASGITransport(app=self.application, client=peer, raise_app_exceptions=False),
            base_url="http://test",
            headers={"X-Forwarded-For": forwarded or _source()},
        )
        self._clients.append(client)
        return client

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()


class _LogTrap(logging.Handler):
    """Keeps every log record that reaches the root logger, fully rendered."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        # ``format`` appends the traceback of an attached exception, which is
        # where an exception's own text (and whatever it quotes) ends up.
        self.lines.append(self.format(record))
        self.lines.append(repr(record.args))

    @property
    def everything(self) -> str:
        return "\n".join(self.lines)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def one_proxy_mail_and_no_request_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment behind one proxy we operate, that can send email.

    A link is minted only when there is a mail transport to carry it; nothing
    ever connects to this host. The per-minute request limits (another layer,
    with its own tests) are raised so that every refusal asserted below comes
    from the code under test.
    """
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.hospital.example")
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_HOSPITAL_PER_MIN", 1_000_000)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    return _Clock(monkeypatch)


@pytest.fixture
def work(monkeypatch: pytest.MonkeyPatch) -> _Work:
    return _Work(monkeypatch)


@pytest.fixture
def all_logs() -> Iterator[_LogTrap]:
    """Everything the application logs, at every level, structlog included.

    The suite otherwise runs at ``CRITICAL``, which would make "nothing was
    logged" pass for the wrong reason; every test using this also asserts that
    an expected event *was* captured.
    """
    root = logging.getLogger()
    trap = _LogTrap()
    with every_log_line_reaches_the_root():
        root.addHandler(trap)
        try:
            yield trap
        finally:
            root.removeHandler(trap)


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


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession, hospital_id: uuid.UUID) -> User:
    """An administrator of hospital A holding every permission in :data:`ADMIN`."""
    return await _make_staff(db_session, hospital_id, ADMIN)


# ── 2. One source, many accounts ─────────────────────────────────────────────


class TestOneSourceSprayingManyAccounts:
    """Attack: password spraying. One guess per account never trips an
    account's own counters, so the only thing in its way is the allowance of
    the address it comes from: 300 attempts, then one every fifteen seconds."""

    async def test_the_spray_is_cut_off_at_the_source_budget_and_only_time_refills_it(
        self,
        net: _Net,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        user: User,
        work: _Work,
        clock: _Clock,
    ) -> None:
        """Attack: one wrong password at each of hundreds of accounts, from one address.

        The owner of one of those accounts sits behind the same address (one
        hospital, one NAT) on a browser that has signed in before.
        """
        shared_address = _source()
        owner = net.browser(shared_address)
        assert (await _login(owner, user.email)).status_code == 200
        assert owner.cookies.get(DEVICE_COOKIE), "the sign-in did not set a device cookie"
        last_target = await _make_user(db_session, hospital_id)
        never_attacked = await _make_user(db_session, hospital_id)
        source_budget = bucket(BucketKind.PW_SOURCE, shared_address)
        # The owner's own sign-in was right, so it cost the address nothing.
        assert await _spent(db_session, source_budget) == 0
        attacker = net.browser(shared_address)

        for _ in range(SOURCE_BURST - 1):
            response = await _login(attacker, _email("sprayed"), WRONG_PASSWORD)
            assert response.status_code == 401
        assert work.dummy == SOURCE_BURST - 1
        assert await _spent(db_session, source_budget) == SOURCE_BURST - 1
        assert _exhausted(await _failed_attempts(db_session, last_target.id)) == []

        # The 300th guess is evaluated, and is the last one that will be.
        owner_checks = work.real
        last = await _login(attacker, last_target.email, WRONG_PASSWORD)
        assert last.status_code == 401
        assert work.real == owner_checks + 1
        assert await _spent(db_session, source_budget) == SOURCE_BURST
        # It says so on the audit trail: every guess after it leaves no entry.
        assert _exhausted(await _failed_attempts(db_session, last_target.id)) == [
            [BucketKind.PW_SOURCE.value]
        ]

        # From that address nothing is looked at any more — not a guess, not
        # the right password for an account nobody has attacked.
        evaluated = work.passwords
        for email, password in (
            (_email("sprayed"), WRONG_PASSWORD),
            (last_target.email, PASSWORD),
            (never_attacked.email, PASSWORD),
        ):
            for _ in range(3):
                refused = await _login(net.browser(shared_address), email, password)
                assert refused.status_code == 401
                assert _body(refused) == GENERIC_LOGIN_FAILURE
                assert "set-cookie" not in refused.headers
        assert work.passwords == evaluated
        assert await _sessions(db_session, never_attacked.id) == 0
        assert len(await _failed_attempts(db_session, last_target.id)) == 1
        # A refused attempt is charged to nobody: the spray cannot be turned
        # into a way of draining the accounts it names.
        assert await _spent(db_session, source_budget) == SOURCE_BURST
        assert await _spent(db_session, bucket(BucketKind.PW_ACCOUNT, never_attacked.email)) == 0
        assert await _backoff(
            db_session, bucket(BucketKind.PW_PAIR, never_attacked.email, shared_address)
        ) == (0, False)

        # Everybody else is untouched: another address signs in to the same accounts ...
        elsewhere = await _login(net.browser(), never_attacked.email)
        assert elsewhere.status_code == 200, elsewhere.text
        assert (await _login(net.browser(), last_target.email)).status_code == 200
        # ... and so does the owner's trusted device, behind the exhausted address itself.
        for _ in range(3):
            trusted = await _login(owner, user.email)
            assert trusted.status_code == 200, trusted.text
            assert trusted.json()["data"]["refresh_token"]
        assert await _spent(db_session, source_budget) == SOURCE_BURST

        # Only time gives the address anything back: one attempt per fifteen seconds.
        sprayed = work.dummy
        clock.advance(seconds=SOURCE_REFILL.total_seconds() + 1)
        for _ in range(5):
            assert (await _login(attacker, _email("sprayed"), WRONG_PASSWORD)).status_code == 401
        assert work.dummy == sprayed + 1
        clock.advance(seconds=10 * SOURCE_REFILL.total_seconds())
        for _ in range(25):
            assert (await _login(attacker, _email("sprayed"), WRONG_PASSWORD)).status_code == 401
        assert work.dummy == sprayed + 1 + 10

        # And it is not a lockout: left alone, the address works again.
        clock.advance(seconds=SOURCE_REFILL.total_seconds() + 1)
        recovered = await _login(net.browser(shared_address), never_attacked.email)
        assert recovered.status_code == 200, recovered.text

    async def test_one_guess_per_account_is_still_counted_against_each_account(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """Attack: spray below every per-account threshold and expect to leave no mark.

        Each account sprayed keeps the attempt against its own shared budget
        and its own (account, source) pair, so a second pass meets them.
        """
        address = _source()
        attacker = net.browser(address)
        victims = [await _make_user(db_session, hospital_id) for _ in range(4)]

        for _ in range(PAIR_FREE):
            for victim in victims:
                assert (await _login(attacker, victim.email, WRONG_PASSWORD)).status_code == 401

        assert work.real == PAIR_FREE * len(victims)
        assert await _spent(db_session, bucket(BucketKind.PW_SOURCE, address)) == (
            PAIR_FREE * len(victims)
        )
        for victim in victims:
            assert await _spent(db_session, bucket(BucketKind.PW_ACCOUNT, victim.email)) == (
                PAIR_FREE
            )
            assert await _backoff(
                db_session, bucket(BucketKind.PW_PAIR, victim.email, address)
            ) == (PAIR_FREE, True)
            # The sixth pass finds every door shut, the right password included.
            assert (await _login(attacker, victim.email, PASSWORD)).status_code == 401
        assert work.real == PAIR_FREE * len(victims)


# ── 8. Forged forwarding headers, through real HTTP ──────────────────────────

#: How the attacker's requests arrive, per proxy configuration:
#: ``(trust the header?, attacker connects through our proxy?)``.
_SPOOFING_MODES = {
    "trust-off": (False, False),
    "trust-on-from-a-peer-that-is-not-our-proxy": (True, False),
    "trust-on-forged-left-entries-through-our-proxy": (True, True),
}


class TestForgedForwardingHeaders:
    """``X-Forwarded-For`` is written by whoever sends the request. An attacker
    uses it for two things: to look like a new source on every guess, and to
    look like *somebody else* — so that the victim's own address is the one
    that gets throttled."""

    @pytest.mark.parametrize("mode", sorted(_SPOOFING_MODES))
    async def test_a_forged_header_neither_buys_a_new_source_nor_frames_the_owners_address(
        self,
        net: _Net,
        db_session: AsyncSession,
        user: User,
        work: _Work,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        """Attack: guess twelve times, naming the owner's address or a fresh one each time."""
        trust_header, through_proxy = _SPOOFING_MODES[mode]
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", trust_header)
        attacker_address = "198.51.100.77"
        owner_address = _source()
        # Through our proxy, the proxy appends the real address after whatever
        # the client sent. Reaching the application directly, the attacker's
        # address is the socket's.
        attacker = net.browser(peer=_PROXY_PEER if through_proxy else (attacker_address, 51000))
        named: list[str] = []

        def _forged() -> str:
            claimed = owner_address if len(named) % 2 == 0 else _source()
            named.append(claimed)
            return f"{claimed}, {attacker_address}" if through_proxy else claimed

        for _ in range(12):
            response = await _login(attacker, user.email, WRONG_PASSWORD, forwarded=_forged())
            assert response.status_code == 401
            assert _body(response) == GENERIC_LOGIN_FAILURE

        # One source, however many it named: five guesses were looked at.
        assert work.real == PAIR_FREE
        refused = await _login(attacker, user.email, PASSWORD, forwarded=_forged())
        assert refused.status_code == 401
        assert work.real == PAIR_FREE
        assert await _backoff(
            db_session, bucket(BucketKind.PW_PAIR, user.email, attacker_address)
        ) == (PAIR_FREE, True)
        assert await _spent(db_session, bucket(BucketKind.PW_SOURCE, attacker_address)) == (
            PAIR_FREE
        )
        # Nothing was ever charged to an address the attacker merely named.
        assert owner_address in named
        for claimed in set(named):
            assert await _row(db_session, bucket(BucketKind.PW_PAIR, user.email, claimed)) is None
            assert await _row(db_session, bucket(BucketKind.PW_SOURCE, claimed)) is None
        # The audit trail has the five evaluated guesses, and none of the named addresses.
        failures = await _failed_attempts(db_session, user.id)
        assert len(failures) == PAIR_FREE
        for entry in failures:
            recorded = f"{entry.ip_address} {entry.context}"
            assert not any(claimed in recorded for claimed in named)

        # The owner, at the address the attacker kept naming, is not held up.
        owner = (
            net.browser(owner_address) if trust_header else net.browser(peer=(owner_address, 52000))
        )
        signed_in = await _login(owner, user.email)
        assert signed_in.status_code == 200, signed_in.text
        seen_at = await db_session.execute(
            select(RefreshToken.ip_address).where(RefreshToken.user_id == user.id)
        )
        assert [str(address) for address in seen_at.scalars().all()] == [owner_address]

    async def test_with_trust_off_a_sign_in_cannot_record_a_forged_address(
        self,
        net: _Net,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: sign in with a stolen password and leave somebody else's address on
        the session and on the audit trail."""
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", False)
        thief = net.browser("10.0.0.5, 192.0.2.1", peer=("198.51.100.88", 51000))

        response = await _login(thief, user.email)

        assert response.status_code == 200, response.text
        sessions = await db_session.execute(
            select(RefreshToken.ip_address).where(RefreshToken.user_id == user.id)
        )
        assert [str(address) for address in sessions.scalars().all()] == ["198.51.100.88"]
        entries = await _audit_rows(db_session, AuditLog.target_id == user.id)
        assert entries
        for entry in entries:
            recorded = f"{entry.ip_address} {entry.before} {entry.after} {entry.context}"
            assert "10.0.0.5" not in recorded
            assert "192.0.2.1" not in recorded

    async def test_with_trust_off_rotating_through_an_ipv6_slash_64_is_one_source(
        self,
        net: _Net,
        db_session: AsyncSession,
        user: User,
        work: _Work,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: a single IPv6 host owns 2^64 addresses and uses a new one per guess."""
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", False)

        for host in range(1, 13):
            attacker = net.browser(peer=(f"2001:db8:77:1::{host:x}", 51000))
            assert (await _login(attacker, user.email, WRONG_PASSWORD)).status_code == 401

        assert work.real == PAIR_FREE
        assert await _backoff(
            db_session, bucket(BucketKind.PW_PAIR, user.email, "2001:db8:77:1::/64")
        ) == (PAIR_FREE, True)
        # The neighbouring /64 is somebody else.
        neighbour = net.browser(peer=("2001:db8:77:2::1", 51000))
        assert (await _login(neighbour, user.email)).status_code == 200


# ── 4 and 11. The audit trail says when an allowance ran out ─────────────────


class TestTheAuditTrailRecordsWhatTheThrottleDid:
    async def test_the_code_that_empties_the_shared_code_budget_says_so(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, work: _Work
    ) -> None:
        """Attack: a distributed code-guessing run that goes quiet in the logs. Once the
        account's code budget is gone every further guess is refused unevaluated and
        leaves no entry, so the last evaluated one must record that it ran out."""
        victim, secret = await _make_mfa_user(db_session, hospital_id)

        async def guess() -> Response:
            attacker = net.browser()
            return await _verify(attacker, await _ticket(attacker, victim), _wrong_code(secret))

        for _ in range(MFA_ACCOUNT_BURST - 1):
            assert (await guess()).status_code == 401
        entries = await _failed_attempts(db_session, victim.id)
        assert len(entries) == MFA_ACCOUNT_BURST - 1
        assert _exhausted(entries) == []

        assert (await guess()).status_code == 401

        entries = await _failed_attempts(db_session, victim.id)
        assert len(entries) == MFA_ACCOUNT_BURST
        assert _exhausted(entries) == [[BucketKind.MFA_ACCOUNT.value]]
        [flagged] = [e for e in entries if (e.context or {}).get("budget_exhausted")]
        assert flagged.context is not None
        assert flagged.context["reason"] == "invalid_mfa_code"
        # After it: refused unevaluated, and nothing more is written.
        for _ in range(5):
            response = await guess()
            assert response.status_code == 401
            assert _body(response) == GENERIC_MFA_FAILURE
        assert work.codes == MFA_ACCOUNT_BURST
        assert len(await _failed_attempts(db_session, victim.id)) == MFA_ACCOUNT_BURST

    async def test_a_browser_becomes_trusted_only_at_the_code_step_and_the_trail_says_so(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Attack: with the password alone, plant a trusted device on an MFA account — a
        foothold with its own password allowance — without leaving a trace.

        The password step trusts nothing and a wrong code trusts nothing; the
        completed second factor trusts the browser and writes
        ``auth.device.trusted`` naming the account, and never the token.
        """
        victim, secret = await _make_mfa_user(db_session, hospital_id)
        browser = net.browser()

        async def trusted_events() -> list[AuditLog]:
            return await _audit_rows(
                db_session,
                AuditLog.target_id == victim.id,
                AuditLog.action == "auth.device.trusted",
            )

        ticket = await _ticket(browser, victim)
        assert (await _verify(browser, ticket, _wrong_code(secret))).status_code == 401
        assert await _device_hashes(db_session, victim.id) == set()
        assert await trusted_events() == []
        assert not browser.cookies

        completed = await _verify(browser, ticket, await _right_code(secret))

        assert completed.status_code == 200, completed.text
        token = browser.cookies.get(DEVICE_COOKIE)
        assert token
        assert await _device_hashes(db_session, victim.id) == {hash_token(token)}
        [event] = await trusted_events()
        assert event.hospital_id == victim.hospital_id
        assert event.actor_user_id == victim.id
        assert event.target_type == "user"
        recorded = f"{event.before} {event.after} {event.context}"
        assert token not in recorded
        assert hash_token(token) not in recorded
        # Coming back on the same browser adds no device, so no second entry.
        again = await _verify(browser, await _ticket(browser, victim), await _right_code(secret))
        assert again.status_code == 200, again.text
        assert len(await trusted_events()) == 1


# ── Forgot-password: the cap counts links that still work ────────────────────


class TestTheForgotPasswordCapCountsOnlyLiveLinks:
    """The cap exists so an anonymous endpoint cannot bury a mailbox. Counted
    on every token ever issued it would also be a way to deny the owner a
    reset: get three links issued and spent, and the fourth request — the
    owner's — is silently dropped."""

    async def test_three_dead_links_do_not_stop_a_new_one(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: use up the account's reset links so the owner cannot get one."""
        dead = [await _plant_token(db_session, user.id, dead=True) for _ in range(3)]
        assert await _live_token_hashes(db_session, user.id) == set()
        browser = net.browser()

        response = await _forgot(browser, user.email)

        assert response.status_code == 200
        token = await _emailed_token(db_session, user.email)
        assert token not in dead
        assert await _live_token_hashes(db_session, user.id) == {hash_token(token)}
        # It is the real thing: the owner resets the password with it.
        assert (await _redeem(browser, token)).status_code == 200
        assert (await _login(net.browser(), user.email, NEW_PASSWORD)).status_code == 200
        for spent in dead:
            assert (await _redeem(browser, spent, PASSWORD)).status_code == 401

    async def test_three_live_links_stop_a_fourth_until_one_of_them_dies(
        self, net: _Net, db_session: AsyncSession, user: User
    ) -> None:
        """Attack: flood a mailbox, and the token table, through the anonymous endpoint."""
        browser = net.browser()
        unknown = _email("nobody")

        answers = [await _forgot(browser, user.email) for _ in range(RESET_LINKS_AT_ONCE + 4)]
        control = await _forgot(browser, unknown)

        assert {_wire_shape(answer) for answer in answers} == {_wire_shape(control)}
        emailed = await _emailed_tokens(db_session, user.email)
        assert len(emailed) == RESET_LINKS_AT_ONCE
        live = await _live_token_hashes(db_session, user.id)
        assert live == {hash_token(token) for token in emailed}
        assert await _count(
            db_session, PasswordResetToken, PasswordResetToken.user_id == user.id
        ) == (RESET_LINKS_AT_ONCE)

        # One of the three stops working (here: withdrawn). The owner can ask again —
        # and is sent exactly one more.
        gone = sorted(live)[0]
        await db_session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.token_hash == gone)
            .values(used_at=datetime.now(UTC))
        )
        await db_session.commit()
        for _ in range(3):
            assert (await _forgot(browser, user.email)).status_code == 200

        assert len(await _emailed_tokens(db_session, user.email)) == RESET_LINKS_AT_ONCE + 1
        assert len(await _live_token_hashes(db_session, user.id)) == RESET_LINKS_AT_ONCE


# ── Reactivation is for suspended accounts only ──────────────────────────────


class TestReactivatingAnAccountThatIsNotSuspended:
    async def test_it_is_refused_and_signs_nobody_out(
        self, net: _Net, db_session: AsyncSession, user: User, admin: User
    ) -> None:
        """Attack: anyone holding ``user.deactivate`` "reactivates" a working colleague.

        Reactivation ends every session, forgets every trusted device and
        kills every outstanding link. Allowed on an account that was never
        suspended it is a way to sign a colleague out everywhere and strip
        the protection their devices give them, under an audit entry that
        reads like a harmless reactivation.
        """
        browser = net.browser()
        signed_in = await _login(browser, user.email)
        assert signed_in.status_code == 200, signed_in.text
        refresh_token = signed_in.json()["data"]["refresh_token"]
        devices = await _device_hashes(db_session, user.id)
        assert len(devices) == 1
        link = await _plant_token(db_session, user.id)
        password_hash = user.password_hash

        refused = await net.browser().post(f"{USERS}/{user.id}/reactivate", headers=_bearer(admin))

        assert refused.status_code == 409, refused.text
        assert refused.json()["success"] is False
        _assert_carries_no_link(refused, link)
        row = (
            await db_session.execute(
                select(User.status, User.password_hash).where(User.id == user.id)
            )
        ).one()
        assert row[0] is UserStatus.ACTIVE
        assert row[1] == password_hash
        assert await _device_hashes(db_session, user.id) == devices
        assert await _live_token_hashes(db_session, user.id) == {hash_token(link)}
        assert (
            await _audit_rows(
                db_session, AuditLog.target_id == user.id, AuditLog.action == "user.reactivated"
            )
            == []
        )
        # The session the owner holds is still good, and so is the browser.
        refreshed = await browser.post(f"{AUTH}/refresh", json={"refresh_token": refresh_token})
        assert refreshed.status_code == 200, refreshed.text
        assert (await _login(browser, user.email)).status_code == 200


# ── 10. No response carries a link ───────────────────────────────────────────


class TestNoResponseCarriesALink:
    """The happy paths are covered in ``test_invitation_delivery``. An attacker
    reads the unhappy ones: refusals and validation errors, which like to quote
    what they were given."""

    async def test_no_refusal_or_validation_error_gives_a_link_back(
        self, net: _Net, db_session: AsyncSession, admin: User
    ) -> None:
        """Attack: provoke every refusal around an invitation and read the link out of it."""
        api = net.browser()
        headers = _bearer(admin)
        email = _email("invitee")
        invited = await _invite(api, headers, email)
        assert invited.status_code == 201, invited.text
        user_id = uuid.UUID(invited.json()["data"]["id"])
        first = await _emailed_token(db_session, email)

        refusals = {
            "weak password": await _redeem(api, first, "short"),
            "missing field": await api.post(f"{AUTH}/password/reset", json={"token": first}),
            "wrong type": await api.post(
                f"{AUTH}/password/reset", json={"token": first, "new_password": ["x"]}
            ),
            "token as a list": await api.post(
                f"{AUTH}/password/reset", json={"token": [first], "new_password": NEW_PASSWORD}
            ),
            "duplicate invitation": await _invite(api, headers, email),
            "sign-in before activating": await _login(api, email),
            "forgot password before activating": await _forgot(api, email),
        }
        resent = [await _resend(api, headers, user_id) for _ in range(RESEND_BURST)]
        assert [response.status_code for response in resent] == [200] * RESEND_BURST
        refusals["one resend too many"] = await _resend(api, headers, user_id)
        assert refusals["one resend too many"].status_code == 429
        refusals["superseded link"] = await _redeem(api, first)

        emailed = await _emailed_tokens(db_session, email)
        assert len(emailed) == 1 + RESEND_BURST
        [newest_hash] = await _live_token_hashes(db_session, user_id)
        [newest] = [token for token in emailed if hash_token(token) == newest_hash]

        activated = await _redeem(api, newest)
        assert activated.status_code == 200, activated.text
        refusals["replayed link"] = await _redeem(api, newest)
        refusals["resend after activation"] = await _resend(api, headers, user_id)
        assert refusals["resend after activation"].status_code == 409
        signed_in = await _login(api, email, NEW_PASSWORD)
        assert signed_in.status_code == 200, signed_in.text

        # None of the refusals was a success in disguise ...
        assert {
            name: response.status_code
            for name, response in refusals.items()
            if response.status_code < 400 and name != "forgot password before activating"
        } == {}
        # ... and nothing that came back, refusal or not, carries any link ever emailed.
        for name, response in {
            **refusals,
            **{f"resend {index}": response for index, response in enumerate(resent)},
            "invitation": invited,
            "activation": activated,
            "sign-in": signed_in,
        }.items():
            assert response.status_code < 500, f"{name}: {response.text}"
            _assert_carries_no_link(response, *emailed)

    async def test_the_reset_link_of_forgot_password_is_in_no_response_the_owner_or_an_admin_gets(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID, admin: User
    ) -> None:
        """Attack: read a colleague's reset link from the notification centre, the audit
        log or its export — or, as the owner's session thief, from the owner's own."""
        owner = await _make_staff(db_session, hospital_id, OWN_NOTIFICATIONS)
        api = net.browser()

        asked = await _forgot(api, owner.email)

        assert asked.status_code == 200
        token = await _emailed_token(db_session, owner.email)
        reads = [asked]
        for path in (f"{USERS}/me", NOTIFICATIONS, f"{NOTIFICATIONS}/unread-count"):
            reads.append(await api.get(path, headers=_bearer(owner)))
        for path in (
            f"{USERS}/{owner.id}",
            f"{USERS}?page_size=100",
            f"{AUDIT}?page_size=100",
            f"{AUDIT}?target_id={owner.id}&page_size=100",
            f"{AUDIT}/export",
            NOTIFICATIONS,
        ):
            reads.append(await api.get(path, headers=_bearer(admin)))
        listing = await api.get(
            f"{AUDIT}?target_id={owner.id}&page_size=100", headers=_bearer(admin)
        )
        for entry in listing.json()["data"]:
            reads.append(await api.get(f"{AUDIT}/{entry['id']}", headers=_bearer(admin)))

        # The premise holds: the request is on the trail the administrator reads.
        assert "auth.password.reset_requested" in listing.text
        for response in reads:
            assert response.status_code == 200, f"{response.request.url}: {response.text}"
            _assert_carries_no_link(response, token)
        # What the owner sees in the application is a notice, not the credential.
        notices = await db_session.execute(
            select(Notification.title, Notification.body, Notification.link).where(
                Notification.recipient_user_id == owner.id
            )
        )
        _assert_never_written(repr(notices.all()), "an in-app notification", token)


# ── 11. No log line or audit record carries a link ───────────────────────────


class _UniqueViolationError(Exception):
    """Stands in for the database driver's error: it has an SQLSTATE and quotes row values."""

    sqlstate = "23505"


#: What a driver error says. Every part of it is somebody's data.
_DRIVER_TEXT = (
    'duplicate key value violates unique constraint "uq_users_email_lower" '
    "DETAIL: Key (lower(email))=(matron.secret@hospital.example) already exists."
)
_STATEMENT = "INSERT INTO notification_deliveries (to_address, subject, body) VALUES ($1, $2, $3)"
_LEAKED_LINK = "https://app.hospital.example/reset-password#token=Zm9yZ2VkLXRva2VuLWZvci10ZXN0cw"


def _database_error(kind: type[DBAPIError]) -> DBAPIError:
    """An unhandled database error whose text quotes an address and a link."""
    return kind(
        _STATEMENT,
        {"to_address": "matron.secret@hospital.example", "body": _LEAKED_LINK},
        _UniqueViolationError(f"{_DRIVER_TEXT} body={_LEAKED_LINK!r}"),
    )


def _assert_logged_by_kind_only(logged: str, printed: str, event: str) -> None:
    assert event in logged, f"the failure was not logged as {event!r}"
    assert "_UniqueViolationError" in logged, "the log does not say what kind of error it was"
    assert "23505" in logged, "the log does not carry the SQLSTATE"
    for output in (logged, printed):
        assert "matron.secret@hospital.example" not in output, "a row value was logged"
        assert "duplicate key value" not in output, "the driver's own text was logged"
        assert "uq_users_email_lower" not in output, "the driver's own text was logged"
        assert "INSERT INTO notification_deliveries" not in output, "the statement was logged"
        assert "Zm9yZ2VkLXRva2VuLWZvci10ZXN0cw" not in output, "a link token was logged"
        assert "reset-password" not in output, "a link was logged"
        assert "Traceback" not in output, "a traceback was logged"


class TestAnUnhandledDatabaseErrorIsLoggedWithoutTheDriversText:
    """Attack: provoke a database error and read row values out of the logs.

    A driver error quotes the offending row ("Key (email)=(…) already
    exists"), and SQLAlchemy adds the statement and its parameters. Both
    last-resort handlers log what *kind* of error it was — its type and
    SQLSTATE — and nothing the error itself says.
    """

    @pytest.mark.parametrize("kind", [DBAPIError, IntegrityError, OperationalError])
    async def test_through_a_real_request(
        self,
        net: _Net,
        user: User,
        all_logs: _LogTrap,
        capfd: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        kind: type[DBAPIError],
    ) -> None:
        error = _database_error(kind)
        # The premise holds: the error's own text carries all of it.
        assert "matron.secret@hospital.example" in str(error)
        assert _LEAKED_LINK in str(error)

        async def _fails(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise error

        monkeypatch.setattr(AuthService, "login", _fails)

        response = await _login(net.browser(), user.email)

        assert response.status_code == 500
        assert _body(response) == GENERIC_SERVER_ERROR
        sent = _everything_sent(response)
        assert "matron.secret" not in sent
        assert "duplicate key" not in sent
        assert "reset-password" not in sent
        _assert_logged_by_kind_only(
            all_logs.everything,
            "".join(capfd.readouterr()),
            "middleware_caught_unhandled_exception",
        )

    @pytest.mark.parametrize("kind", [DBAPIError, IntegrityError, OperationalError])
    async def test_through_the_applications_own_last_resort_handler(
        self,
        net: _Net,
        all_logs: _LogTrap,
        capfd: pytest.CaptureFixture[str],
        kind: type[DBAPIError],
    ) -> None:
        """The handler registered on the application, for an error that reaches it
        without passing the middleware (it is the one the middleware backs up).

        The application comes from ``net`` so that it exists before the log
        capture opens: building one resets the logging level.
        """
        application = net.application
        handler: Any = application.exception_handlers[Exception]
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"{AUTH}/login",
                "headers": [],
                "query_string": b"",
                "app": application,
            }
        )

        response = await handler(request, _database_error(kind))

        assert response.status_code == 500
        payload = json.loads(bytes(response.body))
        payload.pop("metadata", None)
        assert payload == GENERIC_SERVER_ERROR
        assert "matron.secret" not in bytes(response.body).decode()
        _assert_logged_by_kind_only(
            all_logs.everything, "".join(capfd.readouterr()), "unhandled_exception"
        )


class TestForgotPasswordWritesTheLinkNowhereButTheEmail:
    async def test_no_log_line_or_audit_record_carries_the_reset_link(
        self,
        net: _Net,
        db_session: AsyncSession,
        user: User,
        all_logs: _LogTrap,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        """Attack: anyone who can read application logs or the audit table collects
        reset links, each of which sets an account's password."""
        browser = net.browser()

        assert (await _forgot(browser, user.email)).status_code == 200
        token = await _emailed_token(db_session, user.email)
        assert (await _redeem(browser, token)).status_code == 200
        assert (await _redeem(browser, token, PASSWORD)).status_code == 401

        logged = all_logs.everything
        printed = "".join(capfd.readouterr())
        # The capture is real: the events these calls emit are in it.
        for event in ("password_reset_token_created", "password_reset_completed"):
            assert event in logged, f"log capture missed {event!r}"
        for output, where in ((logged, "the logs"), (printed, "the process output")):
            _assert_never_written(output, where, token)
            assert user.email not in output, f"the address is in {where}"
        entries = await _audit_rows(db_session, AuditLog.target_id == user.id)
        actions = {entry.action for entry in entries}
        assert {"auth.password.reset_requested", "auth.password.reset"} <= actions
        for entry in entries:
            _assert_never_written(
                f"{entry.before} {entry.after} {entry.context}",
                f"audit entry {entry.action}",
                token,
            )

    @pytest.mark.parametrize("failure", [RuntimeError, ConnectionError, DBAPIError])
    async def test_a_notifier_that_raises_leaks_nothing_and_changes_nothing_the_caller_can_see(
        self,
        net: _Net,
        db_session: AsyncSession,
        user: User,
        all_logs: _LogTrap,
        capfd: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        failure: type[Exception],
    ) -> None:
        """Attack: break the mail queue, then ask for a victim's reset.

        A notifier is contracted not to raise. One that does must look
        exactly like "not queued". An error here would happen only for a real
        account — a 500, or a missing wait, tells an anonymous caller that the
        address exists — and the error raised quotes the link: logging it would
        hand the victim's account to whoever reads the logs, with a token that
        is still live because nothing killed it.
        """
        requests: list[NotificationRequest] = []
        raised: list[str] = []

        async def _raises(service: NotificationService, request: NotificationRequest) -> bool:
            requests.append(request)
            link = request.secret_variables["action_url"]
            if failure is DBAPIError:
                error: Exception = DBAPIError(_STATEMENT, {"body": link}, Exception(f"bad {link}"))
            else:
                error = failure(f"mail queue refused {link}")
            raised.append(str(error))
            raise error

        monkeypatch.setattr(NotificationService, "deliver_credential", _raises)
        waits: list[float] = []

        async def _sleep(seconds: float) -> None:
            waits.append(seconds)

        floor = 30.0  # far above any real work, so every padded path must wait
        monkeypatch.setattr(settings, "AUTH_FAILURE_MIN_SECONDS", floor)
        monkeypatch.setattr(auth_service_module, "_sleep", _sleep)
        browser = net.browser()
        password_hash = user.password_hash

        answer = await _forgot(browser, user.email)
        waited_for_the_victim = list(waits)
        waits.clear()
        control = await _forgot(browser, _email("nobody"))

        # The premise holds: a link was minted, and the error quotes it.
        [request] = requests
        token = request.secret_variables["action_url"].partition("#token=")[2]
        assert token
        assert token in raised[0]
        # The caller sees exactly what it sees for an address that names nobody ...
        assert answer.status_code == 200
        assert _wire_shape(answer) == _wire_shape(control)
        _assert_carries_no_link(answer, token)
        # ... after the same wait.
        assert len(waited_for_the_victim) == len(waits) == 1
        assert 0 < waited_for_the_victim[0] <= floor * 1.2
        assert 0 < waits[0] <= floor * 1.2

        # The link no email carries is dead before the request ends.
        await db_session.rollback()  # only what the request committed remains
        assert await _live_token_hashes(db_session, user.id) == set()
        used = await db_session.execute(
            select(PasswordResetToken.used_at).where(
                PasswordResetToken.token_hash == hash_token(token)
            )
        )
        assert [stamp is not None for stamp in used.scalars().all()] == [True]
        assert (await _redeem(browser, token)).status_code == 401
        stored = await db_session.execute(select(User.password_hash).where(User.id == user.id))
        assert stored.scalar_one() == password_hash
        assert await _emailed_tokens(db_session, user.email) == set()

        # The failure is logged by kind, and nothing of what it said.
        logged = all_logs.everything
        printed = "".join(capfd.readouterr())
        assert "password_reset_email_failed" in logged, "the failure was not logged"
        assert failure.__name__ in logged, "the log does not say what kind of error it was"
        for output, where in ((logged, "the logs"), (printed, "the process output")):
            _assert_never_written(output, where, token)
            assert "reset-password" not in output, f"a link is in {where}"
            assert "mail queue refused" not in output, f"the error's own text is in {where}"
            assert raised[0] not in output, f"the error's own text is in {where}"
            assert "Traceback" not in output, f"a traceback is in {where}"
            assert user.email not in output, f"the address is in {where}"
        for entry in await _audit_rows(db_session, AuditLog.target_id == user.id):
            _assert_never_written(
                f"{entry.before} {entry.after} {entry.context}",
                f"audit entry {entry.action}",
                token,
            )


# ── Real connections ─────────────────────────────────────────────────────────


class _RealWorld:
    """A committed hospital with an administrator, and an application on real sessions.

    Every request opens its own session on its own pooled connection and
    commits for real, as in service. Nothing is rolled back for us:
    :meth:`cleanup` deletes every row the test or the application wrote.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        self.hospital_id = uuid.uuid4()
        self.admin_headers: dict[str, str] = {}
        self._buckets_before: set[str] = set()
        self._permissions_before: set[uuid.UUID] = set()
        application = create_app()
        application.state.db_session_factory = self.factory
        self.net = _Net(application)

    async def setup(self) -> None:
        async with self.factory() as session:
            self._permissions_before = set(
                (await session.execute(select(Permission.id))).scalars().all()
            )
            self._buckets_before = set(
                (await session.execute(select(AuthThrottleBucket.key_hash))).scalars().all()
            )
            session.add(
                Hospital(
                    id=self.hospital_id,
                    name="Security Gate Hospital",
                    slug=f"gate-{uuid.uuid4().hex[:12]}",
                    address={"line1": "1 Test Road", "city": "Hyderabad", "country": "IN"},
                    settings={},
                )
            )
            await session.flush()
            administrator = _new_user(self.hospital_id, email=_email("admin"))
            session.add(administrator)
            await session.flush()
            await grant_permissions(
                session, hospital_id=self.hospital_id, user_id=administrator.id, codes=ADMIN
            )
            await session.commit()
        self.admin_headers = _bearer(administrator)

    async def cleanup(self) -> None:
        """Delete every row this test committed."""
        await self.net.aclose()
        async with self.factory() as session:
            await session.execute(delete(AuditLog).where(AuditLog.hospital_id == self.hospital_id))
            await session.execute(
                delete(AuthThrottleBucket).where(
                    AuthThrottleBucket.key_hash.not_in(self._buckets_before)
                )
            )
            # Sessions, tokens, notifications and their deliveries, role grants
            # and trusted devices all go with the users.
            await session.execute(delete(User).where(User.hospital_id == self.hospital_id))
            await session.execute(delete(Role).where(Role.hospital_id == self.hospital_id))
            await session.execute(
                delete(Permission).where(Permission.id.not_in(self._permissions_before))
            )
            await session.execute(delete(Hospital).where(Hospital.id == self.hospital_id))
            await session.commit()

    async def make_user(self, **overrides: Any) -> User:
        user = _new_user(self.hospital_id, **overrides)
        async with self.factory() as session:
            session.add(user)
            await session.commit()
        return user

    async def invite(self) -> tuple[uuid.UUID, str, str]:
        """Invite someone over HTTP; return their id, address and the token in their mailbox."""
        email = _email("race")
        response = await _invite(self.net.browser(), self.admin_headers, email)
        assert response.status_code == 201, response.text
        async with self.factory() as session:
            token = await _emailed_token(session, email)
        return uuid.UUID(response.json()["data"]["id"]), email, token

    async def count(self, model: Any, *criteria: Any) -> int:
        async with self.factory() as session:
            return await _count(session, model, *criteria)

    async def spent(self, target: Bucket) -> int:
        async with self.factory() as session:
            return await _spent(session, target)

    async def account(self, user_id: uuid.UUID) -> tuple[UserStatus, str]:
        """An account's committed status and password hash."""
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(User.status, User.password_hash).where(User.id == user_id)
                )
            ).one()
        return row[0], row[1]

    async def links(self, user_id: uuid.UUID, email: str) -> tuple[set[str], set[str]]:
        """Committed state of an account's links: ``(live hashes, tokens ever emailed)``."""
        async with self.factory() as session:
            return await _live_token_hashes(session, user_id), await _emailed_tokens(session, email)


@pytest_asyncio.fixture
async def real(db_engine: AsyncEngine) -> AsyncGenerator[_RealWorld]:
    """Independent connections with real commits; cleaned up whatever happens."""
    engine = create_async_engine(db_engine.url, pool_size=8, max_overflow=0, pool_timeout=120)
    world = _RealWorld(engine)
    try:
        await world.setup()
        yield world
    finally:
        try:
            await world.cleanup()
        finally:
            await engine.dispose()


class TestOneSourceSprayingInParallel:
    async def test_a_parallel_spray_is_not_evaluated_beyond_the_source_budget(
        self, real: _RealWorld, work: _Work, clock: _Clock
    ) -> None:
        """Attack: with ten attempts left for the address, fire forty at once, each at a
        different account, so they all pass the check before any of them is counted."""
        address = _source()
        source_budget = bucket(BucketKind.PW_SOURCE, address)
        victim = await real.make_user()
        attacker = real.net.browser(address)
        for _ in range(SOURCE_BURST - 10):
            assert (await _login(attacker, _email("sprayed"), WRONG_PASSWORD)).status_code == 401
        assert work.dummy == SOURCE_BURST - 10
        assert await real.spent(source_budget) == SOURCE_BURST - 10

        responses = await asyncio.gather(
            *(
                _login(real.net.browser(address), _email("sprayed"), WRONG_PASSWORD)
                for _ in range(40)
            )
        )

        assert [r.status_code for r in responses] == [401] * 40
        assert all(_body(r) == GENERIC_LOGIN_FAILURE for r in responses)
        assert work.dummy <= SOURCE_BURST
        assert work.dummy == SOURCE_BURST  # and not fewer: every unit was usable
        assert await real.spent(source_budget) == SOURCE_BURST
        # The right password for a real account, in parallel, from there: none is looked at.
        attempts = await asyncio.gather(
            *(_login(real.net.browser(address), victim.email) for _ in range(10))
        )
        assert [r.status_code for r in attempts] == [401] * 10
        assert work.real == 0
        assert await real.count(RefreshToken, RefreshToken.user_id == victim.id) == 0
        # While, at the same moment, another address signs in.
        assert (await _login(real.net.browser(), victim.email)).status_code == 200


class TestCodeGuessingRacingTheOwnerInParallel:
    async def test_a_parallel_guessing_run_cannot_shut_out_the_owners_verified_device(
        self, real: _RealWorld, work: _Work
    ) -> None:
        """Attack: the thief has the password. At the very moment the owner signs in on
        the browser that has passed the second factor before, thirty wrong codes
        arrive from thirty addresses to take the account's code budget first."""
        secret = generate_totp_secret()
        victim = await real.make_user(mfa_enabled=True, mfa_secret=encrypt_mfa_secret(secret))
        owner = real.net.browser()
        first = await _verify(owner, await _ticket(owner, victim), await _right_code(secret))
        assert first.status_code == 200, first.text
        assert owner.cookies.get(DEVICE_COOKIE)
        thieves = [real.net.browser() for _ in range(30)]
        tickets = [await _ticket(thief, victim) for thief in thieves]
        owner_ticket = await _ticket(owner, victim)
        looked_at = work.codes
        code = await _right_code(secret)

        owner_answer, *thief_answers = await asyncio.gather(
            _verify(owner, owner_ticket, code),
            *(
                _verify(thief, ticket, _wrong_code(secret))
                for thief, ticket in zip(thieves, tickets, strict=True)
            ),
        )

        assert owner_answer.status_code == 200, owner_answer.text
        assert owner_answer.json()["data"]["refresh_token"]
        assert [r.status_code for r in thief_answers] == [401] * 30
        assert all(_body(r) == GENERIC_MFA_FAILURE for r in thief_answers)
        # Ten of the thief's codes were looked at, plus the owner's one.
        assert work.codes == looked_at + MFA_ACCOUNT_BURST + 1
        assert await real.spent(bucket(BucketKind.MFA_ACCOUNT, victim.id)) == MFA_ACCOUNT_BURST
        assert await real.count(RefreshToken, RefreshToken.user_id == victim.id) == 2
        assert await real.count(TrustedDevice, TrustedDevice.user_id == victim.id) == 1
        # With the shared budget gone the owner still completes sign-in, in parallel
        # with a thief who has the right code and is not that browser.
        stranger = real.net.browser()
        stranger_ticket = await _ticket(stranger, victim)
        owner_ticket = await _ticket(owner, victim)
        code = await _right_code(secret)
        again, refused = await asyncio.gather(
            _verify(owner, owner_ticket, code), _verify(stranger, stranger_ticket, code)
        )
        assert again.status_code == 200, again.text
        assert refused.status_code == 401
        assert _body(refused) == GENERIC_MFA_FAILURE
        assert await real.count(RefreshToken, RefreshToken.user_id == victim.id) == 3


class TestInvitationStormsInParallel:
    """One-against-one races are covered in ``test_invitation_delivery``. Here
    many requests of each kind arrive together, each on its own connection."""

    async def test_parallel_resends_send_no_more_than_the_allowance_and_leave_one_link(
        self, real: _RealWorld
    ) -> None:
        """Attack: fire ten resends at once to bury the invitee's mailbox, or to leave
        several working links for one account."""
        user_id, email, original = await real.invite()

        responses = await asyncio.gather(
            *(_resend(real.net.browser(), real.admin_headers, user_id) for _ in range(10))
        )

        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200] * RESEND_BURST + [429] * (10 - RESEND_BURST), statuses
        live, emailed = await real.links(user_id, email)
        assert len(emailed) == 1 + RESEND_BURST
        assert len(live) == 1, "more than one link is live for one invitation"
        assert live <= {hash_token(token) for token in emailed}
        assert hash_token(original) not in live
        for response in responses:
            _assert_carries_no_link(response, *emailed)
        assert (
            await real.count(
                AuditLog, AuditLog.target_id == user_id, AuditLog.action == "user.invitation_resent"
            )
            == RESEND_BURST
        )
        # Every link ever emailed, redeemed at once: exactly one opens the account.
        redeemed = await asyncio.gather(
            *(
                _redeem(real.net.browser(), token, f"St0rm!Passw0rd-{index:02d}")
                for index, token in enumerate(sorted(emailed))
            )
        )
        assert sorted(r.status_code for r in redeemed) == [200] + [401] * RESEND_BURST
        assert (await real.account(user_id))[0] is UserStatus.ACTIVE
        assert (await real.links(user_id, email))[0] == set()

    async def test_a_storm_of_resends_and_activations_never_leaves_a_link_into_an_active_account(
        self, real: _RealWorld
    ) -> None:
        """Attack: time resends against the invitee's activation so that a fresh, emailed
        link is left alive for an account somebody is already using — or so that one
        of the requests falls over."""
        for _ in range(5):
            user_id, email, token = await real.invite()
            passwords = [f"St0rm!Passw0rd-{index:02d}" for index in range(4)]

            answers = await asyncio.gather(
                *(_redeem(real.net.browser(), token, password) for password in passwords),
                *(_resend(real.net.browser(), real.admin_headers, user_id) for _ in range(4)),
            )

            activations, resends = answers[:4], answers[4:]
            activated = [r.status_code for r in activations]
            resent = [r.status_code for r in resends]
            # Every request was answered properly: one of these, never a 500.
            assert set(activated) <= {200, 401}, [r.text for r in activations]
            assert set(resent) <= {200, 409, 429}, [r.text for r in resends]
            assert activated.count(200) <= 1
            status, password_hash = await real.account(user_id)
            live, emailed = await real.links(user_id, email)
            for response in answers:
                _assert_carries_no_link(response, *emailed)

            if activated.count(200) == 1:
                # The activation won: no resend may have succeeded after it,
                # and none before it either, or this link would have been dead.
                assert status is UserStatus.ACTIVE
                assert resent.count(200) == 0, "an activated account was sent a new link"
                assert live == set(), "an ACTIVE account still has a redeemable link"
                assert emailed == {token}
                winner = passwords[activated.index(200)]
                assert verify_password(winner, password_hash), "a losing request set the password"
            else:
                # A resend won: the account is still waiting, with exactly one link.
                assert status is UserStatus.INVITED
                assert resent.count(200) >= 1
                assert len(emailed) == 1 + resent.count(200)
                assert len(live) == 1, "an invitation has several working links, or none"
                assert hash_token(token) not in live
                assert not any(verify_password(p, password_hash) for p in passwords)

            # Whatever happened, no link ever emailed changes an ACTIVE account.
            if status is UserStatus.ACTIVE:
                later = await asyncio.gather(
                    *(_redeem(real.net.browser(), sent, PASSWORD) for sent in emailed),
                    _resend(real.net.browser(), real.admin_headers, user_id),
                )
                assert [r.status_code for r in later[:-1]] == [401] * len(emailed)
                assert later[-1].status_code == 409
                assert (await real.account(user_id))[1] == password_hash
                assert (await real.links(user_id, email))[0] == set()
