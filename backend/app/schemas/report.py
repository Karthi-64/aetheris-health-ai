"""Pydantic DTOs for Reports and Dashboards.

Response models only: every route in this module is a ``GET``. They are the
only report shapes that cross the API boundary
(``docs/modules/10-reports-dashboard.md``).

**Money goes out as a decimal string** (``"1850.00"``, ``"-350.00"``), never a
JSON number, so no client parses it through a float (CLAUDE.md rule 6).
**Counts are integers.** **Dates** (``from``, ``to``, ``bucket_start``,
``today``) are calendar dates in the hospital's timezone; **datetimes** are UTC.

Every figure a screen shows is a field here. Derived values such as
``in_clinic`` or ``net_collected_amount`` are computed by the service so that
no client ever adds or subtracts.
"""

from __future__ import annotations

# NOTE: runtime imports, not TYPE_CHECKING — Pydantic resolves field
# annotations against the module's real globals (backend/CLAUDE.md).
from datetime import date, datetime  # noqa: TC003
from decimal import Decimal  # noqa: TC003
from typing import Literal
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, Field

from app.models.appointment import AppointmentStatus, AppointmentType  # noqa: TC001
from app.models.billing import PaymentMethod  # noqa: TC001

__all__ = [
    "AdminDashboard",
    "AgeingBucket",
    "AppointmentBucket",
    "AppointmentCounts",
    "AppointmentsByDepartment",
    "AppointmentsByDoctor",
    "AppointmentsFilters",
    "AppointmentsReport",
    "AppointmentsSummary",
    "AtRiskAppointment",
    "BillingDashboard",
    "BillingRevenue",
    "BucketBase",
    "DateRange",
    "DiscountsPendingApproval",
    "DoctorDashboard",
    "DoctorScheduleAppointment",
    "DoctorScheduleToday",
    "DoctorWeek",
    "Granularity",
    "MyPatients",
    "NamedRef",
    "NoShowAlerts",
    "OutstandingFilters",
    "OutstandingInvoice",
    "OutstandingReport",
    "OutstandingSummary",
    "PatientRegistrationsThisMonth",
    "PatientsBucket",
    "PatientsByGender",
    "PatientsReport",
    "PatientsSummary",
    "PeriodFilters",
    "ReceptionDashboard",
    "ReportExportFile",
    "ReportMeta",
    "RevenueBucket",
    "RevenueByMethod",
    "RevenueFigures",
    "RevenuePeriod",
    "RevenueReport",
    "ScheduleToday",
    "WalkInQueue",
]

Granularity = Literal["day", "week", "month"]


# ── Shared pieces ────────────────────────────────────────────────────────────


class ReportMeta(BaseModel):
    """Context every report and dashboard is read in."""

    hospital_name: str
    timezone: str = Field(description="IANA zone used for every figure in the response.")
    currency: str = Field(description="ISO 4217 code of every money value in the response.")
    today: date = Field(description="Today in `timezone`.")
    generated_at: datetime = Field(description="When the figures were read (UTC, whole seconds).")


class DateRange(BaseModel):
    """An inclusive range of hospital-local dates.

    The first date is serialised as ``from``. It is ``from_`` in Python
    because ``from`` is a keyword.
    """

    from_: date = Field(serialization_alias="from")
    to: date


class NamedRef(BaseModel):
    """An id with the name to show for it."""

    id: UUID
    name: str


class BucketBase(BaseModel):
    """One calendar bucket of a period report."""

    bucket_start: date = Field(description="The day, the Monday, or the 1st of the month.")
    bucket_end: date = Field(description="The day, the Sunday, or the last day of the month.")
    partial: bool = Field(
        description="The bucket reaches outside `from`..`to`; its figures cover only the part inside."
    )


class AppointmentCounts(BaseModel):
    """Appointments by status. All six statuses are always present."""

    total: int
    booked: int
    checked_in: int
    in_progress: int
    completed: int
    cancelled: int
    no_show: int


class RevenueFigures(BaseModel):
    """What was billed, collected and refunded over one range.

    Billed is dated by an invoice's issue; collected and refunded by when the
    money moved. They are different time bases on purpose.
    """

    invoice_count: int
    invoiced_amount: Decimal
    payment_count: int
    collected_amount: Decimal
    refund_count: int
    refunded_amount: Decimal
    net_collected_amount: Decimal = Field(description="Collected minus refunded. May be negative.")


