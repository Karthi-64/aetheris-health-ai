"""Unit tests for the Inventory services.

The repositories are small in-memory fakes rather than mocks: what matters is
what is on the shelf and in the ledger afterwards, and that reads more clearly
as assertions on rows than on call arguments. The SQL, including the locking,
is covered in the repository and integration suites.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import BusinessRuleError, ValidationError
from app.models.inventory import (
    InventoryItem,
    InventoryLocation,
    InventoryLocationKind,
    InventoryMovement,
    InventoryMovementReason,
    InventoryPurchaseOrder,
    InventoryPurchaseOrderItem,
    InventoryStock,
)
from app.models.pharmacy import PurchaseOrderStatus
from app.schemas.common import PaginationParams
from app.schemas.inventory import (
    AdjustStockRequest,
    ConsumeRequest,
    CreateInventoryItemRequest,
    CreateInventoryPurchaseOrderRequest,
    CreateLocationRequest,
    ReceiveInventoryPurchaseOrderRequest,
    TransferRequest,
    UpdateInventoryItemRequest,
    UpdateLocationRequest,
)
from app.services.inventory_po_service import (
    InventoryPurchaseOrderNotFoundError,
    InventoryPurchaseOrderService,
    InventoryPurchaseOrderStateError,
)
from app.services.inventory_service import (
    DuplicateInventorySkuError,
    DuplicateLocationCodeError,
    InsufficientInventoryError,
    InventoryItemNotFoundError,
    InventoryLocationNotFoundError,
    InventoryService,
)
from app.tests.conftest import FakeSession, RecordingAuditSink

HOSPITAL_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
TODAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin "today", so expiry rules are tested on a known day."""

    async def today(*_: Any) -> date:
        return TODAY

    for module in ("inventory_service", "inventory_po_service"):
        monkeypatch.setattr(f"app.services.{module}.hospital_today", today)


class FakeInventory:
    """In-memory stand-in for ``InventoryRepository``."""

    def __init__(self) -> None:
        self.items: dict[uuid.UUID, InventoryItem] = {}
        self.locations: dict[uuid.UUID, InventoryLocation] = {}
        self.stock: list[InventoryStock] = []
        self.movements: list[InventoryMovement] = []
        self.raise_on_create: Exception | None = None

    # Builders used by tests.
    def item(self, sku: str, **overrides: Any) -> InventoryItem:
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "hospital_id": HOSPITAL_ID,
            "sku": sku,
            "name": sku.title(),
            "category": "Disposables",
            "unit_of_measure": "box",
            "is_batch_tracked": False,
            "reorder_point": None,
            "target_stock": None,
            "is_active": True,
            "created_at": NOW,
            "updated_at": NOW,
        }
        values.update(overrides)
        item = InventoryItem(**values)
        self.items[item.id] = item
        return item

    def location(self, code: str, **overrides: Any) -> InventoryLocation:
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "hospital_id": HOSPITAL_ID,
            "name": code.title(),
            "code": code,
            "kind": InventoryLocationKind.STORE,
            "is_active": True,
        }
        values.update(overrides)
        location = InventoryLocation(**values)
        self.locations[location.id] = location
        return location

    def put(
        self,
        item: InventoryItem,
        location: InventoryLocation,
        quantity: str,
        *,
        batch: str | None = None,
        days: int | None = None,
    ) -> InventoryStock:
        row = InventoryStock(
            id=uuid.uuid4(),
            hospital_id=item.hospital_id,
            item_id=item.id,
            location_id=location.id,
            batch_number=batch,
            expiry_date=TODAY + timedelta(days=days) if days is not None else None,
            quantity=Decimal(quantity),
        )
        row.__dict__["item"] = item
        row.__dict__["location"] = location
        self.stock.append(row)
        return row

    # Repository surface.
    async def create_item(self, **fields: Any) -> InventoryItem:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        fields.pop("created_by", None)
        fields.pop("hospital_id", None)
        return self.item(fields.pop("sku"), **fields)

    async def update_item(self, item: InventoryItem, *, updated_by: Any = None, **f: Any) -> Any:
        for name, value in f.items():
            setattr(item, name, value)
        return item

    async def get_item_by_id(self, hospital_id: uuid.UUID, item_id: uuid.UUID) -> Any:
        item = self.items.get(item_id)
        return item if item is not None and item.hospital_id == hospital_id else None

    async def get_items_by_ids(self, hospital_id: uuid.UUID, ids: Any) -> list[InventoryItem]:
        return [i for i in self.items.values() if i.id in ids and i.hospital_id == hospital_id]

    async def get_item_by_sku(self, hospital_id: uuid.UUID, sku: str) -> Any:
        return next((i for i in self.items.values() if i.sku == sku), None)

    async def list_items(self, hospital_id: uuid.UUID, *, skip: int, limit: int, **f: Any) -> Any:
        rows = [i for i in self.items.values() if f.get("is_active") in (None, i.is_active)]
        return sorted(rows, key=lambda i: i.name)[skip : skip + limit]

    async def count_items(self, hospital_id: uuid.UUID, **f: Any) -> int:
        return len([i for i in self.items.values() if f.get("is_active") in (None, i.is_active)])

    async def create_location(self, **fields: Any) -> InventoryLocation:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        fields.pop("created_by", None)
        fields.pop("hospital_id", None)
        return self.location(fields.pop("code"), **fields)

    async def update_location(self, location: Any, *, updated_by: Any = None, **f: Any) -> Any:
        for name, value in f.items():
            setattr(location, name, value)
        return location

    async def get_location_by_id(self, hospital_id: uuid.UUID, location_id: uuid.UUID) -> Any:
        location = self.locations.get(location_id)
        return location if location is not None and location.hospital_id == hospital_id else None

    async def get_location_by_code(self, hospital_id: uuid.UUID, code: str) -> Any:
        return next((loc for loc in self.locations.values() if loc.code == code), None)

    async def list_locations(self, hospital_id: uuid.UUID, **f: Any) -> Any:
        return [
            loc for loc in self.locations.values() if f.get("is_active") in (None, loc.is_active)
        ]

    async def get_or_create_stock(
        self,
        *,
        item: Any,
        location: Any,
        batch_number: Any = None,
        expiry_date: Any = None,
        **_: Any,
    ) -> InventoryStock:
        for row in self.stock:
            if (row.item_id, row.location_id, row.batch_number) == (
                item.id,
                location.id,
                batch_number,
            ):
                return row
        row = self.put(item, location, "0", batch=batch_number)
        row.expiry_date = expiry_date
        return row

    async def apply_movement(self, stock: InventoryStock, **fields: Any) -> InventoryMovement:
        movement = InventoryMovement(
            id=uuid.uuid4(),
            hospital_id=stock.hospital_id,
            item_id=stock.item_id,
            location_id=stock.location_id,
            stock_id=stock.id,
            **fields,
        )
        movement.__dict__["stock"] = stock
        self.movements.append(movement)
        stock.quantity += fields["quantity_change"]
        return movement

    def _usable(self, row: InventoryStock, on: date) -> bool:
        return row.expiry_date is None or row.expiry_date >= on

    async def lock_usable_stock(
        self,
        hospital_id: uuid.UUID,
        item_id: uuid.UUID,
        location_id: uuid.UUID,
        *,
        on: date,
        batch_number: Any = None,
    ) -> list[InventoryStock]:
        rows = [
            r
            for r in self.stock
            if (r.item_id, r.location_id) == (item_id, location_id)
            and r.quantity > 0
            and self._usable(r, on)
            and batch_number in (None, r.batch_number)
        ]
        return sorted(rows, key=lambda r: (r.expiry_date is None, r.expiry_date or TODAY))

    async def list_stock(self, hospital_id: uuid.UUID, *, skip: int, limit: int, **f: Any) -> Any:
        rows = [r for r in self.stock if not f.get("in_stock_only") or r.quantity > 0]
        return rows[skip : skip + limit]

    async def count_stock(self, hospital_id: uuid.UUID, **f: Any) -> int:
        return len([r for r in self.stock if not f.get("in_stock_only") or r.quantity > 0])

    async def totals_by_item(
        self, hospital_id: uuid.UUID, *, on: date, item_ids: Any = None
    ) -> Any:
        totals: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
        for row in self.stock:
            if item_ids is not None and row.item_id not in item_ids:
                continue
            held, usable = totals.get(row.item_id, (Decimal("0"), Decimal("0")))
            totals[row.item_id] = (
                held + row.quantity,
                usable + (row.quantity if self._usable(row, on) else Decimal("0")),
            )
        return totals

    async def list_movements(
        self, hospital_id: uuid.UUID, *, skip: int, limit: int, **_: Any
    ) -> Any:
        return list(reversed(self.movements))[skip : skip + limit]

    async def count_movements(self, hospital_id: uuid.UUID, **_: Any) -> int:
        return len(self.movements)


