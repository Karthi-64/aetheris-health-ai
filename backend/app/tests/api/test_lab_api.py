"""API tests for the Laboratory endpoints: test catalog and lab orders.

Real app, real services, real repositories, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3). Billing and
Notifications are the real modules too, so an order placed here really is
charged and a release really does notify.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.dependencies.db import get_db_session
from app.api.dependencies.services import get_audit_sink
from app.main import create_app
from app.models.appointment import AppointmentStatus
from app.models.billing import Invoice
from app.models.notification import Notification
from app.tests.billing_helpers import (
    auth_headers,
    insert_appointment,
    insert_doctor,
    insert_patient,
    insert_user_with_permissions,
)
from app.tests.conftest import RecordingAuditSink

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.appointment import Appointment
    from app.models.doctor import Doctor

pytestmark = pytest.mark.database

CATALOG = "/api/v1/tests-catalog"
ORDERS = "/api/v1/lab-orders"

CATALOG_PERMISSIONS = ["lab.test.read", "lab.test.create", "lab.test.update"]
ORDER_PERMISSIONS = [
    "lab.order.read",
    "lab.order.create",
    "lab.order.cancel",
    "lab.order.collect_sample",
    "lab.order.enter_results",
    "lab.order.release",
    "lab.order.amend",
]
ALL = [*CATALOG_PERMISSIONS, *ORDER_PERMISSIONS]

POTASSIUM = {
    "code": "K",
    "name": "Potassium",
    "category": "Biochemistry",
    "unit": "mmol/L",
    "reference_ranges": [
        {"sex": "any", "low": "3.5", "high": "5.1", "critical_low": "2.5", "critical_high": "6.5"}
    ],
    "turnaround_hours": 4,
    "price": "300.00",
}
HAEMOGLOBIN = {
    "code": "HB",
    "name": "Haemoglobin",
    "category": "Haematology",
    "unit": "g/dL",
    "reference_ranges": [
        {"sex": "male", "age_min": 18, "low": "13.0", "high": "17.0"},
        {"sex": "female", "age_min": 18, "low": "12.0", "high": "15.5"},
    ],
    "price": "250.00",
}


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
    """A user holding every lab permission."""
    user = await insert_user_with_permissions(db_session, hospital_id, ALL)
    return auth_headers(user.id, hospital_id)


@pytest_asyncio.fixture
async def other_tenant(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> dict[str, str]:
    """A fully-permissioned user in a different hospital."""
    user = await insert_user_with_permissions(db_session, other_hospital_id, ALL)
    return auth_headers(user.id, other_hospital_id)


@pytest_asyncio.fixture
async def doctor(db_session: AsyncSession, hospital_id: uuid.UUID) -> Doctor:
    """The ordering doctor, with a login that can read their own notifications."""
    user = await insert_user_with_permissions(
        db_session, hospital_id, ["notification.read.own", "lab.order.read"]
    )
    return await insert_doctor(db_session, hospital_id, user_id=user.id)


@pytest_asyncio.fixture
async def appointment(
    db_session: AsyncSession, hospital_id: uuid.UUID, doctor: Doctor
) -> Appointment:
    """A visit in progress: a woman born 1990-01-01, seen by :func:`doctor`."""
    patient = await insert_patient(db_session, hospital_id)
    return await insert_appointment(
        db_session,
        hospital_id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        status=AppointmentStatus.IN_PROGRESS,
    )


async def _add_test(api: AsyncClient, headers: dict[str, str], body: dict[str, Any]) -> str:
    """Create a catalog test through the API and return its id."""
    response = await api.post(CATALOG, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


@pytest_asyncio.fixture
async def tests(api: AsyncClient, admin: dict[str, str]) -> dict[str, str]:
    """Potassium and haemoglobin in the catalog, by code."""
    return {
        "K": await _add_test(api, admin, POTASSIUM),
        "HB": await _add_test(api, admin, HAEMOGLOBIN),
    }


async def _order(
    api: AsyncClient,
    headers: dict[str, str],
    appointment: Appointment,
    tests: dict[str, str],
    **extra: Any,
) -> dict[str, Any]:
    """Place an order for both tests and return the data block."""
    body = {"appointment_id": str(appointment.id), "test_ids": [tests["K"], tests["HB"]], **extra}
    response = await api.post(ORDERS, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


def _item(order: dict[str, Any], code: str) -> dict[str, Any]:
    return next(item for item in order["items"] if item["test_code"] == code)


async def _post(
    api: AsyncClient, headers: dict[str, str], order: dict[str, Any], step: str, body: Any = None
) -> dict[str, Any]:
    """Run a lifecycle step and return the data block."""
    response = await api.post(f"{ORDERS}/{order['id']}/{step}", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


async def _enter(
    api: AsyncClient, headers: dict[str, str], order: dict[str, Any], **values: str
) -> dict[str, Any]:
    results = [{"item_id": _item(order, code)["id"], "value": v} for code, v in values.items()]
    return await _post(api, headers, order, "enter-results", {"results": results})


async def _released(
    api: AsyncClient,
    headers: dict[str, str],
    appointment: Appointment,
    tests: dict[str, str],
    **values: str,
) -> dict[str, Any]:
    order = await _order(api, headers, appointment, tests)
    await _post(api, headers, order, "collect")
    await _enter(api, headers, order, **(values or {"K": "4.2", "HB": "13.0"}))
    return await _post(api, headers, order, "release")


def _first_field_error(response: Any) -> str:
    """Return the field named by a service-level 422 (it sits at ``errors.errors``)."""
    return str(response.json()["errors"]["errors"][0]["field"])


# ── Authorization ───────────────────────────────────────────────────────────

_ID = str(uuid.uuid4())
ENDPOINTS: list[tuple[str, str, str, dict[str, Any] | None]] = [
    ("POST", CATALOG, "lab.test.create", POTASSIUM),
    ("GET", CATALOG, "lab.test.read", None),
    ("GET", f"{CATALOG}/{_ID}", "lab.test.read", None),
    ("PATCH", f"{CATALOG}/{_ID}", "lab.test.update", {"name": "X"}),
    ("POST", ORDERS, "lab.order.create", {"appointment_id": _ID, "test_ids": [_ID]}),
    ("GET", ORDERS, "lab.order.read", None),
    ("GET", f"{ORDERS}/{_ID}", "lab.order.read", None),
    ("PATCH", f"{ORDERS}/{_ID}", "lab.order.create", {"notes": "x"}),
    ("POST", f"{ORDERS}/{_ID}/collect", "lab.order.collect_sample", None),
    (
        "POST",
        f"{ORDERS}/{_ID}/enter-results",
        "lab.order.enter_results",
        {"results": [{"item_id": _ID, "value": "1"}]},
    ),
    ("POST", f"{ORDERS}/{_ID}/release", "lab.order.release", None),
    ("POST", f"{ORDERS}/{_ID}/cancel", "lab.order.cancel", {"reason": "x"}),
    (
        "POST",
        f"{ORDERS}/{_ID}/items/{_ID}/amend",
        "lab.order.amend",
        {"new_value": "1", "reason": "x"},
    ),
]


class TestAuthorization:
    @pytest.mark.parametrize(("method", "url", "permission", "body"), ENDPOINTS)
    async def test_no_token_is_401(
        self, api: AsyncClient, method: str, url: str, permission: str, body: Any
    ) -> None:
        assert (await api.request(method, url, json=body)).status_code == 401

    @pytest.mark.parametrize(("method", "url", "permission", "body"), ENDPOINTS)
    async def test_every_other_lab_permission_together_is_still_403(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        method: str,
        url: str,
        permission: str,
        body: Any,
    ) -> None:
        # Holding everything except the one code the endpoint needs.
        others = [code for code in ALL if code != permission]
        user = await insert_user_with_permissions(db_session, hospital_id, others)

        response = await api.request(
            method, url, json=body, headers=auth_headers(user.id, hospital_id)
        )

        assert response.status_code == 403

    @pytest.mark.parametrize(("method", "url", "permission", "body"), ENDPOINTS)
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

        # 404/422 for the made-up ids, but never a permission failure.
        assert response.status_code not in (401, 403)


# ── Catalog ─────────────────────────────────────────────────────────────────


class TestCatalog:
    async def test_create_returns_the_test_and_audits(
        self, api: AsyncClient, admin: dict[str, str], audit: RecordingAuditSink
    ) -> None:
        response = await api.post(CATALOG, json={**POTASSIUM, "code": "k"}, headers=admin)

        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["code"] == "K"
        assert data["result_type"] == "numeric"
        assert data["price"] == "300.00"
        assert data["is_active"] is True
        assert data["reference_ranges"][0]["critical_high"] == "6.5"
        assert audit.actions() == ["lab.test.created"]

    async def test_a_duplicate_code_is_409(self, api: AsyncClient, admin: dict[str, str]) -> None:
        await _add_test(api, admin, POTASSIUM)

        response = await api.post(CATALOG, json=POTASSIUM, headers=admin)

        assert response.status_code == 409

    async def test_two_hospitals_may_use_the_same_code(
        self, api: AsyncClient, admin: dict[str, str], other_tenant: dict[str, str]
    ) -> None:
        await _add_test(api, admin, POTASSIUM)

        assert (await api.post(CATALOG, json=POTASSIUM, headers=other_tenant)).status_code == 201

    @pytest.mark.parametrize(
        "overrides",
        [
            {"reference_ranges": []},
            {"result_type": "text"},
            {"price": "-5.00"},
            {"reference_ranges": [{"low": "9", "high": "1"}]},
            {"hospital_id": str(uuid.uuid4())},
        ],
    )
    async def test_a_bad_test_is_422(
        self, api: AsyncClient, admin: dict[str, str], overrides: dict[str, Any]
    ) -> None:
        response = await api.post(CATALOG, json={**POTASSIUM, **overrides}, headers=admin)

        assert response.status_code == 422

    async def test_list_get_and_search(
        self, api: AsyncClient, admin: dict[str, str], tests: dict[str, str]
    ) -> None:
        listed = await api.get(CATALOG, headers=admin)
        searched = await api.get(CATALOG, params={"q": "pot"}, headers=admin)
        by_category = await api.get(CATALOG, params={"category": "Haematology"}, headers=admin)
        one = await api.get(f"{CATALOG}/{tests['K']}", headers=admin)

        assert [t["name"] for t in listed.json()["data"]] == ["Haemoglobin", "Potassium"]
        assert listed.json()["metadata"]["pagination"]["total_records"] == 2
        assert [t["code"] for t in searched.json()["data"]] == ["K"]
        assert [t["code"] for t in by_category.json()["data"]] == ["HB"]
        assert one.json()["data"]["code"] == "K"

    async def test_another_hospitals_catalog_is_invisible(
        self,
        api: AsyncClient,
        tests: dict[str, str],
        other_tenant: dict[str, str],
    ) -> None:
        listed = await api.get(CATALOG, headers=other_tenant)
        one = await api.get(f"{CATALOG}/{tests['K']}", headers=other_tenant)
        patched = await api.patch(
            f"{CATALOG}/{tests['K']}", json={"price": "1.00"}, headers=other_tenant
        )

        assert listed.json()["data"] == []
        assert one.status_code == 404
        assert patched.status_code == 404

    async def test_update_changes_the_price_and_audits_before_and_after(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        tests: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        response = await api.patch(
            f"{CATALOG}/{tests['K']}", json={"price": "320.00"}, headers=admin
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["price"] == "320.00"
        assert audit.last().action == "lab.test.updated"
        assert audit.last().changes == {"price": {"before": "300.00", "after": "320.00"}}

    @pytest.mark.parametrize(
        "body", [{"code": "NEW"}, {"result_type": "text"}, {"reference_ranges": []}, {"name": None}]
    )
    async def test_a_bad_update_is_422(
        self, api: AsyncClient, admin: dict[str, str], tests: dict[str, str], body: dict[str, Any]
    ) -> None:
        response = await api.patch(f"{CATALOG}/{tests['K']}", json=body, headers=admin)

        assert response.status_code == 422


# ── Ordering ────────────────────────────────────────────────────────────────


class TestCreateOrder:
    async def test_returns_201_with_patient_and_doctor_from_the_visit(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        audit.events.clear()

        order = await _order(api, admin, appointment, tests, priority="stat", notes="Fasting.")

        assert order["status"] == "ordered"
        assert order["priority"] == "stat"
        assert order["appointment_id"] == str(appointment.id)
        assert order["patient_id"] == str(appointment.patient_id)
        assert order["doctor_id"] == str(appointment.doctor_id)
        assert order["patient_name"] == "Ananya Rao"
        assert order["patient_mrn"].startswith("MRN-")
        assert order["doctor_name"] == "Dr. Asha Menon"
        assert [i["test_code"] for i in order["items"]] == ["K", "HB"]
        assert [i["price"] for i in order["items"]] == ["300.00", "250.00"]
        assert order["turnaround_minutes"] is None
        # The order and its charge are audited together.
        assert audit.actions() == ["invoice.drafted", "lab.order.created"]

    async def test_the_tests_are_charged_to_a_draft_invoice_for_the_visit(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        order = await _order(api, admin, appointment, tests)

        invoice = await db_session.get(Invoice, uuid.UUID(order["invoice_id"]))
        assert invoice is not None
        assert invoice.hospital_id == appointment.hospital_id
        assert invoice.patient_id == appointment.patient_id
        assert invoice.appointment_id == appointment.id
        assert invoice.status.value == "draft"
        assert [(i.description, str(i.line_total)) for i in invoice.items] == [
            ("Lab test — Potassium", "300.00"),
            ("Lab test — Haemoglobin", "250.00"),
        ]
        assert str(invoice.total) == "550.00"

    async def test_a_second_order_in_the_same_visit_joins_the_same_draft(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        first = await _order(api, admin, appointment, tests, test_ids=[tests["K"]])

        second = await _order(api, admin, appointment, tests, test_ids=[tests["HB"]])

        assert second["invoice_id"] == first["invoice_id"]
        invoice = await db_session.get(Invoice, uuid.UUID(first["invoice_id"]))
        assert invoice is not None
        await db_session.refresh(invoice)
        assert [i.position for i in invoice.items] == [0, 1]
        assert str(invoice.total) == "550.00"
        assert audit.actions()[-2:] == ["invoice.charges_added", "lab.order.created"]

    @pytest.mark.parametrize(
        ("mutate", "field"),
        [
            (lambda body, tests: body.update(appointment_id=str(uuid.uuid4())), "appointment_id"),
            (
                lambda body, tests: body.update(test_ids=[tests["K"], str(uuid.uuid4())]),
                "test_ids.1",
            ),
        ],
    )
    async def test_an_unknown_reference_is_422_and_nothing_is_ordered_or_charged(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
        mutate: Any,
        field: str,
    ) -> None:
        body: dict[str, Any] = {"appointment_id": str(appointment.id), "test_ids": [tests["K"]]}
        mutate(body, tests)

        response = await api.post(ORDERS, json=body, headers=admin)

        assert response.status_code == 422
        assert _first_field_error(response) == field
        assert (await api.get(ORDERS, headers=admin)).json()["data"] == []
        invoices = await db_session.execute(
            select(Invoice).where(Invoice.patient_id == appointment.patient_id)
        )
        assert invoices.scalars().all() == []

    async def test_another_hospitals_appointment_or_test_is_unknown(
        self,
        api: AsyncClient,
        other_tenant: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        theirs = await _add_test(api, other_tenant, POTASSIUM)

        with_our_visit = await api.post(
            ORDERS,
            json={"appointment_id": str(appointment.id), "test_ids": [theirs]},
            headers=other_tenant,
        )

        assert with_our_visit.status_code == 422
        assert _first_field_error(with_our_visit) == "appointment_id"

    async def test_an_inactive_test_cannot_be_ordered(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        await api.patch(f"{CATALOG}/{tests['K']}", json={"is_active": False}, headers=admin)

        response = await api.post(
            ORDERS,
            json={"appointment_id": str(appointment.id), "test_ids": [tests["K"]]},
            headers=admin,
        )

        assert response.status_code == 422
        assert "inactive" in response.json()["message"]

    async def test_a_cancelled_visit_cannot_be_ordered_against(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        appointment.status = AppointmentStatus.CANCELLED
        await db_session.flush()

        response = await api.post(
            ORDERS,
            json={"appointment_id": str(appointment.id), "test_ids": [tests["K"]]},
            headers=admin,
        )

        assert response.status_code == 422


class TestReadOrders:
    async def test_get_and_list_with_filters(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        routine = await _order(api, admin, appointment, tests, test_ids=[tests["K"]])
        urgent = await _order(
            api, admin, appointment, tests, test_ids=[tests["HB"]], priority="urgent"
        )
        await _post(api, admin, routine, "collect")

        async def ids(**params: Any) -> list[str]:
            response = await api.get(ORDERS, params=params, headers=admin)
            assert response.status_code == 200, response.text
            return [o["id"] for o in response.json()["data"]]

        assert set(await ids()) == {routine["id"], urgent["id"]}
        assert await ids(status="collected") == [routine["id"]]
        assert await ids(priority="urgent") == [urgent["id"]]
        assert len(await ids(patient_id=str(appointment.patient_id))) == 2
        assert len(await ids(appointment_id=str(appointment.id), page_size=1)) == 1
        assert await ids(doctor_id=str(uuid.uuid4())) == []
        one = await api.get(f"{ORDERS}/{routine['id']}", headers=admin)
        assert one.json()["data"]["status"] == "collected"

    async def test_a_bad_filter_is_422(self, api: AsyncClient, admin: dict[str, str]) -> None:
        assert (await api.get(ORDERS, params={"status": "lost"}, headers=admin)).status_code == 422

    async def test_another_hospitals_order_is_404_everywhere(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        other_tenant: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        order = await _order(api, admin, appointment, tests)
        item_id = order["items"][0]["id"]
        base = f"{ORDERS}/{order['id']}"

        attempts = [
            await api.get(base, headers=other_tenant),
            await api.patch(base, json={"notes": "x"}, headers=other_tenant),
            await api.post(f"{base}/collect", headers=other_tenant),
            await api.post(
                f"{base}/enter-results",
                json={"results": [{"item_id": item_id, "value": "1"}]},
                headers=other_tenant,
            ),
            await api.post(f"{base}/release", headers=other_tenant),
            await api.post(f"{base}/cancel", json={"reason": "x"}, headers=other_tenant),
            await api.post(
                f"{base}/items/{item_id}/amend",
                json={"new_value": "1", "reason": "x"},
                headers=other_tenant,
            ),
        ]

        assert [r.status_code for r in attempts] == [404] * 7
        assert (await api.get(ORDERS, headers=other_tenant)).json()["data"] == []
        # And nothing happened to it.
        assert (await api.get(base, headers=admin)).json()["data"]["status"] == "ordered"


# ── Lifecycle ───────────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_collect_with_no_body_generates_sample_ids(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        order = await _order(api, admin, appointment, tests)

        collected = await _post(api, admin, order, "collect")

        assert collected["status"] == "collected"
        assert collected["collected_at"] is not None
        assert all(i["sample_id"].startswith("S-") for i in collected["items"])
        assert audit.last().action == "lab.order.samples_collected"

    async def test_a_sample_id_already_used_in_the_hospital_is_409(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        first = await _order(api, admin, appointment, tests, test_ids=[tests["K"]])
        second = await _order(api, admin, appointment, tests, test_ids=[tests["HB"]])
        await _post(
            api,
            admin,
            first,
            "collect",
            {"items": [{"item_id": first["items"][0]["id"], "sample_id": "BC-001"}]},
        )

        clash = await api.post(
            f"{ORDERS}/{second['id']}/collect",
            json={"items": [{"item_id": second["items"][0]["id"], "sample_id": "bc-001"}]},
            headers=admin,
        )

        assert clash.status_code == 409
        # The order is untouched and can still be collected properly.
        assert (await _post(api, admin, second, "collect"))["status"] == "collected"

    async def test_ac2_results_are_flagged_for_the_patients_sex_and_age(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        order = await _order(api, admin, appointment, tests)
        await _post(api, admin, order, "collect")

        result = await _enter(api, admin, order, K="5.6", HB="12.5")

        potassium, haemoglobin = _item(result, "K"), _item(result, "HB")
        assert potassium["result_flag"] == "high"
        assert (potassium["reference_low"], potassium["reference_high"]) == ("3.5000", "5.1000")
        assert potassium["result_unit"] == "mmol/L"
        # 12.5 is low for a man (13.0–17.0) but normal for this woman (12.0–15.5).
        assert haemoglobin["result_flag"] == "normal"
        assert result["status"] == "results_entered"
        assert (result["has_abnormal"], result["has_critical"]) == (True, False)

    async def test_a_client_cannot_supply_its_own_flag(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        order = await _order(api, admin, appointment, tests)
        await _post(api, admin, order, "collect")

        response = await api.post(
            f"{ORDERS}/{order['id']}/enter-results",
            json={
                "results": [
                    {"item_id": _item(order, "K")["id"], "value": "9.9", "result_flag": "normal"}
                ]
            },
            headers=admin,
        )

        assert response.status_code == 422

    async def test_a_non_numeric_value_for_a_numeric_test_is_422(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        order = await _order(api, admin, appointment, tests)
        await _post(api, admin, order, "collect")

        response = await api.post(
            f"{ORDERS}/{order['id']}/enter-results",
            json={"results": [{"item_id": _item(order, "K")["id"], "value": "raised"}]},
            headers=admin,
        )

        assert response.status_code == 422
        assert _first_field_error(response) == "results.0.value"

    @pytest.mark.parametrize(
        ("step", "body"),
        [
            ("enter-results", {"results": [{"item_id": str(uuid.uuid4()), "value": "1"}]}),
            ("release", None),
        ],
    )
    async def test_steps_out_of_order_are_400(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
        step: str,
        body: Any,
    ) -> None:
        order = await _order(api, admin, appointment, tests)

        response = await api.post(f"{ORDERS}/{order['id']}/{step}", json=body, headers=admin)

        assert response.status_code == 400
        assert response.json()["error_code"] == "BUSINESS_RULE_VIOLATION"

    async def test_release_needs_every_result(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        order = await _order(api, admin, appointment, tests)
        await _post(api, admin, order, "collect")
        partial = await _enter(api, admin, order, K="4.2")

        response = await api.post(f"{ORDERS}/{order['id']}/release", headers=admin)

        assert partial["status"] == "in_progress"
        assert response.status_code == 400

    async def test_release_fixes_the_results_and_notifies_the_doctor(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        doctor: Doctor,
        tests: dict[str, str],
        hospital_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        released = await _released(api, admin, appointment, tests)

        assert released["status"] == "released"
        assert released["released_at"] is not None
        assert released["turnaround_minutes"] == 0
        assert all(i["released_at"] is not None for i in released["items"])
        assert audit.last().action == "lab.order.released"
        # The doctor sees it in their own notification centre.
        centre = await api.get(
            "/api/v1/notifications", headers=auth_headers(doctor.user_id, hospital_id)
        )
        [notice] = centre.json()["data"]
        assert notice["kind"] == "lab.results_released"
        assert notice["title"] == "Lab results ready for Ananya Rao"
        assert "Potassium, Haemoglobin" in notice["body"]
        assert notice["link"] == "/laboratory"
        # After release, re-entry, cancel and a second release are all refused.
        base = f"{ORDERS}/{released['id']}"
        refused = [
            await api.post(
                f"{base}/enter-results",
                json={"results": [{"item_id": _item(released, "K")["id"], "value": "9"}]},
                headers=admin,
            ),
            await api.post(f"{base}/cancel", json={"reason": "x"}, headers=admin),
            await api.post(f"{base}/release", headers=admin),
            await api.patch(base, json={"notes": "x"}, headers=admin),
            await api.post(f"{base}/collect", headers=admin),
        ]
        assert [r.status_code for r in refused] == [400] * 5

    async def test_a_critical_value_notifies_the_doctor_before_release(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        doctor: Doctor,
        tests: dict[str, str],
    ) -> None:
        order = await _order(api, admin, appointment, tests)
        await _post(api, admin, order, "collect")

        result = await _enter(api, admin, order, K="6.8")

        assert _item(result, "K")["result_flag"] == "critical"
        assert result["has_critical"] is True
        rows = await db_session.execute(
            select(Notification).where(Notification.recipient_user_id == doctor.user_id)
        )
        [notice] = rows.scalars().all()
        assert notice.kind == "lab.critical_result"
        assert "Potassium for Ananya Rao is 6.8 mmol/L" in notice.body
        assert notice.hospital_id == appointment.hospital_id

    async def test_cancel_keeps_the_reason_and_leaves_the_charge_alone(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        order = await _order(api, admin, appointment, tests)

        cancelled = await _post(api, admin, order, "cancel", {"reason": "Ordered in error"})

        assert cancelled["status"] == "cancelled"
        assert cancelled["cancel_reason"] == "Ordered in error"
        assert audit.last().action == "lab.order.cancelled"
        assert audit.last().context["invoice_id"] == order["invoice_id"]
        invoice = await db_session.get(Invoice, uuid.UUID(order["invoice_id"]))
        assert invoice is not None
        assert str(invoice.total) == "550.00"
        again = await api.post(
            f"{ORDERS}/{order['id']}/cancel", json={"reason": "again"}, headers=admin
        )
        assert again.status_code == 400

    async def test_patch_changes_priority_and_notes(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        order = await _order(api, admin, appointment, tests)

        response = await api.patch(
            f"{ORDERS}/{order['id']}", json={"priority": "urgent", "notes": "Chase."}, headers=admin
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["priority"] == "urgent"
        assert response.json()["data"]["notes"] == "Chase."
        assert audit.last().action == "lab.order.updated"
        tampered = await api.patch(
            f"{ORDERS}/{order['id']}", json={"status": "released"}, headers=admin
        )
        assert tampered.status_code == 422


class TestAmend:
    async def test_rule_4_an_amendment_keeps_the_old_value_and_reflags(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: dict[str, str],
        appointment: Appointment,
        doctor: Doctor,
        tests: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        released = await _released(api, admin, appointment, tests, K="4.2", HB="13.0")
        item = _item(released, "K")

        response = await api.post(
            f"{ORDERS}/{released['id']}/items/{item['id']}/amend",
            json={"new_value": "5.9", "reason": "Transcription error"},
            headers=admin,
        )

        assert response.status_code == 200, response.text
        amended = _item(response.json()["data"], "K")
        assert amended["result_value"] == "5.9"
        assert amended["result_flag"] == "high"
        [amendment] = amended["amendments"]
        assert (amendment["previous_value"], amendment["new_value"]) == ("4.2", "5.9")
        assert (amendment["previous_flag"], amendment["new_flag"]) == ("normal", "high")
        assert amendment["reason"] == "Transcription error"
        assert response.json()["data"]["status"] == "released"
        assert audit.last().action == "lab.order.result_amended"
        rows = await db_session.execute(
            select(Notification.kind)
            .where(Notification.recipient_user_id == doctor.user_id)
            .order_by(Notification.created_at, Notification.kind)
        )
        assert sorted(rows.scalars().all()) == ["lab.result_amended", "lab.results_released"]

    async def test_amending_before_release_is_400(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        order = await _order(api, admin, appointment, tests)
        await _post(api, admin, order, "collect")
        await _enter(api, admin, order, K="4.2", HB="13.0")

        response = await api.post(
            f"{ORDERS}/{order['id']}/items/{_item(order, 'K')['id']}/amend",
            json={"new_value": "5.9", "reason": "x"},
            headers=admin,
        )

        assert response.status_code == 400

    async def test_an_item_from_another_order_is_404(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
    ) -> None:
        released = await _released(api, admin, appointment, tests)
        other = await _order(api, admin, appointment, tests, test_ids=[tests["K"]])

        response = await api.post(
            f"{ORDERS}/{released['id']}/items/{other['items'][0]['id']}/amend",
            json={"new_value": "5.9", "reason": "x"},
            headers=admin,
        )

        assert response.status_code == 404

    @pytest.mark.parametrize(
        "body",
        [
            {"new_value": "4.2", "reason": "Same value"},
            {"new_value": "raised", "reason": "Not a number"},
            {"new_value": "5.9"},
        ],
    )
    async def test_a_bad_amendment_is_422(
        self,
        api: AsyncClient,
        admin: dict[str, str],
        appointment: Appointment,
        tests: dict[str, str],
        body: dict[str, Any],
    ) -> None:
        released = await _released(api, admin, appointment, tests, K="4.2", HB="13.0")

        response = await api.post(
            f"{ORDERS}/{released['id']}/items/{_item(released, 'K')['id']}/amend",
            json=body,
            headers=admin,
        )

        assert response.status_code == 422
