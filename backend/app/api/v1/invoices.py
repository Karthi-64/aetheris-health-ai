"""Invoice and payment API routes.

Implements the invoice endpoints in ``docs/modules/06-billing.md`` §9 that this
sprint covers. Routes parse input, delegate to
:class:`~app.services.billing_service.BillingService`, and wrap the result in
the standard envelope. No business logic, no database access.

**Tenancy.** ``hospital_id`` always comes from the authenticated user.

**Permissions.** Every endpoint declares one from §10. Issue, void and payment
each carry their own code, so someone who may take a payment cannot also void
an invoice.

**Narrower codes.** Reading and paying each have a narrower sibling
(module spec §3): ``invoice.read.own`` limits a doctor to the invoices for
their own visits, and ``invoice.payment.record.cash`` limits a receptionist to
cash. Those endpoints accept either code; the route works out which the caller
holds and tells the service, which applies the limit.

**Idempotency.** ``POST /invoices/{id}/payments`` requires an
``Idempotency-Key`` header (business rule 6). A retry with the same key returns
the original payment and ``200`` rather than recording a second one.

**Not here yet.** ``approve-discount``, ``refund``, ``pdf`` and ``ai-explain``
from §9 are not implemented; calling them is a 404.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Header, Path, Query, Response, status

from app.api.dependencies.auth import (
    require_any_permission,
    require_permission,
    user_has_permission,
)
from app.api.dependencies.services import get_billing_service
from app.core.exceptions import BusinessRuleError
from app.models.billing import InvoiceStatus
from app.models.user import User
from app.schemas.billing import (
    CreateInvoiceRequest,
    InvoiceResponse,
    InvoiceSummaryResponse,
    PaymentRecordedResponse,
    PaymentResponse,
    RecordPaymentRequest,
    UpdateInvoiceRequest,
    VoidInvoiceRequest,
)
from app.schemas.common import (
    MetadataWithPagination,
    PaginatedResponse,
    PaginationMeta,
    PaginationParams,
    SuccessResponse,
)
from app.services.billing_service import BillingService

router = APIRouter(prefix="/invoices", tags=["Billing — Invoices"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    422: {"description": "Request failed validation."},
}

_NOT_FOUND_RESPONSE: dict[int | str, dict[str, str]] = {
    404: {"description": "Invoice not found in this hospital."},
}

_LIFECYCLE_RESPONSES: dict[int | str, dict[str, str]] = {
    400: {"description": "The invoice's current status does not allow this."},
    **_NOT_FOUND_RESPONSE,
    **_COMMON_RESPONSES,
}


#: Either opens the read endpoints; holding only the second narrows the result.
_READ_PERMISSIONS = ("invoice.read", "invoice.read.own")

#: Either opens payment recording; holding only the second limits it to cash.
_PAYMENT_PERMISSIONS = ("invoice.payment.record", "invoice.payment.record.cash")


def _own_visits_scope(current_user: User) -> uuid.UUID | None:
    """Return the user to scope invoice reads to, or ``None`` for no scope.

    A caller holding ``invoice.read`` sees every invoice in the hospital. One
    holding only ``invoice.read.own`` sees the invoices for their own visits.

    :param current_user: The authenticated user.
    :returns: The user's UUID when their reads must be scoped, else ``None``.
    """
    if user_has_permission(current_user, "invoice.read"):
        return None
    return current_user.id


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the request acts within.

    A Super Admin has no ``hospital_id``, so there is no tenant to scope
    invoices to. Rejected rather than silently querying across tenants.

    :param current_user: The authenticated user.
    :returns: The hospital UUID to scope every query by.
    :raises BusinessRuleError: If the user belongs to no hospital.
    """
    if current_user.hospital_id is None:
        msg = "This account is not scoped to a hospital, so invoices cannot be accessed."
        raise BusinessRuleError(msg)
    return current_user.hospital_id


# ── Drafting ────────────────────────────────────────────────────────────────


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[InvoiceResponse],
    summary="Create a draft invoice",
    description=(
        "Create a **draft** invoice (module spec §9).\n\n"
        "Each line is either a catalog line (`service_id`, with price and tax "
        "taken from the catalog) or an ad-hoc line (`description` and "
        "`unit_price`). Totals are computed by the server; a body carrying "
        "`total` or `line_total` is rejected.\n\n"
        "A draft has no `invoice_number` — that is assigned when it is issued. "
        "An appointment can have only one live invoice at a time."
    ),
    responses={
        201: {"description": "Draft created."},
        409: {"description": "The appointment already has an invoice."},
        **_COMMON_RESPONSES,
    },
)
async def create_invoice(
    payload: CreateInvoiceRequest,
    current_user: User = Depends(require_permission("invoice.create")),
    service: BillingService = Depends(get_billing_service),
) -> SuccessResponse[InvoiceResponse]:
    """Create a draft invoice."""
    invoice = await service.create_invoice(
        _tenant_of(current_user), payload, actor_id=current_user.id
    )
    return SuccessResponse[InvoiceResponse](message="Invoice draft created.", data=invoice)


