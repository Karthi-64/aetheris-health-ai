"""API and adversarial tests for Patient App doctor availability (Task 31).

``docs/modules/15-patient-app.md`` §9.1 and §13. Over HTTP, through the real
application and a real PostgreSQL, with the clock under the test's control.

**The oracle.** The slots a patient sees must be the engine's own: wherever a
test says "exactly these", the expectation is written out by hand from the
stored windows, leaves and appointments, so that a change to the engine or to
what the patient layer keeps of it is seen here.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi import Depends
from sqlalchemy import event, func, select, update

from app.api.dependencies.patient import get_consent_service
from app.api.dependencies.repositories import (
    get_patient_account_link_repository,
    get_patient_consent_repository,
)
from app.api.dependencies.services import get_audit_sink, get_unit_of_work
from app.core.audit import AuditSink  # noqa: TC001 — FastAPI resolves the override at runtime
from app.core.config import settings
from app.core.security import create_access_token
from app.database.unit_of_work import UnitOfWork  # noqa: TC001 — as above
from app.models.appointment import Appointment, AppointmentStatus
from app.models.audit_log import AuditLog
from app.models.doctor import Doctor, DoctorLeave
from app.models.patient_consent import ConsentPurpose
from app.repositories import (  # noqa: TC001 — as above
    PatientAccountLinkRepository,
    PatientConsentRepository,
)
from app.repositories.doctor_repository import DoctorRepository
from app.services.doctor_service import BookedInterval, generate_slots
from app.services.patient_app import availability_service
from app.services.patient_app.consent_service import ConsentService
from app.services.patient_app.policies import Policy
from app.tests.patient_app_helpers import (
    PATIENT,
    FakeSmsSender,
    bearer,
    build_patient_application,
    insert_appointment,
    insert_doctor,
    insert_hospital,
    insert_leave,
    insert_patient_record,
    new_phone,
    open_hospital,
    patient_client,
    set_windows,
    sign_in,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from fastapi import FastAPI
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from app.models.hospital import Hospital

pytestmark = pytest.mark.database

HOSPITALS = f"{PATIENT}/hospitals"
IST = ZoneInfo("Asia/Kolkata")
#: Monday 2026-10-05, 09:00 at a Kolkata hospital. The default doctor has a
#: Monday 09:00–12:00 window of 15-minute slots, so with the default lead of
#: 60 minutes the first slot shown is 10:00.
NOW = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)
MONDAY = date(2026, 10, 5)
TOP_KEYS = {
    "timezone",
    "today",
    "horizon_end",
    "min_lead_minutes",
    "start_date",
    "end_date",
    "days",
}
NOT_FOUND = (404, "RESOURCE_NOT_FOUND", "Not Found")
OUTSIDE = (400, "BUSINESS_RULE_VIOLATION", "Outside the bookable window.")
#: Words that must never reach a patient from this endpoint.
INTERNAL = [
    "appointment",
    "status",
    "booked",
    "on_leave",
    "leave",
    "reason",
    "notes",
    "patient",
    "APPTCANARY",
    "NOTESCANARY",
    "LEAVECANARY",
    "OTHERCANARY",
    "staff-secret",
    "LICCANARY",
    "user_id",
    "hospital_id",
    "doctor_id",
    "slot_id",
]


@pytest.fixture(autouse=True)
def _patient_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_HOSPITAL_PER_MIN", 1_000_000)


class _Clock:
    """The instant the server believes it is. Set ``now`` to move it."""

    def __init__(self) -> None:
        self.now = NOW


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    held = _Clock()
    monkeypatch.setattr(availability_service, "utc_now", lambda: held.now)
    return held


@pytest.fixture
def sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def tag() -> str:
    return f"v{uuid.uuid4().hex[:11]}"


@pytest_asyncio.fixture
async def application(db_session: AsyncSession, sms: FakeSmsSender) -> AsyncGenerator[FastAPI]:
    app = build_patient_application(db_session, sms)
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def browser(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with patient_client(application) as client:
        yield client


class _Patient:
    def __init__(self, client: AsyncClient, session: dict[str, Any]) -> None:
        self.client = client
        self.account_id = session["account"]["id"]
        self.headers = bearer(session["access_token"])

    async def availability(
        self, hospital_ref: object, doctor_ref: object, **params: Any
    ) -> Response:
        return await self.client.get(
            f"{HOSPITALS}/{hospital_ref}/doctors/{doctor_ref}/availability",
            params=params,
            headers=self.headers,
        )

    async def days(
        self, hospital_ref: object, doctor_ref: object, **params: Any
    ) -> dict[str, list[str]]:
        """``{date: [start, …]}`` of a request that must succeed."""
        response = await self.availability(hospital_ref, doctor_ref, **params)
        assert response.status_code == 200, response.text
        return _starts(response)


def _starts(response: Response) -> dict[str, list[str]]:
    return {
        day["date"]: [slot["start"] for slot in day["slots"]]
        for day in response.json()["data"]["days"]
    }


def _error(response: Response) -> tuple[int, str, str]:
    body = response.json()
    return response.status_code, body["error_code"], body["message"]


def _refusal(response: Response) -> str:
    body = {k: v for k, v in response.json().items() if "request" not in k and k != "metadata"}
    return json.dumps([response.status_code, body], sort_keys=True)


def _ist(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def _iso(day: date, hour: int, minute: int = 0) -> str:
    return _ist(day, hour, minute).isoformat()


@pytest_asyncio.fixture
async def patient(browser: AsyncClient, sms: FakeSmsSender) -> _Patient:
    return _Patient(browser, await sign_in(browser, sms, new_phone()))


@pytest_asyncio.fixture
async def hospital(db_session: AsyncSession, tag: str) -> Hospital:
    return await insert_hospital(db_session, name=f"{tag} General", timezone="Asia/Kolkata")


@pytest_asyncio.fixture
async def doctor(db_session: AsyncSession, hospital: Hospital) -> Doctor:
    """A listed doctor with a Monday 09:00–12:00 window of 15-minute slots."""
    return await insert_doctor(db_session, hospital.id)


@pytest_asyncio.fixture
async def someone_else(db_session: AsyncSession, hospital: Hospital) -> uuid.UUID:
    """Another patient's record at the hospital, to book the doctor for."""
    record = await insert_patient_record(
        db_session, hospital.id, phone="+919111111111", first_name="OTHERCANARY"
    )
    return record.id


