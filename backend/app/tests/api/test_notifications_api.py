"""API tests for the notification endpoints.

Real app, real service, real repository, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.dependencies.db import get_db_session
from app.api.dependencies.services import get_audit_sink
from app.main import create_app
from app.models.notification import Notification
from app.repositories.notification_repository import NotificationRepository
from app.services.notification_catalog import KINDS
from app.tests.billing_helpers import auth_headers, insert_user_with_permissions
from app.tests.conftest import RecordingAuditSink

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User

pytestmark = pytest.mark.database

URL = "/api/v1/notifications"
READ = "notification.read.own"
PREFERENCES = "notification.preference.update.own"
BROADCAST = "notification.broadcast"


@pytest.fixture
def audit() -> RecordingAuditSink:
    """The sink the app records to for the duration of a test."""
    return RecordingAuditSink()


@pytest_asyncio.fixture
async def api(db_session: AsyncSession, audit: RecordingAuditSink) -> AsyncGenerator[AsyncClient]:
    """An HTTP client sharing the test's rolled-back session and audit sink."""
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override
    application.dependency_overrides[get_audit_sink] = lambda: audit
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def me(db_session: AsyncSession, hospital_id: uuid.UUID) -> User:
    """An ordinary member of staff: may read and tune their own notifications."""
    return await insert_user_with_permissions(db_session, hospital_id, [READ, PREFERENCES])


@pytest.fixture
def headers(me: User, hospital_id: uuid.UUID) -> dict[str, str]:
    """Bearer header for :func:`me`."""
    return auth_headers(me.id, hospital_id)


@pytest_asyncio.fixture
async def nobody(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """An authenticated user holding no notification permission."""
    user = await insert_user_with_permissions(db_session, hospital_id, ["patient.read"])
    return auth_headers(user.id, hospital_id)


async def _notify(session: AsyncSession, user: User, title: str = "Hello") -> Notification:
    """Put a notification in a user's centre."""
    assert user.hospital_id is not None
    return await NotificationRepository(session).create_notification(
        hospital_id=user.hospital_id,
        recipient_user_id=user.id,
        kind="system.broadcast",
        title=title,
        body="Body",
        link="/dashboard",
    )


# Every endpoint, with the body it needs, for the 401/403 matrix.
ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", URL, None),
    ("GET", f"{URL}/unread-count", None),
    ("POST", f"{URL}/read-all", None),
    ("POST", f"{URL}/{uuid.uuid4()}/read", None),
    ("GET", f"{URL}/preferences", None),
    ("PUT", f"{URL}/preferences", {"preferences": {}}),
    ("POST", f"{URL}/broadcast", {"title": "t", "body": "b"}),
]


