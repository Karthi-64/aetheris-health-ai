"""Contract tests for the Arq worker settings.

Arq never *calls* anything on ``WorkerSettings`` — ``arq.worker.get_kwargs``
reads the raw class ``__dict__`` and hands the values straight to the library.
So a shape that looks fine to a reader (a ``@staticmethod`` returning the right
object) is a boot failure, and one no request-path test can see: ``__dict__``
holds the descriptor, not what it would return.

These read the class exactly the way arq does. They deliberately do not call
``get_kwargs`` itself, whose stub annotates its return as
``dict[str, NameError]`` and so cannot be used under ``mypy --strict``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arq.connections import RedisSettings

from app.background.worker import WorkerSettings, sweep_no_shows

if TYPE_CHECKING:
    import pytest


def _as_arq_sees_it() -> dict[str, object]:
    """The class attributes, read from ``__dict__`` the way arq reads them."""
    return dict(vars(WorkerSettings))


def test_redis_settings_is_an_instance_arq_can_connect_with() -> None:
    """Regression: a staticmethod here crashed the worker on boot.

    ``arq.connections.create_pool`` reads ``.host`` straight off this value, so
    it has to be a ``RedisSettings`` — not a callable that returns one.
    """
    redis_settings = _as_arq_sees_it()["redis_settings"]

    assert isinstance(redis_settings, RedisSettings)
    assert redis_settings.host


def test_the_no_show_sweeper_is_registered_and_scheduled() -> None:
    """The sweeper must be both callable by name and on the cron schedule."""
    settings = _as_arq_sees_it()
    functions = settings["functions"]
    cron_jobs = settings["cron_jobs"]

    assert isinstance(functions, list)
    assert sweep_no_shows in functions
    assert isinstance(cron_jobs, list)
    assert any(job.coroutine is sweep_no_shows for job in cron_jobs)


async def test_the_sweeper_records_to_the_durable_audit_trail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status change made by the system belongs in ``audit_logs``.

    The sweeper used to be wired to the log-only sink, so the no-shows it
    marked never reached the compliance trail even after every API path did
    (PR #29 re-review).
    """
    from app.services import appointment_service
    from app.services.audit_service import AuditService

    captured: dict[str, object] = {}

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class _RecordingService:
        def __init__(self, *args: object) -> None:
            captured["audit"] = args[5]

        async def sweep_no_shows(self) -> int:
            return 3

    monkeypatch.setattr("app.database.create_session_factory", lambda: _FakeSession)
    monkeypatch.setattr(appointment_service, "AppointmentService", _RecordingService)

    swept = await sweep_no_shows({})

    assert swept == 3
    assert isinstance(captured["audit"], AuditService)