@pytest.fixture
def statements(db_engine: AsyncEngine) -> Iterator[list[str]]:
    """Every SQL statement sent to the database, in order."""
    seen: list[str] = []

    def _record(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        seen.append(re.sub(r"sa_savepoint_\d+", "sa_savepoint", statement))

    event.listen(db_engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _record)


MONDAY_SLOTS = [_iso(MONDAY, 10, m) for m in (0, 15, 30, 45)] + [
    _iso(MONDAY, 11, m) for m in (0, 15, 30, 45)
]


# ── What is answered ─────────────────────────────────────────────────────────


class TestWhatIsAnswered:
    async def test_the_default_is_the_week_from_today_at_the_hospital(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        response = await patient.availability(hospital.slug, doctor.id)

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == TOP_KEYS
        assert data["timezone"] == "Asia/Kolkata"
        assert data["today"] == "2026-10-05"
        assert data["horizon_end"] == "2026-11-04"
        assert data["min_lead_minutes"] == 60
        assert (data["start_date"], data["end_date"]) == ("2026-10-05", "2026-10-11")
        assert [day["date"] for day in data["days"]] == [
            str(MONDAY + timedelta(days=n)) for n in range(7)
        ]
        assert all(set(day) == {"date", "slots"} for day in data["days"])
        assert _starts(response) == {
            "2026-10-05": MONDAY_SLOTS,
            **{str(MONDAY + timedelta(days=n)): [] for n in range(1, 7)},
        }

    async def test_a_slot_is_exactly_its_start_and_end_in_the_hospital_offset(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        response = await patient.availability(
            hospital.slug, doctor.id, start_date="2026-10-05", end_date="2026-10-05"
        )

        [day] = response.json()["data"]["days"]
        assert all(set(slot) == {"start", "end"} for slot in day["slots"])
        assert all(
            slot["start"].endswith("+05:30") and slot["end"].endswith("+05:30")
            for slot in day["slots"]
        )
        for slot in day["slots"]:
            start, end = datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])
            assert end - start == timedelta(minutes=15)
        assert day["slots"][0] == {"start": _iso(MONDAY, 10), "end": _iso(MONDAY, 10, 15)}
        assert day["slots"][-1] == {"start": _iso(MONDAY, 11, 45), "end": _iso(MONDAY, 12)}

    async def test_the_slots_are_the_engines_own(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        """Same windows, leaves, appointments and generator as the staff slot picker."""
        await set_windows(
            db_session,
            doctor,
            [(0, time(9), time(12), 15), (0, time(14), time(15), 30), (2, time(8), time(9), 20)],
        )
        await insert_leave(db_session, doctor, _ist(MONDAY, 10, 30), _ist(MONDAY, 11))
        await insert_appointment(
            db_session, doctor, someone_else, _ist(MONDAY, 11, 15), _ist(MONDAY, 11, 30)
        )
        await insert_appointment(
            db_session,
            doctor,
            someone_else,
            _ist(MONDAY, 14, 30),
            _ist(MONDAY, 15),
            status=AppointmentStatus.CANCELLED,
        )
        wednesday = MONDAY + timedelta(days=2)
        repository = DoctorRepository(db_session)

        expected: dict[str, list[str]] = {}
        for day in (MONDAY, MONDAY + timedelta(days=1), wednesday):
            rows = await repository.get_availability(hospital.id, doctor.id)
            leaves = [
                (row.starts_at, row.ends_at)
                for row in await repository.list_leaves(hospital.id, doctor.id)
            ]
            booked = [
                BookedInterval(
                    starts_at=row.scheduled_start, ends_at=row.scheduled_end, appointment_id=row.id
                )
                for row in (
                    await db_session.execute(
                        select(Appointment).where(
                            Appointment.doctor_id == doctor.id,
                            Appointment.status != AppointmentStatus.CANCELLED,
                        )
                    )
                ).scalars()
            ]
            slots = generate_slots(
                target_date=day,
                availability=[
                    (r.start_time, r.end_time, r.slot_duration_minutes)
                    for r in rows
                    if r.day_of_week == day.weekday()
                ],
                leaves=leaves,
                booked=booked,
                timezone="Asia/Kolkata",
            )
            expected[str(day)] = [
                s.start.isoformat()
                for s in slots
                if s.status.value == "available" and s.start >= NOW + timedelta(minutes=60)
            ]

        assert (
            await patient.days(
                hospital.slug, doctor.id, start_date="2026-10-05", end_date="2026-10-07"
            )
            == expected
        )
        assert expected["2026-10-05"] == [
            _iso(MONDAY, 10),
            _iso(MONDAY, 10, 15),
            _iso(MONDAY, 11),
            _iso(MONDAY, 11, 30),
            _iso(MONDAY, 11, 45),
            _iso(MONDAY, 14),
            _iso(MONDAY, 14, 30),
        ]
        assert expected["2026-10-07"] == [
            _iso(wednesday, 8),
            _iso(wednesday, 8, 20),
            _iso(wednesday, 8, 40),
        ]

    async def test_recurring_availability_repeats_every_week(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        days = await patient.days(
            hospital.slug, doctor.id, start_date="2026-10-05", end_date="2026-10-18"
        )

        assert len(days) == 14
        assert days["2026-10-05"] == MONDAY_SLOTS
        next_monday = MONDAY + timedelta(days=7)
        assert days["2026-10-12"] == [_iso(next_monday, 9, m) for m in (0, 15, 30, 45)] + [
            _iso(next_monday, h, m) for h in (10, 11) for m in (0, 15, 30, 45)
        ]
        assert all(days[d] == [] for d in days if d not in ("2026-10-05", "2026-10-12"))

    async def test_a_hospital_sets_its_own_horizon_and_lead(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} Strict",
            timezone="Asia/Kolkata",
            settings={"patient_app.booking_horizon_days": 3, "patient_app.min_lead_minutes": 150},
        )
        doctor = await insert_doctor(db_session, hospital.id)

        response = await patient.availability(hospital.slug, doctor.id)

        data = response.json()["data"]
        assert (data["horizon_end"], data["min_lead_minutes"], data["end_date"]) == (
            "2026-10-08",
            150,
            "2026-10-08",
        )
        assert len(data["days"]) == 4
        assert _starts(response)["2026-10-05"] == [_iso(MONDAY, 11, 30), _iso(MONDAY, 11, 45)]
        assert (
            _error(await patient.availability(hospital.slug, doctor.id, start_date="2026-10-09"))
            == OUTSIDE
        )


# ── Now, today and the past ──────────────────────────────────────────────────


class TestNowAndThePast:
    @pytest.mark.parametrize(
        ("lead", "first"),
        [(0, "09:00"), (30, "09:30"), (60, "10:00"), (120, "11:00"), (165, "11:45"), (166, None)],
    )
    async def test_slots_before_now_plus_the_lead_are_not_shown(
        self, patient: _Patient, db_session: AsyncSession, tag: str, lead: int, first: str | None
    ) -> None:
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} H",
            timezone="Asia/Kolkata",
            settings={"patient_app.min_lead_minutes": lead},
        )
        doctor = await insert_doctor(db_session, hospital.id)

        [starts] = (
            await patient.days(
                hospital.slug, doctor.id, start_date="2026-10-05", end_date="2026-10-05"
            )
        ).values()

        assert (starts[0][11:16] if starts else None) == first

    async def test_as_the_clock_moves_slots_fall_away(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, clock: _Clock
    ) -> None:
        assert len((await patient.days(hospital.slug, doctor.id))["2026-10-05"]) == 8
        clock.now = _ist(MONDAY, 10, 46).astimezone(
            UTC
        )  # lead 60 → 11:46 → only none? 11:45 < 11:46
        assert (await patient.days(hospital.slug, doctor.id))["2026-10-05"] == []
        clock.now = _ist(MONDAY, 10, 44).astimezone(UTC)
        assert (await patient.days(hospital.slug, doctor.id))["2026-10-05"] == [
            _iso(MONDAY, 11, 45)
        ]

    async def test_the_lead_time_crossing_midnight_filters_the_next_morning(
        self, patient: _Patient, db_session: AsyncSession, tag: str, clock: _Clock
    ) -> None:
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} H",
            timezone="Asia/Kolkata",
            settings={"patient_app.min_lead_minutes": 600},
        )
        doctor = await insert_doctor(db_session, hospital.id)
        clock.now = _ist(MONDAY - timedelta(days=1), 23, 30).astimezone(UTC)  # Sunday 23:30

        response = await patient.availability(hospital.slug, doctor.id)

        assert response.json()["data"]["today"] == "2026-10-04"
        assert _starts(response)["2026-10-05"] == [_iso(MONDAY, 9, 30), _iso(MONDAY, 9, 45)] + [
            _iso(MONDAY, h, m) for h in (10, 11) for m in (0, 15, 30, 45)
        ]

    async def test_yesterday_at_the_hospital_is_refused(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        assert (
            _error(await patient.availability(hospital.slug, doctor.id, start_date="2026-10-04"))
            == OUTSIDE
        )
        assert (
            _error(
                await patient.availability(
                    hospital.slug, doctor.id, start_date="2026-10-04", end_date="2026-10-06"
                )
            )
            == OUTSIDE
        )
        assert (
            _error(
                await patient.availability(
                    hospital.slug, doctor.id, start_date="2025-10-05", end_date="2025-10-05"
                )
            )
            == OUTSIDE
        )
        assert (
            await patient.availability(hospital.slug, doctor.id, end_date="2026-10-04")
        ).status_code == 422

    async def test_the_horizon_is_the_last_day_asked_about(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        assert (
            await patient.availability(
                hospital.slug, doctor.id, start_date="2026-11-04", end_date="2026-11-04"
            )
        ).status_code == 200
        assert (
            _error(await patient.availability(hospital.slug, doctor.id, start_date="2026-11-05"))
            == OUTSIDE
        )
        assert (
            _error(
                await patient.availability(
                    hospital.slug, doctor.id, start_date="2026-11-01", end_date="2026-11-05"
                )
            )
            == OUTSIDE
        )
        assert (
            _error(
                await patient.availability(
                    hospital.slug, doctor.id, start_date="2027-10-05", end_date="2027-10-05"
                )
            )
            == OUTSIDE
        )

    async def test_midnight_at_the_hospital_turns_the_day(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, clock: _Clock
    ) -> None:
        clock.now = datetime(2026, 10, 5, 18, 29, 59, tzinfo=UTC)  # 23:59:59 IST Monday
        assert (await patient.availability(hospital.slug, doctor.id)).json()["data"][
            "today"
        ] == "2026-10-05"
        assert (
            await patient.availability(hospital.slug, doctor.id, start_date="2026-10-05")
        ).status_code == 200

        clock.now = datetime(2026, 10, 5, 18, 30, tzinfo=UTC)  # 00:00 IST Tuesday
        response = await patient.availability(hospital.slug, doctor.id)
        assert response.json()["data"]["today"] == "2026-10-06"
        assert response.json()["data"]["horizon_end"] == "2026-11-05"
        assert (
            _error(await patient.availability(hospital.slug, doctor.id, start_date="2026-10-05"))
            == OUTSIDE
        )

    async def test_today_is_the_hospitals_even_when_utc_is_still_yesterday(
        self, patient: _Patient, db_session: AsyncSession, tag: str, clock: _Clock
    ) -> None:
        """Kiritimati is UTC+14: at 11:30Z on the 5th it is 01:30 on the 6th there."""
        hospital = await insert_hospital(db_session, name=f"{tag} H", timezone="Pacific/Kiritimati")
        doctor = await insert_doctor(db_session, hospital.id)
        await set_windows(db_session, doctor, [(1, time(9), time(10), 30)])  # Tuesday
        clock.now = datetime(2026, 10, 5, 11, 30, tzinfo=UTC)

        response = await patient.availability(hospital.slug, doctor.id)

        data = response.json()["data"]
        assert (data["today"], data["start_date"]) == ("2026-10-06", "2026-10-06")
        assert _starts(response)["2026-10-06"] == [
            "2026-10-06T09:00:00+14:00",
            "2026-10-06T09:30:00+14:00",
        ]
        assert (
            _error(await patient.availability(hospital.slug, doctor.id, start_date="2026-10-05"))
            == OUTSIDE
        )

    async def test_a_late_slot_is_not_hidden_when_utc_is_already_tomorrow(
        self, patient: _Patient, db_session: AsyncSession, tag: str, clock: _Clock
    ) -> None:
        """Pago Pago is UTC-11: at 09:30Z on the 6th it is 22:30 on the 5th there."""
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} H",
            timezone="Pacific/Pago_Pago",
            settings={"patient_app.min_lead_minutes": 0},
        )
        doctor = await insert_doctor(db_session, hospital.id)
        await set_windows(db_session, doctor, [(0, time(23), time(23, 30), 30)])  # Monday
        clock.now = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)

        response = await patient.availability(hospital.slug, doctor.id)

        assert response.json()["data"]["today"] == "2026-10-05"
        assert _starts(response)["2026-10-05"] == ["2026-10-05T23:00:00-11:00"]

        clock.now = datetime(2026, 10, 6, 10, 0, 1, tzinfo=UTC)  # 23:00:01 local
        assert (await patient.days(hospital.slug, doctor.id))["2026-10-05"] == []


