# ruff: noqa: F811 — the fixtures imported from the booking tests are requested by name
"""API and adversarial tests for the patient's own records (§15, §16, §27.8).

``docs/modules/15-patient-app.md``. Over HTTP, through the real application
and a real PostgreSQL. Rows are planted where the test needs a state only
staff can produce: a released lab order, a pending one, a cancelled
prescription (the ``ruff: noqa: F811`` above covers the fixtures imported
from the booking tests, registered by import).

Reading is not audited, so nothing here writes audit rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.models.lab import LabOrder, LabOrderItem, LabOrderStatus, LabResultFlag, LabResultType
from app.models.pharmacy import Prescription, PrescriptionItem, PrescriptionStatus
from app.tests.api.test_patient_booking_api import (  # noqa: F401 — fixtures, registered by import
    _error,
    _Patient,
    _patient_test_settings,
    application,
    browser,
    doctor,
    hospital,
    patient,
    sms,
    stranger,
    tag,
)
from app.tests.patient_app_helpers import (
    PATIENT,
    insert_appointment,
    insert_patient_record,
    new_phone,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.doctor import Doctor
    from app.models.hospital import Hospital

pytestmark = pytest.mark.database

MYP = f"{PATIENT}/prescriptions"
MYLAB = f"{PATIENT}/lab-results"
HOSP = f"{PATIENT}/hospitals"
NOT_FOUND = (404, "RESOURCE_NOT_FOUND", "Not Found")

FIELDS = {"ref", "status", "prescribed_at", "timezone", "hospital", "doctor", "items"}
ITEM_FIELDS = {
    "medicine_name",
    "dosage",
    "frequency",
    "duration_days",
    "instructions",
    "quantity",
    "quantity_dispensed",
}
LAB_FIELDS = {"ref", "status", "released_at", "timezone", "hospital", "doctor", "items"}
LAB_ITEM_FIELDS = {
    "test_name",
    "value",
    "unit",
    "flag",
    "reference_low",
    "reference_high",
    "released_at",
    "amended_on",
}

LEAKS = ("DOCTORSNOTE", "available_quantity", "batch_id", "invoice_id", "cost_price", "TECHNOTE")


_SLOTS: int = 0


def _ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _next_clock(days_ago: float) -> tuple[datetime, datetime]:
    """A one-hour appointment window that never collides with the last one seeded.

    The database refuses two appointments for the doctor that overlap, so a
    test that seeds several prescriptions walks forward an hour at a time.
    """
    global _SLOTS
    _SLOTS += 1
    start = _ago(-days_ago) + timedelta(hours=3 * _SLOTS)
    return start, start + timedelta(minutes=45)


async def _other_record(db_session: AsyncSession, db_hospital: Hospital) -> uuid.UUID:
    """A second record at one hospital, that the fixtures' patient is not."""
    record = await insert_patient_record(
        db_session, db_hospital.id, phone=new_phone(), first_name="OTHERCANARY"
    )
    return record.id


async def _seed_prescription(
    db_session: AsyncSession,
    db_hospital: Hospital,
    db_doctor: Doctor,
    for_patient: uuid.UUID | None,
    *,
    status: PrescriptionStatus = PrescriptionStatus.ACTIVE,
    dispensed: int = 0,
    days_ago: float = 2,
) -> Prescription:
    """Plant one prescription, with the appointment it belongs to (FK)."""
    assert for_patient is not None, "the patient fixture has not linked a record"
    start, end = _next_clock(days_ago)
    appointment = await insert_appointment(
        db_session, db_doctor, for_patient, starts_at=start, ends_at=end
    )
    prescription = Prescription(
        hospital_id=db_hospital.id,
        patient_id=for_patient,
        doctor_id=db_doctor.id,
        appointment_id=appointment.id,
        prescribed_at=_ago(days_ago),
        status=status,
        notes="DOCTORSNOTE internal",
        items=[
            PrescriptionItem(
                hospital_id=db_hospital.id,
                medicine_name="Paracetamol",
                dosage="1 tablet",
                frequency="twice daily",
                duration_days=5,
                instructions="after food",
                quantity=10,
                quantity_dispensed=dispensed,
            )
        ],
    )
    db_session.add(prescription)
    await db_session.flush()
    await db_session.commit()
    return prescription


async def _test_id(db_session: AsyncSession, db_hospital: Hospital) -> uuid.UUID:
    """One catalog test (Haemoglobin) per hospital, created once."""
    from app.models.lab import LabTest

    result = await db_session.execute(
        select(LabTest).where(LabTest.hospital_id == db_hospital.id, LabTest.code == "HB")
    )
    test = result.scalar_one_or_none()
    if test is None:
        test = LabTest(
            hospital_id=db_hospital.id,
            code="HB",
            name="Haemoglobin",
            category="Haematology",
            unit="g/dL",
            result_type=LabResultType.NUMERIC,
            price=Decimal("100.00"),
        )
        db_session.add(test)
        await db_session.flush()
        await db_session.commit()
    return test.id


