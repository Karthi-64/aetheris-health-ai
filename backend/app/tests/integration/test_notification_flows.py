"""End-to-end notification flows.

Each test starts with the thing a person does in another module — invites a
colleague, asks for a password reset, applies a discount — and follows it to
what the recipient ends up with: an entry in their notification centre and,
where the kind calls for one, an email that the worker sends.

Real app, real database. The only thing faked is the SMTP server.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.api.dependencies.db import get_db_session
from app.api.dependencies.services import get_audit_sink
from app.core.config import settings
from app.core.notifications import NotificationRequest
from app.core.security import hash_token
from app.main import create_app
from app.models.hospital import Hospital
from app.models.notification import DeliveryStatus, Notification, NotificationDelivery
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User, UserStatus
from app.repositories.hospital_repository import HospitalRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.user_repository import UserRepository
from app.services.notification_service import NotificationService
from app.tests.billing_helpers import auth_headers, insert_patient, insert_user_with_permissions
from app.tests.conftest import RecordingAuditSink

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.email import EmailMessage

pytestmark = pytest.mark.database

NOTIFICATIONS = "/api/v1/notifications"
OWN = ["notification.read.own", "notification.preference.update.own"]
PASSWORD = "Str0ng!Passw0rd123"  # noqa: S105 — a test credential, not a real one


class _Outbox:
    """A stand-in SMTP server that keeps what it was asked to send."""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


@pytest.fixture(autouse=True)
def mail_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Email is set up, as it must be for any link to be issued at all.

    An invitation or reset link is only minted when there is a mail transport
    to carry it (``settings.SMTP_HOST``). Nothing connects to this host: the
    worker is handed an :class:`_Outbox` instead of an SMTP client.
    """
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.hospital.example")


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """An HTTP client sharing the test's rolled-back session."""
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override
    application.dependency_overrides[get_audit_sink] = RecordingAuditSink
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest.fixture
def worker(db_session: AsyncSession) -> NotificationService:
    """The service as the background worker builds it."""
    users = UserRepository(db_session)
    return NotificationService(
        NotificationRepository(db_session),
        users,
        HospitalRepository(db_session),
        db_session,
        RecordingAuditSink(),
    )


async def _deliveries(session: AsyncSession, user_id: uuid.UUID) -> list[NotificationDelivery]:
    """Every delivery of every notification addressed to a user."""
    result = await session.execute(
        select(NotificationDelivery)
        .join(Notification, Notification.id == NotificationDelivery.notification_id)
        .where(Notification.recipient_user_id == user_id)
    )
    return list(result.unique().scalars().all())


async def _user_with_real_address(
    session: AsyncSession, hospital_id: uuid.UUID, permissions: list[str]
) -> User:
    """A user whose address passes the API's email validation.

    The shared helper uses a ``.test`` domain, which is fine for rows but is
    rejected by ``EmailStr`` when it has to travel through a request body.
    """
    user = await insert_user_with_permissions(session, hospital_id, permissions)
    user.email = f"staff-{uuid.uuid4().hex[:12]}@hospital.example"
    await session.flush()
    return user


def _token_in(body: str) -> str:
    """Pull the single-use token out of an emailed link.

    The token rides in the URL *fragment*, which a browser never sends to a
    server, so it stays out of access logs and ``Referer`` headers. A link
    that put it back in the query string must fail here.
    """
    assert "?token=" not in body, "the token must not travel in the query string"
    match = re.search(r"/reset-password#token=(\S+)", body)
    assert match is not None, "the email carries no reset link"
    return match.group(1)


async def _tokens_issued_to(session: AsyncSession, user_id: uuid.UUID) -> int:
    """How many reset or activation tokens exist for a user, used or not."""
    rows = await session.execute(
        select(PasswordResetToken.id).where(PasswordResetToken.user_id == user_id)
    )
    return len(rows.scalars().all())


