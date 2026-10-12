"""Repository for vendors and purchase orders.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it, or acts on a row already loaded through one that did
(CLAUDE.md rules 4 and 5).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, select

from app.models.pharmacy import PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus, Vendor
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class ProcurementRepository(BaseRepository[PurchaseOrder]):
    """Persistence for vendors and purchase orders.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(PurchaseOrder, session)

    # ── Vendors ───────────────────────────────────────────────────────────────

    @staticmethod
    def _vendors(hospital_id: uuid.UUID, *, is_active: bool | None = None) -> Select[tuple[Vendor]]:
        """Return a SELECT of live vendors filtered to one hospital."""
        stmt = select(Vendor).where(Vendor.hospital_id == hospital_id, Vendor.deleted_at.is_(None))
        if is_active is not None:
            stmt = stmt.where(Vendor.is_active.is_(is_active))
        return stmt

    async def create_vendor(
        self,
        *,
        hospital_id: uuid.UUID,
        name: str,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> Vendor:
        """Insert a vendor. Does not commit.

        :param hospital_id: Owning tenant.
        :param name: Vendor name, unique per hospital.
        :param created_by: UUID of the acting user.
        :param optional_fields: contact, address, tax_id.
        :returns: The persisted vendor.
        """
        vendor = Vendor(
            hospital_id=hospital_id, name=name, created_by=created_by, **optional_fields
        )
        self._session.add(vendor)
        await self._session.flush()
        await self._session.refresh(vendor)
        return vendor

    async def update_vendor(
        self, vendor: Vendor, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> Vendor:
        """Apply field updates to a vendor."""
        for name, value in fields.items():
            setattr(vendor, name, value)
        vendor.updated_by = updated_by
        await self._session.flush()
        await self._session.refresh(vendor)
        return vendor

    async def get_vendor_by_id(self, hospital_id: uuid.UUID, vendor_id: uuid.UUID) -> Vendor | None:
        """Retrieve one vendor by UUID within a hospital.

        :returns: The vendor, or ``None`` if absent or in another tenant.
        """
        result = await self._session.execute(
            self._vendors(hospital_id).where(Vendor.id == vendor_id)
        )
        return result.scalar_one_or_none()

    async def get_vendor_by_name(self, hospital_id: uuid.UUID, name: str) -> Vendor | None:
        """Retrieve a vendor by its exact name within a hospital."""
        result = await self._session.execute(self._vendors(hospital_id).where(Vendor.name == name))
        return result.scalar_one_or_none()

    async def list_vendors(
        self,
        hospital_id: uuid.UUID,
        *,
        skip: int = 0,
        limit: int = 25,
        is_active: bool | None = None,
    ) -> list[Vendor]:
        """List vendors in a hospital, ordered by name."""
        stmt = (
            self._vendors(hospital_id, is_active=is_active)
            .order_by(Vendor.name.asc(), Vendor.id.asc())
            .offset(skip)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_vendors(self, hospital_id: uuid.UUID, *, is_active: bool | None = None) -> int:
        """Count vendors matching the filter :meth:`list_vendors` uses."""
        stmt = self._vendors(hospital_id, is_active=is_active)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()

    # ── Purchase orders ───────────────────────────────────────────────────────

    def _scoped(self, hospital_id: uuid.UUID) -> Select[tuple[PurchaseOrder]]:
        """Return a base SELECT of purchase orders filtered to one hospital."""
        return self._query().where(PurchaseOrder.hospital_id == hospital_id)

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[PurchaseOrder]],
        *,
        status: PurchaseOrderStatus | None = None,
        vendor_id: uuid.UUID | None = None,
    ) -> Select[tuple[PurchaseOrder]]:
        """Apply the filters shared by list and count."""
        if status is not None:
            stmt = stmt.where(PurchaseOrder.status == status)
        if vendor_id is not None:
            stmt = stmt.where(PurchaseOrder.vendor_id == vendor_id)
        return stmt

    async def _reload(self, order: PurchaseOrder) -> PurchaseOrder:
        """Re-read a purchase order with its items and vendor fully loaded."""
        stmt = (
            select(PurchaseOrder)
            .where(PurchaseOrder.id == order.id)
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
    ) -> PurchaseOrder:
        """Insert a draft purchase order together with its items. Does not commit.

        :param hospital_id: Owning tenant.
        :param vendor_id: Vendor the order is placed with.
        :param po_number: Order number, unique per hospital.
        :param items: Column values per line, in display order.
        :param notes: Notes to the vendor.
        :param created_by: UUID of the acting user.
        :returns: The persisted order, items loaded.
        """
        order = PurchaseOrder(
            hospital_id=hospital_id,
            vendor_id=vendor_id,
            po_number=po_number,
            status=PurchaseOrderStatus.DRAFT,
            notes=notes,
            created_by=created_by,
            items=[
                PurchaseOrderItem(
                    hospital_id=hospital_id, position=position, created_by=created_by, **values
                )
                for position, values in enumerate(items)
            ],
        )
        self._session.add(order)
        await self._session.flush()
        return await self._reload(order)

    async def update_purchase_order(
        self, order: PurchaseOrder, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> PurchaseOrder:
        """Apply field updates to a purchase order and return it fully loaded."""
        for name, value in fields.items():
            setattr(order, name, value)
        order.updated_by = updated_by
        await self._session.flush()
        return await self._reload(order)

    async def get_purchase_order_by_id(
        self, hospital_id: uuid.UUID, order_id: uuid.UUID, *, for_update: bool = False
    ) -> PurchaseOrder | None:
        """Retrieve one purchase order by UUID within a hospital.

        :param for_update: Lock the row until the transaction ends, so an
            order cannot be received twice at once.
        :returns: The order, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id).where(PurchaseOrder.id == order_id)
        if for_update:
            stmt = stmt.with_for_update(of=PurchaseOrder).execution_options(populate_existing=True)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_purchase_orders(
        self, hospital_id: uuid.UUID, *, skip: int = 0, limit: int = 25, **filters: Any
    ) -> list[PurchaseOrder]:
        """List purchase orders in a hospital, newest first."""
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        stmt = self._apply_pagination(
            stmt.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc()),
            skip=skip,
            limit=limit,
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_purchase_orders(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count purchase orders matching the filters :meth:`list_purchase_orders` uses."""
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()