class _Stores:
    """An inventory service over fakes, with a store, a ward and some items."""

    def __init__(self) -> None:
        self.repo = FakeInventory()
        self.session = FakeSession()
        self.audit = RecordingAuditSink()
        self.notifier = AsyncMock()
        self.departments = AsyncMock()
        self.departments.get_department_by_id.return_value = MagicMock()
        self.service = InventoryService(
            self.repo,  # type: ignore[arg-type]
            self.departments,
            AsyncMock(),
            self.session,  # type: ignore[arg-type]
            self.audit,
            notifier=self.notifier,
        )
        self.store = self.repo.location("STORE")
        self.ward = self.repo.location("WARD-A", kind=InventoryLocationKind.WARD)
        self.gloves = self.repo.item("GLOVE-M", name="Gloves", reorder_point=20, target_stock=80)
        self.cannula = self.repo.item("CANN", name="Cannula", is_batch_tracked=True)

    async def consume(self, item: Any, location: Any, quantity: str, **extra: Any) -> Any:
        return await self.service.consume(
            HOSPITAL_ID,
            ConsumeRequest.model_validate(
                {
                    "item_id": str(item.id),
                    "location_id": str(location.id),
                    "quantity": quantity,
                    **extra,
                }
            ),
            actor_id=ACTOR_ID,
        )

    async def adjust(self, item: Any, location: Any, change: str, **extra: Any) -> Any:
        body = {
            "item_id": str(item.id),
            "location_id": str(location.id),
            "quantity_change": change,
            "note": "Stock count",
            **extra,
        }
        return await self.service.adjust(
            HOSPITAL_ID, AdjustStockRequest.model_validate(body), actor_id=ACTOR_ID
        )

    async def transfer(
        self, item: Any, source: Any, target: Any, quantity: str, **extra: Any
    ) -> Any:
        return await self.service.transfer(
            HOSPITAL_ID,
            TransferRequest.model_validate(
                {
                    "item_id": str(item.id),
                    "from_location_id": str(source.id),
                    "to_location_id": str(target.id),
                    "quantity": quantity,
                    **extra,
                }
            ),
            actor_id=ACTOR_ID,
        )


