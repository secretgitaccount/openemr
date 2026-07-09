"""Unit tests for role enforcement (PRP M2-3, UC-5 role half).

OpenEMR's OAuth ``userinfo`` endpoint is mocked with ``respx`` (the async httpx
transport is intercepted); the live proof — a second OpenEMR user in the Front
Office ACL group being denied the same patient a physician receives — is the
``python -m copilot.openemr.roles --user <front-office> --password <…>`` smoke
in the PRP's Live note.

Coverage:

* a physician identity (via a userinfo group claim, and via the username →
  role map) → :attr:`Role.PHYSICIAN` → authorized, no denial logged;
* a front-office identity (via group claim, and via
  ``OPENEMR_FRONT_OFFICE_USER``) → :attr:`Role.FRONT_OFFICE` → denied + a logged
  ``copilot.audit.role_denied`` carrying role/user + correlation ID;
* an unresolvable identity (userinfo unreachable) and an unknown username →
  :attr:`Role.OTHER` → denied (fail closed);
* :func:`authorize_clinical_access` allow/deny + audit behaviour in isolation.
"""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
import respx

from copilot.config import Settings
from copilot.logging import (
    configure_logging,
    reset_correlation_id,
    set_correlation_id,
)
from copilot.openemr.roles import (
    ROLE_DENIED_EVENT,
    Role,
    authorize_clinical_access,
    resolve_role,
)

OAUTH_BASE = "http://oemr.test/oauth2/default"
USERINFO_URL = f"{OAUTH_BASE}/userinfo"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openemr_base_url="http://oemr.test",
        openemr_fhir_base="http://oemr.test/apis/default/fhir",
        openemr_oauth_base=OAUTH_BASE,
    )


@pytest.fixture
def log_stream() -> io.StringIO:
    """Redirect structlog JSON output into an in-memory buffer for assertions."""

    buffer = io.StringIO()
    configure_logging(level="INFO", stream=buffer)
    yield buffer
    configure_logging()


def _log_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


class _StubTokens:
    """Minimal ``TokenSource``: returns a fixed access token."""

    def __init__(self, token: str = "user-access-token") -> None:
        self._token = token

    def get_access_token(self) -> str:
        return self._token


class _FailingTokens:
    """A token source whose acquisition fails (e.g. disabled client)."""

    def get_access_token(self) -> str:
        raise RuntimeError("token acquisition failed")


def _mock_userinfo(**claims: object) -> None:
    respx.get(USERINFO_URL).mock(return_value=httpx.Response(200, json=claims))


def _jwt(payload: dict) -> str:
    """Build an unsigned JWT-shaped access token carrying ``payload`` (scope tests)."""

    def _seg(obj: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_seg({'alg': 'none'})}.{_seg(payload)}.sig"


# ---------------------------------------------------------------------------
# resolve_role — physician
# ---------------------------------------------------------------------------


@respx.mock
async def test_physician_via_userinfo_group_claim(settings: Settings) -> None:
    # OpenEMR surfaces the user's ACL group directly in a claim.
    _mock_userinfo(preferred_username="drwho", group="Physicians")

    role = await resolve_role(client=_StubTokens(), settings=settings)

    assert role is Role.PHYSICIAN
    assert authorize_clinical_access(role) is True


@respx.mock
async def test_physician_via_username_role_map_default_admin(settings: Settings) -> None:
    # No group claim; the dev physician ``admin`` is mapped out of the box.
    _mock_userinfo(sub="admin")

    role = await resolve_role(client=_StubTokens(), settings=settings)

    assert role is Role.PHYSICIAN
    assert authorize_clinical_access(role) is True


@respx.mock
async def test_physician_not_denied(settings: Settings, log_stream: io.StringIO) -> None:
    _mock_userinfo(preferred_username="admin", role="Clinicians")

    role = await resolve_role(client=_StubTokens(), settings=settings)
    assert authorize_clinical_access(role) is True

    denials = [ln for ln in _log_lines(log_stream) if ln["event"] == ROLE_DENIED_EVENT]
    assert denials == []


# ---------------------------------------------------------------------------
# resolve_role — front office (denied + audited)
# ---------------------------------------------------------------------------


@respx.mock
async def test_front_office_via_group_claim_denied_and_logged(
    settings: Settings, log_stream: io.StringIO
) -> None:
    _mock_userinfo(preferred_username="frontdesk", group="Front Office")

    token = set_correlation_id("corr-role")
    try:
        role = await resolve_role(client=_StubTokens(), settings=settings)
        allowed = authorize_clinical_access(role, user="frontdesk")
    finally:
        reset_correlation_id(token)

    assert role is Role.FRONT_OFFICE
    assert allowed is False

    denials = [ln for ln in _log_lines(log_stream) if ln["event"] == ROLE_DENIED_EVENT]
    assert denials, "expected a copilot.audit.role_denied audit event"
    entry = denials[-1]
    assert entry["role"] == "front_office"
    assert entry["user"] == "frontdesk"
    assert entry["correlation_id"] == "corr-role"
    assert entry["reason"]  # non-empty human-readable justification


