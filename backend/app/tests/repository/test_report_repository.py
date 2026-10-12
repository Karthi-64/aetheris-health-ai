"""Repository tests for :class:`~app.repositories.report_repository.ReportRepository`.

Real PostgreSQL, every figure checked against a hand-computed fixture.

The fixtures pin every event to an explicit UTC instant, chosen around the
hospital's local midnight (the test hospitals are in ``Asia/Kolkata``, UTC+5:30,
so local midnight is 18:30 UTC). Nothing reads the wall clock: the suite gives
the same answers at any time of day.

Two things are proven for every method:

- **Tenancy.** A second hospital holds its own, different, non-zero data. The
  first hospital's figures are unchanged by it, and a hospital with no data
  gets zeros.
- **Bucketing runs.** Each bucketed method is executed at ``day``, ``week`` and
  ``month``. A bucket expression built twice is a database error, not a wrong
  number, so only a database test can catch it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from app.models.appointment import AppointmentStatus, AppointmentType
from app.models.billing import InvoiceStatus, PaymentMethod
from app.models.patient import Gender
from app.repositories.report_repository import (
    AGEING_BUCKETS,
    AppointmentCountsRow,
    MoneyTotal,
    OutstandingSummaryRow,
    ReportRepository,
    WalkInRow,
)
from app.tests.report_helpers import (
    REGISTERED_LONG_AGO,
    Clinic,
    insert_named_doctor,
    insert_report_appointment,
    insert_report_invoice,
    insert_report_patient,
    seed_billing,
    seed_clinic,
    seed_other_billing,
    seed_other_clinic,
    seed_reception_desk,
    seed_registrations,
    utc,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

TZ = "Asia/Kolkata"

#: Local midnights, as UTC instants.
SEP_30 = utc("2026-09-29T18:30")
OCT_01 = utc("2026-09-30T18:30")
OCT_04 = utc("2026-10-03T18:30")
OCT_05 = utc("2026-10-04T18:30")
OCT_06 = utc("2026-10-05T18:30")
OCT_07 = utc("2026-10-06T18:30")

GRANULARITIES = ("day", "week", "month")


def money(count: int, amount: str) -> MoneyTotal:
    """A total, with the amount given as text so it is exact."""
    return MoneyTotal(count=count, amount=Decimal(amount))


def assert_exact(value: Decimal, expected: str) -> None:
    """Assert a returned amount is that exact ``Decimal``, to two places."""
    assert isinstance(value, Decimal), f"{value!r} is {type(value).__name__}, not Decimal"
    assert value == Decimal(expected)
    assert value.as_tuple().exponent == -2, f"{value!r} is not quantized to cents"


def by_method(**totals: MoneyTotal) -> dict[str, MoneyTotal]:
    """Every payment method, zero unless given."""
    return {method.value: totals.get(method.value, MoneyTotal()) for method in PaymentMethod}


@pytest.fixture
def repo(db_session: AsyncSession) -> ReportRepository:
    """The repository under test."""
    return ReportRepository(db_session)


# ── Fixture F: billing ───────────────────────────────────────────────────────


@pytest.fixture
async def billing(
    db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    """Fixture F in the hospital under test, with another hospital also holding data."""
    ids = await seed_billing(db_session, hospital_id)
    await seed_other_billing(db_session, other_hospital_id)
    return ids


class TestInvoiced:
    async def test_total_over_the_first_six_days_of_october(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        total = await repo.invoiced_totals(hospital_id, start=OCT_01, end=OCT_07)

        assert total.count == 6
        assert_exact(total.amount, "4350.10")

    async def test_an_invoice_issued_just_after_local_midnight_lands_on_the_new_day(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        """I1 was issued at 19:00 UTC on 30 Sep, which is 00:30 on 1 Oct locally."""
        by_day = await repo.invoiced_by_bucket(
            hospital_id, start=SEP_30, end=OCT_07, tz=TZ, granularity="day"
        )

        assert by_day == {
            date(2026, 10, 1): money(1, "1100.00"),
            date(2026, 10, 5): money(3, "1850.10"),
            date(2026, 10, 6): money(2, "1400.00"),
        }
        assert date(2026, 9, 30) not in by_day
        assert (await repo.invoiced_totals(hospital_id, start=SEP_30, end=OCT_01)) == money(
            0, "0.00"
        )

    async def test_void_draft_and_deleted_invoices_are_not_billed(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        # 5 Oct holds I2 (void, 500.00) and I10 (deleted, 999.00) as well.
        total = await repo.invoiced_totals(hospital_id, start=OCT_05, end=OCT_06)

        assert total == money(3, "1850.10")

    async def test_a_refunded_invoice_is_still_billed(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        total = await repo.invoiced_totals(hospital_id, start=OCT_06, end=OCT_07)

        assert total == money(2, "1400.00")


class TestCollected:
    async def test_totals_by_method_over_the_first_six_days_of_october(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        totals = await repo.payment_totals_by_method(hospital_id, start=OCT_01, end=OCT_07)

        assert totals == by_method(
            cash=money(1, "0.03"),
            card=money(2, "1100.00"),
            upi=money(3, "1200.03"),
            bank_transfer=money(1, "0.04"),
        )
        assert sum(total.count for total in totals.values()) == 7
        assert_exact(sum((t.amount for t in totals.values()), Decimal("0.00")), "2300.10")
        for total in totals.values():
            assert_exact(total.amount, str(total.amount))

    async def test_a_payment_one_second_before_local_midnight_belongs_to_the_day_before(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        september = await repo.payment_totals_by_method(hospital_id, start=SEP_30, end=OCT_01)
        by_day = await repo.payments_by_bucket(
            hospital_id, start=SEP_30, end=OCT_07, tz=TZ, granularity="day"
        )

        assert september == by_method(cash=money(1, "500.00"))
        assert by_day == {
            date(2026, 9, 30): money(1, "500.00"),
            date(2026, 10, 1): money(1, "600.00"),
            date(2026, 10, 5): money(4, "300.10"),
            date(2026, 10, 6): money(2, "1400.00"),
        }

    async def test_three_small_payments_add_to_exactly_ten_paise(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        by_day = await repo.payments_by_bucket(
            hospital_id, start=OCT_05, end=OCT_06, tz=TZ, granularity="day"
        )

        # 300.00 + 0.03 + 0.03 + 0.04, not 300.09999999999997.
        assert_exact(by_day[date(2026, 10, 5)].amount, "300.10")

    async def test_month_granularity_splits_september_from_october(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        by_month = await repo.payments_by_bucket(
            hospital_id, start=SEP_30, end=OCT_07, tz=TZ, granularity="month"
        )

        assert by_month == {
            date(2026, 9, 1): money(1, "500.00"),
            date(2026, 10, 1): money(7, "2300.10"),
        }

    async def test_week_granularity_starts_on_monday(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        by_week = await repo.payments_by_bucket(
            hospital_id, start=SEP_30, end=OCT_07, tz=TZ, granularity="week"
        )

        assert by_week == {
            date(2026, 9, 28): money(2, "1100.00"),
            date(2026, 10, 5): money(6, "1700.10"),
        }
        assert all(day.weekday() == 0 for day in by_week)


class TestRefunded:
    async def test_totals_by_method_and_by_day(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        totals = await repo.refund_totals_by_method(hospital_id, start=OCT_01, end=OCT_07)
        by_day = await repo.refunds_by_bucket(
            hospital_id, start=OCT_01, end=OCT_07, tz=TZ, granularity="day"
        )

        assert totals == by_method(card=money(1, "350.00"), upi=money(1, "600.00"))
        assert by_day == {date(2026, 10, 6): money(2, "950.00")}
        assert_exact(by_day[date(2026, 10, 6)].amount, "950.00")

    async def test_nothing_was_refunded_on_the_fifth(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        totals = await repo.refund_totals_by_method(hospital_id, start=OCT_05, end=OCT_06)

        assert totals == by_method()
        assert_exact(totals["cash"].amount, "0.00")


class TestOutstanding:
    async def test_summary_counts_issued_and_part_paid_only(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        summary = await repo.outstanding_summary(hospital_id)

        # I3 owes 750.00 - 300.00 = 450.00; I4 owes 1100.00.
        assert summary == OutstandingSummaryRow(
            invoice_count=2,
            outstanding_amount=Decimal("1550.00"),
            issued_count=1,
            partially_paid_count=1,
        )
        assert_exact(summary.outstanding_amount, "1550.00")

    async def test_a_refunded_invoice_with_less_paid_than_billed_is_not_outstanding(
        self,
        repo: ReportRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        billing: dict[str, uuid.UUID],
    ) -> None:
        patient = await insert_report_patient(
            db_session, hospital_id, created_at=REGISTERED_LONG_AGO
        )
        await insert_report_invoice(
            db_session,
            hospital_id,
            patient_id=patient.id,
            status=InvoiceStatus.REFUNDED,
            total="900.00",
            issued_at=utc("2026-10-05T05:00"),
            amount_paid="400.00",
            amount_refunded="400.00",
        )

        summary = await repo.outstanding_summary(hospital_id)

        assert (summary.invoice_count, summary.outstanding_amount) == (2, Decimal("1550.00"))

    async def test_invoices_are_listed_oldest_first_with_the_local_issue_date(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        rows = await repo.outstanding_invoices(hospital_id, tz=TZ, limit=100)

        assert [row.invoice_id for row in rows] == [billing["I3"], billing["I4"]]
        first, second = rows
        assert (first.status, second.status) == (
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.ISSUED,
        )
        assert first.issued_at == utc("2026-10-05T05:10")
        assert first.issued_date == date(2026, 10, 5)
        assert (first.patient_name, first.invoice_number[:6]) == ("Ananya Rao", "INV-T-")
        assert first.patient_mrn.startswith("MRN-")
        for value, expected in (
            (first.total, "750.00"),
            (first.amount_paid, "300.00"),
            (first.balance_due, "450.00"),
            (second.total, "1100.00"),
            (second.amount_paid, "0.00"),
            (second.balance_due, "1100.00"),
        ):
            assert_exact(value, expected)

    async def test_the_list_respects_its_limit(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        rows = await repo.outstanding_invoices(hospital_id, tz=TZ, limit=1)

        assert [row.invoice_id for row in rows] == [billing["I3"]]

    async def test_an_invoice_of_a_deactivated_patient_is_still_listed(
        self, repo: ReportRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        gone = await insert_report_patient(
            db_session, hospital_id, created_at=REGISTERED_LONG_AGO, first_name="Gone", deleted=True
        )
        await insert_report_invoice(
            db_session,
            hospital_id,
            patient_id=gone.id,
            status=InvoiceStatus.ISSUED,
            total="10.00",
            issued_at=utc("2026-10-05T05:00"),
        )

        rows = await repo.outstanding_invoices(hospital_id, tz=TZ, limit=10)

        assert [row.patient_name for row in rows] == ["Gone Rao"]

    async def test_pending_discounts(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        pending = await repo.pending_discounts(hospital_id)

        assert pending == money(1, "300.00")
        assert_exact(pending.amount, "300.00")


class TestAgeing:
    async def test_both_unpaid_invoices_are_a_day_old_on_the_sixth(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        ageing = await repo.outstanding_ageing(hospital_id, today=date(2026, 10, 6), tz=TZ)

        assert ageing == {
            "0_30": money(2, "1550.00"),
            "31_60": money(0, "0.00"),
            "61_90": money(0, "0.00"),
            "over_90": money(0, "0.00"),
        }
        assert list(ageing) == [key for key, _, _ in AGEING_BUCKETS]

    async def test_thirty_one_days_later_they_move_to_the_next_band(
        self, repo: ReportRepository, hospital_id: uuid.UUID, billing: dict[str, uuid.UUID]
    ) -> None:
        ageing = await repo.outstanding_ageing(hospital_id, today=date(2026, 11, 5), tz=TZ)

        assert ageing["0_30"] == money(0, "0.00")
        assert ageing["31_60"] == money(2, "1550.00")

    @pytest.mark.parametrize(
        ("age_days", "band"),
        [
            (0, "0_30"),
            (30, "0_30"),
            (31, "31_60"),
            (60, "31_60"),
            (61, "61_90"),
            (90, "61_90"),
            (91, "over_90"),
            (400, "over_90"),
        ],
    )
    async def test_band_boundaries(
        self,
        repo: ReportRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        age_days: int,
        band: str,
    ) -> None:
        patient = await insert_report_patient(
            db_session, hospital_id, created_at=REGISTERED_LONG_AGO
        )
        await insert_report_invoice(
            db_session,
            hospital_id,
            patient_id=patient.id,
            status=InvoiceStatus.ISSUED,
            total="123.45",
            issued_at=utc("2026-06-01T06:00"),  # 1 Jun, local
        )

        ageing = await repo.outstanding_ageing(
            hospital_id, today=date(2026, 6, 1) + timedelta(days=age_days), tz=TZ
        )

        assert {key: total.count for key, total in ageing.items() if total.count} == {band: 1}
        assert_exact(ageing[band].amount, "123.45")

    async def test_age_is_counted_from_the_local_issue_date(
        self, repo: ReportRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Issued 18:45 UTC on 5 Oct is 6 Oct locally: 30 days old on 5 Nov, not 31."""
        patient = await insert_report_patient(
            db_session, hospital_id, created_at=REGISTERED_LONG_AGO
        )
        await insert_report_invoice(
            db_session,
            hospital_id,
            patient_id=patient.id,
            status=InvoiceStatus.ISSUED,
            total="10.00",
            issued_at=utc("2026-10-05T18:45"),
        )

        local = await repo.outstanding_ageing(hospital_id, today=date(2026, 11, 5), tz=TZ)
        in_utc = await repo.outstanding_ageing(hospital_id, today=date(2026, 11, 5), tz="UTC")
        (row,) = await repo.outstanding_invoices(hospital_id, tz=TZ, limit=10)

        assert local["0_30"].count == 1
        assert in_utc["31_60"].count == 1
        assert row.issued_date == date(2026, 10, 6)


