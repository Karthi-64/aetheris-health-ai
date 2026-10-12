"""Business logic for the medicine catalog and its stock.

The catalog-and-stock third of ``docs/modules/08-pharmacy.md``: which
medicines a hospital stocks (FR-1), the batches it holds of each (FR-2), and
every way stock changes other than a dispense or a purchase-order receipt —
receiving a batch directly, recalling one, and correcting or writing off a
count.

Stock is never set; it is moved. Every change here goes through
:meth:`~app.repositories.medicine_repository.MedicineRepository.apply_movement`,
which writes the ledger row and the batch's running total together.

Returns DTOs, never ORM models, and records an audit event per mutation
(CLAUDE.md rule 9).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.pharmacy import StockMovementReason
from app.schemas.common import Page, PaginationParams
from app.schemas.pharmacy import (
    ADJUSTMENT_REASONS,
    EXPIRY_WARNING_DAYS,
    AdjustStockRequest,
    BatchResponse,
    CreateMedicineRequest,
    MedicineResponse,
    MedicineStockResponse,
    ReceiveBatchRequest,
    UpdateBatchRequest,
    UpdateMedicineRequest,
)
from app.services.pharmacy_common import (
    MedicineNotFoundError,
    audit_value,
    field_error,
    hospital_today,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.pharmacy import Medicine, MedicineBatch
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.medicine_repository import MedicineRepository

logger = get_logger(__name__)

__all__ = [
    "BatchNotFoundError",
    "DuplicateMedicineSkuError",
    "PharmacyCatalogService",
]


class BatchNotFoundError(NotFoundError):
    """Raised when a batch is absent, in another tenant, or of another medicine."""

    def __init__(self, batch_id: uuid.UUID) -> None:
        super().__init__(message="Batch not found.", detail={"batch_id": str(batch_id)})


class DuplicateMedicineSkuError(ConflictError):
    """Raised when a SKU is already in use in the hospital."""

    def __init__(self, sku: str) -> None:
        super().__init__(
            message=f"A medicine with SKU '{sku}' already exists.", detail={"sku": sku}
        )


class PharmacyCatalogService:
    """Medicines, batches and stock.

    :param medicines: Medicine, batch and ledger data access.
    :param hospitals: Hospital lookups, for the local date expiry is judged by.
    :param session: Request-scoped session, held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    """

    def __init__(
        self,
        medicines: MedicineRepository,
        hospitals: HospitalRepository,
        session: AsyncSession,
        audit: AuditSink,
    ) -> None:
        self._medicines = medicines
        self._hospitals = hospitals
        self._session = session
        self._audit = audit

    # ── Medicines ─────────────────────────────────────────────────────────────

    async def create_medicine(
        self,
        hospital_id: uuid.UUID,
        payload: CreateMedicineRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> MedicineResponse:
        """Add a medicine to the catalog.

        :raises DuplicateMedicineSkuError: If the SKU is taken.
        """
        if await self._medicines.get_medicine_by_sku(hospital_id, payload.sku) is not None:
            raise DuplicateMedicineSkuError(payload.sku)

        values = payload.model_dump()
        try:
            async with self._session.begin_nested():
                medicine = await self._medicines.create_medicine(
                    hospital_id=hospital_id, created_by=actor_id, **values
                )
        except IntegrityError as exc:
            if "uq_medicines_hospital_sku" in str(getattr(exc, "orig", exc)):
                raise DuplicateMedicineSkuError(payload.sku) from exc
            raise

        await self._audit.record(
            AuditEvent(
                action="pharmacy.medicine.created",
                hospital_id=hospital_id,
                target_type="medicine",
                target_id=medicine.id,
                actor_id=actor_id,
                changes={
                    name: {"before": None, "after": audit_value(value)}
                    for name, value in values.items()
                },
            )
        )
        await self._session.commit()
        logger.info(
            "pharmacy.medicine.created",
            hospital_id=str(hospital_id),
            medicine_id=str(medicine.id),
            sku=medicine.sku,
        )
        return MedicineResponse.from_model(medicine)

    async def update_medicine(
        self,
        hospital_id: uuid.UUID,
        medicine_id: uuid.UUID,
        payload: UpdateMedicineRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> MedicineResponse:
        """Apply a partial update to a medicine.

        Changing the price affects future dispenses only: a dispense line
        keeps the price it was made at.

        :raises MedicineNotFoundError: If absent from this tenant.
        """
        medicine = await self._medicine_or_raise(hospital_id, medicine_id)

        requested = payload.model_dump(exclude_unset=True)
        changes = {
            name: {"before": audit_value(getattr(medicine, name)), "after": audit_value(value)}
            for name, value in requested.items()
            if getattr(medicine, name) != value
        }
        if not changes:
            return MedicineResponse.from_model(medicine)

        medicine = await self._medicines.update_medicine(
            medicine, updated_by=actor_id, **{name: requested[name] for name in changes}
        )
        await self._audit.record(
            AuditEvent(
                action="pharmacy.medicine.updated",
                hospital_id=hospital_id,
                target_type="medicine",
                target_id=medicine.id,
                actor_id=actor_id,
                changes=changes,
            )
        )
        await self._session.commit()
        logger.info(
            "pharmacy.medicine.updated",
            hospital_id=str(hospital_id),
            medicine_id=str(medicine.id),
            changed_fields=sorted(changes),
        )
        return MedicineResponse.from_model(medicine)

    async def get_medicine(
        self, hospital_id: uuid.UUID, medicine_id: uuid.UUID
    ) -> MedicineResponse:
        """Retrieve one medicine.

        :raises MedicineNotFoundError: If absent from this tenant.
        """
        return MedicineResponse.from_model(await self._medicine_or_raise(hospital_id, medicine_id))

    async def list_medicines(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        term: str | None = None,
        is_active: bool | None = None,
    ) -> Page[MedicineResponse]:
        """List medicines, ordered by name.

        :param term: Prefix of the name or generic name, or an exact SKU.
        :param is_active: Filter on the active flag. ``None`` returns both.
        """
        page_params = pagination or PaginationParams()
        filters: dict[str, Any] = {"term": term, "is_active": is_active}
        rows = await self._medicines.list_medicines(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._medicines.count_medicines(hospital_id, **filters)
        return Page[MedicineResponse](
            items=[MedicineResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Batches ───────────────────────────────────────────────────────────────

    async def receive_batch(
        self,
        hospital_id: uuid.UUID,
        medicine_id: uuid.UUID,
        payload: ReceiveBatchRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> BatchResponse:
        """Take stock in directly, without a purchase order.

        A batch number the medicine already has is topped up, provided the
        expiry date agrees — one batch has one expiry.

        :raises MedicineNotFoundError: If absent from this tenant.
        :raises BusinessRuleError: If the medicine is inactive.
        :raises ValidationError: If the batch has already expired (§11), or
            its expiry disagrees with the existing batch of that number.
        """
        medicine = await self._medicine_or_raise(hospital_id, medicine_id)
        if not medicine.is_active:
            msg = f"Medicine '{medicine.sku}' is inactive; stock cannot be received for it."
            raise BusinessRuleError(msg)
        today = await hospital_today(self._hospitals, hospital_id)
        if payload.expiry_date < today:
            raise field_error("expiry_date", "A batch that has already expired cannot be received.")

        batch = await self._medicines.get_batch_by_number(
            hospital_id, medicine.id, payload.batch_number
        )
        if batch is None:
            batch = await self._medicines.create_batch(
                medicine=medicine,
                batch_number=payload.batch_number,
                expiry_date=payload.expiry_date,
                cost_per_unit=payload.cost_per_unit,
                created_by=actor_id,
            )
        elif batch.expiry_date != payload.expiry_date:
            raise field_error(
                "expiry_date",
                f"Batch {batch.batch_number} is already recorded as expiring "
                f"{batch.expiry_date.isoformat()}.",
            )

        await self._medicines.apply_movement(
            batch,
            quantity_change=payload.quantity,
            reason=StockMovementReason.RECEIVED,
            moved_at=datetime.now(UTC),
            moved_by=actor_id,
            reference_type="direct_receipt",
        )
        await self._audit.record(
            AuditEvent(
                action="pharmacy.batch.received",
                hospital_id=hospital_id,
                target_type="medicine_batch",
                target_id=batch.id,
                actor_id=actor_id,
                context={
                    "medicine": medicine.sku,
                    "batch_number": batch.batch_number,
                    "quantity": payload.quantity,
                    "quantity_on_hand": batch.quantity_on_hand,
                },
            )
        )
        await self._session.commit()
        logger.info(
            "pharmacy.batch.received",
            hospital_id=str(hospital_id),
            batch_id=str(batch.id),
            quantity=payload.quantity,
        )
        return BatchResponse.from_model(batch, today=today)

    async def update_batch(
        self,
        hospital_id: uuid.UUID,
        medicine_id: uuid.UUID,
        batch_id: uuid.UUID,
        payload: UpdateBatchRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> BatchResponse:
        """Recall a batch, or lift a recall (module spec §14).

        :raises BatchNotFoundError: If absent, or not a batch of this medicine.
        """
        batch = await self._batch_or_raise(hospital_id, medicine_id, batch_id)
        today = await hospital_today(self._hospitals, hospital_id)
        if batch.is_recalled == payload.is_recalled:
            return BatchResponse.from_model(batch, today=today)

        batch = await self._medicines.update_batch(
            batch, updated_by=actor_id, is_recalled=payload.is_recalled
        )
        await self._audit.record(
            AuditEvent(
                action="pharmacy.batch.recalled"
                if batch.is_recalled
                else "pharmacy.batch.released",
                hospital_id=hospital_id,
                target_type="medicine_batch",
                target_id=batch.id,
                actor_id=actor_id,
                changes={
                    "is_recalled": {"before": not batch.is_recalled, "after": batch.is_recalled}
                },
                context={"quantity_on_hand": batch.quantity_on_hand},
            )
        )
        await self._session.commit()
        logger.info(
            "pharmacy.batch.recall_changed",
            hospital_id=str(hospital_id),
            batch_id=str(batch.id),
            is_recalled=batch.is_recalled,
        )
        return BatchResponse.from_model(batch, today=today)

    async def adjust_stock(
        self,
        hospital_id: uuid.UUID,
        medicine_id: uuid.UUID,
        batch_id: uuid.UUID,
        payload: AdjustStockRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> BatchResponse:
        """Correct a batch's count, or write off expired stock.

        The change is a ledger movement with the reason given; the count is
        never overwritten.

        :raises BatchNotFoundError: If absent, or not a batch of this medicine.
        :raises BusinessRuleError: If it would take the batch below zero.
        """
        batch = await self._batch_or_raise(hospital_id, medicine_id, batch_id, for_update=True)
        if batch.quantity_on_hand + payload.quantity_change < 0:
            msg = (
                f"Batch {batch.batch_number} holds {batch.quantity_on_hand} units; "
                f"{-payload.quantity_change} cannot be removed."
            )
            raise BusinessRuleError(msg, detail={"quantity_on_hand": batch.quantity_on_hand})

        before = batch.quantity_on_hand
        await self._medicines.apply_movement(
            batch,
            quantity_change=payload.quantity_change,
            reason=ADJUSTMENT_REASONS[payload.reason],
            moved_at=datetime.now(UTC),
            moved_by=actor_id,
            reference_type="adjustment",
            note=payload.note,
        )
        await self._audit.record(
            AuditEvent(
                action="pharmacy.batch.adjusted",
                hospital_id=hospital_id,
                target_type="medicine_batch",
                target_id=batch.id,
                actor_id=actor_id,
                changes={"quantity_on_hand": {"before": before, "after": batch.quantity_on_hand}},
                context={"reason": payload.reason, "note": payload.note},
            )
        )
        await self._session.commit()
        logger.info(
            "pharmacy.batch.adjusted",
            hospital_id=str(hospital_id),
            batch_id=str(batch.id),
            quantity_change=payload.quantity_change,
            reason=payload.reason,
        )
        today = await hospital_today(self._hospitals, hospital_id)
        return BatchResponse.from_model(batch, today=today)

    async def list_batches(
        self, hospital_id: uuid.UUID, medicine_id: uuid.UUID, *, in_stock_only: bool = False
    ) -> list[BatchResponse]:
        """List a medicine's batches, earliest expiry first.

        :raises MedicineNotFoundError: If absent from this tenant.
        """
        await self._medicine_or_raise(hospital_id, medicine_id)
        today = await hospital_today(self._hospitals, hospital_id)
        batches = await self._medicines.list_batches(
            hospital_id, medicine_id, in_stock_only=in_stock_only
        )
        return [BatchResponse.from_model(batch, today=today) for batch in batches]

    async def get_stock(
        self, hospital_id: uuid.UUID, medicine_id: uuid.UUID
    ) -> MedicineStockResponse:
        """Return a medicine's stock position (module spec §9).

        :raises MedicineNotFoundError: If absent from this tenant.
        """
        medicine = await self._medicine_or_raise(hospital_id, medicine_id)
        today = await hospital_today(self._hospitals, hospital_id)
        batches = [
            BatchResponse.from_model(batch, today=today)
            for batch in await self._medicines.list_batches(hospital_id, medicine_id)
        ]
        held = [batch for batch in batches if batch.quantity_on_hand > 0]
        return MedicineStockResponse(
            medicine=MedicineResponse.from_model(medicine),
            quantity_on_hand=sum(batch.quantity_on_hand for batch in held),
            dispensable_quantity=sum(b.quantity_on_hand for b in held if b.is_dispensable),
            expiring_soon_quantity=sum(
                b.quantity_on_hand
                for b in held
                if b.is_dispensable and b.days_to_expiry <= EXPIRY_WARNING_DAYS
            ),
            expired_quantity=sum(b.quantity_on_hand for b in held if b.is_expired),
            recalled_quantity=sum(b.quantity_on_hand for b in held if b.is_recalled),
            batches=batches,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _medicine_or_raise(self, hospital_id: uuid.UUID, medicine_id: uuid.UUID) -> Medicine:
        """Fetch a medicine or raise :class:`MedicineNotFoundError`."""
        medicine = await self._medicines.get_medicine_by_id(hospital_id, medicine_id)
        if medicine is None:
            raise MedicineNotFoundError(medicine_id)
        return medicine

    async def _batch_or_raise(
        self,
        hospital_id: uuid.UUID,
        medicine_id: uuid.UUID,
        batch_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> MedicineBatch:
        """Fetch a batch of a given medicine or raise :class:`BatchNotFoundError`."""
        batch = await self._medicines.get_batch_by_id(hospital_id, batch_id, for_update=for_update)
        if batch is None or batch.medicine_id != medicine_id:
            raise BatchNotFoundError(batch_id)
        return batch
