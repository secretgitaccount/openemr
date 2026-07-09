"""Role enforcement — *who is asking* (PRP M2-3, UC-5 role half, FR-3).

The patient-panel gate (M1-2) answers *which patient* the acting clinician may
read; this module answers *who* the acting clinician is. Together they form the
"who + which patient" authorization surface: a **Front-Office** identity is
denied the clinical chart data a **Physician** receives.

The acting user is the OAuth2 user whose identity every FHIR read borrows
(FR-3). Their role therefore must come **from OpenEMR**, never be asserted by
the client. :func:`resolve_role` derives it from the token's identity and
:func:`authorize_clinical_access` turns a :class:`Role` into a fail-closed
allow/deny decision.

Signal precedence (documented so the production path is explicit):

1. **OAuth ``userinfo`` group/role claim.** The primary, live signal:
   ``GET {oauth_base}/userinfo`` with the acting user's bearer token. When
   OpenEMR surfaces the user's ACL group / role in a claim (e.g. "Physicians"
   vs "Front Office") it is mapped directly to a :class:`Role`.
2. **Token clinical scopes.** When ``userinfo`` is unavailable (some OpenEMR
   builds do not expose it, returning 404), the granted ``user/*.read`` scopes
   on the acting token are the borrowed-identity signal: OpenEMR grants a
   clinician the clinical FHIR read scopes and a front-office user far fewer, so
   a token bearing clinical read scopes classifies as :attr:`Role.PHYSICIAN`.
   OpenEMR still enforces every scope on the real read, so this is an
   early-authorization signal, not the security boundary.
3. **Username → role map (dev/config fallback).** The username claim is mapped
   through a configurable table (``OPENEMR_ROLE_MAP`` / ``OPENEMR_FRONT_OFFICE_USER``).
   The dev physician ``admin`` maps to :attr:`Role.PHYSICIAN` out of the box.
4. **Fail closed.** An unresolvable identity resolves to :attr:`Role.OTHER`,
   which :func:`authorize_clinical_access` denies.

**Production path.** The authoritative signal is the acting user's OpenEMR ACL
group (``Physicians`` / ``Clinicians`` vs ``Front Office``), reachable via the
admin ACL API or, on the FHIR surface, a ``PractitionerRole`` for the linked
``fhirUser``. Wiring that in only changes :func:`_role_from_claims` /
:func:`_fetch_userinfo`; the fail-closed contract below is unchanged.

Every role-grounded denial emits a structured ``copilot.audit.role_denied``
event (mirroring :mod:`copilot.audit`): OpenEMR cannot log a call the agent
deliberately refuses, so the agent logs it. The event carries the acting
identity + resolved role (never a clinical value) and, via the logging
processor, the correlation ID.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import enum
import json
import os
import sys
from typing import Any, Mapping, Protocol

import httpx

from copilot.config import Settings, get_settings
from copilot.logging import (
    CORRELATION_ID_HEADER,
    configure_logging,
    current_correlation_id,
    get_logger,
    new_correlation_id,
    set_correlation_id,
)
from copilot.observability import flush, trace

__all__ = [
    "Role",
    "TokenSource",
    "ROLE_DENIED_EVENT",
    "resolve_role",
    "authorize_clinical_access",
]

logger = get_logger(__name__)

#: structlog event name — the agent-side audit record for a role refusal.
ROLE_DENIED_EVENT = "copilot.audit.role_denied"

_HTTP_TIMEOUT_SECONDS = 15.0

# userinfo claim keys that may carry the acting user's ACL group / role.
_ROLE_CLAIM_KEYS: tuple[str, ...] = (
    "role",
    "roles",
    "group",
    "groups",
    "acl",
    "acl_group",
    "user_role",
)

# userinfo claim keys that may carry the acting user's username / identity.
_USERNAME_CLAIM_KEYS: tuple[str, ...] = (
    "preferred_username",
    "username",
    "user_name",
    "name",
    "sub",
)

# Substrings (normalised: lowercased, ``_``/``-`` → space) that identify a
# clinical (physician-equivalent) group vs a front-office group. Front-office
# is checked first so an ambiguous value leans toward *deny*.
_FRONT_OFFICE_HINTS: tuple[str, ...] = (
    "front office",
    "front desk",
    "frontdesk",
    "frontoffice",
    "reception",
    "clerical",
    "billing",
    "scheduler",
)
_PHYSICIAN_HINTS: tuple[str, ...] = (
    "physician",
    "clinician",
    "provider",
    "doctor",
    "practitioner",
    "nurse",
)


class Role(str, enum.Enum):
    """The acting user's coarse authorization role.

    ``str``-backed so the value serialises cleanly into logs / traces.
    :attr:`OTHER` is the catch-all for any non-clinical or unresolved identity
    and is always denied clinical access.
    """

    PHYSICIAN = "physician"
    FRONT_OFFICE = "front_office"
    OTHER = "other"


class TokenSource(Protocol):
    """Anything that can vend the acting user's bearer access token.

    Satisfied by the M0-4 :class:`~copilot.openemr.oauth.TokenProvider` (which
    the panel gate already builds); tests supply a trivial stub.
    """

    def get_access_token(self) -> str:  # pragma: no cover - structural type
        ...


# ---------------------------------------------------------------------------
# Role parsing
# ---------------------------------------------------------------------------


def _role_from_string(value: Any) -> Role | None:
    """Map a free-text ACL group / role string to a :class:`Role`, or ``None``.

    Recognises the exact :class:`Role` values plus OpenEMR-style group names
    ("Physicians", "Front Office", …). Front-office hints win ties so an
    ambiguous label is never mistaken for a clinical one.
    """

    if not isinstance(value, str):
        return None
    normalised = " ".join(value.lower().replace("_", " ").replace("-", " ").split())
    if not normalised:
        return None
    if normalised == Role.PHYSICIAN.value.replace("_", " "):
        return Role.PHYSICIAN
    if normalised == Role.FRONT_OFFICE.value.replace("_", " "):
        return Role.FRONT_OFFICE
    if normalised == Role.OTHER.value:
        return Role.OTHER
    for hint in _FRONT_OFFICE_HINTS:
        if hint in normalised:
            return Role.FRONT_OFFICE
    for hint in _PHYSICIAN_HINTS:
        if hint in normalised:
            return Role.PHYSICIAN
    return None


def _role_from_claims(claims: Mapping[str, Any] | None) -> Role | None:
    """Derive a role from a userinfo group/role claim, if one is present.

    Tolerates a claim value that is a single string or a list of them (a user
    can belong to several groups); the first value that maps to a concrete role
    wins, front-office-leaning via :func:`_role_from_string`.
    """

    if not isinstance(claims, Mapping):
        return None
    for key in _ROLE_CLAIM_KEYS:
        value = claims.get(key)
        candidates = value if isinstance(value, (list, tuple, set)) else [value]
        found: Role | None = None
        for candidate in candidates:
            role = _role_from_string(candidate)
            if role is Role.FRONT_OFFICE:
                return role  # deny-leaning: a front-office group is decisive
            if role is not None and found is None:
                found = role
        if found is not None:
            return found
    return None


def _username_from_claims(claims: Mapping[str, Any] | None) -> str | None:
    """Return the acting user's username from the userinfo claims, if present."""

    if not isinstance(claims, Mapping):
        return None
    for key in _USERNAME_CLAIM_KEYS:
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _load_role_map(env: Mapping[str, str] | None = None) -> dict[str, Role]:
    """Build the username → role fallback table from configuration.

    Seeded with the dev physician (``admin`` → :attr:`Role.PHYSICIAN`) so the
    default stack authorises out of the box. ``OPENEMR_ROLE_MAP`` adds/overrides
    entries as ``user=role`` pairs (comma-separated, ``role`` any label
    :func:`_role_from_string` accepts); ``OPENEMR_FRONT_OFFICE_USER`` names the
    front-office test user the M2-5 acceptance uses. Keys are lower-cased.
    """

    env = os.environ if env is None else env
    mapping: dict[str, Role] = {"admin": Role.PHYSICIAN}

    raw = env.get("OPENEMR_ROLE_MAP", "") or ""
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        user, _, role_label = pair.partition("=")
        role = _role_from_string(role_label)
        if user.strip() and role is not None:
            mapping[user.strip().lower()] = role

    front_office_user = (env.get("OPENEMR_FRONT_OFFICE_USER", "") or "").strip()
    if front_office_user:
        mapping[front_office_user.lower()] = Role.FRONT_OFFICE

    return mapping


