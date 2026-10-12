"""Repository tests for inventory items, locations, stock and purchase orders.

Real Postgres, rolled back per test. Every read method is checked for tenant
isolation, and the stock quantity is checked against its ledger (module spec
§16: "quantity computation"; AC-4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.inventory import InventoryLocationKind, InventoryMovementReason
from app.models.pharmacy import PurchaseOrderStatus
from app.repositories.inventory_po_repository import InventoryPurchaseOrderRepository
from app.repositories.inventory_repository import InventoryRepository
from app.repositories.procurement_repository import ProcurementRepository
from app.tests.billing_helpers import insert_user
from app.tests.inventory_helpers import insert_item, insert_location, insert_stock

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
TODAY = datetime.now(UTC).date()


@pytest.fixture
def inventory(db_session: AsyncSession) -> InventoryRepository:
    """An inventory repository bound to the rolled-back test session."""
    return InventoryRepository(db_session)


class TestItemsAndLocations:
    async def test_sku_and_code_are_unique_per_hospital_only(
        self, db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
    ) -> None:
        await insert_item(db_session, hospital_id, "GLOVE")
        await insert_item(db_session, other_hospital_id, "GLOVE")
        await insert_location(db_session, hospital_id, "STORE")
        await insert_location(db_session, other_hospital_id, "STORE")

        with pytest.raises(IntegrityError, match="uq_inventory_items_hospital_sku"):
            async with db_session.begin_nested():
                await insert_item(db_session, hospital_id, "GLOVE")
        with pytest.raises(IntegrityError, match="uq_inventory_locations_hospital_code"):
            async with db_session.begin_nested():
                await insert_location(db_session, hospital_id, "STORE")

    async def test_item_lookups_are_tenant_scoped_and_searchable(
        self,
        inventory: InventoryRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        gloves = await insert_item(
            db_session, hospital_id, "GLOVE-M", name="Nitrile gloves", category="Disposables"
        )
        await insert_item(db_session, hospital_id, "SYR-5", name="Syringe", category="Disposables")
        await insert_item(db_session, hospital_id, "OLD", name="Retired 100%_", is_active=False)
        theirs = await insert_item(db_session, other_hospital_id, "MASK")

        async def names(**filters: Any) -> list[str]:
            return [i.name for i in await inventory.list_items(hospital_id, **filters)]

        assert await inventory.get_item_by_id(hospital_id, theirs.id) is None
        assert await inventory.get_item_by_sku(hospital_id, "MASK") is None
        assert await inventory.get_items_by_ids(hospital_id, [gloves.id, theirs.id]) == [gloves]
        assert await inventory.get_items_by_ids(hospital_id, []) == []
        assert (await inventory.get_item_by_sku(hospital_id, "GLOVE-M")) is gloves
        assert await names(is_active=True) == ["Nitrile gloves", "Syringe"]
        assert await names(term="nit") == ["Nitrile gloves"]
        assert await names(term="syr-5") == ["Syringe"]
        assert await names(term="%") == []
        assert await names(category="Disposables", skip=1, limit=1) == ["Syringe"]
        assert await inventory.count_items(hospital_id) == 3
        assert await inventory.count_items(hospital_id, is_active=False) == 1
        assert (await inventory.update_item(gloves, reorder_point=20)).reorder_point == 20

    async def test_location_lookups_are_tenant_scoped(
        self,
        inventory: InventoryRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        store = await insert_location(db_session, hospital_id, "STORE")
        ward = await insert_location(
            db_session, hospital_id, "WARD-A", kind=InventoryLocationKind.WARD
        )
        theirs = await insert_location(db_session, other_hospital_id, "ICU")

        assert await inventory.get_location_by_id(hospital_id, theirs.id) is None
        assert await inventory.get_location_by_code(hospital_id, "ICU") is None
        assert (await inventory.get_location_by_code(hospital_id, "STORE")) is store
        assert [loc.code for loc in await inventory.list_locations(hospital_id)] == [
            "STORE",
            "WARD-A",
        ]
        await inventory.update_location(ward, is_active=False)
        assert [
            loc.code for loc in await inventory.list_locations(hospital_id, is_active=True)
        ] == ["STORE"]


class TestStock:
    async def test_ac4_the_quantity_is_always_the_sum_of_the_ledger(
        self, inventory: InventoryRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        user = await insert_user(db_session, hospital_id)
        item = await insert_item(db_session, hospital_id, "GLOVE")
        store = await insert_location(db_session, hospital_id, "STORE")
        stock = await inventory.get_or_create_stock(item=item, location=store)
        assert stock.quantity == 0

        for change, reason in (
            ("100", InventoryMovementReason.RECEIVED),
            ("-12.5", InventoryMovementReason.CONSUMED),
            ("-30", InventoryMovementReason.TRANSFERRED_OUT),
            ("4.25", InventoryMovementReason.ADJUSTED),
        ):
            await inventory.apply_movement(
                stock,
                quantity_change=Decimal(change),
                reason=reason,
                moved_at=NOW,
                moved_by=user.id,
            )

        ledger = await inventory.list_movements(hospital_id, item_id=item.id)
        assert stock.quantity == sum(m.quantity_change for m in ledger) == Decimal("61.75")
        assert {m.hospital_id for m in ledger} == {hospital_id}
        assert ledger[0].stock.id == stock.id
        assert (
            await inventory.count_movements(hospital_id, reason=InventoryMovementReason.CONSUMED)
            == 1
        )
        assert await inventory.count_movements(hospital_id, location_id=store.id) == 4

    async def test_the_database_refuses_a_negative_quantity_and_a_zero_movement(
        self, inventory: InventoryRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        stock = await insert_stock(
            db_session,
            await insert_item(db_session, hospital_id, "GLOVE"),
            await insert_location(db_session, hospital_id, "STORE"),
            "5",
        )

        with pytest.raises(IntegrityError, match="quantity_non_negative"):
            async with db_session.begin_nested():
                await inventory.apply_movement(
                    stock,
                    quantity_change=Decimal(-6),
                    reason=InventoryMovementReason.CONSUMED,
                    moved_at=NOW,
                )
        with pytest.raises(IntegrityError, match="quantity_change_non_zero"):
            async with db_session.begin_nested():
                await inventory.apply_movement(
                    stock,
                    quantity_change=Decimal(0),
                    reason=InventoryMovementReason.ADJUSTED,
                    moved_at=NOW,
                )

    async def test_rule_1_one_row_per_item_location_and_batch(
        self, inventory: InventoryRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        item = await insert_item(db_session, hospital_id, "CANN", is_batch_tracked=True)
        plain = await insert_item(db_session, hospital_id, "GLOVE")
        store = await insert_location(db_session, hospital_id, "STORE")
        ward = await insert_location(db_session, hospital_id, "WARD")

        first = await inventory.get_or_create_stock(item=item, location=store, batch_number="B1")
        again = await inventory.get_or_create_stock(item=item, location=store, batch_number="B1")
        other_batch = await inventory.get_or_create_stock(
            item=item, location=store, batch_number="B2"
        )
        other_place = await inventory.get_or_create_stock(
            item=item, location=ward, batch_number="B1"
        )
        # Two NULL batch numbers must still be one row for an untracked item.
        untracked = await inventory.get_or_create_stock(item=plain, location=store)
        untracked_again = await inventory.get_or_create_stock(item=plain, location=store)

        assert again.id == first.id
        assert len({first.id, other_batch.id, other_place.id}) == 3
        assert untracked_again.id == untracked.id
        assert await inventory.count_stock(hospital_id) == 4

    async def test_usable_stock_comes_earliest_expiry_first_undated_last(
        self, inventory: InventoryRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        item = await insert_item(db_session, hospital_id, "CANN", is_batch_tracked=True)
        store = await insert_location(db_session, hospital_id, "STORE")
        ward = await insert_location(db_session, hospital_id, "WARD")
        await insert_stock(db_session, item, store, "10", batch="NODATE")
        await insert_stock(db_session, item, store, "10", batch="LATE", days=300)
        await insert_stock(db_session, item, store, "10", batch="SOON", days=20)
        await insert_stock(db_session, item, store, "10", batch="TODAY", days=0)
        await insert_stock(db_session, item, store, "10", batch="EXPIRED", days=-1)
        await insert_stock(db_session, item, store, "0", batch="EMPTY", days=5)
        await insert_stock(db_session, item, ward, "10", batch="ELSEWHERE", days=1)

        usable = await inventory.lock_usable_stock(hospital_id, item.id, store.id, on=TODAY)
        one = await inventory.lock_usable_stock(
            hospital_id, item.id, store.id, on=TODAY, batch_number="LATE"
        )

        assert [r.batch_number for r in usable] == ["TODAY", "SOON", "LATE", "NODATE"]
        assert [r.batch_number for r in one] == ["LATE"]
        # Held 60 in all; the expired ten are held but not usable.
        assert await inventory.totals_by_item(hospital_id, on=TODAY, item_ids=[item.id]) == {
            item.id: (Decimal("60.00"), Decimal("50.00"))
        }
        assert await inventory.totals_by_item(hospital_id, on=TODAY, item_ids=[]) == {}
        assert item.id in await inventory.totals_by_item(hospital_id, on=TODAY)

    async def test_stock_reads_are_tenant_scoped_and_filterable(
        self,
        inventory: InventoryRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        gloves = await insert_item(db_session, hospital_id, "GLOVE", name="Gloves")
        masks = await insert_item(db_session, hospital_id, "MASK", name="Masks")
        store = await insert_location(db_session, hospital_id, "STORE")
        ward = await insert_location(db_session, hospital_id, "WARD")
        await insert_stock(db_session, masks, store, "5")
        await insert_stock(db_session, gloves, ward, "3")
        await insert_stock(db_session, gloves, store, "0")
        await insert_stock(
            db_session,
            await insert_item(db_session, other_hospital_id, "GLOVE"),
            await insert_location(db_session, other_hospital_id, "STORE"),
            "99",
        )

        async def rows(**filters: Any) -> list[tuple[str, str]]:
            return [
                (r.item.name, r.location.code)
                for r in await inventory.list_stock(hospital_id, **filters)
            ]

        assert await rows() == [("Gloves", "STORE"), ("Gloves", "WARD"), ("Masks", "STORE")]
        assert await rows(in_stock_only=True) == [("Gloves", "WARD"), ("Masks", "STORE")]
        assert await rows(item_id=gloves.id, in_stock_only=True) == [("Gloves", "WARD")]
        assert await rows(location_id=store.id, skip=1, limit=5) == [("Masks", "STORE")]
        assert await inventory.count_stock(hospital_id, location_id=store.id) == 2
        assert await inventory.list_stock(other_hospital_id, item_id=gloves.id) == []
        assert (
            await inventory.lock_usable_stock(other_hospital_id, gloves.id, ward.id, on=TODAY) == []
        )
        assert await inventory.list_movements(other_hospital_id, item_id=gloves.id) == []
        assert (
            await inventory.totals_by_item(other_hospital_id, on=TODAY, item_ids=[gloves.id]) == {}
        )


class TestPurchaseOrders:
    async def test_round_trip_constraints_and_tenant_scoping(
        self, db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
    ) -> None:
        orders = InventoryPurchaseOrderRepository(db_session)
        vendor = await ProcurementRepository(db_session).create_vendor(
            hospital_id=hospital_id, name="Acme"
        )
        gloves = await insert_item(db_session, hospital_id, "GLOVE")
        masks = await insert_item(db_session, hospital_id, "MASK")

        def line(item: Any, quantity: str) -> dict[str, Any]:
            return {
                "item_id": item.id,
                "quantity": Decimal(quantity),
                "unit_price": Decimal("3.00"),
                "total": Decimal("3.00") * Decimal(quantity),
            }

        order = await orders.create_purchase_order(
            hospital_id=hospital_id,
            vendor_id=vendor.id,
            po_number="IPO-1",
            items=[line(masks, "10"), line(gloves, "2.5")],
        )
        second = await orders.create_purchase_order(
            hospital_id=hospital_id,
            vendor_id=vendor.id,
            po_number="IPO-2",
            items=[line(gloves, "1")],
        )

        assert order.status is PurchaseOrderStatus.DRAFT
        assert [(i.position, i.item.sku, i.quantity) for i in order.items] == [
            (0, "MASK", Decimal("10.00")),
            (1, "GLOVE", Decimal("2.50")),
        ]
        assert order.vendor.name == "Acme"
        with pytest.raises(IntegrityError, match="uq_inventory_purchase_orders_hospital_number"):
            async with db_session.begin_nested():
                await orders.create_purchase_order(
                    hospital_id=hospital_id, vendor_id=vendor.id, po_number="IPO-1", items=[]
                )
        with pytest.raises(IntegrityError, match="uq_inventory_po_items_po_item"):
            async with db_session.begin_nested():
                await orders.create_purchase_order(
                    hospital_id=hospital_id,
                    vendor_id=vendor.id,
                    po_number="IPO-3",
                    items=[line(gloves, "1"), line(gloves, "2")],
                )

        sent = await orders.update_purchase_order(
            order, status=PurchaseOrderStatus.SENT, ordered_at=NOW - timedelta(minutes=1)
        )
        assert sent.items[0].item.sku == "MASK"  # still loaded after the update
        assert await orders.get_purchase_order_by_id(other_hospital_id, order.id) is None
        assert (
            await orders.get_purchase_order_by_id(other_hospital_id, order.id, for_update=True)
        ) is None
        assert (
            await orders.get_purchase_order_by_id(hospital_id, order.id, for_update=True)
        ) is order
        assert await orders.list_purchase_orders(other_hospital_id) == []
        assert await orders.count_purchase_orders(hospital_id) == 2
        drafts = await orders.list_purchase_orders(hospital_id, status=PurchaseOrderStatus.DRAFT)
        assert [o.id for o in drafts] == [second.id]
        assert await orders.count_purchase_orders(hospital_id, vendor_id=vendor.id) == 2
