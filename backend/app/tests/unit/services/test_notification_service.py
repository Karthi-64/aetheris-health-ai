"""Unit tests for the notification service.

The repository is a small in-memory fake rather than a mock, because what
matters here is the *state* a call leaves behind — which notifications exist,
what a queued email contains, what a delivery looks like after a failed
attempt — and that reads far more clearly as assertions on rows than on call
arguments. The SQL itself is covered in the repository suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ValidationError
from app.core.notifications import NotificationRequest, Notifier, NullNotifier
from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
)
from app.schemas.common import PaginationParams
from app.schemas.notification import BroadcastRequest, UpdatePreferencesRequest
from app.services import notification_service as notification_service_module
from app.services.notification_service import (
    EMAIL_BACKOFF_BASE_SECONDS,
    MAX_EMAIL_ATTEMPTS,
    NotificationNotFoundError,
    NotificationService,
)
from app.tests.conftest import FakeSession, RecordingAuditSink

HOSPITAL_ID = uuid.uuid4()
OTHER_HOSPITAL_ID = uuid.uuid4()
NOW = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
TOKEN = "raw-single-use-token"  # noqa: S105 — a literal standing in for a token


def _user(
    *, first_name: str = "Asha", email: str | None = "asha@example.test", hospital_id: Any = None
) -> MagicMock:
    """A user double with the fields the service reads."""
    user = MagicMock()
    user.id = uuid.uuid4()
    user.hospital_id = hospital_id or HOSPITAL_ID
    user.first_name = first_name
    user.last_name = "Menon"
    user.email = email
    return user


class FakeNotificationRepository:
    """In-memory stand-in for ``NotificationRepository``."""

    def __init__(self) -> None:
        self.notifications: list[Notification] = []
        self.deliveries: list[NotificationDelivery] = []
        self.preferences: dict[uuid.UUID, dict[str, Any]] = {}
        self.fail_on_create = False
        #: Raised by the next inserts into the email queue, when set.
        self.email_insert_error: Exception | None = None

    async def create_notification(self, **fields: Any) -> Notification:
        if self.fail_on_create:
            msg = "database is down"
            raise RuntimeError(msg)
        notification = Notification(
            id=uuid.uuid4(),
            read_at=None,
            sent_email=False,
            sent_sms=False,
            created_at=NOW + timedelta(seconds=len(self.notifications)),
            **fields,
        )
        self.notifications.append(notification)
        return notification

    async def create_email_delivery(
        self, *, notification: Notification, due_at: datetime, **fields: Any
    ) -> NotificationDelivery:
        if self.email_insert_error is not None:
            raise self.email_insert_error
        delivery = NotificationDelivery(
            id=uuid.uuid4(),
            hospital_id=notification.hospital_id,
            notification_id=notification.id,
            channel=NotificationChannel.EMAIL,
            status=DeliveryStatus.QUEUED,
            attempts=0,
            next_attempt_at=due_at,
            **fields,
        )
        delivery.__dict__["notification"] = notification
        self.deliveries.append(delivery)
        return delivery

    async def get_preferences_for_users(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
        return {uid: self.preferences[uid] for uid in user_ids if uid in self.preferences}

    async def get_preferences(self, user_id: uuid.UUID) -> dict[str, Any]:
        return dict(self.preferences.get(user_id, {}))

    async def save_preferences(self, user_id: uuid.UUID, preferences: dict[str, Any]) -> None:
        self.preferences[user_id] = preferences

    def _mine(self, hospital_id: uuid.UUID, user_id: uuid.UUID, unread_only: bool) -> list[Any]:
        return [
            n
            for n in sorted(self.notifications, key=lambda n: n.created_at, reverse=True)
            if n.hospital_id == hospital_id
            and n.recipient_user_id == user_id
            and n.in_app
            and not (unread_only and n.read_at is not None)
        ]

    async def list_for_user(
        self,
        hospital_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        unread_only: bool,
        skip: int,
        limit: int,
    ) -> list[Notification]:
        return self._mine(hospital_id, user_id, unread_only)[skip : skip + limit]

    async def count_for_user(
        self, hospital_id: uuid.UUID, user_id: uuid.UUID, *, unread_only: bool = False
    ) -> int:
        return len(self._mine(hospital_id, user_id, unread_only))

    async def get_for_user(
        self, hospital_id: uuid.UUID, user_id: uuid.UUID, notification_id: uuid.UUID
    ) -> Notification | None:
        return next(
            (n for n in self._mine(hospital_id, user_id, False) if n.id == notification_id), None
        )

    async def mark_read(self, notification: Notification, *, read_at: datetime) -> Notification:
        notification.read_at = read_at
        return notification

    async def mark_all_read(
        self, hospital_id: uuid.UUID, user_id: uuid.UUID, *, read_at: datetime
    ) -> int:
        unread = self._mine(hospital_id, user_id, True)
        for notification in unread:
            notification.read_at = read_at
        return len(unread)

    async def claim_due_deliveries(
        self, *, now: datetime, limit: int
    ) -> list[NotificationDelivery]:
        due = [
            d
            for d in self.deliveries
            if d.status is DeliveryStatus.QUEUED
            and d.next_attempt_at is not None
            and d.next_attempt_at <= now
        ]
        return due[:limit]

    async def update_delivery(self, delivery: NotificationDelivery, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(delivery, name, value)


class _World:
    """A service over fakes, plus the handles a test needs to inspect them."""

    def __init__(self, *, users: list[MagicMock] | None = None, actor: MagicMock | None = None):
        self.repo = FakeNotificationRepository()
        self.session = FakeSession()
        self.audit = RecordingAuditSink()
        self.people = {user.id: user for user in (users or [])}
        if actor is not None:
            self.people[actor.id] = actor

        self.users = AsyncMock()
        # Mirrors the repository: a user lookup always carries a tenant scope.
        self.users.get_by_id.side_effect = lambda user_id, _scope: self.people.get(user_id)
        self.users.list_active_recipients.return_value = list(users or [])

        hospital = MagicMock()
        hospital.name = "Demo Hospital"
        self.hospitals = AsyncMock()
        self.hospitals.get_by_id.return_value = hospital

        self.service = NotificationService(
            self.repo,  # type: ignore[arg-type]
            self.users,
            self.hospitals,
            self.session,  # type: ignore[arg-type]
            self.audit,
        )


def _invite(user: MagicMock, **overrides: Any) -> NotificationRequest:
    """An invitation request for one user, carrying a token for the email."""
    values: dict[str, Any] = {
        "kind": "auth.user_invited",
        "hospital_id": HOSPITAL_ID,
        "recipient_user_ids": (user.id,),
        "variables": {"expires_in": "72 hours"},
        "secret_variables": {"action_url": f"https://app.test/reset-password?token={TOKEN}"},
    }
    values.update(overrides)
    return NotificationRequest(**values)


# ── notify ──────────────────────────────────────────────────────────────────


class TestNotify:
    def test_the_service_is_a_notifier(self) -> None:
        assert isinstance(_World().service, Notifier)

    async def test_creates_an_in_app_notification_and_queues_an_email(self) -> None:
        inviter = _user(first_name="Meera")
        user = _user()
        world = _World(users=[user], actor=inviter)

        await world.service.notify(_invite(user, actor_id=inviter.id))

        [notification] = world.repo.notifications
        assert notification.recipient_user_id == user.id
        assert notification.hospital_id == HOSPITAL_ID
        assert notification.kind == "auth.user_invited"
        assert notification.title == "Welcome to Demo Hospital"
        assert "Meera Menon invited you" in notification.body
        assert notification.in_app is True
        assert notification.created_by == inviter.id

        [delivery] = world.repo.deliveries
        assert delivery.status is DeliveryStatus.QUEUED
        assert delivery.to_address == "asha@example.test"
        assert delivery.subject == "You have been invited to Demo Hospital"
        assert "Hello Asha," in (delivery.body or "")
        assert TOKEN in (delivery.body or "")
        assert "72 hours" in (delivery.body or "")

    async def test_the_token_reaches_the_email_but_never_the_in_app_text(self) -> None:
        user = _user()
        world = _World(users=[user])

        await world.service.notify(_invite(user))

        [notification] = world.repo.notifications
        assert TOKEN not in notification.title
        assert TOKEN not in notification.body
        assert TOKEN not in (notification.link or "")
        assert TOKEN in (world.repo.deliveries[0].body or "")

    async def test_it_does_not_commit_the_callers_transaction(self) -> None:
        # The notification must stand or fall with the event that caused it.
        user = _user()
        world = _World(users=[user])

        await world.service.notify(_invite(user))

        assert world.session.commits == 0
        assert world.session.savepoints_opened == 1

    async def test_a_failure_is_swallowed_so_the_caller_is_not_aborted(self) -> None:
        user = _user()
        world = _World(users=[user])
        world.repo.fail_on_create = True

        await world.service.notify(_invite(user))  # must not raise

        assert world.repo.notifications == []
        assert world.session.savepoints_rolled_back == 1

    async def test_an_unknown_kind_is_swallowed_too(self) -> None:
        user = _user()
        world = _World(users=[user])

        await world.service.notify(_invite(user, kind="no.such.kind"))

        assert world.repo.notifications == []

    async def test_a_recipient_in_another_hospital_is_ignored(self) -> None:
        outsider = _user(hospital_id=OTHER_HOSPITAL_ID)
        world = _World(users=[outsider])

        await world.service.notify(_invite(outsider))

        assert world.repo.notifications == []

    async def test_an_unknown_recipient_is_ignored(self) -> None:
        world = _World()
        ghost = _user()

        await world.service.notify(_invite(ghost))

        assert world.repo.notifications == []

    async def test_a_recipient_named_twice_is_notified_once(self) -> None:
        user = _user()
        world = _World(users=[user])

        await world.service.notify(_invite(user, recipient_user_ids=(user.id, user.id)))

        assert len(world.repo.notifications) == 1

    async def test_a_user_with_no_address_gets_the_in_app_copy_only(self) -> None:
        # §14: "recipient without email address -> skip email channel silently".
        user = _user(email=None)
        world = _World(users=[user])

        await world.service.notify(_invite(user))

        assert len(world.repo.notifications) == 1
        assert world.repo.deliveries == []

    async def test_a_missing_template_variable_renders_a_placeholder(self) -> None:
        user = _user()
        world = _World(users=[user])

        await world.service.notify(_invite(user, variables={}))

        assert "[expires_in]" in (world.repo.deliveries[0].body or "")

    async def test_with_no_actor_the_text_names_an_administrator(self) -> None:
        user = _user()
        world = _World(users=[user])

        await world.service.notify(_invite(user))

        assert "An administrator invited you" in world.repo.notifications[0].body


# ── deliver_credential ──────────────────────────────────────────────────────

#: The link a credential email carries. Nothing may log it, or any part of it.
ACTION_URL = f"https://app.test/reset-password#token={TOKEN}"
#: What the failing email queue says. It quotes the body, as a database error does.
QUEUE_ERROR = f"invalid input for query argument $3: 'Set your password: {ACTION_URL}'"


class _SpyLogger:
    """Stands in for the service's logger and keeps every call made to it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __getattr__(self, level: str) -> Any:
        def _log(event: str, *args: Any, **fields: Any) -> None:
            assert not args, "positional log arguments would be interpolated into the message"
            self.calls.append((level, event, fields))

        return _log

    def events(self) -> list[str]:
        return [event for _, event, _ in self.calls]

    def everything(self) -> str:
        return repr(self.calls)


