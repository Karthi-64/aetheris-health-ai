"""Migrations 0022–0025 — the Patient App tables and the patient audit actor.

Run against scratch databases that each test creates and drops (the harness in
``test_staff_email_migration.py``). They check that the tables the models
describe are the tables the migrations build, that the rules that must hold
under concurrency are real constraints, and that every step comes and goes
cleanly without touching anything that was there before.
"""

# ruff: noqa: F811 — the imported ``scratch`` fixture is requested by name in every test

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401 — registers every mapper
from app.database import Base
from app.tests.integration.test_staff_email_migration import (
    _hospital,
    _migrate,
    scratch,  # noqa: F401 — pytest fixture, used by name
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.database

BEFORE = "0021"
IDENTITY = "0022"
LINKS = "0023"
CONSENT = "0024"
AUDIT = "0025"

IDENTITY_TABLES = {
    "patient_accounts",
    "patient_otp_challenges",
    "patient_refresh_tokens",
    "patient_devices",
}
PATIENT_TABLES = IDENTITY_TABLES | {
    "patient_account_links",
    "patient_consent_records",
    "patient_access_grants",
}


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return set(result.scalars().all())


async def _columns(engine: AsyncEngine, table: str) -> dict[str, str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table"
            ),
            {"table": table},
        )
        return {name: nullable for name, nullable in result.all()}


async def _execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(sql), params)


async def _account(engine: AsyncEngine, phone: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    await _execute(
        engine,
        "INSERT INTO patient_accounts (id, phone) VALUES (:id, :phone)",
        id=account_id,
        phone=phone,
    )
    return account_id


async def _patient(engine: AsyncEngine, hospital_id: uuid.UUID) -> uuid.UUID:
    patient_id = uuid.uuid4()
    await _execute(
        engine,
        "INSERT INTO patients (id, hospital_id, mrn, first_name, last_name, date_of_birth, gender) "
        "VALUES (:id, :hid, :mrn, 'A', 'B', '1990-01-01', 'female')",
        id=patient_id,
        hid=hospital_id,
        mrn=f"MRN-{patient_id.hex[:10]}",
    )
    return patient_id


async def _link(
    engine: AsyncEngine, account_id: uuid.UUID, patient_id: uuid.UUID, hospital_id: uuid.UUID
) -> uuid.UUID:
    link_id = uuid.uuid4()
    await _execute(
        engine,
        "INSERT INTO patient_account_links (id, account_id, patient_id, hospital_id, verified_via) "
        "VALUES (:id, :account, :patient, :hospital, 'phone_dob')",
        id=link_id,
        account=account_id,
        patient=patient_id,
        hospital=hospital_id,
    )
    return link_id


class TestUpgrade:
    async def test_every_table_is_created_at_its_own_step(
        self,
        scratch: tuple[str, AsyncEngine],
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        assert not PATIENT_TABLES & await _tables(engine)

        await _migrate(url, "upgrade", IDENTITY)
        assert PATIENT_TABLES & await _tables(engine) == IDENTITY_TABLES
        await _migrate(url, "upgrade", LINKS)
        assert PATIENT_TABLES & await _tables(engine) == IDENTITY_TABLES | {"patient_account_links"}
        await _migrate(url, "upgrade", CONSENT)
        assert PATIENT_TABLES & await _tables(engine) == PATIENT_TABLES

    @pytest.mark.parametrize("table", sorted(PATIENT_TABLES))
    async def test_the_columns_are_exactly_the_models(
        self,
        scratch: tuple[str, AsyncEngine],
        table: str,
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)

        migrated = await _columns(engine, table)
        model = Base.metadata.tables[table]

        assert set(migrated) == set(model.c.keys())
        for column in model.c:
            assert (migrated[column.name] == "YES") is bool(column.nullable), column.name

    async def test_the_identity_tables_have_no_hospital_and_the_others_do(
        self,
        scratch: tuple[str, AsyncEngine],
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)

        for table in IDENTITY_TABLES:
            assert "hospital_id" not in await _columns(engine, table), table
        assert (await _columns(engine, "patient_account_links"))["hospital_id"] == "NO"
        assert (await _columns(engine, "patient_access_grants"))["hospital_id"] == "NO"
        assert (await _columns(engine, "patient_consent_records"))["hospital_id"] == "YES"

    async def test_no_patient_table_references_users(
        self,
        scratch: tuple[str, AsyncEngine],
    ) -> None:
        """A patient is never a ``users`` row — and nothing here points at one."""
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT conrelid::regclass::text FROM pg_constraint "
                    "WHERE contype = 'f' AND confrelid = 'users'::regclass "
                    "AND conrelid::regclass::text LIKE 'patient\\_%'"
                )
            )
            assert result.scalars().all() == []

    async def test_the_audit_log_gains_one_nullable_column(
        self,
        scratch: tuple[str, AsyncEngine],
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", CONSENT)
        before = await _columns(engine, "audit_logs")
        await _execute(
            engine,
            "INSERT INTO audit_logs (id, actor_type, action) "
            "VALUES (gen_random_uuid(), 'system', 'old.event')",
        )

        await _migrate(url, "upgrade", AUDIT)

        after = await _columns(engine, "audit_logs")
        assert set(after) - set(before) == {"patient_account_id"}
        assert after["patient_account_id"] == "YES"
        assert {name: after[name] for name in before} == before
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT actor_type, patient_account_id FROM audit_logs")
                )
            ).one()
        assert tuple(row) == ("system", None)


