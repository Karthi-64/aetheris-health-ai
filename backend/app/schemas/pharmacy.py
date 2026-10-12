"""Pydantic DTOs for the Pharmacy module.

Request models enforce ``docs/modules/08-pharmacy.md`` §11 before a service
sees the payload (``docs/07-SECURITY.md``, rule 5). Response models are the
only pharmacy shapes that cross the API boundary.

**No request model has a price for a dispense, or a batch to take it from.**
The price is the catalog's and the batch is chosen first-expiry-first by the
server (business rule 2); ``extra="forbid"`` turns either in a body into a 422.

**Money goes out as a decimal string**, never a JSON number (CLAUDE.md rule 6).
Quantities are whole units.
"""

from __future__ import annotations

# NOTE: runtime imports, not TYPE_CHECKING — Pydantic resolves field
# annotations against the module's real globals (backend/CLAUDE.md).
import re
from datetime import date, datetime  # noqa: TC003
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal, Self
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.pharmacy import PrescriptionStatus, PurchaseOrderStatus, StockMovementReason

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.models.pharmacy import (
        Dispense,
        Medicine,
        MedicineBatch,
        Prescription,
        PurchaseOrder,
        Vendor,
    )

__all__ = [
    "EXPIRY_WARNING_DAYS",
    "MAX_LINES",
    "AdjustStockRequest",
    "BatchResponse",
    "CancelPrescriptionRequest",
    "CreateMedicineRequest",
    "CreatePrescriptionRequest",
    "CreatePurchaseOrderRequest",
    "CreateVendorRequest",
    "DispenseRequest",
    "DispenseResponse",
    "MedicineResponse",
    "MedicineStockResponse",
    "PrescriptionResponse",
    "PurchaseOrderResponse",
    "ReceiveBatchRequest",
    "ReceivePurchaseOrderRequest",
    "UpdateBatchRequest",
    "UpdateMedicineRequest",
    "UpdateVendorRequest",
    "VendorResponse",
]

#: Business rule 4: a batch this close to its expiry is warned about.
EXPIRY_WARNING_DAYS = 30

#: Upper bound on lines per prescription, dispense, order or receipt.
MAX_LINES = 50

#: SKUs and batch numbers: letters, digits, hyphen, underscore, dot, slash.
_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_./-]*$")

#: A monetary amount on the wire: ``NUMERIC(15, 2)``, never negative.
Money = Annotated[Decimal, Field(max_digits=15, decimal_places=2, ge=0)]

#: A count of units: a positive whole number.
Units = Annotated[int, Field(gt=0, le=1_000_000)]


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


def _code(value: str, label: str) -> str:
    """Uppercase a code and restrict it to label-safe characters."""
    code = value.strip().upper()
    if not _CODE_PATTERN.fullmatch(code):
        msg = f"{label} may contain only letters, digits and the characters - _ . /"
        raise ValueError(msg)
    return code


def _reject_explicit_nulls(model: BaseModel, required: frozenset[str]) -> None:
    """Refuse ``null`` for a column that cannot hold one."""
    nulled = sorted(
        name for name in model.model_fields_set & required if getattr(model, name) is None
    )
    if nulled:
        msg = f"Cannot be null: {', '.join(nulled)}."
        raise ValueError(msg)


def _unique(values: list[object], message: str) -> None:
    """Raise ``ValueError`` if a list repeats a value."""
    if len(set(values)) != len(values):
        raise ValueError(message)


# ── Medicines ───────────────────────────────────────────────────────────────


