"""Unit tests for the OpenEMR OAuth2 client (PRP M0-4).

OpenEMR is mocked with ``respx`` here; the live end-to-end check is the
``python -m copilot.openemr.oauth --smoke`` gate in the PRP's Validation section.

Coverage:

* token-response parsing (drops ``id_token`` / extras — schema forbids them);
* password + refresh grant request shape (grant_type, user_role, client auth,
  concrete non-wildcard scopes);
* ``register_client`` precedence (env config → cache file → dynamic POST);
* ``TokenProvider`` caching (one grant for repeated reads);
* proactive refresh before expiry (uses the refresh token, not a re-login);
* ``invalid_client`` → ``ClientNotEnabledError`` (the admin-enablement gate);
* transient (5xx) failures are retried, permanent (4xx) ones are not.
"""

from __future__ import annotations

import urllib.parse

import httpx
import pytest
import respx

from copilot.config import Settings
from copilot.openemr import oauth
from copilot.openemr.oauth import (
    ClientCredentials,
    ClientNotEnabledError,
    OAuthError,
    TokenProvider,
    default_scope_string,
    register_client,
    request_password_token,
)
from copilot.schemas.core import TokenResponse

OAUTH_BASE = "http://oemr.test/oauth2/default"
TOKEN_URL = f"{OAUTH_BASE}/token"
REG_URL = f"{OAUTH_BASE}/registration"

CREDS = ClientCredentials("client-abc", "secret-xyz")


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    """Settings pointed at the mock host, with the cache isolated to tmp_path."""

    monkeypatch.chdir(tmp_path)  # `.oauth_client.json` lands here
    return Settings(
        openemr_base_url="http://oemr.test",
        openemr_oauth_base=OAUTH_BASE,
        openemr_fhir_base="http://oemr.test/apis/default/fhir",
        openemr_client_id="",
        openemr_client_secret="",
        openemr_dev_user="admin",
        openemr_dev_pass="pass",
    )


def _password_body(*, scope: str | None = None) -> dict:
    return {
        "access_token": "access-1",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "refresh-1",
        "id_token": "id-token-should-be-dropped",
        "scope": scope or default_scope_string(),
    }


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.content.decode()))


# ---------------------------------------------------------------------------
# Scope model
# ---------------------------------------------------------------------------


def test_default_scopes_are_concrete_not_wildcard() -> None:
    scopes = default_scope_string()
    # OpenEMR rejects the wildcard; we must request concrete per-resource reads.
    assert "user/*.read" not in scopes
    assert "user/Patient.read" in scopes
    assert "user/Observation.read" in scopes
    assert "offline_access" in scopes  # needed for refresh / prewarm


# ---------------------------------------------------------------------------
# Token parsing
# ---------------------------------------------------------------------------


def test_parse_token_drops_extra_fields() -> None:
    token = oauth._parse_token(_password_body(scope="openid user/Patient.read"))
    assert isinstance(token, TokenResponse)
    assert token.access_token == "access-1"
    assert token.token_type == "Bearer"
    assert token.expires_in == 3600
    assert token.refresh_token == "refresh-1"
    assert token.scope == "openid user/Patient.read"
    # id_token is not a field on the (extra="forbid") schema.
    assert not hasattr(token, "id_token")


# ---------------------------------------------------------------------------
# Password grant request shape
# ---------------------------------------------------------------------------


@respx.mock
def test_password_grant_sends_expected_form(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_password_body()))
    token = request_password_token("admin", "pass", credentials=CREDS, settings=settings)

    assert token.access_token == "access-1"
    form = _form(route.calls.last.request)
    assert form["grant_type"] == "password"
    assert form["user_role"] == "users"
    assert form["client_id"] == "client-abc"
    assert form["client_secret"] == "secret-xyz"
    assert form["username"] == "admin"
    assert form["password"] == "pass"
    assert "user/Patient.read" in form["scope"]
    assert "user/*.read" not in form["scope"]


# ---------------------------------------------------------------------------
# register_client precedence
# ---------------------------------------------------------------------------


def test_register_client_prefers_env_config(settings: Settings) -> None:
    settings = settings.model_copy(
        update={"openemr_client_id": "env-id", "openemr_client_secret": "env-secret"}
    )
    with respx.mock:  # no routes registered → any HTTP call would error
        creds = register_client(settings=settings)
    assert creds == ClientCredentials("env-id", "env-secret")


def test_register_client_uses_cache(settings: Settings, tmp_path) -> None:
    (tmp_path / oauth.CLIENT_CACHE_FILENAME).write_text(
        '{"client_id": "cached-id", "client_secret": "cached-secret"}'
    )
    with respx.mock:
        creds = register_client(settings=settings)
    assert creds == ClientCredentials("cached-id", "cached-secret")


