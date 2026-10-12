"""Database row helpers shared by the report repository and API tests.

A report is only as right as the fixture it is checked against, so every
helper here takes the **event timestamp explicitly** — when the patient was
registered, the appointment scheduled, the invoice issued, the money received.
Nothing reads the wall clock, which is what lets a test put a row one second
either side of local midnight and pass at any time of day.

Rows are inserted directly rather than through the owning services: a report
test should not fail because billing validation changed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.models.appointment import Appointment, AppointmentStatus, AppointmentType
from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentMethod, Refund
from app.models.department import Department
from app.models.doctor import Doctor
from app.models.patient import Gender, Patient
from app.models.user import User
from app.tests.billing_helpers import insert_user

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "REGISTERED_LONG_AGO",
    "Clinic",
    "insert_department",
    "insert_named_doctor",
    "insert_report_appointment",
    "insert_report_invoice",
    "insert_report_patient",
    "insert_report_payment",
    "insert_report_refund",
    "seed_billing",
    "seed_clinic",
    "seed_other_billing",
    "seed_other_clinic",
    "seed_reception_desk",
    "seed_registrations",
    "utc",
]


def utc(text: str) -> datetime:
    """Parse ``YYYY-MM-DDTHH:MM[:SS]`` as a UTC instant.

    :param text: The timestamp, without an offset.
    :returns: A timezone-aware UTC datetime.
    """
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


async def insert_department(session: AsyncSession, hospital_id: uuid.UUID, name: str) -> Department:
    """Insert a department.

    :param session: The test session.
    :param hospital_id: Hospital the department belongs to.
    :param name: Department name; unique per hospital.
    :returns: The persisted department.
    """
    department = Department(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        code=f"D{uuid.uuid4().hex[:8].upper()}",
        name=name,
    )
    session.add(department)
    await session.flush()
    return department


async def insert_named_doctor(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    first_name: str,
    last_name: str,
    department_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    deleted: bool = False,
) -> Doctor:
    """Insert a doctor whose display name and department are known.

    :param session: The test session.
    :param hospital_id: Hospital the doctor belongs to.
    :param first_name: Given name of the user behind the doctor.
    :param last_name: Family name of the user behind the doctor.
    :param department_id: The doctor's department, or ``None`` for none.
    :param user_id: Attach the profile to this existing user (their name is
        left as it is). A fresh user is created when omitted.
    :param deleted: Insert the profile deactivated.
    :returns: The persisted doctor.
    """
    if user_id is None:
        user = User(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            email=f"report-{uuid.uuid4().hex[:12]}@hospital.test",
            password_hash="test-placeholder-not-a-hash",
            first_name=first_name,
            last_name=last_name,
        )
        session.add(user)
        await session.flush()
        user_id = user.id
    doctor = Doctor(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        user_id=user_id,
        department_id=department_id,
        specialization="General Medicine",
        license_number=f"LIC-{uuid.uuid4().hex[:8]}",
        consultation_fee=Decimal("500.00"),
        deleted_at=utc("2026-01-01T00:00") if deleted else None,
    )
    session.add(doctor)
    await session.flush()
    return doctor


async def insert_report_patient(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    created_at: datetime,
    gender: Gender = Gender.FEMALE,
    first_name: str = "Ananya",
    last_name: str = "Rao",
    deleted: bool = False,
) -> Patient:
    """Insert a patient registered at a known instant.

    :param session: The test session.
    :param hospital_id: Hospital the patient belongs to.
    :param created_at: When the patient was registered (UTC).
    :param gender: Recorded gender.
    :param first_name: Given name.
    :param last_name: Family name.
    :param deleted: Insert the patient deactivated.
    :returns: The persisted patient.
    """
    patient = Patient(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        mrn=f"MRN-{uuid.uuid4().hex[:8]}",
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date(1990, 1, 1),
        gender=gender,
        created_at=created_at,
        deleted_at=created_at + timedelta(hours=1) if deleted else None,
    )
    session.add(patient)
    await session.flush()
    return patient


async def insert_report_appointment(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    patient_id: uuid.UUID,
    doctor_id: uuid.UUID,
    start: datetime,
    status: AppointmentStatus = AppointmentStatus.BOOKED,
    type: AppointmentType = AppointmentType.NEW,  # noqa: A002 — the column's name
    checked_in_at: datetime | None = None,
    deleted: bool = False,
) -> Appointment:
    """Insert a ten-minute appointment starting at a known instant.

    One doctor cannot hold two live appointments that overlap (a database
    exclusion constraint), so give each of a doctor's appointments its own start.

    :param session: The test session.
    :param hospital_id: Hospital the appointment belongs to.
    :param patient_id: Patient being seen.
    :param doctor_id: Doctor seeing them.
    :param start: Scheduled start (UTC).
    :param status: Lifecycle status to insert in.
    :param type: Appointment type.
    :param checked_in_at: When the patient arrived, if they did.
    :param deleted: Insert the appointment soft-deleted.
    :returns: The persisted appointment.
    """
    appointment = Appointment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=10),
        status=status,
        type=type,
        checked_in_at=checked_in_at,
        deleted_at=start if deleted else None,
    )
    session.add(appointment)
    await session.flush()
    return appointment


async def insert_report_invoice(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    patient_id: uuid.UUID,
    status: InvoiceStatus,
    total: str,
    issued_at: datetime | None = None,
    amount_paid: str = "0.00",
    amount_refunded: str = "0.00",
    discount_amount: str = "0.00",
    discount_pending: bool = False,
    deleted: bool = False,
) -> Invoice:
    """Insert an invoice in a given state, issued at a known instant.

    The subtotal is set to ``total + discount_amount`` with no tax, which
    satisfies the table's check constraints for any consistent pair.

    :param session: The test session.
    :param hospital_id: Hospital the invoice belongs to.
    :param patient_id: Patient being billed.
    :param status: Lifecycle status. A draft gets no invoice number.
    :param total: Invoice total, as a decimal string.
    :param issued_at: When the invoice was issued (UTC). ``None`` for a draft.
    :param amount_paid: Sum of payments recorded on the invoice.
    :param amount_refunded: Sum of refunds given back.
    :param discount_amount: Invoice-level discount.
    :param discount_pending: The discount awaits an admin's approval.
    :param deleted: Insert the invoice soft-deleted.
    :returns: The persisted invoice.
    """
    is_draft = status == InvoiceStatus.DRAFT
    invoice = Invoice(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        patient_id=patient_id,
        invoice_number=None if is_draft else f"INV-T-{uuid.uuid4().hex[:12]}",
        subtotal=Decimal(total) + Decimal(discount_amount),
        tax_amount=Decimal("0.00"),
        discount_amount=Decimal(discount_amount),
        discount_pending_approval=discount_pending,
        total=Decimal(total),
        amount_paid=Decimal(amount_paid),
        amount_refunded=Decimal(amount_refunded),
        status=status,
        issued_at=issued_at,
        deleted_at=utc("2026-10-05T12:00") if deleted else None,
    )
    session.add(invoice)
    await session.flush()
    return invoice


async def insert_report_payment(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    invoice_id: uuid.UUID,
    amount: str,
    method: PaymentMethod,
    received_at: datetime,
    received_by: uuid.UUID,
    deleted: bool = False,
) -> Payment:
    """Insert a payment received at a known instant.

    :param session: The test session.
    :param hospital_id: Hospital the payment belongs to.
    :param invoice_id: Invoice it was made against.
    :param amount: Amount received, as a decimal string.
    :param method: How it was paid.
    :param received_at: When the money was received (UTC).
    :param received_by: The user who recorded it; must exist.
    :param deleted: Insert the payment soft-deleted.
    :returns: The persisted payment.
    """
    payment = Payment(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        invoice_id=invoice_id,
        amount=Decimal(amount),
        method=method,
        received_by=received_by,
        received_at=received_at,
        idempotency_key=uuid.uuid4().hex,
        deleted_at=received_at if deleted else None,
    )
    session.add(payment)
    await session.flush()
    return payment


async def insert_report_refund(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    *,
    invoice_id: uuid.UUID,
    amount: str,
    method: PaymentMethod,
    refunded_at: datetime,
    refunded_by: uuid.UUID,
) -> Refund:
    """Insert a refund given at a known instant.

    :param session: The test session.
    :param hospital_id: Hospital the refund belongs to.
    :param invoice_id: Invoice it was made against.
    :param amount: Amount given back, as a decimal string.
    :param method: How it was returned.
    :param refunded_at: When the money was returned (UTC).
    :param refunded_by: The user who issued it; must exist.
    :returns: The persisted refund.
    """
    refund = Refund(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        invoice_id=invoice_id,
        amount=Decimal(amount),
        method=method,
        reason="Test refund",
        refunded_by=refunded_by,
        refunded_at=refunded_at,
        idempotency_key=uuid.uuid4().hex,
    )
    session.add(refund)
    await session.flush()
    return refund


# ── Hand-computed fixtures ───────────────────────────────────────────────────
# Shared by the repository and API suites so both check the same world. The
# test hospitals are in Asia/Kolkata (UTC+5:30): local midnight is 18:30 UTC.

#: When the fixtures' supporting patients were registered — far from any range
#: a test reports on, so they never show up as new registrations.
REGISTERED_LONG_AGO = datetime(2026, 1, 10, 6, 0, tzinfo=UTC)


async def seed_billing(session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Build fixture F in a hospital and return its invoice ids by name.

    Billed 1-6 Oct (local): I1 1100.00 + I3 750.00 + I4 1100.00 + I7 600.00 +
    I8 800.00 + I9 0.10 = **4350.10** over **6** invoices. Not billed: I2
    (void), I5 and I6 (drafts), I10 (soft-deleted).

    Collected 1-6 Oct: 600.00 + 300.00 + 0.03 + 0.03 + 0.04 + 600.00 + 800.00
    = **2300.10** over **7** payments. Not collected: 500.00 received at
    23:59:59 on 30 Sep, the soft-deleted 50.00, and I10's 999.00.

    Refunded 1-6 Oct: 600.00 + 350.00 = **950.00** over **2** refunds.
    """
    cashier = (await insert_user(session, hospital_id)).id
    patient = (await insert_report_patient(session, hospital_id, created_at=REGISTERED_LONG_AGO)).id

    async def invoice(
        status: InvoiceStatus, total: str, issued: str | None, **kw: Any
    ) -> uuid.UUID:
        row = await insert_report_invoice(
            session,
            hospital_id,
            patient_id=patient,
            status=status,
            total=total,
            issued_at=utc(issued) if issued else None,
            **kw,
        )
        return row.id

    async def payment(invoice_id: uuid.UUID, amount: str, method: PaymentMethod, at: str) -> None:
        await insert_report_payment(
            session,
            hospital_id,
            invoice_id=invoice_id,
            amount=amount,
            method=method,
            received_at=utc(at),
            received_by=cashier,
        )

    paid, part, issued, draft = (
        InvoiceStatus.PAID,
        InvoiceStatus.PARTIALLY_PAID,
        InvoiceStatus.ISSUED,
        InvoiceStatus.DRAFT,
    )
    ids = {
        # 00:30 on 1 Oct local — 30 Sep in UTC.
        "I1": await invoice(paid, "1100.00", "2026-09-30T19:00", amount_paid="1100.00"),
        "I2": await invoice(InvoiceStatus.VOID, "500.00", "2026-10-05T05:00"),
        "I3": await invoice(part, "750.00", "2026-10-05T05:10", amount_paid="300.00"),
        "I4": await invoice(issued, "1100.00", "2026-10-05T06:40"),
        "I5": await invoice(draft, "400.00", None),
        "I6": await invoice(draft, "700.00", None, discount_amount="300.00", discount_pending=True),
        "I7": await invoice(
            InvoiceStatus.REFUNDED,
            "600.00",
            "2026-10-06T05:00",
            amount_paid="600.00",
            amount_refunded="600.00",
        ),
        "I8": await invoice(
            paid, "800.00", "2026-10-06T07:00", amount_paid="800.00", amount_refunded="350.00"
        ),
        "I9": await invoice(paid, "0.10", "2026-10-05T07:00", amount_paid="0.10"),
        "I10": await invoice(
            paid, "999.00", "2026-10-05T08:00", amount_paid="999.00", deleted=True
        ),
    }

    cash, card, upi = PaymentMethod.CASH, PaymentMethod.CARD, PaymentMethod.UPI
    # One second before, and exactly at, local midnight between 30 Sep and 1 Oct.
    await payment(ids["I1"], "500.00", cash, "2026-09-30T18:29:59")
    await payment(ids["I1"], "600.00", upi, "2026-09-30T18:30:00")
    await payment(ids["I3"], "300.00", card, "2026-10-05T06:00")
    await payment(ids["I9"], "0.03", cash, "2026-10-05T07:05")
    await payment(ids["I9"], "0.03", upi, "2026-10-05T07:06")
    await payment(ids["I9"], "0.04", PaymentMethod.BANK_TRANSFER, "2026-10-05T07:07")
    await payment(ids["I7"], "600.00", upi, "2026-10-06T05:30")
    await payment(ids["I8"], "800.00", card, "2026-10-06T07:10")
    await payment(ids["I10"], "999.00", cash, "2026-10-05T08:10")
    await insert_report_payment(
        session,
        hospital_id,
        invoice_id=ids["I4"],
        amount="50.00",
        method=cash,
        received_at=utc("2026-10-05T09:00"),
        received_by=cashier,
        deleted=True,
    )

    for name, amount, method, at in (
        ("I7", "600.00", upi, "2026-10-06T08:00"),
        ("I8", "350.00", card, "2026-10-06T09:00"),
    ):
        await insert_report_refund(
            session,
            hospital_id,
            invoice_id=ids[name],
            amount=amount,
            method=method,
            refunded_at=utc(at),
            refunded_by=cashier,
        )
    return ids