class CreateMedicineRequest(BaseModel):
    """Payload for ``POST /api/v1/medicines`` (module spec FR-1)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "sku": "PARA-500",
                "name": "Paracetamol",
                "generic_name": "Paracetamol",
                "strength": "500 mg",
                "form": "tablet",
                "atc_code": "N02BE01",
                "unit_price": "2.50",
                "requires_prescription": False,
            },
        },
    )

    sku: str = Field(min_length=1, max_length=50, description="Stock code. Uppercased.")
    name: str = Field(min_length=1, max_length=200, description="Brand or trade name.")
    generic_name: str | None = Field(default=None, max_length=200, description="Generic name.")
    strength: str | None = Field(default=None, max_length=50, description="e.g. '500 mg'.")
    form: str | None = Field(default=None, max_length=50, description="e.g. 'tablet'.")
    atc_code: str | None = Field(default=None, max_length=20, description="ATC code.")
    unit_price: Money = Field(description="Selling price per unit.")
    requires_prescription: bool = Field(
        default=True, description="Whether a prescription is needed."
    )

    @field_validator("sku")
    @classmethod
    def _check_sku(cls, value: str) -> str:
        """Uppercase the SKU and restrict its characters."""
        return _code(value, "SKU")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """Trim the name and reject a blank one."""
        return _strip_required(value, "Name")

    @field_validator("generic_name", "strength", "form", "atc_code")
    @classmethod
    def _trim_optional(cls, value: str | None) -> str | None:
        """Trim optional text, collapsing blank to ``None``."""
        return _blank_to_none(value)


class UpdateMedicineRequest(BaseModel):
    """Payload for ``PATCH /api/v1/medicines/{id}``. ``sku`` is immutable."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200, description="Name.")
    generic_name: str | None = Field(default=None, max_length=200, description="Generic name.")
    strength: str | None = Field(default=None, max_length=50, description="Strength.")
    form: str | None = Field(default=None, max_length=50, description="Dosage form.")
    atc_code: str | None = Field(default=None, max_length=20, description="ATC code.")
    unit_price: Money | None = Field(default=None, description="Selling price per unit.")
    requires_prescription: bool | None = Field(default=None, description="Needs a prescription.")
    is_active: bool | None = Field(default=None, description="Whether it can be used.")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        """Trim the name and reject a blank one."""
        return _strip_required(value, "Name") if value is not None else None

    @field_validator("generic_name", "strength", "form", "atc_code")
    @classmethod
    def _trim_optional(cls, value: str | None) -> str | None:
        """Trim optional text, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_nulls(self) -> Self:
        """Reject ``null`` for the ``NOT NULL`` columns."""
        _reject_explicit_nulls(
            self, frozenset({"name", "unit_price", "requires_prescription", "is_active"})
        )
        return self


class MedicineResponse(BaseModel):
    """A catalog medicine."""

    id: UUID = Field(description="Medicine UUID.")
    sku: str = Field(description="Stock code, unique per hospital.")
    name: str = Field(description="Brand or trade name.")
    generic_name: str | None = Field(description="Generic name.")
    strength: str | None = Field(description="Strength.")
    form: str | None = Field(description="Dosage form.")
    atc_code: str | None = Field(description="ATC code.")
    unit_price: Decimal = Field(description="Selling price per unit, as a decimal string.")
    requires_prescription: bool = Field(description="Whether a prescription is needed.")
    is_active: bool = Field(description="Whether it can be prescribed, ordered or dispensed.")
    created_at: datetime = Field(description="When it was added (UTC).")
    updated_at: datetime = Field(description="When it was last changed (UTC).")

    @classmethod
    def from_model(cls, medicine: Medicine) -> Self:
        """Build the DTO from a :class:`~app.models.pharmacy.Medicine`."""
        return cls(
            id=medicine.id,
            sku=medicine.sku,
            name=medicine.name,
            generic_name=medicine.generic_name,
            strength=medicine.strength,
            form=medicine.form,
            atc_code=medicine.atc_code,
            unit_price=medicine.unit_price,
            requires_prescription=medicine.requires_prescription,
            is_active=medicine.is_active,
            created_at=medicine.created_at,
            updated_at=medicine.updated_at,
        )


# ── Batches and stock ───────────────────────────────────────────────────────


class ReceiveBatchRequest(BaseModel):
    """Payload for ``POST /api/v1/medicines/{id}/batches`` — stock received."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "batch_number": "B2026-041",
                "expiry_date": "2027-09-30",
                "quantity": 500,
                "cost_per_unit": "1.20",
            },
        },
    )

    batch_number: str = Field(min_length=1, max_length=50, description="Batch number.")
    expiry_date: date = Field(description="Last day the batch may be dispensed.")
    quantity: Units = Field(description="Units received.")
    cost_per_unit: Money = Field(description="Purchase cost per unit.")

    @field_validator("batch_number")
    @classmethod
    def _check_batch_number(cls, value: str) -> str:
        """Uppercase the batch number and restrict its characters."""
        return _code(value, "Batch number")


class UpdateBatchRequest(BaseModel):
    """Payload for ``PATCH .../batches/{batch_id}`` — recall or un-recall."""

    model_config = ConfigDict(extra="forbid")

    is_recalled: bool = Field(description="A recalled batch cannot be dispensed.")


