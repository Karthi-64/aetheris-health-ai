"""Patient one-time codes, attacked (Patient App, Task 28).

Every test here is an attack on the sign-in code a patient is sent by SMS
(``docs/modules/15-patient-app.md`` §5.1–5.3, §5.9, as amended by the owner:
no Redis and no lock on a phone number), replayed through real HTTP against
the application and a real PostgreSQL. Each one states the attack and asserts
what the attacker must not get.

**How the source address is varied.** ``RATE_LIMIT_TRUST_PROXY_HEADER`` is
switched on and the ASGI transport's socket peer is ``127.0.0.1`` — a trusted
proxy under the default ``RATE_LIMIT_TRUSTED_PROXY_CIDRS`` — so the request's
``X-Forwarded-For`` is resolved by the real ``app.core.client_ip.client_ip``,
exactly as behind a load balancer. Nothing in the resolver is patched.

**How time is moved.** Only through the throttle's clock
(``AuthThrottleRepository.now``), which also times the codes; nothing sleeps.

**What stands in for the SMS provider.** An in-memory sender injected through
the ``get_sms_sender`` dependency. It is the only place a test can learn a
code, as the patient's phone is the only place a patient can.

**What the shared session can and cannot prove.** Most tests use the suite's
``db_session``: one connection inside one rolled-back transaction, on which
requests can only run one after another. That proves the arithmetic, not the
atomicity. ``TestParallelVerification`` therefore uses a pool of real
connections with real commits, fires its requests at once, and deletes what
it wrote.

The per-minute request limit of ``app.middleware.rate_limit`` (a different
layer with its own tests) is raised for these tests, so that every refusal
asserted here was decided by the code under test.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from pydantic import ValidationError as SettingsError
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.dependencies.db import get_db_session
from app.api.dependencies.patient import get_sms_sender
from app.core import sms as sms_module
from app.core.config import AppEnv, Settings, settings
from app.core.security import hash_token
from app.core.sms import SmsDeliveryError, SmsMessage
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.auth_throttle import AuthThrottleBucket
from app.models.patient_account import (
    PatientAccount,
    PatientDevice,
    PatientOtpChallenge,
    PatientRefreshToken,
)
from app.models.user import User
from app.services import auth_throttle as throttle_module
from app.services.auth_throttle import AuthThrottle, BucketKind, Budget, bucket
from app.services.patient_app import patient_auth_service as auth_module
from app.services.patient_app.patient_auth_service import PatientAuthService
from app.tests.conftest import every_log_line_reaches_the_root
from app.tests.patient_app_helpers import (
    CSRF,
    DEVICE_COOKIE,
    PATIENT,
    REFRESH_COOKIE,
    FakeSmsSender,
    ThrottleClock,
    audit_rows,
    bearer,
    new_phone,
    set_cookie_headers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.database

REQUEST = f"{PATIENT}/auth/otp/request"
VERIFY = f"{PATIENT}/auth/otp/verify"
REFRESH = f"{PATIENT}/auth/refresh"
LOGOUT = f"{PATIENT}/auth/logout"
LOGOUT_ALL = f"{PATIENT}/auth/logout-all"
ME = f"{PATIENT}/me"

# The documented numbers (PATIENT_DESIGN "OTP", spec §5.2), restated here on
# purpose: if somebody loosens the policy, these tests must fail rather than
# follow it.
CODE_LIFETIME = timedelta(minutes=5)
ATTEMPTS_PER_CHALLENGE = 5
SOURCE_BURST = 30
SOURCE_REFILL = timedelta(seconds=60)
PAIR_BURST = 3
PAIR_REFILL = timedelta(minutes=10)
PHONE_BURST = 6
PHONE_REFILL = timedelta(minutes=5)
DEVICE_BURST = 3
DEVICE_REFILL = timedelta(minutes=10)
GLOBAL_BURST = 600
VERIFY_SOURCE_BURST = 60
VERIFY_SOURCE_REFILL = timedelta(seconds=30)

#: The one answer to every code that is not accepted, whatever the reason.
OTP_INVALID = {
    "success": False,
    "message": "The code is incorrect or has expired.",
    "error_code": "OTP_INVALID",
    "errors": None,
}
#: The one answer to every request for a code that a limit refused.
OTP_THROTTLED = {
    "success": False,
    "message": "Too many requests. Please try again later.",
    "error_code": "OTP_THROTTLED",
    "errors": None,
}
SMS_UNAVAILABLE = {
    "success": False,
    "message": "Sign-in by SMS is temporarily unavailable.",
    "error_code": "SERVICE_UNAVAILABLE",
    "errors": None,
}

#: The socket peer of every test request: a proxy we "operate".
_PROXY_PEER = ("127.0.0.1", 40000)

#: Response headers that differ between any two requests, whoever sends them.
_VOLATILE_HEADERS = frozenset(
    {
        "x-request-id",
        "x-ratelimit-reset",
        # The per-minute request limiter's count of the caller's own requests.
        "x-ratelimit-remaining",
        "x-process-time",
        "x-response-time",
        "date",
    }
)

#: Random identifiers and digests: they are hexadecimal, so by chance they can
#: contain any six digits. They are blanked before looking for a code.
_OPAQUE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,}"
)
_SIX_DIGITS = re.compile(r"\b(\d{6})\b")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _body(response: Response) -> dict[str, Any]:
    """The response envelope without its per-request metadata."""
    payload: dict[str, Any] = response.json()
    payload.pop("metadata", None)
    return payload


def _stable_headers(response: Response) -> dict[str, str]:
    """A response's headers without the ones that are different on every request."""
    return {
        name: value
        for name, value in sorted(response.headers.items())
        if name.lower() not in _VOLATILE_HEADERS
    }


def _source() -> str:
    """A public IPv4 address nobody else in this run is using."""
    return f"203.{secrets.randbelow(256)}.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}"


def _mentions(text: str, code: str) -> bool:
    """Whether ``text`` contains ``code`` as a number of its own.

    Identifiers and digests are blanked first, and a match must not be part of
    a longer number or the fraction of one (a timestamp's microseconds).
    """
    cleaned = _OPAQUE.sub("#", text)
    return re.search(rf"(?<![0-9.]){re.escape(code)}(?![0-9])", cleaned) is not None


def _other_code(code: str, step: int = 1) -> str:
    """A well-formed code that is not ``code``."""
    return f"{(int(code) + step) % 10**6:06d}"


def _code_in(message: SmsMessage) -> str:
    match = _SIX_DIGITS.search(message.body)
    assert match is not None, "the message carries no six-digit code"
    return match.group(1)


def _dump(rows: list[Any]) -> str:
    """Every column of every row, as text."""
    return json.dumps(
        [
            {column.name: getattr(row, column.name) for column in type(row).__table__.columns}
            for row in rows
        ],
        default=str,
    )


async def _all(session: AsyncSession, model: Any, *where: Any) -> list[Any]:
    result = await session.execute(
        select(model).where(*where).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    result = await session.execute(select(func.count()).select_from(model).where(*where))
    return int(result.scalar_one())


async def _challenge_row(session: AsyncSession, challenge_id: str) -> PatientOtpChallenge:
    row: PatientOtpChallenge
    [row] = await _all(
        session, PatientOtpChallenge, PatientOtpChallenge.id == uuid.UUID(challenge_id)
    )
    return row


async def _patient_buckets(session: AsyncSession) -> int:
    return await _count(session, AuthThrottleBucket, AuthThrottleBucket.kind.like("pt_%"))


async def _ask(client: AsyncClient, phone: str) -> Response:
    return await client.post(REQUEST, json={"phone": phone})


async def _try(client: AsyncClient, challenge_id: str, code: str) -> Response:
    return await client.post(VERIFY, json={"challenge_id": challenge_id, "code": code})


async def _challenge(client: AsyncClient, sms: FakeSmsSender, phone: str) -> tuple[str, str]:
    """Ask for a code and return ``(challenge id, the code that was texted)``."""
    sent = len(sms.sent)
    response = await _ask(client, phone)
    assert response.status_code == 202, response.text
    assert len(sms.sent) == sent + 1
    assert sms.sent[-1].to == phone
    return str(response.json()["data"]["challenge_id"]), _code_in(sms.sent[-1])


async def _sign_in(client: AsyncClient, sms: FakeSmsSender, phone: str) -> dict[str, Any]:
    challenge_id, code = await _challenge(client, sms, phone)
    response = await _try(client, challenge_id, code)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


class _Net:
    """Hands out browsers: HTTP clients with their own cookie jar and address."""

    def __init__(self, application: FastAPI) -> None:
        self.application = application
        self._clients: list[AsyncClient] = []

    def browser(
        self,
        source: str | None = None,
        *,
        peer: tuple[str, int] = _PROXY_PEER,
        cookie: str | None = None,
        forwarded: bool = True,
    ) -> AsyncClient:
        """A new browser with an empty cookie jar, seen as coming from ``source``.

        :param cookie: A device-cookie value this client sends on every request,
            whatever the server answers: a copied or made-up cookie.
        :param forwarded: Whether the request carries ``X-Forwarded-For`` at all.
        """
        headers: dict[str, str] = {}
        if forwarded:
            headers["X-Forwarded-For"] = source or _source()
        if cookie is not None:
            headers["Cookie"] = f"{DEVICE_COOKIE}={cookie}"
        client = AsyncClient(
            transport=ASGITransport(app=self.application, client=peer),
            base_url="http://test",
            headers=headers,
        )
        self._clients.append(client)
        return client

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()


class _LogTrap(logging.Handler):
    """Collects every log line while installed on the root logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.lines: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        # ``format`` appends the traceback of an attached exception, which is
        # where an exception's own text (and whatever it quotes) ends up.
        self.lines.append((record.name, self.format(record)))
        self.lines.append((record.name, repr(record.args)))

    @property
    def everything(self) -> str:
        return "\n".join(line for _, line in self.lines)

    @property
    def application(self) -> str:
        """Everything but the database driver's echo of its own statements.

        With every logger opened right up, SQLAlchemy logs each statement's
        parameters — the phone column among them. That is this trap's doing,
        not the application's: nothing configures it in service.
        """
        return "\n".join(line for name, line in self.lines if not name.startswith("sqlalchemy"))


def _held(client: AsyncClient) -> str:
    """The device cookie a browser holds."""
    value = client.cookies.get(DEVICE_COOKIE)
    assert value, "the browser holds no device cookie"
    return value


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _attack_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain-HTTP cookies, one trusted proxy, and the request limiter out of the way."""
    monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)


@pytest.fixture
def sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ThrottleClock:
    return ThrottleClock(monkeypatch)


@contextmanager
def _capturing_logs() -> Iterator[_LogTrap]:
    """Everything the application logs, at every level, structlog included.

    The suite otherwise runs at ``CRITICAL``, which would make "the code was
    not logged" pass for the wrong reason; every test using this asserts that
    an expected event *was* captured. Building the application reconfigures
    logging, so this is entered after the application exists.
    """
    root = logging.getLogger()
    trap = _LogTrap()
    with every_log_line_reaches_the_root():
        root.addHandler(trap)
        try:
            yield trap
        finally:
            root.removeHandler(trap)


@pytest.fixture
def all_logs(net: _Net) -> Iterator[_LogTrap]:
    """The log trap, installed once the ``net`` fixture has built the application."""
    with _capturing_logs() as trap:
        yield trap


