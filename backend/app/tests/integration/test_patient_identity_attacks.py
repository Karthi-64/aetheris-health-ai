"""The patient principal and its sessions, attacked (Patient App, Task 28).

Every test here is an attack on the boundary between the two kinds of
principal — a staff user and a patient account
(``docs/modules/15-patient-app.md`` §5.5–5.6, §6) — or on a patient session,
replayed through real HTTP against the application and a real PostgreSQL. Each
one states the attack and asserts what the attacker must not get.

**What "every staff route" means.** The route table is read from the
application's own OpenAPI document, so a route added tomorrow is attacked
tomorrow without anyone remembering to list it. The same is done, the other
way round, for every patient route.

**How tokens are forged.** Tokens that must carry a *valid* signature are
built with the application's own helpers in ``app.core.security``
(``create_access_token`` takes ``extra_claims``, which is enough to give a
token any type, audience, expiry or claim). Tokens that must not are signed
here with a key the application does not hold.

**How time is moved.** Only through the throttle's clock
(``AuthThrottleRepository.now``), which also times patient sessions; nothing
sleeps.

**What the shared session can and cannot prove.** Most tests use the suite's
``db_session``: one connection inside one rolled-back transaction, on which
requests can only run one after another. ``TestParallelRefresh`` uses a pool
of real connections with real commits, fires its requests at once, and deletes
what it wrote.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt as pyjwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.responses import JSONResponse

from app.api.dependencies.db import get_db_session
from app.api.dependencies.patient import get_sms_sender
from app.core.config import PUBLISHED_SECRET_KEYS, settings
from app.core.security import (
    PATIENT_ACCESS_TTL_SECONDS,
    PATIENT_TOKEN_AUDIENCE,
    PATIENT_TOKEN_TYPE,
    create_access_token,
    create_mfa_ticket,
    create_patient_access_token,
    hash_password,
    hash_token,
    verify_patient_access_token,
)
from app.main import create_app
from app.middleware.rate_limit import RateLimitMiddleware
from app.models.audit_log import AuditLog
from app.models.auth_throttle import AuthThrottleBucket, TrustedDevice
from app.models.patient import Patient
from app.models.patient_account import (
    PatientAccount,
    PatientAccountLink,
    PatientDevice,
    PatientOtpChallenge,
    PatientRefreshToken,
)
from app.models.refresh_token import RefreshToken
from app.models.role import Role
from app.models.user import User, UserRole
from app.repositories.patient_refresh_token_repository import PatientRefreshTokenRepository
from app.services.auth_throttle import BucketKind, bucket
from app.tests.conftest import every_log_line_reaches_the_root
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
    sign_in,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncEngine
    from starlette.requests import Request


pytestmark = pytest.mark.database

AUTH = "/api/v1/auth"
REQUEST = f"{PATIENT}/auth/otp/request"
VERIFY = f"{PATIENT}/auth/otp/verify"
REFRESH = f"{PATIENT}/auth/refresh"
LOGOUT = f"{PATIENT}/auth/logout"
LOGOUT_ALL = f"{PATIENT}/auth/logout-all"
ME = f"{PATIENT}/me"
PROBE = "/api/v1/_identity-probe"
#: The same window, inside the Patient App namespace — the only place a
#: patient token is billed to its account.
PATIENT_PROBE = f"{PATIENT}/_identity-probe"

PASSWORD = "Str0ng!Passw0rd123"  # noqa: S105 — a test credential, not a real one
DOB = date(1990, 5, 17)

#: The one body every patient endpoint answers an unauthenticated caller with.
UNAUTHENTICATED = {
    "success": False,
    "message": "Authentication required.",
    "error_code": "AUTHENTICATION_REQUIRED",
    "errors": None,
}

# Documented numbers (spec §5.5), restated on purpose: a change to the policy
# has to be made here too.
ACCESS_TTL_SECONDS = 900
REFRESH_TTL = timedelta(days=7)

#: Staff route families that must be among those a patient token is refused by.
#: ``invoices`` is billing; ``audit-logs`` is audit.
STAFF_FAMILIES = (
    "auth",
    "users",
    "patients",
    "appointments",
    "invoices",
    "audit-logs",
    "prescriptions",
    "lab-orders",
    "roles",
    "hospitals",
)

#: The socket peer of every test request: a proxy we "operate".
_PROXY_PEER = ("127.0.0.1", 40000)
_PATH_PARAMETER = re.compile(r"\{[^}]+\}")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _body(response: Response) -> dict[str, Any]:
    """The response envelope without its per-request metadata."""
    payload: dict[str, Any] = response.json()
    payload.pop("metadata", None)
    return payload


def _source() -> str:
    """A public IPv4 address nobody else in this run is using."""
    return f"198.{secrets.randbelow(256)}.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}"


def _routes(application: FastAPI, *, patient: bool) -> list[tuple[str, str]]:
    """Every ``(METHOD, path)`` the application documents, inside or outside the patient API."""
    found: list[tuple[str, str]] = []
    for path, operations in application.openapi()["paths"].items():
        if path.startswith(f"{PATIENT}/") != patient:
            continue
        found.extend(
            (method.upper(), path)
            for method in operations
            if method in {"get", "post", "put", "patch", "delete"}
        )
    return sorted(found)


def _concrete(path: str) -> str:
    """A path with every parameter filled in by a random UUID."""
    return _PATH_PARAMETER.sub(lambda _: str(uuid.uuid4()), path)


def _family(path: str) -> str:
    return path.removeprefix("/api/v1/").split("/", 1)[0]


def _claims(token: str) -> dict[str, Any]:
    payload: dict[str, Any] = pyjwt.decode(token, options={"verify_signature": False})
    return payload


def _signed_by_us(subject: uuid.UUID, **claims: Any) -> str:
    """A token with a valid signature and whatever claims the attack calls for.

    Built by the application's own helper, so the signature is the real one.
    ``aud`` and ``type`` default to a patient token's.
    """
    extra = {"aud": PATIENT_TOKEN_AUDIENCE, "type": PATIENT_TOKEN_TYPE, **claims}
    return create_access_token(
        subject, None, extra_claims={k: v for k, v in extra.items() if v is not None}
    )


def _patient_payload(subject: uuid.UUID) -> dict[str, Any]:
    """Exactly the claims of a genuine patient access token."""
    now = datetime.now(UTC)
    return {
        "sub": str(subject),
        "iss": settings.JWT_ISSUER,
        "aud": PATIENT_TOKEN_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(seconds=ACCESS_TTL_SECONDS),
        "type": PATIENT_TOKEN_TYPE,
    }


def _segment(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _forged(kind: str, account_id: uuid.UUID) -> str:
    """A token for a real, active account that the patient API must still refuse."""
    past = datetime.now(UTC) - timedelta(seconds=settings.JWT_LEEWAY_SECONDS + 120)
    payload = _patient_payload(account_id)
    match kind:
        case "signed with another key":
            return pyjwt.encode(payload, secrets.token_urlsafe(48), algorithm="HS256")
        case "signed with a key published in the repository":
            return pyjwt.encode(payload, sorted(PUBLISHED_SECRET_KEYS)[0], algorithm="HS256")
        case "no signature":
            header, body, _ = create_patient_access_token(account_id).split(".")
            return f"{header}.{body}."
        case "algorithm none":
            unsigned: str = pyjwt.encode(payload, None, algorithm="none")  # type: ignore[arg-type]
            return unsigned
        case "algorithm none, with the genuine token's claims":
            genuine = create_patient_access_token(account_id)
            return f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{genuine.split('.')[1]}."
        case "genuine signature, another account's claims":
            # Somebody else's genuine token, with the subject swapped for this account.
            first, _, signature = create_patient_access_token(uuid.uuid4()).split(".")
            swapped = _claims(create_patient_access_token(account_id))
            return f"{first}.{_segment(swapped)}.{signature}"
        case "staff access type":
            return _signed_by_us(account_id, type="access")
        case "mfa ticket type":
            return _signed_by_us(account_id, type="mfa_ticket")
        case "refresh type":
            return _signed_by_us(account_id, type="patient_refresh")
        case "another audience":
            return _signed_by_us(account_id, aud="atheris-staff")
        case "an audience that only looks right":
            return _signed_by_us(account_id, aud="aetheris-patient")
        case "no audience":
            return _signed_by_us(account_id, aud=None)
        case "expired":
            return _signed_by_us(account_id, iat=past - timedelta(minutes=15), exp=past)
        case "another issuer":
            return _signed_by_us(account_id, iss="somebody-else")
        case "truncated":
            return create_patient_access_token(account_id)[:-4]
        case _:  # pragma: no cover — a typo in a parametrize list
            raise AssertionError(kind)


FORGERIES = [
    "signed with another key",
    "signed with a key published in the repository",
    "no signature",
    "algorithm none",
    "algorithm none, with the genuine token's claims",
    "genuine signature, another account's claims",
    "staff access type",
    "mfa ticket type",
    "refresh type",
    "another audience",
    "an audience that only looks right",
    "no audience",
    "expired",
    "another issuer",
    "truncated",
]


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    result = await session.execute(select(func.count()).select_from(model).where(*where))
    return int(result.scalar_one())


async def _account(session: AsyncSession, phone: str) -> PatientAccount:
    result = await session.execute(
        select(PatientAccount)
        .where(PatientAccount.phone == phone)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _live_sessions(session: AsyncSession, account_id: uuid.UUID) -> int:
    return await _count(
        session,
        PatientRefreshToken,
        PatientRefreshToken.account_id == account_id,
        PatientRefreshToken.is_revoked.is_(False),
    )


async def _whoami(request: Request) -> JSONResponse:
    """What the middleware made of the caller: read behind the real middleware stack."""
    state = request.state
    return JSONResponse(
        {
            "user_id": str(state.user_id) if state.user_id else None,
            "hospital_id": str(state.hospital_id) if state.hospital_id else None,
            "patient_account_id": (
                str(state.patient_account_id) if state.patient_account_id else None
            ),
            "billed_to": [key for key, _ in RateLimitMiddleware._applicable_limits(request)],
        }
    )


@dataclass(frozen=True)
class _Staff:
    """A staff user, by value.

    Not the ORM row: a refused patient refresh rolls the shared test session
    back, which expires every loaded object, and reading an expired attribute
    outside a greenlet fails.
    """

    id: uuid.UUID
    hospital_id: uuid.UUID
    email: str


@dataclass(frozen=True)
class _Record:
    """A patient record a hospital holds, by value."""

    id: uuid.UUID
    hospital_id: uuid.UUID
    mrn: str


async def _record(session: AsyncSession, hospital_id: uuid.UUID, phone: str) -> _Record:
    """A record as hospital staff would have registered it, for ``phone`` and :data:`DOB`."""
    row = await insert_patient_record(session, hospital_id, phone=phone, date_of_birth=DOB)
    return _Record(id=row.id, hospital_id=hospital_id, mrn=row.mrn)


class _Net:
    """Hands out browsers: HTTP clients with their own cookie jar and address."""

    def __init__(self, application: FastAPI) -> None:
        self.application = application
        self._clients: list[AsyncClient] = []

    def browser(self, source: str | None = None, *, base_url: str = "http://test") -> AsyncClient:
        client = AsyncClient(
            transport=ASGITransport(app=self.application, client=_PROXY_PEER),
            base_url=base_url,
            headers={"X-Forwarded-For": source or _source()},
        )
        self._clients.append(client)
        return client

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()


class _Patient:
    """A signed-in patient: a phone, a browser, an account and an access token."""

    def __init__(self, client: AsyncClient, phone: str, session: dict[str, Any]) -> None:
        self.client = client
        self.phone = phone
        self.account_id = uuid.UUID(session["account"]["id"])
        self.token: str = session["access_token"]
        self.headers = bearer(self.token)

    @property
    def refresh_token(self) -> str:
        value = self.client.cookies.get(REFRESH_COOKIE)
        assert value, "the browser holds no refresh cookie"
        return value


async def _signed_in(net: _Net, sms: FakeSmsSender, phone: str | None = None) -> _Patient:
    client = net.browser()
    number = phone or new_phone()
    return _Patient(client, number, await sign_in(client, sms, number))


def _bodies(hospital_ref: object) -> dict[tuple[str, str], dict[str, Any] | None]:
    """A well-formed request for every bearer-authenticated patient endpoint."""
    return {
        ("GET", ME): None,
        ("POST", LOGOUT_ALL): None,
        ("POST", f"{PATIENT}/hospitals/{hospital_ref}/link"): {
            "date_of_birth": DOB.isoformat(),
            "consent_policy_version": POLICY,
        },
        ("POST", f"{PATIENT}/hospitals/{hospital_ref}/register"): {
            "first_name": "Asha",
            "last_name": "Verma",
            "date_of_birth": DOB.isoformat(),
            "gender": "female",
            "consent_policy_version": POLICY,
        },
    }


class _LogTrap:
    """Every line the application logs while it is installed."""

    def __init__(self) -> None:
        import logging

        trap = self
        self.lines: list[str] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                trap.lines.append(self.format(record))
                trap.lines.append(repr(record.args))

        self.handler = _Handler(level=0)

    @property
    def everything(self) -> str:
        return "\n".join(self.lines)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _attack_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain-HTTP cookies, one trusted proxy, and the request limiter out of the way.

    A 429 from the per-minute middleware would make "the attacker's request
    failed" pass without the code under test having decided anything.
    """
    monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_HOSPITAL_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_AI_PER_MIN", 1_000_000)


