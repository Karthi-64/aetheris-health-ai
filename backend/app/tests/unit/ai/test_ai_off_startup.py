"""The application imports and starts with AI off.

AI is optional infrastructure. With no key the app must build, serve its
health probe, and report AI as not configured — without attempting any
outbound call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.ai.runtime import get_ai_runtime
from app.tests import conftest
from app.tests.ai_fakes import FAKE_GROQ_KEY

if TYPE_CHECKING:
    import pytest


async def test_app_starts_and_serves_health_with_no_ai_key() -> None:
    from app.core.config import settings
    from app.main import create_app

    assert settings.GROQ_API_KEY is None

    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    status = get_ai_runtime().status
    assert status.configured is False
    assert status.reason == "no_api_key"
    assert status.provider is None
    assert get_ai_runtime().provider is None
    assert conftest.REAL_NETWORK_ATTEMPTS == []


async def test_app_starts_with_the_kill_switch_off_even_when_a_key_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "GROQ_API_KEY", SecretStr(FAKE_GROQ_KEY))
    monkeypatch.setattr(settings, "AI_ENABLED", False)

    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    status = get_ai_runtime().status
    assert status.configured is False
    assert status.reason == "disabled_by_setting"
    assert conftest.REAL_NETWORK_ATTEMPTS == []


def test_the_suite_never_holds_a_real_key() -> None:
    """The safety net itself: whatever ``backend/.env`` holds, tests see no key."""
    import os

    from app.core.config import Settings, settings

    assert settings.GROQ_API_KEY is None
    assert "GROQ_API_KEY" not in os.environ
    # Even a bare Settings() — which would normally read ``.env`` — finds none.
    assert Settings().GROQ_API_KEY is None
    assert Settings.model_config.get("env_file") is None
