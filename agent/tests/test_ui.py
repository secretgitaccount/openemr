"""Tests for the demo UI endpoints (GET / page + GET /patients).

The FHIR client is faked via a dependency override, so no key and no live stack.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from copilot.api.ui import get_ui_fhir_client
from copilot.main import app


class _FakeFhir:
    """Minimal FhirClient stand-in: returns a canned Patient search bundle."""

    async def get(self, path: str, params: dict | None = None) -> dict:
        assert path == "/Patient"
        return {
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "p1", "name": [{"text": "Jane Doe"}]}},
                {"resource": {"resourceType": "Patient", "id": "p2",
                              "name": [{"given": ["John"], "family": "Roe"}]}},
                {"resource": {"resourceType": "Patient"}},  # no id -> skipped
            ]
        }


async def _fake_dep() -> AsyncIterator[_FakeFhir]:
    yield _FakeFhir()


def _client() -> TestClient:
    app.dependency_overrides[get_ui_fhir_client] = _fake_dep
    return TestClient(app)


def test_index_serves_html() -> None:
    try:
        with _client() as c:
            r = c.get("/")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Clinical Co-Pilot" in r.text


def test_list_patients_maps_id_and_name() -> None:
    try:
        with _client() as c:
            r = c.get("/patients")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    data = r.json()
    # id-less resource skipped; both named patients mapped (text and given+family).
    assert len(data) == 2
    assert {"id": "p1", "name": "Jane Doe"} in data
    assert any(p["id"] == "p2" and p["name"] == "John Roe" for p in data)
    assert all(p["id"] for p in data)