class TestConstraints:
    async def test_a_phone_number_has_one_account(self, scratch: tuple[str, AsyncEngine]) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)
        await _account(engine, "+919812345678")

        with pytest.raises(IntegrityError, match="uq_patient_accounts_phone"):
            await _account(engine, "+919812345678")

    async def test_an_account_status_is_one_of_three(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)
        account_id = await _account(engine, "+919812345678")

        with pytest.raises(IntegrityError, match="ck_patient_accounts_status"):
            await _execute(
                engine, "UPDATE patient_accounts SET status = 'admin' WHERE id = :id", id=account_id
            )

    async def test_a_record_has_one_active_link(self, scratch: tuple[str, AsyncEngine]) -> None:
        """Attack: two accounts claim the same record at the same instant."""
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)
        hospital_id = await _hospital(engine, "h-a")
        patient_id = await _patient(engine, hospital_id)
        first = await _account(engine, "+919812345678")
        second = await _account(engine, "+919812345679")
        link_id = await _link(engine, first, patient_id, hospital_id)

        with pytest.raises(IntegrityError, match="uq_patient_account_links_active_patient"):
            await _link(engine, second, patient_id, hospital_id)

        # An ended link is history and does not block a new one.
        await _execute(
            engine,
            "UPDATE patient_account_links SET unlinked_at = now() WHERE id = :id",
            id=link_id,
        )
        await _link(engine, second, patient_id, hospital_id)

    async def test_an_account_has_one_active_self_link_per_hospital(
        self,
        scratch: tuple[str, AsyncEngine],
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)
        hospital_a = await _hospital(engine, "h-a")
        hospital_b = await _hospital(engine, "h-b")
        account = await _account(engine, "+919812345678")
        await _link(engine, account, await _patient(engine, hospital_a), hospital_a)

        with pytest.raises(IntegrityError, match="uq_patient_account_links_active_self"):
            await _link(engine, account, await _patient(engine, hospital_a), hospital_a)

        # One per hospital — not one in all.
        await _link(engine, account, await _patient(engine, hospital_b), hospital_b)

    async def test_only_a_self_link_can_be_stored(self, scratch: tuple[str, AsyncEngine]) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)
        hospital_id = await _hospital(engine, "h-a")
        account = await _account(engine, "+919812345678")

        with pytest.raises(IntegrityError, match="ck_patient_account_links_relationship"):
            await _execute(
                engine,
                "INSERT INTO patient_account_links "
                "(id, account_id, patient_id, hospital_id, relationship, verified_via) "
                "VALUES (gen_random_uuid(), :account, :patient, :hospital, 'parent', 'phone_dob')",
                account=account,
                patient=await _patient(engine, hospital_id),
                hospital=hospital_id,
            )

    async def test_a_hospital_consent_needs_its_hospital_and_a_platform_one_has_none(
        self,
        scratch: tuple[str, AsyncEngine],
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)
        hospital_id = await _hospital(engine, "h-a")
        account = await _account(engine, "+919812345678")
        insert = (
            "INSERT INTO patient_consent_records (id, account_id, purpose, hospital_id, "
            "policy_version) VALUES (gen_random_uuid(), :account, :purpose, :hospital, 'v1')"
        )

        with pytest.raises(IntegrityError, match="ck_patient_consent_records_hospital_scope"):
            await _execute(
                engine, insert, account=account, purpose="hospital_record_link", hospital=None
            )
        with pytest.raises(IntegrityError, match="ck_patient_consent_records_hospital_scope"):
            await _execute(
                engine, insert, account=account, purpose="terms_of_service", hospital=hospital_id
            )
        with pytest.raises(IntegrityError, match="ck_patient_consent_records_purpose"):
            await _execute(engine, insert, account=account, purpose="marketing", hospital=None)

        await _execute(
            engine, insert, account=account, purpose="hospital_record_link", hospital=hospital_id
        )
        await _execute(engine, insert, account=account, purpose="terms_of_service", hospital=None)

    async def test_a_consent_in_force_is_unique_per_version(
        self,
        scratch: tuple[str, AsyncEngine],
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)
        account = await _account(engine, "+919812345678")
        insert = (
            "INSERT INTO patient_consent_records (id, account_id, purpose, policy_version) "
            "VALUES (gen_random_uuid(), :account, 'terms_of_service', :version)"
        )
        await _execute(engine, insert, account=account, version="v1")

        with pytest.raises(IntegrityError, match="uq_patient_consent_records_active_platform"):
            await _execute(engine, insert, account=account, version="v1")

        # A new version is a new row; the old one is kept.
        await _execute(engine, insert, account=account, version="v2")

    async def test_a_grant_needs_a_category_a_future_expiry_and_a_known_category(
        self,
        scratch: tuple[str, AsyncEngine],
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)
        hospital_id = await _hospital(engine, "h-a")
        account = await _account(engine, "+919812345678")
        patient_id = await _patient(engine, hospital_id)
        insert = (
            "INSERT INTO patient_access_grants (id, hospital_id, patient_id, grantor_account_id, "
            "grantee_type, grantee_id, grantee_hospital_id, purpose_note, categories, expires_at) "
            "VALUES (gen_random_uuid(), :hospital, :patient, :account, 'hospital', :hospital, "
            ":hospital, 'consultation', "
            "CAST(CAST(:categories AS text) AS patient_record_category[]), "
            "now() + CAST(CAST(:lifetime AS text) AS interval))"
        )
        values = {"hospital": hospital_id, "patient": patient_id, "account": account}

        with pytest.raises(IntegrityError, match="categories_not_empty"):
            await _execute(engine, insert, categories="{}", lifetime="30 days", **values)
        with pytest.raises(IntegrityError, match="ck_patient_access_grants_expiry"):
            await _execute(engine, insert, categories="{identity}", lifetime="-1 days", **values)
        with pytest.raises(Exception, match="patient_record_category"):
            await _execute(engine, insert, categories="{everything}", lifetime="30 days", **values)

        await _execute(
            engine, insert, categories="{identity,lab_results}", lifetime="30 days", **values
        )


