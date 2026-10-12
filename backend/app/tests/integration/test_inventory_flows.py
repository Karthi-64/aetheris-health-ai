"""End-to-end Inventory flows, the concurrency guarantee, and the demo seed.

``docs/modules/09-inventory.md`` §16 asks for a full lifecycle and — in §14 —
for concurrent movements on one location to be "verified with transaction
test". The concurrency tests cannot use the rolled-back ``db_session``
fixture: proving that transactions take turns requires them to be genuinely
separate and to really commit, so they build a committed hospital and delete
it afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.db import get_db_session
from app.core.exceptions import ConflictError
from app.main import create_app
from app.models.hospital import Hospital
from app.models.inventory import (
    InventoryItem,
    InventoryLocation,
    InventoryLocationKind,
    InventoryMovement,
    InventoryStock,
)
from app.repositories.department_repository import DepartmentRepository
from app.repositories.hospital_repository import HospitalRepository
from app.repositories.inventory_repository import InventoryRepository
from app.repositories.procurement_repository import ProcurementRepository
from app.schemas.inventory import ConsumeRequest, CreateInventoryItemRequest, TransferRequest
from app.seeds.demo_data import seed_demo_data
from app.seeds.demo_inventory import ITEMS, LOCATIONS, STOCK, seed_demo_inventory
from app.seeds.seed import SYSTEM_ROLES
from app.services.inventory_service import InventoryService
from app.tests.billing_helpers import auth_headers, insert_user_with_permissions
from app.tests.conftest import RecordingAuditSink
from app.tests.inventory_helpers import insert_item, insert_location, insert_stock

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.database

BASE = "/api/v1/inventory"
TODAY = datetime.now(UTC).date()


def _seeded_permissions(role: str) -> list[str]:
    """The permission codes the seed gives a role."""
    return next(list(codes) for name, _, codes in SYSTEM_ROLES if name == role)


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """An HTTP client sharing the test's rolled-back session, with real audit."""
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def hospital(db_session: AsyncSession, hospital_id: uuid.UUID) -> Hospital:
    """The tenant under test."""
    result = await db_session.execute(select(Hospital).where(Hospital.id == hospital_id))
    return result.unique().scalar_one()


