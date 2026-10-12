"""Repository for the :class:`~app.models.patient.Patient` model.

Data access only — no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it: multi-tenant isolation is enforced on the way into the database,
never assumed from the caller (CLAUDE.md rule 5).

.. note::

   Scoping is passed explicitly, as an argument, on every method here. That is
   one of two layers: ``app/core/tenancy.py`` also confines the session itself
   to the authenticated principal's hospital, so a query that passed the wrong
   ``hospital_id`` — or forgot the filter — still cannot reach another
   hospital's rows.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, or_, select

from app.models.patient import Gender, Patient
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession


class PatientRepository(BaseRepository[Patient]):
    """Repository for patient persistence.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Patient, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _scoped(
        self, hospital_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Select[tuple[Patient]]:
        """Return a base SELECT filtered to one hospital.

        :param hospital_id: The tenant to scope to.
        :param include_deleted: When ``True``, soft-deleted rows are included.
            Only :meth:`get_patient_by_id` and reactivation paths need this.
        :returns: A statement filtered by ``hospital_id`` and, by default,
            ``deleted_at IS NULL``.
        """
        stmt = select(Patient) if include_deleted else self._query()
        return stmt.where(Patient.hospital_id == hospital_id)

    def _apply_search_filters(
        self,
        stmt: Select[tuple[Patient]],
        *,
        term: str | None = None,
        gender: Gender | None = None,
        date_of_birth: date | None = None,
        born_on_or_after: date | None = None,
        born_on_or_before: date | None = None,
    ) -> Select[tuple[Patient]]:
        """Apply the search and filter predicates shared by list, search, and count.

        ``term`` implements module spec §5.5: case-insensitive **prefix** match
        on first and last name, **exact** match on MRN and phone. Prefix rather
        than substring so the ``lower(...)`` indexes created in migration 0002
        are usable; exact on MRN and phone because a partial identifier is not
        a meaningful query.

        Age bounds arrive already converted to date-of-birth bounds — the
        calendar arithmetic is a business rule and lives in the service.

        :param stmt: The statement to extend.
        :param term: Free-text search term.
        :param gender: Exact gender filter.
        :param date_of_birth: Exact date-of-birth filter.
        :param born_on_or_after: Lower bound on date of birth (upper bound on age).
        :param born_on_or_before: Upper bound on date of birth (lower bound on age).
        :returns: The statement with predicates applied.
        """
        if term:
            needle = term.lower()
            # ``escape`` is set so a term containing % or _ is matched
            # literally rather than acting as a wildcard.
            prefix = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stmt = stmt.where(
                or_(
                    func.lower(Patient.first_name).like(prefix, escape="\\"),
                    func.lower(Patient.last_name).like(prefix, escape="\\"),
                    Patient.mrn == term,
                    Patient.phone == term,
                )
            )
        if gender is not None:
            stmt = stmt.where(Patient.gender == gender)
        if date_of_birth is not None:
            stmt = stmt.where(Patient.date_of_birth == date_of_birth)
        if born_on_or_after is not None:
            stmt = stmt.where(Patient.date_of_birth >= born_on_or_after)
        if born_on_or_before is not None:
            stmt = stmt.where(Patient.date_of_birth <= born_on_or_before)
        return stmt

    @staticmethod
    def _ordered(stmt: Select[tuple[Patient]]) -> Select[tuple[Patient]]:
        """Order by name, with ``id`` as a tiebreaker.

        The ``id`` tiebreak makes pagination stable: without it, two patients
        with identical names can swap places between page requests and a record
        can be shown twice or skipped entirely.
        """
        return stmt.order_by(Patient.last_name.asc(), Patient.first_name.asc(), Patient.id.asc())

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_patient(
        self,
        *,
        hospital_id: uuid.UUID,
        mrn: str,
        first_name: str,
        last_name: str,
        date_of_birth: date,
        gender: Gender,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> Patient:
        """Insert a new patient.

        Does not commit — the service owns the transaction
        (``docs/03-ARCHITECTURE.md`` §9).

        :param hospital_id: Owning tenant.
        :param mrn: Medical Record Number, already generated.
        :param first_name: Patient's given name.
        :param last_name: Patient's family name.
        :param date_of_birth: Date of birth.
        :param gender: Patient gender.
        :param created_by: UUID of the acting user.
        :param optional_fields: Any remaining patient columns.
        :returns: The persisted patient.
        """
        return await super().create(
            hospital_id=hospital_id,
            mrn=mrn,
            first_name=first_name,
            last_name=last_name,
            date_of_birth=date_of_birth,
            gender=gender,
            created_by=created_by,
            **optional_fields,
        )

    async def update_patient(
        self,
        patient: Patient,
        *,
        updated_by: uuid.UUID | None = None,
        **fields: Any,
    ) -> Patient:
        """Apply field updates to an existing patient.

        :param patient: The attached ORM instance to modify.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated patient.
        """
        return await self.update(patient, updated_by=updated_by, **fields)

    async def delete_patient(
        self,
        patient: Patient,
        *,
        deleted_by: uuid.UUID | None = None,
    ) -> Patient:
        """Soft-delete a patient and record who did it.

        Patients are never hard-deleted (module spec §4, rule 4). The base
        :meth:`~app.repositories.base.BaseRepository.soft_delete` sets
        ``deleted_at``; ``deleted_by`` is set here so the audit trail is
        complete in a single flush.

        :param patient: The patient to deactivate.
        :param deleted_by: UUID of the acting user.
        :returns: The soft-deleted patient.
        """
        patient.deleted_by = deleted_by
        return await self.soft_delete(patient)

    async def restore_patient(
        self,
        patient: Patient,
        *,
        updated_by: uuid.UUID | None = None,
    ) -> Patient:
        """Clear the soft delete on a patient, reactivating the record.

        :param patient: The soft-deleted patient to restore.
        :param updated_by: UUID of the acting user.
        :returns: The reactivated patient.
        """
        return await self.update(
            patient,
            deleted_at=None,
            deleted_by=None,
            updated_by=updated_by,
        )

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_patient_by_id(
        self,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> Patient | None:
        """Retrieve one patient by UUID within a hospital.

        :param hospital_id: The tenant to scope to.
        :param patient_id: The patient UUID.
        :param include_deleted: Include soft-deleted records (needed to
            reactivate one).
        :returns: The patient, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(
            Patient.id == patient_id
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_patient_by_mrn(
        self,
        hospital_id: uuid.UUID,
        mrn: str,
        *,
        include_deleted: bool = True,
    ) -> Patient | None:
        """Retrieve one patient by MRN within a hospital.

        Defaults to including soft-deleted rows: MRN is unique per hospital at
        the database level regardless of deletion state, so a duplicate check
        that ignored deactivated patients would report "available" for an MRN
        the unique index will reject.

        :param hospital_id: The tenant to scope to.
        :param mrn: The Medical Record Number.
        :param include_deleted: Include soft-deleted records.
        :returns: The patient, or ``None``.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(Patient.mrn == mrn)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_by_phone(
        self,
        hospital_id: uuid.UUID,
        phone: str,
        *,
        include_deleted: bool = True,
    ) -> list[Patient]:
        """Every record in a hospital whose phone is exactly this number.

        For the Patient App's record match
        (``docs/modules/15-patient-app.md`` §4.5), which must see deactivated
        records too: a deactivated match is refused rather than silently
        duplicated. Exact comparison only — never a prefix or a search term.

        :param hospital_id: The tenant to scope to.
        :param phone: The number, in E.164 form.
        :param include_deleted: Include soft-deleted records.
        :returns: The matching patients, oldest first.
        """
        stmt = (
            self._scoped(hospital_id, include_deleted=include_deleted)
            .where(Patient.phone == phone)
            .order_by(Patient.created_at, Patient.id)
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def lock_phone_for_link(
        self, hospital_id: uuid.UUID, patient_id: uuid.UUID
    ) -> tuple[str | None, bool] | None:
        """Lock one record's row and read the phone it carries right now.

        For the Patient App, at the one moment a link request is about to end
        somebody else's link to this record: what the request matched earlier
        may no longer be true, so the phone is read again, as committed now,
        under a row lock that holds it still until the transaction ends.

        :param hospital_id: The tenant to scope to.
        :param patient_id: The record.
        :returns: ``(phone, is_active)``, or ``None`` if there is no such
            record in this hospital.
        """
        result = await self._session.execute(
            select(Patient.phone, Patient.deleted_at)
            .where(Patient.hospital_id == hospital_id, Patient.id == patient_id)
            .with_for_update()
        )
        row = result.one_or_none()
        if row is None:
            return None
        return row.phone, row.deleted_at is None

    async def list_patients(
        self,
        hospital_id: uuid.UUID,
        *,
        include_deleted: bool = False,
        skip: int = 0,
        limit: int = 25,
    ) -> list[Patient]:
        """List patients in a hospital, ordered by name.

        :param hospital_id: The tenant to scope to.
        :param include_deleted: Include deactivated patients.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :returns: A page of patients.
        """
        stmt = self._ordered(self._scoped(hospital_id, include_deleted=include_deleted))
        stmt = self._apply_pagination(stmt, skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def search_patients(
        self,
        hospital_id: uuid.UUID,
        *,
        term: str | None = None,
        gender: Gender | None = None,
        date_of_birth: date | None = None,
        born_on_or_after: date | None = None,
        born_on_or_before: date | None = None,
        include_deleted: bool = False,
        skip: int = 0,
        limit: int = 25,
    ) -> list[Patient]:
        """Search patients within a hospital.

        See :meth:`_apply_search_filters` for the matching rules.

        :param hospital_id: The tenant to scope to.
        :param term: Free-text term (name prefix, exact MRN, exact phone).
        :param gender: Exact gender filter.
        :param date_of_birth: Exact date-of-birth filter.
        :param born_on_or_after: Lower bound on date of birth.
        :param born_on_or_before: Upper bound on date of birth.
        :param include_deleted: Include deactivated patients.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :returns: A page of matching patients.
        """
        stmt = self._apply_search_filters(
            self._scoped(hospital_id, include_deleted=include_deleted),
            term=term,
            gender=gender,
            date_of_birth=date_of_birth,
            born_on_or_after=born_on_or_after,
            born_on_or_before=born_on_or_before,
        )
        stmt = self._apply_pagination(self._ordered(stmt), skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_patients(
        self,
        hospital_id: uuid.UUID,
        *,
        term: str | None = None,
        gender: Gender | None = None,
        date_of_birth: date | None = None,
        born_on_or_after: date | None = None,
        born_on_or_before: date | None = None,
        include_deleted: bool = False,
    ) -> int:
        """Count patients matching the same filters :meth:`search_patients` uses.

        Takes the identical filter arguments so the total in a paginated
        response can never disagree with the rows on the page.

        :param hospital_id: The tenant to scope to.
        :param term: Free-text term.
        :param gender: Exact gender filter.
        :param date_of_birth: Exact date-of-birth filter.
        :param born_on_or_after: Lower bound on date of birth.
        :param born_on_or_before: Upper bound on date of birth.
        :param include_deleted: Include deactivated patients.
        :returns: The number of matching patients.
        """
        stmt = self._apply_search_filters(
            self._scoped(hospital_id, include_deleted=include_deleted),
            term=term,
            gender=gender,
            date_of_birth=date_of_birth,
            born_on_or_after=born_on_or_after,
            born_on_or_before=born_on_or_before,
        )
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one()

    async def patient_exists(
        self,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> bool:
        """Check whether a patient exists in a hospital.

        :param hospital_id: The tenant to scope to.
        :param patient_id: The patient UUID.
        :param include_deleted: Count deactivated patients as existing.
        :returns: ``True`` if the patient exists in this tenant.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(
            Patient.id == patient_id
        )
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one() > 0
