"""Record access grants (``docs/modules/15-patient-app.md`` §8.3).

A grant lets a named recipient — a hospital, or one doctor — read named
categories of one patient record, until it expires or is revoked.

**No recipient can read on a grant yet.** The reading side lives in the
Hospital application and is a later phase. What exists now is everything that
side will depend on: grants can be created, listed and revoked by the patient,
and :meth:`AccessGrantService.authorize` is the complete gate. It is the only
function that may ever answer "may this staff member read this record on a
grant?", it is to be called by the service method that returns the data on
every request, and it decides against the database at that moment — a check
in a router, in a UI, or at list time only is not enforcement, and grant state
is never cached in a token or anywhere else.

**Status is derived, never stored.** Revoked if ``revoked_at`` is set; else
expired once ``expires_at`` is reached; else active. Revocation and expiry
take effect on the next request, with no grace period. Ending or suspending
the grantor's record link makes every grant from it unusable without touching
the grant rows.

A read with no authorising grant is ``404``, never ``403``: the answer does
not confirm that the record exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from app.core.exceptions import NotFoundError, ValidationError
from app.models.patient_consent import (
    GranteeType,
    GrantPurposeNote,
    GrantStatus,
    RecordCategory,
)
from app.schemas.patient_app.grants import AccessGrantView
from app.services.patient_app.common import ClientContext, patient_event

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from datetime import date

    from app.core.audit import AuditSink
    from app.database.unit_of_work import UnitOfWork
    from app.models.patient_account import PatientAccount
    from app.models.patient_consent import PatientAccessGrant
    from app.repositories.doctor_repository import DoctorRepository
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.patient_access_grant_repository import PatientAccessGrantRepository
    from app.services.patient_app.patient_authorization import PatientAuthorization

__all__ = [
    "DEFAULT_GRANT_LIFETIME",
    "MAX_GRANT_LIFETIME",
    "AccessGrantService",
    "Grantee",
]

#: How long a grant lasts when the patient does not say, and the longest it may.
DEFAULT_GRANT_LIFETIME: Final = timedelta(days=30)
MAX_GRANT_LIFETIME: Final = timedelta(days=365)

#: Categories that are defined but cannot be granted or read: no document
#: source exists yet (§10).
_UNAVAILABLE_CATEGORIES: Final = frozenset({RecordCategory.DOCUMENTS})

_NOT_FOUND: Final = "Not found."


@dataclass(frozen=True, slots=True)
class Grantee:
    """The staff member asking to read on a grant.

    Built by the caller from the *authenticated* staff principal — never from
    a request.

    :param hospital_id: The hospital the staff member belongs to.
    :param doctor_id: Their doctor identity, if they are a doctor.
    """

    hospital_id: uuid.UUID
    doctor_id: uuid.UUID | None = None


def _validation_error(field: str, message: str) -> ValidationError:
    return ValidationError(
        message="The grant is not valid.",
        detail={"errors": [{"field": field, "message": message}]},
    )


class AccessGrantService:
    """Creates, revokes, lists and enforces record access grants.

    :param grants: Grant data access.
    :param authorization: Resolves the grantor's standing at the source hospital.
    :param hospitals: To check that a recipient hospital exists.
    :param doctors: To check that a recipient doctor exists at that hospital.
    :param uow: The request's unit of work.
    :param audit: Where grant changes are recorded.
    """

    def __init__(
        self,
        grants: PatientAccessGrantRepository,
        authorization: PatientAuthorization,
        hospitals: HospitalRepository,
        doctors: DoctorRepository,
        *,
        uow: UnitOfWork,
        audit: AuditSink,
    ) -> None:
        self._grants = grants
        self._authorization = authorization
        self._hospitals = hospitals
        self._doctors = doctors
        self._uow = uow
        self._audit = audit

    # ── Patient side ─────────────────────────────────────────────────────────

    async def create(
        self,
        account: PatientAccount,
        hospital_id: uuid.UUID,
        *,
        grantee_type: GranteeType,
        grantee_id: uuid.UUID,
        grantee_hospital_id: uuid.UUID,
        categories: Sequence[RecordCategory],
        purpose_note: GrantPurposeNote,
        expires_at: datetime | None = None,
        records_from: date | None = None,
        records_to: date | None = None,
        context_appointment_id: uuid.UUID | None = None,
        client: ClientContext | None = None,
    ) -> AccessGrantView:
        """Give a recipient access to categories of the patient's own record.

        :param account: The authenticated account giving the grant.
        :param hospital_id: The source hospital — where the record is.
        :param grantee_type: ``hospital`` or ``doctor``.
        :param grantee_id: The recipient hospital, or the recipient doctor.
        :param grantee_hospital_id: The hospital the recipient belongs to.
        :param categories: What is shared. At least one; none implies another.
        :param purpose_note: Why, from the fixed list.
        :param expires_at: When the grant ends. Thirty days from now if not
            given; never more than 365.
        :param records_from: Earliest record date shared, if bounded.
        :param records_to: Latest record date shared, if bounded.
        :param context_appointment_id: Not accepted yet — see below.
        :param client: Where the request came from.
        :returns: The grant.
        :raises NotFoundError: The account holds no honoured link at the
            source hospital.
        :raises ConsentRequiredError: A required consent is not in force.
        :raises ValidationError: The grant itself is not valid.
        """
        account_id = account.id
        context = await self._authorization.resolve_context(account, hospital_id)
        now = datetime.now(UTC)

        chosen = self._validated_categories(categories)
        expiry = self._validated_expiry(expires_at, now)
        if records_from is not None and records_to is not None and records_from > records_to:
            raise _validation_error("records_to", "Must not be before records_from.")
        if context_appointment_id is not None:
            # An appointment context has to be the patient's own appointment
            # at the recipient hospital, and nothing can check that until
            # patient booking exists. Refused rather than stored unchecked.
            raise _validation_error("context_appointment_id", "Not supported yet.")
        await self._check_grantee(grantee_type, grantee_id, grantee_hospital_id)

        grant = await self._grants.create_grant(
            context.hospital_id,
            patient_id=context.patient_id,
            grantor_account_id=account_id,
            grantee_type=grantee_type.value,
            grantee_id=grantee_id,
            grantee_hospital_id=grantee_hospital_id,
            purpose_note=purpose_note.value,
            categories=chosen,
            records_from=records_from,
            records_to=records_to,
            granted_at=now,
            expires_at=expiry,
        )
        view = self._view(grant, now)
        await self._audit.record(
            patient_event(
                "patient.access_grant.created",
                target_type="patient_access_grant",
                target_id=grant.id,
                account_id=account_id,
                hospital_id=context.hospital_id,
                context={
                    "grantee_type": grantee_type.value,
                    "grantee_hospital_id": str(grantee_hospital_id),
                    "categories": [category.value for category in chosen],
                    "purpose_note": purpose_note.value,
                    "expires_at": expiry.isoformat(),
                },
                client=client,
            )
        )
        await self._uow.commit()
        return view

    async def revoke(
        self,
        account: PatientAccount,
        hospital_id: uuid.UUID,
        grant_id: uuid.UUID,
        *,
        reason: str | None = None,
        client: ClientContext | None = None,
    ) -> AccessGrantView:
        """Revoke a grant the account gave. Effective on the next request.

        Keyed by the grantor alone, with no link check: a patient can always
        take back what they gave, whatever has happened to their link since.
        Revoking a grant that is already revoked changes nothing.

        :param account: The authenticated account.
        :param hospital_id: The source hospital of the grant.
        :param grant_id: The grant.
        :param reason: Why, if the patient says.
        :param client: Where the request came from.
        :returns: The grant as it now stands.
        :raises NotFoundError: No such grant was given by this account there.
        """
        account_id = account.id
        grant = await self._grants.get_for_grantor(
            hospital_id, grant_id, grantor_account_id=account_id
        )
        if grant is None:
            raise NotFoundError(_NOT_FOUND)
        now = datetime.now(UTC)
        if grant.revoked_at is not None:
            return self._view(grant, now)

        await self._grants.revoke(
            grant, now=now, account_id=account_id, reason=reason[:200] if reason else None
        )
        view = self._view(grant, now)
        await self._audit.record(
            patient_event(
                "patient.access_grant.revoked",
                target_type="patient_access_grant",
                target_id=grant.id,
                account_id=account_id,
                hospital_id=hospital_id,
                client=client,
            )
        )
        await self._uow.commit()
        return view

    async def list_for_account(
        self, account: PatientAccount, hospital_id: uuid.UUID
    ) -> list[AccessGrantView]:
        """Every grant the account has given at one hospital, newest first.

        :param account: The authenticated account.
        :param hospital_id: The source hospital.
        :returns: The grants, each with its status as of now.
        """
        now = datetime.now(UTC)
        grants = await self._grants.list_for_grantor(hospital_id, grantor_account_id=account.id)
        return [self._view(grant, now) for grant in grants]

    # ── The gate ─────────────────────────────────────────────────────────────

    async def authorize(
        self,
        grantee: Grantee,
        patient_id: uuid.UUID,
        source_hospital_id: uuid.UUID,
        category: RecordCategory,
        record_date: date | None,
    ) -> PatientAccessGrant:
        """Find the grant that lets this staff member read this record, or refuse.

        Evaluated against the database, now, for this one read:

        * a grant on this record at the source hospital exists and names the
          reader's hospital;
        * it is not revoked and has not expired;
        * the recipient matches — for a ``hospital`` grant, the reader's
          hospital; for a ``doctor`` grant, the reader's own doctor identity;
        * the category is among those granted (no category implies another);
        * the record's date is inside the grant's window, if it has one;
        * the grantor still holds an active, honoured link to the record —
          the link the grant was given under, not one made since.

        The caller writes ``patient.access_grant.used`` for the read it then
        performs, with the id of the grant returned here.

        A staff request is confined to the reader's own hospital by the
        tenant guard, and a cross-hospital grant lives in the source
        hospital's rows — so the reading side must call this under an explicit
        system scope, and that decision belongs to it.

        :param grantee: The authenticated staff member.
        :param patient_id: The record to be read.
        :param source_hospital_id: The hospital that holds it.
        :param category: The category of what is to be read.
        :param record_date: The date of the record to be read, if it has one.
        :returns: The grant that authorises the read.
        :raises NotFoundError: If no grant does — for whatever reason.
        """
        if category in _UNAVAILABLE_CATEGORIES:
            raise NotFoundError(_NOT_FOUND)

        now = datetime.now(UTC)
        candidates = await self._grants.list_unrevoked_for_recipient(
            source_hospital_id, patient_id=patient_id, grantee_hospital_id=grantee.hospital_id
        )
        for grant in candidates:
            if not self._permits(grant, grantee, category, record_date, now):
                continue
            # Last, because it is the only check that reads beyond the grant.
            if await self._authorization.grantor_link_is_live(
                grant.grantor_account_id,
                source_hospital_id,
                patient_id,
                granted_at=grant.granted_at,
            ):
                return grant
        raise NotFoundError(_NOT_FOUND)

    @staticmethod
    def _permits(
        grant: PatientAccessGrant,
        grantee: Grantee,
        category: RecordCategory,
        record_date: date | None,
        now: datetime,
    ) -> bool:
        """Whether one grant, taken on its own, covers one read."""
        if grant.status_at(now) is not GrantStatus.ACTIVE:
            return False
        if grant.grantee_hospital_id != grantee.hospital_id:
            return False
        if grant.grantee_type == GranteeType.HOSPITAL.value:
            if grant.grantee_id != grantee.hospital_id:
                return False
        elif grant.grantee_type == GranteeType.DOCTOR.value:
            if grantee.doctor_id is None or grant.grantee_id != grantee.doctor_id:
                return False
        else:
            return False
        if category not in grant.categories:
            return False
        if grant.records_from is None and grant.records_to is None:
            return True
        # A windowed grant covers only records that carry a date inside it.
        if record_date is None:
            return False
        if grant.records_from is not None and record_date < grant.records_from:
            return False
        return grant.records_to is None or record_date <= grant.records_to

    # ── Internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _validated_categories(categories: Sequence[RecordCategory]) -> list[RecordCategory]:
        """At least one known category, each once, none that cannot be granted yet."""
        chosen: list[RecordCategory] = []
        for category in categories:
            if not isinstance(category, RecordCategory):
                raise _validation_error("categories", "Unknown category.")
            if category in _UNAVAILABLE_CATEGORIES:
                raise _validation_error("categories", "Documents cannot be shared yet.")
            if category not in chosen:
                chosen.append(category)
        if not chosen:
            raise _validation_error("categories", "Choose at least one category.")
        return chosen

    @staticmethod
    def _validated_expiry(expires_at: datetime | None, now: datetime) -> datetime:
        """The grant's expiry: thirty days by default, in the future, at most a year away."""
        if expires_at is None:
            return now + DEFAULT_GRANT_LIFETIME
        if expires_at.tzinfo is None:
            raise _validation_error("expires_at", "Must include a time zone.")
        if expires_at <= now:
            raise _validation_error("expires_at", "Must be in the future.")
        if expires_at > now + MAX_GRANT_LIFETIME:
            raise _validation_error("expires_at", "Must be within 365 days.")
        return expires_at.astimezone(UTC)

    async def _check_grantee(
        self, grantee_type: GranteeType, grantee_id: uuid.UUID, grantee_hospital_id: uuid.UUID
    ) -> None:
        """A recipient is always a hospital or a doctor registered on the platform."""
        hospital = await self._hospitals.get_active_summary(id=grantee_hospital_id)
        if hospital is None:
            raise _validation_error("grantee_hospital_id", "Unknown recipient.")
        if grantee_type is GranteeType.HOSPITAL:
            if grantee_id != grantee_hospital_id:
                raise _validation_error("grantee_id", "Unknown recipient.")
        elif not await self._doctors.doctor_exists(grantee_hospital_id, grantee_id):
            raise _validation_error("grantee_id", "Unknown recipient.")

    @staticmethod
    def _view(grant: PatientAccessGrant, now: datetime) -> AccessGrantView:
        """A grant as its grantor sees it, with its status derived as of ``now``."""
        return AccessGrantView(
            id=grant.id,
            hospital_id=grant.hospital_id,
            grantee_type=GranteeType(grant.grantee_type),
            grantee_id=grant.grantee_id,
            grantee_hospital_id=grant.grantee_hospital_id,
            purpose_note=GrantPurposeNote(grant.purpose_note),
            categories=list(grant.categories),
            records_from=grant.records_from,
            records_to=grant.records_to,
            granted_at=grant.granted_at,
            expires_at=grant.expires_at,
            revoked_at=grant.revoked_at,
            status=grant.status_at(now),
        )
