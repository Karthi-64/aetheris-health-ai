"""API tests for the Pharmacy endpoints.

Real app, real services, real repositories, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3). Billing is the
real module too, so a dispense here really is charged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.dependencies.db import get_db_session
from app.api.dependencies.services import get_audit_sink
from app.main import create_app
from app.models.appointment import AppointmentStatus
from app.models.billing import Invoice
from app.models.pharmacy import MedicineBatch, StockMovement
from app.repositories.hospital_repository import HospitalRepository
from app.services.pharmacy_common import hospital_today
from app.tests.billing_helpers import (
    auth_headers,
    insert_appointment,
    insert_doctor,
    insert_patient,
    insert_user_with_permissions,
)
from app.tests.conftest import RecordingAuditSink
from app.tests.pharmacy_helpers import insert_batch, insert_medicine

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.appointment import Appointment
    from app.models.pharmacy import Medicine

pytestmark = pytest.mark.database

MEDICINES = "/api/v1/medicines"
PRESCRIPTIONS = "/api/v1/prescriptions"
VENDORS = "/api/v1/vendors"
ORDERS = "/api/v1/purchase-orders"

ALL = [
    "pharmacy.medicine.read",
    "pharmacy.medicine.create",
    "pharmacy.medicine.update",
    "pharmacy.batch.read",
    "pharmacy.batch.create",
    "pharmacy.batch.update",
    "pharmacy.prescription.read",
    "pharmacy.prescription.create",
    "pharmacy.dispense.execute",
    "pharmacy.po.read",
    "pharmacy.po.create",
    "pharmacy.po.update",
    "pharmacy.po.receive",
    "pharmacy.vendor.read",
    "pharmacy.vendor.create",
    "pharmacy.vendor.update",
]

TODAY = datetime.now(UTC).date()


def _in(days: int) -> str:
    """An ISO date ``days`` from today. Far enough out that timezones cannot matter."""
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
    """A user holding every pharmacy permission."""
    user = await insert_user_with_permissions(db_session, hospital_id, ALL)
    return auth_headers(user.id, hospital_id)


@pytest_asyncio.fixture
async def other_tenant(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> dict[str, str]:
    """A fully-permissioned user in a different hospital."""
    user = await insert_user_with_permissions(db_session, other_hospital_id, ALL)
    return auth_headers(user.id, other_hospital_id)


@pytest_asyncio.fixture
async def appointment(db_session: AsyncSession, hospital_id: uuid.UUID) -> Appointment:
    """A visit in progress."""
    patient = await insert_patient(db_session, hospital_id)
    doctor = await insert_doctor(db_session, hospital_id)
    return await insert_appointment(
        db_session,
        hospital_id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        status=AppointmentStatus.IN_PROGRESS,
    )


@pytest_asyncio.fixture
async def para(db_session: AsyncSession, hospital_id: uuid.UUID) -> Medicine:
    """Paracetamol at 2.50, nothing in stock yet."""
    return await insert_medicine(db_session, hospital_id, "PARA", name="Paracetamol")


@pytest_asyncio.fixture
async def amox(db_session: AsyncSession, hospital_id: uuid.UUID) -> Medicine:
    """Amoxicillin at 8.00, nothing in stock yet."""
    return await insert_medicine(
        db_session, hospital_id, "AMOX", name="Amoxicillin", unit_price="8.00"
    )


async def _prescribe(
    api: AsyncClient, headers: dict[str, str], appointment: Appointment, *lines: tuple[Any, int]
) -> dict[str, Any]:
    """Write a prescription through the API and return the data block."""
    items = []
    for medicine, quantity in lines:
        line: dict[str, Any] = {
            "dosage": "1 tablet",
            "frequency": "twice daily",
            "quantity": quantity,
        }
        if isinstance(medicine, str):
            line["medicine_name"] = medicine
        else:
            line["medicine_id"] = str(medicine.id)
        items.append(line)
    response = await api.post(
        PRESCRIPTIONS, json={"appointment_id": str(appointment.id), "items": items}, headers=headers
    )
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


def _first_field_error(response: Any) -> str:
    """Return the field named by a service-level 422 (it sits at ``errors.errors``)."""
    return str(response.json()["errors"]["errors"][0]["field"])


# ── Authorization ───────────────────────────────────────────────────────────

_ID = str(uuid.uuid4())
_MEDICINE = {"sku": "X1", "name": "X", "unit_price": "1.00"}
_BATCH = {"batch_number": "B1", "expiry_date": _in(300), "quantity": 1, "cost_per_unit": "1.00"}
_RX = {
    "appointment_id": _ID,
    "items": [{"medicine_name": "X", "dosage": "1", "frequency": "od", "quantity": 1}],
}
_PO = {"vendor_id": _ID, "items": [{"medicine_id": _ID, "quantity": 1, "unit_price": "1.00"}]}
_RECEIPT = {
    "items": [{"po_item_id": _ID, "batch_number": "B1", "expiry_date": _in(300), "quantity": 1}]
}
ENDPOINTS: list[tuple[str, str, str, Any]] = [
    ("POST", MEDICINES, "pharmacy.medicine.create", _MEDICINE),
    ("GET", MEDICINES, "pharmacy.medicine.read", None),
    ("GET", f"{MEDICINES}/{_ID}", "pharmacy.medicine.read", None),
    ("PATCH", f"{MEDICINES}/{_ID}", "pharmacy.medicine.update", {"name": "Y"}),
    ("GET", f"{MEDICINES}/{_ID}/stock", "pharmacy.batch.read", None),
    ("GET", f"{MEDICINES}/{_ID}/batches", "pharmacy.batch.read", None),
    ("POST", f"{MEDICINES}/{_ID}/batches", "pharmacy.batch.create", _BATCH),
    ("PATCH", f"{MEDICINES}/{_ID}/batches/{_ID}", "pharmacy.batch.update", {"is_recalled": True}),
    (
        "POST",
        f"{MEDICINES}/{_ID}/batches/{_ID}/adjust",
        "pharmacy.batch.update",
        {"quantity_change": -1, "note": "x"},
    ),
    ("POST", PRESCRIPTIONS, "pharmacy.prescription.create", _RX),
    ("GET", PRESCRIPTIONS, "pharmacy.prescription.read", None),
    ("GET", f"{PRESCRIPTIONS}/pending", "pharmacy.prescription.read", None),
    ("GET", f"{PRESCRIPTIONS}/{_ID}", "pharmacy.prescription.read", None),
    ("POST", f"{PRESCRIPTIONS}/{_ID}/cancel", "pharmacy.prescription.create", {"reason": "x"}),
    ("POST", f"{PRESCRIPTIONS}/{_ID}/dispense", "pharmacy.dispense.execute", None),
    ("GET", f"{PRESCRIPTIONS}/{_ID}/dispenses", "pharmacy.prescription.read", None),
    ("POST", VENDORS, "pharmacy.vendor.create", {"name": "Acme"}),
    ("GET", VENDORS, "pharmacy.vendor.read", None),
    ("GET", f"{VENDORS}/{_ID}", "pharmacy.vendor.read", None),
    ("PATCH", f"{VENDORS}/{_ID}", "pharmacy.vendor.update", {"name": "Y"}),
    ("POST", ORDERS, "pharmacy.po.create", _PO),
    ("GET", ORDERS, "pharmacy.po.read", None),
    ("GET", f"{ORDERS}/{_ID}", "pharmacy.po.read", None),
    ("POST", f"{ORDERS}/{_ID}/send", "pharmacy.po.update", None),
    ("POST", f"{ORDERS}/{_ID}/cancel", "pharmacy.po.update", None),
    ("POST", f"{ORDERS}/{_ID}/receive", "pharmacy.po.receive", _RECEIPT),
]
_IDS = [f"{method} {url.replace(_ID, ':id')}" for method, url, _, _ in ENDPOINTS]


class TestAuthorization:
    @pytest.mark.parametrize(("method", "url", "permission", "body"), ENDPOINTS, ids=_IDS)
    async def test_no_token_is_401(
        self, api: AsyncClient, method: str, url: str, permission: str, body: Any
    ) -> None:
        assert (await api.request(method, url, json=body)).status_code == 401

    @pytest.mark.parametrize(("method", "url", "permission", "body"), ENDPOINTS, ids=_IDS)
    async def test_every_other_pharmacy_permission_together_is_still_403(
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


# ── Medicines and stock ─────────────────────────────────────────────────────


class TestMedicines:
    async def test_create_list_get_update(
        self, api: AsyncClient, admin: dict[str, str], audit: RecordingAuditSink
    ) -> None:
        created = await api.post(
            MEDICINES,
            json={
                "sku": "para-500",
                "name": "Crocin",
                "generic_name": "Paracetamol",
                "strength": "500 mg",
                "form": "tablet",
                "unit_price": "2.50",
                "requires_prescription": False,
            },
            headers=admin,
        )
        assert created.status_code == 201, created.text
        medicine = created.json()["data"]
        assert (medicine["sku"], medicine["unit_price"], medicine["is_active"]) == (
            "PARA-500",
            "2.50",
            True,
        )

        duplicate = await api.post(
            MEDICINES, json={"sku": "PARA-500", "name": "x", "unit_price": "1.00"}, headers=admin
        )
        searched = await api.get(MEDICINES, params={"q": "parac"}, headers=admin)
        fetched = await api.get(f"{MEDICINES}/{medicine['id']}", headers=admin)
        updated = await api.patch(
            f"{MEDICINES}/{medicine['id']}", json={"unit_price": "3.00"}, headers=admin
        )
        immutable = await api.patch(
            f"{MEDICINES}/{medicine['id']}", json={"sku": "NEW"}, headers=admin
        )

        assert duplicate.status_code == 409
        assert [m["sku"] for m in searched.json()["data"]] == ["PARA-500"]
        assert fetched.json()["data"]["generic_name"] == "Paracetamol"
        assert updated.json()["data"]["unit_price"] == "3.00"
        assert immutable.status_code == 422
        assert audit.actions() == ["pharmacy.medicine.created", "pharmacy.medicine.updated"]
        assert audit.last().changes == {"unit_price": {"before": "2.50", "after": "3.00"}}

    @pytest.mark.parametrize(
        "overrides",
        [{"unit_price": "-1.00"}, {"unit_price": "1.005"}, {"sku": "has space"}, {"name": " "}],
    )
    async def test_a_bad_medicine_is_422(
        self, api: AsyncClient, admin: dict[str, str], overrides: dict[str, Any]
    ) -> None:
        body = {"sku": "X1", "name": "X", "unit_price": "1.00", **overrides}

        assert (await api.post(MEDICINES, json=body, headers=admin)).status_code == 422

    async def test_another_hospitals_medicine_is_invisible(
        self, api: AsyncClient, para: Medicine, other_tenant: dict[str, str]
    ) -> None:
        base = f"{MEDICINES}/{para.id}"

        attempts = [
            await api.get(base, headers=other_tenant),
            await api.patch(base, json={"name": "x"}, headers=other_tenant),
            await api.get(f"{base}/stock", headers=other_tenant),
            await api.get(f"{base}/batches", headers=other_tenant),
            await api.post(f"{base}/batches", json=_BATCH, headers=other_tenant),
        ]

        assert [r.status_code for r in attempts] == [404] * 5
        assert (await api.get(MEDICINES, headers=other_tenant)).json()["data"] == []


class TestStock:
    async def test_receive_recall_adjust_and_the_stock_position(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        para: Medicine,
        audit: RecordingAuditSink,
    ) -> None:
        base = f"{MEDICINES}/{para.id}"

        received = await api.post(
            f"{base}/batches",
            json={
                "batch_number": "b-1",
                "expiry_date": _in(20),
                "quantity": 30,
                "cost_per_unit": "1.10",
            },
            headers=admin,
        )
        assert received.status_code == 201, received.text
        batch = received.json()["data"]
        assert (batch["batch_number"], batch["quantity_on_hand"]) == ("B-1", 30)
        assert (batch["expires_soon"], batch["is_dispensable"]) == (True, True)
        await api.post(
            f"{base}/batches",
            json={
                "batch_number": "B-2",
                "expiry_date": _in(365),
                "quantity": 500,
                "cost_per_unit": "1.20",
            },
            headers=admin,
        )
        # The same batch again tops it up.
        topped = await api.post(
            f"{base}/batches",
            json={
                "batch_number": "B-1",
                "expiry_date": _in(20),
                "quantity": 10,
                "cost_per_unit": "1.10",
            },
            headers=admin,
        )
        assert topped.json()["data"]["quantity_on_hand"] == 40

        adjusted = await api.post(
            f"{base}/batches/{batch['id']}/adjust",
            json={"quantity_change": -4, "reason": "expired", "note": "Damaged strip"},
            headers=admin,
        )
        assert adjusted.json()["data"]["quantity_on_hand"] == 36
        overdrawn = await api.post(
            f"{base}/batches/{batch['id']}/adjust",
            json={"quantity_change": -37, "note": "x"},
            headers=admin,
        )
        assert overdrawn.status_code == 400

        recalled = await api.patch(
            f"{base}/batches/{batch['id']}", json={"is_recalled": True}, headers=admin
        )
        assert recalled.json()["data"]["is_dispensable"] is False

        stock = (await api.get(f"{base}/stock", headers=admin)).json()["data"]
        assert stock["medicine"]["sku"] == "PARA"
        assert stock["quantity_on_hand"] == 536
        assert stock["dispensable_quantity"] == 500
        assert stock["recalled_quantity"] == 36
        assert [b["batch_number"] for b in stock["batches"]] == ["B-1", "B-2"]
        listed = await api.get(f"{base}/batches", params={"in_stock_only": "true"}, headers=admin)
        assert len(listed.json()["data"]) == 2

        # The count is the sum of the ledger.
        on_hand = await db_session.execute(
            select(func.sum(MedicineBatch.quantity_on_hand)).where(
                MedicineBatch.medicine_id == para.id
            )
        )
        ledger = await db_session.execute(
            select(func.sum(StockMovement.quantity_change))
            .join(MedicineBatch, MedicineBatch.id == StockMovement.batch_id)
            .where(MedicineBatch.medicine_id == para.id)
        )
        assert on_hand.scalar_one() == ledger.scalar_one() == 536
        assert audit.actions() == [
            "pharmacy.batch.received",
            "pharmacy.batch.received",
            "pharmacy.batch.received",
            "pharmacy.batch.adjusted",
            "pharmacy.batch.recalled",
        ]

    @pytest.mark.parametrize(
        "body",
        [
            {**_BATCH, "expiry_date": _in(-1)},
            {**_BATCH, "quantity": 0},
            {**_BATCH, "cost_per_unit": "-1"},
            {**_BATCH, "batch_number": "has space"},
        ],
    )
    async def test_a_bad_receipt_is_422(
        self, api: AsyncClient, admin: dict[str, str], para: Medicine, body: dict[str, Any]
    ) -> None:
        response = await api.post(f"{MEDICINES}/{para.id}/batches", json=body, headers=admin)

        assert response.status_code == 422

    async def test_a_batch_is_reached_only_through_its_own_medicine(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        para: Medicine,
        amox: Medicine,
    ) -> None:
        batch = await insert_batch(db_session, para, "B1", 10)

        wrong_parent = await api.patch(
            f"{MEDICINES}/{amox.id}/batches/{batch.id}", json={"is_recalled": True}, headers=admin
        )

        assert wrong_parent.status_code == 404


# ── Prescriptions and dispensing ────────────────────────────────────────────


class TestPrescriptions:
    async def test_create_reads_the_patient_and_doctor_from_the_visit(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        para: Medicine,
        audit: RecordingAuditSink,
    ) -> None:
        await insert_batch(db_session, para, "B1", 40)

        rx = await _prescribe(api, admin, appointment, (para, 10), ("Vitamin D drops", 1))

        assert rx["status"] == "active"
        assert rx["patient_id"] == str(appointment.patient_id)
        assert rx["doctor_id"] == str(appointment.doctor_id)
        assert rx["patient_name"] == "Ananya Rao"
        assert rx["doctor_name"] == "Dr. Asha Menon"
        catalog_line, free_text = rx["items"]
        assert catalog_line["medicine_name"] == "Paracetamol 500 mg"
        assert (catalog_line["quantity_remaining"], catalog_line["available_quantity"]) == (10, 40)
        assert (free_text["medicine_id"], free_text["available_quantity"]) == (None, None)
        assert audit.last().action == "pharmacy.prescription.created"

    @pytest.mark.parametrize(
        ("line", "field"),
        [
            ({"medicine_id": str(uuid.uuid4())}, "items.0.medicine_id"),
            ({"medicine_id": None, "medicine_name": None}, None),
            ({"medicine_name": "X", "quantity": 0}, None),
            ({"medicine_name": "X", "patient_id": str(uuid.uuid4())}, None),
        ],
    )
    async def test_a_bad_prescription_is_422(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        line: dict[str, Any],
        field: str | None,
    ) -> None:
        item = {"dosage": "1", "frequency": "od", "quantity": 1, **line}
        item = {k: v for k, v in item.items() if v is not None}

        response = await api.post(
            PRESCRIPTIONS,
            json={"appointment_id": str(appointment.id), "items": [item]},
            headers=admin,
        )

        assert response.status_code == 422
        if field is not None:
            assert _first_field_error(response) == field

    async def test_an_unknown_or_cancelled_visit_is_422(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
    ) -> None:
        unknown = await api.post(PRESCRIPTIONS, json=_RX, headers=admin)
        appointment.status = AppointmentStatus.CANCELLED
        await db_session.flush()
        cancelled = await api.post(
            PRESCRIPTIONS, json={**_RX, "appointment_id": str(appointment.id)}, headers=admin
        )

        assert (unknown.status_code, cancelled.status_code) == (422, 422)
        assert _first_field_error(unknown) == "appointment_id"

    async def test_lists_the_queue_and_cancel(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        para: Medicine,
        audit: RecordingAuditSink,
    ) -> None:
        await insert_batch(db_session, para, "B1", 40)
        waiting = await _prescribe(api, admin, appointment, (para, 10))
        unwanted = await _prescribe(api, admin, appointment, (para, 5))

        cancelled = await api.post(
            f"{PRESCRIPTIONS}/{unwanted['id']}/cancel", json={"reason": "Allergy"}, headers=admin
        )
        again = await api.post(
            f"{PRESCRIPTIONS}/{unwanted['id']}/cancel", json={"reason": "Allergy"}, headers=admin
        )
        queue = await api.get(f"{PRESCRIPTIONS}/pending", headers=admin)
        everything = await api.get(
            PRESCRIPTIONS, params={"patient_id": str(appointment.patient_id)}, headers=admin
        )
        by_status = await api.get(PRESCRIPTIONS, params={"status": "cancelled"}, headers=admin)
        dispensing_it = await api.post(f"{PRESCRIPTIONS}/{unwanted['id']}/dispense", headers=admin)

        assert cancelled.json()["data"]["status"] == "cancelled"
        assert again.status_code == 400
        assert [p["id"] for p in queue.json()["data"]] == [waiting["id"]]
        assert queue.json()["data"][0]["items"][0]["available_quantity"] == 40
        assert len(everything.json()["data"]) == 2
        assert [p["id"] for p in by_status.json()["data"]] == [unwanted["id"]]
        assert dispensing_it.status_code == 400
        assert audit.last().action == "pharmacy.prescription.cancelled"

    async def test_another_hospitals_prescription_is_404_everywhere(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        appointment: Appointment,
        para: Medicine,
    ) -> None:
        batch = await insert_batch(db_session, para, "B1", 40)
        rx = await _prescribe(api, admin, appointment, (para, 10))
        base = f"{PRESCRIPTIONS}/{rx['id']}"

        attempts = [
            await api.get(base, headers=other_tenant),
            await api.post(f"{base}/dispense", headers=other_tenant),
            await api.post(f"{base}/cancel", json={"reason": "x"}, headers=other_tenant),
            await api.get(f"{base}/dispenses", headers=other_tenant),
        ]

        assert [r.status_code for r in attempts] == [404] * 4
        assert (await api.get(PRESCRIPTIONS, headers=other_tenant)).json()["data"] == []
        assert (await api.get(f"{PRESCRIPTIONS}/pending", headers=other_tenant)).json()[
            "data"
        ] == []
        await db_session.refresh(batch)
        assert batch.quantity_on_hand == 40
        # And they cannot prescribe from our catalog against our visit.
        theirs = await api.post(
            PRESCRIPTIONS,
            json={
                "appointment_id": str(appointment.id),
                "items": [
                    {"medicine_id": str(para.id), "dosage": "1", "frequency": "od", "quantity": 1}
                ],
            },
            headers=other_tenant,
        )
        assert theirs.status_code == 422


class TestDispense:
    async def test_ac2_first_expiry_first_and_it_is_billed(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        para: Medicine,
        audit: RecordingAuditSink,
        hospital_id: uuid.UUID,
    ) -> None:
        # Days are counted from today where the hospital is, as the service
        # counts them. Counted from today in UTC, this test was a day out for
        # the hours each night when the two dates differ.
        today = await hospital_today(HospitalRepository(db_session), hospital_id)
        late = await insert_batch(db_session, para, "LATE", 500, days=365, today=today)
        soon = await insert_batch(db_session, para, "SOON", 6, days=20, today=today)
        rx = await _prescribe(api, admin, appointment, (para, 10))
        audit.events.clear()

        response = await api.post(f"{PRESCRIPTIONS}/{rx['id']}/dispense", headers=admin)

        assert response.status_code == 201, response.text
        dispense = response.json()["data"]
        assert [(i["batch_number"], i["quantity"], i["total"]) for i in dispense["items"]] == [
            ("SOON", 6, "15.00"),
            ("LATE", 4, "10.00"),
        ]
        assert dispense["total_amount"] == "25.00"
        assert len(dispense["warnings"]) == 1
        assert "SOON expires in 20 days" in dispense["warnings"][0]
        await db_session.refresh(soon)
        await db_session.refresh(late)
        assert (soon.quantity_on_hand, late.quantity_on_hand) == (0, 496)

        # Rule 6: one bill line per prescription line, on a draft for the visit.
        invoice = await db_session.get(Invoice, uuid.UUID(dispense["invoice_id"]))
        assert invoice is not None
        assert invoice.appointment_id == appointment.id
        [line] = invoice.items
        assert (line.description, str(line.unit_price), str(line.line_total)) == (
            "Medicine — Paracetamol 500 mg",
            "2.50",
            "25.00",
        )
        assert line.quantity == Decimal(10)
        assert str(invoice.total) == "25.00"

        after = (await api.get(f"{PRESCRIPTIONS}/{rx['id']}", headers=admin)).json()["data"]
        assert after["status"] == "dispensed"
        assert after["items"][0]["quantity_remaining"] == 0
        history = await api.get(f"{PRESCRIPTIONS}/{rx['id']}/dispenses", headers=admin)
        assert [d["id"] for d in history.json()["data"]] == [dispense["id"]]
        assert audit.actions() == ["invoice.drafted", "pharmacy.dispensed"]
        again = await api.post(f"{PRESCRIPTIONS}/{rx['id']}/dispense", headers=admin)
        assert again.status_code == 400

    async def test_ac3_a_shortage_is_409_and_nothing_at_all_changes(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        para: Medicine,
        amox: Medicine,
        audit: RecordingAuditSink,
    ) -> None:
        plenty = await insert_batch(db_session, para, "P1", 100)
        scarce = await insert_batch(db_session, amox, "A1", 5)
        rx = await _prescribe(api, admin, appointment, (para, 10), (amox, 21))
        audit.events.clear()

        response = await api.post(f"{PRESCRIPTIONS}/{rx['id']}/dispense", headers=admin)

        assert response.status_code == 409
        assert "Nothing was dispensed" in response.json()["message"]
        [shortage] = response.json()["errors"]["shortages"]
        assert (shortage["medicine"], shortage["requested"], shortage["available"]) == (
            "Amoxicillin 500 mg",
            21,
            5,
        )
        await db_session.refresh(plenty)
        await db_session.refresh(scarce)
        assert (plenty.quantity_on_hand, scarce.quantity_on_hand) == (100, 5)
        after = (await api.get(f"{PRESCRIPTIONS}/{rx['id']}", headers=admin)).json()["data"]
        assert after["status"] == "active"
        assert (await api.get(f"{PRESCRIPTIONS}/{rx['id']}/dispenses", headers=admin)).json()[
            "data"
        ] == []
        invoices = await db_session.execute(
            select(Invoice).where(Invoice.patient_id == appointment.patient_id)
        )
        assert invoices.scalars().all() == []
        assert audit.events == []

    async def test_ac5_expired_and_recalled_stock_is_never_dispensed(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        para: Medicine,
    ) -> None:
        await insert_batch(db_session, para, "EXPIRED", 100, days=-2)
        await insert_batch(db_session, para, "RECALLED", 100, days=200, recalled=True)
        rx = await _prescribe(api, admin, appointment, (para, 10))

        short = await api.post(f"{PRESCRIPTIONS}/{rx['id']}/dispense", headers=admin)
        assert short.status_code == 409
        assert short.json()["errors"]["shortages"][0]["available"] == 0

        await insert_batch(db_session, para, "GOOD", 10, days=200)
        ok = await api.post(f"{PRESCRIPTIONS}/{rx['id']}/dispense", headers=admin)

        assert ok.status_code == 201, ok.text
        assert [i["batch_number"] for i in ok.json()["data"]["items"]] == ["GOOD"]

    async def test_rule_5_a_partial_dispense_needs_a_reason_then_the_rest_follows(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        para: Medicine,
        amox: Medicine,
    ) -> None:
        await insert_batch(db_session, para, "P1", 100)
        await insert_batch(db_session, amox, "A1", 100)
        rx = await _prescribe(api, admin, appointment, (para, 10), (amox, 21))
        url = f"{PRESCRIPTIONS}/{rx['id']}/dispense"
        part = {"items": [{"prescription_item_id": rx["items"][0]["id"], "quantity": 4}]}

        no_reason = await api.post(url, json=part, headers=admin)
        too_many = await api.post(
            url,
            json={
                "items": [{"prescription_item_id": rx["items"][0]["id"], "quantity": 11}],
                "notes": "x",
            },
            headers=admin,
        )
        first = await api.post(url, json={**part, "notes": "Rest tomorrow."}, headers=admin)
        mid = (await api.get(f"{PRESCRIPTIONS}/{rx['id']}", headers=admin)).json()["data"]
        second = await api.post(url, headers=admin)

        assert no_reason.status_code == 422
        assert _first_field_error(no_reason) == "notes"
        assert too_many.status_code == 422
        assert _first_field_error(too_many) == "items.0.quantity"
        assert first.status_code == 201, first.text
        assert mid["status"] == "partially_dispensed"
        assert [i["quantity_remaining"] for i in mid["items"]] == [6, 21]
        assert second.status_code == 201, second.text
        assert {(i["medicine_name"], i["quantity"]) for i in second.json()["data"]["items"]} == {
            ("Paracetamol", 6),
            ("Amoxicillin", 21),
        }
        # Both dispenses are charged to the same draft for the visit.
        assert second.json()["data"]["invoice_id"] == first.json()["data"]["invoice_id"]
        invoice = await db_session.get(Invoice, uuid.UUID(first.json()["data"]["invoice_id"]))
        assert invoice is not None
        await db_session.refresh(invoice)
        # 4 × 2.50, then 6 × 2.50 and 21 × 8.00.
        assert str(invoice.total) == "193.00"

    async def test_a_client_cannot_choose_the_batch_or_the_price(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        para: Medicine,
    ) -> None:
        batch = await insert_batch(db_session, para, "P1", 100)
        rx = await _prescribe(api, admin, appointment, (para, 10))
        line = {"prescription_item_id": rx["items"][0]["id"], "quantity": 10}

        for extra in ({"batch_id": str(batch.id)}, {"unit_price": "0.01"}):
            response = await api.post(
                f"{PRESCRIPTIONS}/{rx['id']}/dispense",
                json={"items": [{**line, **extra}]},
                headers=admin,
            )
            assert response.status_code == 422


# ── Vendors and purchase orders ─────────────────────────────────────────────


class TestProcurement:
    async def test_vendor_lifecycle(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        created = await api.post(
            VENDORS, json={"name": " Acme Pharma ", "tax_id": "GST1"}, headers=admin
        )
        assert created.status_code == 201, created.text
        vendor = created.json()["data"]
        assert (vendor["name"], vendor["is_active"]) == ("Acme Pharma", True)

        duplicate = await api.post(VENDORS, json={"name": "Acme Pharma"}, headers=admin)
        elsewhere = await api.post(VENDORS, json={"name": "Acme Pharma"}, headers=other_tenant)
        updated = await api.patch(
            f"{VENDORS}/{vendor['id']}", json={"is_active": False}, headers=admin
        )
        active = await api.get(VENDORS, params={"is_active": "true"}, headers=admin)
        foreign = await api.get(f"{VENDORS}/{vendor['id']}", headers=other_tenant)

        assert duplicate.status_code == 409
        assert elsewhere.status_code == 201
        assert updated.json()["data"]["is_active"] is False
        assert active.json()["data"] == []
        assert foreign.status_code == 404
        assert (await api.get(f"{VENDORS}/{vendor['id']}", headers=admin)).status_code == 200
        assert audit.actions()[:2] == ["pharmacy.vendor.created", "pharmacy.vendor.created"]
        assert audit.last().action == "pharmacy.vendor.updated"

    async def test_order_send_receive_puts_stock_on_the_shelf(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        para: Medicine,
        amox: Medicine,
        audit: RecordingAuditSink,
    ) -> None:
        vendor = (await api.post(VENDORS, json={"name": "Acme"}, headers=admin)).json()["data"]
        drafted = await api.post(
            ORDERS,
            json={
                "vendor_id": vendor["id"],
                "notes": "Deliver before noon.",
                "items": [
                    {"medicine_id": str(para.id), "quantity": 500, "unit_price": "1.20"},
                    {"medicine_id": str(amox.id), "quantity": 200, "unit_price": "4.00"},
                ],
            },
            headers=admin,
        )
        assert drafted.status_code == 201, drafted.text
        order = drafted.json()["data"]
        base = f"{ORDERS}/{order['id']}"
        assert (order["status"], order["total_amount"], order["vendor_name"]) == (
            "draft",
            "1400.00",
            "Acme",
        )
        assert order["po_number"].startswith("PO-")
        receipt = {
            "items": [
                {
                    "po_item_id": order["items"][0]["id"],
                    "batch_number": "pa-1",
                    "expiry_date": _in(300),
                    "quantity": 300,
                },
                {
                    "po_item_id": order["items"][0]["id"],
                    "batch_number": "PA-2",
                    "expiry_date": _in(400),
                    "quantity": 200,
                    "cost_per_unit": "1.25",
                },
                {
                    "po_item_id": order["items"][1]["id"],
                    "batch_number": "AX-1",
                    "expiry_date": _in(250),
                    "quantity": 180,
                },
            ]
        }

        too_early = await api.post(f"{base}/receive", json=receipt, headers=admin)
        foreign = await api.post(f"{base}/send", headers=other_tenant)
        sent = await api.post(f"{base}/send", headers=admin)
        received = await api.post(f"{base}/receive", json=receipt, headers=admin)
        twice = await api.post(f"{base}/receive", json=receipt, headers=admin)
        cancel = await api.post(f"{base}/cancel", headers=admin)

        assert too_early.status_code == 400
        assert foreign.status_code == 404
        assert sent.json()["data"]["status"] == "sent"
        assert received.status_code == 200, received.text
        assert received.json()["data"]["status"] == "received"
        assert (twice.status_code, cancel.status_code) == (400, 400)
        para_stock = (await api.get(f"{MEDICINES}/{para.id}/stock", headers=admin)).json()["data"]
        assert para_stock["dispensable_quantity"] == 500
        assert [
            (b["batch_number"], b["quantity_on_hand"], b["cost_per_unit"])
            for b in para_stock["batches"]
        ] == [
            ("PA-1", 300, "1.20"),
            ("PA-2", 200, "1.25"),
        ]
        amox_stock = (await api.get(f"{MEDICINES}/{amox.id}/stock", headers=admin)).json()["data"]
        assert amox_stock["quantity_on_hand"] == 180  # a short delivery is recorded as it came
        listed = await api.get(ORDERS, params={"status": "received"}, headers=admin)
        assert [o["id"] for o in listed.json()["data"]] == [order["id"]]
        assert (await api.get(base, headers=other_tenant)).status_code == 404
        assert audit.actions()[-3:] == [
            "pharmacy.po.created",
            "pharmacy.po.sent",
            "pharmacy.po.received",
        ]

    async def test_a_bad_order_or_receipt_is_422_and_adds_no_stock(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        para: Medicine,
    ) -> None:
        vendor = (await api.post(VENDORS, json={"name": "Acme"}, headers=admin)).json()["data"]
        line = {"medicine_id": str(para.id), "quantity": 10, "unit_price": "1.00"}

        unknown_vendor = await api.post(
            ORDERS, json={"vendor_id": str(uuid.uuid4()), "items": [line]}, headers=admin
        )
        unknown_medicine = await api.post(
            ORDERS,
            json={"vendor_id": vendor["id"], "items": [{**line, "medicine_id": str(uuid.uuid4())}]},
            headers=admin,
        )
        repeated = await api.post(
            ORDERS, json={"vendor_id": vendor["id"], "items": [line, line]}, headers=admin
        )
        order = (
            await api.post(ORDERS, json={"vendor_id": vendor["id"], "items": [line]}, headers=admin)
        ).json()["data"]
        await api.post(f"{ORDERS}/{order['id']}/send", headers=admin)
        expired = await api.post(
            f"{ORDERS}/{order['id']}/receive",
            json={
                "items": [
                    {
                        "po_item_id": order["items"][0]["id"],
                        "batch_number": "OLD",
                        "expiry_date": _in(-5),
                        "quantity": 10,
                    }
                ]
            },
            headers=admin,
        )
        cancelled = await api.post(f"{ORDERS}/{order['id']}/cancel", headers=admin)

        assert _first_field_error(unknown_vendor) == "vendor_id"
        assert _first_field_error(unknown_medicine) == "items.0.medicine_id"
        assert repeated.status_code == 422
        assert expired.status_code == 422
        assert _first_field_error(expired) == "items.0.expiry_date"
        assert cancelled.json()["data"]["status"] == "cancelled"
        batches = await db_session.execute(
            select(func.count())
            .select_from(MedicineBatch)
            .where(MedicineBatch.medicine_id == para.id)
        )
        assert batches.scalar_one() == 0