@pytest.fixture
def spy_logger(monkeypatch: pytest.MonkeyPatch) -> _SpyLogger:
    """Everything ``NotificationService`` logs, with the fields it logged."""
    spy = _SpyLogger()
    monkeypatch.setattr(notification_service_module, "logger", spy)
    return spy


def _credential(user: MagicMock, **overrides: Any) -> NotificationRequest:
    """An invitation whose email carries :data:`ACTION_URL`."""
    return _invite(user, secret_variables={"action_url": ACTION_URL}, **overrides)


def _assert_no_secret_logged(spy: _SpyLogger) -> None:
    """Nothing logged gives away the link, the token, the body or the error's text."""
    logged = spy.everything()
    assert TOKEN not in logged, "the token was logged"
    assert "token=" not in logged, "the link was logged"
    assert "reset-password" not in logged, "the link was logged"
    assert "invalid input" not in logged, "the error's own text was logged"
    assert "asha@example.test" not in logged, "the recipient's address was logged"
    for level, event, fields in spy.calls:
        assert level != "exception", f"{event} was logged with a traceback"
        assert "exc_info" not in fields, f"{event} was logged with a traceback"
        assert "secret_variables" not in fields
        assert "action_url" not in fields


class TestDeliverCredential:
    """``deliver_credential``: the caller is told the truth about the email.

    An invitation or reset link may only travel by email. The caller kills
    the link unless this returns ``True``, so ``True`` has to mean exactly
    "an email to the recipient is in the queue", and every other outcome has
    to be ``False`` without raising and without leaving anything behind. The
    fake repository cannot roll back; that the savepoint was rolled back is
    asserted here, and that it really removes the rows is asserted against
    PostgreSQL in ``integration/test_notification_flows.py``.
    """

    async def test_it_returns_true_with_exactly_one_email_queued(
        self, spy_logger: _SpyLogger
    ) -> None:
        user = _user()
        world = _World(users=[user])

        queued = await world.service.deliver_credential(_credential(user))

        assert queued is True
        [delivery] = world.repo.deliveries
        assert delivery.status is DeliveryStatus.QUEUED
        assert delivery.to_address == "asha@example.test"
        assert ACTION_URL in (delivery.body or "")
        [notification] = world.repo.notifications
        assert delivery.notification_id == notification.id
        assert TOKEN not in notification.title + notification.body + (notification.link or "")
        # In the caller's transaction: a savepoint, kept, and no commit.
        assert world.session.commits == 0
        assert world.session.savepoints_opened == 1
        assert world.session.savepoints_rolled_back == 0
        assert "notification.credential_not_queued" not in spy_logger.events()
        _assert_no_secret_logged(spy_logger)

    @pytest.mark.parametrize("recipient", ["unknown", "other_hospital", "nobody_named"])
    async def test_with_no_recipient_it_returns_false_and_writes_nothing(
        self, spy_logger: _SpyLogger, recipient: str
    ) -> None:
        """Attack: a link is minted for an account the email cannot be addressed to."""
        outsider = _user(hospital_id=OTHER_HOSPITAL_ID)
        world = _World(users=[outsider] if recipient == "other_hospital" else [])
        target = outsider if recipient == "other_hospital" else _user()
        overrides: dict[str, Any] = (
            {"recipient_user_ids": ()} if recipient == "nobody_named" else {}
        )

        queued = await world.service.deliver_credential(_credential(target, **overrides))

        assert queued is False
        assert world.repo.deliveries == []
        assert world.repo.notifications == []
        assert world.session.savepoints_rolled_back == 1
        assert world.session.commits == 0
        self._assert_logged_not_queued(spy_logger, error_type="_NoEmailQueuedError")

    @pytest.mark.parametrize("address", [None, ""])
    async def test_with_no_address_it_returns_false_and_the_in_app_notice_is_undone(
        self, spy_logger: _SpyLogger, address: str | None
    ) -> None:
        """Attack: "you have been invited" appears in-app with no email behind it.

        ``notify`` keeps the in-app copy for a user with no address. For a
        credential that copy would be a promise nobody keeps, so the savepoint
        holding it is rolled back along with everything else.
        """
        user = _user(email=address)
        world = _World(users=[user])

        queued = await world.service.deliver_credential(_credential(user))

        assert queued is False
        assert world.repo.deliveries == []
        # The notice was written inside the savepoint, and the savepoint was undone.
        assert world.session.savepoints_opened == 1
        assert world.session.savepoints_rolled_back == 1
        assert world.session.commits == 0
        self._assert_logged_not_queued(spy_logger, error_type="_NoEmailQueuedError")

    @pytest.mark.parametrize(
        "error",
        [RuntimeError(QUEUE_ERROR), ValueError(QUEUE_ERROR), KeyError(ACTION_URL)],
        ids=lambda error: type(error).__name__,
    )
    async def test_a_failing_insert_returns_false_and_never_raises(
        self, spy_logger: _SpyLogger, error: Exception
    ) -> None:
        """Attack: break the email queue and read the link out of the error."""
        user = _user()
        world = _World(users=[user])
        world.repo.email_insert_error = error

        queued = await world.service.deliver_credential(_credential(user))  # must not raise

        assert queued is False
        assert world.repo.deliveries == []
        assert world.session.savepoints_rolled_back == 1
        assert world.session.commits == 0
        self._assert_logged_not_queued(spy_logger, error_type=type(error).__name__)

    async def test_a_failing_notification_insert_returns_false(
        self, spy_logger: _SpyLogger
    ) -> None:
        user = _user()
        world = _World(users=[user])
        world.repo.fail_on_create = True

        queued = await world.service.deliver_credential(_credential(user))

        assert queued is False
        assert world.repo.notifications == []
        assert world.repo.deliveries == []
        assert world.session.savepoints_rolled_back == 1
        self._assert_logged_not_queued(spy_logger, error_type="RuntimeError")

    async def test_an_unknown_kind_returns_false(self, spy_logger: _SpyLogger) -> None:
        user = _user()
        world = _World(users=[user])

        queued = await world.service.deliver_credential(_credential(user, kind="no.such.kind"))

        assert queued is False
        assert world.repo.deliveries == []
        assert spy_logger.events() == ["notification.credential_not_queued"]
        _assert_no_secret_logged(spy_logger)

    async def test_an_earlier_success_is_not_mistaken_for_this_one(
        self, spy_logger: _SpyLogger
    ) -> None:
        """Attack: one queued email makes every later failure look queued.

        The same service instance handles several requests in one unit of
        work. "Queued" is decided per call, so a success before a failure, or
        a failure before a success, must not leak into the other's answer.
        """
        user = _user()
        unreachable = _user(email=None)
        world = _World(users=[user, unreachable])

        answers = [
            await world.service.deliver_credential(_credential(user)),
            await world.service.deliver_credential(_credential(unreachable)),
            await world.service.deliver_credential(_credential(user)),
            await world.service.deliver_credential(_credential(_user())),
        ]
        world.repo.email_insert_error = RuntimeError(QUEUE_ERROR)
        answers.append(await world.service.deliver_credential(_credential(user)))
        world.repo.email_insert_error = None
        answers.append(await world.service.deliver_credential(_credential(user)))

        assert answers == [True, False, True, False, False, True]
        assert len(world.repo.deliveries) == 3
        assert {delivery.to_address for delivery in world.repo.deliveries} == {"asha@example.test"}
        _assert_no_secret_logged(spy_logger)

    async def test_an_ordinary_notification_does_not_count_as_a_queued_credential(
        self, spy_logger: _SpyLogger
    ) -> None:
        """An email queued by ``notify`` just before must not answer for the credential."""
        user = _user()
        unreachable = _user(email=None)
        world = _World(users=[user, unreachable])

        await world.service.notify(_invite(user))
        queued = await world.service.deliver_credential(_credential(unreachable))

        assert queued is False
        assert [delivery.to_address for delivery in world.repo.deliveries] == ["asha@example.test"]

    async def test_the_null_notifier_queues_nothing_and_says_so(self) -> None:
        """A service wired with no notifier must not report a credential as sent."""
        assert isinstance(NullNotifier(), Notifier)
        assert await NullNotifier().deliver_credential(_credential(_user())) is False

    @staticmethod
    def _assert_logged_not_queued(spy: _SpyLogger, *, error_type: str) -> None:
        """The one event, at error level, with the kind of failure and nothing more."""
        failures = [call for call in spy.calls if call[1] == "notification.credential_not_queued"]
        assert failures == [
            (
                "error",
                "notification.credential_not_queued",
                {
                    "kind": "auth.user_invited",
                    "hospital_id": str(HOSPITAL_ID),
                    "error_type": error_type,
                },
            )
        ]
        # The event ``notify`` uses is not: that path can log a traceback.
        assert "notification.create_failed" not in spy.events()
        _assert_no_secret_logged(spy)