class PeriodFilters(DateRange):
    """The resolved period of a report, with defaults applied."""

    granularity: Granularity


# ── Patients report ──────────────────────────────────────────────────────────


class PatientsByGender(BaseModel):
    """Registrations in the range by recorded gender."""

    male: int
    female: int
    other: int
    unspecified: int


class PatientsSummary(BaseModel):
    """Headline figures of the patients report."""

    registered: int = Field(description="Active patients registered in the range.")
    active_total: int = Field(description="All active patients as of now.")
    by_gender: PatientsByGender


class PatientsBucket(BucketBase):
    """Registrations in one bucket."""

    registered: int


class PatientsReport(BaseModel):
    """``GET /reports/patients``."""

    meta: ReportMeta
    filters: PeriodFilters
    summary: PatientsSummary
    buckets: list[PatientsBucket]


# ── Appointments report ──────────────────────────────────────────────────────


class AppointmentsFilters(PeriodFilters):
    """The resolved period and the doctor and department the report is narrowed to."""

    doctor: NamedRef | None
    department: NamedRef | None


class AppointmentsSummary(AppointmentCounts):
    """Headline figures of the appointments report."""

    no_show_rate_percent: str | None = Field(
        description=(
            "no_show / (completed + no_show) x 100, one decimal. "
            "Null when nothing has reached an outcome."
        )
    )


class AppointmentBucket(BucketBase, AppointmentCounts):
    """Appointments scheduled in one bucket, by status."""


class AppointmentsByDoctor(BaseModel):
    """One doctor's appointments in the range."""

    doctor_id: UUID
    doctor_name: str
    department_id: UUID | None
    department_name: str | None
    total: int
    completed: int
    cancelled: int
    no_show: int


class AppointmentsByDepartment(BaseModel):
    """One department's appointments in the range. Null ids mean no department."""

    department_id: UUID | None
    department_name: str | None
    total: int
    completed: int
    cancelled: int
    no_show: int


class AppointmentsReport(BaseModel):
    """``GET /reports/appointments``."""

    meta: ReportMeta
    filters: AppointmentsFilters
    summary: AppointmentsSummary
    buckets: list[AppointmentBucket]
    by_doctor: list[AppointmentsByDoctor]
    by_department: list[AppointmentsByDepartment]


# ── Revenue report ───────────────────────────────────────────────────────────


class RevenueByMethod(BaseModel):
    """Money in and out through one payment method."""

    method: PaymentMethod
    payment_count: int
    collected_amount: Decimal
    refund_count: int
    refunded_amount: Decimal
    net_collected_amount: Decimal


class RevenueBucket(BucketBase, RevenueFigures):
    """Revenue figures of one bucket."""


class RevenueReport(BaseModel):
    """``GET /reports/revenue``."""

    meta: ReportMeta
    filters: PeriodFilters
    summary: RevenueFigures
    by_method: list[RevenueByMethod]
    buckets: list[RevenueBucket]


# ── Outstanding report ───────────────────────────────────────────────────────


class OutstandingFilters(BaseModel):
    """The date the outstanding balances are aged against."""

    as_of_date: date


class OutstandingSummary(BaseModel):
    """Unpaid invoices: issued or part-paid, and what is still owed on them."""

    invoice_count: int
    outstanding_amount: Decimal
    issued_count: int
    partially_paid_count: int


class AgeingBucket(BaseModel):
    """Unpaid invoices of one age band, counted in days since issue."""

    bucket: Literal["0_30", "31_60", "61_90", "over_90"]
    min_days: int
    max_days: int | None
    invoice_count: int
    outstanding_amount: Decimal


class OutstandingInvoice(BaseModel):
    """One unpaid invoice."""

    invoice_id: UUID
    invoice_number: str
    issued_at: datetime
    issued_date: date = Field(description="The issue date in the hospital's timezone.")
    age_days: int
    patient_id: UUID
    patient_name: str
    patient_mrn: str
    status: Literal["issued", "partially_paid"]
    total: Decimal
    amount_paid: Decimal
    balance_due: Decimal


