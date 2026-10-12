"""Linking a patient account to a hospital record, and self-registration.

Implements the outcome table of ``docs/modules/15-patient-app.md`` §4.5.

**Principle.** A verified phone number is necessary and never sufficient. An
account is linked to a record only when the verified phone **and** the date of
birth identify exactly one active record of the hospital — with the MRN as a
tie-breaker only when they do not. The API never returns, counts or describes
candidate records.

**What comes from where.**

* The phone is the one the account proved at sign-in. It is never read from a
  request.
* The hospital is the one in the path, resolved to an active hospital with the
  Patient App switched on. Anything else is the same ``404`` as "no record".
  A request that would be refused whatever the hospital — a stale consent
  version, an invalid registration, a pending policy — is refused before the
  hospital is looked at, so those answers say nothing about it either.
* The ``patient_id`` is found by the server, inside that one hospital, with
  the session confined to it. It is never accepted from a client.

**Two answers that look alike, on purpose.** "No record on this phone" and
"a record on this phone, but another date of birth" are one ``404`` with one
message: a phone is often shared within a family, and telling them apart would
say that somebody else's record is on that number.

**Concurrency.** Attempts by one account at one hospital are serialised by an
advisory lock, and the two cardinality rules — one active link per record, one
active ``self`` link per account and hospital — are partial unique indexes, so
they hold whatever interleaving happens.

**Registration and the patient service.** A record is created by the existing
:meth:`~app.services.patient_service.PatientService.register_patient`, which
is not modified and commits its own transaction. The registration consent is
written in that same transaction, so "this account has registered here" is
durable exactly when the record is. The link is made in the transaction that
follows; should that one fail, the record is on the account's phone with the
date of birth the patient confirmed, and linking finds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.tenancy import TenantScope, tenant_scope
from app.models.patient_account import LinkVerification
from app.models.patient_consent import ConsentPurpose
from app.schemas.patient import CreatePatientRequest
from app.schemas.patient_app.account import (
    PatientLink,
    PatientProfile,
    RegisterRecordRequest,
    RegisterRecordResponse,
)
from app.services.auth_throttle import BucketKind, bucket
from app.services.patient_app.common import ClientContext, patient_event
from app.services.patient_app.errors import LinkMrnRequiredError, LinkUnavailableError
from app.services.patient_app.hospital_gate import PatientHospitalGate

if TYPE_CHECKING:
    import uuid
    from datetime import date

    from app.core.audit import AuditSink
    from app.database.unit_of_work import UnitOfWork
    from app.models.patient import Patient
    from app.models.patient_account import PatientAccount, PatientAccountLink
    from app.repositories.hospital_repository import HospitalSummary
    from app.repositories.patient_account_link_repository import PatientAccountLinkRepository
    from app.repositories.patient_repository import PatientRepository
    from app.services.auth_throttle import Admission, AuthThrottle
    from app.services.patient_app.consent_service import ConsentService
    from app.services.patient_service import PatientService

__all__ = ["LinkOutcome", "RecordLinkService"]

logger = structlog.get_logger(__name__)

#: The one message for every "no usable match", whatever the cause.
_NO_MATCH: Final = "We could not find a record with these details."
_ALREADY_LINKED: Final = "You are already linked to a record at this hospital."
_RECORD_EXISTS: Final = (
    "A record may already exist for you at this hospital. Please link to it instead."
)
_ALREADY_REGISTERED: Final = "You have already registered at this hospital."

#: Key part of the allowance for attempts at a hospital that cannot be used.
#: A hospital id never spells this, so it cannot collide with a real one.
_UNAVAILABLE: Final = "unavailable"

#: Why a link was ended to make way for another.
_ENDED_SUPERSEDED: Final = "superseded"

#: Consents the two actions need. Registration needs both: it creates a record
#: and links it.
_LINK_CONSENTS: Final = (ConsentPurpose.HOSPITAL_RECORD_LINK,)
_REGISTER_CONSENTS: Final = (
    ConsentPurpose.HOSPITAL_REGISTRATION,
    ConsentPurpose.HOSPITAL_RECORD_LINK,
)


@dataclass(frozen=True, slots=True)
class LinkOutcome:
    """A link, and whether this request made it.

    :param link: The link as the app may show it.
    :param created: ``False`` when the account was already linked to that record.
    """

    link: PatientLink
    created: bool


@dataclass(frozen=True, slots=True)
class _Match:
    """What the phone and the date of birth identify inside one hospital."""

    #: Records on the account's phone, of any date of birth and any state.
    on_phone: int
    #: Active records on the phone with the entered date of birth.
    active: list[Patient]
    #: Whether a deactivated record on the phone has that date of birth.
    inactive: bool


class RecordLinkService:
    """Links accounts to records, and registers new records.

    :param gate: Which hospitals are open to patients.
    :param links: Record links.
    :param patient_records: Patient records, for the match inside one hospital.
    :param patients: The existing patient service, which registers a record.
    :param consent: Records the consents each action requires.
    :param throttle: Bounds attempts per account and hospital.
    :param uow: The request's unit of work.
    :param audit: Where every attempt and its outcome are recorded.
    """

    def __init__(
        self,
        gate: PatientHospitalGate,
        links: PatientAccountLinkRepository,
        patient_records: PatientRepository,
        patients: PatientService,
        consent: ConsentService,
        *,
        throttle: AuthThrottle,
        uow: UnitOfWork,
        audit: AuditSink,
    ) -> None:
        self._gate = gate
        self._links = links
        self._patient_records = patient_records
        self._patients = patients
        self._consent = consent
        self._throttle = throttle
        self._uow = uow
        self._audit = audit

    # ── Link ─────────────────────────────────────────────────────────────────

    async def link(
        self,
        account: PatientAccount,
        hospital_ref: str,
        *,
        date_of_birth: date,
        mrn: str | None,
        consent_policy_version: str,
        client: ClientContext,
    ) -> LinkOutcome:
        """Link the account to its existing record at a hospital.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's id or code, from the path.
        :param date_of_birth: The date of birth the patient entered.
        :param mrn: The MRN the patient entered, if they were asked for one.
        :param consent_policy_version: The record-link consent version shown.
        :param client: Where the request came from.
        :returns: The link, and whether it is new.
        :raises NotFoundError: No usable match — also the wrong date of birth,
            the wrong MRN, and a hospital that cannot be used.
        :raises LinkMrnRequiredError: More than one record matches.
        :raises LinkUnavailableError: The match is deactivated, or the attempt
            limit was reached.
        :raises ConflictError: The account is already linked to another record
            here, or the consent version is not current.
        """
        # Read once: later commits expire the account object.
        account_id, phone = account.id, account.phone
        # Everything that can be refused without knowing the hospital is
        # refused before the hospital is looked at, so none of those answers
        # can tell a hospital that is open to patients from one that is not.
        self._consent.require_current_version(_LINK_CONSENTS, consent_policy_version)
        await self._consent.ensure_policies_accepted(account_id)
        hospital = await self._gate.resolve(hospital_ref)
        if hospital is None:
            await self._refuse_unavailable("link", account_id, client, mrn)
            raise NotFoundError(_NO_MATCH)
        admission = await self._admit(account_id, hospital.id, "link", client)

        with tenant_scope(TenantScope.hospital(hospital.id)):
            await self._links.lock_account_in_hospital(hospital.id, account_id)
            match = await self._match(hospital.id, phone, date_of_birth)

            if not match.active:
                outcome = "inactive_record" if match.inactive else "no_match"
                await self._refuse("link", outcome, account_id, hospital.id, client, mrn)
                if match.inactive:
                    raise LinkUnavailableError
                raise NotFoundError(_NO_MATCH)

            if len(match.active) == 1:
                target = match.active[0]
            elif mrn is None:
                await self._refuse("link", "mrn_required", account_id, hospital.id, client, mrn)
                raise LinkMrnRequiredError
            else:
                wanted = mrn.casefold()
                chosen = [p for p in match.active if p.mrn.casefold() == wanted]
                if len(chosen) != 1:
                    await self._refuse("link", "no_match", account_id, hospital.id, client, mrn)
                    raise NotFoundError(_NO_MATCH)
                target = chosen[0]

            verified_via = (
                LinkVerification.PHONE_DOB
                if len(match.active) == 1
                else LinkVerification.PHONE_DOB_MRN
            )
            try:
                link, created = await self._establish(
                    account_id,
                    phone,
                    hospital.id,
                    target.id,
                    verified_via=verified_via,
                    consent_policy_version=consent_policy_version,
                    client=client,
                )
            except ConflictError:
                await self._refuse("link", "conflict", account_id, hospital.id, client, mrn)
                raise
            except NotFoundError:
                # The record stopped matching between the match and the link.
                # Whatever this request had begun to write is undone first.
                await self._uow.rollback()
                await self._refuse("link", "no_match", account_id, hospital.id, client, mrn)
                raise

            view = self._view(link, hospital)
            await self._audit.record(
                patient_event(
                    "patient.link.attempted",
                    target_type="patient_account_link",
                    target_id=link.id,
                    account_id=account_id,
                    hospital_id=hospital.id,
                    context={
                        "operation": "link",
                        "outcome": "linked" if created else "already_linked",
                        "mrn_supplied": mrn is not None,
                    },
                    client=client,
                )
            )
            await self._uow.commit()

        await self._throttle.settle(admission)
        return LinkOutcome(link=view, created=created)

    # ── Register ─────────────────────────────────────────────────────────────

    async def register(
        self,
        account: PatientAccount,
        hospital_ref: str,
        payload: RegisterRecordRequest,
        *,
        client: ClientContext,
    ) -> RegisterRecordResponse:
        """Register a new record for the account at a hospital, and link it.

        Allowed only when the match, run again here, finds no record for the
        account's phone and the submitted date of birth — and only once per
        account and hospital.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's id or code, from the path.
        :param payload: Name, date of birth, gender and the consent version.
        :param client: Where the request came from.
        :returns: The new link and the new record's profile.
        :raises NotFoundError: The hospital cannot be used.
        :raises ConflictError: A matching record exists (link instead), the
            account is already linked or has already registered here, or the
            consent version is not current.
        :raises LinkUnavailableError: The matching record is deactivated, or
            the attempt limit was reached.
        :raises ValidationError: The details fail the patient rules.
        """
        account_id, phone = account.id, account.phone
        # As for linking: the payload, the consent version and the policy
        # gate answer the same whatever the hospital, so they come first.
        record = self._new_record(payload, phone)
        self._consent.require_current_version(_REGISTER_CONSENTS, payload.consent_policy_version)
        await self._consent.ensure_policies_accepted(account_id)
        hospital = await self._gate.resolve(hospital_ref)
        if hospital is None:
            await self._refuse_unavailable("register", account_id, client)
            raise NotFoundError(_NO_MATCH)
        admission = await self._admit(account_id, hospital.id, "register", client)

        with tenant_scope(TenantScope.hospital(hospital.id)):
            await self._links.lock_account_in_hospital(hospital.id, account_id)

            if await self._consent.has_ever_consented(
                account_id, ConsentPurpose.HOSPITAL_REGISTRATION, hospital_id=hospital.id
            ):
                await self._refuse(
                    "register", "already_registered", account_id, hospital.id, client
                )
                raise ConflictError(_ALREADY_REGISTERED)

            existing = await self._links.get_active_self(hospital.id, account_id)
            if existing is not None and await self._is_bound(existing, phone):
                await self._refuse("register", "already_linked", account_id, hospital.id, client)
                raise ConflictError(_ALREADY_LINKED)

            match = await self._match(hospital.id, phone, payload.date_of_birth)
            if match.active:
                await self._refuse("register", "record_exists", account_id, hospital.id, client)
                raise ConflictError(_RECORD_EXISTS)
            if match.inactive:
                await self._refuse("register", "inactive_record", account_id, hospital.id, client)
                raise LinkUnavailableError

            # The registration consent and the record commit together, inside
            # ``register_patient``'s own transaction: from here on this
            # account has registered at this hospital, and cannot again.
            await self._consent.record(
                account_id,
                ConsentPurpose.HOSPITAL_REGISTRATION,
                hospital_id=hospital.id,
                policy_version=payload.consent_policy_version,
                client=client,
            )
            patient = await self._patients.register_patient(hospital.id, record, actor_id=None)

            # A new transaction: take the lock again and link the new record.
            await self._links.lock_account_in_hospital(hospital.id, account_id)
            try:
                link, _created = await self._establish(
                    account_id,
                    phone,
                    hospital.id,
                    patient.id,
                    verified_via=LinkVerification.SELF_REGISTRATION,
                    consent_policy_version=payload.consent_policy_version,
                    client=client,
                )
            except ConflictError:
                await self._uow.rollback()
                logger.error(
                    "patient_registration_not_linked",
                    account_id=str(account_id),
                    hospital_id=str(hospital.id),
                    patient_id=str(patient.id),
                )
                raise
            view = self._view(link, hospital)
            await self._audit.record(
                patient_event(
                    "patient.record.registered",
                    target_type="patient",
                    target_id=patient.id,
                    account_id=account_id,
                    hospital_id=hospital.id,
                    # For staff reviewing possible duplicates: were there
                    # already records on this phone (under another date of
                    # birth)? A count, never which.
                    context={"other_records_on_phone": match.on_phone},
                    client=client,
                )
            )
            await self._uow.commit()

        await self._throttle.settle(admission)
        return RegisterRecordResponse(
            link=view,
            profile=PatientProfile(
                mrn=patient.mrn,
                first_name=patient.first_name,
                last_name=patient.last_name,
                date_of_birth=patient.date_of_birth,
                gender=patient.gender,
            ),
        )

    # ── Internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _new_record(payload: RegisterRecordRequest, phone: str) -> CreatePatientRequest:
        """Build what the patient service registers, before anything is written.

        The phone is the account's verified one, and no other: the client
        cannot supply one. Everything else a staff registration could set is
        left unset.

        :raises ValidationError: If the details fail the patient rules.
        """
        try:
            return CreatePatientRequest(
                first_name=payload.first_name,
                last_name=payload.last_name,
                date_of_birth=payload.date_of_birth,
                gender=payload.gender,
                phone=phone,
            )
        except PydanticValidationError as exc:
            raise ValidationError(
                message="Patient data failed validation.",
                detail={
                    "errors": [
                        {
                            "field": ".".join(str(part) for part in error["loc"]),
                            "message": "Invalid value.",
                        }
                        for error in exc.errors()
                    ]
                },
            ) from None

    async def _admit(
        self, account_id: uuid.UUID, hospital_id: uuid.UUID, operation: str, client: ClientContext
    ) -> Admission:
        """Charge an attempt to the account's allowances at this hospital, or refuse it.

        Two allowances, both of which must have room: five an hour and ten a
        day (§4.5). The charge is given back only when the attempt ends in a
        link.

        :raises LinkUnavailableError: If either allowance is used up.
        """
        admission = await self._throttle.admit(
            [
                bucket(BucketKind.PT_LINK_ATTEMPT, account_id, hospital_id),
                bucket(BucketKind.PT_LINK_DAILY, account_id, hospital_id),
            ]
        )
        if not admission.admitted:
            await self._refuse(operation, "throttled", account_id, hospital_id, client)
            raise LinkUnavailableError
        return admission

    async def _refuse_unavailable(
        self, operation: str, account_id: uuid.UUID, client: ClientContext, mrn: str | None = None
    ) -> None:
        """Record an attempt at a hospital that cannot be used — within a budget.

        There is no hospital to count the attempt against, so it is charged to
        an allowance of the account alone, **before** anything is written. An
        account that has used it up gets the same answer and leaves no audit
        row: otherwise looping over invented hospital references would be an
        unbounded way to write to the audit trail. The charge is never given
        back. The caller raises the same ``404`` either way.
        """
        admission = await self._throttle.admit(
            [bucket(BucketKind.PT_LINK_ATTEMPT, account_id, _UNAVAILABLE)]
        )
        if not admission.admitted:
            logger.info(
                "patient_link_refused",
                account_id=str(account_id),
                hospital_id=None,
                operation=operation,
                outcome="hospital_unavailable",
                audited=False,
            )
            return
        await self._refuse(operation, "hospital_unavailable", account_id, None, client, mrn)

    async def _match(self, hospital_id: uuid.UUID, phone: str, date_of_birth: date) -> _Match:
        """Run the match of §4.5 inside one hospital.

        One query, on the phone alone, whatever the date of birth turns out to
        be — so "no record on this phone" and "wrong date of birth" do the
        same work.
        """
        on_phone = await self._patient_records.list_by_phone(
            hospital_id, phone, include_deleted=True
        )
        same_birth = [patient for patient in on_phone if patient.date_of_birth == date_of_birth]
        return _Match(
            on_phone=len(on_phone),
            active=[patient for patient in same_birth if patient.deleted_at is None],
            inactive=any(patient.deleted_at is not None for patient in same_birth),
        )

    async def _is_bound(self, link: PatientAccountLink, phone: str) -> bool:
        """The phone-binding rule for a link of this account: is it honoured right now?"""
        patient = await self._patient_records.get_patient_by_id(
            link.hospital_id, link.patient_id, include_deleted=True
        )
        return patient is not None and patient.deleted_at is None and patient.phone == phone

    async def _still_matches(
        self, hospital_id: uuid.UUID, patient_id: uuid.UUID, phone: str
    ) -> bool:
        """Lock a matched record and check it is still active and on this phone."""
        current = await self._patient_records.lock_phone_for_link(hospital_id, patient_id)
        if current is None:
            return False
        record_phone, is_active = current
        return is_active and record_phone == phone

    async def _establish(
        self,
        account_id: uuid.UUID,
        phone: str,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        *,
        verified_via: LinkVerification,
        consent_policy_version: str,
        client: ClientContext,
    ) -> tuple[PatientAccountLink, bool]:
        """Make the account's link to a matched record, or find it already made.

        Handles the three "already linked" rows of §4.5. Does not commit.

        :returns: The link, and whether this call created it.
        :raises ConflictError: The account holds a link, still honoured, to a
            different record at this hospital.
        :raises NotFoundError: Another account holds the record and the
            record is no longer on this account's phone.
        """
        now = datetime.now(UTC)
        own = await self._links.get_active_self(hospital_id, account_id)
        if own is not None and own.patient_id == patient_id:
            # Already linked — same account. Idempotent; the consent is
            # recorded again only if its version has moved on.
            await self._consent.record(
                account_id,
                ConsentPurpose.HOSPITAL_RECORD_LINK,
                hospital_id=hospital_id,
                policy_version=consent_policy_version,
                client=client,
            )
            return own, False

        if own is not None:
            if await self._is_bound(own, phone):
                # Already linked — this account, another record. V1 allows
                # one ``self`` link per hospital.
                raise ConflictError(_ALREADY_LINKED)
            # The account's own link is no longer honoured (the hospital moved
            # that record to another phone). It gives way to the new one.
            await self._end(own, account_id, hospital_id, now, client)

        other = await self._links.get_active_for_patient(hospital_id, patient_id)
        if other is not None:
            # Already linked — another account. That link gives way only if
            # it is genuinely stale, and the match this request ran earlier
            # does not prove that: the hospital may have moved the record to
            # the other account's number since, and that account linked it.
            # So the record is read again, as committed now and under a row
            # lock that holds its phone still until this transaction ends.
            # Stale means the record is active and on *this* account's phone;
            # phones are unique per account, so it is then not on the other's
            # and the phone-binding rule has already suspended that link.
            # Anything else is the answer for "no record matches".
            if not await self._still_matches(hospital_id, patient_id, phone):
                raise NotFoundError(_NO_MATCH)
            await self._end(other, account_id, hospital_id, now, client)

        try:
            link = await self._links.create_link(
                hospital_id,
                account_id=account_id,
                patient_id=patient_id,
                verified_via=verified_via.value,
                now=now,
            )
        except IntegrityError:
            # A partial unique index refused it: something else linked this
            # record, or this account, in the meantime.
            await self._uow.rollback()
            raise ConflictError(_ALREADY_LINKED) from None

        await self._consent.record(
            account_id,
            ConsentPurpose.HOSPITAL_RECORD_LINK,
            hospital_id=hospital_id,
            policy_version=consent_policy_version,
            client=client,
        )
        await self._audit.record(
            patient_event(
                "patient.link.created",
                target_type="patient_account_link",
                target_id=link.id,
                account_id=account_id,
                hospital_id=hospital_id,
                context={"verified_via": verified_via.value},
                client=client,
            )
        )
        return link, True

    async def _end(
        self,
        link: PatientAccountLink,
        account_id: uuid.UUID,
        hospital_id: uuid.UUID,
        now: datetime,
        client: ClientContext,
    ) -> None:
        """End a stale link that a new one replaces, and record it. Does not commit."""
        previous_account_id = link.account_id
        await self._links.end_link(link, now=now, reason=_ENDED_SUPERSEDED)
        await self._audit.record(
            patient_event(
                "patient.link.ended",
                target_type="patient_account_link",
                target_id=link.id,
                account_id=account_id,
                hospital_id=hospital_id,
                context={
                    "reason": _ENDED_SUPERSEDED,
                    "previous_account_id": str(previous_account_id),
                },
                client=client,
            )
        )

    async def _refuse(
        self,
        operation: str,
        outcome: str,
        account_id: uuid.UUID,
        hospital_id: uuid.UUID | None,
        client: ClientContext,
        mrn: str | None = None,
    ) -> None:
        """Record a refused attempt and its outcome, and commit it.

        ``hospital_id`` is ``None`` only when the hospital named in the path
        could not be used: the entry is then a platform-level one, and no
        hospital's staff can see it.

        Committed here rather than by the caller, which raises as soon as this
        returns: the entry must survive the failed request. It never holds the
        date of birth or the MRN that was entered — only whether an MRN was.
        """
        await self._audit.record(
            patient_event(
                "patient.link.attempted",
                target_type="patient_account_link",
                account_id=account_id,
                hospital_id=hospital_id,
                context={
                    "operation": operation,
                    "outcome": outcome,
                    "mrn_supplied": mrn is not None,
                },
                client=client,
            )
        )
        await self._uow.commit()
        logger.info(
            "patient_link_refused",
            account_id=str(account_id),
            hospital_id=str(hospital_id) if hospital_id else None,
            operation=operation,
            outcome=outcome,
        )

    @staticmethod
    def _view(link: PatientAccountLink, hospital: HospitalSummary) -> PatientLink:
        """A link that was just made or confirmed: by construction it is honoured."""
        return PatientLink(
            hospital_id=hospital.id,
            hospital_ref=PatientHospitalGate.public_ref(hospital),
            hospital_name=hospital.name,
            linked_at=link.linked_at,
            suspended=False,
        )
