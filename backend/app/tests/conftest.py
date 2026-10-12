"""Shared pytest fixtures for the backend test suite.

``asyncio_mode = "auto"`` is set in ``pyproject.toml``, so ``async def`` tests
need no decorator.

Three families of fixtures live here:

**Application** (``app``, ``async_client``, ``override_settings``) — for API
tests that drive the ASGI app without starting a server.

**Database** (``db_engine``, ``db_session``, ``uow``, ``hospital_id``,
``actor_id``) — require a real PostgreSQL, because the schema uses JSONB,
native enums, partial indexes, and ``SELECT ... FOR UPDATE``, none of which
SQLite emulates (``docs/11-TESTING_STRATEGY.md`` §2.2). When no database is
reachable they skip rather than fail, so unit tests still run on a laptop with
no Docker. Point them at a database with ``TEST_DATABASE_URL``; the default is
the configured URL with a ``_test`` suffix.

**Test doubles** (``mock_repository``, :class:`FakeSession`,
:class:`RecordingAuditSink`) — for unit tests that need no database at all.

Each database test runs inside an outer transaction that is rolled back on
teardown, so tests never see each other's rows and ordering never matters
(``docs/11-TESTING_STRATEGY.md`` §4.3).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator
    from types import TracebackType

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

    from app.core.audit import AuditEvent
    from app.database.unit_of_work import UnitOfWork

__all__ = [
    "REAL_NETWORK_ATTEMPTS",
    "AdmitAllThrottle",
    "FakeSession",
    "every_log_line_reaches_the_root",
    "RecordingAuditSink",
    "grant_permissions",
    "real_throttle",
]

# The developer's real Groq key lives in ``backend/.env`` and pytest runs from
# ``backend/``. An environment variable outranks the dotenv file, so blanking
# it here — before anything imports ``app.core.config`` — keeps the real key
# out of the settings singleton for the whole run. The autouse fixtures below
# then hold every individual test to "AI off, no network".
os.environ["GROQ_API_KEY"] = ""

# MFA secrets are encrypted at rest and there is deliberately no default key
# (``app/core/config.py``). The suite gets a fresh random one per run, set the
# same way and for the same reason as the line above: before the settings
# singleton is built, and outranking any real key in the developer's shell or
# ``backend/.env`` — a test must never encrypt or decrypt with a real key.
os.environ["MFA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["MFA_ENCRYPTION_PREVIOUS_KEYS"] = ""

# Failed logins are padded to a minimum duration in production so that their
# timing reveals nothing (``AUTH_FAILURE_MIN_SECONDS``). The suite makes
# hundreds of them, so the padding is off here; the tests that are about the
# padding switch it on for themselves.
os.environ["AUTH_FAILURE_MIN_SECONDS"] = "0"

#: Names of every AI setting, removed from the environment for each test.
_AI_ENV_NAMES = (
    "GROQ_API_KEY",
    "AI_ENABLED",
    "AI_FAST_MODEL",
    "GROQ_BASE_URL",
    "AI_REQUEST_TIMEOUT_SECONDS",
    "GROQ_STRICT_JSON_SCHEMA",
    "AI_MAX_CONCURRENT_CALLS",
)

#: Hosts a test tried to reach over a real transport. Filled by
#: :func:`no_real_network`; a test that leaves an entry here fails in teardown.
REAL_NETWORK_ATTEMPTS: list[str] = []


def pytest_configure(config: pytest.Config) -> None:
    """Register the suite's custom markers."""
    config.addinivalue_line(
        "markers",
        "database: test requires a reachable PostgreSQL instance.",
    )


# ── Application ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def app() -> FastAPI:
    """Return the FastAPI application instance (session-scoped)."""
    from app.main import create_app

    return create_app()


# ── Settings ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def override_settings() -> Generator[None]:
    """Override settings for the duration of each test.

    By default, switches to test-friendly values. Individual tests can modify
    settings within their body.

    This fixture is ``autouse`` so every test gets a clean settings context.
    """
    from app.core.config import settings

    original_values: dict[str, Any] = {}

    # Store original values.
    for key in ("APP_ENV", "APP_DEBUG", "LOG_LEVEL", "AUTH_DEVICE_COOKIE_SECURE"):
        original_values[key] = getattr(settings, key, None)

    try:
        # Apply test defaults.
        settings.APP_ENV = "development"  # type: ignore[assignment]
        settings.APP_DEBUG = True
        settings.LOG_LEVEL = "CRITICAL"
        # The suite's HTTP clients talk to ``http://test``, and a ``Secure``
        # cookie is not sent over plain HTTP, so the trusted-device cookie is
        # sent without ``Secure`` here (allowed in development only, which is
        # what the suite runs as). Tests about the cookie's production
        # attributes switch it back on and use an ``https://`` client.
        settings.AUTH_DEVICE_COOKIE_SECURE = False
        yield
    finally:
        # Restore original values.
        for key, value in original_values.items():
            if value is not None:
                setattr(settings, key, value)