# ── Items and locations ─────────────────────────────────────────────────────


class TestItems:
    async def test_create_normalises_and_audits(self) -> None:
        stores = _Stores()

        created = await stores.service.create_item(
            HOSPITAL_ID,
            CreateInventoryItemRequest(
                sku="syr-5", name=" Syringe 5 mL ", reorder_point=20, target_stock=60
            ),
            actor_id=ACTOR_ID,
        )

        assert (created.sku, created.name, created.unit_of_measure) == (
            "SYR-5",
            "Syringe 5 mL",
            "unit",
        )
        assert (created.reorder_point, created.target_stock, created.is_batch_tracked) == (
            20,
            60,
            False,
        )
        assert stores.session.commits == 1
        assert stores.audit.last().action == "inventory.item.created"

    async def test_a_duplicate_sku_is_a_409_from_the_check_or_the_database(self) -> None:
        stores = _Stores()
        request = CreateInventoryItemRequest(sku="GLOVE-M", name="x")

        with pytest.raises(DuplicateInventorySkuError):
            await stores.service.create_item(HOSPITAL_ID, request)

        stores.repo.items.clear()
        stores.repo.raise_on_create = IntegrityError(
            "INSERT", {}, Exception('violates "uq_inventory_items_hospital_sku"')
        )
        with pytest.raises(DuplicateInventorySkuError):
            await stores.service.create_item(HOSPITAL_ID, request)
        stores.repo.raise_on_create = IntegrityError("INSERT", {}, Exception("other"))
        with pytest.raises(IntegrityError):
            await stores.service.create_item(HOSPITAL_ID, request)

    async def test_update_applies_only_what_changed(self) -> None:
        stores = _Stores()

        updated = await stores.service.update_item(
            HOSPITAL_ID,
            stores.gloves.id,
            UpdateInventoryItemRequest.model_validate({"reorder_point": 30, "is_active": True}),
            actor_id=ACTOR_ID,
        )
        commits = stores.session.commits
        await stores.service.update_item(
            HOSPITAL_ID,
            stores.gloves.id,
            UpdateInventoryItemRequest.model_validate({"is_active": True}),
        )

        assert updated.reorder_point == 30
        assert stores.audit.last().changes == {"reorder_point": {"before": 20, "after": 30}}
        assert stores.session.commits == commits

    async def test_the_target_cannot_be_left_below_the_reorder_point(self) -> None:
        stores = _Stores()

        with pytest.raises(ValidationError) as excinfo:
            await stores.service.update_item(
                HOSPITAL_ID,
                stores.gloves.id,
                UpdateInventoryItemRequest.model_validate({"reorder_point": 90}),
            )

        assert excinfo.value.detail["errors"][0]["field"] == "target_stock"

    async def test_get_list_and_404(self) -> None:
        stores = _Stores()

        assert (await stores.service.get_item(HOSPITAL_ID, stores.gloves.id)).sku == "GLOVE-M"
        page = await stores.service.list_items(
            HOSPITAL_ID, pagination=PaginationParams(page=1, page_size=1)
        )
        assert (len(page.items), page.total_records) == (1, 2)
        with pytest.raises(InventoryItemNotFoundError):
            await stores.service.get_item(uuid.uuid4(), stores.gloves.id)
        with pytest.raises(InventoryItemNotFoundError):
            await stores.service.update_item(
                HOSPITAL_ID, uuid.uuid4(), UpdateInventoryItemRequest.model_validate({"name": "X"})
            )


class TestLocations:
    async def test_create_update_and_list(self) -> None:
        stores = _Stores()

        icu = await stores.service.create_location(
            HOSPITAL_ID,
            CreateLocationRequest(name="ICU", code="icu", kind=InventoryLocationKind.ICU),
            actor_id=ACTOR_ID,
        )
        updated = await stores.service.update_location(
            HOSPITAL_ID,
            icu.id,
            UpdateLocationRequest.model_validate({"kind": "ward", "is_active": False}),
            actor_id=ACTOR_ID,
        )
        commits = stores.session.commits
        await stores.service.update_location(
            HOSPITAL_ID, icu.id, UpdateLocationRequest.model_validate({"kind": "ward"})
        )

        assert (icu.code, icu.kind) == ("ICU", InventoryLocationKind.ICU)
        assert (updated.kind, updated.is_active) == (InventoryLocationKind.WARD, False)
        assert stores.audit.last().changes == {
            "kind": {"before": "icu", "after": "ward"},
            "is_active": {"before": True, "after": False},
        }
        assert stores.session.commits == commits
        active = await stores.service.list_locations(HOSPITAL_ID, is_active=True)
        assert sorted(loc.code for loc in active) == ["STORE", "WARD-A"]

    async def test_a_duplicate_code_is_a_409_and_an_unknown_location_a_404(self) -> None:
        stores = _Stores()
        request = CreateLocationRequest(name="Store", code="STORE")

        with pytest.raises(DuplicateLocationCodeError):
            await stores.service.create_location(HOSPITAL_ID, request)
        stores.repo.locations.clear()
        stores.repo.raise_on_create = IntegrityError(
            "INSERT", {}, Exception('violates "uq_inventory_locations_hospital_code"')
        )
        with pytest.raises(DuplicateLocationCodeError):
            await stores.service.create_location(HOSPITAL_ID, request)
        stores.repo.raise_on_create = IntegrityError("INSERT", {}, Exception("other"))
        with pytest.raises(IntegrityError):
            await stores.service.create_location(HOSPITAL_ID, request)
        with pytest.raises(InventoryLocationNotFoundError):
            await stores.service.update_location(
                HOSPITAL_ID, uuid.uuid4(), UpdateLocationRequest.model_validate({"name": "X"})
            )