class AdjustStockRequest(BaseModel):
    """Payload for ``POST .../batches/{batch_id}/adjust`` — a stock correction."""

    model_config = ConfigDict(extra="forbid")

    quantity_change: int = Field(
        ge=-1_000_000, le=1_000_000, description="Units to add (positive) or remove (negative)."
    )
    reason: Literal["adjusted", "expired"] = Field(
        default="adjusted", description="Why the stock is changing."
    )
    note: str = Field(min_length=1, max_length=500, description="Explanation, for the ledger.")

    @field_validator("note")
    @classmethod
    def _check_note(cls, value: str) -> str:
        """Trim the note and reject a blank one."""
        return _strip_required(value, "Note")

    @model_validator(mode="after")
    def _check_change(self) -> Self:
        """A correction changes something, and writing off expired stock removes it."""
        if self.quantity_change == 0:
            msg = "quantity_change must not be zero."
            raise ValueError(msg)
        if self.reason == "expired" and self.quantity_change > 0:
            msg = "Writing off expired stock must remove units."
            raise ValueError(msg)
        return self


class BatchResponse(BaseModel):
    """One batch of a medicine, with how close it is to expiry."""

    id: UUID = Field(description="Batch UUID.")
    medicine_id: UUID = Field(description="Medicine UUID.")
    batch_number: str = Field(description="Batch number.")
    expiry_date: date = Field(description="Last day the batch may be dispensed.")
    cost_per_unit: Decimal = Field(description="Purchase cost per unit, as a decimal string.")
    initial_quantity: int = Field(description="Units received in total.")
    quantity_on_hand: int = Field(description="Units left.")
    is_recalled: bool = Field(description="A recalled batch cannot be dispensed.")
    days_to_expiry: int = Field(description="Days until expiry; negative once expired.")
    is_expired: bool = Field(description="Past its expiry date: cannot be dispensed.")
    expires_soon: bool = Field(description=f"Expires within {EXPIRY_WARNING_DAYS} days.")
    is_dispensable: bool = Field(description="In stock, not expired and not recalled.")

    @classmethod
    def from_model(cls, batch: MedicineBatch, *, today: date) -> Self:
        """Build the DTO from a :class:`~app.models.pharmacy.MedicineBatch`.

        :param batch: The ORM instance to convert.
        :param today: Today in the hospital's timezone.
        """
        days = (batch.expiry_date - today).days
        expired = days < 0
        return cls(
            id=batch.id,
            medicine_id=batch.medicine_id,
            batch_number=batch.batch_number,
            expiry_date=batch.expiry_date,
            cost_per_unit=batch.cost_per_unit,
            initial_quantity=batch.initial_quantity,
            quantity_on_hand=batch.quantity_on_hand,
            is_recalled=batch.is_recalled,
            days_to_expiry=days,
            is_expired=expired,
            expires_soon=not expired and days <= EXPIRY_WARNING_DAYS,
            is_dispensable=batch.quantity_on_hand > 0 and not expired and not batch.is_recalled,
        )


class MedicineStockResponse(BaseModel):
    """A medicine's stock position (``GET /medicines/{id}/stock``)."""

    medicine: MedicineResponse = Field(description="The medicine.")
    quantity_on_hand: int = Field(description="Units physically held, in any state.")
    dispensable_quantity: int = Field(description="Units that can be dispensed today.")
    expiring_soon_quantity: int = Field(
        description=f"Dispensable units expiring within {EXPIRY_WARNING_DAYS} days."
    )
    expired_quantity: int = Field(description="Units held past their expiry date.")
    recalled_quantity: int = Field(description="Units held in recalled batches.")
    batches: list[BatchResponse] = Field(description="Batches, earliest expiry first.")


# ── Prescriptions ───────────────────────────────────────────────────────────


