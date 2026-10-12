"""Business logic for vendors and purchase orders.

The procurement third of ``docs/modules/08-pharmacy.md`` (§5.2, FR-5): who a
hospital buys from, what it has ordered, and taking the goods in.

A purchase order moves one way::

    draft ──send──▶ sent ──receive──▶ received
      └──────── cancel ────────┘

Receiving is where stock is created: each batch on the receipt becomes a
:class:`~app.models.pharmacy.MedicineBatch` (or tops up one with the same
number) through a ``received`` movement that points back at the order.

The ``vendors`` table is shared with Inventory by design (spec 09 §20), so
nothing here assumes a vendor only sells medicines.

Returns DTOs, never ORM models, and records an audit event per mutation
(CLAUDE.md rule 9).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.pharmacy import PurchaseOrderStatus, StockMovementReason
from app.schemas.common import Page, PaginationParams
from app.schemas.pharmacy import (
    CreatePurchaseOrderRequest,
    CreateVendorRequest,
    PurchaseOrderResponse,
    ReceivePurchaseOrderRequest,
    UpdateVendorRequest,
    VendorResponse,
)
from app.services.pharmacy_common import audit_value, field_error, hospital_today

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.pharmacy import MedicineBatch, PurchaseOrder, Vendor
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.medicine_repository import MedicineRepository
    from app.repositories.procurement_repository import ProcurementRepository

logger = get_logger(__name__)

__all__ = [
    "DuplicateVendorNameError",
    "ProcurementService",
    "PurchaseOrderNotFoundError",
    "PurchaseOrderStateError",
    "VendorNotFoundError",
]


class VendorNotFoundError(NotFoundError):
    """Raised when a vendor is absent from the requested hospital."""

    def __init__(self, vendor_id: uuid.UUID) -> None:
        super().__init__(message="Vendor not found.", detail={"vendor_id": str(vendor_id)})


class DuplicateVendorNameError(ConflictError):
    """Raised when a vendor name is already in use in the hospital."""

    def __init__(self, name: str) -> None:
        super().__init__(message=f"A vendor named '{name}' already exists.", detail={"name": name})


class PurchaseOrderNotFoundError(NotFoundError):
    """Raised when a purchase order is absent from the requested hospital."""

    def __init__(self, order_id: uuid.UUID) -> None:
        super().__init__(
            message="Purchase order not found.", detail={"purchase_order_id": str(order_id)}
        )


class PurchaseOrderStateError(BusinessRuleError):
    """Raised when a step is not allowed from the order's status."""

    def __init__(self, action: str, status: PurchaseOrderStatus) -> None:
        super().__init__(
            message=f"Cannot {action} a purchase order that is {status.value}.",
            detail={"status": status.value},
        )


def _new_po_number(now: datetime) -> str:
    """Return a new purchase order number.

    The year plus eight random hex characters. Unlike invoice numbers these
    carry no requirement to be sequential or gap-free, so no counter table is
    needed; the per-hospital unique constraint catches a collision.
    """
    return f"PO-{now.year}-{uuid.uuid4().hex[:8].upper()}"


