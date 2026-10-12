"""Lab order API routes.

Implements the order half of ``docs/modules/07-laboratory.md`` §9. Routes
parse input, delegate to :class:`~app.services.lab_service.LabService`, and
wrap the result in the standard envelope. No business logic, no database
access.

**Tenancy.** ``hospital_id`` always comes from the authenticated user.

**Not here yet.** ``GET /lab-orders/{id}/report.pdf`` and
``POST /lab-orders/{id}/ai-explain`` are not built; calling them is a 404.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Path, Query, status

from app.api.dependencies.auth import require_permission
from app.api.dependencies.services import get_lab_service
from app.core.exceptions import BusinessRuleError
from app.models.lab import LabOrderPriority, LabOrderStatus
from app.models.user import User
from app.schemas.common import (
    MetadataWithPagination,
    PaginatedResponse,
    PaginationMeta,
    PaginationParams,
    SuccessResponse,
)
from app.schemas.lab import (
    AmendResultRequest,
    CancelLabOrderRequest,
    CollectSamplesRequest,
    CreateLabOrderRequest,
    EnterResultsRequest,
    LabOrderResponse,
    UpdateLabOrderRequest,
)
from app.services.lab_service import LabService

router = APIRouter(prefix="/lab-orders", tags=["Laboratory — Orders"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    422: {"description": "Request failed validation."},
}

_NOT_FOUND_RESPONSE: dict[int | str, dict[str, str]] = {
    404: {"description": "Lab order not found in this hospital."},
}

_STATE_RESPONSE: dict[int | str, dict[str, str]] = {
    400: {"description": "The order's status does not allow this step."},
}


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the request acts within.

    :param current_user: The authenticated user.
    :returns: The hospital UUID to scope every query by.
    :raises BusinessRuleError: If the user belongs to no hospital.
    """
    if current_user.hospital_id is None:
        msg = "This account is not scoped to a hospital, so lab orders cannot be accessed."
        raise BusinessRuleError(msg)
    return current_user.hospital_id


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[LabOrderResponse],
    summary="Order lab tests",
    description=(
        "Order one or more catalog tests for the patient of a visit.\n\n"
        "The patient and the ordering doctor are taken from the appointment. "
        "Each test is added as a line to a draft invoice for the patient; the "
        "response's `invoice_id` says which."
    ),
    responses={201: {"description": "Order placed."}, **_COMMON_RESPONSES},
)
async def create_order(
    payload: CreateLabOrderRequest,
    current_user: User = Depends(require_permission("lab.order.create")),
    service: LabService = Depends(get_lab_service),
) -> SuccessResponse[LabOrderResponse]:
    """Place a lab order (module spec §5 step 1)."""
    order = await service.create_order(_tenant_of(current_user), payload, actor_id=current_user.id)
    return SuccessResponse[LabOrderResponse](message="Lab order placed.", data=order)


@router.get(
    "",
    response_model=PaginatedResponse[LabOrderResponse],
    summary="List lab orders",
    description="Return a page of lab orders, newest first. This is the lab worklist.",
    responses={200: {"description": "Page of orders returned."}, **_COMMON_RESPONSES},
)
async def list_orders(
    order_status: LabOrderStatus | None = Query(
        None, alias="status", description="Only orders in this status."
    ),
    priority: LabOrderPriority | None = Query(None, description="Only this priority."),
    patient_id: uuid.UUID | None = Query(None, description="Only this patient's orders."),
    doctor_id: uuid.UUID | None = Query(None, description="Only orders by this doctor."),
    appointment_id: uuid.UUID | None = Query(None, description="Only orders from this visit."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("lab.order.read")),
    service: LabService = Depends(get_lab_service),
) -> PaginatedResponse[LabOrderResponse]:
    """List lab orders (module spec §9)."""
    page_result = await service.list_orders(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        status=order_status,
        priority=priority,
        patient_id=patient_id,
        doctor_id=doctor_id,
        appointment_id=appointment_id,
    )
    return PaginatedResponse[LabOrderResponse](
        message="Lab orders retrieved.",
        data=page_result.items,
        metadata=MetadataWithPagination(
            pagination=PaginationMeta(
                page=page_result.page,
                page_size=page_result.page_size,
                total_records=page_result.total_records,
                total_pages=page_result.total_pages,
            ),
        ),
    )