class PrescriptionLineRequest(BaseModel):
    """One medicine on a new prescription.

    Either a catalog ``medicine_id``, or a free-text ``medicine_name`` for
    something the pharmacy does not stock — never both.
    """

    model_config = ConfigDict(extra="forbid")

    medicine_id: UUID | None = Field(default=None, description="Catalog medicine.")
    medicine_name: str | None = Field(
        default=None, max_length=200, description="Free-text name, for an unstocked medicine."
    )
    dosage: str = Field(min_length=1, max_length=100, description="e.g. '1 tablet'.")
    frequency: str = Field(min_length=1, max_length=100, description="e.g. 'twice daily'.")
    duration_days: int | None = Field(default=None, gt=0, le=3650, description="Days to take it.")
    instructions: str | None = Field(default=None, max_length=1000, description="Instructions.")
    quantity: Units = Field(description="Units prescribed.")

    @field_validator("dosage", "frequency")
    @classmethod
    def _check_required(cls, value: str) -> str:
        """Trim required text and reject a blank value."""
        return _strip_required(value, "Dosage and frequency")

    @field_validator("medicine_name", "instructions")
    @classmethod
    def _trim_optional(cls, value: str | None) -> str | None:
        """Trim optional text, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_medicine(self) -> Self:
        """Exactly one of a catalog medicine and a free-text name."""
        if (self.medicine_id is None) == (self.medicine_name is None):
            msg = "Give either medicine_id or medicine_name, not both and not neither."
            raise ValueError(msg)
        return self


class CreatePrescriptionRequest(BaseModel):
    """Payload for ``POST /api/v1/prescriptions``.

    The patient and the prescribing doctor are taken from the appointment.
    """

    model_config = ConfigDict(extra="forbid")

    appointment_id: UUID = Field(description="The visit the prescription is written in.")
    notes: str | None = Field(default=None, max_length=2000, description="Doctor's notes.")
    items: list[PrescriptionLineRequest] = Field(
        min_length=1, max_length=MAX_LINES, description="Medicines prescribed."
    )

    @field_validator("notes")
    @classmethod
    def _trim_notes(cls, value: str | None) -> str | None:
        """Trim notes, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @field_validator("items")
    @classmethod
    def _check_unique(cls, value: list[PrescriptionLineRequest]) -> list[PrescriptionLineRequest]:
        """A catalog medicine appears once per prescription."""
        _unique(
            [line.medicine_id for line in value if line.medicine_id is not None],
            "Each medicine may appear only once in a prescription.",
        )
        return value


