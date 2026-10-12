"""Billing models — the services catalog, invoices, their lines, and payments.

Columns follow ``docs/05-DATABASE_DESIGN.md`` §2.16–2.19 and
``docs/modules/06-billing.md`` §8. The places this schema departs from the
design — a nullable ``invoice_number``, ``hospital_id`` on lines and payments,
tenant-scoped payment idempotency, ``void_reason`` and ``position`` — are
explained once, in migration 0009, rather than repeated here.

**Money is ``Decimal`` over ``NUMERIC(15, 2)`` everywhere** (CLAUDE.md rule 6,
business rule 1). Nothing in this module is ever a ``float``.

**The money rules live in the database as well as the service.** The check
constraints mirrored in ``__table_args__`` mean a service bug cannot persist an
invoice that has been paid more than its total.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for Mapped[uuid.UUID] resolution
from datetime import datetime  # noqa: TC003 — needed at runtime for Mapped[...] resolution
from decimal import Decimal  # noqa: TC003 — needed at runtime for Mapped[Decimal] resolution
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
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
    func,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import InvoiceStatus
from app.models.base import Base, CommonColumnsMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.hospital import Hospital
    from app.models.patient import Patient

__all__ = [
    "DEFAULT_INVOICE_NUMBER_TEMPLATE",
    "PAYABLE_STATUSES",
    "Invoice",
    "InvoiceItem",
    "InvoiceNumberSequence",
    "InvoiceStatus",
    "Payment",
    "PaymentMethod",
    "REFUNDABLE_STATUSES",
    "Refund",
    "Service",
]

#: Module spec §8. ``{seq:06d}`` rather than the MRN's five digits: a busy
#: hospital raises far more invoices than it registers patients.
DEFAULT_INVOICE_NUMBER_TEMPLATE = "INV-{year}-{seq:06d}"


class PaymentMethod(StrEnum):
    """How a payment was made — the ``payment_method`` Postgres enum."""

    CASH = "cash"
    CARD = "card"
    UPI = "upi"
    BANK_TRANSFER = "bank_transfer"
    INSURANCE = "insurance"


#: Statuses in which an invoice can still take money. A draft has no frozen
#: total to pay against, and paid/void/refunded are closed.
PAYABLE_STATUSES: frozenset[InvoiceStatus] = frozenset(
    {InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID}
)


#: Statuses in which an invoice holds money that can be given back.
REFUNDABLE_STATUSES: frozenset[InvoiceStatus] = frozenset(
    {InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID}
)


class Service(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One billable item in a hospital's services catalog (module spec FR-1)."""

    __tablename__ = "services"

    __table_args__ = (
        UniqueConstraint("hospital_id", "code", name="uq_services_hospital_code"),
        CheckConstraint("price >= 0", name="price_non_negative"),
        Index("ix_services_hospital_category", "hospital_id", "category"),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="UUID of the hospital (tenant) this service belongs to.",
    )
    code: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Short catalog code, unique per hospital."
    )
    name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Display name, copied onto invoice lines."
    )
    category: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="Free-form grouping, e.g. 'Consultation'."
    )
    price: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        nullable=False,
        comment="Unit price in the hospital's configured currency.",
    )
    taxable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Whether the hospital's tax rate applies to this service.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Inactive services stay on old invoices but cannot be added to new ones.",
    )

    hospital: Mapped[Hospital] = relationship(
        "Hospital", foreign_keys="Service.hospital_id", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<Service id={self.id!s:.8} code={self.code!r} hospital={self.hospital_id!s:.8}>"


class InvoiceNumberSequence(Base):
    """Per-hospital counter and format template backing invoice numbers.

    One row per hospital, keyed by ``hospital_id`` (module spec §8). The
    counter is advanced under ``SELECT ... FOR UPDATE`` inside the issue
    transaction, so concurrent issues serialize on this row, and a rolled-back
    issue returns its number — which is what keeps the series gap-free
    (business rule 2, AC-2).

    No audit or soft-delete columns, for the same reason
    :class:`~app.models.patient.MrnSequence` has none: it is an internal
    counter, not a business record.
    """

    __tablename__ = "invoice_number_sequences"

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        primary_key=True,
        comment="UUID of the hospital this counter belongs to.",
    )
    current_value: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="Last issued sequence value. The next invoice uses current_value + 1.",
    )
    format_template: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=DEFAULT_INVOICE_NUMBER_TEMPLATE,
        server_default=DEFAULT_INVOICE_NUMBER_TEMPLATE,
        comment="Invoice number template, e.g. 'INV-{year}-{seq:06d}'.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="Timestamp of the last counter advance (UTC).",
    )

    def __repr__(self) -> str:
        return (
            f"<InvoiceNumberSequence hospital={self.hospital_id!s:.8} "
            f"current_value={self.current_value}>"
        )