# ── Consume ─────────────────────────────────────────────────────────────────


class TestConsume:
    async def test_consumption_is_a_ledger_movement_and_lowers_the_stock(self) -> None:
        stores = _Stores()
        row = stores.repo.put(stores.gloves, stores.ward, "40")
        department_id = uuid.uuid4()

        result = await stores.consume(
            stores.gloves,
            stores.ward,
            "2.5",
            department_id=str(department_id),
            note="Dressing round",
        )

        assert row.quantity == Decimal("37.5")
        [movement] = result.movements
        assert movement.quantity_change == Decimal("-2.5")
        assert movement.reason is InventoryMovementReason.CONSUMED
        assert (movement.department_id, movement.note) == (department_id, "Dressing round")
        assert (movement.moved_by, movement.reference_type) == (ACTOR_ID, "consumption")
        assert result.summary.usable_quantity == Decimal("37.5")
        assert result.summary.is_low is False
        event = stores.audit.last()
        assert event.action == "inventory.consumed"
        assert event.context["quantity"] == "2.5"
        assert stores.session.commits == 1

    async def test_ac4_the_ledger_reconstructs_the_stock(self) -> None:
        stores = _Stores()
        await stores.adjust(stores.gloves, stores.store, "100")
        await stores.consume(stores.gloves, stores.store, "7")
        await stores.transfer(stores.gloves, stores.store, stores.ward, "30")
        await stores.consume(stores.gloves, stores.ward, "4")
        await stores.adjust(stores.gloves, stores.ward, "-1", reason="expired")

        for row in stores.repo.stock:
            ledger = sum(
                (m.quantity_change for m in stores.repo.movements if m.stock_id == row.id),
                Decimal("0"),
            )
            assert ledger == row.quantity
        assert sorted(r.quantity for r in stores.repo.stock) == [Decimal(25), Decimal(63)]

    async def test_stock_leaves_earliest_expiry_first_and_undated_last(self) -> None:
        stores = _Stores()
        undated = stores.repo.put(stores.cannula, stores.store, "50", batch="NODATE")
        late = stores.repo.put(stores.cannula, stores.store, "50", batch="LATE", days=300)
        soon = stores.repo.put(stores.cannula, stores.store, "5", batch="SOON", days=20)

        result = await stores.consume(stores.cannula, stores.store, "60")

        assert [(m.batch_number, m.quantity_change) for m in result.movements] == [
            ("SOON", Decimal(-5)),
            ("LATE", Decimal(-50)),
            ("NODATE", Decimal(-5)),
        ]
        assert (soon.quantity, late.quantity, undated.quantity) == (
            Decimal(0),
            Decimal(0),
            Decimal(45),
        )

    async def test_later_batches_are_not_touched_once_the_need_is_met(self) -> None:
        stores = _Stores()
        soon = stores.repo.put(stores.cannula, stores.store, "50", batch="SOON", days=20)
        late = stores.repo.put(stores.cannula, stores.store, "50", batch="LATE", days=300)

        result = await stores.consume(stores.cannula, stores.store, "10")

        assert len(result.movements) == 1
        assert (soon.quantity, late.quantity) == (Decimal(40), Decimal(50))

    async def test_expired_stock_is_never_used(self) -> None:
        # §14: "expired batches → quantity available drops".
        stores = _Stores()
        expired = stores.repo.put(stores.cannula, stores.store, "100", batch="OLD", days=-1)
        stores.repo.put(
            stores.cannula, stores.store, "3", batch="GOOD", days=0
        )  # today still counts

        with pytest.raises(InsufficientInventoryError) as excinfo:
            await stores.consume(stores.cannula, stores.store, "4")

        assert excinfo.value.status_code == 409
        assert excinfo.value.detail == {"requested": "4.00", "available": "3.00"}
        assert "4 requested, 3 usable" in excinfo.value.message
        assert expired.quantity == 100

    async def test_a_shortage_changes_nothing(self) -> None:
        stores = _Stores()
        row = stores.repo.put(stores.gloves, stores.ward, "3")

        with pytest.raises(InsufficientInventoryError, match="Nothing was changed"):
            await stores.consume(stores.gloves, stores.ward, "5")

        assert row.quantity == 3
        assert stores.repo.movements == []
        assert stores.session.commits == 0
        assert stores.audit.events == []

    async def test_stock_at_another_location_does_not_count(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.gloves, stores.store, "500")

        with pytest.raises(InsufficientInventoryError):
            await stores.consume(stores.gloves, stores.ward, "1")

    async def test_a_named_batch_is_the_only_one_drawn_on(self) -> None:
        stores = _Stores()
        soon = stores.repo.put(stores.cannula, stores.store, "5", batch="SOON", days=20)
        late = stores.repo.put(stores.cannula, stores.store, "50", batch="LATE", days=300)

        await stores.consume(stores.cannula, stores.store, "3", batch_number="late")

        assert (soon.quantity, late.quantity) == (Decimal(5), Decimal(47))

    async def test_unknown_references_are_422s_naming_the_field(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.gloves, stores.ward, "10")
        ghost = MagicMock()
        ghost.id = uuid.uuid4()
        stores.departments.get_department_by_id.return_value = None

        async def field_of(*args: Any, **kwargs: Any) -> str:
            with pytest.raises(ValidationError) as excinfo:
                await stores.consume(*args, **kwargs)
            return str(excinfo.value.detail["errors"][0]["field"])

        assert await field_of(ghost, stores.ward, "1") == "item_id"
        assert await field_of(stores.gloves, ghost, "1") == "location_id"
        assert (
            await field_of(stores.gloves, stores.ward, "1", department_id=str(uuid.uuid4()))
            == "department_id"
        )

    async def test_an_inactive_item_cannot_be_consumed(self) -> None:
        stores = _Stores()
        stores.gloves.is_active = False

        with pytest.raises(BusinessRuleError, match="inactive"):
            await stores.consume(stores.gloves, stores.ward, "1")


