"""Validation tests for the billing DTOs.

These cover ``docs/modules/06-billing.md`` §11 at the API boundary: what a
client may and may not put in a body. The rule that matters most is the one
that is easiest to regress — no request can carry a total (business rule 7).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.billing import InvoiceStatus, PaymentMethod
from app.schemas.billing import (
    MAX_INVOICE_LINES,
    CreateInvoiceRequest,
    CreateServiceRequest,
    InvoiceLineRequest,
    InvoiceResponse,
    InvoiceSummaryResponse,
    PaymentResponse,
    RecordPaymentRequest,
    ServiceResponse,
    UpdateInvoiceRequest,
    UpdateServiceRequest,
    VoidInvoiceRequest,
)
from app.tests.factories import (
    build_invoice_model,
    build_invoice_payload,
    build_payment_model,
    build_payment_payload,
    build_service_model,
    build_service_payload,
)


class TestCreateServiceRequest:
    def test_accepts_a_valid_payload(self) -> None:
        request = CreateServiceRequest.model_validate(build_service_payload())

        assert request.code == "CONS-GEN"
        assert request.price == Decimal("500.00")

    def test_uppercases_and_trims_the_code(self) -> None:
        request = CreateServiceRequest.model_validate(build_service_payload(code="  cons-gen "))

        assert request.code == "CONS-GEN"

    @pytest.mark.parametrize("code", ["CONS GEN", "-CONS", "CONS/GEN", "   "])
    def test_rejects_an_unsafe_code(self, code: str) -> None:
        with pytest.raises(ValidationError):
            CreateServiceRequest.model_validate(build_service_payload(code=code))

    def test_rejects_a_blank_name(self) -> None:
        with pytest.raises(ValidationError, match="Name must not be blank"):
            CreateServiceRequest.model_validate(build_service_payload(name="   "))

    def test_blank_category_becomes_none(self) -> None:
        request = CreateServiceRequest.model_validate(build_service_payload(category="  "))

        assert request.category is None

    @pytest.mark.parametrize("price", ["-0.01", "1.234", "99999999999999.99"])
    def test_rejects_a_price_outside_numeric_15_2(self, price: str) -> None:
        with pytest.raises(ValidationError):
            CreateServiceRequest.model_validate(build_service_payload(price=price))

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            CreateServiceRequest.model_validate(
                build_service_payload(hospital_id=str(uuid.uuid4()))
            )


class TestUpdateServiceRequest:
    def test_only_sent_fields_are_set(self) -> None:
        request = UpdateServiceRequest.model_validate({"price": "650.00"})

        assert request.model_dump(exclude_unset=True) == {"price": Decimal("650.00")}

    def test_code_is_immutable(self) -> None:
        with pytest.raises(ValidationError):
            UpdateServiceRequest.model_validate({"code": "NEW"})

    @pytest.mark.parametrize("field", ["name", "price", "taxable", "is_active"])
    def test_rejects_null_for_a_required_column(self, field: str) -> None:
        with pytest.raises(ValidationError, match="Cannot be null"):
            UpdateServiceRequest.model_validate({field: None})

    def test_category_may_be_cleared(self) -> None:
        request = UpdateServiceRequest.model_validate({"category": None})

        assert request.model_dump(exclude_unset=True) == {"category": None}

    def test_trims_the_name(self) -> None:
        assert UpdateServiceRequest.model_validate({"name": " X-ray "}).name == "X-ray"


class TestInvoiceLineRequest:
    def test_catalog_line(self) -> None:
        service_id = uuid.uuid4()

        line = InvoiceLineRequest.model_validate({"service_id": str(service_id), "quantity": "2"})

        assert line.service_id == service_id
        assert line.quantity == Decimal(2)
        assert line.unit_price is None

    def test_catalog_line_may_override_the_description(self) -> None:
        line = InvoiceLineRequest.model_validate(
            {"service_id": str(uuid.uuid4()), "description": "Follow-up consultation"}
        )

        assert line.description == "Follow-up consultation"

    @pytest.mark.parametrize("extra", [{"unit_price": "10.00"}, {"taxable": True}])
    def test_catalog_line_cannot_set_its_own_price_or_tax(self, extra: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="omit unit_price and taxable"):
            InvoiceLineRequest.model_validate({"service_id": str(uuid.uuid4()), **extra})

    def test_ad_hoc_line(self) -> None:
        line = InvoiceLineRequest.model_validate(
            {"description": "Dressing kit", "unit_price": "75.00"}
        )

        assert line.quantity == Decimal(1)
        assert line.taxable is None

    @pytest.mark.parametrize(
        "body", [{"description": "Dressing kit"}, {"unit_price": "75.00"}, {"description": "  "}]
    )
    def test_ad_hoc_line_needs_description_and_price(self, body: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="needs both description and unit_price"):
            InvoiceLineRequest.model_validate(body)

    @pytest.mark.parametrize("quantity", ["0", "-1", "1.234"])
    def test_rejects_a_bad_quantity(self, quantity: str) -> None:
        with pytest.raises(ValidationError):
            InvoiceLineRequest.model_validate(
                {"description": "Dressing kit", "unit_price": "75.00", "quantity": quantity}
            )

    @pytest.mark.parametrize("field", ["line_total", "tax_rate", "total"])
    def test_a_line_cannot_carry_computed_amounts(self, field: str) -> None:
        # Business rule 7: the client does not get to say what a line costs.
        with pytest.raises(ValidationError):
            InvoiceLineRequest.model_validate(
                {"description": "Dressing kit", "unit_price": "75.00", field: "1.00"}
            )


class TestCreateInvoiceRequest:
    def test_accepts_a_valid_payload(self) -> None:
        request = CreateInvoiceRequest.model_validate(build_invoice_payload())

        assert len(request.items) == 1
        assert request.appointment_id is None

    def test_an_empty_draft_is_allowed(self) -> None:
        request = CreateInvoiceRequest.model_validate(build_invoice_payload(items=[]))

        assert request.items == []

    @pytest.mark.parametrize(
        "field", ["total", "subtotal", "tax_amount", "amount_paid", "status", "invoice_number"]
    )
    def test_server_owned_fields_are_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            CreateInvoiceRequest.model_validate(build_invoice_payload(**{field: "1"}))

    def test_caps_the_number_of_lines(self) -> None:
        line = {"description": "Dressing kit", "unit_price": "1.00"}

        with pytest.raises(ValidationError):
            CreateInvoiceRequest.model_validate(
                build_invoice_payload(items=[line] * (MAX_INVOICE_LINES + 1))
            )

    def test_blank_notes_become_none(self) -> None:
        assert CreateInvoiceRequest.model_validate(build_invoice_payload(notes=" ")).notes is None


class TestUpdateInvoiceRequest:
    def test_notes_only(self) -> None:
        request = UpdateInvoiceRequest.model_validate({"notes": "Corrected"})

        assert request.items is None
        assert request.model_fields_set == {"notes"}

    def test_notes_may_be_cleared(self) -> None:
        request = UpdateInvoiceRequest.model_validate({"notes": None})

        assert request.model_fields_set == {"notes"}

    def test_an_empty_patch_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Provide at least one of"):
            UpdateInvoiceRequest.model_validate({})

    def test_items_cannot_be_null(self) -> None:
        with pytest.raises(ValidationError, match="Cannot be null: items"):
            UpdateInvoiceRequest.model_validate({"items": None})

    @pytest.mark.parametrize("field", ["patient_id", "appointment_id", "status", "total"])
    def test_cannot_repoint_or_restate_the_invoice(self, field: str) -> None:
        with pytest.raises(ValidationError):
            UpdateInvoiceRequest.model_validate({"notes": "x", field: str(uuid.uuid4())})


class TestVoidInvoiceRequest:
    def test_trims_the_reason(self) -> None:
        assert VoidInvoiceRequest.model_validate({"reason": " Wrong patient "}).reason == (
            "Wrong patient"
        )

    @pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}])
    def test_a_reason_is_required(self, body: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            VoidInvoiceRequest.model_validate(body)


class TestRecordPaymentRequest:
    def test_accepts_a_valid_payload(self) -> None:
        request = RecordPaymentRequest.model_validate(build_payment_payload())

        assert request.amount == Decimal("200.00")
        assert request.method is PaymentMethod.UPI

    @pytest.mark.parametrize("amount", ["0", "-5.00", "1.999"])
    def test_rejects_a_bad_amount(self, amount: str) -> None:
        with pytest.raises(ValidationError):
            RecordPaymentRequest.model_validate(build_payment_payload(amount=amount))

    def test_rejects_an_unknown_method(self) -> None:
        with pytest.raises(ValidationError):
            RecordPaymentRequest.model_validate(build_payment_payload(method="cheque"))

    def test_blank_reference_becomes_none(self) -> None:
        request = RecordPaymentRequest.model_validate(build_payment_payload(reference="  "))

        assert request.reference is None


class TestResponses:
    def test_money_serializes_as_a_decimal_string(self) -> None:
        # CLAUDE.md rule 6: a JSON number would be parsed as a float by the client.
        body = ServiceResponse.from_model(build_service_model()).model_dump(mode="json")

        assert body["price"] == "500.00"

    def test_invoice_response_carries_lines_balance_and_currency(self) -> None:
        invoice = build_invoice_model(
            status=InvoiceStatus.PARTIALLY_PAID,
            invoice_number="INV-2026-000001",
            amount_paid=Decimal("200.00"),
        )

        body = InvoiceResponse.from_model(invoice, currency="INR").model_dump(mode="json")

        assert body["currency"] == "INR"
        assert body["patient_name"] == "Ananya Rao"
        assert body["total"] == "500.00"
        assert body["amount_paid"] == "200.00"
        assert body["balance_due"] == "300.00"
        assert [item["line_total"] for item in body["items"]] == ["500.00"]

    def test_invoice_summary_omits_lines(self) -> None:
        body = InvoiceSummaryResponse.from_model(build_invoice_model(), currency="INR").model_dump()

        assert "items" not in body
        assert body["invoice_number"] is None

    def test_payment_response_does_not_expose_the_idempotency_key(self) -> None:
        body = PaymentResponse.from_model(build_payment_model()).model_dump(mode="json")

        assert "idempotency_key" not in body
        assert body["amount"] == "200.00"
        assert body["method"] == "upi"
