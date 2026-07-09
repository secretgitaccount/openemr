"""OpenEMR OAuth2 client — dynamic registration + user-bound password grant.

PRP M0-4 (the high-risk one). This is the foundation the whole agent sits on:
it obtains a **user-bound** access token so every FHIR read carries the logged-in
clinician's identity (borrowed-identity access, PRD FR-3) rather than a standing
privileged credential.

Three pieces:

* :func:`register_client` — idempotent OAuth2 *dynamic client registration*
  (``POST /registration``). Reuses a client configured in the environment or a
  local cache file before registering a new one; persists new credentials to the
  cache.
* :func:`request_password_token` / :func:`request_refresh_token` — the two token
  grants against ``POST /token``.
* :class:`TokenProvider` — caches the token, refreshes it (via the refresh
  token) *before* it expires, retries transient failures with ``tenacity``, and
  threads the correlation ID into every request.

Notes learned from the **live** local OpenEMR (``development-easy``):

* OpenEMR's password grant requires ``user_role=users`` and
  ``client_secret_post`` client authentication.
* OpenEMR does **not** support a ``user/*.read`` wildcard scope — the
  registration endpoint 400s on it. Its FHIR scope model is per-resource, so the
  PRD-mandated "user/*.read (provider context)" is realised as the concrete set
  of ``user/<Resource>.read`` scopes the agent actually reads (PRD §11) plus the
  panel-gate resources. That set is :data:`DEFAULT_SCOPES`.
* A freshly registered client is created **disabled** (``is_enabled=0``) and must
  be enabled by an OpenEMR admin before the token endpoint will authenticate it
  (this is the "admin enablement" gate called out in PRD §15). Attempting the
  grant against a disabled client returns ``invalid_client``; we surface that as
  :class:`ClientNotEnabledError` with the exact remediation.
* The token response carries an ``id_token`` (openid) which is intentionally
  dropped when mapping to :class:`~copilot.schemas.core.TokenResponse`.

Secrets (client secret, tokens) are never written to logs — only masked
prefixes / lengths.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from copilot.config import Settings, get_settings
from copilot.logging import (
    CORRELATION_ID_HEADER,
    configure_logging,
    current_correlation_id,
    get_logger,
)
from copilot.schemas.core import TokenResponse

__all__ = [
    "DEFAULT_SCOPES",
    "default_scope_string",
    "ClientCredentials",
    "OAuthError",
    "ClientNotEnabledError",
    "register_client",
    "request_password_token",
    "request_refresh_token",
    "TokenProvider",
]

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Scopes (PRD §11 + panel-gate resources)
# ---------------------------------------------------------------------------
#
# The provider-context read surface the agent needs. Expressed as concrete
# per-resource scopes because OpenEMR rejects the `user/*.read` wildcard.
_BASE_SCOPES: tuple[str, ...] = ("openid", "offline_access", "api:fhir")
_FHIR_READ_RESOURCES: tuple[str, ...] = (
    "Patient",
    "Encounter",
    "Observation",
    "Condition",
    "MedicationRequest",
    "Medication",
    "AllergyIntolerance",
    "Appointment",
    "Practitioner",
    "CareTeam",
    "DiagnosticReport",
)
DEFAULT_SCOPES: tuple[str, ...] = _BASE_SCOPES + tuple(
    f"user/{resource}.read" for resource in _FHIR_READ_RESOURCES
)

CLIENT_CACHE_FILENAME = ".oauth_client.json"
_CLIENT_NAME = "Clinical Co-Pilot"
_HTTP_TIMEOUT_SECONDS = 15.0


def default_scope_string() -> str:
    """Return the space-delimited default scope string."""

    return " ".join(DEFAULT_SCOPES)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OAuthError(RuntimeError):
    """An OAuth2 exchange with OpenEMR failed.

    ``retriable`` distinguishes a transient failure (network blip, 5xx — a retry
    may help) from a permanent one (bad credentials, invalid scope — retrying is
    pointless). Error messages never contain secrets.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retriable = retriable


class ClientNotEnabledError(OAuthError):
    """The registered client exists but is not admin-enabled in OpenEMR.

    OpenEMR creates dynamically registered clients disabled (``is_enabled=0``);
    the token endpoint then rejects them with ``invalid_client``. An admin must
    enable the client (Administration → System → API Clients, or set
    ``is_enabled=1`` on the ``oauth_clients`` row) before the grant will work.
    """


# ---------------------------------------------------------------------------
# Client credentials + persistence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientCredentials:
    """A registered confidential OAuth2 client's id + secret."""

    client_id: str
    client_secret: str


