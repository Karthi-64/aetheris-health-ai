"""A patient's own appointments: the list, one of them, and cancelling one.

``docs/modules/15-patient-app.md`` §9.1, §9.2 and §14.

**Whose.** The authenticated account's, and nobody else's. The account's
record at each hospital comes from its own honoured links
(:meth:`PatientAuthorization.own_contexts`); an appointment is the caller's
only when both its hospital and its patient record are one of those pairs. No
request names a patient, and an appointment that is somebody else's is the
same ``404`` as one that does not exist.

**One lifecycle.** Nothing here changes a status. Cancelling calls
:meth:`AppointmentService.cancel` — the hospital's own transition, which locks
the appointment's row, consults the same state machine, and writes the same
status-history row — with a precondition that narrows it to what a patient
may do: their own appointment, only while it is ``booked``, only before the
hospital's cut-off. The precondition runs on the locked row, so a patient
cancelling while reception checks them in cannot both succeed.

**Repeating is safe.** Cancelling an appointment that is already cancelled —
a retry after a lost answer — changes nothing and answers with the
appointment as it is.

Reading is not audited; a cancellation is, once, as the patient's act.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

import structlog

from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.core.tenancy import TenantScope, cross_tenant, tenant_scope
from app.models.appointment import AppointmentStatus
from app.schemas.appointment import CancelAppointmentRequest
from app.schemas.common import Page
from app.schemas.patient_app.appointments import (
    PatientAppointmentDetail,
    PatientAppointmentDoctor,
    PatientAppointmentHospital,
)
from app.services.appointment_service import AppointmentNotFoundError, InvalidTransitionError
from app.services.patient_app.booking_policy import BookingPolicy
from app.services.patient_app.common import patient_event
from app.utils.datetime import utc_now

if TYPE_CHECKING:
    from app.core.audit import AuditEvent
    from app.models.appointment import Appointment
    from app.models.patient_account import PatientAccount
    from app.repositories.appointment_repository import AppointmentRepository
    from app.repositories.hospital_repository import HospitalDirectoryEntry
    from app.schemas.patient_app.appointments import CancelAppointment
    from app.services.appointment_service import AppointmentService
    from app.services.patient_app.common import ClientContext
    from app.services.patient_app.hospital_gate import PatientHospitalGate
    from app.services.patient_app.patient_authorization import PatientAuthorization

__all__ = ["PatientAppointmentsService"]

logger = structlog.get_logger(__name__)

_NOT_FOUND: Final = HTTPStatus.NOT_FOUND.phrase
#: The fixed messages of a refused cancellation.
NO_LONGER_CANCELLABLE: Final = "This appointment can no longer be cancelled."
TOO_LATE: Final = (
    "It is too late to cancel this appointment in the app. Please contact the hospital."
)

_UUID_TEXT: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_OWN_APPOINTMENTS: Final = cross_tenant(
    "patient account: its own appointments, by its own honoured record links"
)


class _AlreadyCancelledError(Exception):
    """The locked appointment is already cancelled: there is nothing to do."""


class PatientAppointmentsService:
    """Lists, shows and cancels the authenticated patient's own appointments.

    :param authorization: Resolves the account's own records.
    :param gate: Describes the hospitals those records are at.
    :param appointments: Appointment data access — reads.
    :param lifecycle: The hospital's appointment service, which owns every
        status change.
    """

    def __init__(
        self,
        authorization: PatientAuthorization,
        gate: PatientHospitalGate,
        appointments: AppointmentRepository,
        lifecycle: AppointmentService,
    ) -> None:
        self._authorization = authorization
        self._gate = gate
        self._appointments = appointments
        self._lifecycle = lifecycle

    async def list_appointments(
        self, account: PatientAccount, *, upcoming: bool, page: int, page_size: int
    ) -> Page[PatientAppointmentDetail]:
        """One page of the caller's upcoming, or past, appointments.

        :param account: The authenticated, active account.
        :param upcoming: The upcoming list (soonest first) or the past one
            (most recent first).
        :param page: 1-based page number.
        :param page_size: Appointments per page.
        :returns: The page and the total.
        :raises ConsentRequiredError: If a required policy is pending.
        """
        hospitals = await self._own_hospitals(account)
        now = utc_now()
        rows, total = await self._appointments.list_for_patient_records(
            [(hospital.id, patient_id) for hospital, patient_id in hospitals.values()],
            _OWN_APPOINTMENTS,
            upcoming=upcoming,
            now=now,
            skip=(page - 1) * page_size,
            limit=page_size,
        )
        return Page[PatientAppointmentDetail](
            items=[_view(row, hospitals[row.hospital_id][0], now) for row in rows],
            page=page,
            page_size=page_size,
            total_records=total,
        )

    async def get_appointment(
        self, account: PatientAccount, appointment_ref: str
    ) -> PatientAppointmentDetail:
        """One of the caller's own appointments.

        :param account: The authenticated, active account.
        :param appointment_ref: The appointment's reference, from the path.
        :returns: The appointment.
        :raises ConsentRequiredError: If a required policy is pending.
        :raises NotFoundError: If it is not the caller's, whatever the reason.
        """
        hospitals = await self._own_hospitals(account)
        appointment = await self._own_appointment(hospitals, appointment_ref)
        return _view(appointment, hospitals[appointment.hospital_id][0], utc_now())

    async def cancel_appointment(
        self,
        account: PatientAccount,
        appointment_ref: str,
        payload: CancelAppointment,
        *,
        client: ClientContext | None = None,
    ) -> PatientAppointmentDetail:
        """Cancel one of the caller's own booked appointments.

        :param account: The authenticated, active account.
        :param appointment_ref: The appointment's reference, from the path.
        :param payload: The reason, from the fixed list, and optional text.
        :param client: Where the request came from, for the audit record.
        :returns: The appointment as it now is.
        :raises ConsentRequiredError: If a required policy is pending.
        :raises NotFoundError: If it is not the caller's.
        :raises ConflictError: If it is no longer ``booked``.
        :raises BusinessRuleError: If the hospital's cut-off has passed.
        """
        account_id = account.id
        hospitals = await self._own_hospitals(account)
        found = await self._own_appointment(hospitals, appointment_ref)
        appointment_id, hospital_id = found.id, found.hospital_id
        hospital, patient_id = hospitals[hospital_id]
        cutoff = timedelta(
            minutes=BookingPolicy.from_settings(hospital.settings).cancel_cutoff_minutes
        )

        def _may_cancel(locked: Appointment) -> None:
            """Decide on the locked row: what was read a moment ago proves nothing."""
            if locked.patient_id != patient_id:
                raise NotFoundError(_NOT_FOUND)
            if locked.status is AppointmentStatus.CANCELLED:
                raise _AlreadyCancelledError
            if locked.status is not AppointmentStatus.BOOKED:
                raise ConflictError(message=NO_LONGER_CANCELLABLE)
            if utc_now() > locked.scheduled_start - cutoff:
                raise BusinessRuleError(message=TOO_LATE)

        def _audit(cancelled: Appointment, left: AppointmentStatus) -> AuditEvent:
            return patient_event(
                "patient.appointment.cancelled",
                target_type="appointment",
                account_id=account_id,
                hospital_id=hospital_id,
                target_id=cancelled.id,
                context={"reason_code": payload.reason_code.value, "from_status": left.value},
                client=client,
            )

        stored_reason = f"Cancelled by the patient in the app ({payload.reason_code.value})"
        if payload.reason_text:
            stored_reason = f"{stored_reason}: {payload.reason_text}"
        outcome = "cancelled"
        with tenant_scope(TenantScope.hospital(hospital_id)):
            try:
                await self._lifecycle.cancel(
                    hospital_id,
                    appointment_id,
                    CancelAppointmentRequest(reason=stored_reason),
                    actor_id=None,
                    precondition=_may_cancel,
                    audit_event=_audit,
                )
            except _AlreadyCancelledError:
                outcome = "already_cancelled"
            except AppointmentNotFoundError:
                raise NotFoundError(_NOT_FOUND) from None
            except InvalidTransitionError:
                # The state machine's own refusal; to a patient it is the same thing.
                raise ConflictError(message=NO_LONGER_CANCELLABLE) from None

        logger.info(
            "patient_appointment_cancel",
            account_id=str(account_id),
            hospital_id=str(hospital_id),
            appointment_id=str(appointment_id),
            outcome=outcome,
        )
        current = await self._appointments.get_for_patient_records(
            [(hospital_id, patient_id)], hospital_id, appointment_id
        )
        if current is None:
            raise NotFoundError(_NOT_FOUND)
        return _view(current, hospital, utc_now())

    async def _own_hospitals(
        self, account: PatientAccount
    ) -> dict[uuid.UUID, tuple[HospitalDirectoryEntry, uuid.UUID]]:
        """The hospitals where the account's record is honoured, with that record's id."""
        hospitals: dict[uuid.UUID, tuple[HospitalDirectoryEntry, uuid.UUID]] = {}
        for context in await self._authorization.own_contexts(account):
            hospital = await self._gate.describe(str(context.hospital_id))
            if hospital is not None:
                hospitals[hospital.id] = (hospital, context.patient_id)
        return hospitals

    async def _own_appointment(
        self,
        hospitals: dict[uuid.UUID, tuple[HospitalDirectoryEntry, uuid.UUID]],
        appointment_ref: str,
    ) -> Appointment:
        """The appointment a reference names, if it is the caller's own."""
        reference = appointment_ref.strip().lower()
        if not _UUID_TEXT.fullmatch(reference):
            raise NotFoundError(_NOT_FOUND)
        appointment = await self._appointments.get_for_patient_records(
            [(hospital.id, patient_id) for hospital, patient_id in hospitals.values()],
            _OWN_APPOINTMENTS,
            uuid.UUID(reference),
        )
        if appointment is None:
            raise NotFoundError(_NOT_FOUND)
        return appointment