async def _live_tokens(session: AsyncSession, user_id: uuid.UUID) -> int:
    """How many of a user's tokens could still be redeemed right now."""
    result = await session.execute(
        select(func.count())
        .select_from(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > func.now(),
        )
    )
    return int(result.scalar_one())


async def _notices(session: AsyncSession, user_id: uuid.UUID) -> int:
    """How many notification rows, shown in-app or not, exist for a user."""
    result = await session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.recipient_user_id == user_id)
    )
    return int(result.scalar_one())


def _break_the_email_queue(patch: pytest.MonkeyPatch) -> list[str]:
    """Make every insert into the email queue fail.

    :returns: The bodies that were about to be queued, as they arrive.
    """
    intended: list[str] = []

    async def _insert_fails(self: NotificationRepository, **values: Any) -> None:
        intended.append(str(values["body"]))
        msg = f"the queue is unavailable: {values['body']!r}"
        raise RuntimeError(msg)

    patch.setattr(NotificationRepository, "create_email_delivery", _insert_fails)
    return intended


class TestCredentialDelivery:
    """``NotificationService.deliver_credential`` against a real savepoint.

    The caller of this method kills a link unless it answers ``True``. These
    check, on PostgreSQL, that ``True`` means one queued email and that every
    ``False`` leaves no row behind, the in-app notification included, while
    the caller's own transaction carries on untouched.
    """

    SECRET = "raw-single-use-token"  # noqa: S105 — a literal standing in for a token

    def _request(self, user_id: uuid.UUID, hospital_id: uuid.UUID) -> NotificationRequest:
        return NotificationRequest(
            kind="auth.user_invited",
            hospital_id=hospital_id,
            recipient_user_ids=(user_id,),
            variables={"expires_in": "72 hours"},
            secret_variables={"action_url": f"https://app.test/reset-password#token={self.SECRET}"},
        )

    async def test_true_means_exactly_one_email_is_queued_for_the_recipient(
        self, db_session: AsyncSession, worker: NotificationService, hospital_id: uuid.UUID
    ) -> None:
        user = await _user_with_real_address(db_session, hospital_id, [])

        queued = await worker.deliver_credential(self._request(user.id, hospital_id))

        assert queued is True
        [delivery] = await _deliveries(db_session, user.id)
        assert delivery.status is DeliveryStatus.QUEUED
        assert delivery.to_address == user.email
        assert delivery.hospital_id == hospital_id
        assert _token_in(delivery.body or "") == self.SECRET
        assert await _notices(db_session, user.id) == 1

    async def test_a_recipient_in_another_hospital_is_false_and_writes_nothing(
        self,
        db_session: AsyncSession,
        worker: NotificationService,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Attack: have a credential for one hospital's account emailed across tenants."""
        outsider = await _user_with_real_address(db_session, other_hospital_id, [])

        queued = await worker.deliver_credential(self._request(outsider.id, hospital_id))
        nobody = await worker.deliver_credential(self._request(uuid.uuid4(), hospital_id))

        assert queued is False
        assert nobody is False
        assert await _deliveries(db_session, outsider.id) == []
        assert await _notices(db_session, outsider.id) == 0

    async def test_a_recipient_with_no_address_is_false_and_gets_no_in_app_notice_either(
        self, db_session: AsyncSession, worker: NotificationService, hospital_id: uuid.UUID
    ) -> None:
        """An in-app "you have been invited" with no email behind it must not survive."""
        user = await _user_with_real_address(db_session, hospital_id, [])
        # Straight into the column: the model would not accept an empty address.
        await db_session.execute(update(User).where(User.id == user.id).values(email=""))
        await db_session.refresh(user)
        assert user.email == ""

        queued = await worker.deliver_credential(self._request(user.id, hospital_id))

        assert queued is False
        assert await _deliveries(db_session, user.id) == []
        assert await _notices(db_session, user.id) == 0, "the in-app notice outlived the email"

    async def test_a_failing_insert_is_false_undoes_the_notice_and_spares_the_callers_work(
        self,
        db_session: AsyncSession,
        worker: NotificationService,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: a broken email queue takes the caller's transaction down with it.

        The failure must cost only the savepoint: the in-app notice written
        just before the insert goes, and what the caller wrote before asking
        (here, the user) stays and the session stays usable.
        """
        user = await _user_with_real_address(db_session, hospital_id, [])
        with monkeypatch.context() as patched:
            intended = _break_the_email_queue(patched)

            queued = await worker.deliver_credential(self._request(user.id, hospital_id))

        assert queued is False
        assert len(intended) == 1, "premise: the email was about to be queued"
        assert self.SECRET in intended[0]
        assert await _deliveries(db_session, user.id) == []
        assert await _notices(db_session, user.id) == 0, "the in-app notice outlived the email"
        # The caller's transaction is intact, and the next request is judged afresh.
        assert await db_session.get(User, user.id) is not None
        assert await worker.deliver_credential(self._request(user.id, hospital_id)) is True
        assert len(await _deliveries(db_session, user.id)) == 1
        assert await _notices(db_session, user.id) == 1


class TestInvitation:
    """User-management AC-2: "the invited user receives an email"."""

    async def test_an_invite_is_emailed_and_the_emailed_link_activates_the_account(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        worker: NotificationService,
        hospital_id: uuid.UUID,
    ) -> None:
        admin = await insert_user_with_permissions(db_session, hospital_id, ["user.create"])
        email = f"invitee-{uuid.uuid4().hex[:12]}@hospital.example"

        invited = await api.post(
            "/api/v1/users",
            json={"email": email, "first_name": "Nisha", "last_name": "Nair"},
            headers=auth_headers(admin.id, hospital_id),
        )
        assert invited.status_code == 201, invited.text
        user_id = uuid.UUID(invited.json()["data"]["id"])
        # The administrator is told an email was queued, and is not handed the link.
        assert invited.json()["data"]["invitation"] == {"delivery": "queued"}
        assert "invite_token" not in invited.json()["data"]

        # The email is queued in the same transaction as the invite...
        [delivery] = await _deliveries(db_session, user_id)
        assert delivery.status is DeliveryStatus.QUEUED
        assert delivery.hospital_id == hospital_id
        assert delivery.to_address == email
        token = _token_in(delivery.body or "")
        assert token not in invited.text, "the activation token came back to the inviter"

        # ...and the in-app copy never contains the token.
        [notification] = (
            (
                await db_session.execute(
                    select(Notification).where(Notification.recipient_user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        assert notification.kind == "auth.user_invited"
        assert token not in notification.title + notification.body + (notification.link or "")
        assert "Asha Menon invited you" in notification.body

        # The worker sends it.
        outbox = _Outbox()
        report = await worker.deliver_due_emails(outbox)

        assert report.sent == 1
        [message] = outbox.messages
        assert message.to == email
        assert "Hello Nisha," in message.body
        assert token in message.body
        await db_session.refresh(notification)
        [sent] = await _deliveries(db_session, user_id)
        await db_session.refresh(sent)
        assert sent.status is DeliveryStatus.SENT
        assert sent.body is None, "the token must not outlive the email"
        assert notification.sent_email is True

        # And the link in the email really does activate the account.
        reset = await api.post(
            "/api/v1/auth/password/reset", json={"token": token, "new_password": PASSWORD}
        )
        assert reset.status_code == 200, reset.text
        user = await db_session.get(User, user_id)
        assert user is not None
        await db_session.refresh(user)
        assert user.status is UserStatus.ACTIVE

    async def test_a_rejected_invite_leaves_no_notification_behind(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        admin = await _user_with_real_address(db_session, hospital_id, ["user.create"])
        before = (await db_session.execute(select(Notification.id))).scalars().all()

        duplicate = await api.post(
            "/api/v1/users",
            json={"email": admin.email, "first_name": "Dup", "last_name": "Licate"},
            headers=auth_headers(admin.id, hospital_id),
        )

        assert duplicate.status_code == 409
        after = (await db_session.execute(select(Notification.id))).scalars().all()
        assert after == before


class TestPasswordReset:
    async def test_a_reset_request_is_emailed_and_the_link_works(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        worker: NotificationService,
        hospital_id: uuid.UUID,
    ) -> None:
        user = await _user_with_real_address(db_session, hospital_id, OWN)
        # Even a user who tried to switch this email off still gets it (AC-4).
        await api.put(
            f"{NOTIFICATIONS}/preferences",
            json={"preferences": {"auth.password_reset_requested": {"email": False}}},
            headers=auth_headers(user.id, hospital_id),
        )

        response = await api.post("/api/v1/auth/password/forgot", json={"email": user.email})
        assert response.status_code == 200, response.text

        outbox = _Outbox()
        await worker.deliver_due_emails(outbox)

        [message] = outbox.messages
        assert message.to == user.email
        token = _token_in(message.body)
        # The in-app notice says a reset was requested, and nothing more.
        centre = await api.get(NOTIFICATIONS, headers=auth_headers(user.id, hospital_id))
        [notice] = centre.json()["data"]
        assert notice["kind"] == "auth.password_reset_requested"
        assert token not in str(notice)

        reset = await api.post(
            "/api/v1/auth/password/reset", json={"token": token, "new_password": PASSWORD}
        )
        assert reset.status_code == 200, reset.text

    async def test_a_reset_whose_email_cannot_be_queued_leaves_no_live_token(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        worker: NotificationService,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: a reset link is minted, its email is lost, and the link lives on.

        Forgot-password is anonymous and always answers the same. If the
        email queue is down, the token it minted has no email carrying it; it
        must be dead before the request ends, not left redeemable by whoever
        can recover it. The owner loses nothing: the next request works.
        """
        user = await _user_with_real_address(db_session, hospital_id, OWN)
        with monkeypatch.context() as patched:
            intended = _break_the_email_queue(patched)
            response = await api.post("/api/v1/auth/password/forgot", json={"email": user.email})
        normal = await api.post(
            "/api/v1/auth/password/forgot", json={"email": "nobody@hospital.example"}
        )

        # The same answer as for anyone else.
        assert response.status_code == 200, response.text
        assert response.json()["message"] == normal.json()["message"]
        assert len(intended) == 1, "premise: the email was about to be queued"
        orphan = _token_in(intended[0])
        assert orphan not in response.text
        assert await _tokens_issued_to(db_session, user.id) == 1
        assert await _live_tokens(db_session, user.id) == 0, "a link with no email is live"
        assert await _deliveries(db_session, user.id) == []
        assert await _notices(db_session, user.id) == 0
        outbox = _Outbox()
        await worker.deliver_due_emails(outbox)
        assert outbox.messages == []
        before = user.password_hash
        refused = await api.post(
            "/api/v1/auth/password/reset", json={"token": orphan, "new_password": PASSWORD}
        )
        assert refused.status_code == 401, refused.text
        await db_session.refresh(user)
        assert user.password_hash == before

        # With the queue back, asking again sends a link that works.
        again = await api.post("/api/v1/auth/password/forgot", json={"email": user.email})
        assert again.status_code == 200
        [delivery] = await _deliveries(db_session, user.id)
        working = _token_in(delivery.body or "")
        assert working != orphan
        assert hash_token(working) != hash_token(orphan)
        reset = await api.post(
            "/api/v1/auth/password/reset", json={"token": working, "new_password": PASSWORD}
        )
        assert reset.status_code == 200, reset.text

    async def test_requests_made_while_the_queue_was_down_do_not_use_up_the_owners_resets(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: lock an owner out of password reset by asking during an outage.

        Forgot-password sends at most three links per account per token
        lifetime. If links that were never emailed counted towards that, an
        anonymous caller could spend the allowance while the queue is down
        and the owner would be refused a reset with no working link anywhere.
        Only links that can still be redeemed count.
        """
        user = await _user_with_real_address(db_session, hospital_id, OWN)
        with monkeypatch.context() as patched:
            intended = _break_the_email_queue(patched)
            for _ in range(4):
                await api.post("/api/v1/auth/password/forgot", json={"email": user.email})
        assert len(intended) == 4, "premise: four requests each tried to send a link"
        assert await _live_tokens(db_session, user.id) == 0
        assert await _deliveries(db_session, user.id) == []

        response = await api.post("/api/v1/auth/password/forgot", json={"email": user.email})

        assert response.status_code == 200
        [delivery] = await _deliveries(db_session, user.id)
        reset = await api.post(
            "/api/v1/auth/password/reset",
            json={"token": _token_in(delivery.body or ""), "new_password": PASSWORD},
        )
        assert reset.status_code == 200, reset.text

    async def test_the_cap_still_holds_while_working_links_are_in_the_mailbox(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """Attack: bury a mailbox with reset emails. The fourth request sends nothing."""
        user = await _user_with_real_address(db_session, hospital_id, OWN)

        replies = [
            await api.post("/api/v1/auth/password/forgot", json={"email": user.email})
            for _ in range(5)
        ]

        assert {reply.status_code for reply in replies} == {200}
        assert len({reply.json()["message"] for reply in replies}) == 1
        assert len(await _deliveries(db_session, user.id)) == 3
        assert await _live_tokens(db_session, user.id) == 3
        assert await _tokens_issued_to(db_session, user.id) == 3

    async def test_an_invited_account_gets_nothing_from_forgot_password(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        worker: NotificationService,
        hospital_id: uuid.UUID,
    ) -> None:
        """Attack: keep a lapsed invitation alive for ever by asking for resets.

        Forgot-password is anonymous. If it issued a link to an account that
        has never been activated, anyone who knew the address could renew an
        invitation the administrator had let expire.
        """
        user = await _user_with_real_address(db_session, hospital_id, OWN)
        user.status = UserStatus.INVITED
        await db_session.flush()

        response = await api.post("/api/v1/auth/password/forgot", json={"email": user.email})

        assert response.status_code == 200, response.text
        assert await _tokens_issued_to(db_session, user.id) == 0
        assert await _deliveries(db_session, user.id) == []
        outbox = _Outbox()
        await worker.deliver_due_emails(outbox)
        assert outbox.messages == []

    async def test_without_a_mail_transport_no_reset_token_is_minted(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A reset token with nowhere to go must not exist.

        With no transport the only ways such a token could reach anyone are
        the ways it must never travel: a response, a log line, an operator.
        """
        monkeypatch.setattr(settings, "SMTP_HOST", None)
        user = await _user_with_real_address(db_session, hospital_id, OWN)

        response = await api.post("/api/v1/auth/password/forgot", json={"email": user.email})

        assert response.status_code == 200, response.text
        assert await _tokens_issued_to(db_session, user.id) == 0
        assert await _deliveries(db_session, user.id) == []

    async def test_an_unknown_address_sends_nothing(
        self, api: AsyncClient, db_session: AsyncSession, worker: NotificationService
    ) -> None:
        response = await api.post(
            "/api/v1/auth/password/forgot", json={"email": "nobody@hospital.example"}
        )

        assert response.status_code == 200
        outbox = _Outbox()
        await worker.deliver_due_emails(outbox)
        assert outbox.messages == []


class TestDiscountApproval:
    async def test_a_large_discount_lands_in_every_approvers_centre(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        worker: NotificationService,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        hospital = await db_session.get(Hospital, hospital_id)
        assert hospital is not None
        hospital.settings = {"billing": {"discount_approval_threshold_percent": "10"}}
        await db_session.flush()
        approve = [*OWN, "invoice.approve_discount"]
        billing = await insert_user_with_permissions(
            db_session, hospital_id, [*OWN, "invoice.create", "invoice.update", "invoice.read"]
        )
        admin = await insert_user_with_permissions(db_session, hospital_id, approve)
        # Wants this kind by email as well.
        emailed_admin = await insert_user_with_permissions(db_session, hospital_id, approve)
        elsewhere = await insert_user_with_permissions(db_session, other_hospital_id, approve)
        await api.put(
            f"{NOTIFICATIONS}/preferences",
            json={"preferences": {"billing.discount_approval_requested": {"email": True}}},
            headers=auth_headers(emailed_admin.id, hospital_id),
        )
        patient = await insert_patient(db_session, hospital_id)
        billing_headers = auth_headers(billing.id, hospital_id)
        draft = await api.post(
            "/api/v1/invoices",
            json={
                "patient_id": str(patient.id),
                "items": [{"description": "Consultation", "unit_price": "500.00"}],
            },
            headers=billing_headers,
        )
        assert draft.status_code == 201, draft.text

        patched = await api.patch(
            f"/api/v1/invoices/{draft.json()['data']['id']}",
            json={"discount_amount": "200.00", "discount_reason": "Financial hardship"},
            headers=billing_headers,
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["data"]["discount_pending_approval"] is True

        async def centre(user: User, tenant: uuid.UUID) -> list[dict[str, Any]]:
            response = await api.get(NOTIFICATIONS, headers=auth_headers(user.id, tenant))
            return list(response.json()["data"])

        [notice] = await centre(admin, hospital_id)
        assert notice["kind"] == "billing.discount_approval_requested"
        assert notice["link"] == "/billing"
        assert "Ananya Rao" in notice["body"]
        assert "200.00" in notice["body"]
        assert len(await centre(emailed_admin, hospital_id)) == 1
        # Not the person who applied it, and not another hospital's admin.
        assert await centre(billing, hospital_id) == []
        assert await centre(elsewhere, other_hospital_id) == []

        # Only the admin who opted in is emailed.
        outbox = _Outbox()
        await worker.deliver_due_emails(outbox)
        assert [m.to for m in outbox.messages] == [emailed_admin.email]
        assert await _deliveries(db_session, admin.id) == []


class TestEmailQueue:
    async def _queue_one(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> NotificationDelivery:
        user = await _user_with_real_address(db_session, hospital_id, OWN)
        await api.post("/api/v1/auth/password/forgot", json={"email": user.email})
        [delivery] = await _deliveries(db_session, user.id)
        return delivery

    async def test_with_email_switched_off_the_delivery_fails_with_a_reason(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        worker: NotificationService,
        hospital_id: uuid.UUID,
    ) -> None:
        delivery = await self._queue_one(api, db_session, hospital_id)

        report = await worker.deliver_due_emails(None)

        assert report.failed == 1
        await db_session.refresh(delivery)
        assert delivery.status is DeliveryStatus.FAILED
        assert "SMTP_HOST" in (delivery.last_error or "")
        assert delivery.body is None

    async def test_a_refused_email_is_retried_later_and_then_sent(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        worker: NotificationService,
        hospital_id: uuid.UUID,
    ) -> None:
        delivery = await self._queue_one(api, db_session, hospital_id)

        class _Down:
            async def send(self, message: EmailMessage) -> None:
                raise ConnectionRefusedError

        now = datetime.now(UTC)
        first = await worker.deliver_due_emails(_Down(), now=now)
        too_soon = await worker.deliver_due_emails(_Outbox(), now=now + timedelta(seconds=5))
        outbox = _Outbox()
        later = await worker.deliver_due_emails(outbox, now=now + timedelta(minutes=1))

        assert (first.retrying, too_soon.processed, later.sent) == (1, 0, 1)
        await db_session.refresh(delivery)
        assert delivery.status is DeliveryStatus.SENT
        assert delivery.attempts == 2
        assert len(outbox.messages) == 1