async def _seed_lab_order(
    db_session: AsyncSession,
    db_hospital: Hospital,
    db_doctor: Doctor,
    for_patient: uuid.UUID | None,
    *,
    status: LabOrderStatus = LabOrderStatus.RELEASED,
    days_ago: float = 1,
) -> LabOrder:
    """Plant one lab order in a chosen lifecycle state (§16)."""
    assert for_patient is not None, "the record to seed against is missing"
    released = status is LabOrderStatus.RELEASED
    order = LabOrder(
        hospital_id=db_hospital.id,
        patient_id=for_patient,
        doctor_id=db_doctor.id,
        ordered_at=_ago(days_ago + 1),
        status=status,
        notes="TECHNOTE internal",
        cancel_reason="cancelled at the counter" if status is LabOrderStatus.CANCELLED else None,
        cancelled_at=_ago(days_ago) if status is LabOrderStatus.CANCELLED else None,
        released_at=_ago(days_ago) if released else None,
        items=[
            LabOrderItem(
                hospital_id=db_hospital.id,
                test_id=await _test_id(db_session, db_hospital),
                position=0,
                test_code="HB",
                test_name="Haemoglobin",
                result_type=LabResultType.NUMERIC,
                price=Decimal("100.00"),
                result_value="13.5" if released else None,
                result_unit="g/dL" if released else None,
                result_flag=LabResultFlag.NORMAL if released else None,
                reference_low=Decimal("13.0") if released else None,
                reference_high=Decimal("17.0") if released else None,
                released_at=_ago(days_ago) if released else None,
            )
        ],
    )
    db_session.add(order)
    await db_session.flush()
    await db_session.commit()
    return order


