"""Correlation-ID middleware + structured logging tests (PRP M0-2).

Asserts the three DoD behaviours:
1. A request without ``X-Correlation-ID`` gets a generated ID echoed back.
2. A request WITH ``X-Correlation-ID: abc`` echoes ``abc``.
3. A captured log line for that request carries the same ``correlation_id``.
"""

from __future__ import annotations

import io
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from copilot.logging import (
    CORRELATION_ID_HEADER,
    configure_logging,
    current_correlation_id,
    get_logger,
)
from copilot.main import app
from copilot.middleware import CorrelationIdMiddleware


@pytest.fixture
def log_stream() -> io.StringIO:
    """Redirect structlog JSON output into an in-memory buffer for assertions."""

    buffer = io.StringIO()
    configure_logging(level="INFO", stream=buffer)
    yield buffer
    # Restore default configuration for other tests.
    configure_logging()


def _log_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _build_client() -> TestClient:
    """A tiny app with the middleware and an endpoint that emits its own log."""

    test_app = FastAPI()
    test_app.add_middleware(CorrelationIdMiddleware)

    @test_app.get("/ping")
    def ping() -> dict[str, str]:
        get_logger("copilot.test").info("handler.ran")
        return {"correlation_id": current_correlation_id()}

    return TestClient(test_app)


def test_generated_id_when_header_absent(log_stream: io.StringIO) -> None:
    client = _build_client()
    resp = client.get("/ping")

    assert resp.status_code == 200
    echoed = resp.headers.get(CORRELATION_ID_HEADER)
    assert echoed, "response must echo a correlation ID"
    # A generated ID is a valid uuid4.
    uuid.UUID(echoed)
    # The endpoint saw the same ID via the contextvar.
    assert resp.json()["correlation_id"] == echoed


def test_incoming_id_is_echoed(log_stream: io.StringIO) -> None:
    client = _build_client()
    resp = client.get("/ping", headers={CORRELATION_ID_HEADER: "abc"})

    assert resp.status_code == 200
    assert resp.headers.get(CORRELATION_ID_HEADER) == "abc"
    assert resp.json()["correlation_id"] == "abc"


def test_log_line_carries_correlation_id(log_stream: io.StringIO) -> None:
    client = _build_client()
    resp = client.get("/ping", headers={CORRELATION_ID_HEADER: "abc"})
    assert resp.status_code == 200

    lines = _log_lines(log_stream)
    assert lines, "expected structured log output"
    # Every line emitted during the request carries the correlation ID.
    request_lines = [ln for ln in lines if ln.get("correlation_id") == "abc"]
    events = {ln["event"] for ln in request_lines}
    assert "request.start" in events
    assert "request.end" in events
    assert "handler.ran" in events

    end = next(ln for ln in request_lines if ln["event"] == "request.end")
    assert end["status"] == 200
    assert end["method"] == "GET"
    assert end["path"] == "/ping"
    assert isinstance(end["duration_ms"], (int, float))
    # JSON logs carry level + timestamp.
    assert end["level"] == "info"
    assert "timestamp" in end


def test_middleware_wired_into_main_app() -> None:
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
        assert resp.headers.get(CORRELATION_ID_HEADER)


def test_current_correlation_id_empty_outside_request() -> None:
    assert current_correlation_id() == ""
