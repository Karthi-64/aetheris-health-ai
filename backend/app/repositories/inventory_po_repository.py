"""Repository for inventory purchase orders.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it, or acts on a row already loaded through one that did
(CLAUDE.md rules 4 and 5).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, select

from app.models.inventory import InventoryPurchaseOrder, InventoryPurchaseOrderItem
from app.models.pharmacy import PurchaseOrderStatus
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class InventoryPurchaseOrderRepository(BaseRepository[InventoryPurchaseOrder]):
    """Persistence for inventory purchase orders.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(InventoryPurchaseOrder, session)

    def _scoped(self, hospital_id: uuid.UUID) -> Select[tuple[InventoryPurchaseOrder]]:
        """Return a base SELECT filtered to one hospital."""
        return self._query().where(InventoryPurchaseOrder.hospital_id == hospital_id)

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[InventoryPurchaseOrder]],
        *,
        status: PurchaseOrderStatus | None = None,
        vendor_id: uuid.UUID | None = None,
    ) -> Select[tuple[InventoryPurchaseOrder]]:
        """Apply the filters shared by list and count."""
        if status is not None:
            stmt = stmt.where(InventoryPurchaseOrder.status == status)
        if vendor_id is not None:
            stmt = stmt.where(InventoryPurchaseOrder.vendor_id == vendor_id)
        return stmt

    async def _reload(self, order: InventoryPurchaseOrder) -> InventoryPurchaseOrder:
        """Re-read an order with its items and vendor fully loaded.

        A flush leaves server-maintained columns expired; reading them
        afterwards would be lazy IO, which async SQLAlchemy refuses.
        """
        stmt = (
            select(InventoryPurchaseOrder)
            .where(InventoryPurchaseOrder.id == order.id)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one()

    async def create_purchase_order(
        self,
        *,
        hospital_id: uuid.UUID,
        vendor_id: uuid.UUID,
        po_number: str,
        items: Sequence[dict[str, Any]],
        notes: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> InventoryPurchaseOrder:
        """Insert a draft purchase order together with its items. Does not commit.

        :param hospital_id: Owning tenant.
        :param vendor_id: Vendor the order is placed with.
        :param po_number: Order number, unique per hospital.
        :param items: Column values per line, in display order.
        :param notes: Notes to the vendor.
        :param created_by: UUID of the acting user.
        :returns: The persisted order, items loaded.
        """
        order = InventoryPurchaseOrder(
            hospital_id=hospital_id,
            vendor_id=vendor_id,
            po_number=po_number,
            status=PurchaseOrderStatus.DRAFT,
            notes=notes,
            created_by=created_by,
            items=[
                InventoryPurchaseOrderItem(
                    hospital_id=hospital_id, position=position, created_by=created_by, **values
                )
                for position, values in enumerate(items)
            ],
        )
        self._session.add(order)
        await self._session.flush()
        return await self._reload(order)

    async def update_purchase_order(
        self, order: InventoryPurchaseOrder, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> InventoryPurchaseOrder:
        """Apply field updates to an order and return it fully loaded."""
        for name, value in fields.items():
            setattr(order, name, value)
        order.updated_by = updated_by
        await self._session.flush()
        return await self._reload(order)

    async def get_purchase_order_by_id(
        self, hospital_id: uuid.UUID, order_id: uuid.UUID, *, for_update: bool = False
    ) -> InventoryPurchaseOrder | None:
        """Retrieve one order by UUID within a hospital.

        :param for_update: Lock the row until the transaction ends, so an
            order cannot be received twice at once.
        :returns: The order, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id).where(InventoryPurchaseOrder.id == order_id)
        if for_update:
            stmt = stmt.with_for_update(of=InventoryPurchaseOrder).execution_options(
                populate_existing=True
            )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_purchase_orders(
        self, hospital_id: uuid.UUID, *, skip: int = 0, limit: int = 25, **filters: Any
    ) -> list[InventoryPurchaseOrder]:
        """List orders in a hospital, newest first."""
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        stmt = self._apply_pagination(
            stmt.order_by(
                InventoryPurchaseOrder.created_at.desc(), InventoryPurchaseOrder.id.desc()
            ),
            skip=skip,
            limit=limit,
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_purchase_orders(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count orders matching the filters :meth:`list_purchase_orders` uses."""
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()
