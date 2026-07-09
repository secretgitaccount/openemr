"""Structured (JSON) logging + correlation-ID plumbing (NFR-2, FR-14).

Every log line the agent emits is a single JSON object that always carries the
active request's ``correlation_id`` — so a request can be reconstructed from
logs alone. The ID lives in a :class:`contextvars.ContextVar`, set once per
request by ``copilot.middleware`` and read by a structlog processor here; it is
also the ID threaded into Langfuse traces (``copilot.observability``) and, later,
into OpenEMR's ``api_log``.

Public surface:

* :func:`get_logger` — a bound structlog logger.
* :func:`current_correlation_id` — the active request's ID (``""`` if none).
* :func:`set_correlation_id` / :func:`reset_correlation_id` — contextvar control
  used by the middleware.
* :func:`configure_logging` — (re)install the structlog processor chain; called
  at app startup and by tests (which can redirect output to their own stream).
"""

from __future__ import annotations

import contextvars
import logging as _stdlib_logging
import uuid
from typing import Any, TextIO

import structlog

__all__ = [
    "CORRELATION_ID_HEADER",
    "configure_logging",
    "get_logger",
    "current_correlation_id",
    "new_correlation_id",
    "set_correlation_id",
    "reset_correlation_id",
]

#: HTTP header used to carry the correlation ID in and out of the service.
CORRELATION_ID_HEADER = "X-Correlation-ID"

# The single source of truth for the active request's correlation ID. A
# ContextVar propagates correctly across async tasks, so concurrent requests
# never see each other's IDs.
_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


# ---------------------------------------------------------------------------
# Correlation-ID contextvar control
# ---------------------------------------------------------------------------


def new_correlation_id() -> str:
    """Return a fresh random correlation ID (uuid4 hex-with-dashes)."""

    return str(uuid.uuid4())


def set_correlation_id(correlation_id: str) -> contextvars.Token[str | None]:
    """Bind ``correlation_id`` to the current context; return a reset token."""

    return _correlation_id.set(correlation_id)


def reset_correlation_id(token: contextvars.Token[str | None]) -> None:
    """Restore the correlation ID to its prior value using ``token``."""

    _correlation_id.reset(token)


def current_correlation_id() -> str:
    """Return the active request's correlation ID, or ``""`` when unset."""

    return _correlation_id.get() or ""


# ---------------------------------------------------------------------------
# structlog configuration
# ---------------------------------------------------------------------------


def _add_correlation_id(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: stamp every event with the active correlation ID."""

    cid = _correlation_id.get()
    if cid is not None:
        event_dict["correlation_id"] = cid
    return event_dict


def _resolve_level(level: str | int | None) -> int:
    """Coerce a level name/number into a stdlib logging integer."""

    if isinstance(level, int):
        return level
    if isinstance(level, str):
        return _stdlib_logging.getLevelNamesMapping().get(
            level.upper(), _stdlib_logging.INFO
        )
    return _stdlib_logging.INFO


def configure_logging(
    *,
    level: str | int | None = None,
    stream: TextIO | None = None,
) -> None:
    """Install the JSON structlog processor chain.

    Idempotent and safe to call repeatedly. ``level`` defaults to
    ``Settings.log_level``; ``stream`` (a test seam) defaults to stdout. Every
    record is rendered as one JSON line and carries ``correlation_id``,
    ``level``, an ISO ``timestamp``, and the event name.
    """

    if level is None:
        # Import lazily so this module has no import-time dependency on config.
        from copilot.config import get_settings

        level = get_settings().log_level

    factory = (
        structlog.WriteLoggerFactory(file=stream)
        if stream is not None
        else structlog.WriteLoggerFactory()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_correlation_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_level(level)),
        logger_factory=factory,
        # Do not cache: tests reconfigure the output stream, and the small
        # per-logger cost is irrelevant to this service.
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a bound structlog logger (configuring on first use if needed)."""

    if not structlog.is_configured():
        configure_logging()
    return structlog.get_logger(name)
