"""API tests for the Inventory endpoints.

Real app, real services, real repositories, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3). Notifications
is the real module too, so a low-stock alert here really lands in someone's
notification centre.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.dependencies.db import get_db_session
from app.api.dependencies.services import get_audit_sink
from app.main import create_app
from app.models.inventory import InventoryLocationKind, InventoryMovement, InventoryStock
from app.repositories.procurement_repository import ProcurementRepository
from app.tests.billing_helpers import auth_headers, insert_user_with_permissions
from app.tests.conftest import RecordingAuditSink
from app.tests.inventory_helpers import insert_item, insert_location, insert_stock

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.inventory import InventoryItem, InventoryLocation

pytestmark = pytest.mark.database

BASE = "/api/v1/inventory"
ALL = [
    "inventory.item.read",
    "inventory.item.create",
    "inventory.item.update",
    "inventory.location.read",
    "inventory.location.create",
    "inventory.location.update",
    "inventory.stock.read",
    "inventory.consume",
    "inventory.transfer",
    "inventory.adjust",
    "inventory.po.read",
    "inventory.po.create",
    "inventory.po.update",
    "inventory.po.receive",
]
TODAY = datetime.now(UTC).date()


def _in(days: int) -> str:
    """An ISO date ``days`` from today."""
    return (TODAY + timedelta(days=days)).isoformat()


@pytest.fixture
def audit() -> RecordingAuditSink:
    """The sink the app records to for the duration of a test."""
    return RecordingAuditSink()


@pytest_asyncio.fixture
async def api(db_session: AsyncSession, audit: RecordingAuditSink) -> AsyncGenerator[AsyncClient]:
    """An HTTP client sharing the test's rolled-back session and audit sink."""
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override
    application.dependency_overrides[get_audit_sink] = lambda: audit
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """A user holding every inventory permission, who also reads notifications."""
    user = await insert_user_with_permissions(
        db_session, hospital_id, [*ALL, "notification.read.own"]
    )
    return auth_headers(user.id, hospital_id)