class TestLowStockAlert:
    async def test_ac2_crossing_the_reorder_point_notifies_those_who_can_reorder(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.gloves, stores.store, "20")
        stores.repo.put(stores.gloves, stores.ward, "5")  # 25 in the hospital; reorder at 20

        result = await stores.consume(stores.gloves, stores.ward, "5")

        assert result.summary.is_low is True
        assert result.summary.usable_quantity == Decimal(20)
        # Back to the target of 80.
        assert result.summary.suggested_order_quantity == Decimal(60)
        request = stores.notifier.notify.await_args.args[0]
        assert request.kind == "inventory.low_stock"
        assert request.hospital_id == HOSPITAL_ID
        assert request.recipient_permission == "inventory.po.create"
        assert request.variables["item_name"] == "Gloves"
        # Written for a person: "20", not "20.00" and not "2E+1".
        assert request.variables["quantity"] == "20"
        assert request.variables["reorder_point"] == "20"

    async def test_it_fires_once_not_on_every_use_while_low(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.gloves, stores.store, "22")

        await stores.consume(stores.gloves, stores.store, "1")  # 21: still above
        await stores.consume(stores.gloves, stores.store, "1")  # 20: crosses
        await stores.consume(stores.gloves, stores.store, "1")  # 19: already low

        assert stores.notifier.notify.await_count == 1

    async def test_an_item_with_no_reorder_point_never_alerts(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.cannula, stores.store, "2", batch="B1", days=100)

        result = await stores.consume(stores.cannula, stores.store, "2")

        assert result.summary.is_low is False
        stores.notifier.notify.assert_not_awaited()

    async def test_a_write_off_that_crosses_the_point_alerts_too(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.gloves, stores.store, "25")

        await stores.adjust(stores.gloves, stores.store, "-10", reason="expired")

        stores.notifier.notify.assert_awaited_once()

    async def test_a_transfer_never_alerts_it_moves_nothing_out_of_the_hospital(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.gloves, stores.store, "21")

        result = await stores.transfer(stores.gloves, stores.store, stores.ward, "21")

        assert result.summary.usable_quantity == Decimal(21)
        stores.notifier.notify.assert_not_awaited()


# ── Transfer and adjust ─────────────────────────────────────────────────────


class TestTransfer:
    async def test_batches_keep_their_identity_and_the_pair_shares_a_reference(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.cannula, stores.store, "5", batch="SOON", days=20)
        stores.repo.put(stores.cannula, stores.store, "50", batch="LATE", days=300)

        result = await stores.transfer(
            stores.cannula, stores.store, stores.ward, "8", note="Top-up"
        )

        moved = [(m.reason.value, m.batch_number, m.quantity_change) for m in result.movements]
        assert moved == [
            ("transferred_out", "SOON", Decimal(-5)),
            ("transferred_in", "SOON", Decimal(5)),
            ("transferred_out", "LATE", Decimal(-3)),
            ("transferred_in", "LATE", Decimal(3)),
        ]
        assert len({m.reference_id for m in result.movements}) == 1
        on_ward = {
            r.batch_number: (r.quantity, r.expiry_date)
            for r in stores.repo.stock
            if r.location_id == stores.ward.id
        }
        assert on_ward == {
            "SOON": (Decimal(5), TODAY + timedelta(days=20)),
            "LATE": (Decimal(3), TODAY + timedelta(days=300)),
        }
        # Nothing was created or destroyed.
        assert result.summary.quantity_on_hand == Decimal(55)
        assert stores.audit.last().action == "inventory.transferred"

    async def test_a_second_transfer_tops_up_the_same_destination_row(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.gloves, stores.store, "50")

        await stores.transfer(stores.gloves, stores.store, stores.ward, "10")
        await stores.transfer(stores.gloves, stores.store, stores.ward, "5")

        ward_rows = [r for r in stores.repo.stock if r.location_id == stores.ward.id]
        assert [r.quantity for r in ward_rows] == [Decimal(15)]

    async def test_a_shortage_at_the_source_changes_nothing(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.gloves, stores.store, "3")

        with pytest.raises(InsufficientInventoryError):
            await stores.transfer(stores.gloves, stores.store, stores.ward, "4")

        assert stores.repo.movements == []
        assert len(stores.repo.stock) == 1

    async def test_nothing_is_sent_to_a_closed_location_but_it_can_be_emptied(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.gloves, stores.store, "10")
        stores.repo.put(stores.gloves, stores.ward, "10")
        stores.ward.is_active = False

        with pytest.raises(BusinessRuleError, match="cannot receive stock"):
            await stores.transfer(stores.gloves, stores.store, stores.ward, "1")
        out = await stores.transfer(stores.gloves, stores.ward, stores.store, "10")

        assert len(out.movements) == 2

    async def test_unknown_locations_are_422s_naming_the_field(self) -> None:
        stores = _Stores()
        ghost = MagicMock()
        ghost.id = uuid.uuid4()

        with pytest.raises(ValidationError) as source:
            await stores.transfer(stores.gloves, ghost, stores.ward, "1")
        with pytest.raises(ValidationError) as target:
            await stores.transfer(stores.gloves, stores.store, ghost, "1")

        assert source.value.detail["errors"][0]["field"] == "from_location_id"
        assert target.value.detail["errors"][0]["field"] == "to_location_id"


