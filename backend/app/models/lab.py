"""Laboratory models — the test catalog, orders, their items, and amendments.

Columns follow ``docs/modules/07-laboratory.md`` §8. Where the schema departs
from the spec's sketch — ``hospital_id`` on items and amendments, no
``consultation_id``, the snapshot columns — the reasons are given once, in
migration 0014, rather than repeated here.

**A result keeps the meaning it had when it was reported.** An item copies the
test's code, name and type when ordered, and the reference bounds its flag was
judged against when the result is entered, so later catalog edits cannot
change what an old report says.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for Mapped[uuid.UUID] resolution
from datetime import datetime  # noqa: TC003 — needed at runtime for Mapped[...] resolution
from decimal import Decimal  # noqa: TC003 — needed at runtime for Mapped[Decimal] resolution
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CommonColumnsMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.doctor import Doctor
    from app.models.patient import Patient

__all__ = [
    "CANCELLABLE_STATUSES",
    "LabOrder",
    "LabOrderItem",
    "LabOrderPriority",
    "LabOrderStatus",
    "LabResultAmendment",
    "LabResultFlag",
    "LabResultType",
    "LabTest",
]


class LabResultType(StrEnum):
    """What kind of value a test reports — the ``lab_result_type`` enum."""

    NUMERIC = "numeric"
    TEXT = "text"


class LabOrderPriority(StrEnum):
    """How urgently an order should be run — the ``lab_order_priority`` enum."""

    ROUTINE = "routine"
    URGENT = "urgent"
    STAT = "stat"


class LabOrderStatus(StrEnum):
    """Lifecycle of an order (module spec §8) — the ``lab_order_status`` enum."""

    ORDERED = "ordered"
    COLLECTED = "collected"
    IN_PROGRESS = "in_progress"
    RESULTS_ENTERED = "results_entered"
    RELEASED = "released"
    CANCELLED = "cancelled"


class LabResultFlag(StrEnum):
    """How a result sits against its reference range — ``lab_result_flag``."""

    NORMAL = "normal"
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"


#: Statuses from which an order can still be cancelled. Once released the
#: results are part of the record; corrections go through amendments (rule 4).
CANCELLABLE_STATUSES: frozenset[LabOrderStatus] = frozenset(
    {
        LabOrderStatus.ORDERED,
        LabOrderStatus.COLLECTED,
        LabOrderStatus.IN_PROGRESS,
        LabOrderStatus.RESULTS_ENTERED,
    }
)


def _enum(enum_class: type[StrEnum], name: str) -> SQLEnum:
    """Map a ``StrEnum`` onto an existing Postgres enum by its values."""
    return SQLEnum(
        enum_class,
        name=name,
        create_type=False,
        values_callable=lambda e: [m.value for m in e],
    )


class LabTest(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One orderable test in a hospital's catalog (module spec FR-1)."""

    __tablename__ = "tests_catalog"

    __table_args__ = (
        UniqueConstraint("hospital_id", "code", name="uq_tests_catalog_hospital_code"),
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint(
            "turnaround_hours IS NULL OR turnaround_hours > 0", name="turnaround_positive"
        ),
        Index("ix_tests_catalog_hospital_category", "hospital_id", "category"),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="UUID of the hospital (tenant) this test belongs to.",
    )
    code: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Short catalog code, unique per hospital."
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="Display name.")
    category: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="Free-form grouping, e.g. 'Haematology'."
    )
    unit: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="Unit results are reported in, e.g. 'g/dL'."
    )
    result_type: Mapped[LabResultType] = mapped_column(
        _enum(LabResultType, "lab_result_type"),
        nullable=False,
        default=LabResultType.NUMERIC,
        comment="Whether results are numbers or free text.",
    )
    reference_ranges: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="Array of {sex, age_min, age_max, low, high, critical_low, critical_high}.",
    )
    turnaround_hours: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Expected hours from order to release."
    )
    price: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        nullable=False,
        default=Decimal("0"),
        server_default=text("0"),
        comment="Price charged per test, in the hospital's currency.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Inactive tests stay on old orders but cannot be ordered.",
    )

    def __repr__(self) -> str:
        return f"<LabTest id={self.id!s:.8} code={self.code!r}>"


