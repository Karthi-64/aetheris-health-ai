"""API tests for Patient App hospital discovery — the list, one hospital, the city options.

``docs/modules/15-patient-app.md`` §11 and §27.6, over HTTP, against a real
PostgreSQL. The patient signs in the way a browser does; hospitals are inserted
directly, as the platform would have set them up.

Every test marks its own hospitals with a random tag in their names and cities
and asserts on those, so that a hospital another test left behind can never
change a total here.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

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
from app.core.config import settings
from app.core.security import create_access_token
from app.database.unit_of_work import UnitOfWork  # noqa: TC001 — as above
from app.models.audit_log import AuditLog
from app.models.hospital import Hospital
from app.models.patient import Patient
from app.models.patient_consent import ConsentPurpose
from app.repositories import (  # noqa: TC001 — as above
    PatientAccountLinkRepository,
    PatientConsentRepository,
)
from app.services.patient_app.consent_service import ConsentService
from app.services.patient_app.policies import Policy
from app.tests.patient_app_helpers import (
    PATIENT,
    POLICY,
    FakeSmsSender,
    bearer,
    build_patient_application,
    insert_hospital,
    insert_patient_record,
    new_phone,
    open_hospital,
    patient_client,
    sign_in,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

HOSPITALS = f"{PATIENT}/hospitals"
CITIES = f"{PATIENT}/hospital-cities"
ME = f"{PATIENT}/me"
DOB = "1990-05-17"

HOSPITAL_FIELDS = {"ref", "name", "address", "phone", "logo_url", "timezone", "linked", "listing"}
ADDRESS_FIELDS = {"line1", "line2", "city", "state", "postal_code", "country"}
NOT_FOUND = (404, "RESOURCE_NOT_FOUND", "Not Found")

# Documented numbers, restated on purpose.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
MAX_PAGE = 1000
FILTER_MAX_LENGTH = 80


@pytest.fixture(autouse=True)
def _patient_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)


@pytest.fixture
def sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def tag() -> str:
    """A word no other hospital's name or city contains."""
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
        self.headers = bearer(session["access_token"])

    async def discover(self, **params: Any) -> Response:
        return await self.client.get(HOSPITALS, params=params, headers=self.headers)

    async def found(self, **params: Any) -> list[dict[str, Any]]:
        """The hospitals of one page that must exist."""
        response = await self.discover(**params)
        assert response.status_code == 200, response.text
        data: list[dict[str, Any]] = response.json()["data"]
        return data

    async def hospital(self, hospital_ref: object) -> Response:
        return await self.client.get(f"{HOSPITALS}/{hospital_ref}", headers=self.headers)

    async def cities(self) -> list[str]:
        response = await self.client.get(CITIES, headers=self.headers)
        assert response.status_code == 200, response.text
        assert set(response.json()["data"]) == {"cities"}
        cities: list[str] = response.json()["data"]["cities"]
        return cities

    async def link(self, hospital_ref: object) -> Response:
        return await self.client.post(
            f"{HOSPITALS}/{hospital_ref}/link",
            json={"date_of_birth": DOB, "consent_policy_version": POLICY},
            headers=self.headers,
        )

    async def me(self) -> dict[str, Any]:
        response = await self.client.get(ME, headers=self.headers)
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()["data"]
        return data


async def _signed_in(client: AsyncClient, sms: FakeSmsSender) -> _Patient:
    phone = new_phone()
    return _Patient(client, phone, await sign_in(client, sms, phone))


@pytest_asyncio.fixture
async def patient(browser: AsyncClient, sms: FakeSmsSender) -> _Patient:
    return await _signed_in(browser, sms)


def _refs(hospitals: list[dict[str, Any]]) -> list[str]:
    return [hospital["ref"] for hospital in hospitals]


def _pagination(response: Response) -> dict[str, int]:
    pagination: dict[str, int] = response.json()["metadata"]["pagination"]
    return pagination


def _error(response: Response) -> tuple[int, str, str]:
    body = response.json()
    return response.status_code, body["error_code"], body["message"]