class TestAdjust:
    async def test_a_positive_adjustment_creates_the_opening_balance(self) -> None:
        stores = _Stores()

        result = await stores.adjust(stores.gloves, stores.store, "60")

        [row] = stores.repo.stock
        assert row.quantity == Decimal(60)
        [movement] = result.movements
        assert (movement.reason, movement.note) == (InventoryMovementReason.ADJUSTED, "Stock count")
        event = stores.audit.last()
        assert event.action == "inventory.adjusted"
        assert event.changes == {"quantity": {"before": "0", "after": "60"}}

    async def test_a_tracked_item_needs_a_batch_and_an_untracked_one_must_not_have_one(
        self,
    ) -> None:
        stores = _Stores()

        with pytest.raises(ValidationError, match="batch-tracked: give"):
            await stores.adjust(stores.cannula, stores.store, "10")
        with pytest.raises(ValidationError, match="not batch-tracked"):
            await stores.adjust(stores.gloves, stores.store, "10", batch_number="B1")
        tracked = await stores.adjust(
            stores.cannula, stores.store, "10", batch_number="b1", expiry_date="2027-01-31"
        )

        assert tracked.movements[0].batch_number == "B1"
        assert stores.repo.stock[0].expiry_date == date(2027, 1, 31)

    async def test_stock_cannot_be_taken_below_zero(self) -> None:
        # §11: "quantity ≥ 0 after any movement".
        stores = _Stores()
        row = stores.repo.put(stores.gloves, stores.store, "2")

        with pytest.raises(BusinessRuleError, match="holds 2"):
            await stores.adjust(stores.gloves, stores.store, "-3")

        assert row.quantity == 2
        assert stores.repo.movements == []

    async def test_an_inactive_item_can_still_be_written_off(self) -> None:
        stores = _Stores()
        row = stores.repo.put(stores.gloves, stores.store, "5")
        stores.gloves.is_active = False

        await stores.adjust(stores.gloves, stores.store, "-5", reason="expired")

        assert row.quantity == 0


class TestStockReads:
    async def test_the_summary_separates_held_from_usable_and_lists_empty_items(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.gloves, stores.store, "15")
        stores.repo.put(stores.cannula, stores.store, "30", batch="OLD", days=-5)
        stores.repo.put(stores.cannula, stores.store, "10", batch="NEW", days=100)
        sheets = stores.repo.item("SHEET", name="Sheets", reorder_point=40)
        stores.repo.item("OLD", name="Withdrawn", is_active=False, reorder_point=5)

        everything = await stores.service.stock_summary(HOSPITAL_ID)
        low = await stores.service.stock_summary(
            HOSPITAL_ID, low_stock_only=True, pagination=PaginationParams(page=1, page_size=1)
        )

        by_sku = {s.item.sku: s for s in everything.items}
        assert set(by_sku) == {"GLOVE-M", "CANN", "SHEET"}  # the inactive one is left out
        assert (by_sku["CANN"].quantity_on_hand, by_sku["CANN"].usable_quantity) == (
            Decimal(40),
            Decimal(10),
        )
        assert by_sku["CANN"].is_low is False  # no reorder point
        assert by_sku["GLOVE-M"].is_low is True
        assert by_sku["GLOVE-M"].suggested_order_quantity == Decimal(65)
        # Never stocked at all: the lowest of the lot, ordered up to its reorder point.
        assert (by_sku["SHEET"].usable_quantity, by_sku["SHEET"].is_low) == (Decimal(0), True)
        assert by_sku["SHEET"].suggested_order_quantity == Decimal(40)
        assert low.total_records == 2
        assert len(low.items) == 1
        assert sheets.id in {
            s.item.id
            for s in (await stores.service.stock_summary(HOSPITAL_ID, low_stock_only=True)).items
        }

    async def test_stock_rows_and_the_ledger_are_listed(self) -> None:
        stores = _Stores()
        stores.repo.put(stores.cannula, stores.store, "30", batch="OLD", days=-5)
        stores.repo.put(stores.gloves, stores.store, "0")
        await stores.adjust(stores.gloves, stores.store, "5")

        held = await stores.service.list_stock(HOSPITAL_ID)
        everything = await stores.service.list_stock(HOSPITAL_ID, in_stock_only=False)
        ledger = await stores.service.list_movements(HOSPITAL_ID)

        assert held.total_records == 2
        assert everything.total_records == 2
        expired = next(r for r in held.items if r.batch_number == "OLD")
        assert (expired.is_expired, expired.item_sku, expired.location_code) == (
            True,
            "CANN",
            "STORE",
        )
        assert [m.quantity_change for m in ledger.items] == [Decimal(5)]


# ── Purchase orders ─────────────────────────────────────────────────────────