class TestNotifyByPermission:
    def _request(self, actor_id: uuid.UUID | None = None) -> NotificationRequest:
        return NotificationRequest(
            kind="billing.discount_approval_requested",
            hospital_id=HOSPITAL_ID,
            recipient_permission="invoice.approve_discount",
            variables={"patient_name": "Ananya Rao", "discount_amount": "INR 200.00"},
            link="/billing",
            actor_id=actor_id,
        )

    async def test_every_holder_of_the_permission_is_notified(self) -> None:
        approvers = [_user(first_name="Admin One"), _user(first_name="Admin Two")]
        requester = _user(first_name="Billing")
        world = _World(users=approvers, actor=requester)

        await world.service.notify(self._request(requester.id))

        world.users.list_active_recipients.assert_awaited_once_with(
            HOSPITAL_ID, permission_code="invoice.approve_discount"
        )
        assert {n.recipient_user_id for n in world.repo.notifications} == {a.id for a in approvers}
        notification = world.repo.notifications[0]
        assert "Billing Menon applied a discount of INR 200.00" in notification.body
        assert "Ananya Rao" in notification.body
        assert notification.link == "/billing"
        # In-app only by default: no email unless the approver asks for it.
        assert world.repo.deliveries == []

    async def test_the_actor_is_not_told_about_their_own_action(self) -> None:
        admin = _user(first_name="Admin")
        other = _user(first_name="Other")
        world = _World(users=[admin, other])

        await world.service.notify(self._request(admin.id))

        assert [n.recipient_user_id for n in world.repo.notifications] == [other.id]

    async def test_nobody_holding_the_permission_creates_nothing(self) -> None:
        world = _World(users=[])

        await world.service.notify(self._request())

        assert world.repo.notifications == []