@pytest.fixture
def sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ThrottleClock:
    return ThrottleClock(monkeypatch)


@pytest.fixture
def all_logs(net: _Net) -> Iterator[_LogTrap]:
    """Everything the application logs, at every level, structlog included.

    Installed once ``net`` has built the application, which reconfigures
    logging. The test using it asserts that expected events were captured.
    """
    import logging

    root = logging.getLogger()
    trap = _LogTrap()
    with every_log_line_reaches_the_root():
        root.addHandler(trap.handler)
        try:
            yield trap
        finally:
            root.removeHandler(trap.handler)


@pytest_asyncio.fixture
async def net(db_session: AsyncSession, sms: FakeSmsSender) -> AsyncGenerator[_Net]:
    """Browsers talking to the application on the test's rolled-back session."""
    application = create_app()

    async def _override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _override
    application.dependency_overrides[get_sms_sender] = lambda: sms
    # A window onto ``request.state`` behind the real middleware stack.
    application.add_route(PROBE, _whoami, methods=["GET"])
    application.add_route(PATIENT_PROBE, _whoami, methods=["GET"])
    network = _Net(application)
    try:
        yield network
    finally:
        await network.aclose()
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def hospital(db_session: AsyncSession, hospital_id: uuid.UUID) -> uuid.UUID:
    """Hospital A, open to the Patient App. Its id: see :class:`_Staff` for why not the row."""
    await open_hospital(db_session, hospital_id)
    return hospital_id


@pytest_asyncio.fixture
async def staff(db_session: AsyncSession, hospital_id: uuid.UUID) -> _Staff:
    """An active staff member of hospital A who can really sign in."""
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"staff-{uuid.uuid4().hex[:12]}@hospital.example",
        password_hash=hash_password(PASSWORD),
        first_name="Identity",
        last_name="Tester",
        password_changed_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()
    return _Staff(id=user.id, hospital_id=hospital_id, email=user.email)