async def seed_other_billing(session: AsyncSession, hospital_id: uuid.UUID) -> None:
    """Build a small, different, non-zero billing set in another hospital.

    Billed 1-6 Oct: 77.77 + 11.11 = 88.88 over 2. Collected 11.11 cash.
    Refunded 5.55 cash. Outstanding 77.77 on 1 issued invoice. One pending
    discount of 9.99.
    """
    cashier = (await insert_user(session, hospital_id)).id
    patient = (await insert_report_patient(session, hospital_id, created_at=REGISTERED_LONG_AGO)).id
    await insert_report_invoice(
        session,
        hospital_id,
        patient_id=patient,
        status=InvoiceStatus.ISSUED,
        total="77.77",
        issued_at=utc("2026-10-05T06:00"),
    )
    settled = await insert_report_invoice(
        session,
        hospital_id,
        patient_id=patient,
        status=InvoiceStatus.PAID,
        total="11.11",
        issued_at=utc("2026-10-05T06:05"),
        amount_paid="11.11",
        amount_refunded="5.55",
    )
    await insert_report_invoice(
        session,
        hospital_id,
        patient_id=patient,
        status=InvoiceStatus.DRAFT,
        total="20.00",
        discount_amount="9.99",
        discount_pending=True,
    )
    await insert_report_payment(
        session,
        hospital_id,
        invoice_id=settled.id,
        amount="11.11",
        method=PaymentMethod.CASH,
        received_at=utc("2026-10-05T06:10"),
        received_by=cashier,
    )
    await insert_report_refund(
        session,
        hospital_id,
        invoice_id=settled.id,
        amount="5.55",
        method=PaymentMethod.CASH,
        refunded_at=utc("2026-10-06T06:10"),
        refunded_by=cashier,
    )


