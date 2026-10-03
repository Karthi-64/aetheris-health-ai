"""Pydantic DTOs for the Billing module.

Request models enforce ``docs/modules/06-billing.md`` §11 before a service sees
the payload (``docs/07-SECURITY.md``, rule 5). Response models are the only
billing shapes that cross the API boundary.

**No request model has a total.** Business rule 7 says client-supplied totals
are ignored; here they are not merely ignored but impossible to send —
``extra="forbid"`` turns a ``total`` or ``line_total`` in a body into a 422, so
a client that believes it controls the amount finds out immediately.

**Money goes out as a decimal string** (``"1200.00"``), never a JSON number, so
no client parses it through a float (CLAUDE.md rule 6).
"""

from __future__ import annotations

# NOTE: runtime imports, not TYPE_CHECKING — Pydantic resolves field
# annotations against the module's real globals (backend/CLAUDE.md).
import re
from datetime import datetime  # noqa: TC003
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Self
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.billing import InvoiceStatus, PaymentMethod
from app.schemas.common import Page

if TYPE_CHECKING:
    from app.models.billing import Invoice, InvoiceItem, Payment, Service

__all__ = [
    "MAX_INVOICE_LINES",
    "CreateInvoiceRequest",
    "CreateServiceRequest",
    "InvoiceItemResponse",
    "InvoiceLineRequest",
    "InvoiceListResponse",
    "InvoiceResponse",
    "InvoiceStatus",
    "InvoiceSummaryResponse",
    "PaymentMethod",
    "PaymentRecordedResponse",
    "PaymentResponse",
    "RecordPaymentRequest",
    "ServiceListResponse",
    "ServiceResponse",
    "UpdateInvoiceRequest",
    "UpdateServiceRequest",
    "VoidInvoiceRequest",
]

#: Upper bound on lines per invoice. Far above any real bill; it exists so a
#: single request cannot ask the server to price an unbounded list.
MAX_INVOICE_LINES = 200

#: Catalog codes: letters, digits, hyphen and underscore. Stored uppercased.
_SERVICE_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]*$")

#: A monetary amount on the wire: ``NUMERIC(15, 2)``, never negative.
Money = Annotated[Decimal, Field(max_digits=15, decimal_places=2, ge=0)]

#: A line quantity: ``NUMERIC(10, 2)``, strictly positive (module spec §11).
Quantity = Annotated[Decimal, Field(max_digits=10, decimal_places=2, gt=0)]


def _strip_required(value: str, label: str) -> str:
    """Trim a required string and reject one that is blank.

    :param value: The submitted string.
    :param label: Human-readable field name, for the message.
    :returns: The trimmed value.
    :raises ValueError: If nothing is left after trimming.
    """
    stripped = value.strip()
    if not stripped:
        msg = f"{label} must not be blank."
        raise ValueError(msg)
    return stripped


def _blank_to_none(value: str | None) -> str | None:
    """Trim optional free text, collapsing blank to ``None``."""
    if value is None:
        return None
    return value.strip() or None


def _reject_explicit_nulls(model: BaseModel, required: frozenset[str]) -> None:
    """Refuse ``null`` for a column that cannot hold one.

    A PATCH that omits a field leaves it alone; one that sends ``null`` for a
    ``NOT NULL`` column is asking for something impossible and should hear so,
    rather than be silently ignored.

    :param model: The validated PATCH payload.
    :param required: Field names whose columns are ``NOT NULL``.
    :raises ValueError: If any of them was sent as ``null``.
    """
    nulled = sorted(
        name for name in model.model_fields_set & required if getattr(model, name) is None
    )
    if nulled:
        msg = f"Cannot be null: {', '.join(nulled)}."
        raise ValueError(msg)


# ── Services catalog ────────────────────────────────────────────────────────


