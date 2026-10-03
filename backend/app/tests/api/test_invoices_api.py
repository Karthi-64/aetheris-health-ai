"""API tests for the invoice and payment endpoints.

Real app, real service, real repository, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3).

``docs/modules/06-billing.md`` §16 asks for "full CRUD + all state transitions;
idempotency behavior". Each acceptance criterion this sprint covers has a test
named for what it proves: AC-1 (issued invoices are immutable), AC-2 (numbers
are sequential), AC-3 (a replayed payment is not taken twice).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.dependencies.db import get_db_session
from app.api.dependencies.services import get_audit_sink
from app.main import create_app
from app.models.billing import Payment
from app.tests.billing_helpers import (
    auth_headers,
    insert_appointment,
    insert_doctor,
    insert_patient,
    insert_user_with_permissions,
)
from app.tests.conftest import RecordingAuditSink
from app.tests.factories import build_service_payload

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

URL = "/api/v1/invoices"

ALL_BILLING_PERMISSIONS = [
    "service.read",
    "service.create",
    "service.update",
    "invoice.read",
    "invoice.create",
    "invoice.update",
    "invoice.issue",
    "invoice.void",
    "invoice.payment.record",
]

#: What the seed gives a receptionist for billing: look, and take cash.
RECEPTIONIST_PERMISSIONS = ["service.read", "invoice.read", "invoice.payment.record.cash"]

#: What the seed gives a doctor for billing: the invoices for their own visits.
DOCTOR_PERMISSIONS = ["invoice.read.own"]


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
    """A user holding every billing permission."""
    user = await insert_user_with_permissions(db_session, hospital_id, ALL_BILLING_PERMISSIONS)
    return auth_headers(user.id, hospital_id)


@pytest_asyncio.fixture
async def receptionist(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """A user who may read invoices and record payments, and nothing else."""
    user = await insert_user_with_permissions(db_session, hospital_id, RECEPTIONIST_PERMISSIONS)
    return auth_headers(user.id, hospital_id)


@pytest_asyncio.fixture
async def other_tenant(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> dict[str, str]:
    """A fully-permissioned user in a different hospital."""
    user = await insert_user_with_permissions(
        db_session, other_hospital_id, ALL_BILLING_PERMISSIONS
    )
    return auth_headers(user.id, other_hospital_id)


@pytest_asyncio.fixture
async def patient_id(db_session: AsyncSession, hospital_id: uuid.UUID) -> uuid.UUID:
    """A patient in the caller's hospital."""
    return (await insert_patient(db_session, hospital_id)).id


def _key() -> str:
    """A fresh idempotency key of a legal length."""
    return f"pay-{uuid.uuid4().hex}"


def _line(description: str = "Consultation", price: str = "500.00", **extra: Any) -> dict[str, Any]:
    """An ad-hoc invoice line."""
    return {"description": description, "unit_price": price, **extra}


def _first_field_error(response: Any) -> str:
    """Return the field named by a service-level 422.

    A domain ``ValidationError`` puts its detail dict under ``errors``, so the
    per-field list sits one level down at ``errors.errors`` — the shape every
    module's service-level 422 already has on the wire.
    """
    return str(response.json()["errors"]["errors"][0]["field"])


