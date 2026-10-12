"""API tests for the authentication endpoints.

These exist because the auth module shipped with no HTTP-level coverage, and a
whole class of bug slipped through as a result: every service wrote to the
session but nothing ever committed, so **the API reported success for writes
that were silently rolled back**. Unit tests with mocked repositories cannot
detect that — only a test that writes through the real stack and then reads the
row back can.

``docs/06-API_STANDARDS.md`` §24 and ``docs/11-TESTING_STRATEGY.md`` §2.3.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from starlette.requests import Request

from app.api.dependencies.db import get_db_session
from app.api.v1 import auth as auth_routes
from app.core.security import burn_password_verification, hash_password, verify_password
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.auth_throttle import AuthThrottleBucket
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserStatus
from app.repositories.auth_throttle_repository import AuthThrottleRepository
from app.services import auth_service as auth_service_module

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from datetime import datetime

    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

PASSWORD = "Str0ng!Passw0rd123"
WRONG_PASSWORD = "WrongPassw0rd!"

#: The password throttle for one account from one source
#: (``app/services/auth_throttle.py``, ``pw_pair``): this many attempts are
#: evaluated at once; the last of them starts the first wait.
FREE_ATTEMPTS = 5
FIRST_WAIT = timedelta(seconds=60)

#: Somewhere else on the internet: a second source for the same account.
ELSEWHERE = ("93.184.216.34", 40000)


@pytest_asyncio.fixture
async def application(db_session: AsyncSession) -> AsyncGenerator[FastAPI]:
    """The application, with every request sharing the test's rolled-back session."""
    app = create_app()

    async def _override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def api(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    """HTTP client sharing the test's rolled-back session."""
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def account(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, Any]:
    """A real, active user that can log in."""
    email = f"login-{uuid.uuid4().hex[:12]}@hospital.example"
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=email,
        password_hash=hash_password(PASSWORD),
        first_name="Login",
        last_name="Tester",
    )
    db_session.add(user)
    await db_session.flush()
    return {"id": user.id, "email": email}


class TestLogin:
    """``POST /api/v1/auth/login``."""

    async def test_login_with_valid_credentials_returns_tokens(
        self, api: AsyncClient, account: dict[str, Any]
    ) -> None:
        response = await api.post(
            "/api/v1/auth/login", json={"email": account["email"], "password": PASSWORD}
        )

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["access_token"]
        assert data["refresh_token"]

    async def test_login_persists_the_refresh_token(
        self, api: AsyncClient, db_session: AsyncSession, account: dict[str, Any]
    ) -> None:
        # Regression: the row was created in the session but never committed, so
        # the token handed to the client did not exist server-side and every
        # subsequent refresh failed with "Invalid refresh token".
        await api.post("/api/v1/auth/login", json={"email": account["email"], "password": PASSWORD})

        stored = await db_session.execute(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id == account["id"])
        )
        assert stored.scalar_one() == 1

    async def test_login_with_a_wrong_password_returns_401(
        self, api: AsyncClient, account: dict[str, Any]
    ) -> None:
        response = await api.post(
            "/api/v1/auth/login", json={"email": account["email"], "password": "WrongPassw0rd!"}
        )

        assert response.status_code == 401

    async def test_login_with_an_unknown_email_returns_401(self, api: AsyncClient) -> None:
        # Must be indistinguishable from a wrong password
        # (docs/modules/01-authentication.md §4, rule 11).
        response = await api.post(
            "/api/v1/auth/login",
            json={"email": "nobody@hospital.example", "password": PASSWORD},
        )

        assert response.status_code == 401

    async def test_login_response_uses_the_standard_error_envelope(
        self, api: AsyncClient, account: dict[str, Any]
    ) -> None:
        response = await api.post(
            "/api/v1/auth/login", json={"email": account["email"], "password": "WrongPassw0rd!"}
        )

        body = response.json()
        assert body["success"] is False
        assert body["error_code"] == "AUTHENTICATION_REQUIRED"
        assert "detail" not in body

    async def test_a_validation_failure_uses_the_standard_error_envelope(
        self, api: AsyncClient
    ) -> None:
        # Regression: FastAPI's raw {"detail": [...]} leaked `loc` tuples and
        # did not match docs/06-API_STANDARDS.md §5.3.
        response = await api.post("/api/v1/auth/login", json={"email": "not-an-email"})

        assert response.status_code == 422
        body = response.json()
        assert body["success"] is False
        assert body["error_code"] == "VALIDATION_ERROR"
        assert isinstance(body["errors"], list)
        assert {"field", "message"} <= set(body["errors"][0])


