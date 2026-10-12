"""Migration 0019 — staff email becomes a platform-wide identity.

These tests run the real Alembic migrations against **scratch databases** that
each test creates and drops. Nothing here touches the suite's own test
database beyond issuing ``CREATE DATABASE`` / ``DROP DATABASE`` for a uniquely
named scratch one, and nothing touches any other database on the server.

Rows are inserted with plain SQL, so the application's own lower-casing cannot
mask what the migration has to cope with: data written before the rule existed.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.tests.conftest import _test_database_url

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.database

SCRATCH_PREFIX = "aetheris_scratch_mig_"
INDEX = "uq_users_email_normalized_active"
BEFORE = "0018"
#: The revision under test. Later migrations have their own tests.
HEAD = "0019"


def _alembic(database_url: str, action: str, revision: str) -> None:
    """Run one Alembic command against exactly ``database_url``.

    ``migrations/env.py`` prefers ``DB_URL`` from the environment, so it is set
    explicitly for the call and restored afterwards — the target is never left
    to ``alembic.ini`` or to whatever the shell happens to hold.
    """
    from alembic import command
    from alembic.config import Config

    assert f"/{SCRATCH_PREFIX}" in database_url, "refusing to migrate a non-scratch database"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    previous = os.environ.get("DB_URL")
    os.environ["DB_URL"] = database_url
    try:
        getattr(command, action)(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DB_URL", None)
        else:
            os.environ["DB_URL"] = previous


async def _migrate(database_url: str, action: str, revision: str) -> None:
    # Alembic's env.py uses asyncio.run, which cannot nest in a running loop.
    await asyncio.to_thread(_alembic, database_url, action, revision)


@pytest_asyncio.fixture
async def scratch() -> AsyncGenerator[tuple[str, AsyncEngine]]:
    """A brand-new, empty database, dropped again when the test ends."""
    server_url = _test_database_url()
    name = f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:16]}"
    assert name.startswith(SCRATCH_PREFIX)
    admin = create_async_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 — no permission or no server: skip, do not fail
        await admin.dispose()
        pytest.skip(f"cannot create a scratch database: {exc}")

    url = f"{server_url.rpartition('/')[0]}/{name}"
    engine = create_async_engine(url)
    try:
        yield url, engine
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def _hospital(engine: AsyncEngine, slug: str) -> uuid.UUID:
    hospital_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO hospitals (id, name, slug, address) "
                "VALUES (:id, :name, :slug, '{}'::jsonb)"
            ),
            {"id": hospital_id, "name": slug, "slug": slug},
        )
    return hospital_id


async def _user(
    engine: AsyncEngine, hospital_id: uuid.UUID, email: str, *, deleted: bool = False
) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, hospital_id, email, password_hash, first_name, last_name, "
                "deleted_at) VALUES (:id, :hid, :email, 'x', 'A', 'B', "
                "CASE WHEN :deleted THEN now() ELSE NULL END)"
            ),
            {"id": user_id, "hid": hospital_id, "email": email, "deleted": deleted},
        )
    return user_id


async def _scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar_one_or_none()


async def _revision(engine: AsyncEngine) -> str:
    return str(await _scalar(engine, "SELECT version_num FROM alembic_version"))


async def _index_exists(engine: AsyncEngine) -> bool:
    return bool(
        await _scalar(engine, "SELECT count(*) FROM pg_indexes WHERE indexname = :name", name=INDEX)
    )


async def _email_of(engine: AsyncEngine, user_id: uuid.UUID) -> str:
    return str(await _scalar(engine, "SELECT email FROM users WHERE id = :id", id=user_id))


class TestFreshDatabase:
    async def test_an_empty_database_migrates_from_the_first_revision_to_the_latest(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch

        await _migrate(url, "upgrade", HEAD)

        assert await _revision(engine) == HEAD
        assert await _index_exists(engine)
        definition = str(
            await _scalar(engine, "SELECT indexdef FROM pg_indexes WHERE indexname = :n", n=INDEX)
        )
        assert "UNIQUE" in definition
        assert "lower((email)::text)" in definition
        assert "deleted_at IS NULL" in definition
        # The older per-hospital constraint is kept.
        assert await _scalar(
            engine, "SELECT count(*) FROM pg_constraint WHERE conname = 'uq_users_hospital_email'"
        )


class TestExistingCleanData:
    async def test_live_addresses_are_lower_cased(self, scratch: tuple[str, AsyncEngine]) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        hospital = await _hospital(engine, "a")
        mixed = await _user(engine, hospital, "Asha.Rao@Hospital.Example")
        upper = await _user(engine, hospital, "RAVI@HOSPITAL.EXAMPLE")
        already = await _user(engine, hospital, "meera@hospital.example")

        await _migrate(url, "upgrade", HEAD)

        assert await _revision(engine) == HEAD
        assert await _email_of(engine, mixed) == "asha.rao@hospital.example"
        assert await _email_of(engine, upper) == "ravi@hospital.example"
        assert await _email_of(engine, already) == "meera@hospital.example"
        assert await _index_exists(engine)

    async def test_soft_deleted_users_are_left_exactly_as_they_were(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        hospital = await _hospital(engine, "a")
        gone = await _user(engine, hospital, "Gone.Person@Hospital.Example", deleted=True)

        await _migrate(url, "upgrade", HEAD)

        assert await _email_of(engine, gone) == "Gone.Person@Hospital.Example"

    async def test_the_same_address_live_in_one_hospital_and_deleted_in_another_is_fine(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        first, second = await _hospital(engine, "a"), await _hospital(engine, "b")
        await _user(engine, first, "shared@hospital.example", deleted=True)
        live = await _user(engine, second, "Shared@Hospital.Example")

        await _migrate(url, "upgrade", HEAD)

        assert await _revision(engine) == HEAD
        assert await _email_of(engine, live) == "shared@hospital.example"


class TestDuplicatesAreRefused:
    async def _assert_untouched(self, engine: AsyncEngine, expected: dict[uuid.UUID, str]) -> None:
        """The refusal left the revision, the index and every address as they were."""
        assert await _revision(engine) == BEFORE
        assert not await _index_exists(engine)
        for user_id, email in expected.items():
            assert await _email_of(engine, user_id) == email

    async def test_the_same_email_in_two_hospitals_stops_the_migration(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        first, second = await _hospital(engine, "a"), await _hospital(engine, "b")
        one = await _user(engine, first, "admin@hospital.example")
        two = await _user(engine, second, "admin@hospital.example")
        bystander = await _user(engine, first, "Mixed.Case@Hospital.Example")

        with pytest.raises(RuntimeError, match="Cannot make staff email unique") as raised:
            await _migrate(url, "upgrade", HEAD)

        # Nothing was partially applied — not even the lower-casing of an
        # unrelated, perfectly valid row.
        await self._assert_untouched(
            engine,
            {
                one: "admin@hospital.example",
                two: "admin@hospital.example",
                bystander: "Mixed.Case@Hospital.Example",
            },
        )
        # The operator is told which accounts, by id, and never the address.
        message = str(raised.value)
        assert str(one) in message
        assert str(two) in message
        assert "admin@hospital.example" not in message
        assert "Nothing was changed" in message

    async def test_addresses_that_differ_only_in_case_stop_the_migration(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        first, second = await _hospital(engine, "a"), await _hospital(engine, "b")
        one = await _user(engine, first, "Dr.Rao@Hospital.Example")
        two = await _user(engine, second, "dr.rao@hospital.example")

        with pytest.raises(RuntimeError, match="1 conflict"):
            await _migrate(url, "upgrade", HEAD)

        await self._assert_untouched(
            engine, {one: "Dr.Rao@Hospital.Example", two: "dr.rao@hospital.example"}
        )

    async def test_case_twins_inside_one_hospital_stop_the_migration(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        hospital = await _hospital(engine, "a")
        one = await _user(engine, hospital, "Reception@Hospital.Example")
        two = await _user(engine, hospital, "reception@hospital.example")

        with pytest.raises(RuntimeError, match="Cannot make staff email unique"):
            await _migrate(url, "upgrade", HEAD)

        await self._assert_untouched(
            engine, {one: "Reception@Hospital.Example", two: "reception@hospital.example"}
        )

    async def test_no_winner_is_chosen_and_nobody_is_deleted(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        first, second = await _hospital(engine, "a"), await _hospital(engine, "b")
        await _user(engine, first, "admin@hospital.example")
        await _user(engine, second, "admin@hospital.example")

        with pytest.raises(RuntimeError):
            await _migrate(url, "upgrade", HEAD)

        assert await _scalar(engine, "SELECT count(*) FROM users") == 2
        assert await _scalar(engine, "SELECT count(*) FROM users WHERE deleted_at IS NOT NULL") == 0

    async def test_a_deleted_twin_in_the_same_hospital_is_reported_not_crashed_into(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        """Lower-casing would collide with the older per-hospital constraint."""
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        hospital = await _hospital(engine, "a")
        live = await _user(engine, hospital, "Nurse@Hospital.Example")
        gone = await _user(engine, hospital, "nurse@hospital.example", deleted=True)

        with pytest.raises(RuntimeError, match="cannot be lower-cased") as raised:
            await _migrate(url, "upgrade", HEAD)

        await self._assert_untouched(
            engine, {live: "Nurse@Hospital.Example", gone: "nurse@hospital.example"}
        )
        assert str(live) in str(raised.value)
        assert str(gone) in str(raised.value)

    async def test_once_the_conflict_is_resolved_the_migration_runs(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        first, second = await _hospital(engine, "a"), await _hospital(engine, "b")
        await _user(engine, first, "admin@hospital.example")
        two = await _user(engine, second, "Admin@Hospital.Example")
        with pytest.raises(RuntimeError):
            await _migrate(url, "upgrade", HEAD)

        async with engine.begin() as connection:  # the operator's decision
            await connection.execute(
                text("UPDATE users SET email = 'admin.b@hospital.example' WHERE id = :id"),
                {"id": two},
            )
        await _migrate(url, "upgrade", HEAD)

        assert await _revision(engine) == HEAD
        assert await _index_exists(engine)


class TestUniquenessAfterMigration:
    async def test_a_second_live_account_with_the_same_address_is_refused(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", HEAD)
        first, second = await _hospital(engine, "a"), await _hospital(engine, "b")
        await _user(engine, first, "admin@hospital.example")

        for variant in (
            "admin@hospital.example",
            "Admin@Hospital.Example",
            "ADMIN@HOSPITAL.EXAMPLE",
        ):
            with pytest.raises(IntegrityError, match=INDEX):
                await _user(engine, second, variant)

        assert await _scalar(engine, "SELECT count(*) FROM users") == 1

    async def test_a_deleted_account_does_not_block_a_replacement_elsewhere(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", HEAD)
        first, second = await _hospital(engine, "a"), await _hospital(engine, "b")
        await _user(engine, first, "former@hospital.example", deleted=True)

        replacement = await _user(engine, second, "former@hospital.example")

        assert await _email_of(engine, replacement) == "former@hospital.example"

    async def test_a_deleted_account_still_holds_its_address_inside_its_own_hospital(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        """The kept per-hospital constraint is not partial. Documented, not changed."""
        url, engine = scratch
        await _migrate(url, "upgrade", HEAD)
        hospital = await _hospital(engine, "a")
        await _user(engine, hospital, "former@hospital.example", deleted=True)

        with pytest.raises(IntegrityError, match="uq_users_hospital_email"):
            await _user(engine, hospital, "former@hospital.example")


class TestDowngrade:
    async def test_downgrade_removes_only_the_index(self, scratch: tuple[str, AsyncEngine]) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        hospital = await _hospital(engine, "a")
        user = await _user(engine, hospital, "Mixed@Hospital.Example")
        await _migrate(url, "upgrade", HEAD)

        await _migrate(url, "downgrade", BEFORE)

        assert await _revision(engine) == BEFORE
        assert not await _index_exists(engine)
        assert await _scalar(engine, "SELECT count(*) FROM users") == 1
        # Letter case is not restored; nothing depends on it.
        assert await _email_of(engine, user) == "mixed@hospital.example"
