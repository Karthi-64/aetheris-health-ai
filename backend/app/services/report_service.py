"""Business logic for Reports and Dashboards.

Implements ``docs/modules/10-reports-dashboard.md``: four role dashboards,
four reports and their CSV export. Read-only, apart from the audit entry an
export leaves.

What the service guarantees:

- **Every figure is aggregated by the database.** The repository runs the SQL;
  this class only validates the request, fills gaps with zeros, subtracts
  refunds from collections, and shapes the result. No client adds anything.
- **Days are the hospital's days.** "Today", "this week" (from Monday) and
  every bucket are cut in the hospital's timezone. A hospital whose stored
  timezone is not a known zone is reported in UTC, with a warning logged.
- **One clock reading per request.** ``now`` is read once, to whole seconds,
  and is the ``generated_at`` of the response, the instant lateness is judged
  against, and the stamp in an export's filename.
- **No gaps.** A period report lists every calendar bucket in its range, with
  zeros where nothing happened, and every status, payment method, gender and
  ageing band is always present.
- **Money is exact.** Amounts stay ``Decimal`` from the column to the JSON
  string; a net figure is one ``Decimal`` subtracted from another.
- **An export is the report.** The CSV is rendered from the same method that
  serves the JSON, then recorded in the audit trail.

Not here: AI summaries, PDF export, scheduled delivery and caching
(module spec §3 and §8, deferred).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.audit import AuditEvent
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.models.billing import PaymentMethod
from app.repositories.report_repository import AGEING_BUCKETS, AppointmentCountsRow, MoneyTotal
from app.schemas.report import (
    AdminDashboard,
    AgeingBucket,
    AppointmentBucket,
    AppointmentsByDepartment,
    AppointmentsByDoctor,
    AppointmentsFilters,
    AppointmentsReport,
    AppointmentsSummary,
    AtRiskAppointment,
    BillingDashboard,
    BillingRevenue,
    DiscountsPendingApproval,
    DoctorDashboard,
    DoctorScheduleAppointment,
    DoctorScheduleToday,
    DoctorWeek,
    Granularity,
    MyPatients,
    NamedRef,
    NoShowAlerts,
    OutstandingFilters,
    OutstandingInvoice,
    OutstandingReport,
    OutstandingSummary,
    PatientRegistrationsThisMonth,
    PatientsBucket,
    PatientsByGender,
    PatientsReport,
    PatientsSummary,
    PeriodFilters,
    ReceptionDashboard,
    ReportExportFile,
    ReportMeta,
    RevenueBucket,
    RevenueByMethod,
    RevenueFigures,
    RevenuePeriod,
    RevenueReport,
    ScheduleToday,
    WalkInQueue,
)
from app.services.report_csv import (
    outstanding_filename,
    period_filename,
    render_appointments_csv,
    render_outstanding_csv,
    render_patients_csv,
    render_revenue_csv,
)
from app.services.report_periods import (
    build_buckets,
    field_error,
    local_bounds,
    month_start,
    resolve_period,
    week_start,
)
from app.utils.datetime import utc_now

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.repositories.doctor_repository import DoctorRepository
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.report_repository import ReportRepository

logger = get_logger(__name__)

__all__ = [
    "AT_RISK_LIST_LIMIT",
    "EXPORT_INVOICE_LIMIT",
    "REPORT_IDS",
    "REPORT_INVOICE_LIMIT",
    "DoctorProfileRequiredError",
    "ReportService",
    "no_show_rate_percent",
]

#: The reports that exist, and so the ids an export accepts.
REPORT_IDS: tuple[str, ...] = ("patients", "appointments", "revenue", "outstanding")

#: Unpaid invoices listed by the outstanding report on screen.
REPORT_INVOICE_LIMIT = 100

#: Unpaid invoices written to an export (module spec §7: 10k rows).
EXPORT_INVOICE_LIMIT = 10_000

#: At-risk appointments listed on the reception dashboard. The count is not limited.
AT_RISK_LIST_LIMIT = 20

_ZERO = Decimal("0.00")
_TENTH = Decimal("0.1")

#: The order payment methods are reported in.
_METHOD_ORDER: tuple[PaymentMethod, ...] = (
    PaymentMethod.CASH,
    PaymentMethod.CARD,
    PaymentMethod.UPI,
    PaymentMethod.BANK_TRANSFER,
    PaymentMethod.INSURANCE,
)


class DoctorProfileRequiredError(NotFoundError):
    """Raised when the doctor dashboard is asked for by someone who is not a doctor.

    A missing or deactivated doctor profile answers "not found", never a
    hospital-wide view: the dashboard is the caller's own schedule or nothing.
    """

    def __init__(self) -> None:
        super().__init__(message="No active doctor profile is linked to this account.")


def no_show_rate_percent(no_show: int, outcomes: int) -> str | None:
    """Return the share of concluded appointments that were no-shows.

    :param no_show: Appointments that ended as a no-show.
    :param outcomes: Appointments that were due and reached an outcome:
        completed plus no-show. Open and cancelled appointments are in
        neither number, so booking a future visit does not move the rate.
    :returns: The percentage rounded half-up to one decimal, as a string
        (``"25.0"``), or ``None`` when nothing has reached an outcome.
    """
    if outcomes <= 0:
        return None
    rate = (Decimal(no_show) * 100 / Decimal(outcomes)).quantize(_TENTH, rounding=ROUND_HALF_UP)
    return str(rate)


@dataclass(frozen=True, slots=True)
class _Context:
    """What one request is answered against: the hospital, its zone, and the time."""

    hospital_id: uuid.UUID
    meta: ReportMeta
    zone: ZoneInfo
    now: datetime
    today: date

    @property
    def tz(self) -> str:
        """The effective IANA zone name, as sent to SQL."""
        return self.meta.timezone

    def bounds(self, from_: date, to: date) -> tuple[datetime, datetime]:
        """Return the UTC interval covering the local dates ``from_``..``to``."""
        return local_bounds(from_, to, self.zone)


def _counts(row: AppointmentCountsRow) -> dict[str, int]:
    """Spread a counts row into the fields of ``AppointmentCounts``."""
    return {
        "total": row.total,
        "booked": row.booked,
        "checked_in": row.checked_in,
        "in_progress": row.in_progress,
        "completed": row.completed,
        "cancelled": row.cancelled,
        "no_show": row.no_show,
    }


def _figures(invoiced: MoneyTotal, collected: MoneyTotal, refunded: MoneyTotal) -> dict[str, Any]:
    """Spread three totals into the fields of ``RevenueFigures``, with the net."""
    return {
        "invoice_count": invoiced.count,
        "invoiced_amount": invoiced.amount,
        "payment_count": collected.count,
        "collected_amount": collected.amount,
        "refund_count": refunded.count,
        "refunded_amount": refunded.amount,
        "net_collected_amount": collected.amount - refunded.amount,
    }


class ReportService:
    """Dashboards, reports and their export.

    :param reports: The SQL aggregates.
    :param hospitals: Hospital lookups, for the name, timezone and currency.
    :param doctors: Doctor lookups, to find the caller's own doctor profile.
    :param session: Request-scoped session, held to commit an export's audit entry.
    :param audit: Where an export is recorded.
    """

    def __init__(
        self,
        reports: ReportRepository,
        hospitals: HospitalRepository,
        doctors: DoctorRepository,
        session: AsyncSession,
        audit: AuditSink,
    ) -> None:
        self._reports = reports
        self._hospitals = hospitals
        self._doctors = doctors
        self._session = session
        self._audit = audit

    # ── Dashboards ────────────────────────────────────────────────────────────

    async def admin_dashboard(self, hospital_id: uuid.UUID) -> AdminDashboard:
        """Build the hospital admin's dashboard.

        Today's appointments, revenue from Monday to today, and patient
        registrations from the 1st of the month to today.

        :param hospital_id: The tenant to report on.
        :returns: The dashboard.
        :raises NotFoundError: If the hospital does not exist.
        """
        ctx = await self._context(hospital_id, self._now())
        today_lo, today_hi = ctx.bounds(ctx.today, ctx.today)
        monday, first = week_start(ctx.today), month_start(ctx.today)
        month_lo, month_hi = ctx.bounds(first, ctx.today)

        registered = await self._reports.patient_registrations(
            hospital_id, start=month_lo, end=month_hi, tz=ctx.tz, granularity="month"
        )
        return AdminDashboard(
            meta=ctx.meta,
            appointments_today=await self._schedule_today(ctx, today_lo, today_hi),
            revenue_this_week=await self._revenue_period(ctx, monday, ctx.today),
            patient_registrations_this_month=PatientRegistrationsThisMonth(
                from_=first,
                to=ctx.today,
                registered=sum(registered.values()),
                active_total=await self._reports.count_active_patients(hospital_id),
            ),
        )

    async def doctor_dashboard(self, hospital_id: uuid.UUID, user_id: uuid.UUID) -> DoctorDashboard:
        """Build a doctor's own dashboard. It holds no money.

        Today's schedule, the doctor's distinct patients, and this week's
        appointments from Monday to Sunday.

        :param hospital_id: The tenant to report on.
        :param user_id: The caller. Their active doctor profile scopes everything.
        :returns: The dashboard.
        :raises NotFoundError: If the hospital does not exist.
        :raises DoctorProfileRequiredError: If the caller has no active doctor profile.
        """
        ctx = await self._context(hospital_id, self._now())
        profile = await self._doctors.get_doctor_by_user_id(
            hospital_id, user_id, include_deleted=False
        )
        if profile is None:
            raise DoctorProfileRequiredError
        doctor_id = profile.id
        ref = await self._reports.doctor_ref(hospital_id, doctor_id)
        if ref is None:
            raise DoctorProfileRequiredError

        today_lo, today_hi = ctx.bounds(ctx.today, ctx.today)
        monday = week_start(ctx.today)
        sunday = monday + timedelta(days=6)
        week_lo, week_hi = ctx.bounds(monday, sunday)

        today_counts = await self._reports.appointment_counts(
            hospital_id, start=today_lo, end=today_hi, doctor_id=doctor_id
        )
        schedule = await self._reports.doctor_schedule(
            hospital_id, doctor_id=doctor_id, start=today_lo, end=today_hi
        )
        week_counts = await self._reports.appointment_counts(
            hospital_id, start=week_lo, end=week_hi, doctor_id=doctor_id
        )
        return DoctorDashboard(
            meta=ctx.meta,
            doctor=NamedRef(id=ref.id, name=ref.name),
            schedule_today=DoctorScheduleToday(
                date=ctx.today,
                to_see=today_counts.booked + today_counts.checked_in,
                appointments=[
                    DoctorScheduleAppointment(
                        appointment_id=row.appointment_id,
                        scheduled_start=row.scheduled_start,
                        scheduled_end=row.scheduled_end,
                        status=row.status,
                        type=row.type,
                        patient_id=row.patient_id,
                        patient_name=row.patient_name,
                        patient_mrn=row.patient_mrn,
                        checked_in_at=row.checked_in_at,
                    )
                    for row in schedule
                ],
                **_counts(today_counts),
            ),
            my_patients=MyPatients(
                count=await self._reports.count_doctor_patients(hospital_id, doctor_id=doctor_id)
            ),
            this_week=DoctorWeek(from_=monday, to=sunday, **_counts(week_counts)),
        )

    async def reception_dashboard(self, hospital_id: uuid.UUID) -> ReceptionDashboard:
        """Build the reception dashboard. It holds no money.

        Today's schedule, today's walk-ins by where they are, and no-shows:
        those recorded today and the booked appointments already running late.

        :param hospital_id: The tenant to report on.
        :returns: The dashboard.
        :raises NotFoundError: If the hospital does not exist.
        """
        ctx = await self._context(hospital_id, self._now())
        lo, hi = ctx.bounds(ctx.today, ctx.today)

        schedule = await self._schedule_today(ctx, lo, hi)
        walk_ins = await self._reports.walk_in_queue(hospital_id, start=lo, end=hi)
        at_risk = await self._reports.count_at_risk(hospital_id, start=lo, end=hi, now=ctx.now)
        late = await self._reports.at_risk_appointments(
            hospital_id, start=lo, end=hi, now=ctx.now, limit=AT_RISK_LIST_LIMIT
        )
        earliest = walk_ins.earliest_checked_in_at
        return ReceptionDashboard(
            meta=ctx.meta,
            schedule_today=schedule,
            walk_in_queue=WalkInQueue(
                waiting=walk_ins.waiting,
                not_arrived=walk_ins.not_arrived,
                in_consultation=walk_ins.in_consultation,
                longest_wait_minutes=(
                    None if earliest is None else _whole_minutes(ctx.now - earliest)
                ),
            ),
            no_show_alerts=NoShowAlerts(
                marked_today=schedule.no_show,
                at_risk=at_risk,
                at_risk_appointments=[
                    AtRiskAppointment(
                        appointment_id=row.appointment_id,
                        scheduled_start=row.scheduled_start,
                        minutes_late=_whole_minutes(ctx.now - row.scheduled_start),
                        patient_id=row.patient_id,
                        patient_name=row.patient_name,
                        patient_mrn=row.patient_mrn,
                        doctor_id=row.doctor_id,
                        doctor_name=row.doctor_name,
                    )
                    for row in late
                ],
            ),
        )

    async def billing_dashboard(self, hospital_id: uuid.UUID) -> BillingDashboard:
        """Build the billing dashboard.

        What is owed on unpaid invoices, revenue today, this week (from
        Monday) and this month (from the 1st), and discounts awaiting approval.

        :param hospital_id: The tenant to report on.
        :returns: The dashboard.
        :raises NotFoundError: If the hospital does not exist.
        """
        ctx = await self._context(hospital_id, self._now())
        unpaid = await self._reports.outstanding_summary(hospital_id)
        pending = await self._reports.pending_discounts(hospital_id)
        return BillingDashboard(
            meta=ctx.meta,
            unpaid_invoices=OutstandingSummary(
                invoice_count=unpaid.invoice_count,
                outstanding_amount=unpaid.outstanding_amount,
                issued_count=unpaid.issued_count,
                partially_paid_count=unpaid.partially_paid_count,
            ),
            revenue=BillingRevenue(
                today=await self._revenue_period(ctx, ctx.today, ctx.today),
                this_week=await self._revenue_period(ctx, week_start(ctx.today), ctx.today),
                this_month=await self._revenue_period(ctx, month_start(ctx.today), ctx.today),
            ),
            discounts_pending_approval=DiscountsPendingApproval(
                invoice_count=pending.count, discount_amount=pending.amount
            ),
        )

    # ── Reports ───────────────────────────────────────────────────────────────

    async def patients_report(
        self,
        hospital_id: uuid.UUID,
        *,
        from_: date | None = None,
        to: date | None = None,
        granularity: str | None = None,
    ) -> PatientsReport:
        """Report patient registrations over a period.

        Counts active patients only: one deactivated since is not in any figure.

        :param hospital_id: The tenant to report on.
        :param from_: First local date; defaults to 29 days before ``to``.
        :param to: Last local date; defaults to today.
        :param granularity: ``day`` (default), ``week`` or ``month``.
        :returns: The report.
        :raises NotFoundError: If the hospital does not exist.
        :raises ValidationError: If the period breaks a rule.
        """
        ctx = await self._context(hospital_id, self._now())
        return await self._patients_report(ctx, from_=from_, to=to, granularity=granularity)

    async def appointments_report(
        self,
        hospital_id: uuid.UUID,
        *,
        from_: date | None = None,
        to: date | None = None,
        granularity: str | None = None,
        doctor_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
    ) -> AppointmentsReport:
        """Report appointments over a period, by status, doctor and department.

        Appointments are dated by their scheduled start. Both filters may be
        given; a doctor outside the department yields zeros, not an error.

        :param hospital_id: The tenant to report on.
        :param from_: First local date; defaults to 29 days before ``to``.
        :param to: Last local date; defaults to today.
        :param granularity: ``day`` (default), ``week`` or ``month``.
        :param doctor_id: Only this doctor's appointments.
        :param department_id: Only appointments of doctors now in this department.
        :returns: The report.
        :raises NotFoundError: If the hospital does not exist.
        :raises ValidationError: If the period breaks a rule, or the doctor or
            department is not one of this hospital's.
        """
        ctx = await self._context(hospital_id, self._now())
        return await self._appointments_report(
            ctx,
            from_=from_,
            to=to,
            granularity=granularity,
            doctor_id=doctor_id,
            department_id=department_id,
        )

    async def revenue_report(
        self,
        hospital_id: uuid.UUID,
        *,
        from_: date | None = None,
        to: date | None = None,
        granularity: str | None = None,
    ) -> RevenueReport:
        """Report what was billed, collected and refunded over a period.

        Billed is dated by the invoice's issue; collected and refunded by when
        the money moved.

        :param hospital_id: The tenant to report on.
        :param from_: First local date; defaults to 29 days before ``to``.
        :param to: Last local date; defaults to today.
        :param granularity: ``day`` (default), ``week`` or ``month``.
        :returns: The report.
        :raises NotFoundError: If the hospital does not exist.
        :raises ValidationError: If the period breaks a rule.
        """
        ctx = await self._context(hospital_id, self._now())
        return await self._revenue_report(ctx, from_=from_, to=to, granularity=granularity)

    async def outstanding_report(
        self, hospital_id: uuid.UUID, *, invoice_limit: int = REPORT_INVOICE_LIMIT
    ) -> OutstandingReport:
        """Report what is owed on unpaid invoices, as of now.

        :param hospital_id: The tenant to report on.
        :param invoice_limit: The most invoices to list. Totals cover them all.
        :returns: The report.
        :raises NotFoundError: If the hospital does not exist.
        """
        ctx = await self._context(hospital_id, self._now())
        return await self._outstanding_report(ctx, invoice_limit=invoice_limit)

    # ── Export ────────────────────────────────────────────────────────────────

    async def export_report(
        self,
        hospital_id: uuid.UUID,
        report_id: str,
        *,
        actor_id: uuid.UUID | None,
        format: str | None = None,  # noqa: A002 — the query parameter's name
        from_: date | None = None,
        to: date | None = None,
        granularity: str | None = None,
        doctor_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
    ) -> ReportExportFile:
        """Render a report as a CSV download and record that it was exported.

        The file is built from the same method that serves the JSON report, so
        its figures are the screen's. The format is checked before anything is
        read; a refused export leaves no audit entry.

        :param hospital_id: The tenant to report on.
        :param report_id: ``patients``, ``appointments``, ``revenue`` or ``outstanding``.
        :param actor_id: The user exporting, for the audit entry.
        :param format: ``csv`` (default). Nothing else is available.
        :param from_: First local date, for a period report.
        :param to: Last local date, for a period report.
        :param granularity: Bucket size, for a period report.
        :param doctor_id: Doctor filter, for the appointments report.
        :param department_id: Department filter, for the appointments report.
        :returns: The file's name, text and data-row count.
        :raises ValidationError: If the format is not ``csv`` or the report's
            own parameters break a rule.
        :raises NotFoundError: If the report or the hospital does not exist.
        """
        requested = "csv" if format is None else format
        if requested.lower() == "pdf":
            raise field_error("format", "PDF export is not available yet. Use format=csv.")
        if requested.lower() != "csv":
            raise field_error("format", f"Unsupported export format `{requested}`. Use format=csv.")
        if report_id not in REPORT_IDS:
            raise NotFoundError(message=f"Unknown report: `{report_id}`.")

        ctx = await self._context(hospital_id, self._now())
        context: dict[str, Any] = {"report_id": report_id, "format": "csv"}

        if report_id == "outstanding":
            outstanding = await self._outstanding_report(ctx, invoice_limit=EXPORT_INVOICE_LIMIT)
            content = render_outstanding_csv(outstanding)
            filename = outstanding_filename(outstanding.filters.as_of_date, ctx.now)
            row_count = len(outstanding.invoices)
        else:
            filters: PeriodFilters
            if report_id == "patients":
                patients = await self._patients_report(
                    ctx, from_=from_, to=to, granularity=granularity
                )
                content, filters = render_patients_csv(patients), patients.filters
                row_count = len(patients.buckets)
            elif report_id == "appointments":
                appointments = await self._appointments_report(
                    ctx,
                    from_=from_,
                    to=to,
                    granularity=granularity,
                    doctor_id=doctor_id,
                    department_id=department_id,
                )
                content, filters = render_appointments_csv(appointments), appointments.filters
                row_count = len(appointments.buckets)
                if doctor_id is not None:
                    context["doctor_id"] = str(doctor_id)
                if department_id is not None:
                    context["department_id"] = str(department_id)
            else:
                revenue = await self._revenue_report(
                    ctx, from_=from_, to=to, granularity=granularity
                )
                content, filters = render_revenue_csv(revenue), revenue.filters
                row_count = len(revenue.buckets)
            filename = period_filename(report_id, filters.from_, filters.to, ctx.now)
            context["from"] = filters.from_.isoformat()
            context["to"] = filters.to.isoformat()
            context["granularity"] = filters.granularity
        context["row_count"] = row_count

        await self._audit.record(
            AuditEvent(
                action="report.exported",
                hospital_id=hospital_id,
                target_type="report",
                target_id=None,
                actor_id=actor_id,
                context=context,
            )
        )
        # The sink writes inside a savepoint; the entry is durable only on commit.
        await self._session.commit()
        logger.info(
            "report.exported",
            hospital_id=str(hospital_id),
            report_id=report_id,
            row_count=row_count,
        )
        return ReportExportFile(filename=filename, content=content, row_count=row_count)

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _now() -> datetime:
        """Read the clock once for a request, to whole seconds."""
        return utc_now().replace(microsecond=0)

    async def _context(self, hospital_id: uuid.UUID, now: datetime) -> _Context:
        """Load the hospital and fix the time and zone a request is answered in.

        :param hospital_id: The tenant to report on.
        :param now: The request's single clock reading (UTC, whole seconds).
        :returns: The request context.
        :raises NotFoundError: If the hospital does not exist.
        """
        hospital = await self._hospitals.get_by_id(hospital_id)
        if hospital is None:
            raise NotFoundError(message="Hospital not found.")

        try:
            zone = ZoneInfo(hospital.timezone)
            tz_name = hospital.timezone
        except (ZoneInfoNotFoundError, ValueError, OSError):
            logger.warning(
                "report.hospital_timezone_invalid",
                hospital_id=str(hospital_id),
                timezone=hospital.timezone,
            )
            zone, tz_name = ZoneInfo("UTC"), "UTC"

        today = now.astimezone(zone).date()
        return _Context(
            hospital_id=hospital_id,
            meta=ReportMeta(
                hospital_name=hospital.name,
                timezone=tz_name,
                currency=hospital.currency,
                today=today,
                generated_at=now,
            ),
            zone=zone,
            now=now,
            today=today,
        )

    async def _schedule_today(self, ctx: _Context, lo: datetime, hi: datetime) -> ScheduleToday:
        """Count today's appointments across the hospital, with those in clinic."""
        counts = await self._reports.appointment_counts(ctx.hospital_id, start=lo, end=hi)
        return ScheduleToday(
            date=ctx.today,
            in_clinic=counts.checked_in + counts.in_progress,
            **_counts(counts),
        )

    async def _revenue_period(self, ctx: _Context, from_: date, to: date) -> RevenuePeriod:
        """Total what was billed, collected and refunded over local dates."""
        lo, hi = ctx.bounds(from_, to)
        invoiced = await self._reports.invoiced_totals(ctx.hospital_id, start=lo, end=hi)
        collected = _sum_totals(
            await self._reports.payment_totals_by_method(ctx.hospital_id, start=lo, end=hi)
        )
        refunded = _sum_totals(
            await self._reports.refund_totals_by_method(ctx.hospital_id, start=lo, end=hi)
        )
        return RevenuePeriod(from_=from_, to=to, **_figures(invoiced, collected, refunded))

    async def _patients_report(
        self,
        ctx: _Context,
        *,
        from_: date | None,
        to: date | None,
        granularity: str | None,
    ) -> PatientsReport:
        """Build the patients report against a fixed request context."""
        period = resolve_period(from_, to, granularity, ctx.today)
        lo, hi = ctx.bounds(period.from_, period.to)
        hospital_id = ctx.hospital_id

        by_bucket = await self._reports.patient_registrations(
            hospital_id, start=lo, end=hi, tz=ctx.tz, granularity=period.granularity
        )
        by_gender = await self._reports.patient_registrations_by_gender(
            hospital_id, start=lo, end=hi
        )
        buckets = [
            PatientsBucket(
                bucket_start=span.start,
                bucket_end=span.end,
                partial=span.partial,
                registered=by_bucket.get(span.start, 0),
            )
            for span in build_buckets(period.from_, period.to, period.granularity)
        ]
        return PatientsReport(
            meta=ctx.meta,
            filters=PeriodFilters(
                from_=period.from_,
                to=period.to,
                granularity=cast("Granularity", period.granularity),
            ),
            summary=PatientsSummary(
                registered=sum(by_bucket.values()),
                active_total=await self._reports.count_active_patients(hospital_id),
                by_gender=PatientsByGender(
                    male=by_gender.get("male", 0),
                    female=by_gender.get("female", 0),
                    other=by_gender.get("other", 0),
                    unspecified=by_gender.get("unspecified", 0),
                ),
            ),
            buckets=buckets,
        )

    async def _appointments_report(
        self,
        ctx: _Context,
        *,
        from_: date | None,
        to: date | None,
        granularity: str | None,
        doctor_id: uuid.UUID | None,
        department_id: uuid.UUID | None,
    ) -> AppointmentsReport:
        """Build the appointments report against a fixed request context."""
        period = resolve_period(from_, to, granularity, ctx.today)
        hospital_id = ctx.hospital_id

        doctor: NamedRef | None = None
        if doctor_id is not None:
            found = await self._reports.doctor_ref(hospital_id, doctor_id)
            if found is None:
                raise field_error("doctor_id", "Doctor not found.")
            doctor = NamedRef(id=found.id, name=found.name)
        department: NamedRef | None = None
        if department_id is not None:
            found = await self._reports.department_ref(hospital_id, department_id)
            if found is None:
                raise field_error("department_id", "Department not found.")
            department = NamedRef(id=found.id, name=found.name)

        lo, hi = ctx.bounds(period.from_, period.to)
        totals = await self._reports.appointment_counts(
            hospital_id, start=lo, end=hi, doctor_id=doctor_id, department_id=department_id
        )
        by_bucket = await self._reports.appointment_counts_by_bucket(
            hospital_id,
            start=lo,
            end=hi,
            tz=ctx.tz,
            granularity=period.granularity,
            doctor_id=doctor_id,
            department_id=department_id,
        )
        by_doctor = await self._reports.appointment_counts_by_doctor(
            hospital_id, start=lo, end=hi, doctor_id=doctor_id, department_id=department_id
        )
        by_department = await self._reports.appointment_counts_by_department(
            hospital_id, start=lo, end=hi, doctor_id=doctor_id, department_id=department_id
        )

        buckets: list[AppointmentBucket] = []
        for span in build_buckets(period.from_, period.to, period.granularity):
            buckets.append(
                AppointmentBucket(
                    bucket_start=span.start,
                    bucket_end=span.end,
                    partial=span.partial,
                    **_counts(by_bucket.get(span.start, AppointmentCountsRow())),
                )
            )

        return AppointmentsReport(
            meta=ctx.meta,
            filters=AppointmentsFilters(
                from_=period.from_,
                to=period.to,
                granularity=cast("Granularity", period.granularity),
                doctor=doctor,
                department=department,
            ),
            summary=AppointmentsSummary(
                no_show_rate_percent=no_show_rate_percent(
                    totals.no_show, totals.completed + totals.no_show
                ),
                **_counts(totals),
            ),
            buckets=buckets,
            by_doctor=[
                AppointmentsByDoctor(
                    doctor_id=item.doctor_id,
                    doctor_name=item.doctor_name,
                    department_id=item.department_id,
                    department_name=item.department_name,
                    total=item.total,
                    completed=item.completed,
                    cancelled=item.cancelled,
                    no_show=item.no_show,
                )
                for item in by_doctor
            ],
            by_department=[
                AppointmentsByDepartment(
                    department_id=item.department_id,
                    department_name=item.department_name,
                    total=item.total,
                    completed=item.completed,
                    cancelled=item.cancelled,
                    no_show=item.no_show,
                )
                for item in by_department
            ],
        )

    async def _revenue_report(
        self,
        ctx: _Context,
        *,
        from_: date | None,
        to: date | None,
        granularity: str | None,
    ) -> RevenueReport:
        """Build the revenue report against a fixed request context."""
        period = resolve_period(from_, to, granularity, ctx.today)
        lo, hi = ctx.bounds(period.from_, period.to)
        hospital_id = ctx.hospital_id
        bucketed = {"tz": ctx.tz, "granularity": period.granularity}

        invoiced = await self._reports.invoiced_totals(hospital_id, start=lo, end=hi)
        paid_by_method = await self._reports.payment_totals_by_method(hospital_id, start=lo, end=hi)
        refunded_by_method = await self._reports.refund_totals_by_method(
            hospital_id, start=lo, end=hi
        )
        invoiced_by_bucket = await self._reports.invoiced_by_bucket(
            hospital_id, start=lo, end=hi, **bucketed
        )
        paid_by_bucket = await self._reports.payments_by_bucket(
            hospital_id, start=lo, end=hi, **bucketed
        )
        refunded_by_bucket = await self._reports.refunds_by_bucket(
            hospital_id, start=lo, end=hi, **bucketed
        )

        nothing = MoneyTotal()
        return RevenueReport(
            meta=ctx.meta,
            filters=PeriodFilters(
                from_=period.from_,
                to=period.to,
                granularity=cast("Granularity", period.granularity),
            ),
            summary=RevenueFigures(
                **_figures(invoiced, _sum_totals(paid_by_method), _sum_totals(refunded_by_method))
            ),
            by_method=[
                RevenueByMethod(
                    method=method,
                    payment_count=paid_by_method.get(method.value, nothing).count,
                    collected_amount=paid_by_method.get(method.value, nothing).amount,
                    refund_count=refunded_by_method.get(method.value, nothing).count,
                    refunded_amount=refunded_by_method.get(method.value, nothing).amount,
                    net_collected_amount=(
                        paid_by_method.get(method.value, nothing).amount
                        - refunded_by_method.get(method.value, nothing).amount
                    ),
                )
                for method in _METHOD_ORDER
            ],
            buckets=[
                RevenueBucket(
                    bucket_start=span.start,
                    bucket_end=span.end,
                    partial=span.partial,
                    **_figures(
                        invoiced_by_bucket.get(span.start, nothing),
                        paid_by_bucket.get(span.start, nothing),
                        refunded_by_bucket.get(span.start, nothing),
                    ),
                )
                for span in build_buckets(period.from_, period.to, period.granularity)
            ],
        )

    async def _outstanding_report(self, ctx: _Context, *, invoice_limit: int) -> OutstandingReport:
        """Build the outstanding report against a fixed request context."""
        hospital_id = ctx.hospital_id
        summary = await self._reports.outstanding_summary(hospital_id)
        ageing = await self._reports.outstanding_ageing(hospital_id, today=ctx.today, tz=ctx.tz)
        rows = await self._reports.outstanding_invoices(hospital_id, tz=ctx.tz, limit=invoice_limit)

        nothing = MoneyTotal()
        return OutstandingReport(
            meta=ctx.meta,
            filters=OutstandingFilters(as_of_date=ctx.today),
            summary=OutstandingSummary(
                invoice_count=summary.invoice_count,
                outstanding_amount=summary.outstanding_amount,
                issued_count=summary.issued_count,
                partially_paid_count=summary.partially_paid_count,
            ),
            ageing=[
                AgeingBucket(
                    bucket=cast("Literal['0_30', '31_60', '61_90', 'over_90']", key),
                    min_days=first,
                    max_days=last,
                    invoice_count=ageing.get(key, nothing).count,
                    outstanding_amount=ageing.get(key, nothing).amount,
                )
                for key, first, last in AGEING_BUCKETS
            ],
            invoices=[
                OutstandingInvoice(
                    invoice_id=row.invoice_id,
                    invoice_number=row.invoice_number,
                    issued_at=row.issued_at,
                    issued_date=row.issued_date,
                    age_days=(ctx.today - row.issued_date).days,
                    patient_id=row.patient_id,
                    patient_name=row.patient_name,
                    patient_mrn=row.patient_mrn,
                    status=cast("Literal['issued', 'partially_paid']", row.status.value),
                    total=row.total,
                    amount_paid=row.amount_paid,
                    balance_due=row.balance_due,
                )
                for row in rows
            ],
            invoices_total=summary.invoice_count,
            invoices_truncated=summary.invoice_count > len(rows),
        )


def _whole_minutes(elapsed: timedelta) -> int:
    """Return an elapsed time as whole minutes, rounded down and never negative."""
    return max(0, int(elapsed.total_seconds() // 60))


def _sum_totals(totals: dict[str, MoneyTotal]) -> MoneyTotal:
    """Add per-method totals into one. Exact: ``Decimal`` addition only."""
    return MoneyTotal(
        count=sum(total.count for total in totals.values()),
        amount=sum((total.amount for total in totals.values()), _ZERO),
    )
