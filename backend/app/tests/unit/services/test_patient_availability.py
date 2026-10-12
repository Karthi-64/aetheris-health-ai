"""Unit tests for :class:`DoctorAvailabilityService` — mocked gate and repositories.

What is under test: the order of the checks, the one refusal, the bookable
window reckoned in the hospital's timezone, that every read names the resolved
hospital inside its tenant scope, that the reads do not multiply with the
range, and what of the engine's slots a patient is shown.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.core.exceptions import (
    BusinessRuleError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.feature_flags import PATIENT_APP_ENABLED
from app.core.tenancy import current_tenant_scope
from app.repositories.doctor_repository import DoctorDirectoryEntry
from app.repositories.hospital_repository import HospitalDirectoryEntry
from app.services.patient_app.availability_service import (
    DoctorAvailabilityService,
    SlotState,
    _bounded_range,
)
from app.services.patient_app.errors import ConsentRequiredError
from app.services.patient_app.hospital_gate import PatientHospitalGate

ACCOUNT: Any = SimpleNamespace(id=uuid.uuid4())
#: Monday 2026-10-05, 09:00 in Kolkata.
NOW = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)
D = date(2026, 10, 5)


def _hospital(
    timezone: str = "Asia/Kolkata", settings: dict[str, Any] | None = None
) -> HospitalDirectoryEntry:
    return HospitalDirectoryEntry(
        id=uuid.uuid4(),
        name="City Care",
        slug="city-care",
        settings={PATIENT_APP_ENABLED: True, **(settings or {})},
        address={},
        phone=None,
        logo_url=None,
        timezone=timezone,
    )


def _doctor() -> DoctorDirectoryEntry:
    return DoctorDirectoryEntry(
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


def _window(day: int, start: time, end: time, minutes: int = 15) -> Any:
    return SimpleNamespace(
        day_of_week=day, start_time=start, end_time=end, slot_duration_minutes=minutes
    )


class _Availability:
    """The service over mocks, recording the tenant scope each read ran under."""

    def __init__(
        self, hospital: HospitalDirectoryEntry | None, doctor: DoctorDirectoryEntry | None
    ) -> None:
        self.hospital, self.doctor = hospital, doctor
        self.scopes: list[Any] = []
        self.gate = MagicMock(spec=PatientHospitalGate)
        self.gate.describe = AsyncMock(return_value=hospital)
        self.doctors = AsyncMock()
        self.appointments = AsyncMock()
        self.consent = AsyncMock()
        self.now = NOW
        self._reading("doctors", "get_directory_entry", doctor)
        self._reading("doctors", "get_availability", [])
        self._reading("doctors", "list_leaves", [])
        self._reading("appointments", "booked_intervals_for_doctor", [])
        self.service = DoctorAvailabilityService(
            self.gate, self.doctors, self.appointments, self.consent, clock=lambda: self.now
        )

    def _reading(self, repository: str, name: str, value: Any) -> None:
        async def _read(*_args: Any, **_kwargs: Any) -> Any:
            self.scopes.append(current_tenant_scope())
            return value

        getattr(getattr(self, repository), name).side_effect = _read

    def windows(self, *windows: Any) -> None:
        self._reading("doctors", "get_availability", list(windows))

    async def read(self, **dates: date | None) -> Any:
        assert self.doctor is not None
        return await self.service.get_availability(
            ACCOUNT,
            "city-care",
            str(self.doctor.id),
            start_date=dates.get("start_date"),
            end_date=dates.get("end_date"),
        )


class TestOrderOfChecks:
    async def test_a_pending_policy_is_refused_before_anything_is_looked_up(self) -> None:
        availability = _Availability(_hospital(), _doctor())
        availability.consent.ensure_policies_accepted.side_effect = ConsentRequiredError

        with pytest.raises(ConsentRequiredError):
            await availability.read()

        availability.gate.describe.assert_not_awaited()
        assert availability.doctors.mock_calls == []

    async def test_a_hospital_that_is_not_shown_reads_nothing(self) -> None:
        availability = _Availability(None, _doctor())

        with pytest.raises(NotFoundError) as refusal:
            await availability.read()

        assert refusal.value.message == "Not Found"
        assert availability.doctors.mock_calls == []
        assert availability.appointments.mock_calls == []

    @pytest.mark.parametrize(
        "reference", ["", "x", "' OR 1=1", "../..", "a" * 4000, uuid.uuid4().hex, "{x}"]
    )
    async def test_a_doctor_reference_that_is_not_one_reads_nothing(self, reference: str) -> None:
        availability = _Availability(_hospital(), _doctor())

        with pytest.raises(NotFoundError) as refusal:
            await availability.service.get_availability(
                ACCOUNT, "city-care", reference, start_date=None, end_date=None
            )

        assert refusal.value.message == "Not Found"
        assert availability.doctors.mock_calls == []

    async def test_a_doctor_that_is_not_listed_is_the_same_refusal_and_reads_no_calendar(
        self,
    ) -> None:
        availability = _Availability(_hospital(), None)
        doctor_id = uuid.uuid4()

        with pytest.raises(NotFoundError) as refusal:
            await availability.service.get_availability(
                ACCOUNT, "city-care", str(doctor_id), start_date=None, end_date=None
            )

        assert refusal.value.message == "Not Found"
        assert availability.doctors.get_availability.await_count == 0
        assert availability.appointments.mock_calls == []
        call = availability.doctors.get_directory_entry.await_args
        assert call.args == (availability.hospital.id, doctor_id)  # type: ignore[union-attr]

    async def test_a_hospital_timezone_that_is_not_one_is_a_generic_refusal(self) -> None:
        availability = _Availability(_hospital(timezone="Mars/Olympus"), _doctor())

        with pytest.raises(ServiceUnavailableError) as refusal:
            await availability.read()

        assert "Mars" not in refusal.value.message
        assert availability.doctors.get_availability.await_count == 0


class TestTheBookableWindow:
    """``_bounded_range`` is pure; the policy is applied there and nowhere else."""

    TODAY = D
    HORIZON = D + timedelta(days=30)

    def _range(self, start: date | None, end: date | None) -> tuple[date, date]:
        return _bounded_range(start, end, today=self.TODAY, horizon_end=self.HORIZON)

    def test_the_default_is_a_week_from_today(self) -> None:
        assert self._range(None, None) == (D, D + timedelta(days=6))

    def test_the_default_end_stops_at_the_horizon(self) -> None:
        near = D + timedelta(days=28)
        assert self._range(near, None) == (near, self.HORIZON)
        assert self._range(self.HORIZON, None) == (self.HORIZON, self.HORIZON)

    def test_an_end_alone_counts_from_today(self) -> None:
        assert self._range(None, D + timedelta(days=2)) == (D, D + timedelta(days=2))

    @pytest.mark.parametrize("days", [1, 7, 14])
    def test_a_range_of_up_to_fourteen_days_is_answered(self, days: int) -> None:
        end = D + timedelta(days=days - 1)
        assert self._range(D, end) == (D, end)

    def test_a_backwards_range_is_malformed(self) -> None:
        with pytest.raises(ValidationError):
            self._range(D + timedelta(days=1), D)
        with pytest.raises(ValidationError):
            self._range(None, D - timedelta(days=1))

    def test_a_range_longer_than_fourteen_days_is_malformed(self) -> None:
        with pytest.raises(ValidationError):
            self._range(D, D + timedelta(days=14))

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            (D - timedelta(days=1), None),
            (D - timedelta(days=1), D + timedelta(days=1)),
            (D + timedelta(days=31), None),
            (D + timedelta(days=25), D + timedelta(days=31)),
            (D + timedelta(days=20), D + timedelta(days=31)),
        ],
    )
    def test_a_date_outside_today_to_the_horizon_is_refused(
        self, start: date | None, end: date | None
    ) -> None:
        with pytest.raises(BusinessRuleError) as refusal:
            self._range(start, end)
        assert refusal.value.message == "Outside the bookable window."
        assert not refusal.value.detail

    def test_malformed_is_decided_before_outside(self) -> None:
        """A range that is both backwards and in the past is malformed first."""
        with pytest.raises(ValidationError):
            self._range(D - timedelta(days=1), D - timedelta(days=3))


class TestTodayIsTheHospitals:
    @pytest.mark.parametrize(
        ("timezone", "now", "today"),
        [
            ("Asia/Kolkata", datetime(2026, 10, 5, 18, 29, 59, tzinfo=UTC), date(2026, 10, 5)),
            ("Asia/Kolkata", datetime(2026, 10, 5, 18, 30, tzinfo=UTC), date(2026, 10, 6)),
            ("Pacific/Kiritimati", datetime(2026, 10, 5, 11, 30, tzinfo=UTC), date(2026, 10, 6)),
            ("Pacific/Pago_Pago", datetime(2026, 10, 6, 9, 30, tzinfo=UTC), date(2026, 10, 5)),
            ("UTC", datetime(2026, 10, 5, 23, 59, tzinfo=UTC), date(2026, 10, 5)),
        ],
    )
    async def test_today_and_the_horizon_are_reckoned_in_the_hospital_zone(
        self, timezone: str, now: datetime, today: date
    ) -> None:
        availability = _Availability(_hospital(timezone=timezone), _doctor())
        availability.now = now

        answer = await availability.read()

        assert (answer.today, answer.horizon_end) == (today, today + timedelta(days=30))
        assert answer.timezone == timezone
        assert [day.date for day in answer.days] == [
            today + timedelta(days=offset) for offset in range(7)
        ]

    async def test_the_hospital_policy_sets_horizon_and_lead(self) -> None:
        hospital = _hospital(
            settings={"patient_app.booking_horizon_days": 3, "patient_app.min_lead_minutes": 90}
        )
        availability = _Availability(hospital, _doctor())

        answer = await availability.read()

        assert (answer.horizon_end, answer.min_lead_minutes) == (D + timedelta(days=3), 90)
        assert [day.date for day in answer.days] == [D + timedelta(days=n) for n in range(4)]


class TestEveryReadNamesTheHospital:
    async def test_the_reads_cover_the_whole_range_once_inside_the_tenant_scope(self) -> None:
        hospital, doctor = _hospital(), _doctor()
        availability = _Availability(hospital, doctor)
        end = D + timedelta(days=13)

        await availability.read(start_date=D, end_date=end)

        # Local midnight of the first day to local midnight after the last.
        window_start = datetime(2026, 10, 4, 18, 30, tzinfo=UTC)
        window_end = datetime(2026, 10, 18, 18, 30, tzinfo=UTC)
        assert availability.doctors.get_availability.await_args.args == (hospital.id, doctor.id)
        leaves = availability.doctors.list_leaves.await_args
        assert leaves.args == (hospital.id, doctor.id)
        assert leaves.kwargs == {"starts_before": window_end, "ends_after": window_start}
        booked = availability.appointments.booked_intervals_for_doctor.await_args
        assert booked.args == (hospital.id, doctor.id, window_start, window_end)
        assert {scope.hospital_id for scope in availability.scopes} == {hospital.id}
        assert len(availability.scopes) == 4
        assert current_tenant_scope() is None

    @pytest.mark.parametrize("days", [1, 7, 14])
    async def test_the_number_of_reads_does_not_grow_with_the_range(self, days: int) -> None:
        availability = _Availability(_hospital(), _doctor())
        availability.windows(*[_window(d, time(9), time(17)) for d in range(7)])

        answer = await availability.read(start_date=D, end_date=D + timedelta(days=days - 1))

        assert len(answer.days) == days
        assert sum(len(day.slots) for day in answer.days) > 0
        assert availability.doctors.get_availability.await_count == 1
        assert availability.doctors.list_leaves.await_count == 1
        assert availability.appointments.booked_intervals_for_doctor.await_count == 1

    async def test_nothing_is_written(self) -> None:
        availability = _Availability(_hospital(), _doctor())

        await availability.read()

        names = {call[0] for call in availability.doctors.mock_calls}
        names |= {call[0] for call in availability.appointments.mock_calls}
        assert names <= {
            "get_directory_entry",
            "get_availability",
            "list_leaves",
            "booked_intervals_for_doctor",
        }


class TestWhatIsShown:
    async def test_only_available_slots_from_the_lead_time_on(self) -> None:
        availability = _Availability(_hospital(), _doctor())
        availability.windows(_window(0, time(9), time(12)))
        # 10:30–11:00 IST is leave; 11:15–11:30 IST is booked.
        leave = (datetime(2026, 10, 5, 5, 0, tzinfo=UTC), datetime(2026, 10, 5, 5, 30, tzinfo=UTC))
        availability._reading(
            "doctors", "list_leaves", [SimpleNamespace(starts_at=leave[0], ends_at=leave[1])]
        )
        booked = SimpleNamespace(
            id=uuid.uuid4(),
            scheduled_start=datetime(2026, 10, 5, 5, 45, tzinfo=UTC),
            scheduled_end=datetime(2026, 10, 5, 6, 0, tzinfo=UTC),
        )
        availability._reading("appointments", "booked_intervals_for_doctor", [booked])

        answer = await availability.read(start_date=D, end_date=D)

        [day] = answer.days
        starts = [slot.start.strftime("%H:%M%z") for slot in day.slots]
        # 09:00–09:45 are before now + 60 min; 10:30 and 10:45 are on leave; 11:15 is booked.
        assert starts == [
            "10:00+0530",
            "10:15+0530",
            "11:00+0530",
            "11:30+0530",
            "11:45+0530",
        ]
        assert all(slot.end - slot.start == timedelta(minutes=15) for slot in day.slots)
        assert {slot.start.utcoffset() for slot in day.slots} == {timedelta(hours=5, minutes=30)}

    async def test_a_slot_starting_exactly_at_the_lead_is_shown(self) -> None:
        availability = _Availability(_hospital(), _doctor())
        availability.windows(_window(0, time(9), time(12)))
        availability.now = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)  # 09:00 IST, lead 60 → 10:00

        answer = await availability.read(start_date=D, end_date=D)

        assert answer.days[0].slots[0].start.strftime("%H:%M") == "10:00"

    async def test_a_day_without_a_window_is_listed_empty(self) -> None:
        availability = _Availability(_hospital(), _doctor())
        availability.windows(_window(0, time(10), time(11)))

        answer = await availability.read(start_date=D, end_date=D + timedelta(days=1))

        assert [(day.date, len(day.slots)) for day in answer.days] == [
            (D, 4),
            (D + timedelta(days=1), 0),
        ]

    async def test_the_hour_a_spring_forward_skips_is_not_offered_under_another_name(self) -> None:
        """The engine labels 30-minute slots inside the skipped hour with times that never happen."""
        availability = _Availability(_hospital(timezone="America/New_York"), _doctor())
        availability.windows(_window(6, time(1), time(4), 30))  # Sunday 2026-03-08
        availability.now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
        sunday = date(2026, 3, 8)

        answer = await availability.read(start_date=sunday, end_date=sunday)

        [day] = answer.days
        assert [slot.start.isoformat() for slot in day.slots] == [
            "2026-03-08T01:00:00-05:00",
            "2026-03-08T01:30:00-05:00",
            "2026-03-08T03:00:00-04:00",
            "2026-03-08T03:30:00-04:00",
        ]
        assert len({slot.start.astimezone(UTC) for slot in day.slots}) == 4

    async def test_the_hour_a_fall_back_repeats_is_offered_once_per_instant(self) -> None:
        availability = _Availability(_hospital(timezone="America/New_York"), _doctor())
        availability.windows(_window(6, time(0, 30), time(3, 30), 30))  # Sunday 2026-11-01
        availability.now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
        sunday = date(2026, 11, 1)

        answer = await availability.read(start_date=sunday, end_date=sunday)

        [day] = answer.days
        instants = [slot.start.astimezone(UTC) for slot in day.slots]
        assert instants == sorted(instants) and len(set(instants)) == len(instants)
        assert all(slot.end > slot.start for slot in day.slots)


class TestSlotState:
    """What booking asks before it writes: is exactly this slot bookable right now."""

    async def _state(
        self, start: datetime, end: datetime, *, now: datetime = NOW, **setup: Any
    ) -> Any:
        hospital = _hospital(settings=setup.get("settings"))
        availability = _Availability(hospital, _doctor())
        availability.windows(_window(0, time(9), time(12)))
        if "leave" in setup:
            leave = setup["leave"]
            availability._reading(
                "doctors", "list_leaves", [SimpleNamespace(starts_at=leave[0], ends_at=leave[1])]
            )
        if "booked" in setup:
            booked = setup["booked"]
            availability._reading(
                "appointments",
                "booked_intervals_for_doctor",
                [
                    SimpleNamespace(
                        id=uuid.uuid4(), scheduled_start=booked[0], scheduled_end=booked[1]
                    )
                ],
            )
        return await availability.service.slot_state(hospital, uuid.uuid4(), start, end, now=now)

    @staticmethod
    def _ist(hour: int, minute: int = 0, day: date = D) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo("Asia/Kolkata"))

    async def test_a_real_free_slot_past_the_lead_is_bookable(self) -> None:
        assert await self._state(self._ist(10), self._ist(10, 15)) is SlotState.BOOKABLE
        # The same instants in another offset are the same slot.
        assert (
            await self._state(self._ist(10).astimezone(UTC), self._ist(10, 15).astimezone(UTC))
            is SlotState.BOOKABLE
        )

    async def test_a_slot_under_a_booking_or_a_leave_is_taken(self) -> None:
        booked = (self._ist(10, 5), self._ist(10, 10))
        leave = (self._ist(10), self._ist(11))
        assert await self._state(self._ist(10), self._ist(10, 15), booked=booked) is SlotState.TAKEN
        assert await self._state(self._ist(10), self._ist(10, 15), leave=leave) is SlotState.TAKEN
        assert (
            await self._state(self._ist(11), self._ist(11, 15), leave=leave) is SlotState.BOOKABLE
        )

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            ((10, 5), (10, 20)),
            ((10, 0), (10, 30)),
            ((10, 0), (10, 10)),
            ((8, 45), (9, 0)),
            ((12, 0), (12, 15)),
            ((10, 15), (10, 0)),
        ],
    )
    async def test_bounds_that_are_not_a_slot_of_the_engine(
        self, start: tuple[int, int], end: tuple[int, int]
    ) -> None:
        assert await self._state(self._ist(*start), self._ist(*end)) is SlotState.NOT_A_SLOT

    async def test_the_lead_time_and_the_horizon_bound_it(self) -> None:
        ten = (self._ist(10), self._ist(10, 15))
        assert await self._state(*ten, now=self._ist(9)) is SlotState.BOOKABLE
        assert (
            await self._state(*ten, now=self._ist(9, 0) + timedelta(seconds=1))
            is SlotState.NOT_A_SLOT
        )
        assert await self._state(*ten, now=self._ist(10, 30)) is SlotState.NOT_A_SLOT
        assert (
            await self._state(*ten, now=self._ist(9, day=D - timedelta(days=30)))
            is SlotState.BOOKABLE
        )
        assert (
            await self._state(*ten, now=self._ist(9, day=D - timedelta(days=31)))
            is SlotState.NOT_A_SLOT
        )
        assert (
            await self._state(*ten, now=self._ist(9, day=D + timedelta(days=1)))
            is SlotState.NOT_A_SLOT
        )
        strict = {"patient_app.booking_horizon_days": 2, "patient_app.min_lead_minutes": 0}
        assert await self._state(*ten, now=self._ist(9, 59), settings=strict) is SlotState.BOOKABLE
        assert (
            await self._state(*ten, now=self._ist(9, day=D - timedelta(days=3)), settings=strict)
            is SlotState.NOT_A_SLOT
        )

    async def test_a_day_without_a_window_reads_as_no_slot_without_error(self) -> None:
        tuesday = D + timedelta(days=1)
        assert (
            await self._state(self._ist(10, day=tuesday), self._ist(10, 15, day=tuesday))
            is SlotState.NOT_A_SLOT
        )
