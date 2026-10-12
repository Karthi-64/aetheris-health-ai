"""Unit tests for :class:`~app.services.report_service.ReportService`.

The repository is an in-memory fake that returns what a test sets and records
how it was called. That is what the service is responsible for: the ranges it
asks for, the gaps it fills, the subtraction and the rounding. The SQL is
covered in ``repository/test_report_repository.py``.

The clock is pinned by patching ``app.services.report_service.utc_now``, so
nothing here depends on the time of day the suite runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.models.appointment import AppointmentStatus, AppointmentType
from app.models.billing import InvoiceStatus
from app.repositories.report_repository import (
    AppointmentCountsRow,
    AtRiskRow,
    MoneyTotal,
    NamedRefRow,
    OutstandingInvoiceRow,
    OutstandingSummaryRow,
    ScheduleRow,
    WalkInRow,
)
from app.services import report_service
from app.services.report_service import (
    DoctorProfileRequiredError,
    ReportService,
    no_show_rate_percent,
)
from app.tests.conftest import FakeSession, RecordingAuditSink

HOSPITAL_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
DOCTOR_ID = uuid.uuid4()
DEPARTMENT_ID = uuid.uuid4()

#: 6 Oct 2026 14:02:11 in Asia/Kolkata — a Tuesday afternoon.
NOW = datetime(2026, 10, 6, 8, 32, 11, 482913, tzinfo=UTC)

METHODS = ("cash", "card", "upi", "bank_transfer", "insurance")


def _money(count: int, amount: str) -> MoneyTotal:
    return MoneyTotal(count=count, amount=Decimal(amount))


def _by_method(**totals: MoneyTotal) -> dict[str, MoneyTotal]:
    """Every method present, as the repository returns it."""
    return {method: totals.get(method, MoneyTotal()) for method in METHODS}


@dataclass
class _Hospital:
    name: str = "Demo Hospital"
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"


class FakeHospitals:
    """Hospital lookups."""

    def __init__(self, hospital: _Hospital | None) -> None:
        self.hospital = hospital

    async def get_by_id(self, hospital_id: uuid.UUID) -> _Hospital | None:
        assert hospital_id == HOSPITAL_ID
        return self.hospital


@dataclass
class _Doctor:
    id: uuid.UUID
    deleted: bool = False


class FakeDoctors:
    """Doctor-profile lookups by user."""

    def __init__(self, profiles: dict[uuid.UUID, _Doctor] | None = None) -> None:
        self.profiles = profiles or {}
        self.calls: list[tuple[uuid.UUID, uuid.UUID, bool]] = []

    async def get_doctor_by_user_id(
        self, hospital_id: uuid.UUID, user_id: uuid.UUID, *, include_deleted: bool = True
    ) -> _Doctor | None:
        self.calls.append((hospital_id, user_id, include_deleted))
        profile = self.profiles.get(user_id)
        if profile is None or (profile.deleted and not include_deleted):
            return None
        return profile


@dataclass
class FakeReports:
    """The repository: canned answers out, calls recorded."""

    counts: AppointmentCountsRow = field(default_factory=AppointmentCountsRow)
    counts_by_bucket: dict[date, AppointmentCountsRow] = field(default_factory=dict)
    registrations: dict[date, int] = field(default_factory=dict)
    by_gender: dict[str, int] = field(default_factory=dict)
    active_patients: int = 0
    schedule: list[ScheduleRow] = field(default_factory=list)
    doctor_patients: int = 0
    walk_ins: WalkInRow = field(default_factory=WalkInRow)
    at_risk: list[AtRiskRow] = field(default_factory=list)
    invoiced: MoneyTotal = field(default_factory=MoneyTotal)
    payments: dict[str, MoneyTotal] = field(default_factory=_by_method)
    refunds: dict[str, MoneyTotal] = field(default_factory=_by_method)
    invoiced_buckets: dict[date, MoneyTotal] = field(default_factory=dict)
    payment_buckets: dict[date, MoneyTotal] = field(default_factory=dict)
    refund_buckets: dict[date, MoneyTotal] = field(default_factory=dict)
    outstanding: OutstandingSummaryRow = field(default_factory=OutstandingSummaryRow)
    ageing: dict[str, MoneyTotal] = field(default_factory=dict)
    invoices: list[OutstandingInvoiceRow] = field(default_factory=list)
    discounts: MoneyTotal = field(default_factory=MoneyTotal)
    doctors: dict[uuid.UUID, str] = field(default_factory=dict)
    departments: dict[uuid.UUID, str] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _saw(self, name: str, hospital_id: uuid.UUID, **kwargs: Any) -> None:
        assert hospital_id == HOSPITAL_ID, f"{name} was called for another hospital"
        self.calls.append((name, kwargs))

    def named(self, name: str) -> list[dict[str, Any]]:
        """The keyword arguments of every call to one method, in order."""
        return [kwargs for called, kwargs in self.calls if called == name]

    async def patient_registrations(self, hospital_id: uuid.UUID, **kw: Any) -> dict[date, int]:
        self._saw("patient_registrations", hospital_id, **kw)
        return self.registrations

    async def patient_registrations_by_gender(
        self, hospital_id: uuid.UUID, **kw: Any
    ) -> dict[str, int]:
        self._saw("patient_registrations_by_gender", hospital_id, **kw)
        return self.by_gender

    async def count_active_patients(self, hospital_id: uuid.UUID) -> int:
        self._saw("count_active_patients", hospital_id)
        return self.active_patients

    async def appointment_counts(self, hospital_id: uuid.UUID, **kw: Any) -> AppointmentCountsRow:
        self._saw("appointment_counts", hospital_id, **kw)
        return self.counts

    async def appointment_counts_by_bucket(
        self, hospital_id: uuid.UUID, **kw: Any
    ) -> dict[date, AppointmentCountsRow]:
        self._saw("appointment_counts_by_bucket", hospital_id, **kw)
        return self.counts_by_bucket

    async def appointment_counts_by_doctor(self, hospital_id: uuid.UUID, **kw: Any) -> list[Any]:
        self._saw("appointment_counts_by_doctor", hospital_id, **kw)
        return []

    async def appointment_counts_by_department(
        self, hospital_id: uuid.UUID, **kw: Any
    ) -> list[Any]:
        self._saw("appointment_counts_by_department", hospital_id, **kw)
        return []

    async def doctor_schedule(self, hospital_id: uuid.UUID, **kw: Any) -> list[ScheduleRow]:
        self._saw("doctor_schedule", hospital_id, **kw)
        return self.schedule

    async def count_doctor_patients(self, hospital_id: uuid.UUID, **kw: Any) -> int:
        self._saw("count_doctor_patients", hospital_id, **kw)
        return self.doctor_patients

    async def walk_in_queue(self, hospital_id: uuid.UUID, **kw: Any) -> WalkInRow:
        self._saw("walk_in_queue", hospital_id, **kw)
        return self.walk_ins

    async def count_at_risk(self, hospital_id: uuid.UUID, **kw: Any) -> int:
        self._saw("count_at_risk", hospital_id, **kw)
        return len(self.at_risk)

    async def at_risk_appointments(self, hospital_id: uuid.UUID, **kw: Any) -> list[AtRiskRow]:
        self._saw("at_risk_appointments", hospital_id, **kw)
        return self.at_risk[: kw["limit"]]

    async def invoiced_totals(self, hospital_id: uuid.UUID, **kw: Any) -> MoneyTotal:
        self._saw("invoiced_totals", hospital_id, **kw)
        return self.invoiced

    async def invoiced_by_bucket(self, hospital_id: uuid.UUID, **kw: Any) -> dict[date, MoneyTotal]:
        self._saw("invoiced_by_bucket", hospital_id, **kw)
        return self.invoiced_buckets

    async def payment_totals_by_method(
        self, hospital_id: uuid.UUID, **kw: Any
    ) -> dict[str, MoneyTotal]:
        self._saw("payment_totals_by_method", hospital_id, **kw)
        return self.payments

    async def payments_by_bucket(self, hospital_id: uuid.UUID, **kw: Any) -> dict[date, MoneyTotal]:
        self._saw("payments_by_bucket", hospital_id, **kw)
        return self.payment_buckets

    async def refund_totals_by_method(
        self, hospital_id: uuid.UUID, **kw: Any
    ) -> dict[str, MoneyTotal]:
        self._saw("refund_totals_by_method", hospital_id, **kw)
        return self.refunds

    async def refunds_by_bucket(self, hospital_id: uuid.UUID, **kw: Any) -> dict[date, MoneyTotal]:
        self._saw("refunds_by_bucket", hospital_id, **kw)
        return self.refund_buckets

    async def outstanding_summary(self, hospital_id: uuid.UUID) -> OutstandingSummaryRow:
        self._saw("outstanding_summary", hospital_id)
        return self.outstanding

    async def outstanding_ageing(self, hospital_id: uuid.UUID, **kw: Any) -> dict[str, MoneyTotal]:
        self._saw("outstanding_ageing", hospital_id, **kw)
        return self.ageing

    async def outstanding_invoices(
        self, hospital_id: uuid.UUID, **kw: Any
    ) -> list[OutstandingInvoiceRow]:
        self._saw("outstanding_invoices", hospital_id, **kw)
        return self.invoices[: kw["limit"]]

    async def pending_discounts(self, hospital_id: uuid.UUID) -> MoneyTotal:
        self._saw("pending_discounts", hospital_id)
        return self.discounts

    async def doctor_ref(self, hospital_id: uuid.UUID, doctor_id: uuid.UUID) -> NamedRefRow | None:
        self._saw("doctor_ref", hospital_id, doctor_id=doctor_id)
        name = self.doctors.get(doctor_id)
        return None if name is None else NamedRefRow(id=doctor_id, name=name)

    async def department_ref(
        self, hospital_id: uuid.UUID, department_id: uuid.UUID
    ) -> NamedRefRow | None:
        self._saw("department_ref", hospital_id, department_id=department_id)
        name = self.departments.get(department_id)
        return None if name is None else NamedRefRow(id=department_id, name=name)


@dataclass
class _World:
    """A service with everything it talks to."""

    service: ReportService
    reports: FakeReports
    doctors: FakeDoctors
    session: FakeSession
    audit: RecordingAuditSink


def _world(
    *,
    hospital: _Hospital | None = None,
    missing_hospital: bool = False,
    profiles: dict[uuid.UUID, _Doctor] | None = None,
) -> _World:
    reports = FakeReports()
    doctors = FakeDoctors(profiles)
    session = FakeSession()
    audit = RecordingAuditSink()
    hospitals = FakeHospitals(None if missing_hospital else (hospital or _Hospital()))
    service = ReportService(
        reports,  # type: ignore[arg-type]
        hospitals,  # type: ignore[arg-type]
        doctors,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        audit,
    )
    return _World(service, reports, doctors, session, audit)


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs at :data:`NOW` unless it pins another instant."""
    monkeypatch.setattr(report_service, "utc_now", lambda: NOW)