@dataclass
class Clinic:
    """The ids of the appointment fixture."""

    cardiology: uuid.UUID
    orthopaedics: uuid.UUID
    priya: uuid.UUID
    arjun: uuid.UUID
    vikram: uuid.UUID
    retired: uuid.UUID
    first: uuid.UUID
    second: uuid.UUID
    deactivated: uuid.UUID


async def seed_clinic(session: AsyncSession, hospital_id: uuid.UUID) -> Clinic:
    """Build the appointment fixture in a hospital.

    Local 5 Oct: Priya completed x2 and cancelled x1; Arjun no-show x1.
    Local 6 Oct: Arjun booked (at 00:15 local, which is 5 Oct in UTC); Vikram
    (no department) checked-in, for a deactivated patient; a deactivated
    Cardiology doctor in-progress. Also on 6 Oct: one soft-deleted appointment.
    Local Sunday 4 Oct: Arjun completed x1.

    So 5-6 Oct holds **7**: booked 1, checked-in 1, in-progress 1, completed 2,
    cancelled 1, no-show 1.
    """
    cardiology = (await insert_department(session, hospital_id, "Cardiology")).id
    orthopaedics = (await insert_department(session, hospital_id, "Orthopaedics")).id
    priya = await insert_named_doctor(
        session, hospital_id, first_name="Priya", last_name="Sharma", department_id=cardiology
    )
    arjun = await insert_named_doctor(
        session, hospital_id, first_name="Arjun", last_name="Nair", department_id=orthopaedics
    )
    vikram = await insert_named_doctor(session, hospital_id, first_name="Vikram", last_name="Desai")
    retired = await insert_named_doctor(
        session,
        hospital_id,
        first_name="Old",
        last_name="Doc",
        department_id=cardiology,
        deleted=True,
    )
    first = await insert_report_patient(
        session, hospital_id, created_at=REGISTERED_LONG_AGO, first_name="First"
    )
    second = await insert_report_patient(
        session, hospital_id, created_at=REGISTERED_LONG_AGO, first_name="Second"
    )
    deactivated = await insert_report_patient(
        session, hospital_id, created_at=REGISTERED_LONG_AGO, first_name="Left", deleted=True
    )

    status = AppointmentStatus
    plan: list[tuple[uuid.UUID, uuid.UUID, str, AppointmentStatus, bool]] = [
        (priya.id, first.id, "2026-10-05T04:00", status.COMPLETED, False),
        (priya.id, second.id, "2026-10-05T04:30", status.COMPLETED, False),
        (priya.id, first.id, "2026-10-05T05:00", status.CANCELLED, False),
        (arjun.id, second.id, "2026-10-05T04:00", status.NO_SHOW, False),
        (arjun.id, first.id, "2026-10-05T18:45", status.BOOKED, False),
        (vikram.id, deactivated.id, "2026-10-06T04:00", status.CHECKED_IN, False),
        (retired.id, first.id, "2026-10-06T05:00", status.IN_PROGRESS, False),
        (priya.id, first.id, "2026-10-06T06:00", status.BOOKED, True),
        (arjun.id, first.id, "2026-10-04T10:00", status.COMPLETED, False),
    ]
    for doctor_id, patient_id, start, state, deleted in plan:
        await insert_report_appointment(
            session,
            hospital_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            start=utc(start),
            status=state,
            deleted=deleted,
        )
    return Clinic(
        cardiology=cardiology,
        orthopaedics=orthopaedics,
        priya=priya.id,
        arjun=arjun.id,
        vikram=vikram.id,
        retired=retired.id,
        first=first.id,
        second=second.id,
        deactivated=deactivated.id,
    )


