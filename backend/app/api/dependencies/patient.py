"""Dependencies for the Patient App namespace (``/api/v1/patient``).

The patient counterpart of :mod:`app.api.dependencies.auth`, and deliberately
separate from it. A patient router depends on what is defined here and on
nothing from the staff authentication module: no ``get_current_user``, no
``require_permission``. A patient is authorised by who they are and which
record is theirs, never by a role or a permission.

What each kind of patient endpoint declares:

* **Public** (requesting and verifying a code) — nothing.
* **Cookie** (refresh, logout) — :func:`require_patient_csrf`.
* **Bearer** — :func:`get_patient_account`: a valid patient access token *and*
  an account that is active in the database right now.
* **A record at a hospital** — :func:`get_patient_context`, which adds the
  server-resolved, honoured record link. No endpoint needs it yet.

Usage::

    from fastapi import APIRouter, Depends
    from app.api.dependencies.patient import get_patient_account
    from app.models.patient_account import PatientAccount

    router = APIRouter()

    @router.get("/me")
    async def me(account: PatientAccount = Depends(get_patient_account)):
        ...
"""

from __future__ import annotations

import uuid

# NOTE: these must be runtime imports (not TYPE_CHECKING). FastAPI resolves
# dependency signatures against the module's real globals.
import jwt as pyjwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.dependencies.repositories import (
    get_appointment_repository,
    get_auth_throttle_repository,
    get_doctor_repository,
    get_hospital_repository,
    get_patient_access_grant_repository,
    get_patient_account_link_repository,
    get_patient_account_repository,
    get_patient_consent_repository,
    get_patient_device_repository,
    get_patient_otp_challenge_repository,
    get_patient_refresh_token_repository,
    get_patient_repository,
)
from app.api.dependencies.services import (
    get_appointment_service,
    get_audit_sink,
    get_patient_service,
    get_unit_of_work,
)
from app.core.audit import AuditSink
from app.core.client_ip import client_ip
from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.security import verify_patient_access_token
from app.core.sms import SmsSender
from app.core.sms import get_sms_sender as _configured_sms_sender
from app.core.tenancy import TenantScope, bind_tenant_scope
from app.database.unit_of_work import UnitOfWork
from app.models.patient_account import PatientAccount
from app.repositories import (
    AppointmentRepository,
    AuthThrottleRepository,
    DoctorRepository,
    HospitalRepository,
    PatientAccessGrantRepository,
    PatientAccountLinkRepository,
    PatientAccountRepository,
    PatientConsentRepository,
    PatientDeviceRepository,
    PatientOtpChallengeRepository,
    PatientRefreshTokenRepository,
    PatientRepository,
)
from app.services.appointment_service import AppointmentService
from app.services.auth_throttle import AuthThrottle
from app.services.patient_app.access_grant_service import AccessGrantService
from app.services.patient_app.appointments_service import PatientAppointmentsService
from app.services.patient_app.availability_service import DoctorAvailabilityService
from app.services.patient_app.booking_service import PatientBookingService
from app.services.patient_app.common import ClientContext
from app.services.patient_app.consent_service import ConsentService
from app.services.patient_app.doctor_directory_service import HospitalDoctorDirectoryService
from app.services.patient_app.hospital_directory_service import HospitalDirectoryService
from app.services.patient_app.hospital_gate import PatientHospitalGate
from app.services.patient_app.otp_service import OtpService
from app.services.patient_app.patient_account_service import PatientAccountService
from app.services.patient_app.patient_auth_service import PatientAuthService
from app.services.patient_app.patient_authorization import PatientAuthorization, PatientContext
from app.services.patient_app.record_link_service import RecordLinkService
from app.services.patient_service import PatientService

