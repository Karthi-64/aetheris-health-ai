"""Arq worker definition — the process that runs scheduled backend jobs.

Run with ``make worker`` (``arq app.background.worker.WorkerSettings``).

Arq rather than Celery because the stack is async end to end
(``backend/CLAUDE.md`` sanctions either): Celery would need a sync bridge
around every ``AsyncSession``, and the Redis connection Arq brokers over is
already configured as :attr:`~app.core.config.Settings.REDIS_URL`.

**This process is separate from the API.** It opens its own database sessions
and does not share the request-scoped ones, so a long sweep cannot hold a
connection the web workers need.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = get_logger(__name__)

#: How often the no-show sweeper runs (module spec §5.7). NFR: the sweeper
#: must not lag more than 10 minutes behind real time, so five minutes leaves
#: room for one missed run before that budget is breached.
NO_SHOW_SWEEP_MINUTES = 5


async def startup(ctx: dict[str, Any]) -> None:
    """Prepare the worker process.

    Logging is configured here rather than inherited: the worker does not go
    through :func:`app.main.create_app`, so without this its output would not
    be structured like the rest of the platform.

    :param ctx: Arq job context.
    """
    configure_logging()
    from app.database import initialize_database

    initialize_database(database_url=settings.DATABASE_URL)
    logger.info("worker_started", jobs=["sweep_no_shows"])


async def shutdown(ctx: dict[str, Any]) -> None:
    """Tear the worker process down cleanly.

    :param ctx: Arq job context.
    """
    logger.info("worker_stopped")


async def sweep_no_shows(ctx: dict[str, Any]) -> int:
    """Mark overdue appointments as no-shows (module spec §5.7, FR-7).

    A thin wrapper: all the logic lives in
    :meth:`~app.services.appointment_service.AppointmentService.sweep_no_shows`
    so it is testable without Redis or a running worker. This function only
    owns the session lifecycle.

    :param ctx: Arq job context.
    :returns: How many appointments were marked.
    """
    from app.core.audit import StructlogAuditSink
    from app.database import create_session_factory
    from app.repositories.appointment_repository import AppointmentRepository
    from app.repositories.doctor_repository import DoctorRepository
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.patient_repository import PatientRepository
    from app.services.appointment_service import AppointmentService, NullInvoiceDraftSink

    factory = create_session_factory()
    async with factory() as session:
        service = AppointmentService(
            AppointmentRepository(session),
            PatientRepository(session),
            DoctorRepository(session),
            HospitalRepository(session),
            session,
            StructlogAuditSink(),
            NullInvoiceDraftSink(),
        )
        swept = await service.sweep_no_shows()

    logger.info("job_completed", job="sweep_no_shows", swept=swept)
    return swept


class WorkerSettings:
    """Arq worker configuration.

    ``arq`` discovers this class by name, so the module path in ``make worker``
    is the whole wiring.
    """

    functions: list[Callable[..., Coroutine[Any, Any, Any]]] = [sweep_no_shows]

    cron_jobs = [
        # Every five minutes, on the minute.
        cron(
            sweep_no_shows,
            minute=set(range(0, 60, NO_SHOW_SWEEP_MINUTES)),
            run_at_startup=False,
        )
    ]

    on_startup = startup
    on_shutdown = shutdown

    #: Broker connection, taken from the same URL the app uses.
    #:
    #: Arq reads this straight out of the class ``__dict__`` and passes it to
    #: ``create_pool``, so it must be a :class:`RedisSettings` *instance*. It
    #: was once a ``@staticmethod`` returning one, which handed arq the
    #: descriptor instead — the worker crashed on boot with ``'staticmethod'
    #: object has no attribute 'host'`` and the sweeper never ran.
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
