"""Unit tests for report CSV rendering. No database."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from app.schemas.report import (
    AgeingBucket,
    AppointmentBucket,
    AppointmentsFilters,
    AppointmentsReport,
    AppointmentsSummary,
    NamedRef,
    OutstandingFilters,
    OutstandingInvoice,
    OutstandingReport,
    OutstandingSummary,
    PatientsBucket,
    PatientsByGender,
    PatientsReport,
    PatientsSummary,
    PeriodFilters,
    ReportMeta,
    RevenueBucket,
    RevenueFigures,
    RevenueReport,
)
from app.services.report_csv import (
    CSV_BOM,
    outstanding_filename,
    period_filename,
    render_appointments_csv,
    render_outstanding_csv,
    render_patients_csv,
    render_revenue_csv,
)
from app.utils.csv import csv_safe

NOW = datetime(2026, 10, 6, 8, 32, 11, tzinfo=UTC)


def _meta(hospital_name: str = "Demo Hospital") -> ReportMeta:
    return ReportMeta(
        hospital_name=hospital_name,
        timezone="Asia/Kolkata",
        currency="INR",
        today=date(2026, 10, 6),
        generated_at=NOW,
    )


def _figures(*values: object) -> dict[str, object]:
    keys = (
        "invoice_count",
        "invoiced_amount",
        "payment_count",
        "collected_amount",
        "refund_count",
        "refunded_amount",
        "net_collected_amount",
    )
    return {
        key: Decimal(value) if isinstance(value, str) else value
        for key, value in zip(keys, values, strict=True)
    }


def _revenue_report(hospital_name: str = "Demo Hospital") -> RevenueReport:
    return RevenueReport(
        meta=_meta(hospital_name),
        filters=PeriodFilters(from_=date(2026, 10, 5), to=date(2026, 10, 6), granularity="day"),
        summary=RevenueFigures.model_validate(
            _figures(5, "4350.00", 5, "2800.00", 2, "950.00", "1850.00")
        ),
        by_method=[],
        buckets=[
            RevenueBucket.model_validate(
                {
                    "bucket_start": date(2026, 10, 5),
                    "bucket_end": date(2026, 10, 5),
                    "partial": False,
                    **_figures(3, "2950.00", 3, "1400.00", 0, "0.00", "1400.00"),
                }
            ),
            RevenueBucket.model_validate(
                {
                    "bucket_start": date(2026, 10, 6),
                    "bucket_end": date(2026, 10, 6),
                    "partial": False,
                    **_figures(2, "1400.00", 2, "1400.00", 2, "950.00", "450.00"),
                }
            ),
        ],
    )


def _outstanding_report(
    *, patient_name: str = "Thomas George", total: int = 1, truncated: bool = False
) -> OutstandingReport:
    return OutstandingReport(
        meta=_meta(),
        filters=OutstandingFilters(as_of_date=date(2026, 10, 6)),
        summary=OutstandingSummary(
            invoice_count=total,
            outstanding_amount=Decimal("450.00"),
            issued_count=0,
            partially_paid_count=total,
        ),
        ageing=[
            AgeingBucket(
                bucket="0_30",
                min_days=0,
                max_days=30,
                invoice_count=total,
                outstanding_amount=Decimal("450.00"),
            )
        ],
        invoices=[
            OutstandingInvoice(
                invoice_id=uuid.uuid4(),
                invoice_number="INV-2026-000003",
                issued_at=datetime(2026, 10, 5, 5, 10, tzinfo=UTC),
                issued_date=date(2026, 10, 5),
                age_days=1,
                patient_id=uuid.uuid4(),
                patient_name=patient_name,
                patient_mrn="MRN-000002",
                status="partially_paid",
                total=Decimal("750.00"),
                amount_paid=Decimal("300.00"),
                balance_due=Decimal("450.00"),
            )
        ],
        invoices_total=total,
        invoices_truncated=truncated,
    )


def _rows(text: str) -> list[list[str]]:
    """Parse a rendered file, without its byte-order mark."""
    assert text.startswith(CSV_BOM)
    return list(csv.reader(io.StringIO(text.removeprefix(CSV_BOM))))


class TestRevenueFile:
    def test_the_file_is_exactly_the_documented_example(self) -> None:
        text = render_revenue_csv(_revenue_report())

        expected = "\r\n".join(
            [
                "Hospital,Demo Hospital",
                "Report,Revenue",
                "Period,2026-10-05 to 2026-10-06",
                "Granularity,day",
                "Timezone,Asia/Kolkata",
                "Currency,INR",
                "Generated at,2026-10-06T14:02:11+05:30",
                "",
                "bucket_start,bucket_end,invoice_count,invoiced_amount,payment_count,"
                "collected_amount,refund_count,refunded_amount,net_collected_amount",
                "2026-10-05,2026-10-05,3,2950.00,3,1400.00,0,0.00,1400.00",
                "2026-10-06,2026-10-06,2,1400.00,2,1400.00,2,950.00,450.00",
                "Total,,5,4350.00,5,2800.00,2,950.00,1850.00",
                "",
            ]
        )
        assert text == CSV_BOM + expected

    def test_it_starts_with_a_byte_order_mark_and_uses_crlf(self) -> None:
        text = render_revenue_csv(_revenue_report())

        assert text.encode("utf-8").startswith(b"\xef\xbb\xbf")
        assert "\r\n" in text
        assert "\n" not in text.replace("\r\n", "")

    def test_a_negative_net_stays_a_number(self) -> None:
        report = _revenue_report()
        report.buckets[1].net_collected_amount = Decimal("-350.00")
        report.summary.net_collected_amount = Decimal("-350.00")

        rows = _rows(render_revenue_csv(report))

        assert rows[-2][-1] == "-350.00"
        assert rows[-1][-1] == "-350.00"

    def test_a_hospital_name_that_is_a_formula_is_neutralised(self) -> None:
        rows = _rows(render_revenue_csv(_revenue_report('=HYPERLINK("x")')))

        assert rows[0] == ["Hospital", '\'=HYPERLINK("x")']

    def test_a_name_with_a_comma_or_non_ascii_text_survives_a_round_trip(self) -> None:
        rows = _rows(render_revenue_csv(_revenue_report("Sañjīvanī Clinic, Pune")))

        assert rows[0] == ["Hospital", "Sañjīvanī Clinic, Pune"]


class TestPatientsFile:
    def test_header_block_columns_and_total(self) -> None:
        report = PatientsReport(
            meta=_meta(),
            filters=PeriodFilters(
                from_=date(2026, 9, 30), to=date(2026, 10, 6), granularity="week"
            ),
            summary=PatientsSummary(
                registered=11,
                active_total=40,
                by_gender=PatientsByGender(male=6, female=5, other=0, unspecified=0),
            ),
            buckets=[
                PatientsBucket(
                    bucket_start=date(2026, 9, 28),
                    bucket_end=date(2026, 10, 4),
                    partial=True,
                    registered=4,
                ),
                PatientsBucket(
                    bucket_start=date(2026, 10, 5),
                    bucket_end=date(2026, 10, 11),
                    partial=True,
                    registered=7,
                ),
            ],
        )

        rows = _rows(render_patients_csv(report))

        assert rows == [
            ["Hospital", "Demo Hospital"],
            ["Report", "Patients registered"],
            ["Period", "2026-09-30 to 2026-10-06"],
            ["Granularity", "week"],
            ["Timezone", "Asia/Kolkata"],
            ["Generated at", "2026-10-06T14:02:11+05:30"],
            [],
            ["bucket_start", "bucket_end", "registered"],
            ["2026-09-28", "2026-10-04", "4"],
            ["2026-10-05", "2026-10-11", "7"],
            ["Total", "", "11"],
        ]


class TestAppointmentsFile:
    @staticmethod
    def _report(doctor: NamedRef | None, department: NamedRef | None) -> AppointmentsReport:
        counts = {
            "total": 5,
            "booked": 0,
            "checked_in": 0,
            "in_progress": 0,
            "completed": 3,
            "cancelled": 1,
            "no_show": 1,
        }
        return AppointmentsReport(
            meta=_meta(),
            filters=AppointmentsFilters(
                from_=date(2026, 10, 5),
                to=date(2026, 10, 5),
                granularity="day",
                doctor=doctor,
                department=department,
            ),
            summary=AppointmentsSummary(no_show_rate_percent="25.0", **counts),
            buckets=[
                AppointmentBucket(
                    bucket_start=date(2026, 10, 5),
                    bucket_end=date(2026, 10, 5),
                    partial=False,
                    **counts,
                )
            ],
            by_doctor=[],
            by_department=[],
        )

    def test_unset_filters_read_all(self) -> None:
        rows = _rows(render_appointments_csv(self._report(None, None)))

        assert rows == [
            ["Hospital", "Demo Hospital"],
            ["Report", "Appointments"],
            ["Period", "2026-10-05 to 2026-10-05"],
            ["Granularity", "day"],
            ["Doctor", "All"],
            ["Department", "All"],
            ["Timezone", "Asia/Kolkata"],
            ["Generated at", "2026-10-06T14:02:11+05:30"],
            [],
            [
                "bucket_start",
                "bucket_end",
                "total",
                "booked",
                "checked_in",
                "in_progress",
                "completed",
                "cancelled",
                "no_show",
            ],
            ["2026-10-05", "2026-10-05", "5", "0", "0", "0", "3", "1", "1"],
            ["Total", "", "5", "0", "0", "0", "3", "1", "1"],
        ]

    def test_set_filters_show_names_and_are_neutralised(self) -> None:
        doctor = NamedRef(id=uuid.uuid4(), name="Priya Sharma")
        department = NamedRef(id=uuid.uuid4(), name="@Cardiology")

        rows = _rows(render_appointments_csv(self._report(doctor, department)))

        assert rows[4] == ["Doctor", "Priya Sharma"]
        assert rows[5] == ["Department", "'@Cardiology"]

    def test_the_file_carries_no_rate_and_no_breakdown(self) -> None:
        text = render_appointments_csv(self._report(None, None))

        assert "25.0" not in text
        assert "no_show_rate_percent" not in text


class TestOutstandingFile:
    def test_header_block_columns_and_total(self) -> None:
        rows = _rows(render_outstanding_csv(_outstanding_report()))

        assert rows == [
            ["Hospital", "Demo Hospital"],
            ["Report", "Outstanding invoices"],
            ["As of", "2026-10-06"],
            ["Rows", "1"],
            ["Timezone", "Asia/Kolkata"],
            ["Currency", "INR"],
            ["Generated at", "2026-10-06T14:02:11+05:30"],
            [],
            [
                "invoice_number",
                "issued_date",
                "age_days",
                "patient_mrn",
                "patient_name",
                "status",
                "total",
                "amount_paid",
                "balance_due",
            ],
            [
                "INV-2026-000003",
                "2026-10-05",
                "1",
                "MRN-000002",
                "Thomas George",
                "partially_paid",
                "750.00",
                "300.00",
                "450.00",
            ],
            ["Total", "", "", "", "", "", "", "", "450.00"],
        ]

    def test_a_patient_name_that_is_a_formula_is_neutralised(self) -> None:
        rows = _rows(render_outstanding_csv(_outstanding_report(patient_name="+SUM(A1)")))

        assert rows[9][4] == "'+SUM(A1)"

    def test_a_truncated_list_says_so_on_the_rows_line(self) -> None:
        report = _outstanding_report(total=10001, truncated=True)
        report.invoices = report.invoices * 10000

        rows = _rows(render_outstanding_csv(report))

        assert rows[3] == ["Rows", "10000 of 10001 (truncated)"]
        assert len(rows) == 9 + 10000 + 1


class TestFilenames:
    def test_period_report(self) -> None:
        name = period_filename("revenue", date(2026, 10, 5), date(2026, 10, 6), NOW)

        assert name == "revenue-report-2026-10-05_to_2026-10-06-20261006-083211.csv"

    def test_outstanding_report(self) -> None:
        name = outstanding_filename(date(2026, 10, 6), NOW)

        assert name == "outstanding-report-as-of-2026-10-06-20261006-083211.csv"


class TestCsvSafe:
    def test_every_formula_lead_character_is_prefixed(self) -> None:
        for lead in ("=", "+", "-", "@", "\t", "\r"):
            assert csv_safe(f"{lead}cmd") == f"'{lead}cmd"

    def test_ordinary_text_is_untouched(self) -> None:
        assert csv_safe("Asha Menon") == "Asha Menon"
        assert csv_safe("") == ""
        assert csv_safe("a=b") == "a=b"
