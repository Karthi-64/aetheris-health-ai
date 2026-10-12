"""Pydantic DTOs for the Laboratory module.

Request models enforce ``docs/modules/07-laboratory.md`` §11 before a service
sees the payload (``docs/07-SECURITY.md``, rule 5). Response models are the
only lab shapes that cross the API boundary.

**No request model carries a flag.** Whether a result is abnormal is decided
by the server from the reference range (business rule 2); ``extra="forbid"``
turns a ``result_flag`` in a body into a 422.

**Prices go out as decimal strings**, never JSON numbers (CLAUDE.md rule 6).
"""

from __future__ import annotations

# NOTE: runtime imports, not TYPE_CHECKING — Pydantic resolves field
# annotations against the module's real globals (backend/CLAUDE.md).
import re
from datetime import datetime  # noqa: TC003
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal, Self
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.lab import LabOrderPriority, LabOrderStatus, LabResultFlag, LabResultType

if TYPE_CHECKING:
    from app.models.lab import LabOrder, LabOrderItem, LabResultAmendment, LabTest

__all__ = [
    "MAX_TESTS_PER_ORDER",
    "AmendResultRequest",
    "CancelLabOrderRequest",
    "CollectSamplesRequest",
    "CreateLabOrderRequest",
    "CreateLabTestRequest",
    "EnterResultsRequest",
    "LabOrderItemResponse",
    "LabOrderResponse",
    "LabResultAmendmentResponse",
    "LabTestResponse",
    "ReferenceRangeEntry",
    "ResultEntry",
    "SampleEntry",
    "UpdateLabOrderRequest",
    "UpdateLabTestRequest",
]

#: Upper bound on tests per order. Far above any real request; it exists so a
#: single call cannot ask the server to build an unbounded order.
MAX_TESTS_PER_ORDER = 50

#: Catalog codes: letters, digits, hyphen and underscore. Stored uppercased.
_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]*$")

#: Sample ids are printed as barcodes: no spaces, nothing exotic.
_SAMPLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

#: A monetary amount on the wire: ``NUMERIC(15, 2)``, never negative.
Money = Annotated[Decimal, Field(max_digits=15, decimal_places=2, ge=0)]

#: A bound of a reference range: ``NUMERIC(15, 4)``.
Bound = Annotated[Decimal, Field(max_digits=15, decimal_places=4)]


def _strip_required(value: str, label: str) -> str:
    """Trim a required string and reject one that is blank."""
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
    """Refuse ``null`` for a column that cannot hold one."""
    nulled = sorted(
        name for name in model.model_fields_set & required if getattr(model, name) is None
    )
    if nulled:
        msg = f"Cannot be null: {', '.join(nulled)}."
        raise ValueError(msg)


# ── Test catalog ────────────────────────────────────────────────────────────


class ReferenceRangeEntry(BaseModel):
    """One reference range of a test, for a sex and an age band (FR-1)."""

    model_config = ConfigDict(extra="forbid")

    sex: Literal["male", "female", "any"] = Field(
        default="any", description="Who the range applies to."
    )
    age_min: int | None = Field(
        default=None, ge=0, le=150, description="Youngest age in years, inclusive."
    )
    age_max: int | None = Field(
        default=None, ge=0, le=150, description="Oldest age in years, inclusive."
    )
    low: Bound | None = Field(default=None, description="Below this is flagged low.")
    high: Bound | None = Field(default=None, description="Above this is flagged high.")
    critical_low: Bound | None = Field(default=None, description="Below this is critical.")
    critical_high: Bound | None = Field(default=None, description="Above this is critical.")

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        """Require a usable range whose bounds are in a sensible order."""
        if self.low is None and self.high is None:
            msg = "A reference range needs a low bound, a high bound, or both."
            raise ValueError(msg)
        if self.age_min is not None and self.age_max is not None and self.age_min > self.age_max:
            msg = "age_min must not be greater than age_max."
            raise ValueError(msg)
        if self.low is not None and self.high is not None and self.low > self.high:
            msg = "low must not be greater than high."
            raise ValueError(msg)
        if self.critical_low is not None and self.low is not None and self.critical_low > self.low:
            msg = "critical_low must not be greater than low."
            raise ValueError(msg)
        if (
            self.critical_high is not None
            and self.high is not None
            and self.critical_high < self.high
        ):
            msg = "critical_high must not be less than high."
            raise ValueError(msg)
        return self


