"""Audit log model — the immutable compliance trail.

One row per auditable action. See ``docs/modules/12-audit-logs.md`` and
``docs/05-DATABASE_DESIGN.md`` §2.21 for the table contract. Rows are
append-only: the repository exposes no update or delete, and the migration
grants the application role only INSERT and SELECT (§8 of the module spec).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for column type resolution
from datetime import datetime  # noqa: TC003
from typing import Any

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditLog(Base):
    """One immutable audit entry.

    :attr:`hospital_id` is ``NULL`` for platform-level events and
    :attr:`actor_user_id` is ``NULL`` for system actions (§2.21). A Patient App
    action has ``actor_type = "patient"`` and names its account in
    :attr:`patient_account_id`; a patient is never a ``users`` row, so
    :attr:`actor_user_id` stays ``NULL`` for it.
    """

    __tablename__ = "audit_logs"

    __table_args__ = (
        Index("ix_audit_hospital_created", "hospital_id", "created_at"),
        Index("ix_audit_actor", "actor_user_id"),
        Index("ix_audit_target", "target_type", "target_id"),
        Index("ix_audit_action", "action"),
        Index("ix_audit_patient_account", "patient_account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="Tenant; NULL for platform events."
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="Acting user; NULL for system actions."
    )
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", comment="user / system / ai / patient."
    )
    patient_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="Acting patient account, for actor_type 'patient'; NULL otherwise.",
    )
    action: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Dotted action name, e.g. patient.created."
    )
    target_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Entity type acted on."
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="UUID of the entity acted on."
    )
    before: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="Field values before the change."
    )
    after: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="Field values after the change."
    )
    context: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Non-PII context (reason, counts) carried by the AuditEvent.",
    )
    ip_address: Mapped[str | None] = mapped_column(
        INET, nullable=True, comment="Client address where available."
    )
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="Correlates with request logs (§16)."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When the action occurred.",
    )
