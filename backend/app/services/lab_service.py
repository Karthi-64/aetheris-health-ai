"""Business logic for lab orders.

Implements the order lifecycle of ``docs/modules/07-laboratory.md`` §5::

    ordered ──collect──▶ collected ──enter──▶ in_progress ──enter──▶ results_entered
       │                     │                     │                       │
       └──────────────── cancel (with a reason) ───┴───────────────────────┤
                                                                        release
                                                                           ▼
                                                                       released ──amend──▶ (stays released)

What the service guarantees:

- **An order belongs to a visit** (business rule 1). The patient and the
  ordering doctor are read from the appointment, never from the request.
- **The server decides what is abnormal** (rule 2). A numeric result is judged
  against the range for the patient's sex and their age when the sample was
  taken (§14), and the bounds used are stored with the result.
- **A released result is never silently changed** (rule 4). A correction is an
  amendment that keeps the old value, the new one, who and why.
- **Ordering charges the patient.** Each test becomes a line on a draft
  invoice in the same transaction, through the
  :class:`~app.core.charges.ChargeSink` seam.

Every lifecycle step locks the order row first, so two people acting on one
order run one after the other. Returns DTOs, never ORM models, and records an
audit event per mutation (CLAUDE.md rule 9). Result values never go to the
application log.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent
from app.core.charges import Charge, ChargeSink, NullChargeSink
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.notifications import NotificationRequest, Notifier, NullNotifier
from app.models.appointment import AppointmentStatus
from app.models.lab import (
    CANCELLABLE_STATUSES,
    LabOrderPriority,
    LabOrderStatus,
    LabResultFlag,
    LabResultType,
)
from app.schemas.common import Page, PaginationParams
from app.schemas.lab import (
    AmendResultRequest,
    CancelLabOrderRequest,
    CollectSamplesRequest,
    CreateLabOrderRequest,
    EnterResultsRequest,
    LabOrderResponse,
    UpdateLabOrderRequest,
)
from app.utils.lab_ranges import age_in_years, flag_value, parse_numeric, select_range

if TYPE_CHECKING:
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.lab import LabOrder, LabOrderItem, LabTest
    from app.repositories.appointment_repository import AppointmentRepository
    from app.repositories.lab_order_repository import LabOrderRepository
    from app.repositories.lab_test_repository import LabTestRepository

logger = get_logger(__name__)

__all__ = [
    "DuplicateSampleIdError",
    "LabOrderItemNotFoundError",
    "LabOrderNotFoundError",
    "LabOrderStateError",
    "LabService",
]

#: Appointment statuses a test cannot be ordered against: the visit did not
#: and will not happen.
_UNORDERABLE_VISITS = frozenset({AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW})

#: Statuses in which results may be entered or re-entered. Before release a
#: result is still the lab's working copy and can simply be corrected.
_RESULT_ENTRY_STATUSES = frozenset(
    {LabOrderStatus.COLLECTED, LabOrderStatus.IN_PROGRESS, LabOrderStatus.RESULTS_ENTERED}
)

_ABNORMAL_FLAGS = frozenset({LabResultFlag.LOW, LabResultFlag.HIGH, LabResultFlag.CRITICAL})


# ── Errors ──────────────────────────────────────────────────────────────────


class LabOrderNotFoundError(NotFoundError):
    """Raised when an order is absent from the requested hospital.

    Also raised for one in another tenant: a cross-tenant lookup must be
    indistinguishable from a miss.
    """

    def __init__(self, order_id: uuid.UUID) -> None:
        super().__init__(message="Lab order not found.", detail={"order_id": str(order_id)})


class LabOrderItemNotFoundError(NotFoundError):
    """Raised when an item is not part of the order it was addressed through."""

    def __init__(self, item_id: uuid.UUID) -> None:
        super().__init__(
            message="Lab order item not found on this order.", detail={"item_id": str(item_id)}
        )


class LabOrderStateError(BusinessRuleError):
    """Raised when a lifecycle step is not allowed from the order's status."""

    def __init__(self, action: str, status: LabOrderStatus) -> None:
        super().__init__(
            message=f"Cannot {action} a lab order that is {status.value.replace('_', ' ')}.",
            detail={"status": status.value},
        )