class TestPreferencesAppliedOnNotify:
    def _request(self, user: MagicMock) -> NotificationRequest:
        return NotificationRequest(
            kind="system.broadcast",
            hospital_id=HOSPITAL_ID,
            recipient_user_ids=(user.id,),
            variables={"title": "Maintenance", "body": "Tonight at 23:00."},
        )

    async def test_ac3_a_user_who_opted_into_email_gets_one(self) -> None:
        user = _user()
        world = _World(users=[user])
        world.repo.preferences[user.id] = {"system.broadcast": {"email": True}}

        await world.service.notify(self._request(user))

        assert world.repo.notifications[0].in_app is True
        [delivery] = world.repo.deliveries
        assert delivery.subject == "Maintenance"
        assert "Tonight at 23:00." in (delivery.body or "")

    async def test_ac3_a_user_who_switched_the_kind_off_gets_nothing(self) -> None:
        user = _user()
        world = _World(users=[user])
        world.repo.preferences[user.id] = {"system.broadcast": {"in_app": False}}

        await world.service.notify(self._request(user))

        assert world.repo.notifications == []
        assert world.repo.deliveries == []

    async def test_email_only_is_kept_out_of_the_notification_centre(self) -> None:
        user = _user()
        world = _World(users=[user])
        world.repo.preferences[user.id] = {"system.broadcast": {"in_app": False, "email": True}}

        await world.service.notify(self._request(user))

        # The row exists as the email's parent, but is not shown in the centre.
        assert world.repo.notifications[0].in_app is False
        assert len(world.repo.deliveries) == 1
        assert (await world.service.unread_count(HOSPITAL_ID, user.id)).unread == 0

    async def test_ac4_a_critical_kind_ignores_an_opt_out(self) -> None:
        user = _user()
        world = _World(users=[user])
        world.repo.preferences[user.id] = {"auth.user_invited": {"in_app": False, "email": False}}

        await world.service.notify(_invite(user))

        assert world.repo.notifications[0].in_app is True
        assert len(world.repo.deliveries) == 1