class Invoice(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """A bill raised against a patient, optionally for one appointment."""

    __tablename__ = "invoices"

    __table_args__ = (
        CheckConstraint(
            "subtotal >= 0 AND tax_amount >= 0 AND discount_amount >= 0 "
            "AND total >= 0 AND amount_paid >= 0",
            name="amounts_non_negative",
        ),
        CheckConstraint("discount_amount <= subtotal", name="discount_within_subtotal"),
        CheckConstraint("amount_paid <= total", name="paid_within_total"),
        CheckConstraint(
            "amount_refunded >= 0 AND amount_refunded <= amount_paid",
            name="refunded_within_paid",
        ),
        CheckConstraint(
            "status = 'draft' OR invoice_number IS NOT NULL",
            name="number_required_once_issued",
        ),
        Index(
            "uq_invoices_hospital_number",
            "hospital_id",
            "invoice_number",
            unique=True,
            postgresql_where=text("invoice_number IS NOT NULL"),
        ),
        Index(
            "uq_invoices_live_appointment",
            "appointment_id",
            unique=True,
            postgresql_where=text(
                "appointment_id IS NOT NULL AND status <> 'void' AND deleted_at IS NULL"
            ),
        ),
        Index(
            "ix_invoices_discount_pending",
            "hospital_id",
            postgresql_where=text("discount_pending_approval"),
        ),
        Index("ix_invoices_status", "hospital_id", "status", "issued_at"),
        Index("ix_invoices_patient", "patient_id", text("issued_at DESC")),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="UUID of the hospital (tenant) this invoice belongs to.",
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Patient being billed.",
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="RESTRICT"),
        nullable=True,
        comment="Appointment this invoice is for. NULL for ad-hoc billing.",
    )
    invoice_number: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        comment="Sequential, gap-free number per hospital. NULL until issued.",
    )
    subtotal: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
        comment="Sum of line amounts before tax.",
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
        comment="Sum of line tax.",
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
        comment="Invoice-level discount.",
    )
    discount_reason: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="Why a discount was given."
    )
    discount_approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Admin who approved an above-threshold discount.",
    )
    discount_pending_approval: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="The discount is above the hospital's threshold and awaits an admin.",
    )
    total: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
        comment="subtotal + tax_amount - discount_amount, computed server-side.",
    )
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
        comment="Sum of payments recorded. Never exceeds total.",
    )
    amount_refunded: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
        comment="Sum of refunds given back. Never exceeds amount_paid.",
    )
    consultation_fee_pending: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="Raised by another module's charge mid-visit; completion still owes the fee.",
    )
    status: Mapped[InvoiceStatus] = mapped_column(
        SQLEnum(
            InvoiceStatus,
            name="invoice_status",
            create_type=False,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=InvoiceStatus.DRAFT,
        comment="Current lifecycle state.",
    )
    notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Free-form notes shown on the invoice."
    )
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the invoice was issued (UTC)."
    )
    voided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the invoice was voided (UTC)."
    )
    void_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Why the invoice was voided. Required on void."
    )

    # ── Relationships ───────────────────────────────────────────────────
    # `foreign_keys` everywhere (backend/CLAUDE.md, "Common Pitfalls").
    #
    # `patient` is `lazy="joined"`: every invoice view shows who it is for.
    patient: Mapped[Patient] = relationship(
        "Patient", foreign_keys="Invoice.patient_id", lazy="joined"
    )
    hospital: Mapped[Hospital] = relationship(
        "Hospital", foreign_keys="Invoice.hospital_id", lazy="raise"
    )
    # `items` is `lazy="selectin"`: an invoice is meaningless without its lines,
    # and the totals are recomputed from them on every draft edit.
    items: Mapped[list[InvoiceItem]] = relationship(
        "InvoiceItem",
        foreign_keys="InvoiceItem.invoice_id",
        back_populates="invoice",
        # A draft edit replaces the line set, so the ORM must forget removed rows.
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="InvoiceItem.position",
    )
    # Payments are read only on their own endpoint and grow over time, so they
    # are never loaded implicitly.
    payments: Mapped[list[Payment]] = relationship(
        "Payment",
        foreign_keys="Payment.invoice_id",
        back_populates="invoice",
        lazy="raise",
        order_by="Payment.received_at",
    )

    @property
    def balance_due(self) -> Decimal:
        """What is still owed: ``total - amount_paid``."""
        return self.total - self.amount_paid

    @property
    def refundable_amount(self) -> Decimal:
        """What can still be given back: ``amount_paid - amount_refunded``."""
        return self.amount_paid - self.amount_refunded

    @property
    def is_editable(self) -> bool:
        """Whether lines and notes may still change (business rule 3, AC-1)."""
        return self.status == InvoiceStatus.DRAFT

    def __repr__(self) -> str:
        return (
            f"<Invoice id={self.id!s:.8} number={self.invoice_number!r} "
            f"status={self.status} total={self.total}>"
        )