__all__ = [
    "PATIENT_CSRF_HEADER",
    "get_access_grant_service",
    "get_client_context",
    "get_consent_service",
    "get_doctor_availability_service",
    "get_doctor_directory_service",
    "get_hospital_directory_service",
    "get_patient_account",
    "get_patient_account_service",
    "get_patient_appointments_service",
    "get_patient_auth_service",
    "get_patient_booking_service",
    "get_patient_authorization",
    "get_patient_context",
    "get_record_link_service",
    "get_sms_sender",
    "require_patient_csrf",
]

#: The custom header the cookie-authenticated endpoints require. A cross-site
#: form cannot set it, and a cross-site script cannot send it without a CORS
#: preflight that this API does not grant.
PATIENT_CSRF_HEADER = "X-Atheris-Patient"
_PATIENT_CSRF_VALUE = "1"

_bearer_scheme = HTTPBearer(
    bearerFormat="JWT",
    description="Enter your patient access token",
    auto_error=False,
)


def _unauthenticated() -> HTTPException:
    """The one answer for every authentication failure in the patient namespace."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "message": "Authentication required.",
            "error_code": ErrorCode.AUTHENTICATION_REQUIRED,
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── Request context ──────────────────────────────────────────────────────────


def get_client_context(request: Request) -> ClientContext:
    """Where this request came from, from the one shared address resolver."""
    return ClientContext(
        ip_address=client_ip(request), user_agent=request.headers.get("user-agent")
    )


def require_patient_csrf(request: Request) -> None:
    """Refuse a cookie-authenticated request that a patient's own app did not send.

    Refresh and logout are the only endpoints authenticated by a cookie, so
    they are the only ones a cross-site request could ride on. On top of
    ``SameSite=Strict`` they require:

    * the custom header ``X-Atheris-Patient: 1``; and
    * when the browser sent an ``Origin``, that it is one of
      ``PATIENT_APP_ORIGINS``.

    :param request: The incoming request.
    :raises HTTPException: 401, with the same answer as any other
        authentication failure, if either check fails.
    """
    if request.headers.get(PATIENT_CSRF_HEADER) != _PATIENT_CSRF_VALUE:
        raise _unauthenticated()
    origins = request.headers.getlist("origin")
    if origins:
        allowed = set(settings.PATIENT_APP_ORIGINS)
        if len(origins) != 1 or origins[0].strip().rstrip("/").lower() not in allowed:
            raise _unauthenticated()


# ── Principal ────────────────────────────────────────────────────────────────


async def get_patient_account(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    accounts: PatientAccountRepository = Depends(get_patient_account_repository),
) -> PatientAccount:
    """Resolve the bearer token to an active patient account.

    The token must be a patient access token — the patient audience and the
    patient type — so a staff token is refused here, as a patient token is
    refused by ``get_current_user``. The account is then read from the
    database on every request and must be active: suspending or closing an
    account takes effect at once, not when its token expires.

    No tenant scope is bound here. A patient account belongs to no hospital;
    the hospital of a request is resolved from the account's own record link,
    and bound, by :func:`get_patient_context`.

    :param credentials: The bearer token from the ``Authorization`` header.
    :param accounts: Patient account data access.
    :returns: The authenticated account.
    :raises HTTPException: 401 for every failure, with one message.
    """
    if credentials is None:
        raise _unauthenticated()
    try:
        payload = verify_patient_access_token(credentials.credentials)
        account_id = uuid.UUID(str(payload["sub"]))
    except (pyjwt.InvalidTokenError, KeyError, ValueError):
        raise _unauthenticated() from None

    account = await accounts.get_by_id(account_id)
    if account is None or not account.is_active:
        raise _unauthenticated()
    return account


# ── Services ─────────────────────────────────────────────────────────────────


def get_sms_sender() -> SmsSender | None:
    """Provide the configured SMS transport, or ``None`` when SMS is off.

    A dependency of its own so that tests can substitute an in-memory sender;
    they never use the development one.
    """
    return _configured_sms_sender()


def get_consent_service(
    consents: PatientConsentRepository = Depends(get_patient_consent_repository),
    links: PatientAccountLinkRepository = Depends(get_patient_account_link_repository),
    uow: UnitOfWork = Depends(get_unit_of_work),
    audit: AuditSink = Depends(get_audit_sink),
) -> ConsentService:
    """Provide a :class:`ConsentService` with the policies currently in force."""
    return ConsentService(consents, links, uow, audit)


def get_patient_hospital_gate(
    hospitals: HospitalRepository = Depends(get_hospital_repository),
) -> PatientHospitalGate:
    """Provide the gate that says which hospitals are open to patients."""
    return PatientHospitalGate(hospitals)


def get_patient_authorization(
    accounts: PatientAccountRepository = Depends(get_patient_account_repository),
    links: PatientAccountLinkRepository = Depends(get_patient_account_link_repository),
    gate: PatientHospitalGate = Depends(get_patient_hospital_gate),
    patients: PatientService = Depends(get_patient_service),
    consent: ConsentService = Depends(get_consent_service),
) -> PatientAuthorization:
    """Provide the one entry point that resolves a patient's standing at a hospital."""
    return PatientAuthorization(accounts, links, gate, patients, consent)


