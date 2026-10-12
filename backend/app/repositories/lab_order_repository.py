"""Repository for lab orders, their items, and result amendments.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it (CLAUDE.md rules 4 and 5).

An order is the aggregate root: items and amendments are only ever reached
through the order they belong to, so there is one repository for all three.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, select

from app.models.lab import (
    LabOrder,
    LabOrderItem,
    LabOrderPriority,
    LabOrderStatus,
    LabResultAmendment,
)
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class LabOrderRepository(BaseRepository[LabOrder]):
    """Persistence for lab orders.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(LabOrder, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _scoped(self, hospital_id: uuid.UUID) -> Select[tuple[LabOrder]]:
        """Return a base SELECT filtered to one hospital."""
        return self._query().where(LabOrder.hospital_id == hospital_id)

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[LabOrder]],
        *,
        status: LabOrderStatus | None = None,
        priority: LabOrderPriority | None = None,
        patient_id: uuid.UUID | None = None,
        doctor_id: uuid.UUID | None = None,
        appointment_id: uuid.UUID | None = None,
    ) -> Select[tuple[LabOrder]]:
        """Apply the worklist filters shared by list and count (§12).

        :param stmt: The statement to extend.
        :param status: Only orders in this status.
        :param priority: Only orders of this priority.
        :param patient_id: Only this patient's orders.
        :param doctor_id: Only orders placed by this doctor.
        :param appointment_id: Only orders raised in this visit.
        :returns: The statement with predicates applied.
        """
        if status is not None:
            stmt = stmt.where(LabOrder.status == status)
        if priority is not None:
            stmt = stmt.where(LabOrder.priority == priority)
        if patient_id is not None:
            stmt = stmt.where(LabOrder.patient_id == patient_id)
        if doctor_id is not None:
            stmt = stmt.where(LabOrder.doctor_id == doctor_id)
        if appointment_id is not None:
            stmt = stmt.where(LabOrder.appointment_id == appointment_id)
        return stmt

    async def _reload(self, order: LabOrder) -> LabOrder:
        """Re-read an order with everything a response needs loaded.

        A flush leaves server-maintained columns (``updated_at``) expired on
        every row it touched, and ``session.refresh(order)`` does not reach
        the amendments nested under its items. Reading either afterwards
        would be lazy IO, which async SQLAlchemy refuses. One
        ``populate_existing`` query brings the whole graph back instead.

        :param order: The attached order, already flushed.
        :returns: The same instance, fully loaded.
        """
        stmt = (
            select(LabOrder)
            .where(LabOrder.id == order.id)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one()

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_order(
        self,
        *,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        doctor_id: uuid.UUID,
        ordered_at: datetime,
        items: Sequence[dict[str, Any]],
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> LabOrder:
        """Insert an order together with its items.

        Does not commit — the service owns the transaction.

        :param hospital_id: Owning tenant.
        :param patient_id: Patient the tests are for.
        :param doctor_id: Ordering doctor.
        :param ordered_at: When the order was placed (UTC).
        :param items: Column values per item, in display order.
        :param created_by: UUID of the acting user.
        :param optional_fields: Remaining columns (appointment_id, priority, notes).
        :returns: The persisted order, items loaded.
        """
        order = LabOrder(
            hospital_id=hospital_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            ordered_at=ordered_at,
            status=LabOrderStatus.ORDERED,
            created_by=created_by,
            items=[
                LabOrderItem(hospital_id=hospital_id, position=position, **values)
                for position, values in enumerate(items)
            ],
            **optional_fields,
        )
        self._session.add(order)
        await self._session.flush()
        return await self._reload(order)

    async def update_order(
        self, order: LabOrder, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> LabOrder:
        """Apply field updates to an order.

        Call this last in a lifecycle step, even with no fields: it is what
        brings the order, its items and their amendments back fully loaded
        after the step's flushes (see :meth:`_reload`).

        :param order: The attached ORM instance to modify.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated order, items and amendments loaded.
        """
        for name, value in fields.items():
            setattr(order, name, value)
        order.updated_by = updated_by
        await self._session.flush()
        return await self._reload(order)

    async def update_item(
        self, item: LabOrderItem, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> None:
        """Apply field updates to one item of an order.

        Flushes inside the caller's transaction, so a clash on the per-hospital
        sample id surfaces here as an ``IntegrityError``.

        :param item: The attached item.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        """
        for name, value in fields.items():
            setattr(item, name, value)
        item.updated_by = updated_by
        await self._session.flush()

    async def add_amendment(
        self,
        item: LabOrderItem,
        *,
        new_value: str,
        reason: str,
        amended_by: uuid.UUID,
        amended_at: datetime,
        **optional_fields: Any,
    ) -> LabResultAmendment:
        """Record a correction to a released result.

        :param item: The item being corrected. Supplies the tenant.
        :param new_value: The corrected value.
        :param reason: Why it was corrected.
        :param amended_by: UUID of the acting user.
        :param amended_at: When it was corrected (UTC).
        :param optional_fields: previous_value, previous_flag, new_flag.
        :returns: The persisted amendment.
        """
        amendment = LabResultAmendment(
            hospital_id=item.hospital_id,
            item_id=item.id,
            new_value=new_value,
            reason=reason,
            amended_by=amended_by,
            amended_at=amended_at,
            created_by=amended_by,
            **optional_fields,
        )
        item.amendments.append(amendment)
        await self._session.flush()
        return amendment

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_order_by_id(self, hospital_id: uuid.UUID, order_id: uuid.UUID) -> LabOrder | None:
        """Retrieve one order by UUID within a hospital.

        :param hospital_id: The tenant to scope to.
        :param order_id: The order UUID.
        :returns: The order, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id).where(LabOrder.id == order_id)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_order_for_update(
        self, hospital_id: uuid.UUID, order_id: uuid.UUID
    ) -> LabOrder | None:
        """Retrieve one order and lock its row until the transaction ends.

        Every lifecycle step goes through this, so two technicians acting on
        one order run one after the other rather than both reading the same
        status. ``of=LabOrder`` because the joined patient and doctor loads
        make this an outer join; ``populate_existing`` so a row already in the
        session is refreshed with what the lock actually protects.

        The caller **must** be inside a transaction.

        :param hospital_id: The tenant to scope to.
        :param order_id: The order UUID.
        :returns: The locked order, or ``None`` if absent or in another tenant.
        """
        stmt = (
            self._scoped(hospital_id)
            .where(LabOrder.id == order_id)
            .with_for_update(of=LabOrder)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_orders(
        self, hospital_id: uuid.UUID, *, skip: int = 0, limit: int = 25, **filters: Any
    ) -> list[LabOrder]:
        """List orders in a hospital, newest first.

        :param hospital_id: The tenant to scope to.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        :returns: A page of orders.
        """
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        stmt = self._apply_pagination(
            stmt.order_by(LabOrder.ordered_at.desc(), LabOrder.id.desc()), skip=skip, limit=limit
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_orders(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count orders matching the filters :meth:`list_orders` uses.

        :param hospital_id: The tenant to scope to.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        :returns: The number of matching orders.
        """
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()
