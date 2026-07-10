"""Unit tests for the SMART EHR-launch handshake helpers (no network)."""

from __future__ import annotations

import pytest

from copilot.config import Settings
from copilot.openemr.oauth import ClientCredentials, OAuthError
from copilot.openemr.smart import (
    SmartEndpoints,
    SmartToken,
    StaticTokenSource,
    build_authorize_url,
    discover_endpoints,
    exchange_code,
    validate_issuer,
)

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


# --- issuer pinning (host-based) --------------------------------------------


def test_validate_issuer_accepts_same_host_any_scheme_port():
    # OpenEMR advertises itself over https:9300 even though reads use http:8300.
    assert validate_issuer("https://oemr:9300/apis/default/fhir", S)
    assert validate_issuer("http://oemr/apis/default/fhir", S)


def test_validate_issuer_rejects_foreign_host():
    assert not validate_issuer("https://attacker.example/fhir", S)


# --- discovery --------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("http error")


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def get(self, url, headers=None):  # noqa: ANN001
        return self._resp

    def post(self, url, data=None, headers=None):  # noqa: ANN001
        return self._resp

    def close(self) -> None:
        pass


def test_discover_endpoints_reads_well_known():
    body = {
        "authorization_endpoint": "https://oemr:9300/oauth2/default/authorize",
        "token_endpoint": "https://oemr:9300/oauth2/default/token",
    }
    ep = discover_endpoints(
        "https://oemr:9300/apis/default/fhir", settings=S, client=_Client(_Resp(200, body))
    )
    assert ep.authorize.endswith(":9300/oauth2/default/authorize")
    assert ep.token.endswith(":9300/oauth2/default/token")


def test_discover_endpoints_falls_back_on_failure():
    ep = discover_endpoints(
        "https://oemr:9300/apis/default/fhir", settings=S, client=_Client(_Resp(500, {}))
    )
    # derived from the issuer path
    assert ep.authorize == "https://oemr:9300/oauth2/default/authorize"
    assert ep.token == "https://oemr:9300/oauth2/default/token"


# --- authorize URL ----------------------------------------------------------


def test_build_authorize_url_carries_all_smart_params():
    url = build_authorize_url(
        endpoints=EP, launch="LAUNCH123", state="STATE456", aud=S.openemr_fhir_base,
        credentials=CREDS, settings=S,
    )
    assert url.startswith("https://oemr:9300/oauth2/default/authorize?")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    assert "launch=LAUNCH123" in url
    assert "state=STATE456" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Flaunch%2Fcallback" in url
    assert "launch" in url and "scope=" in url


# --- code exchange ----------------------------------------------------------


def test_exchange_code_keeps_patient_context():
    body = {
        "access_token": "TOK", "patient": "pat-1", "expires_in": 3600,
        "refresh_token": "RT", "scope": "launch user/Patient.read",
    }
    tok = exchange_code(
        code="C", token_endpoint=EP.token, credentials=CREDS, settings=S,
        client=_Client(_Resp(200, body)),
    )
    assert isinstance(tok, SmartToken)
    assert tok.access_token == "TOK"
    assert tok.patient == "pat-1"  # canonical TokenResponse drops this; we keep it
    assert tok.refresh_token == "RT"


def test_exchange_code_maps_error_to_oautherror():
    with pytest.raises(OAuthError):
        exchange_code(
            code="C", token_endpoint=EP.token, credentials=CREDS, settings=S,
            client=_Client(_Resp(400, {"error": "invalid_grant"})),
        )


def test_static_token_source_returns_fixed_token():
    assert StaticTokenSource("abc123").get_access_token() == "abc123"
