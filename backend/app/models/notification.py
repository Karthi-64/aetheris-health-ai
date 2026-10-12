"""Notification models — messages, per-user preferences, and email deliveries.

Columns follow ``docs/05-DATABASE_DESIGN.md`` §2.20 and
``docs/modules/11-notifications.md`` §8. Where this schema departs from the
design — ``hospital_id`` on both tables, ``in_app``, ``next_attempt_at``, and
the stored email content on a delivery — is explained once, in migration 0013.

**A notification row is the in-app notification.** Business rule 4 says in-app
delivery never fails and is persisted directly, so there is no delivery row for
it: writing the :class:`Notification` *is* delivering it. A
:class:`NotificationDelivery` exists only for a channel that can fail and be
retried, which today means email.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for Mapped[uuid.UUID] resolution
from datetime import datetime  # noqa: TC003 — needed at runtime for Mapped[...] resolution
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CommonColumnsMixin, UUIDPrimaryKeyMixin

__all__ = [
    "DeliveryStatus",
    "Notification",
    "NotificationChannel",
    "NotificationDelivery",
    "NotificationPreference",
]


class NotificationChannel(StrEnum):
    """How a notification reaches someone — the ``notification_channel`` enum."""

    IN_APP = "in_app"
    EMAIL = "email"
    SMS = "sms"


class DeliveryStatus(StrEnum):
    """State of one out-of-band send — the ``notification_delivery_status`` enum.

    ``delivered`` is reserved for a provider confirming receipt, which SMTP
    does not do; an accepted email stops at ``sent``.
    """

    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    DELIVERED = "delivered"


class Notification(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One message for one recipient (business rule 1)."""

    __tablename__ = "notifications"

    __table_args__ = (
        Index(
            "ix_notifications_recipient_created",
            "recipient_user_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_notifications_recipient_unread",
            "recipient_user_id",
            postgresql_where=text("read_at IS NULL AND in_app AND deleted_at IS NULL"),
        ),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="UUID of the hospital (tenant) this notification belongs to.",
    )
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="User the notification is for.",
    )
    kind: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Notification kind code, e.g. 'auth.user_invited'."
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="Short headline.")
    body: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Message text. Never contains a secret such as a token."
    )
    link: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="In-app path to open, e.g. '/billing'."
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the recipient read it (UTC)."
    )
    in_app: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Shown in the notification centre. False when only an email was wanted.",
    )
    sent_email: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="An email for this notification was accepted by the mail server.",
    )
    sent_sms: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="An SMS for this notification was sent. Unused until v2.1.",
    )

    deliveries: Mapped[list[NotificationDelivery]] = relationship(
        "NotificationDelivery",
        foreign_keys="NotificationDelivery.notification_id",
        back_populates="notification",
        lazy="raise",
    )

    @property
    def is_read(self) -> bool:
        """Whether the recipient has read this notification."""
        return self.read_at is not None

    def __repr__(self) -> str:
        return (
            f"<Notification id={self.id!s:.8} kind={self.kind!r} "
            f"recipient={self.recipient_user_id!s:.8}>"
        )


class NotificationPreference(Base):
    """One user's channel choices, per notification kind (FR-3).

    ``preferences`` maps a kind code to a channel map, e.g.
    ``{"billing.discount_approval_requested": {"email": true}}``. Anything not
    listed falls back to the kind's defaults, so an empty object is a valid —
    and the usual — value.
    """

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="User these preferences belong to.",
    )
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Kind code -> {channel: enabled} overrides.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="Timestamp of the last change (UTC).",
    )

    def __repr__(self) -> str:
        return f"<NotificationPreference user={self.user_id!s:.8}>"


class NotificationDelivery(UUIDPrimaryKeyMixin, CommonColumnsMixin, Base):
    """One out-of-band send of a notification, with its retry state (FR-5)."""

    __tablename__ = "notification_deliveries"

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        Index(
            "ix_notification_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_notification_deliveries_notification", "notification_id"),
    )

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hospitals.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Owning tenant, carried so deliveries are directly tenant-filterable.",
    )
    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
        comment="Notification being delivered.",
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        SQLEnum(
            NotificationChannel,
            name="notification_channel",
            create_type=False,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        comment="Channel this delivery uses.",
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        SQLEnum(
            DeliveryStatus,
            name="notification_delivery_status",
            create_type=False,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=DeliveryStatus.QUEUED,
        comment="queued, sent, failed, or delivered.",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="How many times a send has been tried.",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Why the most recent attempt failed."
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When a queued delivery is next due (UTC). NULL once finished.",
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the send was accepted (UTC)."
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When receipt was confirmed (UTC)."
    )
    to_address: Mapped[str] = mapped_column(
        String(320), nullable=False, comment="Address the delivery is sent to."
    )
    subject: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="Email subject line."
    )
    body: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Content to send. Cleared once sent or finally failed: it may hold a token.",
    )

    notification: Mapped[Notification] = relationship(
        "Notification",
        foreign_keys="NotificationDelivery.notification_id",
        back_populates="deliveries",
        lazy="joined",
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationDelivery id={self.id!s:.8} channel={self.channel} "
            f"status={self.status} attempts={self.attempts}>"
        )
