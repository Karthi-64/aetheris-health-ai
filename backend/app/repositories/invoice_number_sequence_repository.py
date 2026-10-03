"""Repository for the per-hospital invoice-number counter.

Owns exactly one thing: advancing ``invoice_number_sequences.current_value``
for a hospital under a row lock, so that two concurrent issues cannot be handed
the same number (``docs/modules/06-billing.md`` §8, business rule 2).

Data access only — the rendered invoice number is assembled by
:class:`~app.services.billing_service.BillingService`.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.billing import DEFAULT_INVOICE_NUMBER_TEMPLATE, InvoiceNumberSequence
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class InvoiceNumberSequenceRepository(BaseRepository[InvoiceNumberSequence]):
    """Repository for the ``invoice_number_sequences`` counter table.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(InvoiceNumberSequence, session)

    async def get_for_hospital(self, hospital_id: uuid.UUID) -> InvoiceNumberSequence | None:
        """Read a hospital's counter row without locking it.

        :param hospital_id: The hospital's UUID.
        :returns: The counter row, or ``None`` if the hospital has never issued
            an invoice.
        """
        stmt = select(InvoiceNumberSequence).where(InvoiceNumberSequence.hospital_id == hospital_id)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def advance(self, hospital_id: uuid.UUID) -> tuple[int, str]:
        """Reserve the next sequence value for a hospital.

        The same three steps as
        :meth:`~app.repositories.mrn_sequence_repository.MrnSequenceRepository.advance`,
        all inside the caller's transaction: create the row if it is missing,
        lock it, increment it.

        The lock is held until the caller commits. That is what makes the
        series **gap-free** and not merely unique: if the issuing transaction
        rolls back, the increment rolls back with it and the next issue reuses
        the number. A database sequence could not give that guarantee —
        ``nextval`` is never rolled back.

        The caller **must** be inside a transaction; the lock is meaningless
        otherwise.

        :param hospital_id: The hospital whose counter to advance.
        :returns: A ``(sequence_value, format_template)`` pair, where
            ``sequence_value`` is the newly reserved number.
        """
        # 1. Ensure the row exists.
        ensure_stmt = (
            pg_insert(InvoiceNumberSequence)
            .values(
                hospital_id=hospital_id,
                current_value=0,
                format_template=DEFAULT_INVOICE_NUMBER_TEMPLATE,
            )
            .on_conflict_do_nothing(index_elements=["hospital_id"])
        )
        await self._session.execute(ensure_stmt)

        # 2. Lock the row for the rest of this transaction.
        lock_stmt = (
            select(InvoiceNumberSequence)
            .where(InvoiceNumberSequence.hospital_id == hospital_id)
            .with_for_update()
        )
        result = await self._session.execute(lock_stmt)
        sequence = result.unique().scalar_one()

        # 3. Reserve the next value.
        sequence.current_value += 1
        await self._session.flush()

        return sequence.current_value, sequence.format_template