class FakeOrders:
    """In-memory stand-in for ``InventoryPurchaseOrderRepository``."""

    def __init__(self, inventory: FakeInventory, vendor: Any) -> None:
        self._inventory = inventory
        self._vendor = vendor
        self.orders: dict[uuid.UUID, InventoryPurchaseOrder] = {}

    async def create_purchase_order(self, *, items: list[dict[str, Any]], **fields: Any) -> Any:
        fields.pop("created_by", None)
        order = InventoryPurchaseOrder(
            id=uuid.uuid4(),
            status=PurchaseOrderStatus.DRAFT,
            ordered_at=None,
            received_at=None,
            received_location_id=None,
            created_at=NOW,
            items=[],
            **fields,
        )
        for position, values in enumerate(items):
            line = InventoryPurchaseOrderItem(
                id=uuid.uuid4(), hospital_id=fields["hospital_id"], position=position, **values
            )
            line.__dict__["item"] = self._inventory.items[values["item_id"]]
            order.items.append(line)
        order.__dict__["vendor"] = self._vendor
        self.orders[order.id] = order
        return order

    async def update_purchase_order(self, order: Any, *, updated_by: Any = None, **f: Any) -> Any:
        for name, value in f.items():
            setattr(order, name, value)
        return order

    async def get_purchase_order_by_id(
        self, hospital_id: uuid.UUID, oid: uuid.UUID, **_: Any
    ) -> Any:
        order = self.orders.get(oid)
        return order if order is not None and order.hospital_id == hospital_id else None

    async def list_purchase_orders(
        self, hospital_id: uuid.UUID, *, skip: int, limit: int, **f: Any
    ) -> Any:
        rows = [o for o in self.orders.values() if f.get("status") in (None, o.status)]
        return rows[skip : skip + limit]

    async def count_purchase_orders(self, hospital_id: uuid.UUID, **f: Any) -> int:
        return len([o for o in self.orders.values() if f.get("status") in (None, o.status)])


class _Buying:
    """A purchase-order service over fakes."""

    def __init__(self) -> None:
        self.stores = _Stores()
        self.vendor = MagicMock()
        self.vendor.id = uuid.uuid4()
        self.vendor.name = "Acme Supplies"
        self.vendor.is_active = True
        self.vendors = AsyncMock()
        self.vendors.get_vendor_by_id.return_value = self.vendor
        self.orders = FakeOrders(self.stores.repo, self.vendor)
        self.session = FakeSession()
        self.audit = RecordingAuditSink()
        self.service = InventoryPurchaseOrderService(
            self.orders,  # type: ignore[arg-type]
            self.stores.repo,  # type: ignore[arg-type]
            self.vendors,
            AsyncMock(),
            self.session,  # type: ignore[arg-type]
            self.audit,
        )

    async def order(self, **overrides: Any) -> Any:
        body: dict[str, Any] = {
            "vendor_id": str(self.vendor.id),
            "items": [
                {"item_id": str(self.stores.gloves.id), "quantity": "60", "unit_price": "320.00"},
                {"item_id": str(self.stores.cannula.id), "quantity": "400", "unit_price": "18.50"},
            ],
        }
        body.update(overrides)
        return await self.service.create_purchase_order(
            HOSPITAL_ID, CreateInventoryPurchaseOrderRequest.model_validate(body), actor_id=ACTOR_ID
        )

    async def sent(self) -> Any:
        order = await self.order()
        return await self.service.send_purchase_order(HOSPITAL_ID, order.id, actor_id=ACTOR_ID)

    def receipt(self, order: Any, *lines: dict[str, Any], location: Any = None) -> Any:
        default = [
            {"po_item_id": str(order.items[0].id), "quantity": "60"},
            {
                "po_item_id": str(order.items[1].id),
                "quantity": "400",
                "batch_number": "cn-1",
                "expiry_date": (TODAY + timedelta(days=400)).isoformat(),
            },
        ]
        return ReceiveInventoryPurchaseOrderRequest.model_validate(
            {
                "location_id": str((location or self.stores.store).id),
                "items": list(lines) or default,
            }
        )


