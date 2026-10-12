"""Repository tests for notifications, preferences and deliveries.

Real Postgres, rolled back per test. Every read of a user's notifications is
checked for isolation twice over: against another hospital and against another
user in the same hospital (``backend/CLAUDE.md``: "every repository method has
at least one test that verifies ``hospital_id`` filtering").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from app.models.notification import DeliveryStatus, NotificationChannel
from app.repositories.notification_repository import NotificationRepository
from app.repositories.user_repository import UserRepository
from app.tests.billing_helpers import insert_user, insert_user_with_permissions

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.notification import Notification
    from app.models.user import User

pytestmark = pytest.mark.database

NOW = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)


@pytest.fixture
def repository(db_session: AsyncSession) -> NotificationRepository:
    """A repository bound to the rolled-back test session."""
    return NotificationRepository(db_session)


async def _notify(
    repository: NotificationRepository, user: User, title: str = "Hello", **fields: Any
) -> Notification:
    """Insert a notification for a user."""
    assert user.hospital_id is not None
    values: dict[str, Any] = {"kind": "system.broadcast", "body": "Body"}
    values.update(fields)
    return await repository.create_notification(
        hospital_id=user.hospital_id, recipient_user_id=user.id, title=title, **values
    )


class TestCreate:
    async def test_persists_unread_and_in_app_by_default(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        user = await insert_user(db_session, hospital_id)

        notification = await _notify(repository, user, link="/billing", created_by=user.id)

        assert notification.id is not None
        assert notification.hospital_id == hospital_id
        assert notification.read_at is None
        assert notification.in_app is True
        assert notification.sent_email is False
        assert notification.link == "/billing"
        assert notification.created_at.tzinfo is not None


class TestReadsAreScopedToTheRecipient:
    async def test_list_and_count_exclude_other_users_and_other_hospitals(
        self,
        repository: NotificationRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        colleague = await insert_user(db_session, hospital_id)
        outsider = await insert_user(db_session, other_hospital_id)
        mine = await _notify(repository, me, "Mine")
        await _notify(repository, colleague, "Colleague's")
        await _notify(repository, outsider, "Outsider's")

        listed = await repository.list_for_user(hospital_id, me.id)

        assert [n.id for n in listed] == [mine.id]
        assert await repository.count_for_user(hospital_id, me.id) == 1

    async def test_the_right_user_under_the_wrong_hospital_sees_nothing(
        self,
        repository: NotificationRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        mine = await _notify(repository, me)

        assert await repository.list_for_user(other_hospital_id, me.id) == []
        assert await repository.count_for_user(other_hospital_id, me.id) == 0
        assert await repository.get_for_user(other_hospital_id, me.id, mine.id) is None

    async def test_get_for_user_refuses_someone_elses_notification(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        colleague = await insert_user(db_session, hospital_id)
        theirs = await _notify(repository, colleague)

        assert await repository.get_for_user(hospital_id, me.id, theirs.id) is None
        found = await repository.get_for_user(hospital_id, colleague.id, theirs.id)
        assert found is not None
        assert found.id == theirs.id

    async def test_an_email_only_notification_is_not_in_the_centre(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        hidden = await _notify(repository, me, in_app=False)

        assert await repository.list_for_user(hospital_id, me.id) == []
        assert await repository.count_for_user(hospital_id, me.id, unread_only=True) == 0
        assert await repository.get_for_user(hospital_id, me.id, hidden.id) is None


class TestListing:
    async def test_newest_first_with_paging_and_unread_filter(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        # All three share a transaction, so created_at ties; set it explicitly.
        made = []
        for index, title in enumerate(("First", "Second", "Third")):
            notification = await _notify(repository, me, title)
            notification.created_at = NOW + timedelta(minutes=index)
            made.append(notification)
        await db_session.flush()
        await repository.mark_read(made[1], read_at=NOW)

        page_one = await repository.list_for_user(hospital_id, me.id, limit=2)
        page_two = await repository.list_for_user(hospital_id, me.id, skip=2, limit=2)
        unread = await repository.list_for_user(hospital_id, me.id, unread_only=True)

        assert [n.title for n in page_one] == ["Third", "Second"]
        assert [n.title for n in page_two] == ["First"]
        assert [n.title for n in unread] == ["Third", "First"]
        assert await repository.count_for_user(hospital_id, me.id) == 3
        assert await repository.count_for_user(hospital_id, me.id, unread_only=True) == 2


class TestMarkRead:
    async def test_mark_read_stamps_the_time_and_the_reader(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        notification = await _notify(repository, me)

        updated = await repository.mark_read(notification, read_at=NOW)

        assert updated.read_at == NOW
        assert updated.updated_by == me.id

    async def test_mark_all_read_touches_only_the_callers_unread(
        self,
        repository: NotificationRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        colleague = await insert_user(db_session, hospital_id)
        outsider = await insert_user(db_session, other_hospital_id)
        earlier = NOW - timedelta(hours=1)
        already = await _notify(repository, me, "Already read")
        await repository.mark_read(already, read_at=earlier)
        await _notify(repository, me, "One")
        await _notify(repository, me, "Two")
        await _notify(repository, colleague)
        await _notify(repository, outsider)

        marked = await repository.mark_all_read(hospital_id, me.id, read_at=NOW)

        assert marked == 2
        assert await repository.count_for_user(hospital_id, me.id, unread_only=True) == 0
        # The one read earlier keeps its original time.
        await db_session.refresh(already)
        assert already.read_at == earlier
        assert await repository.count_for_user(hospital_id, colleague.id, unread_only=True) == 1
        assert (
            await repository.count_for_user(other_hospital_id, outsider.id, unread_only=True) == 1
        )

    async def test_mark_all_read_under_the_wrong_hospital_marks_nothing(
        self,
        repository: NotificationRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        await _notify(repository, me)

        assert await repository.mark_all_read(other_hospital_id, me.id, read_at=NOW) == 0
        assert await repository.count_for_user(hospital_id, me.id, unread_only=True) == 1


class TestPreferences:
    async def test_nothing_stored_is_an_empty_dict(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        me = await insert_user(db_session, hospital_id)

        assert await repository.get_preferences(me.id) == {}
        assert await repository.get_preferences_for_users([me.id]) == {}
        assert await repository.get_preferences_for_users([]) == {}

    async def test_save_is_an_upsert(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        me = await insert_user(db_session, hospital_id)

        await repository.save_preferences(me.id, {"system.broadcast": {"email": True}})
        await repository.save_preferences(me.id, {"system.broadcast": {"in_app": False}})

        assert await repository.get_preferences(me.id) == {"system.broadcast": {"in_app": False}}

    async def test_bulk_lookup_returns_only_the_users_asked_for(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        colleague = await insert_user(db_session, hospital_id)
        silent = await insert_user(db_session, hospital_id)
        await repository.save_preferences(me.id, {"system.broadcast": {"email": True}})
        await repository.save_preferences(colleague.id, {"system.broadcast": {"in_app": False}})

        found = await repository.get_preferences_for_users([me.id, silent.id])

        assert found == {me.id: {"system.broadcast": {"email": True}}}


async def _queue(repository: NotificationRepository, user: User, *, due_at: datetime = NOW) -> Any:
    """Queue an email for a fresh notification."""
    notification = await _notify(repository, user)
    return await repository.create_email_delivery(
        notification=notification,
        to_address=user.email,
        subject="Subject",
        body="Body with a token",
        due_at=due_at,
    )


class TestDeliveries:
    async def test_a_queued_email_takes_its_tenant_from_the_notification(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        me = await insert_user(db_session, hospital_id)

        delivery = await _queue(repository, me)

        assert delivery.hospital_id == hospital_id
        assert delivery.channel is NotificationChannel.EMAIL
        assert delivery.status is DeliveryStatus.QUEUED
        assert delivery.attempts == 0
        assert delivery.next_attempt_at == NOW

    async def test_claim_returns_only_what_is_queued_and_due_oldest_first(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        later = await _queue(repository, me, due_at=NOW - timedelta(minutes=1))
        sooner = await _queue(repository, me, due_at=NOW - timedelta(minutes=5))
        future = await _queue(repository, me, due_at=NOW + timedelta(minutes=5))
        sent = await _queue(repository, me, due_at=NOW - timedelta(minutes=9))
        await repository.update_delivery(sent, status=DeliveryStatus.SENT, next_attempt_at=None)

        claimed = await repository.claim_due_deliveries(now=NOW, limit=10)

        ours = [d.id for d in claimed if d.id in {later.id, sooner.id, future.id, sent.id}]
        assert ours == [sooner.id, later.id]
        # The notification comes loaded, so the service can flag it without IO.
        assert claimed[0].notification.recipient_user_id is not None

    async def test_claim_respects_the_limit(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        me = await insert_user(db_session, hospital_id)
        for minutes in (3, 2, 1):
            await _queue(repository, me, due_at=NOW - timedelta(minutes=minutes))

        assert len(await repository.claim_due_deliveries(now=NOW, limit=2)) == 2

    async def test_claim_drains_every_hospital(
        self,
        repository: NotificationRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        # The worker has no tenant: this is the one documented unscoped query.
        ours = await _queue(repository, await insert_user(db_session, hospital_id))
        theirs = await _queue(repository, await insert_user(db_session, other_hospital_id))

        claimed = {d.id for d in await repository.claim_due_deliveries(now=NOW, limit=50)}

        assert {ours.id, theirs.id} <= claimed

    async def test_update_delivery_persists(
        self, repository: NotificationRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        delivery = await _queue(repository, await insert_user(db_session, hospital_id))

        await repository.update_delivery(
            delivery, status=DeliveryStatus.FAILED, attempts=5, last_error="refused", body=None
        )
        await db_session.refresh(delivery)

        assert delivery.status is DeliveryStatus.FAILED
        assert delivery.attempts == 5
        assert delivery.last_error == "refused"
        assert delivery.body is None

    async def test_list_deliveries_is_tenant_scoped(
        self,
        repository: NotificationRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        delivery = await _queue(repository, await insert_user(db_session, hospital_id))

        listed = await repository.list_deliveries(hospital_id, delivery.notification_id)

        assert [d.id for d in listed] == [delivery.id]
        assert await repository.list_deliveries(other_hospital_id, delivery.notification_id) == []


class TestListActiveRecipients:
    """``UserRepository.list_active_recipients`` — who a notification goes to."""

    async def test_everyone_active_in_the_hospital_and_nobody_else(
        self, db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
    ) -> None:
        from app.models.user import UserStatus

        users = UserRepository(db_session)
        active = await insert_user(db_session, hospital_id)
        suspended = await insert_user(db_session, hospital_id)
        invited = await insert_user(db_session, hospital_id)
        suspended.status = UserStatus.SUSPENDED
        invited.status = UserStatus.INVITED
        outsider = await insert_user(db_session, other_hospital_id)
        await db_session.flush()

        found = {u.id for u in await users.list_active_recipients(hospital_id)}

        assert active.id in found
        assert found.isdisjoint({suspended.id, invited.id, outsider.id})

    async def test_narrowed_by_permission(
        self, db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
    ) -> None:
        users = UserRepository(db_session)
        code = "invoice.approve_discount"
        approver = await insert_user_with_permissions(db_session, hospital_id, [code])
        await insert_user_with_permissions(db_session, hospital_id, ["invoice.read"])
        await insert_user(db_session, hospital_id)
        # Holds the permission, but in another hospital.
        await insert_user_with_permissions(db_session, other_hospital_id, [code])

        found = await users.list_active_recipients(hospital_id, permission_code=code)

        assert [u.id for u in found] == [approver.id]

    async def test_narrowed_by_role(self, db_session: AsyncSession, hospital_id: uuid.UUID) -> None:
        from sqlalchemy import select

        from app.models.user import UserRole

        users = UserRepository(db_session)
        member = await insert_user_with_permissions(db_session, hospital_id, ["patient.read"])
        await insert_user_with_permissions(db_session, hospital_id, ["patient.read"])
        role_id = (
            await db_session.execute(select(UserRole.role_id).where(UserRole.user_id == member.id))
        ).scalar_one()

        found = await users.list_active_recipients(hospital_id, role_id=role_id)

        assert [u.id for u in found] == [member.id]