class DuplicateSampleIdError(ConflictError):
    """Raised when a sample id is already in use in the hospital (rule 5)."""

    def __init__(self, sample_id: str) -> None:
        super().__init__(
            message=f"Sample id '{sample_id}' is already in use.", detail={"sample_id": sample_id}
        )


def _field_error(field: str, message: str) -> ValidationError:
    """Build a 422 naming one offending field, in the standard error shape."""
    return ValidationError(
        message=message, detail={"errors": [{"field": field, "message": message}]}
    )


def _generate_sample_id() -> str:
    """Return a new barcode-ready sample id.

    Ten hex characters of a random UUID: short enough to print, and the
    per-hospital unique index catches the collision that will never happen.
    """
    return f"S-{uuid.uuid4().hex[:10].upper()}"


def _display(value: str | None, unit: str | None) -> str:
    """Render a result with its unit for a notification."""
    return f"{value} {unit}".strip() if value is not None else "no value"


# ── Service ─────────────────────────────────────────────────────────────────


class LabService:
    """Ordering tests and taking them through to released results.

    :param orders: Order, item and amendment data access.
    :param tests: Test catalog lookups.
    :param appointments: Appointment lookups, for the visit an order hangs off.
    :param session: Request-scoped session, held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    :param charges: Where ordered tests are charged. Optional; a service built
        without one charges nothing.
    :param notifier: Where the ordering doctor is told about results. Optional.
    """

    def __init__(
        self,
        orders: LabOrderRepository,
        tests: LabTestRepository,
        appointments: AppointmentRepository,
        session: AsyncSession,
        audit: AuditSink,
        charges: ChargeSink | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._orders = orders
        self._tests = tests
        self._appointments = appointments
        self._session = session
        self._audit = audit
        self._charges: ChargeSink = charges or NullChargeSink()
        self._notifier: Notifier = notifier or NullNotifier()

    # ── Ordering ──────────────────────────────────────────────────────────────

    async def create_order(
        self,
        hospital_id: uuid.UUID,
        payload: CreateLabOrderRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> LabOrderResponse:
        """Order tests for the patient of a visit (module spec §5 step 1).

        :param hospital_id: The hospital placing the order.
        :param payload: Validated order data.
        :param actor_id: UUID of the acting user.
        :returns: The order, in ``ordered``.
        :raises ValidationError: If the appointment or a test is unknown in
            this hospital, the visit was cancelled or missed, or a test is
            inactive.
        """
        appointment = await self._appointments.get_appointment_by_id(
            hospital_id, payload.appointment_id
        )
        if appointment is None:
            raise _field_error("appointment_id", "Appointment not found in this hospital.")
        if appointment.status in _UNORDERABLE_VISITS:
            raise _field_error(
                "appointment_id",
                f"Tests cannot be ordered for a visit that is {appointment.status.value}.",
            )

        tests = await self._resolve_tests(hospital_id, payload.test_ids)

        order = await self._orders.create_order(
            hospital_id=hospital_id,
            patient_id=appointment.patient_id,
            doctor_id=appointment.doctor_id,
            appointment_id=appointment.id,
            ordered_at=datetime.now(UTC),
            priority=payload.priority,
            notes=payload.notes,
            created_by=actor_id,
            items=[
                {
                    "test_id": test.id,
                    "test_code": test.code,
                    "test_name": test.name,
                    "result_type": test.result_type,
                    "price": test.price,
                    "created_by": actor_id,
                }
                for test in tests
            ],
        )

        invoice_id = await self._charges.add_charges(
            hospital_id,
            patient_id=order.patient_id,
            appointment_id=order.appointment_id,
            charges=[
                Charge(description=f"Lab test — {test.name}", unit_price=test.price)
                for test in tests
            ],
            source="laboratory",
            actor_id=actor_id,
        )
        if invoice_id is not None:
            order = await self._orders.update_order(
                order, updated_by=actor_id, invoice_id=invoice_id
            )

        await self._audit.record(
            AuditEvent(
                action="lab.order.created",
                hospital_id=hospital_id,
                target_type="lab_order",
                target_id=order.id,
                actor_id=actor_id,
                context={
                    "appointment_id": str(order.appointment_id),
                    "priority": order.priority.value,
                    "tests": [test.code for test in tests],
                    "invoice_id": str(invoice_id) if invoice_id else None,
                },
            )
        )
        await self._session.commit()

        logger.info(
            "lab.order.created",
            hospital_id=str(hospital_id),
            order_id=str(order.id),
            test_count=len(tests),
            priority=order.priority.value,
        )
        return LabOrderResponse.from_model(order)

    async def update_order(
        self,
        hospital_id: uuid.UUID,
        order_id: uuid.UUID,
        payload: UpdateLabOrderRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> LabOrderResponse:
        """Change an open order's priority or notes.

        The tests on an order are not edited: they have been charged for. To
        change them, cancel the order and place another.

        :raises LabOrderNotFoundError: If absent from this tenant.
        :raises LabOrderStateError: If the order is released or cancelled.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status not in CANCELLABLE_STATUSES:
            raise LabOrderStateError("edit", order.status)

        requested = payload.model_dump(exclude_unset=True)
        if "priority" in requested:
            requested["priority"] = payload.priority
        changes = {
            name: {
                "before": getattr(getattr(order, name), "value", getattr(order, name)),
                "after": getattr(value, "value", value),
            }
            for name, value in requested.items()
            if getattr(order, name) != value
        }
        if not changes:
            return LabOrderResponse.from_model(order)

        order = await self._orders.update_order(
            order, updated_by=actor_id, **{name: requested[name] for name in changes}
        )
        await self._audit.record(
            AuditEvent(
                action="lab.order.updated",
                hospital_id=hospital_id,
                target_type="lab_order",
                target_id=order.id,
                actor_id=actor_id,
                changes=changes,
            )
        )
        await self._session.commit()
        logger.info(
            "lab.order.updated",
            hospital_id=str(hospital_id),
            order_id=str(order.id),
            changed_fields=sorted(changes),
        )
        return LabOrderResponse.from_model(order)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def collect_samples(
        self,
        hospital_id: uuid.UUID,
        order_id: uuid.UUID,
        payload: CollectSamplesRequest | None = None,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> LabOrderResponse:
        """Record samples as collected (module spec §5 step 2).

        With no items named, every outstanding sample is collected and given a
        generated id. The order moves to ``collected`` once no sample is
        outstanding; until then it stays ``ordered``.

        :raises LabOrderNotFoundError: If absent from this tenant.
        :raises LabOrderStateError: If the order is past collection.
        :raises LabOrderItemNotFoundError: If a named item is not on the order.
        :raises BusinessRuleError: If a named item's sample is already collected.
        :raises DuplicateSampleIdError: If a sample id is already in use.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status is not LabOrderStatus.ORDERED:
            raise LabOrderStateError("collect samples for", order.status)

        by_id = {item.id: item for item in order.items}
        wanted: list[tuple[LabOrderItem, str | None]]
        if payload is None or payload.items is None:
            wanted = [(item, None) for item in order.items if item.sample_collected_at is None]
        else:
            wanted = []
            for entry in payload.items:
                item = by_id.get(entry.item_id)
                if item is None:
                    raise LabOrderItemNotFoundError(entry.item_id)
                if item.sample_collected_at is not None:
                    msg = f"The sample for {item.test_name} has already been collected."
                    raise BusinessRuleError(msg, detail={"item_id": str(item.id)})
                wanted.append((item, entry.sample_id))

        now = datetime.now(UTC)
        for item, supplied in wanted:
            sample_id = supplied or _generate_sample_id()
            try:
                async with self._session.begin_nested():
                    await self._orders.update_item(
                        item,
                        updated_by=actor_id,
                        sample_id=sample_id,
                        sample_collected_at=now,
                        sample_collected_by=actor_id,
                    )
            except IntegrityError as exc:
                if "uq_lab_order_items_hospital_sample" in str(getattr(exc, "orig", exc)):
                    raise DuplicateSampleIdError(sample_id) from exc
                raise

        done = all(item.sample_collected_at is not None for item in order.items)
        order = await self._orders.update_order(
            order,
            updated_by=actor_id,
            **({"status": LabOrderStatus.COLLECTED, "collected_at": now} if done else {}),
        )

        await self._audit.record(
            AuditEvent(
                action="lab.order.samples_collected",
                hospital_id=hospital_id,
                target_type="lab_order",
                target_id=order.id,
                actor_id=actor_id,
                context={
                    "samples": [item.sample_id for item, _ in wanted],
                    "status": order.status.value,
                },
            )
        )
        await self._session.commit()
        logger.info(
            "lab.order.samples_collected",
            hospital_id=str(hospital_id),
            order_id=str(order.id),
            collected=len(wanted),
            status=order.status.value,
        )
        return LabOrderResponse.from_model(order)

    async def enter_results(
        self,
        hospital_id: uuid.UUID,
        order_id: uuid.UUID,
        payload: EnterResultsRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> LabOrderResponse:
        """Enter results and flag the abnormal ones (module spec §5 steps 3–4).

        Results may be entered a few at a time, and re-entered before release.
        The order is ``in_progress`` while some are missing and
        ``results_entered`` once none are. A result that lands in the critical
        range notifies the ordering doctor at once (§14), before release.

        :raises LabOrderNotFoundError: If absent from this tenant.
        :raises LabOrderStateError: If samples are not all collected yet, or
            the order is released or cancelled.
        :raises LabOrderItemNotFoundError: If a named item is not on the order.
        :raises ValidationError: If a numeric test is given a non-numeric value.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status not in _RESULT_ENTRY_STATUSES:
            raise LabOrderStateError("enter results for", order.status)

        by_id = {item.id: item for item in order.items}
        for index, entry in enumerate(payload.results):
            if entry.item_id not in by_id:
                raise LabOrderItemNotFoundError(entry.item_id)
            if (
                by_id[entry.item_id].result_type is LabResultType.NUMERIC
                and parse_numeric(entry.value) is None
            ):
                raise _field_error(
                    f"results.{index}.value",
                    f"{by_id[entry.item_id].test_name} needs a numeric result.",
                )

        tests = {
            test.id: test
            for test in await self._tests.get_tests_by_ids(
                hospital_id, [by_id[entry.item_id].test_id for entry in payload.results]
            )
        }

        now = datetime.now(UTC)
        changes: dict[str, dict[str, Any]] = {}
        newly_critical: list[LabOrderItem] = []
        for entry in payload.results:
            item = by_id[entry.item_id]
            before = item.result_value
            was_critical = item.result_flag is LabResultFlag.CRITICAL
            flag, low, high = self._judge(order, item, tests.get(item.test_id), entry.value)
            test = tests.get(item.test_id)
            await self._orders.update_item(
                item,
                updated_by=actor_id,
                result_value=entry.value,
                result_unit=test.unit if test is not None else item.result_unit,
                result_flag=flag,
                reference_low=low,
                reference_high=high,
                result_entered_at=now,
                result_entered_by=actor_id,
                notes=entry.notes,
            )
            changes[item.test_code] = {"before": before, "after": entry.value}
            if flag is LabResultFlag.CRITICAL and not was_critical:
                newly_critical.append(item)

        complete = all(item.result_value is not None for item in order.items)
        order = await self._orders.update_order(
            order,
            updated_by=actor_id,
            status=LabOrderStatus.RESULTS_ENTERED if complete else LabOrderStatus.IN_PROGRESS,
            results_entered_at=now if complete else None,
        )

        for item in newly_critical:
            await self._notify_doctor(
                order,
                "lab.critical_result",
                actor_id,
                test_name=item.test_name,
                result=_display(item.result_value, item.result_unit),
            )

        await self._audit.record(
            AuditEvent(
                action="lab.order.results_entered",
                hospital_id=hospital_id,
                target_type="lab_order",
                target_id=order.id,
                actor_id=actor_id,
                changes=changes,
                context={
                    "status": order.status.value,
                    "abnormal": sorted(
                        item.test_code
                        for item in order.items
                        if item.result_flag in _ABNORMAL_FLAGS
                    ),
                },
            )
        )
        await self._session.commit()
        logger.info(
            "lab.order.results_entered",
            hospital_id=str(hospital_id),
            order_id=str(order.id),
            entered=len(payload.results),
            critical=len(newly_critical),
            status=order.status.value,
        )
        return LabOrderResponse.from_model(order)

    async def release_order(
        self,
        hospital_id: uuid.UUID,
        order_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> LabOrderResponse:
        """Release an order's results (module spec §5 steps 5–6, FR-4).

        Only when every item has a result (§11). From here the results are
        fixed: a correction is an amendment.

        :raises LabOrderNotFoundError: If absent from this tenant.
        :raises LabOrderStateError: If results are still missing, or the order
            is already released or cancelled.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status is not LabOrderStatus.RESULTS_ENTERED:
            raise LabOrderStateError("release", order.status)

        now = datetime.now(UTC)
        for item in order.items:
            await self._orders.update_item(
                item, updated_by=actor_id, released_at=now, released_by=actor_id
            )
        order = await self._orders.update_order(
            order,
            updated_by=actor_id,
            status=LabOrderStatus.RELEASED,
            released_at=now,
            released_by=actor_id,
        )

        await self._notify_doctor(
            order,
            "lab.results_released",
            actor_id,
            test_names=", ".join(item.test_name for item in order.items),
        )
        await self._audit.record(
            AuditEvent(
                action="lab.order.released",
                hospital_id=hospital_id,
                target_type="lab_order",
                target_id=order.id,
                actor_id=actor_id,
                context={
                    "turnaround_minutes": int((now - order.ordered_at).total_seconds() // 60),
                    "abnormal": sorted(
                        item.test_code
                        for item in order.items
                        if item.result_flag in _ABNORMAL_FLAGS
                    ),
                },
            )
        )
        await self._session.commit()
        logger.info("lab.order.released", hospital_id=str(hospital_id), order_id=str(order.id))
        return LabOrderResponse.from_model(order)

    async def cancel_order(
        self,
        hospital_id: uuid.UUID,
        order_id: uuid.UUID,
        payload: CancelLabOrderRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> LabOrderResponse:
        """Cancel an order that has not been released.

        The charge raised when the order was placed is **not** removed: the
        draft invoice is Billing's to correct, and by now it may have been
        issued. The audit entry names the invoice so it can be found.

        :raises LabOrderNotFoundError: If absent from this tenant.
        :raises LabOrderStateError: If the order is released or already cancelled.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status not in CANCELLABLE_STATUSES:
            raise LabOrderStateError("cancel", order.status)

        previous = order.status
        order = await self._orders.update_order(
            order,
            updated_by=actor_id,
            status=LabOrderStatus.CANCELLED,
            cancelled_at=datetime.now(UTC),
            cancel_reason=payload.reason,
        )
        await self._audit.record(
            AuditEvent(
                action="lab.order.cancelled",
                hospital_id=hospital_id,
                target_type="lab_order",
                target_id=order.id,
                actor_id=actor_id,
                changes={"status": {"before": previous.value, "after": order.status.value}},
                context={
                    "reason": payload.reason,
                    "invoice_id": str(order.invoice_id) if order.invoice_id else None,
                },
            )
        )
        await self._session.commit()
        logger.info(
            "lab.order.cancelled",
            hospital_id=str(hospital_id),
            order_id=str(order.id),
            from_status=previous.value,
        )
        return LabOrderResponse.from_model(order)

    async def amend_result(
        self,
        hospital_id: uuid.UUID,
        order_id: uuid.UUID,
        item_id: uuid.UUID,
        payload: AmendResultRequest,
        *,
        actor_id: uuid.UUID,
    ) -> LabOrderResponse:
        """Correct a released result (business rule 4).

        The item takes the new value and is flagged afresh against the bounds
        it was first judged by; an amendment row keeps the old value, the old
        flag and the reason. The ordering doctor is told (§14).

        :raises LabOrderNotFoundError: If absent from this tenant.
        :raises LabOrderStateError: If the order is not released — before
            release a result is simply re-entered.
        :raises LabOrderItemNotFoundError: If the item is not on the order.
        :raises ValidationError: If a numeric test is given a non-numeric
            value, or the value is unchanged.
        """
        order = await self._lock_or_raise(hospital_id, order_id)
        if order.status is not LabOrderStatus.RELEASED:
            raise LabOrderStateError("amend a result on", order.status)
        item = next((candidate for candidate in order.items if candidate.id == item_id), None)
        if item is None:
            raise LabOrderItemNotFoundError(item_id)
        if payload.new_value == item.result_value:
            raise _field_error("new_value", "The new value is the same as the current one.")
        if item.result_type is LabResultType.NUMERIC and parse_numeric(payload.new_value) is None:
            raise _field_error("new_value", f"{item.test_name} needs a numeric result.")

        test = next(iter(await self._tests.get_tests_by_ids(hospital_id, [item.test_id])), None)
        previous_value, previous_flag = item.result_value, item.result_flag
        flag, low, high = self._judge(order, item, test, payload.new_value)
        now = datetime.now(UTC)

        await self._orders.add_amendment(
            item,
            previous_value=previous_value,
            new_value=payload.new_value,
            previous_flag=previous_flag,
            new_flag=flag,
            reason=payload.reason,
            amended_by=actor_id,
            amended_at=now,
        )
        await self._orders.update_item(
            item,
            updated_by=actor_id,
            result_value=payload.new_value,
            result_flag=flag,
            reference_low=low,
            reference_high=high,
        )
        order = await self._orders.update_order(order, updated_by=actor_id)

        await self._notify_doctor(
            order,
            "lab.result_amended",
            actor_id,
            test_name=item.test_name,
            previous_result=_display(previous_value, item.result_unit),
            result=_display(item.result_value, item.result_unit),
            reason=payload.reason,
        )
        await self._audit.record(
            AuditEvent(
                action="lab.order.result_amended",
                hospital_id=hospital_id,
                target_type="lab_order",
                target_id=order.id,
                actor_id=actor_id,
                changes={item.test_code: {"before": previous_value, "after": payload.new_value}},
                context={"item_id": str(item.id), "reason": payload.reason},
            )
        )
        await self._session.commit()
        logger.info(
            "lab.order.result_amended",
            hospital_id=str(hospital_id),
            order_id=str(order.id),
            item_id=str(item.id),
        )
        return LabOrderResponse.from_model(order)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_order(self, hospital_id: uuid.UUID, order_id: uuid.UUID) -> LabOrderResponse:
        """Retrieve one order with its items.

        :raises LabOrderNotFoundError: If absent from this tenant.
        """
        order = await self._orders.get_order_by_id(hospital_id, order_id)
        if order is None:
            raise LabOrderNotFoundError(order_id)
        return LabOrderResponse.from_model(order)

    async def list_orders(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        status: LabOrderStatus | None = None,
        priority: LabOrderPriority | None = None,
        patient_id: uuid.UUID | None = None,
        doctor_id: uuid.UUID | None = None,
        appointment_id: uuid.UUID | None = None,
    ) -> Page[LabOrderResponse]:
        """List orders, newest first — the lab worklist (§12).

        :param hospital_id: The hospital to list.
        :param pagination: Page and page size. Defaults to page 1.
        :param status: Only orders in this status.
        :param priority: Only orders of this priority.
        :param patient_id: Only this patient's orders.
        :param doctor_id: Only orders placed by this doctor.
        :param appointment_id: Only orders raised in this visit.
        :returns: One page of orders plus the total count.
        """
        page_params = pagination or PaginationParams()
        filters: dict[str, Any] = {
            "status": status,
            "priority": priority,
            "patient_id": patient_id,
            "doctor_id": doctor_id,
            "appointment_id": appointment_id,
        }
        rows = await self._orders.list_orders(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._orders.count_orders(hospital_id, **filters)
        return Page[LabOrderResponse](
            items=[LabOrderResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _lock_or_raise(self, hospital_id: uuid.UUID, order_id: uuid.UUID) -> LabOrder:
        """Lock an order for a lifecycle step or raise :class:`LabOrderNotFoundError`."""
        order = await self._orders.get_order_for_update(hospital_id, order_id)
        if order is None:
            raise LabOrderNotFoundError(order_id)
        return order

    async def _resolve_tests(
        self, hospital_id: uuid.UUID, test_ids: list[uuid.UUID]
    ) -> list[LabTest]:
        """Return the catalog tests for an order, in the order requested.

        :raises ValidationError: If a test is unknown in this hospital or has
            been deactivated.
        """
        found = {
            test.id: test for test in await self._tests.get_tests_by_ids(hospital_id, test_ids)
        }
        tests = []
        for index, test_id in enumerate(test_ids):
            test = found.get(test_id)
            if test is None:
                raise _field_error(f"test_ids.{index}", "Lab test not found in this hospital.")
            if not test.is_active:
                raise _field_error(
                    f"test_ids.{index}",
                    f"Lab test '{test.code}' is inactive and cannot be ordered.",
                )
            tests.append(test)
        return tests

    @staticmethod
    def _judge(
        order: LabOrder, item: LabOrderItem, test: LabTest | None, value: str
    ) -> tuple[LabResultFlag | None, Decimal | None, Decimal | None]:
        """Flag a result and return the bounds it was judged against.

        A text result has no range. A numeric one uses the range for the
        patient's sex and their age on the day the sample was taken (§14).

        :returns: ``(flag, low, high)``; all ``None`` when no range applies.
        """
        number = parse_numeric(value)
        if item.result_type is not LabResultType.NUMERIC or number is None or test is None:
            return None, None, None
        collected = item.sample_collected_at or order.ordered_at
        bounds = select_range(
            test.reference_ranges,
            sex=order.patient.gender.value,
            age=age_in_years(order.patient.date_of_birth, collected.date()),
        )
        if bounds is None:
            return None, None, None
        return flag_value(number, bounds), bounds.low, bounds.high

    async def _notify_doctor(
        self, order: LabOrder, kind: str, actor_id: uuid.UUID | None, **variables: str
    ) -> None:
        """Tell the ordering doctor something about their order."""
        await self._notifier.notify(
            NotificationRequest(
                kind=kind,
                hospital_id=order.hospital_id,
                recipient_user_ids=(order.doctor.user_id,),
                variables={"patient_name": order.patient.full_name, **variables},
                link="/laboratory",
                actor_id=actor_id,
            )
        )
