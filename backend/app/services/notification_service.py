"""Business logic for the Notifications module.

Three jobs (``docs/modules/11-notifications.md`` §5):

1. **Turn an event into notifications.** :meth:`NotificationService.notify`
   resolves who should hear about it, consults each person's preferences, and
   writes the in-app notification and — where wanted — a queued email. It
   runs inside the *caller's* transaction and never commits, so a
   notification exists if and only if the thing it is about was committed.
2. **Serve the notification centre** — list, unread count, mark read,
   preferences, broadcast.
3. **Drain the email queue.** :meth:`NotificationService.deliver_due_emails`
   is what the background worker calls.

**In-app delivery cannot fail** (business rule 4): writing the row is
delivering it. **Email can**, so it is queued and retried with exponential
backoff for up to five attempts (rule 3), and every email ends as ``sent`` or
as ``failed`` with a reason — never silently dropped (§7).

**A token never outlives its email.** Invitation and reset emails carry a
single-use token. It is rendered only into the queued delivery's body, which is
cleared the moment the delivery is sent or gives up. The in-app notification
never contains it.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.audit import AuditEvent
from app.core.email import EmailMessage
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.notifications import NotificationRequest
from app.core.tenancy import TenantScope, tenant_scope
from app.models.notification import DeliveryStatus, NotificationChannel
from app.schemas.common import Page, PaginationParams
from app.schemas.notification import (
    BroadcastRequest,
    BroadcastResponse,
    KindPreferenceResponse,
    NotificationPreferencesResponse,
    NotificationResponse,
    ReadAllResponse,
    UnreadCountResponse,
    UpdatePreferencesRequest,
)
from app.services.notification_catalog import (
    KINDS,
    USER_CHANNELS,
    NotificationKind,
    get_kind,
    render,
    resolve_channels,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.core.email import EmailSender
    from app.models.notification import Notification, NotificationDelivery
    from app.models.user import User
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.notification_repository import NotificationRepository
    from app.repositories.user_repository import UserRepository

logger = get_logger(__name__)

__all__ = [
    "EMAIL_BACKOFF_BASE_SECONDS",
    "MAX_EMAIL_ATTEMPTS",
    "DeliveryReport",
    "NotificationNotFoundError",
    "NotificationService",
]

#: Business rule 3: "up to 5 attempts".
MAX_EMAIL_ATTEMPTS = 5

#: First retry delay. Each later one doubles: 30s, 1m, 2m, 4m.
EMAIL_BACKOFF_BASE_SECONDS = 30

#: Recorded on a delivery when no mail transport is configured.
_NO_TRANSPORT = "Email transport is not configured (SMTP_HOST is unset)."

#: Used in templates when the actor is the system or cannot be resolved.
_UNKNOWN_ACTOR = "An administrator"

_TITLE_MAX = 200
_ERROR_MAX = 500


class NotificationNotFoundError(NotFoundError):
    """Raised when a notification is not in the caller's notification centre.

    Covers one in another hospital and one addressed to someone else: both
    must be indistinguishable from a miss.
    """

    def __init__(self, notification_id: uuid.UUID) -> None:
        super().__init__(
            message="Notification not found.", detail={"notification_id": str(notification_id)}
        )


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """What one run of the email queue did.

    :param sent: Emails accepted by the mail server.
    :param retrying: Emails that failed and are queued for another attempt.
    :param failed: Emails that failed for good.
    """

    sent: int = 0
    retrying: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        """Total deliveries handled."""
        return self.sent + self.retrying + self.failed


class _NoEmailQueuedError(Exception):
    """A credential's email was not queued (no recipient, no address, channel off)."""


