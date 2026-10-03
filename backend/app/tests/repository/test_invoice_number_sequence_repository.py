"""Repository tests for the per-hospital invoice-number counter.

Two claims are under test (``docs/modules/06-billing.md`` §8, business rule 2,
AC-2):

- **Unique.** ``advance`` hands out each value exactly once, even when several
  invoices are issued at the same moment.
- **Gap-free.** A value reserved by a transaction that then rolls back is
  handed out again, so no number is ever skipped.

Both are claims about a database row lock and transaction rollback, so they can
only be tested against a real database with genuinely separate transactions.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import DEFAULT_INVOICE_NUMBER_TEMPLATE, InvoiceNumberSequence
from app.models.hospital import Hospital
from app.repositories.invoice_number_sequence_repository import InvoiceNumberSequenceRepository

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.database


@pytest.fixture
def repository(db_session: AsyncSession) -> InvoiceNumberSequenceRepository:
    """A repository bound to the rolled-back test session."""
    return InvoiceNumberSequenceRepository(db_session)


@pytest.fixture
async def committed_hospital(db_engine: AsyncEngine) -> AsyncGenerator[uuid.UUID]:
    """A hospital that is really committed, and removed again afterwards.

    The concurrency tests cannot use the rolled-back ``db_session`` fixture:
    proving that transactions serialize requires them to be genuinely separate
    and to really commit.
    """
    hospital_id = uuid.uuid4()
    async with AsyncSession(db_engine) as setup:
        setup.add(
            Hospital(
                id=hospital_id,
                name="Invoice Sequence Test Hospital",
                slug=f"inv-seq-{uuid.uuid4().hex[:12]}",
                address={"line1": "1 Test Road", "city": "Hyderabad", "country": "IN"},
                settings={},
            )
        )
        await setup.commit()

    yield hospital_id

    async with AsyncSession(db_engine) as cleanup:
        await cleanup.execute(
            delete(InvoiceNumberSequence).where(InvoiceNumberSequence.hospital_id == hospital_id)
        )
        await cleanup.execute(delete(Hospital).where(Hospital.id == hospital_id))
        await cleanup.commit()


class TestAdvance:
    """Reserving sequence values."""

    async def test_advance_creates_the_counter_on_first_use(
        self, repository: InvoiceNumberSequenceRepository, hospital_id: uuid.UUID
    ) -> None:
        # A hospital has no counter row until it issues its first invoice.
        assert await repository.get_for_hospital(hospital_id) is None

        value, template = await repository.advance(hospital_id)

        assert value == 1
        assert template == DEFAULT_INVOICE_NUMBER_TEMPLATE

    async def test_advance_increments_by_exactly_one(
        self, repository: InvoiceNumberSequenceRepository, hospital_id: uuid.UUID
    ) -> None:
        values = [(await repository.advance(hospital_id))[0] for _ in range(5)]

        assert values == [1, 2, 3, 4, 5]

    async def test_advance_keeps_a_separate_counter_per_hospital(
        self,
        repository: InvoiceNumberSequenceRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        # Numbers are sequential *per hospital*, so counters must not be shared.
        await repository.advance(hospital_id)
        await repository.advance(hospital_id)

        value, _ = await repository.advance(other_hospital_id)

        assert value == 1
        row = await repository.get_for_hospital(hospital_id)
        assert row is not None
        assert row.current_value == 2

    async def test_advance_honours_a_custom_template(
        self,
        repository: InvoiceNumberSequenceRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        db_session.add(
            InvoiceNumberSequence(
                hospital_id=hospital_id, current_value=99, format_template="AH/{year}/{seq:05d}"
            )
        )
        await db_session.flush()

        value, template = await repository.advance(hospital_id)

        assert value == 100
        assert template == "AH/{year}/{seq:05d}"


class TestAdvanceConcurrency:
    """The row lock that makes invoice numbers unique under load."""

    async def test_concurrent_issues_get_distinct_consecutive_values(
        self, db_engine: AsyncEngine, committed_hospital: uuid.UUID
    ) -> None:
        async def take_one() -> int:
            """Reserve one value in its own transaction, holding the lock briefly."""
            async with AsyncSession(db_engine) as session, session.begin():
                value, _ = await InvoiceNumberSequenceRepository(session).advance(
                    committed_hospital
                )
                # Yield while still holding the row lock, so the other tasks
                # actually reach the lock and have to wait. Without this they
                # could run end-to-end one after another and the test would
                # pass without ever exercising contention.
                await asyncio.sleep(0.05)
                return value

        values = await asyncio.gather(*(take_one() for _ in range(5)))

        # No duplicates and no gaps: the lock serialized all five.
        assert sorted(values) == [1, 2, 3, 4, 5]

    async def test_a_rolled_back_reservation_leaves_no_gap(
        self, db_engine: AsyncEngine, committed_hospital: uuid.UUID
    ) -> None:
        # AC-2. This is the property a database SEQUENCE cannot provide —
        # nextval() is never rolled back — and the reason the counter is a row.
        async with AsyncSession(db_engine) as session, session.begin():
            first, _ = await InvoiceNumberSequenceRepository(session).advance(committed_hospital)

        async with AsyncSession(db_engine) as session:
            await session.begin()
            abandoned, _ = await InvoiceNumberSequenceRepository(session).advance(
                committed_hospital
            )
            await session.rollback()

        async with AsyncSession(db_engine) as session, session.begin():
            reissued, _ = await InvoiceNumberSequenceRepository(session).advance(committed_hospital)

        assert first == 1
        assert abandoned == 2
        # The abandoned 2 is handed out again rather than skipped.
        assert reissued == 2
