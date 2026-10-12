"""API and adversarial tests for Patient App appointment booking (Task 32).

``docs/modules/15-patient-app.md`` §13. Over HTTP, through the real
application and a real PostgreSQL. Slots are on a real future Monday, because
the hospital's own booking path — which creates the row — reads the real
clock; where a test needs "now" to sit next to a slot, it moves the patient
layer's clock, which is the one that applies the lead time and the horizon.

Racing requests need separate database connections and are tested in
``app/tests/integration/test_patient_booking_concurrency.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi import Depends
from sqlalchemy import func, select, update

from app.api.dependencies.patient import get_consent_service
from app.api.dependencies.repositories import (
    get_patient_account_link_repository,
    get_patient_consent_repository,
)
from app.api.dependencies.services import get_audit_sink, get_unit_of_work
from app.core import security
from app.core.audit import AuditSink  # noqa: TC001 — FastAPI resolves the override at runtime
from app.core.config import settings
from app.core.security import create_access_token, create_patient_access_token
from app.database.unit_of_work import UnitOfWork  # noqa: TC001 — as above
from app.models.appointment import Appointment, AppointmentStatus, AppointmentStatusHistory
from app.models.audit_log import AuditLog
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.patient_account import PatientAccount, PatientAccountLink
from app.models.patient_consent import ConsentPurpose
from app.repositories import (  # noqa: TC001 — as above
    PatientAccountLinkRepository,
    PatientConsentRepository,
)
from app.services.patient_app import availability_service, booking_service
from app.services.patient_app.consent_service import ConsentService
from app.services.patient_app.policies import Policy
from app.tests.patient_app_helpers import (
    PATIENT,
    POLICY,
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
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.hospital import Hospital

pytestmark = pytest.mark.database

HOSPITALS = f"{PATIENT}/hospitals"
IST = ZoneInfo("Asia/Kolkata")
DOB = "1990-05-17"
FIELDS = {"ref", "status", "type", "start", "end", "timezone", "hospital", "doctor", "reason"}
NOT_FOUND = (404, "RESOURCE_NOT_FOUND", "Not Found")
TAKEN = (409, "RESOURCE_CONFLICT", "This time is no longer available.")
KEY_REUSED = (409, "RESOURCE_CONFLICT", "This request was already used for another booking.")
NOT_BOOKABLE = (400, "BUSINESS_RULE_VIOLATION", "This time cannot be booked.")
OWN_OVERLAP = (400, "BUSINESS_RULE_VIOLATION", "You already have an appointment at this time.")
LIMIT = (
    400,
    "BUSINESS_RULE_VIOLATION",
    "You have reached the limit of upcoming appointments at this hospital.",
)
LINK_REQUIRED = (403, "RECORD_LINK_REQUIRED", "Link your record at this hospital to continue.")


def _next_monday(zone: ZoneInfo = IST) -> date:
    """A Monday 7–13 days from today at the hospital: in the future, inside the horizon."""
    day = datetime.now(zone).date() + timedelta(days=7)
    return day + timedelta(days=(7 - day.weekday()) % 7)


MONDAY = _next_monday()


def _at(hour: int, minute: int = 0, *, day: date = MONDAY, zone: ZoneInfo = IST) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=zone)


def _slot(hour: int, minute: int = 0, *, minutes: int = 15) -> dict[str, str]:
    start = _at(hour, minute)
    return {"start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat()}


def _key() -> str:
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _patient_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_HOSPITAL_PER_MIN", 1_000_000)


class _Clock:
    """The patient layer's idea of now. ``None`` is the real clock."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def set(self, now: datetime) -> None:
        instant = now.astimezone(UTC)
        self._monkeypatch.setattr(booking_service, "utc_now", lambda: instant)
        self._monkeypatch.setattr(availability_service, "utc_now", lambda: instant)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    return _Clock(monkeypatch)


