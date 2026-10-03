"""API tests for the appointment endpoints.

Real app, real service, real repository, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3).

Module spec §16 asks for "all endpoints × status × permission combinations".
The permission split matters here more than in earlier modules: §3 gives a
nurse check-in but not completion, and a doctor start/complete but not booking,
so each transition endpoint is probed with a role that should be refused.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.dependencies.db import get_db_session
from app.core.security import create_access_token
from app.main import create_app
from app.tests.conftest import grant_permissions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

ALL_APPOINTMENT_PERMISSIONS = [
    "appointment.read",
    "appointment.book",
    "appointment.reschedule",
    "appointment.cancel",
    "appointment.check_in",
    "appointment.start",
    "appointment.complete",
    "appointment.book_override",
    "appointment.recommend_slot",
]

#: Far-future Monday, so "not in the past" holds and the date is stable.
BASE = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)


async def _make_user(
    session: AsyncSession, hospital_id: uuid.UUID, permissions: list[str]
) -> uuid.UUID:
    """Create an active user holding ``permissions``."""
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"appt-api-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Api",
        last_name="Tester",
    )
    session.add(user)
    await session.flush()
    if permissions:
        await grant_permissions(
            session, hospital_id=hospital_id, user_id=user.id, codes=permissions
        )
    return user.id


def _auth(user_id: uuid.UUID, hospital_id: uuid.UUID | None) -> dict[str, str]:
    """Mint a Bearer header."""
    return {
        "Authorization": f"Bearer {create_access_token(user_id=user_id, hospital_id=hospital_id)}"
    }


async def _clinical_fixtures(
    session: AsyncSession, hospital_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a patient and a doctor, returning their ids."""
    from app.models.doctor import Doctor
    from app.models.patient import Gender, Patient
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"doc-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Asha",
        last_name="Menon",
    )
    session.add(user)
    await session.flush()

    doctor = Doctor(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        user_id=user.id,
        specialization="Cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:8]}",
    )
    patient = Patient(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        mrn=f"MRN-{uuid.uuid4().hex[:8]}",
        first_name="Ananya",
        last_name="Rao",
        date_of_birth=datetime(1990, 1, 1).date(),
        gender=Gender.FEMALE,
    )
    session.add_all([doctor, patient])
    await session.flush()
    return patient.id, doctor.id


