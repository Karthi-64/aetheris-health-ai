# ruff: noqa: F811 — the fixtures imported from the booking tests are requested by name
"""API and adversarial tests for the patient's own appointments (Task 33).

``docs/modules/15-patient-app.md`` §9 and §14. Over HTTP, through the real
application and a real PostgreSQL. Appointments are made through the real
booking endpoint where the patient makes them, and planted as rows where the
test needs a state only staff can produce.

Racing transitions need separate connections and are tested in
``app/tests/integration/test_patient_booking_concurrency.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.core.security import create_access_token
from app.models.appointment import Appointment, AppointmentStatus, AppointmentStatusHistory
from app.models.audit_log import AuditLog
from app.models.patient import Patient
from app.models.patient_account import PatientAccountLink
from app.services.patient_app import appointments_service, availability_service, booking_service
from app.tests.api.test_patient_booking_api import (  # noqa: F401 — fixtures, registered by import
    HOSPITALS,
    IST,
    MONDAY,
    _at,
    _error,
    _key,
    _Patient,
    _patient_test_settings,
    _refusal,
    _rows,
    _slot,
    _with_a_required_policy,
    application,
    browser,
    doctor,
    hospital,
    patient,
    sms,
    someone_else,
    stranger,
    tag,
)
from app.tests.patient_app_helpers import (
    PATIENT,
    FakeSmsSender,
    insert_appointment,
    insert_doctor,
    insert_hospital,
    insert_patient_record,
    new_phone,
    open_hospital,
    set_windows,
    sign_in,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.doctor import Doctor
    from app.models.hospital import Hospital

pytestmark = pytest.mark.database

MINE = f"{PATIENT}/appointments"
FIELDS = {
    "ref",
    "status",
    "type",
    "start",
    "end",
    "timezone",
    "hospital",
    "doctor",
    "reason",
    "can_cancel",
    "cancel_until",
}
NOT_FOUND = (404, "RESOURCE_NOT_FOUND", "Not Found")
NO_LONGER = (409, "RESOURCE_CONFLICT", "This appointment can no longer be cancelled.")
TOO_LATE = (
    400,
    "BUSINESS_RULE_VIOLATION",
    "It is too late to cancel this appointment in the app. Please contact the hospital.",
)
REASON = {"reason_code": "schedule_conflict"}


class _Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def set(self, now: datetime) -> None:
        instant = now.astimezone(UTC)
        for module in (appointments_service, booking_service, availability_service):
            self._monkeypatch.setattr(module, "utc_now", lambda: instant)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    return _Clock(monkeypatch)


async def _listed(who: _Patient, **params: Any) -> Response:
    return await who.client.get(MINE, params=params, headers=who.headers)


async def _refs(who: _Patient, scope: str = "upcoming", **params: Any) -> list[str]:
    response = await _listed(who, scope=scope, page_size=50, **params)
    assert response.status_code == 200, response.text
    return [item["ref"] for item in response.json()["data"]]


async def _detail(who: _Patient, ref: object) -> Response:
    return await who.client.get(f"{MINE}/{ref}", headers=who.headers)


async def _cancel(who: _Patient, ref: object, **body: Any) -> Response:
    return await who.client.post(f"{MINE}/{ref}/cancel", json=body or REASON, headers=who.headers)


async def _book(
    who: _Patient, hospital: Hospital, doctor: Doctor, slot: dict[str, str], **body: Any
) -> str:
    response = await who.book(hospital.slug, doctor.id, slot, **body)
    assert response.status_code == 201, response.text
    ref: str = response.json()["data"]["ref"]
    return ref


async def _history(db_session: AsyncSession, ref: str) -> list[tuple[Any, Any, Any]]:
    rows = await db_session.execute(
        select(AppointmentStatusHistory)
        .where(AppointmentStatusHistory.appointment_id == uuid.UUID(ref))
        .order_by(AppointmentStatusHistory.changed_at, AppointmentStatusHistory.id)
    )
    # The test session is one transaction, so timestamps tie: creation sorts first.
    found = sorted(rows.scalars(), key=lambda row: row.from_status is not None)
    return [(row.from_status, row.to_status, row.changed_by) for row in found]


async def _audited(db_session: AsyncSession, ref: str) -> list[AuditLog]:
    rows = await db_session.execute(
        select(AuditLog).where(AuditLog.target_id == uuid.UUID(ref)).order_by(AuditLog.created_at)
    )
    return list(rows.scalars())


async def _set_status(db_session: AsyncSession, ref: str, status: AppointmentStatus) -> None:
    await db_session.execute(
        update(Appointment).where(Appointment.id == uuid.UUID(ref)).values(status=status)
    )
    await db_session.commit()


@pytest_asyncio.fixture
async def other(
    browser: AsyncClient, sms: FakeSmsSender, db_session: AsyncSession, hospital: Hospital
) -> _Patient:
    """Another patient, linked at the same hospital."""
    signed_in = _Patient(browser, phone := new_phone(), await sign_in(browser, sms, phone))
    await signed_in.link(db_session, hospital)
    return signed_in


# ── The list ─────────────────────────────────────────────────────────────────


class TestMyAppointments:
    async def test_upcoming_is_the_callers_own_soonest_first_in_the_patient_shape(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        later = await _book(patient, hospital, doctor, _slot(11), reason="Cough")
        sooner = await _book(patient, hospital, doctor, _slot(10))

        response = await _listed(patient)

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body["metadata"]["pagination"]) >= {"page", "page_size", "total_records"}
        assert body["metadata"]["pagination"]["total_records"] == 2
        assert [item["ref"] for item in body["data"]] == [sooner, later]
        assert all(set(item) == FIELDS for item in body["data"])
        assert body["data"][1] == {
            "ref": later,
            "status": "booked",
            "type": "new",
            "start": _slot(11)["start"],
            "end": _slot(11)["end"],
            "timezone": "Asia/Kolkata",
            "hospital": {"ref": hospital.slug, "name": hospital.name},
            "doctor": {"ref": str(doctor.id), "name": "Asha Menon", "specialization": "Cardiology"},
            "reason": "Cough",
            "can_cancel": True,
            "cancel_until": _at(9).isoformat(),
        }
        assert await _refs(patient, "past") == []

    async def test_an_appointment_is_upcoming_until_the_instant_it_ends(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, clock: _Clock
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))

        clock.set(_at(10, 14) + timedelta(seconds=59))
        assert (await _refs(patient), await _refs(patient, "past")) == ([ref], [])
        clock.set(_at(10, 15))
        assert (await _refs(patient), await _refs(patient, "past")) == ([], [ref])

    @pytest.mark.parametrize(
        ("status", "scope"),
        [
            (AppointmentStatus.BOOKED, "upcoming"),
            (AppointmentStatus.CHECKED_IN, "upcoming"),
            (AppointmentStatus.IN_PROGRESS, "upcoming"),
            (AppointmentStatus.COMPLETED, "past"),
            (AppointmentStatus.CANCELLED, "past"),
            (AppointmentStatus.NO_SHOW, "past"),
        ],
    )
    async def test_the_real_status_decides_the_list_and_is_what_is_shown(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        status: AppointmentStatus,
        scope: str,
    ) -> None:
        """A future appointment that is finished, cancelled or missed is not upcoming."""
        ref = await _book(patient, hospital, doctor, _slot(10))
        await _set_status(db_session, ref, status)
        wrong = "past" if scope == "upcoming" else "upcoming"

        [item] = (await _listed(patient, scope=scope)).json()["data"]

        assert (item["ref"], item["status"]) == (ref, status.value)
        assert await _refs(patient, wrong) == []
        assert item["can_cancel"] is (status is AppointmentStatus.BOOKED)
        assert (item["cancel_until"] is not None) is (status is AppointmentStatus.BOOKED)

    async def test_past_is_most_recent_first_and_pages_cover_it_once(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        clock: _Clock,
    ) -> None:
        assert patient.record_id is not None
        planted = [
            await insert_appointment(
                db_session,
                doctor,
                patient.record_id,
                _at(9) + timedelta(minutes=15 * n),
                _at(9) + timedelta(minutes=15 * n + 15),
                status=AppointmentStatus.COMPLETED,
            )
            for n in range(5)
        ]
        clock.set(_at(13))
        expected = [str(row.id) for row in reversed(planted)]

        assert await _refs(patient, "past") == expected
        walked: list[str] = []
        for page in range(1, 5):
            response = await _listed(patient, scope="past", page=page, page_size=2)
            assert response.json()["metadata"]["pagination"]["total_records"] == 5
            walked += [item["ref"] for item in response.json()["data"]]
        assert walked == expected

    async def test_only_the_callers_own_appointments_are_ever_listed(
        self,
        patient: _Patient,
        other: _Patient,
        stranger: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        """Attack: read another patient's appointments off one's own list."""
        mine = await _book(patient, hospital, doctor, _slot(10))
        theirs = await _book(other, hospital, doctor, _slot(10, 15))
        await insert_appointment(db_session, doctor, someone_else, _at(11), _at(11, 15))

        spoofed = await _refs(
            patient,
            patient_id=str(other.record_id),
            account_id=str(other.account_id),
            hospital_id=str(hospital.id),
            all="1",
        )

        assert await _refs(patient) == spoofed == [mine]
        assert await _refs(other) == [theirs]
        assert await _refs(stranger) == await _refs(stranger, "past") == []
        raw = (await _listed(patient)).text
        for word in (
            theirs,
            str(other.record_id),
            str(someone_else),
            "OTHERCANARY",
            "APPTCANARY",
            "NOTESCANARY",
            str(patient.record_id),
            str(patient.account_id),
            str(hospital.id),
            "patient_id",
            "notes",
            "created_by",
            "idempotency",
            "cancelled_reason",
        ):
            assert word not in raw, word

    async def test_appointments_at_every_linked_hospital_each_in_its_own_timezone(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        tag: str,
    ) -> None:
        zone = ZoneInfo("Pacific/Kiritimati")
        far = await insert_hospital(db_session, name=f"{tag} Far", timezone="Pacific/Kiritimati")
        far_doctor = await insert_doctor(db_session, far.id)
        await set_windows(
            db_session, far_doctor, [(day, time(0), time(23, 45), 15) for day in range(7)]
        )
        await patient.link(db_session, far)
        here = await _book(patient, hospital, doctor, _slot(10))
        start = _at(10).astimezone(zone) - timedelta(hours=3)  # three hours earlier, their clock
        there = await _book(
            patient,
            far,
            far_doctor,
            {"start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat()},
        )

        items = (await _listed(patient)).json()["data"]

        assert [item["ref"] for item in items] == [there, here]
        assert [
            (item["timezone"], item["start"][-6:], item["hospital"]["ref"]) for item in items
        ] == [
            ("Pacific/Kiritimati", "+14:00", far.slug),
            ("Asia/Kolkata", "+05:30", hospital.slug),
        ]

    @pytest.mark.parametrize(
        "stale", ["unlinked", "phone_changed", "record_deleted", "hospital_closed"]
    )
    async def test_a_link_that_is_no_longer_honoured_shows_nothing(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        stale: str,
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))
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
        elif stale == "record_deleted":
            await db_session.execute(
                update(Patient).where(Patient.id == patient.record_id).values(deleted_at=func.now())
            )
        await db_session.commit()
        if stale == "hospital_closed":
            await open_hospital(db_session, hospital.id, enabled=False)

        assert await _refs(patient) == []
        assert _error(await _detail(patient, ref)) == NOT_FOUND
        assert _error(await _cancel(patient, ref)) == NOT_FOUND
        [row] = await _rows(db_session, id=uuid.UUID(ref))
        assert row.status is AppointmentStatus.BOOKED

    @pytest.mark.parametrize(
        "params",
        [
            {"scope": "all"},
            {"scope": ""},
            {"scope": "UPCOMING"},
            {"page": 0},
            {"page": 1001},
            {"page_size": 0},
            {"page_size": 51},
            {"page": "x"},
        ],
    )
    async def test_a_parameter_out_of_bounds_is_refused(
        self, patient: _Patient, params: dict[str, Any]
    ) -> None:
        assert (await _listed(patient, **params)).status_code == 422


# ── One appointment ──────────────────────────────────────────────────────────


class TestOneAppointment:
    async def test_it_is_what_the_list_showed(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10), reason="Cough")

        detail = await _detail(patient, ref)

        assert detail.status_code == 200, detail.text
        assert set(detail.json()["data"]) == FIELDS
        assert detail.json()["data"] == (await _listed(patient)).json()["data"][0]
        assert (await _detail(patient, f" {ref.upper()} ")).json()["data"]["ref"] == ref

    async def test_somebody_elses_appointment_is_the_same_404_as_none(
        self,
        patient: _Patient,
        other: _Patient,
        stranger: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        someone_else: uuid.UUID,
    ) -> None:
        """Attack: read or cancel another patient's appointment by its reference."""
        theirs = await _book(other, hospital, doctor, _slot(10))
        planted = await insert_appointment(db_session, doctor, someone_else, _at(11), _at(11, 15))
        unknown = await _detail(patient, uuid.uuid4())
        unknown_cancel = await _cancel(patient, uuid.uuid4())

        for who in (patient, stranger):
            for ref in (theirs, planted.id):
                assert _refusal(await _detail(who, ref)) == _refusal(unknown)
                assert _refusal(await _cancel(who, ref)) == _refusal(unknown_cancel)
        assert _error(unknown) == _error(unknown_cancel) == NOT_FOUND
        assert {row.status for row in await _rows(db_session, doctor_id=doctor.id)} == {
            AppointmentStatus.BOOKED
        }
        assert await _history(db_session, theirs) == [(None, AppointmentStatus.BOOKED, None)]

    async def test_an_appointment_at_a_hospital_the_caller_is_not_linked_at_is_not_theirs(
        self, patient: _Patient, db_session: AsyncSession, tag: str
    ) -> None:
        """Bad data: a row carrying the caller's record id but another hospital's."""
        elsewhere = await insert_hospital(
            db_session, name=f"{tag} Elsewhere", timezone="Asia/Kolkata"
        )
        doctor = await insert_doctor(db_session, elsewhere.id)
        record = await insert_patient_record(db_session, elsewhere.id, phone=patient.phone)
        planted = await insert_appointment(db_session, doctor, record.id, _at(10), _at(10, 15))

        assert _error(await _detail(patient, planted.id)) == NOT_FOUND
        assert _error(await _cancel(patient, planted.id)) == NOT_FOUND
        assert await _refs(patient) == []

    @pytest.mark.parametrize(
        "shape",
        [
            "x' OR '1'='1",
            "..%2F..%2Fme",
            "%00",
            "a" * 4000,
            "not-a-uuid",
            "{ref}x",
            "{hex}",
            "{record}",
            "{account}",
            "{doctor}",
            "{hospital}",
        ],
    )
    async def test_a_reference_that_is_not_one_gets_the_one_404(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, shape: str
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))
        value = shape.format(
            ref=ref,
            hex=uuid.UUID(ref).hex,
            record=patient.record_id,
            account=patient.account_id,
            doctor=doctor.id,
            hospital=hospital.id,
        )
        unknown = await _detail(patient, uuid.uuid4())
        unknown_cancel = await _cancel(patient, uuid.uuid4())

        assert _refusal(await _detail(patient, value)) == _refusal(unknown)
        assert _refusal(await _cancel(patient, value)) == _refusal(unknown_cancel)