@pytest.fixture
def no_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch off the occasional, random clean-up of dead rows.

    It runs on one request in thirty-two; a test that compares the statements
    two requests execute cannot have it fire on one of them.
    """

    async def _nothing(self: object, now: datetime) -> None:
        return None

    monkeypatch.setattr(AuthThrottle, "_sweep", _nothing)
    monkeypatch.setattr(PatientAuthService, "_sweep", _nothing)


@pytest.fixture
def statements(db_engine: AsyncEngine) -> Iterator[list[str]]:
    """Every SQL statement sent to the database, in order, without its parameters."""
    seen: list[str] = []

    def _record(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        seen.append(re.sub(r"sa_savepoint_\d+", "sa_savepoint", statement))

    event.listen(db_engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _record)


def _application(session: AsyncSession, sms: FakeSmsSender | None) -> FastAPI:
    application = create_app()

    async def _override() -> AsyncGenerator[AsyncSession]:
        yield session

    application.dependency_overrides[get_db_session] = _override
    application.dependency_overrides[get_sms_sender] = lambda: sms
    return application


@pytest_asyncio.fixture
async def net(db_session: AsyncSession, sms: FakeSmsSender) -> AsyncGenerator[_Net]:
    """Browsers talking to the application on the test's rolled-back session."""
    network = _Net(_application(db_session, sms))
    try:
        yield network
    finally:
        await network.aclose()
        network.application.dependency_overrides.clear()


# ── 1. The code is nowhere but in the message ────────────────────────────────