class _ThrottleClock:
    """Moves the throttle's clock (the database's) forward, without sleeping."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.offset = timedelta(0)
        real_now = AuthThrottleRepository.now

        async def _now(repository: AuthThrottleRepository) -> datetime:
            return await real_now(repository) + self.offset

        monkeypatch.setattr(AuthThrottleRepository, "now", _now)

    def advance(self, by: timedelta) -> None:
        self.offset += by


class _PasswordWork:
    """Counts every password evaluation the service performs, real or dummy."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.verifications = 0
        self.burns = 0

        def _verify(password: str, hashed: str) -> bool:
            self.verifications += 1
            return verify_password(password, hashed)

        def _burn(password: str) -> None:
            self.burns += 1
            burn_password_verification(password)

        monkeypatch.setattr(auth_service_module, "verify_password", _verify)
        monkeypatch.setattr(auth_service_module, "burn_password_verification", _burn)

    @property
    def evaluations(self) -> int:
        return self.verifications + self.burns


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _ThrottleClock:
    return _ThrottleClock(monkeypatch)


@pytest.fixture
def work(monkeypatch: pytest.MonkeyPatch) -> _PasswordWork:
    return _PasswordWork(monkeypatch)


async def _attempt(client: AsyncClient, email: str, password: str) -> Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _visible(response: Response) -> tuple[int, dict[str, Any], frozenset[str]]:
    """Everything a caller can tell one refusal from another by.

    The status, the body without its per-request correlation id, and which
    headers are present (their values — a request id, a rate-limit counter, a
    length — differ between any two requests).
    """
    body = dict(response.json())
    metadata = dict(body.pop("metadata", None) or {})
    metadata.pop("request_id", None)
    assert metadata == {}
    return response.status_code, body, frozenset(name.lower() for name in response.headers)