class TestDowngrade:
    async def test_each_step_removes_what_it_added_and_nothing_else(
        self,
        scratch: tuple[str, AsyncEngine],
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", BEFORE)
        before = await _tables(engine)
        audit_before = await _columns(engine, "audit_logs")
        await _migrate(url, "upgrade", AUDIT)

        await _migrate(url, "downgrade", CONSENT)
        assert await _columns(engine, "audit_logs") == audit_before
        await _migrate(url, "downgrade", LINKS)
        assert PATIENT_TABLES & await _tables(engine) == IDENTITY_TABLES | {"patient_account_links"}
        await _migrate(url, "downgrade", IDENTITY)
        assert PATIENT_TABLES & await _tables(engine) == IDENTITY_TABLES
        await _migrate(url, "downgrade", BEFORE)
        assert await _tables(engine) == before

    async def test_the_enum_type_goes_with_its_table(
        self, scratch: tuple[str, AsyncEngine]
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)
        await _migrate(url, "downgrade", LINKS)

        async with engine.connect() as connection:
            found = (
                await connection.execute(
                    text("SELECT count(*) FROM pg_type WHERE typname = 'patient_record_category'")
                )
            ).scalar_one()
        assert found == 0

    async def test_it_can_be_applied_again_after_a_downgrade(
        self,
        scratch: tuple[str, AsyncEngine],
    ) -> None:
        url, engine = scratch
        await _migrate(url, "upgrade", AUDIT)
        await _migrate(url, "downgrade", BEFORE)
        await _migrate(url, "upgrade", AUDIT)

        assert await _tables(engine) >= PATIENT_TABLES
        assert "patient_account_id" in await _columns(engine, "audit_logs")
