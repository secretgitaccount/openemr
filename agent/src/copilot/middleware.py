"""Correlation-ID HTTP middleware (NFR-2, FR-14).

Assigns every request a correlation ID — reusing an inbound
``X-Correlation-ID`` header when present, otherwise minting a uuid4 — binds it
to the logging context for the life of the request, echoes it on the response,
and logs a structured ``request.start`` / ``request.end`` (or ``request.error``)
pair carrying method, path, status, and ``duration_ms``.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from copilot.logging import (
    CORRELATION_ID_HEADER,
    get_logger,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a correlation ID to each request and log its lifecycle."""

    def __init__(self, app, header_name: str = CORRELATION_ID_HEADER) -> None:
        super().__init__(app)
        self.header_name = header_name
        self._log = get_logger("copilot.request")

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming = request.headers.get(self.header_name)
        correlation_id = incoming or new_correlation_id()
        token = set_correlation_id(correlation_id)

        method = request.method
        path = request.url.path
        started = time.perf_counter()
        self._log.info(
            "request.start",
            method=method,
            path=path,
            correlation_id_source="header" if incoming else "generated",
        )

        try:
            response = await call_next(request)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._log.info(
                "request.end",
                method=method,
                path=path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
            response.headers[self.header_name] = correlation_id
            return response
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._log.error(
                "request.error",
                method=method,
                path=path,
                duration_ms=duration_ms,
                exc_info=True,
            )
            raise
        finally:
            # Always unbind so the ID never leaks to another task; the error
            # log above still runs while the ID is bound.
            reset_correlation_id(token)
