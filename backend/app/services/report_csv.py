"""CSV rendering for report exports.

Pure functions, no I/O: each takes the same DTO the JSON endpoint returns and
renders it as one file, so an export can never disagree with the screen it
was downloaded from (``docs/modules/10-reports-dashboard.md`` §4, rule 5).

File layout, the same for every report:

1. a header block of ``label,value`` rows saying what the file is,
2. one empty row,
3. the column header row (the JSON field names),
4. one row per bucket or invoice,
5. one ``Total`` row.

The text starts with a UTF-8 byte-order mark so Excel reads non-ASCII names,
and lines end ``\\r\\n``. Text a user typed goes through
:func:`~app.utils.csv.csv_safe`; dates, counts and money the server computed
are written as they are, so a negative amount stays a number.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.utils.csv import csv_safe

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime

    from app.schemas.report import (
        AppointmentCounts,
        AppointmentsReport,
        OutstandingReport,
        PatientsReport,
        ReportMeta,
        RevenueFigures,
        RevenueReport,
    )

__all__ = [
    "CSV_BOM",
    "FILENAME_STAMP_FORMAT",
    "REPORT_TITLES",
    "outstanding_filename",
    "period_filename",
    "render_appointments_csv",
    "render_outstanding_csv",
    "render_patients_csv",
    "render_revenue_csv",
]

#: Written first so spreadsheet applications read the file as UTF-8.
CSV_BOM = "﻿"

#: The ``Report`` line of each export's header block.
REPORT_TITLES: dict[str, str] = {
    "patients": "Patients registered",
    "appointments": "Appointments",
    "revenue": "Revenue",
    "outstanding": "Outstanding invoices",
}

#: UTC timestamp at the end of an export's filename.
FILENAME_STAMP_FORMAT = "%Y%m%d-%H%M%S"

_ALL = "All"

_APPOINTMENT_COLUMNS = (
    "total",
    "booked",
    "checked_in",
    "in_progress",
    "completed",
    "cancelled",
    "no_show",
)

_REVENUE_COLUMNS = (
    "invoice_count",
    "invoiced_amount",
    "payment_count",
    "collected_amount",
    "refund_count",
    "refunded_amount",
    "net_collected_amount",
)

_OUTSTANDING_COLUMNS = (
    "invoice_number",
    "issued_date",
    "age_days",
    "patient_mrn",
    "patient_name",
    "status",
    "total",
    "amount_paid",
    "balance_due",
)


def _generated_at(meta: ReportMeta) -> str:
    """Return the generation time in the hospital's zone, ISO 8601 with offset."""
    return meta.generated_at.astimezone(ZoneInfo(meta.timezone)).isoformat(timespec="seconds")


def _render(
    header: Sequence[tuple[str, str]],
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    total: Sequence[str],
) -> str:
    """Assemble one export file.

    :param header: The ``label,value`` rows of the header block, in order.
    :param columns: The column header row.
    :param rows: The data rows.
    :param total: The closing ``Total`` row.
    :returns: The file as text, starting with the byte-order mark.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL)
    writer.writerows(header)
    writer.writerow([])
    writer.writerow(columns)
    writer.writerows(rows)
    writer.writerow(total)
    return CSV_BOM + buffer.getvalue()


def _appointment_cells(counts: AppointmentCounts) -> list[str]:
    """Return a row's appointment counts in column order."""
    return [str(getattr(counts, column)) for column in _APPOINTMENT_COLUMNS]


def _revenue_cells(figures: RevenueFigures) -> list[str]:
    """Return a row's revenue figures in column order."""
    return [str(getattr(figures, column)) for column in _REVENUE_COLUMNS]


def render_patients_csv(report: PatientsReport) -> str:
    """Render the patients report: registrations per bucket.

    :param report: The report, as the JSON endpoint returns it.
    :returns: The CSV file as text.
    """
    meta, filters = report.meta, report.filters
    return _render(
        [
            ("Hospital", csv_safe(meta.hospital_name)),
            ("Report", REPORT_TITLES["patients"]),
            ("Period", f"{filters.from_.isoformat()} to {filters.to.isoformat()}"),
            ("Granularity", filters.granularity),
            ("Timezone", csv_safe(meta.timezone)),
            ("Generated at", _generated_at(meta)),
        ],
        ["bucket_start", "bucket_end", "registered"],
        [
            [bucket.bucket_start.isoformat(), bucket.bucket_end.isoformat(), str(bucket.registered)]
            for bucket in report.buckets
        ],
        ["Total", "", str(report.summary.registered)],
    )