class TestLoginThrottle:
    """Brute-force protection through real HTTP: slowed down, never locked out.

    The account lockout this replaces let anyone who knew a staff email keep
    its owner out for as long as they liked. These tests replay both attacks
    — guessing the password, and using the guessing to deny the owner — and
    assert that neither works.
    """

    async def test_wrong_passwords_past_the_free_allowance_are_refused_without_evaluation(
        self, api: AsyncClient, account: dict[str, Any], work: _PasswordWork
    ) -> None:
        """Attack: guess one account's password as fast as the server answers."""
        for _ in range(FREE_ATTEMPTS):
            response = await _attempt(api, account["email"], WRONG_PASSWORD)
            assert response.status_code == 401
        assert work.verifications == FREE_ATTEMPTS

        for guess in range(20):
            response = await _attempt(api, account["email"], f"Gue55!Passw0rd-{guess}")
            assert response.status_code == 401

        # Twenty more guesses, and not one of them reached the password hash.
        assert work.verifications == FREE_ATTEMPTS
        assert work.burns == 0

    async def test_the_correct_password_is_among_the_refused_while_the_wait_lasts(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        account: dict[str, Any],
        work: _PasswordWork,
    ) -> None:
        """Even the right guess learns nothing: it is not looked at, and no session is issued."""
        for _ in range(FREE_ATTEMPTS):
            await _attempt(api, account["email"], WRONG_PASSWORD)

        response = await _attempt(api, account["email"], PASSWORD)

        assert response.status_code == 401
        assert work.verifications == FREE_ATTEMPTS
        sessions = await db_session.execute(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id == account["id"])
        )
        assert sessions.scalar_one() == 0
        assert "set-cookie" not in response.headers

    async def test_a_throttled_refusal_is_the_same_401_as_a_wrong_password(
        self, api: AsyncClient, account: dict[str, Any], work: _PasswordWork
    ) -> None:
        """Attack: tell "wrong password" from "not evaluated" and so time the guessing.

        Also anti-enumeration (docs/07-SECURITY.md rule 10): no ``ACCOUNT_LOCKED``,
        no unlock time, no ``Retry-After`` — nothing that says the address is real.
        """
        evaluated = await _attempt(api, account["email"], WRONG_PASSWORD)
        for _ in range(FREE_ATTEMPTS - 1):
            await _attempt(api, account["email"], WRONG_PASSWORD)
        throttled_wrong = await _attempt(api, account["email"], WRONG_PASSWORD)
        throttled_right = await _attempt(api, account["email"], PASSWORD)
        assert work.verifications == FREE_ATTEMPTS  # the last two were not evaluated

        assert _visible(throttled_wrong) == _visible(evaluated)
        assert _visible(throttled_right) == _visible(evaluated)

        status, body, headers = _visible(throttled_right)
        assert status == 401
        assert body == {
            "success": False,
            "message": "Invalid credentials.",
            "errors": None,
            "error_code": "AUTHENTICATION_REQUIRED",
        }
        assert "retry-after" not in headers
        assert "set-cookie" not in headers
        text = throttled_right.text.lower()
        for leak in ("lock", "unlock_at", "throttl", "too many", "wait", "retry"):
            assert leak not in text

    async def test_an_unknown_address_is_throttled_exactly_like_a_real_one(
        self, api: AsyncClient, account: dict[str, Any], work: _PasswordWork
    ) -> None:
        """Attack: enumerate staff emails by seeing which addresses get throttled.

        The buckets are keyed on the address as typed, never on whether an
        account exists: the same number of attempts is evaluated (here as a
        dummy verification), and the refusals look the same.
        """
        nobody = f"nobody-{uuid.uuid4().hex[:12]}@hospital.example"

        unknown = [await _attempt(api, nobody, WRONG_PASSWORD) for _ in range(FREE_ATTEMPTS + 3)]
        assert work.burns == FREE_ATTEMPTS
        assert work.verifications == 0

        real = [
            await _attempt(api, account["email"], WRONG_PASSWORD) for _ in range(FREE_ATTEMPTS + 3)
        ]
        assert work.verifications == FREE_ATTEMPTS
        assert work.burns == FREE_ATTEMPTS

        assert [_visible(r) for r in unknown] == [_visible(r) for r in real]
        assert all(_visible(r) == _visible(real[0]) for r in real)

    async def test_the_correct_password_works_once_the_wait_has_passed(
        self,
        api: AsyncClient,
        account: dict[str, Any],
        clock: _ThrottleClock,
        work: _PasswordWork,
    ) -> None:
        """Nothing is locked: when the wait is over the next attempt is judged on its merits."""
        for _ in range(FREE_ATTEMPTS):
            await _attempt(api, account["email"], WRONG_PASSWORD)

        clock.advance(FIRST_WAIT - timedelta(seconds=2))
        too_early = await _attempt(api, account["email"], PASSWORD)
        assert too_early.status_code == 401
        assert work.verifications == FREE_ATTEMPTS

        clock.advance(timedelta(seconds=3))
        on_time = await _attempt(api, account["email"], PASSWORD)

        assert on_time.status_code == 200, on_time.text
        assert on_time.json()["data"]["access_token"]
        assert work.verifications == FREE_ATTEMPTS + 1

    async def test_hammering_during_the_wait_does_not_extend_it(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        account: dict[str, Any],
        clock: _ThrottleClock,
    ) -> None:
        """Attack: keep the owner out by never letting the wait run down.

        This is the denial of service the lockout allowed. A refused attempt
        must change nothing — not the wait, not any counter.
        """
        for _ in range(FREE_ATTEMPTS):
            await _attempt(api, account["email"], WRONG_PASSWORD)
        before = await self._buckets(db_session)

        for second in range(1, 31):
            clock.advance(timedelta(seconds=1))
            refused = await _attempt(api, account["email"], WRONG_PASSWORD)
            assert refused.status_code == 401, second

        assert await self._buckets(db_session) == before

        # Thirty of the sixty seconds have gone by under constant hammering;
        # the remaining thirty are all that is left to wait.
        clock.advance(timedelta(seconds=31))
        response = await _attempt(api, account["email"], PASSWORD)
        assert response.status_code == 200, response.text

    async def test_each_further_wrong_password_waits_twice_as_long(
        self,
        api: AsyncClient,
        account: dict[str, Any],
        clock: _ThrottleClock,
        work: _PasswordWork,
    ) -> None:
        """The patient attacker: one guess per wait buys a longer wait, not a steady rate."""
        for _ in range(FREE_ATTEMPTS):
            await _attempt(api, account["email"], WRONG_PASSWORD)
        evaluated = FREE_ATTEMPTS

        for wait_seconds in (60, 120, 240):
            clock.advance(timedelta(seconds=wait_seconds - 2))
            await _attempt(api, account["email"], WRONG_PASSWORD)
            assert work.verifications == evaluated, f"evaluated before {wait_seconds}s were up"

            clock.advance(timedelta(seconds=3))
            await _attempt(api, account["email"], WRONG_PASSWORD)
            evaluated += 1
            assert work.verifications == evaluated, f"not evaluated after {wait_seconds}s"

    async def test_no_number_of_wrong_passwords_locks_the_account(
        self,
        application: FastAPI,
        api: AsyncClient,
        db_session: AsyncSession,
        account: dict[str, Any],
    ) -> None:
        """Attack: fail a colleague's password until they cannot sign in.

        The attacker's own source runs out; the account itself is untouched —
        no counter on the user row, no ``locked_until``, no status change —
        and its owner signs in from somewhere else at once, with no waiting.
        """
        for _ in range(FREE_ATTEMPTS + 7):
            refused = await _attempt(api, account["email"], WRONG_PASSWORD)
            assert refused.status_code == 401

        row = (
            await db_session.execute(
                select(User.failed_login_attempts, User.locked_until, User.status).where(
                    User.id == account["id"]
                )
            )
        ).one()
        assert tuple(row) == (0, None, UserStatus.ACTIVE)

        async with AsyncClient(
            transport=ASGITransport(app=application, client=ELSEWHERE), base_url="http://test"
        ) as owner:
            response = await _attempt(owner, account["email"], PASSWORD)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["refresh_token"]

    async def test_a_legacy_lock_on_the_user_row_is_ignored(
        self, api: AsyncClient, db_session: AsyncSession, account: dict[str, Any]
    ) -> None:
        """A row locked by the old code (or by hand) is not a way to keep someone out."""
        await db_session.execute(
            update(User)
            .where(User.id == account["id"])
            .values(failed_login_attempts=99, locked_until=func.now() + timedelta(days=365))
        )
        await db_session.flush()

        response = await _attempt(api, account["email"], PASSWORD)

        assert response.status_code == 200, response.text

    async def test_the_owners_typos_do_not_count_once_the_password_is_right(
        self, api: AsyncClient, account: dict[str, Any], work: _PasswordWork
    ) -> None:
        """A correct password is not charged: four typos and a success leave no wait behind."""
        for _ in range(FREE_ATTEMPTS - 1):
            await _attempt(api, account["email"], WRONG_PASSWORD)

        response = await _attempt(api, account["email"], PASSWORD)

        assert response.status_code == 200, response.text
        assert work.verifications == FREE_ATTEMPTS

    async def test_the_wait_is_recorded_in_the_audit_trail_not_in_the_response(
        self, api: AsyncClient, db_session: AsyncSession, account: dict[str, Any]
    ) -> None:
        """What the caller is not told, an investigator can still see."""
        for _ in range(FREE_ATTEMPTS + 2):
            await _attempt(api, account["email"], WRONG_PASSWORD)

        result = await db_session.execute(
            select(AuditLog)
            .where(AuditLog.target_id == account["id"], AuditLog.action == "auth.login.failed")
            .order_by(AuditLog.created_at, AuditLog.id)
        )
        entries = list(result.scalars().all())

        # One entry per attempt that was evaluated; refused attempts wrote nothing.
        assert len(entries) == FREE_ATTEMPTS
        contexts = [entry.context or {} for entry in entries]
        assert all(context["reason"] == "invalid_password" for context in contexts)
        engaged = [context["throttle"] for context in contexts if "throttle" in context]
        assert engaged == [{"bucket": "pw_pair", "wait_seconds": 60}]

    @staticmethod
    async def _buckets(session: AsyncSession) -> list[tuple[Any, ...]]:
        """Every throttle counter, exactly as stored."""
        result = await session.execute(
            select(
                AuthThrottleBucket.key_hash,
                AuthThrottleBucket.kind,
                AuthThrottleBucket.failures,
                AuthThrottleBucket.blocked_until,
                AuthThrottleBucket.last_charged_at,
                AuthThrottleBucket.drains_at,
                AuthThrottleBucket.expires_at,
            ).order_by(AuthThrottleBucket.key_hash)
        )
        return [tuple(row) for row in result.all()]