class TestPurchaseOrders:
    async def test_an_order_is_drafted_with_computed_totals(self) -> None:
        buying = _Buying()

        order = await buying.order(notes="Deliver to the store.")

        assert order.status is PurchaseOrderStatus.DRAFT
        assert order.po_number.startswith("IPO-")
        assert order.vendor_name == "Acme Supplies"
        assert [(i.item_sku, i.total) for i in order.items] == [
            ("GLOVE-M", Decimal("19200.00")),
            ("CANN", Decimal("7400.00")),
        ]
        assert order.total_amount == Decimal("26600.00")
        assert buying.audit.last().action == "inventory.po.created"

    async def test_a_fractional_quantity_is_priced_to_two_places(self) -> None:
        buying = _Buying()

        order = await buying.order(
            items=[
                {"item_id": str(buying.stores.gloves.id), "quantity": "2.5", "unit_price": "3.33"}
            ]
        )

        assert order.items[0].total == Decimal("8.32")  # 8.325, half to even

    async def test_an_order_needs_an_active_known_vendor_and_items(self) -> None:
        buying = _Buying()
        retired = buying.stores.repo.item("OLD", is_active=False)

        async def field_of(**overrides: Any) -> str:
            with pytest.raises(ValidationError) as excinfo:
                await buying.order(**overrides)
            return str(excinfo.value.detail["errors"][0]["field"])

        line = {"quantity": "1", "unit_price": "1.00"}
        assert await field_of(items=[{"item_id": str(uuid.uuid4()), **line}]) == "items.0.item_id"
        assert await field_of(items=[{"item_id": str(retired.id), **line}]) == "items.0.item_id"
        buying.vendor.is_active = False
        assert await field_of() == "vendor_id"
        buying.vendors.get_vendor_by_id.return_value = None
        assert await field_of() == "vendor_id"

    async def test_rule_5_receiving_puts_stock_on_the_shelf_through_the_ledger(self) -> None:
        buying = _Buying()
        order = await buying.sent()

        received = await buying.service.receive_purchase_order(
            HOSPITAL_ID, order.id, buying.receipt(order), actor_id=ACTOR_ID
        )

        assert received.status is PurchaseOrderStatus.RECEIVED
        assert received.received_location_id == buying.stores.store.id
        stock = {(r.item.sku, r.batch_number): r.quantity for r in buying.stores.repo.stock}
        assert stock == {("GLOVE-M", None): Decimal(60), ("CANN", "CN-1"): Decimal(400)}
        assert {m.reason for m in buying.stores.repo.movements} == {
            InventoryMovementReason.RECEIVED
        }
        assert {(m.reference_type, m.reference_id) for m in buying.stores.repo.movements} == {
            ("purchase_order", order.id)
        }
        event = buying.audit.last()
        assert event.action == "inventory.po.received"
        assert event.context["location"] == "STORE"
        assert event.context["received"][1] == {"item": "CANN", "batch": "CN-1", "quantity": "400"}

    async def test_a_bad_receipt_is_a_422_naming_the_line(self) -> None:
        buying = _Buying()
        order = await buying.sent()
        gloves, cannula = str(order.items[0].id), str(order.items[1].id)

        async def field_of(*lines: dict[str, Any], location: Any = None) -> str:
            with pytest.raises(ValidationError) as excinfo:
                await buying.service.receive_purchase_order(
                    HOSPITAL_ID, order.id, buying.receipt(order, *lines, location=location)
                )
            return str(excinfo.value.detail["errors"][0]["field"])

        ghost = MagicMock()
        ghost.id = uuid.uuid4()
        yesterday = (TODAY - timedelta(days=1)).isoformat()
        assert await field_of(location=ghost) == "location_id"
        assert (
            await field_of({"po_item_id": str(uuid.uuid4()), "quantity": "1"})
            == "items.0.po_item_id"
        )
        assert await field_of({"po_item_id": cannula, "quantity": "1"}) == "items.0.batch_number"
        assert (
            await field_of({"po_item_id": gloves, "quantity": "1", "batch_number": "B1"})
            == "items.0.batch_number"
        )
        assert (
            await field_of(
                {
                    "po_item_id": cannula,
                    "quantity": "1",
                    "batch_number": "B1",
                    "expiry_date": yesterday,
                }
            )
            == "items.0.expiry_date"
        )
        assert buying.stores.repo.movements == []

    async def test_a_batch_already_held_under_another_expiry_is_refused(self) -> None:
        buying = _Buying()
        order = await buying.sent()
        buying.stores.repo.put(
            buying.stores.cannula, buying.stores.store, "5", batch="CN-1", days=100
        )

        with pytest.raises(ValidationError, match="already recorded as expiring"):
            await buying.service.receive_purchase_order(
                HOSPITAL_ID, order.id, buying.receipt(order)
            )

    async def test_goods_are_not_received_into_a_closed_location(self) -> None:
        buying = _Buying()
        order = await buying.sent()
        buying.stores.store.is_active = False

        with pytest.raises(BusinessRuleError, match="cannot receive stock"):
            await buying.service.receive_purchase_order(
                HOSPITAL_ID, order.id, buying.receipt(order)
            )

    async def test_only_a_sent_order_can_be_received_and_only_once(self) -> None:
        buying = _Buying()
        draft = await buying.order()

        with pytest.raises(InventoryPurchaseOrderStateError, match="draft"):
            await buying.service.receive_purchase_order(
                HOSPITAL_ID, draft.id, buying.receipt(draft)
            )
        await buying.service.send_purchase_order(HOSPITAL_ID, draft.id)
        await buying.service.receive_purchase_order(HOSPITAL_ID, draft.id, buying.receipt(draft))
        for step in (
            buying.service.send_purchase_order(HOSPITAL_ID, draft.id),
            buying.service.cancel_purchase_order(HOSPITAL_ID, draft.id),
            buying.service.receive_purchase_order(HOSPITAL_ID, draft.id, buying.receipt(draft)),
        ):
            with pytest.raises(InventoryPurchaseOrderStateError, match="received"):
                await step

    async def test_cancel_get_list_and_404(self) -> None:
        buying = _Buying()
        order = await buying.sent()

        cancelled = await buying.service.cancel_purchase_order(
            HOSPITAL_ID, order.id, actor_id=ACTOR_ID
        )

        assert cancelled.status is PurchaseOrderStatus.CANCELLED
        assert buying.audit.last().changes == {"status": {"before": "sent", "after": "cancelled"}}
        assert (await buying.service.get_purchase_order(HOSPITAL_ID, order.id)).id == order.id
        listed = await buying.service.list_purchase_orders(
            HOSPITAL_ID, status=PurchaseOrderStatus.CANCELLED
        )
        assert listed.total_records == 1
        with pytest.raises(InventoryPurchaseOrderNotFoundError):
            await buying.service.get_purchase_order(uuid.uuid4(), order.id)
        with pytest.raises(InventoryPurchaseOrderNotFoundError):
            await buying.service.send_purchase_order(uuid.uuid4(), order.id)
