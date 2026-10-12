"""Application lifecycle management.

Handles startup (create engine, configure logging, warm caches)
and shutdown (close connections, flush logs) events.

Used by :mod:`app.main` via FastAPI's ``lifespan`` parameter.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog

from app.ai.runtime import close_ai_runtime, get_ai_runtime
from app.core.config import settings
from app.core.email import email_delivery_configured
from app.core.redis import close_redis_client
from app.core.sms import get_sms_sender
from app.database import create_session_factory, dispose_engine, initialize_database

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:  # noqa: ARG001
    """Application lifespan context manager.

    Called by FastAPI on startup and shutdown.

    **Startup sequence:**
    1. Initialize the async database engine and session factory.
    2. Verify database connectivity (warm the pool).
    3. Build the AI runtime from settings and log whether AI is configured.
       No provider is contacted; with no key the application starts normally.
    4. Log startup confirmation.

    **Notes:**
    - Structured logging is configured in :func:`app.main.create_app` before the lifespan runs.

    **Shutdown sequence:**
    1. Close the AI provider's HTTP client, if one was created.
    2. Dispose the database engine (close all connections).
    3. Log shutdown confirmation.
    """
    # ── Startup ──────────────────────────────────────────────────────────
    # Logging was already configured by create_app() in main.py.

    logger.info(
        "application_startup",
        environment=settings.APP_ENV,
        debug=settings.APP_DEBUG,
        database_pool_size=settings.DATABASE_POOL_SIZE,
    )

    initialize_database(database_url=settings.DATABASE_URL)
    _session_factory = create_session_factory(
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
        echo=settings.DATABASE_ECHO,
    )

    # Store the session factory on the app state for dependency injection.
    app.state.db_session_factory = _session_factory

    try:
        # Warm the connection pool by making a single connectivity check.
        from sqlalchemy import text

        async with _session_factory() as session:
            await session.execute(text("SELECT 1"))
            logger.info("database_connection_verified")
    except Exception as exc:
        logger.error("database_connection_failed", error=str(exc))
        raise

    # Built here only so the status line appears at startup. It makes no
    # network call and never raises: AI being off or misconfigured must not
    # stop the application from starting.
    get_ai_runtime()

    if settings.secret_key_is_ephemeral:
        # Development only — staging and production cannot reach this line.
        logger.warning(
            "app_secret_key_ephemeral",
            detail=(
                "No private APP_SECRET_KEY is configured, so a random signing key was "
                "generated for this process. Sessions end when it restarts."
            ),
        )

    if not email_delivery_configured():
        # Invitations and password resets travel by email and by no other
        # route. Without a transport, nobody can be invited or recover an
        # account; say so once, loudly, rather than let it be discovered.
        logger.warning(
            "email_delivery_not_configured",
            detail=(
                "SMTP_HOST is not set. Invitation and password-reset links "
                "cannot be delivered, and none will be issued."
            ),
        )

    if settings.patient_otp_secret_is_ephemeral:
        # Development only — staging and production cannot reach this line.
        logger.warning(
            "patient_otp_secret_ephemeral",
            detail=(
                "No PATIENT_OTP_SECRET is configured, so a random key was generated for "
                "this process. Patient sign-in codes already sent stop working when it restarts."
            ),
        )

    if get_sms_sender() is None:
        # Patient App sign-in codes travel by SMS and by no other route.
        logger.info(
            "sms_delivery_not_configured",
            detail=(
                "SMS_PROVIDER is not set. Patient App sign-in codes cannot be sent, "
                "and none will be issued."
            ),
        )

    logger.info(
        "application_started",
        app_name=settings.APP_NAME,
        base_url=settings.APP_BASE_URL,
    )

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    logger.info("application_shutdown_started")

    # Redis is created lazily on first use (rate limiting, health probes), so
    # this is a no-op when nothing ever touched it.
    await close_redis_client()

    await close_ai_runtime()

    await dispose_engine()

    logger.info("application_shutdown_complete")