@respx.mock
def test_register_client_dynamic_and_persists(settings: Settings, tmp_path) -> None:
    route = respx.post(REG_URL).mock(
        return_value=httpx.Response(
            201, json={"client_id": "new-id", "client_secret": "new-secret"}
        )
    )
    creds = register_client(settings=settings)
    assert creds == ClientCredentials("new-id", "new-secret")
    assert route.called
    # Persisted to the cache so the next boot is idempotent.
    cache = tmp_path / oauth.CLIENT_CACHE_FILENAME
    assert cache.exists()
    assert "new-secret" in cache.read_text()
    # Registration must not request a wildcard scope.
    import json

    sent = json.loads(route.calls.last.request.content.decode())
    assert "user/*.read" not in sent["scope"]
    assert sent["token_endpoint_auth_method"] == "client_secret_post"


@respx.mock
def test_register_client_error_raises(settings: Settings) -> None:
    respx.post(REG_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_scope"})
    )
    with pytest.raises(OAuthError) as exc:
        register_client(settings=settings)
    assert exc.value.code == "invalid_scope"


# ---------------------------------------------------------------------------
# TokenProvider — caching + refresh
# ---------------------------------------------------------------------------


@respx.mock
def test_token_provider_caches_token(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_password_body()))
    provider = TokenProvider("admin", "pass", settings=settings, credentials=CREDS)

    first = provider.get_access_token()
    second = provider.get_access_token()

    assert first == second == "access-1"
    assert route.call_count == 1  # cached — only one grant


@respx.mock
def test_token_provider_refreshes_before_expiry(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        form = _form(request)
        if form["grant_type"] == "refresh_token":
            assert form["refresh_token"] == "refresh-1"
            return httpx.Response(
                200,
                json={
                    "access_token": "access-2",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "refresh-2",
                    "scope": default_scope_string(),
                },
            )
        return httpx.Response(200, json=_password_body())

    route = respx.post(TOKEN_URL).mock(side_effect=handler)
    # Large skew => the freshly minted token is immediately "stale", forcing a refresh.
    provider = TokenProvider(
        "admin", "pass", settings=settings, credentials=CREDS, refresh_skew_seconds=10_000
    )

    assert provider.get_access_token() == "access-1"  # password grant
    assert provider.get_access_token() == "access-2"  # refresh grant
    assert route.call_count == 2
    grant_types = [_form(c.request)["grant_type"] for c in route.calls]
    assert grant_types == ["password", "refresh_token"]


@respx.mock
def test_token_provider_falls_back_to_password_when_refresh_rejected(
    settings: Settings,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        grant = _form(request)["grant_type"]
        calls.append(grant)
        if grant == "refresh_token":
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json=_password_body())

    respx.post(TOKEN_URL).mock(side_effect=handler)
    provider = TokenProvider(
        "admin", "pass", settings=settings, credentials=CREDS, refresh_skew_seconds=10_000
    )

    provider.get_access_token()  # initial password grant
    # Stale => tries refresh (rejected) => falls back to a fresh password grant.
    assert provider.get_access_token() == "access-1"
    assert calls == ["password", "refresh_token", "password"]


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@respx.mock
def test_invalid_client_maps_to_not_enabled(settings: Settings) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            401, json={"error": "invalid_client", "error_description": "auth failed"}
        )
    )
    with pytest.raises(ClientNotEnabledError) as exc:
        request_password_token("admin", "pass", credentials=CREDS, settings=settings)
    assert exc.value.code == "invalid_client"
    assert exc.value.retriable is False


@respx.mock
def test_transient_5xx_is_retried_then_succeeds(settings: Settings) -> None:
    responses = [
        httpx.Response(503, json={"error": "temporarily_unavailable"}),
        httpx.Response(503, json={"error": "temporarily_unavailable"}),
        httpx.Response(200, json=_password_body()),
    ]
    route = respx.post(TOKEN_URL).mock(side_effect=responses)
    provider = TokenProvider("admin", "pass", settings=settings, credentials=CREDS)

    assert provider.get_access_token() == "access-1"
    assert route.call_count == 3  # two retries then success


@respx.mock
def test_permanent_4xx_is_not_retried(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    provider = TokenProvider("admin", "pass", settings=settings, credentials=CREDS)

    with pytest.raises(OAuthError) as exc:
        provider.get_access_token()
    assert exc.value.code == "invalid_grant"
    assert route.call_count == 1  # no retry on a permanent error
