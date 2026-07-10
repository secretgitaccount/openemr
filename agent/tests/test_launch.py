"""Endpoint tests for the SMART launch flow (network mocked)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from copilot.api import launch as launch_mod
from copilot.config import Settings
from copilot.main import app
from copilot.openemr.oauth import ClientCredentials
from copilot.openemr.smart import SmartEndpoints, SmartToken
from copilot.smart_session import SESSION_COOKIE, remember_state

S = Settings(
    openemr_fhir_base="http://oemr/apis/default/fhir",
    openemr_oauth_base="http://oemr/oauth2/default",
    agent_base_url="http://localhost:8000",
)
CREDS = ClientCredentials("cid", "secret")
EP = SmartEndpoints(
    authorize="https://oemr:9300/oauth2/default/authorize",
    token="https://oemr:9300/oauth2/default/token",
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(launch_mod, "get_settings", lambda: S)
    monkeypatch.setattr(launch_mod, "register_client", lambda settings=None: CREDS)
    # avoid a real .well-known network call
    monkeypatch.setattr(launch_mod, "discover_endpoints", lambda iss, settings=None: EP)
    return TestClient(app)


# --- /launch ----------------------------------------------------------------


def test_launch_redirects_to_authorize(client: TestClient):
    resp = client.get(
        "/launch",
        params={"iss": S.openemr_fhir_base, "launch": "LAUNCH1"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://oemr:9300/oauth2/default/authorize?")
    assert "launch=LAUNCH1" in loc and "state=" in loc and "client_id=cid" in loc


def test_launch_rejects_foreign_issuer(client: TestClient):
    resp = client.get(
        "/launch",
        params={"iss": "http://attacker.example/fhir", "launch": "X"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_issuer"


# --- /launch/callback -------------------------------------------------------


def test_callback_rejects_unknown_state(client: TestClient):
    resp = client.get(
        "/launch/callback",
        params={"code": "C", "state": "never-issued"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_state"


def test_callback_exchanges_code_opens_session_and_patient(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    token = SmartToken(
        access_token="DOCTOR_TOK", patient="pat-99", expires_in=3600,
        refresh_token="RT", scope="launch",
    )
    monkeypatch.setattr(launch_mod, "exchange_code", lambda **kw: token)
    state = remember_state(EP.token)  # same in-memory store the endpoint consumes

    resp = client.get(
        "/launch/callback",
        params={"code": "CODE", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/?patient=pat-99"
    assert SESSION_COOKIE in resp.cookies  # a session cookie was set


def test_callback_state_is_single_use(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        launch_mod, "exchange_code",
        lambda **kw: SmartToken("T", None, 3600, None, ""),
    )
    state = remember_state(EP.token)
    first = client.get("/launch/callback", params={"code": "C", "state": state}, follow_redirects=False)
    assert first.status_code == 302
    # replaying the same state must fail closed
    second = client.get("/launch/callback", params={"code": "C", "state": state}, follow_redirects=False)
    assert second.status_code == 400
