"""API tests for Patient App sign-in — one-time codes, sessions, ``GET /patient/me``.

``docs/modules/15-patient-app.md`` §5 and §27.3. Every test drives the real
application over HTTP against a real PostgreSQL, through the real throttle and
the real cookies. Only the SMS transport is stood in for
(``app/tests/patient_app_helpers.py``).

These are the functional tests: each endpoint's happy path and each documented
refusal. Time is moved only through the throttle's clock; nothing sleeps.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_patient_access_token,
    hash_token,
)
from app.models.auth_throttle import AuthThrottleBucket
from app.models.patient_account import (
    PatientAccount,
    PatientDevice,
    PatientOtpChallenge,
    PatientRefreshToken,
)
from app.models.user import User
from app.services.patient_app import patient_auth_service as auth_module
from app.tests.patient_app_helpers import (
    CSRF,
    DEVICE_COOKIE,
    PATIENT,
    REFRESH_COOKIE,
    FakeSmsSender,
    ThrottleClock,
    audit_rows,
    bearer,
    build_patient_application,
    new_phone,
    patient_client,
    set_cookie_headers,
    sign_in,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

REQUEST = f"{PATIENT}/auth/otp/request"
VERIFY = f"{PATIENT}/auth/otp/verify"
REFRESH = f"{PATIENT}/auth/refresh"
LOGOUT = f"{PATIENT}/auth/logout"
LOGOUT_ALL = f"{PATIENT}/auth/logout-all"
ME = f"{PATIENT}/me"

# Documented numbers, restated on purpose: a change to the policy has to be
# made here too.
PAIR_BURST = 3
ATTEMPTS_PER_CHALLENGE = 5
OTP_LIFETIME_SECONDS = 300


@pytest.fixture(autouse=True)
def _patient_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain-HTTP cookies (the client talks to ``http://test``), and no request limiter."""
    monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)


@pytest.fixture
def sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ThrottleClock:
    return ThrottleClock(monkeypatch)