class InvoiceItem(UUIDPrimaryKeyMixin, Base):
    """One line on an invoice.

    No audit or soft-delete columns (``docs/05-DATABASE_DESIGN.md`` §2.18): a
    line has no life apart from its invoice, whose own audit columns and audit
    events cover every edit.
    """

    __tablename__ = "invoice_items"

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name="tax_rate_range"),
        CheckConstraint("line_total >= 0", name="line_total_non_negative"),
        Index("ix_invoice_items_invoice", "invoice_id", "position"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        comment="Invoice this line belongs to.",
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Owning tenant, carried so lines are directly tenant-filterable.",
    )
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("services.id", ondelete="RESTRICT"),
        nullable=True,
        comment="Catalog service this line was priced from. NULL for an ad-hoc line.",
    )
    description: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="What was billed. Copied from the service at the time."
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("1.00"),
        server_default=text("1"),
        comment="How many units. Fractional quantities are allowed.",
    )
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Price per unit at the time the line was written."
    )
    tax_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
        comment="Tax rate as a percentage, e.g. 18.00.",
    )
    line_total: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Line amount including tax (business rule 8)."
    )
    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="0-based order of the line on the invoice.",
    )

    invoice: Mapped[Invoice] = relationship(
        "Invoice", foreign_keys="InvoiceItem.invoice_id", back_populates="items", lazy="raise"
    )

    def __repr__(self) -> str:
        return (
            f"<InvoiceItem invoice={self.invoice_id!s:.8} position={self.position} "
            f"line_total={self.line_total}>"
        )


class Payment(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """Money received against an invoice. Append-only in practice."""

    __tablename__ = "payments"

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index(
            "uq_payments_hospital_idempotency_key",
            "hospital_id",
            "idempotency_key",
            unique=True,
        ),
        Index("ix_payments_invoice", "invoice_id", "received_at"),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Owning tenant, carried so payments are directly tenant-filterable.",
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Invoice the payment was made against.",
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Amount received. Always positive."
    )
    method: Mapped[PaymentMethod] = mapped_column(
        SQLEnum(
            PaymentMethod,
            name="payment_method",
            create_type=False,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        comment="cash, card, upi, bank_transfer, or insurance.",
    )
    reference: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="Transaction id, cheque number, or UPI reference."
    )
    received_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        comment="User who recorded the payment.",
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When the payment was recorded (UTC)."
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Client-supplied key making payment retries safe (business rule 6).",
    )
    notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Free-form notes about the payment."
    )

    invoice: Mapped[Invoice] = relationship(
        "Invoice", foreign_keys="Payment.invoice_id", back_populates="payments", lazy="raise"
    )

    def __repr__(self) -> str:
        return (
            f"<Payment id={self.id!s:.8} invoice={self.invoice_id!s:.8} "
            f"amount={self.amount} method={self.method}>"
        )


class Refund(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """Money given back against an invoice. Append-only in practice.

    A separate table rather than a negative :class:`Payment` — see migration
    0012 for why.
    """

    __tablename__ = "refunds"

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index(
            "uq_refunds_hospital_idempotency_key",
            "hospital_id",
            "idempotency_key",
            unique=True,
        ),
        Index("ix_refunds_invoice", "invoice_id", "refunded_at"),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Owning tenant, carried so refunds are directly tenant-filterable.",
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Invoice the refund was made against.",
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, comment="Amount given back. Always positive."
    )
    method: Mapped[PaymentMethod] = mapped_column(
        SQLEnum(
            PaymentMethod,
            name="payment_method",
            create_type=False,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        comment="How the money was returned.",
    )
    reason: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="Why the refund was given. Required."
    )
    reference: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="Transaction id or other reference for the refund."
    )
    refunded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        comment="User who issued the refund.",
    )
    refunded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When the refund was issued (UTC)."
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Client-supplied key making refund retries safe.",
    )

    def __repr__(self) -> str:
        return (
            f"<Refund id={self.id!s:.8} invoice={self.invoice_id!s:.8} "
            f"amount={self.amount} method={self.method}>"
        )
