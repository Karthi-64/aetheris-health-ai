"""Read-only aggregates for Reports and Dashboards.

Data access only: no business rules, no HTTP exceptions, no commits. Every
method runs one SQL aggregate and returns plain values — frozen dataclasses,
``Decimal`` and ``int`` — never ORM rows (``docs/modules/10-reports-dashboard.md``).

**Tenancy is written into every statement.** Each method takes ``hospital_id``
first and filters every table the statement names on it, joined tables
included (CLAUDE.md rules 4 and 5). This class does not inherit
:class:`~app.repositories.base.BaseRepository`: that class is bound to one
model and adds a soft-delete filter but no tenant filter, and a report reads
six tables.

**Time.** Range filters compare a ``TIMESTAMPTZ`` column with two UTC instants
the service built from hospital-local dates (``col >= start AND col < end``);
no function is applied to the column in ``WHERE``. Bucketing converts the
column to the hospital's zone in ``SELECT`` and ``GROUP BY`` only. The bucket
expression is built once per statement and that same object is selected and
grouped: building it twice would bind its parameters twice, and PostgreSQL
could no longer see that the two expressions are the same.

**Soft delete.** The fact table of each figure is filtered on
``deleted_at IS NULL``. Rows joined in only for a name (a doctor, a
department, a patient on an invoice) are not, so history does not vanish when
someone is deactivated.

**Money.** Sums are taken on ``NUMERIC(15,2)`` columns and returned as
``Decimal`` quantized to two places. Invoices, payments and refunds are never
joined to each other inside one sum, which would multiply rows.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for dataclass field resolution
from dataclasses import dataclass
from datetime import date, datetime  # noqa: TC003 — needed at runtime, as above
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, Label, Select, case, cast, func, literal, select

from app.models.appointment import Appointment, AppointmentStatus, AppointmentType
from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentMethod, Refund
from app.models.department import Department
from app.models.doctor import Doctor
from app.models.patient import Gender, Patient
from app.models.user import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlalchemy.sql import ColumnElement

__all__ = [
    "AGEING_BUCKETS",
    "AppointmentCountsRow",
    "AtRiskRow",
    "DepartmentCountsRow",
    "DoctorCountsRow",
    "MoneyTotal",
    "NamedRefRow",
    "OutstandingInvoiceRow",
    "OutstandingSummaryRow",
    "ReportRepository",
    "ScheduleRow",
    "WalkInRow",
]

_CENT = Decimal("0.01")

#: The only units a bucket may be cut in. Checked before the value reaches SQL.
_GRANULARITIES = frozenset({"day", "week", "month"})

#: Invoice statuses that count as billed: issued and not voided. A ``refunded``
#: invoice stays in — it was billed, and refunds are reported on their own.
_BILLED_STATUSES = (
    InvoiceStatus.ISSUED,
    InvoiceStatus.PARTIALLY_PAID,
    InvoiceStatus.PAID,
    InvoiceStatus.REFUNDED,
)

#: Invoice statuses that still take money. Outstanding is decided by status,
#: never by ``total - amount_paid > 0``: a part-paid invoice refunded in full is
#: ``refunded`` with ``amount_paid < total`` and is owed nothing.
_UNPAID_STATUSES = (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID)

#: Ageing bands as ``(key, first day, last day)``; ``None`` is open-ended.
AGEING_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0_30", 0, 30),
    ("31_60", 31, 60),
    ("61_90", 61, 90),
    ("over_90", 91, None),
)


@dataclass(frozen=True, slots=True)
class AppointmentCountsRow:
    """Appointments by status."""

    total: int = 0
    booked: int = 0
    checked_in: int = 0
    in_progress: int = 0
    completed: int = 0
    cancelled: int = 0
    no_show: int = 0


@dataclass(frozen=True, slots=True)
class DoctorCountsRow:
    """One doctor's appointments, with the doctor's current department."""

    doctor_id: uuid.UUID
    doctor_name: str
    department_id: uuid.UUID | None
    department_name: str | None
    total: int
    completed: int
    cancelled: int
    no_show: int


@dataclass(frozen=True, slots=True)
class DepartmentCountsRow:
    """One department's appointments. ``None`` ids mean "no department"."""

    department_id: uuid.UUID | None
    department_name: str | None
    total: int
    completed: int
    cancelled: int
    no_show: int


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    """One appointment on a doctor's day."""

    appointment_id: uuid.UUID
    scheduled_start: datetime
    scheduled_end: datetime
    status: AppointmentStatus
    type: AppointmentType
    patient_id: uuid.UUID
    patient_name: str
    patient_mrn: str
    checked_in_at: datetime | None


