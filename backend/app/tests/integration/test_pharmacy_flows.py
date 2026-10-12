"""End-to-end Pharmacy flows, the concurrency guarantee, and the demo seed.

``docs/modules/08-pharmacy.md`` §16:

- **Integration:** prescription → dispense → bill line → payment, run by the
  people the spec gives each step to, each with only the permissions the seed
  gives their role.
- **Concurrency:** many simultaneous dispenses against one batch. These cannot
  use the rolled-back ``db_session`` fixture — proving that transactions take
  turns requires them to be genuinely separate and to really commit — so they
  build a committed hospital and delete it afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.db import get_db_session
from app.core.exceptions import ConflictError
from app.main import create_app
from app.models.appointment import Appointment, AppointmentStatus
from app.models.doctor import Doctor
from app.models.hospital import Hospital
from app.models.patient import Patient
from app.models.pharmacy import (
    Dispense,
    DispenseItem,
    Medicine,
    MedicineBatch,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
    StockMovement,
    StockMovementReason,
    Vendor,
)
from app.models.user import User
from app.repositories.appointment_repository import AppointmentRepository
from app.repositories.hospital_repository import HospitalRepository
from app.repositories.medicine_repository import MedicineRepository
from app.repositories.prescription_repository import PrescriptionRepository
from app.schemas.pharmacy import CreateMedicineRequest, CreatePrescriptionRequest
from app.seeds.demo_data import seed_demo_data
from app.seeds.demo_pharmacy import BATCHES, DEMO_VENDOR, MEDICINES, seed_demo_pharmacy
from app.seeds.seed import SYSTEM_ROLES
from app.services.dispensing_service import DispensingService
from app.tests.billing_helpers import (
    auth_headers,
    insert_appointment,
    insert_doctor,
    insert_patient,
    insert_user,
    insert_user_with_permissions,
)
from app.tests.conftest import RecordingAuditSink
from app.tests.pharmacy_helpers import insert_batch, insert_medicine

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.database

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


class TestPrescriptionToPayment:
    async def test_doctor_prescribes_pharmacist_dispenses_billing_takes_payment(
        self, api: AsyncClient, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        async def login(role: str) -> tuple[User, dict[str, str]]:
            user = await insert_user_with_permissions(
                db_session, hospital.id, _seeded_permissions(role)
            )
            return user, auth_headers(user.id, hospital.id)

        doctor_user, as_doctor = await login("Doctor")
        _, as_pharmacist = await login("Pharmacist")
        _, as_admin = await login("Hospital Admin")
        _, as_billing = await login("Billing Staff")

        para = await insert_medicine(db_session, hospital.id, "PARA", name="Paracetamol")
        amox = await insert_medicine(
            db_session, hospital.id, "AMOX", name="Amoxicillin", unit_price="8.00"
        )
        await insert_batch(db_session, para, "PA-SOON", 6, days=20)
        await insert_batch(db_session, para, "PA-LATE", 500, days=365)
        doctor = await insert_doctor(db_session, hospital.id, user_id=doctor_user.id)
        patient = await insert_patient(db_session, hospital.id)
        appointment = await insert_appointment(
            db_session,
            hospital.id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            status=AppointmentStatus.IN_PROGRESS,
        )

        # The doctor finds the medicines and prescribes.
        found = await api.get("/api/v1/medicines", params={"q": "para"}, headers=as_doctor)
        assert [m["sku"] for m in found.json()["data"]] == ["PARA"]
        written = await api.post(
            "/api/v1/prescriptions",
            json={
                "appointment_id": str(appointment.id),
                "notes": "Review in five days.",
                "items": [
                    {
                        "medicine_id": str(para.id),
                        "dosage": "1 tablet",
                        "frequency": "three times daily",
                        "duration_days": 5,
                        "instructions": "After food.",
                        "quantity": 15,
                    },
                    {
                        "medicine_id": str(amox.id),
                        "dosage": "1 capsule",
                        "frequency": "three times daily",
                        "duration_days": 7,
                        "quantity": 21,
                    },
                ],
            },
            headers=as_doctor,
        )
        assert written.status_code == 201, written.text
        rx = written.json()["data"]
        base = f"/api/v1/prescriptions/{rx['id']}"
        # A doctor prescribes; they do not dispense.
        assert (await api.post(f"{base}/dispense", headers=as_doctor)).status_code == 403

        # The pharmacist sees it in the queue, with amoxicillin out of stock.
        queue = await api.get("/api/v1/prescriptions/pending", headers=as_pharmacist)
        [waiting] = queue.json()["data"]
        assert [i["available_quantity"] for i in waiting["items"]] == [506, 0]
        # AC-3: dispensing everything fails whole.
        short = await api.post(f"{base}/dispense", headers=as_pharmacist)
        assert short.status_code == 409
        # Stock arrives: the pharmacist cannot raise an order, but can receive one.
        vendor = await api.post("/api/v1/vendors", json={"name": "Acme"}, headers=as_admin)
        order_body = {
            "vendor_id": vendor.json()["data"]["id"],
            "items": [{"medicine_id": str(amox.id), "quantity": 200, "unit_price": "4.00"}],
        }
        assert (
            await api.post("/api/v1/purchase-orders", json=order_body, headers=as_pharmacist)
        ).status_code == 403
        order = (
            await api.post("/api/v1/purchase-orders", json=order_body, headers=as_admin)
        ).json()["data"]
        await api.post(f"/api/v1/purchase-orders/{order['id']}/send", headers=as_admin)
        received = await api.post(
            f"/api/v1/purchase-orders/{order['id']}/receive",
            json={
                "items": [
                    {
                        "po_item_id": order["items"][0]["id"],
                        "batch_number": "AX-1",
                        "expiry_date": (TODAY + timedelta(days=300)).isoformat(),
                        "quantity": 200,
                    }
                ]
            },
            headers=as_pharmacist,
        )
        assert received.status_code == 200, received.text

        # Now the whole prescription goes out, first-expiry-first.
        dispensed = await api.post(f"{base}/dispense", headers=as_pharmacist)
        assert dispensed.status_code == 201, dispensed.text
        dispense = dispensed.json()["data"]
        assert [(i["batch_number"], i["quantity"]) for i in dispense["items"]] == [
            ("PA-SOON", 6),
            ("PA-LATE", 9),
            ("AX-1", 21),
        ]
        assert dispense["total_amount"] == "205.50"  # 15 × 2.50 + 21 × 8.00
        assert any("PA-SOON" in warning for warning in dispense["warnings"])
        assert (await api.get(base, headers=as_doctor)).json()["data"]["status"] == "dispensed"

        # Billing finds the draft, issues it and takes payment in full.
        invoice_url = f"/api/v1/invoices/{dispense['invoice_id']}"
        bill = (await api.get(invoice_url, headers=as_billing)).json()["data"]
        assert bill["status"] == "draft"
        assert [(line["description"], line["line_total"]) for line in bill["items"]] == [
            ("Medicine — Paracetamol 500 mg", "37.50"),
            ("Medicine — Amoxicillin 500 mg", "168.00"),
        ]
        assert bill["total"] == "205.50"
        issued = await api.post(f"{invoice_url}/issue", headers=as_billing)
        assert issued.status_code == 200, issued.text
        paid = await api.post(
            f"{invoice_url}/payments",
            json={"amount": "205.50", "method": "upi", "reference": "UPI-TEST-1"},
            headers={**as_billing, "Idempotency-Key": f"pharmacy-flow-{uuid.uuid4().hex}"},
        )
        assert paid.status_code == 201, paid.text
        assert (await api.get(invoice_url, headers=as_billing)).json()["data"]["status"] == "paid"

        # Every step is in the durable audit trail.
        trail = await api.get("/api/v1/audit-logs", params={"page_size": 100}, headers=as_admin)
        actions = {entry["action"] for entry in trail.json()["data"]}
        assert {
            "pharmacy.prescription.created",
            "pharmacy.po.received",
            "pharmacy.dispensed",
            "invoice.drafted",
            "invoice.issued",
        } <= actions


# ── Concurrency ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _World:
    """Ids of rows that are really committed for a concurrency test."""

    hospital_id: uuid.UUID
    pharmacist_id: uuid.UUID
    medicine_id: uuid.UUID
    batch_id: uuid.UUID
    appointment_id: uuid.UUID


STOCK = 10


@pytest.fixture
async def world(db_engine: AsyncEngine) -> AsyncGenerator[_World]:
    """A committed hospital with one batch of ten units, removed afterwards."""
    hospital_id = uuid.uuid4()
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        setup.add(
            Hospital(
                id=hospital_id,
                name="Pharmacy Concurrency Test Hospital",
                slug=f"pharmacy-race-{uuid.uuid4().hex[:12]}",
                address={"line1": "1 Test Road", "city": "Hyderabad", "country": "IN"},
                settings={},
            )
        )
        await setup.flush()
        pharmacist = await insert_user(setup, hospital_id)
        patient = await insert_patient(setup, hospital_id)
        doctor = await insert_doctor(setup, hospital_id)
        appointment = await insert_appointment(
            setup, hospital_id, patient_id=patient.id, doctor_id=doctor.id
        )
        medicine = await insert_medicine(setup, hospital_id, "LAST")
        batch = await insert_batch(setup, medicine, "ONLY", STOCK)
        await setup.commit()

    yield _World(hospital_id, pharmacist.id, medicine.id, batch.id, appointment.id)

    async with AsyncSession(db_engine) as cleanup:
        for model in (
            DispenseItem,
            Dispense,
            PrescriptionItem,
            Prescription,
            StockMovement,
            MedicineBatch,
            Medicine,
            Appointment,
            Doctor,
            Patient,
            User,
        ):
            await cleanup.execute(delete(model).where(model.hospital_id == hospital_id))
        await cleanup.execute(delete(Hospital).where(Hospital.id == hospital_id))
        await cleanup.commit()


def _service(session: AsyncSession) -> DispensingService:
    """A dispensing service on its own session, as one request would build it."""
    return DispensingService(
        PrescriptionRepository(session),
        MedicineRepository(session),
        AppointmentRepository(session),
        HospitalRepository(session),
        session,
        RecordingAuditSink(),
    )


async def _committed_prescription(engine: AsyncEngine, world: _World, quantity: int) -> uuid.UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        rx = await _service(session).create_prescription(
            world.hospital_id,
            CreatePrescriptionRequest.model_validate(
                {
                    "appointment_id": str(world.appointment_id),
                    "items": [
                        {
                            "medicine_id": str(world.medicine_id),
                            "dosage": "1",
                            "frequency": "od",
                            "quantity": quantity,
                        }
                    ],
                }
            ),
        )
        return rx.id


class TestConcurrentDispensing:
    async def test_simultaneous_dispenses_never_oversell_a_batch(
        self, db_engine: AsyncEngine, world: _World
    ) -> None:
        # §14 and §16: more pharmacists than units, all at once.
        contenders = 25
        prescriptions = [
            await _committed_prescription(db_engine, world, 1) for _ in range(contenders)
        ]

        async def attempt(prescription_id: uuid.UUID) -> bool:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                try:
                    await _service(session).dispense(
                        world.hospital_id, prescription_id, actor_id=world.pharmacist_id
                    )
                except ConflictError:
                    await session.rollback()
                    return False
                return True

        outcomes = await asyncio.gather(*(attempt(pid) for pid in prescriptions))

        # Exactly as many succeed as there were units; the rest are told no.
        assert outcomes.count(True) == STOCK
        assert outcomes.count(False) == contenders - STOCK
        async with AsyncSession(db_engine) as check:
            on_hand = await check.scalar(
                select(MedicineBatch.quantity_on_hand).where(MedicineBatch.id == world.batch_id)
            )
            ledger = await check.scalar(
                select(func.sum(StockMovement.quantity_change)).where(
                    StockMovement.batch_id == world.batch_id
                )
            )
            dispensed = await check.scalar(
                select(func.count())
                .select_from(Prescription)
                .where(
                    Prescription.hospital_id == world.hospital_id,
                    Prescription.status == PrescriptionStatus.DISPENSED,
                )
            )
        assert on_hand == 0
        assert ledger == 0  # +10 received, ten × −1 dispensed: the ledger agrees
        assert dispensed == STOCK

    async def test_a_receipt_and_dispenses_of_one_batch_do_not_overwrite_each_other(
        self, db_engine: AsyncEngine, world: _World
    ) -> None:
        # Receiving into an existing batch takes no lock of its own. Stock
        # must still add up when it races the dispenses that do.
        prescriptions = [await _committed_prescription(db_engine, world, 1) for _ in range(6)]

        async def dispense(prescription_id: uuid.UUID) -> None:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                await _service(session).dispense(
                    world.hospital_id, prescription_id, actor_id=world.pharmacist_id
                )

        async def receive(quantity: int) -> None:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                repository = MedicineRepository(session)
                batch = await repository.get_batch_by_id(world.hospital_id, world.batch_id)
                assert batch is not None
                # Hold the stale read across the dispenses' commits.
                await asyncio.sleep(0.2)
                await repository.apply_movement(
                    batch,
                    quantity_change=quantity,
                    reason=StockMovementReason.RECEIVED,
                    moved_at=datetime.now(UTC),
                )
                await session.commit()

        await asyncio.gather(receive(50), receive(7), *(dispense(pid) for pid in prescriptions))

        async with AsyncSession(db_engine) as check:
            batch = await check.get(MedicineBatch, world.batch_id)
            assert batch is not None
            ledger = await check.scalar(
                select(func.sum(StockMovement.quantity_change)).where(
                    StockMovement.batch_id == world.batch_id
                )
            )
        assert batch.quantity_on_hand == ledger == STOCK + 50 + 7 - 6
        assert batch.initial_quantity == STOCK + 50 + 7

    async def test_one_prescription_dispensed_twice_at_once_goes_out_once(
        self, db_engine: AsyncEngine, world: _World
    ) -> None:
        prescription_id = await _committed_prescription(db_engine, world, 4)

        async def attempt() -> str:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                try:
                    await _service(session).dispense(
                        world.hospital_id, prescription_id, actor_id=world.pharmacist_id
                    )
                except Exception as exc:  # noqa: BLE001 — the test asserts on which
                    await session.rollback()
                    return type(exc).__name__
                return "ok"

        outcomes = sorted(await asyncio.gather(attempt(), attempt(), attempt()))

        assert outcomes == ["PrescriptionStateError", "PrescriptionStateError", "ok"]
        async with AsyncSession(db_engine) as check:
            on_hand = await check.scalar(
                select(MedicineBatch.quantity_on_hand).where(MedicineBatch.id == world.batch_id)
            )
        assert on_hand == STOCK - 4


# ── Seed ────────────────────────────────────────────────────────────────────


class TestSeededPharmacy:
    async def test_the_shelf_demonstrates_every_dispensing_rule(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        repository = MedicineRepository(db_session)
        rows = await db_session.execute(select(Medicine).where(Medicine.hospital_id == hospital.id))
        medicines = {m.sku: m for m in rows.unique().scalars().all()}

        assert set(medicines) == {sku for sku, *_ in MEDICINES}
        assert any(not m.is_active for m in medicines.values())
        for medicine in medicines.values():
            # Every seeded medicine would be accepted by the API's own validation.
            CreateMedicineRequest.model_validate(
                {
                    "sku": medicine.sku,
                    "name": medicine.name,
                    "generic_name": medicine.generic_name,
                    "strength": medicine.strength,
                    "form": medicine.form,
                    "atc_code": medicine.atc_code,
                    "unit_price": str(medicine.unit_price),
                }
            )

        batches = (
            (
                await db_session.execute(
                    select(MedicineBatch).where(MedicineBatch.hospital_id == hospital.id)
                )
            )
            .unique()
            .scalars()
            .all()
        )
        assert len(batches) == len(BATCHES)
        # Each batch's count equals its ledger.
        for batch in batches:
            ledger = await repository.list_movements(hospital.id, batch.id)
            assert sum(m.quantity_change for m in ledger) == batch.quantity_on_hand
        # The seed anchors "today" to the last working day, so judge the shelf
        # by the earliest day that could have been.
        earliest = TODAY - timedelta(days=3)
        totals = await repository.dispensable_totals(
            hospital.id, [m.id for m in medicines.values()], on=earliest
        )
        assert totals[medicines["PARA-500"].id] == 530  # two batches, one expiring soon
        assert totals[medicines["AMOX-500"].id] == 200  # the expired forty do not count
        assert totals[medicines["ATOR-10"].id] == 8  # enough to show a shortage
        assert medicines["PANT-40"].id not in totals  # recalled
        assert medicines["INSG-100"].id not in totals  # never stocked
        vendors = await db_session.execute(select(Vendor).where(Vendor.hospital_id == hospital.id))
        assert [v.name for v in vendors.scalars().all()] == [DEMO_VENDOR[0]]

    async def test_a_second_run_adds_nothing_and_does_not_restock(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        await seed_demo_data(db_session, hospital, {})
        repository = MedicineRepository(db_session)
        para = await repository.get_medicine_by_sku(hospital.id, "PARA-500")
        assert para is not None
        batch = await repository.get_batch_by_number(hospital.id, para.id, "PA-2502")
        assert batch is not None
        # A demo dispensed some since the first run.
        batch.quantity_on_hand -= 100
        await db_session.flush()

        again = await seed_demo_pharmacy(db_session, hospital, today=TODAY)

        assert again == {"medicines": 0, "batches": 0, "vendors": 0}
        await db_session.refresh(batch)
        assert batch.quantity_on_hand == 400
