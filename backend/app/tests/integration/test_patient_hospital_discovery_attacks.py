"""Adversarial tests for Patient App hospital discovery (Task 29).

``docs/modules/15-patient-app.md`` §11 and §27.6. Every test names an attack
and asserts the security outcome, over HTTP, through the real application and
a real PostgreSQL. The functional behaviour is covered by
``app/tests/api/test_patient_hospitals_api.py``; nothing here repeats it for
its own sake.

**The oracle.** Wherever a test says "exactly these hospitals", the expected
answer is worked out in Python from the rows in the database
(:func:`_directory`) — the listing rule, the literal substring and the city
comparison are restated here on purpose, so a change to the query has to be
made in two places. The whole directory is compared, not only the hospitals a
test inserted: a wildcard that matched somebody else's hospital would be seen.

**Refusals.** "Identical" means the bytes of the body once the request id is
taken out, and every header that is not a per-request counter.

**Known defects** are kept as ``xfail(strict=True)`` with the reason, so the
suite goes red the day one is fixed and the mark has to be removed. There are
none at present.
"""

from __future__ import annotations

import logging
import math
import random
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import jwt as pyjwt
import pytest
import pytest_asyncio
from fastapi import Depends
from sqlalchemy import func, select, update

from app.api.dependencies.patient import get_consent_service
from app.api.dependencies.repositories import (
    get_patient_account_link_repository,
    get_patient_consent_repository,
)
from app.api.dependencies.services import get_audit_sink, get_unit_of_work
from app.core.audit import AuditSink  # noqa: TC001 — FastAPI resolves the override at runtime
from app.core.config import AppEnv, settings
from app.core.security import (
    PATIENT_TOKEN_AUDIENCE,
    PATIENT_TOKEN_TYPE,
    create_access_token,
    create_mfa_ticket,
    create_patient_access_token,
)
from app.database.unit_of_work import UnitOfWork  # noqa: TC001 — as above
from app.models.audit_log import AuditLog
from app.models.hospital import Hospital
from app.models.patient import Patient
from app.models.patient_account import PatientAccount, PatientAccountLink
from app.models.patient_consent import ConsentPurpose
from app.models.user import User
from app.repositories import (  # noqa: TC001 — as above
    PatientAccountLinkRepository,
    PatientConsentRepository,
)
from app.services.patient_app.consent_service import ConsentService
from app.services.patient_app.policies import Policy
from app.tests.conftest import every_log_line_reaches_the_root
from app.tests.patient_app_helpers import (
    PATIENT,
    POLICY,
    FakeSmsSender,
    bearer,
    build_patient_application,
    insert_hospital,
    insert_patient_record,
    new_phone,
    patient_client,
    sign_in,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from fastapi import FastAPI
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

HOSPITALS = f"{PATIENT}/hospitals"
CITIES = f"{PATIENT}/hospital-cities"
ME = f"{PATIENT}/me"
STAFF_HOSPITAL = "/api/v1/hospitals/current"
DOB = "1990-05-17"

# The allow-lists (design, "API"), restated on purpose.
ENVELOPE = {"success", "message", "data", "metadata"}
PAGINATION = {"page", "page_size", "total_records", "total_pages"}
HOSPITAL_FIELDS = {"ref", "name", "address", "phone", "logo_url", "timezone", "linked", "listing"}
ADDRESS_FIELDS = {"line1", "line2", "city", "state", "postal_code", "country"}
LINK_FIELDS = {"hospital_id", "hospital_ref", "hospital_name", "linked_at", "suspended"}

# Documented numbers and names, restated on purpose.
FLAG = "feature.patient_app.enabled"
MAX_PAGE_SIZE = 50
MAX_PAGE = 1000
FILTER_MAX_LENGTH = 80
MAX_CITIES = 200
_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_ID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

#: The one answer for a hospital that cannot be shown.
NOT_FOUND = {
    "success": False,
    "message": "Not Found",
    "errors": None,
    "error_code": "RESOURCE_NOT_FOUND",
}
#: The one answer every patient endpoint gives a caller it does not know.
UNAUTHENTICATED = {
    "success": False,
    "message": "Authentication required.",
    "errors": None,
    "error_code": "AUTHENTICATION_REQUIRED",
}
#: Headers that count requests or time them: different on every response.
_PER_REQUEST_HEADERS = {
    "x-request-id",
    "x-response-time",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}

#: Every way a hospital row can be closed to patients, as ``insert_hospital``
#: arguments. Only a flag stored as exactly ``true`` on an active row is open.
CLOSED: dict[str, dict[str, Any]] = {
    "inactive": {"is_active": False},
    "flag false": {"enabled": False},
    "flag absent": {"enabled": None},
    'flag "true"': {"enabled": "true"},
    "flag 1": {"enabled": 1},
    "flag null": {"enabled": None, "settings": {FLAG: None}},
    "flag [true]": {"enabled": [True]},
    'flag {"enabled": true}': {"enabled": {"enabled": True}},
    "inactive, flag false": {"is_active": False, "enabled": False},
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _attack_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain-HTTP cookies, and the request limiter out of the way.

    A 429 from the per-minute middleware would make "the attacker's request
    failed" pass without the code under test having decided anything.
    """
    monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_HOSPITAL_PER_MIN", 1_000_000)


@pytest.fixture
def sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def tag() -> str:
    """A word no other hospital's name, code or city contains."""
    return f"t{uuid.uuid4().hex[:11]}"


@pytest_asyncio.fixture
async def application(db_session: AsyncSession, sms: FakeSmsSender) -> AsyncGenerator[FastAPI]:
    app = build_patient_application(db_session, sms)
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def browser(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with patient_client(application) as client:
        yield client


class _Patient:
    """A signed-in patient: a phone, a browser and an access token."""

    def __init__(self, client: AsyncClient, phone: str, session: dict[str, Any]) -> None:
        self.client = client
        self.phone = phone
        self.account_id = uuid.UUID(session["account"]["id"])
        self.token: str = session["access_token"]
        self.headers = bearer(self.token)

    async def get(self, url: str, **params: Any) -> Response:
        return await self.client.get(url, params=params or None, headers=self.headers)

    async def discover(self, **params: Any) -> Response:
        return await self.get(HOSPITALS, **params)

    async def hospital(self, hospital_ref: object) -> Response:
        """One hospital, by a reference sent as a single path segment whatever it holds.

        Dots are escaped too: the client would otherwise resolve ``..`` itself
        and the server would never see it.
        """
        segment = quote(str(hospital_ref), safe="").replace(".", "%2E")
        return await self.get(f"{HOSPITALS}/{segment}")

    async def cities(self) -> list[str]:
        response = await self.get(CITIES)
        assert response.status_code == 200, response.text
        assert set(response.json()["data"]) == {"cities"}
        cities: list[str] = response.json()["data"]["cities"]
        return cities

    async def walk(self, page_size: int = MAX_PAGE_SIZE, **filters: Any) -> tuple[list[str], int]:
        """Every reference a filter finds, page by page, and the total each page reported."""
        refs: list[str] = []
        totals: set[int] = set()
        page = 1
        while True:
            response = await self.discover(page=page, page_size=page_size, **filters)
            assert response.status_code == 200, (filters, response.text)
            pagination = response.json()["metadata"]["pagination"]
            assert set(pagination) == PAGINATION
            refs.extend(hospital["ref"] for hospital in response.json()["data"])
            totals.add(pagination["total_records"])
            if page >= pagination["total_pages"]:
                assert len(totals) == 1, f"the total changed between pages: {totals}"
                return refs, totals.pop()
            page += 1

    async def link(self, hospital_ref: object) -> Response:
        return await self.client.post(
            f"{HOSPITALS}/{hospital_ref}/link",
            json={"date_of_birth": DOB, "consent_policy_version": POLICY},
            headers=self.headers,
        )

    async def me(self) -> dict[str, Any]:
        response = await self.get(ME)
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()["data"]
        return data


async def _signed_in(client: AsyncClient, sms: FakeSmsSender) -> _Patient:
    phone = new_phone()
    return _Patient(client, phone, await sign_in(client, sms, phone))


@pytest_asyncio.fixture
async def patient(browser: AsyncClient, sms: FakeSmsSender) -> _Patient:
    return await _signed_in(browser, sms)


@pytest_asyncio.fixture
async def staff(db_session: AsyncSession, hospital_id: uuid.UUID, actor_id: uuid.UUID) -> User:
    """A real staff user of a real hospital. The row is never touched again."""
    user = await db_session.get(User, actor_id)
    assert user is not None
    await db_session.commit()
    return user


# ── Helpers ──────────────────────────────────────────────────────────────────


def _canary() -> str:
    """A value that must never leave the server: distinct, and easy to find in a body."""
    return f"CANARY{secrets.token_hex(10)}"


def _body(response: Response) -> dict[str, Any]:
    """The response envelope without its per-request metadata."""
    payload: dict[str, Any] = response.json()
    payload.pop("metadata", None)
    return payload


def _wire(response: Response) -> tuple[int, bytes, dict[str, str]]:
    """Everything a client can tell two responses apart by, less the per-request noise."""
    request_id = response.headers.get("x-request-id", "")
    content = response.content.replace(request_id.encode(), b"<id>") if request_id else b""
    headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in _PER_REQUEST_HEADERS
    }
    return response.status_code, content, headers


@dataclass(frozen=True)
class _Row:
    """A hospital row, as the oracle reads it."""

    id: uuid.UUID
    name: str
    slug: str
    address: Any
    settings: Any
    is_active: bool

    @property
    def listed(self) -> bool:
        """The listing rule: active, the flag exactly ``true``, and a code a reference can spell."""
        return (
            self.is_active
            and isinstance(self.settings, dict)
            and self.settings.get(FLAG) is True
            and bool(_CODE.fullmatch(self.slug))
            and not _ID_TEXT.fullmatch(self.slug)
        )

    @property
    def city(self) -> str | None:
        stored = self.address.get("city") if isinstance(self.address, dict) else None
        return stored.strip() if isinstance(stored, str) else None


async def _rows(session: AsyncSession) -> list[_Row]:
    """Every hospital row there is, read fresh as columns."""
    result = await session.execute(
        select(
            Hospital.id,
            Hospital.name,
            Hospital.slug,
            Hospital.address,
            Hospital.settings,
            Hospital.is_active,
        )
    )
    return [_Row(*row) for row in result]


async def _directory(
    session: AsyncSession, *, search: str | None = None, city: str | None = None
) -> set[str]:
    """The references a filter must find — the Python side of the comparison."""
    wanted = (search or "").strip().lower()
    where = (city or "").strip().lower()
    return {
        row.slug
        for row in await _rows(session)
        if row.listed
        and (not wanted or wanted in row.name.lower())
        and (not where or (row.city is not None and row.city.lower() == where))
    }


async def _set_settings(session: AsyncSession, hospital: Hospital, stored: Any) -> None:
    await session.execute(
        update(Hospital).where(Hospital.id == hospital.id).values(settings=stored)
    )
    await session.commit()


async def _link_to(session: AsyncSession, patient: _Patient, hospital: Hospital) -> Patient:
    """Give the patient a record at the hospital and link the account to it."""
    record = await insert_patient_record(session, hospital.id, phone=patient.phone)
    response = await patient.link(hospital.slug)
    assert response.status_code == 201, response.text
    return record


async def _audit_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(AuditLog))
    return int(result.scalar_one())


def _signed_by_us(subject: uuid.UUID, **claims: Any) -> str:
    """A token with the application's own signature and whatever claims the attack calls for."""
    extra = {"aud": PATIENT_TOKEN_AUDIENCE, "type": PATIENT_TOKEN_TYPE, **claims}
    return create_access_token(
        subject, None, extra_claims={k: v for k, v in extra.items() if v is not None}
    )


def _forgeries(account_id: uuid.UUID) -> dict[str, str]:
    """Tokens naming a real, active account that the patient API must still refuse."""
    now = datetime.now(UTC)
    past = now - timedelta(seconds=settings.JWT_LEEWAY_SECONDS + 120)
    payload = {
        "sub": str(account_id),
        "iss": settings.JWT_ISSUER,
        "aud": PATIENT_TOKEN_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "type": PATIENT_TOKEN_TYPE,
    }
    genuine = create_patient_access_token(account_id)
    header, body, _ = genuine.split(".")
    unsigned: str = pyjwt.encode(payload, None, algorithm="none")  # type: ignore[arg-type]
    return {
        "signed with another key": pyjwt.encode(
            payload, secrets.token_urlsafe(48), algorithm="HS256"
        ),
        "no signature": f"{header}.{body}.",
        "algorithm none": unsigned,
        "truncated": genuine[:-4],
        "expired": _signed_by_us(account_id, iat=past - timedelta(minutes=15), exp=past),
        "staff access type": _signed_by_us(account_id, type="access"),
        "mfa ticket type": _signed_by_us(account_id, type="mfa_ticket"),
        "refresh type": _signed_by_us(account_id, type="patient_refresh"),
        "another audience": _signed_by_us(account_id, aud="atheris-staff"),
        "no audience": _signed_by_us(account_id, aud=None),
        "another issuer": _signed_by_us(account_id, iss="somebody-else"),
        "an account that does not exist": create_patient_access_token(uuid.uuid4()),
    }


class _LogTrap:
    """Every line the application logs while it is installed.

    The test's own HTTP client logs the URL it asked for; that is the
    attacker's notebook, not the server's log, and is left out.
    """

    def __init__(self) -> None:
        trap = self
        self.lines: list[str] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.name.split(".")[0] in {"httpx", "httpcore"}:
                    return
                trap.lines.append(self.format(record))
                trap.lines.append(repr(record.args))

        self.handler = _Handler(level=0)

    @property
    def everything(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def all_logs(application: FastAPI) -> Iterator[_LogTrap]:
    """Everything logged, at every level. Installed once the application has configured logging."""
    root = logging.getLogger()
    trap = _LogTrap()
    with every_log_line_reaches_the_root():
        root.addHandler(trap.handler)
        try:
            yield trap
        finally:
            root.removeHandler(trap.handler)


def _with_a_required_policy(application: FastAPI) -> None:
    """Switch one platform policy to required — what ``policies.py`` will do one day."""
    required = (Policy(ConsentPurpose.TERMS_OF_SERVICE, "2027-01", "platform", required=True),)

    def _consent(
        consents: PatientConsentRepository = Depends(get_patient_consent_repository),
        links: PatientAccountLinkRepository = Depends(get_patient_account_link_repository),
        uow: UnitOfWork = Depends(get_unit_of_work),
        audit: AuditSink = Depends(get_audit_sink),
    ) -> ConsentService:
        return ConsentService(consents, links, uow, audit, policies=required)

    application.dependency_overrides[get_consent_service] = _consent


# ── A manipulated hospital reference ─────────────────────────────────────────


class TestManipulatedReference:
    @pytest.mark.parametrize(
        "reference",
        [
            # SQL meta-characters.
            "' OR '1'='1",
            "x' OR 1=1 --",
            "'; DROP TABLE hospitals; --",
            '" OR ""="',
            "%",
            "_",
            "h-%",
            "\\",
            "1; SELECT pg_sleep(5)",
            "' UNION SELECT settings FROM hospitals --",
            # Path traversal. Sent percent-encoded, so each reaches the route as one segment.
            ".",
            "..",
            "...",
            "..;",
            "..\\..\\etc\\passwd",
            "%2e%2e",
            "%252e%252e%252fetc",
            # A slash, once decoded, reaches no route at all.
            "../../etc/passwd",
            "x/../../hospitals/current",
            "a/b",
            # Too long, at the limit and far beyond it.
            "a" * 100,
            "a" * 101,
            "a" * 4000,
            "-" * 50,
            "-leading-hyphen",
            # Unicode and control characters.
            "अस्पताल",
            "🏥",
            "ｃｉｔｉｅｓ",
            "‮slatipsoh",
            "​",
            "x\x00",
            "x\x00y",
            "x\r\ny",
            "x\ty",
            # Things that are almost an id.
            "00000000-0000-0000-0000-000000000000",
            "{00000000-0000-0000-0000-000000000000}",
            "urn:uuid:00000000-0000-0000-0000-000000000000",
            "0" * 32,
            "0x0",
            "null",
            "undefined",
            "true",
            "*",
            "current",
            "me",
        ],
    )
    async def test_a_reference_that_names_no_hospital_is_the_one_404(
        self, patient: _Patient, db_session: AsyncSession, tag: str, reference: str
    ) -> None:
        """Attack: put SQL, a path, an over-long or an odd string where the code goes."""
        opened = await insert_hospital(db_session, name=f"{tag} Open")
        unknown = await patient.hospital(f"{tag}-never-existed")

        response = await patient.hospital(reference)

        assert response.status_code == 404, (reference, response.text)
        assert _body(response) == NOT_FOUND
        assert _wire(response) == _wire(unknown)
        assert opened.name not in response.text
        # The table is still there, and so is the hospital.
        assert (await patient.hospital(opened.slug)).status_code == 200

    async def test_another_spelling_of_a_code_never_opens_anything_but_that_hospital(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: reach a closed hospital, or a different one, by respelling a real code.

        A spelling may be read as the code it respells (upper case and padding
        are) or refused. What it may never do is answer with anything else.
        """
        opened = await insert_hospital(db_session, name=f"{tag} Open", slug=f"{tag}-kelvin")
        closed = await insert_hospital(
            db_session, name=f"{tag} Shut", slug=f"{tag}-kshut", enabled=False
        )
        unknown = await patient.hospital(f"{tag}-never-existed")
        canonical = await patient.hospital(opened.slug)
        assert canonical.status_code == 200, canonical.text

        def spellings(code: str, id_: uuid.UUID) -> list[str]:
            return [
                code.upper(),
                code.title(),
                f" {code}",
                f"{code} ",
                f"\t{code}\n",
                f" {code} ",
                f"{code}​",
                f"{code}\x00",
                f"{code}.",
                f"{code}#",
                f"{code}?",
                f"{code};x",
                f"./{code}".replace("/", "\\"),
                # U+212A KELVIN SIGN lower-cases to "k"; full-width letters do not fold.
                code.replace("k", "K"),
                code.replace("k", "ｋ"),
                quote(code, safe=""),
                str(id_).upper(),
                f" {id_} ",
                f"{{{id_}}}",
                f"urn:uuid:{id_}",
                id_.hex,
                str(id_).replace("-", ""),
            ]

        for spelling in spellings(opened.slug, opened.id):
            response = await patient.hospital(spelling)
            if response.status_code == 200:
                assert response.json()["data"] == canonical.json()["data"], repr(spelling)
            else:
                assert _wire(response) == _wire(unknown), repr(spelling)
        for spelling in spellings(closed.slug, closed.id):
            response = await patient.hospital(spelling)
            assert _wire(response) == _wire(unknown), repr(spelling)

    async def test_the_id_of_something_that_is_not_a_hospital_finds_nothing(
        self, patient: _Patient, db_session: AsyncSession, staff: User, tag: str
    ) -> None:
        """Attack: pass a patient, account, link or staff id where a hospital id is accepted."""
        hospital = await insert_hospital(db_session, name=f"{tag} Mine")
        record = await _link_to(db_session, patient, hospital)
        link_id = (
            await db_session.execute(
                select(PatientAccountLink.id).where(
                    PatientAccountLink.account_id == patient.account_id
                )
            )
        ).scalar_one()
        unknown = await patient.hospital(f"{tag}-never-existed")

        for what, other_id in {
            "patient record": record.id,
            "own account": patient.account_id,
            "record link": link_id,
            "staff user": staff.id,
        }.items():
            response = await patient.hospital(other_id)
            assert _wire(response) == _wire(unknown), what

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    async def test_discovery_cannot_be_written_to(
        self, patient: _Patient, db_session: AsyncSession, tag: str, method: str
    ) -> None:
        """Attack: change, or remove, a hospital through the routes that describe it."""
        hospital = await insert_hospital(db_session, name=f"{tag} Open")
        before = await _rows(db_session)

        for url in (
            HOSPITALS,
            f"{HOSPITALS}/{hospital.slug}",
            f"{HOSPITALS}/{hospital.id}",
            CITIES,
        ):
            response = await patient.client.request(
                method,
                url,
                json={"name": "pwned", "is_active": False, "settings": {FLAG: False}},
                headers=patient.headers,
            )
            assert response.status_code == 405, (method, url, response.text)

        assert await _rows(db_session) == before


# ── A hospital that is closed to patients ────────────────────────────────────


class TestAClosedHospitalIsNowhere:
    @pytest.mark.parametrize("state", [*CLOSED, "settings is a list"])
    async def test_it_is_the_same_404_as_a_hospital_that_never_existed(
        self, patient: _Patient, db_session: AsyncSession, tag: str, state: str
    ) -> None:
        """Attack: learn that a hospital exists, or why it is closed, from how it is refused."""
        closed = await insert_hospital(
            db_session, name=f"{tag} Shut", address={"city": f"{tag}ville"}, **CLOSED.get(state, {})
        )
        if state == "settings is a list":
            await _set_settings(db_session, closed, [{FLAG: True}, FLAG, True])
        unknown_code = await patient.hospital(f"{tag}-never-existed")
        unknown_id = await patient.hospital(uuid.uuid4())

        by_code = await patient.hospital(closed.slug)
        by_id = await patient.hospital(closed.id)

        assert by_code.status_code == 404, by_code.text
        assert _body(by_code) == NOT_FOUND
        assert _wire(by_code) == _wire(by_id) == _wire(unknown_code) == _wire(unknown_id)

    @pytest.mark.parametrize("state", [*CLOSED, "settings is a list"])
    async def test_it_is_in_no_list_no_total_and_no_city_option(
        self, patient: _Patient, db_session: AsyncSession, tag: str, state: str
    ) -> None:
        """Attack: find a closed hospital, or count it, through the list or the city filter."""
        opened = await insert_hospital(db_session, name=f"{tag} Open", address={"city": "Pune"})
        closed = await insert_hospital(
            db_session, name=f"{tag} Shut", address={"city": f"{tag}ville"}, **CLOSED.get(state, {})
        )
        if state == "settings is a list":
            await _set_settings(db_session, closed, [{FLAG: True}, FLAG, True])

        assert await patient.walk(search=tag) == ([opened.slug], 1)
        assert await patient.walk(search=closed.name) == ([], 0)
        assert await patient.walk(city=f"{tag}ville") == ([], 0)
        everything, total = await patient.walk()
        assert closed.slug not in everything
        assert set(everything) == await _directory(db_session)
        assert total == len(everything)
        assert not [city for city in await patient.cities() if tag in city.lower()]

    @pytest.mark.parametrize(
        "how", ["deactivated", "flag false", 'flag "true"', "flag 1", "flag null", "flag removed"]
    )
    async def test_closing_takes_effect_on_the_very_next_request(
        self, patient: _Patient, db_session: AsyncSession, tag: str, how: str
    ) -> None:
        """Attack: keep reading a hospital, from a cache or a stale list, after it was closed."""
        staying = await insert_hospital(db_session, name=f"{tag} Stays", address={"city": "Pune"})
        closing = await insert_hospital(
            db_session,
            name=f"{tag} Closes",
            address={"city": f"{tag}ville"},
            settings={"visiting_hours": "9-5"},
        )
        await _link_to(db_session, patient, closing)
        unknown = await patient.hospital(f"{tag}-never-existed")
        assert await patient.walk(search=tag) == ([closing.slug, staying.slug], 2)
        assert (await patient.hospital(closing.slug)).json()["data"]["linked"] is True
        assert (await patient.hospital(closing.id)).status_code == 200
        assert f"{tag}ville" in await patient.cities()

        if how == "deactivated":
            await db_session.execute(
                update(Hospital).where(Hospital.id == closing.id).values(is_active=False)
            )
            await db_session.commit()
        else:
            stored: dict[str, Any] = {"visiting_hours": "9-5"}
            if how != "flag removed":
                stored[FLAG] = {"flag false": False, 'flag "true"': "true", "flag 1": 1}.get(how)
            await _set_settings(db_session, closing, stored)

        # Even for the patient whose own record is there.
        assert await patient.walk(search=tag) == ([staying.slug], 1)
        assert await patient.walk(city=f"{tag}ville") == ([], 0)
        assert _wire(await patient.hospital(closing.slug)) == _wire(unknown)
        assert _wire(await patient.hospital(closing.id)) == _wire(unknown)
        assert f"{tag}ville" not in await patient.cities()
        assert closing.slug not in (await patient.walk())[0]

    async def test_reopening_is_the_only_way_back(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """The control for the test above: it is the flag that hides the hospital."""
        hospital = await insert_hospital(db_session, name=f"{tag} Shut", enabled="true")
        assert await patient.walk(search=tag) == ([], 0)

        await _set_settings(db_session, hospital, {FLAG: True})

        assert await patient.walk(search=tag) == ([hospital.slug], 1)
        assert (await patient.hospital(hospital.slug)).status_code == 200

    async def test_a_hospital_whose_code_no_reference_can_spell_is_not_counted(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: none — bad data. An active, enabled row with a code like 'Upper-Case'.

        It is rightly not listed (its reference would lead nowhere). The total
        must then not count it either: "3 hospitals" over two cards, and an
        empty page in the middle of a walk, are what the patient would see.
        """
        listed = await insert_hospital(db_session, name=f"{tag} Listed")
        unspellable = await insert_hospital(db_session, name=f"{tag} Odd", slug=f"{tag}-Upper")
        # The half that already holds: it is in no page and has no detail.
        assert (await patient.hospital(unspellable.slug)).status_code == 404
        assert (await patient.hospital(unspellable.id)).status_code == 404

        refs, total = await patient.walk(search=tag)

        assert refs == [listed.slug]
        assert total == 1

    async def test_the_city_of_a_hospital_whose_code_no_reference_can_spell_is_not_an_option(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """The cities must be those of LISTED hospitals: this one is in no list."""
        await insert_hospital(
            db_session, name=f"{tag} Odd", slug=f"{tag}-Upper", address={"city": f"{tag}ville"}
        )
        assert f"{tag}ville" not in await patient.cities()


# ── The shape of every answer ────────────────────────────────────────────────


class TestOnlyTheAllowList:
    async def test_every_answer_has_exactly_the_documented_keys(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: read a field nobody meant to publish, off any of the four answers."""
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} General",
            address={"line1": "1 Road", "city": "Pune", "zip": "411001", "geo": {"lat": 18.5}},
            phone="020 4000 1234",
            logo_url="https://cdn.example.com/logo.png",
            email="office@general.test",
            tax_id="27AAAPL1234C1ZV",
        )
        await _link_to(db_session, patient, hospital)

        listed = await patient.discover(search=tag)
        by_code = await patient.hospital(hospital.slug)
        by_id = await patient.hospital(hospital.id)
        cities = await patient.get(CITIES)
        me = await patient.me()

        assert set(listed.json()) == ENVELOPE
        assert set(listed.json()["metadata"]) == {"request_id", "pagination"}
        assert set(listed.json()["metadata"]["pagination"]) == PAGINATION
        [item] = listed.json()["data"]
        for described in (item, by_code.json()["data"], by_id.json()["data"]):
            assert set(described) == HOSPITAL_FIELDS
            assert set(described["address"]) == ADDRESS_FIELDS
            assert all(v is None or isinstance(v, str) for v in described["address"].values())
            assert described["listing"] == "standard"
            assert described["ref"] == hospital.slug
        assert by_code.json()["data"] == by_id.json()["data"] == item
        for one in (by_code, by_id, cities):
            assert set(one.json()) == ENVELOPE
            assert set(one.json()["metadata"]) == {"request_id"}
        assert set(cities.json()["data"]) == {"cities"}
        assert all(isinstance(city, str) for city in cities.json()["data"]["cities"])
        [link] = me["links"]
        assert set(link) == LINK_FIELDS
        assert link["hospital_ref"] == hospital.slug
        # The id is in /me, as it was in Task 28 — and in no discovery answer.
        for response in (listed, by_code, by_id, cities):
            assert str(hospital.id) not in response.text

    async def test_nothing_planted_in_a_hospital_reaches_any_answer_under_any_query(
        self,
        patient: _Patient,
        db_session: AsyncSession,
        application: FastAPI,
        tag: str,
        sms: FakeSmsSender,
    ) -> None:
        """Attack: read settings, tax id, e-mail, stray address keys, staff or patients.

        A canary is planted in every place discovery must not read from, at an
        open hospital and at a closed one in the same city. No body and no
        header of any of the three endpoints may carry one, whatever is asked.
        """
        canaries = {name: _canary() for name in (
            "settings value", "settings nested", "settings list", "settings key", "tax id",
            "email", "address extra", "address nested", "address in line2", "address in state",
            "address key", "staff email", "staff first name", "staff last name", "staff hash",
            "patient first name", "patient last name", "patient mrn", "closed name", "closed city",
            "closed phone", "closed logo",
        )}  # fmt: skip
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} Target",
            address={
                "line1": "1 Road",
                "city": f"{tag}city",
                "gstin": canaries["address extra"],
                "geo": {"lat": canaries["address nested"]},
                canaries["address key"]: "x",
                # Allow-listed keys holding something that is not text.
                "line2": {"note": canaries["address in line2"]},
                "state": [canaries["address in state"]],
                "postal_code": 411001,
            },
            settings={
                "smtp_password": canaries["settings value"],
                "integrations": {"lab": {"api_key": canaries["settings nested"]}},
                "webhooks": [canaries["settings list"]],
                canaries["settings key"]: True,
            },
            tax_id=canaries["tax id"],
            email=f"{canaries['email']}@hospital.test",
        )
        closed = await insert_hospital(
            db_session,
            name=canaries["closed name"],
            address={"city": canaries["closed city"]},
            phone=canaries["closed phone"][:20],
            logo_url=f"https://cdn.example.com/{canaries['closed logo']}.png",
            enabled=False,
        )
        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital.id,
            email=f"{canaries['staff email']}@hospital.test".lower(),
            password_hash=canaries["staff hash"],
            first_name=canaries["staff first name"],
            last_name=canaries["staff last name"],
        )
        db_session.add(user)
        await db_session.commit()
        record = await insert_patient_record(
            db_session,
            hospital.id,
            phone=new_phone(),
            first_name=canaries["patient first name"],
            last_name=canaries["patient last name"],
            mrn=canaries["patient mrn"],
        )
        # Somebody else is linked here; the caller is not.
        async with patient_client(application) as elsewhere:
            other = await _signed_in(elsewhere, sms)
            other_record = await _link_to(db_session, other, hospital)
        forbidden = {
            **canaries,
            "staff email": canaries["staff email"].lower(),
            "closed phone": canaries["closed phone"][:20],
            "hospital id": str(hospital.id),
            "closed hospital id": str(closed.id),
            "closed hospital code": closed.slug,
            "staff id": str(user.id),
            "patient id": str(record.id),
            "other patient id": str(other_record.id),
            "other account id": str(other.account_id),
            "other phone": other.phone,
            "flag": FLAG,
        }

        queries: list[dict[str, Any]] = [
            {},
            {"search": tag},
            {"city": f"{tag}city"},
            {"search": tag, "city": f"{tag}city", "page_size": 1},
            {"page": 2, "page_size": 1},
            {"page": MAX_PAGE, "page_size": MAX_PAGE_SIZE},
            {"search": tag, "fields": "id,settings,tax_id,email", "expand": "users,patients"},
            {"search": tag, "include": "settings", "include_inactive": "true", "debug": "1"},
            *({"search": canary} for canary in canaries.values()),
            *({"city": canary} for canary in canaries.values()),
        ]
        responses = [await patient.discover(**query) for query in queries]
        # A search for a hidden value finds nothing: it cannot be confirmed by its hit count.
        for response in responses[8:]:
            assert response.json()["metadata"]["pagination"]["total_records"] == 0
        responses += [
            await patient.hospital(hospital.slug),
            await patient.hospital(hospital.id),
            await patient.hospital(closed.slug),
            await patient.hospital(closed.id),
            await patient.get(f"{HOSPITALS}/{hospital.slug}", fields="settings", expand="users"),
            await patient.get(CITIES),
            await patient.get(CITIES, include_inactive="1", city=canaries["closed city"]),
            *[await patient.hospital(canary) for canary in canaries.values()],
        ]

        described = (await patient.hospital(hospital.slug)).json()["data"]
        assert described["address"] == {
            "line1": "1 Road",
            "line2": None,
            "city": f"{tag}city",
            "state": None,
            "postal_code": None,
            "country": None,
        }
        assert described["linked"] is False
        for response in responses:
            assert response.status_code in {200, 404}, response.text
            held = response.text + "\n".join(f"{k}: {v}" for k, v in response.headers.multi_items())
            for what, value in forbidden.items():
                assert value not in held, (what, str(response.request.url))

    async def test_a_logo_a_browser_must_not_be_handed_is_no_logo(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: have the app put a script, an inline document or a local path in ``<img src>``."""
        unsafe = [
            "javascript:alert(document.cookie)",
            "JaVaScRiPt:alert(1)",
            " javascript:alert(1)",
            "java\tscript:alert(1)",
            "data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "blob:https://cdn.example.com/5b1c",
            "file:///etc/passwd",
            "ftp://cdn.example.com/logo.png",
            "/static/logo.png",
            "logo.png",
            "../../api/v1/patient/auth/logout",
            "//evil.example/logo.png",
            "\\\\evil.example\\logo.png",
            "https:evil.example/logo.png",
            "https:/evil.example/logo.png",
            "https://",
            "https:///logo.png",
            "https://user:password@cdn.example.com/logo.png",
            "https://cdn.example.com\\@evil.example/logo.png",
            "https://cdn.example.com/a b.png",
            "https://cdn.example.com/logo.png\njavascript:alert(1)",
            "https://cdn.example.com/‮gnp.exe",
            "https://[::1/logo.png",
            "https://cdn.example.com/" + "a" * 3000,
            "",
            "   ",
        ]
        safe = "https://cdn.example.com/logos/ok.png"
        for index, logo_url in enumerate([*unsafe, safe]):
            await insert_hospital(db_session, name=f"{tag} {index:02d}", logo_url=logo_url)

        response = await patient.discover(search=tag, page_size=MAX_PAGE_SIZE)

        logos = [hospital["logo_url"] for hospital in response.json()["data"]]
        assert logos == [None] * len(unsafe) + [safe]
        for hospital in response.json()["data"][:-1]:
            detail = await patient.hospital(hospital["ref"])
            assert detail.json()["data"]["logo_url"] is None, hospital["name"]

    async def test_a_plain_http_logo_is_no_logo_outside_development(
        self,
        patient: _Patient,
        db_session: AsyncSession,
        tag: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: have a production app load a logo an on-path attacker can replace."""
        hospital = await insert_hospital(
            db_session, name=f"{tag} Plain", logo_url="http://cdn.example.com/logo.png"
        )
        in_development = await patient.hospital(hospital.slug)
        assert in_development.json()["data"]["logo_url"] == "http://cdn.example.com/logo.png"

        for environment in (AppEnv.PRODUCTION, AppEnv.STAGING):
            monkeypatch.setattr(settings, "APP_ENV", environment)
            listed = await patient.discover(search=tag)
            detail = await patient.hospital(hospital.slug)
            assert listed.json()["data"][0]["logo_url"] is None, environment
            assert detail.json()["data"]["logo_url"] is None, environment

    async def test_no_hospital_is_ever_labelled_promoted_or_moved_up(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: buy a place at the top by writing 'promoted' wherever a row can hold it."""
        plain = await insert_hospital(db_session, name=f"{tag} Aardvark")
        pushy = await insert_hospital(
            db_session,
            name=f"{tag} Zebra",
            address={"city": "Pune", "listing": "promoted", "promoted": True},
            settings={"listing": "promoted", "promoted": True, "feature.promoted": True},
        )

        for params in ({}, {"listing": "promoted"}, {"sort": "listing"}, {"promoted": "true"}):
            response = await patient.discover(search=tag, **params)
            found = response.json()["data"]
            assert [h["ref"] for h in found] == [plain.slug, pushy.slug], params
            assert [h["listing"] for h in found] == ["standard", "standard"], params
        assert (await patient.hospital(pushy.slug)).json()["data"]["listing"] == "standard"


# ── Search and the city filter ───────────────────────────────────────────────


#: What an attacker types into the search box.
HOSTILE_TEXT = [
    "%",
    "%%",
    "_",
    "__",
    "%_%",
    "\\",
    "\\\\",
    "\\%",
    "\\_",
    "%\\",
    "'",
    "''",
    '"',
    "`",
    "' OR '1'='1",
    "' OR 1=1 --",
    "%' OR '%'='",
    "'; DROP TABLE hospitals; --",
    "\\' OR 1=1 --",
    "') UNION SELECT name FROM hospitals --",
    "E'\\x41'",
    "$$",
    "$1",
    ":name",
    "%s",
    "%(name)s",
    "{0}",
    "||",
    "/*",
    "--",
    ";",
    "[",
    "]",
    "[a-z]",
    ".*",
    "^",
    "$",
    "?",
    "*",
    "(",
    "|",
    "%00",
    "​",
    "﻿",
    "अस्पताल",
    "🏥",
    "<script>alert(1)</script>",
    "100%",
    "100% c",
    "under_",
    "back\\",
    "o'b",
    "%" * FILTER_MAX_LENGTH,
    "_" * FILTER_MAX_LENGTH,
    "\\" * FILTER_MAX_LENGTH,
    "'" * FILTER_MAX_LENGTH,
]


class TestSearchIsLiteral:
    @pytest_asyncio.fixture
    async def names(self, db_session: AsyncSession, tag: str) -> list[str]:
        """Hospitals whose names hold every character a pattern treats specially."""
        names = [
            f"{tag} 100% Care",
            f"{tag} under_score",
            f"{tag} back\\slash",
            f"{tag} O'Brien Memorial",
            f'{tag} "Quoted" Clinic',
            f"{tag} semi; colon -- dash",
            f"{tag} [bracket] (paren) $ ^ * ? . |",
            f"{tag} अस्पताल",
            f"{tag} 🏥 Emoji",
            f"{tag} Plain General",
            # Exactly as long as the longest search: `_` × 80 would match it as a wildcard.
            f"{tag}{'x' * (FILTER_MAX_LENGTH - len(tag))}",
        ]
        for name in names:
            await insert_hospital(db_session, name=name, address={"city": f"{tag}pur"})
        await insert_hospital(db_session, name=f"{tag} Shut % _ \\ '", enabled=False)
        return names

    async def test_what_is_typed_finds_exactly_the_names_that_contain_it(
        self, patient: _Patient, db_session: AsyncSession, tag: str, names: list[str]
    ) -> None:
        """Attack: a wildcard, a quote or an SQL fragment that lists more than it names.

        For every text, alone and after the test's own tag, the answer is the
        Python-side literal, case-insensitive substring check — over the whole
        directory.
        """
        everything = await _directory(db_session)
        assert len(everything) == len(names)

        for typed in [*HOSTILE_TEXT, *(f"{tag}{text}" for text in HOSTILE_TEXT[:12])]:
            refs, total = await patient.walk(search=typed)
            assert len(refs) == len(set(refs)) == total, repr(typed)
            assert set(refs) == await _directory(db_session, search=typed), repr(typed)

        # The dump an unescaped wildcard would have been.
        for wildcard in ("%", "_", "%%", "%_%", "_" * FILTER_MAX_LENGTH, f"{tag}%", f"{tag}_"):
            refs, _ = await patient.walk(search=wildcard)
            assert len(refs) <= 1, repr(wildcard)
        # Controls: each special character is found where it really is, whatever the case.
        for typed, name in {
            "100% CARE": names[0],
            "UNDER_SCORE": names[1],
            "back\\slash": names[2],
            "o'brien": names[3],
            '"quoted"': names[4],
            "; colon --": names[5],
            "$ ^ * ? . |": names[6],
            "अस्पताल": names[7],
            "🏥": names[8],
        }.items():
            response = await patient.discover(search=typed)
            assert [h["name"] for h in response.json()["data"]] == [name], typed

    async def test_search_reads_the_name_and_nothing_else(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: confirm a hidden value — or find by city, phone or code — one guess at a time."""
        hidden = _canary()
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} General",
            slug=f"{tag}-code",
            address={"line1": f"{tag}street", "city": f"{tag}pur", "state": f"{tag}state"},
            phone="04040001234",
            email=f"{hidden}@general.test",
            tax_id=hidden,
            settings={"note": hidden},
            timezone="Asia/Kolkata",
        )

        for typed in (
            hidden,
            hidden[:8],
            f"{tag}-code",
            f"{tag}street",
            f"{tag}pur",
            f"{tag}state",
            "04040001234",
            str(hospital.id),
            str(hospital.id)[:8],
            FLAG,
            "feature.",
            "general.test",
        ):
            assert await patient.walk(search=typed) == ([], 0), typed
        assert await patient.walk(search=f"{tag} gen") == ([hospital.slug], 1)

    async def test_the_city_is_matched_whole_and_literally(
        self, patient: _Patient, db_session: AsyncSession, tag: str, names: list[str]
    ) -> None:
        """Attack: a wildcard, a prefix or an SQL fragment in the city filter."""
        literal = await insert_hospital(db_session, name=f"{tag} Odd", address={"city": "%"})
        await insert_hospital(db_session, name=f"{tag} Shut", address={"city": f"{tag}pur"},
                              is_active=False)  # fmt: skip
        city = f"{tag}pur"

        for typed in [
            *HOSTILE_TEXT,
            city[:-1],
            city[1:],
            f"{city}%",
            f"{city[:-1]}_",
            f"%{city[1:]}",
            "_" * len(city),
            f"{city}' OR '1'='1",
            f"{city}\\",
        ]:
            refs, total = await patient.walk(city=typed)
            assert len(refs) == len(set(refs)) == total, repr(typed)
            assert set(refs) == await _directory(db_session, city=typed), repr(typed)

        assert await patient.walk(city="%") == ([literal.slug], 1)
        # Controls: the whole city, in any case and padded, finds its open hospitals only.
        for typed in (city, city.upper(), f"  {city}\t"):
            refs, total = await patient.walk(city=typed)
            assert total == len(names), typed
        both, _ = await patient.walk(city=city, search="100%")
        assert both == [next(r.slug for r in await _rows(db_session) if r.name == names[0])]

    @pytest.mark.parametrize("parameter", ["search", "city"])
    async def test_eighty_characters_are_read_and_eighty_one_are_refused(
        self, patient: _Patient, db_session: AsyncSession, tag: str, parameter: str
    ) -> None:
        """Attack: an over-long filter — a cheap way to make the database work, or to break it."""
        await insert_hospital(db_session, name=f"{tag} Open")

        for filler in ("a", "%", "_", "\\", "'", "é", "अ", "🏥"):
            at_the_limit = await patient.discover(**{parameter: filler * FILTER_MAX_LENGTH})
            padded = await patient.discover(**{parameter: f"  {filler * FILTER_MAX_LENGTH}\n"})
            over = await patient.discover(**{parameter: filler * (FILTER_MAX_LENGTH + 1)})
            far_over = await patient.discover(**{parameter: filler * 5000})

            for response in (at_the_limit, padded):
                assert response.status_code == 200, (filler, response.text)
                assert response.json()["data"] == []
                assert response.json()["metadata"]["pagination"]["total_records"] == 0
            for response in (over, far_over):
                assert response.status_code == 422, (filler, response.text)
                assert response.json()["error_code"] == "VALIDATION_ERROR"
                assert "data" not in response.json()
                assert tag not in response.text

    @pytest.mark.parametrize(
        "query",
        [
            "page=0",
            "page=-1",
            "page=1001",
            "page=1e2",
            "page=1.5",
            "page=0x1",
            "page=1%00",
            "page=",
            "page=%20",
            "page=one",
            "page=null",
            "page=true",
            "page=NaN",
            "page=Infinity",
            "page=%D9%A1",
            "page=" + "9" * 400,
            "page=-" + "9" * 400,
            "page=1&page=2",
            "page=1&page=0",
            "page[]=1",
            "page[$gt]=0",
            "page_size=0",
            "page_size=-1",
            "page_size=51",
            "page_size=1000000",
            "page_size=all",
            "page_size=",
            "page_size=50&page_size=1000000",
            "page_size=" + "9" * 400,
            "page=1000&page_size=50",
            "page=1000&page_size=1",
            "limit=1000000&offset=-1&skip=-1",
            "search=" + "a" * 81,
            "city=" + "a" * 81,
            "search=" + "a" * 20000,
            "search=%00",
            "search=a%00b",
            "city=%00",
            "city=a%00",
            "search=%FF%FE",
            "search=%ED%A0%80",
            "search=%C0%AF",
            "search=%",
            "search=%2",
            "search=%25%25",
            "search=a&search=b",
            "search[]=a",
            "search[$ne]=",
            "search=a;city=b",
            "city=a&city=b",
            "%00=1",
            "=",
            "&&&",
            "search",
            "search=&city=&page=1&page_size=20",
        ],
    )
    async def test_an_odd_query_is_answered_or_refused_and_never_breaks_the_server(
        self, patient: _Patient, db_session: AsyncSession, tag: str, query: str
    ) -> None:
        """Attack: a parameter the server did not expect — a 500, or a page bigger than allowed."""
        for index in range(3):
            await insert_hospital(db_session, name=f"{tag} {index}")
        await insert_hospital(db_session, name=f"{tag} Shut", enabled=False)

        response = await patient.client.get(f"{HOSPITALS}?{query}", headers=patient.headers)

        assert response.status_code in {200, 422}, (query, response.status_code, response.text)
        if response.status_code == 422:
            assert response.json()["error_code"] == "VALIDATION_ERROR"
            assert "data" not in response.json()
            assert tag not in response.text
        else:
            found = [hospital["ref"] for hospital in response.json()["data"]]
            pagination = response.json()["metadata"]["pagination"]
            assert 1 <= pagination["page"] <= MAX_PAGE
            assert 1 <= pagination["page_size"] <= MAX_PAGE_SIZE
            assert len(found) <= pagination["page_size"]
            assert set(found) <= await _directory(db_session)

    async def test_a_parameter_nobody_defined_changes_no_answer(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: ask for the closed hospitals, more fields or another order by naming a switch."""
        opened = await insert_hospital(db_session, name=f"{tag} Open", address={"city": f"{tag}a"})
        closed = await insert_hospital(
            db_session, name=f"{tag} Shut", address={"city": f"{tag}b"}, is_active=False
        )
        unknown = await patient.hospital(f"{tag}-never-existed")
        honest_list = await patient.discover(search=tag)
        honest_detail = await patient.hospital(opened.slug)
        honest_cities = await patient.cities()

        for extra in (
            {"is_active": "false"},
            {"active": "0"},
            {"include_inactive": "1"},
            {"include_disabled": "true"},
            {"status": "all"},
            {"enabled": "false"},
            {FLAG: "false"},
            {"hospital_id": str(closed.id)},
            {"id": str(closed.id)},
            {"ref": closed.slug},
            {"slug": closed.slug},
            {"sort": "-name"},
            {"order_by": "created_at desc"},
            {"fields": "*"},
            {"expand": "settings,users"},
            {"account_id": str(uuid.uuid4())},
            {"patient_id": str(uuid.uuid4())},
            {"linked": "true"},
            {"listing": "promoted"},
            {"_method": "DELETE"},
            {"format": "csv"},
            {"limit": "1000", "offset": "0"},
        ):
            listed = await patient.discover(search=tag, **extra)
            assert listed.status_code == 200, extra
            assert listed.json()["data"] == honest_list.json()["data"], extra
            assert listed.json()["metadata"] == honest_list.json()["metadata"], extra
            detail = await patient.get(f"{HOSPITALS}/{opened.slug}", **extra)
            assert detail.json()["data"] == honest_detail.json()["data"], extra
            refused = await patient.get(f"{HOSPITALS}/{closed.slug}", **extra)
            assert _wire(refused) == _wire(unknown), extra
            cities = await patient.get(CITIES, **extra)
            assert cities.json()["data"]["cities"] == honest_cities, extra
        assert [h["ref"] for h in honest_list.json()["data"]] == [opened.slug]
        assert f"{tag}b" not in honest_cities


# ── Pagination ───────────────────────────────────────────────────────────────


class TestPagination:
    async def test_walking_every_page_meets_each_hospital_once_in_one_order(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: none — a list that repeats or loses a hospital hides it from a patient.

        Twenty-three hospitals, most of them sharing a name but for its case,
        inserted in a shuffled order: only the code can break the ties.
        """
        names = (
            [f"{tag} Tie"] * 9
            + [f"{tag} TIE"] * 4
            + [f"{tag} tie"] * 4
            + [f"{tag} alpha", f"{tag} Bravo", f"{tag} CHARLIE", f"{tag} delta", f"{tag} Echo"]
            + [f"{tag} zulu"]
        )
        planned = list(names)
        random.Random(29).shuffle(planned)
        for position, name in enumerate(planned):
            # Codes run against the insertion order, so neither it nor the id explains the result.
            await insert_hospital(
                db_session, name=name, slug=f"{tag}-{len(planned) - position:03d}"
            )
        await insert_hospital(db_session, name=f"{tag} Tie", enabled=False)
        await insert_hospital(db_session, name=f"{tag} Tie", is_active=False)
        rows = [row for row in await _rows(db_session) if row.listed and tag in row.name]
        expected = [row.slug for row in sorted(rows, key=lambda row: (row.name.lower(), row.slug))]
        assert len(expected) == len(names)

        for page_size in (1, 2, 3, 5, 7, 22, 23, 24, MAX_PAGE_SIZE):
            refs, total = await patient.walk(page_size, search=tag)
            assert refs == expected, page_size
            assert total == len(names), page_size
        # The same page asked twice is the same page.
        for page in (1, 2, 3, 4):
            first = await patient.discover(search=tag, page=page, page_size=6)
            again = await patient.discover(search=tag, page=page, page_size=6)
            assert first.json()["data"] == again.json()["data"]
            assert [h["ref"] for h in first.json()["data"]] == expected[(page - 1) * 6 : page * 6]
            assert first.json()["metadata"]["pagination"] == {
                "page": page,
                "page_size": 6,
                "total_records": len(names),
                "total_pages": math.ceil(len(names) / 6),
            }

    async def test_the_whole_directory_is_every_open_hospital_once(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """No filter at all: the walk and the Python-side listing rule agree, row for row."""
        for index in range(7):
            await insert_hospital(db_session, name=f"{tag} {index % 3}")
        for state in CLOSED.values():
            await insert_hospital(db_session, name=f"{tag} Shut", **state)

        for page_size in (1, 3, MAX_PAGE_SIZE):
            refs, total = await patient.walk(page_size)
            assert len(refs) == len(set(refs)) == total, page_size
            assert set(refs) == await _directory(db_session), page_size

    async def test_a_page_beyond_the_end_is_empty_and_still_tells_the_truth(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: read past the end of the list — into nothing, not into other rows."""
        for index in range(3):
            await insert_hospital(db_session, name=f"{tag} {index}")
        await insert_hospital(db_session, name=f"{tag} Shut", enabled=False)

        for page, page_size in ((2, 3), (2, MAX_PAGE_SIZE), (4, 1), (MAX_PAGE, MAX_PAGE_SIZE)):
            response = await patient.discover(search=tag, page=page, page_size=page_size)
            assert response.status_code == 200, response.text
            assert response.json()["data"] == []
            assert response.json()["metadata"]["pagination"] == {
                "page": page,
                "page_size": page_size,
                "total_records": 3,
                "total_pages": math.ceil(3 / page_size),
            }


# ── Whose token it is ────────────────────────────────────────────────────────


class TestTokens:
    async def test_no_credential_but_a_patients_own_opens_discovery(
        self,
        browser: AsyncClient,
        patient: _Patient,
        staff: User,
        db_session: AsyncSession,
        tag: str,
    ) -> None:
        """Attack: read the directory with a staff token, an MFA ticket or a forged token.

        Every one is answered exactly as a caller with no token at all is.
        """
        hospital = await insert_hospital(db_session, name=f"{tag} Open", address={"city": tag})
        urls = [
            HOSPITALS,
            f"{HOSPITALS}?search={tag}",
            f"{HOSPITALS}/{hospital.slug}",
            f"{HOSPITALS}/{hospital.id}",
            f"{HOSPITALS}/{tag}-never-existed",
            CITIES,
        ]
        credentials = {
            "staff access token": bearer(create_access_token(staff.id, staff.hospital_id)),
            "staff token for that hospital": bearer(create_access_token(staff.id, hospital.id)),
            "platform administrator's token": bearer(create_access_token(staff.id, None)),
            "staff token in a patient's name": bearer(
                create_access_token(patient.account_id, hospital.id)
            ),
            "mfa ticket": bearer(create_mfa_ticket(staff.id)),
            "mfa ticket in a patient's name": bearer(create_mfa_ticket(patient.account_id)),
            **{kind: bearer(token) for kind, token in _forgeries(patient.account_id).items()},
            "empty bearer": {"Authorization": "Bearer "},
            "no scheme": {"Authorization": patient.token},
            "basic scheme": {"Authorization": f"Basic {patient.token}"},
            "token in a cookie": {"Cookie": f"access_token={patient.token}"},
            "token in a custom header": {"X-Access-Token": patient.token},
        }

        for url in urls:
            anonymous = await browser.get(url)
            assert anonymous.status_code == 401, (url, anonymous.text)
            assert _body(anonymous) == UNAUTHENTICATED
            for kind, headers in credentials.items():
                response = await browser.get(url, headers=headers)
                assert response.status_code == 401, (kind, url, response.text)
                assert _wire(response) == _wire(anonymous), (kind, url)
                assert tag not in response.text
            in_the_query = await browser.get(url, params={"access_token": patient.token})
            assert _wire(in_the_query) == _wire(anonymous), url
        # The control: it is the credential that is refused, not the request.
        for url in (urls[0], urls[2], urls[5]):
            assert (await browser.get(url, headers=patient.headers)).status_code == 200

    @pytest.mark.parametrize("status", ["suspended", "closed"])
    async def test_a_genuine_token_for_an_account_that_is_not_active_opens_nothing(
        self, browser: AsyncClient, patient: _Patient, db_session: AsyncSession, tag: str,
        status: str,
    ) -> None:  # fmt: skip
        """Attack: keep browsing with a token issued before the account was suspended or closed."""
        hospital = await insert_hospital(db_session, name=f"{tag} Open", address={"city": tag})
        urls = [HOSPITALS, f"{HOSPITALS}/{hospital.slug}", f"{HOSPITALS}/{tag}-nowhere", CITIES]
        for url in urls[:2]:
            assert (await browser.get(url, headers=patient.headers)).status_code == 200

        await db_session.execute(
            update(PatientAccount)
            .where(PatientAccount.id == patient.account_id)
            .values(status=status)
        )
        await db_session.commit()

        for url in urls:
            anonymous = await patient_anonymous(browser, url)
            for token in (patient.token, create_patient_access_token(patient.account_id)):
                response = await browser.get(url, headers=bearer(token))
                assert response.status_code == 401, (url, response.text)
                assert _wire(response) == _wire(anonymous), url
                assert tag not in response.text

    async def test_a_patient_token_is_nobody_to_the_staff_hospital_endpoints(
        self, patient: _Patient, staff: User, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: the other direction — read or change hospital settings with a patient token."""
        hospital = await insert_hospital(db_session, name=f"{tag} Mine", settings={"hours": "9-5"})
        await _link_to(db_session, patient, hospital)
        before = await _rows(db_session)
        tokens = {
            "the patient's own token": patient.token,
            "a patient token naming a staff user": create_patient_access_token(staff.id),
            "a patient token naming the hospital": create_patient_access_token(hospital.id),
        }
        requests: list[tuple[str, str, dict[str, Any] | None]] = [
            ("GET", STAFF_HOSPITAL, None),
            ("GET", f"{STAFF_HOSPITAL}/feature-flags", None),
            ("GET", f"{STAFF_HOSPITAL}/full", None),
            ("PATCH", STAFF_HOSPITAL, {"name": "pwned", "settings": {FLAG: True}}),
            # Staff routes named like the patient ones.
            ("GET", f"/api/v1/hospitals/{hospital.slug}", None),
            ("GET", f"/api/v1/hospitals/{hospital.id}", None),
            ("GET", "/api/v1/hospitals", None),
        ]

        for method, url, body in requests:
            anonymous = await patient.client.request(method, url, json=body)
            for kind, token in tokens.items():
                response = await patient.client.request(
                    method, url, json=body, headers=bearer(token)
                )
                # The staff API words a token it cannot use differently from no token; it is
                # the same refusal, and it carries nothing.
                assert response.status_code == anonymous.status_code, (kind, method, url)
                assert response.status_code in {401, 404, 405}, (kind, method, url, response.text)
                assert response.json()["error_code"] == anonymous.json()["error_code"]
                assert response.json()["success"] is False
                assert "data" not in response.json()
                assert "hours" not in response.text
                assert tag not in response.text
        for method, url, _ in requests[:4]:
            response = await patient.client.request(method, url, headers=patient.headers)
            assert response.status_code == 401, (method, url, response.text)
        assert await _rows(db_session) == before


async def patient_anonymous(browser: AsyncClient, url: str) -> Response:
    """What a caller with no credential at all is told at a URL."""
    response = await browser.get(url)
    assert response.status_code == 401, (url, response.text)
    assert _body(response) == UNAUTHENTICATED
    return response


# ── linked ───────────────────────────────────────────────────────────────────


class TestLinkedIsTheCallersOwn:
    async def test_two_accounts_each_see_their_own_link_and_never_the_others(
        self, application: FastAPI, sms: FakeSmsSender, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: learn where somebody else is a patient from the hospitals marked 'linked'."""
        first = await insert_hospital(db_session, name=f"{tag} A")
        second = await insert_hospital(db_session, name=f"{tag} B")
        neither = await insert_hospital(db_session, name=f"{tag} C")
        async with patient_client(application) as one, patient_client(application) as two:
            asha = await _signed_in(one, sms)
            ravi = await _signed_in(two, sms)
            await _link_to(db_session, asha, first)
            await _link_to(db_session, ravi, second)

            async def view(who: _Patient, **extra: Any) -> dict[str, bool]:
                response = await who.discover(search=tag, **extra)
                listed = {h["ref"]: h["linked"] for h in response.json()["data"]}
                for hospital in (first, second, neither):
                    for reference in (hospital.slug, hospital.id):
                        detail = await who.get(f"{HOSPITALS}/{reference}", **extra)
                        assert detail.json()["data"]["linked"] is listed[hospital.slug]
                return listed

            assert await view(asha) == {first.slug: True, second.slug: False, neither.slug: False}
            assert await view(ravi) == {first.slug: False, second.slug: True, neither.slug: False}
            # Naming the other account, its record or its phone changes nothing.
            ravi_record = (
                await db_session.execute(
                    select(PatientAccountLink.patient_id).where(
                        PatientAccountLink.account_id == ravi.account_id
                    )
                )
            ).scalar_one()
            for extra in (
                {"account_id": str(ravi.account_id)},
                {"patient_id": str(ravi_record)},
                {"phone": ravi.phone},
                {"linked": "true"},
            ):
                assert await view(asha, **extra) == {
                    first.slug: True,
                    second.slug: False,
                    neither.slug: False,
                }, extra
            spoofed = await asha.client.get(
                HOSPITALS,
                params={"search": tag},
                headers={
                    **asha.headers,
                    "X-Account-Id": str(ravi.account_id),
                    "X-Patient-Id": str(ravi_record),
                    "X-Hospital-Id": str(second.id),
                },
            )
            assert {h["ref"]: h["linked"] for h in spoofed.json()["data"]} == await view(asha)
            # Apart from the mark, both are told exactly the same.
            hers = (await asha.discover(search=tag)).json()["data"]
            his = (await ravi.discover(search=tag)).json()["data"]
            assert [{**h, "linked": None} for h in hers] == [{**h, "linked": None} for h in his]
            assert [link["hospital_ref"] for link in (await asha.me())["links"]] == [first.slug]
            assert [link["hospital_ref"] for link in (await ravi.me())["links"]] == [second.slug]

    async def test_a_suspended_link_is_not_linked_and_is_not_handed_to_the_new_number(
        self, application: FastAPI, sms: FakeSmsSender, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: keep a hospital's 'linked' mark — or inherit one — when a record changes phone."""
        hospital = await insert_hospital(db_session, name=f"{tag} Mine")
        async with patient_client(application) as one, patient_client(application) as two:
            old = await _signed_in(one, sms)
            new = await _signed_in(two, sms)
            record = await _link_to(db_session, old, hospital)
            assert (await old.hospital(hospital.slug)).json()["data"]["linked"] is True

            # The hospital moves the record to the other patient's number.
            await db_session.execute(
                update(Patient).where(Patient.id == record.id).values(phone=new.phone)
            )
            await db_session.commit()

            assert [link["suspended"] for link in (await old.me())["links"]] == [True]
            for who in (old, new):
                [listed] = (await who.discover(search=tag)).json()["data"]
                assert listed["linked"] is False
                assert (await who.hospital(hospital.slug)).json()["data"]["linked"] is False
                assert (await who.hospital(hospital.id)).json()["data"]["linked"] is False

    async def test_an_ended_link_is_not_linked(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: none — a link that was ended must stop being shown as one."""
        hospital = await insert_hospital(db_session, name=f"{tag} Mine")
        await _link_to(db_session, patient, hospital)
        assert (await patient.hospital(hospital.slug)).json()["data"]["linked"] is True

        await db_session.execute(
            update(PatientAccountLink)
            .where(PatientAccountLink.account_id == patient.account_id)
            .values(unlinked_at=datetime.now(UTC), unlink_reason="test")
        )
        await db_session.commit()

        [listed] = (await patient.discover(search=tag)).json()["data"]
        assert listed["linked"] is False
        assert (await patient.hospital(hospital.slug)).json()["data"]["linked"] is False
        assert (await patient.hospital(hospital.id)).json()["data"]["linked"] is False
        assert (await patient.me())["links"] == []

    async def test_a_record_on_the_callers_phone_is_not_a_link(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: learn that a hospital holds a record for a number, without proving anything."""
        with_record = await insert_hospital(db_session, name=f"{tag} A")
        without = await insert_hospital(db_session, name=f"{tag} B")
        await insert_patient_record(db_session, with_record.id, phone=patient.phone)

        one, two = (await patient.discover(search=tag)).json()["data"]

        assert (one["ref"], two["ref"]) == (with_record.slug, without.slug)
        assert {**one, "ref": None, "name": None} == {**two, "ref": None, "name": None}
        assert _body(await patient.hospital(with_record.slug))["data"]["linked"] is False


# ── The policy gate ──────────────────────────────────────────────────────────


class TestPolicyGate:
    async def test_a_pending_required_policy_closes_discovery_before_anything_is_looked_up(
        self,
        application: FastAPI,
        browser: AsyncClient,
        patient: _Patient,
        db_session: AsyncSession,
        tag: str,
    ) -> None:
        """Attack: browse — or probe which hospitals exist — without accepting a required policy."""
        opened = await insert_hospital(db_session, name=f"{tag} Open", address={"city": f"{tag}c"})
        closed = await insert_hospital(db_session, name=f"{tag} Shut", enabled=False)
        assert (await patient.discover(search=tag)).status_code == 200
        _with_a_required_policy(application)

        refused = [
            await patient.discover(),
            await patient.discover(search=tag, city=f"{tag}c", page=3, page_size=1),
            await patient.get(CITIES),
        ]
        by_reference = [
            await patient.hospital(reference)
            for reference in (
                opened.slug,
                opened.id,
                closed.slug,
                closed.id,
                f"{tag}-never-existed",
                uuid.uuid4(),
                "' OR '1'='1",
            )
        ]

        for response in (*refused, *by_reference):
            assert response.status_code == 403, response.text
            assert response.json()["error_code"] == "CONSENT_REQUIRED"
            assert "data" not in response.json()
            assert tag not in response.text
        # An open hospital, a closed one and one that never existed are refused alike.
        assert len({repr(_wire(response)) for response in by_reference}) == 1
        # The gate is for a patient we know: it is not a way round signing in.
        for url in (HOSPITALS, f"{HOSPITALS}/{opened.slug}", CITIES):
            assert _body(await browser.get(url)) == UNAUTHENTICATED


# ── No trace ─────────────────────────────────────────────────────────────────


class TestNoTrace:
    async def test_reading_or_probing_the_directory_writes_no_audit_row(
        self, browser: AsyncClient, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Reference data (§27.6): no read, and no refusal, is an audited event."""
        hospital = await insert_hospital(db_session, name=f"{tag} Open")
        closed = await insert_hospital(db_session, name=f"{tag} Shut", enabled=False)
        await _link_to(db_session, patient, hospital)
        before = await _audit_count(db_session)

        statuses = [
            (await patient.discover()).status_code,
            (await patient.discover(search=tag, city="Hyderabad", page=2)).status_code,
            (await patient.hospital(hospital.slug)).status_code,
            (await patient.hospital(hospital.id)).status_code,
            (await patient.get(CITIES)).status_code,
            (await patient.hospital(closed.slug)).status_code,
            (await patient.hospital("' OR '1'='1")).status_code,
            (await patient.discover(page=0)).status_code,
            (await browser.get(HOSPITALS)).status_code,
            (await browser.get(CITIES, headers=bearer("not-a-token"))).status_code,
        ]

        assert statuses == [200, 200, 200, 200, 200, 404, 404, 422, 401, 401]
        assert await _audit_count(db_session) == before

    async def test_no_log_line_carries_a_token_a_phone_or_anything_hidden(
        self,
        browser: AsyncClient,
        patient: _Patient,
        db_session: AsyncSession,
        tag: str,
        all_logs: _LogTrap,
    ) -> None:
        """Attack: read a credential, a phone number or a hospital's private data out of the logs."""
        hidden = {name: _canary() for name in ("settings", "tax id", "email", "address", "typed")}
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} Open",
            address={"city": f"{tag}city", "gstin": hidden["address"]},
            settings={"smtp_password": hidden["settings"]},
            tax_id=hidden["tax id"],
            email=f"{hidden['email']}@hospital.test",
            phone="04040001234",
        )
        await _link_to(db_session, patient, hospital)
        forged = _forgeries(patient.account_id)["signed with another key"]
        all_logs.lines.clear()

        await patient.discover()
        await patient.discover(search=hidden["typed"], city=hidden["typed"])
        await patient.hospital(hospital.slug)
        await patient.hospital(hospital.id)
        await patient.hospital(f"{tag}-never-existed")
        await patient.get(CITIES)
        await patient.discover(page=0)
        await browser.get(HOSPITALS, headers=bearer(forged))
        await browser.get(HOSPITALS, params={"access_token": patient.token})

        logged = all_logs.everything
        # The requests were captured: otherwise the trap proves nothing.
        assert logged.count("request_completed") >= 9
        assert HOSPITALS in logged
        assert CITIES in logged
        for what, secret in {
            **hidden,
            "access token": patient.token,
            "token signature": patient.token.rsplit(".", 1)[1],
            "forged token": forged,
            "phone": patient.phone,
            "phone digits": patient.phone.removeprefix("+91"),
            "flag": FLAG,
        }.items():
            assert secret not in logged, what


# ── The city options ─────────────────────────────────────────────────────────


class TestCities:
    @pytest.mark.parametrize("state", [*CLOSED, "settings is a list"])
    async def test_a_city_only_closed_hospitals_are_in_is_not_an_option(
        self, patient: _Patient, db_session: AsyncSession, tag: str, state: str
    ) -> None:
        """Attack: learn where an unlisted hospital is from the filter's options."""
        await insert_hospital(db_session, name="Open", address={"city": f"{tag}-shared"})
        closed = await insert_hospital(
            db_session, name="Shut", address={"city": f"{tag}-secret"}, **CLOSED.get(state, {})
        )
        also_closed = await insert_hospital(
            db_session, name="Shut", address={"city": f"{tag}-SHARED"}, **CLOSED.get(state, {})
        )
        if state == "settings is a list":
            for hospital in (closed, also_closed):
                await _set_settings(db_session, hospital, [{FLAG: True}])

        response = await patient.get(CITIES)

        assert [c for c in response.json()["data"]["cities"] if tag in c.lower()] == [
            f"{tag}-shared"
        ]
        assert f"{tag}-secret" not in response.text.lower()

    async def test_the_options_are_exactly_the_cities_of_the_listed_hospitals(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """The whole answer against the Python-side rule: nothing more, nothing twice, in order."""
        stored: list[Any] = [
            f"{tag}pur", f"  {tag}pur\t", f"{tag}PUR", f"{tag}abad", f"{tag} Nagar", "", "   ",
            None, 42, True, ["x"], {"name": f"{tag}object"}, f"{tag}{'y' * 80}",
        ]  # fmt: skip
        for index, city in enumerate(stored):
            await insert_hospital(db_session, name=f"{tag} {index}", address={"city": city})
        await insert_hospital(db_session, name=f"{tag} L", address=[f"{tag}list"])
        await insert_hospital(db_session, name=f"{tag} N", address={"line1": "no city"})
        for state in CLOSED.values():
            await insert_hospital(
                db_session, name=f"{tag} S", address={"city": f"{tag}closed"}, **state
            )

        cities = await patient.cities()

        expected = {
            row.city.lower()
            for row in await _rows(db_session)
            if row.listed and row.city and len(row.city) <= FILTER_MAX_LENGTH
        }
        folded = [city.lower() for city in cities]
        assert set(folded) == expected
        assert len(folded) == len(set(folded))
        assert all(city and city == city.strip() for city in cities)
        assert [c for c in folded if tag in c] == [f"{tag} nagar", f"{tag}abad", f"{tag}pur"]
        # Every option, sent back as the filter, finds only listed hospitals — and some.
        for city in cities:
            refs, total = await patient.walk(city=city)
            assert total >= 1, city
            assert set(refs) == await _directory(db_session, city=city), city

    async def test_the_options_are_bounded(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: none — a platform with very many cities must not answer with all of them."""
        db_session.add_all(
            Hospital(
                id=uuid.uuid4(),
                name=f"{tag} {index:03d}",
                slug=f"{tag}-{index:03d}",
                address={"city": f"{tag}-{index:03d}"},
                settings={FLAG: True},
            )
            for index in range(MAX_CITIES + 15)
        )
        await db_session.commit()

        cities = await patient.cities()

        assert len(cities) <= MAX_CITIES
        assert len(cities) == len({city.lower() for city in cities})
        # The hospitals themselves are all still found.
        assert (await patient.walk(search=tag))[1] == MAX_CITIES + 15