def _without_request_id(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    body.pop("metadata", None)
    return body


async def _whole_directory(patient: _Patient) -> tuple[list[str], int]:
    """Every reference in the unfiltered directory, page by page, and its total."""
    refs: list[str] = []
    page = 1
    while True:
        response = await patient.discover(page=page, page_size=MAX_PAGE_SIZE)
        assert response.status_code == 200, response.text
        refs.extend(_refs(response.json()["data"]))
        pagination = _pagination(response)
        if page >= pagination["total_pages"]:
            return refs, pagination["total_records"]
        page += 1


async def _close(session: AsyncSession, hospital: Hospital, how: str) -> None:
    """Close a listed hospital to patients, one way or another."""
    if how == "inactive":
        await session.execute(
            update(Hospital).where(Hospital.id == hospital.id).values(is_active=False)
        )
        await session.commit()
    else:
        await open_hospital(session, hospital.id, enabled=False)


async def _link_to(session: AsyncSession, patient: _Patient, hospital: Hospital) -> Patient:
    """Give the patient a record at the hospital and link the account to it."""
    record = await insert_patient_record(session, hospital.id, phone=patient.phone)
    response = await patient.link(hospital.slug)
    assert response.status_code == 201, response.text
    return record


# ── The list ─────────────────────────────────────────────────────────────────


class TestDiscover:
    async def test_it_answers_with_a_page_in_the_standard_envelope(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        first = await insert_hospital(db_session, name=f"{tag} Alpha")
        second = await insert_hospital(db_session, name=f"{tag} Bravo")

        response = await patient.discover(search=tag)

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"success", "message", "data", "metadata"}
        assert body["success"] is True
        assert _pagination(response) == {
            "page": 1,
            "page_size": DEFAULT_PAGE_SIZE,
            "total_records": 2,
            "total_pages": 1,
        }
        assert _refs(body["data"]) == [first.slug, second.slug]
        for hospital in body["data"]:
            assert set(hospital) == HOSPITAL_FIELDS
            assert set(hospital["address"]) == ADDRESS_FIELDS

    async def test_a_hospital_is_described_by_its_public_fields(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} General Hospital",
            slug=f"{tag}-general",
            address={
                "line1": "12 Lake Road",
                "line2": "Indiranagar",
                "city": "Bengaluru",
                "state": "Karnataka",
                "postal_code": "560038",
                "country": "IN",
            },
            phone="+91 80 4000 1234",
            logo_url="https://cdn.example.com/logos/general.png",
            timezone="Asia/Kolkata",
        )

        [described] = await patient.found(search=tag)

        assert described == {
            "ref": f"{tag}-general",
            "name": f"{tag} General Hospital",
            "address": {
                "line1": "12 Lake Road",
                "line2": "Indiranagar",
                "city": "Bengaluru",
                "state": "Karnataka",
                "postal_code": "560038",
                "country": "IN",
            },
            "phone": "+91 80 4000 1234",
            "logo_url": "https://cdn.example.com/logos/general.png",
            "timezone": "Asia/Kolkata",
            "linked": False,
            "listing": "standard",
        }
        assert str(hospital.id) not in str(described)

    async def test_missing_details_are_null_not_absent(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        await insert_hospital(db_session, name=f"{tag} Bare", address={})

        [described] = await patient.found(search=tag)

        assert described["address"] == dict.fromkeys(ADDRESS_FIELDS)
        assert described["phone"] is None
        assert described["logo_url"] is None

    async def test_the_unfiltered_list_is_every_open_hospital_and_no_other(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        listed = [await insert_hospital(db_session, name=f"{tag} {n}") for n in range(3)]
        switched_off = await insert_hospital(db_session, name=f"{tag} off", enabled=False)
        never_enabled = await insert_hospital(db_session, name=f"{tag} unset", enabled=None)
        inactive = await insert_hospital(db_session, name=f"{tag} closed", is_active=False)

        refs, total = await _whole_directory(patient)

        assert {hospital.slug for hospital in listed} <= set(refs)
        assert not {switched_off.slug, never_enabled.slug, inactive.slug} & set(refs)
        assert len(refs) == len(set(refs)) == total

    async def test_search_matches_any_part_of_the_name_whatever_the_case(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        wanted = await insert_hospital(db_session, name=f"Saint {tag.upper()} Memorial")
        await insert_hospital(db_session, name="Somewhere Else Clinic")

        for typed in (tag, tag.upper(), tag[2:9], f"t {tag}", f"{tag} MEMO", f"  {tag}  "):
            assert _refs(await patient.found(search=typed)) == [wanted.slug], typed
        assert await patient.found(search=f"{tag}x") == []

    async def test_search_takes_percent_underscore_and_backslash_literally(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: type a wildcard to match every hospital, whatever it is called."""
        percent = await insert_hospital(db_session, name=f"{tag}%pct")
        underscore = await insert_hospital(db_session, name=f"{tag}_und")
        backslash = await insert_hospital(db_session, name=f"{tag}\\bsl")
        plain = await insert_hospital(db_session, name=f"{tag}-pln")

        assert _refs(await patient.found(search=f"{tag}%")) == [percent.slug]
        assert _refs(await patient.found(search=f"{tag}_")) == [underscore.slug]
        assert _refs(await patient.found(search=f"{tag}\\")) == [backslash.slug]
        assert await patient.found(search=f"{tag}%pln") == []
        assert await patient.found(search=f"{tag}_pln") == []
        # On their own they are still characters to look for, not "anything".
        for wildcard, holder in (("%", percent), ("_", underscore), ("\\", backslash)):
            refs = _refs(await patient.found(search=wildcard, page_size=MAX_PAGE_SIZE))
            assert holder.slug in refs
            assert plain.slug not in refs

    @pytest.mark.parametrize(
        "typed",
        [
            "' OR '1'='1",
            "'; DROP TABLE hospitals; --",
            "%' OR name ILIKE '%",
            "\\",
            "\\%",
            "{tag}\u202e",
            "{tag} \U0001f3e5",
            "{tag}\x07",
        ],
    )
    async def test_whatever_is_typed_is_text_to_look_for_and_nothing_more(
        self, patient: _Patient, db_session: AsyncSession, tag: str, typed: str
    ) -> None:
        """Attack: type SQL, or a pattern, into the search box or the city filter."""
        hospital = await insert_hospital(db_session, name=f"{tag} Real", address={"city": tag})
        typed = typed.format(tag=tag)

        for params in ({"search": typed}, {"city": typed}, {"search": tag, "city": typed}):
            response = await patient.discover(**params, page_size=MAX_PAGE_SIZE)
            assert response.status_code == 200, (params, response.text)
            assert hospital.slug not in _refs(response.json()["data"]), params
        assert _refs(await patient.found(search=tag)) == [hospital.slug]

    async def test_search_looks_at_the_name_and_nothing_else(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        await insert_hospital(
            db_session,
            name="Plain Name Hospital",
            slug=f"{tag}-slug",
            address={"line1": f"{tag} Street", "city": f"{tag}ville"},
            email=f"{tag}@hospital.test",
            tax_id=f"TAX-{tag}",
        )

        assert await patient.found(search=tag) == []

    async def test_a_blank_search_or_city_is_no_filter(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        await insert_hospital(db_session, name=f"{tag} One")
        unfiltered = _pagination(await patient.discover())["total_records"]

        for params in ({"search": ""}, {"search": "   "}, {"city": ""}, {"city": " \t "}):
            response = await patient.discover(**params)
            assert response.status_code == 200, response.text
            assert _pagination(response)["total_records"] == unfiltered, params

    async def test_the_city_filter_matches_the_whole_city_whatever_the_case(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        city = f"{tag}pur"
        here = await insert_hospital(db_session, name="Aaa", address={"city": city.title()})
        padded = await insert_hospital(db_session, name="Bbb", address={"city": f"  {city} "})
        await insert_hospital(db_session, name="Ccc", address={"city": f"{city} East"})
        await insert_hospital(db_session, name="Ddd", address={"line1": city})

        for typed in (city, city.upper(), f" {city} "):
            assert _refs(await patient.found(city=typed)) == [here.slug, padded.slug], typed
        # Never a part of a city, and never a pattern.
        for typed in (tag, city[:-1], f"{city}%", f"{tag}%", "%"):
            assert await patient.found(city=typed) == [], typed

    async def test_search_and_city_narrow_the_list_together(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        city, elsewhere = f"{tag}nagar", f"{tag}abad"
        both = await insert_hospital(db_session, name=f"{tag} Heart", address={"city": city})
        await insert_hospital(db_session, name=f"{tag} Heart", address={"city": elsewhere})
        await insert_hospital(db_session, name=f"{tag} Eye", address={"city": city})

        response = await patient.discover(search="heart", city=city)

        assert _refs(response.json()["data"]) == [both.slug]
        assert _pagination(response)["total_records"] == 1
        assert _pagination(await patient.discover(search=tag))["total_records"] == 3
        assert _pagination(await patient.discover(city=city))["total_records"] == 2

    async def test_pages_cover_the_list_once_and_tell_the_truth_about_its_size(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospitals = [await insert_hospital(db_session, name=f"{tag} {n}") for n in range(5)]
        expected = [hospital.slug for hospital in hospitals]

        walked: list[str] = []
        for page, size in ((1, 2), (2, 2), (3, 1)):
            response = await patient.discover(search=tag, page=page, page_size=2)
            assert len(response.json()["data"]) == size
            assert _pagination(response) == {
                "page": page,
                "page_size": 2,
                "total_records": 5,
                "total_pages": 3,
            }
            walked.extend(_refs(response.json()["data"]))

        assert walked == expected
        assert _refs(await patient.found(search=tag, page_size=5)) == expected
        assert _refs(await patient.found(search=tag, page_size=MAX_PAGE_SIZE)) == expected
        assert _refs(await patient.found(search=tag, page=2, page_size=3)) == expected[3:]

    @pytest.mark.parametrize(("page", "page_size"), [(4, 2), (2, 5), (MAX_PAGE, MAX_PAGE_SIZE)])
    async def test_a_page_beyond_the_end_is_empty_with_the_right_total(
        self, patient: _Patient, db_session: AsyncSession, tag: str, page: int, page_size: int
    ) -> None:
        for n in range(5):
            await insert_hospital(db_session, name=f"{tag} {n}")

        response = await patient.discover(search=tag, page=page, page_size=page_size)

        assert response.status_code == 200, response.text
        assert response.json()["data"] == []
        assert _pagination(response)["total_records"] == 5
        assert _pagination(response)["total_pages"] == -(-5 // page_size)

    async def test_no_match_is_an_empty_page_not_an_error(
        self, patient: _Patient, tag: str
    ) -> None:
        response = await patient.discover(search=tag)

        assert response.status_code == 200, response.text
        assert response.json()["data"] == []
        assert _pagination(response) == {
            "page": 1,
            "page_size": DEFAULT_PAGE_SIZE,
            "total_records": 0,
            "total_pages": 0,
        }

    async def test_the_order_is_by_name_then_code_and_never_changes(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Three hospitals share a name but for its capitals: the code breaks the tie."""
        await insert_hospital(db_session, name=f"{tag} Zeta", slug=f"{tag}-a")
        await insert_hospital(db_session, name=f"{tag} alpha", slug=f"{tag}-d")
        await insert_hospital(db_session, name=f"{tag} ALPHA", slug=f"{tag}-b")
        await insert_hospital(db_session, name=f"{tag} Alpha", slug=f"{tag}-c")
        await insert_hospital(db_session, name=f"{tag} Mid", slug=f"{tag}-e")
        expected = [f"{tag}-{letter}" for letter in "bcdea"]

        assert _refs(await patient.found(search=tag)) == expected
        assert _refs(await patient.found(search=tag)) == expected
        one_at_a_time = [
            (await patient.found(search=tag, page=page, page_size=1))[0]["ref"]
            for page in range(1, 6)
        ]
        assert one_at_a_time == expected

    @pytest.mark.parametrize("flag", [False, None, "true", 1])
    async def test_a_hospital_without_the_flag_exactly_on_is_not_listed(
        self, patient: _Patient, db_session: AsyncSession, tag: str, flag: Any
    ) -> None:
        await insert_hospital(db_session, name=f"{tag} Shut", enabled=flag)
        opened = await insert_hospital(db_session, name=f"{tag} Open")

        response = await patient.discover(search=tag)

        assert _refs(response.json()["data"]) == [opened.slug]
        assert _pagination(response)["total_records"] == 1

    @pytest.mark.parametrize("how", ["flag_off", "inactive"])
    async def test_a_hospital_that_closes_is_gone_from_the_next_request(
        self, patient: _Patient, db_session: AsyncSession, tag: str, how: str
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} Soon Shut")
        assert _refs(await patient.found(search=tag)) == [hospital.slug]

        await _close(db_session, hospital, how)

        response = await patient.discover(search=tag)
        assert response.json()["data"] == []
        assert _pagination(response)["total_records"] == 0

    @pytest.mark.parametrize(
        "params",
        [
            {"page": 0},
            {"page": -1},
            {"page": MAX_PAGE + 1},
            {"page": "two"},
            {"page": "1.5"},
            {"page_size": 0},
            {"page_size": -5},
            {"page_size": MAX_PAGE_SIZE + 1},
            {"page_size": "all"},
            {"search": "s" * (FILTER_MAX_LENGTH + 1)},
            {"city": "c" * (FILTER_MAX_LENGTH + 1)},
            # The one character the database cannot hold in text.
            {"search": "heart\x00"},
            {"city": "\x00"},
        ],
    )
    async def test_a_parameter_out_of_bounds_is_refused(
        self, patient: _Patient, params: dict[str, Any]
    ) -> None:
        response = await patient.discover(**params)

        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    async def test_the_bounds_themselves_are_accepted(self, patient: _Patient) -> None:
        for params in (
            {"page": MAX_PAGE, "page_size": MAX_PAGE_SIZE},
            {"page": 1, "page_size": 1},
            {"search": "s" * FILTER_MAX_LENGTH},
            {"city": "c" * FILTER_MAX_LENGTH},
            # Trimmed first, measured after.
            {"search": "  " + "s" * FILTER_MAX_LENGTH + "  "},
        ):
            assert (await patient.discover(**params)).status_code == 200, params

    async def test_a_parameter_it_does_not_define_changes_nothing(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Attack: ask for the closed hospitals too, or for another order."""
        opened = await insert_hospital(db_session, name=f"{tag} Open")
        closed = await insert_hospital(db_session, name=f"{tag} Shut", enabled=False)
        honest = await patient.discover(search=tag)

        for extra in (
            {"is_active": "false"},
            {"include_inactive": "1"},
            {"enabled": "false"},
            {"hospital_id": str(closed.id)},
            {"ref": closed.slug},
            {"sort": "-name"},
            {"listing": "promoted"},
            {"linked": "true"},
        ):
            response = await patient.discover(search=tag, **extra)
            assert response.status_code == 200, extra
            assert response.json()["data"] == honest.json()["data"], extra
            assert _pagination(response) == _pagination(honest), extra
        assert _refs(honest.json()["data"]) == [opened.slug]

    async def test_it_needs_a_patient_token(
        self, browser: AsyncClient, hospital_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        assert (await browser.get(HOSPITALS)).status_code == 401
        staff = bearer(create_access_token(actor_id, hospital_id))
        assert (await browser.get(HOSPITALS, headers=staff)).status_code == 401


# ── One hospital ─────────────────────────────────────────────────────────────


class TestOneHospital:
    async def test_it_is_found_by_its_code_and_is_what_the_list_showed(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} City Hospital",
            phone="080 4000 1234",
            logo_url="https://cdn.example.com/city.png",
            address={"line1": "1 MG Road", "city": "Bengaluru", "country": "IN"},
        )
        [listed] = await patient.found(search=tag)

        response = await patient.hospital(hospital.slug)

        assert response.status_code == 200, response.text
        assert set(response.json()) == {"success", "message", "data", "metadata"}
        assert response.json()["data"] == listed
        assert set(listed) == HOSPITAL_FIELDS

    async def test_it_is_found_by_its_id_and_still_answers_with_its_code(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} City Hospital")

        by_id = await patient.hospital(hospital.id)
        by_code = await patient.hospital(hospital.slug)

        assert by_id.status_code == 200, by_id.text
        assert by_id.json()["data"] == by_code.json()["data"]
        assert by_id.json()["data"]["ref"] == hospital.slug
        assert str(hospital.id) not in by_id.text

    @pytest.mark.parametrize(
        "state", ["unknown_code", "unknown_id", "flag_off", "flag_unset", "inactive", "malformed"]
    )
    async def test_a_hospital_that_cannot_be_shown_is_one_and_the_same_404(
        self, patient: _Patient, db_session: AsyncSession, tag: str, state: str
    ) -> None:
        reference: object
        if state == "unknown_code":
            reference = f"{tag}-nowhere"
        elif state == "unknown_id":
            reference = uuid.uuid4()
        elif state == "malformed":
            reference = "no such hospital!"
        else:
            hospital = await insert_hospital(
                db_session,
                name=f"{tag} Shut",
                enabled={"flag_off": False, "flag_unset": None}.get(state, True),
                is_active=state != "inactive",
            )
            reference = hospital.slug
        baseline = await patient.hospital(f"{tag}-never-existed")

        response = await patient.hospital(reference)

        assert _error(response) == NOT_FOUND
        assert _without_request_id(response) == _without_request_id(baseline)

    @pytest.mark.parametrize("how", ["flag_off", "inactive"])
    async def test_a_closed_hospital_is_not_found_by_its_id_either(
        self, patient: _Patient, db_session: AsyncSession, tag: str, how: str
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} Soon Shut")
        assert (await patient.hospital(hospital.id)).status_code == 200

        await _close(db_session, hospital, how)

        assert _error(await patient.hospital(hospital.id)) == NOT_FOUND
        assert _error(await patient.hospital(hospital.slug)) == NOT_FOUND

    async def test_a_reference_that_reaches_no_route_gets_the_very_same_answer(
        self, patient: _Patient, tag: str
    ) -> None:
        """A slash inside a reference never reaches the service: same 404, word for word."""
        unknown = await patient.hospital(f"{tag}-nowhere")

        unrouted = await patient.hospital(f"{tag}%2Fnowhere")

        assert _error(unrouted) == NOT_FOUND
        assert _without_request_id(unrouted) == _without_request_id(unknown)

    async def test_it_needs_a_patient_token(
        self, browser: AsyncClient, db_session: AsyncSession, tag: str, actor_id: uuid.UUID
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} City Hospital")
        url = f"{HOSPITALS}/{hospital.slug}"

        assert (await browser.get(url)).status_code == 401
        staff = bearer(create_access_token(actor_id, hospital.id))
        assert (await browser.get(url, headers=staff)).status_code == 401


# ── linked ───────────────────────────────────────────────────────────────────


class TestLinked:
    async def test_it_is_true_only_at_the_hospital_the_account_is_linked_at(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        mine = await insert_hospital(db_session, name=f"{tag} Mine")
        other = await insert_hospital(db_session, name=f"{tag} Other")
        before = {h["ref"]: h["linked"] for h in await patient.found(search=tag)}

        await _link_to(db_session, patient, mine)

        after = {h["ref"]: h["linked"] for h in await patient.found(search=tag)}
        assert before == {mine.slug: False, other.slug: False}
        assert after == {mine.slug: True, other.slug: False}
        assert (await patient.hospital(mine.slug)).json()["data"]["linked"] is True
        assert (await patient.hospital(mine.id)).json()["data"]["linked"] is True
        assert (await patient.hospital(other.slug)).json()["data"]["linked"] is False

    async def test_a_record_on_the_phone_is_not_a_link(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} Unlinked")
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        [listed] = await patient.found(search=tag)

        assert listed["linked"] is False

    async def test_a_suspended_link_is_not_linked(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """The hospital moved the record to another phone: the link is no longer honoured."""
        hospital = await insert_hospital(db_session, name=f"{tag} Mine")
        record = await _link_to(db_session, patient, hospital)
        assert (await patient.found(search=tag))[0]["linked"] is True

        await db_session.execute(
            update(Patient).where(Patient.id == record.id).values(phone=new_phone())
        )
        await db_session.commit()

        assert [link["suspended"] for link in (await patient.me())["links"]] == [True]
        assert (await patient.found(search=tag))[0]["linked"] is False
        assert (await patient.hospital(hospital.slug)).json()["data"]["linked"] is False

    async def test_it_says_nothing_about_anybody_elses_link(
        self, application: FastAPI, sms: FakeSmsSender, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} Shared")
        async with patient_client(application) as one, patient_client(application) as two:
            linked = await _signed_in(one, sms)
            stranger = await _signed_in(two, sms)
            await _link_to(db_session, linked, hospital)

            theirs = await stranger.found(search=tag)
            detail = await stranger.hospital(hospital.slug)
            mine = await linked.found(search=tag)

        assert [h["linked"] for h in mine] == [True]
        assert [h["linked"] for h in theirs] == [False]
        assert detail.json()["data"]["linked"] is False
        # Apart from that one mark, both are told exactly the same.
        assert {**mine[0], "linked": False} == theirs[0]


# ── The city options ─────────────────────────────────────────────────────────


class TestCities:
    async def test_it_lists_each_city_of_a_listed_hospital_once(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        for city in (f"{tag}pur", f"  {tag}pur ", f"{tag}PUR", f"{tag}abad", f"{tag}Nagar"):
            await insert_hospital(db_session, name="Somewhere", address={"city": city})

        cities = [city for city in await patient.cities() if tag in city.lower()]

        assert [city.lower() for city in cities] == [f"{tag}abad", f"{tag}nagar", f"{tag}pur"]
        assert all(city == city.strip() for city in cities)

    async def test_the_whole_list_is_sorted_and_has_no_blank_and_no_duplicate(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        for city in (f"{tag}b", f"{tag}a", f"{tag}c", "", "   "):
            await insert_hospital(db_session, name="Somewhere", address={"city": city})
        await insert_hospital(db_session, name="Somewhere", address={"line1": "No city"})

        cities = await patient.cities()

        folded = [city.lower() for city in cities]
        assert len(folded) == len(set(folded))
        assert all(city and city == city.strip() for city in cities)
        ours = [city for city in folded if city.startswith(tag)]
        assert ours == [f"{tag}a", f"{tag}b", f"{tag}c"]

    @pytest.mark.parametrize("closed", ["flag_off", "flag_unset", "inactive"])
    async def test_a_city_with_only_closed_hospitals_is_not_an_option(
        self, patient: _Patient, db_session: AsyncSession, tag: str, closed: str
    ) -> None:
        await insert_hospital(db_session, name="Open", address={"city": f"{tag}open"})
        await insert_hospital(
            db_session,
            name="Shut",
            address={"city": f"{tag}shut"},
            enabled={"flag_off": False, "flag_unset": None}.get(closed, True),
            is_active=closed != "inactive",
        )

        cities = [city for city in await patient.cities() if city.startswith(tag)]

        assert cities == [f"{tag}open"]

    async def test_a_city_stops_being_an_option_when_its_last_hospital_closes(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(db_session, name="Only", address={"city": f"{tag}ville"})
        assert f"{tag}ville" in await patient.cities()

        await _close(db_session, hospital, "flag_off")

        assert f"{tag}ville" not in await patient.cities()

    async def test_every_option_finds_its_hospitals_when_sent_back_as_the_filter(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        stored = {f"{tag}pur": 2, f" {tag} Nagar ": 1, f"{tag}ABAD": 1}
        for city, count in stored.items():
            for _ in range(count):
                await insert_hospital(db_session, name="Somewhere", address={"city": city})

        options = [city for city in await patient.cities() if tag in city.lower()]

        assert len(options) == 3
        found = {
            option.lower(): _pagination(await patient.discover(city=option))["total_records"]
            for option in options
        }
        assert found == {f"{tag}pur": 2, f"{tag} nagar": 1, f"{tag}abad": 1}

    async def test_a_city_that_is_not_text_or_too_long_to_filter_by_is_left_out(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        number = int(uuid.uuid4().int % 10**12) + 10**12
        await insert_hospital(db_session, name=f"{tag} A", address={"city": number})
        await insert_hospital(db_session, name=f"{tag} B", address={"city": [f"{tag}list"]})
        await insert_hospital(db_session, name=f"{tag} C", address={"city": tag + "x" * 80})
        await insert_hospital(db_session, name=f"{tag} D", address=[f"{tag}array"])

        cities = await patient.cities()

        assert str(number) not in cities
        assert not [city for city in cities if tag in city]
        # The hospitals themselves are listed; only the unusable city is not shown.
        described = {h["name"]: h["address"]["city"] for h in await patient.found(search=tag)}
        assert described == {
            f"{tag} A": None,
            f"{tag} B": None,
            f"{tag} C": (tag + "x" * 80),
            f"{tag} D": None,
        }
        assert await patient.found(city=str(number)) == []

    async def test_it_needs_a_patient_token(
        self, browser: AsyncClient, hospital_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        assert (await browser.get(CITIES)).status_code == 401
        staff = bearer(create_access_token(actor_id, hospital_id))
        assert (await browser.get(CITIES, headers=staff)).status_code == 401

    async def test_cities_is_never_mistaken_for_a_hospital_code(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Why the options have a path of their own: "cities" can be a hospital's code."""
        existing = (
            await db_session.execute(select(Hospital.id).where(Hospital.slug == "cities"))
        ).first()
        if existing is not None:
            pytest.skip("a hospital with the code 'cities' already exists in this database")
        await insert_hospital(db_session, name=f"{tag} Cities Hospital", slug="cities")

        response = await patient.hospital("cities")

        assert response.status_code == 200, response.text
        assert response.json()["data"]["name"] == f"{tag} Cities Hospital"
        assert isinstance(await patient.cities(), list)


# ── /me carries the reference ────────────────────────────────────────────────


class TestTheLinkCarriesTheReference:
    async def test_a_link_in_me_names_its_hospital_by_the_reference_discovery_uses(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} Mine")
        await _link_to(db_session, patient, hospital)

        [link] = (await patient.me())["links"]

        assert set(link) == {
            "hospital_id",
            "hospital_ref",
            "hospital_name",
            "linked_at",
            "suspended",
        }
        assert link["hospital_ref"] == hospital.slug
        assert link["hospital_id"] == str(hospital.id)
        detail = await patient.hospital(link["hospital_ref"])
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["name"] == link["hospital_name"]
        assert detail.json()["data"]["linked"] is True

    async def test_linking_answers_with_the_reference_too(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} Mine")
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        linked = await patient.link(hospital.id)

        assert linked.status_code == 201, linked.text
        assert linked.json()["data"]["hospital_ref"] == hospital.slug

    @pytest.mark.parametrize("shape", ["{tag}-Upper", "{tag}_under", "{uuid}"])
    async def test_a_hospital_whose_code_no_reference_can_spell_has_no_reference(
        self, patient: _Patient, db_session: AsyncSession, tag: str, shape: str
    ) -> None:
        """Bad data, not an attack: the link is real, but there is no page to send anyone to.

        A reference handed out here would be one hospital discovery refuses,
        so none is handed out — and the hospital is in no list, total or city.
        """
        slug = shape.format(tag=tag, uuid=uuid.uuid4())
        hospital = await insert_hospital(
            db_session, name=f"{tag} Odd", slug=slug, address={"city": f"{tag}ville"}
        )
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        linked = await patient.link(hospital.id)
        [link] = (await patient.me())["links"]
        listed = await patient.discover(search=tag)

        assert linked.status_code == 201, linked.text
        assert linked.json()["data"]["hospital_ref"] is None
        assert link["hospital_id"] == str(hospital.id)
        assert link["hospital_ref"] is None
        assert (await patient.hospital(slug)).status_code == 404
        assert (await patient.hospital(hospital.id)).status_code == 404
        assert listed.json()["data"] == []
        assert _pagination(listed)["total_records"] == 0
        assert f"{tag}ville" not in await patient.cities()


# ── Reference data: gated by policy, never audited ───────────────────────────


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


class TestGates:
    async def test_a_pending_required_policy_closes_discovery(
        self, application: FastAPI, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} Open")
        _with_a_required_policy(application)

        responses = [
            await patient.discover(search=tag),
            await patient.hospital(hospital.slug),
            await patient.hospital(f"{tag}-nowhere"),
            await patient.client.get(CITIES, headers=patient.headers),
        ]

        for response in responses:
            assert response.status_code == 403, response.text
            assert response.json()["error_code"] == "CONSENT_REQUIRED"
            assert hospital.name not in response.text
        # A hospital that exists and one that does not are refused alike.
        assert _without_request_id(responses[1]) == _without_request_id(responses[2])

    async def test_reading_the_directory_writes_no_audit_row(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} Open")

        async def _rows() -> int:
            return (
                await db_session.execute(select(func.count()).select_from(AuditLog))
            ).scalar_one()

        before = await _rows()

        await patient.found(search=tag)
        await patient.hospital(hospital.slug)
        await patient.hospital(f"{tag}-nowhere")
        await patient.cities()

        assert await _rows() == before