class CreateLabTestRequest(BaseModel):
    """Payload for ``POST /api/v1/tests-catalog`` (module spec FR-1)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "code": "HB",
                "name": "Haemoglobin",
                "category": "Haematology",
                "unit": "g/dL",
                "result_type": "numeric",
                "reference_ranges": [
                    {"sex": "male", "age_min": 18, "low": "13.0", "high": "17.0"},
                    {"sex": "female", "age_min": 18, "low": "12.0", "high": "15.5"},
                ],
                "turnaround_hours": 4,
                "price": "250.00",
            },
        },
    )

    code: str = Field(
        min_length=1, max_length=50, description="Catalog code. Uppercased automatically."
    )
    name: str = Field(min_length=1, max_length=200, description="Display name.")
    category: str | None = Field(default=None, max_length=100, description="Free-form grouping.")
    unit: str | None = Field(default=None, max_length=20, description="Unit of the result.")
    result_type: LabResultType = Field(
        default=LabResultType.NUMERIC, description="Whether results are numbers or text."
    )
    reference_ranges: list[ReferenceRangeEntry] = Field(
        default_factory=list, max_length=50, description="Ranges per sex and age band."
    )
    turnaround_hours: int | None = Field(
        default=None, gt=0, le=8760, description="Expected hours from order to release."
    )
    price: Money = Field(default=Decimal("0.00"), description="Price per test.")

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        """Uppercase the code and restrict it to catalog-safe characters."""
        code = value.strip().upper()
        if not _CODE_PATTERN.fullmatch(code):
            msg = "Code may contain only letters, digits, hyphens and underscores."
            raise ValueError(msg)
        return code

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """Trim the name and reject a blank one."""
        return _strip_required(value, "Name")

    @field_validator("category", "unit")
    @classmethod
    def _trim_optional(cls, value: str | None) -> str | None:
        """Trim optional text, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_ranges(self) -> Self:
        """§11: a numeric test needs a range; a text test cannot have one."""
        if self.result_type is LabResultType.NUMERIC and not self.reference_ranges:
            msg = "A numeric test needs at least one reference range."
            raise ValueError(msg)
        if self.result_type is LabResultType.TEXT and self.reference_ranges:
            msg = "A text test cannot have reference ranges."
            raise ValueError(msg)
        return self


class UpdateLabTestRequest(BaseModel):
    """Payload for ``PATCH /api/v1/tests-catalog/{id}``.

    Every field optional; only fields present in the body are applied. ``code``
    and ``result_type`` are immutable: orders already placed refer to the test
    by its code and hold results of its type. Editing ranges or the price never
    changes an existing order — items keep their own copies.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, min_length=1, max_length=200, description="Display name."
    )
    category: str | None = Field(default=None, max_length=100, description="Free-form grouping.")
    unit: str | None = Field(default=None, max_length=20, description="Unit of the result.")
    reference_ranges: list[ReferenceRangeEntry] | None = Field(
        default=None, max_length=50, description="Replaces the whole list of ranges."
    )
    turnaround_hours: int | None = Field(
        default=None, gt=0, le=8760, description="Expected hours from order to release."
    )
    price: Money | None = Field(default=None, description="Price per test.")
    is_active: bool | None = Field(default=None, description="Whether it can be ordered.")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        """Trim the name and reject a blank one."""
        return _strip_required(value, "Name") if value is not None else None

    @field_validator("category", "unit")
    @classmethod
    def _trim_optional(cls, value: str | None) -> str | None:
        """Trim optional text, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_nulls(self) -> Self:
        """Reject ``null`` for the ``NOT NULL`` columns."""
        _reject_explicit_nulls(self, frozenset({"name", "reference_ranges", "price", "is_active"}))
        return self