# ── Cancelling ───────────────────────────────────────────────────────────────


class TestCancel:
    async def test_a_booked_appointment_is_cancelled_by_the_hospitals_own_transition(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))

        response = await _cancel(
            patient, ref, reason_code="feeling_better", reason_text="  PRIVATETEXT all fine  "
        )

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == FIELDS
        assert (data["ref"], data["status"], data["can_cancel"], data["cancel_until"]) == (
            ref,
            "cancelled",
            False,
            None,
        )
        assert data == (await _detail(patient, ref)).json()["data"]
        [row] = await _rows(db_session, id=uuid.UUID(ref))
        assert row.status is AppointmentStatus.CANCELLED
        assert (
            row.cancelled_reason
            == "Cancelled by the patient in the app (feeling_better): PRIVATETEXT all fine"
        )
        assert await _history(db_session, ref) == [
            (None, AppointmentStatus.BOOKED, None),
            (AppointmentStatus.BOOKED, AppointmentStatus.CANCELLED, None),
        ]
        assert (await _refs(patient), await _refs(patient, "past")) == ([], [ref])
        assert _slot(10)["start"] in await patient.free(hospital.slug, doctor.id)
        assert "PRIVATETEXT" not in response.text and "cancelled_reason" not in response.text

    async def test_it_is_audited_once_as_the_patients_act(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))
        await _cancel(patient, ref, reason_code="other", reason_text="PRIVATETEXT")

        rows = await _audited(db_session, ref)

        assert [row.action for row in rows] == [
            "patient.appointment.booked",
            "patient.appointment.cancelled",
        ]
        cancelled = rows[1]
        assert (
            cancelled.actor_type,
            cancelled.patient_account_id,
            cancelled.actor_user_id,
            cancelled.hospital_id,
        ) == ("patient", patient.account_id, None, hospital.id)
        assert cancelled.context == {"reason_code": "other", "from_status": "booked"}
        blob = json.dumps([cancelled.context, cancelled.before, cancelled.after], default=str)
        for secret in ("PRIVATETEXT", patient.phone, patient.token):
            assert secret not in blob

    async def test_cancelling_again_changes_nothing_and_answers_the_same(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        """A retry after a lost answer, or an impatient second tap."""
        ref = await _book(patient, hospital, doctor, _slot(10))
        first = await _cancel(patient, ref)

        again = [
            await _cancel(patient, ref, reason_code="other", reason_text="different")
            for _ in range(3)
        ]

        assert {r.status_code for r in (first, *again)} == {200}
        assert {json.dumps(r.json()["data"], sort_keys=True) for r in again} == {
            json.dumps(first.json()["data"], sort_keys=True)
        }
        assert len(await _history(db_session, ref)) == 2
        assert [row.action for row in await _audited(db_session, ref)].count(
            "patient.appointment.cancelled"
        ) == 1
        [row] = await _rows(db_session, id=uuid.UUID(ref))
        assert row.cancelled_reason == "Cancelled by the patient in the app (schedule_conflict)"

    async def test_an_appointment_staff_already_cancelled_is_answered_as_cancelled_without_a_new_record(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))
        await db_session.execute(
            update(Appointment)
            .where(Appointment.id == uuid.UUID(ref))
            .values(
                status=AppointmentStatus.CANCELLED,
                cancelled_reason="STAFFPRIVATE doctor unavailable",
            )
        )
        await db_session.commit()

        response = await _cancel(patient, ref)

        assert (response.status_code, response.json()["data"]["status"]) == (200, "cancelled")
        assert (
            "STAFFPRIVATE" not in response.text
            and "STAFFPRIVATE" not in (await _detail(patient, ref)).text
        )
        assert len(await _history(db_session, ref)) == 1
        assert [row.action for row in await _audited(db_session, ref)] == [
            "patient.appointment.booked"
        ]

    @pytest.mark.parametrize(
        "status",
        [
            AppointmentStatus.CHECKED_IN,
            AppointmentStatus.IN_PROGRESS,
            AppointmentStatus.COMPLETED,
            AppointmentStatus.NO_SHOW,
        ],
    )
    async def test_only_a_booked_appointment_can_be_cancelled_by_the_patient(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        status: AppointmentStatus,
    ) -> None:
        """The staff state machine lets a checked-in appointment be cancelled; a patient may not."""
        ref = await _book(patient, hospital, doctor, _slot(10))
        assert (await _detail(patient, ref)).json()["data"]["can_cancel"] is True
        await _set_status(db_session, ref, status)

        response = await _cancel(patient, ref)

        assert _error(response) == NO_LONGER
        assert not response.json().get("errors")
        [row] = await _rows(db_session, id=uuid.UUID(ref))
        assert row.status is status
        assert len(await _history(db_session, ref)) == 1
        detail = (await _detail(patient, ref)).json()["data"]
        assert (detail["status"], detail["can_cancel"]) == (status.value, False)

    async def test_the_hospitals_cut_off_is_applied_to_the_second(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        clock: _Clock,
    ) -> None:
        """Default: 120 minutes before the start. The 11:00 appointment may be cancelled until 09:00."""
        first, second = (
            await _book(patient, hospital, doctor, _slot(11)),
            await _book(patient, hospital, doctor, _slot(11, 15)),
        )

        clock.set(_at(9) + timedelta(seconds=1))
        detail = (await _detail(patient, first)).json()["data"]
        assert (detail["status"], detail["can_cancel"], detail["cancel_until"]) == (
            "booked",
            False,
            _at(9).isoformat(),
        )
        assert _error(await _cancel(patient, first)) == TOO_LATE
        assert (await _rows(db_session, id=uuid.UUID(first)))[0].status is AppointmentStatus.BOOKED
        assert len(await _history(db_session, first)) == 1

        clock.set(_at(9))
        assert (await _detail(patient, first)).json()["data"]["can_cancel"] is True
        assert (await _cancel(patient, first)).status_code == 200
        clock.set(_at(12))
        assert _error(await _cancel(patient, second)) == TOO_LATE

    async def test_a_hospital_sets_its_own_cut_off(
        self,
        browser: AsyncClient,
        sms: FakeSmsSender,
        db_session: AsyncSession,
        tag: str,
        clock: _Clock,
    ) -> None:
        hospital = await insert_hospital(
            db_session,
            name=f"{tag} Lenient",
            timezone="Asia/Kolkata",
            settings={"patient_app.cancel_cutoff_minutes": 0},
        )
        doctor = await insert_doctor(db_session, hospital.id)
        who = _Patient(browser, phone := new_phone(), await sign_in(browser, sms, phone))
        await who.link(db_session, hospital)
        ref, late = (
            await _book(who, hospital, doctor, _slot(10)),
            await _book(who, hospital, doctor, _slot(10, 15)),
        )

        clock.set(_at(10))
        assert (await _detail(who, ref)).json()["data"]["cancel_until"] == _at(10).isoformat()
        assert (await _cancel(who, ref)).status_code == 200
        clock.set(_at(10, 15) + timedelta(seconds=1))
        assert _error(await _cancel(who, late)) == TOO_LATE

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"reason_text": "no code"},
            {"reason_code": "because"},
            {"reason_code": ""},
            {"reason_code": "other", "reason_text": "t" * 201},
            {"reason_code": "other", "reason_text": "a\x00b"},
            {"reason_code": "other", "patient_id": "x"},
            {"reason_code": "other", "status": "completed"},
            {"reason_code": "other", "hospital_id": "x"},
            {"reason_code": "other", "cancelled_reason": "x"},
            {"reason_code": ["other"]},
        ],
    )
    async def test_a_malformed_body_is_refused_and_cancels_nothing(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
        body: dict[str, Any],
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))

        response = await patient.client.post(
            f"{MINE}/{ref}/cancel", json=body, headers=patient.headers
        )

        assert response.status_code == 422, response.text
        assert (await _rows(db_session, id=uuid.UUID(ref)))[0].status is AppointmentStatus.BOOKED

    @pytest.mark.parametrize(
        "code", ["schedule_conflict", "feeling_better", "booked_by_mistake", "other"]
    )
    async def test_each_listed_reason_is_accepted(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, code: str
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))
        assert (await _cancel(patient, ref, reason_code=code)).json()["data"][
            "status"
        ] == "cancelled"


