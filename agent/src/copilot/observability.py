"""PHI-scrubbed observability wiring (Langfuse v4).

Every request / tool / LLM step is made observable and keyed by the active
correlation ID (PRD §7.3, NFR-2, NFR-4). Two hard rules shape this module:

1. **Graceful degradation.** If Langfuse keys are absent (the default in dev),
   the client degrades to a no-op: `trace()` still runs the wrapped code, the
   event helpers do nothing, and no network calls are attempted. The app must
   boot and serve without observability configured.
2. **PHI never leaves via traces.** Every payload sent to Langfuse — inputs,
   outputs, metadata, event bodies — passes through :func:`scrub_phi`, which
   keeps record **IDs**, resource **types**, and structural **metadata** but
   redacts clinical **values** (NFR-4, trust boundary 5 in PRD §8).

The correlation ID is sourced lazily from ``copilot.logging`` (owned by M0-2)
so this module has no hard dependency on it: if that module is not present yet
the ID is simply ``None`` and everything else still works.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel

from copilot.config import Settings, get_settings

try:  # Langfuse is a hard dependency, but keep import failure non-fatal.
    from langfuse import Langfuse
except Exception:  # pragma: no cover - defensive; langfuse is installed.
    Langfuse = None  # type: ignore[assignment,misc]


__all__ = [
    "REDACTED",
    "scrub_phi",
    "trace",
    "TraceSpan",
    "get_langfuse_client",
    "langfuse_enabled",
    "reset_langfuse_client",
    "flush",
    "record_verification",
    "record_tool_result",
    "record_event",
]

REDACTED = "[REDACTED]"


# ---------------------------------------------------------------------------
# PHI scrubbing (NFR-4)
# ---------------------------------------------------------------------------
#
# Allowlist philosophy: a leaf value is only kept when its **key** is known to
# be safe (a record ID, a resource type, a timestamp, or structural metadata).
# Everything else is a potential clinical value and is redacted. Absence of an
# allowlist entry always fails closed (redact), so new/unexpected fields never
# leak by default.

# Exact key names (compared case-insensitively) that carry metadata, not PHI.
_SAFE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "resourcetype",
        "resource_type",
        "type",
        "status",
        "system",
        "correlation_id",
        # timestamps / temporal metadata
        "timestamp",
        "date",
        "datetime",
        "time",
        "start",
        "end",
        "period",
        "created",
        "updated",
        "created_at",
        "updated_at",
        "last_updated",
        "lastupdated",
        "recorded_date",
        "effective_date",
        "effectivedatetime",
        # structural / operational metadata
        "version",
        "count",
        "total",
        "duration_ms",
        "latency_ms",
        "success",
        "passed",
        "tool",
        "event",
        "level",
        "model",
        "usage",
        "tokens",
        "input_tokens",
        "output_tokens",
        "retry_count",
        "attempt",
    }
)


def _key_is_safe(key: Any) -> bool:
    """Return True when a dict key is on the metadata allowlist.

    Any key equal to ``id`` or ending in ``_id`` (e.g. ``patient_id``,
    ``source_id``, ``encounter_id``) is treated as a record identifier and
    kept; everything else must be explicitly allowlisted.
    """

    if not isinstance(key, str):
        return False
    k = key.lower()
    if k in _SAFE_KEYS:
        return True
    return k == "id" or k.endswith("_id")


def _scrub(obj: Any, key_is_safe: bool) -> Any:
    """Recursively redact clinical values, preserving structure and IDs.

    ``key_is_safe`` carries the safety verdict of the *parent* dict key down to
    leaf values (and is inherited across list nesting), so a clinical value
    buried inside a list under an unsafe key is still redacted.
    """

    if isinstance(obj, BaseModel):
        obj = obj.model_dump()

    if isinstance(obj, dict):
        return {k: _scrub(v, _key_is_safe(k)) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set)):
        return [_scrub(v, key_is_safe) for v in obj]

    # Leaf value.
    if obj is None:
        return None
    return obj if key_is_safe else REDACTED


def scrub_phi(obj: Any) -> Any:
    """Return a PHI-scrubbed copy of ``obj`` safe to send to Langfuse.

    Keeps record IDs (``*_id`` / ``id``), resource types, timestamps, and
    allowlisted structural metadata; redacts every other leaf value to
    :data:`REDACTED`. Never mutates the input. Pydantic models are dumped to
    plain dicts first.

    >>> scrub_phi({"patient_id": "123", "lab_value": "K 5.9"})
    {'patient_id': '123', 'lab_value': '[REDACTED]'}
    """

    # Top level has no parent key, so leaves are treated as unsafe by default.
    return _scrub(obj, key_is_safe=False)


# ---------------------------------------------------------------------------
# Correlation ID (sourced from M0-2 when available; None otherwise)
# ---------------------------------------------------------------------------


def _current_correlation_id() -> str | None:
    """Best-effort fetch of the active correlation ID.

    Imported lazily from ``copilot.logging`` (M0-2) so this module works
    standalone: if that helper is unavailable the ID is simply ``None``.
    """

    try:
        from copilot.logging import current_correlation_id
    except Exception:
        return None
    try:
        return current_correlation_id()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Langfuse client — configured or no-op
# ---------------------------------------------------------------------------

_client: Any = None
_initialized: bool = False


def _keys_configured(settings: Settings) -> bool:
    """True only when both Langfuse keys look real (not empty / placeholder)."""

    def real(value: str | None) -> bool:
        return bool(value) and "xxxx" not in (value or "").lower()

    return real(settings.langfuse_public_key) and real(settings.langfuse_secret_key)


def get_langfuse_client() -> Any:
    """Return the process-wide Langfuse client, or ``None`` to run no-op.

    Constructs the client on first use from :class:`Settings`. Returns ``None``
    (never raising) when keys are absent/placeholder or client construction
    fails, so callers can treat "no client" as the single degraded path.
    """

    global _client, _initialized
    if _initialized:
        return _client

    _initialized = True
    settings = get_settings()
    if Langfuse is None or not _keys_configured(settings):
        _client = None
        return None

    try:
        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:
        # Degrade to no-op rather than take the app down over telemetry.
        _client = None
    return _client


def langfuse_enabled() -> bool:
    """True when a real Langfuse client is active (tracing will be emitted)."""

    return get_langfuse_client() is not None


def reset_langfuse_client() -> None:
    """Drop the cached client so the next call re-reads configuration.

    Primarily a test seam; also usable if configuration changes at runtime.
    """

    global _client, _initialized
    _client = None
    _initialized = False


def flush() -> None:
    """Flush buffered traces (call on shutdown). No-op when disabled."""

    client = get_langfuse_client()
    if client is not None:
        try:
            client.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------


class TraceSpan:
    """Thin handle yielded by :func:`trace`.

    Wraps a live Langfuse observation, or nothing at all when observability is
    disabled — every method is a safe no-op in the degraded case. All payloads
    are PHI-scrubbed before they reach Langfuse.
    """

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    @property
    def enabled(self) -> bool:
        return self._span is not None

    def update(
        self,
        *,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Attach scrubbed output / metadata to the span."""

        if self._span is None:
            return
        payload: dict[str, Any] = dict(kwargs)
        if output is not None:
            payload["output"] = scrub_phi(output)
        if metadata is not None:
            payload["metadata"] = scrub_phi(metadata)
        try:
            self._span.update(**payload)
        except Exception:
            pass

    def event(self, name: str, *, metadata: dict[str, Any] | None = None) -> None:
        """Record a scrubbed child event on this span."""

        if self._span is None:
            return
        try:
            self._span.create_event(name=name, metadata=scrub_phi(metadata or {}))
        except Exception:
            pass


