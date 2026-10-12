"""The activation link belongs to the invited person, and to nobody else.

An invitation's one-time link is a credential for an account. Whoever holds it
chooses that account's password. It used to come back in the response to
``POST /api/v1/users``, so every administrator held a working credential for
every account they created, and could become the nurse, the doctor or the
second administrator they had just invited.

Each class here replays one way the link could reach someone other than the
owner of the invited mailbox, or outlive the moment it was meant for, against
the real application over HTTP and a real PostgreSQL:

- it must not be in any response, log line, audit record or read endpoint;
- it must work once, for a limited time, and only for the newest invitation;
- with no mail transport it must not exist at all;
- sending it again, suspending the account and reactivating it must each
  leave no older link alive;
- a link whose email could not be queued must not exist, and nobody may be
  told it was sent;
- a link refused once must stay dead, whatever happens to the account later.

The one legitimate path is exercised the way the invited person takes it: the
token is read from the body of the email queued for their address
(``notification_deliveries.body``), never from anything the API returned.

Most tests share the suite's rolled-back session. The ones about concurrency
cannot: a race needs separate connections and real commits, so they build
their own hospital, commit it, and delete every row again in ``finally``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies.db import get_db_session
from app.core.config import settings
from app.core.security import (
    create_access_token,
    generate_opaque_token,
    hash_token,
    verify_password,
)
from app.database import session as database_session_module
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.auth_throttle import AuthThrottleBucket
from app.models.hospital import Hospital
from app.models.notification import DeliveryStatus, Notification, NotificationDelivery
from app.models.password_reset_token import PasswordResetToken
from app.models.permission import Permission
from app.models.role import Role, RolePermission
from app.models.user import User, UserStatus
from app.repositories.notification_repository import NotificationRepository
from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.services.auth_throttle import POLICIES, BucketKind, Budget
from app.tests.conftest import every_log_line_reaches_the_root, grant_permissions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator

    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.database

USERS = "/api/v1/users"
AUTH = "/api/v1/auth"
NOTIFICATIONS = "/api/v1/notifications"
AUDIT = "/api/v1/audit-logs"

PASSWORD = "Str0ng!Passw0rd123"  # noqa: S105 — a test credential, not a real one
OTHER_PASSWORD = "0ther!Passw0rd456"  # noqa: S105

#: What a user needs to open their own notification centre.
OWN_NOTIFICATIONS = ["notification.read.own", "notification.preference.update.own"]
#: An administrator with every way of *looking* that the application offers.
ADMIN = [
    "user.read",
    "user.create",
    "user.update",
    "user.deactivate",
    "user.reset_password",
    "role.assign",
    "audit.read",
    "audit.export",
    *OWN_NOTIFICATIONS,
]

_LINK = re.compile(r"/reset-password#token=([A-Za-z0-9_\-]+)")
_FORGOT_REPLY = "If an account with that email exists, a password reset link has been sent."


# ── Helpers ──────────────────────────────────────────────────────────────────


def _email(prefix: str = "invitee") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@hospital.example"


def _bearer(user_id: uuid.UUID, hospital_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(user_id=user_id, hospital_id=hospital_id)
    return {"Authorization": f"Bearer {token}"}


async def _make_user(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    codes: list[str],
    *,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    """Insert a user holding exactly ``codes``.

    The password hash is a placeholder: these users act through a bearer
    token and never sign in.
    """
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=_email("staff"),
        password_hash="test-placeholder-not-a-hash",  # noqa: S106
        first_name="Asha",
        last_name="Menon",
        status=status,
    )
    session.add(user)
    await session.flush()
    if codes:
        await grant_permissions(session, hospital_id=hospital_id, user_id=user.id, codes=codes)
    return user


async def _role_granting(session: AsyncSession, hospital_id: uuid.UUID, codes: list[str]) -> Role:
    """A role in this hospital carrying exactly ``codes`` (which must exist)."""
    # Resolve the permissions first: a query once the role is half-built
    # would autoflush it.
    permissions = [
        (await session.execute(select(Permission).where(Permission.code == code)))
        .unique()
        .scalar_one()
        for code in codes
    ]
    role = Role(id=uuid.uuid4(), hospital_id=hospital_id, name=f"own-{uuid.uuid4().hex[:8]}")
    for permission in permissions:
        role.role_permissions.append(
            RolePermission(id=uuid.uuid4(), permission_id=permission.id, permission=permission)
        )
    session.add(role)
    await session.flush()
    return role


def _tokens_in(bodies: list[str | None]) -> set[str]:
    found: set[str] = set()
    for body in bodies:
        found.update(_LINK.findall(body or ""))
    return found


async def _emailed_tokens(session: AsyncSession, address: str) -> set[str]:
    """Every activation token in an email queued for ``address``.

    This is the invited person's view: what is in their mailbox.
    """
    rows = await session.execute(
        select(NotificationDelivery.body).where(NotificationDelivery.to_address == address)
    )
    return _tokens_in(list(rows.scalars().all()))


async def _emailed_token(session: AsyncSession, address: str) -> str:
    """The one activation token emailed to ``address``."""
    tokens = await _emailed_tokens(session, address)
    assert len(tokens) == 1, f"expected exactly one emailed link, found {len(tokens)}"
    return next(iter(tokens))


async def _newest_token(session: AsyncSession, address: str, seen: set[str]) -> str:
    """The token emailed to ``address`` that was not there before."""
    fresh = await _emailed_tokens(session, address) - seen
    assert len(fresh) == 1, f"expected one new emailed link, found {len(fresh)}"
    return next(iter(fresh))


async def _delivery_count(session: AsyncSession, address: str) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(NotificationDelivery)
        .where(NotificationDelivery.to_address == address)
    )
    return int(result.scalar_one())


async def _token_rows(session: AsyncSession, user_id: uuid.UUID) -> int:
    """How many activation or reset tokens exist for a user, used or not."""
    result = await session.execute(
        select(func.count())
        .select_from(PasswordResetToken)
        .where(PasswordResetToken.user_id == user_id)
    )
    return int(result.scalar_one())


async def _live_token_rows(session: AsyncSession, user_id: uuid.UUID) -> int:
    """How many tokens for a user could still be redeemed right now."""
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


async def _live_token_hashes(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """The stored hashes of a user's tokens that could still be redeemed."""
    rows = await session.execute(
        select(PasswordResetToken.token_hash).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > func.now(),
        )
    )
    return set(rows.scalars().all())


async def _notices(session: AsyncSession, user_id: uuid.UUID) -> int:
    """How many notifications, in-app or not, exist for a user."""
    result = await session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.recipient_user_id == user_id)
    )
    return int(result.scalar_one())


async def _audited(session: AsyncSession, user_id: uuid.UUID, action: str) -> int:
    """How many audit entries of one action name a user as their target."""
    result = await session.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.target_id == user_id, AuditLog.action == action)
    )
    return int(result.scalar_one())