# ── What occupies a slot ─────────────────────────────────────────────────────


class TestWhatOccupiesASlot:
    @pytest.mark.parametrize(
        ("status", "shown"),
        [
            (AppointmentStatus.BOOKED, False),
            (AppointmentStatus.CHECKED_IN, False),
            (AppointmentStatus.IN_PROGRESS, False),
            (AppointmentStatus.COMPLETED, False),
            (AppointmentStatus.CANCELLED, True),
            (AppointmentStatus.NO_SHOW, True),
        ],
    )
    async def test_an_appointment_occupies_its_slot_unless_it_freed_it(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
        status: AppointmentStatus,
        shown: bool,
    ) -> None:
        await insert_appointment(
            db_session, doctor, someone_else, _ist(MONDAY, 10), _ist(MONDAY, 10, 15), status=status
        )

        starts = (await patient.days(hospital.slug, doctor.id))["2026-10-05"]

        assert (_iso(MONDAY, 10) in starts) is shown
        assert len(starts) == (8 if shown else 7)

    async def test_an_appointment_across_two_slots_takes_both(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        await insert_appointment(
            db_session, doctor, someone_else, _ist(MONDAY, 10, 5), _ist(MONDAY, 10, 20)
        )

        starts = (await patient.days(hospital.slug, doctor.id))["2026-10-05"]

        assert _iso(MONDAY, 10) not in starts and _iso(MONDAY, 10, 15) not in starts
        assert _iso(MONDAY, 10, 30) in starts

    async def test_a_leave_takes_its_slots_and_a_touching_slot_stays(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        await insert_leave(db_session, doctor, _ist(MONDAY, 10, 30), _ist(MONDAY, 11))

        starts = (await patient.days(hospital.slug, doctor.id))["2026-10-05"]

        assert starts == [
            _iso(MONDAY, 10),
            _iso(MONDAY, 10, 15),
            _iso(MONDAY, 11),
            _iso(MONDAY, 11, 15),
            _iso(MONDAY, 11, 30),
            _iso(MONDAY, 11, 45),
        ]

    async def test_a_leave_over_the_whole_week_empties_it(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        await insert_leave(db_session, doctor, _ist(MONDAY, 0), _ist(MONDAY + timedelta(days=7), 0))

        days = await patient.days(
            hospital.slug, doctor.id, start_date="2026-10-05", end_date="2026-10-18"
        )

        assert days["2026-10-05"] == [] and days["2026-10-12"] != []

    async def test_a_slot_freed_between_requests_comes_back_and_one_taken_goes(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        booked = await insert_appointment(
            db_session, doctor, someone_else, _ist(MONDAY, 10), _ist(MONDAY, 10, 15)
        )
        assert _iso(MONDAY, 10) not in (await patient.days(hospital.slug, doctor.id))["2026-10-05"]

        await db_session.execute(
            update(Appointment)
            .where(Appointment.id == booked.id)
            .values(status=AppointmentStatus.CANCELLED)
        )
        await db_session.commit()
        assert _iso(MONDAY, 10) in (await patient.days(hospital.slug, doctor.id))["2026-10-05"]

        await insert_appointment(
            db_session, doctor, someone_else, _ist(MONDAY, 11), _ist(MONDAY, 11, 15)
        )
        assert _iso(MONDAY, 11) not in (await patient.days(hospital.slug, doctor.id))["2026-10-05"]

    async def test_another_doctors_calendar_does_not_touch_this_one(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        """Attack on the scoping: a window, a leave and a booking all belonging to doctor B."""
        other = await insert_doctor(db_session, hospital.id, last_name="Other")
        await set_windows(
            db_session, other, [(0, time(9), time(12), 15), (1, time(9), time(10), 15)]
        )
        await insert_leave(db_session, other, _ist(MONDAY, 10), _ist(MONDAY, 12))
        await insert_appointment(
            db_session, other, someone_else, _ist(MONDAY, 10), _ist(MONDAY, 10, 15)
        )

        days = await patient.days(hospital.slug, doctor.id)

        assert days["2026-10-05"] == MONDAY_SLOTS
        assert days["2026-10-06"] == []

    async def test_an_appointment_filed_under_another_hospital_does_not_count(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        tag: str,
    ) -> None:
        """Bad data: the row names this doctor but another hospital. The tenant filter drops it."""
        other = await insert_hospital(db_session, name=f"{tag} Other")
        record = await insert_patient_record(db_session, other.id, phone="+919222222222")
        await insert_appointment(
            db_session,
            doctor,
            record.id,
            _ist(MONDAY, 10),
            _ist(MONDAY, 10, 15),
            hospital_id=other.id,
        )

        assert (await patient.days(hospital.slug, doctor.id))["2026-10-05"] == MONDAY_SLOTS


# ── Daylight saving ──────────────────────────────────────────────────────────


class TestDaylightSaving:
    async def test_fall_back_never_repeats_an_instant_nor_shrinks_a_slot(
        self, patient: _Patient, db_session: AsyncSession, tag: str, clock: _Clock
    ) -> None:
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} NY",
            timezone="America/New_York",
            settings={"patient_app.min_lead_minutes": 0},
        )
        doctor = await insert_doctor(db_session, hospital.id)
        await set_windows(
            db_session, doctor, [(6, time(0, 30), time(3, 30), 30)]
        )  # Sunday 2026-11-01
        clock.now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

        [starts] = (
            await patient.days(
                hospital.slug, doctor.id, start_date="2026-11-01", end_date="2026-11-01"
            )
        ).values()

        instants = [datetime.fromisoformat(s).astimezone(UTC) for s in starts]
        assert instants == sorted(instants) and len(set(instants)) == len(instants)
        assert {s[-6:] for s in starts} == {"-04:00", "-05:00"}

    async def test_spring_forward_offers_no_slot_in_the_hour_that_does_not_exist(
        self, patient: _Patient, db_session: AsyncSession, tag: str, clock: _Clock
    ) -> None:
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} NY",
            timezone="America/New_York",
            settings={"patient_app.min_lead_minutes": 0},
        )
        doctor = await insert_doctor(db_session, hospital.id)
        await set_windows(db_session, doctor, [(6, time(1), time(4), 30)])  # Sunday 2026-03-08
        clock.now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)

        response = await patient.availability(
            hospital.slug, doctor.id, start_date="2026-03-08", end_date="2026-03-08"
        )

        [day] = response.json()["data"]["days"]
        assert [slot["start"] for slot in day["slots"]] == [
            "2026-03-08T01:00:00-05:00",
            "2026-03-08T01:30:00-05:00",
            "2026-03-08T03:00:00-04:00",
            "2026-03-08T03:30:00-04:00",
        ]
        instants = [datetime.fromisoformat(slot["start"]).astimezone(UTC) for slot in day["slots"]]
        assert len(set(instants)) == len(instants)
        for slot in day["slots"]:
            start, end = datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])
            assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(minutes=30)