async def _set_timezone(session: AsyncSession, hospital_id: uuid.UUID, timezone: str) -> None:
    """Put the test hospital in a specific IANA timezone."""
    from app.models.hospital import Hospital

    hospital = await session.get(Hospital, hospital_id)
    assert hospital is not None
    hospital.timezone = timezone
    await session.flush()


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """An HTTP client sharing the test's rolled-back session."""
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def full_access(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """A user holding every appointment permission."""
    return _auth(
        await _make_user(db_session, hospital_id, ALL_APPOINTMENT_PERMISSIONS), hospital_id
    )


@pytest_asyncio.fixture
async def read_only(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """A user holding only ``appointment.read``."""
    return _auth(await _make_user(db_session, hospital_id, ["appointment.read"]), hospital_id)


@pytest_asyncio.fixture
async def other_tenant(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> dict[str, str]:
    """A fully-permissioned user in a different hospital."""
    return _auth(
        await _make_user(db_session, other_hospital_id, ALL_APPOINTMENT_PERMISSIONS),
        other_hospital_id,
    )


@pytest_asyncio.fixture
async def clinical(db_session: AsyncSession, hospital_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """A patient and doctor in the caller's hospital."""
    return await _clinical_fixtures(db_session, hospital_id)


def _payload(
    patient_id: uuid.UUID, doctor_id: uuid.UUID, *, offset_minutes: int = 0, **overrides: Any
) -> dict[str, Any]:
    """Build a booking body."""
    start = BASE + timedelta(minutes=offset_minutes)
    body: dict[str, Any] = {
        "patient_id": str(patient_id),
        "doctor_id": str(doctor_id),
        "scheduled_start": start.isoformat(),
        "scheduled_end": (start + timedelta(minutes=15)).isoformat(),
        "type": "new",
        "reason": "Persistent cough",
    }
    body.update(overrides)
    return body


async def _book(
    api: AsyncClient,
    headers: dict[str, str],
    patient_id: uuid.UUID,
    doctor_id: uuid.UUID,
    *,
    key: str | None = None,
    offset_minutes: int = 0,
    **overrides: Any,
) -> dict[str, Any]:
    """Book through the API and return the data block."""
    response = await api.post(
        "/api/v1/appointments",
        json=_payload(patient_id, doctor_id, offset_minutes=offset_minutes, **overrides),
        headers={**headers, "Idempotency-Key": key or f"key-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


class TestBooking:
    """``POST /api/v1/appointments``."""

    async def test_book_returns_201(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        data = await _book(api, full_access, *clinical)
        assert data["status"] == "booked"
        assert data["duration_minutes"] == 15
        assert data["patient_name"] == "Ananya Rao"
        assert data["doctor_name"] == "Asha Menon"

    async def test_missing_idempotency_key_returns_422(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Business rule 8 makes the header mandatory, not optional."""
        response = await api.post(
            "/api/v1/appointments", json=_payload(*clinical), headers=full_access
        )
        assert response.status_code == 422

    async def test_replaying_the_key_returns_200_not_a_second_booking(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """FR-8: a client retry after a timeout must be safe."""
        key = "retry-key-00001"
        first = await _book(api, full_access, *clinical, key=key)

        replay = await api.post(
            "/api/v1/appointments",
            json=_payload(*clinical),
            headers={**full_access, "Idempotency-Key": key},
        )

        assert replay.status_code == 200
        assert replay.json()["data"]["id"] == first["id"]

        listed = await api.get("/api/v1/appointments", headers=full_access)
        assert listed.json()["metadata"]["pagination"]["total_records"] == 1

    async def test_double_booking_returns_409_with_the_clash(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """AC-2, surfaced to the client as a useful conflict."""
        first = await _book(api, full_access, *clinical)

        response = await api.post(
            "/api/v1/appointments",
            json=_payload(*clinical, offset_minutes=5),
            headers={**full_access, "Idempotency-Key": f"key-{uuid.uuid4().hex}"},
        )

        assert response.status_code == 409
        body = response.json()
        assert body["error_code"] == "RESOURCE_CONFLICT"

        # The conflict names what clashed, so reception can re-fetch slots and
        # pick another rather than guessing (module spec §14).
        clashes = body["errors"]["conflicting_appointments"]
        assert [c["appointment_id"] for c in clashes] == [first["id"]]
        assert clashes[0]["status"] == "booked"

    async def test_adjacent_slots_are_bookable(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        await _book(api, full_access, *clinical)
        await _book(api, full_access, *clinical, offset_minutes=15)

    async def test_booking_without_permission_returns_403(
        self, api: AsyncClient, read_only: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        response = await api.post(
            "/api/v1/appointments",
            json=_payload(*clinical),
            headers={**read_only, "Idempotency-Key": "no-perm-key-1"},
        )
        assert response.status_code == 403

    async def test_booking_without_token_returns_401(
        self, api: AsyncClient, clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        response = await api.post(
            "/api/v1/appointments",
            json=_payload(*clinical),
            headers={"Idempotency-Key": "no-auth-key-1"},
        )
        assert response.status_code == 401

    async def test_non_slot_duration_returns_422(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        patient_id, doctor_id = clinical
        response = await api.post(
            "/api/v1/appointments",
            json=_payload(
                patient_id,
                doctor_id,
                scheduled_end=(BASE + timedelta(minutes=17)).isoformat(),
            ),
            headers={**full_access, "Idempotency-Key": "bad-duration-1"},
        )
        assert response.status_code == 422

    async def test_naive_timestamp_returns_422(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        patient_id, doctor_id = clinical
        response = await api.post(
            "/api/v1/appointments",
            json=_payload(
                patient_id,
                doctor_id,
                scheduled_start="2030-01-07T09:00:00",
                scheduled_end="2030-01-07T09:15:00",
            ),
            headers={**full_access, "Idempotency-Key": "naive-ts-key-1"},
        )
        assert response.status_code == 422

    async def test_patient_from_another_tenant_is_rejected(
        self, api: AsyncClient, other_tenant: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        response = await api.post(
            "/api/v1/appointments",
            json=_payload(*clinical),
            headers={**other_tenant, "Idempotency-Key": "cross-tenant-1"},
        )
        assert response.status_code == 422


class TestLifecycleEndpoints:
    """Each transition, and the permission that gates it (spec §3, §10)."""

    async def test_full_happy_path(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        created = await _book(api, full_access, *clinical)
        appointment_id = created["id"]

        checked_in = await api.post(
            f"/api/v1/appointments/{appointment_id}/check-in", headers=full_access
        )
        assert checked_in.json()["data"]["status"] == "checked_in"
        assert checked_in.json()["data"]["checked_in_at"] is not None

        started = await api.post(
            f"/api/v1/appointments/{appointment_id}/start", headers=full_access
        )
        assert started.json()["data"]["status"] == "in_progress"

        completed = await api.post(
            f"/api/v1/appointments/{appointment_id}/complete", headers=full_access
        )
        assert completed.json()["data"]["status"] == "completed"
        assert completed.json()["data"]["completed_at"] is not None

    async def test_invalid_transition_returns_400(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """AC-3, through the HTTP stack."""
        created = await _book(api, full_access, *clinical)
        await api.post(
            f"/api/v1/appointments/{created['id']}/cancel",
            json={"reason": "Patient called"},
            headers=full_access,
        )

        response = await api.post(
            f"/api/v1/appointments/{created['id']}/check-in", headers=full_access
        )

        assert response.status_code == 400
        assert response.json()["error_code"] == "BUSINESS_RULE_VIOLATION"

    async def test_cancel_requires_a_reason(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        created = await _book(api, full_access, *clinical)
        response = await api.post(
            f"/api/v1/appointments/{created['id']}/cancel", json={}, headers=full_access
        )
        assert response.status_code == 422

    async def test_cancel_frees_the_slot(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The exclusion constraint ignores cancelled rows, so rebooking works."""
        created = await _book(api, full_access, *clinical)
        await api.post(
            f"/api/v1/appointments/{created['id']}/cancel",
            json={"reason": "Patient called"},
            headers=full_access,
        )

        await _book(api, full_access, *clinical)

    async def test_check_in_permission_does_not_grant_completion(
        self,
        api: AsyncClient,
        full_access: dict[str, str],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        clinical: tuple[uuid.UUID, uuid.UUID],
    ) -> None:
        """Spec §3: a nurse checks patients in but does not complete consultations."""
        created = await _book(api, full_access, *clinical)
        nurse = _auth(
            await _make_user(db_session, hospital_id, ["appointment.read", "appointment.check_in"]),
            hospital_id,
        )

        allowed = await api.post(f"/api/v1/appointments/{created['id']}/check-in", headers=nurse)
        assert allowed.status_code == 200

        refused = await api.post(f"/api/v1/appointments/{created['id']}/complete", headers=nurse)
        assert refused.status_code == 403

    async def test_no_show_endpoint(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        created = await _book(api, full_access, *clinical)
        response = await api.post(
            f"/api/v1/appointments/{created['id']}/no-show", headers=full_access
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "no_show"

    async def test_transition_across_tenants_returns_404(
        self,
        api: AsyncClient,
        full_access: dict[str, str],
        other_tenant: dict[str, str],
        clinical: tuple[uuid.UUID, uuid.UUID],
    ) -> None:
        created = await _book(api, full_access, *clinical)
        response = await api.post(
            f"/api/v1/appointments/{created['id']}/check-in", headers=other_tenant
        )
        assert response.status_code == 404


class TestReschedule:
    """``PATCH /api/v1/appointments/{id}`` (module spec §5.3)."""

    async def test_reschedule_moves_a_booked_appointment(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        created = await _book(api, full_access, *clinical)
        new_start = BASE + timedelta(hours=2)

        response = await api.patch(
            f"/api/v1/appointments/{created['id']}",
            json={
                "scheduled_start": new_start.isoformat(),
                "scheduled_end": (new_start + timedelta(minutes=15)).isoformat(),
            },
            headers=full_access,
        )

        assert response.status_code == 200
        assert response.json()["data"]["scheduled_start"].startswith("2030-01-07T11:00")

    async def test_reschedule_after_check_in_is_refused(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Only a booked appointment can be moved; later it is cancel-and-rebook."""
        created = await _book(api, full_access, *clinical)
        await api.post(f"/api/v1/appointments/{created['id']}/check-in", headers=full_access)

        new_start = BASE + timedelta(hours=2)
        response = await api.patch(
            f"/api/v1/appointments/{created['id']}",
            json={
                "scheduled_start": new_start.isoformat(),
                "scheduled_end": (new_start + timedelta(minutes=15)).isoformat(),
            },
            headers=full_access,
        )

        assert response.status_code == 400

    async def test_reschedule_onto_a_taken_slot_returns_409(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Module spec §14."""
        first = await _book(api, full_access, *clinical)
        second = await _book(api, full_access, *clinical, offset_minutes=60)

        response = await api.patch(
            f"/api/v1/appointments/{second['id']}",
            json={
                "scheduled_start": first["scheduled_start"],
                "scheduled_end": first["scheduled_end"],
            },
            headers=full_access,
        )

        assert response.status_code == 409

    async def test_reschedule_to_its_own_time_is_not_a_clash(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """An appointment must not conflict with itself."""
        created = await _book(api, full_access, *clinical)

        response = await api.patch(
            f"/api/v1/appointments/{created['id']}",
            json={
                "scheduled_start": created["scheduled_start"],
                "scheduled_end": created["scheduled_end"],
            },
            headers=full_access,
        )

        assert response.status_code == 200


class TestReadEndpoints:
    """Listing, history, and the queue."""

    async def test_status_history_records_every_transition(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """AC-6."""
        created = await _book(api, full_access, *clinical)
        await api.post(f"/api/v1/appointments/{created['id']}/check-in", headers=full_access)
        await api.post(f"/api/v1/appointments/{created['id']}/start", headers=full_access)

        response = await api.get(
            f"/api/v1/appointments/{created['id']}/status-history", headers=full_access
        )

        assert response.status_code == 200
        transitions = {(h["from_status"], h["to_status"]) for h in response.json()["data"]}
        assert (None, "booked") in transitions
        assert ("booked", "checked_in") in transitions
        assert ("checked_in", "in_progress") in transitions

    async def test_list_filters_by_doctor_and_status(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        patient_id, doctor_id = clinical
        await _book(api, full_access, patient_id, doctor_id)
        cancelled = await _book(api, full_access, patient_id, doctor_id, offset_minutes=60)
        await api.post(
            f"/api/v1/appointments/{cancelled['id']}/cancel",
            json={"reason": "Patient called"},
            headers=full_access,
        )

        by_doctor = await api.get(
            "/api/v1/appointments", params={"doctor_id": str(doctor_id)}, headers=full_access
        )
        assert by_doctor.json()["metadata"]["pagination"]["total_records"] == 2

        by_status = await api.get(
            "/api/v1/appointments", params={"status": "cancelled"}, headers=full_access
        )
        assert by_status.json()["metadata"]["pagination"]["total_records"] == 1

    async def test_list_filters_by_date(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        await _book(api, full_access, *clinical)

        hit = await api.get(
            "/api/v1/appointments", params={"date": "2030-01-07"}, headers=full_access
        )
        miss = await api.get(
            "/api/v1/appointments", params={"date": "2030-01-08"}, headers=full_access
        )

        assert hit.json()["metadata"]["pagination"]["total_records"] == 1
        assert miss.json()["metadata"]["pagination"]["total_records"] == 0

    async def test_date_is_the_hospitals_local_day_in_a_half_hour_zone(
        self,
        api: AsyncClient,
        full_access: dict[str, str],
        clinical: tuple[uuid.UUID, uuid.UUID],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        # PR #29 review finding 7. India is UTC+5:30, which a whole-hour offset
        # cannot express: the client used to round it to +6 and the day window
        # slid thirty minutes. Two appointments either side of local midnight:
        #   18:15 UTC = 23:45 IST on the 7th
        #   18:45 UTC = 00:15 IST on the 8th
        await _set_timezone(db_session, hospital_id, "Asia/Kolkata")
        late = await _book(api, full_access, *clinical, offset_minutes=555)
        after_midnight = await _book(api, full_access, *clinical, offset_minutes=585)

        seventh = await api.get(
            "/api/v1/appointments", params={"date": "2030-01-07"}, headers=full_access
        )
        eighth = await api.get(
            "/api/v1/appointments", params={"date": "2030-01-08"}, headers=full_access
        )

        assert [a["id"] for a in seventh.json()["data"]] == [late["id"]]
        assert [a["id"] for a in eighth.json()["data"]] == [after_midnight["id"]]

    async def test_date_follows_the_hospitals_timezone_not_the_callers(
        self,
        api: AsyncClient,
        full_access: dict[str, str],
        clinical: tuple[uuid.UUID, uuid.UUID],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        # The same two instants are both early afternoon on the 7th in New York.
        await _set_timezone(db_session, hospital_id, "America/New_York")
        await _book(api, full_access, *clinical, offset_minutes=555)
        await _book(api, full_access, *clinical, offset_minutes=585)

        seventh = await api.get(
            "/api/v1/appointments", params={"date": "2030-01-07"}, headers=full_access
        )
        eighth = await api.get(
            "/api/v1/appointments", params={"date": "2030-01-08"}, headers=full_access
        )

        assert seventh.json()["metadata"]["pagination"]["total_records"] == 2
        assert eighth.json()["metadata"]["pagination"]["total_records"] == 0

    async def test_the_retired_tz_offset_hours_parameter_changes_nothing(
        self,
        api: AsyncClient,
        full_access: dict[str, str],
        clinical: tuple[uuid.UUID, uuid.UUID],
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        # An older client that still sends it must get the correct local day,
        # not a 422 and not a shifted window.
        await _set_timezone(db_session, hospital_id, "Asia/Kolkata")
        late = await _book(api, full_access, *clinical, offset_minutes=555)
        await _book(api, full_access, *clinical, offset_minutes=585)

        response = await api.get(
            "/api/v1/appointments",
            params={"date": "2030-01-07", "tz_offset_hours": 6},
            headers=full_access,
        )

        assert response.status_code == 200
        assert [a["id"] for a in response.json()["data"]] == [late["id"]]

    async def test_list_does_not_leak_another_tenant(
        self,
        api: AsyncClient,
        full_access: dict[str, str],
        other_tenant: dict[str, str],
        clinical: tuple[uuid.UUID, uuid.UUID],
    ) -> None:
        await _book(api, full_access, *clinical)
        response = await api.get("/api/v1/appointments", headers=other_tenant)
        assert response.json()["data"] == []

    async def test_walk_in_queue(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        await _book(api, full_access, *clinical, type="walk_in")
        await _book(api, full_access, *clinical, offset_minutes=60)  # not a walk-in

        response = await api.get("/api/v1/appointments/queue", headers=full_access)

        assert response.status_code == 200
        assert len(response.json()["data"]) == 1
        assert response.json()["data"][0]["type"] == "walk_in"

    async def test_get_unknown_returns_404(
        self, api: AsyncClient, full_access: dict[str, str]
    ) -> None:
        response = await api.get(f"/api/v1/appointments/{uuid.uuid4()}", headers=full_access)
        assert response.status_code == 404


class TestSlotRecommendation:
    """``POST /appointments/recommend-slot`` (module spec §5.9)."""

    async def test_returns_empty_when_the_feature_flag_is_off(
        self, api: AsyncClient, full_access: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Flag defaults off, so this must degrade rather than error."""
        patient_id, doctor_id = clinical
        response = await api.post(
            "/api/v1/appointments/recommend-slot",
            json={"patient_id": str(patient_id), "doctor_id": str(doctor_id)},
            headers=full_access,
        )

        assert response.status_code == 200
        assert response.json()["data"]["recommendations"] == []

    async def test_requires_its_own_permission(
        self, api: AsyncClient, read_only: dict[str, str], clinical: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        patient_id, _ = clinical
        response = await api.post(
            "/api/v1/appointments/recommend-slot",
            json={"patient_id": str(patient_id)},
            headers=read_only,
        )
        assert response.status_code == 403
