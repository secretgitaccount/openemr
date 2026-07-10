"""SMART-on-FHIR **EHR launch** (`authorization_code`) support.

This is the production-correct auth flow (PRD §15 / ARCHITECTURE §3): instead of
the dev password grant that logs in as a fixed ``admin``, the agent is launched
*from within OpenEMR* by a logged-in clinician. OpenEMR renders a launch button
on the patient chart (see ``src/FHIR/SMART/SmartLaunchController.php``); clicking
it sends the browser to the agent's ``/launch`` endpoint with a ``launch`` token
and the FHIR issuer. The agent then runs the standard SMART handshake:

    /launch  → redirect to OpenEMR ``/authorize`` (carrying ``launch`` + scopes)
             → OpenEMR (clinician already authenticated) issues a ``code``
    /callback→ exchange ``code`` at ``/token`` for a **clinician-bound** token
               plus the launch **patient** context

The resulting access token is the clinician's borrowed identity — every read is
then made as *them* (:class:`StaticTokenSource`), and the launch hands over which
patient to open, so there is no picker.

Only the pieces unique to SMART live here; the shared HTTP/TLS/error helpers are
reused from :mod:`copilot.openemr.oauth`.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx

from copilot.config import Settings, get_settings
from copilot.logging import get_logger
from copilot.openemr.oauth import (
    ClientCredentials,
    OAuthError,
    _correlation_headers,
    _extract_oauth_error,
    _tls_verify,
)

__all__ = [
    "SMART_LAUNCH_SCOPES",
    "SmartEndpoints",
    "SmartToken",
    "StaticTokenSource",
    "discover_endpoints",
    "validate_issuer",
    "build_authorize_url",
    "exchange_code",
]

logger = get_logger(__name__)

#: Scopes requested at authorize time for an EHR launch. ``launch`` binds the
#: OpenEMR-supplied patient/encounter context; ``offline_access`` yields a
#: refresh token; the ``user/*.read`` set is the minimum-necessary clinical read
#: surface the summary needs.
SMART_LAUNCH_SCOPES: tuple[str, ...] = (
    "openid",
    "fhirUser",
    "launch",
    "offline_access",
    "user/Patient.read",
    "user/Condition.read",
    "user/Observation.read",
    "user/MedicationRequest.read",
    "user/AllergyIntolerance.read",
    "user/Encounter.read",
    "user/Appointment.read",
)


class StaticTokenSource:
    """A :class:`~copilot.openemr.roles.TokenSource` wrapping one fixed token.

    Used after the SMART handshake to make every read (and the role gate) run as
    the launched clinician, rather than the ``admin`` password-grant identity.
    """

    __slots__ = ("_token",)

    def __init__(self, access_token: str) -> None:
        self._token = access_token

    def get_access_token(self) -> str:
        return self._token


@dataclass(frozen=True, slots=True)
class SmartToken:
    """The result of a successful SMART code exchange.

    ``patient`` is the FHIR Patient id from the launch context (which patient the
    clinician had open) — present for an EHR/patient launch, ``None`` otherwise.
    """

    access_token: str
    patient: str | None
    expires_in: int
    refresh_token: str | None
    scope: str


@dataclass(frozen=True, slots=True)
class SmartEndpoints:
    """An issuer's authorize + token endpoints (from SMART discovery)."""

    authorize: str
    token: str


_HTTP_TIMEOUT_SECONDS = 20.0


def _redirect_uri(settings: Settings) -> str:
    """The registered SMART callback URI (must match the OAuth client)."""

    return f"{settings.agent_base_url.rstrip('/')}/launch/callback"


def validate_issuer(iss: str, settings: Settings | None = None) -> bool:
    """Accept a launch only from our OpenEMR **host** (issuer pinning).

    A launch is an unauthenticated inbound request, so the issuer is pinned to
    stop an attacker pointing the agent at a hostile authorization server. Host
    (not full-URL) matching is deliberate: OpenEMR's dev stack advertises itself
    over a different scheme/port (``https:9300``) than the read API is configured
    with (``http:8300``), yet both are the same trusted server.
    """

    settings = settings or get_settings()
    want = urlparse(settings.openemr_fhir_base).hostname
    got = urlparse(iss).hostname
    return bool(want) and got == want