# ── Notification centre ─────────────────────────────────────────────────────


async def _seed_three(world: _World, user: MagicMock) -> None:
    for title in ("First", "Second", "Third"):
        await world.service.notify(
            NotificationRequest(
                kind="system.broadcast",
                hospital_id=HOSPITAL_ID,
                recipient_user_ids=(user.id,),
                variables={"title": title, "body": "Body"},
            )
        )


class TestNotificationCentre:
    async def test_list_is_newest_first_and_paginated(self) -> None:
        user = _user()
        world = _World(users=[user])
        await _seed_three(world, user)

        page = await world.service.list_mine(
            HOSPITAL_ID, user.id, pagination=PaginationParams(page=1, page_size=2)
        )

        assert [item.title for item in page.items] == ["Third", "Second"]
        assert page.total_records == 3
        assert page.items[0].is_read is False

    async def test_unread_count_and_unread_only(self) -> None:
        user = _user()
        world = _World(users=[user])
        await _seed_three(world, user)
        first = world.repo.notifications[0]

        await world.service.mark_read(HOSPITAL_ID, user.id, first.id)

        assert (await world.service.unread_count(HOSPITAL_ID, user.id)).unread == 2
        unread = await world.service.list_mine(HOSPITAL_ID, user.id, unread_only=True)
        assert [item.title for item in unread.items] == ["Third", "Second"]

    async def test_mark_read_commits_and_audits(self) -> None:
        user = _user()
        world = _World(users=[user])
        await _seed_three(world, user)
        target = world.repo.notifications[0]

        result = await world.service.mark_read(HOSPITAL_ID, user.id, target.id)

        assert result.is_read is True
        assert result.read_at is not None
        assert world.session.commits == 1
        assert world.audit.actions() == ["notification.read"]

    async def test_marking_a_read_notification_again_changes_nothing(self) -> None:
        user = _user()
        world = _World(users=[user])
        await _seed_three(world, user)
        target = world.repo.notifications[0]
        first = await world.service.mark_read(HOSPITAL_ID, user.id, target.id)

        again = await world.service.mark_read(HOSPITAL_ID, user.id, target.id)

        assert again.read_at == first.read_at
        assert world.session.commits == 1
        assert world.audit.actions() == ["notification.read"]

    async def test_another_users_notification_is_a_404(self) -> None:
        owner, intruder = _user(), _user()
        world = _World(users=[owner, intruder])
        await _seed_three(world, owner)
        target = world.repo.notifications[0]

        with pytest.raises(NotificationNotFoundError) as excinfo:
            await world.service.mark_read(HOSPITAL_ID, intruder.id, target.id)

        assert excinfo.value.status_code == 404
        assert target.read_at is None

    async def test_mark_all_read(self) -> None:
        user = _user()
        world = _World(users=[user])
        await _seed_three(world, user)

        result = await world.service.mark_all_read(HOSPITAL_ID, user.id)

        assert result.marked == 3
        assert (await world.service.unread_count(HOSPITAL_ID, user.id)).unread == 0
        assert world.session.commits == 1
        assert world.audit.last().context == {"marked": 3}

    async def test_mark_all_read_with_nothing_unread_writes_nothing(self) -> None:
        user = _user()
        world = _World(users=[user])

        result = await world.service.mark_all_read(HOSPITAL_ID, user.id)

        assert result.marked == 0
        assert world.session.commits == 0
        assert world.audit.events == []