class CreateServiceRequest(BaseModel):
    """Payload for ``POST /api/v1/services`` (module spec FR-1).

    ``hospital_id`` is absent by design: tenancy comes from the authenticated
    user.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "code": "CONS-GEN",
                "name": "General consultation",
                "category": "Consultation",
                "price": "500.00",
                "taxable": False,
            },
        },
    )

    code: str = Field(
        min_length=1, max_length=50, description="Catalog code. Uppercased automatically."
    )
    name: str = Field(min_length=1, max_length=200, description="Display name.")
    category: str | None = Field(default=None, max_length=100, description="Free-form grouping.")
    price: Money = Field(description="Unit price in the hospital's currency.")
    taxable: bool = Field(default=True, description="Whether the hospital's tax rate applies.")

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        """Uppercase the code and restrict it to catalog-safe characters."""
        code = value.strip().upper()
        if not _SERVICE_CODE_PATTERN.fullmatch(code):
            msg = "Code may contain only letters, digits, hyphens and underscores."
            raise ValueError(msg)
        return code

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """Trim the name and reject a blank one."""
        return _strip_required(value, "Name")

    @field_validator("category")
    @classmethod
    def _trim_category(cls, value: str | None) -> str | None:
        """Trim the category, collapsing blank to ``None``."""
        return _blank_to_none(value)


class UpdateServiceRequest(BaseModel):
    """Payload for ``PATCH /api/v1/services/{id}``.

    Every field optional; only fields present in the body are applied. ``code``
    is immutable and rejected — invoice lines and seed data refer to a service
    by it. Retire a service by setting ``is_active`` to ``false``; changing its
    price never rewrites invoices already raised, because lines copy the price.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, min_length=1, max_length=200, description="Display name."
    )
    category: str | None = Field(default=None, max_length=100, description="Free-form grouping.")
    price: Money | None = Field(default=None, description="Unit price.")
    taxable: bool | None = Field(default=None, description="Whether tax applies.")
    is_active: bool | None = Field(default=None, description="Whether it can be billed.")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        """Trim the name and reject a blank one."""
        return _strip_required(value, "Name") if value is not None else None

    @field_validator("category")
    @classmethod
    def _trim_category(cls, value: str | None) -> str | None:
        """Trim the category, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_nulls(self) -> Self:
        """Reject ``null`` for the ``NOT NULL`` columns."""
        _reject_explicit_nulls(self, frozenset({"name", "price", "taxable", "is_active"}))
        return self


class ServiceResponse(BaseModel):
    """A catalog service."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Service UUID.")
    code: str = Field(description="Catalog code, unique per hospital.")
    name: str = Field(description="Display name.")
    category: str | None = Field(description="Free-form grouping.")
    price: Decimal = Field(description="Unit price, as a decimal string.")
    taxable: bool = Field(description="Whether the hospital's tax rate applies.")
    is_active: bool = Field(description="Whether it can be added to new invoices.")
    created_at: datetime = Field(description="Creation timestamp (UTC).")
    updated_at: datetime = Field(description="Last update timestamp (UTC).")

    @classmethod
    def from_model(cls, service: Service) -> Self:
        """Build a DTO from an ORM instance.

        :param service: The ORM instance to convert.
        :returns: The populated DTO.
        """
        return cls.model_validate(service)


# ── Invoice requests ────────────────────────────────────────────────────────


class InvoiceLineRequest(BaseModel):
    """One line of an invoice, as a client describes it.

    Two shapes, and nothing in between:

    - **Catalog line** — ``service_id`` set. Price and taxability come from the
      catalog, so ``unit_price`` and ``taxable`` must be omitted. ``description``
      may be given to override the service's name on this invoice.
    - **Ad-hoc line** — no ``service_id``. ``description`` and ``unit_price`` are
      required; ``taxable`` defaults to ``false``.

    Neither shape carries a tax rate or a line total: the rate is the
    hospital's, and the total is computed (business rules 7 and 8).
    """

    model_config = ConfigDict(extra="forbid")

    service_id: UUID | None = Field(default=None, description="Catalog service to bill.")
    description: str | None = Field(
        default=None, max_length=200, description="What is being billed."
    )
    quantity: Quantity = Field(default=Decimal(1), description="Units billed. Must be positive.")
    unit_price: Money | None = Field(default=None, description="Price per unit. Ad-hoc lines only.")
    taxable: bool | None = Field(
        default=None, description="Whether tax applies. Ad-hoc lines only."
    )

    @field_validator("description")
    @classmethod
    def _trim_description(cls, value: str | None) -> str | None:
        """Trim the description, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        """Enforce the catalog-line / ad-hoc-line split."""
        if self.service_id is not None:
            if self.unit_price is not None or self.taxable is not None:
                msg = (
                    "A catalog line takes its price and tax from the service; "
                    "omit unit_price and taxable."
                )
                raise ValueError(msg)
            return self

        if self.description is None or self.unit_price is None:
            msg = "A line without a service_id needs both description and unit_price."
            raise ValueError(msg)
        return self


class CreateInvoiceRequest(BaseModel):
    """Payload for ``POST /api/v1/invoices`` (module spec §9).

    Creates a **draft**. ``status``, ``invoice_number`` and every total are
    absent by design — a new invoice is always a draft, its number is assigned
    at issue, and the totals are computed from the lines.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "patient_id": "3f1c6c1e-2c3d-4a5b-8c7d-9e0f1a2b3c4d",
                "items": [
                    {"service_id": "8a7b6c5d-4e3f-2a1b-0c9d-8e7f6a5b4c3d", "quantity": "1"},
                    {"description": "Dressing kit", "quantity": "2", "unit_price": "75.00"},
                ],
                "notes": "Walk-in, paid at the desk.",
            },
        },
    )

    patient_id: UUID = Field(description="Patient being billed.")
    appointment_id: UUID | None = Field(
        default=None, description="Appointment this invoice is for, if any."
    )
    items: list[InvoiceLineRequest] = Field(
        default_factory=list, max_length=MAX_INVOICE_LINES, description="Invoice lines, in order."
    )
    notes: str | None = Field(default=None, max_length=2000, description="Notes on the invoice.")

    @field_validator("notes")
    @classmethod
    def _trim_notes(cls, value: str | None) -> str | None:
        """Trim the notes, collapsing blank to ``None``."""
        return _blank_to_none(value)


