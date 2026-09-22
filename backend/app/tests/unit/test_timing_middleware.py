"""Unit tests for :class:`~app.middleware.timing.TimingMiddleware`.

Tests cover:
- Header presence and format on normal requests
- Header presence on error responses
- Correct timing unit (milliseconds)
- Exception propagation (middleware does not swallow errors)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.constants import RESPONSE_TIME_HEADER
from app.middleware.timing import TimingMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

# ── Helper app factories ────────────────────────────────────────────────────

_HEADER_PATTERN = re.compile(r"^\d+\.\d{2}ms$")


def _ok_app() -> Starlette:
    """Return a minimal ASGI app that returns 200 OK."""
    return _build_app(_ok_handler)


def _error_app() -> Starlette:
    """Return a minimal ASGI app that returns 500."""
    return _build_app(_error_handler)


def _build_app(
    handler: Callable[..., object],
) -> Starlette:
    """Wrap *handler* in a Starlette app with only the timing middleware."""
    return Starlette(
        routes=[Route("/test", endpoint=handler)],
        middleware=[Middleware(TimingMiddleware)],
    )


async def _ok_handler(request: object) -> JSONResponse:  # noqa: ARG001
    """Return a 200 OK response."""
    return JSONResponse({"status": "ok"})


async def _error_handler(request: object) -> JSONResponse:  # noqa: ARG001
    """Return a 500 error response."""
    return JSONResponse({"error": "fail"}, status_code=500)


async def _crash_handler(request: object) -> None:  # noqa: ARG001
    """Handler that raises an exception."""
    msg = "intentional crash"
    raise RuntimeError(msg)


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestTimingMiddleware:
    """Suite of tests for the TimingMiddleware."""

    async def test_response_has_timing_header_on_success(self) -> None:
        """The X-Response-Time header must be present on a successful response."""
        async with AsyncClient(
            transport=ASGITransport(app=_ok_app()),
            base_url="http://test",
        ) as client:
            response = await client.get("/test")

        assert RESPONSE_TIME_HEADER in response.headers

    async def test_timing_header_format(self) -> None:
        """The header value must match NNN.NNms (e.g. ``1.23ms``)."""
        async with AsyncClient(
            transport=ASGITransport(app=_ok_app()),
            base_url="http://test",
        ) as client:
            response = await client.get("/test")

        value = response.headers[RESPONSE_TIME_HEADER]
        assert _HEADER_PATTERN.match(value), f"Expected pattern like '1.23ms', got '{value}'"

    async def test_timing_header_on_error_response(self) -> None:
        """The header must be present even on a 5xx error response."""
        async with AsyncClient(
            transport=ASGITransport(app=_error_app()),
            base_url="http://test",
        ) as client:
            response = await client.get("/test")

        assert response.status_code == 500
        assert RESPONSE_TIME_HEADER in response.headers

    async def test_timing_value_is_non_negative(self) -> None:
        """The measured duration must be >= 0 (a reasonable sanity check)."""
        async with AsyncClient(
            transport=ASGITransport(app=_ok_app()),
            base_url="http://test",
        ) as client:
            response = await client.get("/test")

        value = response.headers[RESPONSE_TIME_HEADER]
        numeric = float(value.replace("ms", ""))
        assert numeric >= 0

    async def test_timing_value_is_reasonable(self) -> None:
        """The measured duration must be < 10s for a trivial handler."""
        async with AsyncClient(
            transport=ASGITransport(app=_ok_app()),
            base_url="http://test",
        ) as client:
            response = await client.get("/test")

        value = response.headers[RESPONSE_TIME_HEADER]
        numeric = float(value.replace("ms", ""))
        assert numeric < 10_000, "Timing seems unreasonably large"

    async def test_multiple_requests_produce_independent_timings(self) -> None:
        """Each request should get its own measured duration."""
        timings: list[float] = []
        async with AsyncClient(
            transport=ASGITransport(app=_ok_app()),
            base_url="http://test",
        ) as client:
            for _ in range(5):
                response = await client.get("/test")
                value = float(response.headers[RESPONSE_TIME_HEADER].replace("ms", ""))
                timings.append(value)

        # All should be valid floats and non-negative
        assert all(t >= 0 for t in timings)

    async def test_middleware_does_not_swallow_exceptions(self) -> None:
        """Exceptions raised inside the middleware chain must propagate."""
        async with AsyncClient(
            transport=ASGITransport(app=_build_app(_crash_handler)),
            base_url="http://test",
        ) as client:
            with pytest.raises(RuntimeError, match="intentional crash"):
                await client.get("/test")