def _cookie_request(*cookie_headers: str) -> Request:
    """A request carrying exactly these raw ``Cookie`` headers."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [(b"cookie", header.encode("latin-1")) for header in cookie_headers],
            "client": ("93.184.216.34", 40000),
        }
    )


def _planted(count: int, *, start: int = 0) -> list[str]:
    """Well-formed device tokens the server never issued."""
    return [f"{index:04d}".ljust(43, "p") for index in range(start, start + count)]


class TestDeviceCookiePadding:
    """Attack: bury the real device token under made-up ones so it is never read.

    The owner's browser draws on its own buckets *because* its token is
    recognised. If a reader that stops after a handful of tokens could be made
    to stop before the real one — by planting cookies from a sibling page, or
    by a second ``Cookie`` header — the owner would be pushed back onto the
    shared account budget an attacker can drain.
    """

    NAME = "aetheris-device"  # the suite runs with AUTH_DEVICE_COOKIE_SECURE off
    REAL = "r" * 43

    def test_sixteen_planted_tokens_cannot_hide_the_real_one(self) -> None:
        """More than a whole cookie's worth (eight) of padding, twice over."""
        value = ".".join([*_planted(16), self.REAL])

        tokens = auth_routes._get_device_tokens(_cookie_request(f"{self.NAME}={value}"))  # noqa: SLF001

        assert self.REAL in tokens
        assert len(tokens) == 17

    @pytest.mark.parametrize("padding", [8, 9, 16, 32, 63])
    def test_the_real_token_is_read_behind_any_padding_a_browser_could_hold(
        self, padding: int
    ) -> None:
        value = ".".join([*_planted(padding), self.REAL])

        tokens = auth_routes._get_device_tokens(_cookie_request(f"{self.NAME}={value}"))  # noqa: SLF001

        assert tokens[-1] == self.REAL

    def test_padding_spread_over_duplicate_cookies_and_headers_does_not_hide_it_either(
        self,
    ) -> None:
        """Every cookie of the name is read from the raw headers, not only the one a parser keeps."""
        first = f"{self.NAME}={'.'.join(_planted(8))}; other=1"
        second = f"session=x; {self.NAME}={'.'.join(_planted(8, start=8))}; {self.NAME}={self.REAL}"

        tokens = auth_routes._get_device_tokens(_cookie_request(first, second))  # noqa: SLF001

        assert self.REAL in tokens
        assert len(tokens) == 17

    def test_repeating_one_token_does_not_use_up_the_room(self) -> None:
        value = ".".join([*(["q" * 43] * 500), self.REAL])

        tokens = auth_routes._get_device_tokens(_cookie_request(f"{self.NAME}={value}"))  # noqa: SLF001

        assert tokens == ("q" * 43, self.REAL)

    def test_malformed_values_take_no_room_and_are_never_returned(self) -> None:
        junk = ["short", "x" * 65, "has space" * 5, "per%cent" * 5, "", "é" * 40, "a" * 31]
        value = ".".join([*junk, self.REAL])

        tokens = auth_routes._get_device_tokens(_cookie_request(f'{self.NAME}="{value}"'))  # noqa: SLF001

        assert tokens == (self.REAL,)

    def test_one_request_still_cannot_ask_for_unbounded_work(self) -> None:
        """The bound is far above what a browser holds — and it is still a bound."""
        assert auth_routes._DEVICE_TOKENS_READ == 64  # noqa: SLF001
        assert (
            auth_routes._DEVICE_TOKENS_READ  # noqa: SLF001
            >= 8 * auth_service_module.DEVICE_TOKENS_PER_COOKIE
        )
        value = ".".join(_planted(5000))

        tokens = auth_routes._get_device_tokens(_cookie_request(f"{self.NAME}={value}"))  # noqa: SLF001

        assert len(tokens) == 64

    def test_a_cookie_of_another_name_is_not_a_device_token(self) -> None:
        request = _cookie_request(
            f"x{self.NAME}={self.REAL}; {self.NAME}x={self.REAL}; __Host-{self.NAME}={self.REAL}"
        )

        assert auth_routes._get_device_tokens(request) == ()  # noqa: SLF001

    async def test_the_owners_browser_keeps_its_own_buckets_behind_sixteen_planted_tokens(
        self,
        application: FastAPI,
        api: AsyncClient,
        db_session: AsyncSession,
        account: dict[str, Any],
    ) -> None:
        """End to end: a typo from the owner's padded browser is charged to the device, not the account."""
        signed_in = await _attempt(api, account["email"], PASSWORD)
        assert signed_in.status_code == 200, signed_in.text
        real = signed_in.cookies[self.NAME]
        assert "." not in real
        before = await self._failures(db_session)
        assert before.get("pw_device", 0) == 0

        padded = ".".join([*_planted(16), real])
        async with AsyncClient(
            transport=ASGITransport(app=application, client=ELSEWHERE), base_url="http://test"
        ) as browser:
            typo = await browser.post(
                "/api/v1/auth/login",
                json={"email": account["email"], "password": WRONG_PASSWORD},
                headers={"cookie": f"{self.NAME}={padded}"},
            )
        assert typo.status_code == 401

        after = await self._failures(db_session)
        assert after.get("pw_device") == 1
        # Nothing was charged to the buckets an outsider shares.
        assert after.get("pw_pair", 0) == before.get("pw_pair", 0) == 0

    @staticmethod
    async def _failures(session: AsyncSession) -> dict[str, int]:
        """Counted failures per backoff kind, summed over every bucket."""
        result = await session.execute(
            select(AuthThrottleBucket.kind, func.sum(AuthThrottleBucket.failures)).group_by(
                AuthThrottleBucket.kind
            )
        )
        return {str(kind): int(total or 0) for kind, total in result.all()}