# ── Appointments ─────────────────────────────────────────────────────────────


@pytest.fixture
async def clinic(
    db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
) -> Clinic:
    """The appointment fixture, with another hospital also holding appointments."""
    built = await seed_clinic(db_session, hospital_id)
    await seed_other_clinic(db_session, other_hospital_id)
    return built


class TestAppointmentCounts:
    async def test_all_six_statuses_over_two_local_days(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        counts = await repo.appointment_counts(hospital_id, start=OCT_05, end=OCT_07)

        assert counts == AppointmentCountsRow(
            total=7, booked=1, checked_in=1, in_progress=1, completed=2, cancelled=1, no_show=1
        )

    async def test_absent_statuses_are_zero_not_missing(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        sunday = await repo.appointment_counts(hospital_id, start=OCT_04, end=OCT_05)
        nothing = await repo.appointment_counts(hospital_id, start=OCT_07, end=OCT_07)

        assert sunday == AppointmentCountsRow(total=1, completed=1)
        assert nothing == AppointmentCountsRow()

    async def test_an_appointment_at_quarter_past_midnight_local_is_counted_on_the_new_day(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        """18:45 UTC on 5 Oct is 00:15 on 6 Oct in Kolkata. A UTC-day bucket fails this."""
        by_day = await repo.appointment_counts_by_bucket(
            hospital_id, start=OCT_05, end=OCT_07, tz=TZ, granularity="day"
        )

        assert by_day == {
            date(2026, 10, 5): AppointmentCountsRow(total=4, completed=2, cancelled=1, no_show=1),
            date(2026, 10, 6): AppointmentCountsRow(total=3, booked=1, checked_in=1, in_progress=1),
        }
        fifth = await repo.appointment_counts(hospital_id, start=OCT_05, end=OCT_06)
        assert fifth.booked == 0

    async def test_the_same_data_bucketed_in_utc_moves_that_appointment_back_a_day(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        by_utc_day = await repo.appointment_counts_by_bucket(
            hospital_id, start=OCT_05, end=OCT_07, tz="UTC", granularity="day"
        )

        assert by_utc_day[date(2026, 10, 5)].booked == 1
        assert by_utc_day[date(2026, 10, 6)].booked == 0

    async def test_a_sunday_and_the_following_monday_are_different_weeks(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        by_week = await repo.appointment_counts_by_bucket(
            hospital_id, start=OCT_04, end=OCT_06, tz=TZ, granularity="week"
        )

        assert by_week == {
            date(2026, 9, 28): AppointmentCountsRow(total=1, completed=1),
            date(2026, 10, 5): AppointmentCountsRow(total=4, completed=2, cancelled=1, no_show=1),
        }

    async def test_month_bucket(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        by_month = await repo.appointment_counts_by_bucket(
            hospital_id, start=OCT_04, end=OCT_07, tz=TZ, granularity="month"
        )

        assert by_month == {
            date(2026, 10, 1): AppointmentCountsRow(
                total=8, booked=1, checked_in=1, in_progress=1, completed=3, cancelled=1, no_show=1
            )
        }

    async def test_doctor_filter(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        counts = await repo.appointment_counts(
            hospital_id, start=OCT_05, end=OCT_07, doctor_id=clinic.priya
        )
        by_day = await repo.appointment_counts_by_bucket(
            hospital_id, start=OCT_05, end=OCT_07, tz=TZ, granularity="day", doctor_id=clinic.priya
        )

        assert counts == AppointmentCountsRow(total=3, completed=2, cancelled=1)
        assert by_day == {date(2026, 10, 5): counts}

    async def test_department_filter_uses_the_doctors_department(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        counts = await repo.appointment_counts(
            hospital_id, start=OCT_05, end=OCT_07, department_id=clinic.cardiology
        )
        by_day = await repo.appointment_counts_by_bucket(
            hospital_id,
            start=OCT_05,
            end=OCT_07,
            tz=TZ,
            granularity="day",
            department_id=clinic.cardiology,
        )

        # Priya's three and the deactivated Cardiology doctor's one.
        assert counts == AppointmentCountsRow(total=4, in_progress=1, completed=2, cancelled=1)
        assert {day: row.total for day, row in by_day.items()} == {
            date(2026, 10, 5): 3,
            date(2026, 10, 6): 1,
        }

    async def test_a_doctor_outside_the_department_gives_zeros_not_an_error(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        both: dict[str, Any] = {"doctor_id": clinic.arjun, "department_id": clinic.cardiology}

        counts = await repo.appointment_counts(hospital_id, start=OCT_05, end=OCT_07, **both)
        by_day = await repo.appointment_counts_by_bucket(
            hospital_id, start=OCT_05, end=OCT_07, tz=TZ, granularity="day", **both
        )
        by_doctor = await repo.appointment_counts_by_doctor(
            hospital_id, start=OCT_05, end=OCT_07, **both
        )
        by_department = await repo.appointment_counts_by_department(
            hospital_id, start=OCT_05, end=OCT_07, **both
        )

        assert counts == AppointmentCountsRow()
        assert (by_day, by_doctor, by_department) == ({}, [], [])


class TestAppointmentBreakdowns:
    async def test_by_doctor_is_ordered_busiest_first_then_by_name(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        rows = await repo.appointment_counts_by_doctor(hospital_id, start=OCT_05, end=OCT_07)

        assert [
            (r.doctor_name, r.department_name, r.total, r.completed, r.cancelled, r.no_show)
            for r in rows
        ] == [
            ("Priya Sharma", "Cardiology", 3, 2, 1, 0),
            ("Arjun Nair", "Orthopaedics", 2, 0, 0, 1),
            ("Old Doc", "Cardiology", 1, 0, 0, 0),
            ("Vikram Desai", None, 1, 0, 0, 0),
        ]
        assert [r.doctor_id for r in rows] == [
            clinic.priya,
            clinic.arjun,
            clinic.retired,
            clinic.vikram,
        ]
        assert (rows[0].department_id, rows[3].department_id) == (clinic.cardiology, None)

    async def test_a_deactivated_doctor_still_appears(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        rows = await repo.appointment_counts_by_doctor(
            hospital_id, start=OCT_05, end=OCT_07, doctor_id=clinic.retired
        )

        assert [(r.doctor_id, r.doctor_name, r.total) for r in rows] == [
            (clinic.retired, "Old Doc", 1)
        ]

    async def test_by_department_puts_doctors_without_one_in_a_null_row_last(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        rows = await repo.appointment_counts_by_department(hospital_id, start=OCT_05, end=OCT_07)

        assert [
            (r.department_id, r.department_name, r.total, r.completed, r.cancelled, r.no_show)
            for r in rows
        ] == [
            (clinic.cardiology, "Cardiology", 4, 2, 1, 0),
            (clinic.orthopaedics, "Orthopaedics", 2, 0, 0, 1),
            (None, None, 1, 0, 0, 0),
        ]

    async def test_ties_put_the_null_department_after_named_ones(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        # 6 Oct only: Orthopaedics 1 (Arjun), Cardiology 1 (retired), none 1 (Vikram).
        rows = await repo.appointment_counts_by_department(hospital_id, start=OCT_06, end=OCT_07)

        assert [r.department_name for r in rows] == ["Cardiology", "Orthopaedics", None]

    async def test_department_filter_narrows_both_breakdowns(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        by_doctor = await repo.appointment_counts_by_doctor(
            hospital_id, start=OCT_05, end=OCT_07, department_id=clinic.orthopaedics
        )
        by_department = await repo.appointment_counts_by_department(
            hospital_id, start=OCT_05, end=OCT_07, department_id=clinic.orthopaedics
        )

        assert [(r.doctor_name, r.total) for r in by_doctor] == [("Arjun Nair", 2)]
        assert [(r.department_name, r.total) for r in by_department] == [("Orthopaedics", 2)]


class TestDoctorViews:
    async def test_schedule_is_that_doctors_day_in_start_order(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        rows = await repo.doctor_schedule(
            hospital_id, doctor_id=clinic.priya, start=OCT_05, end=OCT_06
        )

        assert [(r.scheduled_start, r.status, r.patient_name) for r in rows] == [
            (utc("2026-10-05T04:00"), AppointmentStatus.COMPLETED, "First Rao"),
            (utc("2026-10-05T04:30"), AppointmentStatus.COMPLETED, "Second Rao"),
            (utc("2026-10-05T05:00"), AppointmentStatus.CANCELLED, "First Rao"),
        ]
        assert rows[0].scheduled_end == utc("2026-10-05T04:10")
        assert rows[0].type == AppointmentType.NEW
        assert rows[0].patient_id == clinic.first
        assert rows[0].patient_mrn.startswith("MRN-")
        assert rows[0].checked_in_at is None

    async def test_schedule_leaves_out_deleted_appointments_and_keeps_deactivated_patients(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        priya = await repo.doctor_schedule(
            hospital_id, doctor_id=clinic.priya, start=OCT_06, end=OCT_07
        )
        vikram = await repo.doctor_schedule(
            hospital_id, doctor_id=clinic.vikram, start=OCT_06, end=OCT_07
        )

        assert priya == []
        assert [(r.patient_id, r.patient_name) for r in vikram] == [
            (clinic.deactivated, "Left Rao")
        ]

    async def test_my_patients_counts_each_active_patient_seen_or_due_once(
        self, repo: ReportRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        doctor = await insert_named_doctor(
            db_session, hospital_id, first_name="Meera", last_name="Krishnan"
        )
        colleague = await insert_named_doctor(
            db_session, hospital_id, first_name="Some", last_name="Colleague"
        )

        async def patient(name: str, *, deleted: bool = False) -> uuid.UUID:
            row = await insert_report_patient(
                db_session,
                hospital_id,
                created_at=REGISTERED_LONG_AGO,
                first_name=name,
                deleted=deleted,
            )
            return row.id

        twice, missed, gone, due, theirs = (
            await patient("Twice"),
            await patient("Missed"),
            await patient("Gone", deleted=True),
            await patient("Due"),
            await patient("Theirs"),
        )
        status = AppointmentStatus
        visits = [
            (doctor.id, twice, "2026-08-01T04:00", status.COMPLETED),
            (doctor.id, twice, "2026-09-01T04:00", status.COMPLETED),
            (doctor.id, missed, "2026-08-02T04:00", status.CANCELLED),
            (doctor.id, missed, "2026-08-03T04:00", status.NO_SHOW),
            (doctor.id, gone, "2026-08-04T04:00", status.COMPLETED),
            (doctor.id, due, "2027-01-04T04:00", status.BOOKED),
            (colleague.id, theirs, "2026-08-05T04:00", status.COMPLETED),
        ]
        for doctor_id, patient_id, start, state in visits:
            await insert_report_appointment(
                db_session,
                hospital_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                start=utc(start),
                status=state,
            )

        # "Twice" once, "Due" once. Not "Missed", "Gone" or "Theirs".
        assert await repo.count_doctor_patients(hospital_id, doctor_id=doctor.id) == 2
        assert await repo.count_doctor_patients(hospital_id, doctor_id=colleague.id) == 1


class TestReception:
    async def test_walk_in_queue_counts_todays_walk_ins_by_status(
        self,
        repo: ReportRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        await seed_reception_desk(db_session, hospital_id)
        await seed_reception_desk(db_session, other_hospital_id)

        queue = await repo.walk_in_queue(hospital_id, start=OCT_06, end=OCT_07)

        assert queue == WalkInRow(
            waiting=3,
            not_arrived=1,
            in_consultation=1,
            earliest_checked_in_at=utc("2026-10-06T03:05"),
        )

    async def test_yesterdays_walk_in_is_only_in_yesterdays_queue(
        self, repo: ReportRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        await seed_reception_desk(db_session, hospital_id)

        yesterday = await repo.walk_in_queue(hospital_id, start=OCT_05, end=OCT_06)

        assert yesterday == WalkInRow(waiting=1, earliest_checked_in_at=utc("2026-10-05T03:00"))

    async def test_an_empty_queue_has_no_earliest_check_in(
        self, repo: ReportRepository, hospital_id: uuid.UUID
    ) -> None:
        assert await repo.walk_in_queue(hospital_id, start=OCT_06, end=OCT_07) == WalkInRow()

    async def test_at_risk_is_booked_today_and_already_past_its_start(
        self,
        repo: ReportRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        doctor_id = await seed_reception_desk(db_session, hospital_id)
        await seed_reception_desk(db_session, other_hospital_id)
        window: dict[str, Any] = {"start": OCT_06, "end": OCT_07}

        # The only booked row today starts at 03:30 UTC.
        before = await repo.count_at_risk(hospital_id, now=utc("2026-10-06T03:30"), **window)
        after = await repo.count_at_risk(hospital_id, now=utc("2026-10-06T03:30:01"), **window)
        rows = await repo.at_risk_appointments(
            hospital_id, now=utc("2026-10-06T08:00"), limit=20, **window
        )

        assert (before, after) == (0, 1)
        (row,) = rows
        assert row.scheduled_start == utc("2026-10-06T03:30")
        assert (row.patient_name, row.doctor_name, row.doctor_id) == (
            "Sunita Rao",
            "Arjun Nair",
            doctor_id,
        )
        assert row.patient_mrn.startswith("MRN-")

    async def test_the_list_is_ordered_and_limited_but_the_count_is_not(
        self, repo: ReportRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        doctor = await insert_named_doctor(
            db_session, hospital_id, first_name="Arjun", last_name="Nair"
        )
        patient = await insert_report_patient(
            db_session, hospital_id, created_at=REGISTERED_LONG_AGO
        )
        first = utc("2026-10-06T02:00")
        # Inserted latest first, so insertion order cannot pass for start order.
        for slot in reversed(range(22)):
            await insert_report_appointment(
                db_session,
                hospital_id,
                patient_id=patient.id,
                doctor_id=doctor.id,
                start=first + timedelta(minutes=10 * slot),
                status=AppointmentStatus.BOOKED,
            )
        # Yesterday's stale booking and a booking still to come are not at risk today.
        for start in ("2026-10-05T05:00", "2026-10-06T12:00"):
            await insert_report_appointment(
                db_session,
                hospital_id,
                patient_id=patient.id,
                doctor_id=doctor.id,
                start=utc(start),
                status=AppointmentStatus.BOOKED,
            )
        now = utc("2026-10-06T08:00")

        count = await repo.count_at_risk(hospital_id, start=OCT_06, end=OCT_07, now=now)
        rows = await repo.at_risk_appointments(
            hospital_id, start=OCT_06, end=OCT_07, now=now, limit=20
        )

        assert count == 22
        assert [row.scheduled_start for row in rows] == [
            first + timedelta(minutes=10 * slot) for slot in range(20)
        ]


# ── Patients ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def registrations(
    db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
) -> None:
    """Registrations in the hospital under test, with another hospital's alongside."""
    await seed_registrations(db_session, hospital_id)
    for _ in range(5):
        await insert_report_patient(
            db_session, other_hospital_id, created_at=utc("2026-10-05T10:00"), gender=Gender.MALE
        )


class TestPatients:
    async def test_registered_per_local_day_excludes_deactivated_patients(
        self, repo: ReportRepository, hospital_id: uuid.UUID, registrations: None
    ) -> None:
        by_day = await repo.patient_registrations(
            hospital_id, start=OCT_01, end=OCT_07, tz=TZ, granularity="day"
        )

        assert by_day == {date(2026, 10, 5): 1, date(2026, 10, 6): 1}

    async def test_a_registration_one_second_before_local_midnight_is_in_september(
        self, repo: ReportRepository, hospital_id: uuid.UUID, registrations: None
    ) -> None:
        by_day = await repo.patient_registrations(
            hospital_id, start=SEP_30, end=OCT_07, tz=TZ, granularity="day"
        )
        by_month = await repo.patient_registrations(
            hospital_id, start=SEP_30, end=OCT_07, tz=TZ, granularity="month"
        )
        by_week = await repo.patient_registrations(
            hospital_id, start=SEP_30, end=OCT_07, tz=TZ, granularity="week"
        )

        assert by_day[date(2026, 9, 30)] == 1
        assert by_month == {date(2026, 9, 1): 1, date(2026, 10, 1): 2}
        assert by_week == {date(2026, 9, 28): 1, date(2026, 10, 5): 2}

    async def test_by_gender_has_all_four_keys(
        self, repo: ReportRepository, hospital_id: uuid.UUID, registrations: None
    ) -> None:
        october = await repo.patient_registrations_by_gender(hospital_id, start=OCT_01, end=OCT_07)
        nothing = await repo.patient_registrations_by_gender(hospital_id, start=OCT_07, end=OCT_07)

        assert october == {"male": 1, "female": 1, "other": 0, "unspecified": 0}
        assert nothing == {"male": 0, "female": 0, "other": 0, "unspecified": 0}

    async def test_active_total_is_every_patient_not_deactivated(
        self, repo: ReportRepository, hospital_id: uuid.UUID, registrations: None
    ) -> None:
        assert await repo.count_active_patients(hospital_id) == 3


# ── Filter lookups ───────────────────────────────────────────────────────────


class TestRefs:
    async def test_doctor_and_department_names_including_deactivated_ones(
        self, repo: ReportRepository, hospital_id: uuid.UUID, clinic: Clinic
    ) -> None:
        priya = await repo.doctor_ref(hospital_id, clinic.priya)
        retired = await repo.doctor_ref(hospital_id, clinic.retired)
        department = await repo.department_ref(hospital_id, clinic.cardiology)

        assert priya is not None
        assert (priya.id, priya.name) == (clinic.priya, "Priya Sharma")
        assert retired is not None
        assert retired.name == "Old Doc"
        assert department is not None
        assert (department.id, department.name) == (clinic.cardiology, "Cardiology")

    async def test_ids_of_another_hospital_or_of_nothing_are_not_found(
        self,
        repo: ReportRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        clinic: Clinic,
    ) -> None:
        assert await repo.doctor_ref(other_hospital_id, clinic.priya) is None
        assert await repo.department_ref(other_hospital_id, clinic.cardiology) is None
        assert await repo.doctor_ref(hospital_id, uuid.uuid4()) is None
        assert await repo.department_ref(hospital_id, uuid.uuid4()) is None


# ── Every bucketed method, at every granularity ──────────────────────────────


@pytest.mark.parametrize("granularity", GRANULARITIES)
async def test_every_bucketed_method_runs_at_every_granularity(
    repo: ReportRepository,
    hospital_id: uuid.UUID,
    billing: dict[str, uuid.UUID],
    clinic: Clinic,
    registrations: None,
    granularity: str,
) -> None:
    """A bucket expression built twice fails here, as a database error."""
    window: dict[str, Any] = {"start": SEP_30, "end": OCT_07, "tz": TZ, "granularity": granularity}

    patients = await repo.patient_registrations(hospital_id, **window)
    appointments = await repo.appointment_counts_by_bucket(hospital_id, **window)
    filtered = await repo.appointment_counts_by_bucket(
        hospital_id, doctor_id=clinic.priya, department_id=clinic.cardiology, **window
    )
    invoiced = await repo.invoiced_by_bucket(hospital_id, **window)
    payments = await repo.payments_by_bucket(hospital_id, **window)
    refunds = await repo.refunds_by_bucket(hospital_id, **window)

    # The clinic and billing fixtures register patients too, all in January.
    assert sum(patients.values()) == 3
    assert sum(row.total for row in appointments.values()) == 8
    assert sum(row.total for row in filtered.values()) == 3
    assert sum(total.count for total in invoiced.values()) == 6
    assert sum((t.amount for t in invoiced.values()), Decimal("0.00")) == Decimal("4350.10")
    assert sum(total.count for total in payments.values()) == 8
    assert sum((t.amount for t in payments.values()), Decimal("0.00")) == Decimal("2800.10")
    assert sum((t.amount for t in refunds.values()), Decimal("0.00")) == Decimal("950.00")
    for buckets in (patients, appointments, invoiced, payments, refunds):
        for day in buckets:
            assert isinstance(day, date)
            assert not isinstance(day, datetime)
            if granularity == "week":
                assert day.weekday() == 0
            if granularity == "month":
                assert day.day == 1


async def test_an_unknown_granularity_never_reaches_the_database(
    repo: ReportRepository, hospital_id: uuid.UUID
) -> None:
    with pytest.raises(ValueError, match="Unknown granularity"):
        await repo.patient_registrations(
            hospital_id, start=OCT_01, end=OCT_07, tz=TZ, granularity="hour'; DROP TABLE patients"
        )


# ── Tenant isolation ─────────────────────────────────────────────────────────


class TestTenantIsolation:
    async def test_a_hospital_with_no_data_gets_zeros_from_every_method(
        self,
        repo: ReportRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Everything is seeded in one hospital; the other sees none of it."""
        await seed_billing(db_session, hospital_id)
        clinic = await seed_clinic(db_session, hospital_id)
        await seed_registrations(db_session, hospital_id)
        await seed_reception_desk(db_session, hospital_id)

        other = other_hospital_id
        wide: dict[str, Any] = {"start": utc("2026-01-01T00:00"), "end": utc("2027-01-01T00:00")}
        bucketed: dict[str, Any] = {**wide, "tz": TZ, "granularity": "day"}
        now = utc("2026-12-31T00:00")

        assert await repo.patient_registrations(other, **bucketed) == {}
        assert await repo.patient_registrations_by_gender(other, **wide) == {
            "male": 0,
            "female": 0,
            "other": 0,
            "unspecified": 0,
        }
        assert await repo.count_active_patients(other) == 0
        assert await repo.appointment_counts(other, **wide) == AppointmentCountsRow()
        assert await repo.appointment_counts(other, doctor_id=clinic.priya, **wide) == (
            AppointmentCountsRow()
        )
        assert await repo.appointment_counts(other, department_id=clinic.cardiology, **wide) == (
            AppointmentCountsRow()
        )
        assert await repo.appointment_counts_by_bucket(other, **bucketed) == {}
        assert await repo.appointment_counts_by_doctor(other, **wide) == []
        assert await repo.appointment_counts_by_department(other, **wide) == []
        assert await repo.doctor_schedule(other, doctor_id=clinic.priya, **wide) == []
        assert await repo.count_doctor_patients(other, doctor_id=clinic.priya) == 0
        assert await repo.walk_in_queue(other, **wide) == WalkInRow()
        assert await repo.count_at_risk(other, now=now, **wide) == 0
        assert await repo.at_risk_appointments(other, now=now, limit=20, **wide) == []
        assert await repo.invoiced_totals(other, **wide) == money(0, "0.00")
        assert await repo.invoiced_by_bucket(other, **bucketed) == {}
        assert await repo.payment_totals_by_method(other, **wide) == by_method()
        assert await repo.payments_by_bucket(other, **bucketed) == {}
        assert await repo.refund_totals_by_method(other, **wide) == by_method()
        assert await repo.refunds_by_bucket(other, **bucketed) == {}
        assert await repo.outstanding_summary(other) == OutstandingSummaryRow()
        assert await repo.outstanding_ageing(other, today=date(2026, 10, 6), tz=TZ) == {
            key: money(0, "0.00") for key, _, _ in AGEING_BUCKETS
        }
        assert await repo.outstanding_invoices(other, tz=TZ, limit=100) == []
        assert await repo.pending_discounts(other) == money(0, "0.00")
        assert await repo.doctor_ref(other, clinic.priya) is None
        assert await repo.department_ref(other, clinic.cardiology) is None

        # And the data is really there for its owner.
        assert (await repo.appointment_counts(hospital_id, **wide)).total > 0
        assert (await repo.invoiced_totals(hospital_id, **wide)).count > 0

    async def test_the_other_hospital_sees_only_its_own_figures(
        self,
        repo: ReportRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        billing: dict[str, uuid.UUID],
        clinic: Clinic,
    ) -> None:
        """Both hospitals hold data; neither total contains the other's."""
        other = other_hospital_id
        window: dict[str, Any] = {"start": OCT_01, "end": OCT_07}

        assert await repo.invoiced_totals(other, **window) == money(2, "88.88")
        assert await repo.payment_totals_by_method(other, **window) == by_method(
            cash=money(1, "11.11")
        )
        assert await repo.refund_totals_by_method(other, **window) == by_method(
            cash=money(1, "5.55")
        )
        assert await repo.outstanding_summary(other) == OutstandingSummaryRow(
            invoice_count=1, outstanding_amount=Decimal("77.77"), issued_count=1
        )
        assert await repo.pending_discounts(other) == money(1, "9.99")
        assert await repo.appointment_counts(other, **window) == AppointmentCountsRow(
            total=3, completed=1, no_show=2
        )
        assert [
            (row.doctor_name, row.total)
            for row in await repo.appointment_counts_by_doctor(other, **window)
        ] == [("Other Doctor", 3)]
        # One patient from each of the other hospital's two fixtures.
        assert await repo.count_active_patients(other) == 2

        # The hospital under test is exactly as computed with the other one present.
        assert await repo.invoiced_totals(hospital_id, **window) == money(6, "4350.10")
        assert await repo.outstanding_summary(hospital_id) == OutstandingSummaryRow(
            invoice_count=2,
            outstanding_amount=Decimal("1550.00"),
            issued_count=1,
            partially_paid_count=1,
        )
        assert (await repo.appointment_counts(hospital_id, start=OCT_05, end=OCT_07)).total == 7

    async def test_a_cross_tenant_join_cannot_borrow_a_name(
        self,
        repo: ReportRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """A row pointing at another hospital's doctor is not reported with that doctor."""
        foreign_doctor = await insert_named_doctor(
            db_session, other_hospital_id, first_name="Foreign", last_name="Doctor"
        )
        patient = await insert_report_patient(
            db_session, hospital_id, created_at=REGISTERED_LONG_AGO
        )
        await insert_report_appointment(
            db_session,
            hospital_id,
            patient_id=patient.id,
            doctor_id=foreign_doctor.id,
            start=utc("2026-10-05T04:00"),
            status=AppointmentStatus.BOOKED,
        )
        window: dict[str, Any] = {"start": OCT_05, "end": OCT_06}

        by_doctor = await repo.appointment_counts_by_doctor(hospital_id, **window)
        late = await repo.at_risk_appointments(
            hospital_id, now=utc("2026-10-05T09:00"), limit=20, **window
        )

        assert by_doctor == []
        assert late == []
        assert await repo.doctor_ref(hospital_id, foreign_doctor.id) is None
