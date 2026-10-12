"""Pharmacy models — medicines and stock, prescriptions and dispenses, vendors
and purchase orders.

Columns follow ``docs/modules/08-pharmacy.md`` §8. Where the schema departs
from the spec's sketch — ``hospital_id`` everywhere, prescriptions on the
appointment, ``quantity_on_hand`` on the batch — the reasons are given once,
in migration 0015, rather than repeated here.

**Stock is a ledger plus a running total.** Every change to a batch is a
:class:`StockMovement`; the batch's ``quantity_on_hand`` is the sum of its
movements, kept on the row so the database can refuse to let it go negative.
The two are only ever written together, by the repository.

**Money is ``Decimal`` over ``NUMERIC(15, 2)``** (CLAUDE.md rule 6).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for Mapped[uuid.UUID] resolution
from datetime import date, datetime  # noqa: TC003 — needed at runtime for Mapped[...]
from decimal import Decimal  # noqa: TC003 — needed at runtime for Mapped[Decimal] resolution
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CommonColumnsMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.doctor import Doctor
    from app.models.patient import Patient

__all__ = [
    "DISPENSABLE_STATUSES",
    "Dispense",
    "DispenseItem",
    "Medicine",
    "MedicineBatch",
    "Prescription",
    "PrescriptionItem",
    "PrescriptionStatus",
    "PurchaseOrder",
    "PurchaseOrderItem",
    "PurchaseOrderStatus",
    "StockMovement",
    "StockMovementReason",
    "Vendor",
]


class StockMovementReason(StrEnum):
    """Why a batch's stock changed — the ``stock_movement_reason`` enum."""

    RECEIVED = "received"
    DISPENSED = "dispensed"
    ADJUSTED = "adjusted"
    EXPIRED = "expired"


class PrescriptionStatus(StrEnum):
    """How far a prescription has been dispensed — ``prescription_status``."""

    ACTIVE = "active"
    PARTIALLY_DISPENSED = "partially_dispensed"
    DISPENSED = "dispensed"
    CANCELLED = "cancelled"


class PurchaseOrderStatus(StrEnum):
    """Lifecycle of a purchase order — the ``purchase_order_status`` enum."""

    DRAFT = "draft"
    SENT = "sent"
    RECEIVED = "received"
    CANCELLED = "cancelled"


#: Statuses in which a prescription still has something to dispense (§11).
DISPENSABLE_STATUSES: frozenset[PrescriptionStatus] = frozenset(
    {PrescriptionStatus.ACTIVE, PrescriptionStatus.PARTIALLY_DISPENSED}
)


def _enum(enum_class: type[StrEnum], name: str) -> SQLEnum:
    """Map a ``StrEnum`` onto an existing Postgres enum by its values."""
    return SQLEnum(
        enum_class,
        name=name,
        create_type=False,
        values_callable=lambda e: [m.value for m in e],
    )


def _tenant() -> Mapped[uuid.UUID]:
    """The ``hospital_id`` column every pharmacy table carries."""
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="UUID of the hospital (tenant) this row belongs to.",
    )


def _ref(
    target: str, comment: str, *, nullable: bool = False, ondelete: str = "RESTRICT"
) -> Mapped[uuid.UUID]:
    """A UUID foreign key column."""
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        comment=comment,
    )


class Vendor(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """A supplier the hospital buys from. Shared with Inventory (spec 09 §20)."""

    __tablename__ = "vendors"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="uq_vendors_hospital_name"),)

    hospital_id: Mapped[uuid.UUID] = _tenant()
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="Vendor name.")
    contact: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="Contact person, phone or email."
    )
    address: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Postal address.")
    tax_id: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Tax registration number."
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Inactive vendors cannot be given new purchase orders.",
    )

    def __repr__(self) -> str:
        return f"<Vendor id={self.id!s:.8} name={self.name!r}>"


class Medicine(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One medicine in a hospital's catalog (module spec FR-1)."""

    __tablename__ = "medicines"
    __table_args__ = (
        UniqueConstraint("hospital_id", "sku", name="uq_medicines_hospital_sku"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        Index("ix_medicines_hospital_name", "hospital_id", "name"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    sku: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Stock-keeping code, unique per hospital."
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="Brand or trade name.")
    generic_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="Generic (INN) name."
    )
    strength: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Strength, e.g. '500 mg'."
    )
    form: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Dosage form, e.g. 'tablet'."
    )
    atc_code: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="ATC therapeutic classification code."
    )
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Selling price per unit."
    )
    requires_prescription: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Whether it may only be dispensed against a prescription.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Inactive medicines cannot be prescribed, ordered or dispensed.",
    )

    def __repr__(self) -> str:
        return f"<Medicine id={self.id!s:.8} sku={self.sku!r}>"