async def seed_other_clinic(session: AsyncSession, hospital_id: uuid.UUID) -> Clinic:
    """Build a different appointment set in another hospital: three on 5 Oct."""
    department = (await insert_department(session, hospital_id, "Cardiology")).id
    doctor = await insert_named_doctor(
        session, hospital_id, first_name="Other", last_name="Doctor", department_id=department
    )
    patient = await insert_report_patient(
        session, hospital_id, created_at=utc("2026-10-05T10:00"), gender=Gender.MALE
    )
    for start, state in (
        ("2026-10-05T04:00", AppointmentStatus.COMPLETED),
        ("2026-10-05T04:30", AppointmentStatus.NO_SHOW),
        ("2026-10-05T05:00", AppointmentStatus.NO_SHOW),
    ):
        await insert_report_appointment(
            session,
            hospital_id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            start=utc(start),
            status=state,
        )
    return Clinic(
        cardiology=department,
        orthopaedics=department,
        priya=doctor.id,
        arjun=doctor.id,
        vikram=doctor.id,
        retired=doctor.id,
        first=patient.id,
        second=patient.id,
        deactivated=patient.id,
    )


async def seed_registrations(session: AsyncSession, hospital_id: uuid.UUID) -> None:
    """Register four patients around local midnights.

    Active: a man at 00:15 on 6 Oct local (18:45 UTC on the 5th), a woman on
    5 Oct, and someone at 23:59:59 on 30 Sep local. Deactivated: a woman on 5 Oct.
    """
    await insert_report_patient(
        session, hospital_id, created_at=utc("2026-10-05T18:45"), gender=Gender.MALE
    )
    await insert_report_patient(
        session, hospital_id, created_at=utc("2026-10-05T10:00"), gender=Gender.FEMALE
    )
    await insert_report_patient(
        session,
        hospital_id,
        created_at=utc("2026-10-05T11:00"),
        gender=Gender.FEMALE,
        deleted=True,
    )
    await insert_report_patient(
        session, hospital_id, created_at=utc("2026-09-30T18:29:59"), gender=Gender.OTHER
    )