@dataclass(frozen=True, slots=True)
class WalkInRow:
    """Walk-ins of a day by where they are, and the earliest waiting check-in."""

    waiting: int = 0
    not_arrived: int = 0
    in_consultation: int = 0
    earliest_checked_in_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AtRiskRow:
    """A booked appointment whose start has passed."""

    appointment_id: uuid.UUID
    scheduled_start: datetime
    patient_id: uuid.UUID
    patient_name: str
    patient_mrn: str
    doctor_id: uuid.UUID
    doctor_name: str


@dataclass(frozen=True, slots=True)
class MoneyTotal:
    """How many rows, and what they add up to."""

    count: int = 0
    amount: Decimal = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class OutstandingSummaryRow:
    """Unpaid invoices and what is owed on them."""

    invoice_count: int = 0
    outstanding_amount: Decimal = Decimal("0.00")
    issued_count: int = 0
    partially_paid_count: int = 0


@dataclass(frozen=True, slots=True)
class OutstandingInvoiceRow:
    """One unpaid invoice with the patient it was raised for."""

    invoice_id: uuid.UUID
    invoice_number: str
    issued_at: datetime
    issued_date: date
    patient_id: uuid.UUID
    patient_name: str
    patient_mrn: str
    status: InvoiceStatus
    total: Decimal
    amount_paid: Decimal
    balance_due: Decimal


@dataclass(frozen=True, slots=True)
class NamedRefRow:
    """An id with the name to show for it."""

    id: uuid.UUID
    name: str


def _money(value: Any) -> Decimal:
    """Return a summed ``NUMERIC`` as a ``Decimal`` with exactly two places."""
    return Decimal(value).quantize(_CENT)


def _bucket(granularity: str, tz: str, column: InstrumentedAttribute[Any]) -> Label[Any]:
    """Build the bucket expression of a statement: the local day, week or month.

    Call it **once** per statement and use the returned object in both
    ``select()`` and ``group_by()``.

    :param granularity: ``day``, ``week`` or ``month``; validated by the caller.
    :param tz: IANA zone name of the hospital.
    :param column: The ``TIMESTAMPTZ`` column to bucket.
    :returns: A labelled ``date_trunc(g, column AT TIME ZONE tz)::date``.
    :raises ValueError: If the granularity is not one of the three units.
    """
    if granularity not in _GRANULARITIES:
        msg = f"Unknown granularity: {granularity!r}"
        raise ValueError(msg)
    return cast(func.date_trunc(granularity, func.timezone(tz, column)), Date).label("bucket")


def _status_counts() -> list[ColumnElement[int]]:
    """Return ``count(*)`` and one filtered count per appointment status."""
    return [
        func.count(),
        *(func.count().filter(Appointment.status == status) for status in AppointmentStatus),
    ]


def _outcome_counts() -> list[ColumnElement[int]]:
    """Return the completed, cancelled and no-show counts, in that order."""
    return [
        func.count().filter(Appointment.status == AppointmentStatus.COMPLETED),
        func.count().filter(Appointment.status == AppointmentStatus.CANCELLED),
        func.count().filter(Appointment.status == AppointmentStatus.NO_SHOW),
    ]


def _counts_row(values: Any) -> AppointmentCountsRow:
    """Build an :class:`AppointmentCountsRow` from the columns of :func:`_status_counts`."""
    total, booked, checked_in, in_progress, completed, cancelled, no_show = values
    return AppointmentCountsRow(
        total=int(total),
        booked=int(booked),
        checked_in=int(checked_in),
        in_progress=int(in_progress),
        completed=int(completed),
        cancelled=int(cancelled),
        no_show=int(no_show),
    )


def _full_name(first: InstrumentedAttribute[str], last: InstrumentedAttribute[str]) -> Any:
    """Return ``first || ' ' || last`` as a SQL expression."""
    return func.concat(first, " ", last)