class TestWardRunsLowAndIsRestocked:
    async def test_nurse_uses_manager_is_alerted_orders_and_receives(
        self, api: AsyncClient, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        async def login(role: str) -> dict[str, str]:
            user = await insert_user_with_permissions(
                db_session, hospital.id, _seeded_permissions(role)
            )
            return auth_headers(user.id, hospital.id)

        as_nurse = await login("Nurse")
        as_manager = await login("Inventory Manager")
        as_admin = await login("Hospital Admin")

        locations = {
            loc["code"]: loc["id"]
            for loc in (await api.get(f"{BASE}/locations", headers=as_nurse)).json()["data"]
        }
        syringes = (await api.get(f"{BASE}/items", params={"q": "syr-5"}, headers=as_nurse)).json()[
            "data"
        ][0]

        # The ward has 5 boxes; the hospital has 25 against a reorder point of 20.
        on_ward = await api.get(
            f"{BASE}/stock",
            params={"item_id": syringes["id"], "location_id": locations["WARD-A"]},
            headers=as_nurse,
        )
        assert [r["quantity"] for r in on_ward.json()["data"]] == ["5.00"]
        # A nurse uses stock; she does not move it or correct it.
        move = {
            "item_id": syringes["id"],
            "from_location_id": locations["STORE"],
            "to_location_id": locations["WARD-A"],
            "quantity": "10",
        }
        assert (await api.post(f"{BASE}/transfer", json=move, headers=as_nurse)).status_code == 403
        used = await api.post(
            f"{BASE}/consume",
            json={"item_id": syringes["id"], "location_id": locations["WARD-A"], "quantity": "5"},
            headers=as_nurse,
        )
        assert used.status_code == 200, used.text
        assert used.json()["data"]["summary"]["is_low"] is True  # 20 left: at the reorder point
        empty = await api.post(
            f"{BASE}/consume",
            json={"item_id": syringes["id"], "location_id": locations["WARD-A"], "quantity": "1"},
            headers=as_nurse,
        )
        assert empty.status_code == 409  # the store's 20 are not the ward's

        # The manager is told, the nurse is not.
        notices = (await api.get("/api/v1/notifications", headers=as_manager)).json()["data"]
        assert [n["kind"] for n in notices] == ["inventory.low_stock"]
        assert "Syringe 5 mL is down to 20 box of 50" in notices[0]["body"]
        assert (await api.get("/api/v1/notifications", headers=as_nurse)).json()["data"] == []
        panel = await api.get(
            f"{BASE}/stock/summary", params={"low_stock": "true"}, headers=as_manager
        )
        low = {s["item"]["sku"]: s["suggested_order_quantity"] for s in panel.json()["data"]}
        assert low["SYR-5"] == "40.00"  # 20 held, target 60
        assert {"MASK-3P", "SHEET-S"} <= set(low)  # seeded low, and never stocked

        # The manager tops the ward up from the store, then reorders.
        topped = await api.post(f"{BASE}/transfer", json=move, headers=as_manager)
        assert topped.status_code == 200, topped.text
        vendor = (await api.get("/api/v1/vendors", headers=as_manager)).json()["data"][0]
        order = (
            await api.post(
                f"{BASE}/purchase-orders",
                json={
                    "vendor_id": vendor["id"],
                    "items": [
                        {
                            "item_id": syringes["id"],
                            "quantity": low["SYR-5"],
                            "unit_price": "210.00",
                        }
                    ],
                },
                headers=as_manager,
            )
        ).json()["data"]
        assert order["total_amount"] == "8400.00"
        await api.post(f"{BASE}/purchase-orders/{order['id']}/send", headers=as_manager)
        received = await api.post(
            f"{BASE}/purchase-orders/{order['id']}/receive",
            json={
                "location_id": locations["STORE"],
                "items": [{"po_item_id": order["items"][0]["id"], "quantity": "40"}],
            },
            headers=as_manager,
        )
        assert received.status_code == 200, received.text

        after = await api.get(f"{BASE}/stock/summary", params={"q": "SYR-5"}, headers=as_manager)
        [summary] = after.json()["data"]
        assert (summary["usable_quantity"], summary["is_low"]) == ("60.00", False)
        rows = await api.get(
            f"{BASE}/stock", params={"item_id": syringes["id"]}, headers=as_manager
        )
        assert [(r["location_code"], r["quantity"]) for r in rows.json()["data"]] == [
            ("STORE", "50.00"),
            ("WARD-A", "10.00"),
        ]
        # AC-4: the ledger reconstructs what is on the shelves.
        ledger = await api.get(
            f"{BASE}/movements",
            params={"item_id": syringes["id"], "page_size": 100},
            headers=as_manager,
        )
        assert sum(Decimal(m["quantity_change"]) for m in ledger.json()["data"]) == Decimal("60.00")
        trail = await api.get("/api/v1/audit-logs", params={"page_size": 100}, headers=as_admin)
        actions = {entry["action"] for entry in trail.json()["data"]}
        assert {"inventory.consumed", "inventory.transferred", "inventory.po.received"} <= actions


# ── Concurrency ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _World:
    """Ids of rows that are really committed for a concurrency test."""

    hospital_id: uuid.UUID
    item_id: uuid.UUID
    store_id: uuid.UUID
    ward_id: uuid.UUID


STOCK_HELD = 10


@pytest.fixture
async def world(db_engine: AsyncEngine) -> AsyncGenerator[_World]:
    """A committed hospital with ten units in a store, removed afterwards."""
    hospital_id = uuid.uuid4()
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        setup.add(
            Hospital(
                id=hospital_id,
                name="Inventory Concurrency Test Hospital",
                slug=f"inventory-race-{uuid.uuid4().hex[:12]}",
                address={"line1": "1 Test Road", "city": "Hyderabad", "country": "IN"},
                settings={},
            )
        )
        await setup.flush()
        item = await insert_item(setup, hospital_id, "LAST")
        store = await insert_location(setup, hospital_id, "STORE")
        ward = await insert_location(setup, hospital_id, "WARD", kind=InventoryLocationKind.WARD)
        await insert_stock(setup, item, store, str(STOCK_HELD))
        await setup.commit()

    yield _World(hospital_id, item.id, store.id, ward.id)

    async with AsyncSession(db_engine) as cleanup:
        for model in (InventoryMovement, InventoryStock, InventoryLocation, InventoryItem):
            await cleanup.execute(delete(model).where(model.hospital_id == hospital_id))
        await cleanup.execute(delete(Hospital).where(Hospital.id == hospital_id))
        await cleanup.commit()


def _service(session: AsyncSession) -> InventoryService:
    """An inventory service on its own session, as one request would build it."""
    return InventoryService(
        InventoryRepository(session),
        DepartmentRepository(session),
        HospitalRepository(session),
        session,
        RecordingAuditSink(),
    )


async def _totals(engine: AsyncEngine, world: _World) -> tuple[dict[uuid.UUID, Decimal], Decimal]:
    """Stock by location, and the sum of the whole ledger for the item."""
    async with AsyncSession(engine) as check:
        rows = await check.execute(
            select(InventoryStock.location_id, InventoryStock.quantity).where(
                InventoryStock.item_id == world.item_id
            )
        )
        ledger = await check.scalar(
            select(func.sum(InventoryMovement.quantity_change)).where(
                InventoryMovement.item_id == world.item_id
            )
        )
    return {location_id: quantity for location_id, quantity in rows.all()}, Decimal(ledger or 0)


class TestConcurrentMovements:
    async def test_simultaneous_consumption_never_overdraws_a_location(
        self, db_engine: AsyncEngine, world: _World
    ) -> None:
        contenders = 25

        async def attempt() -> bool:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                try:
                    await _service(session).consume(
                        world.hospital_id,
                        ConsumeRequest(
                            item_id=world.item_id, location_id=world.store_id, quantity=Decimal(1)
                        ),
                    )
                except ConflictError:
                    await session.rollback()
                    return False
                return True

        outcomes = await asyncio.gather(*(attempt() for _ in range(contenders)))

        assert outcomes.count(True) == STOCK_HELD
        assert outcomes.count(False) == contenders - STOCK_HELD
        stock, ledger = await _totals(db_engine, world)
        assert stock == {world.store_id: Decimal("0.00")}
        assert ledger == 0  # +10 opening, ten × −1: the ledger agrees

    async def test_s14_concurrent_transfers_and_use_conserve_stock(
        self, db_engine: AsyncEngine, world: _World
    ) -> None:
        # Transfers out of the store racing consumption from it: whatever
        # order they land in, nothing is created, lost or overdrawn.
        async def transfer() -> bool:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                try:
                    await _service(session).transfer(
                        world.hospital_id,
                        TransferRequest(
                            item_id=world.item_id,
                            from_location_id=world.store_id,
                            to_location_id=world.ward_id,
                            quantity=Decimal(2),
                        ),
                    )
                except ConflictError:
                    await session.rollback()
                    return False
                return True

        async def consume() -> bool:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                try:
                    await _service(session).consume(
                        world.hospital_id,
                        ConsumeRequest(
                            item_id=world.item_id, location_id=world.store_id, quantity=Decimal(1)
                        ),
                    )
                except ConflictError:
                    await session.rollback()
                    return False
                return True

        results = await asyncio.gather(
            *(transfer() for _ in range(6)), *(consume() for _ in range(6))
        )

        moved = Decimal(2) * results[:6].count(True)
        used = Decimal(1) * results[6:].count(True)
        stock, ledger = await _totals(db_engine, world)
        assert stock.get(world.ward_id, Decimal(0)) == moved
        assert stock[world.store_id] == STOCK_HELD - moved - used
        assert stock[world.store_id] >= 0
        # Only use takes stock out of the hospital.
        assert sum(stock.values()) == ledger == STOCK_HELD - used
        # There were 18 units of demand for 10 of stock, so something was refused.
        assert results.count(False) >= 1


# ── Seed ────────────────────────────────────────────────────────────────────


class TestSeededInventory:
    async def test_the_shelves_demonstrate_each_rule(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        repository = InventoryRepository(db_session)

        items = {i.sku: i for i in await repository.list_items(hospital.id, limit=100)}
        assert set(items) == {sku for sku, *_ in ITEMS}
        assert any(not item.is_active for item in items.values())
        for item in items.values():
            # Every seeded item would be accepted by the API's own validation.
            CreateInventoryItemRequest.model_validate(
                {
                    "sku": item.sku,
                    "name": item.name,
                    "category": item.category,
                    "unit_of_measure": item.unit_of_measure,
                    "is_batch_tracked": item.is_batch_tracked,
                    "reorder_point": item.reorder_point,
                    "target_stock": item.target_stock,
                }
            )
        assert {loc.code for loc in await repository.list_locations(hospital.id)} == {
            code for code, *_ in LOCATIONS
        }
        assert await repository.count_stock(hospital.id) == len(STOCK)
        # Tracked items have batches; untracked ones do not.
        for row in await repository.list_stock(hospital.id, limit=100):
            assert (row.batch_number is not None) == row.item.is_batch_tracked
            ledger = await repository.list_movements(hospital.id, item_id=row.item_id, limit=100)
            held = sum(m.quantity_change for m in ledger if m.stock_id == row.id)
            assert held == row.quantity
        # Judge the shelf by the earliest day the seed's "today" could have been.
        totals = await repository.totals_by_item(hospital.id, on=TODAY - timedelta(days=3))
        assert totals[items["SYR-5"].id] == (Decimal("25.00"), Decimal("25.00"))  # just above 20
        assert totals[items["MASK-3P"].id][1] < 30  # already low
        assert totals[items["GAUZE-ST"].id] == (Decimal("175.00"), Decimal("150.00"))  # 25 expired
        assert items["SHEET-S"].id not in totals  # never stocked
        # The vendor to order from is Pharmacy's.
        assert await ProcurementRepository(db_session).count_vendors(hospital.id) == 1

    async def test_a_second_run_adds_nothing_and_does_not_restock(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        repository = InventoryRepository(db_session)
        gloves = await repository.get_item_by_sku(hospital.id, "GLOVE-M")
        assert gloves is not None
        before = await repository.totals_by_item(hospital.id, on=TODAY, item_ids=[gloves.id])
        row = (await repository.list_stock(hospital.id, item_id=gloves.id))[0]
        # A demo used some since the first run.
        await _service(db_session).consume(
            hospital.id,
            ConsumeRequest(item_id=gloves.id, location_id=row.location_id, quantity=Decimal(3)),
        )

        again = await seed_demo_inventory(db_session, hospital, today=TODAY)

        assert again == {"locations": 0, "items": 0, "stock": 0}
        after = await repository.totals_by_item(hospital.id, on=TODAY, item_ids=[gloves.id])
        assert after[gloves.id][0] == before[gloves.id][0] - 3
