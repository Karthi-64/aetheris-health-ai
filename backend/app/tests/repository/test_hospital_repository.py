"""Repository tests for the hospital directory queries of :class:`HospitalRepository`.

Run against a real PostgreSQL because what is under test is the SQL: which
stored flag values count as "on", how a city is read out of the address JSON,
literal matching, ordering and paging. None of that is observable through a
mock.

``hospitals`` is the tenant root — it has no ``hospital_id`` — so there is no
tenant filter to prove here. What stands in its place is the flag filter: the
directory must never return a hospital the Patient App gate would refuse.
"""

from __future__ import annotations

import re
import sys
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.core.feature_flags import PATIENT_APP_ENABLED, flag_is_on
from app.models.hospital import Hospital
from app.repositories.hospital_repository import (
    WHITESPACE,
    HospitalDirectoryEntry,
    HospitalRepository,
)
from app.services.patient_app.hospital_gate import PatientHospitalGate

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

FLAG = PATIENT_APP_ENABLED
#: A flag other than the Patient App's: the repository is told which flag to read.
OTHER_FLAG = "feature.test.other"


@pytest.fixture
def repository(db_session: AsyncSession) -> HospitalRepository:
    """A repository bound to the rolled-back test session."""
    return HospitalRepository(db_session)


@pytest.fixture
def tag() -> str:
    """A word no other hospital's name or city contains."""
    return f"r{uuid.uuid4().hex[:11]}"


async def _insert(session: AsyncSession, **overrides: Any) -> Hospital:
    """Insert a hospital with the flag on, overridable per test."""
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": f"Hospital {uuid.uuid4().hex[:8]}",
        "slug": f"h-{uuid.uuid4().hex[:16]}",
        "address": {"city": "Hyderabad"},
        "settings": {FLAG: True},
    }
    values.update(overrides)
    hospital = Hospital(**values)
    session.add(hospital)
    await session.flush()
    return hospital


async def _slugs(repository: HospitalRepository, flag: str = FLAG, **filters: Any) -> list[str]:
    entries, total = await repository.list_directory(flag, limit=50, **filters)
    assert total == len(entries)
    return [entry.slug for entry in entries]


