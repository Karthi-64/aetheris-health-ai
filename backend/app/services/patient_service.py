"""Business logic for the Patient Management module.

Owns every patient business rule from
``docs/modules/03-patient-management.md`` §4, the transaction boundary for
patient writes (``docs/03-ARCHITECTURE.md`` §9), and the audit record for every
mutation (CLAUDE.md rule 9).

Returns Pydantic DTOs, never ORM models (``docs/03-ARCHITECTURE.md`` §15,
rule 7).

**Tenancy.** Every public method takes ``hospital_id`` and passes it to the
repository. Until ``app/core/tenancy.py`` provides ambient tenant context, the
caller supplies it — and a caller that supplies the wrong one gets nothing back
rather than another tenant's data, because the filter is applied in SQL.

**Actor.** ``actor_id`` is the acting user's UUID, supplied by the caller. When
the authentication module lands it comes from ``get_current_user``; the
signature does not change.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import date, timedelta  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.schemas.common import Page, PaginationParams
from app.schemas.patient import (
    MAX_PATIENT_AGE_YEARS,
    CreatePatientRequest,
    PatientResponse,
    PatientSummaryResponse,
    SearchPatientRequest,
    UpdatePatientRequest,
)
from app.utils.datetime import age, subtract_years, utc_today
from app.utils.phone import is_e164

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.patient import Patient
    from app.repositories.patient_repository import PatientRepository
    from app.services.mrn_service import MRNService

logger = get_logger(__name__)

__all__ = [
    "MAX_MRN_ATTEMPTS",
    "DuplicateMrnError",
    "PatientAlreadyActiveError",
    "PatientNotDeactivatedError",
    "PatientNotFoundError",
    "PatientService",
]

#: How many times registration re-draws an MRN after a unique-constraint
#: collision (module spec §14). The row lock in ``MrnSequenceRepository.advance``
#: makes a collision between two concurrent registrations impossible; these
#: retries cover the case where the counter has drifted behind existing rows,
#: e.g. after a data import that wrote MRNs directly.
MAX_MRN_ATTEMPTS = 5

#: Name of the database constraint that enforces MRN uniqueness per hospital.
_MRN_UNIQUE_CONSTRAINT = "uq_patients_hospital_mrn"

#: Column names a client may set through create/update. Anything outside this
#: set is rejected before it reaches the repository, so a schema change that
#: accidentally widens a DTO cannot silently widen what is writable.
_WRITABLE_COLUMNS = frozenset(
    {
        "first_name",
        "last_name",
        "date_of_birth",
        "gender",
        "blood_group",
        "phone",
        "email",
        "address",
        "emergency_contact",
        "marital_status",
        "occupation",
        "allergies",
        "chronic_conditions",
        "current_medications",
        "notes",
    }
)


# ── Module exceptions ───────────────────────────────────────────────────────
# docs/03-ARCHITECTURE.md §10: business exceptions belong to the module that
# raises them. They subclass the shared hierarchy in app/core/exceptions.py so
# the global handler maps them to the right status and error code with no
# per-module wiring.


class PatientNotFoundError(NotFoundError):
    """Raised when a patient does not exist in the requested hospital.

    Also raised when the patient exists in a *different* hospital: a
    cross-tenant lookup must be indistinguishable from a miss, or the 404/403
    difference leaks the existence of another tenant's records.
    """

    def __init__(self, patient_id: uuid.UUID) -> None:
        super().__init__(
            message="Patient not found.",
            detail={"patient_id": str(patient_id)},
        )


class DuplicateMrnError(ConflictError):
    """Raised when an MRN is already in use within the hospital."""

    def __init__(self, mrn: str | None = None) -> None:
        super().__init__(
            message="A patient with this Medical Record Number already exists.",
            detail={"mrn": mrn} if mrn else None,
        )


class PatientAlreadyActiveError(BusinessRuleError):
    """Raised when reactivating a patient who was never deactivated."""

    def __init__(self, patient_id: uuid.UUID) -> None:
        super().__init__(
            message="Patient is already active.",
            detail={"patient_id": str(patient_id)},
        )


class PatientNotDeactivatedError(BusinessRuleError):
    """Raised when deactivating a patient who is already deactivated."""

    def __init__(self, patient_id: uuid.UUID) -> None:
        super().__init__(
            message="Patient is already deactivated.",
            detail={"patient_id": str(patient_id)},
        )


class PatientService:
    """Patient registration, retrieval, search, and lifecycle.

    :param patients: Patient data access.
    :param mrn_service: Generates Medical Record Numbers.
    :param session: The request-scoped session. Held only to own the
        transaction boundary — the service never queries through it directly
        (``docs/03-ARCHITECTURE.md`` §15, rule 3).
    :param audit: Where audit events are recorded.
    """

    def __init__(
        self,
        patients: PatientRepository,
        mrn_service: MRNService,
        session: AsyncSession,
        audit: AuditSink,
    ) -> None:
        self._patients = patients
        self._mrn = mrn_service
        self._session = session
        self._audit = audit

    # ── Validation ────────────────────────────────────────────────────────────

    def validate_patient_data(self, payload: CreatePatientRequest | UpdatePatientRequest) -> None:
        """Assert the module's business rules over a create or update payload.

        The Pydantic schemas already enforce these at the API boundary. They are
        re-asserted here because the service is also reachable from seed
        scripts, background jobs, and other services, none of which go through
        the HTTP layer. A rule that only holds for HTTP callers is not a rule.

        Checks (module spec §4 and §11):

        - date of birth is not in the future and implies an age ≤ 130
        - phone, when present, is E.164
        - names, when present, are non-blank

        Patient email is deliberately **not** checked for uniqueness. Family
        members routinely share one address, so a uniqueness constraint would
        block legitimate registrations. The module spec's "check duplicate
        email (if required)" is answered here: not required.

        :param payload: The create or update payload to validate.
        :raises ValidationError: If any rule is violated. ``detail`` carries
            per-field messages in the shape ``docs/06-API_STANDARDS.md`` §5.3
            expects.
        """
        errors: list[dict[str, str]] = []
        today = utc_today()

        dob = payload.date_of_birth
        if dob is not None:
            if dob > today:
                errors.append({"field": "date_of_birth", "message": "Cannot be in the future."})
            elif age(dob, today) > MAX_PATIENT_AGE_YEARS:
                errors.append(
                    {
                        "field": "date_of_birth",
                        "message": f"Cannot be more than {MAX_PATIENT_AGE_YEARS} years ago.",
                    }
                )

        if payload.phone and not is_e164(payload.phone):
            errors.append(
                {"field": "phone", "message": "Must be in E.164 format, e.g. +919812345678."}
            )

        for field_name in ("first_name", "last_name"):
            value = getattr(payload, field_name, None)
            if value is not None and not value.strip():
                errors.append({"field": field_name, "message": "Must not be blank."})

        if errors:
            logger.warning(
                "patient.validation_failed",
                # Field names only — the values that failed are PII.
                fields=[error["field"] for error in errors],
            )
            raise ValidationError(
                message="Patient data failed validation.",
                detail={"errors": errors},
            )

    # ── Commands ──────────────────────────────────────────────────────────────

    async def register_patient(
        self,
        hospital_id: uuid.UUID,
        payload: CreatePatientRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> PatientResponse:
        """Register a new patient and generate their MRN (module spec §5.1).

        The MRN reservation and the patient insert share one transaction, so a
        failed insert never burns an MRN that no patient holds. On a unique
        collision the MRN is redrawn up to :data:`MAX_MRN_ATTEMPTS` times
        (module spec §14).

        :param hospital_id: The hospital registering the patient.
        :param payload: Validated registration data.
        :param actor_id: UUID of the acting user.
        :returns: The created patient.
        :raises ValidationError: If the payload violates a business rule.
        :raises DuplicateMrnError: If a free MRN could not be found.
        """
        self.validate_patient_data(payload)

        values = self._writable_values(payload.model_dump(mode="json"))
        # date_of_birth and the enums round-trip through ``mode="json"`` as
        # strings; SQLAlchemy needs the Python objects for a DATE column.
        values["date_of_birth"] = payload.date_of_birth
        values["gender"] = payload.gender

        patient = await self._insert_with_generated_mrn(
            hospital_id=hospital_id,
            values=values,
            actor_id=actor_id,
        )

        await self._audit.record(
            AuditEvent(
                action="patient.created",
                hospital_id=hospital_id,
                target_type="patient",
                target_id=patient.id,
                actor_id=actor_id,
                changes={name: {"before": None, "after": value} for name, value in values.items()},
            )
        )
        await self._session.commit()

        logger.info(
            "patient.created",
            hospital_id=str(hospital_id),
            patient_id=str(patient.id),
            actor_id=str(actor_id) if actor_id else None,
        )
        return PatientResponse.from_model(patient)

    async def update_patient(
        self,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        payload: UpdatePatientRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> PatientResponse:
        """Apply a partial update to a patient (module spec §5.2).

        Only fields the client actually sent are applied — an absent field and
        an explicit ``null`` are different requests, and conflating them would
        silently wipe data the client never mentioned.

        :param hospital_id: The hospital the patient belongs to.
        :param patient_id: The patient to update.
        :param payload: Partial update data.
        :param actor_id: UUID of the acting user.
        :returns: The updated patient.
        :raises PatientNotFoundError: If the patient is absent from this tenant.
        :raises ValidationError: If the payload violates a business rule.
        """
        self.validate_patient_data(payload)

        patient = await self._get_or_raise(hospital_id, patient_id)

        changes = self._writable_values(payload.changed_fields())
        if "date_of_birth" in changes and payload.date_of_birth is not None:
            changes["date_of_birth"] = payload.date_of_birth
        if "gender" in changes and payload.gender is not None:
            changes["gender"] = payload.gender

        diff = self._diff(patient, changes)
        if not diff:
            # Nothing actually changed. Return the current state rather than
            # writing a no-op row and an audit entry that implies an edit.
            logger.info(
                "patient.update_noop",
                hospital_id=str(hospital_id),
                patient_id=str(patient_id),
            )
            return PatientResponse.from_model(patient)

        patient = await self._patients.update_patient(patient, updated_by=actor_id, **changes)

        await self._audit.record(
            AuditEvent(
                action="patient.updated",
                hospital_id=hospital_id,
                target_type="patient",
                target_id=patient.id,
                actor_id=actor_id,
                changes=diff,
            )
        )
        await self._session.commit()

        logger.info(
            "patient.updated",
            hospital_id=str(hospital_id),
            patient_id=str(patient.id),
            actor_id=str(actor_id) if actor_id else None,
            # Field names only, never values (docs/07-SECURITY.md rule 10).
            changed_fields=sorted(diff),
        )
        return PatientResponse.from_model(patient)

    async def deactivate_patient(
        self,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> PatientResponse:
        """Deactivate a patient by soft-deleting the record.

        This is what ``DELETE /api/v1/patients/{id}`` performs. Patients are
        never removed from the database (module spec §4, rule 4) — appointments
        and invoices keep referencing them, and a hard delete would orphan
        clinical history.

        :param hospital_id: The hospital the patient belongs to.
        :param patient_id: The patient to deactivate.
        :param actor_id: UUID of the acting user.
        :returns: The deactivated patient.
        :raises PatientNotFoundError: If the patient is absent from this tenant.
        :raises PatientNotDeactivatedError: If already deactivated.
        """
        patient = await self._patients.get_patient_by_id(
            hospital_id, patient_id, include_deleted=True
        )
        if patient is None:
            raise PatientNotFoundError(patient_id)
        if patient.deleted_at is not None:
            raise PatientNotDeactivatedError(patient_id)

        patient = await self._patients.delete_patient(patient, deleted_by=actor_id)

        await self._audit.record(
            AuditEvent(
                action="patient.deactivated",
                hospital_id=hospital_id,
                target_type="patient",
                target_id=patient.id,
                actor_id=actor_id,
            )
        )
        await self._session.commit()

        logger.info(
            "patient.deactivated",
            hospital_id=str(hospital_id),
            patient_id=str(patient.id),
            actor_id=str(actor_id) if actor_id else None,
        )
        return PatientResponse.from_model(patient)

    async def activate_patient(
        self,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> PatientResponse:
        """Reactivate a previously deactivated patient.

        :param hospital_id: The hospital the patient belongs to.
        :param patient_id: The patient to reactivate.
        :param actor_id: UUID of the acting user.
        :returns: The reactivated patient.
        :raises PatientNotFoundError: If the patient is absent from this tenant.
        :raises PatientAlreadyActiveError: If the patient was never deactivated.
        """
        patient = await self._patients.get_patient_by_id(
            hospital_id, patient_id, include_deleted=True
        )
        if patient is None:
            raise PatientNotFoundError(patient_id)
        if patient.deleted_at is None:
            raise PatientAlreadyActiveError(patient_id)

        patient = await self._patients.restore_patient(patient, updated_by=actor_id)

        await self._audit.record(
            AuditEvent(
                action="patient.activated",
                hospital_id=hospital_id,
                target_type="patient",
                target_id=patient.id,
                actor_id=actor_id,
            )
        )
        await self._session.commit()

        logger.info(
            "patient.activated",
            hospital_id=str(hospital_id),
            patient_id=str(patient.id),
            actor_id=str(actor_id) if actor_id else None,
        )
        return PatientResponse.from_model(patient)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_patient_details(
        self,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        *,
        include_inactive: bool = False,
    ) -> PatientResponse:
        """Retrieve one patient's full record.

        :param hospital_id: The hospital the patient belongs to.
        :param patient_id: The patient UUID.
        :param include_inactive: Return the record even if deactivated.
        :returns: The patient.
        :raises PatientNotFoundError: If the patient is absent from this tenant.
        """
        patient = await self._patients.get_patient_by_id(
            hospital_id, patient_id, include_deleted=include_inactive
        )
        if patient is None:
            raise PatientNotFoundError(patient_id)
        return PatientResponse.from_model(patient)

    async def list_patients(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        include_inactive: bool = False,
    ) -> Page[PatientSummaryResponse]:
        """List patients in a hospital, ordered by name.

        :param hospital_id: The hospital to list.
        :param pagination: Page and page size. Defaults to page 1.
        :param include_inactive: Include deactivated patients.
        :returns: One page of patient summaries plus the total count.
        """
        page_params = pagination or PaginationParams()

        rows = await self._patients.list_patients(
            hospital_id,
            include_deleted=include_inactive,
            skip=page_params.offset,
            limit=page_params.limit,
        )
        total = await self._patients.count_patients(
            hospital_id,
            include_deleted=include_inactive,
        )

        return Page[PatientSummaryResponse](
            items=[PatientSummaryResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    async def search_patients(
        self,
        hospital_id: uuid.UUID,
        filters: SearchPatientRequest,
        *,
        pagination: PaginationParams | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> Page[PatientSummaryResponse]:
        """Search patients within a hospital (module spec §5.5).

        ``q`` prefix-matches name case-insensitively and exact-matches MRN and
        phone. Age bounds are converted to date-of-birth bounds here rather
        than in SQL, so the query stays index-friendly and the calendar
        arithmetic stays in one testable place.

        :param hospital_id: The hospital to search.
        :param filters: Search term and filters.
        :param pagination: Page and page size. Defaults to page 1.
        :param actor_id: UUID of the acting user, recorded on the search audit
            event (module spec §"Logging": patient search is logged).
        :returns: One page of matching summaries plus the total match count.
        """
        page_params = pagination or PaginationParams()
        born_on_or_after, born_on_or_before = self._age_bounds_to_dob_bounds(
            age_gte=filters.age_gte,
            age_lte=filters.age_lte,
        )

        criteria: dict[str, Any] = {
            "term": filters.q,
            "gender": filters.gender,
            "date_of_birth": filters.date_of_birth,
            "born_on_or_after": born_on_or_after,
            "born_on_or_before": born_on_or_before,
            "include_deleted": filters.include_inactive,
        }

        rows = await self._patients.search_patients(
            hospital_id,
            skip=page_params.offset,
            limit=page_params.limit,
            **criteria,
        )
        total = await self._patients.count_patients(hospital_id, **criteria)

        await self._audit.record(
            AuditEvent(
                action="patient.searched",
                hospital_id=hospital_id,
                target_type="patient",
                actor_id=actor_id,
                # The search term itself can be a patient's name or phone
                # number, so only which filters were used is recorded.
                context={
                    "filters_used": sorted(
                        name for name, value in criteria.items() if value not in (None, False)
                    ),
                    "result_count": total,
                },
            )
        )

        logger.info(
            "patient.searched",
            hospital_id=str(hospital_id),
            actor_id=str(actor_id) if actor_id else None,
            result_count=total,
        )
        # Deliberately no commit: a read path must not commit a shared
        # transaction (the suite pins this invariant). The search event is an
        # observability entry — it reaches structlog via the sink, while the
        # durable trail carries mutations only.

        return Page[PatientSummaryResponse](
            items=[PatientSummaryResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _get_or_raise(self, hospital_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
        """Fetch an active patient or raise :class:`PatientNotFoundError`."""
        patient = await self._patients.get_patient_by_id(hospital_id, patient_id)
        if patient is None:
            raise PatientNotFoundError(patient_id)
        return patient

    async def _insert_with_generated_mrn(
        self,
        *,
        hospital_id: uuid.UUID,
        values: dict[str, Any],
        actor_id: uuid.UUID | None,
    ) -> Patient:
        """Insert a patient, redrawing the MRN on a unique-constraint collision.

        Each attempt runs inside a ``SAVEPOINT``. Rolling back to the savepoint
        after a collision discards the failed insert *and* the counter advance
        that produced the colliding MRN, leaving the outer transaction usable —
        a plain rollback would abort the whole transaction and force the caller
        to start over.

        :param hospital_id: The hospital registering the patient.
        :param values: Column values for the new patient.
        :param actor_id: UUID of the acting user.
        :returns: The inserted patient.
        :raises DuplicateMrnError: If every attempt collided.
        """
        last_mrn: str | None = None

        for attempt in range(1, MAX_MRN_ATTEMPTS + 1):
            try:
                async with self._session.begin_nested():
                    last_mrn = await self._mrn.next(hospital_id)
                    return await self._patients.create_patient(
                        hospital_id=hospital_id,
                        mrn=last_mrn,
                        created_by=actor_id,
                        **values,
                    )
            except IntegrityError as exc:
                if not _is_mrn_collision(exc):
                    raise
                logger.warning(
                    "patient.mrn_collision_retry",
                    hospital_id=str(hospital_id),
                    attempt=attempt,
                    max_attempts=MAX_MRN_ATTEMPTS,
                )

        logger.error(
            "patient.mrn_generation_exhausted",
            hospital_id=str(hospital_id),
            attempts=MAX_MRN_ATTEMPTS,
        )
        raise DuplicateMrnError(last_mrn)

    @staticmethod
    def _writable_values(values: dict[str, Any]) -> dict[str, Any]:
        """Keep only the columns a client is allowed to write.

        :param values: Candidate column values.
        :returns: The subset present in :data:`_WRITABLE_COLUMNS`.
        """
        return {name: value for name, value in values.items() if name in _WRITABLE_COLUMNS}

    @staticmethod
    def _diff(patient: Patient, changes: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Build a before/after diff, dropping fields whose value is unchanged.

        :param patient: The patient as it stands before the update.
        :param changes: Proposed column values.
        :returns: ``{field: {"before": old, "after": new}}`` for real changes.
        """
        diff: dict[str, dict[str, Any]] = {}
        for name, new_value in changes.items():
            current = getattr(patient, name, None)
            if current != new_value:
                diff[name] = {"before": current, "after": new_value}
        return diff

    @staticmethod
    def _age_bounds_to_dob_bounds(
        *,
        age_gte: int | None,
        age_lte: int | None,
        today: date | None = None,
    ) -> tuple[date | None, date | None]:
        """Convert an age range into a date-of-birth range.

        Age and date of birth run in opposite directions: a *minimum* age is a
        *latest* birth date, and vice versa.

        ``age >= n`` means born on or before ``today - n years``. ``age <= n``
        means born strictly after ``today - (n + 1) years``, i.e. on or after
        the day following it — someone born exactly ``n + 1`` years ago has
        already had that birthday and is ``n + 1``, not ``n``.

        :param age_gte: Minimum age in years, or ``None``.
        :param age_lte: Maximum age in years, or ``None``.
        :param today: Reference date. Defaults to today in UTC.
        :returns: ``(born_on_or_after, born_on_or_before)``, either of which may
            be ``None``.
        """
        reference = today or utc_today()
        born_on_or_before = subtract_years(reference, age_gte) if age_gte is not None else None
        born_on_or_after = (
            subtract_years(reference, age_lte + 1) + timedelta(days=1)
            if age_lte is not None
            else None
        )
        return born_on_or_after, born_on_or_before


def _is_mrn_collision(exc: IntegrityError) -> bool:
    """Check whether an integrity error came from the MRN unique constraint.

    Any other integrity error (a bad ``hospital_id`` foreign key, a NOT NULL
    violation) is a real bug and must propagate rather than be retried.

    :param exc: The raised integrity error.
    :returns: ``True`` if the MRN unique constraint was violated.
    """
    driver_error = getattr(exc, "orig", None)
    # asyncpg surfaces the constraint name on the wrapped exception; the
    # string check is the fallback for drivers that do not.
    constraint_name = getattr(getattr(driver_error, "__cause__", None), "constraint_name", None)
    if constraint_name == _MRN_UNIQUE_CONSTRAINT:
        return True
    return _MRN_UNIQUE_CONSTRAINT in str(driver_error)