class MedicineBatch(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One received lot of a medicine, with its own expiry (FR-2)."""

    __tablename__ = "medicine_batches"
    __table_args__ = (
        UniqueConstraint("medicine_id", "batch_number", name="uq_medicine_batches_medicine_batch"),
        CheckConstraint("quantity_on_hand >= 0", name="quantity_on_hand_non_negative"),
        CheckConstraint("initial_quantity >= 0", name="initial_quantity_non_negative"),
        CheckConstraint("cost_per_unit >= 0", name="cost_non_negative"),
        Index("ix_medicine_batches_fifo", "hospital_id", "medicine_id", "expiry_date"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    medicine_id: Mapped[uuid.UUID] = _ref("medicines.id", "Medicine this batch is of.")
    batch_number: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Manufacturer's batch number."
    )
    expiry_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Last day the batch may be dispensed."
    )
    cost_per_unit: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Purchase cost per unit."
    )
    initial_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Units received."
    )
    quantity_on_hand: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Units left: the sum of the batch's movements."
    )
    is_recalled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="A recalled batch cannot be dispensed.",
    )

    medicine: Mapped[Medicine] = relationship(
        "Medicine", foreign_keys="MedicineBatch.medicine_id", lazy="joined"
    )

    def __repr__(self) -> str:
        return (
            f"<MedicineBatch id={self.id!s:.8} batch={self.batch_number!r} "
            f"on_hand={self.quantity_on_hand}>"
        )


class StockMovement(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One change to a batch's stock. Append-only: the ledger of record."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint("quantity_change <> 0", name="quantity_change_non_zero"),
        Index("ix_stock_movements_batch", "batch_id", "moved_at"),
        Index("ix_stock_movements_reference", "reference_type", "reference_id"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    batch_id: Mapped[uuid.UUID] = _ref("medicine_batches.id", "Batch whose stock changed.")
    quantity_change: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Units added (positive) or removed (negative)."
    )
    reason: Mapped[StockMovementReason] = mapped_column(
        _enum(StockMovementReason, "stock_movement_reason"),
        nullable=False,
        comment="received, dispensed, adjusted or expired.",
    )
    reference_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="What caused it, e.g. 'dispense'."
    )
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="UUID of what caused it."
    )
    note: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Why, for an adjustment."
    )
    moved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When the stock changed (UTC)."
    )
    moved_by: Mapped[uuid.UUID | None] = _ref(
        "users.id", "User who caused the change.", nullable=True
    )

    def __repr__(self) -> str:
        return f"<StockMovement id={self.id!s:.8} change={self.quantity_change:+d}>"


class Prescription(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """What a doctor prescribed in one visit."""

    __tablename__ = "prescriptions"
    __table_args__ = (
        Index("ix_prescriptions_hospital_status", "hospital_id", "status", "prescribed_at"),
        Index("ix_prescriptions_patient", "hospital_id", "patient_id", "prescribed_at"),
        Index("ix_prescriptions_appointment", "appointment_id"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    appointment_id: Mapped[uuid.UUID] = _ref("appointments.id", "Visit it was written in.")
    patient_id: Mapped[uuid.UUID] = _ref("patients.id", "Patient it is for.")
    doctor_id: Mapped[uuid.UUID] = _ref("doctors.id", "Doctor who wrote it.")
    status: Mapped[PrescriptionStatus] = mapped_column(
        _enum(PrescriptionStatus, "prescription_status"),
        nullable=False,
        default=PrescriptionStatus.ACTIVE,
        comment="active, partially_dispensed, dispensed or cancelled.",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Doctor's notes.")
    prescribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When it was written (UTC)."
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When it was cancelled (UTC)."
    )
    cancel_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Why it was cancelled."
    )

    items: Mapped[list[PrescriptionItem]] = relationship(
        "PrescriptionItem",
        foreign_keys="PrescriptionItem.prescription_id",
        back_populates="prescription",
        order_by="PrescriptionItem.position",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    patient: Mapped[Patient] = relationship(
        "Patient", foreign_keys="Prescription.patient_id", lazy="joined"
    )
    doctor: Mapped[Doctor] = relationship(
        "Doctor", foreign_keys="Prescription.doctor_id", lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<Prescription id={self.id!s:.8} status={self.status}>"


class PrescriptionItem(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One medicine on a prescription, and how much of it has been dispensed."""

    __tablename__ = "prescription_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "quantity_dispensed >= 0 AND quantity_dispensed <= quantity",
            name="dispensed_within_prescribed",
        ),
        Index("ix_prescription_items_prescription", "prescription_id", "position"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    prescription_id: Mapped[uuid.UUID] = _ref(
        "prescriptions.id", "Prescription this line belongs to.", ondelete="CASCADE"
    )
    medicine_id: Mapped[uuid.UUID | None] = _ref(
        "medicines.id", "Catalog medicine; NULL for a free-text line.", nullable=True
    )
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="Display order."
    )
    medicine_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Name as prescribed (snapshot)."
    )
    dosage: Mapped[str] = mapped_column(String(100), nullable=False, comment="e.g. '1 tablet'.")
    frequency: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="e.g. 'twice daily'."
    )
    duration_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="How many days to take it."
    )
    instructions: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="e.g. 'after food'."
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, comment="Units prescribed.")
    quantity_dispensed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="Units dispensed so far.",
    )

    prescription: Mapped[Prescription] = relationship(
        "Prescription",
        foreign_keys="PrescriptionItem.prescription_id",
        back_populates="items",
        lazy="raise",
    )

    @property
    def quantity_remaining(self) -> int:
        """Units still to dispense."""
        return self.quantity - self.quantity_dispensed

    def __repr__(self) -> str:
        return (
            f"<PrescriptionItem id={self.id!s:.8} medicine={self.medicine_name!r} "
            f"{self.quantity_dispensed}/{self.quantity}>"
        )


