"""Unit tests for hospital discovery — the directory service, and the gate's side of it.

No database. What is under test here is what a patient is told about a
hospital and in which order things are checked; the SQL behind the directory
is in ``app/tests/repository/test_hospital_repository.py`` and the endpoints
are in ``app/tests/api/test_patient_hospitals_api.py``.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.config import AppEnv, settings
from app.core.exceptions import NotFoundError
from app.core.feature_flags import PATIENT_APP_ENABLED
from app.repositories.hospital_repository import HospitalDirectoryEntry, HospitalSummary
from app.schemas.patient_app.account import PatientLink
from app.schemas.patient_app.hospitals import HospitalListing, PatientHospital
from app.services.patient_app.errors import ConsentRequiredError
from app.services.patient_app.hospital_directory_service import HospitalDirectoryService
from app.services.patient_app.hospital_gate import PatientHospitalGate

HOSPITAL_FIELDS = {"ref", "name", "address", "phone", "logo_url", "timezone", "linked", "listing"}
ADDRESS_FIELDS = {"line1", "line2", "city", "state", "postal_code", "country"}
OPEN = {PATIENT_APP_ENABLED: True}


def _entry(**overrides: Any) -> HospitalDirectoryEntry:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": "City Hospital",
        "slug": "city-hospital",
        "settings": dict(OPEN),
        "address": {"line1": "1 MG Road", "city": "Bengaluru", "country": "IN"},
        "phone": "+91 80 4000 1234",
        "logo_url": "https://cdn.example.com/city.png",
        "timezone": "Asia/Kolkata",
    }
    values.update(overrides)
    return HospitalDirectoryEntry(**values)


def _link(hospital_id: uuid.UUID, *, suspended: bool = False) -> PatientLink:
    return PatientLink(
        hospital_id=hospital_id,
        hospital_ref="somewhere",
        hospital_name="Somewhere",
        linked_at=datetime(2026, 10, 1, tzinfo=UTC),
        suspended=suspended,
    )


class _Directory:
    """The service on mocked collaborators, which stay inspectable."""

    def __init__(
        self,
        *,
        entries: list[HospitalDirectoryEntry] | None = None,
        total: int | None = None,
        described: HospitalDirectoryEntry | None = None,
        links: list[PatientLink] | None = None,
        cities: list[str] | None = None,
    ) -> None:
        self.account: Any = SimpleNamespace(id=uuid.uuid4(), phone="+919812345678")
        self.gate = AsyncMock()
        self.gate.list_open.return_value = (
            entries or [],
            len(entries or []) if total is None else total,
        )
        self.gate.describe.return_value = described
        self.gate.open_cities.return_value = cities or []
        self.authorization = AsyncMock()
        self.authorization.describe_links.return_value = links or []
        self.consent = AsyncMock()
        self.service = HospitalDirectoryService(self.gate, self.authorization, self.consent)

    async def discover(self, **params: Any) -> Any:
        arguments = {"search": None, "city": None, "page": 1, "page_size": 20, **params}
        return await self.service.discover(self.account, **arguments)

    async def describe(self, entry: HospitalDirectoryEntry) -> dict[str, Any]:
        """One hospital, as the JSON a patient receives."""
        self.gate.describe.return_value = entry
        hospital = await self.service.get_hospital(self.account, entry.slug)
        dumped: dict[str, Any] = hospital.model_dump(mode="json")
        return dumped


# ── The policy gate comes first ──────────────────────────────────────────────


class TestThePolicyGate:
    async def test_nothing_is_looked_up_for_an_account_with_a_policy_to_accept(self) -> None:
        directory = _Directory(entries=[_entry()], described=_entry(), cities=["Pune"])
        directory.consent.ensure_policies_accepted.side_effect = ConsentRequiredError()

        with pytest.raises(ConsentRequiredError):
            await directory.discover()
        with pytest.raises(ConsentRequiredError):
            await directory.service.get_hospital(directory.account, "city-hospital")
        with pytest.raises(ConsentRequiredError):
            await directory.service.list_cities(directory.account)

        directory.gate.list_open.assert_not_awaited()
        directory.gate.describe.assert_not_awaited()
        directory.gate.open_cities.assert_not_awaited()
        directory.authorization.describe_links.assert_not_awaited()

    async def test_it_is_asked_about_the_authenticated_account_on_every_read(self) -> None:
        directory = _Directory(described=_entry())

        await directory.discover()
        await directory.service.get_hospital(directory.account, "city-hospital")
        await directory.service.list_cities(directory.account)

        asked = directory.consent.ensure_policies_accepted.await_args_list
        assert [call.args for call in asked] == [(directory.account.id,)] * 3


# ── The list ─────────────────────────────────────────────────────────────────


class TestDiscover:
    async def test_the_page_number_becomes_an_offset(self) -> None:
        directory = _Directory()

        await directory.discover(page=3, page_size=20)

        assert directory.gate.list_open.await_args.kwargs == {
            "search": None,
            "city": None,
            "skip": 40,
            "limit": 20,
        }

    async def test_filters_are_trimmed_and_a_blank_one_is_no_filter(self) -> None:
        directory = _Directory()

        await directory.discover(search="  heart ", city="\tPune\n")
        typed = directory.gate.list_open.await_args.kwargs
        await directory.discover(search="   ", city="")
        blank = directory.gate.list_open.await_args.kwargs

        assert (typed["search"], typed["city"]) == ("heart", "Pune")
        assert (blank["search"], blank["city"]) == (None, None)

    async def test_the_page_carries_the_total_the_gate_counted(self) -> None:
        entries = [_entry(slug="one"), _entry(slug="two")]
        directory = _Directory(entries=entries, total=41)

        page = await directory.discover(page=2, page_size=2)

        assert [hospital.ref for hospital in page.items] == ["one", "two"]
        assert (page.page, page.page_size, page.total_records, page.total_pages) == (2, 2, 41, 21)
        assert all(isinstance(hospital, PatientHospital) for hospital in page.items)

    async def test_an_empty_page_keeps_its_total_and_reads_no_links(self) -> None:
        directory = _Directory(entries=[], total=5)

        page = await directory.discover(page=9, page_size=20)

        assert page.items == []
        assert page.total_records == 5
        directory.authorization.describe_links.assert_not_awaited()

    async def test_linked_is_the_callers_own_honoured_link_and_nothing_else(self) -> None:
        mine, suspended, never = _entry(slug="mine"), _entry(slug="suspended"), _entry(slug="never")
        directory = _Directory(
            entries=[mine, suspended, never],
            links=[_link(mine.id), _link(suspended.id, suspended=True), _link(uuid.uuid4())],
        )

        page = await directory.discover()

        assert {hospital.ref: hospital.linked for hospital in page.items} == {
            "mine": True,
            "suspended": False,
            "never": False,
        }
        # The links are those of the account the token proved, read once.
        directory.authorization.describe_links.assert_awaited_once_with(directory.account)


# ── One hospital ─────────────────────────────────────────────────────────────


class TestOneHospital:
    async def test_the_reference_goes_to_the_gate_as_it_came(self) -> None:
        entry = _entry()
        directory = _Directory(described=entry, links=[_link(entry.id)])

        hospital = await directory.service.get_hospital(directory.account, "  City-Hospital ")

        directory.gate.describe.assert_awaited_once_with("  City-Hospital ")
        assert (hospital.ref, hospital.linked) == ("city-hospital", True)

    async def test_a_hospital_the_gate_does_not_open_is_not_found(self) -> None:
        directory = _Directory(described=None)

        with pytest.raises(NotFoundError) as raised:
            await directory.service.get_hospital(directory.account, "anything")

        assert raised.value.status_code == 404
        assert str(raised.value.error_code) == "RESOURCE_NOT_FOUND"
        # The framework's own words for a path that matches no route.
        assert raised.value.message == "Not Found"
        assert raised.value.detail == {}
        directory.authorization.describe_links.assert_not_awaited()


# ── The city options ─────────────────────────────────────────────────────────


class TestCities:
    async def test_it_offers_at_most_200_cities_none_longer_than_a_filter_may_be(self) -> None:
        directory = _Directory(cities=["Bengaluru", "Pune"])

        result = await directory.service.list_cities(directory.account)

        assert result.model_dump() == {"cities": ["Bengaluru", "Pune"]}
        assert directory.gate.open_cities.await_args.kwargs == {"max_length": 80, "limit": 200}


# ── What a patient is told ───────────────────────────────────────────────────


class TestWhatIsTold:
    async def test_exactly_the_allow_listed_fields(self) -> None:
        """Attack: read a secret out of the settings, or a column the directory never shows."""
        entry = _entry(
            settings={**OPEN, "smtp_password": "s3cr3t-settings", "feature.ai.x": True},
            address={
                "line1": "1 MG Road",
                "city": "Bengaluru",
                "gps": "12.97,77.59",
                "internal_note": "s3cr3t-address",
                "tax_id": "s3cr3t-tax",
            },
        )

        told = await _Directory().describe(entry)

        assert set(told) == HOSPITAL_FIELDS
        assert set(told["address"]) == ADDRESS_FIELDS
        text = json.dumps(told)
        for secret in (str(entry.id), "s3cr3t", "gps", "12.97", "feature.", "settings"):
            assert secret not in text
        assert told["ref"] == "city-hospital"

    async def test_the_address_keeps_six_named_values_as_trimmed_text(self) -> None:
        told = await _Directory().describe(
            _entry(
                address={
                    "line1": "  1 MG Road  ",
                    "line2": "",
                    "city": " Bengaluru\n",
                    "state": 29,
                    "postal_code": ["560001"],
                    "country": {"code": "IN"},
                }
            )
        )

        assert told["address"] == {
            "line1": "1 MG Road",
            "line2": None,
            "city": "Bengaluru",
            "state": None,
            "postal_code": None,
            "country": None,
        }

    async def test_an_address_value_is_shown_at_200_characters_at_most(self) -> None:
        told = await _Directory().describe(_entry(address={"line1": "a" * 199 + " " + "b" * 50}))

        assert told["address"]["line1"] == "a" * 199

    @pytest.mark.parametrize("stored", [None, [], ["city"], "Bengaluru", 7, True])
    async def test_an_address_that_is_not_an_object_is_six_nulls(self, stored: Any) -> None:
        told = await _Directory().describe(_entry(address=stored))

        assert told["address"] == dict.fromkeys(ADDRESS_FIELDS)

    @pytest.mark.parametrize(
        ("stored", "told"),
        [("+91 80 4000 1234", "+91 80 4000 1234"), ("  080-4000  ", "080-4000"), ("", None)],
    )
    async def test_the_phone_is_trimmed_and_a_blank_one_is_none(
        self, stored: str, told: str | None
    ) -> None:
        assert (await _Directory().describe(_entry(phone=stored)))["phone"] == told
        assert (await _Directory().describe(_entry(phone=None)))["phone"] is None

    @pytest.mark.parametrize(
        ("stored", "told"),
        [
            ("https://cdn.example.com/logo.png", "https://cdn.example.com/logo.png"),
            ("https://cdn.example.com:8443/l.png?v=2", "https://cdn.example.com:8443/l.png?v=2"),
            ("https://cdn.example.com/logo@2x.png", "https://cdn.example.com/logo@2x.png"),
            ("  https://cdn.example.com/logo.png \n", "https://cdn.example.com/logo.png"),
        ],
    )
    async def test_an_absolute_https_logo_url_is_passed_on(self, stored: str, told: str) -> None:
        assert (await _Directory().describe(_entry(logo_url=stored)))["logo_url"] == told

    @pytest.mark.parametrize(
        "stored",
        [
            "javascript:alert(document.cookie)",
            "JaVaScRiPt:alert(1)",
            "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "ftp://cdn.example.com/logo.png",
            "/static/logo.png",
            "logo.png",
            "../../logo.png",
            "//cdn.example.com/logo.png",
            "HTTPS://cdn.example.com/logo.png",
            "https:/cdn.example.com/logo.png",
            "https:\\\\cdn.example.com\\logo.png",
            "https://",
            "https:///logo.png",
            "https://user:secret@cdn.example.com/logo.png",
            "https://token@cdn.example.com/logo.png",
            "https://cdn.example.com/lo go.png",
            "https://cdn.example.com/logo.png\nhttps://evil.example",
            "https://cdn.example.com/\x00logo.png",
            "https://[::1",
            "https://cdn.example.com/" + "a" * 2100,
            "",
            "   ",
            None,
            42,
        ],
    )
    async def test_any_other_logo_url_is_no_logo(self, stored: Any) -> None:
        """Attack: have every patient's browser load a script, or send credentials along."""
        assert (await _Directory().describe(_entry(logo_url=stored)))["logo_url"] is None

    async def test_a_plain_http_logo_is_passed_on_in_development_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local = "http://localhost:9000/logos/city.png"

        monkeypatch.setattr(settings, "APP_ENV", AppEnv.DEVELOPMENT)
        in_development = await _Directory().describe(_entry(logo_url=local))
        told: dict[AppEnv, str | None] = {}
        for environment in AppEnv:
            monkeypatch.setattr(settings, "APP_ENV", environment)
            told[environment] = (await _Directory().describe(_entry(logo_url=local)))["logo_url"]

        assert in_development["logo_url"] == local
        assert {env for env, url in told.items() if url is not None} == {AppEnv.DEVELOPMENT}

    async def test_every_hospital_is_a_standard_listing(self) -> None:
        """Nothing stored can make a hospital "promoted": the data model has no such thing."""
        tempting = _entry(
            settings={**OPEN, "listing": "promoted", "promoted": True, "sponsored": True},
            address={"listing": "promoted"},
        )
        directory = _Directory(entries=[_entry(), tempting])

        page = await directory.discover()

        assert [hospital.listing for hospital in page.items] == [HospitalListing.STANDARD] * 2
        assert (await _Directory().describe(tempting))["listing"] == "standard"
        assert {member.value for member in HospitalListing} == {"standard", "promoted"}