class CancelPrescriptionRequest(BaseModel):
    """Payload for ``POST /api/v1/prescriptions/{id}/cancel``."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500, description="Why it is cancelled.")

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, value: str) -> str:
        """Trim the reason and reject a blank one."""
        return _strip_required(value, "Reason")


class PrescriptionItemResponse(BaseModel):
    """One medicine on a prescription."""

    id: UUID = Field(description="Prescription item UUID.")
    medicine_id: UUID | None = Field(description="Catalog medicine; null for free text.")
    medicine_name: str = Field(description="Name as prescribed.")
    dosage: str = Field(description="Dosage.")
    frequency: str = Field(description="Frequency.")
    duration_days: int | None = Field(description="Days to take it.")
    instructions: str | None = Field(description="Instructions.")
    quantity: int = Field(description="Units prescribed.")
    quantity_dispensed: int = Field(description="Units dispensed so far.")
    quantity_remaining: int = Field(description="Units still to dispense.")
    available_quantity: int | None = Field(
        description="Units in stock that can be dispensed today. Null for free text."
    )


class PrescriptionResponse(BaseModel):
    """A prescription with its items."""

    id: UUID = Field(description="Prescription UUID.")
    appointment_id: UUID = Field(description="Visit it was written in.")
    patient_id: UUID = Field(description="Patient UUID.")
    patient_name: str = Field(description="Patient's full name.")
    patient_mrn: str = Field(description="Patient's medical record number.")
    doctor_id: UUID = Field(description="Prescribing doctor's UUID.")
    doctor_name: str = Field(description="Prescribing doctor's name.")
    status: PrescriptionStatus = Field(description="How far it has been dispensed.")
    notes: str | None = Field(description="Doctor's notes.")
    prescribed_at: datetime = Field(description="When it was written (UTC).")
    cancelled_at: datetime | None = Field(description="When it was cancelled (UTC).")
    cancel_reason: str | None = Field(description="Why it was cancelled.")
    items: list[PrescriptionItemResponse] = Field(description="Medicines prescribed.")

    @classmethod
    def from_model(
        cls, prescription: Prescription, *, availability: Mapping[UUID, int] | None = None
    ) -> Self:
        """Build the DTO from a :class:`~app.models.pharmacy.Prescription`.

        :param prescription: The ORM instance, items, patient and doctor loaded.
        :param availability: Dispensable units by medicine id.
        """
        stock = availability or {}
        doctor_user = prescription.doctor.user
        return cls(
            id=prescription.id,
            appointment_id=prescription.appointment_id,
            patient_id=prescription.patient_id,
            patient_name=prescription.patient.full_name,
            patient_mrn=prescription.patient.mrn,
            doctor_id=prescription.doctor_id,
            doctor_name=f"Dr. {doctor_user.first_name} {doctor_user.last_name}",
            status=prescription.status,
            notes=prescription.notes,
            prescribed_at=prescription.prescribed_at,
            cancelled_at=prescription.cancelled_at,
            cancel_reason=prescription.cancel_reason,
            items=[
                PrescriptionItemResponse(
                    id=item.id,
                    medicine_id=item.medicine_id,
                    medicine_name=item.medicine_name,
                    dosage=item.dosage,
                    frequency=item.frequency,
                    duration_days=item.duration_days,
                    instructions=item.instructions,
                    quantity=item.quantity,
                    quantity_dispensed=item.quantity_dispensed,
                    quantity_remaining=item.quantity_remaining,
                    available_quantity=(
                        stock.get(item.medicine_id, 0) if item.medicine_id is not None else None
                    ),
                )
                for item in prescription.items
            ],
        )


# ── Dispensing ──────────────────────────────────────────────────────────────


class DispenseLineRequest(BaseModel):
    """How much of one prescription line to dispense."""

    model_config = ConfigDict(extra="forbid")

    prescription_item_id: UUID = Field(description="The prescription line to fill.")
    quantity: Units = Field(description="Units to dispense.")


class DispenseRequest(BaseModel):
    """Payload for ``POST /api/v1/prescriptions/{id}/dispense``.

    Omit ``items`` to dispense everything still outstanding. Naming items, or
    quantities below what is outstanding, is a partial dispense and needs
    ``notes`` saying why (business rule 5).
    """

    model_config = ConfigDict(extra="forbid")

    items: list[DispenseLineRequest] | None = Field(
        default=None, min_length=1, max_length=MAX_LINES, description="Lines to dispense."
    )
    notes: str | None = Field(default=None, max_length=2000, description="Reason, or notes.")

    @field_validator("notes")
    @classmethod
    def _trim_notes(cls, value: str | None) -> str | None:
        """Trim notes, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @field_validator("items")
    @classmethod
    def _check_unique(
        cls, value: list[DispenseLineRequest] | None
    ) -> list[DispenseLineRequest] | None:
        """A prescription line appears once per dispense."""
        if value is not None:
            _unique(
                [line.prescription_item_id for line in value],
                "Each prescription item may appear only once.",
            )
        return value


class DispenseItemResponse(BaseModel):
    """Units of one medicine taken from one batch."""

    id: UUID = Field(description="Dispense item UUID.")
    prescription_item_id: UUID = Field(description="Prescription line filled.")
    medicine_id: UUID = Field(description="Medicine dispensed.")
    medicine_name: str = Field(description="Medicine name.")
    batch_id: UUID = Field(description="Batch the units came from.")
    batch_number: str = Field(description="Batch number.")
    expiry_date: date = Field(description="The batch's expiry date.")
    quantity: int = Field(description="Units dispensed from this batch.")
    unit_price: Decimal = Field(description="Selling price per unit, as a decimal string.")
    total: Decimal = Field(description="Line total, as a decimal string.")


class DispenseResponse(BaseModel):
    """One dispense against a prescription."""

    id: UUID = Field(description="Dispense UUID.")
    prescription_id: UUID = Field(description="Prescription dispensed.")
    dispensed_at: datetime = Field(description="When it was dispensed (UTC).")
    dispensed_by: UUID | None = Field(description="Pharmacist who dispensed.")
    total_amount: Decimal = Field(description="Sum of the lines, as a decimal string.")
    notes: str | None = Field(description="Reason for a partial dispense, or notes.")
    invoice_id: UUID | None = Field(description="Draft invoice it was charged to.")
    items: list[DispenseItemResponse] = Field(description="One line per batch drawn from.")
    warnings: list[str] = Field(description="Things the pharmacist should tell the patient.")

    @classmethod
    def from_model(cls, dispense: Dispense, *, today: date) -> Self:
        """Build the DTO from a :class:`~app.models.pharmacy.Dispense`.

        :param dispense: The ORM instance, lines and their batches loaded.
        :param today: Today in the hospital's timezone, for expiry warnings.
        """
        warnings: list[str] = []
        items = []
        for line in dispense.items:
            batch = line.batch
            days = (batch.expiry_date - today).days
            if days <= EXPIRY_WARNING_DAYS:
                when = "today" if days == 0 else f"in {days} days"
                warning = (
                    f"{batch.medicine.name} batch {batch.batch_number} expires {when} "
                    f"({batch.expiry_date.isoformat()})."
                )
                if warning not in warnings:
                    warnings.append(warning)
            items.append(
                DispenseItemResponse(
                    id=line.id,
                    prescription_item_id=line.prescription_item_id,
                    medicine_id=line.medicine_id,
                    medicine_name=batch.medicine.name,
                    batch_id=line.batch_id,
                    batch_number=batch.batch_number,
                    expiry_date=batch.expiry_date,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    total=line.total,
                )
            )
        return cls(
            id=dispense.id,
            prescription_id=dispense.prescription_id,
            dispensed_at=dispense.dispensed_at,
            dispensed_by=dispense.dispensed_by,
            total_amount=dispense.total_amount,
            notes=dispense.notes,
            invoice_id=dispense.invoice_id,
            items=items,
            warnings=warnings,
        )