# ── Tokens and policy ────────────────────────────────────────────────────────


class TestGates:
    async def test_every_route_needs_a_genuine_patient_token(
        self,
        browser: AsyncClient,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))
        staff = create_access_token(
            doctor.user_id, hospital.id, permissions=["appointment.read", "appointment.cancel"]
        )
        forged = patient.token[:-2] + ("ab" if not patient.token.endswith("ab") else "cd")

        for headers in (
            {},
            {"Authorization": "Bearer nonsense"},
            {"Authorization": f"Bearer {forged}"},
            {"Authorization": f"Bearer {staff}"},
            {"Cookie": f"access_token={patient.token}"},
        ):
            answers = [
                await browser.get(MINE, headers=headers),
                await browser.get(f"{MINE}/{ref}", headers=headers),
                await browser.post(f"{MINE}/{ref}/cancel", json=REASON, headers=headers),
            ]
            assert {a.status_code for a in answers} == {401}, headers.keys()
            assert ref not in "".join(a.text for a in answers)
        assert (await _rows(db_session, id=uuid.UUID(ref)))[0].status is AppointmentStatus.BOOKED

    async def test_a_patient_token_reaches_no_staff_appointment_route(
        self,
        browser: AsyncClient,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))

        answers = [
            await browser.get("/api/v1/appointments", headers=patient.headers),
            await browser.get(f"/api/v1/appointments/{ref}", headers=patient.headers),
            await browser.post(
                f"/api/v1/appointments/{ref}/cancel", json={"reason": "x"}, headers=patient.headers
            ),
            await browser.post(f"/api/v1/appointments/{ref}/check-in", headers=patient.headers),
        ]

        assert {a.status_code for a in answers} == {401}
        assert (await _rows(db_session, id=uuid.UUID(ref)))[0].status is AppointmentStatus.BOOKED

    async def test_a_pending_required_policy_closes_all_three(
        self,
        application: FastAPI,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))
        _with_a_required_policy(application)

        answers = [
            await _listed(patient),
            await _detail(patient, ref),
            await _cancel(patient, ref),
            await _detail(patient, "x' OR 1=1"),
        ]

        assert {a.status_code for a in answers} == {403}
        assert {a.json()["error_code"] for a in answers} == {"CONSENT_REQUIRED"}
        assert (await _rows(db_session, id=uuid.UUID(ref)))[0].status is AppointmentStatus.BOOKED

    async def test_reading_writes_nothing(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))
        count = select(func.count()).select_from(AuditLog)
        before = (await db_session.execute(count)).scalar_one()

        await _listed(patient)
        await _listed(patient, scope="past")
        await _detail(patient, ref)
        await _detail(patient, uuid.uuid4())

        assert (await db_session.execute(count)).scalar_one() == before
        assert len(await _history(db_session, ref)) == 1

    @pytest.mark.parametrize(
        ("method", "suffix"),
        [
            ("POST", ""),
            ("PUT", "/{ref}"),
            ("PATCH", "/{ref}"),
            ("DELETE", "/{ref}"),
            ("GET", "/{ref}/cancel"),
            ("DELETE", "/{ref}/cancel"),
        ],
    )
    async def test_nothing_else_is_defined(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor, method: str, suffix: str
    ) -> None:
        ref = await _book(patient, hospital, doctor, _slot(10))
        response = await patient.client.request(
            method, MINE + suffix.format(ref=ref), headers=patient.headers
        )
        assert response.status_code == 405