@contextmanager
def trace(
    name: str,
    *,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    as_type: str = "span",
) -> Iterator[TraceSpan]:
    """Open a Langfuse span tagged with the active correlation ID.

    Usable as a context manager or a decorator (``@trace("name")``). Records
    duration and success/failure; on an exception the span is marked ``ERROR``
    with the exception type (never its message — that could carry PHI) and the
    exception is re-raised. When observability is disabled the wrapped code
    still runs and a no-op :class:`TraceSpan` is yielded.

    All ``input`` / ``metadata`` is PHI-scrubbed before being sent.
    """

    base_metadata: dict[str, Any] = {
        "correlation_id": _current_correlation_id(),
        **(metadata or {}),
    }
    started = time.perf_counter()
    client = get_langfuse_client()

    if client is None:
        # Degraded path: run the body, emit nothing.
        yield TraceSpan(None)
        return

    observation = client.start_as_current_observation(
        name=name,
        as_type=as_type,
        input=scrub_phi(input) if input is not None else None,
        metadata=scrub_phi(base_metadata),
    )
    with observation as span:
        handle = TraceSpan(span)
        try:
            yield handle
        except BaseException as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            handle.update(
                metadata={**base_metadata, "success": False, "duration_ms": duration_ms},
                level="ERROR",
                status_message=type(exc).__name__,
            )
            raise
        else:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            handle.update(
                metadata={**base_metadata, "success": True, "duration_ms": duration_ms},
            )


# ---------------------------------------------------------------------------
# Event helpers (feed the M3 dashboard: verification + tool success rates)
# ---------------------------------------------------------------------------


def record_event(name: str, metadata: dict[str, Any] | None = None) -> None:
    """Emit a scrubbed, correlation-tagged Langfuse event. No-op when disabled.

    Events attach to the currently-active observation/trace when one exists.
    """

    client = get_langfuse_client()
    if client is None:
        return
    payload = {"correlation_id": _current_correlation_id(), **(metadata or {})}
    try:
        client.create_event(name=name, metadata=scrub_phi(payload))
    except Exception:
        pass


def record_verification(
    passed: bool,
    *,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a verification pass/fail event (FR-10; drives the M3 dashboard).

    ``reason`` is treated as free text and is redacted by ``scrub_phi`` — it is
    passed under an unsafe key on purpose so a leaked clinical detail can never
    escape through it.
    """

    md: dict[str, Any] = {"passed": passed, **(metadata or {})}
    if reason is not None:
        md["reason"] = reason
    record_event("verification.pass" if passed else "verification.fail", md)


def record_tool_result(
    tool: str,
    success: bool,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a tool success/fail event (drives the M3 tool-failure metrics)."""

    md: dict[str, Any] = {"tool": tool, "success": success, **(metadata or {})}
    record_event("tool.success" if success else "tool.fail", md)