# ---------------------------------------------------------------------------
# OpenEMR identity lookup
# ---------------------------------------------------------------------------


def _tls_verify(url: str) -> bool:
    """Whether to verify TLS for ``url`` (self-signed localhost exempted)."""

    if not url.startswith("https://"):
        return True
    return not (
        url.startswith("https://localhost") or url.startswith("https://127.0.0.1")
    )


async def _fetch_userinfo(
    client: TokenSource,
    settings: Settings,
) -> dict[str, Any] | None:
    """Fetch the acting user's OIDC ``userinfo`` claims, or ``None`` on failure.

    ``GET {oauth_base}/userinfo`` with the acting user's bearer token and the
    active correlation ID. Any failure — token acquisition, transport, non-2xx,
    or a non-object body — returns ``None`` so :func:`resolve_role` fails closed
    rather than treating an unresolved identity as privileged. Never logs the
    token or response body (which could carry identity/PHI) — only status.
    """

    url = f"{settings.openemr_oauth_base.rstrip('/')}/userinfo"

    headers: dict[str, str] = {"Accept": "application/json"}
    try:
        headers["Authorization"] = f"Bearer {client.get_access_token()}"
    except Exception:
        logger.warning("copilot.roles.token_unavailable")
        return None
    cid = current_correlation_id()
    if cid:
        headers[CORRELATION_ID_HEADER] = cid

    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_SECONDS,
            verify=_tls_verify(url),
        ) as http:
            resp = await http.get(url, headers=headers)
    except httpx.HTTPError:
        logger.warning("copilot.roles.userinfo_transport_error", url=url)
        return None

    if resp.status_code >= 400:
        logger.warning(
            "copilot.roles.userinfo_http_error", status=resp.status_code, url=url
        )
        return None

    try:
        body = resp.json()
    except ValueError:
        logger.warning("copilot.roles.userinfo_bad_body", url=url)
        return None
    return body if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# Token-scope classification (borrowed-identity signal when userinfo is absent)