# ── Preferences ─────────────────────────────────────────────────────────────


class TestPreferences:
    async def test_defaults_list_every_kind_with_what_is_in_effect(self) -> None:
        user = _user()
        world = _World(users=[user])

        result = await world.service.get_preferences(user.id)

        by_kind = {kind.kind: kind for kind in result.kinds}
        invite = by_kind["auth.user_invited"]
        assert (invite.in_app, invite.email, invite.critical) == (True, True, True)
        assert invite.locked_channels == ["in_app", "email"]
        broadcast = by_kind["system.broadcast"]
        assert (broadcast.in_app, broadcast.email, broadcast.critical) == (True, False, False)
        assert broadcast.locked_channels == []
        assert broadcast.email_available is True

    async def test_update_stores_and_returns_the_effective_channels(self) -> None:
        user = _user()
        world = _World(users=[user])

        result = await world.service.update_preferences(
            HOSPITAL_ID,
            user.id,
            UpdatePreferencesRequest.model_validate(
                {"preferences": {"system.broadcast": {"email": True}}}
            ),
        )

        broadcast = next(k for k in result.kinds if k.kind == "system.broadcast")
        assert (broadcast.in_app, broadcast.email) == (True, True)
        assert world.repo.preferences[user.id] == {"system.broadcast": {"email": True}}
        assert world.session.commits == 1
        assert world.audit.actions() == ["notification.preferences_updated"]

    async def test_update_merges_with_what_was_stored(self) -> None:
        user = _user()
        world = _World(users=[user])
        world.repo.preferences[user.id] = {
            "system.broadcast": {"email": True},
            "billing.discount_approval_requested": {"email": True},
        }

        await world.service.update_preferences(
            HOSPITAL_ID,
            user.id,
            UpdatePreferencesRequest.model_validate(
                {"preferences": {"system.broadcast": {"in_app": False}}}
            ),
        )

        assert world.repo.preferences[user.id] == {
            # The new channel choice is merged into the kind's existing entry...
            "system.broadcast": {"email": True, "in_app": False},
            # ...and a kind not mentioned is left as it was.
            "billing.discount_approval_requested": {"email": True},
        }

    async def test_ac4_an_opt_out_of_a_critical_kind_is_stored_but_has_no_effect(self) -> None:
        user = _user()
        world = _World(users=[user])

        result = await world.service.update_preferences(
            HOSPITAL_ID,
            user.id,
            UpdatePreferencesRequest.model_validate(
                {"preferences": {"auth.password_reset_requested": {"email": False}}}
            ),
        )

        reset = next(k for k in result.kinds if k.kind == "auth.password_reset_requested")
        assert reset.email is True

    async def test_an_unknown_kind_is_a_422_and_stores_nothing(self) -> None:
        user = _user()
        world = _World(users=[user])

        with pytest.raises(ValidationError) as excinfo:
            await world.service.update_preferences(
                HOSPITAL_ID,
                user.id,
                UpdatePreferencesRequest.model_validate(
                    {"preferences": {"no.such.kind": {"email": True}}}
                ),
            )

        assert excinfo.value.detail["errors"][0]["field"] == "preferences.no.such.kind"
        assert world.repo.preferences == {}
        assert world.session.commits == 0


