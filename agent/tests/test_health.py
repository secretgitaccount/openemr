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

DEPENDENCY_KEYS = {
    "openemr",
    "anthropic",
    "langfuse",
    "audit_globals",
    # Week-2 dependencies (PRP-14)
    "document_storage",
    "vector_index",
    "reranker",
}


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


async def _ok(*_a: object, **_k: object) -> DependencyStatus:
    """A generic 'this required dependency is up' probe stub.

    Lets a degradation test force the two *required* deps (OpenEMR, Anthropic) ok
    so the aggregate reflects the non-gating dependency under test — hermetic
    regardless of whether a live stack or API key is present locally."""

    return DependencyStatus(status="ok", required=True, detail="stubbed ok")


def _force_required_deps_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_check_openemr", _ok)
    monkeypatch.setattr(health, "_check_anthropic", _ok)


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
    # Status is three-valued (not a binary up/down) and carries the degraded list.
    assert body["status"] in {"ready", "degraded", "not_ready"}
    assert isinstance(body["degraded"], list)


def test_ready_audit_globals_is_stubbed(client: TestClient) -> None:
    audit = client.get("/ready").json()["checks"]["audit_globals"]
    assert audit["status"] == "skipped"
    assert audit["required"] is False
    assert "TODO" in (audit["detail"] or "")


def test_ready_langfuse_degrades_without_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent Langfuse keys → not_configured, non-gating.

    Pin placeholder keys so the assertion holds regardless of the developer's
    local ``.env`` (real Langfuse keys must not turn this red).
    """

    placeholder = health.get_settings().model_copy(
        update={"langfuse_public_key": "pk-xxxx", "langfuse_secret_key": "sk-xxxx"}
    )
    monkeypatch.setattr(health, "get_settings", lambda: placeholder)

    langfuse = client.get("/ready").json()["checks"]["langfuse"]
    assert langfuse["status"] == "not_configured"
    assert langfuse["required"] is False


# ---------------------------------------------------------------------------
# /ready — OpenEMR reachable at localhost:8300 (live dev stack)
# ---------------------------------------------------------------------------


@pytest.mark.live
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
# /ready — Week-2 dependencies (PRP-14): document storage, vector index, reranker
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_ready_week2_deps_ok_when_reachable(client: TestClient) -> None:
    """With the local stack + Week-2 deps installed, all three report ok and
    are marked non-gating."""

    checks = client.get("/ready").json()["checks"]
    for name in ("document_storage", "vector_index", "reranker"):
        assert checks[name]["status"] == "ok", (name, checks[name])
        assert checks[name]["required"] is False


def test_ready_degrades_when_reranker_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stubbed-down Week-2 dependency DEGRADES readiness (not a 503, not a
    binary flip) and is named in the ``degraded`` list."""

    async def _down(*_a: object, **_k: object) -> DependencyStatus:
        return DependencyStatus(
            status="degraded",
            required=False,
            detail="simulated: reranker model not loadable",
        )

    _force_required_deps_ok(monkeypatch)
    monkeypatch.setattr(health, "_check_reranker", _down)

    with TestClient(app) as c:
        resp = c.get("/ready")

    # Degraded still serves traffic — 200, not 503.
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["reranker"]["status"] == "degraded"
    # The down dependency is named.
    assert "reranker" in body["degraded"]


def test_ready_degraded_document_storage_does_not_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Document storage is non-gating: unreachable degrades, never 503s."""

    async def _unreachable(*_a: object, **_k: object) -> DependencyStatus:
        return DependencyStatus(
            status="unreachable",
            required=False,
            detail="ConnectError: simulated Standard REST API outage",
        )

    _force_required_deps_ok(monkeypatch)
    monkeypatch.setattr(health, "_check_document_storage", _unreachable)

    with TestClient(app) as c:
        resp = c.get("/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert "document_storage" in body["degraded"]


def test_required_dep_down_still_not_ready_over_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required dep down wins over a degraded Week-2 dep: 503 not_ready."""

    async def _openemr_down(*_a: object, **_k: object) -> DependencyStatus:
        return DependencyStatus(
            status="unreachable", required=True, detail="simulated outage"
        )

    async def _reranker_down(*_a: object, **_k: object) -> DependencyStatus:
        return DependencyStatus(status="degraded", required=False, detail="simulated")

    monkeypatch.setattr(health, "_check_openemr", _openemr_down)
    monkeypatch.setattr(health, "_check_reranker", _reranker_down)

    with TestClient(app) as c:
        resp = c.get("/ready")

    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


async def test_document_storage_probe_treats_401_as_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = health.get_settings()

    class _Resp:
        status_code = 401

    class _FakeClient:
        async def get(self, _url: str, **_kwargs: object) -> _Resp:
            return _Resp()

    result = await health._check_document_storage(_FakeClient(), settings)  # type: ignore[arg-type]
    assert result.status == "ok"
    assert result.required is False


async def test_vector_index_probe_reports_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await health._check_vector_index(health.get_settings())
    assert result.status == "ok"
    assert "chunks" in (result.detail or "")


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
        async def get(self, _url: str, **_kwargs: object) -> _Resp:
            return _Resp()

    result = await health._check_openemr(_FakeClient(), settings)  # type: ignore[arg-type]
    assert result.status == "ok"
    assert f"HTTP {code}" in (result.detail or "")


async def test_anthropic_probe_flags_placeholder_key() -> None:
    # Use an explicit placeholder key so the probe's logic is tested
    # deterministically, independent of whatever real key sits in the ambient
    # .env (a real key would otherwise make this probe report "ok").
    settings = health.get_settings().model_copy(update={"anthropic_api_key": "sk-ant-xxxxxxxx"})
    result = await health._check_anthropic(settings)
    assert result.status == "not_configured"
    assert result.required is True
