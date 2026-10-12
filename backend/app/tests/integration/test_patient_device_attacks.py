"""Attacks on the recognised device (``docs/modules/15-patient-app.md`` §5).

A *recognised device* is a browser that has completed a sign-in to a patient
account. It holds a random token in an ``HttpOnly`` cookie; the server keeps
only its SHA-256. It exists for one reason — so that somebody who merely knows
a phone number cannot use up the allowance of the browser the number's owner
signs in from — and it must be worth **nothing else**. This suite attacks
that claim from every side:

* a device token is issued only by a correct one-time code, and a value the
  client supplies is never adopted;
* it is never an authentication factor: alone, or with a wrong or expired
  code, it starts no session and returns no account data;
* it authorises nothing: every bearer and cookie endpoint answers it exactly
  as it answers nobody;
* it cannot make one patient claim another;
* minting device after device, or copying one, multiplies no budget;
* a deleted, pruned or expired row is not recognised, and "log out
  everywhere" forgets every device of the account;
* concurrent requests never exceed the device or the account budget;
* an attacker who knows only the number cannot stop the owner's recognised
  device getting a code — from one address or from many;
* an attacker who has *stolen* the cookie gets no session and no more codes
  than the one account's recognised allowance, and the owner can still sign in.

What a recognised device is charged to (``PatientAuthService._send_buckets``):

==========================  ==================================================
``pt_otp_send_device``      this device row: 3, then one per 10 minutes
``pt_otp_send_phone``       keyed ``("recognised", account id)``: every
                            recognised device of the account together —
                            6, then one per 5 minutes
``pt_otp_send_source``      the address it came from: 30, then one a minute
``pt_otp_send_global``      the platform ceiling
==========================  ==================================================

Every test drives the real application over HTTP, with the real PostgreSQL
throttle and the real cookies. Only the SMS transport is stood in for. Each
request is sent by a client that carries exactly the cookie the test names —
no cookie jar is relied on — so "with the cookie" and "without the cookie"
mean precisely that. Throttle time is moved with :class:`ThrottleClock`; no
test sleeps. The concurrency tests use independent connections with real
commits and clean up in ``finally``.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.dependencies.db import get_db_session
from app.api.dependencies.patient import get_sms_sender
from app.core.config import settings
from app.core.security import hash_token
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.auth_throttle import AuthThrottleBucket
from app.models.patient_account import (
    PatientAccount,
    PatientDevice,
    PatientOtpChallenge,
    PatientRefreshToken,
)
from app.repositories.auth_throttle_repository import AuthThrottleRepository
from app.services.auth_throttle import POLICIES, BucketKind, Budget, bucket
from app.tests.patient_app_helpers import (
    CSRF,
    DEVICE_COOKIE,
    PATIENT,
    POLICY,
    REFRESH_COOKIE,
    FakeSmsSender,
    ThrottleClock,
    audit_rows,
    bearer,
    insert_patient_record,
    new_phone,
    open_hospital,
    set_cookie_headers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

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

# The documented numbers, restated here on purpose: if somebody loosens the
# policy, these tests must fail rather than follow it.
DEVICE_BURST = 3
DEVICE_REFILL = timedelta(minutes=10)
#: Every recognised device of one account, together.
ACCOUNT_BURST = 6
ACCOUNT_REFILL = timedelta(minutes=5)
#: One number, from every caller that is not a recognised device of it.
PHONE_BURST = 6
PAIR_BURST = 3
SOURCE_BURST = 30
SOURCE_REFILL = timedelta(seconds=60)
DEVICES_PER_ACCOUNT = 5
DEVICE_IDLE = timedelta(days=90)

OTP_INVALID = {
    "success": False,
    "message": "The code is incorrect or has expired.",
    "error_code": "OTP_INVALID",
    "errors": None,
}
OTP_THROTTLED = {
    "success": False,
    "message": "Too many requests. Please try again later.",
    "error_code": "OTP_THROTTLED",
    "errors": None,
}

#: The socket peer of every test request: a proxy we "operate".
_PROXY_PEER = ("127.0.0.1", 40000)
_SIX_DIGITS = re.compile(r"\b(\d{6})\b")
#: Response headers that differ between any two requests, whoever sends them.
_VOLATILE_HEADERS = frozenset(
    {
        "x-request-id",
        "x-ratelimit-reset",
        "x-ratelimit-remaining",
        "x-process-time",
        "x-response-time",
        "date",
    }
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _source() -> str:
    """A public IPv4 address nobody else in this run is using."""
    return f"192.{secrets.randbelow(256)}.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}"


def _body(response: Response) -> dict[str, Any]:
    """The response envelope without its per-request metadata."""
    payload: dict[str, Any] = response.json()
    payload.pop("metadata", None)
    return payload


def _stable_headers(response: Response) -> dict[str, str]:
    return {
        name: value
        for name, value in sorted(response.headers.items())
        if name.lower() not in _VOLATILE_HEADERS
    }


def _cookie_set(response: Response, name: str) -> str | None:
    """The value a response sets for a cookie, or ``None`` if it sets none."""
    for header in set_cookie_headers(response):
        key, _, rest = header.partition("=")
        if key.strip() == name:
            return rest.split(";", 1)[0].strip().strip('"')
    return None


def _other_code(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    result = await session.execute(select(func.count()).select_from(model).where(*where))
    return int(result.scalar_one())


async def _all(session: AsyncSession, model: Any, *where: Any) -> list[Any]:
    result = await session.execute(
        select(model).where(*where).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _recognised(session: AsyncSession) -> list[bool]:
    """For every code that was sent: was the request taken for a recognised device?

    In no particular order — the rows of one test share a transaction, and so
    a timestamp. Use :func:`_was_recognised` to ask about one request.
    """
    rows = await audit_rows(session, "patient.auth.otp_requested")
    return [bool((row.context or {})["recognised_device"]) for row in rows]


async def _was_recognised(session: AsyncSession, response: Response) -> bool:
    """Whether one accepted request for a code was taken for a recognised device."""
    assert response.status_code == 202, response.text
    challenge_id = uuid.UUID(response.json()["data"]["challenge_id"])
    [row] = [
        row
        for row in await audit_rows(session, "patient.auth.otp_requested")
        if row.target_id == challenge_id
    ]
    return bool((row.context or {})["recognised_device"])


async def _bucket_kinds(session: AsyncSession) -> dict[str, int]:
    """How many throttle rows of each Patient App kind exist."""
    result = await session.execute(
        select(AuthThrottleBucket.kind, func.count())
        .where(AuthThrottleBucket.kind.like("pt_%"))
        .group_by(AuthThrottleBucket.kind)
    )
    return {str(kind): int(count) for kind, count in result.all()}


async def _has_bucket(session: AsyncSession, kind: BucketKind, *parts: Any) -> bool:
    return (
        await _count(
            session,
            AuthThrottleBucket,
            AuthThrottleBucket.key_hash == bucket(kind, *parts).key_hash,
        )
        == 1
    )


@dataclass(frozen=True)
class _Session:
    """What one successful sign-in gave the browser."""

    account_id: uuid.UUID
    access_token: str
    refresh_token: str
    #: The whole device cookie value (one token per account signed in on the browser).
    device: str
    body: dict[str, Any]

    @property
    def headers(self) -> dict[str, str]:
        return bearer(self.access_token)


class _Net:
    """Sends single requests, each carrying exactly the cookies it is told to.

    No cookie jar: a response's cookies are read off the response, and a
    request has the device cookie only when the test passes one.
    """

    def __init__(self, application: FastAPI, sms: FakeSmsSender) -> None:
        self.application = application
        self.sms = sms
        self.sources: set[str] = set()

    async def send(
        self,
        method: str,
        url: str,
        *,
        source: str | None = None,
        device: str | None = None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
    ) -> Response:
        address = source or _source()
        self.sources.add(address)
        jar = dict(cookies or {})
        if device is not None:
            jar[DEVICE_COOKIE] = device
        sent = {"X-Forwarded-For": address, **(headers or {})}
        if jar:
            sent["Cookie"] = "; ".join(f"{name}={value}" for name, value in jar.items())
        async with AsyncClient(
            transport=ASGITransport(app=self.application, client=_PROXY_PEER),
            base_url="http://test",
        ) as client:
            return await client.request(method, url, headers=sent, json=json)

    async def ask(
        self, phone: str, *, source: str | None = None, device: str | None = None
    ) -> Response:
        """Request a code."""
        return await self.send("POST", REQUEST, source=source, device=device, json={"phone": phone})

    async def code(
        self, phone: str, *, source: str | None = None, device: str | None = None
    ) -> tuple[str, str]:
        """Request a code and return ``(challenge id, the code that was texted)``."""
        sent = len(self.sms.sent)
        response = await self.ask(phone, source=source, device=device)
        assert response.status_code == 202, response.text
        assert len(self.sms.sent) == sent + 1
        assert self.sms.sent[-1].to == phone
        match = _SIX_DIGITS.search(self.sms.sent[-1].body)
        assert match is not None
        return str(response.json()["data"]["challenge_id"]), match.group(1)

    async def verify(
        self,
        challenge_id: str,
        code: str,
        *,
        source: str | None = None,
        device: str | None = None,
    ) -> Response:
        return await self.send(
            "POST",
            VERIFY,
            source=source,
            device=device,
            json={"challenge_id": challenge_id, "code": code},
        )

    async def finish(
        self,
        challenge_id: str,
        code: str,
        *,
        source: str | None = None,
        device: str | None = None,
    ) -> _Session:
        """Verify the right code and return what the browser was given."""
        response = await self.verify(challenge_id, code, source=source, device=device)
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()["data"]
        refresh, cookie = (
            _cookie_set(response, REFRESH_COOKIE),
            _cookie_set(response, DEVICE_COOKIE),
        )
        assert refresh, "a sign-in set no refresh cookie"
        assert cookie, "a sign-in set no device cookie"
        return _Session(
            account_id=uuid.UUID(data["account"]["id"]),
            access_token=data["access_token"],
            refresh_token=refresh,
            device=cookie,
            body=data,
        )

    async def sign_in(
        self, phone: str, *, source: str | None = None, device: str | None = None
    ) -> _Session:
        """A whole sign-in: request a code, verify it."""
        address = source or _source()
        challenge_id, code = await self.code(phone, source=address, device=device)
        return await self.finish(challenge_id, code, source=address, device=device)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _attack_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain-HTTP cookies, one trusted proxy, and the request limiter out of the way.

    A 429 from the per-minute middleware would make "the attacker was refused"
    pass without the code under test having decided anything.
    """
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