# ---------------------------------------------------------------------------

#: FHIR clinical read scopes an OpenEMR **clinician** token carries and a
#: front-office token does not — the role signal when ``userinfo`` is
#: unavailable.
_CLINICAL_SCOPE_MARKERS: frozenset[str] = frozenset(
    {
        "user/observation.read",
        "user/condition.read",
        "user/medicationrequest.read",
        "user/allergyintolerance.read",
        "user/diagnosticreport.read",
    }
)


def _decode_jwt_scopes(token: str) -> set[str]:
    """Best-effort read of the granted scopes from a JWT access token.

    The token is decoded **without signature verification**, solely to read its
    granted scopes for a local early-authorization decision — OpenEMR
    re-verifies and enforces every scope on the actual FHIR read, which remains
    the security boundary. Returns an empty set for anything not decodable.
    """

    parts = token.split(".")
    if len(parts) != 3:
        return set()
    segment = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        payload = json.loads(decoded)
    except (ValueError, binascii.Error):
        return set()
    if not isinstance(payload, dict):
        return set()
    raw = payload.get("scopes")
    if raw is None:
        raw = payload.get("scope", [])
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, (list, tuple)):
        return set()
    return {str(item).lower() for item in raw}


def _role_from_token_scopes(client: TokenSource) -> Role | None:
    """Classify :attr:`Role.PHYSICIAN` when the acting token grants clinical scopes.

    A token OpenEMR granted clinical ``user/*.read`` scopes is acting in a
    clinical capacity; one without them is not. Returns ``None`` when the token
    can't be read or carries no clinical scope, so resolution falls through to
    the role map and, ultimately, fail-closed.
    """

    try:
        token = client.get_access_token()
    except Exception:
        return None
    if _decode_jwt_scopes(token) & _CLINICAL_SCOPE_MARKERS:
        return Role.PHYSICIAN
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve_role(
    *,
    client: TokenSource,
    settings: Settings | None = None,
    role_map: Mapping[str, Role] | None = None,
) -> Role:
    """Resolve the acting user's :class:`Role` from OpenEMR (fail-closed).

    ``client`` is a token source (e.g. the M0-4
    :class:`~copilot.openemr.oauth.TokenProvider`) vending the acting user's
    bearer token — the identity every read borrows. Precedence: a userinfo
    group/role claim, then the username → role fallback map, then
    :attr:`Role.OTHER` when the identity cannot be resolved. Never raises: an
    unresolvable role is a *denial*, not an error.

    ``role_map`` overrides the configured fallback table (a test/DI seam);
    ``None`` loads it from configuration via :func:`_load_role_map`.
    """

    settings = settings or get_settings()
    with trace("resolve_role") as span:
        claims = await _fetch_userinfo(client, settings)

        role = _role_from_claims(claims)
        source = "userinfo_group"
        if role is None:
            # userinfo unavailable/uninformative → use the granted clinical
            # scopes on the acting token as the borrowed-identity signal.
            role = _role_from_token_scopes(client)
            if role is not None:
                source = "token_scopes"
        if role is None:
            username = _username_from_claims(claims)
            table = _load_role_map() if role_map is None else role_map
            role = table.get(username.lower()) if username else None
            source = "role_map"
        if role is None:
            role = Role.OTHER  # fail closed on an unresolvable identity
            source = "unresolved"

        span.update(output={"role": role.value}, metadata={"source": source})
        logger.info("copilot.roles.resolved", role=role.value, source=source)
        return role


