"""Unit tests for :class:`PatientBookingService` — every collaborator mocked.

What is under test: the order of the checks, that nothing is written when one
fails, what is handed to the hospital's booking service, and how its answers
and refusals are turned into the patient's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.core.feature_flags import PATIENT_APP_ENABLED
from app.core.tenancy import current_tenant_scope
from app.models.appointment import AppointmentStatus, AppointmentType
from app.repositories.doctor_repository import DoctorDirectoryEntry
from app.repositories.hospital_repository import HospitalDirectoryEntry
from app.schemas.patient_app.appointments import BookAppointment
from app.services.appointment_service import DoubleBookingError, OutsideAvailabilityError
from app.services.patient_app.availability_service import SlotState
from app.services.patient_app.booking_service import PatientBookingService
from app.services.patient_app.errors import ConsentRequiredError, RecordLinkRequiredError
from app.services.patient_app.patient_authorization import PatientContext

START = datetime.now(UTC).replace(microsecond=0) + timedelta(days=5)
END = START + timedelta(minutes=15)
KEY = "k" * 32


def _payload(**overrides: Any) -> BookAppointment:
    return BookAppointment.model_validate({"start": START, "end": END, **overrides})


class _Booking:
    """The service over mocks, recording the tenant scope the write ran under."""

    def __init__(self, *, settings: dict[str, Any] | None = None) -> None:
        self.account: Any = SimpleNamespace(id=uuid.uuid4())
        self.hospital = HospitalDirectoryEntry(
            id=uuid.uuid4(),
            name="City Care",
            slug="city-care",
            settings={PATIENT_APP_ENABLED: True, **(settings or {})},
            address={},
            phone=None,
            logo_url=None,
            timezone="Asia/Kolkata",
        )
        self.doctor = DoctorDirectoryEntry(
            id=uuid.uuid4(),
            first_name="Asha",
            last_name="Menon",
            specialization="Cardiology",
            qualifications=[],
            languages=[],
            bio=None,
            department_id=None,
            department_name=None,
        )
        self.context = PatientContext(
            account_id=self.account.id, hospital_id=self.hospital.id, patient_id=uuid.uuid4()
        )
        self.scopes: list[Any] = []
        self.gate = AsyncMock()
        self.gate.describe.return_value = self.hospital
        self.authorization = AsyncMock()
        self.authorization.resolve_context.return_value = self.context
        self.doctors = AsyncMock()
        self.doctors.get_directory_entry.return_value = self.doctor
        self.appointments = AsyncMock()
        self.appointments.get_by_idempotency_key.return_value = None
        self.appointments.find_overlapping_for_patient.return_value = []
        self.appointments.count_upcoming_booked_for_patient.return_value = 0
        self.links = AsyncMock()
        self.availability = AsyncMock()
        self.availability.slot_state.return_value = SlotState.BOOKABLE
        self.staff = AsyncMock()
        self.staff.book_appointment.side_effect = self._booked
        self.created = True
        self.service = PatientBookingService(
            self.gate,
            self.authorization,
            self.doctors,
            self.appointments,
            self.links,
            self.availability,
            self.staff,
        )

    def stored(self, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "patient_id": self.context.patient_id,
            "doctor_id": self.doctor.id,
            "scheduled_start": START,
            "scheduled_end": END,
            "status": AppointmentStatus.BOOKED,
            "type": AppointmentType.NEW,
            "reason": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    async def _booked(self, hospital_id: uuid.UUID, request: Any, **kwargs: Any) -> Any:
        self.scopes.append(current_tenant_scope())
        return self.stored(reason=request.reason, type=request.type), self.created

    async def book(
        self, hospital_ref: str = "city-care", doctor_ref: str | None = None, **body: Any
    ) -> Any:
        return await self.service.book(
            self.account,
            hospital_ref,
            doctor_ref or str(self.doctor.id),
            _payload(**body),
            idempotency_key=KEY,
        )

    def nothing_written(self) -> None:
        self.staff.book_appointment.assert_not_awaited()


class TestWhatIsHandedToTheHospitalsBookingService:
    async def test_the_patient_is_the_links_own_and_nothing_can_be_overridden(self) -> None:
        booking = _Booking()

        outcome = await booking.book(reason="Cough", type="follow_up")

        call = booking.staff.book_appointment.await_args
        request = call.args[1]
        assert call.args[0] == booking.hospital.id
        assert (request.patient_id, request.doctor_id) == (
            booking.context.patient_id,
            booking.doctor.id,
        )
        assert (request.scheduled_start, request.scheduled_end) == (START, END)
        assert (request.type, request.reason, request.notes) == (
            AppointmentType.FOLLOW_UP,
            "Cough",
            None,
        )
        assert call.kwargs["idempotency_key"] == f"pt:{booking.account.id}:{KEY}"
        assert (call.kwargs["actor_id"], call.kwargs["allow_override"]) == (None, False)
        assert [scope.hospital_id for scope in booking.scopes] == [booking.hospital.id]
        assert current_tenant_scope() is None
        assert outcome.created is True
        assert outcome.appointment.model_dump()["doctor"] == {
            "ref": str(booking.doctor.id),
            "name": "Asha Menon",
            "specialization": "Cardiology",
        }
        assert outcome.appointment.start.utcoffset() == timedelta(hours=5, minutes=30)

    async def test_the_audit_event_names_the_patient_and_holds_no_free_text(self) -> None:
        booking = _Booking()
        await booking.book(reason="PRIVATE reason")

        build = booking.staff.book_appointment.await_args.kwargs["audit_event"]
        appointment_id = uuid.uuid4()
        event = build(SimpleNamespace(id=appointment_id))

        assert event.action == "patient.appointment.booked"
        assert (event.actor_type, event.patient_account_id, event.actor_id) == (
            "patient",
            booking.account.id,
            None,
        )
        assert (event.hospital_id, event.target_type, event.target_id) == (
            booking.hospital.id,
            "appointment",
            appointment_id,
        )
        assert event.context == {"doctor_id": str(booking.doctor.id), "type": "new"}
        assert event.changes == {}

    async def test_the_patients_lock_is_taken_before_anything_is_decided(self) -> None:
        booking = _Booking()
        order: list[str] = []

        async def _lock(*_args: Any) -> None:
            order.append("lock")

        async def _replay(*_args: Any) -> None:
            order.append("replay")

        async def _slot(*_args: Any, **_kwargs: Any) -> SlotState:
            order.append("slot")
            return SlotState.BOOKABLE

        booking.links.lock_account_in_hospital.side_effect = _lock
        booking.appointments.get_by_idempotency_key.side_effect = _replay
        booking.availability.slot_state.side_effect = _slot

        await booking.book()

        assert order == ["lock", "replay", "slot"]
        assert booking.links.lock_account_in_hospital.await_args.args == (
            booking.hospital.id,
            booking.account.id,
        )


class TestRefusalsWriteNothing:
    async def test_an_unknown_hospital_is_404_after_the_policy_gate(self) -> None:
        booking = _Booking()
        booking.gate.describe.return_value = None

        with pytest.raises(NotFoundError) as refusal:
            await booking.book()

        assert refusal.value.message == "Not Found"
        booking.authorization.ensure_policies_accepted.assert_awaited_once_with(booking.account.id)
        booking.authorization.resolve_context.assert_not_awaited()
        booking.nothing_written()

    async def test_a_pending_policy_is_refused_whatever_the_path_names(self) -> None:
        for unknown_hospital in (True, False):
            booking = _Booking()
            if unknown_hospital:
                booking.gate.describe.return_value = None
            booking.authorization.ensure_policies_accepted.side_effect = ConsentRequiredError
            booking.authorization.resolve_context.side_effect = ConsentRequiredError

            with pytest.raises(ConsentRequiredError):
                await booking.book()
            booking.nothing_written()

    @pytest.mark.parametrize("reference", ["", "x", "' OR 1=1", "a" * 4000, uuid.uuid4().hex])
    async def test_a_doctor_reference_that_is_not_one_is_404(self, reference: str) -> None:
        booking = _Booking()

        with pytest.raises(NotFoundError):
            await booking.book(doctor_ref=reference or " ")

        booking.doctors.get_directory_entry.assert_not_awaited()
        booking.nothing_written()

    async def test_no_usable_link_is_link_required_not_not_found(self) -> None:
        booking = _Booking()
        booking.authorization.resolve_context.side_effect = NotFoundError("Not found.")

        with pytest.raises(RecordLinkRequiredError):
            await booking.book()

        booking.doctors.get_directory_entry.assert_not_awaited()
        booking.links.lock_account_in_hospital.assert_not_awaited()
        booking.nothing_written()

    async def test_a_doctor_the_directory_does_not_list_is_404(self) -> None:
        booking = _Booking()
        booking.doctors.get_directory_entry.return_value = None

        with pytest.raises(NotFoundError):
            await booking.book()

        assert booking.doctors.get_directory_entry.await_args.args == (
            booking.hospital.id,
            booking.doctor.id,
        )
        booking.nothing_written()

    @pytest.mark.parametrize(
        ("state", "error", "message"),
        [
            (SlotState.TAKEN, ConflictError, "This time is no longer available."),
            (SlotState.NOT_A_SLOT, BusinessRuleError, "This time cannot be booked."),
        ],
    )
    async def test_a_slot_that_is_not_bookable_now(
        self, state: SlotState, error: type[Exception], message: str
    ) -> None:
        booking = _Booking()
        booking.availability.slot_state.return_value = state

        with pytest.raises(error) as refusal:
            await booking.book()

        assert refusal.value.message == message  # type: ignore[attr-defined]
        assert not refusal.value.detail  # type: ignore[attr-defined]
        booking.nothing_written()

    async def test_the_patients_own_overlap(self) -> None:
        booking = _Booking()
        booking.appointments.find_overlapping_for_patient.return_value = [booking.stored()]

        with pytest.raises(BusinessRuleError) as refusal:
            await booking.book()

        assert refusal.value.message == "You already have an appointment at this time."
        call = booking.appointments.find_overlapping_for_patient.await_args
        assert call.args == (booking.hospital.id, booking.context.patient_id, START, END)
        booking.nothing_written()

    @pytest.mark.parametrize(
        ("limit", "held", "allowed"), [(3, 2, True), (3, 3, False), (1, 1, False), (5, 4, True)]
    )
    async def test_the_booking_limit_is_the_hospitals(
        self, limit: int, held: int, allowed: bool
    ) -> None:
        booking = _Booking(settings={"patient_app.max_active_bookings": limit})
        booking.appointments.count_upcoming_booked_for_patient.return_value = held

        if allowed:
            assert (await booking.book()).created is True
        else:
            with pytest.raises(BusinessRuleError) as refusal:
                await booking.book()
            assert "limit" in refusal.value.message
            booking.nothing_written()


class TestTheHospitalsAnswerBecomesThePatients:
    @pytest.mark.parametrize(
        "raised",
        [
            DoubleBookingError([{"appointment_id": "SECRET", "patient_name": "SECRET"}]),
            IntegrityError("insert", {}, Exception("no_overlap_per_doctor")),
        ],
    )
    async def test_a_lost_race_is_the_one_conflict_and_describes_nobody(
        self, raised: Exception
    ) -> None:
        booking = _Booking()
        booking.staff.book_appointment.side_effect = raised

        with pytest.raises(ConflictError) as refusal:
            await booking.book()

        assert refusal.value.message == "This time is no longer available."
        assert not refusal.value.detail
        assert refusal.value.__cause__ is None

    @pytest.mark.parametrize(
        "raised",
        [
            OutsideAvailabilityError(uuid.uuid4()),
            ValidationError(message="Cannot book an appointment in the past."),
            ValidationError(message="Patient not found in this hospital."),
        ],
    )
    async def test_a_staff_path_refusal_is_one_generic_refusal(self, raised: Exception) -> None:
        booking = _Booking()
        booking.staff.book_appointment.side_effect = raised

        with pytest.raises(BusinessRuleError) as refusal:
            await booking.book()

        assert refusal.value.message == "This time cannot be booked."
        assert not refusal.value.detail

    async def test_an_appointment_the_key_already_holds_is_returned_only_for_the_same_request(
        self,
    ) -> None:
        booking = _Booking()
        held = booking.stored()
        booking.appointments.get_by_idempotency_key.return_value = held

        outcome = await booking.book()

        assert (outcome.created, outcome.appointment.ref) == (False, str(held.id))
        booking.availability.slot_state.assert_not_awaited()
        booking.nothing_written()
        call = booking.appointments.get_by_idempotency_key.await_args
        assert call.args == (booking.hospital.id, f"pt:{booking.account.id}:{KEY}")

    @pytest.mark.parametrize(
        "difference",
        [
            {"patient_id": uuid.uuid4()},
            {"doctor_id": uuid.uuid4()},
            {
                "scheduled_start": START + timedelta(minutes=15),
                "scheduled_end": END + timedelta(minutes=15),
            },
            {"scheduled_end": END + timedelta(minutes=15)},
        ],
    )
    async def test_the_same_key_for_anything_else_is_refused(
        self, difference: dict[str, Any]
    ) -> None:
        for through_staff_path in (False, True):
            booking = _Booking()
            held = booking.stored(**difference)
            if through_staff_path:
                booking.staff.book_appointment.side_effect = None
                booking.staff.book_appointment.return_value = (held, False)
            else:
                booking.appointments.get_by_idempotency_key.return_value = held

            with pytest.raises(ConflictError) as refusal:
                await booking.book()

            assert refusal.value.message == "This request was already used for another booking."
            assert str(held.id) not in str(refusal.value.detail)


class TestTheRequestBody:
    @pytest.mark.parametrize(
        "body",
        [
            {"start": "2026-10-12T10:00:00", "end": "2026-10-12T10:15:00"},
            {"start": START, "end": END, "patient_id": str(uuid.uuid4())},
            {"start": START, "end": END, "doctor_id": str(uuid.uuid4())},
            {"start": START, "end": END, "hospital_id": str(uuid.uuid4())},
            {"start": START, "end": END, "notes": "x"},
            {"start": START, "end": END, "type": "walk_in"},
            {"start": START, "end": END, "type": "emergency"},
            {"start": START, "end": END, "reason": "r" * 501},
            {"start": START, "end": END, "reason": "a\x00b"},
            {"start": START},
        ],
    )
    def test_what_the_body_refuses(self, body: dict[str, Any]) -> None:
        with pytest.raises(Exception, match="validation error"):
            BookAppointment.model_validate(body)

    def test_a_reason_is_trimmed_and_blank_is_absent(self) -> None:
        assert _payload(reason="  Cough ").reason == "Cough"
        assert _payload(reason="   ").reason is None
        assert _payload().type.value == "new"