class Dispense(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One handing-over of medicines against a prescription (FR-3)."""

    __tablename__ = "dispenses"
    __table_args__ = (Index("ix_dispenses_prescription", "prescription_id", "dispensed_at"),)

    hospital_id: Mapped[uuid.UUID] = _tenant()
    prescription_id: Mapped[uuid.UUID] = _ref("prescriptions.id", "Prescription dispensed.")
    dispensed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When it was dispensed (UTC)."
    )
    dispensed_by: Mapped[uuid.UUID | None] = _ref(
        "users.id", "Pharmacist who dispensed.", nullable=True
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Sum of the dispense lines."
    )
    notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Reason for a partial dispense, or other notes."
    )
    invoice_id: Mapped[uuid.UUID | None] = _ref(
        "invoices.id", "Draft invoice it was charged to.", nullable=True, ondelete="SET NULL"
    )

    items: Mapped[list[DispenseItem]] = relationship(
        "DispenseItem",
        foreign_keys="DispenseItem.dispense_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Dispense id={self.id!s:.8} total={self.total_amount}>"


class DispenseItem(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """Units of one medicine taken from one batch in a dispense."""

    __tablename__ = "dispense_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_dispense_items_dispense", "dispense_id"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    dispense_id: Mapped[uuid.UUID] = _ref(
        "dispenses.id", "Dispense this line belongs to.", ondelete="CASCADE"
    )
    prescription_item_id: Mapped[uuid.UUID] = _ref(
        "prescription_items.id", "Prescription line being filled."
    )
    medicine_id: Mapped[uuid.UUID] = _ref("medicines.id", "Medicine dispensed.")
    batch_id: Mapped[uuid.UUID] = _ref("medicine_batches.id", "Batch the units came from.")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, comment="Units dispensed.")
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Selling price per unit when dispensed."
    )
    total: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="quantity x unit_price."
    )

    batch: Mapped[MedicineBatch] = relationship(
        "MedicineBatch", foreign_keys="DispenseItem.batch_id", lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<DispenseItem id={self.id!s:.8} quantity={self.quantity}>"


class PurchaseOrder(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """An order placed with a vendor for medicines (FR-5)."""

    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("hospital_id", "po_number", name="uq_purchase_orders_hospital_number"),
        Index("ix_purchase_orders_hospital_status", "hospital_id", "status"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    vendor_id: Mapped[uuid.UUID] = _ref("vendors.id", "Vendor the order is placed with.")
    po_number: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Order number, unique per hospital."
    )
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        _enum(PurchaseOrderStatus, "purchase_order_status"),
        nullable=False,
        default=PurchaseOrderStatus.DRAFT,
        comment="draft, sent, received or cancelled.",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Notes to the vendor.")
    ordered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When it was sent (UTC)."
    )
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the goods were received (UTC)."
    )
    received_by: Mapped[uuid.UUID | None] = _ref(
        "users.id", "User who received the goods.", nullable=True
    )

    items: Mapped[list[PurchaseOrderItem]] = relationship(
        "PurchaseOrderItem",
        foreign_keys="PurchaseOrderItem.po_id",
        order_by="PurchaseOrderItem.position",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    vendor: Mapped[Vendor] = relationship(
        "Vendor", foreign_keys="PurchaseOrder.vendor_id", lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<PurchaseOrder id={self.id!s:.8} number={self.po_number!r} status={self.status}>"


class PurchaseOrderItem(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One medicine on a purchase order."""

    __tablename__ = "po_items"
    __table_args__ = (
        UniqueConstraint("po_id", "medicine_id", name="uq_po_items_po_medicine"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_po_items_po", "po_id", "position"),
    )

    hospital_id: Mapped[uuid.UUID] = _tenant()
    po_id: Mapped[uuid.UUID] = _ref(
        "purchase_orders.id", "Purchase order this line belongs to.", ondelete="CASCADE"
    )
    medicine_id: Mapped[uuid.UUID] = _ref("medicines.id", "Medicine ordered.")
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="Display order."
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, comment="Units ordered.")
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Agreed purchase price per unit."
    )
    total: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="quantity x unit_price."
    )

    medicine: Mapped[Medicine] = relationship(
        "Medicine", foreign_keys="PurchaseOrderItem.medicine_id", lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<PurchaseOrderItem id={self.id!s:.8} quantity={self.quantity}>"
