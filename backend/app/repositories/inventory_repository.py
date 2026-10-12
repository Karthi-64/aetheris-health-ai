"""Repository for inventory items, locations, stock, and the movement ledger.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it, or acts on a row already loaded through one that did
(CLAUDE.md rules 4 and 5).

**Stock changes in exactly one place.**
:meth:`InventoryRepository.apply_movement` writes the ledger row and changes
the stock row's total by a relative SQL update, together. The total can never
drift from the sum of its movements (AC-4), and two transactions changing one
row add up instead of overwriting each other.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.inventory import (
    InventoryItem,
    InventoryLocation,
    InventoryMovement,
    InventoryMovementReason,
    InventoryStock,
)
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession

_ZERO = Decimal("0")


class InventoryRepository(BaseRepository[InventoryItem]):
    """Persistence for inventory items, locations and stock.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(InventoryItem, session)

    # ── Items ─────────────────────────────────────────────────────────────────

    def _items(self, hospital_id: uuid.UUID) -> Select[tuple[InventoryItem]]:
        """Return a base SELECT of items filtered to one hospital."""
        return self._query().where(InventoryItem.hospital_id == hospital_id)

    @staticmethod
    def _apply_item_filters(
        stmt: Select[tuple[InventoryItem]],
        *,
        term: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
    ) -> Select[tuple[InventoryItem]]:
        """Apply the filters shared by list and count.

        ``term`` is a case-insensitive **prefix** match on name and an exact
        match on SKU.
        """
        if term:
            # ``escape`` is set so a term containing % or _ is matched literally.
            prefix = (
                term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            stmt = stmt.where(
                or_(
                    func.lower(InventoryItem.name).like(prefix, escape="\\"),
                    InventoryItem.sku == term.upper(),
                )
            )
        if category is not None:
            stmt = stmt.where(InventoryItem.category == category)
        if is_active is not None:
            stmt = stmt.where(InventoryItem.is_active.is_(is_active))
        return stmt

    async def create_item(
        self,
        *,
        hospital_id: uuid.UUID,
        sku: str,
        name: str,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> InventoryItem:
        """Insert an item. Does not commit.

        :param hospital_id: Owning tenant.
        :param sku: Stock-keeping code, already uppercased.
        :param name: Display name.
        :param created_by: UUID of the acting user.
        :param optional_fields: Remaining columns.
        :returns: The persisted item.
        """
        return await super().create(
            hospital_id=hospital_id, sku=sku, name=name, created_by=created_by, **optional_fields
        )

    async def update_item(
        self, item: InventoryItem, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> InventoryItem:
        """Apply field updates to an item."""
        return await self.update(item, updated_by=updated_by, **fields)

    async def get_item_by_id(
        self, hospital_id: uuid.UUID, item_id: uuid.UUID
    ) -> InventoryItem | None:
        """Retrieve one item by UUID within a hospital.

        :returns: The item, or ``None`` if absent or in another tenant.
        """
        result = await self._session.execute(
            self._items(hospital_id).where(InventoryItem.id == item_id)
        )
        return result.unique().scalar_one_or_none()

    async def get_items_by_ids(
        self, hospital_id: uuid.UUID, item_ids: Sequence[uuid.UUID]
    ) -> list[InventoryItem]:
        """Retrieve several items in one query. Ids from another tenant are absent."""
        if not item_ids:
            return []
        result = await self._session.execute(
            self._items(hospital_id).where(InventoryItem.id.in_(item_ids))
        )
        return list(result.unique().scalars().all())

    async def get_item_by_sku(self, hospital_id: uuid.UUID, sku: str) -> InventoryItem | None:
        """Retrieve an item by its SKU within a hospital."""
        result = await self._session.execute(
            self._items(hospital_id).where(InventoryItem.sku == sku)
        )
        return result.unique().scalar_one_or_none()

    async def list_items(
        self, hospital_id: uuid.UUID, *, skip: int = 0, limit: int = 25, **filters: Any
    ) -> list[InventoryItem]:
        """List items in a hospital, ordered by name."""
        stmt = self._apply_item_filters(self._items(hospital_id), **filters)
        stmt = self._apply_pagination(
            stmt.order_by(InventoryItem.name.asc(), InventoryItem.id.asc()), skip=skip, limit=limit
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_items(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count items matching the filters :meth:`list_items` uses."""
        stmt = self._apply_item_filters(self._items(hospital_id), **filters)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()

    # ── Locations ─────────────────────────────────────────────────────────────

    @staticmethod
    def _locations(
        hospital_id: uuid.UUID, *, is_active: bool | None = None
    ) -> Select[tuple[InventoryLocation]]:
        """Return a SELECT of live locations filtered to one hospital."""
        stmt = select(InventoryLocation).where(
            InventoryLocation.hospital_id == hospital_id, InventoryLocation.deleted_at.is_(None)
        )
        if is_active is not None:
            stmt = stmt.where(InventoryLocation.is_active.is_(is_active))
        return stmt

    async def create_location(
        self,
        *,
        hospital_id: uuid.UUID,
        name: str,
        code: str,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> InventoryLocation:
        """Insert a location. Does not commit."""
        location = InventoryLocation(
            hospital_id=hospital_id, name=name, code=code, created_by=created_by, **optional_fields
        )
        self._session.add(location)
        await self._session.flush()
        await self._session.refresh(location)
        return location

    async def update_location(
        self, location: InventoryLocation, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> InventoryLocation:
        """Apply field updates to a location."""
        for name, value in fields.items():
            setattr(location, name, value)
        location.updated_by = updated_by
        await self._session.flush()
        await self._session.refresh(location)
        return location

    async def get_location_by_id(
        self, hospital_id: uuid.UUID, location_id: uuid.UUID
    ) -> InventoryLocation | None:
        """Retrieve one location by UUID within a hospital.

        :returns: The location, or ``None`` if absent or in another tenant.
        """
        result = await self._session.execute(
            self._locations(hospital_id).where(InventoryLocation.id == location_id)
        )
        return result.scalar_one_or_none()

    async def get_location_by_code(
        self, hospital_id: uuid.UUID, code: str
    ) -> InventoryLocation | None:
        """Retrieve a location by its code within a hospital."""
        result = await self._session.execute(
            self._locations(hospital_id).where(InventoryLocation.code == code)
        )
        return result.scalar_one_or_none()

    async def list_locations(
        self, hospital_id: uuid.UUID, *, is_active: bool | None = None
    ) -> list[InventoryLocation]:
        """List a hospital's locations, ordered by name."""
        stmt = self._locations(hospital_id, is_active=is_active).order_by(
            InventoryLocation.name.asc(), InventoryLocation.id.asc()
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # ── Stock ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _stock(
        hospital_id: uuid.UUID,
        *,
        item_id: uuid.UUID | None = None,
        location_id: uuid.UUID | None = None,
        in_stock_only: bool = False,
    ) -> Select[tuple[InventoryStock]]:
        """Return a SELECT of live stock rows filtered to one hospital."""
        stmt = select(InventoryStock).where(
            InventoryStock.hospital_id == hospital_id, InventoryStock.deleted_at.is_(None)
        )
        if item_id is not None:
            stmt = stmt.where(InventoryStock.item_id == item_id)
        if location_id is not None:
            stmt = stmt.where(InventoryStock.location_id == location_id)
        if in_stock_only:
            stmt = stmt.where(InventoryStock.quantity > 0)
        return stmt

    async def get_or_create_stock(
        self,
        *,
        item: InventoryItem,
        location: InventoryLocation,
        batch_number: str | None = None,
        expiry_date: date | None = None,
        created_by: uuid.UUID | None = None,
    ) -> InventoryStock:
        """Return the stock row for a batch of an item at a location, creating it empty.

        ``INSERT … ON CONFLICT DO NOTHING`` against the unique index, then a
        read: two requests receiving the same new batch at once both end up
        with the one row rather than one of them failing.

        :param item: The item. Supplies the tenant.
        :param location: Where it is held.
        :param batch_number: Batch number; ``None`` for an untracked item.
        :param expiry_date: Expiry to record if the row is created.
        :param created_by: UUID of the acting user.
        :returns: The stock row, existing or new.
        """
        await self._session.execute(
            pg_insert(InventoryStock)
            .values(
                hospital_id=item.hospital_id,
                item_id=item.id,
                location_id=location.id,
                batch_number=batch_number,
                expiry_date=expiry_date,
                quantity=_ZERO,
                created_by=created_by,
            )
            .on_conflict_do_nothing()
        )
        stmt = (
            self._stock(item.hospital_id, item_id=item.id, location_id=location.id)
            .where(func.coalesce(InventoryStock.batch_number, "") == (batch_number or ""))
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one()

    async def apply_movement(
        self,
        stock: InventoryStock,
        *,
        quantity_change: Decimal,
        reason: InventoryMovementReason,
        moved_at: datetime,
        moved_by: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
        reference_type: str | None = None,
        reference_id: uuid.UUID | None = None,
        note: str | None = None,
    ) -> InventoryMovement:
        """Change a stock row: one ledger row and the running total, together.

        The total is changed by a relative UPDATE, so concurrent movements on
        one row add up rather than overwrite each other. The database refuses
        a total below zero (§11), so a movement that would overdraw the row
        raises ``IntegrityError`` here.

        :param stock: The attached stock row.
        :param quantity_change: Units added (positive) or removed (negative).
        :param reason: Why the stock changed.
        :param moved_at: When it changed (UTC).
        :param moved_by: UUID of the acting user.
        :param department_id: Department that consumed it.
        :param reference_type: What caused it.
        :param reference_id: UUID of what caused it.
        :param note: Free-text reason, for an adjustment.
        :returns: The persisted movement.
        """
        movement = InventoryMovement(
            hospital_id=stock.hospital_id,
            item_id=stock.item_id,
            location_id=stock.location_id,
            stock_id=stock.id,
            quantity_change=quantity_change,
            reason=reason,
            department_id=department_id,
            reference_type=reference_type,
            reference_id=reference_id,
            note=note,
            moved_at=moved_at,
            moved_by=moved_by,
            created_by=moved_by,
        )
        self._session.add(movement)
        await self._session.execute(
            update(InventoryStock)
            .where(InventoryStock.id == stock.id)
            .values(quantity=InventoryStock.quantity + quantity_change, updated_by=moved_by)
        )
        await self._session.flush()
        await self._session.refresh(stock)
        return movement

    async def lock_usable_stock(
        self,
        hospital_id: uuid.UUID,
        item_id: uuid.UUID,
        location_id: uuid.UUID,
        *,
        on: date,
        batch_number: str | None = None,
    ) -> list[InventoryStock]:
        """Lock and return the stock of an item at a location that can be used.

        Nothing empty and nothing expired (§14). Earliest expiry first, with
        stock that has no expiry last, so what will be wasted soonest is used
        first. ``FOR UPDATE`` so two requests drawing on the same stock take
        turns; always ordered the same way, so they cannot deadlock. The
        caller **must** be inside a transaction.

        :param hospital_id: The tenant to scope to.
        :param item_id: The item wanted.
        :param location_id: Where to take it from.
        :param on: Today, in the hospital's timezone.
        :param batch_number: Only this batch.
        :returns: Usable stock rows, locked.
        """
        stmt = self._stock(
            hospital_id, item_id=item_id, location_id=location_id, in_stock_only=True
        ).where(or_(InventoryStock.expiry_date.is_(None), InventoryStock.expiry_date >= on))
        if batch_number is not None:
            stmt = stmt.where(InventoryStock.batch_number == batch_number)
        stmt = (
            stmt.order_by(InventoryStock.expiry_date.asc().nulls_last(), InventoryStock.id.asc())
            .with_for_update(of=InventoryStock)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def list_stock(
        self, hospital_id: uuid.UUID, *, skip: int = 0, limit: int = 25, **filters: Any
    ) -> list[InventoryStock]:
        """List stock rows, by item name, location name and expiry.

        :param filters: ``item_id``, ``location_id``, ``in_stock_only``.
        """
        stmt = (
            self._stock(hospital_id, **filters)
            .join(InventoryItem, InventoryItem.id == InventoryStock.item_id)
            .join(InventoryLocation, InventoryLocation.id == InventoryStock.location_id)
            .order_by(
                InventoryItem.name.asc(),
                InventoryLocation.name.asc(),
                InventoryStock.expiry_date.asc().nulls_last(),
                InventoryStock.id.asc(),
            )
            .offset(skip)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_stock(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count stock rows matching the filters :meth:`list_stock` uses."""
        stmt = self._stock(hospital_id, **filters)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()

    async def totals_by_item(
        self, hospital_id: uuid.UUID, *, on: date, item_ids: Sequence[uuid.UUID] | None = None
    ) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
        """Return each item's hospital-wide stock: what is held, and what is usable.

        :param hospital_id: The tenant to scope to.
        :param on: Today, in the hospital's timezone; expired stock is held
            but not usable.
        :param item_ids: Only these items. ``None`` for every item with stock.
        :returns: Item id to ``(on_hand, usable)``. An item with no stock rows
            is absent.
        """
        usable = case(
            (
                or_(InventoryStock.expiry_date.is_(None), InventoryStock.expiry_date >= on),
                InventoryStock.quantity,
            ),
            else_=_ZERO,
        )
        stmt = (
            select(InventoryStock.item_id, func.sum(InventoryStock.quantity), func.sum(usable))
            .where(InventoryStock.hospital_id == hospital_id, InventoryStock.deleted_at.is_(None))
            .group_by(InventoryStock.item_id)
        )
        if item_ids is not None:
            if not item_ids:
                return {}
            stmt = stmt.where(InventoryStock.item_id.in_(item_ids))
        result = await self._session.execute(stmt)
        return {
            item_id: (Decimal(on_hand), Decimal(available))
            for item_id, on_hand, available in result.all()
        }

    # ── Movements ─────────────────────────────────────────────────────────────

    @staticmethod
    def _movements(
        hospital_id: uuid.UUID,
        *,
        item_id: uuid.UUID | None = None,
        location_id: uuid.UUID | None = None,
        reason: InventoryMovementReason | None = None,
    ) -> Select[tuple[InventoryMovement]]:
        """Return a SELECT of movements filtered to one hospital."""
        stmt = select(InventoryMovement).where(InventoryMovement.hospital_id == hospital_id)
        if item_id is not None:
            stmt = stmt.where(InventoryMovement.item_id == item_id)
        if location_id is not None:
            stmt = stmt.where(InventoryMovement.location_id == location_id)
        if reason is not None:
            stmt = stmt.where(InventoryMovement.reason == reason)
        return stmt

    async def list_movements(
        self, hospital_id: uuid.UUID, *, skip: int = 0, limit: int = 25, **filters: Any
    ) -> list[InventoryMovement]:
        """List the ledger, newest first.

        :param filters: ``item_id``, ``location_id``, ``reason``.
        """
        stmt = (
            self._movements(hospital_id, **filters)
            .order_by(InventoryMovement.moved_at.desc(), InventoryMovement.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_movements(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count movements matching the filters :meth:`list_movements` uses."""
        stmt = self._movements(hospital_id, **filters)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()
