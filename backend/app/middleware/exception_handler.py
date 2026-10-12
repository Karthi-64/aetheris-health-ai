"""Global exception handler middleware.

Catches unhandled exceptions at the middleware level and returns structured
JSON error responses. Also handles :class:`AetherisError` subclasses that may
surface outside the registered route-level exception handlers.

The route-level handlers registered in :func:`app.main._register_exception_handlers`
are the primary mechanism. This middleware is the last-resort layer.
"""

from __future__ import annotations

import structlog
from sqlalchemy.exc import DBAPIError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.envelope import error_envelope
from app.core.error_codes import ErrorCode
from app.core.exceptions import AetherisError

logger = structlog.get_logger(__name__)


class ExceptionHandlerMiddleware(BaseHTTPMiddleware):
    """Last-resort exception handler that catches anything that escapes route handlers."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Wrap the handler chain in a try/except for fallback error handling.

        :param request: The incoming HTTP request.
        :param call_next: The next middleware or route handler.
        :returns: A JSON error response on failure, or the normal response.
        """
        try:
            return await call_next(request)
        except AetherisError as exc:
            logger.warning(
                "middleware_caught_application_error",
                error_code=exc.error_code,
                status_code=exc.status_code,
                message=exc.message,
                path=str(request.url.path),
            )
            return JSONResponse(
                status_code=exc.status_code,
                content=error_envelope(
                    exc.message,
                    error_code=exc.error_code,
                    errors=exc.detail,
                    request_id=getattr(request.state, "request_id", None),
                ),
                headers=getattr(exc, "headers", None),
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, DBAPIError):
                # A database error's own text quotes row values ("Key
                # (email)=(…) already exists"). Only what kind it was is logged.
                logger.error(
                    "middleware_caught_unhandled_exception",
                    path=str(request.url.path),
                    error_type=type(exc.orig).__name__ if exc.orig else type(exc).__name__,
                    sqlstate=getattr(exc.orig, "sqlstate", None),
                )
            else:
                logger.exception(
                    "middleware_caught_unhandled_exception",
                    path=str(request.url.path),
                    exc_info=exc,
                )
            return JSONResponse(
                status_code=500,
                content=error_envelope(
                    "An unexpected error occurred.",
                    error_code=ErrorCode.INTERNAL_ERROR,
                    request_id=getattr(request.state, "request_id", None),
                ),
            )


__all__ = ["ExceptionHandlerMiddleware"]
