"""Pharmacy demo data — a medicine catalog, stock on the shelf, and a vendor.

A section of the one seed mechanism, called by
:func:`app.seeds.demo_data.seed_demo_data`. Split out for the same reason
``demo_billing`` and ``demo_lab`` are: length.

**Everything here is fictional**, including the batch numbers and the vendor.
Prices are plausible retail prices chosen for the demo, not a tariff.

**What a demo gets.** A catalog to prescribe from, with stock arranged so
every dispensing rule can be shown:

=====================  =====================================================
Medicine               Stock
=====================  =====================================================
Paracetamol 500 mg     Two batches: a small one expiring in 20 days and a
                       large one a year out. A dispense takes the near one
                       first (rule 2) and warns about it (rule 4).
Amoxicillin 500 mg     One healthy batch, and one that expired last month,
                       which is never dispensed (AC-5).
Atorvastatin 10 mg     A single batch of 8, so a prescription for more shows
                       the all-or-nothing rule (AC-3).
Pantoprazole 40 mg     One batch, recalled — in stock but not dispensable.
Insulin glargine       Nothing in stock.
The rest               One healthy batch each.
=====================  =====================================================

No prescriptions or purchase orders are seeded: writing and dispensing one
live is the thing worth showing.

**Idempotency.** A medicine is keyed on ``(hospital_id, sku)``, a batch on
``(medicine_id, batch_number)`` and the vendor on ``(hospital_id, name)`` — each
the table's unique constraint. A batch that exists is left alone, so a second
run neither adds stock nor resets what a demo has since dispensed. Expiry
dates are relative to the day of the first run.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.models.pharmacy import StockMovementReason
from app.repositories.medicine_repository import MedicineRepository
from app.repositories.procurement_repository import ProcurementRepository

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.hospital import Hospital

logger = get_logger(__name__)

__all__ = ["DEMO_VENDOR", "MEDICINES", "seed_demo_pharmacy"]

#: ``(sku, name, generic, strength, form, atc, unit_price, needs_rx, active)``.
#: One retired medicine makes the ``is_active`` filter demonstrable.
MEDICINES: list[tuple[str, str, str, str, str, str, str, bool, bool]] = [
    ("PARA-500", "Paracetamol", "Paracetamol", "500 mg", "tablet", "N02BE01", "2.50", False, True),
    ("AMOX-500", "Amoxicillin", "Amoxicillin", "500 mg", "capsule", "J01CA04", "8.00", True, True),
    ("ATOR-10", "Atorvastatin", "Atorvastatin", "10 mg", "tablet", "C10AA05", "6.50", True, True),
    ("METF-500", "Metformin", "Metformin", "500 mg", "tablet", "A10BA02", "3.00", True, True),
    ("AMLO-5", "Amlodipine", "Amlodipine", "5 mg", "tablet", "C08CA01", "4.00", True, True),
    ("PANT-40", "Pantoprazole", "Pantoprazole", "40 mg", "tablet", "A02BC02", "7.50", True, True),
    ("CETZ-10", "Cetirizine", "Cetirizine", "10 mg", "tablet", "R06AE07", "3.50", False, True),
    (
        "ORS-21",
        "ORS sachet",
        "Oral rehydration salts",
        "21 g",
        "sachet",
        "A07CA",
        "20.00",
        False,
        True,
    ),
    (
        "INSG-100",
        "Insulin glargine",
        "Insulin glargine",
        "100 IU/mL",
        "pen",
        "A10AE04",
        "650.00",
        True,
        True,
    ),
    (
        "RANI-150",
        "Ranitidine (withdrawn)",
        "Ranitidine",
        "150 mg",
        "tablet",
        "A02BA02",
        "2.00",
        True,
        False,
    ),
]

#: ``(sku, batch_number, days_to_expiry, quantity, cost_per_unit, recalled)``.
BATCHES: list[tuple[str, str, int, int, str, bool]] = [
    ("PARA-500", "PA-2401", 20, 30, "1.10", False),
    ("PARA-500", "PA-2502", 365, 500, "1.20", False),
    ("AMOX-500", "AX-2311", -30, 40, "4.00", False),
    ("AMOX-500", "AX-2503", 300, 200, "4.20", False),
    ("ATOR-10", "AT-2504", 240, 8, "3.10", False),
    ("METF-500", "MF-2505", 400, 300, "1.40", False),
    ("AMLO-5", "AL-2506", 420, 250, "1.90", False),
    ("PANT-40", "PN-2507", 200, 120, "3.60", True),
    ("CETZ-10", "CZ-2508", 500, 150, "1.50", False),
    ("ORS-21", "OR-2509", 180, 60, "11.00", False),
]

#: The demo vendor: ``(name, contact, address, tax_id)``.
DEMO_VENDOR = (
    "Sanjeevani Pharma Distributors",
    "orders@sanjeevani-pharma.example",
    "14 Market Road, Demo City",
    "29ABCDE1234F1Z5",
)


async def seed_demo_pharmacy(
    session: AsyncSession,
    hospital: Hospital,
    *,
    today: date,
    actor_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Create the demo medicines, their stock and a vendor.

    :param session: An open session inside a transaction.
    :param hospital: The demo hospital.
    :param today: The seeded "today", which expiry dates are relative to.
    :param actor_id: User recorded as having received the stock.
    :returns: How many medicines, batches and vendors this run created.
    """
    medicines = MedicineRepository(session)
    procurement = ProcurementRepository(session)
    created = {"medicines": 0, "batches": 0, "vendors": 0}

    by_sku = {}
    for sku, name, generic, strength, form, atc, price, needs_rx, is_active in MEDICINES:
        medicine = await medicines.get_medicine_by_sku(hospital.id, sku)
        if medicine is None:
            medicine = await medicines.create_medicine(
                hospital_id=hospital.id,
                sku=sku,
                name=name,
                generic_name=generic,
                strength=strength,
                form=form,
                atc_code=atc,
                unit_price=Decimal(price),
                requires_prescription=needs_rx,
                is_active=is_active,
                created_by=actor_id,
            )
            created["medicines"] += 1
        by_sku[sku] = medicine

    now = datetime.now(UTC)
    for sku, batch_number, days, quantity, cost, recalled in BATCHES:
        medicine = by_sku[sku]
        if await medicines.get_batch_by_number(hospital.id, medicine.id, batch_number) is not None:
            continue
        batch = await medicines.create_batch(
            medicine=medicine,
            batch_number=batch_number,
            expiry_date=today + timedelta(days=days),
            cost_per_unit=Decimal(cost),
            created_by=actor_id,
        )
        await medicines.apply_movement(
            batch,
            quantity_change=quantity,
            reason=StockMovementReason.RECEIVED,
            moved_at=now,
            moved_by=actor_id,
            reference_type="seed",
        )
        if recalled:
            await medicines.update_batch(batch, updated_by=actor_id, is_recalled=True)
        created["batches"] += 1

    name, contact, address, tax_id = DEMO_VENDOR
    if await procurement.get_vendor_by_name(hospital.id, name) is None:
        await procurement.create_vendor(
            hospital_id=hospital.id,
            name=name,
            contact=contact,
            address=address,
            tax_id=tax_id,
            created_by=actor_id,
        )
        created["vendors"] += 1

    logger.info("demo_pharmacy_seeded", **created)
    return created
