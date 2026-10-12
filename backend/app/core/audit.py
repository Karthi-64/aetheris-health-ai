"""Audit recording seam.

CLAUDE.md rule 9 requires every mutating operation to produce an audit entry,
and ``docs/03-ARCHITECTURE.md`` §15 rule 10 repeats it. The durable
``audit_logs`` table and ``AuditService`` are owned by the Audit Logs module
(``docs/modules/12-audit-logs.md``) and do not exist yet.

This module defines the *interface* services record against, plus a structlog
implementation to use until the durable one lands. Services depend on
:class:`AuditSink`, never on a concrete implementation, so the audit module can
drop in its own without touching a single service.

Logs and audit entries are related but different (``docs/03-ARCHITECTURE.md``
§11): logs are observability, audit is compliance. Until the compliance store
exists, this writes to the observability stream — with values stripped.

Usage::

    from app.core.audit import AuditEvent, AuditSink, StructlogAuditSink

    sink: AuditSink = StructlogAuditSink()
    await sink.record(AuditEvent(action="patient.created", ...))
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for dataclass field resolution
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from app.core.logging import get_logger

__all__ = ["AuditEvent", "AuditSink", "StructlogAuditSink"]

_logger = get_logger("aetheris.audit")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One auditable action.

    :param action: Dotted action name, e.g. ``patient.created``. Stable across
        releases — dashboards and compliance queries filter on it.
    :param hospital_id: Tenant the action occurred in. ``None`` only for a
        platform-level event — a patient signing in, before any hospital is
        involved.
    :param target_type: Entity type acted on, e.g. ``patient``.
    :param target_id: UUID of the entity acted on.
    :param actor_id: UUID of the acting user. ``None`` for system actions.
    :param changes: Field-level before/after pairs, as
        ``{"phone": {"before": ..., "after": ...}}``. May contain PII — see
        :class:`StructlogAuditSink` for how that is handled today.
    :param context: Non-PII contextual detail (result counts, filter names).
    :param actor_type: ``"staff"`` (the default: a user, or the system when
        there is no ``actor_id``) or ``"patient"`` for a Patient App action.
    :param patient_account_id: UUID of the acting patient account. Set only
        with ``actor_type="patient"``; a patient is never an ``actor_id``.
    :param ip_address: The caller's address, where the caller is known.
    :param user_agent: The caller's user agent, where known.
    """

    action: str
    hospital_id: uuid.UUID | None
    target_type: str
    target_id: uuid.UUID | None = None
    actor_id: uuid.UUID | None = None
    changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    actor_type: Literal["staff", "patient"] = "staff"
    patient_account_id: uuid.UUID | None = None
    ip_address: str | None = None
    user_agent: str | None = None


@runtime_checkable
class AuditSink(Protocol):
    """Where audit events go.

    Implemented today by :class:`StructlogAuditSink` and, once
    ``docs/modules/12-audit-logs.md`` ships, by the database-backed
    ``AuditService``.
    """

    async def record(self, event: AuditEvent) -> None:
        """Record one audit event.

        Implementations must not raise on a recording failure in a way that
        aborts the caller's business transaction — an audit backend being down
        is not a reason to refuse to register a patient. Log and move on.

        :param event: The event to record.
        """
        ...


class StructlogAuditSink:
    """Interim :class:`AuditSink` that emits structured log events.

    **Values are deliberately dropped.** ``docs/07-SECURITY.md`` rule 10
    requires PII to be redacted or omitted from logs, and a patient diff is
    almost entirely PII. Only the *names* of changed fields are emitted, which
    is enough to answer "who touched what, when" from the observability stream.

    The full before/after values in :attr:`AuditEvent.changes` are passed
    through the interface untouched, so the database-backed ``AuditService``
    can persist them to the access-controlled ``audit_logs`` table when it
    lands.
    """

    async def record(self, event: AuditEvent) -> None:
        """Emit the event as a structured log entry, without field values.

        :param event: The event to record.
        """
        # A staff event keeps exactly the line it always had; the patient
        # fields appear only on a patient event.
        patient: dict[str, Any] = {}
        if event.actor_type == "patient":
            patient = {
                "actor_type": "patient",
                "patient_account_id": (
                    str(event.patient_account_id) if event.patient_account_id else None
                ),
            }
        _logger.info(
            event.action,
            hospital_id=str(event.hospital_id),
            actor_id=str(event.actor_id) if event.actor_id else None,
            target_type=event.target_type,
            target_id=str(event.target_id) if event.target_id else None,
            # Names only — never values. See the class docstring.
            changed_fields=sorted(event.changes),
            **patient,
            **event.context,
        )
