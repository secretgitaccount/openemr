"""Fixtures for the deterministic eval suite (no key, no network).

Provides an in-memory structlog capture so adversarial cases can assert that a
refusal / role-denial actually emitted its audit event, and a real-key
``Settings`` for the rare case that constructs an ``LLMClient`` (every eval case
injects a :class:`~_helpers.FakeLLM`, so no live call ever fires).
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest

from copilot.config import Settings
from copilot.logging import configure_logging


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="sk-ant-real", anthropic_model="claude-sonnet-5")


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Redirect structlog JSON output into an in-memory buffer for assertions."""

    buffer = io.StringIO()
    configure_logging(level="INFO", stream=buffer)
    yield buffer
    configure_logging()


def log_lines(stream: io.StringIO) -> list[dict]:
    """Parse the captured structlog buffer into a list of event dicts."""

    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
