"""Racing bookings, on real concurrent database connections (Task 32).

Every other patient test shares one rolled-back session, which cannot race
with itself. These tests need requests that really run at once and really
commit, so this module builds its own short-lived database next to the test
database — created, migrated and dropped here — and gives each request its own
session on it. Nothing is written to the shared test database.

It runs only when ``TEST_DATABASE_URL`` is set explicitly: the database it
creates is named after that one, and a run that fell back to a configured
default has no business creating databases.

What is proven: however many requests arrive together, the appointments that
exist afterwards are exactly the ones the rules allow — one per slot, one per
idempotency key, no more than the limit per patient.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.dependencies.db import get_db_session
from app.api.dependencies.patient import get_sms_sender
from app.core.audit import StructlogAuditSink
from app.core.config import settings
from app.core.exceptions import AetherisError
from app.core.security import create_patient_access_token
from app.main import create_app
from app.models.appointment import Appointment, AppointmentStatus, AppointmentStatusHistory
from app.models.audit_log import AuditLog
from app.models.patient_account import PatientAccount
from app.models.patient_consent import ConsentPurpose
from app.repositories.appointment_repository import AppointmentRepository
from app.repositories.doctor_repository import DoctorRepository
from app.repositories.hospital_repository import HospitalRepository
from app.repositories.patient_account_link_repository import PatientAccountLinkRepository
from app.repositories.patient_consent_repository import PatientConsentRepository
from app.repositories.patient_repository import PatientRepository
from app.schemas.appointment import CancelAppointmentRequest
from app.services.appointment_service import AppointmentService, NullInvoiceDraftSink
from app.tests.conftest import _run_migrations
from app.tests.patient_app_helpers import (
    PATIENT,
    POLICY,
    insert_doctor,
    insert_hospital,
    insert_patient_record,
    set_windows,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.models.doctor import Doctor
    from app.models.hospital import Hospital

pytestmark = [
    pytest.mark.database,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL"),
        reason="needs an explicit TEST_DATABASE_URL to create its own database beside",
    ),
]

IST = ZoneInfo("Asia/Kolkata")
TAKEN = "This time is no longer available."
LIMIT = "You have reached the limit of upcoming appointments at this hospital."
OWN_OVERLAP = "You already have an appointment at this time."


def _next_monday() -> date:
    day = datetime.now(IST).date() + timedelta(days=7)
    return day + timedelta(days=(7 - day.weekday()) % 7)


MONDAY = _next_monday()


def _slot(hour: int, minute: int = 0, *, minutes: int = 15) -> dict[str, str]:
    start = datetime.combine(MONDAY, time(hour, minute), tzinfo=IST)
    return {"start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat()}


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def racing_engine() -> AsyncGenerator[AsyncEngine]:
    """A migrated database of this module's own, dropped when the module ends."""
    base = make_url(os.environ["TEST_DATABASE_URL"])
    name = f"{base.database}_race_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    engine = create_async_engine(url, pool_size=60, max_overflow=10)
    try:
        await asyncio.to_thread(_run_migrations, url)
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.fixture(autouse=True)
def _limits_out_of_the_way(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_HOSPITAL_PER_MIN", 1_000_000)


@pytest_asyncio.fixture(loop_scope="session")
async def application(racing_engine: AsyncEngine) -> AsyncGenerator[FastAPI]:
    """The real application, each request on its own session and connection."""
    app = create_app()

    async def _session() -> AsyncGenerator[AsyncSession]:
        async with AsyncSession(racing_engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_sms_sender] = lambda: None
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(loop_scope="session")
async def client(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver", timeout=120
    ) as http:
        yield http


class _World:
    """Rows committed to the racing database, and reads of what is there afterwards."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def hospital(self, **columns: Any) -> Hospital:
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            return await insert_hospital(
                session, name=f"Race {uuid.uuid4().hex[:8]}", timezone="Asia/Kolkata", **columns
            )

    async def doctor(self, hospital: Hospital, *, slot_minutes: int = 15) -> Doctor:
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            doctor = await insert_doctor(session, hospital.id, last_name=uuid.uuid4().hex[:6])
            await set_windows(session, doctor, [(0, time(9), time(17), slot_minutes)])
            return doctor

    async def patient(self, *hospitals: Hospital) -> dict[str, str]:
        """A patient account linked, with consent, at each hospital. Returns its headers."""
        phone = f"+9197{uuid.uuid4().int % 10**8:08d}"
        now = datetime.now(UTC)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            account = PatientAccount(id=uuid.uuid4(), phone=phone, phone_verified_at=now)
            session.add(account)
            await session.flush()
            for hospital in hospitals:
                record = await insert_patient_record(session, hospital.id, phone=phone)
                await PatientAccountLinkRepository(session).create_link(
                    hospital.id,
                    account_id=account.id,
                    patient_id=record.id,
                    verified_via="phone_dob",
                    now=now,
                )
                await PatientConsentRepository(session).add(
                    hospital.id,
                    account_id=account.id,
                    purpose=ConsentPurpose.HOSPITAL_RECORD_LINK.value,
                    policy_version=POLICY,
                    now=now,
                    ip_address=None,
                    user_agent=None,
                )
            await session.commit()
            return {"Authorization": f"Bearer {create_patient_access_token(account.id)}"}

    async def appointments(self, hospital: Hospital) -> list[Appointment]:
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            rows = await session.execute(
                select(Appointment).where(Appointment.hospital_id == hospital.id)
            )
            return list(rows.unique().scalars())

    async def audited(self, hospital: Hospital) -> int:
        async with AsyncSession(self.engine) as session:
            count = await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.hospital_id == hospital.id)
                .where(AuditLog.action == "patient.appointment.booked")
            )
            return count.scalar_one()


@pytest.fixture
def world(racing_engine: AsyncEngine) -> _World:
    return _World(racing_engine)


def _book(
    client: AsyncClient,
    headers: dict[str, str],
    hospital: Hospital,
    doctor: Doctor,
    slot: dict[str, str],
    *,
    key: str | None = None,
) -> Any:
    return client.post(
        f"{PATIENT}/hospitals/{hospital.slug}/doctors/{doctor.id}/appointments",
        json=slot,
        headers={"Idempotency-Key": key or str(uuid.uuid4()), **headers},
    )


def _outcomes(responses: list[Response]) -> Counter[tuple[int, str]]:
    return Counter(
        (r.status_code, r.json().get("message", "") if r.status_code >= 400 else "")
        for r in responses
    )


def _never_a_server_error(responses: list[Response]) -> None:
    assert [r.status_code for r in responses if r.status_code >= 500] == []


@pytest.mark.asyncio(loop_scope="session")
class TestOneSlotManyPatients:
    @pytest.mark.parametrize("patients", [2, 10, 50])
    async def test_exactly_one_patient_gets_the_slot(
        self, client: AsyncClient, world: _World, patients: int
    ) -> None:
        """Attack on the check-then-insert gap: everyone saw 10:00 free and confirms at once."""
        hospital = await world.hospital()
        doctor = await world.doctor(hospital)
        people = [await world.patient(hospital) for _ in range(patients)]

        responses = await asyncio.gather(
            *[_book(client, headers, hospital, doctor, _slot(10)) for headers in people]
        )

        _never_a_server_error(responses)
        assert _outcomes(responses) == Counter({(201, ""): 1, (409, TAKEN): patients - 1})
        rows = await world.appointments(hospital)
        assert len(rows) == 1
        [winner] = [r for r in responses if r.status_code == 201]
        assert str(rows[0].id) == winner.json()["data"]["ref"]
        assert rows[0].status is AppointmentStatus.BOOKED
        assert await world.audited(hospital) == 1
        assert not any(str(rows[0].id) in r.text for r in responses if r.status_code == 409)

    async def test_fifty_patients_over_three_slots_fill_each_slot_once(
        self, client: AsyncClient, world: _World
    ) -> None:
        hospital = await world.hospital()
        doctor = await world.doctor(hospital)
        people = [await world.patient(hospital) for _ in range(50)]
        slots = [_slot(10), _slot(10, 15), _slot(10, 30)]

        responses = await asyncio.gather(
            *[
                _book(client, headers, hospital, doctor, slots[index % 3])
                for index, headers in enumerate(people)
            ]
        )

        _never_a_server_error(responses)
        assert _outcomes(responses) == Counter({(201, ""): 3, (409, TAKEN): 47})
        rows = await world.appointments(hospital)
        assert sorted(row.scheduled_start.astimezone(IST).isoformat() for row in rows) == [
            slot["start"] for slot in slots
        ]
        assert len({row.patient_id for row in rows}) == 3

    async def test_overlapping_requests_of_different_lengths_cannot_both_win(
        self, client: AsyncClient, world: _World
    ) -> None:
        """Two doctors' grids are not involved: one doctor, and the constraint is on time ranges."""
        hospital = await world.hospital()
        doctor = await world.doctor(hospital, slot_minutes=30)
        people = [await world.patient(hospital) for _ in range(10)]

        responses = await asyncio.gather(
            *[_book(client, headers, hospital, doctor, _slot(10, minutes=30)) for headers in people]
        )

        _never_a_server_error(responses)
        assert _outcomes(responses)[(201, "")] == 1
        assert len(await world.appointments(hospital)) == 1


@pytest.mark.asyncio(loop_scope="session")
class TestOnePatientManyRequests:
    @pytest.mark.parametrize("requests", [2, 10, 50])
    async def test_the_same_key_sent_many_times_at_once_is_one_appointment(
        self, client: AsyncClient, world: _World, requests: int
    ) -> None:
        """A double click, a retrying network layer, a reconnecting phone — all at once."""
        hospital = await world.hospital()
        doctor = await world.doctor(hospital)
        headers = await world.patient(hospital)
        key = str(uuid.uuid4())

        responses = await asyncio.gather(
            *[_book(client, headers, hospital, doctor, _slot(10), key=key) for _ in range(requests)]
        )

        _never_a_server_error(responses)
        assert _outcomes(responses) == Counter({(201, ""): 1, (200, ""): requests - 1})
        assert len({r.json()["data"]["ref"] for r in responses}) == 1
        assert len(await world.appointments(hospital)) == 1
        assert await world.audited(hospital) == 1

    async def test_different_keys_for_the_same_slot_are_still_one_appointment(
        self, client: AsyncClient, world: _World
    ) -> None:
        hospital = await world.hospital()
        doctor = await world.doctor(hospital)
        headers = await world.patient(hospital)

        responses = await asyncio.gather(
            *[_book(client, headers, hospital, doctor, _slot(10)) for _ in range(10)]
        )

        _never_a_server_error(responses)
        assert _outcomes(responses) == Counter({(201, ""): 1, (409, TAKEN): 9})
        assert len(await world.appointments(hospital)) == 1

    async def test_the_booking_limit_holds_under_a_burst(
        self, client: AsyncClient, world: _World
    ) -> None:
        """Attack: beat the limit of three by asking for eight slots in the same instant."""
        hospital = await world.hospital()
        doctor = await world.doctor(hospital)
        headers = await world.patient(hospital)
        slots = [_slot(10 + index // 4, 15 * (index % 4)) for index in range(8)]

        responses = await asyncio.gather(
            *[_book(client, headers, hospital, doctor, slot) for slot in slots]
        )

        _never_a_server_error(responses)
        assert _outcomes(responses) == Counter({(201, ""): 3, (400, LIMIT): 5})
        assert len(await world.appointments(hospital)) == 3

    async def test_one_patient_cannot_be_in_two_rooms_at_once(
        self, client: AsyncClient, world: _World
    ) -> None:
        hospital = await world.hospital()
        doctors = [await world.doctor(hospital) for _ in range(6)]
        headers = await world.patient(hospital)

        responses = await asyncio.gather(
            *[_book(client, headers, hospital, doctor, _slot(10)) for doctor in doctors]
        )

        _never_a_server_error(responses)
        assert _outcomes(responses) == Counter({(201, ""): 1, (400, OWN_OVERLAP): 5})
        assert len(await world.appointments(hospital)) == 1


@pytest.mark.asyncio(loop_scope="session")
class TestAcrossHospitals:
    async def test_a_race_at_one_hospital_never_writes_into_another(
        self, client: AsyncClient, world: _World
    ) -> None:
        """Linked at both: the right path books, the crossed paths book nothing, all at once."""
        here, there = await world.hospital(), await world.hospital()
        doctor_here, doctor_there = await world.doctor(here), await world.doctor(there)
        people = [await world.patient(here, there) for _ in range(10)]

        responses = await asyncio.gather(
            *[_book(client, headers, here, doctor_here, _slot(10)) for headers in people],
            *[_book(client, headers, there, doctor_there, _slot(10)) for headers in people],
            *[_book(client, headers, here, doctor_there, _slot(11)) for headers in people],
            *[_book(client, headers, there, doctor_here, _slot(11)) for headers in people],
        )

        _never_a_server_error(responses)
        assert _outcomes(responses[:10])[(201, "")] == 1
        assert _outcomes(responses[10:20])[(201, "")] == 1
        assert {r.status_code for r in responses[20:]} == {404}
        rows_here, rows_there = await world.appointments(here), await world.appointments(there)
        assert [(r.doctor_id, r.hospital_id) for r in rows_here] == [(doctor_here.id, here.id)]
        assert [(r.doctor_id, r.hospital_id) for r in rows_there] == [(doctor_there.id, there.id)]

    async def test_the_same_key_at_two_hospitals_is_two_separate_bookings(
        self, client: AsyncClient, world: _World
    ) -> None:
        """A key is scoped to the hospital it was used at, as the staff key is."""
        here, there = await world.hospital(), await world.hospital()
        doctor_here, doctor_there = await world.doctor(here), await world.doctor(there)
        headers = await world.patient(here, there)
        key = str(uuid.uuid4())

        first, second = await asyncio.gather(
            _book(client, headers, here, doctor_here, _slot(10), key=key),
            _book(client, headers, there, doctor_there, _slot(11), key=key),
        )

        assert (first.status_code, second.status_code) == (201, 201)
        assert len(await world.appointments(here)) == len(await world.appointments(there)) == 1


# ── Cancelling, against everything else that can change an appointment ───────


def _cancel(client: AsyncClient, headers: dict[str, str], ref: str) -> Any:
    return client.post(
        f"{PATIENT}/appointments/{ref}/cancel",
        json={"reason_code": "schedule_conflict"},
        headers=headers,
    )


async def _staff(engine: AsyncEngine, hospital: Hospital, ref: str, move: str) -> str:
    """One staff transition on its own connection. Returns "ok" or the refusal's class name."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        service = AppointmentService(
            AppointmentRepository(session),
            PatientRepository(session),
            DoctorRepository(session),
            HospitalRepository(session),
            session,
            StructlogAuditSink(),
            NullInvoiceDraftSink(),
        )
        try:
            if move == "cancel":
                await service.cancel(
                    hospital.id, uuid.UUID(ref), CancelAppointmentRequest(reason="staff cancelled")
                )
            else:
                await getattr(service, move)(hospital.id, uuid.UUID(ref))
        except AetherisError as refusal:
            await session.rollback()
            return type(refusal).__name__
        return "ok"


async def _trail(world: _World, ref: str) -> list[tuple[str | None, str]]:
    """The appointment's status history, in order, as (from, to)."""
    async with AsyncSession(world.engine) as session:
        rows = await session.execute(
            select(AppointmentStatusHistory)
            .where(AppointmentStatusHistory.appointment_id == uuid.UUID(ref))
            .order_by(AppointmentStatusHistory.changed_at, AppointmentStatusHistory.id)
        )
        return [
            (row.from_status.value if row.from_status else None, row.to_status.value)
            for row in rows.scalars()
        ]


async def _status(world: _World, ref: str) -> str:
    async with AsyncSession(world.engine) as session:
        row = await session.get(Appointment, uuid.UUID(ref))
        assert row is not None
        return row.status.value


def _consistent(trail: list[tuple[str | None, str]], final: str) -> None:
    """Each step starts where the last one ended, and the last one is where the row is."""
    assert trail[0] == (None, "booked")
    for (_, reached), (left, _) in zip(trail, trail[1:], strict=False):
        assert left == reached, trail
    assert trail[-1][1] == final, (trail, final)


@pytest.mark.asyncio(loop_scope="session")
class TestCancellationRaces:
    @pytest.mark.parametrize("requests", [2, 10, 30])
    async def test_many_cancellations_at_once_are_one_transition(
        self, client: AsyncClient, world: _World, requests: int
    ) -> None:
        hospital = await world.hospital()
        doctor = await world.doctor(hospital)
        headers = await world.patient(hospital)
        ref = (await _book(client, headers, hospital, doctor, _slot(10))).json()["data"]["ref"]

        responses = await asyncio.gather(*[_cancel(client, headers, ref) for _ in range(requests)])

        assert {r.status_code for r in responses} == {200}
        assert {r.json()["data"]["status"] for r in responses} == {"cancelled"}
        assert await _trail(world, ref) == [(None, "booked"), ("booked", "cancelled")]
        async with AsyncSession(world.engine) as session:
            audited = await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.target_id == uuid.UUID(ref))
                .where(AuditLog.action == "patient.appointment.cancelled")
            )
            assert audited.scalar_one() == 1

    @pytest.mark.parametrize("move", ["check_in", "start", "cancel", "mark_no_show"])
    async def test_a_patient_cancelling_while_staff_act_leaves_one_consistent_history(
        self, client: AsyncClient, world: _World, racing_engine: AsyncEngine, move: str
    ) -> None:
        """Attack on the read-then-write gap: both saw `booked`; only one may leave from it."""
        hospital = await world.hospital()
        doctor = await world.doctor(hospital)
        people = [await world.patient(hospital) for _ in range(8)]
        refs = [
            (
                await _book(client, headers, hospital, doctor, _slot(9 + n // 4, 15 * (n % 4)))
            ).json()["data"]["ref"]
            for n, headers in enumerate(people)
        ]

        outcomes = await asyncio.gather(
            *[_cancel(client, headers, ref) for headers, ref in zip(people, refs, strict=True)],
            *[_staff(racing_engine, hospital, ref, move) for ref in refs],
        )
        patient_side, staff_side = outcomes[:8], outcomes[8:]

        for ref, answer, staff in zip(refs, patient_side, staff_side, strict=True):
            assert answer.status_code in (200, 409), answer.text
            final, trail = await _status(world, ref), await _trail(world, ref)
            _consistent(trail, final)
            left_booked = [step for step in trail if step[0] == "booked"]
            assert len(left_booked) == 1, trail
            if answer.status_code == 200:
                assert answer.json()["data"]["status"] == "cancelled"
                assert "cancelled" in final
            else:
                assert final != "cancelled" or move == "cancel"
            # Staff either moved it or were refused by the state machine — never an error.
            assert staff in ("ok", "InvalidTransitionError"), staff
            if move == "cancel":
                assert trail == [(None, "booked"), ("booked", "cancelled")]

    async def test_cancelling_and_rebooking_the_freed_slot_at_once_never_double_books(
        self, client: AsyncClient, world: _World
    ) -> None:
        hospital = await world.hospital()
        doctor = await world.doctor(hospital)
        holder = await world.patient(hospital)
        others = [await world.patient(hospital) for _ in range(10)]
        ref = (await _book(client, holder, hospital, doctor, _slot(10))).json()["data"]["ref"]

        outcomes = await asyncio.gather(
            _cancel(client, holder, ref),
            *[_book(client, headers, hospital, doctor, _slot(10)) for headers in others],
        )

        _never_a_server_error(list(outcomes))
        assert outcomes[0].status_code == 200
        live = [
            row
            for row in await world.appointments(hospital)
            if row.status is not AppointmentStatus.CANCELLED
        ]
        assert len(live) <= 1
        assert sum(r.status_code == 201 for r in outcomes[1:]) == len(live)
