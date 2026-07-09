"""Liveness / readiness endpoint tests (PRP M0-3).

Covers the Definition of Done:

* ``/health`` → 200 ``{"status": "ok"}`` regardless of dependency state.
* ``/ready`` returns a JSON object with a key per dependency.
* with OpenEMR reachable at localhost:8300 the OpenEMR check is ``ok``.
* with a dependency mocked unreachable, ``/ready`` → 503 and names the
  failing dependency.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from copilot import health
from copilot.health import DependencyStatus
from copilot.main import app

DEPENDENCY_KEYS = {"openemr", "anthropic", "langfuse", "audit_globals"}


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# /health — pure liveness
# ---------------------------------------------------------------------------


def test_health_is_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_does_not_touch_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """/health must stay 200 even when every dependency probe would fail."""

    async def _boom(*_a: object, **_k: object) -> DependencyStatus:  # pragma: no cover
        raise AssertionError("/health must not run dependency probes")

    monkeypatch.setattr(health, "_check_openemr", _boom)
    monkeypatch.setattr(health, "_check_langfuse", _boom)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# /ready — shape
# ---------------------------------------------------------------------------


def test_ready_has_a_key_per_dependency(client: TestClient) -> None:
    body = client.get("/ready").json()
    assert set(body["checks"]) == DEPENDENCY_KEYS
    # Every check is a well-formed status object.
    for name, check in body["checks"].items():
        assert "status" in check, name
        assert isinstance(check["required"], bool)
    assert body["status"] in {"ready", "not_ready"}


def test_ready_audit_globals_is_stubbed(client: TestClient) -> None:
    audit = client.get("/ready").json()["checks"]["audit_globals"]
    assert audit["status"] == "skipped"
    assert audit["required"] is False
    assert "TODO" in (audit["detail"] or "")


def test_ready_langfuse_degrades_without_keys(client: TestClient) -> None:
    """Default dev config has no Langfuse keys → not_configured, non-gating."""

    langfuse = client.get("/ready").json()["checks"]["langfuse"]
    assert langfuse["status"] == "not_configured"
    assert langfuse["required"] is False


# ---------------------------------------------------------------------------
# /ready — OpenEMR reachable at localhost:8300 (live dev stack)
# ---------------------------------------------------------------------------


def test_ready_openemr_ok_when_reachable(client: TestClient) -> None:
    openemr = client.get("/ready").json()["checks"]["openemr"]
    assert openemr["status"] == "ok", (
        "expected the live OpenEMR dev stack at localhost:8300 to be reachable; "
        f"got {openemr}"
    )
    assert openemr["required"] is True
    assert isinstance(openemr["latency_ms"], (int, float))


# ---------------------------------------------------------------------------
# /ready — a dependency down → 503, naming the failure
# ---------------------------------------------------------------------------


def test_ready_503_when_openemr_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _unreachable(*_a: object, **_k: object) -> DependencyStatus:
        return DependencyStatus(
            status="unreachable",
            required=True,
            detail="ConnectError: simulated outage",
        )

    monkeypatch.setattr(health, "_check_openemr", _unreachable)

    with TestClient(app) as c:
        resp = c.get("/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    # The failing dependency is named and marked unreachable.
    assert body["checks"]["openemr"]["status"] == "unreachable"


def test_ready_names_failing_dependency_via_real_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point OpenEMR at a dead port; the probe should turn a real ConnectError
    into an ``unreachable`` status without hanging or crashing /ready."""

    settings = health.get_settings()
    monkeypatch.setattr(settings, "openemr_base_url", "http://127.0.0.1:59321", raising=False)

    async def _run() -> DependencyStatus:
        async with httpx.AsyncClient(timeout=1.0) as hc:
            return await health._check_openemr(hc, settings)

    import asyncio

    result = asyncio.run(_run())
    assert result.status == "unreachable"
    assert result.required is True
    assert result.detail and "127.0.0.1:59321" in result.detail


# ---------------------------------------------------------------------------
# probe unit behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [200, 401])
async def test_openemr_probe_treats_200_and_401_as_reachable(
    code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = health.get_settings()

    class _Resp:
        status_code = code

    class _FakeClient:
        async def get(self, _url: str) -> _Resp:
            return _Resp()

    result = await health._check_openemr(_FakeClient(), settings)  # type: ignore[arg-type]
    assert result.status == "ok"
    assert f"HTTP {code}" in (result.detail or "")


async def test_anthropic_probe_flags_placeholder_key() -> None:
    settings = health.get_settings()
    # Default dev config uses a placeholder key.
    result = await health._check_anthropic(settings)
    assert result.status == "not_configured"
    assert result.required is True