async def _staff_session(net: _Net, user: _Staff) -> dict[str, Any]:
    """Sign a staff member in for real: an access token and a refresh token."""
    response = await net.browser().post(
        f"{AUTH}/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    assert data.get("refresh_token"), data
    return data


# ── 1. A patient is not a staff user ─────────────────────────────────────────


class TestAPatientIsNeverAStaffUser:
    async def test_no_patient_flow_creates_a_staff_row_of_any_kind(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        hospital: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Attack: find the patient flow that leaves behind something a staff check would accept."""
        await open_hospital(db_session, other_hospital_id)
        other = other_hospital_id
        staff_tables = (User, UserRole, Role, RefreshToken, TrustedDevice)
        before = [await _count(db_session, model) for model in staff_tables]

        # Every patient flow there is: sign-in (twice, on two browsers), the
        # account page, linking to an existing record, registering a new one,
        # a refresh, a refused code, a sign-out and a sign-out everywhere.
        phone = new_phone()
        await _record(db_session, hospital, phone)
        patient = await _signed_in(net, sms, phone)
        second = await _signed_in(net, sms, phone)
        requests = _bodies(hospital)
        linked = await patient.client.post(
            f"{PATIENT}/hospitals/{hospital}/link",
            json=requests["POST", f"{PATIENT}/hospitals/{hospital}/link"],
            headers=patient.headers,
        )
        registered = await patient.client.post(
            f"{PATIENT}/hospitals/{other}/register",
            json=_bodies(other)["POST", f"{PATIENT}/hospitals/{other}/register"],
            headers=patient.headers,
        )
        me = await patient.client.get(ME, headers=patient.headers)
        refreshed = await patient.client.post(REFRESH, headers=CSRF)
        challenge = await patient.client.post(REQUEST, json={"phone": phone})
        refused = await patient.client.post(
            VERIFY,
            json={
                "challenge_id": challenge.json()["data"]["challenge_id"],
                "code": f"{(int(sms.code_for(phone)) + 1) % 10**6:06d}",
            },
        )
        signed_out = await second.client.post(LOGOUT, headers=CSRF)
        everywhere = await patient.client.post(LOGOUT_ALL, headers=patient.headers)

        assert [
            r.status_code for r in (linked, registered, me, refreshed, refused, signed_out)
        ] == [201, 201, 200, 200, 401, 204]
        assert everywhere.status_code == 204
        assert len(me.json()["data"]["links"]) == 2

        assert [await _count(db_session, model) for model in staff_tables] == before
        # Nor does the account's id name a user, nor its phone a staff login.
        assert await _count(db_session, User, User.id == patient.account_id) == 0
        assert (
            await _count(db_session, RefreshToken, RefreshToken.user_id == patient.account_id) == 0
        )

    async def test_every_patient_action_is_audited_as_a_patient_never_as_a_user(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, hospital: uuid.UUID
    ) -> None:
        """Attack: act as a patient and look for an audit row that names a staff actor."""
        phone = new_phone()
        await _record(db_session, hospital, phone)
        patient = await _signed_in(net, sms, phone)
        url = f"{PATIENT}/hospitals/{hospital}/link"
        await patient.client.post(url, json=_bodies(hospital)["POST", url], headers=patient.headers)
        await patient.client.post(REFRESH, headers=CSRF)
        await patient.client.post(LOGOUT_ALL, headers=patient.headers)

        rows = (
            (
                await db_session.execute(
                    select(AuditLog)
                    .where(AuditLog.action.like("patient.%"))
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )

        actions = {row.action for row in rows}
        assert {
            "patient.auth.otp_requested",
            "patient.auth.account_created",
            "patient.auth.login",
            "patient.link.attempted",
            "patient.link.created",
            "patient.auth.logout_all",
        } <= actions
        for row in rows:
            assert row.actor_user_id is None, row.action
            assert row.actor_type == "patient", row.action
            assert row.patient_account_id in {None, patient.account_id}, row.action

    async def test_a_self_registered_record_has_no_staff_author(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, hospital: uuid.UUID
    ) -> None:
        """Attack: register a record and see whether the account id was written as a user id."""
        patient = await _signed_in(net, sms)
        url = f"{PATIENT}/hospitals/{hospital}/register"

        response = await patient.client.post(
            url, json=_bodies(hospital)["POST", url], headers=patient.headers
        )

        assert response.status_code == 201, response.text
        record = (
            await db_session.execute(
                select(Patient)
                .where(Patient.hospital_id == hospital, Patient.phone == patient.phone)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert record.created_by is None
        assert record.updated_by is None


# ── 2. A patient token against the staff API ─────────────────────────────────


class TestAPatientTokenAgainstTheStaffApi:
    async def test_every_staff_route_treats_a_patient_token_as_a_token_it_cannot_read(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: walk the whole staff API with a genuine patient token.

        Each route is called three times: with no token, with a token signed
        by nobody, and with the patient token. The patient token must get
        exactly what the worthless one gets.
        """
        patient = await _signed_in(net, sms)
        routes = _routes(net.application, patient=False)
        assert len(routes) > 100, "the route table was not read"
        outsider = net.browser()
        worthless = bearer(
            pyjwt.encode(
                {**_patient_payload(uuid.uuid4()), "type": "access"},
                secrets.token_urlsafe(48),
                algorithm="HS256",
            )
        )

        refused: dict[str, int] = {}
        for method, path in routes:
            url = _concrete(path)
            anonymous = await outsider.request(method, url)
            invalid = await outsider.request(method, url, headers=worthless)
            as_patient = await patient.client.request(method, url, headers=patient.headers)

            assert as_patient.status_code == invalid.status_code, (method, path)
            if anonymous.status_code == 401:
                assert as_patient.status_code == 401, (method, path)
                assert _body(as_patient) == _body(invalid), (method, path)
                refused[_family(path)] = refused.get(_family(path), 0) + 1
            # On a route that does not ask who is calling, the token must not
            # have turned a refusal into an answer.
            assert not (200 <= as_patient.status_code < 300 and anonymous.status_code >= 400), (
                method,
                path,
            )

        for family in STAFF_FAMILIES:
            assert refused.get(family, 0) >= 1, f"no protected {family} route was attacked"
        # Nearly all of the staff API asks who is calling.
        assert sum(refused.values()) > len(routes) * 0.8, refused

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/v1/auth/logout-all"),
            ("POST", "/api/v1/auth/password/change"),
            ("POST", "/api/v1/auth/mfa/enroll"),
            ("POST", "/api/v1/auth/mfa/disable"),
            ("GET", "/api/v1/users"),
            ("POST", "/api/v1/users"),
            ("GET", "/api/v1/patients"),
            ("POST", "/api/v1/patients"),
            ("GET", "/api/v1/appointments"),
            ("POST", "/api/v1/appointments"),
            ("GET", "/api/v1/invoices"),
            ("POST", "/api/v1/invoices"),
            ("GET", "/api/v1/audit-logs"),
            ("GET", "/api/v1/roles"),
            ("GET", "/api/v1/hospitals/current"),
        ],
    )
    async def test_a_patient_token_is_refused_by_each_staff_route_family(
        self, net: _Net, sms: FakeSmsSender, method: str, path: str
    ) -> None:
        """Attack: a patient calls the staff API with their token."""
        patient = await _signed_in(net, sms)

        response = await patient.client.request(method, path, headers=patient.headers)

        assert response.status_code == 401, response.text
        assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"

    async def test_a_patients_own_record_is_not_readable_through_the_staff_api(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, hospital: uuid.UUID
    ) -> None:
        """Attack: a linked patient asks the staff API for the record that is theirs."""
        phone = new_phone()
        record = await _record(db_session, hospital, phone)
        patient = await _signed_in(net, sms, phone)
        url = f"{PATIENT}/hospitals/{hospital}/link"
        linked = await patient.client.post(
            url, json=_bodies(hospital)["POST", url], headers=patient.headers
        )
        assert linked.status_code == 201, linked.text

        for path in (f"/api/v1/patients/{record.id}", f"/api/v1/patients?search={phone[3:]}"):
            response = await patient.client.get(path, headers=patient.headers)
            assert response.status_code == 401, path
            assert record.mrn not in response.text

    async def test_the_middleware_never_takes_a_patient_for_a_staff_user(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: get a patient token past the middleware as a user — above all one with no hospital."""
        patient = await _signed_in(net, sms)

        seen = (await patient.client.get(PATIENT_PROBE, headers=patient.headers)).json()

        assert seen["user_id"] is None
        assert seen["hospital_id"] is None
        assert seen["patient_account_id"] == str(patient.account_id)
        assert seen["billed_to"] == [f"patient:{patient.account_id}"]

    @pytest.mark.parametrize(
        "path",
        [
            PROBE,
            "/api/v1/auth/login",
            "/api/v1/auth/forgot-password",
            "/api/v1/patients",
            f"{PATIENT}/auth/otp/request",
            f"{PATIENT}/auth/otp/verify",
            "/api/v1/patient",
            "/API/V1/PATIENT/me",
        ],
    )
    async def test_a_patient_token_buys_no_larger_allowance_outside_its_own_endpoints(
        self, net: _Net, sms: FakeSmsSender, path: str
    ) -> None:
        """Attack: attach a self-service patient token to staff sign-in or to the code endpoints.

        Anybody with a phone can get a patient token. If it moved every
        request onto the per-account tier, one address with a handful of
        accounts would get several hundred sign-in or code attempts a minute
        instead of the per-source allowance. Outside the Patient App's own
        authenticated endpoints the token changes nothing about the billing.
        """
        patient = await _signed_in(net, sms)
        source = _source()
        # The probe, on a method the real route does not answer, at that path.
        net.application.add_route(path, _whoami, methods=["PUT"])

        seen = (await net.browser(source).put(path, headers=patient.headers)).json()

        assert seen["user_id"] is None
        assert seen["hospital_id"] is None
        assert seen["billed_to"] == [f"ip:{source}"]

    async def test_a_staff_token_is_never_taken_for_a_patient_by_the_middleware(
        self, net: _Net, staff: _Staff
    ) -> None:
        """The other direction: at most one of the two identities is ever set."""
        token = create_access_token(staff.id, staff.hospital_id)

        seen = (await net.browser().get(PROBE, headers=bearer(token))).json()

        assert seen["user_id"] == str(staff.id)
        assert seen["patient_account_id"] is None
        assert not any(key.startswith("patient:") for key in seen["billed_to"])

    @pytest.mark.parametrize("kind", FORGERIES)
    async def test_a_forged_patient_token_is_nobody_to_the_middleware(
        self, net: _Net, sms: FakeSmsSender, kind: str
    ) -> None:
        """Attack: have a forged token billed to — or logged as — the account it names."""
        patient = await _signed_in(net, sms)
        source = _source()

        seen = (
            await net.browser(source).get(PROBE, headers=bearer(_forged(kind, patient.account_id)))
        ).json()

        assert seen["patient_account_id"] is None
        assert seen["user_id"] is None
        assert seen["hospital_id"] is None
        assert seen["billed_to"] == [f"ip:{source}"]

    async def test_a_patient_refresh_token_does_not_refresh_a_staff_session(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: exchange the patient refresh token at the staff refresh endpoint."""
        patient = await _signed_in(net, sms)
        raw = patient.refresh_token

        as_body = await net.browser().post(f"{AUTH}/refresh", json={"refresh_token": raw})
        logout = await net.browser().post(f"{AUTH}/logout", json={"refresh_token": raw})

        assert as_body.status_code == 401
        assert "access_token" not in as_body.text
        assert logout.status_code in {200, 204, 401}
        # The staff endpoints did not touch the patient session.
        assert await _live_sessions(db_session, patient.account_id) == 1
        assert (await patient.client.post(REFRESH, headers=CSRF)).status_code == 200


# ── 3. Staff credentials against the patient API ─────────────────────────────


class TestStaffCredentialsAgainstThePatientApi:
    async def test_every_patient_route_treats_a_staff_credential_as_no_credential(
        self, net: _Net, db_session: AsyncSession, staff: _Staff, hospital: uuid.UUID
    ) -> None:
        """Attack: walk the whole patient API with each kind of staff credential, as a bearer."""
        session = await _staff_session(net, staff)
        credentials = {
            "staff access token": session["access_token"],
            "minted staff access token": create_access_token(staff.id, staff.hospital_id),
            "platform administrator's token": create_access_token(staff.id, None),
            "mfa ticket": create_mfa_ticket(staff.id),
            "staff refresh token": session["refresh_token"],
        }
        routes = _routes(net.application, patient=True)
        assert len(routes) >= 8, routes
        anonymous_browser = net.browser()

        protected = 0
        for method, path in routes:
            url = path.replace("{hospital_ref}", str(hospital))
            body = _bodies(hospital).get((method, url))
            anonymous = await anonymous_browser.request(method, url, json=body, headers=CSRF)
            for name, credential in credentials.items():
                attacker = net.browser()
                response = await attacker.request(
                    method, url, json=body, headers={**CSRF, **bearer(credential)}
                )
                assert response.status_code == anonymous.status_code, (name, method, path)
                assert _body_or_none(response) == _body_or_none(anonymous), (name, method, path)
                assert set_cookie_names(response) == set_cookie_names(anonymous), (name, path)
            if anonymous.status_code == 401:
                assert _body(anonymous) == UNAUTHENTICATED, (method, path)
                protected += 1

        # /me, logout-all, link, register, refresh, the three hospital
        # discovery routes, the three doctor discovery routes, doctor
        # availability, booking and the three own-appointment routes all
        # refused; request and verify are public (422 with no body) and logout
        # has nothing to end.
        assert protected == 16
        assert await _count(db_session, PatientAccount) == 0
        assert await _count(db_session, PatientRefreshToken) == 0
        assert await _count(db_session, PatientAccountLink) == 0

    @pytest.mark.parametrize("credential", ["access", "administrator", "mfa_ticket", "refresh"])
    async def test_a_staff_credential_in_the_refresh_cookie_starts_no_patient_session(
        self, net: _Net, db_session: AsyncSession, staff: _Staff, credential: str
    ) -> None:
        """Attack: put a staff credential where the patient refresh token goes.

        A staff refresh token has the very same shape as a patient one (both
        are ``generate_opaque_token``), so this one gets all the way to the
        lookup.
        """
        session = await _staff_session(net, staff)
        value = {
            "access": session["access_token"],
            "administrator": create_access_token(staff.id, None),
            "mfa_ticket": create_mfa_ticket(staff.id),
            "refresh": session["refresh_token"],
        }[credential]
        attacker = net.browser()
        cookie = {"Cookie": f"{REFRESH_COOKIE}={value}"}

        refreshed = await attacker.post(REFRESH, headers={**CSRF, **cookie})
        signed_out = await attacker.post(LOGOUT, headers={**CSRF, **cookie})

        assert refreshed.status_code == 401
        assert _body(refreshed) == UNAUTHENTICATED
        assert signed_out.status_code == 204
        assert await _count(db_session, PatientRefreshToken) == 0
        # And the staff session was neither used up nor ended by the attempt.
        assert (
            await _count(
                db_session,
                RefreshToken,
                RefreshToken.user_id == staff.id,
                RefreshToken.is_revoked.is_(False),
            )
            == 1
        )
        still_good = await net.browser().post(
            f"{AUTH}/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert still_good.status_code == 200, still_good.text

    async def test_a_staff_user_whose_id_is_also_an_account_id_is_still_refused(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, staff: _Staff
    ) -> None:
        """Attack: the ids collide — a staff token whose subject *is* a live patient account."""
        patient = await _signed_in(net, sms)
        staff_shaped = create_access_token(patient.account_id, staff.hospital_id)
        administrator_shaped = create_access_token(patient.account_id, None)

        for token in (staff_shaped, administrator_shaped):
            assert (await net.browser().get(ME, headers=bearer(token))).status_code == 401
            # And the patient's id is no user, least of all a platform administrator.
            for path in ("/api/v1/users", "/api/v1/patients", "/api/v1/audit-logs"):
                response = await net.browser().get(path, headers=bearer(token))
                assert response.status_code == 401, (path, response.text)


def _body_or_none(response: Response) -> dict[str, Any] | None:
    return _body(response) if response.content else None


def set_cookie_names(response: Response) -> list[str]:
    return sorted(header.split("=", 1)[0] for header in set_cookie_headers(response))


# ── 4. Forged tokens ─────────────────────────────────────────────────────────


class TestForgedTokens:
    @pytest.mark.parametrize("kind", FORGERIES)
    async def test_a_forged_token_is_refused_by_every_bearer_endpoint(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        hospital: uuid.UUID,
        kind: str,
    ) -> None:
        """Attack: name a real, active, linkable account in a token the server did not issue."""
        phone = new_phone()
        await _record(db_session, hospital, phone)
        victim = await _signed_in(net, sms, phone)
        token = _forged(kind, victim.account_id)
        attacker = net.browser()

        for (method, url), body in _bodies(hospital).items():
            response = await attacker.request(method, url, json=body, headers=bearer(token))

            assert response.status_code == 401, (kind, url, response.text)
            assert _body(response) == UNAUTHENTICATED, (kind, url)
            assert response.headers["www-authenticate"] == "Bearer"

        # Nothing was done in the victim's name: still signed in, still unlinked.
        assert await _live_sessions(db_session, victim.account_id) == 1
        assert await _count(db_session, PatientAccountLink) == 0
        assert await audit_rows(db_session, "patient.auth.logout_all") == []
        assert await audit_rows(db_session, "patient.link.attempted") == []

    async def test_the_genuine_token_for_the_same_account_is_accepted(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """The control for the test above: it is the forgery that is refused, not the account."""
        patient = await _signed_in(net, sms)

        for token in (patient.token, create_patient_access_token(patient.account_id)):
            response = await net.browser().get(ME, headers=bearer(token))
            assert response.status_code == 200, response.text
            assert response.json()["data"]["account"]["id"] == str(patient.account_id)

    @pytest.mark.parametrize("status", ["suspended", "closed"])
    async def test_a_genuine_token_for_an_account_that_is_not_active_is_refused_everywhere(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        hospital: uuid.UUID,
        status: str,
    ) -> None:
        """Attack: keep using a token after the account was suspended or closed."""
        patient = await _signed_in(net, sms)
        await db_session.execute(
            update(PatientAccount)
            .where(PatientAccount.id == patient.account_id)
            .values(status=status)
        )
        await db_session.commit()

        for (method, url), body in _bodies(hospital).items():
            response = await patient.client.request(method, url, json=body, headers=patient.headers)
            assert response.status_code == 401, (url, response.text)
            assert _body(response) == UNAUTHENTICATED

        # Nor can the cookie mint a new token for it.
        assert (await patient.client.post(REFRESH, headers=CSRF)).status_code == 401
        # logout-all was refused, so it ended nothing by itself.
        assert await audit_rows(db_session, "patient.auth.logout_all") == []

    async def test_a_genuine_token_for_an_account_that_does_not_exist_is_refused_everywhere(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, hospital: uuid.UUID
    ) -> None:
        """Attack: a token for an id that never was an account, and one whose account is gone."""
        patient = await _signed_in(net, sms)
        await db_session.execute(
            delete(PatientAccount).where(PatientAccount.id == patient.account_id)
        )
        await db_session.commit()

        for token in (patient.token, create_patient_access_token(uuid.uuid4())):
            for (method, url), body in _bodies(hospital).items():
                response = await net.browser().request(
                    method, url, json=body, headers=bearer(token)
                )
                assert response.status_code == 401, (url, response.text)
                assert _body(response) == UNAUTHENTICATED

    @pytest.mark.parametrize(
        "header",
        [
            "{token}",
            "bearer{token}",
            "Basic {token}",
            "Token {token}",
            "Bearer",
            "Bearer  ",
            "Bearer null",
            "Bearer undefined",
        ],
    )
    async def test_a_malformed_authorization_header_is_refused(
        self, net: _Net, sms: FakeSmsSender, header: str
    ) -> None:
        patient = await _signed_in(net, sms)

        response = await net.browser().get(
            ME, headers={"Authorization": header.format(token=patient.token)}
        )

        assert response.status_code == 401
        assert _body(response) == UNAUTHENTICATED

    async def test_the_token_is_not_read_from_anywhere_but_the_authorization_header(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: smuggle a genuine token in a query string, a cookie or another header."""
        patient = await _signed_in(net, sms)
        attacker = net.browser()

        for response in (
            await attacker.get(f"{ME}?access_token={patient.token}"),
            await attacker.get(f"{ME}?token={patient.token}"),
            await attacker.get(ME, headers={"Cookie": f"access_token={patient.token}"}),
            await attacker.get(ME, headers={"X-Access-Token": patient.token}),
            await attacker.get(ME, headers={"X-Atheris-Patient": patient.token}),
        ):
            assert response.status_code == 401


# ── 5. No claim is trusted for authorization ─────────────────────────────────


class TestNoClaimIsTrusted:
    async def _victim(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, hospital: uuid.UUID
    ) -> tuple[_Patient, _Record]:
        """Somebody else: a patient linked to their own record at the hospital."""
        phone = new_phone()
        record = await _record(db_session, hospital, phone)
        victim = await _signed_in(net, sms, phone)
        url = f"{PATIENT}/hospitals/{hospital}/link"
        linked = await victim.client.post(
            url, json=_bodies(hospital)["POST", url], headers=victim.headers
        )
        assert linked.status_code == 201, linked.text
        return victim, record

    def _loaded(self, attacker: _Patient, victim: _Patient, record: _Record, staff: _Staff) -> str:
        """The attacker's own patient token, genuinely signed, with every claim worth claiming."""
        token = create_access_token(
            attacker.account_id,
            record.hospital_id,
            roles=["super_admin", "hospital_admin", "doctor"],
            permissions=["patient.read", "patient.update", "user.read", "audit.read", "*"],
            extra_claims={
                "aud": PATIENT_TOKEN_AUDIENCE,
                "type": PATIENT_TOKEN_TYPE,
                "patient_id": str(record.id),
                "account_id": str(victim.account_id),
                "patient_account_id": str(victim.account_id),
                "user_id": str(staff.id),
                "phone": victim.phone,
                "links": [{"hospital_id": str(record.hospital_id), "patient_id": str(record.id)}],
                "is_superadmin": True,
                "scope": "staff admin",
            },
        )
        # The premise: this *is* a patient token the verifier accepts.
        claims = verify_patient_access_token(token)
        assert claims["hospital_id"] == str(record.hospital_id)
        assert claims["roles"][0] == "super_admin"
        return token

    async def test_extra_claims_change_nothing_the_patient_api_answers(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        hospital: uuid.UUID,
        staff: _Staff,
    ) -> None:
        """Attack: add a hospital, roles, permissions and the victim's ids to one's own token."""
        victim, record = await self._victim(net, sms, db_session, hospital)
        attacker = await _signed_in(net, sms)
        loaded = bearer(self._loaded(attacker, victim, record, staff))

        plain = await attacker.client.get(ME, headers=attacker.headers)
        claimed = await net.browser().get(ME, headers=loaded)

        assert claimed.status_code == 200
        assert claimed.json()["data"] == plain.json()["data"]
        data = claimed.json()["data"]
        assert data["account"]["id"] == str(attacker.account_id)
        assert data["links"] == []
        assert victim.phone[-10:] not in claimed.text
        assert record.mrn not in claimed.text

    async def test_extra_claims_do_not_link_the_attacker_to_the_victims_record(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        hospital: uuid.UUID,
        staff: _Staff,
    ) -> None:
        """Attack: claim the victim's phone and record, and link with the victim's real date of birth."""
        victim, record = await self._victim(net, sms, db_session, hospital)
        attacker = await _signed_in(net, sms)
        loaded = bearer(self._loaded(attacker, victim, record, staff))
        url = f"{PATIENT}/hospitals/{hospital}/link"

        with_dob = await net.browser().post(
            url, json=_bodies(hospital)["POST", url], headers=loaded
        )
        with_mrn = await net.browser().post(
            url,
            json={**(_bodies(hospital)["POST", url] or {}), "mrn": record.mrn},
            headers=loaded,
        )

        assert with_dob.status_code == 404, with_dob.text
        assert with_mrn.status_code == 404, with_mrn.text
        assert record.mrn not in with_dob.text + with_mrn.text
        links = (
            (
                await db_session.execute(
                    select(PatientAccountLink).execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        assert [(link.account_id, link.patient_id) for link in links] == [
            (victim.account_id, record.id)
        ]

    async def test_extra_claims_open_no_staff_route(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        hospital: uuid.UUID,
        staff: _Staff,
    ) -> None:
        """Attack: carry a hospital, a role and permissions into the staff API on a patient token."""
        victim, record = await self._victim(net, sms, db_session, hospital)
        attacker = await _signed_in(net, sms)
        loaded = bearer(self._loaded(attacker, victim, record, staff))

        for method, path in (
            ("POST", "/api/v1/auth/logout-all"),
            ("POST", "/api/v1/auth/mfa/disable"),
            ("GET", "/api/v1/users"),
            ("GET", "/api/v1/patients"),
            ("GET", f"/api/v1/patients/{record.id}"),
            ("GET", "/api/v1/appointments"),
            ("GET", "/api/v1/invoices"),
            ("GET", "/api/v1/audit-logs"),
            ("GET", "/api/v1/hospitals/current"),
        ):
            response = await net.browser().request(method, path, headers=loaded)
            assert response.status_code == 401, (path, response.text)
            assert record.mrn not in response.text

    async def test_extra_claims_are_invisible_to_the_middleware(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        hospital: uuid.UUID,
        staff: _Staff,
    ) -> None:
        """Attack: have the middleware put the claimed hospital or user on the request."""
        victim, record = await self._victim(net, sms, db_session, hospital)
        attacker = await _signed_in(net, sms)
        loaded = bearer(self._loaded(attacker, victim, record, staff))

        seen = (await net.browser().get(PATIENT_PROBE, headers=loaded)).json()

        assert seen == {
            "user_id": None,
            "hospital_id": None,
            "patient_account_id": str(attacker.account_id),
            "billed_to": [f"patient:{attacker.account_id}"],
        }

    async def test_a_staff_users_id_in_a_patient_token_is_nobody(
        self, net: _Net, db_session: AsyncSession, hospital: uuid.UUID, staff: _Staff
    ) -> None:
        """Attack: a patient-typed token whose subject is a real staff user."""
        token = _signed_by_us(staff.id, hospital_id=str(hospital), roles=["hospital_admin"])
        assert verify_patient_access_token(token)["sub"] == str(staff.id)

        for (method, url), body in _bodies(hospital).items():
            response = await net.browser().request(method, url, json=body, headers=bearer(token))
            assert response.status_code == 401, (url, response.text)
        for path in ("/api/v1/users", "/api/v1/patients", "/api/v1/audit-logs"):
            assert (await net.browser().get(path, headers=bearer(token))).status_code == 401
        assert await _count(db_session, PatientAccount) == 0

    async def test_the_genuine_token_carries_nothing_to_trust(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """What the server itself issues names the account and nothing else."""
        patient = await _signed_in(net, sms)

        claims = _claims(patient.token)

        assert set(claims) == {"sub", "iss", "aud", "iat", "exp", "type"}
        assert claims["sub"] == str(patient.account_id)
        assert claims["aud"] == "atheris-patient"
        assert claims["type"] == "patient_access"
        assert claims["exp"] - claims["iat"] == ACCESS_TTL_SECONDS == PATIENT_ACCESS_TTL_SECONDS
        assert patient.phone[-10:] not in json.dumps(claims)


# ── 6. Sessions ──────────────────────────────────────────────────────────────


class TestSessions:
    async def test_a_valid_session_reads_its_own_account_and_no_other(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        first = await _signed_in(net, sms)
        second = await _signed_in(net, sms)

        mine = await first.client.get(ME, headers=first.headers)

        assert mine.status_code == 200
        account = mine.json()["data"]["account"]
        assert account["id"] == str(first.account_id)
        assert account["phone_masked"].endswith(first.phone[-4:])
        assert first.phone[-10:] not in mine.text
        assert str(second.account_id) not in mine.text

    async def test_an_expired_access_token_is_refused_and_the_cookie_alone_restores_nothing_else(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: keep using an access token past its fifteen minutes."""
        patient = await _signed_in(net, sms)
        past = datetime.now(UTC) - timedelta(seconds=settings.JWT_LEEWAY_SECONDS + 5)
        expired = _signed_by_us(patient.account_id, iat=past - timedelta(minutes=15), exp=past)

        response = await patient.client.get(ME, headers=bearer(expired))

        assert response.status_code == 401
        assert _body(response) == UNAUTHENTICATED
        # The session itself is intact: the cookie buys a new token, as designed.
        refreshed = await patient.client.post(REFRESH, headers=CSRF)
        assert refreshed.status_code == 200
        fresh = refreshed.json()["data"]["access_token"]
        assert (await patient.client.get(ME, headers=bearer(fresh))).status_code == 200

    async def test_configuration_cannot_stretch_a_patient_access_token(
        self, net: _Net, sms: FakeSmsSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack (misconfiguration): a long staff TTL must not lengthen patient tokens."""
        monkeypatch.setattr(settings, "JWT_ACCESS_TTL_SECONDS", 30 * 24 * 3600)

        patient = await _signed_in(net, sms)
        refreshed = await patient.client.post(REFRESH, headers=CSRF)

        for token in (patient.token, refreshed.json()["data"]["access_token"]):
            claims = _claims(token)
            assert claims["exp"] - claims["iat"] == ACCESS_TTL_SECONDS

    async def test_a_refresh_rotates_the_token_and_retires_the_old_one(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        patient = await _signed_in(net, sms)
        old = patient.refresh_token

        response = await patient.client.post(REFRESH, headers=CSRF)

        assert response.status_code == 200, response.text
        new = patient.refresh_token
        assert new != old
        assert set(response.json()["data"]) == {"access_token", "expires_in"}
        assert response.json()["data"]["expires_in"] == ACCESS_TTL_SECONDS
        assert response.headers["cache-control"] == "no-store"
        rows = {
            row.token_hash: row
            for row in (
                await db_session.execute(
                    select(PatientRefreshToken)
                    .where(PatientRefreshToken.account_id == patient.account_id)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }
        assert set(rows) == {hash_token(old), hash_token(new)}
        assert rows[hash_token(old)].is_revoked is True
        revoked_at = rows[hash_token(old)].revoked_at
        assert revoked_at is not None
        assert rows[hash_token(old)].rotated_by_token_id == rows[hash_token(new)].id
        assert rows[hash_token(new)].is_revoked is False
        assert rows[hash_token(new)].expires_at - revoked_at == REFRESH_TTL

    async def test_a_rotated_token_presented_again_ends_every_session_and_is_audited(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: a thief replays a refresh token the owner has already rotated."""
        phone = new_phone()
        owner = await _signed_in(net, sms, phone)
        other_device = await _signed_in(net, sms, phone)
        stolen = owner.refresh_token
        assert (await owner.client.post(REFRESH, headers=CSRF)).status_code == 200
        assert await _live_sessions(db_session, owner.account_id) == 2

        thief = net.browser()
        replay = await thief.post(REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={stolen}"})

        assert replay.status_code == 401
        assert _body(replay) == UNAUTHENTICATED
        assert "access_token" not in replay.text
        assert await _live_sessions(db_session, owner.account_id) == 0
        [row] = await audit_rows(db_session, "patient.auth.refresh_reuse_detected")
        # The owner's current token and the other device's are both dead.
        assert (await owner.client.post(REFRESH, headers=CSRF)).status_code == 401
        assert (await other_device.client.post(REFRESH, headers=CSRF)).status_code == 401

        assert row.actor_type == "patient"
        assert row.actor_user_id is None
        assert row.hospital_id is None
        assert row.patient_account_id == owner.account_id
        assert row.target_id == owner.account_id
        assert row.context == {"sessions_ended": 2}
        assert stolen not in json.dumps(row.context)

    async def test_a_reuse_does_not_touch_anybody_elses_sessions(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: use reuse detection to sign a stranger out."""
        attacker = await _signed_in(net, sms)
        bystander = await _signed_in(net, sms)
        old = attacker.refresh_token
        await attacker.client.post(REFRESH, headers=CSRF)

        await net.browser().post(REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={old}"})

        assert await _live_sessions(db_session, attacker.account_id) == 0
        assert await _live_sessions(db_session, bystander.account_id) == 1
        assert (await bystander.client.post(REFRESH, headers=CSRF)).status_code == 200

    async def test_a_revoked_session_cannot_be_refreshed(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: keep a session alive after it was revoked on the server."""
        patient = await _signed_in(net, sms)
        await db_session.execute(
            update(PatientRefreshToken)
            .where(PatientRefreshToken.token_hash == hash_token(patient.refresh_token))
            .values(is_revoked=True, revoked_at=datetime.now(UTC))
        )
        await db_session.commit()

        response = await patient.client.post(REFRESH, headers=CSRF)

        assert response.status_code == 401
        assert _body(response) == UNAUTHENTICATED
        assert any(
            header.startswith(f"{REFRESH_COOKIE}=") and "Max-Age=0" in header
            for header in set_cookie_headers(response)
        )
        assert patient.client.cookies.get(REFRESH_COOKIE) is None

    async def test_a_session_ends_after_seven_days_and_not_before(
        self, net: _Net, sms: FakeSmsSender, clock: ThrottleClock
    ) -> None:
        """Attack: refresh a session that was last used more than seven days ago."""
        on_time = await _signed_in(net, sms)
        too_late = await _signed_in(net, sms)

        clock.advance(days=6, hours=23, minutes=59)
        assert (await on_time.client.post(REFRESH, headers=CSRF)).status_code == 200
        clock.advance(minutes=2)

        assert (await too_late.client.post(REFRESH, headers=CSRF)).status_code == 401
        # The one that was rotated in time got a new seven days.
        assert (await on_time.client.post(REFRESH, headers=CSRF)).status_code == 200

    async def test_logout_ends_this_session_only(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        here = await _signed_in(net, sms, phone)
        elsewhere = await _signed_in(net, sms, phone)
        ended = here.refresh_token

        response = await here.client.post(LOGOUT, headers=CSRF)

        assert response.status_code == 204
        assert response.content == b""
        assert here.client.cookies.get(REFRESH_COOKIE) is None
        assert await _live_sessions(db_session, here.account_id) == 1
        assert (await elsewhere.client.post(REFRESH, headers=CSRF)).status_code == 200
        [row] = await audit_rows(db_session, "patient.auth.logout")
        assert row.patient_account_id == here.account_id
        assert row.actor_type == "patient"
        # The ended token is dead for good — and presenting it is treated as theft.
        replay = await net.browser().post(
            REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={ended}"}
        )
        assert replay.status_code == 401

    async def test_logout_all_ends_every_session_and_forgets_every_device(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        phone = new_phone()
        here = await _signed_in(net, sms, phone)
        elsewhere = await _signed_in(net, sms, phone)
        bystander = await _signed_in(net, sms)
        assert (
            await _count(db_session, PatientDevice, PatientDevice.account_id == here.account_id)
            == 2
        )

        response = await here.client.post(LOGOUT_ALL, headers=here.headers)

        assert response.status_code == 204
        assert await _live_sessions(db_session, here.account_id) == 0
        assert (
            await _count(db_session, PatientDevice, PatientDevice.account_id == here.account_id)
            == 0
        )
        assert (await elsewhere.client.post(REFRESH, headers=CSRF)).status_code == 401
        assert (await here.client.post(REFRESH, headers=CSRF)).status_code == 401
        [row] = await audit_rows(db_session, "patient.auth.logout_all")
        assert row.patient_account_id == here.account_id
        assert row.context == {"sessions_ended": 2}
        # Nobody else was signed out.
        assert await _live_sessions(db_session, bystander.account_id) == 1

    async def test_logout_all_cannot_be_called_on_the_cookie_alone(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack (CSRF): a cross-site request, riding the cookie, signs the patient out everywhere."""
        patient = await _signed_in(net, sms)

        response = await patient.client.post(LOGOUT_ALL, headers=CSRF)

        assert response.status_code == 401
        assert await _live_sessions(db_session, patient.account_id) == 1

    async def test_after_logout_all_an_access_token_already_issued_dies_within_its_fifteen_minutes(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """What "log out everywhere" does and does not end, pinned.

        Refresh tokens end at once. An access token that was already issued is
        a signed, self-contained fifteen-minute token: it cannot be renewed,
        and it cannot outlive the ``exp`` it was signed with. (That it keeps
        working until then is reported as an open issue; nothing in the
        binding design asks for a per-token revocation check.)
        """
        patient = await _signed_in(net, sms)
        await patient.client.post(LOGOUT_ALL, headers=patient.headers)

        claims = _claims(patient.token)

        assert claims["exp"] - claims["iat"] == ACCESS_TTL_SECONDS
        assert (await patient.client.post(REFRESH, headers=CSRF)).status_code == 401
        assert await _live_sessions(db_session, patient.account_id) == 0


# ── 7. The cookies ───────────────────────────────────────────────────────────


def _attributes(header: str) -> tuple[str, str, dict[str, str]]:
    """A ``Set-Cookie`` header as ``(name, value, {attribute: value})``, attributes lower-cased."""
    first, *rest = (part.strip() for part in header.split(";"))
    name, _, value = first.partition("=")
    attributes: dict[str, str] = {}
    for part in rest:
        key, _, attribute_value = part.partition("=")
        attributes[key.strip().lower()] = attribute_value.strip()
    return name, value, attributes


class TestCookies:
    @pytest.fixture(autouse=True)
    def _as_deployed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The cookies as production sends them: Secure, prefixed, over HTTPS."""
        monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", True)

    async def _sign_in(self, net: _Net, sms: FakeSmsSender) -> tuple[AsyncClient, Response]:
        client = net.browser(base_url="https://test")
        phone = new_phone()
        requested = await client.post(REQUEST, json={"phone": phone})
        verified = await client.post(
            VERIFY,
            json={
                "challenge_id": requested.json()["data"]["challenge_id"],
                "code": sms.code_for(phone),
            },
        )
        assert verified.status_code == 200, verified.text
        return client, verified

    def _check_refresh_cookie(self, header: str) -> str:
        name, value, attributes = _attributes(header)
        assert name == "__Secure-atheris-patient-refresh"
        assert "httponly" in attributes
        assert "secure" in attributes
        assert attributes["samesite"].lower() == "strict"
        assert attributes["path"] == "/api/v1/patient/auth"
        assert "domain" not in attributes
        return value

    async def test_the_refresh_cookie_is_httponly_secure_strict_path_scoped_and_host_only(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: read the cookie from script, over HTTP, cross-site, or from a sibling host."""
        _, verified = await self._sign_in(net, sms)

        cookies = {_attributes(h)[0]: h for h in set_cookie_headers(verified)}

        assert set(cookies) == {"__Secure-atheris-patient-refresh", "__Host-atheris-patient-device"}
        value = self._check_refresh_cookie(cookies["__Secure-atheris-patient-refresh"])
        assert re.fullmatch(r"[A-Za-z0-9_-]{64}", value)
        _, _, attributes = _attributes(cookies["__Secure-atheris-patient-refresh"])
        assert int(attributes["max-age"]) == int(REFRESH_TTL.total_seconds())

    async def test_the_device_cookie_meets_the_host_prefix_rules(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """``__Host-`` is honoured by a browser only with Secure, Path=/ and no Domain."""
        _, verified = await self._sign_in(net, sms)

        [header] = [h for h in set_cookie_headers(verified) if h.startswith("__Host-")]
        name, _, attributes = _attributes(header)

        assert name == "__Host-atheris-patient-device"
        assert "httponly" in attributes
        assert "secure" in attributes
        assert attributes["samesite"].lower() == "strict"
        assert attributes["path"] == "/"
        assert "domain" not in attributes

    async def test_a_rotated_cookie_keeps_every_attribute(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        client, verified = await self._sign_in(net, sms)

        refreshed = await client.post(REFRESH, headers=CSRF)

        assert refreshed.status_code == 200, refreshed.text
        [header] = set_cookie_headers(refreshed)
        new = self._check_refresh_cookie(header)
        [old_header] = [h for h in set_cookie_headers(verified) if h.startswith("__Secure-")]
        assert new != _attributes(old_header)[1]

    async def test_clearing_the_cookie_uses_the_same_attributes_so_the_browser_obeys(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """A ``__Secure-`` cookie can only be overwritten by a Secure one on the same path."""
        client, _ = await self._sign_in(net, sms)

        for response in (
            await client.post(LOGOUT, headers=CSRF),
            await client.post(REFRESH, headers=CSRF),  # now refused: nothing to refresh
        ):
            [header] = set_cookie_headers(response)
            value = self._check_refresh_cookie(header)
            _, _, attributes = _attributes(header)
            assert value in {"", '""'}
            assert attributes["max-age"] == "0"

    async def test_the_browser_sends_the_refresh_cookie_to_the_auth_path_and_nowhere_else(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: harvest the refresh token from a request to any other endpoint."""
        client, verified = await self._sign_in(net, sms)
        token = verified.json()["data"]["access_token"]

        to_me = await client.get(ME, headers=bearer(token))
        to_probe = await client.get(PROBE)
        to_refresh = await client.post(REFRESH, headers=CSRF)

        assert "__Secure-atheris-patient-refresh" not in to_me.request.headers.get("cookie", "")
        assert "__Secure-atheris-patient-refresh" not in to_probe.request.headers.get("cookie", "")
        assert "__Secure-atheris-patient-refresh" in to_refresh.request.headers["cookie"]

    async def test_the_unprefixed_cookie_name_is_ignored_when_cookies_are_secure(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        """Attack: a sibling host plants the token under the name that carries no prefix."""
        client, _ = await self._sign_in(net, sms)
        raw = client.cookies.get("__Secure-atheris-patient-refresh")
        assert raw

        planted = await net.browser(base_url="https://test").post(
            REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={raw}"}
        )

        assert planted.status_code == 401
        # The real cookie was not burnt by the attempt.
        assert (await client.post(REFRESH, headers=CSRF)).status_code == 200

    async def test_a_second_cookie_of_the_same_name_does_not_win(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack (cookie tossing): send the victim's browser a second refresh cookie."""
        client, _ = await self._sign_in(net, sms)
        name = "__Secure-atheris-patient-refresh"
        raw = client.cookies.get(name)
        attacker_client, _ = await self._sign_in(net, sms)
        attackers = attacker_client.cookies.get(name)

        for cookie in (f"{name}={attackers}; {name}={raw}", f"{name}={raw}; {name}={attackers}"):
            response = await net.browser(base_url="https://test").post(
                REFRESH, headers={**CSRF, "Cookie": cookie}
            )
            assert response.status_code == 401
            assert "access_token" not in response.text

        # Neither session was rotated, revoked or swapped for the other.
        assert await _count(db_session, PatientRefreshToken, PatientRefreshToken.is_revoked) == 0


# ── 8. Cross-site requests on the cookie endpoints ───────────────────────────


#: Headers a cross-site page can cause a browser to send without a preflight
#: — and so the ones that must never be enough.
NOT_THE_APP: list[dict[str, str]] = [
    {},
    {"X-Atheris-Patient": "0"},
    {"X-Atheris-Patient": "true"},
    {"X-Atheris-Patient": ""},
    {"X-Atheris-Patient": "1, 1"},
    {"X-Requested-With": "XMLHttpRequest"},
    {"X-Atheris-Patient": "1", "Origin": "https://evil.example"},
    {"X-Atheris-Patient": "1", "Origin": "null"},
    {"X-Atheris-Patient": "1", "Origin": "http://localhost:5174.evil.example"},
    {"X-Atheris-Patient": "1", "Origin": "http://evil.example/http://localhost:5174"},
    {"X-Atheris-Patient": "1", "Origin": "http://localhost:5173"},
    {"X-Atheris-Patient": "1", "Origin": "https://localhost:5174"},
    {"X-Atheris-Patient": "1", "Origin": "http://localhost"},
    {"X-Atheris-Patient": "1", "Origin": "http://localhost:5174@evil.example"},
    {"X-Atheris-Patient": "1", "Origin": "http://localhost:5174, https://evil.example"},
    {"X-Atheris-Patient": "1", "Origin": ""},
]


class TestCrossSiteRequests:
    @pytest.mark.parametrize("headers", NOT_THE_APP, ids=lambda h: json.dumps(h, sort_keys=True))
    async def test_a_refresh_the_app_did_not_send_is_refused_and_rotates_nothing(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        headers: dict[str, str],
    ) -> None:
        """Attack (CSRF): another site makes the patient's browser call refresh."""
        patient = await _signed_in(net, sms)
        held = patient.refresh_token

        response = await patient.client.post(REFRESH, headers=headers)

        assert response.status_code == 401, response.text
        assert _body(response) == UNAUTHENTICATED
        assert "access_token" not in response.text
        assert set_cookie_headers(response) == []
        # No rotation happened: the very same token is still the live one.
        assert patient.refresh_token == held
        assert await _count(db_session, PatientRefreshToken) == 1
        assert await _live_sessions(db_session, patient.account_id) == 1
        assert (await patient.client.post(REFRESH, headers=CSRF)).status_code == 200

    @pytest.mark.parametrize("headers", NOT_THE_APP, ids=lambda h: json.dumps(h, sort_keys=True))
    async def test_a_logout_the_app_did_not_send_is_refused_and_ends_nothing(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        headers: dict[str, str],
    ) -> None:
        """Attack (CSRF): another site signs the patient out."""
        patient = await _signed_in(net, sms)

        response = await patient.client.post(LOGOUT, headers=headers)

        assert response.status_code == 401, response.text
        assert _body(response) == UNAUTHENTICATED
        assert set_cookie_headers(response) == []
        assert await _live_sessions(db_session, patient.account_id) == 1
        assert await audit_rows(db_session, "patient.auth.logout") == []

    async def test_two_origin_headers_are_refused_even_when_one_is_the_app(
        self, net: _Net, sms: FakeSmsSender
    ) -> None:
        patient = await _signed_in(net, sms)

        response = await patient.client.post(
            REFRESH,
            headers=[
                ("X-Atheris-Patient", "1"),
                ("Origin", "http://localhost:5174"),
                ("Origin", "https://evil.example"),
            ],
        )

        assert response.status_code == 401

    @pytest.mark.parametrize("origin", ["http://localhost:5174", "HTTP://LOCALHOST:5174", None])
    async def test_the_app_itself_is_let_through(
        self, net: _Net, sms: FakeSmsSender, origin: str | None
    ) -> None:
        """The control: the header, and the configured origin (or none, as a non-browser sends)."""
        patient = await _signed_in(net, sms)
        headers = {**CSRF, **({"Origin": origin} if origin else {})}

        assert (await patient.client.post(REFRESH, headers=headers)).status_code == 200
        assert (await patient.client.post(LOGOUT, headers=headers)).status_code == 204

    async def test_only_the_configured_origins_count(
        self, net: _Net, sms: FakeSmsSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: rely on the development default once a deployment has named its own origin."""
        monkeypatch.setattr(settings, "PATIENT_APP_ORIGINS", ["https://patients.hospital.example"])
        patient = await _signed_in(net, sms)

        local = await patient.client.post(
            REFRESH, headers={**CSRF, "Origin": "http://localhost:5174"}
        )
        configured = await patient.client.post(
            REFRESH, headers={**CSRF, "Origin": "https://patients.hospital.example"}
        )

        assert local.status_code == 401
        assert configured.status_code == 200

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
    async def test_the_cookie_endpoints_answer_post_only(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, method: str
    ) -> None:
        """Attack: a top-level GET navigation carries a cookie; it must not reach the handler."""
        patient = await _signed_in(net, sms)

        for url in (REFRESH, LOGOUT):
            response = await patient.client.request(method, url, headers=CSRF)
            assert response.status_code == 405, (method, url)

        assert await _live_sessions(db_session, patient.account_id) == 1
        assert await _count(db_session, PatientRefreshToken) == 1


# ── 9. Where the refresh token is and is not ─────────────────────────────────


class TestTheRefreshTokenStaysInItsCookie:
    async def test_it_is_in_no_response_body_log_line_or_audit_record_and_is_stored_hashed(
        self,
        net: _Net,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        all_logs: _LogTrap,
    ) -> None:
        """Attack: read a refresh token out of a body, the logs, the audit trail or the table."""
        phone = new_phone()
        patient = await _signed_in(net, sms, phone)
        other = await _signed_in(net, sms, phone)
        tokens = [patient.refresh_token, other.refresh_token]
        device_tokens = [
            token
            for client in (patient.client, other.client)
            for token in (client.cookies.get(DEVICE_COOKIE) or "").split(".")
            if token
        ]
        assert len(device_tokens) == 2

        responses = [
            await patient.client.post(REQUEST, json={"phone": phone}),
            await patient.client.get(ME, headers=patient.headers),
            await patient.client.post(REFRESH, headers=CSRF),
        ]
        tokens.append(patient.refresh_token)
        responses.append(await patient.client.post(REFRESH, headers=CSRF))
        tokens.append(patient.refresh_token)
        # A reuse (the first token again), a sign-out and a sign-out everywhere.
        responses.append(
            await net.browser().post(
                REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={tokens[0]}"}
            )
        )
        responses.append(await other.client.post(LOGOUT, headers=CSRF))
        fresh = await _signed_in(net, sms, phone)
        tokens.append(fresh.refresh_token)
        responses.append(await fresh.client.post(LOGOUT_ALL, headers=fresh.headers))
        assert len(set(tokens)) == 5

        secrets_held = tokens + device_tokens
        # Bodies, and every header but the one cookie it travels in.
        for response in responses:
            for secret in secrets_held:
                assert secret not in response.text, response.request.url
                for name, value in response.headers.multi_items():
                    if name.lower() != "set-cookie":
                        assert secret not in value, (response.request.url, name)

        # Logs: the events were captured, and none of them carries a token.
        logged = all_logs.everything
        for event in (
            "patient_login",
            "patient_token_refreshed",
            "patient_refresh_token_reuse_detected",
        ):
            assert event in logged, f"{event} was not captured: the trap proves nothing"
        for secret in secrets_held:
            assert secret not in logged

        # Audit: every column of every row.
        audit = (
            (await db_session.execute(select(AuditLog).execution_options(populate_existing=True)))
            .scalars()
            .all()
        )
        assert len(audit) >= 8
        dumped = json.dumps(
            [
                {column.name: getattr(row, column.name) for column in AuditLog.__table__.columns}
                for row in audit
            ],
            default=str,
        )
        for secret in secrets_held:
            assert secret not in dumped

        # At rest: only SHA-256 hashes.
        stored = (
            (
                await db_session.execute(
                    select(PatientRefreshToken).execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        assert {row.token_hash for row in stored} == {hash_token(token) for token in tokens}
        for row in stored:
            flat = json.dumps(
                {c.name: getattr(row, c.name) for c in PatientRefreshToken.__table__.columns},
                default=str,
            )
            for secret in secrets_held:
                assert secret not in flat
        devices = (
            (
                await db_session.execute(
                    select(PatientDevice).execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        for device in devices:
            assert device.token_hash not in device_tokens

    async def test_a_refresh_token_is_not_an_access_token_and_the_reverse(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: present each of the session's two credentials where the other belongs."""
        patient = await _signed_in(net, sms)

        as_bearer = await net.browser().get(ME, headers=bearer(patient.refresh_token))
        as_cookie = await net.browser().post(
            REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={patient.token}"}
        )

        assert as_bearer.status_code == 401
        assert as_cookie.status_code == 401
        assert await _live_sessions(db_session, patient.account_id) == 1

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "x",
            "A" * 63,
            "A" * 65,
            "A" * 64,
            "../" * 21 + "a",
            "%00" * 21 + "a",
            "'; DROP--" * 7 + "a",
        ],
    )
    async def test_a_made_up_refresh_token_is_refused(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession, value: str
    ) -> None:
        patient = await _signed_in(net, sms)

        response = await net.browser().post(
            REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={value}"}
        )

        assert response.status_code == 401
        assert _body(response) == UNAUTHENTICATED
        assert await _live_sessions(db_session, patient.account_id) == 1

    async def test_the_hash_of_a_refresh_token_is_not_a_refresh_token(
        self, net: _Net, sms: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Attack: somebody who read ``patient_refresh_tokens`` presents what is stored there."""
        patient = await _signed_in(net, sms)
        stored = hash_token(patient.refresh_token)
        assert len(stored) == 64

        response = await net.browser().post(
            REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={stored}"}
        )

        assert response.status_code == 401
        assert await _live_sessions(db_session, patient.account_id) == 1


# ── 10. Parallel refreshes, on real connections ──────────────────────────────


class _RealDatabase:
    """A pool of real connections to the test database. Everything is committed."""

    def __init__(self, engine: AsyncEngine, sms: FakeSmsSender) -> None:
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        self.phones: list[str] = []
        self.sources: set[str] = set()
        application = create_app()
        # The application's own request-scoped dependency then opens one real
        # session (one pooled connection) per request, as it does in service.
        application.state.db_session_factory = self.factory
        application.dependency_overrides[get_sms_sender] = lambda: sms
        self.net = _Net(application)
        self.sms = sms

    def browser(self) -> AsyncClient:
        source = _source()
        self.sources.add(source)
        return self.net.browser(source)

    async def sign_in(self, phone: str | None = None) -> _Patient:
        number = phone or new_phone()
        if number not in self.phones:
            self.phones.append(number)
        client = self.browser()
        return _Patient(client, number, await sign_in(client, self.sms, number))

    async def count(self, model: Any, *criteria: Any) -> int:
        async with self.factory() as session:
            return await _count(session, model, *criteria)

    async def wait_for_a_blocked_statement(self) -> None:
        """Return once some statement in this database is waiting on a row lock."""
        async with self.factory() as session:
            for _ in range(3000):
                waiting = await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
                if int(waiting.scalar_one()) > 0:
                    return
                await session.rollback()
                await asyncio.sleep(0.01)
        raise AssertionError("nothing ever waited on the lock")

    async def cleanup(self) -> None:
        """Delete every row this test committed: by phone, by account, by throttle key."""
        await self.net.aclose()
        async with self.factory() as session:
            accounts = (
                (
                    await session.execute(
                        select(PatientAccount.id).where(PatientAccount.phone.in_(self.phones))
                    )
                )
                .scalars()
                .all()
            )
            challenges = (
                (
                    await session.execute(
                        select(PatientOtpChallenge.id).where(
                            PatientOtpChallenge.phone.in_(self.phones)
                        )
                    )
                )
                .scalars()
                .all()
            )
            devices = (
                (
                    await session.execute(
                        select(PatientDevice.id).where(PatientDevice.account_id.in_(accounts))
                    )
                )
                .scalars()
                .all()
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


class _RotationGate:
    """Holds the first rotation between writing the new token and committing it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.inside = asyncio.Event()
        self.release = asyncio.Event()
        self._armed = True
        real_revoke = PatientRefreshTokenRepository.revoke
        gate = self

        async def _revoke(
            repository: PatientRefreshTokenRepository,
            token: PatientRefreshToken,
            *,
            now: datetime,
            rotated_by_id: uuid.UUID | None = None,
        ) -> None:
            if gate._armed and rotated_by_id is not None:
                gate._armed = False
                gate.inside.set()
                await asyncio.wait_for(gate.release.wait(), timeout=30)
            await real_revoke(repository, token, now=now, rotated_by_id=rotated_by_id)

        monkeypatch.setattr(PatientRefreshTokenRepository, "revoke", _revoke)


class TestParallelRefresh:
    """Requests that arrive at the same moment, each on its own connection.

    The single-connection ``db_session`` used elsewhere cannot show this: on
    it two requests can never be inside a rotation at once.
    """

    async def test_one_token_refreshed_many_times_at_once_yields_exactly_one_new_session(
        self, real_db: _RealDatabase
    ) -> None:
        """Attack: a thief and the owner refresh the same token in the same instant."""
        patient = await real_db.sign_in()
        cookie = {**CSRF, "Cookie": f"{REFRESH_COOKIE}={patient.refresh_token}"}
        racers = [real_db.browser() for _ in range(8)]

        responses = await asyncio.gather(*(racer.post(REFRESH, headers=cookie) for racer in racers))

        statuses = sorted(response.status_code for response in responses)
        assert statuses == [200] + [401] * 7
        # One rotation happened: the original token and one successor exist.
        assert (
            await real_db.count(
                PatientRefreshToken, PatientRefreshToken.account_id == patient.account_id
            )
            == 2
        )
        # The losers presented a token that had just been rotated away. That
        # is indistinguishable from theft, so every session is ended — the
        # winner's included — and it is on the audit trail.
        assert (
            await real_db.count(
                PatientRefreshToken,
                PatientRefreshToken.account_id == patient.account_id,
                PatientRefreshToken.is_revoked.is_(False),
            )
            == 0
        )
        assert (
            await real_db.count(
                AuditLog,
                AuditLog.action == "patient.auth.refresh_reuse_detected",
                AuditLog.patient_account_id == patient.account_id,
            )
            >= 1
        )
        [winner] = [response for response in responses if response.status_code == 200]
        assert "access_token" in winner.json()["data"]

    async def test_parallel_logouts_and_a_refresh_never_leave_a_live_session_behind(
        self, real_db: _RealDatabase
    ) -> None:
        """Attack: race a refresh against the owner's sign-out to keep a session alive."""
        patient = await real_db.sign_in()
        cookie = {**CSRF, "Cookie": f"{REFRESH_COOKIE}={patient.refresh_token}"}

        responses = await asyncio.gather(
            real_db.browser().post(LOGOUT, headers=cookie),
            real_db.browser().post(REFRESH, headers=cookie),
            real_db.browser().post(LOGOUT, headers=cookie),
            real_db.browser().post(REFRESH, headers=cookie),
        )

        refreshes = [r for r in responses if str(r.request.url).endswith("/refresh")]
        assert sum(r.status_code == 200 for r in refreshes) <= 1
        live = await real_db.count(
            PatientRefreshToken,
            PatientRefreshToken.account_id == patient.account_id,
            PatientRefreshToken.is_revoked.is_(False),
        )
        if all(r.status_code == 401 for r in refreshes):
            # The sign-out won outright.
            assert live == 0
        else:
            # A refresh won the race for the row. The sign-out that followed
            # found the token already rotated and ended nothing — at most the
            # one successor is alive, never the token that was signed out.
            assert live <= 1
            assert (
                await real_db.count(
                    PatientRefreshToken,
                    PatientRefreshToken.token_hash == hash_token(patient.refresh_token),
                    PatientRefreshToken.is_revoked.is_(False),
                )
                == 0
            )

    async def test_logout_all_also_ends_a_session_that_is_being_rotated_at_that_moment(
        self, real_db: _RealDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: a thief holding a session keeps refreshing it while the owner logs out everywhere.

        Made exact rather than left to chance: the thief's refresh is held
        between inserting the replacement token and committing, the owner's
        "log out everywhere" is started and seen to be waiting on the lock,
        and then both are let go.
        """
        phone = new_phone()
        owner = await real_db.sign_in(phone)
        thief_cookie = {**CSRF, "Cookie": f"{REFRESH_COOKIE}={owner.refresh_token}"}
        gate = _RotationGate(monkeypatch)

        refresh = asyncio.create_task(real_db.browser().post(REFRESH, headers=thief_cookie))
        await asyncio.wait_for(gate.inside.wait(), timeout=30)
        logout_all = asyncio.create_task(owner.client.post(LOGOUT_ALL, headers=owner.headers))
        await real_db.wait_for_a_blocked_statement()
        gate.release.set()
        refreshed, signed_out = await asyncio.gather(refresh, logout_all)

        assert signed_out.status_code == 204
        assert refreshed.status_code in {200, 401}
        # "Everywhere" has to include the session the thief just rotated into.
        assert (
            await real_db.count(
                PatientRefreshToken,
                PatientRefreshToken.account_id == owner.account_id,
                PatientRefreshToken.is_revoked.is_(False),
            )
            == 0
        )

    async def test_reuse_detection_also_ends_a_session_that_is_being_rotated_at_that_moment(
        self, real_db: _RealDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: a thief keeps rotating a stolen session while theft is being detected.

        The same window as "log out everywhere", reached the other way: a
        token that was already rotated away is presented again, which ends
        every session of the account. A sibling session caught mid-rotation
        must be ended with the rest, not survive on a replacement the
        revocation could not see.
        """
        phone = new_phone()
        owner = await real_db.sign_in(phone)
        rotated_away = owner.refresh_token
        first_rotation = await real_db.browser().post(
            REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={rotated_away}"}
        )
        assert first_rotation.status_code == 200
        stolen = (await real_db.sign_in(phone)).refresh_token
        gate = _RotationGate(monkeypatch)

        thief = asyncio.create_task(
            real_db.browser().post(
                REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={stolen}"}
            )
        )
        await asyncio.wait_for(gate.inside.wait(), timeout=30)
        replay = asyncio.create_task(
            real_db.browser().post(
                REFRESH, headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={rotated_away}"}
            )
        )
        await real_db.wait_for_a_blocked_statement()
        gate.release.set()
        thief_response, replay_response = await asyncio.gather(thief, replay)

        assert replay_response.status_code == 401
        assert thief_response.status_code in {200, 401}
        assert (
            await real_db.count(
                PatientRefreshToken,
                PatientRefreshToken.account_id == owner.account_id,
                PatientRefreshToken.is_revoked.is_(False),
            )
            == 0
        )
        assert (
            await real_db.count(
                AuditLog,
                AuditLog.action == "patient.auth.refresh_reuse_detected",
                AuditLog.patient_account_id == owner.account_id,
            )
            == 1
        )

    async def test_log_out_everywhere_and_rotations_never_deadlock_or_leave_a_session(
        self, real_db: _RealDatabase
    ) -> None:
        """Rotations, a replay and "log out everywhere", all at once, several times over.

        Every path takes the account's row before any of its token rows, so
        none can wait on another in a circle: no request fails with a server
        error, and nothing is left alive once everything has been ended.
        """
        for _ in range(4):
            # A new number each round: four sign-ins stay inside one number's allowance.
            phone = new_phone()
            sessions = [await real_db.sign_in(phone) for _ in range(3)]
            owner = sessions[0]
            cookies = [
                {**CSRF, "Cookie": f"{REFRESH_COOKIE}={session.refresh_token}"}
                for session in sessions
            ]

            responses = await asyncio.gather(
                real_db.browser().post(REFRESH, headers=cookies[1]),
                owner.client.post(LOGOUT_ALL, headers=owner.headers),
                real_db.browser().post(REFRESH, headers=cookies[2]),
                real_db.browser().post(REFRESH, headers=cookies[1]),
                real_db.browser().post(LOGOUT, headers=cookies[2]),
                real_db.browser().post(REFRESH, headers=cookies[0]),
            )

            assert all(response.status_code < 500 for response in responses), [
                response.status_code for response in responses
            ]
            # Whatever survived the race is ended by one more "log out everywhere".
            again = await real_db.sign_in(phone)
            assert (await again.client.post(LOGOUT_ALL, headers=again.headers)).status_code == 204
            assert (
                await real_db.count(
                    PatientRefreshToken,
                    PatientRefreshToken.account_id == owner.account_id,
                    PatientRefreshToken.is_revoked.is_(False),
                )
                == 0
            )