class LabOrder(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """A doctor's request for one or more tests on a patient (FR-2)."""

    __tablename__ = "lab_orders"

    __table_args__ = (
        CheckConstraint(
            "status <> 'released' OR released_at IS NOT NULL", name="released_has_time"
        ),
        CheckConstraint(
            "status <> 'cancelled' OR cancel_reason IS NOT NULL", name="cancelled_has_reason"
        ),
        Index("ix_lab_orders_hospital_status", "hospital_id", "status", "ordered_at"),
        Index("ix_lab_orders_patient", "hospital_id", "patient_id", "ordered_at"),
        Index("ix_lab_orders_doctor", "hospital_id", "doctor_id", "ordered_at"),
        Index("ix_lab_orders_appointment", "appointment_id"),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="UUID of the hospital (tenant) this order belongs to.",
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Patient the tests are for.",
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("doctors.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Doctor who ordered the tests.",
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="RESTRICT"),
        nullable=True,
        comment="Visit the order was raised in (business rule 1).",
    )
    ordered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When the order was placed (UTC)."
    )
    priority: Mapped[LabOrderPriority] = mapped_column(
        _enum(LabOrderPriority, "lab_order_priority"),
        nullable=False,
        default=LabOrderPriority.ROUTINE,
        comment="routine, urgent, or stat.",
    )
    status: Mapped[LabOrderStatus] = mapped_column(
        _enum(LabOrderStatus, "lab_order_status"),
        nullable=False,
        default=LabOrderStatus.ORDERED,
        comment="Lifecycle status.",
    )
    notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Clinical notes for the lab."
    )
    collected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When every sample had been collected."
    )
    results_entered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the last result was entered."
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When results were released (UTC)."
    )
    released_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        comment="User who released the results.",
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the order was cancelled (UTC)."
    )
    cancel_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Why the order was cancelled."
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
        comment="Draft invoice the tests were charged to.",
    )

    items: Mapped[list[LabOrderItem]] = relationship(
        "LabOrderItem",
        foreign_keys="LabOrderItem.lab_order_id",
        back_populates="order",
        order_by="LabOrderItem.position",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    patient: Mapped[Patient] = relationship(
        "Patient", foreign_keys="LabOrder.patient_id", lazy="joined"
    )
    doctor: Mapped[Doctor] = relationship(
        "Doctor", foreign_keys="LabOrder.doctor_id", lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<LabOrder id={self.id!s:.8} status={self.status} items={len(self.items)}>"


class LabOrderItem(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One test on an order: its sample, its result, and its release."""

    __tablename__ = "lab_order_items"

    __table_args__ = (
        UniqueConstraint("lab_order_id", "test_id", name="uq_lab_order_items_order_test"),
        CheckConstraint("price >= 0", name="price_non_negative"),
        Index(
            "uq_lab_order_items_hospital_sample",
            "hospital_id",
            "sample_id",
            unique=True,
            postgresql_where=text("sample_id IS NOT NULL"),
        ),
        Index("ix_lab_order_items_order", "lab_order_id", "position"),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Owning tenant, carried so items are directly tenant-filterable.",
    )
    lab_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lab_orders.id", ondelete="CASCADE"),
        nullable=False,
        comment="Order this item belongs to.",
    )
    test_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tests_catalog.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Catalog test ordered.",
    )
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="Display order."
    )
    test_code: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Catalog code when ordered."
    )
    test_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Catalog name when ordered."
    )
    result_type: Mapped[LabResultType] = mapped_column(
        _enum(LabResultType, "lab_result_type"),
        nullable=False,
        comment="Whether the result is a number or free text.",
    )
    price: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Price charged when ordered."
    )
    sample_id: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Sample identifier, unique per hospital (rule 5)."
    )
    sample_collected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the sample was taken (UTC)."
    )
    sample_collected_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        comment="User who collected the sample.",
    )
    result_value: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="The result, numeric or text."
    )
    result_unit: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="Unit the result is reported in."
    )
    result_flag: Mapped[LabResultFlag | None] = mapped_column(
        _enum(LabResultFlag, "lab_result_flag"),
        nullable=True,
        comment="normal, low, high or critical. NULL when no range applied.",
    )
    reference_low: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 4), nullable=True, comment="Lower bound the flag was judged against."
    )
    reference_high: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 4), nullable=True, comment="Upper bound the flag was judged against."
    )
    result_entered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the result was entered (UTC)."
    )
    result_entered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        comment="User who entered the result.",
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the result was released (UTC)."
    )
    released_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        comment="User who released the result.",
    )
    notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Technician's note on the result."
    )

    order: Mapped[LabOrder] = relationship(
        "LabOrder",
        foreign_keys="LabOrderItem.lab_order_id",
        back_populates="items",
        lazy="raise",
    )
    amendments: Mapped[list[LabResultAmendment]] = relationship(
        "LabResultAmendment",
        foreign_keys="LabResultAmendment.item_id",
        order_by="LabResultAmendment.amended_at",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<LabOrderItem id={self.id!s:.8} test={self.test_code!r} flag={self.result_flag}>"


class LabResultAmendment(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """A correction to a released result (business rule 4).

    Released results are never edited in place without a trace: the item takes
    the new value, and this row keeps what it said before and why it changed.
    """

    __tablename__ = "lab_result_amendments"

    __table_args__ = (Index("ix_lab_result_amendments_item", "item_id", "amended_at"),)

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Owning tenant.",
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lab_order_items.id", ondelete="CASCADE"),
        nullable=False,
        comment="Result that was amended.",
    )
    previous_value: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="The value before the amendment."
    )
    new_value: Mapped[str] = mapped_column(
        Text, nullable=False, comment="The value after the amendment."
    )
    previous_flag: Mapped[LabResultFlag | None] = mapped_column(
        _enum(LabResultFlag, "lab_result_flag"), nullable=True, comment="Flag before."
    )
    new_flag: Mapped[LabResultFlag | None] = mapped_column(
        _enum(LabResultFlag, "lab_result_flag"), nullable=True, comment="Flag after."
    )
    reason: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="Why the result was corrected."
    )
    amended_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        comment="User who made the amendment.",
    )
    amended_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When it was amended (UTC)."
    )

    def __repr__(self) -> str:
        return f"<LabResultAmendment id={self.id!s:.8} item={self.item_id!s:.8}>"