def _cache_path(settings: Settings) -> Path:
    """Location of the on-disk client-credentials cache (cwd-relative)."""

    return Path.cwd() / CLIENT_CACHE_FILENAME


def _load_cached_credentials(path: Path) -> ClientCredentials | None:
    """Return cached credentials if the cache file holds a complete pair."""

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    if client_id and client_secret:
        return ClientCredentials(client_id, client_secret)
    return None


def _persist_credentials(path: Path, creds: ClientCredentials) -> None:
    """Write credentials to the cache file with owner-only permissions."""

    path.write_text(
        json.dumps(
            {"client_id": creds.client_id, "client_secret": creds.client_secret},
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _tls_verify(url: str) -> bool:
    """Whether to verify TLS for ``url``.

    Dev OpenEMR is HTTP on :8300 (verify is irrelevant). If HTTPS is used
    against localhost it is the stack's self-signed cert, so verification is
    disabled for that host only; every other HTTPS host is verified normally.
    """

    if not url.startswith("https://"):
        return True
    return not (url.startswith("https://localhost") or url.startswith("https://127.0.0.1"))


def _new_http_client(settings: Settings) -> httpx.Client:
    """Construct an httpx client for OAuth calls (self-signed TLS handled)."""

    return httpx.Client(
        timeout=_HTTP_TIMEOUT_SECONDS,
        verify=_tls_verify(settings.openemr_oauth_base),
    )


def _correlation_headers() -> dict[str, str]:
    """Return the correlation-ID header for the active request, if any."""

    cid = current_correlation_id()
    return {CORRELATION_ID_HEADER: cid} if cid else {}


# ---------------------------------------------------------------------------
# Dynamic client registration
# ---------------------------------------------------------------------------


def register_client(
    *,
    settings: Settings | None = None,
    scopes: tuple[str, ...] | None = None,
    client: httpx.Client | None = None,
) -> ClientCredentials:
    """Return usable client credentials, registering a new client if needed.

    Idempotent, in precedence order:

    1. Credentials configured in the environment (``OPENEMR_CLIENT_ID`` /
       ``OPENEMR_CLIENT_SECRET``).
    2. Credentials cached from a previous registration (``.oauth_client.json``).
    3. A fresh dynamic registration (``POST /registration``), persisted to the
       cache.

    Raises :class:`OAuthError` if a new registration is required but fails.
    """

    settings = settings or get_settings()

    # 1. Explicit configuration wins.
    if settings.openemr_client_id and settings.openemr_client_secret:
        return ClientCredentials(settings.openemr_client_id, settings.openemr_client_secret)

    # 2. Reuse a previously cached registration.
    cache = _cache_path(settings)
    cached = _load_cached_credentials(cache)
    if cached is not None:
        logger.info("openemr.oauth.client_reused", source="cache")
        return cached

    # 3. Dynamic registration.
    scope_str = " ".join(scopes) if scopes is not None else default_scope_string()
    payload: dict[str, Any] = {
        "application_type": "private",
        "client_name": _CLIENT_NAME,
        "redirect_uris": [f"{settings.openemr_base_url}/oauth-redirect"],
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["password", "refresh_token", "authorization_code"],
        "scope": scope_str,
    }

    url = f"{settings.openemr_oauth_base}/registration"
    owns_client = client is None
    client = client or _new_http_client(settings)
    try:
        resp = client.post(url, json=payload, headers=_correlation_headers())
    except httpx.HTTPError as exc:
        raise OAuthError(
            "OpenEMR client registration request failed to connect",
            code="transport_error",
            retriable=True,
        ) from exc
    finally:
        if owns_client:
            client.close()

    if resp.status_code >= 400:
        error, description = _extract_oauth_error(resp)
        raise OAuthError(
            f"OpenEMR client registration failed ({resp.status_code}): {error}",
            code=error,
            status_code=resp.status_code,
            retriable=resp.status_code >= 500,
        )

    body = resp.json()
    client_id = body.get("client_id")
    client_secret = body.get("client_secret")
    if not client_id or not client_secret:
        raise OAuthError(
            "OpenEMR registration response missing client_id/client_secret",
            code="malformed_registration",
        )
    creds = ClientCredentials(client_id, client_secret)
    _persist_credentials(cache, creds)
    logger.info(
        "openemr.oauth.client_registered",
        client_id_prefix=client_id[:6],
        scope_count=len(scope_str.split()),
    )
    return creds


# ---------------------------------------------------------------------------
# Token grants
# ---------------------------------------------------------------------------


def _extract_oauth_error(resp: httpx.Response) -> tuple[str, str]:
    """Pull ``(error, error_description)`` from an OAuth error response body."""

    try:
        body = resp.json()
    except ValueError:
        return ("unknown_error", "")
    return (
        str(body.get("error", "unknown_error")),
        str(body.get("error_description", "")),
    )


def _parse_token(body: dict[str, Any]) -> TokenResponse:
    """Map a raw token-endpoint body to the canonical ``TokenResponse``.

    Only the fields on the schema are carried across; extras such as
    ``id_token`` are intentionally dropped (``TokenResponse`` forbids extras).
    """

    return TokenResponse(
        access_token=body["access_token"],
        token_type=body.get("token_type", "Bearer"),
        expires_in=int(body.get("expires_in", 3600)),
        refresh_token=body.get("refresh_token"),
        scope=body.get("scope", ""),
    )


def _raise_token_error(resp: httpx.Response, *, grant_type: str) -> None:
    """Translate a >=400 token response into a typed ``OAuthError``."""

    error, description = _extract_oauth_error(resp)
    if error == "invalid_client":
        raise ClientNotEnabledError(
            "OpenEMR rejected client authentication (invalid_client). If the "
            "client was just registered it is disabled by default and needs an "
            "admin to enable it (Administration → System → API Clients, or set "
            "oauth_clients.is_enabled=1); otherwise the client secret is wrong.",
            code=error,
            status_code=resp.status_code,
            retriable=False,
        )
    raise OAuthError(
        f"OpenEMR {grant_type} grant failed ({resp.status_code}): {error}",
        code=error,
        status_code=resp.status_code,
        retriable=resp.status_code >= 500,
    )


def _post_token(
    data: dict[str, str],
    *,
    settings: Settings,
    client: httpx.Client | None,
) -> TokenResponse:
    """POST to the token endpoint and parse the result (single attempt)."""

    url = f"{settings.openemr_oauth_base}/token"
    owns_client = client is None
    client = client or _new_http_client(settings)
    try:
        resp = client.post(url, data=data, headers=_correlation_headers())
    except httpx.HTTPError as exc:
        raise OAuthError(
            "OpenEMR token request failed to connect",
            code="transport_error",
            retriable=True,
        ) from exc
    finally:
        if owns_client:
            client.close()

    if resp.status_code >= 400:
        _raise_token_error(resp, grant_type=data.get("grant_type", "token"))
    return _parse_token(resp.json())


def request_password_token(
    username: str,
    password: str,
    *,
    credentials: ClientCredentials,
    settings: Settings | None = None,
    scopes: tuple[str, ...] | None = None,
    client: httpx.Client | None = None,
) -> TokenResponse:
    """Obtain a user-bound token via the OAuth2 password grant (dev flow)."""

    settings = settings or get_settings()
    data = {
        "grant_type": "password",
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scope": " ".join(scopes) if scopes is not None else default_scope_string(),
        "user_role": "users",
        "username": username,
        "password": password,
    }
    return _post_token(data, settings=settings, client=client)


def request_refresh_token(
    refresh_token: str,
    *,
    credentials: ClientCredentials,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> TokenResponse:
    """Exchange a refresh token for a fresh access token."""

    settings = settings or get_settings()
    data = {
        "grant_type": "refresh_token",
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "refresh_token": refresh_token,
    }
    return _post_token(data, settings=settings, client=client)


# ---------------------------------------------------------------------------
# TokenProvider — caching + proactive refresh + transient retry
# ---------------------------------------------------------------------------


def _is_retriable(exc: BaseException) -> bool:
    """Retry predicate: only transient OAuth failures are worth retrying."""

    return isinstance(exc, OAuthError) and exc.retriable


class TokenProvider:
    """Caches a user-bound token and keeps it fresh.

    * Serves the cached access token until it is within ``refresh_skew_seconds``
      of expiry, then refreshes it using the refresh token (falling back to a
      full password grant if the refresh is rejected).
    * Retries transient failures (network errors, 5xx) with exponential backoff
      via ``tenacity``; permanent failures (bad credentials, disabled client)
      propagate immediately.
    * Thread-safe: concurrent callers share one in-flight refresh.

    Registration is performed lazily on first use unless ``credentials`` are
    supplied.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        settings: Settings | None = None,
        credentials: ClientCredentials | None = None,
        refresh_skew_seconds: float = 60.0,
        max_attempts: int = 3,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._settings = settings or get_settings()
        self._credentials = credentials
        self._skew = refresh_skew_seconds
        self._max_attempts = max_attempts
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._token: TokenResponse | None = None
        self._expires_at_monotonic: float = 0.0

    # -- public API --------------------------------------------------------

    def get_token(self) -> TokenResponse:
        """Return a currently-valid ``TokenResponse``, refreshing if needed."""

        with self._lock:
            if self._token is not None and not self._is_stale():
                return self._token

            if self._token is not None and self._token.refresh_token:
                try:
                    self._store(self._do(self._refresh))
                    return self._token
                except ClientNotEnabledError:
                    raise
                except OAuthError:
                    # Refresh token rejected/expired — fall back to a full grant.
                    logger.warning("openemr.oauth.refresh_failed_reauth")

            self._store(self._do(self._password_grant))
            return self._token  # type: ignore[return-value]

    def get_access_token(self) -> str:
        """Convenience: the bearer access-token string, refreshing if needed."""

        return self.get_token().access_token

    def invalidate(self) -> None:
        """Drop the cached token so the next call re-acquires one."""

        with self._lock:
            self._token = None
            self._expires_at_monotonic = 0.0

    # -- internals ---------------------------------------------------------

    def _is_stale(self) -> bool:
        return time.monotonic() >= (self._expires_at_monotonic - self._skew)

    def _store(self, token: TokenResponse) -> None:
        self._token = token
        self._expires_at_monotonic = time.monotonic() + token.expires_in

    def _client(self) -> httpx.Client | None:
        return self._client_factory() if self._client_factory is not None else None

    def _ensure_credentials(self) -> ClientCredentials:
        if self._credentials is None:
            self._credentials = register_client(
                settings=self._settings, client=self._client()
            )
        return self._credentials

    def _password_grant(self) -> TokenResponse:
        creds = self._ensure_credentials()
        return request_password_token(
            self._username,
            self._password,
            credentials=creds,
            settings=self._settings,
            client=self._client(),
        )

    def _refresh(self) -> TokenResponse:
        assert self._token is not None and self._token.refresh_token is not None
        creds = self._ensure_credentials()
        return request_refresh_token(
            self._token.refresh_token,
            credentials=creds,
            settings=self._settings,
            client=self._client(),
        )

    def _do(self, fn: Callable[[], TokenResponse]) -> TokenResponse:
        """Run ``fn`` with tenacity retry on transient OAuth failures."""

        for attempt in Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.2, max=2.0),
            retry=retry_if_exception(_is_retriable),
            reraise=True,
        ):
            with attempt:
                return fn()
        raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# CLI smoke test — live check against the local OpenEMR
# ---------------------------------------------------------------------------


def _mask(secret: str) -> str:
    """Mask a secret for logging: keep a short prefix + suffix only."""

    if len(secret) <= 12:
        return "***"
    return f"{secret[:6]}…{secret[-4:]} (len={len(secret)})"


def _smoke() -> int:
    """Register a client + obtain a real user token; print masked results."""

    configure_logging()
    settings = get_settings()
    print("OpenEMR OAuth2 smoke — live check against", settings.openemr_oauth_base)

    try:
        creds = register_client(settings=settings)
    except OAuthError as exc:
        print(f"BLOCKED: client registration failed: {exc}", file=sys.stderr)
        return 2
    print(f"  client_id: {_mask(creds.client_id)}")

    provider = TokenProvider(
        settings.openemr_dev_user,
        settings.openemr_dev_pass,
        settings=settings,
        credentials=creds,
    )

    try:
        token = provider.get_token()
    except ClientNotEnabledError as exc:
        print(f"BLOCKED (admin setup required): {exc}", file=sys.stderr)
        return 2
    except OAuthError as exc:
        print(f"BLOCKED: token acquisition failed: {exc}", file=sys.stderr)
        return 2

    granted = token.scope.split()
    read_scopes = [s for s in granted if s.startswith("user/") and s.endswith(".read")]

    print("  access_token:", _mask(token.access_token), "(masked — never logged in full)")
    print("  token_type:", token.token_type)
    print("  expires_in:", token.expires_in)
    print("  refresh_token:", "present" if token.refresh_token else "absent")
    print("  user/*.read scopes granted:", len(read_scopes))
    print("  scope:", token.scope)

    # Prove refresh works end-to-end by forcing a proactive refresh.
    provider._skew = float(token.expires_in) + 1  # force staleness
    try:
        refreshed = provider.get_token()
        print("  refresh: OK — new access_token", _mask(refreshed.access_token))
    except OAuthError as exc:
        print(f"  refresh: FAILED: {exc}", file=sys.stderr)
        return 2

    if not read_scopes or not token.refresh_token:
        print(
            "BLOCKED: token missing provider-context read scope or refresh token",
            file=sys.stderr,
        )
        return 2

    print("SMOKE OK — user-bound token acquired and refreshed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--smoke" in argv:
        return _smoke()
    print("usage: python -m copilot.openemr.oauth --smoke", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
