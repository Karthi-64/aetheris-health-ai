"""Async database engine and session factory management.

Provides:

- :func:`initialize_database` — store the database URL for later engine creation.
- :func:`create_session_factory` — build an async session factory.
- :func:`get_async_engine` — retrieve or create the async engine.
- :func:`dispose_engine` — close all connections gracefully.

Usage::

    from app.database import initialize_database, create_session_factory

    # Called once during application startup:
    initialize_database(database_url="postgresql+asyncpg://...")
    factory = create_session_factory()

    # In a service or route:
    async with factory() as session:
        result = await session.execute(...)
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

_engine: AsyncEngine | None = None
_database_url: str | None = None


def initialize_database(database_url: str | None = None) -> None:
    """Store the database URL for later engine creation.

    Call this once during application startup, **before** calling
    :func:`create_session_factory`.

    :param database_url: PostgreSQL async connection string.
        Defaults to ``settings.DATABASE_URL``.
    """
    global _database_url  # noqa: PLW0603
    _database_url = database_url or settings.DATABASE_URL


def get_async_engine() -> AsyncEngine:
    """Return the global async database engine, creating it if necessary.

    The engine is created lazily on first access. This ensures the
    application can start even if the database is temporarily unavailable.

    :returns: The global :class:`AsyncEngine` instance.
    :raises RuntimeError: If :func:`initialize_database` was not called first.
    """
    global _engine, _database_url  # noqa: PLW0603, PLW0602

    if _engine is not None:
        return _engine

    if _database_url is None:
        msg = "initialize_database() must be called before accessing the engine."
        raise RuntimeError(msg)

    _engine = create_async_engine(
        url=_database_url,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
        echo=settings.DATABASE_ECHO,
        # Bound parameters are kept out of SQL logging and out of the text of
        # database errors. They carry password hashes, token hashes, email
        # bodies with one-time links, and patient data; an error message that
        # prints them puts all of that in the application log.
        hide_parameters=True,
        pool_pre_ping=True,
        pool_recycle=3600,
        connect_args={
            "server_settings": {
                "timezone": "UTC",
                "application_name": settings.APP_NAME,
            },
        },
    )

    return _engine


def create_session_factory(
    pool_size: int | None = None,
    max_overflow: int | None = None,
    echo: bool | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the global engine.

    :param pool_size: Override the default connection pool size.
    :param max_overflow: Override the default max overflow.
    :param echo: Override the default SQL echo setting.
    :returns: An :class:`async_sessionmaker` configured for :class:`AsyncSession`.
    """
    engine = get_async_engine()

    # If custom pool settings are provided, create a new engine with them.
    if pool_size is not None or max_overflow is not None or echo is not None:
        global _database_url  # noqa: PLW0602
        engine = create_async_engine(
            url=_database_url or settings.DATABASE_URL,
            pool_size=pool_size or settings.DATABASE_POOL_SIZE,
            max_overflow=max_overflow or settings.DATABASE_MAX_OVERFLOW,
            echo=echo if echo is not None else settings.DATABASE_ECHO,
            hide_parameters=True,
            pool_pre_ping=True,
            pool_recycle=3600,
        )

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def dispose_engine() -> None:
    """Dispose of the global async engine, closing all connections.

    Call this during application shutdown. Safe to call multiple times.
    """
    global _engine  # noqa: PLW0603
    if _engine is not None:
        await _engine.dispose()
        _engine = None
