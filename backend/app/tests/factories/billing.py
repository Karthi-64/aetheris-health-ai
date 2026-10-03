"""Factories for billing test data.

Every factory returns a *valid* object by default. Tests that need an invalid
one override exactly the field under test.

Money is always built from strings, never floats, so a factory cannot be the
place a binary-fraction error enters a test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from app.models.billing import Invoice, InvoiceItem, InvoiceStatus, Payment, PaymentMethod, Service
from app.schemas.billing import (
    CreateInvoiceRequest,
    CreateServiceRequest,
    RecordPaymentRequest,
    UpdateInvoiceRequest,
    UpdateServiceRequest,
    VoidInvoiceRequest,
)

__all__ = [
    "build_create_invoice_request",
    "build_create_service_request",
    "build_invoice_item_model",
    "build_invoice_model",
    "build_invoice_payload",
    "build_payment_model",
    "build_payment_payload",
    "build_record_payment_request",
    "build_service_model",
    "build_service_payload",
    "build_update_invoice_request",
    "build_update_service_request",
    "build_void_request",
]

#: A fixed instant for the timestamp columns the database would normally fill.
_NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


# ── Services catalog ────────────────────────────────────────────────────────


def build_service_payload(**overrides: Any) -> dict[str, Any]:
    """Build a valid ``POST /services`` body as a plain dict.

    :param overrides: Field values to replace.
    :returns: A dict suitable for ``CreateServiceRequest.model_validate``.
    """
    payload: dict[str, Any] = {
        "code": "CONS-GEN",
        "name": "General consultation",
        "category": "Consultation",
        "price": "500.00",
        "taxable": False,
    }
    payload.update(overrides)
    return payload


def build_create_service_request(**overrides: Any) -> CreateServiceRequest:
    """Build a validated :class:`CreateServiceRequest`."""
    return CreateServiceRequest.model_validate(build_service_payload(**overrides))


def build_update_service_request(**fields: Any) -> UpdateServiceRequest:
    """Build a validated :class:`UpdateServiceRequest` with exactly ``fields`` set."""
    return UpdateServiceRequest.model_validate(fields)


def build_service_model(**overrides: Any) -> Service:
    """Build an unattached :class:`~app.models.billing.Service`.

    :param overrides: Column values to replace.
    :returns: A detached Service suitable for unit tests.
    """
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "hospital_id": uuid.uuid4(),
        "code": "CONS-GEN",
        "name": "General consultation",
        "category": "Consultation",
        "price": Decimal("500.00"),
        "taxable": False,
        "is_active": True,
        "created_at": _NOW,
        "updated_at": _NOW,
        "deleted_at": None,
    }
    values.update(overrides)
    return Service(**values)


# ── Invoices ────────────────────────────────────────────────────────────────


def build_invoice_payload(**overrides: Any) -> dict[str, Any]:
    """Build a valid ``POST /invoices`` body as a plain dict.

    Defaults to a single ad-hoc line, so the payload is valid without a
    catalog service existing.

    :param overrides: Field values to replace.
    :returns: A dict suitable for ``CreateInvoiceRequest.model_validate``.
    """
    payload: dict[str, Any] = {
        "patient_id": str(uuid.uuid4()),
        "items": [{"description": "Dressing kit", "quantity": "2", "unit_price": "75.00"}],
        "notes": "Walk-in, paid at the desk.",
    }
    payload.update(overrides)
    return payload


def build_create_invoice_request(**overrides: Any) -> CreateInvoiceRequest:
    """Build a validated :class:`CreateInvoiceRequest`."""
    return CreateInvoiceRequest.model_validate(build_invoice_payload(**overrides))


def build_update_invoice_request(**fields: Any) -> UpdateInvoiceRequest:
    """Build a validated :class:`UpdateInvoiceRequest` with exactly ``fields`` set."""
    return UpdateInvoiceRequest.model_validate(fields)


def build_void_request(**overrides: Any) -> VoidInvoiceRequest:
    """Build a validated :class:`VoidInvoiceRequest`."""
    payload: dict[str, Any] = {"reason": "Raised against the wrong patient"}
    payload.update(overrides)
    return VoidInvoiceRequest.model_validate(payload)


def build_invoice_item_model(**overrides: Any) -> InvoiceItem:
    """Build an unattached :class:`~app.models.billing.InvoiceItem`.

    Defaults to one untaxed unit at 500.00.

    :param overrides: Column values to replace.
    :returns: A detached InvoiceItem suitable for unit tests.
    """
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "hospital_id": uuid.uuid4(),
        "service_id": None,
        "description": "General consultation",
        "quantity": Decimal("1.00"),
        "unit_price": Decimal("500.00"),
        "tax_rate": Decimal("0.00"),
        "line_total": Decimal("500.00"),
        "position": 0,
    }
    values.update(overrides)
    return InvoiceItem(**values)


def build_invoice_model(**overrides: Any) -> Invoice:
    """Build an unattached :class:`~app.models.billing.Invoice`.

    Defaults to a **draft** with one 500.00 line. ``patient`` is a double with
    a ``full_name``, because the response DTOs read it and a real Patient would
    drag the patient factory into every billing unit test.

    :param overrides: Column values to replace. Pass ``items=[...]`` to control
        the lines; pass ``status`` with a matching ``invoice_number`` for
        anything past draft.
    :returns: A detached Invoice suitable for unit tests.
    """
    hospital_id = overrides.pop("hospital_id", uuid.uuid4())
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "hospital_id": hospital_id,
        "patient_id": uuid.uuid4(),
        "appointment_id": None,
        "invoice_number": None,
        "subtotal": Decimal("500.00"),
        "tax_amount": Decimal("0.00"),
        "discount_amount": Decimal("0.00"),
        "discount_reason": None,
        "discount_approved_by": None,
        "total": Decimal("500.00"),
        "amount_paid": Decimal("0.00"),
        "status": InvoiceStatus.DRAFT,
        "notes": None,
        "issued_at": None,
        "voided_at": None,
        "void_reason": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "deleted_at": None,
        "items": [build_invoice_item_model(hospital_id=hospital_id)],
    }
    values.update(overrides)
    invoice = Invoice(**values)

    patient = MagicMock()
    patient.full_name = "Ananya Rao"
    # Bypass the instrumented relationship: assigning a MagicMock through it
    # would be rejected, and a detached unit-test instance never loads it.
    invoice.__dict__["patient"] = patient
    return invoice


# ── Payments ────────────────────────────────────────────────────────────────


def build_payment_payload(**overrides: Any) -> dict[str, Any]:
    """Build a valid ``POST /invoices/{id}/payments`` body as a plain dict."""
    payload: dict[str, Any] = {
        "amount": "200.00",
        "method": "upi",
        "reference": "UPI-TXN-8899",
        "notes": "Paid at the front desk.",
    }
    payload.update(overrides)
    return payload


def build_record_payment_request(**overrides: Any) -> RecordPaymentRequest:
    """Build a validated :class:`RecordPaymentRequest`."""
    return RecordPaymentRequest.model_validate(build_payment_payload(**overrides))


def build_payment_model(**overrides: Any) -> Payment:
    """Build an unattached :class:`~app.models.billing.Payment`.

    :param overrides: Column values to replace.
    :returns: A detached Payment suitable for unit tests.
    """
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "hospital_id": uuid.uuid4(),
        "invoice_id": uuid.uuid4(),
        "amount": Decimal("200.00"),
        "method": PaymentMethod.UPI,
        "reference": "UPI-TXN-8899",
        "notes": None,
        "received_by": uuid.uuid4(),
        "received_at": _NOW,
        "idempotency_key": "pay-key-0000000001",
        "created_at": _NOW,
        "updated_at": _NOW,
        "deleted_at": None,
    }
    values.update(overrides)
    return Payment(**values)