class LabTestResponse(BaseModel):
    """A catalog test."""

    id: UUID = Field(description="Test UUID.")
    code: str = Field(description="Catalog code, unique per hospital.")
    name: str = Field(description="Display name.")
    category: str | None = Field(description="Free-form grouping.")
    unit: str | None = Field(description="Unit of the result.")
    result_type: LabResultType = Field(description="numeric or text.")
    reference_ranges: list[ReferenceRangeEntry] = Field(description="Ranges per sex and age.")
    turnaround_hours: int | None = Field(description="Expected hours from order to release.")
    price: Decimal = Field(description="Price per test, as a decimal string.")
    is_active: bool = Field(description="Whether it can be ordered.")
    created_at: datetime = Field(description="When it was added (UTC).")
    updated_at: datetime = Field(description="When it was last changed (UTC).")

    @classmethod
    def from_model(cls, test: LabTest) -> Self:
        """Build the DTO from a :class:`~app.models.lab.LabTest`.

        :param test: The ORM instance to convert.
        :returns: The populated DTO.
        """
        return cls(
            id=test.id,
            code=test.code,
            name=test.name,
            category=test.category,
            unit=test.unit,
            result_type=test.result_type,
            reference_ranges=[
                ReferenceRangeEntry.model_validate(entry) for entry in test.reference_ranges or []
            ],
            turnaround_hours=test.turnaround_hours,
            price=test.price,
            is_active=test.is_active,
            created_at=test.created_at,
            updated_at=test.updated_at,
        )


# ── Orders ──────────────────────────────────────────────────────────────────


class CreateLabOrderRequest(BaseModel):
    """Payload for ``POST /api/v1/lab-orders`` (module spec §5 step 1).

    The patient and the ordering doctor are not sent: business rule 1 ties an
    order to a visit, and both are taken from that appointment.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "appointment_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "test_ids": ["8a6e0804-2bd0-4672-b79d-d97027f9071a"],
                "priority": "routine",
                "notes": "Fasting sample.",
            },
        },
    )

    appointment_id: UUID = Field(description="The visit the tests are ordered in.")
    test_ids: list[UUID] = Field(
        min_length=1, max_length=MAX_TESTS_PER_ORDER, description="Catalog tests to run."
    )
    priority: LabOrderPriority = Field(
        default=LabOrderPriority.ROUTINE, description="routine, urgent or stat."
    )
    notes: str | None = Field(default=None, max_length=2000, description="Notes for the lab.")

    @field_validator("test_ids")
    @classmethod
    def _check_unique(cls, value: list[UUID]) -> list[UUID]:
        """A test is ordered once per order."""
        if len(set(value)) != len(value):
            msg = "Each test may appear only once in an order."
            raise ValueError(msg)
        return value

    @field_validator("notes")
    @classmethod
    def _trim_notes(cls, value: str | None) -> str | None:
        """Trim notes, collapsing blank to ``None``."""
        return _blank_to_none(value)


class UpdateLabOrderRequest(BaseModel):
    """Payload for ``PATCH /api/v1/lab-orders/{id}`` — priority and notes only."""

    model_config = ConfigDict(extra="forbid")

    priority: LabOrderPriority | None = Field(default=None, description="New priority.")
    notes: str | None = Field(default=None, max_length=2000, description="Notes for the lab.")

    @field_validator("notes")
    @classmethod
    def _trim_notes(cls, value: str | None) -> str | None:
        """Trim notes, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_nulls(self) -> Self:
        """Reject ``null`` for the ``NOT NULL`` priority."""
        _reject_explicit_nulls(self, frozenset({"priority"}))
        return self


class SampleEntry(BaseModel):
    """One sample being recorded as collected."""

    model_config = ConfigDict(extra="forbid")

    item_id: UUID = Field(description="The order item the sample is for.")
    sample_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="Barcode on the sample. Generated when omitted.",
    )

    @field_validator("sample_id")
    @classmethod
    def _check_sample_id(cls, value: str | None) -> str | None:
        """Restrict a sample id to barcode-safe characters, uppercased."""
        if value is None:
            return None
        sample_id = value.strip()
        if not _SAMPLE_ID_PATTERN.fullmatch(sample_id):
            msg = "Sample id may contain only letters, digits, hyphens and underscores."
            raise ValueError(msg)
        return sample_id.upper()