async def _draft(
    api: AsyncClient,
    headers: dict[str, str],
    patient_id: uuid.UUID,
    *,
    items: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Create a draft through the API and return the data block."""
    body = {
        "patient_id": str(patient_id),
        "items": [_line()] if items is None else items,
        **extra,
    }
    response = await api.post(URL, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


async def _issue(api: AsyncClient, headers: dict[str, str], invoice_id: str) -> dict[str, Any]:
    """Issue an invoice through the API and return the data block."""
    response = await api.post(f"{URL}/{invoice_id}/issue", headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


async def _issued(
    api: AsyncClient, headers: dict[str, str], patient_id: uuid.UUID, **kwargs: Any
) -> dict[str, Any]:
    """Create and issue an invoice (500.00 by default)."""
    draft = await _draft(api, headers, patient_id, **kwargs)
    return await _issue(api, headers, draft["id"])


async def _pay(
    api: AsyncClient,
    headers: dict[str, str],
    invoice_id: str,
    amount: str,
    *,
    key: str | None = None,
    method: str = "cash",
) -> Any:
    """Post a payment and return the raw response."""
    return await api.post(
        f"{URL}/{invoice_id}/payments",
        json={"amount": amount, "method": method},
        headers={**headers, "Idempotency-Key": key or _key()},
    )


# ── Drafting ────────────────────────────────────────────────────────────────


class TestCreateDraft:
    async def test_returns_201_with_server_computed_totals(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        patient_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        response = await api.post(
            URL,
            json={
                "patient_id": str(patient_id),
                "items": [
                    _line("Consultation", "500.00"),
                    _line("Dressing kit", "75.00", quantity="2"),
                ],
                "notes": "Walk-in",
            },
            headers=admin,
        )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "draft"
        assert data["invoice_number"] is None
        assert data["patient_name"] == "Ananya Rao"
        assert data["currency"] == "INR"
        assert data["subtotal"] == "650.00"
        assert data["tax_amount"] == "0.00"
        assert data["discount_amount"] == "0.00"
        assert data["total"] == "650.00"
        assert data["amount_paid"] == "0.00"
        assert data["balance_due"] == "650.00"
        assert [(i["position"], i["description"], i["line_total"]) for i in data["items"]] == [
            (0, "Consultation", "500.00"),
            (1, "Dressing kit", "150.00"),
        ]
        assert audit.actions() == ["invoice.drafted"]

    async def test_a_catalog_line_is_priced_from_the_catalog(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        created = await api.post(
            "/api/v1/services",
            json=build_service_payload(code="ECG", name="ECG", price="450.00"),
            headers=admin,
        )
        service_id = created.json()["data"]["id"]

        draft = await _draft(
            api, admin, patient_id, items=[{"service_id": service_id, "quantity": "2"}]
        )

        line = draft["items"][0]
        assert line["service_id"] == service_id
        assert line["description"] == "ECG"
        assert line["unit_price"] == "450.00"
        assert line["line_total"] == "900.00"
        assert draft["total"] == "900.00"

    async def test_a_later_price_change_does_not_touch_the_draft(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        created = await api.post(
            "/api/v1/services", json=build_service_payload(price="450.00"), headers=admin
        )
        service_id = created.json()["data"]["id"]
        draft = await _draft(api, admin, patient_id, items=[{"service_id": service_id}])

        await api.patch(f"/api/v1/services/{service_id}", json={"price": "999.00"}, headers=admin)

        fetched = await api.get(f"{URL}/{draft['id']}", headers=admin)
        assert fetched.json()["data"]["total"] == "450.00"

    @pytest.mark.parametrize("field", ["total", "subtotal", "amount_paid", "status"])
    async def test_client_supplied_totals_are_rejected(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID, field: str
    ) -> None:
        # Business rule 7.
        response = await api.post(
            URL,
            json={"patient_id": str(patient_id), "items": [_line()], field: "1.00"},
            headers=admin,
        )

        assert response.status_code == 422

    async def test_a_line_cannot_carry_its_own_total(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        response = await api.post(
            URL,
            json={"patient_id": str(patient_id), "items": [_line(line_total="1.00")]},
            headers=admin,
        )

        assert response.status_code == 422

    async def test_unknown_patient_returns_422(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await api.post(
            URL, json={"patient_id": str(uuid.uuid4()), "items": [_line()]}, headers=admin
        )

        assert response.status_code == 422
        assert _first_field_error(response) == "patient_id"

    async def test_unknown_service_returns_422(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        response = await api.post(
            URL,
            json={"patient_id": str(patient_id), "items": [{"service_id": str(uuid.uuid4())}]},
            headers=admin,
        )

        assert response.status_code == 422
        assert _first_field_error(response) == "items.0.service_id"

    async def test_an_inactive_service_cannot_be_billed(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        created = await api.post("/api/v1/services", json=build_service_payload(), headers=admin)
        service_id = created.json()["data"]["id"]
        await api.patch(f"/api/v1/services/{service_id}", json={"is_active": False}, headers=admin)

        response = await api.post(
            URL,
            json={"patient_id": str(patient_id), "items": [{"service_id": service_id}]},
            headers=admin,
        )

        assert response.status_code == 422


class TestAppointmentLink:
    async def test_an_appointment_can_have_only_one_live_invoice(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
    ) -> None:
        doctor = await insert_doctor(db_session, hospital_id)
        appointment = await insert_appointment(
            db_session, hospital_id, patient_id=patient_id, doctor_id=doctor.id
        )
        first = await _draft(api, admin, patient_id, appointment_id=str(appointment.id))

        second = await api.post(
            URL,
            json={
                "patient_id": str(patient_id),
                "appointment_id": str(appointment.id),
                "items": [_line()],
            },
            headers=admin,
        )

        assert first["appointment_id"] == str(appointment.id)
        assert second.status_code == 409
        assert second.json()["error_code"] == "RESOURCE_CONFLICT"

    async def test_another_patients_appointment_returns_422(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
    ) -> None:
        someone_else = await insert_patient(db_session, hospital_id, first_name="Meera")
        doctor = await insert_doctor(db_session, hospital_id)
        appointment = await insert_appointment(
            db_session, hospital_id, patient_id=someone_else.id, doctor_id=doctor.id
        )

        response = await api.post(
            URL,
            json={
                "patient_id": str(patient_id),
                "appointment_id": str(appointment.id),
                "items": [_line()],
            },
            headers=admin,
        )

        assert response.status_code == 422
        assert _first_field_error(response) == "appointment_id"


class TestEditDraft:
    async def test_replacing_lines_recomputes_the_total(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        patient_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        draft = await _draft(api, admin, patient_id)

        response = await api.patch(
            f"{URL}/{draft['id']}",
            json={"items": [_line("X-ray", "1250.00")], "notes": "Corrected"},
            headers=admin,
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert [item["description"] for item in data["items"]] == ["X-ray"]
        assert data["total"] == "1250.00"
        assert data["notes"] == "Corrected"
        assert audit.actions() == ["invoice.drafted", "invoice.updated"]

    async def test_an_empty_patch_returns_422(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        draft = await _draft(api, admin, patient_id)

        response = await api.patch(f"{URL}/{draft['id']}", json={}, headers=admin)

        assert response.status_code == 422

    async def test_ac1_an_issued_invoice_cannot_be_edited(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        patient_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        response = await api.patch(
            f"{URL}/{invoice['id']}", json={"items": [_line("Changed", "1.00")]}, headers=admin
        )

        assert response.status_code == 400
        assert response.json()["error_code"] == "BUSINESS_RULE_VIOLATION"
        fetched = await api.get(f"{URL}/{invoice['id']}", headers=admin)
        assert fetched.json()["data"]["total"] == "500.00"
        assert "invoice.updated" not in audit.actions()


# ── Lifecycle ───────────────────────────────────────────────────────────────


class TestIssue:
    async def test_issue_assigns_a_number_and_freezes_the_invoice(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        patient_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        draft = await _draft(api, admin, patient_id)

        response = await api.post(f"{URL}/{draft['id']}/issue", headers=admin)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "issued"
        assert data["issued_at"] is not None
        assert data["invoice_number"].startswith("INV-")
        assert data["invoice_number"].endswith("-000001")
        assert audit.actions() == ["invoice.drafted", "invoice.issued"]
        assert audit.last().context["invoice_number"] == data["invoice_number"]

    async def test_ac2_numbers_are_sequential_and_a_refused_issue_uses_none(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        first = await _issued(api, admin, patient_id)
        # A refused issue (no lines) and a repeated issue must not burn a number.
        empty = await _draft(api, admin, patient_id, items=[])
        assert (await api.post(f"{URL}/{empty['id']}/issue", headers=admin)).status_code == 400
        assert (await api.post(f"{URL}/{first['id']}/issue", headers=admin)).status_code == 400
        second = await _issued(api, admin, patient_id)
        third = await _issued(api, admin, patient_id)

        sequence = [
            int(invoice["invoice_number"].rsplit("-", 1)[1]) for invoice in (first, second, third)
        ]
        assert sequence == [1, 2, 3]

    async def test_each_hospital_has_its_own_series(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        db_session: AsyncSession,
        other_hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
    ) -> None:
        their_patient = await insert_patient(db_session, other_hospital_id)
        await _issued(api, admin, patient_id)
        await _issued(api, admin, patient_id)

        theirs = await _issued(api, other_tenant, their_patient.id)

        assert theirs["invoice_number"].endswith("-000001")

    async def test_a_zero_total_invoice_is_paid_on_issue(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id, items=[_line("Free follow-up", "0.00")])

        assert invoice["status"] == "paid"
        assert invoice["balance_due"] == "0.00"

    async def test_issuing_an_empty_draft_returns_400(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        draft = await _draft(api, admin, patient_id, items=[])

        response = await api.post(f"{URL}/{draft['id']}/issue", headers=admin)

        assert response.status_code == 400
        assert response.json()["error_code"] == "BUSINESS_RULE_VIOLATION"

    async def test_issuing_twice_returns_400(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        response = await api.post(f"{URL}/{invoice['id']}/issue", headers=admin)

        assert response.status_code == 400


class TestVoid:
    async def test_void_keeps_the_number_and_records_the_reason(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        patient_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        response = await api.post(
            f"{URL}/{invoice['id']}/void", json={"reason": "Wrong patient"}, headers=admin
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "void"
        assert data["void_reason"] == "Wrong patient"
        assert data["voided_at"] is not None
        assert data["invoice_number"] == invoice["invoice_number"]
        assert audit.last().action == "invoice.voided"

    async def test_void_requires_a_reason(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        for body in ({}, {"reason": "   "}):
            response = await api.post(f"{URL}/{invoice['id']}/void", json=body, headers=admin)
            assert response.status_code == 422

    async def test_a_draft_cannot_be_voided(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        draft = await _draft(api, admin, patient_id)

        response = await api.post(
            f"{URL}/{draft['id']}/void", json={"reason": "Mistake"}, headers=admin
        )

        assert response.status_code == 400

    async def test_an_invoice_with_a_payment_cannot_be_voided(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id)
        assert (await _pay(api, admin, invoice["id"], "100.00")).status_code == 201

        response = await api.post(
            f"{URL}/{invoice['id']}/void", json={"reason": "Mistake"}, headers=admin
        )

        assert response.status_code == 400
        assert "refunded first" in response.json()["message"]

    async def test_a_voided_invoice_takes_no_payment(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id)
        await api.post(f"{URL}/{invoice['id']}/void", json={"reason": "Mistake"}, headers=admin)

        response = await _pay(api, admin, invoice["id"], "100.00")

        assert response.status_code == 400


# ── Payments ────────────────────────────────────────────────────────────────


class TestRecordPayment:
    async def test_partial_then_full_payment(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        patient_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        partial = await _pay(api, admin, invoice["id"], "200.00", method="cash")
        full = await _pay(api, admin, invoice["id"], "300.00", method="upi")

        assert partial.status_code == 201
        first = partial.json()["data"]
        assert first["payment"]["amount"] == "200.00"
        assert first["payment"]["method"] == "cash"
        assert first["invoice"]["status"] == "partially_paid"
        assert first["invoice"]["amount_paid"] == "200.00"
        assert first["invoice"]["balance_due"] == "300.00"

        assert full.status_code == 201
        second = full.json()["data"]
        assert second["invoice"]["status"] == "paid"
        assert second["invoice"]["amount_paid"] == "500.00"
        assert second["invoice"]["balance_due"] == "0.00"
        assert audit.actions()[-2:] == ["invoice.payment_recorded", "invoice.payment_recorded"]

    async def test_payments_are_listed_oldest_first(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id)
        await _pay(api, admin, invoice["id"], "200.00", method="cash")
        await _pay(api, admin, invoice["id"], "50.00", method="card")

        response = await api.get(f"{URL}/{invoice['id']}/payments", headers=admin)

        assert response.status_code == 200
        assert [(p["amount"], p["method"]) for p in response.json()["data"]] == [
            ("200.00", "cash"),
            ("50.00", "card"),
        ]
        assert "idempotency_key" not in response.json()["data"][0]

    async def test_overpayment_returns_400_and_changes_nothing(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        response = await _pay(api, admin, invoice["id"], "500.01")

        assert response.status_code == 400
        assert response.json()["error_code"] == "BUSINESS_RULE_VIOLATION"
        fetched = await api.get(f"{URL}/{invoice['id']}", headers=admin)
        assert fetched.json()["data"]["amount_paid"] == "0.00"
        assert fetched.json()["data"]["status"] == "issued"

    async def test_a_paid_invoice_takes_no_more_money(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id)
        await _pay(api, admin, invoice["id"], "500.00")

        response = await _pay(api, admin, invoice["id"], "0.01")

        assert response.status_code == 400

    async def test_a_draft_takes_no_payment(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        draft = await _draft(api, admin, patient_id)

        response = await _pay(api, admin, draft["id"], "100.00")

        assert response.status_code == 400
        assert "Issue the invoice first" in response.json()["message"]

    @pytest.mark.parametrize("amount", ["0", "-10.00", "10.005"])
    async def test_invalid_amount_returns_422(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID, amount: str
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        response = await _pay(api, admin, invoice["id"], amount)

        assert response.status_code == 422

    async def test_unknown_method_returns_422(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        response = await _pay(api, admin, invoice["id"], "10.00", method="cheque")

        assert response.status_code == 422


class TestPaymentIdempotency:
    async def test_missing_key_returns_422(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        response = await api.post(
            f"{URL}/{invoice['id']}/payments",
            json={"amount": "100.00", "method": "cash"},
            headers=admin,
        )

        assert response.status_code == 422

    @pytest.mark.parametrize("key", ["short", "x" * 101])
    async def test_key_length_is_bounded(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID, key: str
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        response = await _pay(api, admin, invoice["id"], "100.00", key=key)

        assert response.status_code == 422

    async def test_ac3_replaying_a_key_does_not_take_the_money_twice(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        patient_id: uuid.UUID,
        db_session: AsyncSession,
        audit: RecordingAuditSink,
    ) -> None:
        invoice = await _issued(api, admin, patient_id)
        key = _key()

        first = await _pay(api, admin, invoice["id"], "200.00", key=key)
        replay = await _pay(api, admin, invoice["id"], "200.00", key=key)

        assert first.status_code == 201
        # 200, not 201: a replay is not a new payment.
        assert replay.status_code == 200
        assert replay.json()["data"]["payment"]["id"] == first.json()["data"]["payment"]["id"]
        assert replay.json()["data"]["invoice"]["amount_paid"] == "200.00"

        recorded = await db_session.scalar(
            select(func.count()).select_from(Payment).where(Payment.idempotency_key == key)
        )
        assert recorded == 1
        assert audit.actions().count("invoice.payment_recorded") == 1

    async def test_replaying_the_final_payment_still_returns_the_original(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        # The invoice is `paid` by the time the retry arrives. A retry must
        # still get its original answer rather than "cannot take payments".
        invoice = await _issued(api, admin, patient_id)
        key = _key()
        first = await _pay(api, admin, invoice["id"], "500.00", key=key)

        replay = await _pay(api, admin, invoice["id"], "500.00", key=key)

        assert replay.status_code == 200
        assert replay.json()["data"]["payment"]["id"] == first.json()["data"]["payment"]["id"]
        assert replay.json()["data"]["invoice"]["status"] == "paid"

    async def test_a_key_reused_for_a_different_amount_returns_409(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        invoice = await _issued(api, admin, patient_id)
        key = _key()
        await _pay(api, admin, invoice["id"], "200.00", key=key)

        response = await _pay(api, admin, invoice["id"], "100.00", key=key)

        assert response.status_code == 409
        assert response.json()["error_code"] == "RESOURCE_CONFLICT"
        fetched = await api.get(f"{URL}/{invoice['id']}", headers=admin)
        assert fetched.json()["data"]["amount_paid"] == "200.00"

    async def test_a_key_reused_on_another_invoice_returns_409(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        first = await _issued(api, admin, patient_id)
        second = await _issued(api, admin, patient_id)
        key = _key()
        await _pay(api, admin, first["id"], "200.00", key=key)

        response = await _pay(api, admin, second["id"], "200.00", key=key)

        assert response.status_code == 409
        fetched = await api.get(f"{URL}/{second['id']}", headers=admin)
        assert fetched.json()["data"]["amount_paid"] == "0.00"

    async def test_the_same_key_in_another_hospital_is_a_separate_payment(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        db_session: AsyncSession,
        other_hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
    ) -> None:
        their_patient = await insert_patient(db_session, other_hospital_id)
        ours = await _issued(api, admin, patient_id)
        theirs = await _issued(api, other_tenant, their_patient.id)
        key = _key()

        mine = await _pay(api, admin, ours["id"], "200.00", key=key)
        other = await _pay(api, other_tenant, theirs["id"], "300.00", key=key)

        assert mine.status_code == 201
        # 201, not a replay of ours and not a 409: keys are scoped per tenant.
        assert other.status_code == 201
        assert other.json()["data"]["payment"]["amount"] == "300.00"


# ── Reads ───────────────────────────────────────────────────────────────────


class TestRead:
    async def test_get_unknown_returns_404(self, api: AsyncClient, admin: dict[str, str]) -> None:
        response = await api.get(f"{URL}/{uuid.uuid4()}", headers=admin)

        assert response.status_code == 404
        assert response.json()["error_code"] == "RESOURCE_NOT_FOUND"

    async def test_list_is_paginated_and_omits_lines(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        for _ in range(3):
            await _draft(api, admin, patient_id)

        response = await api.get(URL, params={"page_size": 2}, headers=admin)

        assert response.status_code == 200
        body = response.json()
        assert len(body["data"]) == 2
        assert "items" not in body["data"][0]
        assert body["data"][0]["balance_due"] == "500.00"
        assert body["metadata"]["pagination"]["total_records"] == 3
        assert body["metadata"]["pagination"]["total_pages"] == 2

    async def test_list_filters_by_status_and_patient(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
    ) -> None:
        other_patient = await insert_patient(db_session, hospital_id, first_name="Meera")
        draft = await _draft(api, admin, patient_id)
        issued = await _issued(api, admin, patient_id)
        await _draft(api, admin, other_patient.id)

        by_status = await api.get(URL, params={"status": "issued"}, headers=admin)
        by_patient = await api.get(URL, params={"patient_id": str(patient_id)}, headers=admin)

        assert [item["id"] for item in by_status.json()["data"]] == [issued["id"]]
        assert {item["id"] for item in by_patient.json()["data"]} == {draft["id"], issued["id"]}

    async def test_date_filter_matches_issue_date_and_never_drafts(
        self, api: AsyncClient, admin: dict[str, str], patient_id: uuid.UUID
    ) -> None:
        await _draft(api, admin, patient_id)
        issued = await _issued(api, admin, patient_id)
        issued_on = issued["issued_at"][:10]

        # A wide window around "now" avoids depending on which side of local
        # midnight the test happens to run.
        hits = await api.get(
            URL, params={"issued_from": "2020-01-01", "issued_to": "2099-12-31"}, headers=admin
        )
        misses = await api.get(
            URL, params={"issued_from": "2020-01-01", "issued_to": "2020-01-02"}, headers=admin
        )

        assert issued_on
        assert [item["id"] for item in hits.json()["data"]] == [issued["id"]]
        assert misses.json()["data"] == []

    async def test_an_inverted_date_range_returns_422(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await api.get(
            URL, params={"issued_from": "2026-10-02", "issued_to": "2026-10-01"}, headers=admin
        )

        assert response.status_code == 422
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    async def test_an_unknown_status_returns_422(
        self, api: AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await api.get(URL, params={"status": "overdue"}, headers=admin)

        assert response.status_code == 422


# ── Authorization ───────────────────────────────────────────────────────────


class TestAuthorization:
    async def test_no_token_returns_401_everywhere(self, api: AsyncClient) -> None:
        invoice_id = uuid.uuid4()
        calls = [
            ("GET", URL, None),
            ("POST", URL, {"patient_id": str(uuid.uuid4())}),
            ("GET", f"{URL}/{invoice_id}", None),
            ("PATCH", f"{URL}/{invoice_id}", {"notes": "x"}),
            ("POST", f"{URL}/{invoice_id}/issue", None),
            ("POST", f"{URL}/{invoice_id}/void", {"reason": "x"}),
            ("GET", f"{URL}/{invoice_id}/payments", None),
        ]

        for method, url, body in calls:
            response = await api.request(method, url, json=body)
            assert response.status_code == 401, (method, url)

        payment = await api.post(
            f"{URL}/{invoice_id}/payments",
            json={"amount": "1.00", "method": "cash"},
            headers={"Idempotency-Key": _key()},
        )
        assert payment.status_code == 401

    async def test_a_receptionist_can_read_and_take_payments_only(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        receptionist: dict[str, str],
        patient_id: uuid.UUID,
    ) -> None:
        draft = await _draft(api, admin, patient_id)
        issued = await _issued(api, admin, patient_id)

        # Allowed.
        assert (await api.get(URL, headers=receptionist)).status_code == 200
        assert (await api.get(f"{URL}/{issued['id']}", headers=receptionist)).status_code == 200
        assert (
            await api.get(f"{URL}/{issued['id']}/payments", headers=receptionist)
        ).status_code == 200
        assert (await _pay(api, receptionist, issued["id"], "100.00")).status_code == 201

        # Refused: create, edit, issue, void.
        create = await api.post(
            URL, json={"patient_id": str(patient_id), "items": [_line()]}, headers=receptionist
        )
        edit = await api.patch(f"{URL}/{draft['id']}", json={"notes": "x"}, headers=receptionist)
        issue = await api.post(f"{URL}/{draft['id']}/issue", headers=receptionist)
        void = await api.post(
            f"{URL}/{issued['id']}/void", json={"reason": "x"}, headers=receptionist
        )

        assert [r.status_code for r in (create, edit, issue, void)] == [403, 403, 403, 403]
        assert create.json()["error_code"] == "PERMISSION_DENIED"

    async def test_reading_without_invoice_read_returns_403(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        user = await insert_user_with_permissions(db_session, hospital_id, ["patient.read"])
        headers = auth_headers(user.id, hospital_id)

        assert (await api.get(URL, headers=headers)).status_code == 403
        assert (await api.get(f"{URL}/{uuid.uuid4()}", headers=headers)).status_code == 403

    async def test_recording_a_payment_needs_its_own_permission(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
    ) -> None:
        invoice = await _issued(api, admin, patient_id)
        user = await insert_user_with_permissions(
            db_session, hospital_id, ["invoice.read", "invoice.create", "invoice.issue"]
        )

        response = await _pay(api, auth_headers(user.id, hospital_id), invoice["id"], "100.00")

        assert response.status_code == 403


class TestTenantIsolation:
    async def test_another_hospital_cannot_see_or_act_on_an_invoice(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        patient_id: uuid.UUID,
    ) -> None:
        draft = await _draft(api, admin, patient_id)
        issued = await _issued(api, admin, patient_id)

        responses = {
            "get": await api.get(f"{URL}/{issued['id']}", headers=other_tenant),
            "patch": await api.patch(
                f"{URL}/{draft['id']}", json={"notes": "x"}, headers=other_tenant
            ),
            "issue": await api.post(f"{URL}/{draft['id']}/issue", headers=other_tenant),
            "void": await api.post(
                f"{URL}/{issued['id']}/void", json={"reason": "x"}, headers=other_tenant
            ),
            "pay": await _pay(api, other_tenant, issued["id"], "100.00"),
            "payments": await api.get(f"{URL}/{issued['id']}/payments", headers=other_tenant),
        }

        # 404, not 403: a cross-tenant lookup must look exactly like a miss.
        assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(
            responses, 404
        )
        assert (await api.get(URL, headers=other_tenant)).json()["data"] == []

        # And nothing changed for the owner.
        mine = (await api.get(f"{URL}/{issued['id']}", headers=admin)).json()["data"]
        assert mine["status"] == "issued"
        assert mine["amount_paid"] == "0.00"

    async def test_a_patient_from_another_hospital_cannot_be_billed(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        db_session: AsyncSession,
        other_hospital_id: uuid.UUID,
    ) -> None:
        their_patient = await insert_patient(db_session, other_hospital_id)

        response = await api.post(
            URL, json={"patient_id": str(their_patient.id), "items": [_line()]}, headers=admin
        )

        assert response.status_code == 422

    async def test_a_service_from_another_hospital_cannot_be_billed(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        patient_id: uuid.UUID,
    ) -> None:
        theirs = await api.post(
            "/api/v1/services", json=build_service_payload(), headers=other_tenant
        )

        response = await api.post(
            URL,
            json={
                "patient_id": str(patient_id),
                "items": [{"service_id": theirs.json()["data"]["id"]}],
            },
            headers=admin,
        )

        assert response.status_code == 422


# ── Role rules (module spec §3) ─────────────────────────────────────────────


class TestReceptionistCashOnly:
    """``invoice.payment.record.cash`` records cash and nothing else."""

    async def test_cash_is_accepted(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        receptionist: dict[str, str],
        patient_id: uuid.UUID,
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        response = await _pay(api, receptionist, invoice["id"], "200.00", method="cash")

        assert response.status_code == 201
        assert response.json()["data"]["payment"]["method"] == "cash"

    @pytest.mark.parametrize("method", ["card", "upi", "bank_transfer", "insurance"])
    async def test_any_other_method_returns_403_and_records_nothing(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        receptionist: dict[str, str],
        patient_id: uuid.UUID,
        audit: RecordingAuditSink,
        method: str,
    ) -> None:
        invoice = await _issued(api, admin, patient_id)

        response = await _pay(api, receptionist, invoice["id"], "200.00", method=method)

        assert response.status_code == 403
        body = response.json()
        assert body["error_code"] == "PERMISSION_DENIED"
        assert "cash payments only" in body["message"]
        fetched = await api.get(f"{URL}/{invoice['id']}", headers=admin)
        assert fetched.json()["data"]["amount_paid"] == "0.00"
        assert "invoice.payment_recorded" not in audit.actions()

    async def test_a_non_cash_attempt_does_not_reveal_whether_the_invoice_exists(
        self, api: AsyncClient, receptionist: dict[str, str]
    ) -> None:
        # 403 for an invoice that is not there, the same as for one that is:
        # the method is refused before the invoice is looked up.
        response = await _pay(api, receptionist, str(uuid.uuid4()), "200.00", method="upi")

        assert response.status_code == 403

    async def test_holding_the_full_permission_as_well_lifts_the_limit(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
    ) -> None:
        # The admin roles hold both codes; the wider one must win.
        invoice = await _issued(api, admin, patient_id)
        user = await insert_user_with_permissions(
            db_session, hospital_id, ["invoice.payment.record", "invoice.payment.record.cash"]
        )

        response = await _pay(
            api, auth_headers(user.id, hospital_id), invoice["id"], "200.00", method="upi"
        )

        assert response.status_code == 201


class TestDoctorOwnVisits:
    """``invoice.read.own`` shows a doctor the invoices for their own visits."""

    @pytest_asyncio.fixture
    async def scene(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
    ) -> dict[str, Any]:
        """One patient seen by two doctors, each visit invoiced, plus a counter sale.

        The first doctor is a real login holding only ``invoice.read.own``.
        """
        user = await insert_user_with_permissions(db_session, hospital_id, DOCTOR_PERMISSIONS)
        me = await insert_doctor(db_session, hospital_id, user_id=user.id)
        colleague = await insert_doctor(db_session, hospital_id)
        my_visit = await insert_appointment(
            db_session, hospital_id, patient_id=patient_id, doctor_id=me.id
        )
        their_visit = await insert_appointment(
            db_session, hospital_id, patient_id=patient_id, doctor_id=colleague.id
        )
        mine = await _issued(api, admin, patient_id, appointment_id=str(my_visit.id))
        theirs = await _issued(api, admin, patient_id, appointment_id=str(their_visit.id))
        counter = await _issued(api, admin, patient_id)
        assert (await _pay(api, admin, mine["id"], "100.00")).status_code == 201
        assert (await _pay(api, admin, theirs["id"], "100.00")).status_code == 201
        return {
            "doctor": auth_headers(user.id, hospital_id),
            "mine": mine,
            "theirs": theirs,
            "counter": counter,
        }

    async def test_the_list_holds_only_their_own_visits(
        self, api: AsyncClient, admin: dict[str, str], scene: dict[str, Any]
    ) -> None:
        response = await api.get(URL, headers=scene["doctor"])

        assert response.status_code == 200
        body = response.json()
        assert [item["id"] for item in body["data"]] == [scene["mine"]["id"]]
        # The total is scoped as well — it must not reveal the other two exist.
        assert body["metadata"]["pagination"]["total_records"] == 1
        # An unrestricted reader sees all three.
        everyone = await api.get(URL, headers=admin)
        assert everyone.json()["metadata"]["pagination"]["total_records"] == 3

    async def test_filters_cannot_widen_the_scope(
        self, api: AsyncClient, scene: dict[str, Any], patient_id: uuid.UUID
    ) -> None:
        # Same patient as the colleague's invoice and the counter sale.
        response = await api.get(
            URL,
            params={"patient_id": str(patient_id), "status": "partially_paid"},
            headers=scene["doctor"],
        )

        assert [item["id"] for item in response.json()["data"]] == [scene["mine"]["id"]]

    async def test_their_own_invoice_and_its_payments_are_readable(
        self, api: AsyncClient, scene: dict[str, Any]
    ) -> None:
        invoice = await api.get(f"{URL}/{scene['mine']['id']}", headers=scene["doctor"])
        payments = await api.get(f"{URL}/{scene['mine']['id']}/payments", headers=scene["doctor"])

        assert invoice.status_code == 200
        assert invoice.json()["data"]["id"] == scene["mine"]["id"]
        assert payments.status_code == 200
        assert len(payments.json()["data"]) == 1

    @pytest.mark.parametrize("which", ["theirs", "counter"])
    async def test_any_other_invoice_is_a_404(
        self, api: AsyncClient, scene: dict[str, Any], which: str
    ) -> None:
        # 404, not 403: it must look exactly like an invoice that does not exist.
        invoice = await api.get(f"{URL}/{scene[which]['id']}", headers=scene["doctor"])
        payments = await api.get(f"{URL}/{scene[which]['id']}/payments", headers=scene["doctor"])
        missing = await api.get(f"{URL}/{uuid.uuid4()}", headers=scene["doctor"])

        assert invoice.status_code == 404
        assert payments.status_code == 404
        assert invoice.json()["error_code"] == missing.json()["error_code"] == "RESOURCE_NOT_FOUND"
        assert invoice.json()["message"] == missing.json()["message"]

    async def test_read_own_grants_no_write_access(
        self, api: AsyncClient, scene: dict[str, Any], patient_id: uuid.UUID
    ) -> None:
        mine = scene["mine"]["id"]
        doctor = scene["doctor"]

        responses = [
            await api.post(
                URL, json={"patient_id": str(patient_id), "items": [_line()]}, headers=doctor
            ),
            await api.patch(f"{URL}/{mine}", json={"notes": "x"}, headers=doctor),
            await api.post(f"{URL}/{mine}/issue", headers=doctor),
            await api.post(f"{URL}/{mine}/void", json={"reason": "x"}, headers=doctor),
            await _pay(api, doctor, mine, "10.00"),
            await api.get("/api/v1/services", headers=doctor),
        ]

        assert [r.status_code for r in responses] == [403] * len(responses)

    async def test_read_own_without_a_doctor_profile_sees_nothing(
        self,
        api: AsyncClient,
        scene: dict[str, Any],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        user = await insert_user_with_permissions(db_session, hospital_id, DOCTOR_PERMISSIONS)
        headers = auth_headers(user.id, hospital_id)

        listed = await api.get(URL, headers=headers)
        fetched = await api.get(f"{URL}/{scene['mine']['id']}", headers=headers)

        assert listed.status_code == 200
        assert listed.json()["data"] == []
        assert listed.json()["metadata"]["pagination"]["total_records"] == 0
        assert fetched.status_code == 404

    async def test_holding_invoice_read_as_well_lifts_the_scope(
        self,
        api: AsyncClient,
        scene: dict[str, Any],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        # The admin roles hold both codes; the wider one must win.
        user = await insert_user_with_permissions(
            db_session, hospital_id, ["invoice.read", "invoice.read.own"]
        )

        response = await api.get(URL, headers=auth_headers(user.id, hospital_id))

        assert response.json()["metadata"]["pagination"]["total_records"] == 3

    async def test_a_doctor_in_another_hospital_sees_none_of_it(
        self,
        api: AsyncClient,
        scene: dict[str, Any],
        db_session: AsyncSession,
        other_hospital_id: uuid.UUID,
    ) -> None:
        user = await insert_user_with_permissions(db_session, other_hospital_id, DOCTOR_PERMISSIONS)
        await insert_doctor(db_session, other_hospital_id, user_id=user.id)
        headers = auth_headers(user.id, other_hospital_id)

        assert (await api.get(URL, headers=headers)).json()["data"] == []
        assert (await api.get(f"{URL}/{scene['mine']['id']}", headers=headers)).status_code == 404
