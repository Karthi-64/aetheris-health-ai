"""Request timing middleware.

Measures total request processing time and exposes it via the
``X-Response-Time`` response header. The duration is also logged
by :class:`~app.middleware.logging.RequestLoggingMiddleware`, so this
middleware does **not** produce its own structured log entries.

Registered in :func:`app.main._register_middleware`.

Middleware order
----------------
This middleware runs **after** (inside) :class:`ExceptionHandlerMiddleware`
and **before** (outside) :class:`AuthMiddleware`, so the measured duration
includes all request processing through the route handler and every inner
middleware layer.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.constants import RESPONSE_TIME_HEADER


class TimingMiddleware(BaseHTTPMiddleware):
    """Middleware that records total request duration as a response header.

    The response header follows the convention established by the existing
    ``X-Request-ID`` header (see :class:`~app.middleware.request_id.RequestIDMiddleware`)::

        X-Response-Time: 25.31ms

    The value is wall-clock time measured with ``time.perf_counter_ns()``,
    reported to two decimal places.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Time the request, execute it, and attach the ``X-Response-Time`` header.

        :param request: The incoming HTTP request.
        :param call_next: The next middleware or route handler.
        :returns: The HTTP response with the ``X-Response-Time`` header set.
        """
        start_ns = time.perf_counter_ns()
        response: Response = await call_next(request)
        duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000

        response.headers[RESPONSE_TIME_HEADER] = f"{duration_ms:.2f}ms"
        return response


__all__ = ["TimingMiddleware"]