class TestTheCodeIsNowhereButInTheMessage:
    async def test_it_is_in_no_response_log_line_audit_record_or_table(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        clock: ThrottleClock,
        all_logs: _LogTrap,
    ) -> None:
        """Attack: read a code from a response, the logs, the audit trail or the database."""
        known, unknown = new_phone(), new_phone()
        browser = net.browser()
        responses: list[Response] = []

        async def _record(response: Response) -> Response:
            responses.append(response)
            return response

        async def _issue(phone: str) -> tuple[str, str]:
            response = await _record(await _ask(browser, phone))
            assert response.status_code == 202, response.text
            return str(response.json()["data"]["challenge_id"]), _code_in(sms.sent[-1])

        # A whole life of codes: used, guessed at, guessed to death, expired,
        # never used, throttled and refused, then a refresh and a sign-out.
        first = await _issue(known)
        signed_in = await _record(await _try(browser, *first))
        assert signed_in.status_code == 200
        await _record(await _try(browser, *first))  # used again
        second = await _issue(known)
        for step in range(1, ATTEMPTS_PER_CHALLENGE + 1):
            await _record(await _try(browser, second[0], _other_code(second[1], step)))
        await _record(await _try(browser, *second))  # dead by now
        third = await _issue(known)
        # Asked for until a limit says no (the browser is a recognised device
        # by now, so it is that budget which runs out).
        for _ in range(PAIR_BURST + DEVICE_BURST):
            if (await _record(await _ask(browser, known))).status_code == 429:
                break
        assert responses[-1].status_code == 429
        await _issue(unknown)
        clock.advance(minutes=6)
        await _record(await _try(browser, *third))  # expired
        await _record(
            await browser.get(ME, headers=bearer(signed_in.json()["data"]["access_token"]))
        )
        await _record(await browser.post(REFRESH, headers=CSRF))
        await _record(await browser.post(LOGOUT, headers=CSRF))

        codes = [_code_in(message) for message in sms.sent]
        assert len(codes) >= 4
        # The detector does detect: it finds each code in the message itself.
        for message, code in zip(sms.sent, codes, strict=True):
            assert _mentions(message.body, code)
            assert _mentions(f'{{"code": "{code}"}}', code)
            assert _mentions(f"otp={code} sent", code)

        # 1. No response: body or header.
        for response in responses:
            everything = (
                response.text
                + "\n"
                + "\n".join(f"{name}: {value}" for name, value in response.headers.multi_items())
            )
            for code in codes:
                assert not _mentions(everything, code), (response.request.url, response.status_code)

        # 2. No log line. The events were captured, so the trap proves something.
        logged = all_logs.everything
        for expected in ("patient_login", "patient_otp_rejected", "patient_otp_send_throttled"):
            assert expected in logged, f"{expected} was not captured: the trap proves nothing"
        for code in codes:
            assert not _mentions(logged, code)
        # Nor the number a code was sent to, in any form, in anything the
        # application itself logs.
        for phone in (known, unknown):
            assert phone not in all_logs.application
            assert phone[3:] not in all_logs.application

        # 3. No audit record: every column of every row.
        audit = await _all(db_session, AuditLog)
        assert {row.action for row in audit} >= {
            "patient.auth.otp_requested",
            "patient.auth.otp_failed",
            "patient.auth.login",
            "patient.auth.account_created",
        }
        dumped = _dump(audit)
        for code in codes:
            assert not _mentions(dumped, code)
        for phone in (known, unknown):
            assert phone not in dumped
            assert phone[3:] not in dumped

        # 4. No table: every column of every row the flows wrote.
        challenges = await _all(db_session, PatientOtpChallenge)
        assert len(challenges) == len(codes)
        tables = (
            PatientOtpChallenge,
            PatientAccount,
            PatientRefreshToken,
            PatientDevice,
            AuthThrottleBucket,
        )
        stored = "\n".join([_dump(await _all(db_session, model)) for model in tables])
        for code in codes:
            assert not _mentions(stored, code)
        for row in challenges:
            for column in PatientOtpChallenge.__table__.columns:
                assert str(getattr(row, column.name)) not in codes, column.name

    async def test_a_stolen_challenge_table_cannot_be_reversed_without_the_key(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: with a copy of the table, hash all million codes and find the match.

        Against a plain digest this takes about a second — which is why the
        code must be stored under a key the table does not contain.
        """
        phone = new_phone()
        challenge_id, code = await _challenge(net.browser(), sms, phone)
        row = await _challenge_row(db_session, challenge_id)
        assert re.fullmatch(r"[0-9a-f]{64}", row.code_hash)

        prefixes = [b"", (challenge_id + phone).encode(), (phone + challenge_id).encode()]
        recovered = [
            guess
            for guess in (f"{number:06d}".encode() for number in range(10**6))
            for prefix in prefixes
            if hashlib.sha256(prefix + guess).hexdigest() == row.code_hash
        ]

        assert recovered == []
        # The same search does work on an unkeyed digest: the attack is real.
        weak = hashlib.sha256(code.encode()).hexdigest()
        assert any(
            hashlib.sha256(f"{number:06d}".encode()).hexdigest() == weak
            for number in (int(code) - 1, int(code), int(code) + 1)
        )

    async def test_the_stored_hash_depends_on_the_private_key(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: verify a code against a hash computed under a key the attacker chose.

        Seen from the outside: under any other key, the right code is a wrong
        one — so the key is part of what is stored.
        """
        phone = new_phone()
        browser = net.browser()
        challenge_id, code = await _challenge(browser, sms, phone)
        real_key = settings.PATIENT_OTP_SECRET

        for other in ("", "k" * 32, settings.APP_SECRET_KEY.get_secret_value()):
            monkeypatch.setattr(settings, "PATIENT_OTP_SECRET", SecretStr(other))
            refused = await _try(browser, challenge_id, code)
            assert refused.status_code == 401, f"the code verified under key {other[:1]!r}…"
        monkeypatch.setattr(settings, "PATIENT_OTP_SECRET", real_key)

        assert (await _try(browser, challenge_id, code)).status_code == 200

    async def test_every_code_is_six_digits_and_they_are_not_all_the_same(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: predict the next code from the last one."""
        for _ in range(24):
            await _challenge(net.browser(), sms, new_phone())

        codes = [_code_in(message) for message in sms.sent]

        assert all(re.fullmatch(r"\d{6}", code) for code in codes)
        assert len(set(codes)) >= 22
        differences = {(int(b) - int(a)) % 10**6 for a, b in zip(codes, codes[1:], strict=False)}
        assert len(differences) >= 20, "consecutive codes follow a pattern"

    async def test_the_message_says_nothing_but_the_code_and_its_lifetime(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: learn from the text who the patient is or which hospital they use."""
        phone = new_phone()
        _, code = await _challenge(net.browser(), sms, phone)

        [message] = sms.sent

        assert message.to == phone
        assert message.purpose == "otp"
        assert (
            message.body == f"{code} is your Aetheris verification code. It expires in 5 minutes."
        )
        assert code not in repr(message)


# ── 2. A code dies ───────────────────────────────────────────────────────────


class TestACodeDies:
    async def test_it_works_until_five_minutes_and_not_after(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock, db_session: AsyncSession
    ) -> None:
        """Attack: use a code found in an old message."""
        browser = net.browser()
        in_time = await _challenge(browser, sms, new_phone())
        too_late_phone = new_phone()
        too_late = await _challenge(browser, sms, too_late_phone)

        clock.advance(seconds=CODE_LIFETIME.total_seconds() - 2)
        accepted = await _try(browser, *in_time)
        clock.advance(seconds=3)
        refused = await _try(browser, *too_late)

        assert accepted.status_code == 200, accepted.text
        assert refused.status_code == 401
        assert _body(refused) == OTP_INVALID
        assert set_cookie_headers(refused) == []
        assert await _count(db_session, PatientAccount, PatientAccount.phone == too_late_phone) == 0
        # An expired challenge is not even counted against: it is simply dead.
        row = await _challenge_row(db_session, too_late[0])
        assert (row.attempts, row.consumed_at) == (0, None)

    async def test_an_expired_code_stays_dead_however_long_one_waits(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock
    ) -> None:
        browser = net.browser()
        challenge = await _challenge(browser, sms, new_phone())

        for wait in (timedelta(minutes=6), timedelta(hours=1), timedelta(days=2)):
            clock.advance(seconds=wait.total_seconds())
            assert _body(await _try(net.browser(), *challenge)) == OTP_INVALID

    async def test_it_works_once(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: replay a code that was just used — from the same browser and from another."""
        phone = new_phone()
        browser = net.browser()
        challenge = await _challenge(browser, sms, phone)
        assert (await _try(browser, *challenge)).status_code == 200

        replays = [await _try(browser, *challenge), await _try(net.browser(), *challenge)]

        for replay in replays:
            assert replay.status_code == 401
            assert _body(replay) == OTP_INVALID
            assert set_cookie_headers(replay) == []
        assert await _count(db_session, PatientRefreshToken) == 1
        assert len(await audit_rows(db_session, "patient.auth.login")) == 1

    async def test_a_new_code_does_not_bring_an_old_one_back_or_take_it_away(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock
    ) -> None:
        """Attack: ask for a second code to revive an expired one — or to cancel the owner's."""
        phone = new_phone()
        owner = net.browser()
        expired = await _challenge(owner, sms, phone)
        clock.advance(minutes=6)
        live = await _challenge(owner, sms, phone)
        # Somebody else asks for a code for the same number.
        await _challenge(net.browser(), sms, phone)

        assert _body(await _try(owner, *expired)) == OTP_INVALID
        assert (await _try(owner, *live)).status_code == 200

    async def test_five_wrong_guesses_kill_a_challenge_and_the_right_code_is_then_refused(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: guess, and when the guesses run out, try the real code after all."""
        phone = new_phone()
        browser = net.browser()
        challenge_id, code = await _challenge(browser, sms, phone)

        guesses = [
            await _try(browser, challenge_id, _other_code(code, step))
            for step in range(1, ATTEMPTS_PER_CHALLENGE + 1)
        ]
        right_code = await _try(browser, challenge_id, code)
        from_elsewhere = await _try(net.browser(), challenge_id, code)

        for response in (*guesses, right_code, from_elsewhere):
            assert response.status_code == 401
            assert _body(response) == OTP_INVALID
        row = await _challenge_row(db_session, challenge_id)
        assert row.attempts == ATTEMPTS_PER_CHALLENGE
        assert row.consumed_at is None
        assert await _count(db_session, PatientAccount) == 0
        assert await _count(db_session, PatientRefreshToken) == 0
        failures = await audit_rows(db_session, "patient.auth.otp_failed")
        assert sorted(row.context["attempts"] for row in failures if row.context) == [1, 2, 3, 4, 5]

    async def test_the_fifth_attempt_is_still_a_real_one(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Five means five: four wrong guesses leave the owner one to get it right."""
        browser = net.browser()
        challenge_id, code = await _challenge(browser, sms, new_phone())
        for step in range(1, ATTEMPTS_PER_CHALLENGE):
            assert (await _try(browser, challenge_id, _other_code(code, step))).status_code == 401

        assert (await _try(browser, challenge_id, code)).status_code == 200

    async def test_a_dead_challenge_is_not_counted_any_further(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: keep hammering a dead challenge to fill the audit trail or overflow a counter."""
        browser = net.browser()
        challenge_id, code = await _challenge(browser, sms, new_phone())
        for step in range(1, 25):
            await _try(browser, challenge_id, _other_code(code, step))

        assert (await _challenge_row(db_session, challenge_id)).attempts == ATTEMPTS_PER_CHALLENGE
        assert (
            len(await audit_rows(db_session, "patient.auth.otp_failed")) == ATTEMPTS_PER_CHALLENGE
        )

    async def test_killing_ones_own_challenge_does_not_touch_the_owners(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: ask for a code for the victim's number and burn the guesses, to lock them out."""
        phone = new_phone()
        owner = net.browser()
        owners = await _challenge(owner, sms, phone)
        attacker = net.browser()
        attackers_id, unseen = await _challenge(attacker, sms, phone)
        for step in range(1, 11):
            await _try(attacker, attackers_id, _other_code(unseen, step))

        assert (await _try(owner, *owners)).status_code == 200

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"challenge_id": "not-a-uuid", "code": "123456"},
            {"challenge_id": str(uuid.uuid4())},
            {"challenge_id": str(uuid.uuid4()), "code": "12345"},
            {"challenge_id": str(uuid.uuid4()), "code": "1234567"},
            {"challenge_id": str(uuid.uuid4()), "code": "12345a"},
            {"challenge_id": str(uuid.uuid4()), "code": " 123456"},
            {"challenge_id": str(uuid.uuid4()), "code": "123456\n"},
            {"challenge_id": str(uuid.uuid4()), "code": 123456},
            {"challenge_id": str(uuid.uuid4()), "code": ["123456", "654321"]},
            {"challenge_id": str(uuid.uuid4()), "code": "123456", "phone": "+919876543210"},
            {"challenge_id": str(uuid.uuid4()), "code": "123456", "account_id": str(uuid.uuid4())},
        ],
    )
    async def test_a_malformed_verification_is_refused_before_anything_is_looked_at(
        self, net: _Net, db_session: AsyncSession, body: dict[str, Any]
    ) -> None:
        """Attack: several codes in one request, a code of another shape, or a phone of one's choosing."""
        response = await net.browser().post(VERIFY, json=body)

        assert response.status_code == 422, response.text
        assert await _patient_buckets(db_session) == 0


class TestACodeIsExactlyWhatWasSent:
    @pytest.mark.parametrize(
        "zero",
        ["\u0660", "\u06f0", "\u0966", "\uff10"],
        ids=["arabic-indic", "extended-arabic-indic", "devanagari", "fullwidth"],
    )
    async def test_the_code_in_another_script_is_not_the_code(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, zero: str
    ) -> None:
        """Attack: present the code's digits in another script, hoping they are folded to ASCII.

        If they were, every code would have several spellings and the
        five-attempt cap would count spellings, not codes.
        """
        browser = net.browser()
        challenge_id, code = await _challenge(browser, sms, new_phone())
        disguised = "".join(chr(ord(zero) + int(digit)) for digit in code)
        assert disguised.isdigit()
        assert int(disguised) == int(code)

        response = await _try(browser, challenge_id, disguised)

        assert response.status_code in {401, 422}, response.text
        assert set_cookie_headers(response) == []
        assert await _count(db_session, PatientAccount) == 0


# ── 3. A code belongs to one challenge and one phone ─────────────────────────


class TestACodeBelongsToOneChallengeAndOnePhone:
    async def _two(
        self, net: _Net, sms: FakeSmsSender, first_phone: str, second_phone: str
    ) -> tuple[tuple[str, str], tuple[str, str]]:
        """Two live challenges whose codes are different."""
        first = await _challenge(net.browser(), sms, first_phone)
        for _ in range(PAIR_BURST):
            second = await _challenge(net.browser(), sms, second_phone)
            if second[1] != first[1]:
                return first, second
        raise AssertionError("three codes in a row were equal")  # pragma: no cover

    async def test_a_code_does_not_verify_another_challenge_of_the_same_phone(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: the patient asked twice; use the first message's code on the second request."""
        phone = new_phone()
        (first_id, first_code), (second_id, second_code) = await self._two(net, sms, phone, phone)
        browser = net.browser()

        assert _body(await _try(browser, second_id, first_code)) == OTP_INVALID
        assert _body(await _try(browser, first_id, second_code)) == OTP_INVALID
        assert await _count(db_session, PatientAccount) == 0
        # Each still opens with its own.
        assert (await _try(browser, first_id, first_code)).status_code == 200

    async def test_a_code_does_not_verify_a_challenge_of_another_phone(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: the attacker gets a code on their own phone and presents it for the victim's."""
        attacker_phone, victim_phone = new_phone(), new_phone()
        (own_id, own_code), (victim_id, _) = await self._two(net, sms, attacker_phone, victim_phone)
        attacker = net.browser()

        refused = await _try(attacker, victim_id, own_code)

        assert _body(refused) == OTP_INVALID
        assert await _count(db_session, PatientAccount, PatientAccount.phone == victim_phone) == 0
        # And their own code signs them in to their own account, not the victim's.
        accepted = await _try(attacker, own_id, own_code)
        assert accepted.status_code == 200
        [account] = await _all(db_session, PatientAccount)
        assert account.phone == attacker_phone
        assert accepted.json()["data"]["account"]["id"] == str(account.id)

    async def test_a_hash_copied_onto_another_challenge_does_not_verify_there(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: with write access to one column, graft a known code's hash onto the victim's row.

        The stored value binds the challenge id and the phone, so it is good
        for the row it was made for and no other.
        """
        attacker_phone, victim_phone = new_phone(), new_phone()
        (own_id, own_code), (victim_id, _) = await self._two(net, sms, attacker_phone, victim_phone)
        own = await _challenge_row(db_session, own_id)
        await db_session.execute(
            update(PatientOtpChallenge)
            .where(PatientOtpChallenge.id == uuid.UUID(victim_id))
            .values(code_hash=own.code_hash)
        )
        await db_session.commit()

        refused = await _try(net.browser(), victim_id, own_code)

        assert _body(refused) == OTP_INVALID
        assert await _count(db_session, PatientAccount, PatientAccount.phone == victim_phone) == 0

    async def test_a_challenge_pointed_at_another_phone_no_longer_verifies(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: retarget one's own challenge at the victim's number and use one's own code."""
        attacker_phone, victim_phone = new_phone(), new_phone()
        own_id, own_code = await _challenge(net.browser(), sms, attacker_phone)
        await db_session.execute(
            update(PatientOtpChallenge)
            .where(PatientOtpChallenge.id == uuid.UUID(own_id))
            .values(phone=victim_phone)
        )
        await db_session.commit()

        refused = await _try(net.browser(), own_id, own_code)

        assert _body(refused) == OTP_INVALID
        assert await _count(db_session, PatientAccount) == 0

    async def test_the_session_is_for_the_phone_the_code_was_sent_to_and_nothing_else(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: sign in as oneself while an account for the victim's number already exists."""
        victim_phone, attacker_phone = new_phone(), new_phone()
        victim = await _sign_in(net.browser(), sms, victim_phone)

        attacker = await _sign_in(net.browser(), sms, attacker_phone)

        assert attacker["account"]["id"] != victim["account"]["id"]
        assert attacker["account"]["phone_masked"].endswith(attacker_phone[-4:])
        accounts = {row.phone: str(row.id) for row in await _all(db_session, PatientAccount)}
        assert accounts == {
            victim_phone: victim["account"]["id"],
            attacker_phone: attacker["account"]["id"],
        }

    async def test_a_challenge_id_cannot_be_guessed_from_another(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: enumerate challenge ids near one's own to find the victim's."""
        browser = net.browser()
        ids = [uuid.UUID((await _challenge(browser, sms, new_phone()))[0]) for _ in range(8)]

        assert all(identifier.version == 4 for identifier in ids)
        assert len({identifier.int >> 64 for identifier in ids}) == len(ids)


# ── 4. Parallel verification, on real connections ────────────────────────────


class _RealDatabase:
    """A pool of real connections to the test database. Everything is committed."""

    def __init__(self, engine: AsyncEngine, sms: FakeSmsSender) -> None:
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        self.phones: list[str] = []
        self.sources: set[str] = set()
        self.sms = sms
        application = create_app()
        # The application's own request-scoped dependency then opens one real
        # session (one pooled connection) per request, as it does in service.
        application.state.db_session_factory = self.factory
        application.dependency_overrides[get_sms_sender] = lambda: sms
        self.net = _Net(application)

    def phone(self) -> str:
        number = new_phone()
        self.phones.append(number)
        return number

    def browser(self) -> AsyncClient:
        source = _source()
        self.sources.add(source)
        return self.net.browser(source)

    async def challenge(self, phone: str) -> tuple[str, str]:
        return await _challenge(self.browser(), self.sms, phone)

    async def count(self, model: Any, *criteria: Any) -> int:
        async with self.factory() as session:
            return await _count(session, model, *criteria)

    async def challenge_row(self, challenge_id: str) -> PatientOtpChallenge:
        async with self.factory() as session:
            return await _challenge_row(session, challenge_id)

    async def cleanup(self) -> None:
        """Delete every row this test committed: by phone, by account, by throttle key."""
        await self.net.aclose()
        async with self.factory() as session:
            accounts = list(
                (
                    await session.execute(
                        select(PatientAccount.id).where(PatientAccount.phone.in_(self.phones))
                    )
                ).scalars()
            )
            challenges = list(
                (
                    await session.execute(
                        select(PatientOtpChallenge.id).where(
                            PatientOtpChallenge.phone.in_(self.phones)
                        )
                    )
                ).scalars()
            )
            devices = list(
                (
                    await session.execute(
                        select(PatientDevice.id).where(PatientDevice.account_id.in_(accounts))
                    )
                ).scalars()
            )
            keys = {bucket(BucketKind.PT_OTP_SEND_GLOBAL).key_hash}
            for source in self.sources:
                keys.add(bucket(BucketKind.PT_OTP_SEND_SOURCE, source).key_hash)
                keys.add(bucket(BucketKind.PT_OTP_VERIFY_SOURCE, source).key_hash)
                for phone in self.phones:
                    keys.add(bucket(BucketKind.PT_OTP_SEND_PAIR, phone, source).key_hash)
            for phone in self.phones:
                keys.add(bucket(BucketKind.PT_OTP_SEND_PHONE, phone).key_hash)
            for account_id in accounts:
                keys.add(bucket(BucketKind.PT_OTP_SEND_PHONE, "recognised", account_id).key_hash)
            for device_id in devices:
                keys.add(bucket(BucketKind.PT_OTP_SEND_DEVICE, device_id).key_hash)
            await session.execute(
                delete(AuthThrottleBucket).where(AuthThrottleBucket.key_hash.in_(keys))
            )
            await session.execute(
                delete(AuditLog).where(
                    AuditLog.action.like("patient.%"),
                    AuditLog.patient_account_id.in_(accounts) | AuditLog.target_id.in_(challenges),
                )
            )
            await session.execute(
                delete(PatientOtpChallenge).where(PatientOtpChallenge.phone.in_(self.phones))
            )
            # Sessions and recognised devices go with the account (CASCADE).
            await session.execute(
                delete(PatientAccount).where(PatientAccount.phone.in_(self.phones))
            )
            await session.commit()


@pytest_asyncio.fixture
async def real_db(db_engine: AsyncEngine, sms: FakeSmsSender) -> AsyncGenerator[_RealDatabase]:
    """Independent connections with real commits; cleaned up whatever happens."""
    engine = create_async_engine(db_engine.url, pool_size=8, max_overflow=0, pool_timeout=120)
    database = _RealDatabase(engine, sms)
    try:
        yield database
    finally:
        try:
            await database.cleanup()
        finally:
            await engine.dispose()


class TestParallelVerification:
    """Requests that arrive at the same moment, each on its own connection.

    The single-connection ``db_session`` used elsewhere cannot show this: on
    it two requests can never be inside a verification at once.
    """

    async def _failures(self, real_db: _RealDatabase, challenge_id: str) -> int:
        return await real_db.count(
            AuditLog,
            AuditLog.action == "patient.auth.otp_failed",
            AuditLog.target_id == uuid.UUID(challenge_id),
        )

    async def test_the_right_code_presented_many_times_at_once_starts_exactly_one_session(
        self, real_db: _RealDatabase
    ) -> None:
        """Attack: replay an intercepted code in the same instant the owner submits it."""
        phone = real_db.phone()
        challenge_id, code = await real_db.challenge(phone)
        racers = [real_db.browser() for _ in range(16)]

        responses = await asyncio.gather(*(_try(racer, challenge_id, code) for racer in racers))

        assert sorted(r.status_code for r in responses) == [200] + [401] * 15
        assert all(_body(r) == OTP_INVALID for r in responses if r.status_code == 401)
        assert all(set_cookie_headers(r) == [] for r in responses if r.status_code == 401)
        [account] = await self._accounts(real_db, phone)
        assert (
            await real_db.count(PatientRefreshToken, PatientRefreshToken.account_id == account.id)
            == 1
        )
        assert await real_db.count(PatientDevice, PatientDevice.account_id == account.id) == 1
        for action in ("patient.auth.login", "patient.auth.account_created"):
            assert (
                await real_db.count(
                    AuditLog, AuditLog.action == action, AuditLog.patient_account_id == account.id
                )
                == 1
            )
        assert (await real_db.challenge_row(challenge_id)).consumed_at is not None

    async def _accounts(self, real_db: _RealDatabase, phone: str) -> list[PatientAccount]:
        async with real_db.factory() as session:
            return await _all(session, PatientAccount, PatientAccount.phone == phone)

    async def test_parallel_guesses_are_not_evaluated_beyond_five(
        self, real_db: _RealDatabase
    ) -> None:
        """Attack: fire forty guesses at once so they all pass the count before any is counted."""
        phone = real_db.phone()
        challenge_id, code = await real_db.challenge(phone)
        guesses = [_other_code(code, step) for step in range(1, 41)]
        attackers = [real_db.browser() for _ in guesses]

        responses = await asyncio.gather(
            *(
                _try(attacker, challenge_id, guess)
                for attacker, guess in zip(attackers, guesses, strict=True)
            )
        )

        assert [r.status_code for r in responses] == [401] * 40
        assert all(_body(r) == OTP_INVALID for r in responses)
        assert (await real_db.challenge_row(challenge_id)).attempts == ATTEMPTS_PER_CHALLENGE
        # Exactly five guesses were compared with the code — not fewer either.
        assert await self._failures(real_db, challenge_id) == ATTEMPTS_PER_CHALLENGE

        # And the right code, alone or in a crowd, is refused afterwards.
        after = await asyncio.gather(
            *(_try(real_db.browser(), challenge_id, code) for _ in range(6))
        )
        assert [r.status_code for r in after] == [401] * 6
        assert await self._accounts(real_db, phone) == []

    async def test_a_second_parallel_wave_finds_the_first_one_counted(
        self, real_db: _RealDatabase
    ) -> None:
        """Attack: repeat the burst. The first wave's count is durable; nothing is left."""
        phone = real_db.phone()
        challenge_id, code = await real_db.challenge(phone)

        for wave in range(3):
            await asyncio.gather(
                *(
                    _try(real_db.browser(), challenge_id, _other_code(code, 1 + wave * 10 + step))
                    for step in range(10)
                )
            )

        assert (await real_db.challenge_row(challenge_id)).attempts == ATTEMPTS_PER_CHALLENGE
        assert await self._failures(real_db, challenge_id) == ATTEMPTS_PER_CHALLENGE

    async def test_the_right_code_in_a_crowd_of_wrong_ones_starts_at_most_one_session(
        self, real_db: _RealDatabase
    ) -> None:
        """Attack: hide many guesses around the moment the owner types the code."""
        phone = real_db.phone()
        challenge_id, code = await real_db.challenge(phone)
        presented = [_other_code(code, step) for step in range(1, 20)] + [code, code, code]

        responses = await asyncio.gather(
            *(_try(real_db.browser(), challenge_id, value) for value in presented)
        )

        sessions = sum(r.status_code == 200 for r in responses)
        assert sessions <= 1
        row = await real_db.challenge_row(challenge_id)
        assert row.attempts <= ATTEMPTS_PER_CHALLENGE
        assert await self._failures(real_db, challenge_id) <= ATTEMPTS_PER_CHALLENGE
        assert await self._failures(real_db, challenge_id) + sessions <= ATTEMPTS_PER_CHALLENGE
        assert len(await self._accounts(real_db, phone)) == sessions

    async def test_two_first_sign_ins_at_once_make_one_account(
        self, real_db: _RealDatabase
    ) -> None:
        """Attack: race two first sign-ins of one number to get two accounts for it."""
        phone = real_db.phone()
        challenges = [await real_db.challenge(phone) for _ in range(3)]

        responses = await asyncio.gather(
            *(_try(real_db.browser(), *challenge) for challenge in challenges)
        )

        assert [r.status_code for r in responses] == [200] * 3
        assert len({r.json()["data"]["account"]["id"] for r in responses}) == 1
        [account] = await self._accounts(real_db, phone)
        assert (
            await real_db.count(
                AuditLog,
                AuditLog.action == "patient.auth.account_created",
                AuditLog.patient_account_id == account.id,
            )
            == 1
        )

    async def test_parallel_requests_for_one_number_do_not_send_more_than_the_allowance(
        self, real_db: _RealDatabase
    ) -> None:
        """Attack: fire thirty requests at once from thirty addresses to text one number thirty times."""
        phone = real_db.phone()
        attackers = [real_db.browser() for _ in range(30)]

        responses = await asyncio.gather(*(_ask(attacker, phone) for attacker in attackers))

        statuses = [r.status_code for r in responses]
        assert statuses.count(202) == PHONE_BURST
        assert statuses.count(429) == 30 - PHONE_BURST
        assert len([m for m in real_db.sms.sent if m.to == phone]) == PHONE_BURST
        assert (
            await real_db.count(PatientOtpChallenge, PatientOtpChallenge.phone == phone)
            == PHONE_BURST
        )

    async def test_parallel_requests_from_one_source_do_not_send_more_than_the_allowance(
        self, real_db: _RealDatabase
    ) -> None:
        """Attack: the same race against one address's allowance for one number."""
        phone = real_db.phone()
        source = _source()
        real_db.sources.add(source)
        attackers = [real_db.net.browser(source) for _ in range(20)]

        responses = await asyncio.gather(*(_ask(attacker, phone) for attacker in attackers))

        assert [r.status_code for r in responses].count(202) == PAIR_BURST
        assert len([m for m in real_db.sms.sent if m.to == phone]) == PAIR_BURST


# ── 5. One source ────────────────────────────────────────────────────────────


class TestOneSourceIsThrottled:
    async def test_one_source_gets_three_codes_for_a_number_and_then_one_per_ten_minutes(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock, db_session: AsyncSession
    ) -> None:
        """Attack: text the victim over and over from one address."""
        phone = new_phone()
        attacker = net.browser(_source())
        for _ in range(PAIR_BURST):
            assert (await _ask(attacker, phone)).status_code == 202

        refused = [await _ask(attacker, phone) for _ in range(20)]

        for response in refused:
            assert response.status_code == 429
            assert _body(response) == OTP_THROTTLED
            assert response.headers["retry-after"] == "600"
        assert len(sms.sent) == PAIR_BURST
        assert await _count(db_session, PatientOtpChallenge) == PAIR_BURST

        # A refusal costs nothing and lengthens nothing: one refill later,
        # exactly one more code — after twenty refused requests.
        clock.advance(seconds=PAIR_REFILL.total_seconds() - 5)
        assert (await _ask(attacker, phone)).status_code == 429
        clock.advance(seconds=10)
        assert (await _ask(attacker, phone)).status_code == 202
        assert (await _ask(attacker, phone)).status_code == 429
        assert len(sms.sent) == PAIR_BURST + 1

    async def test_one_source_cannot_text_more_than_thirty_numbers(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock, db_session: AsyncSession
    ) -> None:
        """Attack (SMS pumping): one address asks for a code for number after number."""
        source = _source()
        attacker = net.browser(source)
        for _ in range(SOURCE_BURST):
            assert (await _ask(attacker, new_phone())).status_code == 202

        refused = [await _ask(attacker, new_phone()) for _ in range(10)]

        assert [r.status_code for r in refused] == [429] * 10
        assert all(_body(r) == OTP_THROTTLED for r in refused)
        assert len(sms.sent) == SOURCE_BURST
        assert await _count(db_session, PatientOtpChallenge) == SOURCE_BURST
        # A new browser at the same address is the same source.
        assert (await _ask(net.browser(source), new_phone())).status_code == 429
        # Somebody at another address is not affected.
        assert (await _ask(net.browser(), new_phone())).status_code == 202
        # One refill buys one more, not a new burst.
        clock.advance(seconds=SOURCE_REFILL.total_seconds() + 1)
        assert (await _ask(attacker, new_phone())).status_code == 202
        assert (await _ask(attacker, new_phone())).status_code == 429

    async def test_a_forwarded_for_the_client_wrote_does_not_make_a_new_source(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: send a different ``X-Forwarded-For`` each time; the proxy appends the real one."""
        phone = new_phone()
        real = _source()
        attacker = net.browser(real)
        statuses = []
        for _ in range(PAIR_BURST + 5):
            response = await attacker.post(
                REQUEST, json={"phone": phone}, headers={"X-Forwarded-For": f"{_source()}, {real}"}
            )
            statuses.append(response.status_code)

        assert statuses == [202] * PAIR_BURST + [429] * 5
        assert len(sms.sent) == PAIR_BURST

    async def test_an_address_that_reaches_the_application_directly_cannot_name_its_own_source(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: bypass the proxy and claim a new address in the header on every request."""
        phone = new_phone()
        direct = ("198.51.100.77", 5000)  # not one of our proxies
        statuses = []
        for _ in range(PAIR_BURST + 3):
            response = await net.browser(_source(), peer=direct).post(
                REQUEST, json={"phone": phone}
            )
            statuses.append(response.status_code)

        assert statuses == [202] * PAIR_BURST + [429] * 3

    async def test_one_ipv6_site_is_one_source(self, net: _Net, sms: FakeSmsSender) -> None:
        """Attack: rotate through the addresses of one /64, of which there are 2^64."""
        phone = new_phone()
        prefix = f"2001:db8:{secrets.randbelow(65536):x}:{secrets.randbelow(65536):x}"
        statuses = [
            (await _ask(net.browser(f"{prefix}::{host:x}"), phone)).status_code
            for host in range(1, PAIR_BURST + 4)
        ]

        assert statuses == [202] * PAIR_BURST + [429] * 3

    async def test_hiding_the_source_does_not_escape_the_numbers_budget(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: arrive with no address at all, so that no per-source bucket applies."""
        phone = new_phone()
        statuses = []
        for _ in range(PHONE_BURST + 3):
            nowhere = net.browser(peer=("not-an-address", 0), forwarded=False)
            statuses.append((await _ask(nowhere, phone)).status_code)

        assert statuses == [202] * PHONE_BURST + [429] * 3
        assert len(sms.sent) == PHONE_BURST
        # Callers with no address do not share a source bucket between them.
        kinds = {row.kind for row in await _all(db_session, AuthThrottleBucket)}
        assert kinds == {"pt_otp_send_phone", "pt_otp_send_global"}

    async def test_one_source_gets_sixty_verifications_and_then_even_the_right_code_is_refused(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock, db_session: AsyncSession
    ) -> None:
        """Attack: one address works through challenge after challenge."""
        source = _source()
        attacker = net.browser(source)
        phone = new_phone()
        challenge_id, code = await _challenge(attacker, sms, phone)
        for _ in range(VERIFY_SOURCE_BURST):
            assert _body(await _try(attacker, str(uuid.uuid4()), "000000")) == OTP_INVALID

        throttled = await _try(attacker, challenge_id, code)

        # Refused exactly like a wrong code — and not looked at: the challenge
        # is uncounted and still alive.
        assert throttled.status_code == 401
        assert _body(throttled) == OTP_INVALID
        assert "retry-after" not in throttled.headers
        row = await _challenge_row(db_session, challenge_id)
        assert (row.attempts, row.consumed_at) == (0, None)
        assert await _count(db_session, PatientAccount) == 0
        # The owner, somewhere else, is not affected by that address's guessing.
        assert (await _try(net.browser(), challenge_id, code)).status_code == 200

        # One refill, one more verification from that address.
        clock.advance(seconds=VERIFY_SOURCE_REFILL.total_seconds() + 1)
        assert _body(await _try(attacker, str(uuid.uuid4()), "000000")) == OTP_INVALID
        second_id, second_code = await _challenge(attacker, sms, new_phone())
        assert _body(await _try(attacker, second_id, second_code)) == OTP_INVALID
        assert (await _challenge_row(db_session, second_id)).attempts == 0

    async def test_signing_in_does_not_use_up_a_shared_addresss_verifications(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """A waiting room behind one address: a sign-in gives its charge back, a guess does not.

        Thirty patients sign in from one address. If those thirty counted, the
        address would have thirty verifications left; it has all sixty —
        fifty-nine guesses, and the sixtieth is still looked at.
        """
        source = _source()
        for _ in range(SOURCE_BURST):
            await _sign_in(net.browser(source), sms, new_phone())
        # Asked for from elsewhere: this address has used its allowance of codes.
        challenge_id, code = await _challenge(net.browser(), sms, new_phone())
        attacker = net.browser(source)

        for _ in range(VERIFY_SOURCE_BURST - 1):
            assert _body(await _try(attacker, str(uuid.uuid4()), "000000")) == OTP_INVALID
        sixtieth = await _try(attacker, challenge_id, code)
        another_id, another_code = await _challenge(net.browser(), sms, new_phone())
        sixty_first = await _try(attacker, another_id, another_code)

        assert sixtieth.status_code == 200
        # That success gave its charge back too, so one more is looked at —
        # and the guesses are still all counted: the one after is not.
        assert sixty_first.status_code == 200
        for _ in range(2):
            await _try(attacker, str(uuid.uuid4()), "000000")
        last_id, last_code = await _challenge(net.browser(), sms, new_phone())
        assert _body(await _try(attacker, last_id, last_code)) == OTP_INVALID


# ── 6. One number, many sources ──────────────────────────────────────────────


class TestOneNumberFromManySources:
    async def test_a_number_gets_six_codes_from_everywhere_and_then_one_per_five_minutes(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock, db_session: AsyncSession
    ) -> None:
        """Attack: flood the victim's phone with texts from a different address each time."""
        phone = new_phone()
        statuses = [(await _ask(net.browser(), phone)).status_code for _ in range(PHONE_BURST + 14)]

        assert statuses == [202] * PHONE_BURST + [429] * 14
        assert len(sms.sent) == PHONE_BURST
        assert await _count(db_session, PatientOtpChallenge) == PHONE_BURST

        clock.advance(seconds=PHONE_REFILL.total_seconds() + 1)
        again = [(await _ask(net.browser(), phone)).status_code for _ in range(5)]
        assert again == [202, 429, 429, 429, 429]
        # A day of it: at most 288 more texts, one per refill, never a burst
        # for having waited.
        clock.advance(hours=24)
        after_a_day = [
            (await _ask(net.browser(), phone)).status_code for _ in range(PHONE_BURST + 4)
        ]
        assert after_a_day == [202] * PHONE_BURST + [429] * 4

    async def test_the_refusal_is_the_same_whichever_limit_was_reached(
        self, net: _Net, sms: FakeSmsSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: tell from the refusal which limit it was, and so where to attack next."""
        # The number's budget, from fresh addresses.
        flooded = new_phone()
        for _ in range(PHONE_BURST):
            await _ask(net.browser(), flooded)
        by_phone = await _ask(net.browser(), flooded)
        # One address's allowance for one number.
        one = net.browser()
        pair_phone = new_phone()
        for _ in range(PAIR_BURST):
            await _ask(one, pair_phone)
        by_pair = await _ask(one, pair_phone)
        # One address's allowance for all numbers.
        many = net.browser()
        for _ in range(SOURCE_BURST):
            await _ask(many, new_phone())
        by_source = await _ask(many, new_phone())
        # The platform's ceiling.
        monkeypatch.setitem(
            throttle_module.POLICIES,
            BucketKind.PT_OTP_SEND_GLOBAL,
            Budget(burst=1, refill=timedelta(hours=1)),
        )
        by_platform = await _ask(net.browser(), new_phone())

        refusals = [by_phone, by_pair, by_source, by_platform]
        assert [r.status_code for r in refusals] == [429] * 4
        assert all(_body(r) == OTP_THROTTLED for r in refusals)
        assert all(r.headers["retry-after"] == "600" for r in refusals)
        assert len({json.dumps(_stable_headers(r), sort_keys=True) for r in refusals}) == 1
        assert all(set_cookie_headers(r) == [] for r in refusals)

    async def test_flooding_one_number_does_not_touch_another(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        victim, bystander = new_phone(), new_phone()
        for _ in range(PHONE_BURST + 5):
            await _ask(net.browser(), victim)

        assert (await _ask(net.browser(), bystander)).status_code == 202

    async def test_the_platform_ceiling_stops_everybody_and_raises_an_alert(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        all_logs: _LogTrap,
    ) -> None:
        """Attack (SMS pumping at scale): many addresses, many numbers, each within its own limits.

        The ceiling is 600 codes with one more each second; it is lowered here
        so the test does not have to send 600 messages, and the documented
        number is asserted alongside.
        """
        policy = throttle_module.POLICIES[BucketKind.PT_OTP_SEND_GLOBAL]
        assert policy == Budget(burst=GLOBAL_BURST, refill=timedelta(seconds=1))
        monkeypatch.setitem(
            throttle_module.POLICIES,
            BucketKind.PT_OTP_SEND_GLOBAL,
            Budget(burst=4, refill=timedelta(hours=1)),
        )
        owner_phone = new_phone()
        owner = net.browser()
        await _sign_in(owner, sms, owner_phone)  # the first of the four

        statuses = [(await _ask(net.browser(), new_phone())).status_code for _ in range(8)]

        assert statuses == [202] * 3 + [429] * 5
        assert len(sms.sent) == 4
        # A recognised device is not exempt from the ceiling: it caps the bill.
        assert (await _ask(owner, owner_phone)).status_code == 429
        assert "patient_otp_global_ceiling_reached" in all_logs.everything
        assert await _count(db_session, PatientOtpChallenge) == 4

    async def test_a_distributed_guesser_gets_thirty_guesses_at_a_number_and_no_more(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: from many addresses, ask for codes for the victim's number and guess at each.

        There is no lock on a number. What bounds the guessing is five
        attempts a challenge, times the challenges the number's budget allows.
        """
        phone = new_phone()
        evaluated = 0
        for _ in range(PHONE_BURST + 6):
            attacker = net.browser()
            response = await _ask(attacker, phone)
            if response.status_code != 202:
                continue
            challenge_id = response.json()["data"]["challenge_id"]
            code = _code_in(sms.sent[-1])
            for step in range(1, 9):
                await _try(net.browser(), challenge_id, _other_code(code, step))

        evaluated = len(await audit_rows(db_session, "patient.auth.otp_failed"))

        assert evaluated == PHONE_BURST * ATTEMPTS_PER_CHALLENGE == 30
        assert await _count(db_session, PatientAccount) == 0


# ── 7. The recognised device ─────────────────────────────────────────────────


class TestTheOwnerIsNotLockedOut:
    async def _flood(self, net: _Net, phone: str) -> None:
        """Use up a number's shared budget from as many addresses as it takes."""
        for _ in range(PHONE_BURST):
            await _ask(net.browser(), phone)
        assert (await _ask(net.browser(), phone)).status_code == 429

    async def test_a_recognised_device_still_gets_a_code_while_the_number_is_flooded(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: knowing only the number, use up its allowance so its owner cannot sign in."""
        phone = new_phone()
        owner = net.browser()
        await _sign_in(owner, sms, phone)
        await owner.post(LOGOUT, headers=CSRF)
        await self._flood(net, phone)

        # The owner's browser, from an address nobody has seen before.
        response = await owner.post(
            REQUEST, json={"phone": phone}, headers={"X-Forwarded-For": _source()}
        )

        assert response.status_code == 202, response.text
        assert sms.sent[-1].to == phone
        signed_in = await _try(
            owner, response.json()["data"]["challenge_id"], _code_in(sms.sent[-1])
        )
        assert signed_in.status_code == 200
        # A stranger is still refused: the exemption is the device's, not the number's.
        assert (await _ask(net.browser(), phone)).status_code == 429
        rows = await audit_rows(db_session, "patient.auth.otp_requested")
        assert [row.context["recognised_device"] for row in rows if row.context].count(True) == 1

    async def test_an_attacker_at_one_address_cannot_stop_the_owner_at_another(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: hammer the number from one address. The owner has no recognised device."""
        phone = new_phone()
        attacker = net.browser(_source())
        statuses = [(await _ask(attacker, phone)).status_code for _ in range(60)]
        assert statuses.count(202) == PAIR_BURST

        # A browser the owner has never signed in from, at another address.
        owner = net.browser()
        response = await _ask(owner, phone)

        assert response.status_code == 202, response.text
        signed_in = await _try(
            owner, response.json()["data"]["challenge_id"], _code_in(sms.sent[-1])
        )
        assert signed_in.status_code == 200
        # The 57 refused requests cost the number nothing: it had six, the
        # attacker got three, and the remaining three are all still there.
        others = [(await _ask(net.browser(), phone)).status_code for _ in range(4)]
        assert others == [202, 202, 429, 429]

    async def test_a_made_up_device_cookie_is_not_adopted(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: plant a device cookie in the victim's browser, then use the same value oneself."""
        phone = new_phone()
        planted = secrets.token_urlsafe(32)
        victim = net.browser(cookie=planted)
        await _sign_in(victim, sms, phone)
        issued = _held(victim)

        assert issued != planted
        assert planted not in issued.split(".")
        hashes = {row.token_hash for row in await _all(db_session, PatientDevice)}
        assert hashes == {hash_token(issued)}

        await self._flood(net, phone)
        attacker = net.browser(cookie=planted)
        assert (await _ask(attacker, phone)).status_code == 429

    async def test_asking_for_a_code_never_recognises_a_browser(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: become a recognised device of the victim's number without ever having a code."""
        phone = new_phone()
        await _sign_in(net.browser(), sms, phone)
        attacker = net.browser(cookie=secrets.token_urlsafe(32))

        asked = await _ask(attacker, phone)
        guessed = await _try(
            attacker, asked.json()["data"]["challenge_id"], _other_code(_code_in(sms.sent[-1]))
        )

        assert asked.status_code == 202
        assert guessed.status_code == 401
        assert set_cookie_headers(asked) == []
        assert set_cookie_headers(guessed) == []
        assert await _count(db_session, PatientDevice) == 1

    async def test_a_device_recognised_for_one_number_is_nothing_for_another(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: the attacker's own, genuinely recognised device asks for the victim's codes."""
        victim_phone, attacker_phone = new_phone(), new_phone()
        await _sign_in(net.browser(), sms, victim_phone)  # the victim has an account
        attacker = net.browser()
        await _sign_in(attacker, sms, attacker_phone)
        await self._flood(net, victim_phone)

        refused = await attacker.post(
            REQUEST, json={"phone": victim_phone}, headers={"X-Forwarded-For": _source()}
        )
        own = await attacker.post(
            REQUEST, json={"phone": attacker_phone}, headers={"X-Forwarded-For": _source()}
        )

        assert refused.status_code == 429
        assert _body(refused) == OTP_THROTTLED
        assert own.status_code == 202
        # Recognised once only: for its own number.
        rows = await audit_rows(db_session, "patient.auth.otp_requested")
        assert [row.context["recognised_device"] for row in rows if row.context].count(True) == 1

    async def test_a_copied_device_cookie_buys_three_codes_and_no_session(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock, db_session: AsyncSession
    ) -> None:
        """Attack: steal the device cookie. It is not a credential and it has its own small budget."""
        phone = new_phone()
        owner = net.browser()
        session = await _sign_in(owner, sms, phone)
        stolen = _held(owner)
        sent = len(sms.sent)

        statuses = [
            (await _ask(net.browser(cookie=stolen), phone)).status_code
            for _ in range(DEVICE_BURST + 4)
        ]

        assert statuses == [202] * DEVICE_BURST + [429] * 4
        assert len(sms.sent) == sent + DEVICE_BURST
        clock.advance(seconds=DEVICE_REFILL.total_seconds() + 1)
        assert (await _ask(net.browser(cookie=stolen), phone)).status_code == 202
        assert (await _ask(net.browser(cookie=stolen), phone)).status_code == 429

        thief = net.browser(cookie=stolen)
        assert (await thief.get(ME)).status_code == 401
        assert (await thief.get(ME, headers=bearer(stolen))).status_code == 401
        as_refresh = await thief.post(
            REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={stolen}"}
        )
        assert as_refresh.status_code == 401
        assert await _count(db_session, PatientRefreshToken) == 1
        assert (await owner.get(ME, headers=bearer(session["access_token"]))).status_code == 200

    async def test_logging_out_everywhere_forgets_the_device(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: keep using a stolen device cookie after the owner has logged out everywhere."""
        phone = new_phone()
        owner = net.browser()
        session = await _sign_in(owner, sms, phone)
        stolen = _held(owner)
        assert (
            await owner.post(LOGOUT_ALL, headers=bearer(session["access_token"]))
        ).status_code == 204
        await self._flood(net, phone)

        assert (await _ask(net.browser(cookie=stolen), phone)).status_code == 429

    async def test_recognition_lapses_when_the_device_has_not_signed_in_for_ninety_days(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock
    ) -> None:
        phone = new_phone()
        owner = net.browser()
        await _sign_in(owner, sms, phone)
        clock.advance(days=91)
        await self._flood(net, phone)

        assert (await _ask(net.browser(cookie=_held(owner)), phone)).status_code == 429

    @pytest.mark.parametrize(
        "cookie",
        [
            "",
            ".",
            "." * 500,
            "A" * 31,
            "A" * 65,
            "../../etc/passwd",
            "'; DROP TABLE patient_devices; --",
            ".".join(["A" * 43] * 500),
            "%00" * 20,
        ],
    )
    async def test_a_malformed_device_cookie_changes_nothing(
        self, net: _Net, sms: FakeSmsSender, cookie: str
    ) -> None:
        """Attack: break the request, or the lookup, with a device cookie of one's own making."""
        phone = new_phone()
        browser = net.browser(cookie=cookie)

        data = await _sign_in(browser, sms, phone)

        assert data["account"]["phone_masked"].endswith(phone[-4:])
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", _held(browser))


# ── 8. No enumeration ────────────────────────────────────────────────────────


class _Observation:
    """Everything one request did that somebody could tell apart from another."""

    TABLES = (
        PatientAccount,
        PatientOtpChallenge,
        PatientRefreshToken,
        PatientDevice,
        AuthThrottleBucket,
        AuditLog,
        User,
    )

    def __init__(self, session: AsyncSession, statements: list[str]) -> None:
        self._session = session
        self._statements = statements
        self._mark = 0
        self._before: list[int] = []
        self._audited: set[uuid.UUID] = set()

    async def start(self) -> None:
        self._before = [await _count(self._session, model) for model in self.TABLES]
        self._audited = {row.id for row in await _all(self._session, AuditLog)}
        self._mark = len(self._statements)

    async def finish(self, response: Response) -> dict[str, Any]:
        executed = self._statements[self._mark :]
        after = [await _count(self._session, model) for model in self.TABLES]
        new_audit = sorted(
            (row for row in await _all(self._session, AuditLog) if row.id not in self._audited),
            key=lambda row: (row.created_at, row.action),
        )
        body = _body(response) if response.content else None
        if body and isinstance(body.get("data"), dict):
            body = {**body, "data": _shape(body["data"])}
        return {
            "status": response.status_code,
            "body": body,
            "headers": _stable_headers(response),
            "cookies": sorted(h.split("=", 1)[0] for h in set_cookie_headers(response)),
            "rows_written": {
                model.__tablename__: now - then
                for model, then, now in zip(self.TABLES, self._before, after, strict=True)
            },
            "audit": [
                (
                    row.action,
                    row.actor_type,
                    row.hospital_id,
                    row.actor_user_id,
                    row.patient_account_id is None,
                    row.target_type,
                    sorted(row.context or {}),
                    {
                        key: value
                        for key, value in (row.context or {}).items()
                        if key != "phone_ref"
                    },
                )
                for row in new_audit
            ],
            "statements": executed,
        }


def _shape(data: dict[str, Any]) -> dict[str, Any]:
    """Response data with the values that are random by design replaced by their type."""
    random_by_design = {"challenge_id", "access_token", "id", "phone_masked"}
    return {
        key: (
            _shape(value)
            if isinstance(value, dict)
            else type(value).__name__
            if key in random_by_design
            else value
        )
        for key, value in sorted(data.items())
    }


class TestNoEnumeration:
    """Whether a number has an account must not be observable by somebody who cannot read its texts."""

    async def _accounts(
        self, net: _Net, sms: FakeSmsSender, session: AsyncSession
    ) -> dict[str, str]:
        """A number with no account, and numbers whose accounts are active, suspended and closed.

        The number with no account has been sent a code before, like the
        others — somebody typed it once and went no further — so that the only
        difference between the four is the account.
        """
        phones = {name: new_phone() for name in ("unknown", "active", "suspended", "closed")}
        await _challenge(net.browser(), sms, phones["unknown"])
        for name in ("active", "suspended", "closed"):
            await _sign_in(net.browser(), sms, phones[name])
        for name in ("suspended", "closed"):
            await session.execute(
                update(PatientAccount)
                .where(PatientAccount.phone == phones[name])
                .values(status=name)
            )
        await session.commit()
        return phones

    async def test_asking_for_a_code_is_identical_for_a_number_with_and_without_an_account(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        statements: list[str],
        no_sweeps: None,
    ) -> None:
        """Attack: ask for a code for a number and see from the answer whether it is a patient."""
        phones = await self._accounts(net, sms, db_session)
        seen: dict[str, dict[str, Any]] = {}
        for name, phone in phones.items():
            stranger = net.browser()
            watch = _Observation(db_session, statements)
            await watch.start()
            response = await _ask(stranger, phone)
            seen[name] = await watch.finish(response)
            assert sms.sent[-1].to == phone

        baseline = seen["unknown"]
        assert baseline["status"] == 202
        assert baseline["body"]["data"] == {
            "challenge_id": "str",
            "expires_in": 300,
            "resend_after": 60,
        }
        assert baseline["cookies"] == []
        assert baseline["rows_written"] == {
            "patient_accounts": 0,
            "patient_otp_challenges": 1,
            "patient_refresh_tokens": 0,
            "patient_devices": 0,
            # The stranger's own address: its bucket, and its bucket for this number.
            "auth_throttle_buckets": 2,
            "audit_logs": 1,
            "users": 0,
        }
        assert [entry[0] for entry in baseline["audit"]] == ["patient.auth.otp_requested"]
        assert baseline["audit"][0][4] is True  # no account is named, even when one exists
        assert len(baseline["statements"]) > 5
        # No statement of the request reads the accounts table at all.
        assert not any("patient_accounts" in statement for statement in baseline["statements"])
        for name in ("active", "suspended", "closed"):
            for aspect, expected in baseline.items():
                assert seen[name][aspect] == expected, (name, aspect)

    async def test_it_is_identical_when_the_browser_presents_somebody_elses_device_cookie(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        statements: list[str],
        no_sweeps: None,
    ) -> None:
        """Attack: use the device lookup — the one place an account is joined — as an oracle."""
        phones = await self._accounts(net, sms, db_session)
        attacker = net.browser()
        await _sign_in(attacker, sms, new_phone())
        cookie = _held(attacker)
        seen: dict[str, dict[str, Any]] = {}
        for name, phone in phones.items():
            watch = _Observation(db_session, statements)
            await watch.start()
            response = await _ask(net.browser(cookie=cookie), phone)
            seen[name] = await watch.finish(response)

        baseline = seen["unknown"]
        assert baseline["status"] == 202
        # The lookup ran — the same lookup — for the number with no account.
        assert any("patient_devices" in statement for statement in baseline["statements"])
        assert baseline["audit"][0][7] == {"recognised_device": False}
        for name in ("active", "suspended", "closed"):
            for aspect, expected in baseline.items():
                assert seen[name][aspect] == expected, (name, aspect)

    async def test_a_wrong_code_is_identical_for_a_number_with_and_without_an_account(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        statements: list[str],
        no_sweeps: None,
    ) -> None:
        """Attack: guess at a challenge and see from the refusal whether the number is a patient."""
        phones = await self._accounts(net, sms, db_session)
        seen: dict[str, dict[str, Any]] = {}
        for name, phone in phones.items():
            stranger = net.browser()
            challenge_id, code = await _challenge(stranger, sms, phone)
            watch = _Observation(db_session, statements)
            await watch.start()
            response = await _try(stranger, challenge_id, _other_code(code))
            seen[name] = await watch.finish(response)

        baseline = seen["unknown"]
        assert baseline["status"] == 401
        assert baseline["body"] == OTP_INVALID
        assert baseline["cookies"] == []
        assert baseline["rows_written"]["audit_logs"] == 1
        assert baseline["rows_written"]["patient_accounts"] == 0
        assert [entry[0] for entry in baseline["audit"]] == ["patient.auth.otp_failed"]
        assert baseline["audit"][0][4] is True
        assert not any("patient_accounts" in statement for statement in baseline["statements"])
        for name in ("active", "suspended", "closed"):
            for aspect, expected in baseline.items():
                assert seen[name][aspect] == expected, (name, aspect)

    async def test_a_right_code_answers_the_same_for_a_new_and_an_existing_account(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """The owner of a number may sign in; the answer does not say whether they had before."""
        phones = await self._accounts(net, sms, db_session)
        answers = {}
        for name in ("unknown", "active"):
            browser = net.browser()
            challenge_id, code = await _challenge(browser, sms, phones[name])
            response = await _try(browser, challenge_id, code)
            assert response.status_code == 200
            answers[name] = (
                _shape(response.json()["data"]),
                response.json()["message"],
                sorted(h.split("=", 1)[0] for h in set_cookie_headers(response)),
                sorted(_stable_headers(response)),
            )

        assert answers["unknown"] == answers["active"]
        assert answers["unknown"][0]["account"]["status"] == "active"

    @pytest.mark.parametrize("status", ["suspended", "closed"])
    async def test_the_owner_of_an_account_that_is_not_active_is_refused_like_a_wrong_code(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, status: str
    ) -> None:
        """Attack: learn that an account is suspended from how the right code is refused."""
        phones = await self._accounts(net, sms, db_session)
        browser = net.browser()
        challenge_id, code = await _challenge(browser, sms, phones[status])

        other_id, other_code = await _challenge(net.browser(), sms, phones["unknown"])

        refused = await _try(browser, challenge_id, code)
        wrong = await _try(net.browser(), other_id, _other_code(other_code))

        assert refused.status_code == 401
        assert _body(refused) == OTP_INVALID == _body(wrong)
        assert _stable_headers(refused) == _stable_headers(wrong)
        assert set_cookie_headers(refused) == []
        # No session was started for it: only the three from before it was suspended.
        assert await _count(db_session, PatientRefreshToken) == 3

    async def test_every_refusal_is_one_answer(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock, db_session: AsyncSession
    ) -> None:
        """Attack: tell an unknown challenge from a used, expired, exhausted or throttled one."""
        browser = net.browser()
        used = await _challenge(browser, sms, new_phone())
        await _try(browser, *used)
        exhausted = await _challenge(browser, sms, new_phone())
        for step in range(1, ATTEMPTS_PER_CHALLENGE + 1):
            await _try(browser, exhausted[0], _other_code(exhausted[1], step))
        expired = await _challenge(browser, sms, new_phone())
        clock.advance(minutes=6)
        live = await _challenge(browser, sms, new_phone())
        throttled_source = net.browser()
        for _ in range(VERIFY_SOURCE_BURST):
            await _try(throttled_source, str(uuid.uuid4()), "000000")

        refusals = {
            "unknown challenge": await _try(browser, str(uuid.uuid4()), "123456"),
            "wrong code": await _try(browser, live[0], _other_code(live[1])),
            "used": await _try(browser, *used),
            "exhausted": await _try(browser, *exhausted),
            "expired": await _try(browser, *expired),
            "throttled source": await _try(throttled_source, *live),
        }

        for cause, response in refusals.items():
            assert response.status_code == 401, cause
            assert _body(response) == OTP_INVALID, cause
            assert set_cookie_headers(response) == [], cause
        assert len({json.dumps(_stable_headers(r), sort_keys=True) for r in refusals.values()}) == 1

    async def test_every_refusal_is_held_to_the_same_minimum_duration(
        self,
        net: _Net,
        sms: FakeSmsSender,
        clock: ThrottleClock,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: time the refusal — a dead challenge is one statement, a wrong code several."""
        floor = 5.0
        waits: list[float] = []

        async def _sleep(seconds: float) -> None:
            waits.append(seconds)

        browser = net.browser()
        used = await _challenge(browser, sms, new_phone())
        await _try(browser, *used)
        exhausted = await _challenge(browser, sms, new_phone())
        for step in range(1, ATTEMPTS_PER_CHALLENGE + 1):
            await _try(browser, exhausted[0], _other_code(exhausted[1], step))
        suspended_phone = new_phone()
        await _sign_in(net.browser(), sms, suspended_phone)
        await db_session.execute(
            update(PatientAccount)
            .where(PatientAccount.phone == suspended_phone)
            .values(status="suspended")
        )
        await db_session.commit()
        suspended = await _challenge(net.browser(), sms, suspended_phone)
        expired = await _challenge(browser, sms, new_phone())
        clock.advance(minutes=6)
        live = await _challenge(browser, sms, new_phone())
        throttled_source = net.browser()
        for _ in range(VERIFY_SOURCE_BURST):
            await _try(throttled_source, str(uuid.uuid4()), "000000")
        monkeypatch.setattr(settings, "AUTH_FAILURE_MIN_SECONDS", floor)
        monkeypatch.setattr(auth_module, "_sleep", _sleep)

        causes = {
            "unknown challenge": (browser, str(uuid.uuid4()), "123456"),
            "wrong code": (browser, live[0], _other_code(live[1])),
            "used": (browser, *used),
            "exhausted": (browser, *exhausted),
            "expired": (browser, *expired),
            "throttled source": (throttled_source, *live),
            "suspended account": (net.browser(), *suspended),
        }
        for cause, (client, challenge_id, code) in causes.items():
            waits.clear()
            response = await _try(client, challenge_id, code)
            assert response.status_code == 401, cause
            assert len(waits) == 1, cause
            # The whole of the floor minus the little time the work took,
            # plus up to a fifth of it at random.
            assert floor - 2.0 < waits[0] <= floor * 1.2, (cause, waits)

        # A sign-in is not delayed.
        waits.clear()
        assert (await _try(browser, *live)).status_code == 200
        assert waits == []


# ── 9. The SMS provider ──────────────────────────────────────────────────────


class _QuotingProvider:
    """A provider whose error quotes the request it rejected — as real ones do."""

    def __init__(self) -> None:
        self.attempted: list[SmsMessage] = []

    async def send(self, message: SmsMessage) -> None:
        self.attempted.append(message)
        detail = f"provider rejected message to {message.to}: {message.body!r}"
        raise RuntimeError(detail)


class _LateProvider:
    """A provider that takes the message and then never answers."""

    def __init__(self) -> None:
        self.attempted: list[SmsMessage] = []

    async def send(self, message: SmsMessage) -> None:
        self.attempted.append(message)
        await asyncio.Event().wait()


class TestTheSmsProvider:
    async def test_with_no_provider_the_answer_is_503_and_nothing_at_all_is_written(
        self, db_session: AsyncSession
    ) -> None:
        """Attack: with SMS off, get the application to behave as if a code had been sent.

        The application as it is really wired: no override of the sender, and
        ``SMS_PROVIDER`` unset — which is how the suite, and a deployment that
        has not chosen a provider, runs.
        """
        assert settings.SMS_PROVIDER is None
        application = create_app()

        async def _override() -> AsyncGenerator[AsyncSession]:
            yield db_session

        application.dependency_overrides[get_db_session] = _override
        network = _Net(application)
        try:
            responses = [await _ask(network.browser(), new_phone()) for _ in range(5)]
        finally:
            await network.aclose()

        for response in responses:
            assert response.status_code == 503
            assert _body(response) == SMS_UNAVAILABLE
            assert "challenge_id" not in response.text
            assert set_cookie_headers(response) == []
        assert await _count(db_session, PatientOtpChallenge) == 0
        assert await _patient_buckets(db_session) == 0
        assert await _count(db_session, AuditLog) == 0

    async def test_with_no_provider_a_number_outside_the_allowed_countries_is_still_a_422(
        self, db_session: AsyncSession
    ) -> None:
        """The validation answer does not depend on whether SMS happens to be configured."""
        network = _Net(_application(db_session, None))
        try:
            response = await _ask(network.browser(), "+14155550123")
        finally:
            await network.aclose()

        assert response.status_code == 422

    async def test_a_failed_send_is_a_503_and_the_challenge_is_gone(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: verify against a challenge whose code was never delivered."""
        sms.fail = True

        response = await _ask(net.browser(), new_phone())

        assert response.status_code == 503
        assert _body(response) == SMS_UNAVAILABLE
        assert "challenge_id" not in response.text
        assert await _count(db_session, PatientOtpChallenge) == 0
        assert await audit_rows(db_session, "patient.auth.otp_requested") == []

    async def test_a_provider_error_that_quotes_the_message_leaks_neither_code_nor_number(
        self, db_session: AsyncSession
    ) -> None:
        """Attack: read the code out of the error a failing provider returned."""
        provider = _QuotingProvider()
        network = _Net(_application(db_session, provider))  # type: ignore[arg-type]
        phone = new_phone()
        try:
            with _capturing_logs() as logs:
                response = await _ask(network.browser(), phone)
        finally:
            await network.aclose()

        [message] = provider.attempted
        code = _code_in(message)
        assert response.status_code == 503
        assert _body(response) == SMS_UNAVAILABLE
        everything = response.text + json.dumps(dict(response.headers))
        assert not _mentions(everything, code)
        assert phone not in everything
        assert "patient_otp_send_failed" in logs.everything, "the failure was not captured"
        assert not _mentions(logs.everything, code)
        assert "provider rejected" not in logs.everything
        assert phone not in logs.application
        assert phone[3:] not in logs.application
        assert await _count(db_session, PatientOtpChallenge) == 0
        assert not _mentions(_dump(await _all(db_session, AuditLog)), code)

    async def test_a_code_that_arrives_after_the_send_was_given_up_on_opens_nothing(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: the provider times out but delivers anyway; the text holds a code for a dead challenge."""
        monkeypatch.setattr(settings, "SMS_TIMEOUT_SECONDS", 0.02)
        provider = _LateProvider()
        network = _Net(_application(db_session, provider))  # type: ignore[arg-type]
        phone = new_phone()
        try:
            browser = network.browser()
            response = await _ask(browser, phone)
            [message] = provider.attempted
            guesses = [await _try(browser, str(uuid.uuid4()), _code_in(message)) for _ in range(3)]
        finally:
            await network.aclose()

        assert response.status_code == 503
        assert "challenge_id" not in response.text
        assert await _count(db_session, PatientOtpChallenge) == 0
        assert all(_body(guess) == OTP_INVALID for guess in guesses)
        assert await _count(db_session, PatientAccount) == 0

    async def test_a_failing_provider_is_still_charged_for(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: use a provider outage to ask for codes without limit (each one may yet be billed)."""
        phone = new_phone()
        attacker = net.browser()
        sms.fail = True

        statuses = [(await _ask(attacker, phone)).status_code for _ in range(PAIR_BURST + 3)]

        assert statuses == [503] * PAIR_BURST + [429] * 3
        assert await _count(db_session, PatientOtpChallenge) == 0

    def test_the_development_sender_is_never_built_outside_development(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: a settings object that was changed after start-up names the printing sender."""
        monkeypatch.setattr(settings, "SMS_PROVIDER", "dev")
        assert isinstance(sms_module.get_sms_sender(), sms_module.DevSmsSender)

        for environment in (AppEnv.STAGING, AppEnv.PRODUCTION):
            monkeypatch.setattr(settings, "APP_ENV", environment)
            assert sms_module.get_sms_sender() is None

    def test_a_delivery_error_says_nothing_about_the_message(self) -> None:
        error = SmsDeliveryError()

        assert str(error) == "The SMS could not be delivered."
        assert error.args == ("The SMS could not be delivered.",)


# ── 10. Configuration that must not start ────────────────────────────────────

SIGNING_KEY = "a-private-signing-key-for-these-tests-0123456789"
OTP_SECRET = "a-private-otp-key-for-these-tests-0123456789abcdef"
OUTSIDE_DEVELOPMENT = ["staging", "production"]


def _settings(**values: Any) -> Settings:
    """Build settings that would start anywhere, except for what a test overrides."""
    values.setdefault("APP_SECRET_KEY", SIGNING_KEY)
    values.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
    values.setdefault("RATE_LIMIT_TRUST_PROXY_HEADER", False)
    values.setdefault("PATIENT_OTP_SECRET", OTP_SECRET)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class TestConfigurationThatMustNotStart:
    """Each of these is a deployment mistake that would put codes, or cookies, in the open."""

    @pytest.fixture(autouse=True)
    def _no_settings_in_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit values only: remove every variable that names a setting."""
        names = {name.upper() for name in Settings.model_fields}
        for variable in list(os.environ):
            if variable.upper() in names:
                monkeypatch.delenv(variable)

    @pytest.mark.parametrize("environment", OUTSIDE_DEVELOPMENT)
    def test_the_control_starts(self, environment: str) -> None:
        """What the refusals below are measured against: these settings do start."""
        started = _settings(APP_ENV=environment)

        assert started.SMS_PROVIDER is None
        assert started.PATIENT_COOKIE_SECURE is True
        assert started.PATIENT_OTP_ALLOWED_COUNTRY_CODES == ["+91"]

    @pytest.mark.parametrize("environment", OUTSIDE_DEVELOPMENT)
    @pytest.mark.parametrize("name", ["dev", "DEV", " dev ", "Dev"])
    def test_the_sender_that_prints_codes_refuses_to_start_outside_development(
        self, environment: str, name: str
    ) -> None:
        with pytest.raises(SettingsError, match="SMS_PROVIDER"):
            _settings(APP_ENV=environment, SMS_PROVIDER=name)

    @pytest.mark.parametrize("environment", ["development", *OUTSIDE_DEVELOPMENT])
    @pytest.mark.parametrize("name", ["twilio", "msg91", "console", "stdout", "devv", "none", "0"])
    def test_an_unknown_provider_refuses_to_start_everywhere(
        self, environment: str, name: str
    ) -> None:
        """Read as "off", a typo would hide until the first patient could not sign in."""
        with pytest.raises(SettingsError, match="SMS_PROVIDER"):
            _settings(APP_ENV=environment, SMS_PROVIDER=name)

    @pytest.mark.parametrize("environment", OUTSIDE_DEVELOPMENT)
    @pytest.mark.parametrize("value", ["", "   ", "\t\n"])
    def test_a_missing_otp_key_refuses_to_start_outside_development(
        self, environment: str, value: str
    ) -> None:
        with pytest.raises(SettingsError, match="PATIENT_OTP_SECRET"):
            _settings(APP_ENV=environment, PATIENT_OTP_SECRET=value)

    @pytest.mark.parametrize("environment", ["development", *OUTSIDE_DEVELOPMENT])
    def test_a_short_otp_key_refuses_to_start_and_is_not_quoted(self, environment: str) -> None:
        weak = "only-thirty-one-characters-long"
        assert len(weak) == 31

        with pytest.raises(SettingsError) as refusal:
            _settings(APP_ENV=environment, PATIENT_OTP_SECRET=weak)

        assert "PATIENT_OTP_SECRET" in str(refusal.value)
        assert weak not in str(refusal.value)
        assert weak not in repr(refusal.value)

    def test_development_without_an_otp_key_gets_a_random_one_not_a_built_in_one(self) -> None:
        """Attack: forge codes for a development server from a key that is in the source."""
        first, second = _settings(PATIENT_OTP_SECRET=""), _settings(PATIENT_OTP_SECRET="")

        keys = [each.PATIENT_OTP_SECRET.get_secret_value() for each in (first, second)]

        assert all(len(key) >= 32 for key in keys)
        assert keys[0] != keys[1]
        assert first.patient_otp_secret_is_ephemeral is True
        assert keys[0] not in repr(first)
        assert keys[0] != first.APP_SECRET_KEY.get_secret_value()

    @pytest.mark.parametrize("environment", OUTSIDE_DEVELOPMENT)
    def test_cookies_in_the_clear_refuse_to_start_outside_development(
        self, environment: str
    ) -> None:
        with pytest.raises(SettingsError, match="PATIENT_COOKIE_SECURE"):
            _settings(APP_ENV=environment, PATIENT_COOKIE_SECURE=False)

    @pytest.mark.parametrize(
        "value", [[], "[]", "", ["91"], ["+"], ["+0"], ["*"], ["+91", ""], "+91"]
    )
    def test_a_country_list_that_allows_everything_or_nothing_refuses_to_start(
        self, value: Any
    ) -> None:
        """An empty list must not come to mean "any country": the list is the cost bound."""
        with pytest.raises(SettingsError):
            _settings(PATIENT_OTP_ALLOWED_COUNTRY_CODES=value)

    @pytest.mark.parametrize(
        "value",
        [
            ["*"],
            ["https://*.hospital.example"],
            ["https://app.hospital.example/path"],
            ["https://user:pw@app.hospital.example"],
            ["app.hospital.example"],
            ["javascript:alert(1)"],
            ["null"],
        ],
    )
    def test_an_origin_list_that_is_not_exact_origins_refuses_to_start(self, value: Any) -> None:
        with pytest.raises(SettingsError):
            _settings(PATIENT_APP_ORIGINS=value)


# ── 11. Numbers a code may not be sent to ────────────────────────────────────


class TestNumbersOutsideTheAllowedCountries:
    @pytest.mark.parametrize(
        "phone",
        [
            "+14155550123",
            "+447911123456",
            "+8613800138000",
            "+923001234567",
            "+971501234567",
            "+9779812345678",
            "+881612345678",  # satellite: the classic pumping destination
            "0014155550123",
            "+1 (415) 555-0123",
            "+91",
            "+915812345678",  # India, but not a mobile number
            "+9198765432101",  # a digit too many
            "+91987654321",  # a digit too few
            "+919876543210,+14155550123",
            "+14155550123;+919876543210",
            "+91+14155550123",
            "12345",
            "++919876543210",
        ],
    )
    async def test_it_is_refused_and_nothing_is_sent_charged_or_recorded(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, phone: str
    ) -> None:
        """Attack (international SMS pumping): have codes sent to a number abroad."""
        response = await _ask(net.browser(), phone)

        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == "VALIDATION_ERROR"
        assert sms.sent == []
        assert await _count(db_session, PatientOtpChallenge) == 0
        assert await _patient_buckets(db_session) == 0
        assert await _count(db_session, AuditLog) == 0

    @pytest.mark.parametrize(
        "spelling",
        [
            pytest.param(
                "+919\u096e\u096d\u096c\u096b\u096a\u0969\u0968\u0967\u0966", id="devanagari"
            ),
            pytest.param(
                "+919\u0668\u0667\u0666\u0665\u0664\u0663\u0662\u0661\u0660", id="arabic-indic"
            ),
            pytest.param(
                "+919\u06f8\u06f7\u06f6\u06f5\u06f4\u06f3\u06f2\u06f1\u06f0", id="persian"
            ),
            pytest.param("+91987654321\u0966", id="one-devanagari-digit"),
            pytest.param("+91987654321\u0660", id="one-arabic-indic-digit"),
            pytest.param(
                "\u096f\u096e\u096d\u096c\u096b\u096a\u0969\u0968\u0967\u0966",
                id="national-devanagari",
            ),
            pytest.param("+\u096f\u09679876543210", id="calling-code-devanagari"),
            pytest.param("\uff0b919876543210", id="fullwidth-plus"),
            pytest.param("+91\uff19876543210", id="fullwidth-digit"),
            pytest.param("+9198765\u00a043210", id="no-break-space"),
            pytest.param("+91\u200b9876543210", id="zero-width-space"),
            pytest.param("+919876543210\u202e", id="right-to-left-override"),
        ],
    )
    async def test_a_number_that_is_not_plain_ascii_is_refused_outright(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, spelling: str
    ) -> None:
        """Attack: spell one handset's number in another script to get a second allowance for it.

        Every one of these reads, to a person or to a provider that folds
        digits, as +91 98765 43210 — and before the fix most of them were
        accepted as *different* numbers, each with its own budget, challenge
        and account. None is a number: 422, and nothing sent, charged or kept.
        """
        assert not spelling.isascii()

        response = await _ask(net.browser(), spelling)
        ascii_answer = await _ask(net.browser(), "+14155550123")

        assert response.status_code == 422, response.text
        # The same refusal as any other number a code may not be sent to.
        assert _body(response) == _body(ascii_answer)
        assert sms.sent == []
        assert await _count(db_session, PatientOtpChallenge) == 0
        assert await _patient_buckets(db_session) == 0
        assert await _count(db_session, AuditLog) == 0
        assert await _count(db_session, PatientAccount) == 0

    @pytest.mark.parametrize(
        "spelling",
        [
            "+919\u096e\u096d\u096c\u096b\u096a\u0969\u0968\u0967\u0966",
            "+919\u0668\u0667\u0666\u0665\u0664\u0663\u0662\u0661\u0660",
        ],
        ids=["devanagari", "arabic-indic"],
    )
    def test_the_normaliser_refuses_it_before_any_pattern_sees_it(self, spelling: str) -> None:
        """The check is the service's own, and comes first: the shared patterns accept these."""
        from app.core.exceptions import ValidationError
        from app.services.patient_app.common import normalize_patient_phone
        from app.utils.phone import E164_PATTERN

        # What made this a defect: the shared pattern takes it for an E.164 number.
        assert E164_PATTERN.fullmatch(spelling) is not None
        assert int(spelling[1:]) == 919876543210

        with pytest.raises(ValidationError):
            normalize_patient_phone(spelling)
        assert normalize_patient_phone("+919876543210") == "+919876543210"

    async def test_every_refused_number_gets_the_same_answer(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: learn which countries are allowed from how a number is refused."""
        answers = [
            _body(await _ask(net.browser(), phone))
            for phone in ("+14155550123", "+447911123456", "+915812345678", "+9198765432101")
        ]

        assert all(answer == answers[0] for answer in answers)
        assert "+91" not in json.dumps(answers[0])

    @pytest.mark.parametrize(
        "typed",
        ["{national}", "0{national}", "+91{national}", "+91 {a} {b}", "({a}) {b}", "91-{national}"],
    )
    async def test_an_allowed_number_is_one_number_however_it_is_typed(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, typed: str
    ) -> None:
        """Attack: write one number several ways to get several allowances for it."""
        phone = new_phone()
        national = phone[3:]
        raw = typed.format(national=national, a=national[:5], b=national[5:])
        attacker = net.browser()

        first = await _ask(attacker, raw)

        if first.status_code == 422:
            # A spelling the normaliser does not accept is simply refused.
            assert sms.sent == []
            return
        assert first.status_code == 202
        assert sms.sent[-1].to == phone
        # Whatever the spelling, it drew on the canonical number's allowance.
        for _ in range(PAIR_BURST - 1):
            assert (await _ask(attacker, phone)).status_code == 202
        assert (await _ask(attacker, raw)).status_code == 429
        assert {row.phone for row in await _all(db_session, PatientOtpChallenge)} == {phone}

    async def test_a_number_written_in_another_scripts_digits_is_not_a_second_number(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: spell the victim's number with non-ASCII digits to get a fresh allowance each time.

        Twelve spellings of one number, each from a new address. Either they
        are refused, or they are the number they spell and share its budget of
        six. They must never be twelve numbers.
        """
        phone = new_phone()
        zeros = [0x0660, 0x06F0, 0x07C0, 0x0966, 0x09E6, 0x0A66, 0x0AE6, 0x0B66, 0x0BE6, 0x0C66]
        spellings = [
            phone[:4] + "".join(chr(zero + int(digit)) for digit in phone[4:]) for zero in zeros
        ]
        # And two that change a single digit only.
        spellings.append(phone[:-1] + chr(0x0660 + int(phone[-1])))
        spellings.append(phone[:-2] + chr(0xFF10 + int(phone[-2])) + phone[-1])
        assert len(set(spellings)) == 12
        assert all(
            "".join(str(int(character)) for character in spelling[1:]) == phone[1:]
            for spelling in spellings
        )

        statuses = [(await _ask(net.browser(), spelling)).status_code for spelling in spellings]

        assert set(statuses) <= {202, 422, 429}
        assert all(message.to.isascii() for message in sms.sent), "a non-E.164 recipient was texted"
        assert {message.to for message in sms.sent} <= {phone}
        assert len(sms.sent) <= PHONE_BURST
        stored = {row.phone for row in await _all(db_session, PatientOtpChallenge)}
        assert stored <= {phone}

    async def test_only_the_configured_countries_count(
        self, net: _Net, sms: FakeSmsSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The list is the list: India is not special once a deployment has named another country."""
        monkeypatch.setattr(settings, "PATIENT_OTP_ALLOWED_COUNTRY_CODES", ["+1"])

        allowed = await _ask(net.browser(), "+14155550123")
        indian = await _ask(net.browser(), new_phone())

        assert allowed.status_code == 202
        assert indian.status_code == 422
        assert [message.to for message in sms.sent] == ["+14155550123"]

    async def test_a_calling_code_that_merely_resembles_an_allowed_one_is_refused(
        self, net: _Net, sms: FakeSmsSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: the allow-list names ``+92``; send to ``+91…``, one digit away."""
        monkeypatch.setattr(settings, "PATIENT_OTP_ALLOWED_COUNTRY_CODES", ["+92"])

        response = await _ask(net.browser(), new_phone())

        assert response.status_code == 422
        assert sms.sent == []