class TestAccountState:
    """Account states that must read as invalid credentials."""

    async def test_a_suspended_account_login_is_a_generic_401(
        self, api: AsyncClient, db_session: AsyncSession, account: dict[str, Any]
    ) -> None:
        # Anti-enumeration: ACCOUNT_SUSPENDED would confirm the email belongs to
        # a real (if disabled) account. It must read as invalid credentials.
        await db_session.execute(
            update(User).where(User.id == account["id"]).values(status=UserStatus.SUSPENDED)
        )
        await db_session.flush()

        response = await api.post(
            "/api/v1/auth/login", json={"email": account["email"], "password": PASSWORD}
        )

        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["error_code"] == "AUTHENTICATION_REQUIRED"
        assert body["message"] == "Invalid credentials."
        assert body["errors"] is None


class TestRefreshRotation:
    """``POST /api/v1/auth/refresh`` — rotation and reuse detection."""

    async def _login(self, api: AsyncClient, email: str) -> dict[str, Any]:
        response = await api.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert response.status_code == 200, response.text
        return dict(response.json()["data"])

    async def test_a_fresh_refresh_token_is_accepted(
        self, api: AsyncClient, account: dict[str, Any]
    ) -> None:
        # Regression: this returned 401 on first use because the token was
        # never persisted, so sessions could not be renewed at all.
        tokens = await self._login(api, account["email"])

        response = await api.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["access_token"]

    async def test_refresh_rotates_the_token(
        self, api: AsyncClient, account: dict[str, Any]
    ) -> None:
        tokens = await self._login(api, account["email"])

        rotated = await api.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )

        assert rotated.json()["data"]["refresh_token"] != tokens["refresh_token"]

    async def test_reusing_a_rotated_token_is_rejected(
        self, api: AsyncClient, account: dict[str, Any]
    ) -> None:
        # Reuse detection (docs/modules/01-authentication.md §4, rule 7).
        tokens = await self._login(api, account["email"])
        await api.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

        replay = await api.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )

        assert replay.status_code == 401

    async def test_an_unknown_refresh_token_is_rejected(self, api: AsyncClient) -> None:
        response = await api.post(
            "/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"}
        )

        assert response.status_code == 401