# ── Broadcast ───────────────────────────────────────────────────────────────


class TestBroadcast:
    async def test_every_recipient_gets_their_own_notification(self) -> None:
        staff = [_user(first_name="One"), _user(first_name="Two"), _user(first_name="Three")]
        admin = staff[0]
        world = _World(users=staff)

        result = await world.service.broadcast(
            HOSPITAL_ID,
            BroadcastRequest(title="Fire drill", body="At 15:00 today.", link="/dashboard"),
            actor_id=admin.id,
        )

        assert result.recipients == 3
        # The sender receives it too: a broadcast is to the hospital, them included.
        assert {n.recipient_user_id for n in world.repo.notifications} == {u.id for u in staff}
        assert {n.title for n in world.repo.notifications} == {"Fire drill"}
        assert {n.link for n in world.repo.notifications} == {"/dashboard"}
        assert world.session.commits == 1
        assert world.audit.actions() == ["notification.broadcast"]
        assert world.audit.last().context == {"recipients": 3, "role_id": None}

    async def test_a_role_narrows_the_recipients(self) -> None:
        nurse = _user()
        world = _World(users=[nurse])
        role_id = uuid.uuid4()

        result = await world.service.broadcast(
            HOSPITAL_ID,
            BroadcastRequest(title="Ward meeting", body="Now.", role_id=role_id),
            actor_id=nurse.id,
        )

        world.users.list_active_recipients.assert_awaited_once_with(HOSPITAL_ID, role_id=role_id)
        assert result.recipients == 1

    async def test_a_broadcast_failure_is_raised_not_swallowed(self) -> None:
        # Unlike notify(): here the broadcast *is* the request.
        user = _user()
        world = _World(users=[user])
        world.repo.fail_on_create = True

        with pytest.raises(RuntimeError):
            await world.service.broadcast(
                HOSPITAL_ID, BroadcastRequest(title="x", body="y"), actor_id=user.id
            )

        assert world.session.commits == 0

    async def test_template_syntax_in_a_broadcast_is_inert(self) -> None:
        user = _user()
        world = _World(users=[user])

        await world.service.broadcast(
            HOSPITAL_ID,
            BroadcastRequest(title="${hospital_name}", body="Hello ${first_name}"),
            actor_id=user.id,
        )

        [notification] = world.repo.notifications
        assert notification.title == "${hospital_name}"
        assert notification.body == "Hello ${first_name}"


# ── Email queue ─────────────────────────────────────────────────────────────