def discover_endpoints(
    iss: str,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> SmartEndpoints:
    """Return the authorize + token endpoints the issuer advertises.

    Fetches ``{iss}/.well-known/smart-configuration`` (the SMART standard) so the
    agent uses exactly the endpoints — scheme/host/port — OpenEMR expects, which
    is also what it validates ``aud`` against. Falls back to deriving them from
    the issuer path if discovery is unavailable.
    """

    settings = settings or get_settings()
    url = f"{iss.rstrip('/')}/.well-known/smart-configuration"
    owns = client is None
    client = client or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, verify=_tls_verify(url))
    try:
        resp = client.get(url, headers=_correlation_headers())
        resp.raise_for_status()
        body = resp.json()
        authorize, token = body.get("authorization_endpoint"), body.get("token_endpoint")
        if authorize and token:
            return SmartEndpoints(authorize=authorize, token=token)
    except Exception:  # discovery is best-effort — fall through to derivation
        pass
    finally:
        if owns:
            client.close()

    base = iss.rstrip("/")
    if base.endswith("/apis/default/fhir"):
        base = base[: -len("/apis/default/fhir")] + "/oauth2/default"
    else:
        base = settings.openemr_oauth_base.rstrip("/")
    return SmartEndpoints(authorize=f"{base}/authorize", token=f"{base}/token")


def build_authorize_url(
    *,
    endpoints: SmartEndpoints,
    launch: str,
    state: str,
    aud: str,
    credentials: ClientCredentials,
    settings: Settings | None = None,
) -> str:
    """Build the OpenEMR ``/authorize`` redirect URL for an EHR launch."""

    settings = settings or get_settings()
    params = {
        "response_type": "code",
        "client_id": credentials.client_id,
        "redirect_uri": _redirect_uri(settings),
        "scope": " ".join(SMART_LAUNCH_SCOPES),
        "state": state,
        "aud": aud,
        "launch": launch,
    }
    return f"{endpoints.authorize}?{urlencode(params)}"


def exchange_code(
    *,
    code: str,
    token_endpoint: str,
    credentials: ClientCredentials,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> SmartToken:
    """Exchange an authorization ``code`` for a clinician-bound token.

    Unlike :func:`copilot.openemr.oauth._parse_token`, this keeps the SMART
    ``patient`` launch context from the token response (which the canonical
    ``TokenResponse`` schema drops). ``token_endpoint`` is the discovered token
    URL (carried across the redirect via the launch state).
    """

    settings = settings or get_settings()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(settings),
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
    }
    owns_client = client is None
    client = client or httpx.Client(
        timeout=_HTTP_TIMEOUT_SECONDS, verify=_tls_verify(token_endpoint)
    )
    try:
        resp = client.post(token_endpoint, data=data, headers=_correlation_headers())
    except httpx.HTTPError as exc:
        raise OAuthError(
            "OpenEMR SMART code exchange failed to connect",
            code="transport_error",
            retriable=True,
        ) from exc
    finally:
        if owns_client:
            client.close()

    if resp.status_code >= 400:
        error, _ = _extract_oauth_error(resp)
        raise OAuthError(
            f"OpenEMR SMART code exchange failed ({resp.status_code}): {error}",
            code=error,
            status_code=resp.status_code,
            retriable=resp.status_code >= 500,
        )

    body = resp.json()
    if "access_token" not in body:
        raise OAuthError(
            "OpenEMR SMART token response missing access_token",
            code="malformed_token_response",
        )
    logger.info(
        "openemr.smart.code_exchanged",
        has_patient=bool(body.get("patient")),
        has_refresh=bool(body.get("refresh_token")),
    )
    return SmartToken(
        access_token=body["access_token"],
        patient=body.get("patient"),
        expires_in=int(body.get("expires_in", 3600)),
        refresh_token=body.get("refresh_token"),
        scope=body.get("scope", ""),
    )
