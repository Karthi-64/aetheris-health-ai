"""Database row helpers shared by the inventory repository, API and integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.models.inventory import InventoryLocationKind, InventoryMovementReason
from app.repositories.inventory_repository import InventoryRepository

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.inventory import InventoryItem, InventoryLocation, InventoryStock

__all__ = ["insert_item", "insert_location", "insert_stock"]


async def insert_item(
    session: AsyncSession, hospital_id: uuid.UUID, sku: str, **fields: Any
) -> InventoryItem:
    """Insert an inventory item.

    :param session: The test session.
    :param hospital_id: Hospital the item belongs to.
    :param sku: Stock code.
    :param fields: Any other column, e.g. ``is_batch_tracked`` or ``reorder_point``.
    :returns: The persisted item.
    """
    values: dict[str, Any] = {"name": sku.title(), "unit_of_measure": "box"}
    values.update(fields)
    return await InventoryRepository(session).create_item(
        hospital_id=hospital_id, sku=sku, **values
    )


async def insert_location(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    code: str,
    *,
    kind: InventoryLocationKind = InventoryLocationKind.STORE,
    is_active: bool = True,
) -> InventoryLocation:
    """Insert a stock location."""
    return await InventoryRepository(session).create_location(
        hospital_id=hospital_id, name=code.title(), code=code, kind=kind, is_active=is_active
    )


async def insert_stock(
    session: AsyncSession,
    item: InventoryItem,
    location: InventoryLocation,
    quantity: str,
    *,
    batch: str | None = None,
    days: int | None = None,
) -> InventoryStock:
    """Put stock on a shelf, through a real ledger movement.

    :param session: The test session.
    :param item: The item.
    :param location: Where it is held.
    :param quantity: Units to hold.
    :param batch: Batch number, for a tracked item.
    :param days: Days from today to expiry; negative for an expired batch.
    :returns: The stock row.
    """
    repository = InventoryRepository(session)
    stock = await repository.get_or_create_stock(
        item=item,
        location=location,
        batch_number=batch,
        expiry_date=(datetime.now(UTC).date() + timedelta(days=days) if days is not None else None),
    )
    if Decimal(quantity):
        await repository.apply_movement(
            stock,
            quantity_change=Decimal(quantity),
            reason=InventoryMovementReason.ADJUSTED,
            moved_at=datetime.now(UTC),
            reference_type="test",
            note="Opening balance",
        )
    return stock
