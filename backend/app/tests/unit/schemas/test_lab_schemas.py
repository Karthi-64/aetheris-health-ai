"""Unit tests for the Laboratory DTOs (module spec §11)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.lab import (
    AmendResultRequest,
    CancelLabOrderRequest,
    CollectSamplesRequest,
    CreateLabOrderRequest,
    CreateLabTestRequest,
    EnterResultsRequest,
    ReferenceRangeEntry,
    UpdateLabOrderRequest,
    UpdateLabTestRequest,
)

RANGE = {"sex": "any", "low": "3.5", "high": "5.1"}


def _test_payload(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"code": "k", "name": " Potassium ", "reference_ranges": [RANGE]}
    values.update(overrides)
    return values


class TestReferenceRangeEntry:
    def test_defaults_to_anyone(self) -> None:
        assert ReferenceRangeEntry.model_validate({"high": "200"}).sex == "any"

    @pytest.mark.parametrize(
        "entry",
        [
            {"sex": "any"},  # no bound at all
            {"low": "5", "high": "3"},
            {"low": "5", "high": "9", "age_min": 30, "age_max": 20},
            {"low": "5", "high": "9", "critical_low": "6"},
            {"low": "5", "high": "9", "critical_high": "8"},
            {"low": "5", "high": "9", "sex": "unknown"},
            {"low": "5", "high": "9", "age_min": -1},
            {"low": "5", "high": "9", "flag": "normal"},
        ],
    )
    def test_rejects_an_unusable_range(self, entry: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            ReferenceRangeEntry.model_validate(entry)


class TestCreateLabTestRequest:
    def test_normalises_code_and_name(self) -> None:
        request = CreateLabTestRequest.model_validate(_test_payload(category="  ", unit=" mmol/L "))

        assert request.code == "K"
        assert request.name == "Potassium"
        assert request.category is None
        assert request.unit == "mmol/L"

    def test_a_numeric_test_needs_a_range(self) -> None:
        with pytest.raises(ValidationError, match="at least one reference range"):
            CreateLabTestRequest.model_validate(_test_payload(reference_ranges=[]))

    def test_a_text_test_cannot_have_one(self) -> None:
        with pytest.raises(ValidationError, match="cannot have reference ranges"):
            CreateLabTestRequest.model_validate(_test_payload(result_type="text"))

    def test_a_text_test_without_ranges_is_fine(self) -> None:
        request = CreateLabTestRequest.model_validate(
            _test_payload(result_type="text", reference_ranges=[])
        )

        assert request.reference_ranges == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"code": "has space"},
            {"name": "   "},
            {"price": "-1.00"},
            {"price": "1.005"},
            {"turnaround_hours": 0},
            {"is_active": False},  # not settable on create
        ],
    )
    def test_rejects_bad_input(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            CreateLabTestRequest.model_validate(_test_payload(**overrides))


class TestUpdateLabTestRequest:
    def test_only_sent_fields_are_set(self) -> None:
        request = UpdateLabTestRequest.model_validate({"price": "300.00"})

        assert request.model_fields_set == {"price"}

    @pytest.mark.parametrize("body", [{"code": "NEW"}, {"result_type": "text"}])
    def test_code_and_type_are_immutable(self, body: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            UpdateLabTestRequest.model_validate(body)

    @pytest.mark.parametrize("field", ["name", "price", "is_active", "reference_ranges"])
    def test_null_for_a_required_column_is_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError, match="Cannot be null"):
            UpdateLabTestRequest.model_validate({field: None})

    def test_null_clears_an_optional_column(self) -> None:
        assert UpdateLabTestRequest.model_validate({"unit": None}).unit is None


class TestOrderRequests:
    def test_create_defaults_to_routine(self) -> None:
        request = CreateLabOrderRequest.model_validate(
            {"appointment_id": str(uuid.uuid4()), "test_ids": [str(uuid.uuid4())], "notes": " "}
        )

        assert request.priority.value == "routine"
        assert request.notes is None

    def test_create_rejects_a_repeated_test(self) -> None:
        test_id = str(uuid.uuid4())

        with pytest.raises(ValidationError, match="only once"):
            CreateLabOrderRequest.model_validate(
                {"appointment_id": str(uuid.uuid4()), "test_ids": [test_id, test_id]}
            )

    @pytest.mark.parametrize(
        "body",
        [
            {"test_ids": []},
            {"test_ids": [str(uuid.uuid4())], "patient_id": str(uuid.uuid4())},
            {"test_ids": [str(uuid.uuid4())], "doctor_id": str(uuid.uuid4())},
            {"test_ids": [str(uuid.uuid4())], "priority": "whenever"},
        ],
    )
    def test_create_rejects_bad_input(self, body: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            CreateLabOrderRequest.model_validate({"appointment_id": str(uuid.uuid4()), **body})

    def test_update_rejects_a_null_priority(self) -> None:
        with pytest.raises(ValidationError, match="Cannot be null"):
            UpdateLabOrderRequest.model_validate({"priority": None})

    def test_update_rejects_changing_the_tests(self) -> None:
        with pytest.raises(ValidationError):
            UpdateLabOrderRequest.model_validate({"test_ids": [str(uuid.uuid4())]})


class TestCollectSamplesRequest:
    def test_an_empty_body_means_everything_outstanding(self) -> None:
        assert CollectSamplesRequest.model_validate({}).items is None

    def test_a_sample_id_is_uppercased(self) -> None:
        request = CollectSamplesRequest.model_validate(
            {"items": [{"item_id": str(uuid.uuid4()), "sample_id": " bc-001 "}]}
        )

        assert request.items is not None
        assert request.items[0].sample_id == "BC-001"

    def test_rejects_a_repeated_item_or_sample_id(self) -> None:
        item_id = str(uuid.uuid4())

        with pytest.raises(ValidationError, match="Each item"):
            CollectSamplesRequest.model_validate(
                {"items": [{"item_id": item_id}, {"item_id": item_id}]}
            )
        with pytest.raises(ValidationError, match="Each sample id"):
            CollectSamplesRequest.model_validate(
                {
                    "items": [
                        {"item_id": str(uuid.uuid4()), "sample_id": "A1"},
                        {"item_id": str(uuid.uuid4()), "sample_id": "a1"},
                    ]
                }
            )

    def test_rejects_a_sample_id_that_would_not_print_as_a_barcode(self) -> None:
        with pytest.raises(ValidationError):
            CollectSamplesRequest.model_validate(
                {"items": [{"item_id": str(uuid.uuid4()), "sample_id": "has space"}]}
            )


class TestResultRequests:
    def test_a_client_cannot_send_a_flag(self) -> None:
        # Business rule 2: the server decides what is abnormal.
        with pytest.raises(ValidationError):
            EnterResultsRequest.model_validate(
                {"results": [{"item_id": str(uuid.uuid4()), "value": "5", "result_flag": "normal"}]}
            )

    def test_rejects_a_blank_value_and_a_repeated_item(self) -> None:
        item_id = str(uuid.uuid4())

        with pytest.raises(ValidationError):
            EnterResultsRequest.model_validate({"results": [{"item_id": item_id, "value": "  "}]})
        with pytest.raises(ValidationError, match="only once"):
            EnterResultsRequest.model_validate(
                {
                    "results": [
                        {"item_id": item_id, "value": "1"},
                        {"item_id": item_id, "value": "2"},
                    ]
                }
            )
        with pytest.raises(ValidationError):
            EnterResultsRequest.model_validate({"results": []})

    @pytest.mark.parametrize(
        ("model", "body"),
        [
            (CancelLabOrderRequest, {"reason": "  "}),
            (CancelLabOrderRequest, {}),
            (AmendResultRequest, {"new_value": "5", "reason": " "}),
            (AmendResultRequest, {"new_value": " ", "reason": "Transcription error"}),
        ],
    )
    def test_a_reason_and_a_value_are_required(self, model: Any, body: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            model.model_validate(body)