@router.get(
    "",
    response_model=PaginatedResponse[InvoiceSummaryResponse],
    summary="List invoices",
    description=(
        "Return a page of invoices, newest first.\n\n"
        "Filters: `patient_id`, `status`, and an issue-date range. "
        "`issued_from` and `issued_to` are calendar dates in the **hospital's "
        "timezone**, both inclusive. Drafts have no issue date, so a date "
        "filter never returns them.\n\n"
        "A caller holding only `invoice.read.own` gets the invoices for "
        "appointments where they are the doctor, and no others."
    ),
    responses={200: {"description": "Page of invoices returned."}, **_COMMON_RESPONSES},
)
async def list_invoices(
    patient_id: uuid.UUID | None = Query(None, description="Filter by patient."),
    invoice_status: InvoiceStatus | None = Query(
        None, alias="status", description="Filter by lifecycle status."
    ),
    issued_from: date | None = Query(None, description="Earliest issue date, YYYY-MM-DD."),
    issued_to: date | None = Query(None, description="Latest issue date, YYYY-MM-DD."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_any_permission(*_READ_PERMISSIONS)),
    service: BillingService = Depends(get_billing_service),
) -> PaginatedResponse[InvoiceSummaryResponse]:
    """List invoices with the module spec §9 filters."""
    page_result = await service.list_invoices(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        patient_id=patient_id,
        status=invoice_status,
        issued_from=issued_from,
        issued_to=issued_to,
        own_visits_of=_own_visits_scope(current_user),
    )
    return PaginatedResponse[InvoiceSummaryResponse](
        message="Invoices retrieved.",
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
    "/{invoice_id}",
    response_model=SuccessResponse[InvoiceResponse],
    summary="Get an invoice",
    description=(
        "Return one invoice with its lines.\n\n"
        "For a caller holding only `invoice.read.own`, an invoice that is not "
        "for one of their own visits is a `404`, the same as one that does not "
        "exist."
    ),
    responses={
        200: {"description": "Invoice returned."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def get_invoice(
    invoice_id: uuid.UUID = Path(description="Invoice UUID."),
    current_user: User = Depends(require_any_permission(*_READ_PERMISSIONS)),
    service: BillingService = Depends(get_billing_service),
) -> SuccessResponse[InvoiceResponse]:
    """Retrieve one invoice by UUID."""
    invoice = await service.get_invoice(
        _tenant_of(current_user), invoice_id, own_visits_of=_own_visits_scope(current_user)
    )
    return SuccessResponse[InvoiceResponse](message="Invoice retrieved.", data=invoice)


@router.patch(
    "/{invoice_id}",
    response_model=SuccessResponse[InvoiceResponse],
    summary="Edit a draft invoice",
    description=(
        "Edit a draft (module spec §5.2). **Drafts only** — an issued invoice "
        "cannot be changed and returns `400`.\n\n"
        "`items`, when present, **replaces** the whole line set and the totals "
        "are recomputed. Omit `items` to leave the lines alone."
    ),
    responses={200: {"description": "Draft updated."}, **_LIFECYCLE_RESPONSES},
)
async def update_invoice(
    payload: UpdateInvoiceRequest,
    invoice_id: uuid.UUID = Path(description="Invoice UUID."),
    current_user: User = Depends(require_permission("invoice.update")),
    service: BillingService = Depends(get_billing_service),
) -> SuccessResponse[InvoiceResponse]:
    """Edit a draft invoice."""
    invoice = await service.update_invoice(
        _tenant_of(current_user), invoice_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[InvoiceResponse](message="Invoice draft updated.", data=invoice)


# ── Lifecycle ───────────────────────────────────────────────────────────────


@router.post(
    "/{invoice_id}/issue",
    response_model=SuccessResponse[InvoiceResponse],
    summary="Issue an invoice",
    description=(
        "Issue a draft (module spec §5.3): recompute and freeze its totals and "
        "assign the hospital's next invoice number.\n\n"
        "Numbers are sequential and gap-free per hospital. The draft must have "
        "at least one line. A zero-total invoice becomes `paid` immediately."
    ),
    responses={200: {"description": "Invoice issued."}, **_LIFECYCLE_RESPONSES},
)
async def issue_invoice(
    invoice_id: uuid.UUID = Path(description="Invoice UUID."),
    current_user: User = Depends(require_permission("invoice.issue")),
    service: BillingService = Depends(get_billing_service),
) -> SuccessResponse[InvoiceResponse]:
    """Move a draft invoice to ``issued``."""
    invoice = await service.issue_invoice(
        _tenant_of(current_user), invoice_id, actor_id=current_user.id
    )
    return SuccessResponse[InvoiceResponse](message="Invoice issued.", data=invoice)


@router.post(
    "/{invoice_id}/void",
    response_model=SuccessResponse[InvoiceResponse],
    summary="Void an invoice",
    description=(
        "Void an issued invoice (module spec §5.6). A reason is required.\n\n"
        "Allowed only while the invoice is `issued` and has taken no payment. "
        "The invoice keeps its number, so the series stays gap-free."
    ),
    responses={200: {"description": "Invoice voided."}, **_LIFECYCLE_RESPONSES},
)
async def void_invoice(
    payload: VoidInvoiceRequest,
    invoice_id: uuid.UUID = Path(description="Invoice UUID."),
    current_user: User = Depends(require_permission("invoice.void")),
    service: BillingService = Depends(get_billing_service),
) -> SuccessResponse[InvoiceResponse]:
    """Move an issued invoice to ``void``."""
    invoice = await service.void_invoice(
        _tenant_of(current_user), invoice_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[InvoiceResponse](message="Invoice voided.", data=invoice)


# ── Payments ────────────────────────────────────────────────────────────────


@router.post(
    "/{invoice_id}/payments",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[PaymentRecordedResponse],
    summary="Record a payment",
    description=(
        "Record a payment against an issued invoice (module spec §5.4).\n\n"
        "An `Idempotency-Key` header is **required**. Retrying with the same "
        "key returns the original payment with `200` instead of recording a "
        "second one, so a client retry after a timeout is safe. Reusing a key "
        "for a different invoice, amount or method is a `409`.\n\n"
        "The amount cannot exceed the balance due. The invoice moves to "
        "`partially_paid`, or to `paid` once the balance reaches zero.\n\n"
        "A caller holding only `invoice.payment.record.cash` may record "
        "`cash` and nothing else; any other method is a `403`."
    ),
    responses={
        201: {"description": "Payment recorded."},
        200: {"description": "Idempotent replay — the original payment."},
        400: {"description": "The invoice cannot take this payment."},
        409: {"description": "The Idempotency-Key was used for a different payment."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
        # After the spread: this endpoint's 403 means more than the common one.
        403: {"description": "No payment permission, or a non-cash method from a cash-only user."},
    },
)
async def record_payment(
    payload: RecordPaymentRequest,
    response: Response,
    invoice_id: uuid.UUID = Path(description="Invoice UUID."),
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=16,
        max_length=100,
        description="Client-generated key making retries safe. Required.",
    ),
    current_user: User = Depends(require_any_permission(*_PAYMENT_PERMISSIONS)),
    service: BillingService = Depends(get_billing_service),
) -> SuccessResponse[PaymentRecordedResponse]:
    """Record a payment, idempotently."""
    result, created = await service.record_payment(
        _tenant_of(current_user),
        invoice_id,
        payload,
        idempotency_key=idempotency_key,
        actor_id=current_user.id,
        cash_only=not user_has_permission(current_user, "invoice.payment.record"),
    )
    if not created:
        # A replay is not a new payment. Returning 201 again would tell the
        # client it took the money twice.
        response.status_code = status.HTTP_200_OK
        return SuccessResponse[PaymentRecordedResponse](
            message="Payment already recorded with this key.", data=result
        )
    return SuccessResponse[PaymentRecordedResponse](message="Payment recorded.", data=result)


@router.get(
    "/{invoice_id}/payments",
    response_model=SuccessResponse[list[PaymentResponse]],
    summary="List an invoice's payments",
    description="Return every payment recorded against an invoice, oldest first.",
    responses={
        200: {"description": "Payments returned."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def list_payments(
    invoice_id: uuid.UUID = Path(description="Invoice UUID."),
    current_user: User = Depends(require_any_permission(*_READ_PERMISSIONS)),
    service: BillingService = Depends(get_billing_service),
) -> SuccessResponse[list[PaymentResponse]]:
    """Return an invoice's payments."""
    payments = await service.list_payments(
        _tenant_of(current_user), invoice_id, own_visits_of=_own_visits_scope(current_user)
    )
    return SuccessResponse[list[PaymentResponse]](message="Payments retrieved.", data=payments)