async def _list(who: _Patient, url: str, **params: Any) -> Any:
    response = await who.client.get(url, params=params, headers=who.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _detail(who: _Patient, record: str, record_ref: object, hospital_ref: str) -> Any:
    return await who.client.get(f"{HOSP}/{hospital_ref}/{record}/{record_ref}", headers=who.headers)


class TestPrescriptionList:
    async def test_the_page_is_the_patient_safe_view_newest_first(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        older = await _seed_prescription(
            db_session, hospital, doctor, patient.record_id, days_ago=4
        )
        newer = await _seed_prescription(
            db_session, hospital, doctor, patient.record_id, days_ago=1
        )

        page = await _list(patient, MYP)

        assert [item["ref"] for item in page["data"]] == [str(newer.id), str(older.id)]
        row = page["data"][0]
        assert set(row) == FIELDS
        assert row["status"] == "active"
        assert row["timezone"] == "Asia/Kolkata"
        assert row["hospital"] == {"ref": hospital.slug, "name": hospital.name}
        assert row["doctor"]["specialization"] == "Cardiology"
        (item,) = row["items"]
        assert set(item) == ITEM_FIELDS
        assert item["quantity_dispensed"] == 0
        assert not any(leak in str(page) for leak in LEAKS)
        assert page["metadata"]["pagination"]["total_records"] == 2

    async def test_every_status_is_shown_labelled(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        await _seed_prescription(
            db_session, hospital, doctor, patient.record_id, status=PrescriptionStatus.CANCELLED
        )
        await _seed_prescription(
            db_session,
            hospital,
            doctor,
            patient.record_id,
            status=PrescriptionStatus.DISPENSED,
            dispensed=10,
        )

        page = await _list(patient, MYP)

        assert {item["status"] for item in page["data"]} == {"cancelled", "dispensed"}

    async def test_a_status_filter_selects_one_status(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        await _seed_prescription(
            db_session, hospital, doctor, patient.record_id, status=PrescriptionStatus.ACTIVE
        )
        await _seed_prescription(
            db_session, hospital, doctor, patient.record_id, status=PrescriptionStatus.CANCELLED
        )

        page = await _list(patient, MYP, status="cancelled")

        assert page["metadata"]["pagination"]["total_records"] == 1
        assert page["data"][0]["status"] == "cancelled"

    async def test_an_account_without_links_sees_an_empty_page(self, stranger: _Patient) -> None:
        page = await _list(stranger, MYP)

        assert page["data"] == []
        assert page["metadata"]["pagination"]["total_records"] == 0

    async def test_records_of_no_other_hospital_are_listed(
        self, patient: _Patient, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        from app.tests.patient_app_helpers import insert_hospital

        elsewhere = await insert_hospital(db_session, name="Elsewhere General")
        other = await _other_record(db_session, elsewhere)
        await _seed_prescription(db_session, elsewhere, doctor, other)

        page = await _list(patient, MYP)

        assert page["data"] == []


class TestPrescriptionDetail:
    async def test_one_own_prescription(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        prescription = await _seed_prescription(
            db_session,
            hospital,
            doctor,
            patient.record_id,
            status=PrescriptionStatus.PARTIALLY_DISPENSED,
            dispensed=4,
        )

        response = await _detail(patient, "prescriptions", prescription.id, hospital.slug)

        assert response.status_code == 200, response.text
        row = response.json()["data"]
        assert row["ref"] == str(prescription.id)
        assert row["status"] == "partially_dispensed"
        assert row["items"][0]["quantity_dispensed"] == 4
        assert not any(leak in str(row) for leak in LEAKS)

    async def test_a_malformed_reference_is_one_not_found(
        self, patient: _Patient, hospital: Hospital
    ) -> None:
        response = await _detail(patient, "prescriptions", "%20nope", hospital.slug)

        assert _error(response) == NOT_FOUND

    async def test_an_unknown_reference_is_not_found(
        self, patient: _Patient, hospital: Hospital
    ) -> None:
        response = await _detail(patient, "prescriptions", uuid.uuid4(), hospital.slug)

        assert _error(response) == NOT_FOUND

    async def test_somebody_elses_prescription_is_the_same_not_found(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        other = await _other_record(db_session, hospital)
        theirs = await _seed_prescription(db_session, hospital, doctor, other)

        response = await _detail(patient, "prescriptions", theirs.id, hospital.slug)

        assert _error(response) == NOT_FOUND


class TestLabResultList:
    async def test_only_released_orders_exist_for_the_patient(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        released = await _seed_lab_order(db_session, hospital, doctor, patient.record_id)
        await _seed_lab_order(
            db_session, hospital, doctor, patient.record_id, status=LabOrderStatus.ORDERED
        )
        await _seed_lab_order(
            db_session, hospital, doctor, patient.record_id, status=LabOrderStatus.CANCELLED
        )

        page = await _list(patient, MYLAB)

        assert [item["ref"] for item in page["data"]] == [str(released.id)]
        row = page["data"][0]
        assert set(row) == LAB_FIELDS
        assert row["status"] == "released"
        assert row["timezone"] == "Asia/Kolkata"
        (item,) = row["items"]
        assert set(item) == LAB_ITEM_FIELDS
        assert item["value"] == "13.5"
        assert item["flag"] == "normal"
        assert item["amended_on"] is None
        assert not any(leak in str(page) for leak in LEAKS)

    async def test_an_account_without_links_sees_an_empty_page(self, stranger: _Patient) -> None:
        page = await _list(stranger, MYLAB)

        assert page["data"] == []

    async def test_results_are_newest_first(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        older = await _seed_lab_order(db_session, hospital, doctor, patient.record_id, days_ago=5)
        newer = await _seed_lab_order(db_session, hospital, doctor, patient.record_id, days_ago=1)

        page = await _list(patient, MYLAB)

        assert [item["ref"] for item in page["data"]] == [str(newer.id), str(older.id)]


class TestLabResultDetail:
    async def test_one_released_order(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        order = await _seed_lab_order(db_session, hospital, doctor, patient.record_id)

        response = await _detail(patient, "lab-results", order.id, hospital.slug)

        assert response.status_code == 200, response.text
        row = response.json()["data"]
        assert row["ref"] == str(order.id)
        (item,) = row["items"]
        assert item["test_name"] == "Haemoglobin"
        assert Decimal(item["reference_low"]) == Decimal("13.0")
        assert not any(leak in str(row) for leak in LEAKS)

    async def test_an_unreleased_order_is_the_same_not_found(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        pending = await _seed_lab_order(
            db_session, hospital, doctor, patient.record_id, status=LabOrderStatus.ORDERED
        )

        response = await _detail(patient, "lab-results", pending.id, hospital.slug)

        assert _error(response) == NOT_FOUND

    async def test_a_cancelled_order_is_the_same_not_found(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        cancelled = await _seed_lab_order(
            db_session, hospital, doctor, patient.record_id, status=LabOrderStatus.CANCELLED
        )

        response = await _detail(patient, "lab-results", cancelled.id, hospital.slug)

        assert _error(response) == NOT_FOUND

    async def test_somebody_elses_released_order_is_the_same_not_found(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        other = await _other_record(db_session, hospital)
        theirs = await _seed_lab_order(db_session, hospital, doctor, other)

        response = await _detail(patient, "lab-results", theirs.id, hospital.slug)

        assert _error(response) == NOT_FOUND
