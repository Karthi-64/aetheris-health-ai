"""Live check of the Groq configuration. Run by a person, never by a test.

This script talks to the real Groq API with the key in ``backend/.env``. It
reads the key only through ``app.core.config.settings`` and never prints it,
its length, a header, a request object or an exception message — on any
failure it prints an exception *class name* or a typed error ``kind`` only.

Usage (from ``backend/`` so that ``.env`` resolves)::

    cd backend && uv run python ../scripts/verify_groq.py models
    cd backend && uv run python ../scripts/verify_groq.py complete

``models``
    Asks Groq which models it serves and reports whether the model the
    ``fast`` hint resolves to (``AI_FAST_MODEL``, else the repository mapping)
    is among them. Exit code 0 only when it is.

``complete``
    Builds the AI runtime exactly as the application does and asks for one
    slot suggestion over a synthetic three-slot day (no patient, no database).
    Exit code 0 only when a valid suggestion came back.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Importable when run as ``python ../scripts/verify_groq.py`` from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import httpx  # noqa: E402

from app.ai.constants import DEFAULT_HINT_MAPPING, ModelHint  # noqa: E402
from app.ai.errors import AIError  # noqa: E402
from app.ai.runtime import build_ai_runtime  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.services.appointment_service import DaySlot  # noqa: E402
from app.services.slot_ranker import AISlotRanker  # noqa: E402


def _fast_model() -> str:
    """The model the ``fast`` hint resolves to, as the runtime resolves it."""
    return settings.AI_FAST_MODEL or DEFAULT_HINT_MAPPING[ModelHint.FAST][1]


async def check_models() -> int:
    """Report whether Groq serves the configured fast model."""
    if settings.GROQ_API_KEY is None:
        print("GROQ_API_KEY is not set: nothing to check.")
        return 2
    model = _fast_model()
    print(f"configured fast model: {model}")

    served: list[str] = []
    status: int | None = None
    failure: str | None = None
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            response = await client.get(
                f"{settings.GROQ_BASE_URL}/models",
                headers={"Authorization": f"Bearer {settings.GROQ_API_KEY.get_secret_value()}"},
                timeout=10.0,
            )
        status = response.status_code
        if status == httpx.codes.OK:
            data = response.json().get("data", [])
            served = sorted(
                str(item["id"]) for item in data if isinstance(item, dict) and "id" in item
            )
    except Exception as exc:  # noqa: BLE001 — only the class name is ever shown
        failure = type(exc).__name__

    if failure is not None:
        print(f"request failed: {failure}")
        return 1
    print(f"HTTP status: {status}")
    if status != httpx.codes.OK:
        return 1
    is_served = model in served
    print(f"served by Groq: {'yes' if is_served else 'no'}")
    if not is_served:
        # Model ids are public; they are listed to help choose a replacement.
        print("served model ids:")
        for name in served:
            print(f"  {name}")
    return 0 if is_served else 1


class _Recorder:
    """Keeps the ``ai_interaction`` log entry so its counters can be printed."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.entries.append({"event": event, **fields})

    warning = info
    error = info
    debug = info


async def check_complete() -> int:
    """Ask for one slot suggestion over a synthetic day, through the runtime."""
    import app.ai.services.ai_service as ai_service_module

    recorder = _Recorder()
    ai_service_module.logger = recorder  # type: ignore[assignment]

    runtime = build_ai_runtime(settings)
    if not runtime.status.configured:
        print(f"AI is not configured: {runtime.status.reason}")
        return 2

    zone = ZoneInfo("Asia/Kolkata")
    target = date(2030, 1, 7)

    def slot(hour: int, minute: int, slot_id: str | None) -> DaySlot:
        start = datetime(2030, 1, 7, hour, minute, tzinfo=zone)
        return DaySlot(start=start, end=start + timedelta(minutes=30), slot_id=slot_id)

    day = [
        slot(9, 0, "S1"),
        slot(9, 30, None),
        slot(10, 0, None),
        slot(10, 30, "S2"),
        slot(11, 0, "S3"),
    ]

    failure: str | None = None
    choice = None
    try:
        choice = await AISlotRanker(runtime.service, runtime.prompts).choose_slot(
            hospital_id=uuid.uuid4(),
            actor_id=None,
            request_id="verify-groq",
            target_date=target,
            day=day,
        )
    except AIError as exc:
        failure = f"{type(exc).__name__} kind={exc.kind}"
    except Exception as exc:  # noqa: BLE001 — only the class name is ever shown
        failure = type(exc).__name__
    finally:
        if runtime.provider is not None:
            await runtime.provider.aclose()

    interaction = next((e for e in recorder.entries if e["event"] == "ai_interaction"), {})
    print(f"provider: {interaction.get('provider')}")
    print(f"requested model: {interaction.get('model')}")
    print(f"returned model: {interaction.get('response_model')}")
    print(f"finish reason: {interaction.get('finish_reason')}")
    print(f"prompt tokens: {interaction.get('input_tokens')}")
    print(f"completion tokens: {interaction.get('output_tokens')}")
    print(f"latency ms: {interaction.get('latency_ms')}")
    print(f"status: {interaction.get('status')}")
    if failure is not None or choice is None:
        print(f"FAILED: {failure}")
        return 1
    print(f"chosen slot_id: {choice.slot_id}")
    # Synthetic schedule text only: no patient or hospital data was sent.
    print(f"reason: {choice.reason}")
    return 0


def main() -> int:
    """Dispatch on the one command-line argument."""
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "models":
        return asyncio.run(check_models())
    if command == "complete":
        return asyncio.run(check_complete())
    print("usage: verify_groq.py models|complete")
    return 2


if __name__ == "__main__":
    sys.exit(main())