@respx.mock
async def test_front_office_via_configured_user(settings: Settings) -> None:
    # A username with no group claim, mapped via an injected role table
    # (the OPENEMR_FRONT_OFFICE_USER path the M2-5 acceptance uses).
    _mock_userinfo(sub="reception1")

    role = await resolve_role(
        client=_StubTokens(),
        settings=settings,
        role_map={"reception1": Role.FRONT_OFFICE},
    )

    assert role is Role.FRONT_OFFICE
    assert authorize_clinical_access(role) is False


@respx.mock
async def test_front_office_group_wins_over_physician_group(settings: Settings) -> None:
    # Belonging to several groups: a front-office membership is decisive (deny).
    _mock_userinfo(preferred_username="mixed", groups=["Physicians", "Front Office"])

    role = await resolve_role(client=_StubTokens(), settings=settings)

    assert role is Role.FRONT_OFFICE


# ---------------------------------------------------------------------------
# resolve_role — fail closed
# ---------------------------------------------------------------------------


@respx.mock
async def test_unresolvable_userinfo_fails_closed(
    settings: Settings, log_stream: io.StringIO
) -> None:
    # userinfo rejects the token → identity unresolved → OTHER → denied.
    respx.get(USERINFO_URL).mock(return_value=httpx.Response(401))

    role = await resolve_role(client=_StubTokens(), settings=settings)
    assert role is Role.OTHER
    assert authorize_clinical_access(role) is False

    denials = [ln for ln in _log_lines(log_stream) if ln["event"] == ROLE_DENIED_EVENT]
    assert denials, "an unresolved role must be denied and logged"


@respx.mock
async def test_unknown_username_fails_closed(settings: Settings) -> None:
    # userinfo resolves an identity, but it maps to no known role.
    _mock_userinfo(sub="stranger")

    role = await resolve_role(client=_StubTokens(), settings=settings)

    assert role is Role.OTHER
    assert authorize_clinical_access(role) is False


@respx.mock
async def test_token_acquisition_failure_fails_closed(settings: Settings) -> None:
    # The bearer token can't even be obtained → no identity → fail closed.
    role = await resolve_role(client=_FailingTokens(), settings=settings)

    assert role is Role.OTHER
    assert authorize_clinical_access(role) is False


# ---------------------------------------------------------------------------
# resolve_role — token clinical-scope signal (userinfo unavailable)
# ---------------------------------------------------------------------------


@respx.mock
async def test_physician_via_token_clinical_scopes(settings: Settings) -> None:
    # userinfo 404s (as the real OpenEMR build does); the acting token's granted
    # clinical user/*.read scopes are the borrowed-identity signal → PHYSICIAN.
    respx.get(USERINFO_URL).mock(return_value=httpx.Response(404))
    token = _jwt({"sub": "a-uuid", "scopes": ["user/Observation.read", "user/Condition.read"]})

    role = await resolve_role(client=_StubTokens(token), settings=settings)

    assert role is Role.PHYSICIAN
    assert authorize_clinical_access(role) is True


@respx.mock
async def test_non_clinical_scoped_token_fails_closed(settings: Settings) -> None:
    # userinfo 404s and the token grants only non-clinical scopes (no clinical
    # user/*.read resources) and no resolvable username → OTHER → denied.
    respx.get(USERINFO_URL).mock(return_value=httpx.Response(404))
    token = _jwt({"sub": "x", "scopes": ["openid", "user/Appointment.read"]})

    role = await resolve_role(client=_StubTokens(token), settings=settings)

    assert role is Role.OTHER
    assert authorize_clinical_access(role) is False


# ---------------------------------------------------------------------------
# authorize_clinical_access — in isolation
# ---------------------------------------------------------------------------


def test_authorize_allows_only_physician(log_stream: io.StringIO) -> None:
    assert authorize_clinical_access(Role.PHYSICIAN) is True
    assert authorize_clinical_access(Role.FRONT_OFFICE, user="fo") is False
    assert authorize_clinical_access(Role.OTHER) is False

    denials = [ln for ln in _log_lines(log_stream) if ln["event"] == ROLE_DENIED_EVENT]
    # Two denials (front office + other), none for the physician.
    assert len(denials) == 2
    assert {d["role"] for d in denials} == {"front_office", "other"}