@pytest_asyncio.fixture
async def other_tenant(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> dict[str, str]:
    """A fully-permissioned user in a different hospital."""
    user = await insert_user_with_permissions(db_session, other_hospital_id, ALL)
    return auth_headers(user.id, other_hospital_id)


@pytest_asyncio.fixture
async def store(db_session: AsyncSession, hospital_id: uuid.UUID) -> InventoryLocation:
    """The general store."""
    return await insert_location(db_session, hospital_id, "STORE")


@pytest_asyncio.fixture
async def ward(db_session: AsyncSession, hospital_id: uuid.UUID) -> InventoryLocation:
    """A ward."""
    return await insert_location(db_session, hospital_id, "WARD-A", kind=InventoryLocationKind.WARD)


@pytest_asyncio.fixture
async def gloves(db_session: AsyncSession, hospital_id: uuid.UUID) -> InventoryItem:
    """Gloves: untracked, reorder at 20, target 80."""
    return await insert_item(
        db_session, hospital_id, "GLOVE-M", name="Nitrile gloves", reorder_point=20, target_stock=80
    )


@pytest_asyncio.fixture
async def cannula(db_session: AsyncSession, hospital_id: uuid.UUID) -> InventoryItem:
    """Cannulas: batch-tracked."""
    return await insert_item(
        db_session, hospital_id, "CANN-20G", name="IV cannula", is_batch_tracked=True
    )


def _first_field_error(response: Any) -> str:
    """Return the field named by a service-level 422 (it sits at ``errors.errors``)."""
    return str(response.json()["errors"]["errors"][0]["field"])


# ── Authorization ───────────────────────────────────────────────────────────

_ID = str(uuid.uuid4())
_MOVE = {"item_id": _ID, "location_id": _ID, "quantity": "1"}
ENDPOINTS: list[tuple[str, str, str, Any]] = [
    ("POST", f"{BASE}/items", "inventory.item.create", {"sku": "X", "name": "X"}),
    ("GET", f"{BASE}/items", "inventory.item.read", None),
    ("GET", f"{BASE}/items/{_ID}", "inventory.item.read", None),
    ("PATCH", f"{BASE}/items/{_ID}", "inventory.item.update", {"name": "Y"}),
    ("POST", f"{BASE}/locations", "inventory.location.create", {"name": "X", "code": "X"}),
    ("GET", f"{BASE}/locations", "inventory.location.read", None),
    ("PATCH", f"{BASE}/locations/{_ID}", "inventory.location.update", {"name": "Y"}),
    ("GET", f"{BASE}/stock", "inventory.stock.read", None),
    ("GET", f"{BASE}/stock/summary", "inventory.stock.read", None),
    ("GET", f"{BASE}/movements", "inventory.stock.read", None),
    ("POST", f"{BASE}/consume", "inventory.consume", _MOVE),
    (
        "POST",
        f"{BASE}/transfer",
        "inventory.transfer",
        {
            "item_id": _ID,
            "from_location_id": _ID,
            "to_location_id": str(uuid.uuid4()),
            "quantity": "1",
        },
    ),
    (
        "POST",
        f"{BASE}/adjust",
        "inventory.adjust",
        {"item_id": _ID, "location_id": _ID, "quantity_change": "1", "note": "x"},
    ),
    (
        "POST",
        f"{BASE}/purchase-orders",
        "inventory.po.create",
        {"vendor_id": _ID, "items": [{"item_id": _ID, "quantity": "1", "unit_price": "1.00"}]},
    ),
    ("GET", f"{BASE}/purchase-orders", "inventory.po.read", None),
    ("GET", f"{BASE}/purchase-orders/{_ID}", "inventory.po.read", None),
    ("POST", f"{BASE}/purchase-orders/{_ID}/send", "inventory.po.update", None),
    ("POST", f"{BASE}/purchase-orders/{_ID}/cancel", "inventory.po.update", None),
    (
        "POST",
        f"{BASE}/purchase-orders/{_ID}/receive",
        "inventory.po.receive",
        {"location_id": _ID, "items": [{"po_item_id": _ID, "quantity": "1"}]},
    ),
]
_IDS = [f"{method} {url.replace(_ID, ':id').removeprefix(BASE)}" for method, url, _, _ in ENDPOINTS]


class TestAuthorization:
    @pytest.mark.parametrize(("method", "url", "permission", "body"), ENDPOINTS, ids=_IDS)
    async def test_no_token_is_401(
        self, api: AsyncClient, method: str, url: str, permission: str, body: Any
    ) -> None:
        assert (await api.request(method, url, json=body)).status_code == 401

    @pytest.mark.parametrize(("method", "url", "permission", "body"), ENDPOINTS, ids=_IDS)
    async def test_every_other_inventory_permission_together_is_still_403(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        method: str,
        url: str,
        permission: str,
        body: Any,
    ) -> None:
        others = [code for code in ALL if code != permission]
        user = await insert_user_with_permissions(db_session, hospital_id, others)

        response = await api.request(
            method, url, json=body, headers=auth_headers(user.id, hospital_id)
        )

        assert response.status_code == 403

    @pytest.mark.parametrize(("method", "url", "permission", "body"), ENDPOINTS, ids=_IDS)
    async def test_the_one_permission_is_enough_to_get_past_the_gate(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        method: str,
        url: str,
        permission: str,
        body: Any,
    ) -> None:
        user = await insert_user_with_permissions(db_session, hospital_id, [permission])

        response = await api.request(
            method, url, json=body, headers=auth_headers(user.id, hospital_id)
        )

        assert response.status_code not in (401, 403)


# ── Items and locations ─────────────────────────────────────────────────────


class TestItemsAndLocations:
    async def test_item_lifecycle(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        created = await api.post(
            f"{BASE}/items",
            json={
                "sku": "syr-5",
                "name": "Syringe 5 mL",
                "category": "Disposables",
                "unit_of_measure": "box of 50",
                "reorder_point": 20,
                "target_stock": 60,
            },
            headers=admin,
        )
        assert created.status_code == 201, created.text
        item = created.json()["data"]
        assert (item["sku"], item["is_batch_tracked"], item["is_active"]) == ("SYR-5", False, True)
        url = f"{BASE}/items/{item['id']}"

        duplicate = await api.post(
            f"{BASE}/items", json={"sku": "SYR-5", "name": "x"}, headers=admin
        )
        elsewhere = await api.post(
            f"{BASE}/items", json={"sku": "SYR-5", "name": "x"}, headers=other_tenant
        )
        searched = await api.get(f"{BASE}/items", params={"q": "syr"}, headers=admin)
        updated = await api.patch(url, json={"reorder_point": 25}, headers=admin)
        below = await api.patch(url, json={"target_stock": 10}, headers=admin)
        immutable = await api.patch(url, json={"is_batch_tracked": True}, headers=admin)
        foreign = [
            await api.get(url, headers=other_tenant),
            await api.patch(url, json={"name": "x"}, headers=other_tenant),
        ]

        assert duplicate.status_code == 409
        assert elsewhere.status_code == 201
        assert [i["sku"] for i in searched.json()["data"]] == ["SYR-5"]
        assert updated.json()["data"]["reorder_point"] == 25
        assert below.status_code == 422
        assert _first_field_error(below) == "target_stock"
        assert immutable.status_code == 422
        assert [r.status_code for r in foreign] == [404, 404]
        assert audit.last().action == "inventory.item.updated"
        assert audit.last().changes == {"reorder_point": {"before": 20, "after": 25}}

    @pytest.mark.parametrize(
        "body",
        [
            {"sku": "has space", "name": "x"},
            {"sku": "X", "name": " "},
            {"sku": "X", "name": "x", "reorder_point": 50, "target_stock": 10},
            {"sku": "X", "name": "x", "reorder_point": -1},
            {"sku": "X", "name": "x", "hospital_id": str(uuid.uuid4())},
        ],
    )
    async def test_a_bad_item_is_422(
        self, api: AsyncClient, admin: dict[str, str], body: dict[str, Any]
    ) -> None:
        assert (await api.post(f"{BASE}/items", json=body, headers=admin)).status_code == 422

    async def test_location_lifecycle(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        created = await api.post(
            f"{BASE}/locations",
            json={"name": "Intensive care", "code": "icu", "kind": "icu"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        location = created.json()["data"]
        assert (location["code"], location["kind"], location["is_active"]) == ("ICU", "icu", True)

        duplicate = await api.post(
            f"{BASE}/locations", json={"name": "x", "code": "ICU"}, headers=admin
        )
        bad_kind = await api.post(
            f"{BASE}/locations", json={"name": "x", "code": "Y", "kind": "garage"}, headers=admin
        )
        updated = await api.patch(
            f"{BASE}/locations/{location['id']}", json={"is_active": False}, headers=admin
        )
        foreign = await api.patch(
            f"{BASE}/locations/{location['id']}", json={"name": "x"}, headers=other_tenant
        )
        active = await api.get(f"{BASE}/locations", params={"is_active": "true"}, headers=admin)

        assert (duplicate.status_code, bad_kind.status_code, foreign.status_code) == (409, 422, 404)
        assert updated.json()["data"]["is_active"] is False
        assert active.json()["data"] == []
        assert (await api.get(f"{BASE}/locations", headers=other_tenant)).json()["data"] == []
        assert audit.actions() == ["inventory.location.created", "inventory.location.updated"]


# ── Stock movements ─────────────────────────────────────────────────────────


class TestStock:
    async def test_adjust_consume_transfer_and_the_ledger_agrees(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        store: InventoryLocation,
        ward: InventoryLocation,
        gloves: InventoryItem,
        audit: RecordingAuditSink,
    ) -> None:
        body = {"item_id": str(gloves.id), "location_id": str(store.id)}

        opening = await api.post(
            f"{BASE}/adjust",
            json={**body, "quantity_change": "100", "note": "Opening balance"},
            headers=admin,
        )
        assert opening.status_code == 200, opening.text
        assert opening.json()["data"]["summary"]["quantity_on_hand"] == "100.00"
        moved = await api.post(
            f"{BASE}/transfer",
            json={
                "item_id": str(gloves.id),
                "from_location_id": str(store.id),
                "to_location_id": str(ward.id),
                "quantity": "30",
            },
            headers=admin,
        )
        assert moved.status_code == 200, moved.text
        reasons = [m["reason"] for m in moved.json()["data"]["movements"]]
        assert reasons == ["transferred_out", "transferred_in"]
        used = await api.post(
            f"{BASE}/consume",
            json={
                "item_id": str(gloves.id),
                "location_id": str(ward.id),
                "quantity": "4.5",
                "note": "Round",
            },
            headers=admin,
        )
        assert used.status_code == 200, used.text
        assert used.json()["data"]["movements"][0]["quantity_change"] == "-4.50"
        assert used.json()["data"]["summary"]["usable_quantity"] == "95.50"

        rows = (await api.get(f"{BASE}/stock", headers=admin)).json()["data"]
        assert [(r["location_code"], r["quantity"]) for r in rows] == [
            ("STORE", "70.00"),
            ("WARD-A", "25.50"),
        ]
        on_ward = await api.get(
            f"{BASE}/stock", params={"location_id": str(ward.id)}, headers=admin
        )
        assert len(on_ward.json()["data"]) == 1
        ledger = await api.get(
            f"{BASE}/movements", params={"item_id": str(gloves.id)}, headers=admin
        )
        assert ledger.json()["metadata"]["pagination"]["total_records"] == 4
        consumed = await api.get(f"{BASE}/movements", params={"reason": "consumed"}, headers=admin)
        assert [m["note"] for m in consumed.json()["data"]] == ["Round"]

        # AC-4: every stock row equals the sum of its movements.
        drift = await db_session.execute(
            select(func.count())
            .select_from(InventoryStock)
            .where(
                InventoryStock.item_id == gloves.id,
                InventoryStock.quantity
                != select(func.coalesce(func.sum(InventoryMovement.quantity_change), 0))
                .where(InventoryMovement.stock_id == InventoryStock.id)
                .scalar_subquery(),
            )
        )
        assert drift.scalar_one() == 0
        assert audit.actions() == [
            "inventory.adjusted",
            "inventory.transferred",
            "inventory.consumed",
        ]

    async def test_a_shortage_is_409_and_nothing_changes(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        store: InventoryLocation,
        ward: InventoryLocation,
        gloves: InventoryItem,
        audit: RecordingAuditSink,
    ) -> None:
        stock = await insert_stock(db_session, gloves, ward, "3")
        await insert_stock(db_session, gloves, store, "500")  # elsewhere does not count

        consume = await api.post(
            f"{BASE}/consume",
            json={"item_id": str(gloves.id), "location_id": str(ward.id), "quantity": "5"},
            headers=admin,
        )
        transfer = await api.post(
            f"{BASE}/transfer",
            json={
                "item_id": str(gloves.id),
                "from_location_id": str(ward.id),
                "to_location_id": str(store.id),
                "quantity": "5",
            },
            headers=admin,
        )
        overdraw = await api.post(
            f"{BASE}/adjust",
            json={
                "item_id": str(gloves.id),
                "location_id": str(ward.id),
                "quantity_change": "-4",
                "note": "x",
            },
            headers=admin,
        )

        assert (consume.status_code, transfer.status_code, overdraw.status_code) == (409, 409, 400)
        assert consume.json()["errors"] == {"requested": "5.00", "available": "3.00"}
        assert "Nothing was changed" in consume.json()["message"]
        await db_session.refresh(stock)
        assert str(stock.quantity) == "3.00"
        assert audit.events == []

    async def test_tracked_stock_leaves_earliest_expiry_first_and_expired_never(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        store: InventoryLocation,
        cannula: InventoryItem,
    ) -> None:
        await insert_stock(db_session, cannula, store, "50", batch="LATE", days=300)
        await insert_stock(db_session, cannula, store, "5", batch="SOON", days=20)
        await insert_stock(db_session, cannula, store, "40", batch="OLD", days=-10)
        body = {"item_id": str(cannula.id), "location_id": str(store.id)}

        used = await api.post(f"{BASE}/consume", json={**body, "quantity": "8"}, headers=admin)
        too_many = await api.post(f"{BASE}/consume", json={**body, "quantity": "48"}, headers=admin)

        assert [
            (m["batch_number"], m["quantity_change"]) for m in used.json()["data"]["movements"]
        ] == [
            ("SOON", "-5.00"),
            ("LATE", "-3.00"),
        ]
        summary = used.json()["data"]["summary"]
        # The expired forty are held, but not usable.
        assert (summary["quantity_on_hand"], summary["usable_quantity"]) == ("87.00", "47.00")
        assert too_many.status_code == 409
        rows = (
            await api.get(f"{BASE}/stock", params={"item_id": str(cannula.id)}, headers=admin)
        ).json()["data"]
        assert {r["batch_number"]: r["is_expired"] for r in rows} == {"OLD": True, "LATE": False}

    async def test_batch_rules_and_bad_requests_are_422(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        store: InventoryLocation,
        gloves: InventoryItem,
        cannula: InventoryItem,
    ) -> None:
        def adjust(item: Any, **extra: Any) -> dict[str, Any]:
            return {
                "item_id": str(item.id),
                "location_id": str(store.id),
                "quantity_change": "5",
                "note": "x",
                **extra,
            }

        cases = [
            (adjust(cannula), "batch_number"),
            (adjust(gloves, batch_number="B1"), "batch_number"),
            (adjust(gloves, item_id=str(uuid.uuid4())), "item_id"),
            (adjust(gloves, location_id=str(uuid.uuid4())), "location_id"),
        ]
        for body, field in cases:
            response = await api.post(f"{BASE}/adjust", json=body, headers=admin)
            assert response.status_code == 422, response.text
            assert _first_field_error(response) == field
        for body in (
            adjust(gloves, note=" "),
            adjust(gloves, quantity_change="0"),
            adjust(gloves, quantity_change="5", reason="expired"),
            {k: v for k, v in adjust(gloves).items() if k != "note"},
        ):
            assert (await api.post(f"{BASE}/adjust", json=body, headers=admin)).status_code == 422
        same_place = await api.post(
            f"{BASE}/transfer",
            json={
                "item_id": str(gloves.id),
                "from_location_id": str(store.id),
                "to_location_id": str(store.id),
                "quantity": "1",
            },
            headers=admin,
        )
        negative = await api.post(
            f"{BASE}/consume",
            json={"item_id": str(gloves.id), "location_id": str(store.id), "quantity": "-1"},
            headers=admin,
        )
        assert (same_place.status_code, negative.status_code) == (422, 422)

    async def test_another_hospital_cannot_see_or_move_our_stock(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        other_tenant: dict[str, str],
        store: InventoryLocation,
        gloves: InventoryItem,
    ) -> None:
        stock = await insert_stock(db_session, gloves, store, "50")
        body = {"item_id": str(gloves.id), "location_id": str(store.id)}

        consume = await api.post(
            f"{BASE}/consume", json={**body, "quantity": "1"}, headers=other_tenant
        )
        adjust = await api.post(
            f"{BASE}/adjust",
            json={**body, "quantity_change": "-1", "note": "x"},
            headers=other_tenant,
        )

        assert (consume.status_code, adjust.status_code) == (422, 422)
        assert _first_field_error(consume) == "item_id"
        for path in ("stock", "stock/summary", "movements", "items", "locations"):
            assert (await api.get(f"{BASE}/{path}", headers=other_tenant)).json()["data"] == []
        await db_session.refresh(stock)
        assert str(stock.quantity) == "50.00"


class TestLowStock:
    async def test_ac2_the_alert_fires_at_the_threshold_once_and_shows_on_the_panel(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        store: InventoryLocation,
        gloves: InventoryItem,
        cannula: InventoryItem,
        hospital_id: uuid.UUID,
    ) -> None:
        await insert_stock(db_session, gloves, store, "22")  # reorder at 20, target 80
        await insert_stock(
            db_session, cannula, store, "5", batch="B1", days=100
        )  # no reorder point
        # Someone else who can raise a purchase order, and someone who cannot.
        buyer = await insert_user_with_permissions(
            db_session, hospital_id, ["inventory.po.create", "notification.read.own"]
        )
        nurse = await insert_user_with_permissions(
            db_session, hospital_id, ["inventory.consume", "notification.read.own"]
        )
        body = {"item_id": str(gloves.id), "location_id": str(store.id), "quantity": "1"}

        async def centre(headers: dict[str, str]) -> list[dict[str, Any]]:
            return list((await api.get("/api/v1/notifications", headers=headers)).json()["data"])

        above = await api.post(f"{BASE}/consume", json=body, headers=admin)  # 21
        assert above.json()["data"]["summary"]["is_low"] is False
        assert await centre(auth_headers(buyer.id, hospital_id)) == []

        crossing = await api.post(f"{BASE}/consume", json=body, headers=admin)  # 20
        summary = crossing.json()["data"]["summary"]
        assert (summary["is_low"], summary["suggested_order_quantity"]) == (True, "60.00")
        await api.post(f"{BASE}/consume", json=body, headers=admin)  # 19: already low

        [notice] = await centre(auth_headers(buyer.id, hospital_id))
        assert notice["kind"] == "inventory.low_stock"
        assert notice["title"] == "Nitrile gloves is running low"
        assert "down to 20 box" in notice["body"]
        assert notice["link"] == "/inventory"
        assert len(await centre(admin)) == 1  # the admin can reorder too
        assert await centre(auth_headers(nurse.id, hospital_id)) == []

        panel = await api.get(f"{BASE}/stock/summary", params={"low_stock": "true"}, headers=admin)
        assert [(s["item"]["sku"], s["usable_quantity"]) for s in panel.json()["data"]] == [
            ("GLOVE-M", "19.00")
        ]
        everything = await api.get(f"{BASE}/stock/summary", headers=admin)
        assert {s["item"]["sku"]: s["is_low"] for s in everything.json()["data"]} == {
            "CANN-20G": False,
            "GLOVE-M": True,
        }

    async def test_an_item_never_stocked_is_low_and_reported_like_any_other(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        hospital_id: uuid.UUID,
    ) -> None:
        await insert_item(db_session, hospital_id, "SHEET-S", reorder_point=40, target_stock=120)

        panel = await api.get(f"{BASE}/stock/summary", params={"low_stock": "true"}, headers=admin)

        [summary] = panel.json()["data"]
        # Two decimal places even though no stock row exists to take them from.
        assert summary["quantity_on_hand"] == "0.00"
        assert summary["usable_quantity"] == "0.00"
        assert summary["suggested_order_quantity"] == "120.00"
        assert summary["is_low"] is True


# ── Purchase orders ─────────────────────────────────────────────────────────


class TestPurchaseOrders:
    async def test_order_send_receive_puts_stock_on_the_shelf(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        store: InventoryLocation,
        gloves: InventoryItem,
        cannula: InventoryItem,
        hospital_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        # The vendor is Pharmacy's: the table is shared.
        vendor = await ProcurementRepository(db_session).create_vendor(
            hospital_id=hospital_id, name="Acme"
        )
        drafted = await api.post(
            f"{BASE}/purchase-orders",
            json={
                "vendor_id": str(vendor.id),
                "notes": "Deliver to the store.",
                "items": [
                    {"item_id": str(gloves.id), "quantity": "60", "unit_price": "320.00"},
                    {"item_id": str(cannula.id), "quantity": "400", "unit_price": "18.50"},
                ],
            },
            headers=admin,
        )
        assert drafted.status_code == 201, drafted.text
        order = drafted.json()["data"]
        url = f"{BASE}/purchase-orders/{order['id']}"
        assert (order["status"], order["total_amount"], order["vendor_name"]) == (
            "draft",
            "26600.00",
            "Acme",
        )
        assert order["po_number"].startswith("IPO-")
        receipt = {
            "location_id": str(store.id),
            "items": [
                {"po_item_id": order["items"][0]["id"], "quantity": "55"},
                {
                    "po_item_id": order["items"][1]["id"],
                    "quantity": "250",
                    "batch_number": "cn-1",
                    "expiry_date": _in(400),
                },
                {
                    "po_item_id": order["items"][1]["id"],
                    "quantity": "150",
                    "batch_number": "CN-2",
                    "expiry_date": _in(500),
                },
            ],
        }

        too_early = await api.post(f"{url}/receive", json=receipt, headers=admin)
        foreign = await api.post(f"{url}/send", headers=other_tenant)
        sent = await api.post(f"{url}/send", headers=admin)
        missing_batch = await api.post(
            f"{url}/receive",
            json={
                "location_id": str(store.id),
                "items": [{"po_item_id": order["items"][1]["id"], "quantity": "1"}],
            },
            headers=admin,
        )
        received = await api.post(f"{url}/receive", json=receipt, headers=admin)
        twice = await api.post(f"{url}/receive", json=receipt, headers=admin)
        cancel = await api.post(f"{url}/cancel", headers=admin)

        assert (too_early.status_code, foreign.status_code) == (400, 404)
        assert sent.json()["data"]["status"] == "sent"
        assert missing_batch.status_code == 422
        assert _first_field_error(missing_batch) == "items.0.batch_number"
        assert received.status_code == 200, received.text
        assert received.json()["data"]["status"] == "received"
        assert received.json()["data"]["received_location_id"] == str(store.id)
        assert (twice.status_code, cancel.status_code) == (400, 400)
        rows = (await api.get(f"{BASE}/stock", headers=admin)).json()["data"]
        assert [(r["item_sku"], r["batch_number"], r["quantity"]) for r in rows] == [
            ("CANN-20G", "CN-1", "250.00"),
            ("CANN-20G", "CN-2", "150.00"),
            ("GLOVE-M", None, "55.00"),  # a short delivery is recorded as it came
        ]
        ledger = await api.get(f"{BASE}/movements", params={"reason": "received"}, headers=admin)
        assert {m["reference_id"] for m in ledger.json()["data"]} == {order["id"]}
        listed = await api.get(
            f"{BASE}/purchase-orders", params={"status": "received"}, headers=admin
        )
        assert [o["id"] for o in listed.json()["data"]] == [order["id"]]
        assert (await api.get(url, headers=other_tenant)).status_code == 404
        assert audit.actions() == [
            "inventory.po.created",
            "inventory.po.sent",
            "inventory.po.received",
        ]

    async def test_a_bad_order_is_422_and_a_draft_can_be_cancelled(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        gloves: InventoryItem,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        procurement = ProcurementRepository(db_session)
        vendor = await procurement.create_vendor(hospital_id=hospital_id, name="Acme")
        theirs = await procurement.create_vendor(hospital_id=other_hospital_id, name="Elsewhere")
        line = {"item_id": str(gloves.id), "quantity": "10", "unit_price": "1.00"}

        async def post(body: dict[str, Any]) -> Any:
            return await api.post(f"{BASE}/purchase-orders", json=body, headers=admin)

        foreign_vendor = await post({"vendor_id": str(theirs.id), "items": [line]})
        unknown_item = await post(
            {"vendor_id": str(vendor.id), "items": [{**line, "item_id": str(uuid.uuid4())}]}
        )
        repeated = await post({"vendor_id": str(vendor.id), "items": [line, line]})
        order = (await post({"vendor_id": str(vendor.id), "items": [line]})).json()["data"]
        cancelled = await api.post(f"{BASE}/purchase-orders/{order['id']}/cancel", headers=admin)

        assert _first_field_error(foreign_vendor) == "vendor_id"
        assert _first_field_error(unknown_item) == "items.0.item_id"
        assert repeated.status_code == 422
        assert cancelled.json()["data"]["status"] == "cancelled"