class CollectSamplesRequest(BaseModel):
    """Payload for ``POST /api/v1/lab-orders/{id}/collect``.

    Send no body, or omit ``items``, to collect every outstanding sample with
    generated sample ids.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[SampleEntry] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_TESTS_PER_ORDER,
        description="Samples collected. Omit for all outstanding items.",
    )

    @model_validator(mode="after")
    def _check_unique(self) -> Self:
        """An item is collected once per request, and sample ids do not repeat."""
        entries = self.items or []
        item_ids = [entry.item_id for entry in entries]
        sample_ids = [entry.sample_id for entry in entries if entry.sample_id is not None]
        if len(set(item_ids)) != len(item_ids):
            msg = "Each item may appear only once."
            raise ValueError(msg)
        if len(set(sample_ids)) != len(sample_ids):
            msg = "Each sample id may be used only once."
            raise ValueError(msg)
        return self


class ResultEntry(BaseModel):
    """One result being entered."""

    model_config = ConfigDict(extra="forbid")

    item_id: UUID = Field(description="The order item the result is for.")
    value: str = Field(min_length=1, max_length=2000, description="The result.")
    notes: str | None = Field(default=None, max_length=2000, description="Technician's note.")

    @field_validator("value")
    @classmethod
    def _check_value(cls, value: str) -> str:
        """Trim the value and reject a blank one."""
        return _strip_required(value, "Result value")

    @field_validator("notes")
    @classmethod
    def _trim_notes(cls, value: str | None) -> str | None:
        """Trim notes, collapsing blank to ``None``."""
        return _blank_to_none(value)


class EnterResultsRequest(BaseModel):
    """Payload for ``POST /api/v1/lab-orders/{id}/enter-results``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "results": [{"item_id": "8a6e0804-2bd0-4672-b79d-d97027f9071a", "value": "11.2"}]
            },
        },
    )

    results: list[ResultEntry] = Field(
        min_length=1, max_length=MAX_TESTS_PER_ORDER, description="Results to record."
    )

    @field_validator("results")
    @classmethod
    def _check_unique(cls, value: list[ResultEntry]) -> list[ResultEntry]:
        """An item gets one result per request."""
        item_ids = [entry.item_id for entry in value]
        if len(set(item_ids)) != len(item_ids):
            msg = "Each item may appear only once."
            raise ValueError(msg)
        return value


