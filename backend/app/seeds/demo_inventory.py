"""Inventory demo data — locations, an item catalog and stock on the shelves.

A section of the one seed mechanism, called by
:func:`app.seeds.demo_data.seed_demo_data`. Split out for the same reason the
other demo sections are: length.

**Everything here is fictional**, including the batch numbers.

**What a demo gets.** Four locations — a general store, a ward, the ICU and a
theatre — and a catalog with stock arranged so each rule can be shown:

========================  ==================================================
Item                      Stock
========================  ==================================================
Nitrile gloves (M)        Plenty in the store, a little on the ward: a
                          transfer tops the ward up.
Syringe 5 mL              25 held against a reorder point of 20, so using six
                          trips the low-stock alert live (AC-2).
Surgical masks            Already below their reorder point: the alerts panel
                          is not empty on first load.
IV cannula 20G            Batch-tracked, two batches in the store; the one
                          expiring in 25 days is used first.
Sterile gauze             Batch-tracked, with one batch that expired last
                          month: held, but never usable (§14).
Oxygen cylinder (B type)  No reorder point, so it never alerts.
Bed sheets                Nothing in stock.
========================  ==================================================

No purchase orders are seeded: raising one live is the thing worth showing.
The vendor is Pharmacy's (the table is shared).

**Idempotency.** A location is keyed on ``(hospital_id, code)`` and an item on
``(hospital_id, sku)`` — each the table's unique constraint. Stock is seeded
only for an item this run created, so a second run neither adds stock nor
resets what a demo has since used. Expiry dates are relative to the day of the
first run.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.models.inventory import InventoryLocationKind, InventoryMovementReason
from app.repositories.inventory_repository import InventoryRepository

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.hospital import Hospital

logger = get_logger(__name__)

__all__ = ["ITEMS", "LOCATIONS", "STOCK", "seed_demo_inventory"]

#: ``(code, name, kind)``.
LOCATIONS: list[tuple[str, str, InventoryLocationKind]] = [
    ("STORE", "General store", InventoryLocationKind.STORE),
    ("WARD-A", "Ward A", InventoryLocationKind.WARD),
    ("ICU", "Intensive care unit", InventoryLocationKind.ICU),
    ("OT-1", "Operating theatre 1", InventoryLocationKind.OT),
]

#: ``(sku, name, category, unit, batch_tracked, reorder_point, target, active)``.
ITEMS: list[tuple[str, str, str, str, bool, int | None, int | None, bool]] = [
    ("GLOVE-M", "Nitrile gloves, medium", "Disposables", "box of 100", False, 20, 80, True),
    ("SYR-5", "Syringe 5 mL", "Disposables", "box of 50", False, 20, 60, True),
    ("MASK-3P", "Surgical mask, 3-ply", "Disposables", "box of 50", False, 30, 100, True),
    ("CANN-20G", "IV cannula 20G", "Sterile", "piece", True, 100, 400, True),
    ("GAUZE-ST", "Sterile gauze swab", "Sterile", "pack of 10", True, 50, 200, True),
    ("O2-B", "Oxygen cylinder, B type", "Gases", "cylinder", False, None, None, True),
    ("SHEET-S", "Bed sheet, single", "Linen", "piece", False, 40, 120, True),
    ("GLOVE-LX", "Latex gloves (withdrawn)", "Disposables", "box of 100", False, None, None, False),
]

#: ``(sku, location code, batch number, days to expiry, quantity)``.
STOCK: list[tuple[str, str, str | None, int | None, str]] = [
    ("GLOVE-M", "STORE", None, None, "60"),
    ("GLOVE-M", "WARD-A", None, None, "4"),
    ("SYR-5", "STORE", None, None, "20"),
    ("SYR-5", "WARD-A", None, None, "5"),
    ("MASK-3P", "STORE", None, None, "18"),
    ("CANN-20G", "STORE", "CN-2401", 25, "80"),
    ("CANN-20G", "STORE", "CN-2502", 400, "300"),
    ("CANN-20G", "ICU", "CN-2502", 400, "40"),
    ("GAUZE-ST", "STORE", "GZ-2310", -30, "25"),
    ("GAUZE-ST", "STORE", "GZ-2504", 300, "150"),
    ("O2-B", "STORE", None, None, "12"),
    ("O2-B", "OT-1", None, None, "2"),
]


async def seed_demo_inventory(
    session: AsyncSession,
    hospital: Hospital,
    *,
    today: date,
    actor_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Create the demo locations, items and their opening stock.

    :param session: An open session inside a transaction.
    :param hospital: The demo hospital.
    :param today: The seeded "today", which expiry dates are relative to.
    :param actor_id: User recorded as having entered the stock.
    :returns: How many locations, items and stock rows this run created.
    """
    repository = InventoryRepository(session)
    created = {"locations": 0, "items": 0, "stock": 0}

    locations = {}
    for code, name, kind in LOCATIONS:
        location = await repository.get_location_by_code(hospital.id, code)
        if location is None:
            location = await repository.create_location(
                hospital_id=hospital.id, name=name, code=code, kind=kind, created_by=actor_id
            )
            created["locations"] += 1
        locations[code] = location

    new_items = {}
    for sku, name, category, unit, tracked, reorder_point, target, is_active in ITEMS:
        if await repository.get_item_by_sku(hospital.id, sku) is not None:
            continue
        new_items[sku] = await repository.create_item(
            hospital_id=hospital.id,
            sku=sku,
            name=name,
            category=category,
            unit_of_measure=unit,
            is_batch_tracked=tracked,
            reorder_point=reorder_point,
            target_stock=target,
            is_active=is_active,
            created_by=actor_id,
        )
        created["items"] += 1

    now = datetime.now(UTC)
    for sku, code, batch_number, days, quantity in STOCK:
        item = new_items.get(sku)
        if item is None:
            # The item was already there: its stock is whatever the demo left.
            continue
        stock = await repository.get_or_create_stock(
            item=item,
            location=locations[code],
            batch_number=batch_number,
            expiry_date=today + timedelta(days=days) if days is not None else None,
            created_by=actor_id,
        )
        await repository.apply_movement(
            stock,
            quantity_change=Decimal(quantity),
            reason=InventoryMovementReason.ADJUSTED,
            moved_at=now,
            moved_by=actor_id,
            reference_type="seed",
            note="Opening balance",
        )
        created["stock"] += 1

    logger.info("demo_inventory_seeded", **created)
    return created