class ReportRepository:
    """SQL aggregates behind every report and dashboard tile.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Patients ──────────────────────────────────────────────────────────────

    async def patient_registrations(
        self,
        hospital_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        tz: str,
        granularity: str,
    ) -> dict[date, int]:
        """Count active patients registered in a range, per local bucket.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound (UTC).
        :param end: Exclusive upper bound (UTC).
        :param tz: IANA zone the buckets are cut in.
        :param granularity: ``day``, ``week`` or ``month``.
        :returns: Bucket start date to count. Buckets with none are absent.
        """
        bucket = _bucket(granularity, tz, Patient.created_at)
        stmt = (
            select(bucket, func.count())
            .where(
                Patient.hospital_id == hospital_id,
                Patient.deleted_at.is_(None),
                Patient.created_at >= start,
                Patient.created_at < end,
            )
            .group_by(bucket)
        )
        result = await self._session.execute(stmt)
        return {day: int(count) for day, count in result.all()}

    async def patient_registrations_by_gender(
        self, hospital_id: uuid.UUID, *, start: datetime, end: datetime
    ) -> dict[str, int]:
        """Count active patients registered in a range, per gender.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound (UTC).
        :param end: Exclusive upper bound (UTC).
        :returns: Every gender value to its count, zero included.
        """
        stmt = (
            select(Patient.gender, func.count())
            .where(
                Patient.hospital_id == hospital_id,
                Patient.deleted_at.is_(None),
                Patient.created_at >= start,
                Patient.created_at < end,
            )
            .group_by(Patient.gender)
        )
        result = await self._session.execute(stmt)
        counts = {gender.value: 0 for gender in Gender}
        for gender, count in result.all():
            counts[Gender(gender).value] = int(count)
        return counts

    async def count_active_patients(self, hospital_id: uuid.UUID) -> int:
        """Count the hospital's active patients as of now.

        :param hospital_id: The tenant to scope to.
        :returns: Patients that are not deactivated.
        """
        stmt = (
            select(func.count())
            .select_from(Patient)
            .where(Patient.hospital_id == hospital_id, Patient.deleted_at.is_(None))
        )
        return int((await self._session.execute(stmt)).scalar_one())

    # ── Appointments ──────────────────────────────────────────────────────────

    @staticmethod
    def _appointments_in(
        stmt: Select[Any],
        hospital_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        doctor_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
        doctors_joined: bool = False,
    ) -> Select[Any]:
        """Narrow a statement over ``appointments`` to one hospital and range.

        The range is anchored on ``scheduled_start``. A department filter goes
        through the doctor's current department; the join to ``doctors`` is
        added here unless the statement already has it.

        :param stmt: A ``SELECT`` whose ``FROM`` is ``appointments``.
        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound (UTC).
        :param end: Exclusive upper bound (UTC).
        :param doctor_id: Only this doctor's appointments.
        :param department_id: Only appointments of doctors in this department.
        :param doctors_joined: The statement already joins ``doctors``.
        :returns: The narrowed statement.
        """
        stmt = stmt.where(
            Appointment.hospital_id == hospital_id,
            Appointment.deleted_at.is_(None),
            Appointment.scheduled_start >= start,
            Appointment.scheduled_start < end,
        )
        if doctor_id is not None:
            stmt = stmt.where(Appointment.doctor_id == doctor_id)
        if department_id is not None:
            if not doctors_joined:
                stmt = stmt.join(
                    Doctor,
                    (Doctor.id == Appointment.doctor_id) & (Doctor.hospital_id == hospital_id),
                )
            stmt = stmt.where(Doctor.department_id == department_id)
        return stmt

    async def appointment_counts(
        self,
        hospital_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        doctor_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
    ) -> AppointmentCountsRow:
        """Count appointments scheduled in a range, by status.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``scheduled_start`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :param doctor_id: Only this doctor's appointments.
        :param department_id: Only appointments of doctors in this department.
        :returns: The counts; all zero when there are none.
        """
        stmt = self._appointments_in(
            select(*_status_counts()).select_from(Appointment),
            hospital_id,
            start=start,
            end=end,
            doctor_id=doctor_id,
            department_id=department_id,
        )
        return _counts_row((await self._session.execute(stmt)).one())

    async def appointment_counts_by_bucket(
        self,
        hospital_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        tz: str,
        granularity: str,
        doctor_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
    ) -> dict[date, AppointmentCountsRow]:
        """Count appointments scheduled in a range, by status, per local bucket.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``scheduled_start`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :param tz: IANA zone the buckets are cut in.
        :param granularity: ``day``, ``week`` or ``month``.
        :param doctor_id: Only this doctor's appointments.
        :param department_id: Only appointments of doctors in this department.
        :returns: Bucket start date to counts. Buckets with none are absent.
        """
        bucket = _bucket(granularity, tz, Appointment.scheduled_start)
        stmt = self._appointments_in(
            select(bucket, *_status_counts()).select_from(Appointment),
            hospital_id,
            start=start,
            end=end,
            doctor_id=doctor_id,
            department_id=department_id,
        ).group_by(bucket)
        result = await self._session.execute(stmt)
        return {row[0]: _counts_row(row[1:]) for row in result.all()}

    async def appointment_counts_by_doctor(
        self,
        hospital_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        doctor_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
    ) -> list[DoctorCountsRow]:
        """Count appointments scheduled in a range, per doctor.

        Deactivated doctors are included: the visits happened.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``scheduled_start`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :param doctor_id: Only this doctor.
        :param department_id: Only doctors in this department.
        :returns: Doctors with at least one appointment, busiest first, then by
            name, then by id.
        """
        doctor_name = _full_name(User.first_name, User.last_name).label("doctor_name")
        total = func.count().label("total")
        stmt = (
            select(
                Doctor.id, doctor_name, Department.id, Department.name, *_outcome_counts(), total
            )
            .select_from(Appointment)
            .join(
                Doctor,
                (Doctor.id == Appointment.doctor_id) & (Doctor.hospital_id == hospital_id),
            )
            .join(User, (User.id == Doctor.user_id) & (User.hospital_id == hospital_id))
            .outerjoin(
                Department,
                (Department.id == Doctor.department_id) & (Department.hospital_id == hospital_id),
            )
        )
        stmt = self._appointments_in(
            stmt,
            hospital_id,
            start=start,
            end=end,
            doctor_id=doctor_id,
            department_id=department_id,
            doctors_joined=True,
        )
        stmt = stmt.group_by(
            Doctor.id, User.first_name, User.last_name, Department.id, Department.name
        ).order_by(total.desc(), doctor_name.asc(), Doctor.id.asc())
        result = await self._session.execute(stmt)
        return [
            DoctorCountsRow(
                doctor_id=row[0],
                doctor_name=str(row[1]),
                department_id=row[2],
                department_name=row[3],
                completed=int(row[4]),
                cancelled=int(row[5]),
                no_show=int(row[6]),
                total=int(row[7]),
            )
            for row in result.all()
        ]

    async def appointment_counts_by_department(
        self,
        hospital_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        doctor_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
    ) -> list[DepartmentCountsRow]:
        """Count appointments scheduled in a range, per department.

        The department is the doctor's current one. Doctors with none are
        gathered into one row whose ids are ``None``.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``scheduled_start`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :param doctor_id: Only this doctor.
        :param department_id: Only this department.
        :returns: Departments with at least one appointment, busiest first,
            then by name with "no department" last.
        """
        total = func.count().label("total")
        stmt = (
            select(Department.id, Department.name, *_outcome_counts(), total)
            .select_from(Appointment)
            .join(
                Doctor,
                (Doctor.id == Appointment.doctor_id) & (Doctor.hospital_id == hospital_id),
            )
            .outerjoin(
                Department,
                (Department.id == Doctor.department_id) & (Department.hospital_id == hospital_id),
            )
        )
        stmt = self._appointments_in(
            stmt,
            hospital_id,
            start=start,
            end=end,
            doctor_id=doctor_id,
            department_id=department_id,
            doctors_joined=True,
        )
        stmt = stmt.group_by(Department.id, Department.name).order_by(
            total.desc(), Department.name.asc().nulls_last(), Department.id.asc()
        )
        result = await self._session.execute(stmt)
        return [
            DepartmentCountsRow(
                department_id=row[0],
                department_name=row[1],
                completed=int(row[2]),
                cancelled=int(row[3]),
                no_show=int(row[4]),
                total=int(row[5]),
            )
            for row in result.all()
        ]

    async def doctor_schedule(
        self,
        hospital_id: uuid.UUID,
        *,
        doctor_id: uuid.UUID,
        start: datetime,
        end: datetime,
    ) -> list[ScheduleRow]:
        """List one doctor's appointments in a range, in schedule order.

        The patient is joined for a name only, so an appointment of a
        deactivated patient is still listed.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor whose schedule to read.
        :param start: Inclusive lower bound on ``scheduled_start`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :returns: The appointments, earliest first.
        """
        stmt = (
            select(
                Appointment.id,
                Appointment.scheduled_start,
                Appointment.scheduled_end,
                Appointment.status,
                Appointment.type,
                Patient.id,
                _full_name(Patient.first_name, Patient.last_name),
                Patient.mrn,
                Appointment.checked_in_at,
            )
            .select_from(Appointment)
            .join(
                Patient,
                (Patient.id == Appointment.patient_id) & (Patient.hospital_id == hospital_id),
            )
        )
        stmt = self._appointments_in(
            stmt, hospital_id, start=start, end=end, doctor_id=doctor_id
        ).order_by(Appointment.scheduled_start, Appointment.id)
        result = await self._session.execute(stmt)
        return [
            ScheduleRow(
                appointment_id=row[0],
                scheduled_start=row[1],
                scheduled_end=row[2],
                status=AppointmentStatus(row[3]),
                type=AppointmentType(row[4]),
                patient_id=row[5],
                patient_name=str(row[6]),
                patient_mrn=str(row[7]),
                checked_in_at=row[8],
            )
            for row in result.all()
        ]

    async def count_doctor_patients(self, hospital_id: uuid.UUID, *, doctor_id: uuid.UUID) -> int:
        """Count the distinct active patients a doctor has seen or is due to see.

        All time. A patient whose only appointments were cancelled or missed is
        not counted, and neither is a deactivated patient.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor.
        :returns: The number of distinct patients.
        """
        stmt = (
            select(func.count(Appointment.patient_id.distinct()))
            .select_from(Appointment)
            .join(
                Patient,
                (Patient.id == Appointment.patient_id) & (Patient.hospital_id == hospital_id),
            )
            .where(
                Appointment.hospital_id == hospital_id,
                Appointment.doctor_id == doctor_id,
                Appointment.deleted_at.is_(None),
                Patient.deleted_at.is_(None),
                Appointment.status.not_in([AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW]),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def walk_in_queue(
        self, hospital_id: uuid.UUID, *, start: datetime, end: datetime
    ) -> WalkInRow:
        """Count the walk-ins scheduled in a range by where they are.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``scheduled_start`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :returns: Waiting (checked in), not arrived (booked) and in
            consultation (in progress) counts, and the earliest check-in among
            those waiting.
        """
        waiting = Appointment.status == AppointmentStatus.CHECKED_IN
        stmt = self._appointments_in(
            select(
                func.count().filter(waiting),
                func.count().filter(Appointment.status == AppointmentStatus.BOOKED),
                func.count().filter(Appointment.status == AppointmentStatus.IN_PROGRESS),
                func.min(Appointment.checked_in_at).filter(
                    waiting, Appointment.checked_in_at.is_not(None)
                ),
            ).select_from(Appointment),
            hospital_id,
            start=start,
            end=end,
        ).where(Appointment.type == AppointmentType.WALK_IN)
        row = (await self._session.execute(stmt)).one()
        return WalkInRow(
            waiting=int(row[0]),
            not_arrived=int(row[1]),
            in_consultation=int(row[2]),
            earliest_checked_in_at=row[3],
        )

    async def count_at_risk(
        self, hospital_id: uuid.UUID, *, start: datetime, end: datetime, now: datetime
    ) -> int:
        """Count booked appointments in a range whose start time has passed.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``scheduled_start`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :param now: The instant to judge "passed" against.
        :returns: The number of such appointments.
        """
        stmt = self._appointments_in(
            select(func.count()).select_from(Appointment), hospital_id, start=start, end=end
        ).where(
            Appointment.status == AppointmentStatus.BOOKED,
            Appointment.scheduled_start < now,
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def at_risk_appointments(
        self,
        hospital_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        now: datetime,
        limit: int,
    ) -> list[AtRiskRow]:
        """List booked appointments in a range whose start time has passed.

        Patient, doctor and the doctor's user are joined for names only.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``scheduled_start`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :param now: The instant to judge "passed" against.
        :param limit: The most rows to return.
        :returns: The appointments, longest overdue first.
        """
        stmt = (
            select(
                Appointment.id,
                Appointment.scheduled_start,
                Patient.id,
                _full_name(Patient.first_name, Patient.last_name),
                Patient.mrn,
                Doctor.id,
                _full_name(User.first_name, User.last_name),
            )
            .select_from(Appointment)
            .join(
                Patient,
                (Patient.id == Appointment.patient_id) & (Patient.hospital_id == hospital_id),
            )
            .join(
                Doctor,
                (Doctor.id == Appointment.doctor_id) & (Doctor.hospital_id == hospital_id),
            )
            .join(User, (User.id == Doctor.user_id) & (User.hospital_id == hospital_id))
        )
        stmt = (
            self._appointments_in(stmt, hospital_id, start=start, end=end)
            .where(
                Appointment.status == AppointmentStatus.BOOKED,
                Appointment.scheduled_start < now,
            )
            .order_by(Appointment.scheduled_start, Appointment.id)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [
            AtRiskRow(
                appointment_id=row[0],
                scheduled_start=row[1],
                patient_id=row[2],
                patient_name=str(row[3]),
                patient_mrn=str(row[4]),
                doctor_id=row[5],
                doctor_name=str(row[6]),
            )
            for row in result.all()
        ]

    # ── Revenue ───────────────────────────────────────────────────────────────

    @staticmethod
    def _billed(
        stmt: Select[Any], hospital_id: uuid.UUID, *, start: datetime, end: datetime
    ) -> Select[Any]:
        """Narrow a statement over ``invoices`` to those billed in a range."""
        return stmt.where(
            Invoice.hospital_id == hospital_id,
            Invoice.deleted_at.is_(None),
            Invoice.status.in_(_BILLED_STATUSES),
            Invoice.issued_at >= start,
            Invoice.issued_at < end,
        )

    @staticmethod
    def _collected(
        stmt: Select[Any], hospital_id: uuid.UUID, *, start: datetime, end: datetime
    ) -> Select[Any]:
        """Narrow a statement over ``payments`` to money received in a range."""
        return stmt.join(
            Invoice,
            (Invoice.id == Payment.invoice_id) & (Invoice.hospital_id == hospital_id),
        ).where(
            Payment.hospital_id == hospital_id,
            Payment.deleted_at.is_(None),
            Invoice.deleted_at.is_(None),
            Payment.received_at >= start,
            Payment.received_at < end,
        )

    @staticmethod
    def _given_back(
        stmt: Select[Any], hospital_id: uuid.UUID, *, start: datetime, end: datetime
    ) -> Select[Any]:
        """Narrow a statement over ``refunds`` to money returned in a range."""
        return stmt.join(
            Invoice,
            (Invoice.id == Refund.invoice_id) & (Invoice.hospital_id == hospital_id),
        ).where(
            Refund.hospital_id == hospital_id,
            Refund.deleted_at.is_(None),
            Invoice.deleted_at.is_(None),
            Refund.refunded_at >= start,
            Refund.refunded_at < end,
        )

    async def _money_by_bucket(self, stmt: Select[Any]) -> dict[date, MoneyTotal]:
        """Run a ``(bucket, count, sum)`` statement and key the rows by bucket."""
        result = await self._session.execute(stmt)
        return {
            day: MoneyTotal(count=int(count), amount=_money(amount))
            for day, count, amount in result.all()
        }

    async def _money_by_method(self, stmt: Select[Any]) -> dict[str, MoneyTotal]:
        """Run a ``(method, count, sum)`` statement; every method is present."""
        result = await self._session.execute(stmt)
        totals = {method.value: MoneyTotal() for method in PaymentMethod}
        for method, count, amount in result.all():
            totals[PaymentMethod(method).value] = MoneyTotal(
                count=int(count), amount=_money(amount)
            )
        return totals

    async def invoiced_totals(
        self, hospital_id: uuid.UUID, *, start: datetime, end: datetime
    ) -> MoneyTotal:
        """Total the invoices issued in a range.

        Drafts have no issue time and drop out; void invoices are excluded;
        refunded invoices stay in.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``issued_at`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :returns: How many invoices, and the sum of their totals.
        """
        stmt = self._billed(
            select(func.count(), func.coalesce(func.sum(Invoice.total), 0)).select_from(Invoice),
            hospital_id,
            start=start,
            end=end,
        )
        count, amount = (await self._session.execute(stmt)).one()
        return MoneyTotal(count=int(count), amount=_money(amount))

    async def invoiced_by_bucket(
        self,
        hospital_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        tz: str,
        granularity: str,
    ) -> dict[date, MoneyTotal]:
        """Total the invoices issued in a range, per local bucket of the issue time.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``issued_at`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :param tz: IANA zone the buckets are cut in.
        :param granularity: ``day``, ``week`` or ``month``.
        :returns: Bucket start date to totals. Buckets with none are absent.
        """
        bucket = _bucket(granularity, tz, Invoice.issued_at)
        stmt = self._billed(
            select(bucket, func.count(), func.coalesce(func.sum(Invoice.total), 0)).select_from(
                Invoice
            ),
            hospital_id,
            start=start,
            end=end,
        ).group_by(bucket)
        return await self._money_by_bucket(stmt)

    async def payment_totals_by_method(
        self, hospital_id: uuid.UUID, *, start: datetime, end: datetime
    ) -> dict[str, MoneyTotal]:
        """Total the payments received in a range, per payment method.

        Dated by when the money was received, not by the invoice's issue.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``received_at`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :returns: Every payment method to its totals, zero included.
        """
        stmt = self._collected(
            select(
                Payment.method, func.count(), func.coalesce(func.sum(Payment.amount), 0)
            ).select_from(Payment),
            hospital_id,
            start=start,
            end=end,
        ).group_by(Payment.method)
        return await self._money_by_method(stmt)

    async def payments_by_bucket(
        self,
        hospital_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        tz: str,
        granularity: str,
    ) -> dict[date, MoneyTotal]:
        """Total the payments received in a range, per local bucket.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``received_at`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :param tz: IANA zone the buckets are cut in.
        :param granularity: ``day``, ``week`` or ``month``.
        :returns: Bucket start date to totals. Buckets with none are absent.
        """
        bucket = _bucket(granularity, tz, Payment.received_at)
        stmt = self._collected(
            select(bucket, func.count(), func.coalesce(func.sum(Payment.amount), 0)).select_from(
                Payment
            ),
            hospital_id,
            start=start,
            end=end,
        ).group_by(bucket)
        return await self._money_by_bucket(stmt)

    async def refund_totals_by_method(
        self, hospital_id: uuid.UUID, *, start: datetime, end: datetime
    ) -> dict[str, MoneyTotal]:
        """Total the refunds given in a range, per payment method.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``refunded_at`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :returns: Every payment method to its totals, zero included.
        """
        stmt = self._given_back(
            select(
                Refund.method, func.count(), func.coalesce(func.sum(Refund.amount), 0)
            ).select_from(Refund),
            hospital_id,
            start=start,
            end=end,
        ).group_by(Refund.method)
        return await self._money_by_method(stmt)

    async def refunds_by_bucket(
        self,
        hospital_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        tz: str,
        granularity: str,
    ) -> dict[date, MoneyTotal]:
        """Total the refunds given in a range, per local bucket.

        :param hospital_id: The tenant to scope to.
        :param start: Inclusive lower bound on ``refunded_at`` (UTC).
        :param end: Exclusive upper bound (UTC).
        :param tz: IANA zone the buckets are cut in.
        :param granularity: ``day``, ``week`` or ``month``.
        :returns: Bucket start date to totals. Buckets with none are absent.
        """
        bucket = _bucket(granularity, tz, Refund.refunded_at)
        stmt = self._given_back(
            select(bucket, func.count(), func.coalesce(func.sum(Refund.amount), 0)).select_from(
                Refund
            ),
            hospital_id,
            start=start,
            end=end,
        ).group_by(bucket)
        return await self._money_by_bucket(stmt)

    # ── Outstanding and discounts ─────────────────────────────────────────────

    @staticmethod
    def _unpaid(stmt: Select[Any], hospital_id: uuid.UUID) -> Select[Any]:
        """Narrow a statement over ``invoices`` to those still owed money."""
        return stmt.where(
            Invoice.hospital_id == hospital_id,
            Invoice.deleted_at.is_(None),
            Invoice.status.in_(_UNPAID_STATUSES),
        )

    async def outstanding_summary(self, hospital_id: uuid.UUID) -> OutstandingSummaryRow:
        """Total what is owed on the hospital's unpaid invoices, as of now.

        :param hospital_id: The tenant to scope to.
        :returns: The invoice count, the balance owed, and the count per status.
        """
        stmt = self._unpaid(
            select(
                func.count(),
                func.coalesce(func.sum(Invoice.total - Invoice.amount_paid), 0),
                func.count().filter(Invoice.status == InvoiceStatus.ISSUED),
                func.count().filter(Invoice.status == InvoiceStatus.PARTIALLY_PAID),
            ).select_from(Invoice),
            hospital_id,
        )
        row = (await self._session.execute(stmt)).one()
        return OutstandingSummaryRow(
            invoice_count=int(row[0]),
            outstanding_amount=_money(row[1]),
            issued_count=int(row[2]),
            partially_paid_count=int(row[3]),
        )

    async def outstanding_ageing(
        self, hospital_id: uuid.UUID, *, today: date, tz: str
    ) -> dict[str, MoneyTotal]:
        """Total what is owed on unpaid invoices, per age band.

        Age is whole days from the issue date in the hospital's zone to
        ``today``. Bands are those of :data:`AGEING_BUCKETS`.

        :param hospital_id: The tenant to scope to.
        :param today: Today in the hospital's timezone.
        :param tz: IANA zone the issue date is read in.
        :returns: Every band key to its totals, zero included.
        """
        age = literal(today, Date) - cast(func.timezone(tz, Invoice.issued_at), Date)
        band = case(
            *((age <= last, key) for key, _, last in AGEING_BUCKETS if last is not None),
            else_=AGEING_BUCKETS[-1][0],
        ).label("band")
        stmt = self._unpaid(
            select(
                band,
                func.count(),
                func.coalesce(func.sum(Invoice.total - Invoice.amount_paid), 0),
            ).select_from(Invoice),
            hospital_id,
        ).group_by(band)
        result = await self._session.execute(stmt)
        totals = {key: MoneyTotal() for key, _, _ in AGEING_BUCKETS}
        for key, count, amount in result.all():
            totals[str(key)] = MoneyTotal(count=int(count), amount=_money(amount))
        return totals

    async def outstanding_invoices(
        self, hospital_id: uuid.UUID, *, tz: str, limit: int
    ) -> list[OutstandingInvoiceRow]:
        """List the hospital's unpaid invoices, oldest first.

        The patient is joined for a name only, so an invoice of a deactivated
        patient is still listed.

        :param hospital_id: The tenant to scope to.
        :param tz: IANA zone the issue date is read in.
        :param limit: The most rows to return.
        :returns: The invoices, earliest issued first.
        """
        stmt = (
            self._unpaid(
                select(
                    Invoice.id,
                    Invoice.invoice_number,
                    Invoice.issued_at,
                    cast(func.timezone(tz, Invoice.issued_at), Date),
                    Patient.id,
                    _full_name(Patient.first_name, Patient.last_name),
                    Patient.mrn,
                    Invoice.status,
                    Invoice.total,
                    Invoice.amount_paid,
                    Invoice.total - Invoice.amount_paid,
                )
                .select_from(Invoice)
                .join(
                    Patient,
                    (Patient.id == Invoice.patient_id) & (Patient.hospital_id == hospital_id),
                ),
                hospital_id,
            )
            .order_by(Invoice.issued_at.asc(), Invoice.id.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [
            OutstandingInvoiceRow(
                invoice_id=row[0],
                invoice_number=str(row[1] or ""),
                issued_at=row[2],
                issued_date=row[3],
                patient_id=row[4],
                patient_name=str(row[5]),
                patient_mrn=str(row[6]),
                status=InvoiceStatus(row[7]),
                total=_money(row[8]),
                amount_paid=_money(row[9]),
                balance_due=_money(row[10]),
            )
            for row in result.all()
        ]

    async def pending_discounts(self, hospital_id: uuid.UUID) -> MoneyTotal:
        """Total the discounts waiting for an admin's approval.

        No status filter, so the figure equals the list
        ``GET /invoices?discount_pending=true`` returns.

        :param hospital_id: The tenant to scope to.
        :returns: How many invoices, and the sum of their discounts.
        """
        stmt = (
            select(func.count(), func.coalesce(func.sum(Invoice.discount_amount), 0))
            .select_from(Invoice)
            .where(
                Invoice.hospital_id == hospital_id,
                Invoice.deleted_at.is_(None),
                Invoice.discount_pending_approval.is_(True),
            )
        )
        count, amount = (await self._session.execute(stmt)).one()
        return MoneyTotal(count=int(count), amount=_money(amount))

    # ── Filter lookups ────────────────────────────────────────────────────────

    async def doctor_ref(self, hospital_id: uuid.UUID, doctor_id: uuid.UUID) -> NamedRefRow | None:
        """Return a doctor's id and display name, deactivated doctors included.

        :param hospital_id: The tenant to scope to.
        :param doctor_id: The doctor to look up.
        :returns: The reference, or ``None`` if absent or in another hospital.
        """
        stmt = (
            select(Doctor.id, _full_name(User.first_name, User.last_name))
            .select_from(Doctor)
            .join(User, (User.id == Doctor.user_id) & (User.hospital_id == hospital_id))
            .where(Doctor.hospital_id == hospital_id, Doctor.id == doctor_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return None if row is None else NamedRefRow(id=row[0], name=str(row[1]))

    async def department_ref(
        self, hospital_id: uuid.UUID, department_id: uuid.UUID
    ) -> NamedRefRow | None:
        """Return a department's id and name, deactivated departments included.

        :param hospital_id: The tenant to scope to.
        :param department_id: The department to look up.
        :returns: The reference, or ``None`` if absent or in another hospital.
        """
        stmt = select(Department.id, Department.name).where(
            Department.hospital_id == hospital_id, Department.id == department_id
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return None if row is None else NamedRefRow(id=row[0], name=str(row[1]))
