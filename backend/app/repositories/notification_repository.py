"""Repository for notifications, preferences and deliveries.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4).

Reads of a user's notifications are scoped by **both** ``hospital_id`` and
``recipient_user_id`` (CLAUDE.md rules 4 and 5): a notification belongs to one
person, so the tenant filter alone would still let one user read another's.

One method is deliberately not tenant-scoped:
:meth:`NotificationRepository.claim_due_deliveries` serves the background
worker, which drains every hospital's queue and has no tenant context — the
same documented exception as the appointment no-show sweeper.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationPreference,
)
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class NotificationRepository(BaseRepository[Notification]):
    """Persistence for notifications, preferences and deliveries.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Notification, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _mine(
        self, hospital_id: uuid.UUID, user_id: uuid.UUID, *, unread_only: bool = False
    ) -> Select[tuple[Notification]]:
        """Return a SELECT of one user's notification-centre entries.

        :param hospital_id: The tenant to scope to.
        :param user_id: The recipient.
        :param unread_only: Only entries not yet read.
        """
        stmt = self._query().where(
            Notification.hospital_id == hospital_id,
            Notification.recipient_user_id == user_id,
            Notification.in_app.is_(True),
        )
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        return stmt

    # ── Notifications ─────────────────────────────────────────────────────────

    async def create_notification(
        self,
        *,
        hospital_id: uuid.UUID,
        recipient_user_id: uuid.UUID,
        kind: str,
        title: str,
        body: str,
        link: str | None = None,
        in_app: bool = True,
        created_by: uuid.UUID | None = None,
    ) -> Notification:
        """Insert a notification. Writing it is what delivers it in-app.

        Does not commit — the caller owns the transaction.

        :param hospital_id: Owning tenant.
        :param recipient_user_id: User the notification is for.
        :param kind: Kind code.
        :param title: Rendered headline.
        :param body: Rendered text. Must not contain a secret.
        :param link: In-app path to open.
        :param in_app: Whether it appears in the notification centre.
        :param created_by: User whose action caused it.
        :returns: The persisted notification.
        """
        return await super().create(
            hospital_id=hospital_id,
            recipient_user_id=recipient_user_id,
            kind=kind,
            title=title,
            body=body,
            link=link,
            in_app=in_app,
            created_by=created_by,
        )

    async def list_for_user(
        self,
        hospital_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        unread_only: bool = False,
        skip: int = 0,
        limit: int = 25,
    ) -> list[Notification]:
        """List a user's notification-centre entries, newest first.

        :param hospital_id: The tenant to scope to.
        :param user_id: The recipient.
        :param unread_only: Only entries not yet read.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :returns: A page of notifications.
        """
        stmt = (
            self._mine(hospital_id, user_id, unread_only=unread_only)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_for_user(
        self, hospital_id: uuid.UUID, user_id: uuid.UUID, *, unread_only: bool = False
    ) -> int:
        """Count a user's notification-centre entries.

        With ``unread_only`` this is the number on the bell (FR-1).

        :param hospital_id: The tenant to scope to.
        :param user_id: The recipient.
        :param unread_only: Only entries not yet read.
        :returns: The count.
        """
        stmt = self._mine(hospital_id, user_id, unread_only=unread_only)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()

    async def get_for_user(
        self, hospital_id: uuid.UUID, user_id: uuid.UUID, notification_id: uuid.UUID
    ) -> Notification | None:
        """Retrieve one of a user's notification-centre entries.

        :param hospital_id: The tenant to scope to.
        :param user_id: The recipient.
        :param notification_id: The notification UUID.
        :returns: The notification, or ``None`` if absent, in another tenant,
            or addressed to someone else.
        """
        stmt = self._mine(hospital_id, user_id).where(Notification.id == notification_id)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def mark_read(self, notification: Notification, *, read_at: datetime) -> Notification:
        """Mark one notification read.

        :param notification: The attached notification.
        :param read_at: When it was read (UTC).
        :returns: The updated notification.
        """
        return await self.update(
            notification, read_at=read_at, updated_by=notification.recipient_user_id
        )

    async def mark_all_read(
        self, hospital_id: uuid.UUID, user_id: uuid.UUID, *, read_at: datetime
    ) -> int:
        """Mark every unread notification of a user as read, in one statement.

        :param hospital_id: The tenant to scope to.
        :param user_id: The recipient.
        :param read_at: When they were read (UTC).
        :returns: How many notifications were marked.
        """
        stmt = (
            update(Notification)
            .where(
                Notification.hospital_id == hospital_id,
                Notification.recipient_user_id == user_id,
                Notification.in_app.is_(True),
                Notification.read_at.is_(None),
                Notification.deleted_at.is_(None),
            )
            .values(read_at=read_at, updated_by=user_id)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # ── Preferences ───────────────────────────────────────────────────────────

    async def get_preferences(self, user_id: uuid.UUID) -> dict[str, Any]:
        """Return a user's stored preference overrides.

        :param user_id: The user.
        :returns: Kind code to ``{channel: bool}``. Empty if none are stored.
        """
        result = await self._session.execute(
            select(NotificationPreference.preferences).where(
                NotificationPreference.user_id == user_id
            )
        )
        stored = result.scalar_one_or_none()
        return dict(stored) if stored else {}

    async def get_preferences_for_users(
        self, user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, Any]]:
        """Return stored preferences for several users in one query.

        Used when one event notifies many people, so a broadcast to a hundred
        staff costs one lookup rather than a hundred.

        :param user_ids: The users.
        :returns: User id to preferences, for the users who have any stored.
        """
        if not user_ids:
            return {}
        result = await self._session.execute(
            select(NotificationPreference.user_id, NotificationPreference.preferences).where(
                NotificationPreference.user_id.in_(user_ids)
            )
        )
        return {user_id: dict(preferences or {}) for user_id, preferences in result.all()}

    async def save_preferences(self, user_id: uuid.UUID, preferences: dict[str, Any]) -> None:
        """Store a user's preference overrides, replacing what was there.

        An upsert: the row is created the first time a user changes anything.

        :param user_id: The user.
        :param preferences: Kind code to ``{channel: bool}``.
        """
        stmt = (
            pg_insert(NotificationPreference)
            .values(user_id=user_id, preferences=preferences)
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={"preferences": preferences, "updated_at": func.now()},
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()

    # ── Deliveries ────────────────────────────────────────────────────────────

    async def create_email_delivery(
        self,
        *,
        notification: Notification,
        to_address: str,
        subject: str,
        body: str,
        due_at: datetime,
    ) -> NotificationDelivery:
        """Queue an email for a notification.

        :param notification: The notification being delivered. Supplies the tenant.
        :param to_address: Recipient address.
        :param subject: Rendered subject line.
        :param body: Rendered body. May contain a token; cleared after sending.
        :param due_at: When it becomes due (UTC) — normally now.
        :returns: The persisted delivery, in ``queued``.
        """
        delivery = NotificationDelivery(
            hospital_id=notification.hospital_id,
            notification_id=notification.id,
            channel=NotificationChannel.EMAIL,
            status=DeliveryStatus.QUEUED,
            to_address=to_address,
            subject=subject,
            body=body,
            next_attempt_at=due_at,
        )
        self._session.add(delivery)
        await self._session.flush()
        return delivery

    async def claim_due_deliveries(
        self, *, now: datetime, limit: int
    ) -> list[NotificationDelivery]:
        """Lock and return queued deliveries that are due, oldest first.

        **Not tenant-scoped** — see the module docstring.

        ``FOR UPDATE SKIP LOCKED`` lets two workers drain the queue at once
        without sending the same email twice: a row one worker holds is simply
        not offered to the other. ``of=NotificationDelivery`` because the
        joined notification makes this a join, and only the delivery row needs
        the lock.

        The caller **must** be inside a transaction, and holds the locks until
        it commits.

        :param now: The current instant (UTC).
        :param limit: Maximum deliveries to claim.
        :returns: Due deliveries, each with its notification loaded.
        """
        stmt = (
            select(NotificationDelivery)
            .where(
                NotificationDelivery.status == DeliveryStatus.QUEUED,
                NotificationDelivery.next_attempt_at <= now,
            )
            .order_by(NotificationDelivery.next_attempt_at.asc(), NotificationDelivery.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True, of=NotificationDelivery)
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def update_delivery(self, delivery: NotificationDelivery, **fields: Any) -> None:
        """Apply field updates to a delivery.

        :param delivery: The attached delivery.
        :param fields: Column names and their new values.
        """
        for name, value in fields.items():
            setattr(delivery, name, value)
        await self._session.flush()

    async def list_deliveries(
        self, hospital_id: uuid.UUID, notification_id: uuid.UUID
    ) -> list[NotificationDelivery]:
        """Return a notification's deliveries, oldest first.

        :param hospital_id: The tenant to scope to.
        :param notification_id: The notification.
        :returns: Its deliveries.
        """
        stmt = (
            select(NotificationDelivery)
            .where(
                NotificationDelivery.hospital_id == hospital_id,
                NotificationDelivery.notification_id == notification_id,
            )
            .order_by(NotificationDelivery.created_at.asc(), NotificationDelivery.id.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())