@pytest.fixture
def sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def tag() -> str:
    return f"b{uuid.uuid4().hex[:11]}"


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
    """A signed-in patient."""

    def __init__(self, client: AsyncClient, phone: str, session: dict[str, Any]) -> None:
        self.client = client
        self.phone = phone
        self.account_id = uuid.UUID(session["account"]["id"])
        self.token = session["access_token"]
        self.headers = bearer(self.token)
        self.record_id: uuid.UUID | None = None

    async def link(self, db_session: AsyncSession, hospital: Hospital) -> uuid.UUID:
        """Give the patient a record at the hospital and link the account to it."""
        record = await insert_patient_record(db_session, hospital.id, phone=self.phone)
        response = await self.client.post(
            f"{HOSPITALS}/{hospital.slug}/link",
            json={"date_of_birth": DOB, "consent_policy_version": POLICY},
            headers=self.headers,
        )
        assert response.status_code == 201, response.text
        self.record_id = record.id
        return record.id

    async def book(
        self,
        hospital_ref: object,
        doctor_ref: object,
        slot: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        **body: Any,
    ) -> Response:
        sent = {"Idempotency-Key": key or _key(), **self.headers, **(extra_headers or {})}
        return await self.client.post(
            f"{HOSPITALS}/{hospital_ref}/doctors/{doctor_ref}/appointments",
            json={**(slot if slot is not None else _slot(10)), **body},
            headers=sent,
        )

    async def free(self, hospital_ref: object, doctor_ref: object) -> list[str]:
        """The Monday slots availability still offers."""
        response = await self.client.get(
            f"{HOSPITALS}/{hospital_ref}/doctors/{doctor_ref}/availability",
            params={"start_date": str(MONDAY), "end_date": str(MONDAY)},
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        return [slot["start"] for slot in response.json()["data"]["days"][0]["slots"]]


def _error(response: Response) -> tuple[int, str, str]:
    body = response.json()
    return response.status_code, body["error_code"], body["message"]


def _refusal(response: Response) -> str:
    body = {k: v for k, v in response.json().items() if "request" not in k and k != "metadata"}
    return json.dumps([response.status_code, body], sort_keys=True)


async def _rows(db_session: AsyncSession, **where: Any) -> list[Appointment]:
    stmt = select(Appointment).execution_options(populate_existing=True)
    for column, value in where.items():
        stmt = stmt.where(getattr(Appointment, column) == value)
    return list(
        (await db_session.execute(stmt.order_by(Appointment.scheduled_start, Appointment.id)))
        .unique()
        .scalars()
    )


@pytest_asyncio.fixture
async def hospital(db_session: AsyncSession, tag: str) -> Hospital:
    return await insert_hospital(db_session, name=f"{tag} General", timezone="Asia/Kolkata")


@pytest_asyncio.fixture
async def doctor(db_session: AsyncSession, hospital: Hospital) -> Doctor:
    """A listed doctor with a Monday 09:00–12:00 window of 15-minute slots."""
    return await insert_doctor(db_session, hospital.id)


@pytest_asyncio.fixture
async def patient(
    browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession, hospital: Hospital
) -> _Patient:
    """A patient with a linked record at the hospital."""
    signed_in = _Patient(browser, phone := new_phone(), await sign_in(browser, sms, phone))
    await signed_in.link(db_session, hospital)
    return signed_in


@pytest_asyncio.fixture
async def stranger(browser: AsyncClient, sms: FakeSmsSender) -> _Patient:
    """A signed-in patient with no record anywhere."""
    return _Patient(browser, phone := new_phone(), await sign_in(browser, sms, phone))


@pytest_asyncio.fixture
async def someone_else(db_session: AsyncSession, hospital: Hospital) -> uuid.UUID:
    """Another patient's record, to book the doctor for."""
    record = await insert_patient_record(
        db_session, hospital.id, phone="+919111111111", first_name="OTHERCANARY"
    )
    return record.id


# ── A booking ────────────────────────────────────────────────────────────────


class TestBooking:
    async def test_a_slot_becomes_a_real_appointment_for_the_callers_own_record(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        key = _key()
        assert _slot(10)["start"] in await patient.free(hospital.slug, doctor.id)

        response = await patient.book(
            hospital.slug, doctor.id, _slot(10), key=key, reason=" Cough "
        )

        assert response.status_code == 201, response.text
        data = response.json()["data"]
        [row] = await _rows(db_session, doctor_id=doctor.id)
        assert set(data) == FIELDS
        assert data == {
            "ref": str(row.id),
            "status": "booked",
            "type": "new",
            "start": _slot(10)["start"],
            "end": _slot(10)["end"],
            "timezone": "Asia/Kolkata",
            "hospital": {"ref": hospital.slug, "name": hospital.name},
            "doctor": {"ref": str(doctor.id), "name": "Asha Menon", "specialization": "Cardiology"},
            "reason": "Cough",
        }
        assert (row.hospital_id, row.patient_id, row.doctor_id) == (
            hospital.id,
            patient.record_id,
            doctor.id,
        )
        assert (row.scheduled_start, row.scheduled_end) == (_at(10), _at(10, 15))
        assert row.status is AppointmentStatus.BOOKED
        assert (row.created_by, row.notes, row.reason) == (None, None, "Cough")
        assert row.idempotency_key == f"pt:{patient.account_id}:{key}"
        history = (
            (
                await db_session.execute(
                    select(AppointmentStatusHistory).where(
                        AppointmentStatusHistory.appointment_id == row.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(h.from_status, h.to_status, h.changed_by) for h in history] == [
            (None, AppointmentStatus.BOOKED, None)
        ]
        assert _slot(10)["start"] not in await patient.free(hospital.slug, doctor.id)

    async def test_the_booking_is_audited_once_as_the_patients_act(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        response = await patient.book(hospital.slug, doctor.id, reason="PRIVATEREASON headache")

        ref = uuid.UUID(response.json()["data"]["ref"])
        rows = (
            (await db_session.execute(select(AuditLog).where(AuditLog.target_id == ref)))
            .scalars()
            .all()
        )
        assert [row.action for row in rows] == ["patient.appointment.booked"]
        [row] = rows
        assert (row.hospital_id, row.target_type, row.actor_type) == (
            hospital.id,
            "appointment",
            "patient",
        )
        assert (row.patient_account_id, row.actor_user_id) == (patient.account_id, None)
        assert row.context == {"doctor_id": str(doctor.id), "type": "new"}
        blob = json.dumps([row.context, row.before, row.after], default=str)
        for secret in ("PRIVATEREASON", patient.phone, patient.token, DOB):
            assert secret not in blob

    @pytest.mark.parametrize("kind", ["new", "follow_up"])
    async def test_a_patient_may_book_a_new_visit_or_a_follow_up(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, kind: str
    ) -> None:
        response = await patient.book(hospital.slug, doctor.id, type=kind)
        assert (response.status_code, response.json()["data"]["type"]) == (201, kind)

    async def test_the_same_instant_written_in_another_offset_is_the_same_slot(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        slot = {
            "start": _at(10).astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end": _at(10, 15).astimezone(ZoneInfo("America/New_York")).isoformat(),
        }

        response = await patient.book(hospital.slug, doctor.id, slot)

        assert response.status_code == 201, response.text
        assert response.json()["data"]["start"] == _slot(10)["start"]
        assert response.json()["data"]["end"] == _slot(10)["end"]

    @pytest.mark.parametrize("timezone", ["Pacific/Kiritimati", "Pacific/Pago_Pago", "UTC"])
    async def test_a_hospital_far_from_utc_books_its_own_local_slot(
        self,
        browser: AsyncClient,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        tag: str,
        timezone: str,
    ) -> None:
        zone = ZoneInfo(timezone)
        hospital = await insert_hospital(db_session, name=f"{tag} Far", timezone=timezone)
        doctor = await insert_doctor(db_session, hospital.id)
        monday = _next_monday(zone)
        patient = _Patient(browser, phone := new_phone(), await sign_in(browser, sms, phone))
        await patient.link(db_session, hospital)
        start = _at(10, day=monday, zone=zone)

        response = await patient.book(
            hospital.slug,
            doctor.id,
            {"start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat()},
        )

        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert (data["timezone"], datetime.fromisoformat(data["start"])) == (timezone, start)
        assert datetime.fromisoformat(data["start"]).utcoffset() == start.utcoffset()
        [row] = await _rows(db_session, doctor_id=doctor.id)
        assert row.scheduled_start == start
        assert row.scheduled_start.astimezone(zone).date() == monday

    async def test_the_response_and_refusals_carry_nothing_internal(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        other = await insert_appointment(db_session, doctor, someone_else, _at(11), _at(11, 15))
        booked = await patient.book(hospital.slug, doctor.id, _slot(10))
        refused = await patient.book(hospital.slug, doctor.id, _slot(11))

        assert _error(refused) == TAKEN
        for raw in (booked.text, refused.text):
            for word in (
                str(other.id),
                str(someone_else),
                str(patient.record_id),
                str(patient.account_id),
                str(hospital.id),
                str(doctor.user_id),
                "OTHERCANARY",
                "APPTCANARY",
                "NOTESCANARY",
                "conflicting",
                "patient_id",
                "created_by",
                "notes",
                "idempotency",
                "staff-secret",
                "LICCANARY",
                "consultation_fee",
            ):
                assert word not in raw, word


# ── Idempotency ──────────────────────────────────────────────────────────────


class TestIdempotency:
    async def test_the_same_request_with_the_same_key_is_the_same_appointment(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        key = _key()
        first = await patient.book(hospital.slug, doctor.id, _slot(10), key=key, reason="Cough")
        again = [
            await patient.book(hospital.slug, doctor.id, _slot(10), key=key, reason="Cough")
            for _ in range(3)
        ]

        assert first.status_code == 201
        assert {response.status_code for response in again} == {200}
        assert {json.dumps(r.json()["data"], sort_keys=True) for r in (first, *again)} == {
            json.dumps(first.json()["data"], sort_keys=True)
        }
        assert len(await _rows(db_session, doctor_id=doctor.id)) == 1
        audited = await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "patient.appointment.booked")
            .where(AuditLog.patient_account_id == patient.account_id)
        )
        assert audited.scalar_one() == 1

    async def test_a_replay_is_answered_even_though_the_slot_is_now_taken_by_itself(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, clock: _Clock
    ) -> None:
        """A network-style retry: the first answer was lost, and time has moved on."""
        key = _key()
        first = await patient.book(hospital.slug, doctor.id, _slot(10), key=key)
        clock.set(_at(9, 30))  # the slot is now inside the lead time

        retry = await patient.book(hospital.slug, doctor.id, _slot(10), key=key)

        assert (first.status_code, retry.status_code) == (201, 200)
        assert retry.json()["data"]["ref"] == first.json()["data"]["ref"]

    async def test_the_same_key_for_another_slot_or_doctor_is_refused_and_books_nothing(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        other_doctor = await insert_doctor(db_session, hospital.id, last_name="Other")
        key = _key()
        first = await patient.book(hospital.slug, doctor.id, _slot(10), key=key)

        another_slot = await patient.book(hospital.slug, doctor.id, _slot(11), key=key)
        longer = await patient.book(hospital.slug, doctor.id, _slot(10, minutes=30), key=key)
        another_doctor = await patient.book(hospital.slug, other_doctor.id, _slot(10), key=key)

        assert first.status_code == 201
        assert [_error(r) for r in (another_slot, longer, another_doctor)] == [KEY_REUSED] * 3
        assert first.json()["data"]["ref"] not in another_slot.text
        assert len(await _rows(db_session, hospital_id=hospital.id)) == 1

    async def test_a_key_is_the_patients_own_another_patient_may_use_the_same_text(
        self,
        patient: _Patient,
        browser: AsyncClient,
        sms: FakeSmsSender,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        """Attack: replay another patient's key to read or take over their appointment."""
        other = _Patient(browser, phone := new_phone(), await sign_in(browser, sms, phone))
        await other.link(db_session, hospital)
        key = _key()
        mine = await patient.book(hospital.slug, doctor.id, _slot(10), key=key)

        same_slot = await other.book(hospital.slug, doctor.id, _slot(10), key=key)
        own_slot = await other.book(hospital.slug, doctor.id, _slot(11), key=key)

        assert _error(same_slot) == TAKEN
        assert mine.json()["data"]["ref"] not in same_slot.text
        assert own_slot.status_code == 201
        assert own_slot.json()["data"]["ref"] != mine.json()["data"]["ref"]
        rows = await _rows(db_session, doctor_id=doctor.id)
        assert {row.patient_id for row in rows} == {patient.record_id, other.record_id}
        assert {row.idempotency_key for row in rows} == {
            f"pt:{patient.account_id}:{key}",
            f"pt:{other.account_id}:{key}",
        }

    async def test_a_refused_request_does_not_use_up_its_key(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        taken = await insert_appointment(db_session, doctor, someone_else, _at(10), _at(10, 15))
        key = _key()
        refused = await patient.book(hospital.slug, doctor.id, _slot(10), key=key)

        await db_session.execute(
            update(Appointment)
            .where(Appointment.id == taken.id)
            .values(status=AppointmentStatus.CANCELLED)
        )
        await db_session.commit()
        retried = await patient.book(hospital.slug, doctor.id, _slot(10), key=key)

        assert _error(refused) == TAKEN
        assert retried.status_code == 201

    @pytest.mark.parametrize(
        "key",
        [
            None,
            "",
            "short",
            "a" * 15,
            "a" * 65,
            "has space in the key!!",
            "key-with-a-dot.and-a-slash/x",
            "a" * 4000,
            "' OR 1=1 -- padding",
        ],
    )
    async def test_a_missing_or_malformed_key_is_refused_and_books_nothing(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        key: str | None,
    ) -> None:
        headers = dict(patient.headers)
        if key is not None:
            headers["Idempotency-Key"] = key
        response = await patient.client.post(
            f"{HOSPITALS}/{hospital.slug}/doctors/{doctor.id}/appointments",
            json=_slot(10),
            headers=headers,
        )

        assert response.status_code == 422, response.text
        assert await _rows(db_session, doctor_id=doctor.id) == []

    @pytest.mark.parametrize("key", ["a" * 16, "A_b-9" * 12 + "abcd", str(uuid.uuid4())])
    async def test_the_documented_key_forms_are_accepted(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, key: str
    ) -> None:
        assert (await patient.book(hospital.slug, doctor.id, key=key)).status_code == 201


# ── The slot is checked again ────────────────────────────────────────────────


class TestTheSlotIsRevalidated:
    @pytest.mark.parametrize(
        ("status", "outcome"),
        [
            (AppointmentStatus.BOOKED, 409),
            (AppointmentStatus.CHECKED_IN, 409),
            (AppointmentStatus.IN_PROGRESS, 409),
            (AppointmentStatus.COMPLETED, 409),
            (AppointmentStatus.CANCELLED, 201),
            (AppointmentStatus.NO_SHOW, 201),
        ],
    )
    async def test_a_slot_somebody_holds_is_refused_and_one_that_was_freed_is_booked(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
        status: AppointmentStatus,
        outcome: int,
    ) -> None:
        """The availability screen showed it free; the database decides."""
        await insert_appointment(
            db_session, doctor, someone_else, _at(10), _at(10, 15), status=status
        )

        response = await patient.book(hospital.slug, doctor.id, _slot(10))

        assert response.status_code == outcome, response.text
        if outcome == 409:
            assert _error(response) == TAKEN
        mine = await _rows(db_session, doctor_id=doctor.id, patient_id=patient.record_id)
        assert len(mine) == (1 if outcome == 201 else 0)

    async def test_an_appointment_overlapping_part_of_the_slot_takes_it(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        await insert_appointment(db_session, doctor, someone_else, _at(10, 10), _at(10, 25))

        assert _error(await patient.book(hospital.slug, doctor.id, _slot(10))) == TAKEN
        assert _error(await patient.book(hospital.slug, doctor.id, _slot(10, 15))) == TAKEN
        assert (await patient.book(hospital.slug, doctor.id, _slot(10, 30))).status_code == 201

    async def test_a_leave_recorded_after_the_patient_looked_takes_the_slot(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        assert _slot(10)["start"] in await patient.free(hospital.slug, doctor.id)
        await insert_leave(db_session, doctor, _at(10), _at(10, 30))

        assert _error(await patient.book(hospital.slug, doctor.id, _slot(10))) == TAKEN
        assert _error(await patient.book(hospital.slug, doctor.id, _slot(10, 15))) == TAKEN
        assert (await patient.book(hospital.slug, doctor.id, _slot(10, 30))).status_code == 201
        assert "LEAVECANARY" not in (await patient.book(hospital.slug, doctor.id, _slot(10))).text

    @pytest.mark.parametrize(
        "slot",
        [
            _slot(10, 5),  # off the grid
            _slot(10, minutes=30),  # two slots as one
            _slot(10, minutes=10),  # shorter than a slot
            _slot(10, minutes=20),  # a valid duration elsewhere, not this doctor's
            _slot(8, 45),  # before the window
            _slot(12),  # after the window
            _slot(11, 55, minutes=15),  # runs past the window
            {
                "start": _at(10, day=MONDAY + timedelta(days=1)).isoformat(),
                "end": _at(10, 15, day=MONDAY + timedelta(days=1)).isoformat(),
            },  # a day with no window
            {"start": _at(10, 15).isoformat(), "end": _at(10).isoformat()},  # backwards
            {"start": _at(10).isoformat(), "end": _at(10).isoformat()},  # empty
        ],
    )
    async def test_a_time_the_engine_does_not_offer_cannot_be_booked(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        slot: dict[str, str],
    ) -> None:
        """Attack: book an arbitrary time by posting it instead of picking it."""
        response = await patient.book(hospital.slug, doctor.id, slot)

        assert _error(response) == NOT_BOOKABLE
        assert await _rows(db_session, doctor_id=doctor.id) == []

    async def test_the_window_is_read_now_not_when_the_patient_looked(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        assert _slot(10)["start"] in await patient.free(hospital.slug, doctor.id)
        await set_windows(db_session, doctor, [(0, time(14), time(15), 30)])

        assert _error(await patient.book(hospital.slug, doctor.id, _slot(10))) == NOT_BOOKABLE
        assert (
            await patient.book(hospital.slug, doctor.id, _slot(14, minutes=30))
        ).status_code == 201

    async def test_the_lead_time_is_applied_at_the_moment_of_booking(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, clock: _Clock
    ) -> None:
        """Default lead: 60 minutes. The 10:00 slot is bookable until 09:00 sharp."""
        clock.set(_at(9, 0) + timedelta(seconds=1))
        assert _error(await patient.book(hospital.slug, doctor.id, _slot(10))) == NOT_BOOKABLE

        clock.set(_at(9, 0))
        assert (await patient.book(hospital.slug, doctor.id, _slot(10))).status_code == 201

    async def test_a_slot_that_has_started_or_passed_cannot_be_booked(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, clock: _Clock
    ) -> None:
        clock.set(_at(10, 5))
        assert _error(await patient.book(hospital.slug, doctor.id, _slot(10))) == NOT_BOOKABLE
        clock.set(_at(10, day=MONDAY + timedelta(days=1)))
        assert _error(await patient.book(hospital.slug, doctor.id, _slot(10))) == NOT_BOOKABLE

    async def test_the_horizon_is_applied_at_the_moment_of_booking(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, clock: _Clock
    ) -> None:
        """Default horizon: 30 days. MONDAY is bookable from 30 days before, not 31."""
        clock.set(_at(23, 59, day=MONDAY - timedelta(days=31)))
        assert _error(await patient.book(hospital.slug, doctor.id, _slot(10))) == NOT_BOOKABLE

        clock.set(_at(0, 0, day=MONDAY - timedelta(days=30)))
        assert (await patient.book(hospital.slug, doctor.id, _slot(10))).status_code == 201

    async def test_a_hospital_s_own_policy_is_the_one_applied(
        self,
        browser: AsyncClient,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        tag: str,
        clock: _Clock,
    ) -> None:
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} Strict",
            timezone="Asia/Kolkata",
            settings={
                "patient_app.booking_horizon_days": 3,
                "patient_app.min_lead_minutes": 120,
                "patient_app.max_active_bookings": 1,
            },
        )
        doctor = await insert_doctor(db_session, hospital.id)
        patient = _Patient(browser, phone := new_phone(), await sign_in(browser, sms, phone))
        await patient.link(db_session, hospital)

        clock.set(_at(9, day=MONDAY - timedelta(days=4)))
        assert _error(await patient.book(hospital.slug, doctor.id, _slot(11))) == NOT_BOOKABLE
        clock.set(_at(9, 1))
        assert _error(await patient.book(hospital.slug, doctor.id, _slot(11))) == NOT_BOOKABLE
        clock.set(_at(9, 0))
        assert (await patient.book(hospital.slug, doctor.id, _slot(11))).status_code == 201
        assert _error(await patient.book(hospital.slug, doctor.id, _slot(11, 30))) == LIMIT


# ── The patient's own calendar ───────────────────────────────────────────────


class TestThePatientsOwnCalendar:
    async def test_two_doctors_at_the_same_time_is_refused(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        other_doctor = await insert_doctor(db_session, hospital.id, last_name="Other")
        await set_windows(db_session, other_doctor, [(0, time(9), time(12), 30)])
        assert (await patient.book(hospital.slug, doctor.id, _slot(10))).status_code == 201

        same_time = await patient.book(hospital.slug, other_doctor.id, _slot(10, minutes=30))
        later = await patient.book(hospital.slug, other_doctor.id, _slot(10, 30, minutes=30))

        assert _error(same_time) == OWN_OVERLAP
        assert later.status_code == 201

    async def test_several_slots_with_one_doctor_are_allowed_up_to_the_limit(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        """Default limit: three upcoming booked appointments at one hospital."""
        booked = [await patient.book(hospital.slug, doctor.id, _slot(10, m)) for m in (0, 15, 30)]
        fourth = await patient.book(hospital.slug, doctor.id, _slot(10, 45))

        assert [r.status_code for r in booked] == [201, 201, 201]
        assert _error(fourth) == LIMIT
        assert len(await _rows(db_session, patient_id=patient.record_id)) == 3

        first = uuid.UUID(booked[0].json()["data"]["ref"])
        await db_session.execute(
            update(Appointment)
            .where(Appointment.id == first)
            .values(status=AppointmentStatus.CANCELLED)
        )
        await db_session.commit()
        assert (await patient.book(hospital.slug, doctor.id, _slot(10, 45))).status_code == 201

    async def test_the_limit_counts_this_hospital_only(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        tag: str,
    ) -> None:
        other = await insert_hospital(db_session, name=f"{tag} Other", timezone="Asia/Kolkata")
        other_doctor = await insert_doctor(db_session, other.id)
        await patient.link(db_session, other)
        for minute in (0, 15, 30):
            assert (
                await patient.book(hospital.slug, doctor.id, _slot(11, minute))
            ).status_code == 201

        assert (await patient.book(other.slug, other_doctor.id, _slot(9))).status_code == 201


# ── Ownership ────────────────────────────────────────────────────────────────


class TestOwnership:
    @pytest.mark.parametrize(
        "extra",
        [
            {"patient_id": "{victim}"},
            {"hospital_id": "{other_hospital}"},
            {"doctor_id": "{other_doctor}"},
            {"account_id": "{victim}"},
            {"user_id": "{victim}"},
            {"created_by": "{victim}"},
            {"status": "completed"},
            {"notes": "reception note"},
            {"allow_override": True},
            {"idempotency_key": "pt:x:y"},
            {"scheduled_start": "2020-01-01T00:00:00Z"},
        ],
    )
    async def test_there_is_no_field_to_name_a_patient_hospital_or_doctor(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
        extra: dict[str, Any],
    ) -> None:
        """Attack: book as another patient, or elsewhere, by adding a field."""
        other_doctor = await insert_doctor(db_session, hospital.id, last_name="Other")
        filled: dict[str, Any] = {
            name: value.format(
                victim=someone_else, other_hospital=uuid.uuid4(), other_doctor=other_doctor.id
            )
            if isinstance(value, str)
            else value
            for name, value in extra.items()
        }

        response = await patient.book(hospital.slug, doctor.id, _slot(10), **filled)

        assert response.status_code == 422, response.text
        assert await _rows(db_session, hospital_id=hospital.id) == []

    async def test_the_appointment_is_always_the_callers_own_record(
        self,
        patient: _Patient,
        browser: AsyncClient,
        sms: FakeSmsSender,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        other = _Patient(browser, phone := new_phone(), await sign_in(browser, sms, phone))
        await other.link(db_session, hospital)

        mine = await patient.book(
            hospital.slug,
            doctor.id,
            _slot(10),
            extra_headers={
                "X-Patient-Id": str(other.record_id),
                "X-Account-Id": str(other.account_id),
            },
        )
        theirs = await other.book(hospital.slug, doctor.id, _slot(11))

        assert (mine.status_code, theirs.status_code) == (201, 201)
        [my_row] = await _rows(db_session, patient_id=patient.record_id)
        [their_row] = await _rows(db_session, patient_id=other.record_id)
        assert str(my_row.id) == mine.json()["data"]["ref"]
        assert str(their_row.id) == theirs.json()["data"]["ref"]

    async def test_without_a_record_link_at_this_hospital_nothing_is_booked(
        self,
        stranger: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        tag: str,
    ) -> None:
        """A link elsewhere, or a record that merely carries the caller's phone, is not a link here."""
        other = await insert_hospital(db_session, name=f"{tag} Other", timezone="Asia/Kolkata")
        await stranger.link(db_session, other)
        await insert_patient_record(db_session, hospital.id, phone=stranger.phone)

        response = await stranger.book(hospital.slug, doctor.id)

        assert _error(response) == LINK_REQUIRED
        assert await _rows(db_session, hospital_id=hospital.id) == []

    @pytest.mark.parametrize("stale", ["unlinked", "phone_changed", "record_deleted"])
    async def test_a_link_that_is_no_longer_honoured_books_nothing(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        stale: str,
    ) -> None:
        if stale == "unlinked":
            await db_session.execute(
                update(PatientAccountLink)
                .where(PatientAccountLink.account_id == patient.account_id)
                .values(unlinked_at=func.now())
            )
        elif stale == "phone_changed":
            await db_session.execute(
                update(Patient).where(Patient.id == patient.record_id).values(phone="+919333333333")
            )
        else:
            await db_session.execute(
                update(Patient).where(Patient.id == patient.record_id).values(deleted_at=func.now())
            )
        await db_session.commit()

        response = await patient.book(hospital.slug, doctor.id)

        assert _error(response) == LINK_REQUIRED
        assert await _rows(db_session, hospital_id=hospital.id) == []

    async def test_a_suspended_or_closed_account_books_nothing(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        await db_session.execute(
            update(PatientAccount)
            .where(PatientAccount.id == patient.account_id)
            .values(status="suspended")
        )
        await db_session.commit()

        response = await patient.book(hospital.slug, doctor.id)

        assert response.status_code == 401
        assert await _rows(db_session, hospital_id=hospital.id) == []


# ── Hospital and doctor ──────────────────────────────────────────────────────


class TestHospitalAndDoctor:
    async def test_a_doctor_is_booked_only_under_their_own_hospital(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        tag: str,
    ) -> None:
        """Attack: Hospital A's path with Hospital B's doctor, and the reverse — linked at both."""
        other = await insert_hospital(db_session, name=f"{tag} Other", timezone="Asia/Kolkata")
        theirs = await insert_doctor(db_session, other.id)
        await patient.link(db_session, other)
        unknown = await patient.book(hospital.slug, uuid.uuid4())

        crossed = [
            await patient.book(hospital.slug, theirs.id),
            await patient.book(hospital.id, theirs.id),
            await patient.book(other.slug, doctor.id),
        ]

        assert _error(unknown) == NOT_FOUND
        assert {_refusal(r) for r in crossed} == {_refusal(unknown)}
        assert await _rows(db_session, hospital_id=hospital.id) == []
        assert await _rows(db_session, hospital_id=other.id) == []
        assert (await patient.book(other.slug, theirs.id)).status_code == 201

    @pytest.mark.parametrize(
        "hidden",
        [
            {"deleted": True},
            {"available": False},
            {"user_status": "suspended"},
            {"user_deleted": True},
        ],
    )
    async def test_a_doctor_who_is_not_listed_cannot_be_booked(
        self,
        patient: _Patient,
        hospital: Hospital,
        db_session: AsyncSession,
        hidden: dict[str, Any],
    ) -> None:
        doctor = await insert_doctor(db_session, hospital.id, **hidden)
        unknown = await patient.book(hospital.slug, uuid.uuid4())

        response = await patient.book(hospital.slug, doctor.id)

        assert _refusal(response) == _refusal(unknown)
        assert await _rows(db_session, hospital_id=hospital.id) == []

    async def test_a_doctor_deactivated_after_the_patient_looked_cannot_be_booked(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        assert _slot(10)["start"] in await patient.free(hospital.slug, doctor.id)
        await db_session.execute(
            update(Doctor).where(Doctor.id == doctor.id).values(deleted_at=func.now())
        )
        await db_session.commit()

        assert _error(await patient.book(hospital.slug, doctor.id)) == NOT_FOUND
        assert await _rows(db_session, hospital_id=hospital.id) == []

    @pytest.mark.parametrize("closed", ["flag_off", "inactive"])
    async def test_a_hospital_closed_after_the_patient_linked_books_nothing(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        closed: str,
    ) -> None:
        if closed == "flag_off":
            await open_hospital(db_session, hospital.id, enabled=False)
        else:
            from app.models.hospital import Hospital as HospitalRow

            await db_session.execute(
                update(HospitalRow).where(HospitalRow.id == hospital.id).values(is_active=False)
            )
            await db_session.commit()

        for ref in (hospital.slug, hospital.id):
            assert _error(await patient.book(ref, doctor.id)) == NOT_FOUND
        assert await _rows(db_session, hospital_id=hospital.id) == []

    @pytest.mark.parametrize(
        ("hospital_ref", "doctor_ref"),
        [
            ("{hospital}", "x' OR '1'='1"),
            ("{hospital}", "..%2F..%2Fme"),
            ("{hospital}", "a" * 4000),
            ("{hospital}", "{doctor_hex}"),
            ("{hospital}", "{user}"),
            ("{hospital}", "{record}"),
            ("x' OR '1'='1", "{doctor}"),
            ("UPPER_case", "{doctor}"),
            ("a" * 4000, "{doctor}"),
            ("no-such-hospital", "{doctor}"),
        ],
    )
    async def test_a_reference_that_is_not_one_gets_the_one_404_and_books_nothing(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        hospital_ref: str,
        doctor_ref: str,
    ) -> None:
        fill = {
            "hospital": hospital.slug,
            "doctor": doctor.id,
            "doctor_hex": doctor.id.hex,
            "user": doctor.user_id,
            "record": patient.record_id,
        }
        unknown = await patient.book(hospital.slug, uuid.uuid4())

        response = await patient.book(hospital_ref.format(**fill), doctor_ref.format(**fill))

        assert _refusal(response) == _refusal(unknown)
        assert await _rows(db_session, hospital_id=hospital.id) == []


# ── Tokens, policy, body ─────────────────────────────────────────────────────


class TestGates:
    async def test_it_needs_a_genuine_unexpired_patient_token(
        self,
        browser: AsyncClient,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: book with no token, a forged, expired or staff token, or a token in a cookie."""
        staff = create_access_token(doctor.user_id, hospital.id, permissions=["appointment.create"])
        monkeypatch.setattr(security, "PATIENT_ACCESS_TTL_SECONDS", -3600)
        expired = create_patient_access_token(patient.account_id)
        monkeypatch.undo()
        monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
        monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)
        monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
        forged = patient.token[:-2] + ("ab" if not patient.token.endswith("ab") else "cd")
        url = f"{HOSPITALS}/{hospital.slug}/doctors/{doctor.id}/appointments"

        for headers in (
            {},
            {"Authorization": "Bearer nonsense"},
            {"Authorization": f"Bearer {forged}"},
            {"Authorization": f"Bearer {expired}"},
            {"Authorization": f"Bearer {staff}"},
            {"Cookie": f"access_token={patient.token}"},
            {"Authorization": f"Basic {patient.token}"},
        ):
            response = await browser.post(
                url, json=_slot(10), headers={"Idempotency-Key": _key(), **headers}
            )
            assert response.status_code == 401, headers.keys()
            assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"
        assert await _rows(db_session, hospital_id=hospital.id) == []

    async def test_a_patient_token_cannot_book_through_the_staff_api(
        self,
        browser: AsyncClient,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        response = await browser.post(
            "/api/v1/appointments",
            json={
                "patient_id": str(patient.record_id),
                "doctor_id": str(doctor.id),
                "scheduled_start": _slot(10)["start"],
                "scheduled_end": _slot(10)["end"],
                "type": "new",
            },
            headers={"Idempotency-Key": _key(), **patient.headers},
        )

        assert response.status_code == 401
        assert await _rows(db_session, hospital_id=hospital.id) == []

    async def test_a_pending_required_policy_closes_booking(
        self,
        application: FastAPI,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        _with_a_required_policy(application)

        answers = [
            await patient.book(hospital.slug, doctor.id),
            await patient.book("no-such-hospital", doctor.id),
            await patient.book(hospital.slug, "x' OR 1=1"),
            await patient.book(hospital.slug, uuid.uuid4()),
        ]

        assert {a.status_code for a in answers} == {403}
        assert {a.json()["error_code"] for a in answers} == {"CONSENT_REQUIRED"}
        assert await _rows(db_session, hospital_id=hospital.id) == []

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"start": _slot(10)["start"]},
            {"start": "2026-10-12T10:00:00", "end": "2026-10-12T10:15:00"},  # no offset
            {"start": "tomorrow", "end": "later"},
            {"start": 1760000000, "end": 1760000900, "extra": 1},
            {**_slot(10), "type": "walk_in"},
            {**_slot(10), "type": "emergency"},
            {**_slot(10), "type": "anything"},
            {**_slot(10), "reason": "r" * 501},
            {**_slot(10), "reason": "a\x00b"},
            {**_slot(10), "reason": ["a list"]},
        ],
    )
    async def test_a_malformed_body_is_refused_and_books_nothing(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        body: dict[str, Any],
    ) -> None:
        response = await patient.client.post(
            f"{HOSPITALS}/{hospital.slug}/doctors/{doctor.id}/appointments",
            json=body,
            headers={"Idempotency-Key": _key(), **patient.headers},
        )

        assert response.status_code == 422, response.text
        assert await _rows(db_session, hospital_id=hospital.id) == []

    async def test_a_reason_is_stored_as_the_text_it_is(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        reason = "<script>alert(1)</script>'; DROP TABLE appointments;-- 😀"

        response = await patient.book(hospital.slug, doctor.id, reason=f"  {reason}  ")
        blank = await patient.book(hospital.slug, doctor.id, _slot(11), reason="   ")

        assert response.json()["data"]["reason"] == reason
        assert blank.json()["data"]["reason"] is None
        rows = await _rows(db_session, patient_id=patient.record_id)
        assert [row.reason for row in rows] == [reason, None]

    @pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
    async def test_only_post_is_defined(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, method: str
    ) -> None:
        response = await patient.client.request(
            method,
            f"{HOSPITALS}/{hospital.slug}/doctors/{doctor.id}/appointments",
            headers=patient.headers,
        )
        assert response.status_code == 405


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