# ── Bounds ───────────────────────────────────────────────────────────────────


class TestBounds:
    @pytest.mark.parametrize(
        "value",
        [
            "2026-13-01",
            "yesterday",
            "2026-10-5",
            "",
            "2026-10-05T00:00",
            "05/10/2026",
            "2026-10-05 ",
            "' OR 1=1",
            "a" * 400,
        ],
    )
    async def test_a_malformed_date_is_refused_without_a_lookup(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        value: str,
        statements: list[str],
    ) -> None:
        for name in ("start_date", "end_date"):
            statements.clear()
            response = await patient.availability(hospital.slug, doctor.id, **{name: value})
            assert response.status_code == 422, (name, value, response.text)
            assert not [s for s in statements if "doctor_availability" in s or "appointments" in s]

    async def test_a_backwards_or_overlong_range_is_refused(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        assert (
            await patient.availability(
                hospital.slug, doctor.id, start_date="2026-10-07", end_date="2026-10-06"
            )
        ).status_code == 422
        assert (
            await patient.availability(
                hospital.slug, doctor.id, start_date="2026-10-05", end_date="2026-10-19"
            )
        ).status_code == 422
        fourteen = await patient.availability(
            hospital.slug, doctor.id, start_date="2026-10-05", end_date="2026-10-18"
        )
        assert fourteen.status_code == 200 and len(fourteen.json()["data"]["days"]) == 14
        assert (
            await patient.availability(
                hospital.slug, doctor.id, start_date="2026-10-20", end_date="2026-10-20"
            )
        ).status_code == 200

    async def test_a_parameter_it_does_not_define_changes_nothing(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        await insert_appointment(
            db_session, doctor, someone_else, _ist(MONDAY, 10), _ist(MONDAY, 10, 15)
        )

        days = await patient.days(
            hospital.slug,
            doctor.id,
            include_booked="true",
            status="all",
            patient_id=str(someone_else),
            doctor_id=str(doctor.id),
            days=60,
            lead=0,
            timezone="UTC",
        )

        assert days["2026-10-05"] == MONDAY_SLOTS[1:]


# ── Who, and what of it ──────────────────────────────────────────────────────


class TestWhoIsAnswered:
    @pytest.mark.parametrize(
        "hidden",
        [
            {"deleted": True},
            {"available": False},
            {"user_status": "suspended"},
            {"user_deleted": True},
        ],
    )
    async def test_a_doctor_who_is_not_listed_has_no_availability(
        self,
        patient: _Patient,
        hospital: Hospital,
        db_session: AsyncSession,
        hidden: dict[str, Any],
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id, **hidden)
        unknown = await patient.availability(hospital.slug, uuid.uuid4())

        answer = await patient.availability(hospital.slug, doctor.id)

        assert _error(answer) == NOT_FOUND
        assert _refusal(answer) == _refusal(unknown)

    async def test_a_doctor_is_only_ever_under_their_own_hospital(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        tag: str,
    ) -> None:
        other = await insert_hospital(db_session, name=f"{tag} Other", timezone="Asia/Kolkata")
        theirs = await insert_doctor(db_session, other.id)
        unknown = await patient.availability(hospital.slug, uuid.uuid4())

        assert (await patient.availability(other.slug, theirs.id)).status_code == 200
        for hospital_ref in (hospital.slug, hospital.id):
            assert _refusal(await patient.availability(hospital_ref, theirs.id)) == _refusal(
                unknown
            )
        assert _refusal(await patient.availability(other.slug, doctor.id)) == _refusal(unknown)

    @pytest.mark.parametrize("closed", ["flag_off", "inactive", "unusable_code"])
    async def test_a_hospital_that_is_not_shown_has_no_availability(
        self, patient: _Patient, db_session: AsyncSession, tag: str, closed: str
    ) -> None:
        states: dict[str, dict[str, Any]] = {
            "flag_off": {"enabled": False},
            "inactive": {"is_active": False},
            "unusable_code": {"slug": f"{tag}-Upper"},
        }
        hospital = await insert_hospital(db_session, name=f"{tag} Shut", **states[closed])
        doctor = await insert_doctor(db_session, hospital.id)
        unknown = await patient.availability(f"{tag}-nowhere", doctor.id)

        for ref in (hospital.slug, hospital.id):
            assert _refusal(await patient.availability(ref, doctor.id)) == _refusal(unknown)
        assert _error(unknown) == NOT_FOUND

    async def test_closing_the_hospital_or_hiding_the_doctor_takes_effect_at_once(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        assert (await patient.availability(hospital.slug, doctor.id)).status_code == 200
        await db_session.execute(
            update(Doctor).where(Doctor.id == doctor.id).values(deleted_at=func.now())
        )
        await db_session.commit()
        assert _error(await patient.availability(hospital.slug, doctor.id)) == NOT_FOUND
        await db_session.execute(
            update(Doctor).where(Doctor.id == doctor.id).values(deleted_at=None)
        )
        await db_session.commit()
        assert (await patient.availability(hospital.slug, doctor.id)).status_code == 200
        await open_hospital(db_session, hospital.id, enabled=False)
        assert _error(await patient.availability(hospital.slug, doctor.id)) == NOT_FOUND

    @pytest.mark.parametrize(
        "shape",
        [
            "x' OR '1'='1",
            "..%2F..%2Fme",
            "%00",
            "a" * 4000,
            "not-a-uuid",
            "{doctor}x",
            "{doctor_hex}",
            "{user}",
            "{hospital}",
            "{account}",
            "{availability}",
        ],
    )
    async def test_a_doctor_reference_that_is_not_one_gets_the_one_404(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        shape: str,
    ) -> None:
        availability_id = (
            await db_session.execute(
                select(func.min(func.cast(DoctorLeave.id, __import__("sqlalchemy").String)))
            )
        ).scalar() or str(uuid.uuid4())
        ref = shape.format(
            doctor=doctor.id,
            doctor_hex=doctor.id.hex,
            user=doctor.user_id,
            hospital=hospital.id,
            account=patient.account_id,
            availability=availability_id,
        )
        unknown = await patient.availability(hospital.slug, uuid.uuid4())

        assert _refusal(await patient.availability(hospital.slug, ref)) == _refusal(unknown)

    @pytest.mark.parametrize(
        "hospital_ref", ["x' OR '1'='1", "..%2F..%2Fme", "a" * 4000, "%25", "UPPER_case", "%00"]
    )
    async def test_a_hospital_reference_that_is_not_one_gets_the_one_404(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, hospital_ref: str
    ) -> None:
        unknown = await patient.availability("no-such-hospital", doctor.id)

        assert _refusal(await patient.availability(hospital_ref, doctor.id)) == _refusal(unknown)

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    async def test_availability_is_read_only(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, method: str
    ) -> None:
        response = await patient.client.request(
            method,
            f"{HOSPITALS}/{hospital.slug}/doctors/{doctor.id}/availability",
            json={},
            headers=patient.headers,
        )
        assert response.status_code == 405

    async def test_it_needs_a_patient_token(
        self, browser: AsyncClient, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        url = f"{HOSPITALS}/{hospital.slug}/doctors/{doctor.id}/availability"
        token = patient.headers["Authorization"]
        staff = create_access_token(
            doctor.user_id, hospital.id, permissions=["doctor.availability.read"]
        )
        for headers in (
            {},
            {"Authorization": "Bearer nonsense"},
            {"Authorization": token[:-2] + ("ab" if not token.endswith("ab") else "cd")},
            {"Authorization": f"Bearer {staff}"},
        ):
            response = await browser.get(url, headers=headers)
            assert response.status_code == 401, headers.keys()
            assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"
            assert "2026-10-05T10" not in response.text
        for path in (
            f"/api/v1/doctors/{doctor.id}/slots?date=2026-10-05",
            f"/api/v1/doctors/{doctor.id}/availability",
            f"/api/v1/doctors/{doctor.id}/leaves",
        ):
            assert (await browser.get(path, headers=patient.headers)).status_code == 401, path

    async def test_a_pending_required_policy_closes_availability(
        self, application: FastAPI, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        _with_a_required_policy(application)

        answers = [
            await patient.availability(hospital.slug, doctor.id),
            await patient.availability(hospital.slug, uuid.uuid4()),
            await patient.availability("no-such-hospital", doctor.id),
            await patient.availability(hospital.slug, doctor.id, start_date="2020-01-01"),
        ]

        assert {a.status_code for a in answers} == {403}
        assert len({_refusal(a) for a in answers}) == 1

    async def test_a_hospital_whose_timezone_cannot_be_used_is_a_generic_503(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        hospital = await insert_hospital(db_session, name=f"{tag} Odd", timezone="Mars/Olympus")
        doctor = await insert_doctor(db_session, hospital.id)

        response = await patient.availability(hospital.slug, doctor.id)

        assert response.status_code == 503
        assert response.json()["error_code"] == "SERVICE_UNAVAILABLE"
        assert "Mars" not in response.text and "Olympus" not in response.text


class TestNothingLeaksAndNothingIsWritten:
    async def test_the_answer_carries_nothing_of_anybody_s_appointment_or_leave(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        booked = await insert_appointment(
            db_session, doctor, someone_else, _ist(MONDAY, 10), _ist(MONDAY, 10, 15)
        )
        leave = await insert_leave(db_session, doctor, _ist(MONDAY, 10, 30), _ist(MONDAY, 11))

        response = await patient.availability(hospital.slug, doctor.id)

        raw = response.text
        for word in (
            *INTERNAL,
            str(booked.id),
            str(leave.id),
            str(someone_else),
            str(doctor.user_id),
            str(hospital.id),
            "+919111111111",
        ):
            assert word not in raw, word
        data = response.json()["data"]
        assert set(data) == TOP_KEYS
        assert all(set(day) == {"date", "slots"} for day in data["days"])
        assert all(set(slot) == {"start", "end"} for day in data["days"] for slot in day["slots"])

    async def test_reading_writes_nothing_and_reserves_nothing(
        self,
        patient: _Patient,
        browser: AsyncClient,
        sms: FakeSmsSender,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        """Two patients see the same free slot; neither request changes a row."""
        other = _Patient(browser, await sign_in(browser, sms, new_phone()))
        counts = {
            table: select(func.count()).select_from(table)
            for table in (Appointment, DoctorLeave, AuditLog)
        }
        before = {
            name: (await db_session.execute(stmt)).scalar_one() for name, stmt in counts.items()
        }

        mine = await patient.days(hospital.slug, doctor.id)
        theirs = await other.days(hospital.slug, doctor.id)
        await patient.availability(hospital.slug, doctor.id, start_date="2020-01-01")
        await patient.availability(hospital.slug, uuid.uuid4())
        again = await patient.days(hospital.slug, doctor.id)

        assert mine == theirs == again
        assert _iso(MONDAY, 10) in mine["2026-10-05"]
        after = {
            name: (await db_session.execute(stmt)).scalar_one() for name, stmt in counts.items()
        }
        assert after == before

    async def test_the_reads_do_not_grow_with_the_range(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
        statements: list[str],
    ) -> None:
        for day in range(14):
            await insert_appointment(
                db_session,
                doctor,
                someone_else,
                _ist(MONDAY, 10) + timedelta(days=day),
                _ist(MONDAY, 10, 15) + timedelta(days=day),
            )
        await insert_leave(db_session, doctor, _ist(MONDAY, 11), _ist(MONDAY, 11, 15))

        statements.clear()
        assert (
            await patient.availability(
                hospital.slug, doctor.id, start_date="2026-10-05", end_date="2026-10-05"
            )
        ).status_code == 200
        one_day = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        statements.clear()
        assert (
            await patient.availability(
                hospital.slug, doctor.id, start_date="2026-10-05", end_date="2026-10-18"
            )
        ).status_code == 200
        fourteen_days = [s for s in statements if s.lstrip().upper().startswith("SELECT")]

        assert one_day == fourteen_days
        assert len(fourteen_days) <= 12, fourteen_days
        assert any("doctor_leaves" in s for s in fourteen_days)
        assert any("FROM appointments" in s for s in fourteen_days)


def _with_a_required_policy(application: FastAPI) -> None:
    required = (Policy(ConsentPurpose.TERMS_OF_SERVICE, "2027-01", "platform", required=True),)

    def _consent(
        consents: PatientConsentRepository = Depends(get_patient_consent_repository),
        links: PatientAccountLinkRepository = Depends(get_patient_account_link_repository),
        uow: UnitOfWork = Depends(get_unit_of_work),
        audit: AuditSink = Depends(get_audit_sink),
    ) -> ConsentService:
        return ConsentService(consents, links, uow, audit, policies=required)

    application.dependency_overrides[get_consent_service] = _consent