class TestWhichHospitalsAreInTheDirectory:
    @pytest.mark.parametrize(
        "stored",
        [
            {FLAG: False},
            {},
            {FLAG: None},
            {FLAG: "true"},
            {FLAG: "True"},
            {FLAG: 1},
            {FLAG: 1.0},
            {FLAG: "1"},
            {FLAG: [True]},
            {FLAG: {"enabled": True}},
            {FLAG.upper(): True},
            {"feature": {"patient_app": {"enabled": True}}},
            {OTHER_FLAG: True},
            [FLAG, True],
            "true",
            True,
        ],
    )
    async def test_only_a_flag_stored_as_exactly_true_counts(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str, stored: Any
    ) -> None:
        """Attack: a row that merely holds something truthy switches a hospital on."""
        shut = await _insert(db_session, name=f"{tag} Shut", settings=stored)
        opened = await _insert(db_session, name=f"{tag} Open")

        assert await _slugs(repository, search=tag) == [opened.slug]
        assert await repository.get_directory_entry(shut.id, FLAG) is None
        assert await repository.get_directory_entry(opened.id, FLAG) is not None

    async def test_an_inactive_hospital_is_not_in_it_even_with_the_flag_on(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        inactive = await _insert(
            db_session, name=f"{tag} Closed", address={"city": f"{tag}ville"}, is_active=False
        )

        assert await _slugs(repository, search=tag) == []
        assert await _slugs(repository, city=f"{tag}ville") == []
        assert await repository.get_directory_entry(inactive.id, FLAG) is None
        assert f"{tag}ville" not in await repository.list_directory_cities(
            FLAG, max_length=80, limit=200
        )

    async def test_the_flag_is_the_one_the_caller_names(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        patients = await _insert(db_session, name=f"{tag} A", settings={FLAG: True})
        other = await _insert(db_session, name=f"{tag} B", settings={OTHER_FLAG: True})

        assert await _slugs(repository, FLAG, search=tag) == [patients.slug]
        assert await _slugs(repository, OTHER_FLAG, search=tag) == [other.slug]

    async def test_the_query_and_the_gate_give_one_answer_for_every_stored_value(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        """The directory's filter is SQL; the gate's rule is Python. They must be one rule."""
        values: list[Any] = [True, False, None, "true", "false", 1, 0, 1.0, "", [], {}, [True]]
        hospitals = [
            await _insert(
                db_session,
                name=f"{tag} {index}",
                slug=f"{tag}-{index}",
                settings={FLAG: value},
                is_active=active,
            )
            for index, (value, active) in enumerate(
                [(value, active) for value in values for active in (True, False)]
            )
        ]
        gate = PatientHospitalGate(repository)

        by_the_query = set(await _slugs(repository, search=tag))
        by_the_gate = {
            hospital.slug for hospital in hospitals if await gate.resolve(hospital.slug) is not None
        }
        by_the_rule = {
            hospital.slug
            for hospital in hospitals
            if hospital.is_active and flag_is_on(hospital.settings, FLAG)
        }

        assert by_the_query == by_the_gate == by_the_rule == {f"{tag}-0"}
        listed, total = await gate.list_open(search=tag, city=None, skip=0, limit=50)
        assert ([entry.slug for entry in listed], total) == ([f"{tag}-0"], 1)

    @pytest.mark.parametrize("stored", [[FLAG], "settings", 7, True])
    async def test_settings_that_are_not_an_object_close_a_hospital_without_an_error(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str, stored: Any
    ) -> None:
        hospital = await _insert(db_session, name=f"{tag} Odd", settings=stored)
        gate = PatientHospitalGate(repository)

        summary = await repository.get_active_summary(id=hospital.id)

        assert summary is not None
        assert summary.settings == {}
        assert await gate.resolve(hospital.slug) is None
        assert await gate.describe(str(hospital.id)) is None


class TestAnEntry:
    async def test_it_carries_the_directory_columns_as_stored(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await _insert(
            db_session,
            name=f"{tag} City Hospital",
            address={"line1": "1 MG Road", "city": "Bengaluru", "note": "kept as stored"},
            phone="080 4000 1234",
            logo_url="https://cdn.example.com/city.png",
            timezone="Asia/Kolkata",
            email="front-desk@hospital.test",
            tax_id="TAX-123",
            settings={FLAG: True, "hours": "9-5"},
        )

        entry = await repository.get_directory_entry(hospital.id, FLAG)
        [listed] = (await repository.list_directory(FLAG, search=tag))[0]

        assert entry == listed
        assert entry == HospitalDirectoryEntry(
            id=hospital.id,
            name=f"{tag} City Hospital",
            slug=hospital.slug,
            settings={FLAG: True, "hours": "9-5"},
            address={"line1": "1 MG Road", "city": "Bengaluru", "note": "kept as stored"},
            phone="080 4000 1234",
            logo_url="https://cdn.example.com/city.png",
            timezone="Asia/Kolkata",
        )
        # Columns, not the entity: there is nowhere for an e-mail or a tax id to be.
        assert not {"email", "tax_id", "currency", "locale", "is_active"} & set(entry.__slots__)

    async def test_an_unknown_id_is_none(self, repository: HospitalRepository) -> None:
        assert await repository.get_directory_entry(uuid.uuid4(), FLAG) is None


class TestSearchAndOrder:
    async def test_search_is_a_literal_substring_of_the_name_in_any_case(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        percent = await _insert(db_session, name=f"{tag} 100% Care")
        underscore = await _insert(db_session, name=f"{tag} under_score")
        backslash = await _insert(db_session, name=f"{tag} back\\slash")

        assert await _slugs(repository, search=f"{tag.upper()} 100% c") == [percent.slug]
        assert await _slugs(repository, search=f"{tag} under_") == [underscore.slug]
        assert await _slugs(repository, search=f"{tag} back\\s") == [backslash.slug]
        assert await _slugs(repository, search=f"{tag} 1%") == []
        assert await _slugs(repository, search=f"{tag} under_core") == []
        assert await _slugs(repository, search=f"{tag}%") == []
        assert await _slugs(repository, search=f"{tag}_") == []

    async def test_the_order_is_name_without_case_then_slug_across_pages(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        for name, letter in (("Zeta", "a"), ("alpha", "d"), ("ALPHA", "b"), ("Alpha", "c")):
            await _insert(db_session, name=f"{tag} {name}", slug=f"{tag}-{letter}")
        expected = [f"{tag}-{letter}" for letter in "bcda"]

        assert await _slugs(repository, search=tag) == expected
        paged: list[str] = []
        for skip in range(4):
            entries, total = await repository.list_directory(FLAG, search=tag, skip=skip, limit=1)
            assert total == 4
            paged.extend(entry.slug for entry in entries)
        assert paged == expected
        assert await repository.list_directory(FLAG, search=tag, skip=4, limit=10) == ([], 4)


class TestCities:
    def test_a_city_is_trimmed_of_exactly_what_python_strips(self) -> None:
        """One meaning of "trimmed", in the query and in the service."""
        every = "".join(char for char in map(chr, range(sys.maxunicode + 1)) if char.isspace())

        assert every == WHITESPACE
        assert (WHITESPACE + "Pune" + WHITESPACE).strip() == "Pune"

    async def test_the_filter_reads_the_trimmed_text_city_without_regard_to_case(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        city = f"{tag}pur"
        exact = await _insert(db_session, name="A", address={"city": city})
        padded = await _insert(db_session, name="B", address={"city": f" {city.upper()}\t\n"})
        await _insert(db_session, name="C", address={"city": f"{city}a"})
        await _insert(db_session, name="D", address={"city": [city]})
        await _insert(db_session, name="E", address=[city])
        await _insert(db_session, name="F", address={"town": city})

        assert await _slugs(repository, city=city) == [exact.slug, padded.slug]
        assert await _slugs(repository, city=city.upper()) == [exact.slug, padded.slug]
        assert await _slugs(repository, city=tag) == []
        assert await _slugs(repository, city=f"{tag}%") == []

    async def test_a_number_stored_as_the_city_is_not_a_city(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        number = int(uuid.uuid4().int % 10**12) + 10**12
        await _insert(db_session, name=f"{tag} N", address={"city": number})

        assert await _slugs(repository, city=str(number)) == []
        assert str(number) not in await repository.list_directory_cities(
            FLAG, max_length=80, limit=200
        )

    async def test_the_cities_are_distinct_sorted_bounded_and_of_listed_hospitals_only(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        for city in (f"{tag}c", f" {tag}a ", f"{tag}A", f"{tag}b", "", "   ", "x" * 81):
            await _insert(db_session, address={"city": city})
        await _insert(db_session, address={"city": f"{tag}shut"}, settings={FLAG: False})
        await _insert(db_session, address={"city": f"{tag}gone"}, is_active=False)

        cities = await repository.list_directory_cities(FLAG, max_length=80, limit=200)
        ours = [city for city in cities if city.lower().startswith(tag)]

        assert [city.lower() for city in ours] == [f"{tag}a", f"{tag}b", f"{tag}c"]
        assert all(city == city.strip() and 0 < len(city) <= 80 for city in cities)
        assert len({city.lower() for city in cities}) == len(cities)

    async def test_no_more_cities_than_the_limit_and_none_longer_than_asked(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        for index in range(5):
            await _insert(db_session, address={"city": f"{tag}{index}"})
        await _insert(db_session, address={"city": f"{tag}long-name"})

        everything = await repository.list_directory_cities(FLAG, max_length=80, limit=200)
        short = await repository.list_directory_cities(FLAG, max_length=len(tag) + 1, limit=200)
        few = await repository.list_directory_cities(FLAG, max_length=80, limit=3)

        assert f"{tag}long-name" in everything
        assert [city for city in short if city.startswith(tag)] == [
            f"{tag}{index}" for index in range(5)
        ]
        assert few == everything[:3]


# ── The slug rule: which codes the caller wants listed ───────────────────────

#: The patterns the Patient App gate passes, restated: a code, and not an id.
CODE = r"^[a-z0-9][a-z0-9-]{0,99}$"
ID_SHAPED = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

UNSPELLABLE = [
    "{tag}-Upper",
    "{tag}_under",
    "{tag} space",
    "{tag}.dot",
    "{tag}/slash",
    "{tag}%",
    "{tag}-caf\u00e9",
    "{tag}-\u212a",  # KELVIN SIGN: lower-cases to "k", but is not "k"
    "{tag}-\uff41",  # FULLWIDTH a
    "{tag}-\u0661",  # ARABIC-INDIC DIGIT ONE
    "-{tag}",
    " {tag}",
    "{tag}\n",
    "{uuid}",
]


class TestTheSlugRule:
    @pytest.mark.parametrize("shape", UNSPELLABLE)
    async def test_a_slug_outside_the_rule_is_in_no_page_no_total_and_no_city(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str, shape: str
    ) -> None:
        """The database reads the patterns exactly as Python does — ranges by code point."""
        slug = shape.format(tag=tag, uuid=uuid.uuid4())
        kept = await _insert(db_session, name=f"{tag} Kept", slug=f"{tag}-kept")
        await _insert(db_session, name=f"{tag} Odd", slug=slug, address={"city": f"{tag}ville"})
        in_python = re.fullmatch(CODE, slug) and not re.fullmatch(ID_SHAPED, slug)

        entries, total = await repository.list_directory(
            FLAG, search=tag, slug_like=CODE, slug_unlike=ID_SHAPED
        )
        paged = [
            await repository.list_directory(
                FLAG, search=tag, slug_like=CODE, slug_unlike=ID_SHAPED, skip=skip, limit=1
            )
            for skip in range(2)
        ]
        cities = await repository.list_directory_cities(
            FLAG, max_length=80, limit=200, slug_like=CODE, slug_unlike=ID_SHAPED
        )

        assert not in_python
        assert ([entry.slug for entry in entries], total) == ([kept.slug], 1)
        assert [([entry.slug for entry in page], count) for page, count in paged] == [
            ([kept.slug], 1),
            ([], 1),
        ]
        assert f"{tag}ville" not in cities

    async def test_without_a_rule_every_slug_is_listed(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        """The rule is the caller's: this repository has none of its own."""
        await _insert(db_session, name=f"{tag} A", slug=f"{tag}-Upper", address={"city": tag})

        assert await _slugs(repository, search=tag) == [f"{tag}-Upper"]
        assert tag in await repository.list_directory_cities(FLAG, max_length=80, limit=200)

    async def test_each_half_of_the_rule_works_alone(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str
    ) -> None:
        shaped = str(uuid.uuid4())
        await _insert(db_session, name=f"{tag} A", slug=f"{tag}-plain")
        await _insert(db_session, name=f"{tag} B", slug=f"{tag}-Upper")
        await _insert(db_session, name=f"{tag} C", slug=shaped)

        assert await _slugs(repository, search=tag, slug_like=CODE) == [f"{tag}-plain", shaped]
        assert await _slugs(repository, search=tag, slug_unlike=ID_SHAPED) == [
            f"{tag}-plain",
            f"{tag}-Upper",
        ]

    @pytest.mark.parametrize("length", [1, 2, 99, 100])
    async def test_a_code_of_every_length_the_column_holds_is_kept(
        self, repository: HospitalRepository, db_session: AsyncSession, tag: str, length: int
    ) -> None:
        slug = uuid.uuid4().hex[:1] if length == 1 else (tag + "z" * 100)[:length]
        existing = await repository.get_active_summary(slug=slug)
        if existing is not None:  # a one-character code may already be taken
            pytest.skip("slug in use")
        await _insert(db_session, name=f"{tag} Long", slug=slug)

        assert await _slugs(repository, search=tag, slug_like=CODE, slug_unlike=ID_SHAPED) == [slug]