class OutstandingReport(BaseModel):
    """``GET /reports/outstanding``."""

    meta: ReportMeta
    filters: OutstandingFilters
    summary: OutstandingSummary
    ageing: list[AgeingBucket]
    invoices: list[OutstandingInvoice] = Field(description="Oldest first; a capped list.")
    invoices_total: int = Field(description="How many unpaid invoices exist in all.")
    invoices_truncated: bool = Field(description="`invoices` holds fewer than `invoices_total`.")


# ── Dashboards ───────────────────────────────────────────────────────────────


class ScheduleToday(AppointmentCounts):
    """Today's appointments across the hospital."""

    date: date
    in_clinic: int = Field(description="Checked in plus in progress.")


class RevenuePeriod(DateRange, RevenueFigures):
    """Revenue figures over a named range."""


class PatientRegistrationsThisMonth(DateRange):
    """Registrations from the 1st of the month to today."""

    registered: int
    active_total: int


class AdminDashboard(BaseModel):
    """``GET /dashboards/admin``."""

    meta: ReportMeta
    appointments_today: ScheduleToday
    revenue_this_week: RevenuePeriod
    patient_registrations_this_month: PatientRegistrationsThisMonth


class DoctorScheduleAppointment(BaseModel):
    """One of the doctor's appointments today."""

    appointment_id: UUID
    scheduled_start: datetime
    scheduled_end: datetime
    status: AppointmentStatus
    type: AppointmentType
    patient_id: UUID
    patient_name: str
    patient_mrn: str
    checked_in_at: datetime | None


class DoctorScheduleToday(AppointmentCounts):
    """The doctor's own appointments today."""

    date: date
    to_see: int = Field(description="Booked plus checked in.")
    appointments: list[DoctorScheduleAppointment]


class MyPatients(BaseModel):
    """Distinct active patients the doctor has seen or is due to see."""

    count: int


class DoctorWeek(DateRange, AppointmentCounts):
    """The doctor's appointments from Monday to Sunday of the current week."""


class DoctorDashboard(BaseModel):
    """``GET /dashboards/doctor``. Holds no money."""

    meta: ReportMeta
    doctor: NamedRef
    schedule_today: DoctorScheduleToday
    my_patients: MyPatients
    this_week: DoctorWeek


class WalkInQueue(BaseModel):
    """Today's walk-ins by where they are."""

    waiting: int = Field(description="Checked in, not yet started.")
    not_arrived: int = Field(description="Booked, not yet checked in.")
    in_consultation: int
    longest_wait_minutes: int | None = Field(
        description="Whole minutes since the earliest waiting check-in. Null when nobody waits."
    )


class AtRiskAppointment(BaseModel):
    """A booked appointment whose start time has passed with no check-in."""

    appointment_id: UUID
    scheduled_start: datetime
    minutes_late: int
    patient_id: UUID
    patient_name: str
    patient_mrn: str
    doctor_id: UUID
    doctor_name: str


class NoShowAlerts(BaseModel):
    """No-shows recorded today and the appointments heading that way."""

    marked_today: int
    at_risk: int = Field(description="All at-risk appointments, not only those listed.")
    at_risk_appointments: list[AtRiskAppointment]


class ReceptionDashboard(BaseModel):
    """``GET /dashboards/reception``. Holds no money."""

    meta: ReportMeta
    schedule_today: ScheduleToday
    walk_in_queue: WalkInQueue
    no_show_alerts: NoShowAlerts


class BillingRevenue(BaseModel):
    """Revenue today, this week (from Monday) and this month (from the 1st)."""

    today: RevenuePeriod
    this_week: RevenuePeriod
    this_month: RevenuePeriod


class DiscountsPendingApproval(BaseModel):
    """Invoices whose discount is waiting for an admin."""

    invoice_count: int
    discount_amount: Decimal


class BillingDashboard(BaseModel):
    """``GET /dashboards/billing``."""

    meta: ReportMeta
    unpaid_invoices: OutstandingSummary
    revenue: BillingRevenue
    discounts_pending_approval: DiscountsPendingApproval


# ── Export ───────────────────────────────────────────────────────────────────


class ReportExportFile(BaseModel):
    """A rendered export, ready to be sent as a download."""

    filename: str
    content: str = Field(description="The whole file as text, starting with a UTF-8 BOM.")
    row_count: int = Field(description="Data rows in the file, not counting headers or the total.")
