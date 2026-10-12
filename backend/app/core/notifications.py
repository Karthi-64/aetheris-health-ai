"""Notification seam.

``docs/modules/11-notifications.md`` §5 has every module emit an event that
the Notifications module turns into messages. This file defines the
*interface* a service emits against, in the same way :mod:`app.core.audit`
defines the audit one: services depend on :class:`Notifier`, never on
``NotificationService``, so a module can say "tell the approvers" without
importing anything from Notifications or knowing who the approvers are.

Usage::

    from app.core.notifications import NotificationRequest, Notifier

    await notifier.notify(
        NotificationRequest(
            kind="billing.discount_approval_requested",
            hospital_id=hospital_id,
            recipient_permission="invoice.approve_discount",
            variables={"patient_name": "Ananya Rao", "discount_amount": "200.00"},
            link="/billing",
        )
    )
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for dataclass field resolution
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = ["NotificationRequest", "Notifier", "NullNotifier"]


@dataclass(frozen=True, slots=True)
class NotificationRequest:
    """A request to notify one or more users about something that happened.

    Recipients are named either explicitly or by a permission they hold — not
    both. "Everyone who may approve a discount" is a question about roles, and
    the emitting module should not have to answer it.

    :param kind: Kind code from the notification catalog.
    :param hospital_id: Tenant the event occurred in. Recipients outside it
        are ignored.
    :param recipient_user_ids: Users to notify.
    :param recipient_permission: Notify every active user holding this code.
    :param variables: Values for the kind's templates. These reach the in-app
        text, so they must not contain secrets.
    :param secret_variables: Values used **only** in the email body — a reset
        or invite token. Never written to the notification row.
    :param link: In-app path to open. Overrides the kind's default.
    :param actor_id: User whose action caused this. They are not notified of
        their own action when recipients come from a permission.
    """

    kind: str
    hospital_id: uuid.UUID
    recipient_user_ids: tuple[uuid.UUID, ...] = ()
    recipient_permission: str | None = None
    variables: dict[str, str] = field(default_factory=dict)
    secret_variables: dict[str, str] = field(default_factory=dict)
    link: str | None = None
    actor_id: uuid.UUID | None = None


@runtime_checkable
class Notifier(Protocol):
    """Where notification requests go.

    Implemented by ``NotificationService`` and, for code that runs without the
    Notifications module wired in (unit tests, the background sweeper), by
    :class:`NullNotifier`.
    """

    async def notify(self, request: NotificationRequest) -> None:
        """Turn a request into notifications.

        Implementations must not raise in a way that aborts the caller's
        business transaction: failing to tell an admin about a discount is not
        a reason to refuse the discount. Log and move on.

        :param request: What happened, and who should hear about it.
        """
        ...

    async def deliver_credential(self, request: NotificationRequest) -> bool:
        """Queue the email that carries a one-time credential, and say whether it was queued.

        For an invitation or a password-reset link, where the email is the
        only way the credential may travel. Unlike :meth:`notify`, the caller
        needs the answer: a credential whose email was never queued must not
        be left alive, and nobody may be told it was sent.

        Must not raise, and must not log the request's ``secret_variables`` or
        anything rendered from them.

        :param request: The request; its ``secret_variables`` carry the link.
        :returns: ``True`` only if an email to the recipient was queued.
        """
        ...


class NullNotifier:
    """A :class:`Notifier` that does nothing.

    The default for a service constructed without one, so that notifying is
    opt-in wiring rather than a constructor argument every test must supply.
    """

    async def notify(self, request: NotificationRequest) -> None:
        """Discard the request."""

    async def deliver_credential(self, request: NotificationRequest) -> bool:
        """Queue nothing, and say so."""
        return False
