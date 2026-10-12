"""Database row helpers shared by the pharmacy repository, API and integration tests.

Like :mod:`app.tests.billing_helpers`, these insert the minimum valid row and
return it, so the three suites build the same shelf the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.pharmacy import StockMovementReason
from app.repositories.medicine_repository import MedicineRepository

if TYPE_CHECKING:
    import uuid
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.pharmacy import Medicine, MedicineBatch

__all__ = ["insert_batch", "insert_medicine"]


async def insert_medicine(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    sku: str,
    *,
    name: str | None = None,
    unit_price: str = "2.50",
    is_active: bool = True,
) -> Medicine:
    """Insert a catalog medicine.

    :param session: The test session.
    :param hospital_id: Hospital the medicine belongs to.
    :param sku: Stock code.
    :param name: Display name. Defaults to the SKU, title-cased.
    :param unit_price: Selling price per unit.
    :param is_active: Whether it can be used.
    :returns: The persisted medicine.
    """
    return await MedicineRepository(session).create_medicine(
        hospital_id=hospital_id,
        sku=sku,
        name=name or sku.title(),
        strength="500 mg",
        form="tablet",
        unit_price=Decimal(unit_price),
        is_active=is_active,
    )


async def insert_batch(
    session: AsyncSession,
    medicine: Medicine,
    number: str,
    quantity: int,
    *,
    days: int = 365,
    recalled: bool = False,
    today: date | None = None,
) -> MedicineBatch:
    """Insert a batch holding ``quantity`` units, through a real stock movement.

    :param session: The test session.
    :param medicine: The medicine the batch is of.
    :param number: Batch number.
    :param quantity: Units on hand.
    :param days: Days from today to expiry; negative for an expired batch.
    :param recalled: Whether the batch is recalled.
    :param today: The day ``days`` counts from. Defaults to today in UTC.
    :returns: The persisted batch.
    """
    repository = MedicineRepository(session)
    batch = await repository.create_batch(
        medicine=medicine,
        batch_number=number,
        expiry_date=(today or datetime.now(UTC).date()) + timedelta(days=days),
        cost_per_unit=Decimal("1.00"),
    )
    if quantity:
        await repository.apply_movement(
            batch,
            quantity_change=quantity,
            reason=StockMovementReason.RECEIVED,
            moved_at=datetime.now(UTC),
            reference_type="test",
        )
    if recalled:
        await repository.update_batch(batch, is_recalled=True)
    return batch
