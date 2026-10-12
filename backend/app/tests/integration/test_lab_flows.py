"""End-to-end Laboratory flows, and the lab section of the demo seed.

``docs/modules/07-laboratory.md`` §16: "order → collect → result → release".
The flow here is run by the people the spec gives each step to — a doctor
orders, a technician collects and enters, a supervisor releases — each with
only the permissions the seed gives their role, against the real Billing and
Notifications modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.dependencies.db import get_db_session
from app.main import create_app
from app.models.appointment import AppointmentStatus
from app.models.hospital import Hospital
from app.models.lab import LabResultType, LabTest
from app.schemas.lab import CreateLabTestRequest
from app.seeds.demo_data import seed_demo_data
from app.seeds.demo_lab import LAB_TESTS, seed_demo_lab
from app.seeds.seed import SYSTEM_ROLES
from app.tests.billing_helpers import (
    auth_headers,
    insert_appointment,
    insert_doctor,
    insert_patient,
    insert_user_with_permissions,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

ORDERS = "/api/v1/lab-orders"


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


class TestOrderToRelease:
    async def test_doctor_orders_technician_runs_supervisor_releases(
        self, api: AsyncClient, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_lab(db_session, hospital)
        doctor_user = await insert_user_with_permissions(
            db_session, hospital.id, _seeded_permissions("Doctor")
        )
        technician = await insert_user_with_permissions(
            db_session, hospital.id, _seeded_permissions("Lab Technician")
        )
        supervisor = await insert_user_with_permissions(
            db_session, hospital.id, _seeded_permissions("Hospital Admin")
        )
        as_doctor = auth_headers(doctor_user.id, hospital.id)
        as_technician = auth_headers(technician.id, hospital.id)
        as_supervisor = auth_headers(supervisor.id, hospital.id)

        doctor = await insert_doctor(db_session, hospital.id, user_id=doctor_user.id)
        patient = await insert_patient(db_session, hospital.id)
        appointment = await insert_appointment(
            db_session,
            hospital.id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            status=AppointmentStatus.IN_PROGRESS,
        )

        # The doctor picks tests from the catalog and orders them.
        catalog = await api.get(
            "/api/v1/tests-catalog",
            params={"is_active": "true", "page_size": 100},
            headers=as_doctor,
        )
        assert catalog.status_code == 200, catalog.text
        by_code = {test["code"]: test["id"] for test in catalog.json()["data"]}
        assert "ESR" not in by_code  # retired
        placed = await api.post(
            ORDERS,
            json={
                "appointment_id": str(appointment.id),
                "test_ids": [by_code["K"], by_code["HB"], by_code["URINE-ME"]],
                "priority": "urgent",
            },
            headers=as_doctor,
        )
        assert placed.status_code == 201, placed.text
        order = placed.json()["data"]
        base = f"{ORDERS}/{order['id']}"
        items = {item["test_code"]: item["id"] for item in order["items"]}

        # A doctor orders; they do not run the lab.
        assert (await api.post(f"{base}/collect", headers=as_doctor)).status_code == 403

        # The technician finds it on the worklist, collects, and enters results.
        worklist = await api.get(ORDERS, params={"status": "ordered"}, headers=as_technician)
        assert [o["id"] for o in worklist.json()["data"]] == [order["id"]]
        assert (await api.post(f"{base}/collect", headers=as_technician)).status_code == 200
        entered = await api.post(
            f"{base}/enter-results",
            json={
                "results": [
                    {"item_id": items["K"], "value": "6.9"},
                    {"item_id": items["HB"], "value": "10.4", "notes": "Repeat advised."},
                    {"item_id": items["URINE-ME"], "value": "No casts or crystals seen."},
                ]
            },
            headers=as_technician,
        )
        assert entered.status_code == 200, entered.text
        flags = {i["test_code"]: i["result_flag"] for i in entered.json()["data"]["items"]}
        assert flags == {"K": "critical", "HB": "low", "URINE-ME": None}

        # Release is the supervisor's step, not the technician's (§3).
        assert (await api.post(f"{base}/release", headers=as_technician)).status_code == 403
        released = await api.post(f"{base}/release", headers=as_supervisor)
        assert released.status_code == 200, released.text
        assert released.json()["data"]["status"] == "released"

        # The doctor was told about the critical value first, then the release.
        centre = await api.get("/api/v1/notifications", headers=as_doctor)
        kinds = [n["kind"] for n in centre.json()["data"]]
        assert sorted(kinds) == ["lab.critical_result", "lab.results_released"]
        # ...and reads the results.
        seen = await api.get(base, headers=as_doctor)
        assert seen.json()["data"]["has_critical"] is True

        # Billing has a draft for the visit with one line per test.
        invoice = await api.get(f"/api/v1/invoices/{order['invoice_id']}", headers=as_supervisor)
        assert invoice.status_code == 200, invoice.text
        bill = invoice.json()["data"]
        assert bill["status"] == "draft"
        assert bill["appointment_id"] == str(appointment.id)
        assert [line["description"] for line in bill["items"]] == [
            "Lab test — Serum potassium",
            "Lab test — Haemoglobin",
            "Lab test — Urine microscopy",
        ]
        assert bill["total"] == "750.00"

        # Every step is in the durable audit trail, with who did it.
        trail = await api.get(
            "/api/v1/audit-logs", params={"page_size": 100}, headers=as_supervisor
        )
        assert trail.status_code == 200, trail.text
        actions = {entry["action"] for entry in trail.json()["data"]}
        assert {
            "lab.order.created",
            "lab.order.samples_collected",
            "lab.order.results_entered",
            "lab.order.released",
            "invoice.drafted",
        } <= actions

    async def test_a_test_ordered_mid_visit_does_not_cost_the_consultation_fee(
        self, api: AsyncClient, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        # The usual order of events: tests are ordered during the consultation,
        # and the visit is completed afterwards.
        await seed_demo_lab(db_session, hospital)
        admin = await insert_user_with_permissions(
            db_session, hospital.id, _seeded_permissions("Hospital Admin")
        )
        headers = auth_headers(admin.id, hospital.id)
        doctor = await insert_doctor(db_session, hospital.id, consultation_fee="800.00")
        patient = await insert_patient(db_session, hospital.id)
        appointment = await insert_appointment(
            db_session,
            hospital.id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            status=AppointmentStatus.IN_PROGRESS,
        )
        potassium = (
            await db_session.execute(
                select(LabTest.id).where(LabTest.hospital_id == hospital.id, LabTest.code == "K")
            )
        ).scalar_one()
        placed = await api.post(
            ORDERS,
            json={"appointment_id": str(appointment.id), "test_ids": [str(potassium)]},
            headers=headers,
        )
        assert placed.status_code == 201, placed.text
        invoice_url = f"/api/v1/invoices/{placed.json()['data']['invoice_id']}"

        completed = await api.post(
            f"/api/v1/appointments/{appointment.id}/complete", headers=headers
        )

        assert completed.status_code == 200, completed.text
        bill = (await api.get(invoice_url, headers=headers)).json()["data"]
        assert [line["line_total"] for line in bill["items"]] == ["300.00", "800.00"]
        assert bill["total"] == "1100.00"
        assert bill["appointment_id"] == str(appointment.id)
        # One bill for the visit, not two.
        listed = await api.get(f"/api/v1/invoices?patient_id={patient.id}", headers=headers)
        assert [invoice["id"] for invoice in listed.json()["data"]] == [bill["id"]]

    async def test_an_order_joins_the_invoice_the_completed_visit_already_has(
        self, api: AsyncClient, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_lab(db_session, hospital)
        admin = await insert_user_with_permissions(
            db_session, hospital.id, _seeded_permissions("Hospital Admin")
        )
        headers = auth_headers(admin.id, hospital.id)
        doctor = await insert_doctor(db_session, hospital.id, consultation_fee="800.00")
        patient = await insert_patient(db_session, hospital.id)
        appointment = await insert_appointment(
            db_session,
            hospital.id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            status=AppointmentStatus.IN_PROGRESS,
        )
        # Completing the visit drafts its invoice with the consultation fee.
        completed = await api.post(
            f"/api/v1/appointments/{appointment.id}/complete", headers=headers
        )
        assert completed.status_code == 200, completed.text
        potassium = (
            await db_session.execute(
                select(LabTest.id).where(LabTest.hospital_id == hospital.id, LabTest.code == "K")
            )
        ).scalar_one()

        placed = await api.post(
            ORDERS,
            json={"appointment_id": str(appointment.id), "test_ids": [str(potassium)]},
            headers=headers,
        )

        assert placed.status_code == 201, placed.text
        bill = (
            await api.get(
                f"/api/v1/invoices/{placed.json()['data']['invoice_id']}", headers=headers
            )
        ).json()["data"]
        assert [line["line_total"] for line in bill["items"]] == ["800.00", "300.00"]
        assert bill["total"] == "1100.00"

        # Once that invoice is issued its lines are frozen, so a later order
        # for the same visit is charged to a fresh draft.
        assert (
            await api.post(f"/api/v1/invoices/{bill['id']}/issue", headers=headers)
        ).status_code == 200
        haemoglobin = (
            await db_session.execute(
                select(LabTest.id).where(LabTest.hospital_id == hospital.id, LabTest.code == "HB")
            )
        ).scalar_one()
        later = await api.post(
            ORDERS,
            json={"appointment_id": str(appointment.id), "test_ids": [str(haemoglobin)]},
            headers=headers,
        )
        assert later.status_code == 201, later.text
        assert later.json()["data"]["invoice_id"] != bill["id"]
        issued = (await api.get(f"/api/v1/invoices/{bill['id']}", headers=headers)).json()["data"]
        assert issued["total"] == "1100.00"


class TestSeededLab:
    async def test_the_catalog_is_valid_and_complete(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})

        rows = await db_session.execute(select(LabTest).where(LabTest.hospital_id == hospital.id))
        tests = list(rows.unique().scalars().all())

        assert {test.code for test in tests} == {code for code, *_ in LAB_TESTS}
        assert any(not test.is_active for test in tests)
        assert {test.result_type for test in tests} == {LabResultType.NUMERIC, LabResultType.TEXT}
        # Every seeded test would be accepted by the API's own validation.
        for test in tests:
            payload: dict[str, Any] = {
                "code": test.code,
                "name": test.name,
                "category": test.category,
                "unit": test.unit,
                "result_type": test.result_type,
                "reference_ranges": test.reference_ranges,
                "turnaround_hours": test.turnaround_hours,
                "price": str(test.price),
            }
            CreateLabTestRequest.model_validate(payload)

    async def test_a_second_run_creates_nothing_and_keeps_an_edited_range(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        potassium = (
            await db_session.execute(
                select(LabTest).where(LabTest.hospital_id == hospital.id, LabTest.code == "K")
            )
        ).scalar_one()
        edited = [{"sex": "any", "low": "3.6", "high": "5.0"}]
        potassium.reference_ranges = edited
        await db_session.flush()

        assert await seed_demo_lab(db_session, hospital) == 0

        count = await db_session.execute(
            select(func.count()).select_from(LabTest).where(LabTest.hospital_id == hospital.id)
        )
        assert count.scalar_one() == len(LAB_TESTS)
        await db_session.refresh(potassium)
        assert potassium.reference_ranges == edited