@pytest.fixture(autouse=True)
def ai_off_by_default(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """No test sees the developer's real Groq key; each test starts with AI off.

    Three layers, because the real key is on disk next to the test run:

    - the AI variables are removed from the environment, so a key exported in
      the developer's shell reaches no ``Settings`` built in a test;
    - ``Settings`` stops reading ``.env`` for the duration of the test, so
      even a bare ``Settings()`` cannot pick the key up from ``backend/.env``;
    - the imported singleton is pinned to the AI defaults with no key, and the
      cached AI runtime is dropped before and after.

    A test that wants AI on installs a runtime built on a fake transport with
    ``app.tests.ai_fakes.make_runtime``.
    """
    from app.ai import runtime
    from app.core.config import Settings, settings

    for name in _AI_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    monkeypatch.setattr(settings, "AI_ENABLED", None)
    monkeypatch.setattr(settings, "AI_FAST_MODEL", None)
    monkeypatch.setattr(settings, "GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(settings, "AI_REQUEST_TIMEOUT_SECONDS", 8.0)
    monkeypatch.setattr(settings, "GROQ_STRICT_JSON_SCHEMA", True)
    monkeypatch.setattr(settings, "AI_MAX_CONCURRENT_CALLS", 4)
    runtime.reset_ai_runtime()
    yield
    runtime.reset_ai_runtime()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """A test that reaches the real network fails instead of calling a provider.

    The guard records *and* raises, and the fixture fails the test in
    teardown. Raising alone is not enough on the one path the guard exists
    for: the Groq adapter turns any exception from the HTTP call into an
    ordinary "provider unavailable" error, which several tests expect.

    ``ASGITransport`` and ``MockTransport`` are different classes and are
    unaffected. Only the host is recorded — never a header.
    """
    import httpx

    async def _refuse_async(self: object, request: httpx.Request) -> httpx.Response:
        REAL_NETWORK_ATTEMPTS.append(request.url.host)
        msg = "A test tried to open a real network connection."
        raise AssertionError(msg)

    def _refuse_sync(self: object, request: httpx.Request) -> httpx.Response:
        REAL_NETWORK_ATTEMPTS.append(request.url.host)
        msg = "A test tried to open a real network connection."
        raise AssertionError(msg)

    REAL_NETWORK_ATTEMPTS.clear()
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _refuse_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _refuse_sync)
    yield
    attempts = len(REAL_NETWORK_ATTEMPTS)
    REAL_NETWORK_ATTEMPTS.clear()
    assert attempts == 0, "A test tried to open a real network connection."


@pytest.fixture(autouse=True)
def reset_rate_limiters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from the shared rate-limiter state.

    ``app.middleware.rate_limit`` builds its limiters as module-level
    singletons at import time, so their counters are shared by every app
    instance in the process and accumulate across tests. Without this reset the
    suite starts returning 429 partway through, and which tests fail depends on
    execution order.

    The Redis backend is also forced open here. Counters are tracked in Redis in
    production (``docs/06-API_STANDARDS.md`` §15), but the suite must not depend
    on a running Redis: without this the first request of every test would pay
    the full connect timeout before falling back to the in-process counter.
    Redis-backed behaviour has its own tests with an explicit double.
    """
    from app.middleware import rate_limit

    rate_limit.reset_limiter_state()
    monkeypatch.setattr(rate_limit, "_redis_is_circuit_open", lambda: True)


# ── Test doubles ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_repository() -> AsyncMock:
    """Return a generic :class:`AsyncMock` for repository injection.

    Usage::

        def test_service(mock_repository):
            mock_repository.get_by_id.return_value = FakeModel()
            service = MyService(mock_repository)
    """
    return AsyncMock()


class _FakeNestedTransaction:
    """Stand-in for the object ``AsyncSession.begin_nested()`` returns.

    Rolls nothing back — unit tests assert on repository calls, not on database
    state. Returning ``False`` from ``__aexit__`` lets exceptions propagate,
    which is what a real savepoint does after rolling back.
    """

    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> Self:
        self._session.savepoints_opened += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc_type is not None:
            self._session.savepoints_rolled_back += 1
        return False


class FakeSession:
    """Minimal :class:`~sqlalchemy.ext.asyncio.AsyncSession` stand-in.

    :class:`~app.services.patient_service.PatientService` holds a session only
    to own the transaction boundary, so a double that counts commits and
    savepoints is enough to assert the boundary is respected — and it makes the
    assertions readable, which mock call-chain inspection does not.
    """

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.savepoints_opened = 0
        self.savepoints_rolled_back = 0

    def begin_nested(self) -> _FakeNestedTransaction:
        """Open a fake savepoint."""
        return _FakeNestedTransaction(self)

    async def commit(self) -> None:
        """Record a commit."""
        self.commits += 1

    async def rollback(self) -> None:
        """Record a rollback."""
        self.rollbacks += 1


@contextlib.contextmanager
def every_log_line_reaches_the_root() -> Generator[None]:
    """Undo, for a while, everything that can keep a log line from the root logger.

    Tests that assert "this secret is in no log line" attach a handler to the
    root logger. That proves nothing if lines never get there — and across a
    whole run they may not: the suite logs at ``CRITICAL``, and any earlier
    test may have set a level on a named logger, switched propagation off or
    called ``logging.disable``. All of that is lifted here and put back after.
    """
    manager = logging.root.manager
    disabled = manager.disable
    saved: dict[logging.Logger, tuple[int, bool, bool]] = {}
    logging.disable(logging.NOTSET)
    for candidate in list(manager.loggerDict.values()):
        if isinstance(candidate, logging.Logger):
            saved[candidate] = (candidate.level, candidate.propagate, candidate.disabled)
            candidate.setLevel(logging.NOTSET)
            candidate.propagate = True
            candidate.disabled = False
    previous_root = logging.root.level
    logging.root.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        logging.root.setLevel(previous_root)
        for candidate, (level, propagate, was_disabled) in saved.items():
            candidate.setLevel(level)
            candidate.propagate = propagate
            candidate.disabled = was_disabled
        logging.disable(disabled)


class AdmitAllThrottle:
    """An authentication throttle that lets everything through and remembers it.

    For unit tests of ``AuthService`` that are about something else. It is a
    test double only: ``AuthService`` cannot be built without a throttle, and
    the application always gives it the real one.
    """

    def __init__(self) -> None:
        self.admitted: list[list[Any]] = []
        self.settled: list[tuple[Any, list[Any]]] = []
        self.refuse = False

    async def now(self) -> Any:
        """The current time."""
        from datetime import UTC, datetime

        return datetime.now(UTC)

    async def admit(self, targets: Any) -> Any:
        """Admit (or, with ``refuse`` set, refuse) and record the buckets asked for."""
        from app.services.auth_throttle import Admission

        self.admitted.append(list(targets))
        if self.refuse:
            return Admission(admitted=False, refused_by=targets[0].kind)
        return Admission(admitted=True)

    async def settle(self, admission: Any, *, clear: Any = ()) -> None:
        """Record a settlement."""
        self.settled.append((admission, list(clear)))


def real_throttle(session: AsyncSession, uow: UnitOfWork) -> dict[str, Any]:
    """The real throttle and trusted-device store on a test session.

    :returns: Keyword arguments for ``AuthService(...)``.
    """
    from app.repositories.auth_throttle_repository import (
        AuthThrottleRepository,
        TrustedDeviceRepository,
    )
    from app.services.auth_throttle import AuthThrottle

    return {
        "throttle": AuthThrottle(AuthThrottleRepository(session), uow),
        "trusted_devices": TrustedDeviceRepository(session),
    }


class RecordingAuditSink:
    """:class:`~app.core.audit.AuditSink` that keeps events in memory.

    Lets tests assert that every mutation produced the right audit event —
    required by the backend Definition of Done and AC-5 of the module spec.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        """Store the event for later assertion."""
        self.events.append(event)

    def actions(self) -> list[str]:
        """Return the action name of every recorded event, in order."""
        return [event.action for event in self.events]

    def last(self) -> AuditEvent:
        """Return the most recent event.

        :raises AssertionError: If no event was recorded — a clearer failure
            than an ``IndexError`` three frames deep.
        """
        assert self.events, "Expected at least one audit event, but none were recorded."
        return self.events[-1]


@pytest.fixture
def mock_uow() -> AsyncMock:
    """Return a mocked :class:`~app.database.unit_of_work.UnitOfWork`.

    Services own the transaction boundary, so unit tests assert that a write
    path committed by checking ``mock_uow.commit.await_count``.
    """
    return AsyncMock()


@pytest.fixture
def fake_session() -> FakeSession:
    """Provide a fresh :class:`FakeSession` for a unit test."""
    return FakeSession()


@pytest.fixture
def audit_sink() -> RecordingAuditSink:
    """Provide a fresh :class:`RecordingAuditSink` for a unit test."""
    return RecordingAuditSink()


# ── Database ─────────────────────────────────────────────────────────────────


def _test_database_url() -> str:
    """Return the URL of the test database.

    ``TEST_DATABASE_URL`` wins. Otherwise the configured URL is reused with a
    ``_test`` suffix on the database name, so a misconfigured run cannot write
    to a developer's working data.
    """
    from app.core.config import settings

    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit
    base, _, database = settings.DATABASE_URL.rpartition("/")
    return f"{base}/{database}_test"


def _run_migrations(database_url: str) -> None:
    """Apply every Alembic migration to the test database.

    Tests run against the migrated schema rather than
    ``Base.metadata.create_all`` so that a migration which does not match the
    models fails the suite instead of passing it
    (``docs/11-TESTING_STRATEGY.md`` §4.2).

    Alembic's ``env.py`` drives an async engine through :func:`asyncio.run`,
    which cannot run inside an already-running event loop — hence the call from
    a worker thread via :func:`asyncio.to_thread`.
    """
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    # migrations/env.py prefers DB_URL from the environment over the ini value.
    previous = os.environ.get("DB_URL")
    os.environ["DB_URL"] = database_url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DB_URL", None)
        else:
            os.environ["DB_URL"] = previous


@pytest_asyncio.fixture(scope="session")
async def db_engine() -> AsyncGenerator[AsyncEngine]:
    """Session-scoped engine bound to the migrated test database.

    Skips every database-backed test when PostgreSQL is unreachable.
    """
    url = _test_database_url()
    engine = create_async_engine(url)

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — any connection failure means "no database"
        await engine.dispose()
        pytest.skip(f"PostgreSQL is not reachable at {url}: {exc}")

    await asyncio.to_thread(_run_migrations, url)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory() -> async_sessionmaker[AsyncSession]:
    """Create an ephemeral async session factory for testing.

    .. caution::

        This connects to ``settings.DATABASE_URL`` and does **not** wrap the
        session in a rolled-back transaction. Prefer :func:`db_session`, which
        is isolated and points at the dedicated test database.
    """
    from app.core.config import settings
    from app.database import create_session_factory, initialize_database

    initialize_database(database_url=settings.DATABASE_URL)
    return create_session_factory(pool_size=5, max_overflow=0, echo=False)


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    """Provide a session whose writes are discarded when the test ends.

    The session joins an outer transaction that is rolled back on teardown.
    ``join_transaction_mode="create_savepoint"`` means a ``commit()`` inside the
    code under test releases a savepoint rather than committing for real, so
    services can own their transaction boundary honestly and the test still
    leaves no rows behind.

    This matters for more than tidiness: a plain ``session.rollback()`` on
    teardown does not undo work a service already committed, so any service
    that commits — as :class:`~app.services.patient_service.PatientService`
    does — would otherwise leave rows in the database permanently.
    """
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest_asyncio.fixture
async def uow(db_session: AsyncSession) -> UnitOfWork:
    """Yield a :class:`UnitOfWork` bound to the test-scoped session."""
    from app.database.unit_of_work import UnitOfWork

    return UnitOfWork(db_session)


# ── HTTP client ──────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def async_client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Yield an ``httpx.AsyncClient`` configured against the FastAPI app.

    Sends real HTTP requests through ASGI without starting a server.

    Usage::

        async def test_health(async_client: AsyncClient):
            response = await async_client.get("/api/v1/health/live")
            assert response.status_code == 200
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── Domain fixtures ──────────────────────────────────────────────────────────


async def _create_hospital(session: AsyncSession, *, slug: str) -> uuid.UUID:
    """Insert a minimal hospital row and return its UUID."""
    from app.models.hospital import Hospital

    hospital = Hospital(
        id=uuid.uuid4(),
        name=f"Test Hospital {slug}",
        slug=slug,
        address={"line1": "1 Test Road", "city": "Hyderabad", "country": "IN"},
        settings={},
    )
    session.add(hospital)
    await session.flush()
    return hospital.id


@pytest_asyncio.fixture
async def hospital_id(db_session: AsyncSession) -> uuid.UUID:
    """Create a hospital and return its UUID.

    Every patient row needs a real ``hospital_id``: the foreign key is
    ``NOT NULL`` with ``ON DELETE RESTRICT``.
    """
    return await _create_hospital(db_session, slug=f"test-{uuid.uuid4().hex[:12]}")


@pytest_asyncio.fixture
async def other_hospital_id(db_session: AsyncSession) -> uuid.UUID:
    """Create a second hospital, for multi-tenant isolation tests."""
    return await _create_hospital(db_session, slug=f"other-{uuid.uuid4().hex[:12]}")


async def grant_permissions(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    user_id: uuid.UUID,
    codes: list[str],
) -> None:
    """Give a user a role carrying exactly ``codes``.

    ``require_permission`` resolves permissions from the database — it walks
    ``user_roles → role_permissions → permission.code`` — not from the JWT
    claims. A token minted with permission claims therefore proves nothing;
    the rows have to exist.

    :param session: The test session.
    :param hospital_id: Hospital the role belongs to.
    :param user_id: User to grant the role to.
    :param codes: Permission codes to include, e.g. ``["patient.read"]``.
    """
    from app.models.permission import Permission
    from app.models.role import Role, RolePermission
    from app.models.user import User, UserRole

    # Resolve every permission row first, because creating one needs a flush and
    # flushing would make `role` below persistent before its graph is built.
    permissions: list[Permission] = []
    for code in codes:
        # Permission codes are globally unique and seeded once, so reuse an
        # existing row when the suite has already created it.
        existing = await session.execute(select(Permission).where(Permission.code == code))
        permission = existing.unique().scalar_one_or_none()
        if permission is None:
            permission = Permission(
                id=uuid.uuid4(), code=code, module=code.split(".")[0], description=code
            )
            session.add(permission)
            await session.flush()
        permissions.append(permission)

    # Build the role's graph while it is still *pending*. Touching a collection
    # on a pending object never emits SQL; on a flushed (persistent) one it
    # triggers a lazy load, which in async SQLAlchemy raises MissingGreenlet
    # because plain attribute access cannot await IO.
    role = Role(id=uuid.uuid4(), hospital_id=hospital_id, name=f"test-role-{uuid.uuid4().hex[:8]}")
    for permission in permissions:
        # `permission` is passed as an object, not just an id, so both
        # `role.role_permissions` and `RolePermission.permission` are correct in
        # memory as well as on disk.
        role.role_permissions.append(
            RolePermission(id=uuid.uuid4(), permission_id=permission.id, permission=permission)
        )
    session.add(role)
    await session.flush()

    session.add(UserRole(id=uuid.uuid4(), user_id=user_id, role_id=role.id))
    await session.flush()

    # Re-load the user's role collection so the in-memory graph matches the rows.
    #
    # The UserRole above is inserted by raw foreign key, which leaves any
    # already-cached `User.user_roles` untouched — and `lazy="selectin"` will not
    # re-read a collection it believes is populated. Callers create the user and
    # flush it before calling this helper, so by the time a request resolves
    # permissions the stale value can win.
    #
    # The symptom when this is missing: `require_permission` walks
    # `user.user_roles`, finds it empty, and returns 403 to a user who genuinely
    # holds the permission. It surfaces only for the second and later users built
    # within one test, which makes it read as a flake rather than a bug.
    #
    # `refresh` rather than `expire`: both fix the staleness, but refresh is
    # awaited and leaves the attribute *loaded*, so a later plain attribute
    # access cannot trigger lazy IO and raise MissingGreenlet. Expiring would
    # simply move the problem to whoever touched it next.
    user = await session.get(User, user_id)
    assert user is not None, f"grant_permissions called for unknown user {user_id}"
    await session.refresh(user, ["user_roles"])


@pytest_asyncio.fixture
async def actor_id(db_session: AsyncSession, hospital_id: uuid.UUID) -> uuid.UUID:
    """Create a user to act as the caller, and return its UUID.

    The audit columns (``created_by``, ``updated_by``, ``deleted_by``) are real
    foreign keys to ``users.id``, so an actor UUID that does not exist as a row
    fails the insert. Tests that pass an actor must use this fixture.
    """
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"actor-{uuid.uuid4().hex[:12]}@hospital.test",
        # Not a credential — the column is NOT NULL and nothing under test
        # reads it. Real hashing is the auth module's concern.
        password_hash="test-placeholder-not-a-hash",
        first_name="Test",
        last_name="Actor",
    )
    db_session.add(user)
    await db_session.flush()
    return user.id