# ── Vendors ─────────────────────────────────────────────────────────────────


class CreateVendorRequest(BaseModel):
    """Payload for ``POST /api/v1/vendors``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, description="Vendor name.")
    contact: str | None = Field(default=None, max_length=200, description="Contact details.")
    address: str | None = Field(default=None, max_length=1000, description="Postal address.")
    tax_id: str | None = Field(default=None, max_length=50, description="Tax registration.")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """Trim the name and reject a blank one."""
        return _strip_required(value, "Name")

    @field_validator("contact", "address", "tax_id")
    @classmethod
    def _trim_optional(cls, value: str | None) -> str | None:
        """Trim optional text, collapsing blank to ``None``."""
        return _blank_to_none(value)


class UpdateVendorRequest(BaseModel):
    """Payload for ``PATCH /api/v1/vendors/{id}``."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200, description="Name.")
    contact: str | None = Field(default=None, max_length=200, description="Contact details.")
    address: str | None = Field(default=None, max_length=1000, description="Postal address.")
    tax_id: str | None = Field(default=None, max_length=50, description="Tax registration.")
    is_active: bool | None = Field(default=None, description="Whether it can be ordered from.")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        """Trim the name and reject a blank one."""
        return _strip_required(value, "Name") if value is not None else None

    @field_validator("contact", "address", "tax_id")
    @classmethod
    def _trim_optional(cls, value: str | None) -> str | None:
        """Trim optional text, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_nulls(self) -> Self:
        """Reject ``null`` for the ``NOT NULL`` columns."""
        _reject_explicit_nulls(self, frozenset({"name", "is_active"}))
        return self


class VendorResponse(BaseModel):
    """A vendor."""

    id: UUID = Field(description="Vendor UUID.")
    name: str = Field(description="Vendor name.")
    contact: str | None = Field(description="Contact details.")
    address: str | None = Field(description="Postal address.")
    tax_id: str | None = Field(description="Tax registration.")
    is_active: bool = Field(description="Whether it can be ordered from.")
    created_at: datetime = Field(description="When it was added (UTC).")

    @classmethod
    def from_model(cls, vendor: Vendor) -> Self:
        """Build the DTO from a :class:`~app.models.pharmacy.Vendor`."""
        return cls(
            id=vendor.id,
            name=vendor.name,
            contact=vendor.contact,
            address=vendor.address,
            tax_id=vendor.tax_id,
            is_active=vendor.is_active,
            created_at=vendor.created_at,
        )


# ── Purchase orders ─────────────────────────────────────────────────────────


class PurchaseOrderLineRequest(BaseModel):
    """One medicine on a new purchase order."""

    model_config = ConfigDict(extra="forbid")

    medicine_id: UUID = Field(description="Medicine to order.")
    quantity: Units = Field(description="Units to order.")
    unit_price: Money = Field(description="Agreed purchase price per unit.")


class CreatePurchaseOrderRequest(BaseModel):
    """Payload for ``POST /api/v1/purchase-orders``. Created as a draft."""

    model_config = ConfigDict(extra="forbid")

    vendor_id: UUID = Field(description="Vendor to order from.")
    notes: str | None = Field(default=None, max_length=2000, description="Notes to the vendor.")
    items: list[PurchaseOrderLineRequest] = Field(
        min_length=1, max_length=MAX_LINES, description="Medicines to order."
    )

    @field_validator("notes")
    @classmethod
    def _trim_notes(cls, value: str | None) -> str | None:
        """Trim notes, collapsing blank to ``None``."""
        return _blank_to_none(value)

    @field_validator("items")
    @classmethod
    def _check_unique(cls, value: list[PurchaseOrderLineRequest]) -> list[PurchaseOrderLineRequest]:
        """A medicine appears once per order."""
        _unique(
            [line.medicine_id for line in value], "Each medicine may appear only once in an order."
        )
        return value


class ReceiptLineRequest(BaseModel):
    """One batch received against a purchase order line."""

    model_config = ConfigDict(extra="forbid")

    po_item_id: UUID = Field(description="The order line the batch is for.")
    batch_number: str = Field(min_length=1, max_length=50, description="Batch number.")
    expiry_date: date = Field(description="Last day the batch may be dispensed.")
    quantity: Units = Field(description="Units received in this batch.")
    cost_per_unit: Money | None = Field(
        default=None, description="Cost per unit. Defaults to the order line's price."
    )

    @field_validator("batch_number")
    @classmethod
    def _check_batch_number(cls, value: str) -> str:
        """Uppercase the batch number and restrict its characters."""
        return _code(value, "Batch number")


class ReceivePurchaseOrderRequest(BaseModel):
    """Payload for ``POST /api/v1/purchase-orders/{id}/receive``."""

    model_config = ConfigDict(extra="forbid")

    items: list[ReceiptLineRequest] = Field(
        min_length=1, max_length=MAX_LINES, description="Batches received."
    )

    @field_validator("items")
    @classmethod
    def _check_unique(cls, value: list[ReceiptLineRequest]) -> list[ReceiptLineRequest]:
        """A batch number is listed once per order line."""
        _unique(
            [(line.po_item_id, line.batch_number) for line in value],
            "Each batch may be listed only once per order line.",
        )
        return value


class PurchaseOrderItemResponse(BaseModel):
    """One medicine on a purchase order."""

    id: UUID = Field(description="Order item UUID.")
    medicine_id: UUID = Field(description="Medicine ordered.")
    medicine_sku: str = Field(description="Medicine SKU.")
    medicine_name: str = Field(description="Medicine name.")
    quantity: int = Field(description="Units ordered.")
    unit_price: Decimal = Field(description="Purchase price per unit, as a decimal string.")
    total: Decimal = Field(description="Line total, as a decimal string.")


class PurchaseOrderResponse(BaseModel):
    """A purchase order with its items."""

    id: UUID = Field(description="Purchase order UUID.")
    po_number: str = Field(description="Order number.")
    vendor_id: UUID = Field(description="Vendor UUID.")
    vendor_name: str = Field(description="Vendor name.")
    status: PurchaseOrderStatus = Field(description="draft, sent, received or cancelled.")
    notes: str | None = Field(description="Notes to the vendor.")
    ordered_at: datetime | None = Field(description="When it was sent (UTC).")
    received_at: datetime | None = Field(description="When the goods were received (UTC).")
    total_amount: Decimal = Field(description="Sum of the lines, as a decimal string.")
    created_at: datetime = Field(description="When it was drafted (UTC).")
    items: list[PurchaseOrderItemResponse] = Field(description="Medicines ordered.")

    @classmethod
    def from_model(cls, order: PurchaseOrder) -> Self:
        """Build the DTO from a :class:`~app.models.pharmacy.PurchaseOrder`."""
        return cls(
            id=order.id,
            po_number=order.po_number,
            vendor_id=order.vendor_id,
            vendor_name=order.vendor.name,
            status=order.status,
            notes=order.notes,
            ordered_at=order.ordered_at,
            received_at=order.received_at,
            total_amount=sum((item.total for item in order.items), Decimal("0.00")),
            created_at=order.created_at,
            items=[
                PurchaseOrderItemResponse(
                    id=item.id,
                    medicine_id=item.medicine_id,
                    medicine_sku=item.medicine.sku,
                    medicine_name=item.medicine.name,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    total=item.total,
                )
                for item in order.items
            ],
        )


#: Stock movement reasons a client may ask for in an adjustment.
ADJUSTMENT_REASONS: dict[str, StockMovementReason] = {
    "adjusted": StockMovementReason.ADJUSTED,
    "expired": StockMovementReason.EXPIRED,
}