class NotificationService:
    """Creates, serves and delivers notifications.

    :param notifications: Notification, preference and delivery data access.
    :param users: User lookups, for recipients and their addresses.
    :param hospitals: Hospital lookups, for the name used in templates.
    :param session: Session held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    """

    def __init__(
        self,
        notifications: NotificationRepository,
        users: UserRepository,
        hospitals: HospitalRepository,
        session: AsyncSession,
        audit: AuditSink,
    ) -> None:
        self._notifications = notifications
        self._users = users
        self._hospitals = hospitals
        self._session = session
        self._audit = audit
        #: Emails this instance has queued; see :meth:`deliver_credential`.
        self._emails_queued = 0

    # ── Notifier: events from other modules ───────────────────────────────────

    async def notify(self, request: NotificationRequest) -> None:
        """Turn a request from another module into notifications.

        Runs in a savepoint inside the caller's transaction and **never
        raises**: failing to notify must not undo the invite, reset or
        discount that prompted it. A failure is logged with the kind and
        tenant, and the caller carries on.

        :param request: What happened, and who should hear about it.
        """
        try:
            async with self._session.begin_nested():
                await self._create(request)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            if request.secret_variables:
                # The request carries a credential (an invitation or reset
                # link) that was on its way into an email body. An error
                # raised while writing that body can quote it — a database
                # error prints the offending value — so neither the message
                # nor the traceback is logged. Only what kind of error it was.
                logger.error(
                    "notification.create_failed",
                    kind=request.kind,
                    hospital_id=str(request.hospital_id),
                    error_type=type(exc).__name__,
                )
                return
            logger.exception(
                "notification.create_failed",
                kind=request.kind,
                hospital_id=str(request.hospital_id),
            )

    async def deliver_credential(self, request: NotificationRequest) -> bool:
        """Queue the email that carries a one-time credential, and report whether it was queued.

        Like :meth:`notify`, this runs in a savepoint and never raises. Unlike
        it, the caller is told the outcome, and "nothing was queued" undoes
        the whole request: an in-app "you have been invited" notice with no
        email behind it would be a promise nobody keeps.

        Nothing rendered from the request is logged — not the message of an
        error, which can quote the email body, and not a traceback.

        :param request: The request; its ``secret_variables`` carry the link.
        :returns: ``True`` only if an email was queued.
        """
        queued_before = self._emails_queued
        try:
            async with self._session.begin_nested():
                await self._create(request)
                if self._emails_queued == queued_before:
                    raise _NoEmailQueuedError
        except Exception as exc:  # noqa: BLE001 — never raises; see the docstring
            self._emails_queued = queued_before
            logger.error(
                "notification.credential_not_queued",
                kind=request.kind,
                hospital_id=str(request.hospital_id),
                error_type=type(exc).__name__,
            )
            return False
        return True

    async def _create(self, request: NotificationRequest) -> list[Notification]:
        """Write the notifications, and queue the emails, for one request.

        :param request: The request to fulfil.
        :returns: The notifications created, one per recipient who has at
            least one channel enabled.
        """
        kind = get_kind(request.kind)
        recipients = await self._resolve_recipients(request)
        if not recipients:
            logger.info(
                "notification.no_recipients", kind=kind.code, hospital_id=str(request.hospital_id)
            )
            return []

        shared = await self._shared_variables(request)
        preferences = await self._notifications.get_preferences_for_users(
            [user.id for user in recipients]
        )
        now = datetime.now(UTC)
        created: list[Notification] = []

        for user in recipients:
            channels = resolve_channels(kind, preferences.get(user.id))
            if not channels:
                continue

            variables = {**shared, "first_name": user.first_name, **request.variables}
            notification = await self._notifications.create_notification(
                hospital_id=request.hospital_id,
                recipient_user_id=user.id,
                kind=kind.code,
                title=render(kind.title, variables)[:_TITLE_MAX],
                body=render(kind.body, variables),
                link=request.link or kind.link,
                in_app=NotificationChannel.IN_APP in channels,
                created_by=request.actor_id,
            )
            created.append(notification)

            if NotificationChannel.EMAIL in channels:
                await self._queue_email(
                    kind, notification, user, {**variables, **request.secret_variables}, now
                )

        logger.info(
            "notification.created",
            kind=kind.code,
            hospital_id=str(request.hospital_id),
            recipients=len(created),
        )
        return created

    async def _queue_email(
        self,
        kind: NotificationKind,
        notification: Notification,
        user: User,
        variables: dict[str, str],
        now: datetime,
    ) -> None:
        """Queue the email form of a notification for one recipient."""
        # resolve_channels only offers EMAIL for a kind that has both templates.
        assert kind.email_subject is not None
        assert kind.email_body is not None
        if not user.email:
            # §14: no address means no email, quietly — the in-app copy stands.
            logger.info(
                "notification.email_skipped_no_address", notification_id=str(notification.id)
            )
            return
        await self._notifications.create_email_delivery(
            notification=notification,
            to_address=user.email,
            subject=render(kind.email_subject, variables)[:_TITLE_MAX],
            body=render(kind.email_body, variables),
            due_at=now,
        )
        self._emails_queued += 1

    async def _resolve_recipients(self, request: NotificationRequest) -> list[User]:
        """Work out who a request is for.

        Explicit recipients are taken as given, provided they belong to the
        request's hospital — including an ``invited`` user, who is exactly who
        an invitation is for. Recipients found by permission are active users
        only, and never the actor: nobody needs telling what they just did.
        """
        if request.recipient_permission is not None:
            holders = await self._users.list_active_recipients(
                request.hospital_id, permission_code=request.recipient_permission
            )
            return [user for user in holders if user.id != request.actor_id]

        recipients: list[User] = []
        for user_id in dict.fromkeys(request.recipient_user_ids):
            user = await self._users.get_by_id(user_id, request.hospital_id)
            if user is not None and user.hospital_id == request.hospital_id:
                recipients.append(user)
        return recipients

    async def _shared_variables(self, request: NotificationRequest) -> dict[str, str]:
        """Return the template variables every notification in a request shares."""
        hospital = await self._hospitals.get_by_id(request.hospital_id)
        actor = (
            await self._users.get_by_id(request.actor_id, request.hospital_id)
            if request.actor_id
            else None
        )
        return {
            "hospital_name": hospital.name if hospital else "your hospital",
            "actor_name": f"{actor.first_name} {actor.last_name}" if actor else _UNKNOWN_ACTOR,
        }

    # ── Notification centre ───────────────────────────────────────────────────

    async def list_mine(
        self,
        hospital_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        unread_only: bool = False,
    ) -> Page[NotificationResponse]:
        """List the caller's notifications, newest first (FR-1).

        :param hospital_id: The caller's hospital.
        :param user_id: The caller.
        :param pagination: Page and page size. Defaults to page 1.
        :param unread_only: Only notifications not yet read.
        :returns: One page of notifications plus the total count.
        """
        page_params = pagination or PaginationParams()
        rows = await self._notifications.list_for_user(
            hospital_id,
            user_id,
            unread_only=unread_only,
            skip=page_params.offset,
            limit=page_params.limit,
        )
        total = await self._notifications.count_for_user(
            hospital_id, user_id, unread_only=unread_only
        )
        return Page[NotificationResponse](
            items=[NotificationResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    async def unread_count(self, hospital_id: uuid.UUID, user_id: uuid.UUID) -> UnreadCountResponse:
        """Return the number on the caller's bell (FR-1)."""
        unread = await self._notifications.count_for_user(hospital_id, user_id, unread_only=True)
        return UnreadCountResponse(unread=unread)

    async def mark_read(
        self, hospital_id: uuid.UUID, user_id: uuid.UUID, notification_id: uuid.UUID
    ) -> NotificationResponse:
        """Mark one of the caller's notifications read.

        Idempotent: marking a read notification again changes nothing and
        records nothing.

        :raises NotificationNotFoundError: If it is not the caller's.
        """
        notification = await self._notifications.get_for_user(hospital_id, user_id, notification_id)
        if notification is None:
            raise NotificationNotFoundError(notification_id)
        if notification.is_read:
            return NotificationResponse.from_model(notification)

        notification = await self._notifications.mark_read(notification, read_at=datetime.now(UTC))
        await self._audit.record(
            AuditEvent(
                action="notification.read",
                hospital_id=hospital_id,
                target_type="notification",
                target_id=notification.id,
                actor_id=user_id,
            )
        )
        await self._session.commit()
        return NotificationResponse.from_model(notification)

    async def mark_all_read(self, hospital_id: uuid.UUID, user_id: uuid.UUID) -> ReadAllResponse:
        """Mark every unread notification of the caller as read."""
        marked = await self._notifications.mark_all_read(
            hospital_id, user_id, read_at=datetime.now(UTC)
        )
        if marked:
            await self._audit.record(
                AuditEvent(
                    action="notification.read_all",
                    hospital_id=hospital_id,
                    target_type="notification",
                    actor_id=user_id,
                    context={"marked": marked},
                )
            )
            await self._session.commit()
        return ReadAllResponse(marked=marked)

    # ── Preferences ───────────────────────────────────────────────────────────

    async def get_preferences(self, user_id: uuid.UUID) -> NotificationPreferencesResponse:
        """Return every kind with the channels in effect for the caller (FR-3)."""
        stored = await self._notifications.get_preferences(user_id)
        return self._preferences_response(stored)

    async def update_preferences(
        self,
        hospital_id: uuid.UUID,
        user_id: uuid.UUID,
        payload: UpdatePreferencesRequest,
    ) -> NotificationPreferencesResponse:
        """Change the caller's channel preferences (FR-3).

        Kinds not mentioned keep what was stored. A critical kind's default
        channels stay on whatever is sent for them (AC-4): the choice is
        stored, and the response shows what is actually in effect.

        :raises ValidationError: If a kind code is unknown.
        """
        unknown = sorted(code for code in payload.preferences if code not in KINDS)
        if unknown:
            raise ValidationError(
                message=f"Unknown notification kind: {', '.join(unknown)}.",
                detail={
                    "errors": [
                        {"field": f"preferences.{code}", "message": "Unknown notification kind."}
                        for code in unknown
                    ]
                },
            )

        stored = await self._notifications.get_preferences(user_id)
        merged: dict[str, Any] = {code: dict(value) for code, value in stored.items()}
        for code, choice in payload.preferences.items():
            entry = dict(merged.get(code) or {})
            entry.update(choice.model_dump(exclude_none=True))
            merged[code] = entry

        await self._notifications.save_preferences(user_id, merged)
        await self._audit.record(
            AuditEvent(
                action="notification.preferences_updated",
                hospital_id=hospital_id,
                target_type="user",
                target_id=user_id,
                actor_id=user_id,
                context={"kinds": sorted(payload.preferences)},
            )
        )
        await self._session.commit()
        return self._preferences_response(merged)

    @staticmethod
    def _preferences_response(stored: dict[str, Any]) -> NotificationPreferencesResponse:
        """Build the preferences page from stored overrides."""
        kinds = []
        for kind in KINDS.values():
            channels = resolve_channels(kind, stored)
            locked = kind.default_channels if kind.critical else frozenset()
            kinds.append(
                KindPreferenceResponse(
                    kind=kind.code,
                    category=kind.category,
                    label=kind.label,
                    critical=kind.critical,
                    in_app=NotificationChannel.IN_APP in channels,
                    email=NotificationChannel.EMAIL in channels,
                    email_available=kind.supports_email,
                    locked_channels=[c.value for c in USER_CHANNELS if c in locked],
                )
            )
        return NotificationPreferencesResponse(kinds=kinds)

    # ── Broadcast ─────────────────────────────────────────────────────────────

    async def broadcast(
        self,
        hospital_id: uuid.UUID,
        payload: BroadcastRequest,
        *,
        actor_id: uuid.UUID,
    ) -> BroadcastResponse:
        """Send an announcement to a role or to the whole hospital (FR-6).

        Each recipient gets their own notification (business rule 6), so one
        person's email failing does not affect anyone else's.

        Unlike :meth:`notify`, this *does* raise: the broadcast is the action
        the admin asked for, not a side effect of something else.

        :param hospital_id: The hospital to broadcast in.
        :param payload: The announcement.
        :param actor_id: The admin sending it. They receive it too.
        :returns: How many users were notified.
        """
        recipients = await self._users.list_active_recipients(hospital_id, role_id=payload.role_id)
        created = await self._create(
            NotificationRequest(
                kind="system.broadcast",
                hospital_id=hospital_id,
                recipient_user_ids=tuple(user.id for user in recipients),
                variables={"title": payload.title, "body": payload.body},
                link=payload.link,
                actor_id=actor_id,
            )
        )

        await self._audit.record(
            AuditEvent(
                action="notification.broadcast",
                hospital_id=hospital_id,
                target_type="notification",
                actor_id=actor_id,
                context={
                    "recipients": len(created),
                    "role_id": str(payload.role_id) if payload.role_id else None,
                },
            )
        )
        await self._session.commit()
        return BroadcastResponse(recipients=len(created))

    # ── Email queue (called by the worker) ────────────────────────────────────

    async def deliver_due_emails(
        self,
        sender: EmailSender | None,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> DeliveryReport:
        """Send the queued emails that are due.

        Each claimed delivery ends this run in exactly one of three states:

        - ``sent`` — the mail server accepted it;
        - still ``queued`` with a later ``next_attempt_at`` — it failed and
          will be tried again, with the delay doubling each time (rule 3);
        - ``failed`` — it has used all five attempts, or there is no mail
          transport at all, which no amount of retrying will fix.

        The body is cleared in the first and last cases: it may hold a token.

        Tenancy is explicit, because a scheduled job has no user and no single
        hospital (``app/core/tenancy.py``): the queue is *claimed* across
        hospitals under a named system scope, and each delivery is then
        *handled* under its own hospital, read off the row. While that is
        bound the session cannot read or write another hospital's rows.

        :param sender: The mail transport, or ``None`` if email is switched off.
        :param now: The current instant. Defaults to now; a parameter so tests
            can drive the backoff.
        :param limit: Maximum deliveries to handle in one run.
        :returns: What this run did.
        """
        current = now or datetime.now(UTC)
        with tenant_scope(TenantScope.system("email queue: claim due deliveries")):
            deliveries = await self._notifications.claim_due_deliveries(now=current, limit=limit)
        sent = retrying = failed = 0

        for delivery in deliveries:
            with tenant_scope(TenantScope.hospital(delivery.hospital_id)):
                outcome = await self._deliver_one(delivery, sender, current)
            if outcome is DeliveryStatus.SENT:
                sent += 1
            elif outcome is DeliveryStatus.FAILED:
                failed += 1
            else:
                retrying += 1

        await self._session.commit()
        report = DeliveryReport(sent=sent, retrying=retrying, failed=failed)
        if report.processed:
            logger.info(
                "notification.email_queue_run",
                sent=sent,
                retrying=retrying,
                failed=failed,
            )
        return report

    async def _deliver_one(
        self,
        delivery: NotificationDelivery,
        sender: EmailSender | None,
        current: datetime,
    ) -> DeliveryStatus:
        """Attempt one queued email, inside its own hospital.

        The caller has bound the delivery's hospital as the tenant scope.

        :param delivery: A claimed, locked delivery.
        :param sender: The mail transport, or ``None`` if email is switched off.
        :param current: The instant this run is measured against.
        :returns: ``SENT``, ``FAILED``, or ``QUEUED`` when it will be retried.
        """
        attempts = delivery.attempts + 1

        if sender is None:
            await self._notifications.update_delivery(
                delivery,
                status=DeliveryStatus.FAILED,
                attempts=attempts,
                last_error=_NO_TRANSPORT,
                next_attempt_at=None,
                body=None,
            )
            return DeliveryStatus.FAILED

        try:
            await sender.send(
                EmailMessage(
                    to=delivery.to_address,
                    subject=delivery.subject or "",
                    body=delivery.body or "",
                )
            )
        except Exception as exc:  # noqa: BLE001 — any transport failure is retried
            error = f"{type(exc).__name__}: {exc}"[:_ERROR_MAX]
            exhausted = attempts >= MAX_EMAIL_ATTEMPTS
            if exhausted:
                await self._notifications.update_delivery(
                    delivery,
                    status=DeliveryStatus.FAILED,
                    attempts=attempts,
                    last_error=error,
                    next_attempt_at=None,
                    body=None,
                )
            else:
                delay = EMAIL_BACKOFF_BASE_SECONDS * 2 ** (attempts - 1)
                await self._notifications.update_delivery(
                    delivery,
                    attempts=attempts,
                    last_error=error,
                    next_attempt_at=current + timedelta(seconds=delay),
                )
            # The address and server reply stay out of the log (PII).
            logger.warning(
                "notification.email_attempt_failed",
                delivery_id=str(delivery.id),
                attempts=attempts,
                error_type=type(exc).__name__,
            )
            return DeliveryStatus.FAILED if exhausted else DeliveryStatus.QUEUED

        # Marked before the delivery update, not after: that update flushes,
        # so the notification row is written while its own hospital is still
        # the bound tenant scope. Left for a later flush it would be written
        # under the next delivery's hospital, which the session refuses.
        delivery.notification.sent_email = True
        await self._notifications.update_delivery(
            delivery,
            status=DeliveryStatus.SENT,
            attempts=attempts,
            last_error=None,
            next_attempt_at=None,
            sent_at=current,
            body=None,
        )
        return DeliveryStatus.SENT