class _RecordingSender:
    """An :class:`EmailSender` that records what it was asked to send."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[Any] = []
        self.fail = fail

    async def send(self, message: Any) -> None:
        if self.fail:
            msg = "smtp.example.test refused asha@example.test"
            raise ConnectionRefusedError(msg)
        self.sent.append(message)


async def _queued_invite() -> tuple[_World, NotificationDelivery]:
    user = _user()
    world = _World(users=[user])
    await world.service.notify(_invite(user))
    delivery = world.repo.deliveries[0]
    # notify() stamps the real clock. Pin the due time to the tests' fixed
    # clock, or every assertion below depends on the day the suite is run.
    delivery.next_attempt_at = NOW
    return world, delivery


class TestDeliverDueEmails:
    async def test_a_due_email_is_sent_and_its_body_cleared(self) -> None:
        world, delivery = await _queued_invite()
        sender = _RecordingSender()

        report = await world.service.deliver_due_emails(sender, now=NOW + timedelta(seconds=1))

        assert (report.sent, report.retrying, report.failed) == (1, 0, 0)
        [message] = sender.sent
        assert message.to == "asha@example.test"
        assert TOKEN in message.body
        assert delivery.status is DeliveryStatus.SENT
        assert delivery.attempts == 1
        assert delivery.sent_at is not None
        assert delivery.next_attempt_at is None
        # The token does not outlive the email.
        assert delivery.body is None
        assert world.repo.notifications[0].sent_email is True
        assert world.session.commits == 1

    async def test_rule_3_a_failure_is_retried_with_exponential_backoff(self) -> None:
        world, delivery = await _queued_invite()
        sender = _RecordingSender(fail=True)
        now = NOW + timedelta(seconds=1)
        delays = []

        for _ in range(MAX_EMAIL_ATTEMPTS - 1):
            report = await world.service.deliver_due_emails(sender, now=now)
            assert (report.sent, report.retrying, report.failed) == (0, 1, 0)
            assert delivery.status is DeliveryStatus.QUEUED
            assert delivery.next_attempt_at is not None
            delays.append((delivery.next_attempt_at - now).total_seconds())
            now = delivery.next_attempt_at

        base = EMAIL_BACKOFF_BASE_SECONDS
        assert delays == [base, base * 2, base * 4, base * 8]
        assert delivery.attempts == MAX_EMAIL_ATTEMPTS - 1
        assert "ConnectionRefusedError" in (delivery.last_error or "")
        # Still queued, so the body is still needed.
        assert delivery.body is not None

    async def test_rule_3_the_fifth_failure_is_final(self) -> None:
        world, delivery = await _queued_invite()
        sender = _RecordingSender(fail=True)
        now = NOW + timedelta(seconds=1)

        for _ in range(MAX_EMAIL_ATTEMPTS):
            report = await world.service.deliver_due_emails(sender, now=now)
            now = delivery.next_attempt_at or now

        assert report.failed == 1
        assert delivery.status is DeliveryStatus.FAILED
        assert delivery.attempts == MAX_EMAIL_ATTEMPTS
        assert delivery.next_attempt_at is None
        assert delivery.last_error
        assert delivery.body is None
        assert world.repo.notifications[0].sent_email is False

        # And it is never picked up again.
        later = await world.service.deliver_due_emails(sender, now=now + timedelta(days=1))
        assert later.processed == 0

    async def test_a_retry_that_succeeds_ends_as_sent(self) -> None:
        world, delivery = await _queued_invite()
        now = NOW + timedelta(seconds=1)
        await world.service.deliver_due_emails(_RecordingSender(fail=True), now=now)
        assert delivery.next_attempt_at is not None

        report = await world.service.deliver_due_emails(
            _RecordingSender(), now=delivery.next_attempt_at
        )

        assert report.sent == 1
        assert delivery.status is DeliveryStatus.SENT
        assert delivery.attempts == 2
        assert delivery.last_error is None

    async def test_a_delivery_that_is_not_due_yet_is_left_alone(self) -> None:
        world, delivery = await _queued_invite()
        sender = _RecordingSender(fail=True)
        now = NOW + timedelta(seconds=1)
        await world.service.deliver_due_emails(sender, now=now)

        report = await world.service.deliver_due_emails(_RecordingSender(), now=now)

        assert report.processed == 0
        assert delivery.attempts == 1

    async def test_with_no_transport_the_email_fails_with_a_reason(self) -> None:
        # §7: "zero silent drops". No SMTP host means nothing can ever be
        # sent, so it fails at once instead of retrying five times.
        world, delivery = await _queued_invite()

        report = await world.service.deliver_due_emails(None, now=NOW + timedelta(seconds=1))

        assert (report.sent, report.retrying, report.failed) == (0, 0, 1)
        assert delivery.status is DeliveryStatus.FAILED
        assert "SMTP_HOST" in (delivery.last_error or "")
        assert delivery.body is None
        assert delivery.next_attempt_at is None

    async def test_an_empty_queue_is_a_no_op(self) -> None:
        world = _World()

        report = await world.service.deliver_due_emails(_RecordingSender(), now=NOW)

        assert report.processed == 0