class TestAuthorization:
    @pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS)
    async def test_no_token_is_401(
        self, api: AsyncClient, method: str, url: str, body: dict[str, Any] | None
    ) -> None:
        response = await api.request(method, url, json=body)

        assert response.status_code == 401

    @pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS)
    async def test_no_permission_is_403(
        self,
        api: AsyncClient,
        nobody: dict[str, str],
        method: str,
        url: str,
        body: dict[str, Any] | None,
    ) -> None:
        response = await api.request(method, url, json=body, headers=nobody)

        assert response.status_code == 403

    async def test_reading_does_not_grant_updating_preferences(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        reader = await insert_user_with_permissions(db_session, hospital_id, [READ])

        response = await api.put(
            f"{URL}/preferences",
            json={"preferences": {}},
            headers=auth_headers(reader.id, hospital_id),
        )

        assert response.status_code == 403

    async def test_ordinary_staff_cannot_broadcast(
        self, api: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await api.post(
            f"{URL}/broadcast", json={"title": "t", "body": "b"}, headers=headers
        )

        assert response.status_code == 403


class TestList:
    async def test_returns_my_notifications_newest_first(
        self, api: AsyncClient, db_session: AsyncSession, me: User, headers: dict[str, str]
    ) -> None:
        await _notify(db_session, me, "Only one")

        response = await api.get(URL, headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        [item] = body["data"]
        assert item["title"] == "Only one"
        assert item["kind"] == "system.broadcast"
        assert item["link"] == "/dashboard"
        assert item["is_read"] is False
        assert item["read_at"] is None
        assert item["created_at"]
        assert body["metadata"]["pagination"]["total_records"] == 1

    async def test_never_shows_a_colleagues_or_another_hospitals(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        me: User,
        headers: dict[str, str],
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        colleague = await insert_user_with_permissions(db_session, hospital_id, [READ])
        outsider = await insert_user_with_permissions(db_session, other_hospital_id, [READ])
        await _notify(db_session, colleague, "Colleague's")
        await _notify(db_session, outsider, "Outsider's")

        mine = await api.get(URL, headers=headers)
        theirs = await api.get(URL, headers=auth_headers(outsider.id, other_hospital_id))

        assert mine.json()["data"] == []
        assert [n["title"] for n in theirs.json()["data"]] == ["Outsider's"]

    async def test_unread_only_and_paging(
        self, api: AsyncClient, db_session: AsyncSession, me: User, headers: dict[str, str]
    ) -> None:
        first = await _notify(db_session, me, "First")
        await _notify(db_session, me, "Second")
        await api.post(f"{URL}/{first.id}/read", headers=headers)

        unread = await api.get(URL, params={"unread_only": "true"}, headers=headers)
        paged = await api.get(URL, params={"page_size": 1}, headers=headers)

        assert [n["title"] for n in unread.json()["data"]] == ["Second"]
        assert len(paged.json()["data"]) == 1
        assert paged.json()["metadata"]["pagination"]["total_pages"] == 2

    async def test_a_page_size_over_100_is_422(
        self, api: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await api.get(URL, params={"page_size": 101}, headers=headers)

        assert response.status_code == 422


class TestUnreadCount:
    async def test_counts_only_my_unread(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        me: User,
        headers: dict[str, str],
        hospital_id: uuid.UUID,
    ) -> None:
        colleague = await insert_user_with_permissions(db_session, hospital_id, [READ])
        read = await _notify(db_session, me)
        await _notify(db_session, me)
        await _notify(db_session, colleague)
        await api.post(f"{URL}/{read.id}/read", headers=headers)

        response = await api.get(f"{URL}/unread-count", headers=headers)

        assert response.status_code == 200
        assert response.json()["data"] == {"unread": 1}


class TestMarkRead:
    async def test_marks_it_read_and_audits(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        me: User,
        headers: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        notification = await _notify(db_session, me)

        response = await api.post(f"{URL}/{notification.id}/read", headers=headers)

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["is_read"] is True
        assert data["read_at"] is not None
        assert audit.actions() == ["notification.read"]
        assert audit.last().actor_id == me.id
        assert audit.last().target_id == notification.id

    async def test_marking_twice_is_not_an_error(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        me: User,
        headers: dict[str, str],
        audit: RecordingAuditSink,
    ) -> None:
        notification = await _notify(db_session, me)
        first = await api.post(f"{URL}/{notification.id}/read", headers=headers)

        second = await api.post(f"{URL}/{notification.id}/read", headers=headers)

        assert second.status_code == 200
        assert second.json()["data"]["read_at"] == first.json()["data"]["read_at"]
        assert audit.actions() == ["notification.read"]

    async def test_a_colleagues_notification_is_404_and_stays_unread(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        headers: dict[str, str],
        hospital_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        colleague = await insert_user_with_permissions(db_session, hospital_id, [READ])
        theirs = await _notify(db_session, colleague)

        response = await api.post(f"{URL}/{theirs.id}/read", headers=headers)

        assert response.status_code == 404
        await db_session.refresh(theirs)
        assert theirs.read_at is None
        assert audit.events == []

    async def test_another_hospitals_notification_is_404(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        headers: dict[str, str],
        other_hospital_id: uuid.UUID,
    ) -> None:
        outsider = await insert_user_with_permissions(db_session, other_hospital_id, [READ])
        theirs = await _notify(db_session, outsider)

        response = await api.post(f"{URL}/{theirs.id}/read", headers=headers)

        assert response.status_code == 404

    async def test_an_unknown_id_is_404_and_a_malformed_one_422(
        self, api: AsyncClient, headers: dict[str, str]
    ) -> None:
        unknown = await api.post(f"{URL}/{uuid.uuid4()}/read", headers=headers)
        malformed = await api.post(f"{URL}/not-a-uuid/read", headers=headers)

        assert unknown.status_code == 404
        assert malformed.status_code == 422


class TestMarkAllRead:
    async def test_marks_mine_and_leaves_a_colleagues(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        me: User,
        headers: dict[str, str],
        hospital_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        colleague = await insert_user_with_permissions(db_session, hospital_id, [READ])
        await _notify(db_session, me)
        await _notify(db_session, me)
        theirs = await _notify(db_session, colleague)

        response = await api.post(f"{URL}/read-all", headers=headers)

        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"marked": 2}
        count = await api.get(f"{URL}/unread-count", headers=headers)
        assert count.json()["data"] == {"unread": 0}
        await db_session.refresh(theirs)
        assert theirs.read_at is None
        assert audit.actions() == ["notification.read_all"]

    async def test_with_nothing_unread_it_marks_zero_and_audits_nothing(
        self, api: AsyncClient, headers: dict[str, str], audit: RecordingAuditSink
    ) -> None:
        response = await api.post(f"{URL}/read-all", headers=headers)

        assert response.json()["data"] == {"marked": 0}
        assert audit.events == []


class TestPreferences:
    async def test_get_lists_every_kind_with_its_defaults(
        self, api: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await api.get(f"{URL}/preferences", headers=headers)

        assert response.status_code == 200, response.text
        kinds = {k["kind"]: k for k in response.json()["data"]["kinds"]}
        # Every kind in the catalog, whichever module added it.
        assert set(kinds) == set(KINDS)
        assert kinds["auth.user_invited"]["critical"] is True
        assert kinds["auth.user_invited"]["locked_channels"] == ["in_app", "email"]
        discount = kinds["billing.discount_approval_requested"]
        assert (discount["in_app"], discount["email"]) == (True, False)
        assert discount["label"]
        assert discount["category"]

    async def test_put_changes_what_is_in_effect_and_audits(
        self, api: AsyncClient, me: User, headers: dict[str, str], audit: RecordingAuditSink
    ) -> None:
        response = await api.put(
            f"{URL}/preferences",
            json={"preferences": {"billing.discount_approval_requested": {"email": True}}},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        kinds = {k["kind"]: k for k in response.json()["data"]["kinds"]}
        assert kinds["billing.discount_approval_requested"]["email"] is True
        again = await api.get(f"{URL}/preferences", headers=headers)
        kinds = {k["kind"]: k for k in again.json()["data"]["kinds"]}
        assert kinds["billing.discount_approval_requested"]["email"] is True
        assert audit.actions() == ["notification.preferences_updated"]
        assert audit.last().actor_id == me.id

    async def test_my_preferences_do_not_change_a_colleagues(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        headers: dict[str, str],
        hospital_id: uuid.UUID,
    ) -> None:
        colleague = await insert_user_with_permissions(db_session, hospital_id, [READ])
        await api.put(
            f"{URL}/preferences",
            json={"preferences": {"system.broadcast": {"in_app": False}}},
            headers=headers,
        )

        theirs = await api.get(
            f"{URL}/preferences", headers=auth_headers(colleague.id, hospital_id)
        )

        kinds = {k["kind"]: k for k in theirs.json()["data"]["kinds"]}
        assert kinds["system.broadcast"]["in_app"] is True

    async def test_ac4_a_critical_kind_cannot_be_switched_off(
        self, api: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await api.put(
            f"{URL}/preferences",
            json={"preferences": {"auth.password_reset_requested": {"email": False}}},
            headers=headers,
        )

        assert response.status_code == 200
        kinds = {k["kind"]: k for k in response.json()["data"]["kinds"]}
        assert kinds["auth.password_reset_requested"]["email"] is True

    async def test_an_unknown_kind_is_422(self, api: AsyncClient, headers: dict[str, str]) -> None:
        response = await api.put(
            f"{URL}/preferences",
            json={"preferences": {"no.such.kind": {"email": True}}},
            headers=headers,
        )

        assert response.status_code == 422
        assert response.json()["errors"]["errors"][0]["field"] == "preferences.no.such.kind"

    async def test_sms_is_not_an_accepted_channel(
        self, api: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await api.put(
            f"{URL}/preferences",
            json={"preferences": {"system.broadcast": {"sms": True}}},
            headers=headers,
        )

        assert response.status_code == 422


class TestBroadcast:
    @pytest_asyncio.fixture
    async def admin(self, db_session: AsyncSession, hospital_id: uuid.UUID) -> User:
        """A user who may broadcast."""
        return await insert_user_with_permissions(
            db_session, hospital_id, [READ, PREFERENCES, BROADCAST]
        )

    async def test_reaches_the_whole_hospital_and_only_it(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: User,
        me: User,
        headers: dict[str, str],
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        audit: RecordingAuditSink,
    ) -> None:
        outsider = await insert_user_with_permissions(db_session, other_hospital_id, [READ])

        response = await api.post(
            f"{URL}/broadcast",
            json={"title": "Fire drill", "body": "At 15:00 today.", "link": "/dashboard"},
            headers=auth_headers(admin.id, hospital_id),
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"recipients": 2}
        mine = await api.get(URL, headers=headers)
        assert [n["title"] for n in mine.json()["data"]] == ["Fire drill"]
        theirs = await api.get(URL, headers=auth_headers(outsider.id, other_hospital_id))
        assert theirs.json()["data"] == []
        assert audit.actions() == ["notification.broadcast"]
        assert audit.last().actor_id == admin.id
        assert me.id != admin.id

    async def test_role_id_narrows_it_to_that_role(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: User,
        me: User,
        headers: dict[str, str],
        hospital_id: uuid.UUID,
    ) -> None:
        from app.models.user import UserRole

        my_role = (
            await db_session.execute(select(UserRole.role_id).where(UserRole.user_id == me.id))
        ).scalar_one()
        admin_headers = auth_headers(admin.id, hospital_id)

        response = await api.post(
            f"{URL}/broadcast",
            json={"title": "Ward meeting", "body": "Now.", "role_id": str(my_role)},
            headers=admin_headers,
        )

        assert response.json()["data"] == {"recipients": 1}
        assert len((await api.get(URL, headers=headers)).json()["data"]) == 1
        assert (await api.get(URL, headers=admin_headers)).json()["data"] == []

    async def test_another_hospitals_role_reaches_nobody(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: User,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        from app.models.user import UserRole

        outsider = await insert_user_with_permissions(db_session, other_hospital_id, [READ])
        their_role = (
            await db_session.execute(
                select(UserRole.role_id).where(UserRole.user_id == outsider.id)
            )
        ).scalar_one()

        response = await api.post(
            f"{URL}/broadcast",
            json={"title": "Hello", "body": "World", "role_id": str(their_role)},
            headers=auth_headers(admin.id, hospital_id),
        )

        assert response.json()["data"] == {"recipients": 0}
        count = await db_session.execute(
            select(Notification).where(Notification.recipient_user_id == outsider.id)
        )
        assert count.scalars().all() == []

    @pytest.mark.parametrize(
        "body",
        [
            {"title": "", "body": "b"},
            {"title": "   ", "body": "b"},
            {"title": "t"},
            {"title": "t", "body": "b", "link": "https://evil.example/phish"},
            {"title": "t", "body": "b", "link": "//evil.example"},
            {"title": "x" * 201, "body": "b"},
        ],
    )
    async def test_a_bad_announcement_is_422(
        self, api: AsyncClient, admin: User, hospital_id: uuid.UUID, body: dict[str, Any]
    ) -> None:
        response = await api.post(
            f"{URL}/broadcast", json=body, headers=auth_headers(admin.id, hospital_id)
        )

        assert response.status_code == 422