class ProcurementService:
    """Vendors and purchase orders.

    :param procurement: Vendor and purchase-order data access.
    :param medicines: Medicine and batch data access, for what is ordered and
        the stock a receipt creates.
    :param hospitals: Hospital lookups, for the local date expiry is judged by.
    :param session: Request-scoped session, held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    """

    def __init__(
        self,
        procurement: ProcurementRepository,
        medicines: MedicineRepository,
        hospitals: HospitalRepository,
        session: AsyncSession,
        audit: AuditSink,
    ) -> None:
        self._procurement = procurement
        self._medicines = medicines
        self._hospitals = hospitals
        self._session = session
        self._audit = audit

    # ── Vendors ───────────────────────────────────────────────────────────────

    async def create_vendor(
        self,
        hospital_id: uuid.UUID,
        payload: CreateVendorRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> VendorResponse:
        """Add a vendor.

        :raises DuplicateVendorNameError: If the name is taken.
        """
        if await self._procurement.get_vendor_by_name(hospital_id, payload.name) is not None:
            raise DuplicateVendorNameError(payload.name)

        values = payload.model_dump()
        try:
            async with self._session.begin_nested():
                vendor = await self._procurement.create_vendor(
                    hospital_id=hospital_id, created_by=actor_id, **values
                )
        except IntegrityError as exc:
            if "uq_vendors_hospital_name" in str(getattr(exc, "orig", exc)):
                raise DuplicateVendorNameError(payload.name) from exc
            raise

        await self._audit.record(
            AuditEvent(
                action="pharmacy.vendor.created",
                hospital_id=hospital_id,
                target_type="vendor",
                target_id=vendor.id,
                actor_id=actor_id,
                changes={
                    name: {"before": None, "after": audit_value(value)}
                    for name, value in values.items()
                },
            )
        )
        await self._session.commit()
        logger.info(
            "pharmacy.vendor.created", hospital_id=str(hospital_id), vendor_id=str(vendor.id)
        )
        return VendorResponse.from_model(vendor)

    async def update_vendor(
        self,
        hospital_id: uuid.UUID,
        vendor_id: uuid.UUID,
        payload: UpdateVendorRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> VendorResponse:
        """Apply a partial update to a vendor.

        :raises VendorNotFoundError: If absent from this tenant.
        :raises DuplicateVendorNameError: If renamed to a name already in use.
        """
        vendor = await self._vendor_or_raise(hospital_id, vendor_id)

        requested = payload.model_dump(exclude_unset=True)
        changes = {
            name: {"before": audit_value(getattr(vendor, name)), "after": audit_value(value)}
            for name, value in requested.items()
            if getattr(vendor, name) != value
        }
        if not changes:
            return VendorResponse.from_model(vendor)
        if "name" in changes:
            clash = await self._procurement.get_vendor_by_name(hospital_id, requested["name"])
            if clash is not None:
                raise DuplicateVendorNameError(requested["name"])

        vendor = await self._procurement.update_vendor(
            vendor, updated_by=actor_id, **{name: requested[name] for name in changes}
        )
        await self._audit.record(
            AuditEvent(
                action="pharmacy.vendor.updated",
                hospital_id=hospital_id,
                target_type="vendor",
                target_id=vendor.id,
                actor_id=actor_id,
                changes=changes,
            )
        )
        await self._session.commit()
        logger.info(
            "pharmacy.vendor.updated",
            hospital_id=str(hospital_id),
            vendor_id=str(vendor.id),
            changed_fields=sorted(changes),
        )
        return VendorResponse.from_model(vendor)

    async def get_vendor(self, hospital_id: uuid.UUID, vendor_id: uuid.UUID) -> VendorResponse:
        """Retrieve one vendor.

        :raises VendorNotFoundError: If absent from this tenant.
        """
        return VendorResponse.from_model(await self._vendor_or_raise(hospital_id, vendor_id))

    async def list_vendors(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        is_active: bool | None = None,
    ) -> Page[VendorResponse]:
        """List vendors, ordered by name."""
        page_params = pagination or PaginationParams()
        rows = await self._procurement.list_vendors(
            hospital_id, skip=page_params.offset, limit=page_params.limit, is_active=is_active
        )
        total = await self._procurement.count_vendors(hospital_id, is_active=is_active)
        return Page[VendorResponse](
            items=[VendorResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Purchase orders ───────────────────────────────────────────────────────

    async def create_purchase_order(
        self,
        hospital_id: uuid.UUID,
        payload: CreatePurchaseOrderRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> PurchaseOrderResponse:
        """Draft a purchase order.

        :raises ValidationError: If the vendor or a medicine is unknown in
            this hospital, or inactive.
        """
        vendor = await self._procurement.get_vendor_by_id(hospital_id, payload.vendor_id)
        if vendor is None:
            raise field_error("vendor_id", "Vendor not found in this hospital.")
        if not vendor.is_active:
            raise field_error("vendor_id", f"Vendor '{vendor.name}' is inactive.")

        catalog = {
            medicine.id: medicine
            for medicine in await self._medicines.get_medicines_by_ids(
                hospital_id, [line.medicine_id for line in payload.items]
            )
        }
        items: list[dict[str, Any]] = []
        for index, line in enumerate(payload.items):
            medicine = catalog.get(line.medicine_id)
            if medicine is None:
                raise field_error(
                    f"items.{index}.medicine_id", "Medicine not found in this hospital."
                )
            if not medicine.is_active:
                raise field_error(
                    f"items.{index}.medicine_id",
                    f"Medicine '{medicine.sku}' is inactive and cannot be ordered.",
                )
            items.append(
                {
                    "medicine_id": medicine.id,
                    "quantity": line.quantity,
                    "unit_price": line.unit_price,
                    "total": line.unit_price * line.quantity,
                }
            )

        order = await self._procurement.create_purchase_order(
            hospital_id=hospital_id,
            vendor_id=vendor.id,
            po_number=_new_po_number(datetime.now(UTC)),
            notes=payload.notes,
            items=items,
            created_by=actor_id,
        )
        await self._record(order, "pharmacy.po.created", actor_id, line_count=len(items))
        await self._session.commit()
        return PurchaseOrderResponse.from_model(order)

    async def send_purchase_order(
        self, hospital_id: uuid.UUID, order_id: uuid.UUID, *, actor_id: uuid.UUID | None = None
    ) -> PurchaseOrderResponse:
        """Mark a draft as sent to the vendor.

        :raises PurchaseOrderNotFoundError: If absent from this tenant.
        :raises PurchaseOrderStateError: If it is not a draft.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status is not PurchaseOrderStatus.DRAFT:
            raise PurchaseOrderStateError("send", order.status)
        order = await self._procurement.update_purchase_order(
            order,
            updated_by=actor_id,
            status=PurchaseOrderStatus.SENT,
            ordered_at=datetime.now(UTC),
        )
        await self._record(order, "pharmacy.po.sent", actor_id, before="draft")
        await self._session.commit()
        return PurchaseOrderResponse.from_model(order)

    async def cancel_purchase_order(
        self, hospital_id: uuid.UUID, order_id: uuid.UUID, *, actor_id: uuid.UUID | None = None
    ) -> PurchaseOrderResponse:
        """Cancel an order whose goods have not been received.

        :raises PurchaseOrderNotFoundError: If absent from this tenant.
        :raises PurchaseOrderStateError: If it is received or already cancelled.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status not in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.SENT):
            raise PurchaseOrderStateError("cancel", order.status)
        before = order.status.value
        order = await self._procurement.update_purchase_order(
            order, updated_by=actor_id, status=PurchaseOrderStatus.CANCELLED
        )
        await self._record(order, "pharmacy.po.cancelled", actor_id, before=before)
        await self._session.commit()
        return PurchaseOrderResponse.from_model(order)

    async def receive_purchase_order(
        self,
        hospital_id: uuid.UUID,
        order_id: uuid.UUID,
        payload: ReceivePurchaseOrderRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> PurchaseOrderResponse:
        """Take in the goods of a sent order (module spec §5.2).

        Each receipt line names an order line and a batch. What arrived may
        differ from what was ordered — a short or split delivery is recorded
        as it came — but every batch must be of a medicine on the order.

        :raises PurchaseOrderNotFoundError: If absent from this tenant.
        :raises PurchaseOrderStateError: If it has not been sent, or is
            already received or cancelled.
        :raises ValidationError: If a line is not on the order, a batch has
            already expired (§11), or its expiry disagrees with an existing
            batch of that number.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status is not PurchaseOrderStatus.SENT:
            raise PurchaseOrderStateError("receive", order.status)

        today = await hospital_today(self._hospitals, hospital_id)
        by_id = {item.id: item for item in order.items}

        # Validate the whole receipt before any of it is put on the shelf.
        existing: dict[int, MedicineBatch | None] = {}
        for index, line in enumerate(payload.items):
            item = by_id.get(line.po_item_id)
            if item is None:
                raise field_error(f"items.{index}.po_item_id", "Not an item on this order.")
            if line.expiry_date < today:
                raise field_error(
                    f"items.{index}.expiry_date",
                    "A batch that has already expired cannot be received.",
                )
            batch = await self._medicines.get_batch_by_number(
                hospital_id, item.medicine_id, line.batch_number
            )
            if batch is not None and batch.expiry_date != line.expiry_date:
                raise field_error(
                    f"items.{index}.expiry_date",
                    f"Batch {batch.batch_number} is already recorded as expiring "
                    f"{batch.expiry_date.isoformat()}.",
                )
            existing[index] = batch

        now = datetime.now(UTC)
        received: list[dict[str, Any]] = []
        created: dict[tuple[uuid.UUID, str], MedicineBatch] = {}
        for index, line in enumerate(payload.items):
            item = by_id[line.po_item_id]
            # Two lines of one receipt may name the same new batch number.
            key = (item.medicine_id, line.batch_number)
            batch = existing[index] or created.get(key)
            if batch is None:
                batch = await self._medicines.create_batch(
                    medicine=item.medicine,
                    batch_number=line.batch_number,
                    expiry_date=line.expiry_date,
                    cost_per_unit=(
                        line.cost_per_unit if line.cost_per_unit is not None else item.unit_price
                    ),
                    created_by=actor_id,
                )
                created[key] = batch
            await self._medicines.apply_movement(
                batch,
                quantity_change=line.quantity,
                reason=StockMovementReason.RECEIVED,
                moved_at=now,
                moved_by=actor_id,
                reference_type="purchase_order",
                reference_id=order.id,
            )
            received.append(
                {
                    "medicine": item.medicine.sku,
                    "batch": batch.batch_number,
                    "quantity": line.quantity,
                }
            )

        order = await self._procurement.update_purchase_order(
            order,
            updated_by=actor_id,
            status=PurchaseOrderStatus.RECEIVED,
            received_at=now,
            received_by=actor_id,
        )
        await self._record(
            order, "pharmacy.po.received", actor_id, before="sent", received=received
        )
        await self._session.commit()
        return PurchaseOrderResponse.from_model(order)

    async def get_purchase_order(
        self, hospital_id: uuid.UUID, order_id: uuid.UUID
    ) -> PurchaseOrderResponse:
        """Retrieve one purchase order.

        :raises PurchaseOrderNotFoundError: If absent from this tenant.
        """
        order = await self._procurement.get_purchase_order_by_id(hospital_id, order_id)
        if order is None:
            raise PurchaseOrderNotFoundError(order_id)
        return PurchaseOrderResponse.from_model(order)

    async def list_purchase_orders(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        status: PurchaseOrderStatus | None = None,
        vendor_id: uuid.UUID | None = None,
    ) -> Page[PurchaseOrderResponse]:
        """List purchase orders, newest first."""
        page_params = pagination or PaginationParams()
        filters: dict[str, Any] = {"status": status, "vendor_id": vendor_id}
        rows = await self._procurement.list_purchase_orders(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._procurement.count_purchase_orders(hospital_id, **filters)
        return Page[PurchaseOrderResponse](
            items=[PurchaseOrderResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _vendor_or_raise(self, hospital_id: uuid.UUID, vendor_id: uuid.UUID) -> Vendor:
        """Fetch a vendor or raise :class:`VendorNotFoundError`."""
        vendor = await self._procurement.get_vendor_by_id(hospital_id, vendor_id)
        if vendor is None:
            raise VendorNotFoundError(vendor_id)
        return vendor

    async def _lock_or_raise(self, hospital_id: uuid.UUID, order_id: uuid.UUID) -> PurchaseOrder:
        """Lock a purchase order or raise :class:`PurchaseOrderNotFoundError`."""
        order = await self._procurement.get_purchase_order_by_id(
            hospital_id, order_id, for_update=True
        )
        if order is None:
            raise PurchaseOrderNotFoundError(order_id)
        return order

    async def _record(
        self,
        order: PurchaseOrder,
        action: str,
        actor_id: uuid.UUID | None,
        *,
        before: str | None = None,
        **context: Any,
    ) -> None:
        """Record an audit event and a log line for a purchase-order step."""
        await self._audit.record(
            AuditEvent(
                action=action,
                hospital_id=order.hospital_id,
                target_type="purchase_order",
                target_id=order.id,
                actor_id=actor_id,
                changes=(
                    {"status": {"before": before, "after": order.status.value}} if before else {}
                ),
                context={"po_number": order.po_number, **context},
            )
        )
        logger.info(
            action,
            hospital_id=str(order.hospital_id),
            purchase_order_id=str(order.id),
            status=order.status.value,
        )