class UpdateInvoiceRequest(BaseModel):
    """Payload for ``PATCH /api/v1/invoices/{id}`` — drafts only (AC-1).

    ``items``, when present, **replaces** the whole line set; it is not merged.
    Omit it to leave the lines alone. Patient and appointment cannot be changed
    — an invoice raised against the wrong patient is voided and re-raised, not
    quietly repointed.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[InvoiceLineRequest] | None = Field(
        default=None, max_length=MAX_INVOICE_LINES, description="Replacement line set."
    )
    notes: str | None = Field(default=None, max_length=2000, description="Notes on the invoice.")

    @field_validator("notes")
    @classmethod
    def _trim_notes(cls, value: str | None) -> str | None:
        """Trim the notes, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_something_changes(self) -> Self:
        """Reject an empty PATCH and a ``null`` line set."""
        if not self.model_fields_set:
            msg = "Provide at least one of: items, notes."
            raise ValueError(msg)
        _reject_explicit_nulls(self, frozenset({"items"}))
        return self


class VoidInvoiceRequest(BaseModel):
    """Payload for ``POST /api/v1/invoices/{id}/void``.

    A reason is **required** (business rule 4). A voided invoice keeps its
    number forever, so someone will eventually ask why that number bills nothing.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500, description="Why it is being voided.")

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        """Reject a whitespace-only reason."""
        return _strip_required(value, "Void reason")


class RecordPaymentRequest(BaseModel):
    """Payload for ``POST /api/v1/invoices/{id}/payments`` (module spec §5.4)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "amount": "1200.00",
                "method": "upi",
                "reference": "UPI-TXN-8899",
                "notes": "Paid at the front desk.",
            },
        },
    )

    amount: Decimal = Field(
        max_digits=15, decimal_places=2, gt=0, description="Amount received. Must be positive."
    )
    method: PaymentMethod = Field(description="cash, card, upi, bank_transfer, or insurance.")
    reference: str | None = Field(
        default=None, max_length=100, description="Transaction id, cheque number, or UPI reference."
    )
    notes: str | None = Field(default=None, max_length=2000, description="Notes on the payment.")

    @field_validator("reference", "notes")
    @classmethod
    def _trim_optional(cls, value: str | None) -> str | None:
        """Trim optional free text, collapsing blank to ``None``."""
        return _blank_to_none(value)


# ── Invoice responses ───────────────────────────────────────────────────────


class InvoiceItemResponse(BaseModel):
    """One invoice line."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Line UUID.")
    service_id: UUID | None = Field(description="Catalog service. Null for an ad-hoc line.")
    description: str = Field(description="What was billed.")
    quantity: Decimal = Field(description="Units billed.")
    unit_price: Decimal = Field(description="Price per unit.")
    tax_rate: Decimal = Field(description="Tax rate as a percentage.")
    line_total: Decimal = Field(description="Line amount including tax.")
    position: int = Field(description="0-based order on the invoice.")

    @classmethod
    def from_model(cls, item: InvoiceItem) -> Self:
        """Build a DTO from an ORM instance.

        :param item: The ORM instance to convert.
        :returns: The populated DTO.
        """
        return cls.model_validate(item)


class InvoiceSummaryResponse(BaseModel):
    """Compact invoice shape for lists."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Invoice UUID.")
    invoice_number: str | None = Field(description="Sequential number. Null while draft.")
    patient_id: UUID = Field(description="Patient UUID.")
    patient_name: str = Field(description="Patient's display name.")
    appointment_id: UUID | None = Field(description="Appointment billed, if any.")
    status: InvoiceStatus = Field(description="Current lifecycle state.")
    currency: str = Field(description="ISO 4217 code, inherited from the hospital.")
    total: Decimal = Field(description="Invoice total.")
    amount_paid: Decimal = Field(description="Sum of payments recorded.")
    balance_due: Decimal = Field(description="total minus amount_paid.")
    issued_at: datetime | None = Field(description="When it was issued (UTC).")
    created_at: datetime = Field(description="Creation timestamp (UTC).")

    @classmethod
    def from_model(cls, invoice: Invoice, *, currency: str) -> Self:
        """Build a summary DTO from an ORM instance.

        Reads ``patient``, which is ``lazy="joined"``, so this costs no extra
        query.

        :param invoice: The ORM instance to convert.
        :param currency: The hospital's currency code.
        :returns: The populated DTO.
        """
        return cls(
            id=invoice.id,
            invoice_number=invoice.invoice_number,
            patient_id=invoice.patient_id,
            patient_name=invoice.patient.full_name,
            appointment_id=invoice.appointment_id,
            status=invoice.status,
            currency=currency,
            total=invoice.total,
            amount_paid=invoice.amount_paid,
            balance_due=invoice.balance_due,
            issued_at=invoice.issued_at,
            created_at=invoice.created_at,
        )