async def _plant_live_token(session: AsyncSession, user_id: uuid.UUID) -> str:
    """Give an account a redeemable token directly, and return the raw token.

    For states the application does not produce on its own: a live link on an
    account whose links should all be dead. What matters is what the next
    operation does about it.
    """
    raw, token_hash = generate_opaque_token()
    session.add(
        PasswordResetToken(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await session.flush()
    return raw


#: The text of the error the broken email queue raises. It must never be logged.
_QUEUE_ERROR = "invalid input for query argument $3"


def _break_the_email_queue(patch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    """Make every insert into the email queue fail with an error that quotes the body.

    A SQLAlchemy error prints the statement's bound parameters, and the
    database driver quotes the offending value. For the email queue those are
    the message body: the activation link itself.

    :returns: The bodies that were about to be queued, and the text of each
        error raised.
    """
    intended: list[str] = []
    raised: list[str] = []

    async def _insert_fails(self: NotificationRepository, **values: Any) -> None:
        body = str(values["body"])
        intended.append(body)
        error = DBAPIError(
            "INSERT INTO notification_deliveries (to_address, subject, body) VALUES ($1, $2, $3)",
            {name: str(value) for name, value in values.items() if name != "notification"},
            Exception(f"{_QUEUE_ERROR}: {body!r}"),
        )
        raised.append(str(error))
        raise error

    patch.setattr(NotificationRepository, "create_email_delivery", _insert_fails)
    return intended, raised


async def _account(session: AsyncSession, user_id: uuid.UUID) -> tuple[UserStatus, str, Any]:
    """An account's status, password hash and ``password_changed_at``, read fresh."""
    row = (
        await session.execute(
            select(User.status, User.password_hash, User.password_changed_at).where(
                User.id == user_id
            )
        )
    ).one()
    return row[0], row[1], row[2]


async def _assert_never_activated(session: AsyncSession, user_id: uuid.UUID) -> None:
    status, password_hash, changed_at = await _account(session, user_id)
    assert status is not UserStatus.ACTIVE, "the account was activated"
    assert changed_at is None, "a password was set on the account"
    assert not verify_password(PASSWORD, password_hash)
    assert not verify_password(OTHER_PASSWORD, password_hash)


async def _invite(api: AsyncClient, headers: dict[str, str], email: str, **extra: Any) -> Response:
    return await api.post(
        USERS,
        json={"email": email, "first_name": "Nisha", "last_name": "Nair", **extra},
        headers=headers,
    )


async def _resend(api: AsyncClient, headers: dict[str, str], user_id: uuid.UUID) -> Response:
    return await api.post(f"{USERS}/{user_id}/invitation", headers=headers)


async def _redeem(api: AsyncClient, token: str, password: str = PASSWORD) -> Response:
    return await api.post(f"{AUTH}/password/reset", json={"token": token, "new_password": password})


async def _login(api: AsyncClient, email: str, password: str = PASSWORD) -> Response:
    return await api.post(f"{AUTH}/login", json={"email": email, "password": password})


def _keys(value: Any) -> Iterator[str]:
    """Every key, at any depth, of a decoded JSON document."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def _leaves(value: Any) -> Iterator[str]:
    """Every scalar, at any depth, of a decoded JSON document, as text."""
    if isinstance(value, dict):
        for inner in value.values():
            yield from _leaves(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _leaves(inner)
    elif value is not None:
        yield str(value)


def _everything_sent(response: Response) -> str:
    """The whole response as the caller receives it: status line, headers, body."""
    headers = "\n".join(f"{name}: {value}" for name, value in response.headers.multi_items())
    return f"{response.status_code}\n{headers}\n\n{response.text}"


def _assert_carries_no_credential(response: Response, token: str) -> None:
    """The response gives away neither the token nor anything derived from it."""
    sent = _everything_sent(response)
    assert token not in sent, "the raw activation token is in the response"
    assert hash_token(token) not in sent, "the stored token hash is in the response"
    assert "token=" not in sent, "an activation link is in the response"
    assert "reset-password" not in sent, "an activation link is in the response"


async def _read_everything(
    api: AsyncClient, headers: dict[str, str], *, about: uuid.UUID | None
) -> list[Response]:
    """Call every read endpoint this caller can reach and return the responses.

    ``about`` is the invited user, when the caller is allowed to look at them.
    A read that is refused is returned too; the caller decides what to expect.
    """
    paths = [
        f"{USERS}/me",
        NOTIFICATIONS,
        f"{NOTIFICATIONS}?unread_only=true&page_size=100",
        f"{NOTIFICATIONS}/unread-count",
        f"{NOTIFICATIONS}/preferences",
    ]
    if about is not None:
        paths += [
            f"{USERS}?page_size=100",
            f"{USERS}?status=invited&page_size=100",
            f"{USERS}/{about}",
            f"{USERS}/{about}/roles",
            f"{AUDIT}?page_size=100",
            f"{AUDIT}?target_id={about}&page_size=100",
            f"{AUDIT}/export",
        ]
    responses = [await api.get(path, headers=headers) for path in paths]
    if about is not None:
        listing = await api.get(f"{AUDIT}?page_size=100", headers=headers)
        for entry in listing.json()["data"]:
            responses.append(await api.get(f"{AUDIT}/{entry['id']}", headers=headers))
    return responses


class _LogTrap(logging.Handler):
    """Keeps every log record that reaches the root logger, fully rendered."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        # ``format`` appends the traceback of an attached exception, which is
        # where an exception's own text (and whatever it quotes) ends up.
        self.lines.append(self.format(record))
        self.lines.append(repr(record.args))

    @property
    def everything(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def all_logs() -> Iterator[_LogTrap]:
    """Everything the application logs, at every level, structlog included.

    The application's structlog configuration renders each event and hands it
    to the standard library, so one handler on the root logger with the level
    opened right up sees all of it. The suite otherwise runs at ``CRITICAL``,
    which would make "nothing was logged" pass for the wrong reason; every
    test using this asserts that an expected event *was* captured.
    """
    root = logging.getLogger()
    trap = _LogTrap()
    with every_log_line_reaches_the_root():
        root.addHandler(trap)
        try:
            yield trap
        finally:
            root.removeHandler(trap)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mail_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Email is set up; the request-rate middleware is out of the way.

    A link is minted only when there is a mail transport to carry it, so the
    default here is "configured". Nothing ever connects to this host: no
    worker runs, and the tests read the queue directly.

    The per-minute request limits are not what these tests are about, and a
    429 from the middleware would make "the attacker's request failed" pass
    without the application having decided anything. They are raised so every
    refusal asserted below comes from the code under test.
    """
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.hospital.example")
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_HOSPITAL_PER_MIN", 1_000_000)


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """HTTP client on the test's rolled-back session, with the real audit sink."""
    application = create_app()

    async def _override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _override
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client
    application.dependency_overrides.clear()


@dataclass
class _Admin:
    id: uuid.UUID
    hospital_id: uuid.UUID
    headers: dict[str, str]


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession, hospital_id: uuid.UUID) -> _Admin:
    """The inviting administrator, holding every permission in :data:`ADMIN`."""
    user = await _make_user(db_session, hospital_id, ADMIN)
    return _Admin(user.id, hospital_id, _bearer(user.id, hospital_id))


@dataclass
class _Invited:
    id: uuid.UUID
    email: str
    response: Response


async def _invited(api: AsyncClient, admin: _Admin, **extra: Any) -> _Invited:
    email = _email()
    response = await _invite(api, admin.headers, email, **extra)
    assert response.status_code == 201, response.text
    return _Invited(uuid.UUID(response.json()["data"]["id"]), email, response)


# ── 1. The response ──────────────────────────────────────────────────────────


class TestTheResponseCarriesNoActivationToken:
    """Attack: an administrator reads the new account's credential off the API."""

    async def test_the_invite_response_does_not_contain_the_token(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """``POST /users`` used to return ``invite_token``. It must return nothing like it."""
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)

        payload = invited.response.json()
        assert "invite_token" not in payload["data"]
        offending = [key for key in _keys(payload) if "token" in key.lower()]
        assert offending == [], f"keys that look like a credential: {offending}"
        assert payload["data"]["invitation"] == {"delivery": "queued"}
        _assert_carries_no_credential(invited.response, token)

    async def test_the_resend_response_does_not_contain_the_token(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Sending again mints a new link; the caller must not get that one either."""
        invited = await _invited(api, admin)
        first = await _emailed_token(db_session, invited.email)

        response = await _resend(api, admin.headers, invited.id)

        assert response.status_code == 200, response.text
        second = await _newest_token(db_session, invited.email, {first})
        payload = response.json()
        assert payload["data"] == {"delivery": "queued"}
        assert [key for key in _keys(payload) if "token" in key.lower()] == []
        for token in (first, second):
            _assert_carries_no_credential(response, token)

    async def test_no_lifecycle_response_contains_the_token(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """The other writes an administrator can make on the account stay silent too."""
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)

        responses = [
            await api.patch(
                f"{USERS}/{invited.id}", json={"last_name": "Menon"}, headers=admin.headers
            ),
            await api.post(f"{USERS}/{invited.id}/reset-password", headers=admin.headers),
            await api.post(f"{USERS}/{invited.id}/deactivate", headers=admin.headers),
            await api.post(f"{USERS}/{invited.id}/reactivate", headers=admin.headers),
        ]

        for response in responses:
            assert response.status_code == 200, response.text
            _assert_carries_no_credential(response, token)

    def test_the_published_contract_no_longer_offers_a_token(self) -> None:
        """The OpenAPI document must not promise a field the API must never send."""
        contract = create_app().openapi()
        for path in (USERS, f"{USERS}/{{user_id}}/invitation"):
            described = json.dumps(contract["paths"][path]["post"])
            assert "invite_token" not in described
            assert "token=" not in described


# ── 2. The logs ──────────────────────────────────────────────────────────────


class TestTheLogsCarryNoActivationToken:
    """Attack: anyone who can read application logs collects activation links."""

    async def test_inviting_resending_and_activating_log_no_token(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: _Admin,
        all_logs: _LogTrap,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        invited = await _invited(api, admin)
        first = await _emailed_token(db_session, invited.email)
        assert (await _resend(api, admin.headers, invited.id)).status_code == 200
        second = await _newest_token(db_session, invited.email, {first})
        assert (await _redeem(api, first)).status_code == 401
        assert (await _redeem(api, second)).status_code == 200

        logged = all_logs.everything
        printed = "".join(capfd.readouterr())
        # The capture is real: the events these calls emit are in it.
        for event in ("user_invited", "user_invitation_resent", "password_reset_completed"):
            assert event in logged, f"log capture missed {event!r}"
        for token in (first, second):
            for output in (logged, printed):
                assert token not in output, "a raw activation token was logged"
                assert hash_token(token) not in output, "a token hash was logged"
        for output in (logged, printed):
            assert "token=" not in output, "an activation link was logged"
            assert "reset-password#" not in output, "an activation link was logged"

    @pytest.mark.parametrize("step", ["invite", "resend"])
    async def test_a_failed_email_insert_does_not_log_the_link(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: _Admin,
        all_logs: _LogTrap,
        capfd: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        step: str,
    ) -> None:
        """Attack: provoke a database error and read the link out of the traceback.

        The error raised by the insert into the email queue quotes the
        message body, which is the activation link. The failure is logged as
        ``notification.credential_not_queued`` with the kind of error and
        nothing else: not the link, not the token, not the error's own text,
        and no traceback.
        """
        if step == "resend":
            invited = await _invited(api, admin)
        intended, raised = _break_the_email_queue(monkeypatch)
        if step == "invite":
            invited = await _invited(api, admin)
            answer = invited.response
            said = answer.json()["data"]["invitation"]
        else:
            answer = await _resend(api, admin.headers, invited.id)
            assert answer.status_code == 200, answer.text
            said = answer.json()["data"]

        # The premise holds: the link was about to be queued, and the error
        # that stopped it quotes the link.
        [token] = _tokens_in(list(intended))
        assert len(raised) == 1
        assert token in raised[0]
        assert _QUEUE_ERROR in raised[0]
        assert said == {"delivery": "unavailable"}

        logged = all_logs.everything
        assert "notification.credential_not_queued" in logged, "the failure was not logged"
        assert "auth.user_invited" in logged, "the log does not say which kind failed"
        assert "DBAPIError" in logged, "the log does not say what kind of error it was"
        # The older event logged the exception itself when it was used for this.
        assert "notification.create_failed" not in logged

        printed = "".join(capfd.readouterr())
        for output in (logged, printed):
            assert token not in output, "the activation token was logged with the error"
            assert "token=" not in output, "the activation link was logged with the error"
            assert "reset-password" not in output, "the activation link was logged with the error"
            assert hash_token(token) not in output
            assert _QUEUE_ERROR not in output, "the error's own text was logged"
            assert raised[0] not in output
            assert "INSERT INTO notification_deliveries" not in output, "the statement was logged"
            assert "Traceback" not in output, "a traceback was logged"
            assert invited.email not in output, "the invited address was logged"
        _assert_carries_no_credential(answer, token)

    def test_the_application_engines_keep_bound_parameters_out_of_errors_and_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every engine the application builds hides statement parameters.

        The email body, and so the link, is a bound parameter of the insert
        into ``notification_deliveries``. With ``hide_parameters`` off, SQL
        echo and every database error would print it.
        """
        monkeypatch.setattr(database_session_module, "_engine", None)
        monkeypatch.setattr(
            database_session_module,
            "_database_url",
            "postgresql+asyncpg://nobody@localhost:1/not_a_database",
        )

        shared = database_session_module.get_async_engine()
        dedicated = database_session_module.create_session_factory(pool_size=1).kw["bind"]

        # Nothing connected: an engine opens no connection until it is used.
        assert shared.sync_engine.hide_parameters is True
        assert dedicated.sync_engine.hide_parameters is True


# ── 3. The audit trail ───────────────────────────────────────────────────────


class TestTheAuditTrailCarriesNoActivationToken:
    """Attack: the audit log, which administrators can read, records the link."""

    async def test_no_audit_row_written_by_the_invitation_contains_the_token(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        invited = await _invited(api, admin)
        first = await _emailed_token(db_session, invited.email)
        assert (await _resend(api, admin.headers, invited.id)).status_code == 200
        second = await _newest_token(db_session, invited.email, {first})
        assert (await _redeem(api, second)).status_code == 200

        rows = (
            (await db_session.execute(select(AuditLog).where(AuditLog.target_id == invited.id)))
            .scalars()
            .all()
        )
        # The trail is real: each step left its entry.
        assert {row.action for row in rows} >= {
            "user.invited",
            "user.invitation_resent",
            "auth.password.reset",
        }
        every_row = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.hospital_id == admin.hospital_id)
                )
            )
            .scalars()
            .all()
        )
        for row in every_row:
            whole = json.dumps(
                {column.name: getattr(row, column.name) for column in AuditLog.__table__.columns},
                default=str,
            )
            for token in (first, second):
                assert token not in whole, f"{row.action} recorded the raw token"
                assert hash_token(token) not in whole, f"{row.action} recorded the token hash"
            assert "token=" not in whole, f"{row.action} recorded an activation link"


# ── 4. The read APIs ─────────────────────────────────────────────────────────


class TestNoReadEndpointRevealsTheToken:
    """Attack: the link is not in the invite response, so look for it elsewhere."""

    async def test_the_inviting_admin_finds_it_nowhere(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Every read the admin can make, while the link is still live."""
        invited = await _invited(api, admin)
        first = await _emailed_token(db_session, invited.email)
        assert (await _resend(api, admin.headers, invited.id)).status_code == 200
        second = await _newest_token(db_session, invited.email, {first})

        responses = await _read_everything(api, admin.headers, about=invited.id)

        assert len(responses) > 12
        for response in responses:
            assert response.status_code == 200, f"{response.request.url}: {response.text}"
            for token in (first, second):
                _assert_carries_no_credential(response, token)
        # The reads did see the invited account, so this was not a search of nothing.
        assert any(invited.email in response.text for response in responses)
        assert await _live_token_rows(db_session, invited.id) == 1

    async def test_the_invited_user_finds_it_nowhere_after_activating(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """The in-app copy of the invitation never held the link, before or after."""
        role = await _role_granting(db_session, admin.hospital_id, OWN_NOTIFICATIONS)
        invited = await _invited(api, admin, role_ids=[str(role.id)])
        token = await _emailed_token(db_session, invited.email)
        assert (await _redeem(api, token)).status_code == 200
        signed_in = await _login(api, invited.email)
        assert signed_in.status_code == 200, signed_in.text
        own = {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"}

        responses = await _read_everything(api, own, about=None)
        centre = await api.get(NOTIFICATIONS, headers=own)
        [welcome] = centre.json()["data"]
        responses.append(await api.post(f"{NOTIFICATIONS}/{welcome['id']}/read", headers=own))
        responses.append(await api.post(f"{NOTIFICATIONS}/read-all", headers=own))

        assert welcome["kind"] == "auth.user_invited"
        for response in [*responses, signed_in]:
            assert response.status_code == 200, f"{response.request.url}: {response.text}"
            sent = _everything_sent(response)
            assert token not in sent
            assert hash_token(token) not in sent
            assert "token=" not in sent
        # Still the admin's view, now that the account is active.
        for response in await _read_everything(api, admin.headers, about=invited.id):
            _assert_carries_no_credential(response, token)


# ── 5. Single use ────────────────────────────────────────────────────────────


class TestTheLinkWorksOnce:
    """Attack: reuse a link that has already activated the account."""

    async def test_a_second_use_is_refused_and_changes_nothing(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Whoever finds the email later must not be able to take the account over."""
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)

        first = await _redeem(api, token, PASSWORD)
        second = await _redeem(api, token, OTHER_PASSWORD)

        assert first.status_code == 200, first.text
        assert second.status_code == 401, second.text
        status, password_hash, _ = await _account(db_session, invited.id)
        assert status is UserStatus.ACTIVE
        assert verify_password(PASSWORD, password_hash)
        assert not verify_password(OTHER_PASSWORD, password_hash)
        assert await _live_token_rows(db_session, invited.id) == 0


# ── 6. Expiry ────────────────────────────────────────────────────────────────


class TestAnExpiredLinkCannotActivate:
    """Attack: use an invitation found in an old mailbox."""

    async def test_an_expired_token_is_refused_and_the_account_stays_invited(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)
        await db_session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == invited.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

        response = await _redeem(api, token)

        assert response.status_code == 401, response.text
        await _assert_never_activated(db_session, invited.id)
        assert (await _account(db_session, invited.id))[0] is UserStatus.INVITED
        assert (await _login(api, invited.email)).status_code == 401

    async def test_the_link_is_issued_with_the_configured_lifetime_and_no_longer(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """The stored expiry is what the refusal above depends on."""
        before = datetime.now(UTC)
        invited = await _invited(api, admin)

        expires_at = (
            await db_session.execute(
                select(PasswordResetToken.expires_at).where(
                    PasswordResetToken.user_id == invited.id
                )
            )
        ).scalar_one()

        lifetime = timedelta(hours=settings.INVITE_TOKEN_TTL_HOURS)
        assert before + lifetime <= expires_at <= datetime.now(UTC) + lifetime


# ── 7. The inviter ───────────────────────────────────────────────────────────


class TestTheInviterCannotActivateTheInvitee:
    """Attack: the administrator sets the new account's password themselves."""

    async def test_nothing_the_api_gave_the_admin_works_as_a_token(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Every value the admin was ever shown is tried as the activation token.

        The invite response, a resend, and every read endpoint: each key,
        each value, each header. None of them may activate the account.
        """
        invited = await _invited(api, admin)
        resent = await _resend(api, admin.headers, invited.id)
        assert resent.status_code == 200, resent.text
        tokens = await _emailed_tokens(db_session, invited.email)
        seen = [invited.response, resent]
        seen += await _read_everything(api, admin.headers, about=invited.id)

        candidates: set[str] = set()
        for response in seen:
            candidates.update(value for _, value in response.headers.multi_items())
            if response.headers.get("content-type", "").startswith("application/json"):
                document = response.json()
                candidates.update(_keys(document))
                candidates.update(_leaves(document))
            else:
                candidates.update(re.split(r"[,\r\n]+", response.text))
        # ...and the obvious things to derive from what was shown.
        candidates.update(
            [
                invited.id.hex,
                str(invited.id).upper(),
                invited.email,
                hash_token(str(invited.id)),
                hash_token(invited.email),
                admin.headers["Authorization"].removeprefix("Bearer "),
                "",
                "null",
                "queued",
            ]
        )
        assert len(candidates) > 40
        assert not candidates & tokens, "a real token was among what the admin was shown"

        for candidate in sorted(candidates):
            response = await _redeem(api, candidate)
            assert response.status_code in (401, 422), (
                f"{candidate!r} was accepted as an activation token: {response.text}"
            )

        await _assert_never_activated(db_session, invited.id)
        assert (await _account(db_session, invited.id))[0] is UserStatus.INVITED
        # The invited person is unaffected by all of that guessing.
        newest = next(iter(tokens - {await _first_token(db_session, invited.id, tokens)}))
        assert (await _redeem(api, newest)).status_code == 200

    async def test_the_stored_hash_is_not_a_token(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Attack: somebody who can read the token table replays what is in it.

        Only a hash is stored. Presenting the hash itself must not work, or
        read access to the database would be as good as the mailbox.
        """
        invited = await _invited(api, admin)
        stored = (
            await db_session.execute(
                select(PasswordResetToken.token_hash).where(
                    PasswordResetToken.user_id == invited.id
                )
            )
        ).scalar_one()
        token = await _emailed_token(db_session, invited.email)
        assert stored == hash_token(token)
        assert stored != token

        response = await _redeem(api, stored)

        assert response.status_code == 401, response.text
        await _assert_never_activated(db_session, invited.id)

    async def test_the_admin_reset_endpoint_does_not_activate_or_reveal_a_password(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Attack: use "reset this user's password" to take over an invited account."""
        invited = await _invited(api, admin)

        response = await api.post(f"{USERS}/{invited.id}/reset-password", headers=admin.headers)

        assert response.status_code == 200, response.text
        assert response.json().get("data") in (None, {}, [])
        await _assert_never_activated(db_session, invited.id)
        assert (await _account(db_session, invited.id))[0] is UserStatus.INVITED


async def _first_token(session: AsyncSession, user_id: uuid.UUID, tokens: set[str]) -> str:
    """Of the emailed tokens, the one that has already been invalidated."""
    dead_hashes = set(
        (
            await session.execute(
                select(PasswordResetToken.token_hash).where(
                    PasswordResetToken.user_id == user_id,
                    PasswordResetToken.used_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    dead = [token for token in tokens if hash_token(token) in dead_hashes]
    assert len(dead) == 1
    return dead[0]


# ── 8. The secure path ───────────────────────────────────────────────────────


class TestTheEmailedLinkStillActivatesTheAccount:
    """The fix must not have closed the front door along with the back one."""

    async def test_the_token_from_the_queued_email_activates_and_the_user_signs_in(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        invited = await _invited(api, admin)
        assert (await _login(api, invited.email)).status_code == 401

        delivery = (
            (
                await db_session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.to_address == invited.email
                    )
                )
            )
            .scalars()
            .one()
        )
        assert delivery.status is DeliveryStatus.QUEUED
        assert delivery.hospital_id == admin.hospital_id
        body = delivery.body or ""
        # In the fragment, which a browser does not send to any server.
        assert "?token=" not in body
        [token] = _tokens_in([body])

        notification = (
            (
                await db_session.execute(
                    select(Notification).where(Notification.recipient_user_id == invited.id)
                )
            )
            .scalars()
            .one()
        )
        in_app = json.dumps(
            {
                column.name: getattr(notification, column.name)
                for column in Notification.__table__.columns
            },
            default=str,
        )
        assert notification.kind == "auth.user_invited"
        assert token not in in_app, "the in-app notification holds the token"
        assert "token=" not in in_app
        assert "reset-password" not in in_app

        activated = await _redeem(api, token)

        assert activated.status_code == 200, activated.text
        assert token not in _everything_sent(activated)
        status, password_hash, changed_at = await _account(db_session, invited.id)
        assert status is UserStatus.ACTIVE
        assert changed_at is not None
        assert verify_password(PASSWORD, password_hash)
        signed_in = await _login(api, invited.email)
        assert signed_in.status_code == 200, signed_in.text
        assert signed_in.json()["data"]["user"]["email"] == invited.email
        assert (await _login(api, invited.email, OTHER_PASSWORD)).status_code == 401


# ── 9. No mail transport ─────────────────────────────────────────────────────


class TestWithoutEmailThereIsNoLink:
    """Attack: with email off, the link has to go *somewhere*. It must not exist."""

    @pytest.mark.parametrize("smtp_host", [None, "", "   "])
    async def test_an_invite_without_a_mail_transport_mints_nothing(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: _Admin,
        monkeypatch: pytest.MonkeyPatch,
        smtp_host: str | None,
    ) -> None:
        monkeypatch.setattr(settings, "SMTP_HOST", smtp_host)

        invited = await _invited(api, admin)

        assert invited.response.json()["data"]["invitation"] == {"delivery": "unavailable"}
        assert invited.response.json()["data"]["status"] == "invited"
        assert await _token_rows(db_session, invited.id) == 0, "a token exists with no mailbox"
        assert await _delivery_count(db_session, invited.email) == 0
        notices = await db_session.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.recipient_user_id == invited.id)
        )
        assert notices.scalar_one() == 0
        assert (await _account(db_session, invited.id))[0] is UserStatus.INVITED

    async def test_there_is_no_other_way_to_activate_the_account(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: _Admin,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Forgot-password, a resend, and the admin's reset: none produce a link."""
        monkeypatch.setattr(settings, "SMTP_HOST", None)
        invited = await _invited(api, admin)

        forgot = await api.post(f"{AUTH}/password/forgot", json={"email": invited.email})
        resent = await _resend(api, admin.headers, invited.id)
        reset = await api.post(f"{USERS}/{invited.id}/reset-password", headers=admin.headers)

        assert forgot.status_code == 200
        assert forgot.json()["message"] == _FORGOT_REPLY
        assert resent.status_code == 200, resent.text
        assert resent.json()["data"] == {"delivery": "unavailable"}
        assert reset.status_code == 200
        assert await _token_rows(db_session, invited.id) == 0
        assert await _delivery_count(db_session, invited.email) == 0
        await _assert_never_activated(db_session, invited.id)
        assert (await _account(db_session, invited.id))[0] is UserStatus.INVITED
        assert (await _login(api, invited.email)).status_code == 401

    async def test_once_email_is_configured_a_resend_delivers_a_working_link(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: _Admin,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "SMTP_HOST", None)
        invited = await _invited(api, admin)
        # Asking while email is off does not use up the resend allowance.
        for _ in range(5):
            assert (await _resend(api, admin.headers, invited.id)).json()["data"] == {
                "delivery": "unavailable"
            }

        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.hospital.example")
        # Forgot-password still gives an invited account nothing...
        await api.post(f"{AUTH}/password/forgot", json={"email": invited.email})
        assert await _token_rows(db_session, invited.id) == 0
        # ...the administrator sending the invitation is the only way.
        resent = await _resend(api, admin.headers, invited.id)

        assert resent.status_code == 200, resent.text
        assert resent.json()["data"] == {"delivery": "queued"}
        token = await _emailed_token(db_session, invited.email)
        _assert_carries_no_credential(resent, token)
        assert (await _redeem(api, token)).status_code == 200
        assert (await _account(db_session, invited.id))[0] is UserStatus.ACTIVE
        assert (await _login(api, invited.email)).status_code == 200


# ── 10. Sending it again ─────────────────────────────────────────────────────


class TestResendingAnInvitation:
    """Attacks on ``POST /users/{id}/invitation``."""

    async def test_it_requires_user_create(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Attack: a user who may only read and edit users re-issues a colleague's link."""
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)
        lesser = await _make_user(
            db_session,
            admin.hospital_id,
            ["user.read", "user.update", "user.deactivate", "user.reset_password"],
        )

        refused = await _resend(api, _bearer(lesser.id, admin.hospital_id), invited.id)
        anonymous = await api.post(f"{USERS}/{invited.id}/invitation")

        assert refused.status_code == 403, refused.text
        assert anonymous.status_code == 401, anonymous.text
        assert await _delivery_count(db_session, invited.email) == 1
        assert await _token_rows(db_session, invited.id) == 1
        # The link the invited person already has was not disturbed.
        assert (await _redeem(api, token)).status_code == 200

    async def test_another_hospitals_admin_gets_404_and_changes_nothing(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: _Admin,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Attack: an admin elsewhere kills this hospital's invitation, or probes for it.

        A resend invalidates the link already sent and emails the invited
        person again, so reaching across hospitals would be both a denial of
        service and a way to confirm that an account id exists.
        """
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)
        outsider = await _make_user(db_session, other_hospital_id, ADMIN)
        outsider_headers = _bearer(outsider.id, other_hospital_id)

        crossed = await _resend(api, outsider_headers, invited.id)
        missing = await _resend(api, outsider_headers, uuid.uuid4())

        assert crossed.status_code == 404, crossed.text
        assert crossed.json()["message"] == missing.json()["message"]
        assert await _delivery_count(db_session, invited.email) == 1
        assert await _token_rows(db_session, invited.id) == 1
        assert await _live_token_rows(db_session, invited.id) == 1
        audited = await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.target_id == invited.id, AuditLog.action == "user.invitation_resent")
        )
        assert audited.scalar_one() == 0
        # The outsider did not spend this account's resend allowance either.
        for _ in range(3):
            assert (await _resend(api, admin.headers, invited.id)).status_code == 200
        assert token in await _emailed_tokens(db_session, invited.email)

    @pytest.mark.parametrize("status", [UserStatus.ACTIVE, UserStatus.SUSPENDED])
    async def test_a_user_who_is_not_invited_gives_409_and_no_link(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin, status: UserStatus
    ) -> None:
        """Attack: mint an activation link for an account that already has an owner.

        Such a link would be a password reset for any account in the
        hospital, sent on an administrator's say-so.
        """
        member = await _make_user(db_session, admin.hospital_id, [], status=status)

        response = await _resend(api, admin.headers, member.id)

        assert response.status_code == 409, response.text
        assert await _token_rows(db_session, member.id) == 0
        assert await _delivery_count(db_session, member.email) == 0

    async def test_an_activated_invitee_cannot_be_sent_another_link(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """The same refusal for an account that got to ACTIVE through its invitation."""
        invited = await _invited(api, admin)
        assert (
            await _redeem(api, await _emailed_token(db_session, invited.email))
        ).status_code == 200

        response = await _resend(api, admin.headers, invited.id)

        assert response.status_code == 409, response.text
        assert await _live_token_rows(db_session, invited.id) == 0
        assert await _delivery_count(db_session, invited.email) == 1

    async def test_the_old_link_dies_the_moment_a_new_one_is_sent(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Attack: an invitation sent to the wrong person is "corrected" by resending.

        If the earlier link kept working, whoever received it could still
        activate the account after the administrator believed it replaced.
        """
        invited = await _invited(api, admin)
        old = await _emailed_token(db_session, invited.email)

        assert (await _resend(api, admin.headers, invited.id)).status_code == 200
        new = await _newest_token(db_session, invited.email, {old})

        assert new != old
        assert await _live_token_rows(db_session, invited.id) == 1
        refused = await _redeem(api, old, OTHER_PASSWORD)
        assert refused.status_code == 401, refused.text
        await _assert_never_activated(db_session, invited.id)
        assert (await _redeem(api, new)).status_code == 200
        _, password_hash, _ = await _account(db_session, invited.id)
        assert verify_password(PASSWORD, password_hash)

    async def test_the_fourth_resend_in_a_row_is_refused_and_sends_nothing(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Attack: bury a mailbox, or keep killing the link before it can be used."""
        policy = POLICIES[BucketKind.INVITE_RESEND]
        assert isinstance(policy, Budget)
        assert policy.burst == 3, "this test encodes the documented allowance of three"
        invited = await _invited(api, admin)
        seen = {await _emailed_token(db_session, invited.email)}
        for _ in range(policy.burst):
            response = await _resend(api, admin.headers, invited.id)
            assert response.status_code == 200, response.text
            seen.add(await _newest_token(db_session, invited.email, seen))
        deliveries = await _delivery_count(db_session, invited.email)
        tokens = await _token_rows(db_session, invited.id)
        last = (
            await db_session.execute(
                select(PasswordResetToken.token_hash).where(
                    PasswordResetToken.user_id == invited.id,
                    PasswordResetToken.used_at.is_(None),
                )
            )
        ).scalar_one()

        fourth = await _resend(api, admin.headers, invited.id)

        assert fourth.status_code == 429, fourth.text
        assert "invitations" in fourth.json()["message"], "refused by something else"
        assert await _delivery_count(db_session, invited.email) == deliveries
        assert await _token_rows(db_session, invited.id) == tokens
        for token in seen:
            _assert_carries_no_credential(fourth, token)
        # The refusal did not kill the link from the third resend.
        [working] = [token for token in seen if hash_token(token) == last]
        assert (await _redeem(api, working)).status_code == 200

    async def test_one_accounts_allowance_is_not_anothers(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Using up the resends for one invitee must not block inviting anyone else."""
        exhausted = await _invited(api, admin)
        for _ in range(3):
            assert (await _resend(api, admin.headers, exhausted.id)).status_code == 200
        assert (await _resend(api, admin.headers, exhausted.id)).status_code == 429

        other = await _invited(api, admin)

        assert (await _resend(api, admin.headers, other.id)).status_code == 200


# ── 11. An email that could not be queued ────────────────────────────────────


class TestALinkWithNoEmailBehindItDoesNotExist:
    """Attack: a link is minted, its email is lost, and the link lives on.

    A live token that no email carries is a credential nobody is holding. The
    administrator, told "queued", would wait for an activation that cannot
    happen, while anyone who could recover the token (a database reader, a
    log, a later bug) would have a working way into the account. So: no email
    queued means no live link from that request, and an honest answer.
    """

    async def test_a_first_invitation_that_cannot_be_queued_leaves_no_live_token(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: _Admin,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with monkeypatch.context() as patched:
            intended, _ = _break_the_email_queue(patched)
            invited = await _invited(api, admin)
        [orphan] = _tokens_in(list(intended))

        assert invited.response.json()["data"]["invitation"] == {"delivery": "unavailable"}
        assert invited.response.json()["data"]["status"] == "invited"
        _assert_carries_no_credential(invited.response, orphan)
        assert await _live_token_rows(db_session, invited.id) == 0, "a link with no email is live"
        assert await _delivery_count(db_session, invited.email) == 0
        # The in-app "you have been invited" went with the email it promised.
        assert await _notices(db_session, invited.id) == 0
        assert (await _account(db_session, invited.id))[0] is UserStatus.INVITED
        # The token that was minted and never sent does not work.
        refused = await _redeem(api, orphan)
        assert refused.status_code == 401, refused.text
        await _assert_never_activated(db_session, invited.id)
        assert (await _login(api, invited.email)).status_code == 401
        # The account was still created and audited, and can be invited properly.
        assert await _audited(db_session, invited.id, "user.invited") == 1
        resent = await _resend(api, admin.headers, invited.id)
        assert resent.json()["data"] == {"delivery": "queued"}, resent.text
        working = await _emailed_token(db_session, invited.email)
        assert working != orphan
        assert (await _redeem(api, working)).status_code == 200

    async def test_a_resend_that_cannot_be_queued_says_so_and_leaves_the_earlier_link_working(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: _Admin,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attack: lock an invited person out by resending while the queue is down.

        Killing the earlier link before its replacement was safely queued
        left the invited person with no working link at all, and the
        administrator was told a new one had been sent.
        """
        invited = await _invited(api, admin)
        earlier = await _emailed_token(db_session, invited.email)

        with monkeypatch.context() as patched:
            intended, _ = _break_the_email_queue(patched)
            response = await _resend(api, admin.headers, invited.id)
        [orphan] = _tokens_in(list(intended))

        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"delivery": "unavailable"}
        assert "queued" not in response.json()["message"].lower()
        for token in (earlier, orphan):
            _assert_carries_no_credential(response, token)
        # Nothing new was queued or shown to the invited person...
        assert await _delivery_count(db_session, invited.email) == 1, "premise: nothing queued"
        assert await _notices(db_session, invited.id) == 1
        assert await _emailed_tokens(db_session, invited.email) == {earlier}
        # ...nothing says an invitation was sent again...
        assert await _audited(db_session, invited.id, "user.invitation_resent") == 0
        # ...and exactly one link is live: the one already in their mailbox.
        assert orphan != earlier
        assert await _live_token_hashes(db_session, invited.id) == {hash_token(earlier)}
        refused = await _redeem(api, orphan, OTHER_PASSWORD)
        assert refused.status_code == 401, "the link that was never sent works"
        await _assert_never_activated(db_session, invited.id)
        activated = await _redeem(api, earlier)
        assert activated.status_code == 200, activated.text
        status, password_hash, _ = await _account(db_session, invited.id)
        assert status is UserStatus.ACTIVE
        assert verify_password(PASSWORD, password_hash)

    async def test_a_resend_that_is_queued_after_a_failed_one_leaves_exactly_one_link(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: _Admin,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once the queue is back, a resend replaces the earlier link as it always did."""
        invited = await _invited(api, admin)
        earlier = await _emailed_token(db_session, invited.email)
        with monkeypatch.context() as patched:
            intended, _ = _break_the_email_queue(patched)
            assert (await _resend(api, admin.headers, invited.id)).json()["data"] == {
                "delivery": "unavailable"
            }
        [orphan] = _tokens_in(list(intended))

        again = await _resend(api, admin.headers, invited.id)

        assert again.json()["data"] == {"delivery": "queued"}, again.text
        newest = await _newest_token(db_session, invited.email, {earlier})
        assert await _live_token_hashes(db_session, invited.id) == {hash_token(newest)}
        for dead in (earlier, orphan):
            assert (await _redeem(api, dead, OTHER_PASSWORD)).status_code == 401
        await _assert_never_activated(db_session, invited.id)
        assert (await _redeem(api, newest)).status_code == 200


# ── 12. Lifecycle ────────────────────────────────────────────────────────────


class TestSuspensionAndReactivation:
    """Attacks through the account lifecycle around an unused invitation."""

    async def test_suspending_an_invited_user_kills_the_outstanding_link(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Attack: an invitation withdrawn by suspension is activated anyway."""
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)

        suspended = await api.post(f"{USERS}/{invited.id}/deactivate", headers=admin.headers)

        assert suspended.status_code == 200, suspended.text
        assert suspended.json()["data"]["status"] == "suspended"
        assert await _live_token_rows(db_session, invited.id) == 0
        assert (await _redeem(api, token)).status_code == 401
        await _assert_never_activated(db_session, invited.id)
        assert (await _account(db_session, invited.id))[0] is UserStatus.SUSPENDED

    async def test_reactivating_a_never_activated_user_returns_them_to_invited(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Attack: suspend then reactivate to turn an invitation into a live account.

        ``ACTIVE`` here would be an account nobody has set a password on and
        whose mailbox nobody has proved they hold. And the link sent before
        the suspension must not come back to life with the account.
        """
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)
        assert (
            await api.post(f"{USERS}/{invited.id}/deactivate", headers=admin.headers)
        ).status_code == 200

        reactivated = await api.post(f"{USERS}/{invited.id}/reactivate", headers=admin.headers)

        assert reactivated.status_code == 200, reactivated.text
        assert reactivated.json()["data"]["status"] == "invited"
        _assert_carries_no_credential(reactivated, token)
        assert (await _account(db_session, invited.id))[0] is UserStatus.INVITED
        assert await _live_token_rows(db_session, invited.id) == 0
        assert await _delivery_count(db_session, invited.email) == 1, "reactivation sent a link"
        assert (await _redeem(api, token)).status_code == 401
        await _assert_never_activated(db_session, invited.id)
        assert (await _login(api, invited.email)).status_code == 401
        # The way back in is a new invitation, sent on purpose.
        assert (await _resend(api, admin.headers, invited.id)).status_code == 200
        fresh = await _newest_token(db_session, invited.email, {token})
        assert (await _redeem(api, fresh)).status_code == 200
        assert (await _account(db_session, invited.id))[0] is UserStatus.ACTIVE

    @pytest.mark.parametrize("activated_before", [False, True])
    async def test_reactivation_kills_a_link_that_survived_the_suspension(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        admin: _Admin,
        activated_before: bool,
    ) -> None:
        """Attack: a link that outlived the suspension comes back to life with the account.

        Reactivation must not rely on the suspension having cleaned up. A
        live token is put on the suspended account directly, as a failed or
        raced clean-up would leave it; reactivating must kill it itself. For
        a never-activated account this matters most: it goes back to
        ``INVITED``, the one state in which such a link activates it.
        """
        invited = await _invited(api, admin)
        if activated_before:
            first = await _emailed_token(db_session, invited.email)
            assert (await _redeem(api, first)).status_code == 200
        assert (
            await api.post(f"{USERS}/{invited.id}/deactivate", headers=admin.headers)
        ).status_code == 200
        survivor = await _plant_live_token(db_session, invited.id)
        assert await _live_token_rows(db_session, invited.id) == 1, "premise: a live link"
        _, hash_before, _ = await _account(db_session, invited.id)

        reactivated = await api.post(f"{USERS}/{invited.id}/reactivate", headers=admin.headers)

        assert reactivated.status_code == 200, reactivated.text
        expected = "active" if activated_before else "invited"
        assert reactivated.json()["data"]["status"] == expected
        _assert_carries_no_credential(reactivated, survivor)
        assert await _live_token_rows(db_session, invited.id) == 0, "reactivation left a live link"
        refused = await _redeem(api, survivor, OTHER_PASSWORD)
        assert refused.status_code == 401, "a link from before the suspension still works"
        status, hash_after, _ = await _account(db_session, invited.id)
        assert status.value == expected
        assert hash_after == hash_before, "the surviving link set the password"
        assert await _audited(db_session, invited.id, "user.reactivated") == 1

    async def test_reactivating_an_account_that_is_not_suspended_is_refused_and_kills_nothing(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Attack: "reactivate" a pending invitee to kill the link in their mailbox.

        Reactivation invalidates every outstanding link. Allowed on an
        account that was never suspended, it would let anyone holding
        ``user.deactivate`` silently void an invitation, with an audit entry
        that reads like a harmless reactivation.
        """
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)

        refused = await api.post(f"{USERS}/{invited.id}/reactivate", headers=admin.headers)

        assert refused.status_code == 409, refused.text
        _assert_carries_no_credential(refused, token)
        assert (await _account(db_session, invited.id))[0] is UserStatus.INVITED
        assert await _live_token_hashes(db_session, invited.id) == {hash_token(token)}
        assert await _audited(db_session, invited.id, "user.reactivated") == 0
        # The invited person's link still works, and so does the account after it.
        assert (await _redeem(api, token)).status_code == 200
        again = await api.post(f"{USERS}/{invited.id}/reactivate", headers=admin.headers)
        assert again.status_code == 409, again.text
        assert (await _login(api, invited.email)).status_code == 200

    async def test_reactivating_an_account_that_was_activated_makes_it_active_again(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """The control: only a *never-activated* account goes back to invited."""
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)
        assert (await _redeem(api, token)).status_code == 200
        assert (
            await api.post(f"{USERS}/{invited.id}/deactivate", headers=admin.headers)
        ).status_code == 200
        assert (await _login(api, invited.email)).status_code == 401

        reactivated = await api.post(f"{USERS}/{invited.id}/reactivate", headers=admin.headers)

        assert reactivated.json()["data"]["status"] == "active"
        assert (await _login(api, invited.email)).status_code == 200
        assert (await _resend(api, admin.headers, invited.id)).status_code == 409

    async def test_forgot_password_does_not_keep_a_lapsed_invitation_alive(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Attack: anyone who knows the address renews an expired invitation for ever.

        Forgot-password needs no sign-in. If it issued a link to an invited
        account, an invitation would never really lapse.
        """
        invited = await _invited(api, admin)
        token = await _emailed_token(db_session, invited.email)
        await db_session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == invited.id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )

        for _ in range(3):
            reply = await api.post(f"{AUTH}/password/forgot", json={"email": invited.email})
            assert reply.status_code == 200
            assert reply.json()["message"] == _FORGOT_REPLY

        assert await _token_rows(db_session, invited.id) == 1
        assert await _live_token_rows(db_session, invited.id) == 0
        assert await _delivery_count(db_session, invited.email) == 1
        assert (await _redeem(api, token)).status_code == 401
        await _assert_never_activated(db_session, invited.id)
        assert (await _account(db_session, invited.id))[0] is UserStatus.INVITED

    async def test_forgot_password_gives_an_unexpired_invitee_no_second_link(
        self, api: AsyncClient, db_session: AsyncSession, admin: _Admin
    ) -> None:
        """Nor may it multiply the links for an invitation that is still live."""
        invited = await _invited(api, admin)

        await api.post(f"{AUTH}/password/forgot", json={"email": invited.email})

        assert await _token_rows(db_session, invited.id) == 1
        assert await _delivery_count(db_session, invited.email) == 1


# ── Concurrency: real connections, real commits ──────────────────────────────


@dataclass
class _Live:
    """A committed hospital and administrator, and an app on real sessions."""

    engine: AsyncEngine
    api: AsyncClient
    hospital_id: uuid.UUID
    headers: dict[str, str]
    invited_emails: list[str] = field(default_factory=list)

    async def invite(self) -> tuple[uuid.UUID, str, str]:
        """Invite someone over HTTP and read their emailed token back.

        :returns: The user id, their address, and the token in their mailbox.
        """
        email = _email("race")
        response = await _invite(self.api, self.headers, email)
        assert response.status_code == 201, response.text
        async with AsyncSession(self.engine) as session:
            token = await _emailed_token(session, email)
        return uuid.UUID(response.json()["data"]["id"]), email, token

    async def waiting_on_locks(self) -> int:
        """How many of this database's connections are blocked on a lock."""
        async with self.engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            return int(result.scalar_one())

    async def until_waiting(self, expected: int) -> None:
        """Wait until ``expected`` connections are blocked on a lock."""
        for _ in range(500):
            if await self.waiting_on_locks() >= expected:
                return
            await asyncio.sleep(0.01)
        pytest.fail(f"{expected} requests never queued on the lock")


@pytest_asyncio.fixture
async def live(db_engine: AsyncEngine) -> AsyncGenerator[_Live]:
    """A committed world for races, deleted again whatever happens.

    Each request gets its own session, and so its own connection, exactly as
    in production. Nothing here is rolled back for us: the ``finally`` removes
    every row the test or the application wrote.
    """
    hospital_id = uuid.uuid4()
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        permissions_before = set((await session.execute(select(Permission.id))).scalars().all())
        buckets_before = set(
            (await session.execute(select(AuthThrottleBucket.key_hash))).scalars().all()
        )
        session.add(
            Hospital(
                id=hospital_id,
                name="Race Hospital",
                slug=f"race-{uuid.uuid4().hex[:12]}",
                address={"line1": "1 Test Road", "city": "Hyderabad", "country": "IN"},
                settings={},
            )
        )
        await session.flush()
        administrator = await _make_user(session, hospital_id, ADMIN)
        await session.commit()

    application = create_app()
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)

    async def _own_session() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    application.dependency_overrides[get_db_session] = _own_session
    try:
        # A deadlock or any other unexpected error must come back as the 500
        # the application would send, not as an exception in the test.
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield _Live(db_engine, client, hospital_id, _bearer(administrator.id, hospital_id))
    finally:
        application.dependency_overrides.clear()
        async with AsyncSession(db_engine) as session:
            await session.execute(delete(AuditLog).where(AuditLog.hospital_id == hospital_id))
            await session.execute(
                delete(AuthThrottleBucket).where(AuthThrottleBucket.key_hash.not_in(buckets_before))
            )
            # Tokens, notifications and their deliveries, role grants and
            # trusted devices all go with the users.
            await session.execute(delete(User).where(User.hospital_id == hospital_id))
            await session.execute(delete(Role).where(Role.hospital_id == hospital_id))
            await session.execute(
                delete(Permission).where(Permission.id.not_in(permissions_before))
            )
            await session.execute(delete(Hospital).where(Hospital.id == hospital_id))
            await session.commit()


async def _assert_no_link_into_an_active_account(
    live: _Live, user_id: uuid.UUID, email: str
) -> UserStatus:
    """The invariant every race must keep, checked against committed state.

    An ``ACTIVE`` account has no redeemable token, and none of the links ever
    emailed for it changes its password. An account still ``INVITED`` has at
    most one live link.

    :returns: The account's status.
    """
    async with AsyncSession(live.engine) as session:
        status, password_hash, _ = await _account(session, user_id)
        live_tokens = await _live_token_rows(session, user_id)
        emailed = await _emailed_tokens(session, email)

    if status is UserStatus.ACTIVE:
        assert live_tokens == 0, "an ACTIVE account still has a redeemable link"
        for token in emailed:
            attempt = await _redeem(live.api, token, OTHER_PASSWORD)
            assert attempt.status_code == 401, "an emailed link still works on an ACTIVE account"
        async with AsyncSession(live.engine) as session:
            assert (await _account(session, user_id))[1] == password_hash
    else:
        assert status is UserStatus.INVITED
        assert live_tokens <= 1, "more than one link is live for one invitation"
    return status


class TestParallelRedemption:
    """Attack: present the same link many times at the same instant."""

    async def test_parallel_redemptions_of_one_link_produce_exactly_one_success(
        self, live: _Live
    ) -> None:
        """Eight connections, one token, each with its own password.

        The token row is held locked from a ninth connection until all eight
        requests are queued on it, so they genuinely contend rather than run
        one after another.
        """
        attempts = 8
        user_id, _, token = await live.invite()
        passwords = [f"Par4llel!Passw0rd{index:02d}" for index in range(attempts)]

        async with live.engine.connect() as holder:
            await holder.execute(
                text("SELECT id FROM password_reset_tokens WHERE user_id = :u FOR UPDATE"),
                {"u": user_id},
            )
            racing = [
                asyncio.create_task(_redeem(live.api, token, password)) for password in passwords
            ]
            try:
                await live.until_waiting(attempts)
            finally:
                await holder.rollback()
            responses = await asyncio.gather(*racing)

        codes = sorted(response.status_code for response in responses)
        assert codes == [200] + [401] * (attempts - 1), codes
        [winner] = [
            password
            for password, response in zip(passwords, responses, strict=True)
            if response.status_code == 200
        ]
        async with AsyncSession(live.engine) as session:
            status, password_hash, _ = await _account(session, user_id)
            assert status is UserStatus.ACTIVE
            assert verify_password(winner, password_hash), "a losing request set the password"
            assert await _live_token_rows(session, user_id) == 0
            completed = await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.target_id == user_id, AuditLog.action == "auth.password.reset")
            )
            assert completed.scalar_one() == 1


async def _released_together(
    live: _Live,
    user_id: uuid.UUID,
    first: Callable[[], Awaitable[Response]],
    second: Callable[[], Awaitable[Response]],
) -> tuple[Response, Response]:
    """Hold two requests on the account's rows, then let both go.

    The token row is held locked from a separate connection. ``first`` is
    started and seen to block, then ``second``, so the order in which they
    arrive is chosen by the test rather than by the scheduler. Every writer
    takes the user row before the token row, so ``first`` holds the user row
    while it waits for the token, and ``second`` waits behind it on the user
    row: two requests blocked, neither waiting on the other in a cycle.

    :returns: The responses of ``first`` and ``second``.
    """
    async with live.engine.connect() as holder:
        await holder.execute(
            text("SELECT id FROM password_reset_tokens WHERE user_id = :u FOR UPDATE"),
            {"u": user_id},
        )
        started: list[asyncio.Task[Response]] = []
        try:
            started.append(asyncio.ensure_future(first()))
            await live.until_waiting(1)
            started.append(asyncio.ensure_future(second()))
            await live.until_waiting(2)
        finally:
            await holder.rollback()
        one, two = await asyncio.gather(*started)
    return one, two


#: The only two ways a resend racing an activation may end.
_CLEAN_OUTCOMES = {(200, 409), (401, 200)}


class TestResendRacingActivation:
    """Attack: get a fresh link issued for an account in the instant it activates.

    If both the activation and the resend went through, the administrator's
    resend would leave a live, emailed link that sets the password of an
    account somebody is already using.
    """

    @pytest.mark.parametrize("first", ["activation", "resend"])
    async def test_queued_on_the_same_token_they_never_both_win(
        self, live: _Live, first: str
    ) -> None:
        """Both requests are held at the token row, then released together.

        ``first`` is which of them reaches the row first. Whichever way it
        goes, the committed result must be one of exactly two things: the
        account is active and every link is dead, or it is still invited with
        one link.
        """
        user_id, email, token = await live.invite()

        def activate() -> Awaitable[Response]:
            return _redeem(live.api, token)

        def resend() -> Awaitable[Response]:
            return _resend(live.api, live.headers, user_id)

        if first == "activation":
            activated, resent = await _released_together(live, user_id, activate, resend)
        else:
            resent, activated = await _released_together(live, user_id, resend, activate)

        assert not (activated.status_code == 200 and resent.status_code == 200), (
            "the account was activated and a new link was issued for it"
        )
        status = await _assert_no_link_into_an_active_account(live, user_id, email)
        assert (status is UserStatus.ACTIVE) == (activated.status_code == 200)
        async with AsyncSession(live.engine) as session:
            emailed = await _emailed_tokens(session, email)
        if resent.status_code == 200:
            assert len(emailed) == 2
        else:
            assert emailed == {token}, "a refused resend still emailed a link"
        if first == "resend":
            # The resend committed first: the old link is dead, cleanly.
            assert (activated.status_code, resent.status_code) == (401, 200)

    @pytest.mark.parametrize(
        ("first", "expected"),
        [("activation", (200, 409)), ("resend", (401, 200))],
    )
    async def test_the_loser_of_the_race_is_refused_cleanly(
        self, live: _Live, first: str, expected: tuple[int, int]
    ) -> None:
        """Attack: time a resend against an activation to make one of them fall over.

        Redeeming a link used to take the token row and then the user row,
        while resending took them the other way round. Released together they
        deadlocked, PostgreSQL killed one, and that request answered 500. Both
        now take the user row first, so whoever arrives first wins and the
        other is refused properly: 409 for the resend ("not waiting to
        activate"), or 401 for the link ("invalid or expired").
        """
        user_id, email, token = await live.invite()

        def activate() -> Awaitable[Response]:
            return _redeem(live.api, token)

        def resend() -> Awaitable[Response]:
            return _resend(live.api, live.headers, user_id)

        if first == "activation":
            activated, resent = await _released_together(live, user_id, activate, resend)
        else:
            resent, activated = await _released_together(live, user_id, resend, activate)

        assert (activated.status_code, resent.status_code) == expected, (
            f"activation: {activated.text} / resend: {resent.text}"
        )
        loser = resent if first == "activation" else activated
        assert loser.json()["success"] is False
        assert "deadlock" not in loser.text.lower()
        _assert_carries_no_credential(loser, token)
        await _assert_no_link_into_an_active_account(live, user_id, email)

    async def test_unsynchronised_races_never_leave_a_link_into_an_active_account(
        self, live: _Live
    ) -> None:
        """The same race left to the scheduler, several times over."""
        for _ in range(6):
            user_id, email, token = await live.invite()

            activated, resent = await asyncio.gather(
                _redeem(live.api, token), _resend(live.api, live.headers, user_id)
            )

            # One wins, the other is refused; neither falls over with a 500.
            assert (activated.status_code, resent.status_code) in _CLEAN_OUTCOMES, (
                f"activation: {activated.text} / resend: {resent.text}"
            )
            status = await _assert_no_link_into_an_active_account(live, user_id, email)
            assert (status is UserStatus.ACTIVE) == (activated.status_code == 200)


class TestSuspensionRacingActivation:
    """Attack: activate an invitation in the instant the administrator withdraws it."""

    @pytest.mark.parametrize("first", ["activation", "suspension"])
    async def test_a_suspension_that_succeeds_leaves_no_way_in(
        self, live: _Live, first: str
    ) -> None:
        """If the administrator is told "suspended", the account is, and stays, shut."""
        user_id, email, token = await live.invite()

        def activate() -> Awaitable[Response]:
            return _redeem(live.api, token)

        def suspend() -> Awaitable[Response]:
            return live.api.post(f"{USERS}/{user_id}/deactivate", headers=live.headers)

        if first == "activation":
            activated, suspended = await _released_together(live, user_id, activate, suspend)
        else:
            suspended, activated = await _released_together(live, user_id, suspend, activate)

        async with AsyncSession(live.engine) as session:
            status, password_hash, _ = await _account(session, user_id)
            live_tokens = await _live_token_rows(session, user_id)
        assert live_tokens == 0 or status is UserStatus.INVITED
        if suspended.status_code == 200:
            assert status is UserStatus.SUSPENDED
            assert live_tokens == 0
            again = await _redeem(live.api, token, OTHER_PASSWORD)
            assert again.status_code == 401
            signed_in = await _login(live.api, email)
            assert signed_in.status_code == 401, "a suspended account signed in"
            async with AsyncSession(live.engine) as session:
                assert (await _account(session, user_id))[1] == password_hash
        else:
            # The suspension did not happen, and said so; it must not have
            # half-happened.
            assert status in (UserStatus.ACTIVE, UserStatus.INVITED)
        if first == "suspension":
            assert (suspended.status_code, activated.status_code) == (200, 401)

    @pytest.mark.parametrize("dies_at", ["links", "sessions"])
    async def test_a_suspension_that_fails_half_way_does_not_strand_a_live_link(
        self, live: _Live, monkeypatch: pytest.MonkeyPatch, dies_at: str
    ) -> None:
        """Attack: a link survives a suspension that died part of the way through.

        Suspending must be all or nothing. It used to commit the status
        change first (revoking the sessions committed) and only then kill the
        links and write the audit entry, so a failure in between left the
        account "suspended" with a live link and no record of who suspended
        it, and reactivating it (which returns a never-activated account to
        invited) brought that link back to life.

        ``dies_at`` is the step that fails: killing the links, or revoking
        the sessions, which is now the last step and the one that commits.
        """
        user_id, _, token = await live.invite()

        with monkeypatch.context() as patched:
            _fail(patched, dies_at)
            failed = await live.api.post(f"{USERS}/{user_id}/deactivate", headers=live.headers)
        assert failed.status_code == 500
        _assert_carries_no_credential(failed, token)

        # Nothing of the suspension was committed: not the status, and not
        # an audit entry for a suspension that did not happen.
        async with AsyncSession(live.engine) as session:
            assert (await _account(session, user_id))[0] is UserStatus.INVITED, (
                "the failed suspension was half committed"
            )
            assert await _live_token_hashes(session, user_id) == {hash_token(token)}
            assert await _audited(session, user_id, "user.deactivated") == 0

        # The administrator tries again, and this time it takes: all of it.
        suspended = await live.api.post(f"{USERS}/{user_id}/deactivate", headers=live.headers)
        assert suspended.status_code == 200, suspended.text
        async with AsyncSession(live.engine) as session:
            assert (await _account(session, user_id))[0] is UserStatus.SUSPENDED
            assert await _live_token_rows(session, user_id) == 0
            assert await _audited(session, user_id, "user.deactivated") == 1
        assert (await _redeem(live.api, token)).status_code == 401
        reactivated = await live.api.post(f"{USERS}/{user_id}/reactivate", headers=live.headers)
        assert reactivated.status_code == 200
        assert (await _redeem(live.api, token)).status_code == 401, "reactivation revived the link"
        async with AsyncSession(live.engine) as session:
            await _assert_never_activated(session, user_id)

    @pytest.mark.parametrize("dies_at", ["links", "sessions"])
    async def test_a_reactivation_that_fails_half_way_reactivates_nothing(
        self, live: _Live, monkeypatch: pytest.MonkeyPatch, dies_at: str
    ) -> None:
        """Attack: an account comes back without its old links having been killed.

        Reactivation changes the status, kills every outstanding link and
        writes the audit entry in one transaction. If the status change were
        committed on its own, a failure straight after would leave the
        account usable again with whatever links were still out.
        """
        user_id, _, _ = await live.invite()
        suspended = await live.api.post(f"{USERS}/{user_id}/deactivate", headers=live.headers)
        assert suspended.status_code == 200, suspended.text
        # A link that outlived the suspension, which reactivation must kill.
        async with AsyncSession(live.engine) as session:
            survivor = await _plant_live_token(session, user_id)
            await session.commit()

        with monkeypatch.context() as patched:
            _fail(patched, dies_at)
            failed = await live.api.post(f"{USERS}/{user_id}/reactivate", headers=live.headers)
        assert failed.status_code == 500

        async with AsyncSession(live.engine) as session:
            assert (await _account(session, user_id))[0] is UserStatus.SUSPENDED, (
                "the failed reactivation was half committed"
            )
            assert await _audited(session, user_id, "user.reactivated") == 0
        # Still suspended, so the surviving link is refused...
        assert (await _redeem(live.api, survivor)).status_code == 401
        # ...and a reactivation that does go through leaves it dead.
        reactivated = await live.api.post(f"{USERS}/{user_id}/reactivate", headers=live.headers)
        assert reactivated.status_code == 200, reactivated.text
        async with AsyncSession(live.engine) as session:
            assert (await _account(session, user_id))[0] is UserStatus.INVITED
            assert await _live_token_rows(session, user_id) == 0
        assert (await _redeem(live.api, survivor)).status_code == 401
        async with AsyncSession(live.engine) as session:
            await _assert_never_activated(session, user_id)


def _fail(patch: pytest.MonkeyPatch, step: str) -> None:
    """Make one step of a suspension or reactivation raise.

    :param patch: Where to apply it, so the caller decides when it ends.
    :param step: ``"links"`` for invalidating the outstanding tokens, or
        ``"sessions"`` for revoking the refresh tokens.
    """

    async def _dies(*args: Any, **kwargs: Any) -> int:
        msg = "the database went away"
        raise RuntimeError(msg)

    if step == "links":
        patch.setattr(PasswordResetTokenRepository, "invalidate_all_for_user", _dies)
    else:
        patch.setattr(RefreshTokenRepository, "revoke_all_for_user", _dies)


class TestARefusedLinkStaysDead:
    """Attack: keep a refused link, wait for the account to be reinstated, use it then.

    A link presented while its account cannot use it (suspended, or in a
    hospital that has been switched off) is refused exactly like a bad token.
    The refusal used to undo itself: the token was marked used in a
    transaction that was then rolled back, so the same link worked again the
    moment the account or hospital came back. Only real commits show this, so
    these run on their own connections.
    """

    @pytest.mark.parametrize("reason", ["suspended", "inactive_hospital"])
    async def test_a_link_refused_for_an_unusable_account_does_not_revive_with_it(
        self, live: _Live, reason: str
    ) -> None:
        user_id, email, token = await live.invite()

        async def _switch(*, usable: bool) -> None:
            """Take the account out of use, or put it back, behind the application's back.

            Directly, so that nothing but the refused redemption itself can
            be what killed the link: the suspend and reactivate endpoints
            invalidate links on their own.
            """
            async with AsyncSession(live.engine) as session:
                if reason == "suspended":
                    await session.execute(
                        update(User)
                        .where(User.id == user_id)
                        .values(status=UserStatus.INVITED if usable else UserStatus.SUSPENDED)
                    )
                else:
                    await session.execute(
                        update(Hospital)
                        .where(Hospital.id == live.hospital_id)
                        .values(is_active=usable)
                    )
                await session.commit()

        await _switch(usable=False)
        async with AsyncSession(live.engine) as session:
            assert await _live_token_hashes(session, user_id) == {hash_token(token)}, "premise"

        refused = await _redeem(live.api, token)
        unknown = await _redeem(live.api, generate_opaque_token()[0])

        assert refused.status_code == 401, refused.text
        # Indistinguishable from a token that never existed.
        assert refused.json()["message"] == unknown.json()["message"]
        assert refused.json().get("error_code") == unknown.json().get("error_code")
        # The refusal is committed: the token is spent.
        async with AsyncSession(live.engine) as session:
            assert await _live_token_rows(session, user_id) == 0, "the refused link is still live"
            await _assert_never_activated(session, user_id)

        await _switch(usable=True)

        again = await _redeem(live.api, token, OTHER_PASSWORD)
        assert again.status_code == 401, "a refused link came back to life with the account"
        async with AsyncSession(live.engine) as session:
            await _assert_never_activated(session, user_id)
            assert (await _account(session, user_id))[0] is UserStatus.INVITED
            assert await _live_token_rows(session, user_id) == 0
        assert (await _login(live.api, email)).status_code == 401
        assert (await _login(live.api, email, OTHER_PASSWORD)).status_code == 401
        # The account is not lost: a new invitation, sent on purpose, works.
        resent = await _resend(live.api, live.headers, user_id)
        assert resent.status_code == 200, resent.text
        async with AsyncSession(live.engine) as session:
            fresh = await _newest_token(session, email, {token})
        assert (await _redeem(live.api, fresh)).status_code == 200