def get_patient_auth_service(
    accounts: PatientAccountRepository = Depends(get_patient_account_repository),
    refresh_tokens: PatientRefreshTokenRepository = Depends(get_patient_refresh_token_repository),
    devices: PatientDeviceRepository = Depends(get_patient_device_repository),
    challenges: PatientOtpChallengeRepository = Depends(get_patient_otp_challenge_repository),
    consent: ConsentService = Depends(get_consent_service),
    throttle_buckets: AuthThrottleRepository = Depends(get_auth_throttle_repository),
    uow: UnitOfWork = Depends(get_unit_of_work),
    audit: AuditSink = Depends(get_audit_sink),
    sms: SmsSender | None = Depends(get_sms_sender),
) -> PatientAuthService:
    """Provide a :class:`PatientAuthService`, always behind the real throttle."""
    return PatientAuthService(
        accounts,
        refresh_tokens,
        devices,
        OtpService(challenges),
        consent,
        throttle=AuthThrottle(throttle_buckets, uow),
        uow=uow,
        audit=audit,
        sms=sms,
    )


def get_patient_account_service(
    authorization: PatientAuthorization = Depends(get_patient_authorization),
    consent: ConsentService = Depends(get_consent_service),
) -> PatientAccountService:
    """Provide a :class:`PatientAccountService`."""
    return PatientAccountService(authorization, consent)


def get_hospital_directory_service(
    gate: PatientHospitalGate = Depends(get_patient_hospital_gate),
    authorization: PatientAuthorization = Depends(get_patient_authorization),
    consent: ConsentService = Depends(get_consent_service),
) -> HospitalDirectoryService:
    """Provide a :class:`HospitalDirectoryService`."""
    return HospitalDirectoryService(gate, authorization, consent)


def get_doctor_availability_service(
    gate: PatientHospitalGate = Depends(get_patient_hospital_gate),
    doctors: DoctorRepository = Depends(get_doctor_repository),
    appointments: AppointmentRepository = Depends(get_appointment_repository),
    consent: ConsentService = Depends(get_consent_service),
) -> DoctorAvailabilityService:
    """Provide a :class:`DoctorAvailabilityService`."""
    return DoctorAvailabilityService(gate, doctors, appointments, consent)


def get_doctor_directory_service(
    gate: PatientHospitalGate = Depends(get_patient_hospital_gate),
    doctors: DoctorRepository = Depends(get_doctor_repository),
    consent: ConsentService = Depends(get_consent_service),
) -> HospitalDoctorDirectoryService:
    """Provide a :class:`HospitalDoctorDirectoryService`."""
    return HospitalDoctorDirectoryService(gate, doctors, consent)


