"""Background job that drains the notification email queue.

``docs/modules/11-notifications.md`` §5: emails are not sent in the request
that caused them. They are written as ``queued`` deliveries in the same
transaction as the event, and this job sends them.

The queue lives in Postgres, not Redis, on purpose. An email is queued by
committing a row, so it cannot be lost between "the invite was saved" and "the
job was enqueued", and it survives a worker or broker outage: whatever is still
queued when the worker comes back is simply due (§14).

All the logic lives in
:meth:`~app.services.notification_service.NotificationService.deliver_due_emails`
so it is testable without Redis or a running worker. This function only owns
the session lifecycle.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["EMAIL_QUEUE_POLL_SECONDS", "deliver_notification_emails"]

#: How often the queue is polled. AC-2 asks for delivery within 30 seconds of
#: enqueue; a 10-second poll leaves room for the send itself.
EMAIL_QUEUE_POLL_SECONDS = 10


async def deliver_notification_emails(ctx: dict[str, Any]) -> int:
    """Send the queued notification emails that are due.

    :param ctx: Arq job context.
    :returns: How many deliveries were handled.
    """
    from app.core.email import get_email_sender
    from app.database import create_session_factory
    from app.repositories.audit_log_repository import AuditLogRepository
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.notification_repository import NotificationRepository
    from app.repositories.user_repository import UserRepository
    from app.services.audit_service import AuditService
    from app.services.notification_service import NotificationService

    factory = create_session_factory()
    async with factory() as session:
        users = UserRepository(session)
        service = NotificationService(
            NotificationRepository(session),
            users,
            HospitalRepository(session),
            session,
            AuditService(session, AuditLogRepository(session), users),
        )
        report = await service.deliver_due_emails(get_email_sender())

    if report.processed:
        logger.info(
            "job_completed",
            job="deliver_notification_emails",
            sent=report.sent,
            retrying=report.retrying,
            failed=report.failed,
        )
    return report.processed