def render_appointments_csv(report: AppointmentsReport) -> str:
    """Render the appointments report: status counts per bucket.

    :param report: The report, as the JSON endpoint returns it.
    :returns: The CSV file as text.
    """
    meta, filters = report.meta, report.filters
    doctor = csv_safe(filters.doctor.name) if filters.doctor is not None else _ALL
    department = csv_safe(filters.department.name) if filters.department is not None else _ALL
    return _render(
        [
            ("Hospital", csv_safe(meta.hospital_name)),
            ("Report", REPORT_TITLES["appointments"]),
            ("Period", f"{filters.from_.isoformat()} to {filters.to.isoformat()}"),
            ("Granularity", filters.granularity),
            ("Doctor", doctor),
            ("Department", department),
            ("Timezone", csv_safe(meta.timezone)),
            ("Generated at", _generated_at(meta)),
        ],
        ["bucket_start", "bucket_end", *_APPOINTMENT_COLUMNS],
        [
            [
                bucket.bucket_start.isoformat(),
                bucket.bucket_end.isoformat(),
                *_appointment_cells(bucket),
            ]
            for bucket in report.buckets
        ],
        ["Total", "", *_appointment_cells(report.summary)],
    )


def render_revenue_csv(report: RevenueReport) -> str:
    """Render the revenue report: billed, collected and refunded per bucket.

    :param report: The report, as the JSON endpoint returns it.
    :returns: The CSV file as text.
    """
    meta, filters = report.meta, report.filters
    return _render(
        [
            ("Hospital", csv_safe(meta.hospital_name)),
            ("Report", REPORT_TITLES["revenue"]),
            ("Period", f"{filters.from_.isoformat()} to {filters.to.isoformat()}"),
            ("Granularity", filters.granularity),
            ("Timezone", csv_safe(meta.timezone)),
            ("Currency", csv_safe(meta.currency)),
            ("Generated at", _generated_at(meta)),
        ],
        ["bucket_start", "bucket_end", *_REVENUE_COLUMNS],
        [
            [
                bucket.bucket_start.isoformat(),
                bucket.bucket_end.isoformat(),
                *_revenue_cells(bucket),
            ]
            for bucket in report.buckets
        ],
        ["Total", "", *_revenue_cells(report.summary)],
    )


def render_outstanding_csv(report: OutstandingReport) -> str:
    """Render the outstanding report: one row per unpaid invoice, oldest first.

    The ``Total`` row is what is owed on **all** unpaid invoices, which is more
    than the listed rows add up to when the list was cut short; the ``Rows``
    header line says so.

    :param report: The report, as the JSON endpoint returns it.
    :returns: The CSV file as text.
    """
    meta = report.meta
    listed = len(report.invoices)
    rows_line = (
        f"{listed} of {report.invoices_total} (truncated)"
        if report.invoices_truncated
        else str(listed)
    )
    return _render(
        [
            ("Hospital", csv_safe(meta.hospital_name)),
            ("Report", REPORT_TITLES["outstanding"]),
            ("As of", report.filters.as_of_date.isoformat()),
            ("Rows", rows_line),
            ("Timezone", csv_safe(meta.timezone)),
            ("Currency", csv_safe(meta.currency)),
            ("Generated at", _generated_at(meta)),
        ],
        _OUTSTANDING_COLUMNS,
        [
            [
                csv_safe(invoice.invoice_number),
                invoice.issued_date.isoformat(),
                str(invoice.age_days),
                csv_safe(invoice.patient_mrn),
                csv_safe(invoice.patient_name),
                invoice.status,
                str(invoice.total),
                str(invoice.amount_paid),
                str(invoice.balance_due),
            ]
            for invoice in report.invoices
        ],
        ["Total", "", "", "", "", "", "", "", str(report.summary.outstanding_amount)],
    )


def period_filename(report_id: str, from_: date, to: date, now: datetime) -> str:
    """Name the export of a period report.

    :param report_id: ``patients``, ``appointments`` or ``revenue``.
    :param from_: First date of the period.
    :param to: Last date of the period.
    :param now: The request's single timestamp (UTC).
    :returns: E.g. ``revenue-report-2026-10-05_to_2026-10-06-20261006-083211.csv``.
    """
    stamp = now.strftime(FILENAME_STAMP_FORMAT)
    return f"{report_id}-report-{from_.isoformat()}_to_{to.isoformat()}-{stamp}.csv"


def outstanding_filename(as_of: date, now: datetime) -> str:
    """Name the export of the outstanding report.

    :param as_of: The date balances are aged against.
    :param now: The request's single timestamp (UTC).
    :returns: E.g. ``outstanding-report-as-of-2026-10-06-20261006-083211.csv``.
    """
    stamp = now.strftime(FILENAME_STAMP_FORMAT)
    return f"outstanding-report-as-of-{as_of.isoformat()}-{stamp}.csv"