def authorize_clinical_access(role: Role, *, user: str | None = None) -> bool:
    """Return whether ``role`` may read clinical chart data — fail closed.

    ``True`` only for :attr:`Role.PHYSICIAN` (clinician roles);
    :attr:`Role.FRONT_OFFICE`, :attr:`Role.OTHER`, and any unresolved role are
    denied. Every denial emits a ``copilot.audit.role_denied`` event carrying
    the resolved role and (when known) the acting ``user`` — never a clinical
    value; the correlation ID is stamped by the logging processor.
    """

    if role is Role.PHYSICIAN:
        return True

    logger.warning(
        ROLE_DENIED_EVENT,
        role=role.value,
        user=user,
        reason=(
            "Acting user's role is not authorized for clinical chart access; "
            "only physician (clinician) roles may read patient charts."
        ),
    )
    return False


# ---------------------------------------------------------------------------
# CLI smoke — live role check against the local OpenEMR (M2-5 acceptance)
# ---------------------------------------------------------------------------


async def _smoke(username: str, password: str) -> int:
    """Resolve + authorize a real OpenEMR user's role; print the decision."""

    # Imported here so the module has no import-time dependency on the OAuth
    # stack (keeps unit tests light and the public surface minimal).
    from copilot.openemr.oauth import OAuthError, TokenProvider, register_client

    configure_logging()
    settings = get_settings()
    correlation_id = new_correlation_id()
    set_correlation_id(correlation_id)
    print(f"Role smoke — {settings.openemr_oauth_base}")
    print(f"  correlation_id: {correlation_id}")
    print(f"  user:           {username}")

    try:
        creds = register_client(settings=settings)
    except OAuthError as exc:
        print(f"BLOCKED: client registration failed: {exc}", file=sys.stderr)
        return 2

    provider = TokenProvider(username, password, settings=settings, credentials=creds)
    try:
        role = await resolve_role(client=provider, settings=settings)
    except OAuthError as exc:
        print(f"BLOCKED: token acquisition failed: {exc}", file=sys.stderr)
        return 2

    allowed = authorize_clinical_access(role, user=username)
    print(f"  role:           {role.value}")
    print(f"  clinical access: {'GRANTED' if allowed else 'DENIED'}")
    flush()
    print("SMOKE OK — role resolved from OpenEMR before any clinical retrieval.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.openemr.roles",
        description="Live smoke: resolve an OpenEMR user's role and authorize.",
    )
    parser.add_argument("--user", required=True, help="OpenEMR username, e.g. admin.")
    parser.add_argument("--password", required=True, help="OpenEMR password.")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_smoke(args.user, args.password))


if __name__ == "__main__":
    raise SystemExit(main())
