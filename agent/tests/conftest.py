"""Shared test fixtures."""

from __future__ import annotations

import pytest

from copilot.orchestrator.summary_cache import summary_cache


@pytest.fixture(autouse=True)
def _isolate_summary_cache():
    """Clear the process-wide summary cache around every test.

    The cache is a module-level singleton (server-side, shared across requests),
    so without this a summary cached by one test would be served to another and
    skip that test's LLM stub. Isolate it so each test starts empty.
    """

    summary_cache.clear()
    yield
    summary_cache.clear()