class InvoiceResponse(BaseModel):
    """Full invoice record, lines included (module spec §9)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Invoice UUID.")
    hospital_id: UUID = Field(description="Owning hospital (tenant) UUID.")
    invoice_number: str | None = Field(description="Sequential number. Null while draft.")
    patient_id: UUID = Field(description="Patient UUID.")
    patient_name: str = Field(description="Patient's display name.")
    appointment_id: UUID | None = Field(description="Appointment billed, if any.")
    status: InvoiceStatus = Field(description="Current lifecycle state.")
    currency: str = Field(description="ISO 4217 code, inherited from the hospital.")
    items: list[InvoiceItemResponse] = Field(description="Lines, in invoice order.")
    subtotal: Decimal = Field(description="Sum of line amounts before tax.")
    tax_amount: Decimal = Field(description="Sum of line tax.")
    discount_amount: Decimal = Field(description="Invoice-level discount.")
    total: Decimal = Field(description="subtotal + tax_amount - discount_amount.")
    amount_paid: Decimal = Field(description="Sum of payments recorded.")
    balance_due: Decimal = Field(description="total minus amount_paid.")
    notes: str | None = Field(description="Notes on the invoice.")
    issued_at: datetime | None = Field(description="When it was issued (UTC).")
    voided_at: datetime | None = Field(description="When it was voided (UTC).")
    void_reason: str | None = Field(description="Why it was voided, if it was.")
    created_at: datetime = Field(description="Creation timestamp (UTC).")
    updated_at: datetime = Field(description="Last update timestamp (UTC).")

    @classmethod
    def from_model(cls, invoice: Invoice, *, currency: str) -> Self:
        """Build a full DTO from an ORM instance.

        :param invoice: The ORM instance to convert.
        :param currency: The hospital's currency code.
        :returns: The populated DTO.
        """
        return cls(
            id=invoice.id,
            hospital_id=invoice.hospital_id,
            invoice_number=invoice.invoice_number,
            patient_id=invoice.patient_id,
            patient_name=invoice.patient.full_name,
            appointment_id=invoice.appointment_id,
            status=invoice.status,
            currency=currency,
            items=[InvoiceItemResponse.from_model(item) for item in invoice.items],
            subtotal=invoice.subtotal,
            tax_amount=invoice.tax_amount,
            discount_amount=invoice.discount_amount,
            total=invoice.total,
            amount_paid=invoice.amount_paid,
            balance_due=invoice.balance_due,
            notes=invoice.notes,
            issued_at=invoice.issued_at,
            voided_at=invoice.voided_at,
            void_reason=invoice.void_reason,
            created_at=invoice.created_at,
            updated_at=invoice.updated_at,
        )


class PaymentResponse(BaseModel):
    """One recorded payment."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Payment UUID.")
    invoice_id: UUID = Field(description="Invoice the payment was made against.")
    amount: Decimal = Field(description="Amount received.")
    method: PaymentMethod = Field(description="How it was paid.")
    reference: str | None = Field(description="Transaction id, cheque number, or UPI reference.")
    notes: str | None = Field(description="Notes on the payment.")
    received_by: UUID = Field(description="User who recorded the payment.")
    received_at: datetime = Field(description="When it was recorded (UTC).")

    @classmethod
    def from_model(cls, payment: Payment) -> Self:
        """Build a DTO from an ORM instance.

        :param payment: The ORM instance to convert.
        :returns: The populated DTO.
        """
        return cls.model_validate(payment)


class PaymentRecordedResponse(BaseModel):
    """Body of ``POST /invoices/{id}/payments`` (module spec §9).

    The payment plus the invoice as it stands afterwards, so the client can
    update the balance and status it is showing without a second request.
    """

    payment: PaymentResponse = Field(description="The payment recorded.")
    invoice: InvoiceSummaryResponse = Field(description="The invoice after this payment.")


#: One page of catalog services — the body of a list response.
ServiceListResponse = Page[ServiceResponse]

#: One page of invoice summaries — the body of a list response.
InvoiceListResponse = Page[InvoiceSummaryResponse]