async def seed_reception_desk(session: AsyncSession, hospital_id: uuid.UUID) -> uuid.UUID:
    """Seed a reception day on local 6 Oct and return the doctor's id."""
    doctor = await insert_named_doctor(session, hospital_id, first_name="Arjun", last_name="Nair")
    patient = await insert_report_patient(
        session, hospital_id, created_at=REGISTERED_LONG_AGO, first_name="Sunita"
    )
    walk_in, status = AppointmentType.WALK_IN, AppointmentStatus
    plan: list[tuple[str, AppointmentStatus, AppointmentType, str | None]] = [
        # Today's walk-ins.
        ("2026-10-06T03:00", status.CHECKED_IN, walk_in, "2026-10-06T03:20"),
        ("2026-10-06T03:10", status.CHECKED_IN, walk_in, "2026-10-06T03:05"),
        ("2026-10-06T03:20", status.CHECKED_IN, walk_in, None),
        ("2026-10-06T03:30", status.BOOKED, walk_in, None),
        ("2026-10-06T03:40", status.IN_PROGRESS, walk_in, "2026-10-06T02:00"),
        ("2026-10-06T03:50", status.COMPLETED, walk_in, "2026-10-06T01:00"),
        # Not a walk-in.
        ("2026-10-06T04:00", status.CHECKED_IN, AppointmentType.NEW, "2026-10-06T01:30"),
        # A walk-in yesterday, still marked waiting.
        ("2026-10-05T04:00", status.CHECKED_IN, walk_in, "2026-10-05T03:00"),
    ]
    for start, state, kind, arrived in plan:
        await insert_report_appointment(
            session,
            hospital_id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            start=utc(start),
            status=state,
            type=kind,
            checked_in_at=utc(arrived) if arrived else None,
        )
    return doctor.id
