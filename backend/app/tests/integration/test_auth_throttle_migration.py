"""Migrations 0020 and 0021 — throttle buckets and trusted devices.

Run against scratch databases that each test creates and drops (the harness in
``test_staff_email_migration.py``). They check that the tables the models
describe are the tables the migrations build, that they come and go cleanly,
and that the old lockout columns — and whatever they hold — are left alone.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.tests.integration.test_staff_email_migration import (
    _hospital,
    _migrate,
    _user,
    scratch,  # noqa: F401 — pytest fixture, used by name
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.database

BEFORE = "0019"
BUCKETS = "0020"
DEVICES = "0021"


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return set(result.scalars().all())


async def _columns(engine: AsyncEngine, table: str) -> dict[str, tuple[str, str]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table"
            ),
            {"table": table},
        )
        return {name: (kind, nullable) for name, kind, nullable in result.all()}


class TestUpgrade:
    async def test_both_tables_are_created(self, scratch: tuple[str, AsyncEngine]) -> None:  # noqa: F811
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        assert not {"auth_throttle_buckets", "trusted_devices"} & await _tables(engine)

        await _migrate(url, "upgrade", DEVICES)

        assert {"auth_throttle_buckets", "trusted_devices"} <= await _tables(engine)

    async def test_the_migrated_tables_match_the_models(
        self,
        scratch: tuple[str, AsyncEngine],  # noqa: F811
    ) -> None:
        """A column the model has and the table lacks would fail every sign-in."""
        from app.models.auth_throttle import AuthThrottleBucket, TrustedDevice

        url, engine = scratch
        await _migrate(url, "upgrade", DEVICES)

        for model in (AuthThrottleBucket, TrustedDevice):
            migrated = await _columns(engine, model.__tablename__)
            declared = {column.name: column for column in model.__table__.columns}
            assert set(migrated) == set(declared), model.__tablename__
            for name, column in declared.items():
                assert (migrated[name][1] == "YES") is bool(column.nullable), (
                    f"{model.__tablename__}.{name} nullability differs"
                )

    async def test_neither_table_carries_tenant_or_personal_columns(
        self,
        scratch: tuple[str, AsyncEngine],  # noqa: F811
    ) -> None:
        """A bucket is a hash and counters: no email, address, user or hospital."""
        url, engine = scratch
        await _migrate(url, "upgrade", DEVICES)

        buckets = set(await _columns(engine, "auth_throttle_buckets"))
        assert buckets == {
            "id",
            "key_hash",
            "kind",
            "failures",
            "blocked_until",
            "last_charged_at",
            "drains_at",
            "expires_at",
        }
        devices = set(await _columns(engine, "trusted_devices"))
        assert "token" not in devices, "only the hash of a device token is stored"
        assert "token_hash" in devices

    async def test_a_bucket_key_is_unique(self, scratch: tuple[str, AsyncEngine]) -> None:  # noqa: F811
        """Two rows for one key would be two allowances for one attacker."""
        url, engine = scratch
        await _migrate(url, "upgrade", DEVICES)
        insert = text(
            "INSERT INTO auth_throttle_buckets (key_hash, kind, expires_at) "
            "VALUES (:key, 'pw_account', now())"
        )
        async with engine.begin() as connection:
            await connection.execute(insert, {"key": "k" * 64})

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(insert, {"key": "k" * 64})

    async def test_a_device_token_is_unique_and_dies_with_its_user(
        self,
        scratch: tuple[str, AsyncEngine],  # noqa: F811
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", DEVICES)
        hospital_id = await _hospital(engine, "h-one")
        user_id = await _user(engine, hospital_id, "a@example.test")
        other_id = await _user(engine, hospital_id, "b@example.test")
        insert = text(
            "INSERT INTO trusted_devices (user_id, token_hash, expires_at) "
            "VALUES (:user, :hash, now() + interval '1 day')"
        )
        async with engine.begin() as connection:
            await connection.execute(insert, {"user": user_id, "hash": "t" * 64})

        # One token never stands for two accounts.
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(insert, {"user": other_id, "hash": "t" * 64})

        # A device row cannot name a user that does not exist.
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(insert, {"user": uuid.uuid4(), "hash": "u" * 64})

        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        async with engine.connect() as connection:
            left = await connection.execute(text("SELECT count(*) FROM trusted_devices"))
            assert left.scalar_one() == 0


class TestExistingDataIsLeftAlone:
    async def test_the_old_lockout_columns_and_their_values_survive(
        self,
        scratch: tuple[str, AsyncEngine],  # noqa: F811
    ) -> None:
        """The columns stay so a rollback has a schema to return to; nothing rewrites them."""
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        hospital_id = await _hospital(engine, "h-one")
        user_id = await _user(engine, hospital_id, "locked@example.test")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE users SET failed_login_attempts = 5, "
                    "locked_until = now() + interval '30 minutes' WHERE id = :id"
                ),
                {"id": user_id},
            )

        await _migrate(url, "upgrade", DEVICES)

        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT failed_login_attempts, locked_until IS NOT NULL "
                        "FROM users WHERE id = :id"
                    ),
                    {"id": user_id},
                )
            ).one()
        assert tuple(row) == (5, True)


class TestDowngrade:
    async def test_downgrade_removes_the_tables_and_nothing_else(
        self,
        scratch: tuple[str, AsyncEngine],  # noqa: F811
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        before = await _tables(engine)
        hospital_id = await _hospital(engine, "h-one")
        user_id = await _user(engine, hospital_id, "a@example.test")

        await _migrate(url, "upgrade", DEVICES)
        await _migrate(url, "downgrade", BUCKETS)
        assert "trusted_devices" not in await _tables(engine)
        assert "auth_throttle_buckets" in await _tables(engine)

        await _migrate(url, "downgrade", BEFORE)
        assert await _tables(engine) == before

        async with engine.connect() as connection:
            kept = await connection.execute(
                text("SELECT count(*) FROM users WHERE id = :id"), {"id": user_id}
            )
            assert kept.scalar_one() == 1

    async def test_it_can_be_applied_again_after_a_downgrade(
        self,
        scratch: tuple[str, AsyncEngine],  # noqa: F811
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", DEVICES)
        await _migrate(url, "downgrade", BEFORE)
        await _migrate(url, "upgrade", DEVICES)

        assert {"auth_throttle_buckets", "trusted_devices"} <= await _tables(engine)
