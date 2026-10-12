"""Repository for prescriptions and the dispenses made against them.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it, or acts on a row already loaded through one that did
(CLAUDE.md rules 4 and 5).

A prescription is the aggregate root: its items, and the dispenses that fill
them, are only reached through it.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, select

from app.models.pharmacy import (
    Dispense,
    DispenseItem,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
)
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession


class PrescriptionRepository(BaseRepository[Prescription]):
    """Persistence for prescriptions and dispenses.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Prescription, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _scoped(self, hospital_id: uuid.UUID) -> Select[tuple[Prescription]]:
        """Return a base SELECT filtered to one hospital."""
        return self._query().where(Prescription.hospital_id == hospital_id)

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[Prescription]],
        *,
        statuses: Sequence[PrescriptionStatus] | None = None,
        patient_id: uuid.UUID | None = None,
        doctor_id: uuid.UUID | None = None,
        appointment_id: uuid.UUID | None = None,
    ) -> Select[tuple[Prescription]]:
        """Apply the filters shared by list and count.

        :param stmt: The statement to extend.
        :param statuses: Only prescriptions in one of these statuses.
        :param patient_id: Only this patient's prescriptions.
        :param doctor_id: Only prescriptions written by this doctor.
        :param appointment_id: Only prescriptions from this visit.
        :returns: The statement with predicates applied.
        """
        if statuses:
            stmt = stmt.where(Prescription.status.in_(statuses))
        if patient_id is not None:
            stmt = stmt.where(Prescription.patient_id == patient_id)
        if doctor_id is not None:
            stmt = stmt.where(Prescription.doctor_id == doctor_id)
        if appointment_id is not None:
            stmt = stmt.where(Prescription.appointment_id == appointment_id)
        return stmt

    async def _reload(self, prescription: Prescription) -> Prescription:
        """Re-read a prescription with its items fully loaded.

        A flush leaves server-maintained columns expired on every row it
        touched; reading them afterwards would be lazy IO, which async
        SQLAlchemy refuses. One ``populate_existing`` query brings the whole
        aggregate back instead.
        """
        stmt = (
            select(Prescription)
            .where(Prescription.id == prescription.id)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one()

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_prescription(
        self,
        *,
        hospital_id: uuid.UUID,
        appointment_id: uuid.UUID,
        patient_id: uuid.UUID,
        doctor_id: uuid.UUID,
        prescribed_at: datetime,
        items: Sequence[dict[str, Any]],
        notes: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> Prescription:
        """Insert a prescription together with its items. Does not commit.

        :param hospital_id: Owning tenant.
        :param appointment_id: Visit it was written in.
        :param patient_id: Patient it is for.
        :param doctor_id: Doctor who wrote it.
        :param prescribed_at: When it was written (UTC).
        :param items: Column values per line, in display order.
        :param notes: Doctor's notes.
        :param created_by: UUID of the acting user.
        :returns: The persisted prescription, items loaded.
        """
        prescription = Prescription(
            hospital_id=hospital_id,
            appointment_id=appointment_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            prescribed_at=prescribed_at,
            status=PrescriptionStatus.ACTIVE,
            notes=notes,
            created_by=created_by,
            items=[
                PrescriptionItem(
                    hospital_id=hospital_id, position=position, created_by=created_by, **values
                )
                for position, values in enumerate(items)
            ],
        )
        self._session.add(prescription)
        await self._session.flush()
        return await self._reload(prescription)

    async def update_prescription(
        self, prescription: Prescription, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> Prescription:
        """Apply field updates to a prescription and return it fully loaded.

        Call this last in a step that also changed items.

        :param prescription: The attached ORM instance to modify.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated prescription, items loaded.
        """
        for name, value in fields.items():
            setattr(prescription, name, value)
        prescription.updated_by = updated_by
        await self._session.flush()
        return await self._reload(prescription)

    async def record_dispensed(
        self, item: PrescriptionItem, quantity: int, *, updated_by: uuid.UUID | None = None
    ) -> None:
        """Add to how much of a prescription line has been dispensed.

        The database refuses a total above the prescribed quantity.

        :param item: The attached prescription line.
        :param quantity: Units just dispensed.
        :param updated_by: UUID of the acting user.
        """
        item.quantity_dispensed = item.quantity_dispensed + quantity
        item.updated_by = updated_by
        await self._session.flush()

    async def create_dispense(
        self,
        *,
        prescription: Prescription,
        dispensed_at: datetime,
        dispensed_by: uuid.UUID | None,
        total_amount: Decimal,
        notes: str | None,
        lines: Sequence[dict[str, Any]],
    ) -> Dispense:
        """Insert a dispense together with its lines. Does not commit.

        :param prescription: The prescription being filled. Supplies the tenant.
        :param dispensed_at: When it was dispensed (UTC).
        :param dispensed_by: UUID of the pharmacist.
        :param total_amount: Sum of the lines.
        :param notes: Reason for a partial dispense, or other notes.
        :param lines: Column values per line: one per batch drawn from.
        :returns: The persisted dispense, lines and their batches loaded.
        """
        dispense = Dispense(
            hospital_id=prescription.hospital_id,
            prescription_id=prescription.id,
            dispensed_at=dispensed_at,
            dispensed_by=dispensed_by,
            total_amount=total_amount,
            notes=notes,
            created_by=dispensed_by,
            items=[
                DispenseItem(
                    hospital_id=prescription.hospital_id, created_by=dispensed_by, **values
                )
                for values in lines
            ],
        )
        self._session.add(dispense)
        await self._session.flush()
        return await self._reload_dispense(dispense)

    async def set_dispense_invoice(self, dispense: Dispense, invoice_id: uuid.UUID) -> Dispense:
        """Record which invoice a dispense was charged to."""
        dispense.invoice_id = invoice_id
        await self._session.flush()
        return await self._reload_dispense(dispense)

    async def _reload_dispense(self, dispense: Dispense) -> Dispense:
        """Re-read a dispense with its lines and their batches loaded."""
        stmt = (
            select(Dispense)
            .where(Dispense.id == dispense.id)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one()

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_prescription_by_id(
        self, hospital_id: uuid.UUID, prescription_id: uuid.UUID, *, for_update: bool = False
    ) -> Prescription | None:
        """Retrieve one prescription by UUID within a hospital.

        :param for_update: Lock the row until the transaction ends, so two
            dispenses of one prescription run one after the other.
            ``of=Prescription`` because the joined patient and doctor loads
            make this an outer join.
        :returns: The prescription, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id).where(Prescription.id == prescription_id)
        if for_update:
            stmt = stmt.with_for_update(of=Prescription).execution_options(populate_existing=True)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_prescriptions(
        self,
        hospital_id: uuid.UUID,
        *,
        skip: int = 0,
        limit: int = 25,
        oldest_first: bool = False,
        **filters: Any,
    ) -> list[Prescription]:
        """List prescriptions in a hospital.

        :param oldest_first: Queue order — the longest-waiting first. Otherwise
            newest first.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        """
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        order = (
            (Prescription.prescribed_at.asc(), Prescription.id.asc())
            if oldest_first
            else (Prescription.prescribed_at.desc(), Prescription.id.desc())
        )
        stmt = self._apply_pagination(stmt.order_by(*order), skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_prescriptions(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count prescriptions matching the filters :meth:`list_prescriptions` uses."""
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()

    async def list_dispenses(
        self, hospital_id: uuid.UUID, prescription_id: uuid.UUID
    ) -> list[Dispense]:
        """Return the dispenses made against a prescription, oldest first."""
        stmt = (
            select(Dispense)
            .where(
                Dispense.hospital_id == hospital_id,
                Dispense.prescription_id == prescription_id,
                Dispense.deleted_at.is_(None),
            )
            .order_by(Dispense.dispensed_at.asc(), Dispense.id.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())