def get_patient_booking_service(
    gate: PatientHospitalGate = Depends(get_patient_hospital_gate),
    authorization: PatientAuthorization = Depends(get_patient_authorization),
    doctors: DoctorRepository = Depends(get_doctor_repository),
    appointments: AppointmentRepository = Depends(get_appointment_repository),
    links: PatientAccountLinkRepository = Depends(get_patient_account_link_repository),
    availability: DoctorAvailabilityService = Depends(get_doctor_availability_service),
    booking: AppointmentService = Depends(get_appointment_service),
) -> PatientBookingService:
    """Provide a :class:`PatientBookingService` over the hospital's own booking service."""
    return PatientBookingService(
        gate, authorization, doctors, appointments, links, availability, booking
    )


def get_patient_appointments_service(
    authorization: PatientAuthorization = Depends(get_patient_authorization),
    gate: PatientHospitalGate = Depends(get_patient_hospital_gate),
    appointments: AppointmentRepository = Depends(get_appointment_repository),
    lifecycle: AppointmentService = Depends(get_appointment_service),
) -> PatientAppointmentsService:
    """Provide a :class:`PatientAppointmentsService` over the hospital's appointment lifecycle."""
    return PatientAppointmentsService(authorization, gate, appointments, lifecycle)


def get_record_link_service(
    gate: PatientHospitalGate = Depends(get_patient_hospital_gate),
    links: PatientAccountLinkRepository = Depends(get_patient_account_link_repository),
    patient_records: PatientRepository = Depends(get_patient_repository),
    patients: PatientService = Depends(get_patient_service),
    consent: ConsentService = Depends(get_consent_service),
    throttle_buckets: AuthThrottleRepository = Depends(get_auth_throttle_repository),
    uow: UnitOfWork = Depends(get_unit_of_work),
    audit: AuditSink = Depends(get_audit_sink),
) -> RecordLinkService:
    """Provide a :class:`RecordLinkService`, always behind the real throttle."""
    return RecordLinkService(
        gate,
        links,
        patient_records,
        patients,
        consent,
        throttle=AuthThrottle(throttle_buckets, uow),
        uow=uow,
        audit=audit,
    )


def get_access_grant_service(
    grants: PatientAccessGrantRepository = Depends(get_patient_access_grant_repository),
    authorization: PatientAuthorization = Depends(get_patient_authorization),
    hospitals: HospitalRepository = Depends(get_hospital_repository),
    doctors: DoctorRepository = Depends(get_doctor_repository),
    uow: UnitOfWork = Depends(get_unit_of_work),
    audit: AuditSink = Depends(get_audit_sink),
) -> AccessGrantService:
    """Provide an :class:`AccessGrantService`. No endpoint uses it yet."""
    return AccessGrantService(grants, authorization, hospitals, doctors, uow=uow, audit=audit)


async def get_patient_context(
    hospital_id: uuid.UUID,
    account: PatientAccount = Depends(get_patient_account),
    authorization: PatientAuthorization = Depends(get_patient_authorization),
) -> PatientContext:
    """Resolve the authenticated account to a patient context at the hospital in the path.

    For record endpoints (none exists yet). The ``patient_id`` in the result
    comes from the account's own link — never from the request — and every
    refusal short of a missing consent is the same ``404``.

    Before it returns, the request is confined to the hospital of the
    account's own link (``app/core/tenancy.py``: a patient principal runs
    under ``TenantScope.hospital(link.hospital_id)``). From then on the
    session's tenant guard filters every read to that hospital and refuses a
    flush that would touch another's row — so a record endpoint built on this
    dependency cannot reach across hospitals even by mistake. Nothing is
    bound when resolution fails: the request ends there.

    :param hospital_id: The hospital in the path.
    :param account: The authenticated account.
    :param authorization: Resolves the account's standing there.
    :returns: The context to call record services with.
    """
    context = await authorization.resolve_context(account, hospital_id)
    bind_tenant_scope(TenantScope.hospital(context.hospital_id))
    return context