@router.get(
    "/{order_id}",
    response_model=SuccessResponse[LabOrderResponse],
    summary="Get a lab order",
    description="Return one lab order with its items, results and amendments.",
    responses={
        200: {"description": "Order returned."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def get_order(
    order_id: uuid.UUID = Path(description="Lab order UUID."),
    current_user: User = Depends(require_permission("lab.order.read")),
    service: LabService = Depends(get_lab_service),
) -> SuccessResponse[LabOrderResponse]:
    """Retrieve one lab order."""
    order = await service.get_order(_tenant_of(current_user), order_id)
    return SuccessResponse[LabOrderResponse](message="Lab order retrieved.", data=order)


@router.patch(
    "/{order_id}",
    response_model=SuccessResponse[LabOrderResponse],
    summary="Update a lab order",
    description=(
        "Change the priority or notes of an order that is not yet released or "
        "cancelled. The tests on an order cannot be changed."
    ),
    responses={
        200: {"description": "Order updated."},
        **_STATE_RESPONSE,
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def update_order(
    payload: UpdateLabOrderRequest,
    order_id: uuid.UUID = Path(description="Lab order UUID."),
    current_user: User = Depends(require_permission("lab.order.create")),
    service: LabService = Depends(get_lab_service),
) -> SuccessResponse[LabOrderResponse]:
    """Update a lab order's priority or notes."""
    order = await service.update_order(
        _tenant_of(current_user), order_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[LabOrderResponse](message="Lab order updated.", data=order)


@router.post(
    "/{order_id}/collect",
    response_model=SuccessResponse[LabOrderResponse],
    summary="Record samples as collected",
    description=(
        "Record samples as collected. Send no body to collect every "
        "outstanding sample with generated sample ids, or name items — each "
        "with an optional `sample_id` — to collect some.\n\n"
        "The order becomes `collected` once no sample is outstanding."
    ),
    responses={
        200: {"description": "Samples recorded."},
        409: {"description": "A sample id is already in use in this hospital."},
        **_STATE_RESPONSE,
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def collect_samples(
    order_id: uuid.UUID = Path(description="Lab order UUID."),
    payload: CollectSamplesRequest | None = Body(default=None),
    current_user: User = Depends(require_permission("lab.order.collect_sample")),
    service: LabService = Depends(get_lab_service),
) -> SuccessResponse[LabOrderResponse]:
    """Record sample collection (module spec §5 step 2)."""
    order = await service.collect_samples(
        _tenant_of(current_user), order_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[LabOrderResponse](message="Samples recorded.", data=order)


@router.post(
    "/{order_id}/enter-results",
    response_model=SuccessResponse[LabOrderResponse],
    summary="Enter results",
    description=(
        "Enter results for some or all items. The server flags each numeric "
        "result against the reference range for the patient's sex and age; "
        "a client cannot send a flag.\n\n"
        "Results may be re-entered until the order is released. The order is "
        "`in_progress` while results are missing and `results_entered` once "
        "every item has one."
    ),
    responses={
        200: {"description": "Results recorded."},
        **_STATE_RESPONSE,
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def enter_results(
    payload: EnterResultsRequest,
    order_id: uuid.UUID = Path(description="Lab order UUID."),
    current_user: User = Depends(require_permission("lab.order.enter_results")),
    service: LabService = Depends(get_lab_service),
) -> SuccessResponse[LabOrderResponse]:
    """Enter results (module spec §5 steps 3–4)."""
    order = await service.enter_results(
        _tenant_of(current_user), order_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[LabOrderResponse](message="Results recorded.", data=order)


@router.post(
    "/{order_id}/release",
    response_model=SuccessResponse[LabOrderResponse],
    summary="Release results",
    description=(
        "Release an order's results. Allowed only when every item has a "
        "result. The ordering doctor is notified. After release a result can "
        "only be changed through an amendment."
    ),
    responses={
        200: {"description": "Results released."},
        **_STATE_RESPONSE,
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def release_order(
    order_id: uuid.UUID = Path(description="Lab order UUID."),
    current_user: User = Depends(require_permission("lab.order.release")),
    service: LabService = Depends(get_lab_service),
) -> SuccessResponse[LabOrderResponse]:
    """Release results (module spec §5 step 5)."""
    order = await service.release_order(
        _tenant_of(current_user), order_id, actor_id=current_user.id
    )
    return SuccessResponse[LabOrderResponse](message="Results released.", data=order)


@router.post(
    "/{order_id}/cancel",
    response_model=SuccessResponse[LabOrderResponse],
    summary="Cancel a lab order",
    description=(
        "Cancel an order that has not been released, with a reason. The "
        "charge raised when the order was placed is not removed — correct the "
        "invoice named in `invoice_id` if needed."
    ),
    responses={
        200: {"description": "Order cancelled."},
        **_STATE_RESPONSE,
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def cancel_order(
    payload: CancelLabOrderRequest,
    order_id: uuid.UUID = Path(description="Lab order UUID."),
    current_user: User = Depends(require_permission("lab.order.cancel")),
    service: LabService = Depends(get_lab_service),
) -> SuccessResponse[LabOrderResponse]:
    """Cancel a lab order."""
    order = await service.cancel_order(
        _tenant_of(current_user), order_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[LabOrderResponse](message="Lab order cancelled.", data=order)


@router.post(
    "/{order_id}/items/{item_id}/amend",
    response_model=SuccessResponse[LabOrderResponse],
    summary="Amend a released result",
    description=(
        "Correct one result on a released order, with a reason. The previous "
        "value is kept in the item's `amendments`, the result is flagged "
        "again, and the ordering doctor is notified."
    ),
    responses={
        200: {"description": "Result amended."},
        **_STATE_RESPONSE,
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def amend_result(
    payload: AmendResultRequest,
    order_id: uuid.UUID = Path(description="Lab order UUID."),
    item_id: uuid.UUID = Path(description="Lab order item UUID."),
    current_user: User = Depends(require_permission("lab.order.amend")),
    service: LabService = Depends(get_lab_service),
) -> SuccessResponse[LabOrderResponse]:
    """Amend a released result (business rule 4)."""
    order = await service.amend_result(
        _tenant_of(current_user), order_id, item_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[LabOrderResponse](message="Result amended.", data=order)