@pytest_asyncio.fixture
async def application(db_session: AsyncSession, sms: FakeSmsSender) -> AsyncGenerator[FastAPI]:
    app = build_patient_application(db_session, sms)
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def browser(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with patient_client(application) as client:
        yield client


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    result = await session.execute(select(func.count()).select_from(model).where(*where))
    return int(result.scalar_one())


async def _account(session: AsyncSession, phone: str) -> PatientAccount | None:
    result = await session.execute(
        select(PatientAccount)
        .where(PatientAccount.phone == phone)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _request_code(client: AsyncClient, phone: str) -> Response:
    return await client.post(REQUEST, json={"phone": phone})


async def _challenge(client: AsyncClient, phone: str) -> str:
    response = await _request_code(client, phone)
    assert response.status_code == 202, response.text
    return str(response.json()["data"]["challenge_id"])


def _error(response: Response) -> tuple[int, str, str]:
    body = response.json()
    return response.status_code, body["error_code"], body["message"]


# ── POST /auth/otp/request ───────────────────────────────────────────────────


class TestRequestOtp:
    async def test_it_sends_one_code_and_answers_202(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        phone = new_phone()

        response = await _request_code(browser, phone)

        assert response.status_code == 202, response.text
        data = response.json()["data"]
        assert set(data) == {"challenge_id", "expires_in", "resend_after"}
        assert data["expires_in"] == OTP_LIFETIME_SECONDS
        assert data["resend_after"] == 60
        uuid.UUID(data["challenge_id"])
        assert [message.to for message in sms.sent] == [phone]
        assert sms.sent[0].purpose == "otp"
        assert len(sms.code_for(phone)) == 6

    async def test_the_message_is_the_code_and_its_lifetime_and_nothing_else(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        phone = new_phone()
        await _request_code(browser, phone)

        body = sms.sent[0].body

        assert (
            body
            == f"{sms.code_for(phone)} is your Aetheris verification code. It expires in 5 minutes."
        )

    async def test_the_number_is_normalised_before_anything_is_sent(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        phone = new_phone()

        response = await _request_code(browser, f" {phone[3:8]} {phone[8:]} ")

        assert response.status_code == 202, response.text
        assert sms.sent[0].to == phone

    async def test_the_code_is_stored_only_as_a_keyed_hash(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)
        code = sms.code_for(phone)

        row = (
            await db_session.execute(
                select(PatientOtpChallenge).where(PatientOtpChallenge.id == uuid.UUID(challenge_id))
            )
        ).scalar_one()

        assert row.phone == phone
        assert row.attempts == 0
        assert row.consumed_at is None
        assert len(row.code_hash) == 64
        assert code not in row.code_hash
        assert row.code_hash != hash_token(code)  # not a plain SHA-256 of the code

    async def test_no_account_is_created_or_looked_up_by_asking_for_a_code(
        self, browser: AsyncClient, db_session: AsyncSession
    ) -> None:
        phone = new_phone()

        await _request_code(browser, phone)

        assert await _account(db_session, phone) is None

    async def test_the_answer_is_the_same_for_a_known_and_an_unknown_number(
        self, application: FastAPI, sms: FakeSmsSender
    ) -> None:
        known, unknown = new_phone(), new_phone()
        async with patient_client(application) as first:
            await sign_in(first, sms, known)
        async with patient_client(application) as stranger:
            for_known = await _request_code(stranger, known)
            for_unknown = await _request_code(stranger, unknown)

        assert for_known.status_code == for_unknown.status_code == 202
        assert for_known.json()["message"] == for_unknown.json()["message"]
        assert set(for_known.json()["data"]) == set(for_unknown.json()["data"])
        assert for_known.json()["data"]["expires_in"] == for_unknown.json()["data"]["expires_in"]
        assert set_cookie_headers(for_known) == set_cookie_headers(for_unknown) == []

    async def test_it_is_audited_without_the_phone_or_the_code(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)

        [row] = await audit_rows(db_session, "patient.auth.otp_requested")

        assert row.actor_type == "patient"
        assert row.hospital_id is None
        assert row.actor_user_id is None
        assert row.patient_account_id is None
        assert row.target_id == uuid.UUID(challenge_id)
        stored = json.dumps(row.context)
        assert phone not in stored
        assert phone[3:] not in stored
        assert sms.code_for(phone) not in stored
        assert row.context is not None
        assert len(row.context["phone_ref"]) == 16

    @pytest.mark.parametrize("phone", ["+14155550123", "+447911123456", "12345", "+915812345678"])
    async def test_a_number_outside_the_allowed_countries_is_refused(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession, phone: str
    ) -> None:
        response = await _request_code(browser, phone)

        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == "VALIDATION_ERROR"
        assert sms.sent == []
        assert await _count(db_session, PatientOtpChallenge) == 0

    @pytest.mark.parametrize(
        "body", [{}, {"phone": ""}, {"phone": new_phone(), "hospital_id": str(uuid.uuid4())}]
    )
    async def test_a_malformed_body_is_refused(
        self, browser: AsyncClient, sms: FakeSmsSender, body: dict[str, Any]
    ) -> None:
        response = await browser.post(REQUEST, json=body)

        assert response.status_code == 422, response.text
        assert sms.sent == []

    async def test_with_no_sms_sender_it_answers_503_and_creates_nothing(
        self, db_session: AsyncSession
    ) -> None:
        application = build_patient_application(db_session, None)
        async with patient_client(application) as client:
            response = await _request_code(client, new_phone())

        assert _error(response)[:2] == (503, "SERVICE_UNAVAILABLE")
        assert await _count(db_session, PatientOtpChallenge) == 0
        # Nothing was charged either: there was nothing to charge for.
        assert (
            await _count(db_session, AuthThrottleBucket, AuthThrottleBucket.kind.like("pt_%")) == 0
        )
        assert await audit_rows(db_session, "patient.auth.otp_requested") == []

    async def test_when_the_send_fails_it_answers_503_and_discards_the_challenge(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        sms.fail = True

        response = await _request_code(browser, new_phone())

        assert _error(response)[:2] == (503, "SERVICE_UNAVAILABLE")
        assert await _count(db_session, PatientOtpChallenge) == 0
        assert await audit_rows(db_session, "patient.auth.otp_requested") == []

    async def test_a_send_that_hangs_is_cut_off_and_treated_as_a_failure(
        self,
        browser: AsyncClient,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio

        async def _hang(message: Any) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(sms, "send", _hang)
        monkeypatch.setattr(settings, "SMS_TIMEOUT_SECONDS", 0.01)

        response = await _request_code(browser, new_phone())

        assert response.status_code == 503
        assert await _count(db_session, PatientOtpChallenge) == 0

    async def test_a_number_from_one_source_gets_three_codes_and_then_429(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        for _ in range(PAIR_BURST):
            assert (await _request_code(browser, phone)).status_code == 202

        refused = await _request_code(browser, phone)

        assert _error(refused) == (
            429,
            "OTP_THROTTLED",
            "Too many requests. Please try again later.",
        )
        assert refused.headers["retry-after"] == "600"
        assert len(sms.sent) == PAIR_BURST
        assert await _count(db_session, PatientOtpChallenge) == PAIR_BURST

    async def test_the_allowance_comes_back_with_time(
        self, browser: AsyncClient, clock: ThrottleClock
    ) -> None:
        phone = new_phone()
        for _ in range(PAIR_BURST):
            await _request_code(browser, phone)
        assert (await _request_code(browser, phone)).status_code == 429

        clock.advance(minutes=10, seconds=1)

        assert (await _request_code(browser, phone)).status_code == 202


# ── POST /auth/otp/verify ────────────────────────────────────────────────────


class TestVerifyOtp:
    async def test_the_right_code_starts_a_session(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)

        response = await browser.post(
            VERIFY, json={"challenge_id": challenge_id, "code": sms.code_for(phone)}
        )

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == {"access_token", "expires_in", "account", "pending_policies"}
        assert data["expires_in"] == 900
        assert data["pending_policies"] == []
        assert set(data["account"]) == {"id", "phone_masked", "status"}
        assert data["account"]["status"] == "active"
        assert data["account"]["phone_masked"].endswith(phone[-4:])
        assert phone not in response.text
        assert response.headers["cache-control"] == "no-store"

        account = await _account(db_session, phone)
        assert account is not None
        assert str(account.id) == data["account"]["id"]
        assert account.phone_verified_at is not None
        assert account.last_login_at is not None

    async def test_the_refresh_token_is_only_ever_in_the_cookie(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)

        response = await browser.post(
            VERIFY, json={"challenge_id": challenge_id, "code": sms.code_for(phone)}
        )

        token = browser.cookies.get(REFRESH_COOKIE)
        assert token is not None
        assert len(token) == 64
        assert token not in response.text
        assert "refresh_token" not in response.text

    async def test_the_cookies_are_httponly_strict_and_scoped(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)

        response = await browser.post(
            VERIFY, json={"challenge_id": challenge_id, "code": sms.code_for(phone)}
        )

        cookies = {
            header.split("=", 1)[0]: header.lower() for header in set_cookie_headers(response)
        }
        assert set(cookies) == {REFRESH_COOKIE, DEVICE_COOKIE}
        refresh, device = cookies[REFRESH_COOKIE], cookies[DEVICE_COOKIE]
        for cookie in (refresh, device):
            assert "httponly" in cookie
            assert "samesite=strict" in cookie
            assert "domain=" not in cookie
        assert "path=/api/v1/patient/auth" in refresh
        assert "max-age=604800" in refresh
        assert "path=/;" in device or device.rstrip().endswith("path=/")

    async def test_in_production_the_cookies_are_secure_and_prefixed(
        self,
        application: FastAPI,
        sms: FakeSmsSender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", True)
        phone = new_phone()
        async with patient_client(application) as client:
            client.base_url = "https://test"
            challenge_id = await _challenge(client, phone)
            response = await client.post(
                VERIFY, json={"challenge_id": challenge_id, "code": sms.code_for(phone)}
            )

        cookies = {
            header.split("=", 1)[0]: header.lower() for header in set_cookie_headers(response)
        }
        assert set(cookies) == {"__Secure-atheris-patient-refresh", "__Host-atheris-patient-device"}
        for cookie in cookies.values():
            assert "secure" in cookie.split("; ")
            assert "httponly" in cookie
            assert "samesite=strict" in cookie
            assert "domain=" not in cookie
        assert "path=/api/v1/patient/auth" in cookies["__Secure-atheris-patient-refresh"]

    async def test_the_first_sign_in_creates_the_account_and_later_ones_do_not(
        self, application: FastAPI, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        async with patient_client(application) as first:
            one = await sign_in(first, sms, phone)
        async with patient_client(application) as second:
            two = await sign_in(second, sms, phone)

        assert one["account"]["id"] == two["account"]["id"]
        assert await _count(db_session, PatientAccount, PatientAccount.phone == phone) == 1
        created = await audit_rows(db_session, "patient.auth.account_created")
        logins = await audit_rows(db_session, "patient.auth.login")
        assert len(created) == 1
        assert len(logins) == 2
        for row in (*created, *logins):
            assert row.actor_type == "patient"
            assert str(row.patient_account_id) == one["account"]["id"]
            assert row.actor_user_id is None
            assert row.hospital_id is None
            assert phone not in json.dumps(row.context)

    async def test_a_patient_account_is_never_a_staff_user(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        users_before = await _count(db_session, User)

        await sign_in(browser, sms, new_phone())

        assert await _count(db_session, User) == users_before

    async def test_a_wrong_code_is_refused_and_the_right_one_still_works(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)
        code = sms.code_for(phone)
        wrong = f"{(int(code) + 1) % 10**6:06d}"

        refused = await browser.post(VERIFY, json={"challenge_id": challenge_id, "code": wrong})

        assert _error(refused) == (401, "OTP_INVALID", "The code is incorrect or has expired.")
        assert set_cookie_headers(refused) == []
        assert await _account(db_session, phone) is None
        [failed] = await audit_rows(db_session, "patient.auth.otp_failed")
        assert failed.actor_type == "patient"
        assert failed.context is not None
        assert failed.context["reason"] == "wrong_code"
        assert failed.context["attempts"] == 1
        assert wrong not in json.dumps(failed.context)
        assert phone not in json.dumps(failed.context)

        accepted = await browser.post(VERIFY, json={"challenge_id": challenge_id, "code": code})
        assert accepted.status_code == 200, accepted.text

    async def test_a_challenge_dies_after_five_attempts(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)
        code = sms.code_for(phone)
        wrong = f"{(int(code) + 1) % 10**6:06d}"
        for _ in range(ATTEMPTS_PER_CHALLENGE):
            response = await browser.post(
                VERIFY, json={"challenge_id": challenge_id, "code": wrong}
            )
            assert response.status_code == 401

        with_the_right_code = await browser.post(
            VERIFY, json={"challenge_id": challenge_id, "code": code}
        )

        assert _error(with_the_right_code)[:2] == (401, "OTP_INVALID")
        assert await _account(db_session, phone) is None
        row = (
            await db_session.execute(
                select(PatientOtpChallenge)
                .where(PatientOtpChallenge.id == uuid.UUID(challenge_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.attempts == ATTEMPTS_PER_CHALLENGE

    async def test_a_code_can_be_used_once(self, browser: AsyncClient, sms: FakeSmsSender) -> None:
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)
        code = sms.code_for(phone)
        assert (
            await browser.post(VERIFY, json={"challenge_id": challenge_id, "code": code})
        ).status_code == 200

        again = await browser.post(VERIFY, json={"challenge_id": challenge_id, "code": code})

        assert _error(again)[:2] == (401, "OTP_INVALID")

    async def test_a_code_expires_after_five_minutes(
        self, browser: AsyncClient, sms: FakeSmsSender, clock: ThrottleClock
    ) -> None:
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)
        clock.advance(seconds=OTP_LIFETIME_SECONDS + 1)

        late = await browser.post(
            VERIFY, json={"challenge_id": challenge_id, "code": sms.code_for(phone)}
        )

        assert _error(late)[:2] == (401, "OTP_INVALID")

    async def test_a_code_does_not_verify_another_challenge(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        mine, theirs = new_phone(), new_phone()
        await _challenge(browser, mine)
        their_challenge = await _challenge(browser, theirs)

        response = await browser.post(
            VERIFY, json={"challenge_id": their_challenge, "code": sms.code_for(mine)}
        )

        # (One time in a million the two codes are equal; then this is a sign-in.)
        if sms.code_for(mine) != sms.code_for(theirs):
            assert _error(response)[:2] == (401, "OTP_INVALID")

    async def test_every_refusal_looks_the_same(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)
        code = sms.code_for(phone)
        wrong = f"{(int(code) + 1) % 10**6:06d}"

        wrong_code = await browser.post(VERIFY, json={"challenge_id": challenge_id, "code": wrong})
        unknown = await browser.post(VERIFY, json={"challenge_id": str(uuid.uuid4()), "code": code})

        assert _error(wrong_code) == _error(unknown)
        assert set(wrong_code.json()) == set(unknown.json())
        assert set_cookie_headers(wrong_code) == set_cookie_headers(unknown) == []

    @pytest.mark.parametrize("status", ["suspended", "closed"])
    async def test_an_account_that_is_not_active_gets_the_same_refusal(
        self,
        application: FastAPI,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        status: str,
    ) -> None:
        phone = new_phone()
        async with patient_client(application) as first:
            await sign_in(first, sms, phone)
        await db_session.execute(
            update(PatientAccount).where(PatientAccount.phone == phone).values(status=status)
        )
        await db_session.commit()
        async with patient_client(application) as client:
            challenge_id = await _challenge(client, phone)
            response = await client.post(
                VERIFY, json={"challenge_id": challenge_id, "code": sms.code_for(phone)}
            )

        assert _error(response) == (401, "OTP_INVALID", "The code is incorrect or has expired.")
        assert set_cookie_headers(response) == []

    @pytest.mark.parametrize(
        "body",
        [
            {"challenge_id": "not-a-uuid", "code": "123456"},
            {"challenge_id": str(uuid.uuid4()), "code": "12345"},
            {"challenge_id": str(uuid.uuid4()), "code": "abcdef"},
            {"challenge_id": str(uuid.uuid4()), "code": "123456", "phone": "+919812345678"},
            {"code": "123456"},
        ],
    )
    async def test_a_malformed_body_is_refused(
        self, browser: AsyncClient, body: dict[str, Any]
    ) -> None:
        response = await browser.post(VERIFY, json=body)

        assert response.status_code == 422, response.text

    async def test_a_refusal_is_held_to_the_uniform_floor_and_a_sign_in_is_not(
        self, browser: AsyncClient, sms: FakeSmsSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        waits: list[float] = []

        async def _record(seconds: float) -> None:
            waits.append(seconds)

        monkeypatch.setattr(auth_module, "_sleep", _record)
        monkeypatch.setattr(settings, "AUTH_FAILURE_MIN_SECONDS", 0.5)
        phone = new_phone()
        challenge_id = await _challenge(browser, phone)

        await browser.post(VERIFY, json={"challenge_id": str(uuid.uuid4()), "code": "000000"})
        assert len(waits) == 1
        assert 0 < waits[0] <= 0.5 * 1.2

        await browser.post(VERIFY, json={"challenge_id": challenge_id, "code": sms.code_for(phone)})
        assert len(waits) == 1


# ── Recognised device ────────────────────────────────────────────────────────


class TestDevice:
    async def test_a_sign_in_recognises_the_browser_and_a_second_one_reuses_it(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        await sign_in(browser, sms, phone)
        token = browser.cookies.get(DEVICE_COOKIE)
        assert token is not None

        await sign_in(browser, sms, phone)

        devices = (await db_session.execute(select(PatientDevice))).scalars().all()
        assert [device.token_hash for device in devices] == [hash_token(token)]
        assert browser.cookies.get(DEVICE_COOKIE) == token

    async def test_only_the_hash_of_the_device_token_is_stored(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        await sign_in(browser, sms, new_phone())
        token = browser.cookies.get(DEVICE_COOKIE)

        [device] = (await db_session.execute(select(PatientDevice))).scalars().all()

        assert device.token_hash == hash_token(str(token))
        assert "token" not in PatientDevice.__table__.c

    async def test_a_planted_device_token_is_never_adopted(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: plant a device cookie in the victim's browser before they sign in."""
        planted = "P" * 43
        browser.cookies.set(DEVICE_COOKIE, planted, domain="test.local", path="/")

        await sign_in(browser, sms, new_phone())

        [device] = (await db_session.execute(select(PatientDevice))).scalars().all()
        assert device.token_hash != hash_token(planted)
        assert planted not in str(browser.cookies.get(DEVICE_COOKIE))

    async def test_a_recognised_browser_draws_on_its_own_allowance(
        self, application: FastAPI, sms: FakeSmsSender
    ) -> None:
        """The owner's browser still gets a code after a stranger used up the number's."""
        phone = new_phone()
        async with patient_client(application) as owner:
            await sign_in(owner, sms, phone)  # 1 of the pair budget, from this source
            async with patient_client(application) as stranger:
                for _ in range(PAIR_BURST - 1):
                    assert (await _request_code(stranger, phone)).status_code == 202
                assert (await _request_code(stranger, phone)).status_code == 429

            assert (await _request_code(owner, phone)).status_code == 202


# ── POST /auth/refresh ───────────────────────────────────────────────────────


class TestRefresh:
    async def test_it_rotates_the_session(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        session = await sign_in(browser, sms, phone)
        before = browser.cookies.get(REFRESH_COOKIE)

        response = await browser.post(REFRESH, headers=CSRF)

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == {"access_token", "expires_in"}
        assert data["expires_in"] == 900
        after = browser.cookies.get(REFRESH_COOKIE)
        assert after is not None
        assert after != before
        assert after not in response.text
        assert response.headers["cache-control"] == "no-store"
        me = await browser.get(ME, headers=bearer(data["access_token"]))
        assert me.json()["data"]["account"]["id"] == session["account"]["id"]

        old = (
            await db_session.execute(
                select(PatientRefreshToken)
                .where(PatientRefreshToken.token_hash == hash_token(str(before)))
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        new = (
            await db_session.execute(
                select(PatientRefreshToken).where(
                    PatientRefreshToken.token_hash == hash_token(after)
                )
            )
        ).scalar_one()
        assert old.is_revoked is True
        assert old.rotated_by_token_id == new.id
        assert new.is_revoked is False

    async def test_the_token_is_stored_only_as_a_hash(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        await sign_in(browser, sms, new_phone())
        token = str(browser.cookies.get(REFRESH_COOKIE))

        [row] = (await db_session.execute(select(PatientRefreshToken))).scalars().all()

        assert row.token_hash == hash_token(token)
        assert token not in (row.token_hash, row.device_info or "")

    async def test_a_rotated_token_presented_again_ends_every_session(
        self, application: FastAPI, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        async with patient_client(application) as browser, patient_client(application) as other:
            session = await sign_in(browser, sms, phone)
            await sign_in(other, sms, phone)
            stolen = str(browser.cookies.get(REFRESH_COOKIE))
            assert (await browser.post(REFRESH, headers=CSRF)).status_code == 200

            async with patient_client(application) as thief:
                replayed = await thief.post(
                    REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={stolen}"}
                )

            assert _error(replayed) == (401, "AUTHENTICATION_REQUIRED", "Authentication required.")
            # The rotated-to session and the other browser's session are both dead.
            assert (await browser.post(REFRESH, headers=CSRF)).status_code == 401
            assert (await other.post(REFRESH, headers=CSRF)).status_code == 401

        live = await _count(
            db_session, PatientRefreshToken, PatientRefreshToken.is_revoked.is_(False)
        )
        assert live == 0
        # One event for the replay, and one for each of the two browsers that
        # then presented a token the replay had revoked.
        events = await audit_rows(db_session, "patient.auth.refresh_reuse_detected")
        assert sorted(event.context["sessions_ended"] for event in events if event.context) == [
            0,
            0,
            2,
        ]
        for event in events:
            assert event.actor_type == "patient"
            assert str(event.patient_account_id) == session["account"]["id"]
            assert stolen not in json.dumps(event.context)

    async def test_a_refusal_clears_the_cookie(self, browser: AsyncClient) -> None:
        response = await browser.post(
            REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={'x' * 64}"}
        )

        assert response.status_code == 401
        [cleared] = set_cookie_headers(response)
        assert cleared.startswith(f'{REFRESH_COOKIE}=""') or cleared.startswith(
            f"{REFRESH_COOKIE}=;"
        )
        assert "max-age=0" in cleared.lower()
        assert "path=/api/v1/patient/auth" in cleared.lower()

    async def test_no_cookie_is_refused(self, browser: AsyncClient) -> None:
        response = await browser.post(REFRESH, headers=CSRF)

        assert _error(response)[:2] == (401, "AUTHENTICATION_REQUIRED")

    async def test_it_needs_the_custom_header(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        """Attack: a cross-site form post riding on the cookie."""
        await sign_in(browser, sms, new_phone())
        before = browser.cookies.get(REFRESH_COOKIE)

        for headers in ({}, {"X-Atheris-Patient": "0"}, {"X-Atheris-Patient": "true"}):
            response = await browser.post(REFRESH, headers=headers)
            assert _error(response)[:2] == (401, "AUTHENTICATION_REQUIRED")
            # The session was not touched: not rotated, not cleared.
            assert set_cookie_headers(response) == []
        assert browser.cookies.get(REFRESH_COOKIE) == before
        assert (await browser.post(REFRESH, headers=CSRF)).status_code == 200

    async def test_an_origin_that_is_not_the_patient_app_is_refused(
        self, browser: AsyncClient, sms: FakeSmsSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "PATIENT_APP_ORIGINS", ["https://patients.example.com"])
        await sign_in(browser, sms, new_phone())

        for origin in ("https://evil.example", "https://patients.example.com.evil.example", "null"):
            response = await browser.post(REFRESH, headers={**CSRF, "Origin": origin})
            assert response.status_code == 401, origin
            assert set_cookie_headers(response) == []

        allowed = await browser.post(
            REFRESH, headers={**CSRF, "Origin": "https://patients.example.com"}
        )
        assert allowed.status_code == 200

    async def test_two_different_refresh_cookies_are_both_ignored(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        """Attack: shadow the real cookie with one set from a sibling domain."""
        await sign_in(browser, sms, new_phone())
        real = str(browser.cookies.get(REFRESH_COOKIE))
        browser.cookies.clear()

        response = await browser.post(
            REFRESH,
            headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={'a' * 64}; {REFRESH_COOKIE}={real}"},
        )

        assert response.status_code == 401

    async def test_an_expired_session_is_refused(
        self, browser: AsyncClient, sms: FakeSmsSender, clock: ThrottleClock
    ) -> None:
        await sign_in(browser, sms, new_phone())
        clock.advance(days=7, seconds=1)

        assert (await browser.post(REFRESH, headers=CSRF)).status_code == 401

    async def test_a_suspended_account_cannot_refresh(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        await sign_in(browser, sms, phone)
        await db_session.execute(
            update(PatientAccount).where(PatientAccount.phone == phone).values(status="suspended")
        )
        await db_session.commit()

        assert (await browser.post(REFRESH, headers=CSRF)).status_code == 401

    async def test_a_refresh_does_not_make_a_browser_a_recognised_device(
        self, application: FastAPI, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        async with patient_client(application) as browser:
            await sign_in(browser, sms, new_phone())
            token = str(browser.cookies.get(REFRESH_COOKIE))
        async with patient_client(application) as elsewhere:
            response = await elsewhere.post(
                REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={token}"}
            )

        assert response.status_code == 200
        assert not any(DEVICE_COOKIE in header for header in set_cookie_headers(response))
        assert await _count(db_session, PatientDevice) == 1


# ── POST /auth/logout, /auth/logout-all ──────────────────────────────────────


class TestLogout:
    async def test_it_ends_the_session_and_clears_the_cookie(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        session = await sign_in(browser, sms, new_phone())
        token = str(browser.cookies.get(REFRESH_COOKIE))

        response = await browser.post(LOGOUT, headers=CSRF)

        assert response.status_code == 204
        assert response.content == b""
        assert any("max-age=0" in header.lower() for header in set_cookie_headers(response))
        replay = await browser.post(
            REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={token}"}
        )
        assert replay.status_code == 401
        [row] = await audit_rows(db_session, "patient.auth.logout")
        assert str(row.patient_account_id) == session["account"]["id"]
        assert row.actor_type == "patient"

    async def test_it_needs_the_custom_header(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        """Attack: sign somebody out with a cross-site request."""
        await sign_in(browser, sms, new_phone())

        assert (await browser.post(LOGOUT)).status_code == 401
        assert (await browser.post(REFRESH, headers=CSRF)).status_code == 200

    async def test_without_a_session_it_still_answers_204(
        self, browser: AsyncClient, db_session: AsyncSession
    ) -> None:
        response = await browser.post(LOGOUT, headers=CSRF)

        assert response.status_code == 204
        assert await audit_rows(db_session, "patient.auth.logout") == []

    async def test_logout_all_ends_every_session_and_forgets_every_device(
        self, application: FastAPI, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        async with patient_client(application) as one, patient_client(application) as two:
            session = await sign_in(one, sms, phone)
            await sign_in(two, sms, phone)

            response = await one.post(LOGOUT_ALL, headers=bearer(session["access_token"]))

            assert response.status_code == 204
            assert (await one.post(REFRESH, headers=CSRF)).status_code == 401
            assert (await two.post(REFRESH, headers=CSRF)).status_code == 401

        assert await _count(db_session, PatientDevice) == 0
        [row] = await audit_rows(db_session, "patient.auth.logout_all")
        assert str(row.patient_account_id) == session["account"]["id"]
        assert row.context == {"sessions_ended": 2}

    async def test_logout_all_needs_a_patient_token(self, browser: AsyncClient) -> None:
        assert (await browser.post(LOGOUT_ALL)).status_code == 401
        assert (await browser.post(LOGOUT_ALL, headers=bearer("not.a.jwt"))).status_code == 401


# ── GET /me, and the boundary between the two kinds of principal ─────────────


class TestMe:
    async def test_it_describes_the_account_with_the_phone_masked(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        phone = new_phone()
        session = await sign_in(browser, sms, phone)

        response = await browser.get(ME, headers=bearer(session["access_token"]))

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == {"account", "links", "pending_policies"}
        assert data["account"] == session["account"]
        assert data["links"] == []
        assert data["pending_policies"] == []
        assert phone not in response.text

    async def test_it_needs_a_token(self, browser: AsyncClient) -> None:
        response = await browser.get(ME)

        assert _error(response) == (401, "AUTHENTICATION_REQUIRED", "Authentication required.")
        assert response.headers["www-authenticate"] == "Bearer"

    async def test_the_cookie_alone_is_not_enough(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        await sign_in(browser, sms, new_phone())

        assert (await browser.get(ME)).status_code == 401

    async def test_a_staff_token_is_refused_by_a_patient_endpoint(
        self,
        browser: AsyncClient,
        db_session: AsyncSession,
        actor_id: uuid.UUID,
        hospital_id: uuid.UUID,
    ) -> None:
        """Attack: a staff member — or a Super Admin — calls the patient API with their token."""
        for token in (
            create_access_token(actor_id, hospital_id),
            create_access_token(actor_id, None),
        ):
            response = await browser.get(ME, headers=bearer(token))
            assert _error(response)[:2] == (401, "AUTHENTICATION_REQUIRED")

    async def test_a_patient_token_is_refused_by_staff_endpoints(
        self, browser: AsyncClient, sms: FakeSmsSender
    ) -> None:
        """Attack: a patient calls the staff API with their token."""
        session = await sign_in(browser, sms, new_phone())
        headers = bearer(session["access_token"])

        for method, path in (
            ("GET", "/api/v1/patients"),
            ("GET", "/api/v1/users"),
            ("POST", "/api/v1/auth/logout-all"),
            ("GET", "/api/v1/hospitals/current"),
            ("GET", "/api/v1/audit-logs"),
        ):
            response = await browser.request(method, path, headers=headers)
            assert response.status_code == 401, (path, response.text)

    async def test_a_token_for_an_account_that_does_not_exist_is_refused(
        self, browser: AsyncClient
    ) -> None:
        response = await browser.get(ME, headers=bearer(create_patient_access_token(uuid.uuid4())))

        assert response.status_code == 401

    @pytest.mark.parametrize("status", ["suspended", "closed"])
    async def test_suspending_an_account_takes_effect_at_once(
        self, browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession, status: str
    ) -> None:
        phone = new_phone()
        session = await sign_in(browser, sms, phone)
        assert (await browser.get(ME, headers=bearer(session["access_token"]))).status_code == 200

        await db_session.execute(
            update(PatientAccount).where(PatientAccount.phone == phone).values(status=status)
        )
        await db_session.commit()

        assert (await browser.get(ME, headers=bearer(session["access_token"]))).status_code == 401