def _view(
    appointment: Appointment, hospital: HospitalDirectoryEntry, now: datetime
) -> PatientAppointmentDetail:
    """An appointment as its patient may see it. Every field is set here, by name."""
    zone = ZoneInfo(hospital.timezone)
    doctor = appointment.doctor
    cutoff = timedelta(minutes=BookingPolicy.from_settings(hospital.settings).cancel_cutoff_minutes)
    booked = appointment.status is AppointmentStatus.BOOKED
    cancel_until = appointment.scheduled_start - cutoff
    return PatientAppointmentDetail(
        ref=str(appointment.id),
        status=appointment.status,
        type=appointment.type.value,
        start=appointment.scheduled_start.astimezone(zone),
        end=appointment.scheduled_end.astimezone(zone),
        timezone=hospital.timezone,
        hospital=PatientAppointmentHospital(ref=hospital.slug, name=hospital.name),
        doctor=PatientAppointmentDoctor(
            ref=str(doctor.id),
            name=" ".join(
                part
                for part in (doctor.user.first_name.strip(), doctor.user.last_name.strip())
                if part
            ),
            specialization=doctor.specialization.strip(),
        ),
        reason=appointment.reason,
        can_cancel=booked and now <= cancel_until,
        cancel_until=cancel_until.astimezone(zone) if booked else None,
    )