@pytest_asyncio.fixture
async def net(db_session: AsyncSession, sms: FakeSmsSender) -> AsyncGenerator[_Net]:
    """The real application on the test's rolled-back session."""
    application = create_app()

    async def _override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _override
    application.dependency_overrides[get_sms_sender] = lambda: sms
    try:
        yield _Net(application, sms)
    finally:
        application.dependency_overrides.clear()


# ── 0. The numbers this suite restates are the numbers in force ──────────────


def test_the_documented_budgets_are_the_ones_in_force() -> None:
    assert POLICIES[BucketKind.PT_OTP_SEND_DEVICE] == Budget(DEVICE_BURST, DEVICE_REFILL)
    # The account-wide recognised budget is charged under this kind's policy.
    assert POLICIES[BucketKind.PT_OTP_SEND_PHONE] == Budget(ACCOUNT_BURST, ACCOUNT_REFILL)
    assert POLICIES[BucketKind.PT_OTP_SEND_SOURCE] == Budget(SOURCE_BURST, SOURCE_REFILL)
    assert POLICIES[BucketKind.PT_OTP_SEND_PAIR] == Budget(PAIR_BURST, timedelta(minutes=10))
    assert PHONE_BURST == ACCOUNT_BURST


# ── 1. Only a correct one-time code issues a device token ────────────────────


