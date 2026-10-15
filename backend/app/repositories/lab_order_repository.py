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

from sqlalchemy import Select, func, select, tuple_

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
    from sqlalchemy.sql import ColumnElement

    from app.core.tenancy import CrossTenant


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

    # ── A patient's own released results ──────────────────────────────────────

    @staticmethod
    def _owned_by(records: list[tuple[uuid.UUID, uuid.UUID]]) -> ColumnElement[bool]:
        """The order is at one of the hospitals, for that hospital's record."""
        return tuple_(LabOrder.hospital_id, LabOrder.patient_id).in_(records)

    async def list_for_patient_records(
        self,
        records: list[tuple[uuid.UUID, uuid.UUID]],
        scope: uuid.UUID | CrossTenant,
        *,
        statuses: Sequence[LabOrderStatus] = (LabOrderStatus.RELEASED,),
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[LabOrder], int]:
        """One page of the orders of some patient records, and their total.

        For a patient's own list across the hospitals they are linked at: each
        record is named together with its hospital, so a row is returned only
        where both match. Server-side only: no request can name a patient or
        a hospital here.

        The patient may read **released orders only** (§16), so the default is
        exactly that and the caller has no way to widen it — a list to a
        patient is always released, and forgetting to say so is not a bug to
        be caught in review but a default that already refuses.

        :param records: ``(hospital_id, patient_id)`` pairs. Empty means none.
        :param scope: A hospital id, or a ``cross_tenant(...)`` marker for a
            list that spans the caller's own hospitals.
        :param statuses: Only orders in one of these statuses. Fixed to
            ``released`` by the default; never widened for a patient read.
        :param skip: Rows to skip.
        :param limit: Maximum rows to return.
        :returns: The page, newest first, and how many orders the list holds
            in all.
        """
        if not records:
            return [], 0
        stmt = self._confine_to_tenant(
            self._query().where(self._owned_by(records)), scope, "list_for_patient_records"
        )
        stmt = stmt.where(LabOrder.status.in_(tuple(statuses)))
        total = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.order_by(LabOrder.ordered_at.desc(), LabOrder.id.desc()).offset(skip).limit(limit)
        )
        return list(rows.unique().scalars().all()), total.scalar_one()

    async def get_for_patient_records(
        self,
        records: list[tuple[uuid.UUID, uuid.UUID]],
        scope: uuid.UUID | CrossTenant,
        order_id: uuid.UUID,
        *,
        statuses: Sequence[LabOrderStatus] = (LabOrderStatus.RELEASED,),
    ) -> LabOrder | None:
        """Read one order if it belongs to one of some patient records.

        The patient may read **released orders only** (§16): by default an
        ordered, collected, in-progress, entered or cancelled order is exactly
        as invisible as one that does not exist.

        :param records: ``(hospital_id, patient_id)`` pairs. Empty means none.
        :param scope: A hospital id, or a ``cross_tenant(...)`` marker.
        :param order_id: The order.
        :param statuses: The statuses the caller may read. Fixed to
            ``released`` by the default; never widened for a patient read.
        :returns: The order, or ``None`` — also when it is somebody else's,
            not released, or cancelled: the answer never confirms existence.
        """
        if not records:
            return None
        stmt = self._confine_to_tenant(
            self._query().where(LabOrder.id == order_id, self._owned_by(records)),
            scope,
            "get_for_patient_records",
        ).execution_options(populate_existing=True)
        stmt = stmt.where(LabOrder.status.in_(tuple(statuses)))
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()