# ── The gate's side: the directory is held to the gate's own rule ────────────


def _gate(hospitals: AsyncMock) -> PatientHospitalGate:
    return PatientHospitalGate(hospitals)


#: The slug rule the gate hands the database: a code, and not an id.
SPELLABLE: dict[str, str] = {
    "slug_like": r"^[a-z0-9][a-z0-9-]{0,99}$",
    "slug_unlike": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
}


class TestTheGateOwnsTheDirectory:
    async def test_the_page_is_asked_for_under_the_patient_app_flag(self) -> None:
        hospitals = AsyncMock()
        entries = [_entry(slug="one"), _entry(slug="two")]
        hospitals.list_directory.return_value = (entries, 12)

        listed, total = await _gate(hospitals).list_open(
            search="heart", city="Pune", skip=20, limit=10
        )

        assert (listed, total) == (entries, 12)
        assert hospitals.list_directory.await_args.args == (PATIENT_APP_ENABLED,)
        assert hospitals.list_directory.await_args.kwargs == {
            "search": "heart",
            "city": "Pune",
            "skip": 20,
            "limit": 10,
            **SPELLABLE,
        }

    @pytest.mark.parametrize(
        "settings_value",
        [{}, {PATIENT_APP_ENABLED: False}, {PATIENT_APP_ENABLED: "true"}, {PATIENT_APP_ENABLED: 1}],
    )
    async def test_a_row_the_gate_would_refuse_is_never_listed(
        self, settings_value: dict[str, Any]
    ) -> None:
        """Even if the query let it through: a listed hospital is one ``resolve`` would find."""
        hospitals = AsyncMock()
        opened = _entry(slug="open")
        hospitals.list_directory.return_value = (
            [_entry(slug="shut", settings=settings_value), opened],
            2,
        )

        listed, _total = await _gate(hospitals).list_open(search=None, city=None, skip=0, limit=20)

        assert listed == [opened]

    @pytest.mark.parametrize(
        "slug",
        ["City_Hospital", "CITY", "city hospital", "-city", "", "c" * 101, str(uuid.uuid4())],
    )
    async def test_a_hospital_whose_code_no_reference_can_spell_is_left_out(
        self, slug: str
    ) -> None:
        hospitals = AsyncMock()
        hospitals.list_directory.return_value = ([_entry(slug=slug)], 1)
        hospitals.get_active_summary.return_value = HospitalSummary(
            id=uuid.uuid4(), name="City Hospital", slug=slug, settings=dict(OPEN)
        )
        hospitals.get_directory_entry.return_value = _entry(slug=slug)
        gate = _gate(hospitals)

        listed, _total = await gate.list_open(search=None, city=None, skip=0, limit=20)

        assert listed == []
        assert await gate.describe(str(uuid.uuid4())) is None

    async def test_one_hospital_is_resolved_first_and_read_by_the_id_the_server_found(
        self,
    ) -> None:
        hospitals = AsyncMock()
        entry = _entry()
        hospitals.get_active_summary.return_value = HospitalSummary(
            id=entry.id, name=entry.name, slug=entry.slug, settings=dict(OPEN)
        )
        hospitals.get_directory_entry.return_value = entry

        assert await _gate(hospitals).describe("  City-Hospital ") is entry

        assert hospitals.get_active_summary.await_args.kwargs == {"slug": "city-hospital"}
        assert hospitals.get_directory_entry.await_args.args == (entry.id, PATIENT_APP_ENABLED)

    @pytest.mark.parametrize("reference", ["", "../etc", "a b", "city;drop", "%", "C" * 101])
    async def test_a_malformed_reference_reads_nothing(self, reference: str) -> None:
        hospitals = AsyncMock()

        assert await _gate(hospitals).describe(reference) is None

        hospitals.get_active_summary.assert_not_awaited()
        hospitals.get_directory_entry.assert_not_awaited()

    async def test_a_hospital_that_does_not_resolve_is_not_read_at_all(self) -> None:
        hospitals = AsyncMock()
        hospitals.get_active_summary.return_value = HospitalSummary(
            id=uuid.uuid4(), name="Shut", slug="shut", settings={PATIENT_APP_ENABLED: False}
        )

        assert await _gate(hospitals).describe("shut") is None

        hospitals.get_directory_entry.assert_not_awaited()

    @pytest.mark.parametrize("second_read", [None, {PATIENT_APP_ENABLED: False}])
    async def test_a_hospital_that_closes_between_the_two_reads_is_not_described(
        self, second_read: dict[str, Any] | None
    ) -> None:
        hospitals = AsyncMock()
        entry = _entry()
        hospitals.get_active_summary.return_value = HospitalSummary(
            id=entry.id, name=entry.name, slug=entry.slug, settings=dict(OPEN)
        )
        hospitals.get_directory_entry.return_value = (
            None if second_read is None else _entry(id=entry.id, settings=second_read)
        )

        assert await _gate(hospitals).describe("city-hospital") is None

    async def test_the_cities_are_asked_for_under_the_patient_app_flag(self) -> None:
        hospitals = AsyncMock()
        hospitals.list_directory_cities.return_value = ["Bengaluru", "Pune"]

        cities = await _gate(hospitals).open_cities(max_length=80, limit=200)

        assert cities == ["Bengaluru", "Pune"]
        assert hospitals.list_directory_cities.await_args.args == (PATIENT_APP_ENABLED,)
        assert hospitals.list_directory_cities.await_args.kwargs == {
            "max_length": 80,
            "limit": 200,
            **SPELLABLE,
        }

    @pytest.mark.parametrize(
        "slug",
        ["City_Hospital", "CITY", "city hospital", "-city", "", "c" * 101, str(uuid.uuid4())],
    )
    def test_the_database_is_given_the_rule_a_reference_is_read_by(self, slug: str) -> None:
        """The page, its total and the cities are counted under ``resolve``'s own patterns."""
        like, unlike = SPELLABLE["slug_like"], SPELLABLE["slug_unlike"]
        hospital = HospitalSummary(id=uuid.uuid4(), name="City", slug=slug, settings=dict(OPEN))

        assert re.fullmatch(like, "city-hospital-2")
        assert not re.fullmatch(unlike, "city-hospital-2")
        assert not re.fullmatch(like, slug) or re.fullmatch(unlike, slug)
        assert PatientHospitalGate.public_ref(hospital) is None

    def test_a_code_a_reference_can_spell_is_the_public_reference(self) -> None:
        hospital = HospitalSummary(
            id=uuid.uuid4(), name="City", slug="city-hospital-2", settings=dict(OPEN)
        )

        assert PatientHospitalGate.public_ref(hospital) == "city-hospital-2"