class CancelLabOrderRequest(BaseModel):
    """Payload for ``POST /api/v1/lab-orders/{id}/cancel``."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500, description="Why it is cancelled.")

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, value: str) -> str:
        """Trim the reason and reject a blank one."""
        return _strip_required(value, "Reason")


class AmendResultRequest(BaseModel):
    """Payload for ``POST /api/v1/lab-orders/{id}/items/{item_id}/amend``."""

    model_config = ConfigDict(extra="forbid")

    new_value: str = Field(min_length=1, max_length=2000, description="The corrected result.")
    reason: str = Field(min_length=1, max_length=500, description="Why it is corrected.")

    @field_validator("new_value")
    @classmethod
    def _check_value(cls, value: str) -> str:
        """Trim the value and reject a blank one."""
        return _strip_required(value, "New value")

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, value: str) -> str:
        """Trim the reason and reject a blank one."""
        return _strip_required(value, "Reason")


class LabResultAmendmentResponse(BaseModel):
    """One correction made to a released result."""

    id: UUID = Field(description="Amendment UUID.")
    previous_value: str | None = Field(description="The value before.")
    new_value: str = Field(description="The value after.")
    previous_flag: LabResultFlag | None = Field(description="The flag before.")
    new_flag: LabResultFlag | None = Field(description="The flag after.")
    reason: str = Field(description="Why it was corrected.")
    amended_by: UUID = Field(description="User who corrected it.")
    amended_at: datetime = Field(description="When it was corrected (UTC).")

    @classmethod
    def from_model(cls, amendment: LabResultAmendment) -> Self:
        """Build the DTO from a :class:`~app.models.lab.LabResultAmendment`."""
        return cls(
            id=amendment.id,
            previous_value=amendment.previous_value,
            new_value=amendment.new_value,
            previous_flag=amendment.previous_flag,
            new_flag=amendment.new_flag,
            reason=amendment.reason,
            amended_by=amendment.amended_by,
            amended_at=amendment.amended_at,
        )


class LabOrderItemResponse(BaseModel):
    """One test on an order, with its sample and result."""

    id: UUID = Field(description="Item UUID.")
    test_id: UUID = Field(description="Catalog test UUID.")
    test_code: str = Field(description="Catalog code when ordered.")
    test_name: str = Field(description="Catalog name when ordered.")
    result_type: LabResultType = Field(description="numeric or text.")
    price: Decimal = Field(description="Price charged, as a decimal string.")
    sample_id: str | None = Field(description="Sample barcode, once collected.")
    sample_collected_at: datetime | None = Field(description="When the sample was taken (UTC).")
    result_value: str | None = Field(description="The result, once entered.")
    result_unit: str | None = Field(description="Unit of the result.")
    result_flag: LabResultFlag | None = Field(
        description="normal, low, high or critical. Null when no range applied."
    )
    reference_low: Decimal | None = Field(description="Lower bound the flag was judged against.")
    reference_high: Decimal | None = Field(description="Upper bound the flag was judged against.")
    result_entered_at: datetime | None = Field(description="When the result was entered (UTC).")
    released_at: datetime | None = Field(description="When the result was released (UTC).")
    notes: str | None = Field(description="Technician's note.")
    amendments: list[LabResultAmendmentResponse] = Field(
        description="Corrections made after release, oldest first."
    )

    @classmethod
    def from_model(cls, item: LabOrderItem) -> Self:
        """Build the DTO from a :class:`~app.models.lab.LabOrderItem`."""
        return cls(
            id=item.id,
            test_id=item.test_id,
            test_code=item.test_code,
            test_name=item.test_name,
            result_type=item.result_type,
            price=item.price,
            sample_id=item.sample_id,
            sample_collected_at=item.sample_collected_at,
            result_value=item.result_value,
            result_unit=item.result_unit,
            result_flag=item.result_flag,
            reference_low=item.reference_low,
            reference_high=item.reference_high,
            result_entered_at=item.result_entered_at,
            released_at=item.released_at,
            notes=item.notes,
            amendments=[LabResultAmendmentResponse.from_model(a) for a in item.amendments],
        )


class LabOrderResponse(BaseModel):
    """A lab order with its items."""

    id: UUID = Field(description="Order UUID.")
    appointment_id: UUID | None = Field(description="Visit the order was raised in.")
    patient_id: UUID = Field(description="Patient UUID.")
    patient_name: str = Field(description="Patient's full name.")
    patient_mrn: str = Field(description="Patient's medical record number.")
    doctor_id: UUID = Field(description="Ordering doctor's UUID.")
    doctor_name: str = Field(description="Ordering doctor's name.")
    ordered_at: datetime = Field(description="When the order was placed (UTC).")
    priority: LabOrderPriority = Field(description="routine, urgent or stat.")
    status: LabOrderStatus = Field(description="Lifecycle status.")
    notes: str | None = Field(description="Notes for the lab.")
    collected_at: datetime | None = Field(description="When every sample was collected (UTC).")
    results_entered_at: datetime | None = Field(description="When the last result was entered.")
    released_at: datetime | None = Field(description="When results were released (UTC).")
    released_by: UUID | None = Field(description="User who released the results.")
    cancelled_at: datetime | None = Field(description="When the order was cancelled (UTC).")
    cancel_reason: str | None = Field(description="Why the order was cancelled.")
    invoice_id: UUID | None = Field(description="Draft invoice the tests were charged to.")
    turnaround_minutes: int | None = Field(
        description="Minutes from order to release. Null until released."
    )
    has_abnormal: bool = Field(description="Whether any result is low, high or critical.")
    has_critical: bool = Field(description="Whether any result is critical.")
    items: list[LabOrderItemResponse] = Field(description="The tests on the order.")

    @classmethod
    def from_model(cls, order: LabOrder) -> Self:
        """Build the DTO from a :class:`~app.models.lab.LabOrder`.

        :param order: The ORM instance, with items, patient and doctor loaded.
        :returns: The populated DTO.
        """
        flags = {item.result_flag for item in order.items}
        turnaround = (
            int((order.released_at - order.ordered_at).total_seconds() // 60)
            if order.released_at is not None
            else None
        )
        doctor_user = order.doctor.user
        return cls(
            id=order.id,
            appointment_id=order.appointment_id,
            patient_id=order.patient_id,
            patient_name=order.patient.full_name,
            patient_mrn=order.patient.mrn,
            doctor_id=order.doctor_id,
            doctor_name=f"Dr. {doctor_user.first_name} {doctor_user.last_name}",
            ordered_at=order.ordered_at,
            priority=order.priority,
            status=order.status,
            notes=order.notes,
            collected_at=order.collected_at,
            results_entered_at=order.results_entered_at,
            released_at=order.released_at,
            released_by=order.released_by,
            cancelled_at=order.cancelled_at,
            cancel_reason=order.cancel_reason,
            invoice_id=order.invoice_id,
            turnaround_minutes=turnaround,
            has_abnormal=bool(
                flags & {LabResultFlag.LOW, LabResultFlag.HIGH, LabResultFlag.CRITICAL}
            ),
            has_critical=LabResultFlag.CRITICAL in flags,
            items=[LabOrderItemResponse.from_model(item) for item in order.items],
        )
