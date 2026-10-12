"""Patient appointment booking: one patient, one slot, booked by the hospital's own booking path.

``docs/modules/15-patient-app.md`` §13.

**No second booking engine.** The appointment is created by
:meth:`AppointmentService.book_appointment` — the staff path: same row, same
status-history row, same transaction, and the same database exclusion
constraint as the final authority against a double booking. This service is
the patient's authorization and policy around that call, and nothing else.

**Nothing is trusted from the request but a slot.** The patient is the
authenticated account; the record is the one its own link names at the
hospital the path names; the doctor is one doctor discovery lists there. The
body has no field for any of them.

**The slot is re-checked from current rows** immediately before the write, by
the code that lists slots (:meth:`DoctorAvailabilityService.slot_state`): it
must be a real slot of the hospital's engine, available, no earlier than the
lead time, no later than the horizon. What the availability screen showed a
moment ago proves nothing.

**One patient at a time.** A transaction-scoped advisory lock on the
(hospital, account) pair serialises a patient's own bookings there, so the
booking limit, the own-overlap rule and the idempotent replay are each decided
against what the previous attempt committed. Two *different* patients racing
for one slot are decided by the exclusion constraint: exactly one row exists.

**Idempotency.** The stored key is ``pt:{account_id}:{client_key}``, so it can
collide with no staff key and no other patient's. A replay returns the
appointment only if it is the caller's and for the same doctor and slot;
the same key with a different request is refused.

**Refusals say little.** A slot that is taken — however that was found out —
is one ``409`` with one message, and never carries the other booking.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.core.tenancy import TenantScope, tenant_scope
from app.models.appointment import AppointmentType
from app.schemas.appointment import BookAppointmentRequest
from app.schemas.patient_app.appointments import (
    PatientAppointment,
    PatientAppointmentDoctor,
    PatientAppointmentHospital,
)
from app.services.appointment_service import DoubleBookingError
from app.services.patient_app.availability_service import SlotState
from app.services.patient_app.booking_policy import BookingPolicy
from app.services.patient_app.common import patient_event
from app.services.patient_app.errors import RecordLinkRequiredError
from app.utils.datetime import utc_now

if TYPE_CHECKING:
    from datetime import datetime

    from app.core.audit import AuditEvent
    from app.models.appointment import Appointment
    from app.models.patient_account import PatientAccount
    from app.repositories.appointment_repository import AppointmentRepository
    from app.repositories.doctor_repository import DoctorDirectoryEntry, DoctorRepository
    from app.repositories.hospital_repository import HospitalDirectoryEntry
    from app.repositories.patient_account_link_repository import PatientAccountLinkRepository
    from app.schemas.patient_app.appointments import BookAppointment
    from app.services.appointment_service import AppointmentService
    from app.services.patient_app.availability_service import DoctorAvailabilityService
    from app.services.patient_app.common import ClientContext
    from app.services.patient_app.hospital_gate import PatientHospitalGate
    from app.services.patient_app.patient_authorization import PatientAuthorization

__all__ = ["BookingOutcome", "PatientBookingService"]

logger = structlog.get_logger(__name__)

_NOT_FOUND: Final = HTTPStatus.NOT_FOUND.phrase
#: The fixed messages of a refusal. None is ever made more specific at a call site.
SLOT_TAKEN: Final = "This time is no longer available."
KEY_REUSED: Final = "This request was already used for another booking."
NOT_BOOKABLE: Final = "This time cannot be booked."
OWN_OVERLAP: Final = "You already have an appointment at this time."
LIMIT_REACHED: Final = "You have reached the limit of upcoming appointments at this hospital."

_UUID_TEXT: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True, slots=True)
class BookingOutcome:
    """The result of a booking request.

    :param appointment: The patient's appointment.
    :param created: ``False`` when the request replayed an earlier booking.
    """

    appointment: PatientAppointment
    created: bool


class PatientBookingService:
    """Books an appointment for the authenticated patient.

    :param gate: Which hospitals are open to patients.
    :param authorization: Resolves the account's own record at a hospital.
    :param doctors: Doctor data access — the directory.
    :param appointments: Appointment data access — replay and the patient's calendar.
    :param links: Record-link data access — the per-patient lock.
    :param availability: The slot check booking shares with the slot list.
    :param booking: The hospital's booking service, which creates the row.
    """

    def __init__(
        self,
        gate: PatientHospitalGate,
        authorization: PatientAuthorization,
        doctors: DoctorRepository,
        appointments: AppointmentRepository,
        links: PatientAccountLinkRepository,
        availability: DoctorAvailabilityService,
        booking: AppointmentService,
    ) -> None:
        self._gate = gate
        self._authorization = authorization
        self._doctors = doctors
        self._appointments = appointments
        self._links = links
        self._availability = availability
        self._booking = booking

    async def book(
        self,
        account: PatientAccount,
        hospital_ref: str,
        doctor_ref: str,
        payload: BookAppointment,
        *,
        idempotency_key: str,
        client: ClientContext | None = None,
    ) -> BookingOutcome:
        """Book one slot of one doctor for the caller's own record.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's code or id, from the path.
        :param doctor_ref: The doctor's reference, from the path.
        :param payload: The slot, the type and an optional reason.
        :param idempotency_key: The client's key, already validated for form.
        :param client: Where the request came from, for the audit record.
        :returns: The appointment, and whether this request created it.
        :raises ConsentRequiredError: If a required policy or the record-link
            consent is not in force.
        :raises NotFoundError: If the hospital or the doctor cannot be shown.
        :raises RecordLinkRequiredError: If the account has no usable link there.
        :raises ConflictError: If the slot is taken, or the key was used for
            another request.
        :raises BusinessRuleError: If the slot cannot be booked, or the
            patient's own calendar or booking limit forbids it.
        """
        # Read once: the commit below expires the account object.
        account_id = account.id
        hospital = await self._gate.describe(hospital_ref)
        reference = doctor_ref.strip().lower()
        if hospital is None or not _UUID_TEXT.fullmatch(reference):
            # The policy gate still comes first for a signed-in patient.
            await self._authorization.ensure_policies_accepted(account_id)
            raise NotFoundError(_NOT_FOUND)
        try:
            context = await self._authorization.resolve_context(account, hospital.id)
        except NotFoundError:
            # The hospital is open, so what is missing is the caller's own link.
            raise RecordLinkRequiredError from None

        stored_key = f"pt:{account_id}:{idempotency_key}"
        start, end = payload.start, payload.end

        with tenant_scope(TenantScope.hospital(context.hospital_id)):
            doctor = await self._doctors.get_directory_entry(hospital.id, uuid.UUID(reference))
            if doctor is None:
                raise NotFoundError(_NOT_FOUND)

            # From here to the commit, this patient's bookings at this hospital
            # run one at a time.
            await self._links.lock_account_in_hospital(hospital.id, account_id)

            replayed = await self._appointments.get_by_idempotency_key(hospital.id, stored_key)
            if replayed is not None:
                return self._replay(replayed, context.patient_id, doctor, hospital, payload)

            now = utc_now()
            state = await self._availability.slot_state(hospital, doctor.id, start, end, now=now)
            if state is SlotState.TAKEN:
                self._refused("slot_taken", account_id, hospital.id)
                raise ConflictError(message=SLOT_TAKEN)
            if state is not SlotState.BOOKABLE:
                self._refused("not_bookable", account_id, hospital.id)
                raise BusinessRuleError(message=NOT_BOOKABLE)

            if await self._appointments.find_overlapping_for_patient(
                hospital.id, context.patient_id, start, end
            ):
                self._refused("own_overlap", account_id, hospital.id)
                raise BusinessRuleError(message=OWN_OVERLAP)
            policy = BookingPolicy.from_settings(hospital.settings)
            held = await self._appointments.count_upcoming_booked_for_patient(
                hospital.id, context.patient_id, after=now
            )
            if held >= policy.max_active_bookings:
                self._refused("limit_reached", account_id, hospital.id)
                raise BusinessRuleError(message=LIMIT_REACHED)

            def _audit(appointment: Appointment) -> AuditEvent:
                return patient_event(
                    "patient.appointment.booked",
                    target_type="appointment",
                    account_id=account_id,
                    hospital_id=hospital.id,
                    target_id=appointment.id,
                    context={"doctor_id": str(doctor.id), "type": payload.type.value},
                    client=client,
                )

            request = BookAppointmentRequest(
                patient_id=context.patient_id,
                doctor_id=doctor.id,
                scheduled_start=start,
                scheduled_end=end,
                type=AppointmentType(payload.type.value),
                reason=payload.reason,
            )
            try:
                booked, created = await self._booking.book_appointment(
                    hospital.id,
                    request,
                    idempotency_key=stored_key,
                    actor_id=None,
                    allow_override=False,
                    audit_event=_audit,
                )
            except (DoubleBookingError, IntegrityError):
                # Lost a race for the slot — the exclusion constraint decided.
                # The other booking is never described.
                self._refused("lost_race", account_id, hospital.id)
                raise ConflictError(message=SLOT_TAKEN) from None
            except (ValidationError, BusinessRuleError):
                # The staff path's own refusals (a past start, a duration that
                # is not a slot length, outside the weekly windows): to a
                # patient they are one thing.
                self._refused("not_bookable", account_id, hospital.id)
                raise BusinessRuleError(message=NOT_BOOKABLE) from None

        # The staff path returns whatever holds the key; hold it to the same
        # test as a replay found here.
        if (
            booked.patient_id != context.patient_id
            or booked.doctor_id != doctor.id
            or not payload.same_request(booked.scheduled_start, booked.scheduled_end)
        ):
            raise ConflictError(message=KEY_REUSED)
        logger.info(
            "patient_appointment_booked",
            account_id=str(account_id),
            hospital_id=str(hospital.id),
            appointment_id=str(booked.id),
            created=created,
        )
        return BookingOutcome(
            appointment=_view(
                appointment_id=booked.id,
                status=booked.status,
                appointment_type=booked.type,
                start=booked.scheduled_start,
                end=booked.scheduled_end,
                reason=booked.reason,
                hospital=hospital,
                doctor=doctor,
            ),
            created=created,
        )

    @staticmethod
    def _replay(
        appointment: Appointment,
        patient_id: uuid.UUID,
        doctor: DoctorDirectoryEntry,
        hospital: HospitalDirectoryEntry,
        payload: BookAppointment,
    ) -> BookingOutcome:
        """Answer a request whose key already holds an appointment.

        The same request gets the same appointment; anything else is refused
        without saying what the key was used for.
        """
        if (
            appointment.patient_id != patient_id
            or appointment.doctor_id != doctor.id
            or not payload.same_request(appointment.scheduled_start, appointment.scheduled_end)
        ):
            raise ConflictError(message=KEY_REUSED)
        return BookingOutcome(
            appointment=_view(
                appointment_id=appointment.id,
                status=appointment.status,
                appointment_type=appointment.type,
                start=appointment.scheduled_start,
                end=appointment.scheduled_end,
                reason=appointment.reason,
                hospital=hospital,
                doctor=doctor,
            ),
            created=False,
        )

    @staticmethod
    def _refused(outcome: str, account_id: uuid.UUID, hospital_id: uuid.UUID) -> None:
        """Log a refused booking: who, where and why — never the slot or the reason text."""
        logger.info(
            "patient_booking_refused",
            account_id=str(account_id),
            hospital_id=str(hospital_id),
            outcome=outcome,
        )


def _view(
    *,
    appointment_id: uuid.UUID,
    status: object,
    appointment_type: object,
    start: datetime,
    end: datetime,
    reason: str | None,
    hospital: HospitalDirectoryEntry,
    doctor: DoctorDirectoryEntry,
) -> PatientAppointment:
    """An appointment as its patient may see it. Every field is set here, by name."""
    zone = ZoneInfo(hospital.timezone)
    return PatientAppointment.build(
        ref=str(appointment_id),
        status=status,
        type=getattr(appointment_type, "value", appointment_type),
        start=start.astimezone(zone),
        end=end.astimezone(zone),
        timezone=hospital.timezone,
        hospital=PatientAppointmentHospital(ref=hospital.slug, name=hospital.name),
        doctor=PatientAppointmentDoctor(
            ref=str(doctor.id),
            name=" ".join(
                part for part in (doctor.first_name.strip(), doctor.last_name.strip()) if part
            ),
            specialization=doctor.specialization.strip(),
        ),
        reason=reason,
    )