def _at(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    monkeypatch.setattr(report_service, "utc_now", lambda: instant)


def _field_error(exc: pytest.ExceptionInfo[ValidationError]) -> tuple[str, str]:
    first = (exc.value.detail or {})["errors"][0]
    assert first["message"] == exc.value.message
    return first["field"], exc.value.message


def _json(model: Any) -> dict[str, Any]:
    """Serialise a DTO the way the API does."""
    dumped: dict[str, Any] = model.model_dump(mode="json", by_alias=True)
    return dumped


# ── The pure rate function ───────────────────────────────────────────────────


class TestNoShowRate:
    @pytest.mark.parametrize(
        ("no_show", "outcomes", "expected"),
        [
            (1, 4, "25.0"),
            (1, 13, "7.7"),
            (1, 8, "12.5"),
            (1, 16, "6.3"),  # 6.25 rounds half-up, not to even
            (3, 16, "18.8"),  # 18.75 likewise
            (2, 2, "100.0"),
            (0, 7, "0.0"),
        ],
    )
    def test_rounds_half_up_to_one_decimal(
        self, no_show: int, outcomes: int, expected: str
    ) -> None:
        assert no_show_rate_percent(no_show, outcomes) == expected

    def test_nothing_concluded_has_no_rate(self) -> None:
        assert no_show_rate_percent(0, 0) is None


# ── Zero-fill ────────────────────────────────────────────────────────────────


class TestEmptyHospital:
    async def test_patients_report_lists_every_bucket_with_zero(self) -> None:
        world = _world()

        report = await world.service.patients_report(
            HOSPITAL_ID, from_=date(2026, 10, 4), to=date(2026, 10, 6)
        )

        assert [(b.bucket_start, b.registered) for b in report.buckets] == [
            (date(2026, 10, 4), 0),
            (date(2026, 10, 5), 0),
            (date(2026, 10, 6), 0),
        ]
        assert report.summary.registered == 0
        assert _json(report.summary.by_gender) == {
            "male": 0,
            "female": 0,
            "other": 0,
            "unspecified": 0,
        }

    async def test_appointments_report_has_zeros_and_no_rate(self) -> None:
        world = _world()

        report = await world.service.appointments_report(
            HOSPITAL_ID, from_=date(2026, 10, 5), to=date(2026, 10, 6)
        )

        assert len(report.buckets) == 2
        assert all(_json(b)["total"] == 0 and _json(b)["no_show"] == 0 for b in report.buckets)
        assert report.summary.no_show_rate_percent is None
        assert report.summary.total == 0
        assert (report.by_doctor, report.by_department) == ([], [])
        assert (report.filters.doctor, report.filters.department) == (None, None)

    async def test_revenue_report_has_five_methods_and_zero_strings(self) -> None:
        world = _world()

        report = await world.service.revenue_report(
            HOSPITAL_ID, from_=date(2026, 10, 5), to=date(2026, 10, 6)
        )
        body = _json(report)

        assert [row["method"] for row in body["by_method"]] == list(METHODS)
        assert all(
            row["collected_amount"] == row["refunded_amount"] == row["net_collected_amount"]
            for row in body["by_method"]
        )
        assert body["by_method"][0]["collected_amount"] == "0.00"
        assert body["summary"] == {
            "invoice_count": 0,
            "invoiced_amount": "0.00",
            "payment_count": 0,
            "collected_amount": "0.00",
            "refund_count": 0,
            "refunded_amount": "0.00",
            "net_collected_amount": "0.00",
        }
        assert [b["invoiced_amount"] for b in body["buckets"]] == ["0.00", "0.00"]

    async def test_outstanding_report_has_four_ageing_rows(self) -> None:
        world = _world()

        body = _json(await world.service.outstanding_report(HOSPITAL_ID))

        assert body["ageing"] == [
            {"bucket": "0_30", "min_days": 0, "max_days": 30, "invoice_count": 0,
             "outstanding_amount": "0.00"},
            {"bucket": "31_60", "min_days": 31, "max_days": 60, "invoice_count": 0,
             "outstanding_amount": "0.00"},
            {"bucket": "61_90", "min_days": 61, "max_days": 90, "invoice_count": 0,
             "outstanding_amount": "0.00"},
            {"bucket": "over_90", "min_days": 91, "max_days": None, "invoice_count": 0,
             "outstanding_amount": "0.00"},
        ]  # fmt: skip
        assert body["invoices"] == []
        assert (body["invoices_total"], body["invoices_truncated"]) == (0, False)
        assert body["filters"] == {"as_of_date": "2026-10-06"}

    async def test_a_missing_hospital_is_not_found(self) -> None:
        world = _world(missing_hospital=True)

        with pytest.raises(NotFoundError) as exc:
            await world.service.revenue_report(HOSPITAL_ID)

        assert exc.value.message == "Hospital not found."
        assert world.reports.calls == []


# ── Shaping ──────────────────────────────────────────────────────────────────


class TestRevenueShaping:
    async def test_net_is_collected_minus_refunded_and_may_be_negative(self) -> None:
        world = _world()
        world.reports.invoiced = _money(3, "2000.00")
        world.reports.payments = _by_method(cash=_money(1, "500.00"), card=_money(1, "100.00"))
        world.reports.refunds = _by_method(card=_money(1, "450.00"))
        world.reports.payment_buckets = {date(2026, 10, 5): _money(2, "600.00")}
        world.reports.refund_buckets = {date(2026, 10, 6): _money(1, "350.00")}

        body = _json(
            await world.service.revenue_report(
                HOSPITAL_ID, from_=date(2026, 10, 5), to=date(2026, 10, 6)
            )
        )

        assert body["summary"]["collected_amount"] == "600.00"
        assert body["summary"]["refunded_amount"] == "450.00"
        assert body["summary"]["net_collected_amount"] == "150.00"
        assert (body["summary"]["payment_count"], body["summary"]["refund_count"]) == (2, 1)
        card = body["by_method"][1]
        assert (card["method"], card["net_collected_amount"]) == ("card", "-350.00")
        assert [b["net_collected_amount"] for b in body["buckets"]] == ["600.00", "-350.00"]

    async def test_small_amounts_add_exactly(self) -> None:
        world = _world()
        world.reports.payments = _by_method(
            cash=_money(1, "0.03"), card=_money(1, "0.03"), upi=_money(1, "0.04")
        )

        report = await world.service.revenue_report(HOSPITAL_ID)

        assert report.summary.collected_amount == Decimal("0.10")
        assert report.summary.collected_amount.as_tuple().exponent == -2

    async def test_the_summary_has_no_from_or_to(self) -> None:
        body = _json(await _world().service.revenue_report(HOSPITAL_ID))

        assert "from" not in body["summary"]
        assert body["filters"] == {"from": "2026-09-07", "to": "2026-10-06", "granularity": "day"}


class TestAppointmentShaping:
    async def test_the_rate_ignores_open_and_cancelled_appointments(self) -> None:
        world = _world()
        world.reports.counts = AppointmentCountsRow(
            total=14, booked=5, checked_in=3, in_progress=1, completed=3, cancelled=1, no_show=1
        )

        report = await world.service.appointments_report(HOSPITAL_ID)

        assert report.summary.no_show_rate_percent == "25.0"

        world.reports.counts = AppointmentCountsRow(
            total=24, booked=15, checked_in=3, in_progress=1, completed=3, cancelled=1, no_show=1
        )
        later = await world.service.appointments_report(HOSPITAL_ID)

        assert later.summary.no_show_rate_percent == "25.0"

    async def test_only_bookings_have_no_rate(self) -> None:
        world = _world()
        world.reports.counts = AppointmentCountsRow(total=5, booked=5)

        report = await world.service.appointments_report(HOSPITAL_ID)

        assert report.summary.no_show_rate_percent is None

    async def test_only_no_shows_is_a_hundred_percent(self) -> None:
        world = _world()
        world.reports.counts = AppointmentCountsRow(total=2, no_show=2)

        report = await world.service.appointments_report(HOSPITAL_ID)

        assert report.summary.no_show_rate_percent == "100.0"

    async def test_buckets_are_filled_from_the_repository_and_zero_elsewhere(self) -> None:
        world = _world()
        world.reports.counts_by_bucket = {
            date(2026, 10, 6): AppointmentCountsRow(total=2, booked=1, completed=1)
        }

        report = await world.service.appointments_report(
            HOSPITAL_ID, from_=date(2026, 10, 5), to=date(2026, 10, 6)
        )

        assert [(b.bucket_start, b.total, b.booked, b.completed) for b in report.buckets] == [
            (date(2026, 10, 5), 0, 0, 0),
            (date(2026, 10, 6), 2, 1, 1),
        ]

    async def test_filters_are_passed_down_and_echoed_with_names(self) -> None:
        world = _world()
        world.reports.doctors = {DOCTOR_ID: "Priya Sharma"}
        world.reports.departments = {DEPARTMENT_ID: "Cardiology"}

        report = await world.service.appointments_report(
            HOSPITAL_ID, doctor_id=DOCTOR_ID, department_id=DEPARTMENT_ID, granularity="week"
        )

        assert _json(report.filters) == {
            "from": "2026-09-07",
            "to": "2026-10-06",
            "granularity": "week",
            "doctor": {"id": str(DOCTOR_ID), "name": "Priya Sharma"},
            "department": {"id": str(DEPARTMENT_ID), "name": "Cardiology"},
        }
        for method in (
            "appointment_counts",
            "appointment_counts_by_bucket",
            "appointment_counts_by_doctor",
            "appointment_counts_by_department",
        ):
            (call,) = world.reports.named(method)
            assert (call["doctor_id"], call["department_id"]) == (DOCTOR_ID, DEPARTMENT_ID)

    async def test_an_unknown_doctor_is_a_422_on_doctor_id(self) -> None:
        world = _world()

        with pytest.raises(ValidationError) as exc:
            await world.service.appointments_report(HOSPITAL_ID, doctor_id=uuid.uuid4())

        assert _field_error(exc) == ("doctor_id", "Doctor not found.")
        assert world.reports.named("appointment_counts") == []

    async def test_an_unknown_department_is_a_422_on_department_id(self) -> None:
        world = _world()
        world.reports.doctors = {DOCTOR_ID: "Priya Sharma"}

        with pytest.raises(ValidationError) as exc:
            await world.service.appointments_report(
                HOSPITAL_ID, doctor_id=DOCTOR_ID, department_id=uuid.uuid4()
            )

        assert _field_error(exc) == ("department_id", "Department not found.")

    async def test_the_period_is_checked_before_the_doctor(self) -> None:
        world = _world()

        with pytest.raises(ValidationError) as exc:
            await world.service.appointments_report(
                HOSPITAL_ID, granularity="hour", doctor_id=uuid.uuid4()
            )

        assert _field_error(exc)[0] == "granularity"
        assert world.reports.named("doctor_ref") == []


class TestOutstandingShaping:
    @staticmethod
    def _invoice(issued_date: date) -> OutstandingInvoiceRow:
        return OutstandingInvoiceRow(
            invoice_id=uuid.uuid4(),
            invoice_number="INV-2026-000003",
            issued_at=datetime(issued_date.year, issued_date.month, issued_date.day, 5, tzinfo=UTC),
            issued_date=issued_date,
            patient_id=uuid.uuid4(),
            patient_name="Thomas George",
            patient_mrn="MRN-000002",
            status=InvoiceStatus.PARTIALLY_PAID,
            total=Decimal("750.00"),
            amount_paid=Decimal("300.00"),
            balance_due=Decimal("450.00"),
        )

    async def test_age_is_days_from_the_local_issue_date_to_today(self) -> None:
        world = _world()
        world.reports.invoices = [self._invoice(date(2026, 10, 5)), self._invoice(date(2026, 9, 5))]
        world.reports.outstanding = OutstandingSummaryRow(
            invoice_count=2, outstanding_amount=Decimal("900.00"), partially_paid_count=2
        )

        report = await world.service.outstanding_report(HOSPITAL_ID)

        assert [invoice.age_days for invoice in report.invoices] == [1, 31]
        assert report.invoices[0].status == "partially_paid"
        assert (report.invoices_total, report.invoices_truncated) == (2, False)

    async def test_the_screen_lists_a_hundred_and_says_when_there_are_more(self) -> None:
        world = _world()
        world.reports.invoices = [self._invoice(date(2026, 10, 5))] * 150
        world.reports.outstanding = OutstandingSummaryRow(
            invoice_count=150, outstanding_amount=Decimal("67500.00"), partially_paid_count=150
        )

        report = await world.service.outstanding_report(HOSPITAL_ID)

        assert world.reports.named("outstanding_invoices") == [{"tz": "Asia/Kolkata", "limit": 100}]
        assert len(report.invoices) == 100
        assert (report.invoices_total, report.invoices_truncated) == (150, True)
        assert report.summary.outstanding_amount == Decimal("67500.00")

    async def test_ageing_is_asked_for_as_of_the_local_today(self) -> None:
        world = _world()
        world.reports.ageing = {"31_60": _money(2, "1550.00")}

        report = await world.service.outstanding_report(HOSPITAL_ID)

        assert world.reports.named("outstanding_ageing") == [
            {"today": date(2026, 10, 6), "tz": "Asia/Kolkata"}
        ]
        assert [(row.bucket, row.invoice_count) for row in report.ageing] == [
            ("0_30", 0),
            ("31_60", 2),
            ("61_90", 0),
            ("over_90", 0),
        ]


# ── Dashboards ───────────────────────────────────────────────────────────────


class TestAdminDashboard:
    async def test_in_clinic_is_checked_in_plus_in_progress(self) -> None:
        world = _world()
        world.reports.counts = AppointmentCountsRow(total=5, booked=1, checked_in=3, in_progress=1)
        world.reports.registrations = {date(2026, 10, 1): 11}
        world.reports.active_patients = 40

        dashboard = await world.service.admin_dashboard(HOSPITAL_ID)
        body = _json(dashboard)

        assert body["appointments_today"] == {
            "date": "2026-10-06",
            "total": 5,
            "booked": 1,
            "checked_in": 3,
            "in_progress": 1,
            "completed": 0,
            "cancelled": 0,
            "no_show": 0,
            "in_clinic": 4,
        }
        assert body["patient_registrations_this_month"] == {
            "from": "2026-10-01",
            "to": "2026-10-06",
            "registered": 11,
            "active_total": 40,
        }

    async def test_today_is_the_local_day_not_the_utc_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 00:15 on 6 Oct in Kolkata is still 5 Oct in UTC.
        _at(monkeypatch, datetime(2026, 10, 5, 18, 45, tzinfo=UTC))
        world = _world()

        dashboard = await world.service.admin_dashboard(HOSPITAL_ID)

        assert dashboard.meta.today == date(2026, 10, 6)
        assert dashboard.appointments_today.date == date(2026, 10, 6)
        (today_call,) = world.reports.named("appointment_counts")
        assert (today_call["start"], today_call["end"]) == (
            datetime(2026, 10, 5, 18, 30, tzinfo=UTC),
            datetime(2026, 10, 6, 18, 30, tzinfo=UTC),
        )

    async def test_one_second_before_local_midnight_is_still_yesterday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _at(monkeypatch, datetime(2026, 10, 5, 18, 29, 59, tzinfo=UTC))

        dashboard = await _world().service.admin_dashboard(HOSPITAL_ID)

        assert dashboard.meta.today == date(2026, 10, 5)

    @pytest.mark.parametrize(
        ("instant", "today", "monday", "first"),
        [
            # A Monday: the week is that one day.
            (datetime(2026, 10, 5, 6, 0, tzinfo=UTC), date(2026, 10, 5), date(2026, 10, 5),
             date(2026, 10, 1)),
            # A Sunday: the week began six days earlier.
            (datetime(2026, 10, 11, 6, 0, tzinfo=UTC), date(2026, 10, 11), date(2026, 10, 5),
             date(2026, 10, 1)),
            # The 1st of a month (a Thursday): the month is that one day and
            # the week reaches back into September.
            (datetime(2026, 10, 1, 6, 0, tzinfo=UTC), date(2026, 10, 1), date(2026, 9, 28),
             date(2026, 10, 1)),
        ],
    )  # fmt: skip
    async def test_week_runs_from_monday_and_month_from_the_first(
        self,
        monkeypatch: pytest.MonkeyPatch,
        instant: datetime,
        today: date,
        monday: date,
        first: date,
    ) -> None:
        _at(monkeypatch, instant)
        world = _world()

        admin = await world.service.admin_dashboard(HOSPITAL_ID)
        billing = await world.service.billing_dashboard(HOSPITAL_ID)

        assert (admin.revenue_this_week.from_, admin.revenue_this_week.to) == (monday, today)
        month = admin.patient_registrations_this_month
        assert (month.from_, month.to) == (first, today)
        assert (billing.revenue.today.from_, billing.revenue.today.to) == (today, today)
        assert (billing.revenue.this_week.from_, billing.revenue.this_week.to) == (monday, today)
        assert (billing.revenue.this_month.from_, billing.revenue.this_month.to) == (first, today)


class TestBillingDashboard:
    async def test_tiles_carry_the_repository_figures(self) -> None:
        world = _world()
        world.reports.outstanding = OutstandingSummaryRow(
            invoice_count=2,
            outstanding_amount=Decimal("1550.00"),
            issued_count=1,
            partially_paid_count=1,
        )
        world.reports.discounts = _money(1, "300.00")
        world.reports.invoiced = _money(2, "1400.00")
        world.reports.payments = _by_method(upi=_money(2, "1400.00"))
        world.reports.refunds = _by_method(upi=_money(2, "950.00"))

        body = _json(await world.service.billing_dashboard(HOSPITAL_ID))

        assert body["unpaid_invoices"] == {
            "invoice_count": 2,
            "outstanding_amount": "1550.00",
            "issued_count": 1,
            "partially_paid_count": 1,
        }
        assert body["discounts_pending_approval"] == {
            "invoice_count": 1,
            "discount_amount": "300.00",
        }
        assert body["revenue"]["today"] == {
            "from": "2026-10-06",
            "to": "2026-10-06",
            "invoice_count": 2,
            "invoiced_amount": "1400.00",
            "payment_count": 2,
            "collected_amount": "1400.00",
            "refund_count": 2,
            "refunded_amount": "950.00",
            "net_collected_amount": "450.00",
        }

    async def test_each_revenue_range_is_asked_for_in_local_bounds(self) -> None:
        world = _world()

        await world.service.billing_dashboard(HOSPITAL_ID)

        asked = [(call["start"], call["end"]) for call in world.reports.named("invoiced_totals")]
        end = datetime(2026, 10, 6, 18, 30, tzinfo=UTC)
        assert asked == [
            (datetime(2026, 10, 5, 18, 30, tzinfo=UTC), end),  # today
            (datetime(2026, 10, 4, 18, 30, tzinfo=UTC), end),  # from Monday 5 Oct
            (datetime(2026, 9, 30, 18, 30, tzinfo=UTC), end),  # from 1 Oct
        ]


class TestReceptionDashboard:
    @staticmethod
    def _late(start: datetime) -> AtRiskRow:
        return AtRiskRow(
            appointment_id=uuid.uuid4(),
            scheduled_start=start,
            patient_id=uuid.uuid4(),
            patient_name="Sunita Verma",
            patient_mrn="MRN-000010",
            doctor_id=DOCTOR_ID,
            doctor_name="Arjun Nair",
        )

    async def test_tiles(self) -> None:
        world = _world()
        world.reports.counts = AppointmentCountsRow(
            total=6, booked=1, checked_in=3, in_progress=1, no_show=1
        )
        world.reports.walk_ins = WalkInRow(
            waiting=2,
            not_arrived=1,
            in_consultation=0,
            earliest_checked_in_at=datetime(2026, 10, 6, 7, 50, tzinfo=UTC),
        )
        world.reports.at_risk = [self._late(datetime(2026, 10, 6, 8, 15, tzinfo=UTC))]

        body = _json(await world.service.reception_dashboard(HOSPITAL_ID))

        assert body["schedule_today"]["in_clinic"] == 4
        assert body["walk_in_queue"] == {
            "waiting": 2,
            "not_arrived": 1,
            "in_consultation": 0,
            "longest_wait_minutes": 42,
        }
        assert body["no_show_alerts"]["marked_today"] == 1
        assert body["no_show_alerts"]["at_risk"] == 1
        (late,) = body["no_show_alerts"]["at_risk_appointments"]
        assert late["minutes_late"] == 17
        assert late["scheduled_start"] == "2026-10-06T08:15:00Z"

    async def test_nobody_waiting_has_no_longest_wait(self) -> None:
        dashboard = await _world().service.reception_dashboard(HOSPITAL_ID)

        assert dashboard.walk_in_queue.longest_wait_minutes is None
        assert dashboard.no_show_alerts.at_risk_appointments == []

    async def test_lateness_is_measured_from_the_truncated_clock(self) -> None:
        """08:32:11.48 minus 08:31:11.20 is over a minute; from 08:32:11 it is not."""
        world = _world()
        world.reports.at_risk = [self._late(datetime(2026, 10, 6, 8, 31, 11, 200000, tzinfo=UTC))]

        dashboard = await world.service.reception_dashboard(HOSPITAL_ID)

        assert dashboard.no_show_alerts.at_risk_appointments[0].minutes_late == 0
        (call,) = world.reports.named("count_at_risk")
        assert call["now"] == datetime(2026, 10, 6, 8, 32, 11, tzinfo=UTC)

    async def test_the_list_is_capped_at_twenty_but_the_count_is_not(self) -> None:
        world = _world()
        start = datetime(2026, 10, 6, 3, 0, tzinfo=UTC)
        world.reports.at_risk = [self._late(start + timedelta(minutes=i)) for i in range(25)]

        dashboard = await world.service.reception_dashboard(HOSPITAL_ID)

        assert dashboard.no_show_alerts.at_risk == 25
        assert len(dashboard.no_show_alerts.at_risk_appointments) == 20
        assert world.reports.named("at_risk_appointments")[0]["limit"] == 20


class TestDoctorDashboard:
    @staticmethod
    def _visit() -> ScheduleRow:
        return ScheduleRow(
            appointment_id=uuid.uuid4(),
            scheduled_start=datetime(2026, 10, 6, 8, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 10, 6, 8, 30, tzinfo=UTC),
            status=AppointmentStatus.CHECKED_IN,
            type=AppointmentType.FOLLOW_UP,
            patient_id=uuid.uuid4(),
            patient_name="Harish Pillai",
            patient_mrn="MRN-000007",
            checked_in_at=datetime(2026, 10, 6, 7, 50, tzinfo=UTC),
        )

    async def test_to_see_is_booked_plus_checked_in(self) -> None:
        world = _world(profiles={USER_ID: _Doctor(DOCTOR_ID)})
        world.reports.doctors = {DOCTOR_ID: "Priya Sharma"}
        world.reports.counts = AppointmentCountsRow(total=4, booked=2, checked_in=1, in_progress=1)
        world.reports.schedule = [self._visit()]
        world.reports.doctor_patients = 5

        body = _json(await world.service.doctor_dashboard(HOSPITAL_ID, USER_ID))

        assert body["doctor"] == {"id": str(DOCTOR_ID), "name": "Priya Sharma"}
        assert body["schedule_today"]["to_see"] == 3
        assert body["schedule_today"]["date"] == "2026-10-06"
        assert body["my_patients"] == {"count": 5}
        (visit,) = body["schedule_today"]["appointments"]
        assert (visit["status"], visit["type"], visit["patient_mrn"]) == (
            "checked_in",
            "follow_up",
            "MRN-000007",
        )
        assert "amount" not in str(body)

    async def test_the_week_is_monday_to_sunday_including_days_to_come(self) -> None:
        world = _world(profiles={USER_ID: _Doctor(DOCTOR_ID)})
        world.reports.doctors = {DOCTOR_ID: "Priya Sharma"}

        dashboard = await world.service.doctor_dashboard(HOSPITAL_ID, USER_ID)

        assert (dashboard.this_week.from_, dashboard.this_week.to) == (
            date(2026, 10, 5),
            date(2026, 10, 11),
        )
        today_call, week_call = world.reports.named("appointment_counts")
        assert (week_call["start"], week_call["end"]) == (
            datetime(2026, 10, 4, 18, 30, tzinfo=UTC),
            datetime(2026, 10, 11, 18, 30, tzinfo=UTC),
        )
        assert today_call["end"] - today_call["start"] == timedelta(days=1)

    async def test_every_query_is_scoped_to_the_callers_own_doctor_id(self) -> None:
        world = _world(profiles={USER_ID: _Doctor(DOCTOR_ID), uuid.uuid4(): _Doctor(uuid.uuid4())})
        world.reports.doctors = {DOCTOR_ID: "Priya Sharma"}

        await world.service.doctor_dashboard(HOSPITAL_ID, USER_ID)

        assert world.doctors.calls == [(HOSPITAL_ID, USER_ID, False)]
        scoped = [
            kwargs["doctor_id"] for name, kwargs in world.reports.calls if name != "doctor_ref"
        ]
        assert scoped == [DOCTOR_ID, DOCTOR_ID, DOCTOR_ID, DOCTOR_ID]
        assert {name for name, _ in world.reports.calls} == {
            "doctor_ref",
            "appointment_counts",
            "doctor_schedule",
            "count_doctor_patients",
        }

    async def test_someone_without_a_doctor_profile_is_not_found(self) -> None:
        world = _world()

        with pytest.raises(DoctorProfileRequiredError) as exc:
            await world.service.doctor_dashboard(HOSPITAL_ID, USER_ID)

        assert exc.value.status_code == 404
        assert exc.value.message == "No active doctor profile is linked to this account."
        assert world.reports.calls == []

    async def test_a_deactivated_doctor_profile_is_not_found_either(self) -> None:
        world = _world(profiles={USER_ID: _Doctor(DOCTOR_ID, deleted=True)})

        with pytest.raises(DoctorProfileRequiredError):
            await world.service.doctor_dashboard(HOSPITAL_ID, USER_ID)

        assert world.reports.calls == []


# ── Clock and timezone ───────────────────────────────────────────────────────


class TestClockAndZone:
    async def test_generated_at_is_the_clock_reading_to_whole_seconds(self) -> None:
        body = _json(await _world().service.revenue_report(HOSPITAL_ID))

        assert body["meta"] == {
            "hospital_name": "Demo Hospital",
            "timezone": "Asia/Kolkata",
            "currency": "INR",
            "today": "2026-10-06",
            "generated_at": "2026-10-06T08:32:11Z",
        }

    async def test_the_clock_is_read_once_per_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        readings: list[datetime] = []

        def ticking() -> datetime:
            readings.append(NOW + timedelta(seconds=len(readings)))
            return readings[-1]

        monkeypatch.setattr(report_service, "utc_now", ticking)
        world = _world()

        await world.service.reception_dashboard(HOSPITAL_ID)
        await world.service.export_report(HOSPITAL_ID, "revenue", actor_id=USER_ID)

        assert len(readings) == 2

    @pytest.mark.parametrize("stored", ["Not/AZone", "", "Asia/Kolkatta"])
    async def test_an_unknown_hospital_timezone_falls_back_to_utc(
        self, monkeypatch: pytest.MonkeyPatch, stored: str
    ) -> None:
        # 00:15 in Kolkata, 18:45 the day before in UTC: the fallback shows.
        _at(monkeypatch, datetime(2026, 10, 5, 18, 45, tzinfo=UTC))
        world = _world(hospital=_Hospital(timezone=stored))

        report = await world.service.patients_report(
            HOSPITAL_ID, from_=date(2026, 10, 5), to=date(2026, 10, 5)
        )

        assert report.meta.timezone == "UTC"
        assert report.meta.today == date(2026, 10, 5)
        (call,) = world.reports.named("patient_registrations")
        assert call["tz"] == "UTC"
        assert (call["start"], call["end"]) == (
            datetime(2026, 10, 5, tzinfo=UTC),
            datetime(2026, 10, 6, tzinfo=UTC),
        )

    async def test_a_dst_zone_asks_for_the_local_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _at(monkeypatch, datetime(2026, 3, 8, 16, 0, tzinfo=UTC))
        world = _world(hospital=_Hospital(timezone="America/New_York", currency="USD"))

        report = await world.service.revenue_report(
            HOSPITAL_ID, from_=date(2026, 3, 8), to=date(2026, 3, 8)
        )

        (call,) = world.reports.named("invoiced_totals")
        assert call["end"] - call["start"] == timedelta(hours=23)
        assert (report.meta.currency, report.meta.today) == ("USD", date(2026, 3, 8))


# ── Export ───────────────────────────────────────────────────────────────────


class TestExport:
    @pytest.mark.parametrize(
        ("requested", "message"),
        [
            ("pdf", "PDF export is not available yet. Use format=csv."),
            ("PDF", "PDF export is not available yet. Use format=csv."),
            ("xlsx", "Unsupported export format `xlsx`. Use format=csv."),
            ("", "Unsupported export format ``. Use format=csv."),
        ],
    )
    async def test_any_format_but_csv_is_refused_before_anything_is_read(
        self, requested: str, message: str
    ) -> None:
        world = _world()

        with pytest.raises(ValidationError) as exc:
            await world.service.export_report(
                HOSPITAL_ID, "revenue", actor_id=USER_ID, format=requested
            )

        assert _field_error(exc) == ("format", message)
        assert world.audit.events == []
        assert world.session.commits == 0
        assert world.reports.calls == []

    async def test_format_is_checked_before_the_period(self) -> None:
        world = _world()

        with pytest.raises(ValidationError) as exc:
            await world.service.export_report(
                HOSPITAL_ID, "revenue", actor_id=USER_ID, format="pdf", granularity="hour"
            )

        assert _field_error(exc)[0] == "format"

    async def test_an_unknown_report_is_not_found_and_leaves_no_trace(self) -> None:
        world = _world()

        with pytest.raises(NotFoundError) as exc:
            await world.service.export_report(HOSPITAL_ID, "inventory", actor_id=USER_ID)

        assert exc.value.message == "Unknown report: `inventory`."
        assert world.audit.events == []

    async def test_a_bad_period_leaves_no_audit_entry(self) -> None:
        world = _world()

        with pytest.raises(ValidationError):
            await world.service.export_report(
                HOSPITAL_ID, "patients", actor_id=USER_ID, granularity="hour"
            )

        assert world.audit.events == []
        assert world.session.commits == 0

    async def test_a_revenue_export_is_the_report_recorded_once_and_committed(self) -> None:
        world = _world()
        world.reports.invoiced = _money(1, "500.00")

        export = await world.service.export_report(
            HOSPITAL_ID,
            "revenue",
            actor_id=USER_ID,
            format="CSV",
            from_=date(2026, 10, 5),
            to=date(2026, 10, 6),
        )

        assert export.filename == "revenue-report-2026-10-05_to_2026-10-06-20261006-083211.csv"
        assert export.row_count == 2
        assert export.content.startswith("﻿Hospital,Demo Hospital\r\nReport,Revenue\r\n")
        assert "Generated at,2026-10-06T14:02:11+05:30\r\n" in export.content
        assert export.content.endswith("Total,,1,500.00,0,0.00,0,0.00,0.00\r\n")

        assert world.audit.actions() == ["report.exported"]
        event = world.audit.last()
        assert (event.hospital_id, event.actor_id) == (HOSPITAL_ID, USER_ID)
        assert (event.target_type, event.target_id) == ("report", None)
        assert event.changes == {}
        assert event.context == {
            "report_id": "revenue",
            "format": "csv",
            "from": "2026-10-05",
            "to": "2026-10-06",
            "granularity": "day",
            "row_count": 2,
        }
        assert world.session.commits == 1

    async def test_an_appointments_export_records_its_filters(self) -> None:
        world = _world()
        world.reports.doctors = {DOCTOR_ID: "Priya Sharma"}
        world.reports.departments = {DEPARTMENT_ID: "Cardiology"}

        export = await world.service.export_report(
            HOSPITAL_ID,
            "appointments",
            actor_id=USER_ID,
            granularity="month",
            doctor_id=DOCTOR_ID,
            department_id=DEPARTMENT_ID,
        )

        assert "Doctor,Priya Sharma\r\nDepartment,Cardiology\r\n" in export.content
        assert world.audit.last().context == {
            "report_id": "appointments",
            "format": "csv",
            "doctor_id": str(DOCTOR_ID),
            "department_id": str(DEPARTMENT_ID),
            "from": "2026-09-07",
            "to": "2026-10-06",
            "granularity": "month",
            "row_count": 2,
        }

    async def test_a_patients_export_defaults_like_the_report(self) -> None:
        world = _world()

        export = await world.service.export_report(HOSPITAL_ID, "patients", actor_id=USER_ID)

        assert export.filename == "patients-report-2026-09-07_to_2026-10-06-20261006-083211.csv"
        assert export.row_count == 30
        assert world.audit.last().context["row_count"] == 30

    async def test_an_outstanding_export_asks_for_ten_thousand_rows(self) -> None:
        world = _world()
        world.reports.invoices = [TestOutstandingShaping._invoice(date(2026, 10, 5))] * 3
        world.reports.outstanding = OutstandingSummaryRow(
            invoice_count=3, outstanding_amount=Decimal("1350.00"), partially_paid_count=3
        )

        export = await world.service.export_report(HOSPITAL_ID, "outstanding", actor_id=USER_ID)

        assert world.reports.named("outstanding_invoices") == [
            {"tz": "Asia/Kolkata", "limit": 10_000}
        ]
        assert export.filename == "outstanding-report-as-of-2026-10-06-20261006-083211.csv"
        assert export.row_count == 3
        assert "Rows,3\r\n" in export.content
        assert export.content.endswith("Total,,,,,,,,1350.00\r\n")
        assert world.audit.last().context == {
            "report_id": "outstanding",
            "format": "csv",
            "row_count": 3,
        }
