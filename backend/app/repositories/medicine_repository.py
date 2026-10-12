"""Repository for medicines, their batches, and the stock ledger.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it, or acts on a row already loaded through one that did
(CLAUDE.md rules 4 and 5).

**Stock changes in exactly one place.** :meth:`MedicineRepository.apply_movement`
writes the ledger row and the batch's running total together, so
``quantity_on_hand`` can never drift from the sum of its movements. Nothing
else assigns that column.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, or_, select, update

from app.models.pharmacy import Medicine, MedicineBatch, StockMovement, StockMovementReason
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession


class MedicineRepository(BaseRepository[Medicine]):
    """Persistence for the medicine catalog and its stock.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Medicine, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _scoped(self, hospital_id: uuid.UUID) -> Select[tuple[Medicine]]:
        """Return a base SELECT of medicines filtered to one hospital."""
        return self._query().where(Medicine.hospital_id == hospital_id)

    @staticmethod
    def _batches(hospital_id: uuid.UUID) -> Select[tuple[MedicineBatch]]:
        """Return a base SELECT of live batches filtered to one hospital."""
        return select(MedicineBatch).where(
            MedicineBatch.hospital_id == hospital_id, MedicineBatch.deleted_at.is_(None)
        )

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[Medicine]], *, term: str | None = None, is_active: bool | None = None
    ) -> Select[tuple[Medicine]]:
        """Apply the filters shared by list and count.

        ``term`` is a case-insensitive **prefix** match on the name and the
        generic name, and an exact match on the SKU.

        :param stmt: The statement to extend.
        :param term: Free-text search term.
        :param is_active: Filter on the active flag. ``None`` returns both.
        :returns: The statement with predicates applied.
        """
        if term:
            # ``escape`` is set so a term containing % or _ is matched
            # literally rather than acting as a wildcard.
            prefix = (
                term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            stmt = stmt.where(
                or_(
                    func.lower(Medicine.name).like(prefix, escape="\\"),
                    func.lower(Medicine.generic_name).like(prefix, escape="\\"),
                    Medicine.sku == term.upper(),
                )
            )
        if is_active is not None:
            stmt = stmt.where(Medicine.is_active.is_(is_active))
        return stmt

    # ── Medicines ─────────────────────────────────────────────────────────────

    async def create_medicine(
        self,
        *,
        hospital_id: uuid.UUID,
        sku: str,
        name: str,
        unit_price: Decimal,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> Medicine:
        """Insert a medicine. Does not commit.

        :param hospital_id: Owning tenant.
        :param sku: Stock-keeping code, already uppercased.
        :param name: Brand or trade name.
        :param unit_price: Selling price per unit.
        :param created_by: UUID of the acting user.
        :param optional_fields: Remaining columns.
        :returns: The persisted medicine.
        """
        return await super().create(
            hospital_id=hospital_id,
            sku=sku,
            name=name,
            unit_price=unit_price,
            created_by=created_by,
            **optional_fields,
        )

    async def update_medicine(
        self, medicine: Medicine, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> Medicine:
        """Apply field updates to a medicine.

        :param medicine: The attached ORM instance to modify.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated medicine.
        """
        return await self.update(medicine, updated_by=updated_by, **fields)

    async def get_medicine_by_id(
        self, hospital_id: uuid.UUID, medicine_id: uuid.UUID
    ) -> Medicine | None:
        """Retrieve one medicine by UUID within a hospital.

        :returns: The medicine, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id).where(Medicine.id == medicine_id)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_medicines_by_ids(
        self, hospital_id: uuid.UUID, medicine_ids: Sequence[uuid.UUID]
    ) -> list[Medicine]:
        """Retrieve several medicines in one query.

        :returns: The medicines found. Ids from another tenant are simply absent.
        """
        if not medicine_ids:
            return []
        stmt = self._scoped(hospital_id).where(Medicine.id.in_(medicine_ids))
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def get_medicine_by_sku(self, hospital_id: uuid.UUID, sku: str) -> Medicine | None:
        """Retrieve a medicine by its SKU within a hospital.

        :param sku: The SKU, already uppercased.
        """
        stmt = self._scoped(hospital_id).where(Medicine.sku == sku)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_medicines(
        self, hospital_id: uuid.UUID, *, skip: int = 0, limit: int = 25, **filters: Any
    ) -> list[Medicine]:
        """List medicines in a hospital, ordered by name.

        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        """
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        stmt = self._apply_pagination(
            stmt.order_by(Medicine.name.asc(), Medicine.id.asc()), skip=skip, limit=limit
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_medicines(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count medicines matching the filters :meth:`list_medicines` uses."""
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()

    # ── Batches ───────────────────────────────────────────────────────────────

    async def create_batch(
        self,
        *,
        medicine: Medicine,
        batch_number: str,
        expiry_date: date,
        cost_per_unit: Decimal,
        created_by: uuid.UUID | None = None,
    ) -> MedicineBatch:
        """Insert an empty batch. Stock arrives through :meth:`apply_movement`.

        :param medicine: The medicine the batch is of. Supplies the tenant.
        :param batch_number: Manufacturer's batch number.
        :param expiry_date: Last day the batch may be dispensed.
        :param cost_per_unit: Purchase cost per unit.
        :param created_by: UUID of the acting user.
        :returns: The persisted batch, holding nothing yet.
        """
        batch = MedicineBatch(
            hospital_id=medicine.hospital_id,
            medicine_id=medicine.id,
            batch_number=batch_number,
            expiry_date=expiry_date,
            cost_per_unit=cost_per_unit,
            initial_quantity=0,
            quantity_on_hand=0,
            created_by=created_by,
        )
        self._session.add(batch)
        await self._session.flush()
        await self._session.refresh(batch)
        return batch

    async def update_batch(
        self, batch: MedicineBatch, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> MedicineBatch:
        """Apply field updates to a batch. Never used for the quantity.

        :param batch: The attached batch.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated batch.
        """
        for name, value in fields.items():
            setattr(batch, name, value)
        batch.updated_by = updated_by
        await self._session.flush()
        await self._session.refresh(batch)
        return batch

    async def apply_movement(
        self,
        batch: MedicineBatch,
        *,
        quantity_change: int,
        reason: StockMovementReason,
        moved_at: datetime,
        moved_by: uuid.UUID | None = None,
        reference_type: str | None = None,
        reference_id: uuid.UUID | None = None,
        note: str | None = None,
    ) -> StockMovement:
        """Change a batch's stock: one ledger row and the running total, together.

        The total is changed by a relative UPDATE, so concurrent movements on
        one batch add up rather than overwrite each other. The database refuses
        a total below zero, so a movement that would overdraw the batch raises
        ``IntegrityError`` here.

        :param batch: The attached batch, locked by the caller when removing.
        :param quantity_change: Units added (positive) or removed (negative).
        :param reason: Why the stock changed.
        :param moved_at: When it changed (UTC).
        :param moved_by: UUID of the acting user.
        :param reference_type: What caused it, e.g. ``dispense``.
        :param reference_id: UUID of what caused it.
        :param note: Free-text reason, for an adjustment.
        :returns: The persisted movement.
        """
        movement = StockMovement(
            hospital_id=batch.hospital_id,
            batch_id=batch.id,
            quantity_change=quantity_change,
            reason=reason,
            reference_type=reference_type,
            reference_id=reference_id,
            note=note,
            moved_at=moved_at,
            moved_by=moved_by,
            created_by=moved_by,
        )
        self._session.add(movement)
        # ``quantity_on_hand = quantity_on_hand + :change`` in SQL, not a value
        # computed here from what this session last read. The UPDATE waits for
        # any other transaction touching the row and then adds to what that
        # transaction left, so a receipt and a dispense of the same batch can
        # never overwrite one another — whether or not the caller took a lock.
        received = quantity_change if reason is StockMovementReason.RECEIVED else 0
        await self._session.execute(
            update(MedicineBatch)
            .where(MedicineBatch.id == batch.id)
            .values(
                quantity_on_hand=MedicineBatch.quantity_on_hand + quantity_change,
                initial_quantity=MedicineBatch.initial_quantity + received,
                updated_by=moved_by,
            )
        )
        await self._session.flush()
        await self._session.refresh(batch)
        return movement

    async def get_batch_by_id(
        self, hospital_id: uuid.UUID, batch_id: uuid.UUID, *, for_update: bool = False
    ) -> MedicineBatch | None:
        """Retrieve one batch by UUID within a hospital.

        :param for_update: Lock the row until the transaction ends.
        :returns: The batch, or ``None`` if absent or in another tenant.
        """
        stmt = self._batches(hospital_id).where(MedicineBatch.id == batch_id)
        if for_update:
            stmt = stmt.with_for_update(of=MedicineBatch).execution_options(populate_existing=True)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_batch_by_number(
        self, hospital_id: uuid.UUID, medicine_id: uuid.UUID, batch_number: str
    ) -> MedicineBatch | None:
        """Retrieve a medicine's batch by its batch number."""
        stmt = self._batches(hospital_id).where(
            MedicineBatch.medicine_id == medicine_id, MedicineBatch.batch_number == batch_number
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_batches(
        self, hospital_id: uuid.UUID, medicine_id: uuid.UUID, *, in_stock_only: bool = False
    ) -> list[MedicineBatch]:
        """List a medicine's batches, earliest expiry first.

        :param in_stock_only: Leave out batches holding nothing.
        """
        stmt = self._batches(hospital_id).where(MedicineBatch.medicine_id == medicine_id)
        if in_stock_only:
            stmt = stmt.where(MedicineBatch.quantity_on_hand > 0)
        stmt = stmt.order_by(MedicineBatch.expiry_date.asc(), MedicineBatch.id.asc())
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def lock_dispensable_batches(
        self, hospital_id: uuid.UUID, medicine_id: uuid.UUID, *, on: date
    ) -> list[MedicineBatch]:
        """Lock and return the batches a medicine can be dispensed from.

        Business rules 2 and 4: earliest expiry first; nothing expired,
        recalled or empty. A batch expiring ``on`` the day itself is still
        dispensable (§14).

        ``FOR UPDATE`` so that two pharmacists dispensing the same medicine
        take turns; ``of=MedicineBatch`` because the joined medicine load makes
        this a join. Always ordered the same way, so two dispenses cannot lock
        the same batches in opposite orders and deadlock. The caller **must**
        be inside a transaction.

        :param hospital_id: The tenant to scope to.
        :param medicine_id: The medicine wanted.
        :param on: Today, in the hospital's timezone.
        :returns: Dispensable batches, earliest expiry first, locked.
        """
        stmt = (
            self._batches(hospital_id)
            .where(
                MedicineBatch.medicine_id == medicine_id,
                MedicineBatch.quantity_on_hand > 0,
                MedicineBatch.expiry_date >= on,
                MedicineBatch.is_recalled.is_(False),
            )
            .order_by(MedicineBatch.expiry_date.asc(), MedicineBatch.id.asc())
            .with_for_update(of=MedicineBatch)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def dispensable_totals(
        self, hospital_id: uuid.UUID, medicine_ids: Sequence[uuid.UUID], *, on: date
    ) -> dict[uuid.UUID, int]:
        """Return how many units of each medicine can be dispensed today.

        :param hospital_id: The tenant to scope to.
        :param medicine_ids: The medicines wanted.
        :param on: Today, in the hospital's timezone.
        :returns: Medicine id to dispensable units; a medicine with none is absent.
        """
        if not medicine_ids:
            return {}
        stmt = (
            select(MedicineBatch.medicine_id, func.sum(MedicineBatch.quantity_on_hand))
            .where(
                MedicineBatch.hospital_id == hospital_id,
                MedicineBatch.deleted_at.is_(None),
                MedicineBatch.medicine_id.in_(medicine_ids),
                MedicineBatch.quantity_on_hand > 0,
                MedicineBatch.expiry_date >= on,
                MedicineBatch.is_recalled.is_(False),
            )
            .group_by(MedicineBatch.medicine_id)
        )
        result = await self._session.execute(stmt)
        return {medicine_id: int(total) for medicine_id, total in result.all()}

    async def list_movements(
        self, hospital_id: uuid.UUID, batch_id: uuid.UUID
    ) -> list[StockMovement]:
        """Return a batch's ledger, oldest first."""
        stmt = (
            select(StockMovement)
            .where(StockMovement.hospital_id == hospital_id, StockMovement.batch_id == batch_id)
            .order_by(StockMovement.moved_at.asc(), StockMovement.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