class TestADeviceTokenIsIssuedOnlyByAVerifiedCode:
    async def test_asking_for_a_code_recognises_nobody(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: become a recognised device of a number by asking for its code."""
        response = await net.ask(new_phone())

        assert response.status_code == 202
        assert set_cookie_headers(response) == []
        assert await _count(db_session, PatientDevice) == 0

    @pytest.mark.parametrize(
        "how", ["wrong_code", "expired_code", "unknown_challenge", "used_code"]
    )
    async def test_a_refused_verification_issues_nothing(
        self, net: _Net, db_session: AsyncSession, clock: ThrottleClock, how: str
    ) -> None:
        """Attack: collect a device token from a sign-in that did not succeed."""
        phone = new_phone()
        challenge_id, code = await net.code(phone)
        devices_before = 0
        if how == "wrong_code":
            code = _other_code(code)
        elif how == "expired_code":
            clock.advance(minutes=5, seconds=1)
        elif how == "unknown_challenge":
            challenge_id = str(uuid.uuid4())
        else:
            await net.finish(challenge_id, code)
            devices_before = 1

        response = await net.verify(challenge_id, code)

        assert response.status_code == 401
        assert _body(response) == OTP_INVALID
        assert _cookie_set(response, DEVICE_COOKIE) is None
        assert await _count(db_session, PatientDevice) == devices_before

    async def test_a_correct_code_issues_one_and_only_its_hash_is_kept(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        session = await net.sign_in(new_phone())

        [row] = await _all(db_session, PatientDevice)
        assert row.account_id == session.account_id
        assert row.token_hash == hash_token(session.device)
        assert session.device not in str(
            {column.name: getattr(row, column.name) for column in PatientDevice.__table__.columns}
        )
        # Random, and long enough not to be guessed: 32 bytes, URL-safe.
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", session.device)
        # Never in the response body.
        assert session.device not in str(session.body)

    async def test_no_other_endpoint_ever_issues_or_changes_one(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Attack: get recognised through a session instead of through a code.

        A session is not a proof of the phone number. Refresh, ``/me``,
        linking, registering and both sign-outs set no device cookie and
        create no device row.
        """
        phone = new_phone()
        await open_hospital(db_session, hospital_id)
        await insert_patient_record(db_session, hospital_id, phone=phone)
        session = await net.sign_in(phone)
        cookie = {REFRESH_COOKIE: session.refresh_token}
        link = {"date_of_birth": "1990-05-17", "consent_policy_version": POLICY}

        refreshed = await net.send("POST", REFRESH, cookies=cookie, headers=CSRF)
        assert refreshed.status_code == 200
        new_access = bearer(refreshed.json()["data"]["access_token"])
        new_cookie = {REFRESH_COOKIE: _cookie_set(refreshed, REFRESH_COOKIE) or ""}
        answers = [
            refreshed,
            await net.send("GET", ME, headers=new_access),
            await net.send(
                "POST", f"{PATIENT}/hospitals/{hospital_id}/link", headers=new_access, json=link
            ),
            await net.send("POST", LOGOUT, cookies=new_cookie, headers=CSRF),
        ]

        assert [answer.status_code for answer in answers] == [200, 200, 201, 204]
        assert all(_cookie_set(answer, DEVICE_COOKIE) is None for answer in answers)
        [row] = await _all(db_session, PatientDevice)
        assert row.token_hash == hash_token(session.device)

    @pytest.mark.parametrize(
        "planted",
        [
            "A" * 43,
            "attacker-chosen-device-token-0123456789abcdefg",
            secrets.token_urlsafe(32),
        ],
    )
    async def test_a_value_the_client_supplies_is_never_adopted(
        self, net: _Net, db_session: AsyncSession, planted: str
    ) -> None:
        """Attack (fixation): plant a device token in the victim's browser before they sign in.

        If the server adopted it, the attacker — who knows the value — would
        hold a recognised device of the victim's account.
        """
        phone = new_phone()

        victim = await net.sign_in(phone, device=planted)

        assert planted not in victim.device.split(".")
        [row] = await _all(db_session, PatientDevice)
        assert row.token_hash != hash_token(planted)
        # And the attacker's copy of what they planted is nobody's device.
        assert await _was_recognised(db_session, await net.ask(phone, device=planted)) is False

    async def test_signing_in_again_from_a_recognised_browser_keeps_its_token(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """One browser, one row: coming back does not pile up devices."""
        phone = new_phone()
        first = await net.sign_in(phone)

        second = await net.sign_in(phone, device=first.device)

        assert second.device == first.device
        assert await _count(db_session, PatientDevice) == 1
        assert sorted(await _recognised(db_session)) == [False, True]


# ── 2. It is not an authentication factor ────────────────────────────────────


class TestADeviceTokenIsNotACredential:
    async def test_the_cookie_alone_starts_no_session_and_returns_no_account_data(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: present a valid device cookie and nothing else.

        The only thing it can do is ask for a code — and the answer to that
        is what anybody gets: a challenge id and two durations.
        """
        phone = new_phone()
        owner = await net.sign_in(phone)

        with_cookie = await net.ask(phone, device=owner.device)
        stranger = await net.ask(new_phone())

        assert with_cookie.status_code == 202
        assert set(with_cookie.json()["data"]) == {"challenge_id", "expires_in", "resend_after"}
        assert set(_body(with_cookie)) == set(_body(stranger))
        assert _stable_headers(with_cookie) == _stable_headers(stranger)
        assert set_cookie_headers(with_cookie) == []
        text = with_cookie.text
        assert str(owner.account_id) not in text
        assert phone not in text
        assert phone[-4:] not in text
        # No session came of it: the one from the sign-in is the only one.
        assert await _count(db_session, PatientRefreshToken) == 1

    @pytest.mark.parametrize("how", ["wrong_code", "expired_code", "other_challenge", "no_code"])
    async def test_with_a_wrong_or_expired_code_it_is_refused_exactly_as_without_it(
        self, net: _Net, db_session: AsyncSession, clock: ThrottleClock, how: str
    ) -> None:
        """Attack: use the recognised device as the second factor that excuses a bad code."""
        phone = new_phone()
        owner = await net.sign_in(phone)
        challenge_id, code = await net.code(phone, device=owner.device)
        baseline_id, baseline_code = await net.code(new_phone())
        if how == "wrong_code":
            code = _other_code(code)
        elif how == "expired_code":
            clock.advance(minutes=5, seconds=1)
        elif how == "other_challenge":
            code = baseline_code  # a real code — for somebody else's challenge
        else:
            code = "000000" if code != "000000" else "000001"

        refused = await net.verify(challenge_id, code, device=owner.device)
        without = await net.verify(baseline_id, _other_code(baseline_code))

        assert refused.status_code == 401
        assert _body(refused) == _body(without) == OTP_INVALID
        assert _stable_headers(refused) == _stable_headers(without)
        assert set_cookie_headers(refused) == []
        assert "access_token" not in refused.text
        assert str(owner.account_id) not in refused.text
        assert await _count(db_session, PatientRefreshToken) == 1

    async def test_it_buys_no_extra_guesses_at_a_code(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: guess the code from the recognised browser, where guessing might be softer."""
        phone = new_phone()
        owner = await net.sign_in(phone)
        challenge_id, code = await net.code(phone, device=owner.device)

        guesses = [
            await net.verify(challenge_id, _other_code(code), device=owner.device) for _ in range(5)
        ]
        right_code_too_late = await net.verify(challenge_id, code, device=owner.device)

        assert [guess.status_code for guess in guesses] == [401] * 5
        assert right_code_too_late.status_code == 401
        [challenge] = await _all(
            db_session, PatientOtpChallenge, PatientOtpChallenge.id == uuid.UUID(challenge_id)
        )
        assert challenge.attempts == 5
        assert challenge.consumed_at is None

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", ME),
            ("POST", LOGOUT_ALL),
            ("POST", f"{PATIENT}/hospitals/any-hospital/link"),
            ("POST", f"{PATIENT}/hospitals/any-hospital/register"),
        ],
    )
    async def test_it_authorises_nothing(
        self, net: _Net, db_session: AsyncSession, method: str, path: str
    ) -> None:
        """Attack: call an authenticated endpoint with only the device cookie.

        Wherever it is put — the cookie it came in, the refresh cookie's
        name, or the ``Authorization`` header — the answer is the one nobody
        gets, and nothing changes.
        """
        phone = new_phone()
        owner = await net.sign_in(phone)
        token = owner.device
        body = {
            "date_of_birth": "1990-05-17",
            "consent_policy_version": POLICY,
            "first_name": "Asha",
            "last_name": "Verma",
            "gender": "female",
        }
        payload = None if method == "GET" or path == LOGOUT_ALL else body
        if path.endswith("/link"):
            payload = {"date_of_birth": "1990-05-17", "consent_policy_version": POLICY}

        nobody = await net.send(method, path, json=payload)
        attempts = [
            await net.send(method, path, device=token, json=payload),
            await net.send(method, path, device=token, headers=CSRF, json=payload),
            await net.send(method, path, cookies={REFRESH_COOKIE: token}, json=payload),
            await net.send(method, path, headers=bearer(token), json=payload),
            await net.send(method, path, device=token, headers=bearer(token), json=payload),
        ]

        assert nobody.status_code == 401
        for attempt in attempts:
            assert attempt.status_code == 401, attempt.text
            assert _body(attempt) == _body(nobody)
            assert str(owner.account_id) not in attempt.text
            assert phone[-4:] not in attempt.text
        # The owner's session and device are as they were.
        assert await _count(db_session, PatientRefreshToken, PatientRefreshToken.is_revoked) == 0
        assert await _count(db_session, PatientDevice) == 1

    async def test_it_does_not_refresh_or_end_a_session(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: exchange the device token for a session at the cookie endpoints."""
        owner = await net.sign_in(new_phone())
        token = owner.device

        answers = [
            await net.send("POST", REFRESH, device=token, headers=CSRF),
            await net.send("POST", REFRESH, cookies={REFRESH_COOKIE: token}, headers=CSRF),
            await net.send(
                "POST", REFRESH, cookies={REFRESH_COOKIE: token}, device=token, headers=CSRF
            ),
        ]
        signed_out = await net.send(
            "POST", LOGOUT, cookies={REFRESH_COOKIE: token}, device=token, headers=CSRF
        )

        assert [answer.status_code for answer in answers] == [401, 401, 401]
        assert all("access_token" not in answer.text for answer in answers)
        assert signed_out.status_code == 204  # sign-out never fails, and ended nothing:
        [session] = await _all(db_session, PatientRefreshToken)
        assert session.is_revoked is False
        assert await audit_rows(db_session, "patient.auth.logout") == []
        assert await audit_rows(db_session, "patient.auth.refresh_reuse_detected") == []

    async def test_the_real_owner_still_reads_their_account_only_with_their_session(
        self, net: _Net
    ) -> None:
        """The control for everything above: the access token works, the cookie adds nothing."""
        owner = await net.sign_in(new_phone())

        with_token = await net.send("GET", ME, headers=owner.headers)
        with_both = await net.send("GET", ME, headers=owner.headers, device=owner.device)

        assert with_token.status_code == with_both.status_code == 200
        assert _body(with_token) == _body(with_both)


# ── 3. It cannot make one patient claim another ──────────────────────────────


class TestOnePatientCannotClaimAnother:
    async def test_a_devices_cookie_is_nothing_for_somebody_elses_number(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: A, recognised for A's number, asks for a code for B's number.

        The request is an ordinary stranger's: charged to B's own budgets,
        and the code goes to B's handset — A sees nothing of it.
        """
        phone_a, phone_b = new_phone(), new_phone()
        a = await net.sign_in(phone_a)
        b = await net.sign_in(phone_b)
        sent_before = len(net.sms.sent)
        attacker_address = _source()

        asked = await net.ask(phone_b, source=attacker_address, device=a.device)

        assert await _was_recognised(db_session, asked) is False
        assert [message.to for message in net.sms.sent[sent_before:]] == [phone_b]
        assert await _has_bucket(db_session, BucketKind.PT_OTP_SEND_PAIR, phone_b, attacker_address)
        # Nothing of B's account-wide recognised budget, or of A's, was drawn on.
        assert not await _has_bucket(
            db_session, BucketKind.PT_OTP_SEND_PHONE, "recognised", b.account_id
        )
        assert not await _has_bucket(
            db_session, BucketKind.PT_OTP_SEND_PHONE, "recognised", a.account_id
        )
        # A cannot finish it: a guess is a guess.
        challenge_id = asked.json()["data"]["challenge_id"]
        guess = await net.verify(challenge_id, "000000", device=a.device)
        assert guess.status_code == 401
        assert "access_token" not in guess.text

    async def test_as_cookie_gives_a_no_extra_codes_for_bs_number(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: flood B's handset using the allowance of A's recognised device."""
        phone_a, phone_b = new_phone(), new_phone()
        a = await net.sign_in(phone_a)
        address = _source()

        statuses = [
            (await net.ask(phone_b, source=address, device=a.device)).status_code for _ in range(6)
        ]

        # One address, one number: three, exactly as without any cookie.
        assert statuses == [202] * PAIR_BURST + [429] * 3
        assert not any(await _recognised(db_session))
        # And A's own device allowance is untouched by any of it.
        assert [
            (await net.ask(phone_a, device=a.device)).status_code for _ in range(DEVICE_BURST + 1)
        ] == [202] * DEVICE_BURST + [429]

    async def test_verifying_bs_code_in_as_browser_gives_b_and_nothing_of_a(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """A family phone: B signs in on the browser A used. Each account stays its own.

        The session is B's — decided by the code, which only B's handset
        received. A's device row is still A's; B gets a row of their own.
        """
        phone_a, phone_b = new_phone(), new_phone()
        a = await net.sign_in(phone_a)

        b = await net.sign_in(phone_b, device=a.device)

        assert b.account_id != a.account_id
        assert b.body["account"]["phone_masked"].endswith(phone_b[-4:])
        assert str(a.account_id) not in str(b.body)
        me = await net.send("GET", ME, headers=b.headers)
        assert me.json()["data"]["account"]["id"] == str(b.account_id)
        rows = {row.account_id: row for row in await _all(db_session, PatientDevice)}
        assert set(rows) == {a.account_id, b.account_id}
        assert rows[a.account_id].token_hash == hash_token(a.device)
        # The browser now holds one token per account, and each names only its own.
        tokens = b.device.split(".")
        assert len(tokens) == 2
        assert a.device in tokens
        assert {hash_token(token) for token in tokens} == {row.token_hash for row in rows.values()}
        # A's token recognises A's number and not B's; B's the other way round.
        [b_token] = [token for token in tokens if token != a.device]
        assert await _was_recognised(db_session, await net.ask(phone_a, device=b_token)) is False
        assert await _was_recognised(db_session, await net.ask(phone_b, device=a.device)) is False
        assert await _was_recognised(db_session, await net.ask(phone_b, device=b_token)) is True

    async def test_a_cannot_reach_bs_account_with_as_cookie_and_bs_challenge(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: verify B's challenge with A's cookie and a code A does know — A's own."""
        phone_a, phone_b = new_phone(), new_phone()
        a = await net.sign_in(phone_a)
        b_challenge, _b_code = await net.code(phone_b)
        _a_challenge, a_code = await net.code(phone_a, device=a.device)

        response = await net.verify(b_challenge, a_code, device=a.device)

        assert response.status_code == 401
        assert _body(response) == OTP_INVALID
        assert await _count(db_session, PatientAccount, PatientAccount.phone == phone_b) == 0
        assert await _count(db_session, PatientRefreshToken) == 1

    async def test_the_owner_of_b_is_served_only_b_whatever_a_did_first(
        self, net: _Net, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """After A's attempts, B signs in elsewhere: B's data is B's, and A's token still reads A."""
        phone_a, phone_b = new_phone(), new_phone()
        await open_hospital(db_session, hospital_id)
        await insert_patient_record(db_session, hospital_id, phone=phone_b)
        a = await net.sign_in(phone_a)
        await net.ask(phone_b, device=a.device)
        b = await net.sign_in(phone_b)
        link = {"date_of_birth": "1990-05-17", "consent_policy_version": POLICY}
        url = f"{PATIENT}/hospitals/{hospital_id}/link"

        b_linked = await net.send("POST", url, headers=b.headers, json=link)
        # A, with A's own valid session and the cookie, asks for B's record.
        a_tries = await net.send("POST", url, headers=a.headers, device=a.device, json=link)
        a_me = await net.send("GET", ME, headers=a.headers, device=b.device)

        assert b_linked.status_code == 201
        assert a_tries.status_code == 404
        assert a_me.json()["data"]["account"]["id"] == str(a.account_id)
        assert a_me.json()["data"]["links"] == []


# ── 4. Minting devices multiplies no budget ──────────────────────────────────


class TestMintingDevicesMultipliesNothing:
    async def test_request_three_then_verify_without_the_cookie_never_passes_the_account_bound(
        self, net: _Net, db_session: AsyncSession, clock: ThrottleClock
    ) -> None:
        """Attack (SMS pumping with one SIM): turn every sign-in into three more codes.

        The attacker asks for three codes with a device cookie, then verifies
        the last one *without* the cookie, which mints a fresh device row —
        and, if a device row were all a recognised request is charged to, a
        fresh allowance of three. Repeated for ever from one host, that was
        an unbounded, self-funding stream of texts.

        Every recognised device of an account draws on one budget keyed by
        the account. However many rows are minted, the codes stop at six.
        """
        phone = new_phone()
        address = _source()
        first = await net.sign_in(phone, source=address)
        device, minted = first.device, [first.device]
        cycles: list[list[int]] = []

        for _ in range(8):
            statuses, last = [], None
            for _ in range(DEVICE_BURST):
                response = await net.ask(phone, source=address, device=device)
                statuses.append(response.status_code)
                if response.status_code == 202:
                    last = response.json()["data"]["challenge_id"]
            cycles.append(statuses)
            if last is None:
                continue  # no code to verify: the attacker keeps hammering with what they hold
            # Verify without the cookie: the server cannot tell this browser
            # from a new one, and mints a new device row.
            session = await net.finish(last, net.sms.code_for(phone), source=address)
            assert session.device not in minted
            device = session.device
            minted.append(device)

        assert cycles == [[202] * 3, [202] * 3] + [[429] * 3] * 6
        recognised = await _recognised(db_session)
        assert recognised.count(True) == ACCOUNT_BURST
        assert len(minted) == 3
        # No cookie the attacker ever held gets one more — from any address.
        for token in minted:
            for source in (address, _source()):
                refused = await net.ask(phone, source=source, device=token)
                assert refused.status_code == 429
                assert _body(refused) == OTP_THROTTLED
        assert (await _recognised(db_session)).count(True) == ACCOUNT_BURST
        # The budget that stopped it is the account's, not any device row's.
        assert await _has_bucket(
            db_session, BucketKind.PT_OTP_SEND_PHONE, "recognised", first.account_id
        )
        # It refills at the account's rate: one code per five minutes, not three per device.
        clock.advance(minutes=5, seconds=1)
        after = [
            (await net.ask(phone, source=_source(), device=minted[-1])).status_code
            for _ in range(3)
        ]
        assert after == [202, 429, 429]

    async def test_the_bound_holds_when_every_cycle_comes_from_a_new_address(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """The same attack from a botnet: the account bound is not a per-source bound."""
        phone = new_phone()
        device = (await net.sign_in(phone)).device
        sent = 0

        for _ in range(6):
            last = None
            for _ in range(DEVICE_BURST):
                response = await net.ask(phone, device=device)  # a new address every request
                if response.status_code == 202:
                    sent += 1
                    last = response.json()["data"]["challenge_id"]
            if last is not None:
                device = (await net.finish(last, net.sms.code_for(phone))).device

        assert sent == ACCOUNT_BURST
        assert (await _recognised(db_session)).count(True) == ACCOUNT_BURST
        assert await _count(db_session, PatientDevice) <= DEVICES_PER_ACCOUNT

    async def test_one_address_gets_thirty_codes_a_minute_however_many_devices_it_holds(
        self, net: _Net, db_session: AsyncSession, clock: ThrottleClock
    ) -> None:
        """Attack: one host holding recognised devices of many accounts (a SIM farm).

        Each account's allowance is its own, so the per-source budget has to
        apply to recognised requests too: thirty codes, then one a minute.
        """
        accounts = [(phone, await net.sign_in(phone)) for phone in (new_phone() for _ in range(11))]
        farm = _source()

        statuses = [
            (await net.ask(phone, source=farm, device=session.device)).status_code
            for phone, session in accounts
            for _ in range(DEVICE_BURST)
        ]

        assert statuses == [202] * SOURCE_BURST + [429] * 3
        assert (await _recognised(db_session)).count(True) == SOURCE_BURST
        assert await _has_bucket(db_session, BucketKind.PT_OTP_SEND_SOURCE, farm)
        # The same device from another address is not held back by the farm's budget.
        last_phone, last_session = accounts[-1]
        elsewhere = await net.ask(last_phone, device=last_session.device)
        assert elsewhere.status_code == 202
        clock.advance(seconds=61)
        refilled = [
            (await net.ask(last_phone, source=farm, device=last_session.device)).status_code
            for _ in range(2)
        ]
        assert refilled == [202, 429]

    async def test_a_request_with_no_usable_address_is_still_held_to_the_account_bound(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: arrive with no address, so that the per-source budget is left out."""
        phone = new_phone()
        session = await net.sign_in(phone)

        async def _ask_from_nowhere(device: str) -> int:
            async with AsyncClient(
                transport=ASGITransport(app=net.application, client=("not-an-address", 0)),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    REQUEST, json={"phone": phone}, headers={"Cookie": f"{DEVICE_COOKIE}={device}"}
                )
                return response.status_code

        statuses = [await _ask_from_nowhere(session.device) for _ in range(DEVICE_BURST + 2)]

        assert statuses == [202] * DEVICE_BURST + [429] * 2
        kinds = await _bucket_kinds(db_session)
        assert kinds["pt_otp_send_device"] == 1
        assert await _has_bucket(
            db_session, BucketKind.PT_OTP_SEND_PHONE, "recognised", session.account_id
        )

    async def test_the_owners_recognised_path_does_not_draw_on_the_numbers_shared_budget(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Both directions: neither budget can be used to empty the other.

        The owner's recognised requests leave the number's budget for
        unrecognised callers whole, and a flood of unrecognised requests
        leaves the owner's recognised budget whole.
        """
        phone = new_phone()
        owner = await net.sign_in(phone)  # one unrecognised code: the first sign-in

        recognised = [
            (await net.ask(phone, device=owner.device)).status_code for _ in range(DEVICE_BURST)
        ]
        strangers = [(await net.ask(phone)).status_code for _ in range(PHONE_BURST + 2)]

        assert recognised == [202] * DEVICE_BURST
        # The number's own budget lost only the first sign-in's code.
        assert strangers == [202] * (PHONE_BURST - 1) + [429] * 3
        # Two rows under one policy, in separate key spaces.
        assert await _has_bucket(db_session, BucketKind.PT_OTP_SEND_PHONE, phone)
        assert await _has_bucket(
            db_session, BucketKind.PT_OTP_SEND_PHONE, "recognised", owner.account_id
        )
        assert (await _bucket_kinds(db_session))["pt_otp_send_phone"] == 2
        assert (
            bucket(BucketKind.PT_OTP_SEND_PHONE, phone).key_hash
            != bucket(BucketKind.PT_OTP_SEND_PHONE, "recognised", owner.account_id).key_hash
        )

    async def test_a_sign_in_from_the_recognised_browser_gives_no_code_back(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """A send is never refunded: signing in does not reload the device's allowance."""
        phone = new_phone()
        device = (await net.sign_in(phone)).device

        for _ in range(DEVICE_BURST):
            device = (await net.sign_in(phone, device=device)).device

        assert (await net.ask(phone, device=device)).status_code == 429
        assert await _count(db_session, PatientDevice) == 1


# ── 5. A copied device token ─────────────────────────────────────────────────


class TestACopiedDeviceToken:
    async def test_the_copy_and_the_original_share_one_allowance(
        self, net: _Net, db_session: AsyncSession, clock: ThrottleClock
    ) -> None:
        """Attack: copy the cookie to a second machine to get a second allowance."""
        phone = new_phone()
        owner = await net.sign_in(phone)
        original, copy = _source(), _source()

        statuses = [
            (await net.ask(phone, source=original, device=owner.device)).status_code,
            (await net.ask(phone, source=copy, device=owner.device)).status_code,
            (await net.ask(phone, source=original, device=owner.device)).status_code,
            (await net.ask(phone, source=copy, device=owner.device)).status_code,
            (await net.ask(phone, source=original, device=owner.device)).status_code,
        ]

        assert statuses == [202, 202, 202, 429, 429]
        assert (await _bucket_kinds(db_session))["pt_otp_send_device"] == 1
        assert await _count(db_session, PatientDevice) == 1
        # One comes back per ten minutes — to both, because it is one bucket.
        clock.advance(minutes=10, seconds=1)
        assert (await net.ask(phone, source=copy, device=owner.device)).status_code == 202
        assert (await net.ask(phone, source=original, device=owner.device)).status_code == 429

    async def test_many_copies_at_many_addresses_are_still_one_device(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        owner = await net.sign_in(phone)

        statuses = [(await net.ask(phone, device=owner.device)).status_code for _ in range(12)]

        assert statuses == [202] * DEVICE_BURST + [429] * 9
        assert len(net.sms.sent) == 1 + DEVICE_BURST

    async def test_the_same_token_twice_in_one_cookie_is_one_token(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: repeat or pad the cookie to be counted more than once."""
        phone = new_phone()
        owner = await net.sign_in(phone)
        padded = ".".join([owner.device, owner.device, "A" * 43, owner.device])

        statuses = [(await net.ask(phone, device=padded)).status_code for _ in range(4)]

        assert statuses == [202] * DEVICE_BURST + [429]
        assert (await _bucket_kinds(db_session))["pt_otp_send_device"] == 1


# ── 6. Dead devices ──────────────────────────────────────────────────────────


class TestADeadDeviceIsNotRecognised:
    async def _is_recognised(
        self, net: _Net, session: AsyncSession, phone: str, token: str
    ) -> bool:
        return await _was_recognised(session, await net.ask(phone, device=token))

    async def test_a_deleted_row_is_not_recognised(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        owner = await net.sign_in(phone)
        assert await self._is_recognised(net, db_session, phone, owner.device) is True

        await db_session.execute(delete(PatientDevice))
        await db_session.commit()

        assert await self._is_recognised(net, db_session, phone, owner.device) is False

    async def test_logging_out_everywhere_forgets_every_device_of_the_account(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Pinned behaviour: "log out everywhere" deletes the account's device rows.

        ``PatientAuthService.logout_all`` revokes every session **and** calls
        ``PatientDeviceRepository.delete_for_account``. So a device cookie
        that was copied or stolen stops being recognised the moment the owner
        signs out everywhere — the remedy for a stolen cookie. Only that
        account's devices go; another account on the same browser keeps its
        own.
        """
        phone, other_phone = new_phone(), new_phone()
        first = await net.sign_in(phone)
        second = await net.sign_in(phone)  # a second browser of the same account
        other = await net.sign_in(other_phone)
        assert await _count(db_session, PatientDevice) == 3

        signed_out = await net.send("POST", LOGOUT_ALL, headers=first.headers)

        assert signed_out.status_code == 204
        remaining = await _all(db_session, PatientDevice)
        assert [row.account_id for row in remaining] == [other.account_id]
        assert await self._is_recognised(net, db_session, phone, first.device) is False
        assert await self._is_recognised(net, db_session, phone, second.device) is False
        assert await self._is_recognised(net, db_session, other_phone, other.device) is True

    async def test_signing_out_of_one_session_does_not_forget_the_device(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Pinned behaviour: an ordinary sign-out keeps the browser recognised."""
        phone = new_phone()
        owner = await net.sign_in(phone)

        signed_out = await net.send(
            "POST", LOGOUT, cookies={REFRESH_COOKIE: owner.refresh_token}, headers=CSRF
        )

        assert signed_out.status_code == 204
        assert await self._is_recognised(net, db_session, phone, owner.device) is True

    async def test_an_expired_row_is_not_recognised(
        self, net: _Net, db_session: AsyncSession, clock: ThrottleClock
    ) -> None:
        """Ninety days without a sign-in end recognition — to the second."""
        phone = new_phone()
        owner = await net.sign_in(phone)

        clock.advance(days=89, hours=23)
        still = await self._is_recognised(net, db_session, phone, owner.device)
        clock.advance(hours=1, seconds=1)
        lapsed = await net.ask(phone, device=owner.device)

        assert still is True
        assert await _was_recognised(db_session, lapsed) is False

    async def test_a_row_expiring_at_this_instant_is_already_dead(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """The boundary: ``expires_at`` must be strictly in the future."""
        phone = new_phone()
        owner = await net.sign_in(phone)
        now = await AuthThrottleRepository(db_session).now()

        for expires_at, expected in (
            (now + timedelta(minutes=5), True),
            (now - timedelta(seconds=1), False),
            (now - timedelta(days=400), False),
        ):
            await db_session.execute(update(PatientDevice).values(expires_at=expires_at))
            await db_session.commit()
            assert await self._is_recognised(net, db_session, phone, owner.device) is expected

    async def test_an_expired_device_does_not_come_back_by_signing_in_with_it(
        self, net: _Net, db_session: AsyncSession, clock: ThrottleClock
    ) -> None:
        """A lapsed token is not revived: the sign-in mints a new one and the old stays dead."""
        phone = new_phone()
        owner = await net.sign_in(phone)
        clock.advance(days=91)

        again = await net.sign_in(phone, device=owner.device)

        assert owner.device not in again.device.split(".")
        assert await self._is_recognised(net, db_session, phone, owner.device) is False
        assert await self._is_recognised(net, db_session, phone, again.device) is True

    async def test_a_pruned_device_is_not_recognised_and_at_most_five_are_kept(
        self, net: _Net, db_session: AsyncSession, clock: ThrottleClock
    ) -> None:
        """Attack: pile up recognised devices on one account.

        Each sign-in without a cookie mints one; the account keeps five. A
        token whose row was dropped is nobody's device.
        """
        phone = new_phone()
        tokens = []
        for _ in range(DEVICES_PER_ACCOUNT + 3):
            tokens.append((await net.sign_in(phone)).device)
            clock.advance(minutes=6)  # the number's own budget: one code back per five minutes

        rows = await _all(db_session, PatientDevice)
        assert len(rows) == DEVICES_PER_ACCOUNT
        live = {row.token_hash for row in rows}
        dropped = [token for token in tokens if hash_token(token) not in live]
        assert len(dropped) == 3
        clock.advance(minutes=30)
        for token in dropped:
            assert await self._is_recognised(net, db_session, phone, token) is False
        assert await self._is_recognised(net, db_session, phone, tokens[-1]) is True

    @pytest.mark.parametrize(
        "cookie",
        [
            "",
            "x",
            "A" * 43,
            "A" * 31,
            "A" * 65,
            "not a token at all!",
            "../../etc/passwd",
            "%00%00%00",
            "'; DROP TABLE patient_devices; --",
            ".".join(["B" * 43] * 64),
        ],
    )
    async def test_a_made_up_or_malformed_cookie_is_nobody(
        self, net: _Net, db_session: AsyncSession, cookie: str
    ) -> None:
        phone = new_phone()
        await net.sign_in(phone)

        response = await net.ask(phone, device=cookie)

        assert await _was_recognised(db_session, response) is False
        assert await _count(db_session, PatientDevice) == 1


# ── 7. Somebody who knows only the number ────────────────────────────────────


class TestAnAttackerWhoKnowsOnlyTheNumber:
    async def test_from_one_address_they_cannot_stop_the_owners_recognised_device(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack (lock-out): hammer the victim's number from one machine."""
        phone = new_phone()
        owner = await net.sign_in(phone)
        attacker = _source()

        flood = [(await net.ask(phone, source=attacker)).status_code for _ in range(40)]
        owner_challenge, owner_code = await net.code(phone, device=owner.device)
        signed_in = await net.finish(owner_challenge, owner_code, device=owner.device)

        assert flood == [202] * PAIR_BURST + [429] * 37
        assert signed_in.account_id == owner.account_id
        assert (await _recognised(db_session)).count(True) == 1

    async def test_from_many_addresses_they_cannot_stop_the_owners_recognised_device(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack (lock-out, distributed): empty the number's budget from a botnet and keep it empty.

        The number's shared budget is gone — a stranger is refused — and the
        owner's browser, which draws on budgets an outsider cannot reach,
        still gets its three codes and signs in.
        """
        phone = new_phone()
        owner = await net.sign_in(phone)

        flood = [(await net.ask(phone)).status_code for _ in range(60)]
        stranger = await net.ask(phone)
        owner_codes = [
            (await net.ask(phone, device=owner.device)).status_code for _ in range(DEVICE_BURST - 1)
        ]
        challenge_id, code = await net.code(phone, device=owner.device)
        signed_in = await net.finish(challenge_id, code, device=owner.device)

        assert flood.count(202) == PHONE_BURST - 1
        assert flood[PHONE_BURST - 1 :] == [429] * (60 - (PHONE_BURST - 1))
        assert stranger.status_code == 429
        assert owner_codes == [202] * (DEVICE_BURST - 1)
        assert signed_in.account_id == owner.account_id
        assert (await _recognised(db_session)).count(True) == DEVICE_BURST

    async def test_the_flood_goes_on_and_the_owner_keeps_being_served(
        self, net: _Net, db_session: AsyncSession, clock: ThrottleClock
    ) -> None:
        """An attacker who never stops: every refill of the number's budget is taken at once."""
        phone = new_phone()
        owner = await net.sign_in(phone)
        device = owner.device

        for _ in range(6):
            for _ in range(10):
                await net.ask(phone)  # the botnet takes whatever the number's budget has
            assert (await net.ask(phone)).status_code == 429
            # The owner signs in from their browser, as they do every visit.
            device = (await net.sign_in(phone, device=device)).device
            clock.advance(minutes=30)

        assert await _count(db_session, PatientDevice) == 1
        assert await _count(db_session, PatientRefreshToken) == 7

    async def test_guessing_device_tokens_gets_nowhere(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: send well-formed random tokens hoping one names the victim's device."""
        phone = new_phone()
        await net.sign_in(phone)
        guesses = ".".join(secrets.token_urlsafe(32) for _ in range(32))

        statuses = [(await net.ask(phone, device=guesses)).status_code for _ in range(PHONE_BURST)]

        # Unrecognised, so it is charged like any stranger and runs out like one.
        assert statuses == [202] * (PHONE_BURST - 1) + [429]
        assert not any(await _recognised(db_session))

    async def test_an_attacker_on_the_owners_own_address_shares_only_that_addresses_budget(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """What the per-source budget costs, pinned so it is a known quantity.

        A recognised request is also charged to its address, so somebody
        behind the *same* address as the owner who spends that address's
        thirty codes a minute (on other numbers — the victim's own number
        gives them three) holds the owner up *at that address*. The owner is
        served at once from any other network, where the same cookie and the
        same account budget apply.
        """
        phone = new_phone()
        owner = await net.sign_in(phone)
        shared = _source()

        burned = [(await net.ask(new_phone(), source=shared)).status_code for _ in range(31)]
        at_home = await net.ask(phone, source=shared, device=owner.device)
        on_mobile_data = await net.ask(phone, device=owner.device)

        assert burned == [202] * SOURCE_BURST + [429]
        assert at_home.status_code == 429
        assert await _was_recognised(db_session, on_mobile_data) is True


# ── 8. Somebody who stole the cookie ─────────────────────────────────────────


class TestAStolenDeviceCookie:
    async def test_it_gives_no_session_and_no_account_data(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """Attack: the thief has the cookie and the number — everything but the handset."""
        phone = new_phone()
        owner = await net.sign_in(phone)
        stolen = owner.device
        thief = _source()

        asked = await net.ask(phone, source=thief, device=stolen)
        challenge_id = asked.json()["data"]["challenge_id"]
        guesses = [
            await net.verify(challenge_id, f"{n:06d}", source=thief, device=stolen)
            for n in range(5)
        ]
        elsewhere = [
            await net.send("GET", ME, source=thief, device=stolen),
            await net.send("POST", REFRESH, source=thief, device=stolen, headers=CSRF),
            await net.send(
                "POST", REFRESH, source=thief, cookies={REFRESH_COOKIE: stolen}, headers=CSRF
            ),
            await net.send("POST", LOGOUT_ALL, source=thief, headers=bearer(stolen)),
        ]

        real_code = net.sms.code_for(phone)
        if any(guess.status_code == 200 for guess in guesses):  # pragma: no cover — 5 in 10^6
            pytest.skip("a sequential guess happened to be the code")
        assert real_code not in {f"{n:06d}" for n in range(5)}
        assert [guess.status_code for guess in guesses] == [401] * 5
        assert all(_body(guess) == OTP_INVALID for guess in guesses)
        assert [answer.status_code for answer in elsewhere] == [401] * 4
        for answer in (asked, *guesses, *elsewhere):
            assert "access_token" not in answer.text
            assert str(owner.account_id) not in answer.text
            assert phone[-4:] not in answer.text
        assert await _count(db_session, PatientRefreshToken) == 1
        assert await _count(db_session, PatientRefreshToken, PatientRefreshToken.is_revoked) == 0

    async def test_it_gives_no_more_codes_than_the_one_accounts_recognised_allowance(
        self, net: _Net, db_session: AsyncSession, clock: ThrottleClock
    ) -> None:
        """Attack: pump texts to the owner's handset with the stolen cookie, from everywhere.

        Without the handset the thief cannot complete a sign-in, so cannot
        mint a second device: the one stolen device's three codes are all
        there is — inside the account's six — and one more per ten minutes.
        """
        phone = new_phone()
        owner = await net.sign_in(phone)
        stolen = owner.device
        sent_before = len(net.sms.sent)

        statuses = [(await net.ask(phone, device=stolen)).status_code for _ in range(50)]
        clock.advance(minutes=10, seconds=1)
        later = [(await net.ask(phone, device=stolen)).status_code for _ in range(10)]

        assert statuses == [202] * DEVICE_BURST + [429] * 47
        assert later == [202] + [429] * 9
        assert len(net.sms.sent) - sent_before == DEVICE_BURST + 1
        assert DEVICE_BURST + 1 <= ACCOUNT_BURST
        assert await _count(db_session, PatientDevice) == 1

    async def test_the_owner_can_still_sign_in_from_another_source_and_then_kill_the_cookie(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """The thief has drained the stolen device; the owner is not locked out.

        The owner's own browser shares that device's allowance (a copy is the
        same device), so it is refused while the thief holds it empty —
        pinned here. But the number's budget for unrecognised callers is
        untouched by anything a recognised device does: from another browser
        or a private window the owner gets a code, signs in, and "log out
        everywhere" then makes the stolen cookie worthless.
        """
        phone = new_phone()
        owner = await net.sign_in(phone)
        stolen = owner.device
        for _ in range(20):
            await net.ask(phone, device=stolen)

        own_browser = await net.ask(phone, device=owner.device)
        fresh = await net.sign_in(phone)  # no cookie, another address
        signed_out = await net.send("POST", LOGOUT_ALL, headers=fresh.headers)
        thief_after = await net.ask(phone, device=stolen)

        assert own_browser.status_code == 429  # pinned: one device, one allowance
        assert fresh.account_id == owner.account_id
        assert signed_out.status_code == 204
        assert await _count(db_session, PatientDevice) == 0
        # The thief's cookie is now an unknown value: charged as a stranger.
        assert await _was_recognised(db_session, thief_after) is False

    async def test_the_thief_cannot_keep_the_owners_new_device_from_working(
        self, net: _Net, db_session: AsyncSession
    ) -> None:
        """After the theft: the owner's newly recognised browser has an allowance of its own.

        The stolen device spent three of the account's six. The owner's new
        device still gets the other three — the thief cannot spend those,
        because the stolen device's own bucket is empty.
        """
        phone = new_phone()
        stolen = (await net.sign_in(phone)).device
        thief = [(await net.ask(phone, device=stolen)).status_code for _ in range(10)]

        owner = await net.sign_in(phone)  # another browser: a new device row
        owner_codes = [
            (await net.ask(phone, device=owner.device)).status_code for _ in range(DEVICE_BURST + 1)
        ]

        assert thief == [202] * DEVICE_BURST + [429] * 7
        assert owner_codes == [202] * DEVICE_BURST + [429]
        assert (await _recognised(db_session)).count(True) == ACCOUNT_BURST


# ── 9. The same, at the same moment ──────────────────────────────────────────


class _RealDatabase:
    """A pool of real connections to the test database. Everything is committed."""

    def __init__(self, engine: AsyncEngine, sms: FakeSmsSender) -> None:
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        self.phones: list[str] = []
        self.device_ids: set[uuid.UUID] = set()
        application = create_app()
        # The application's own request-scoped dependency then opens one real
        # session (one pooled connection) per request, as it does in service.
        application.state.db_session_factory = self.factory
        application.dependency_overrides[get_sms_sender] = lambda: sms
        self.net = _Net(application, sms)

    def phone(self) -> str:
        number = new_phone()
        self.phones.append(number)
        return number

    async def sign_in(self, phone: str, *, device: str | None = None) -> _Session:
        session = await self.net.sign_in(phone, device=device)
        await self.remember_devices()
        return session

    async def remember_devices(self) -> None:
        """Note every device row of this test's accounts, so its bucket can be deleted."""
        async with self.factory() as session:
            ids = await session.execute(
                select(PatientDevice.id)
                .join(PatientAccount, PatientAccount.id == PatientDevice.account_id)
                .where(PatientAccount.phone.in_(self.phones))
            )
            self.device_ids.update(ids.scalars().all())

    async def recognised(self, phone: str) -> list[bool]:
        async with self.factory() as session:
            rows = await audit_rows(session, "patient.auth.otp_requested")
            mine = await session.execute(
                select(PatientOtpChallenge.id).where(PatientOtpChallenge.phone == phone)
            )
            challenges = set(mine.scalars().all())
        return [
            bool((row.context or {})["recognised_device"])
            for row in rows
            if row.target_id in challenges
        ]

    async def cleanup(self) -> None:
        """Delete every row this test committed: by phone, by account, by throttle key."""
        self.net.application.dependency_overrides.clear()
        await self.remember_devices()
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
            keys = {bucket(BucketKind.PT_OTP_SEND_GLOBAL).key_hash}
            for source in self.net.sources:
                keys.add(bucket(BucketKind.PT_OTP_SEND_SOURCE, source).key_hash)
                keys.add(bucket(BucketKind.PT_OTP_VERIFY_SOURCE, source).key_hash)
                for phone in self.phones:
                    keys.add(bucket(BucketKind.PT_OTP_SEND_PAIR, phone, source).key_hash)
            for phone in self.phones:
                keys.add(bucket(BucketKind.PT_OTP_SEND_PHONE, phone).key_hash)
            for account_id in accounts:
                keys.add(bucket(BucketKind.PT_OTP_SEND_PHONE, "recognised", account_id).key_hash)
            for device_id in self.device_ids:
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


class TestConcurrentRequestsOnADevice:
    """Requests that arrive at the same moment, each on its own connection.

    The single-connection ``db_session`` used elsewhere cannot show this: on
    it two requests can never be inside the throttle at once.
    """

    async def test_parallel_requests_on_one_device_never_exceed_its_allowance(
        self, real_db: _RealDatabase, sms: FakeSmsSender
    ) -> None:
        """Attack: fire the stolen cookie many times at once, each seeing "room left"."""
        phone = real_db.phone()
        owner = await real_db.sign_in(phone)
        sent_before = len(sms.sent)

        responses = await asyncio.gather(
            *(real_db.net.ask(phone, device=owner.device) for _ in range(16))
        )

        statuses = sorted(response.status_code for response in responses)
        assert statuses == [202] * DEVICE_BURST + [429] * 13
        assert len(sms.sent) - sent_before == DEVICE_BURST
        assert (await real_db.recognised(phone)).count(True) == DEVICE_BURST
        assert all(
            _body(response) == OTP_THROTTLED
            for response in responses
            if response.status_code == 429
        )

    async def test_parallel_requests_across_every_device_never_exceed_the_accounts_allowance(
        self, real_db: _RealDatabase, sms: FakeSmsSender
    ) -> None:
        """Attack: five recognised devices of one account, all fired at the same instant.

        Each device row alone would allow three — fifteen in all. The
        account's budget allows six, and under real concurrency it is exactly
        six: a request the account refuses charges nothing to its device.
        """
        phone = real_db.phone()
        devices = [(await real_db.sign_in(phone)).device for _ in range(DEVICES_PER_ACCOUNT)]
        sent_before = len(sms.sent)
        assert len(set(devices)) == DEVICES_PER_ACCOUNT

        responses = await asyncio.gather(
            *(real_db.net.ask(phone, device=device) for device in devices for _ in range(4))
        )

        statuses = sorted(response.status_code for response in responses)
        assert statuses == [202] * ACCOUNT_BURST + [429] * (20 - ACCOUNT_BURST)
        assert len(sms.sent) - sent_before == ACCOUNT_BURST
        assert (await real_db.recognised(phone)).count(True) == ACCOUNT_BURST
        # A second wave finds nothing left on any of them.
        again = await asyncio.gather(
            *(real_db.net.ask(phone, device=device) for device in devices for _ in range(2))
        )
        assert {response.status_code for response in again} == {429}

    async def test_parallel_sign_ins_without_the_cookie_mint_no_extra_allowance(
        self, real_db: _RealDatabase, sms: FakeSmsSender
    ) -> None:
        """Attack: verify several codes at once without the cookie, then spend every new device.

        The minting race: each verification creates a device row. However
        many rows come out of it, what they can send together is the
        account's six.
        """
        phone = real_db.phone()
        first = await real_db.sign_in(phone)
        challenges = [
            await real_db.net.code(phone, device=first.device) for _ in range(DEVICE_BURST)
        ]

        sessions = await asyncio.gather(
            *(real_db.net.finish(challenge_id, code) for challenge_id, code in challenges)
        )
        await real_db.remember_devices()
        minted = [first.device, *(session.device for session in sessions)]
        responses = await asyncio.gather(
            *(real_db.net.ask(phone, device=device) for device in minted for _ in range(3))
        )

        assert len(set(minted)) == 4
        # Three were already spent on the challenges above: three are left, in total.
        assert sorted(r.status_code for r in responses) == [202] * 3 + [429] * 9
        assert (await real_db.recognised(phone)).count(True) == ACCOUNT_BURST
