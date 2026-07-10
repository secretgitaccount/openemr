"""In-memory session + CSRF-state store for the SMART launch flow.

After the SMART handshake the agent holds a **clinician-bound** access token and
the launch **patient** context. Those are kept server-side, keyed by an opaque
random session id handed to the browser as an HttpOnly cookie — the cookie never
carries the token itself.

Two short-lived stores:

* **launch state** — a one-time CSRF nonce created at ``/launch`` and consumed at
  ``/launch/callback`` (guards the authorization-code redirect).
* **sessions** — ``session id → (clinician token, patient)`` used by the summary
  path so reads run as the launched clinician.

Both are process-local with a TTL. That is correct for a single instance; a
multi-replica deployment needs a shared store (Redis/signed cookie) — tracked as
part of the FULL INTEGRATION hardening tail.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from copilot.openemr.smart import SmartToken

__all__ = [
    "SESSION_COOKIE",
    "SmartSession",
    "remember_state",
    "consume_state",
    "create_session",
    "get_session",
]

SESSION_COOKIE = "copilot_session"

#: How long a launch may sit between ``/launch`` and ``/launch/callback``.
_STATE_TTL_SECONDS = 300
#: Session lifetime cap (independent of the access token's own expiry).
_SESSION_TTL_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class SmartSession:
    """A launched clinician's active session."""

    access_token: str
    patient: str | None
    provider: str | None
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


# state -> (token_endpoint, expiry): the token endpoint discovered at /launch is
# carried across the redirect so /callback knows where to exchange the code.
_states: dict[str, tuple[str, float]] = {}
_sessions: dict[str, SmartSession] = {}


def _sweep_states() -> None:
    now = time.time()
    for k in [k for k, (_, exp) in _states.items() if now >= exp]:
        _states.pop(k, None)


def _sweep_sessions() -> None:
    now = time.time()
    for k in [k for k, s in _sessions.items() if now >= s.expires_at]:
        _sessions.pop(k, None)


# --- CSRF launch state ------------------------------------------------------


def remember_state(token_endpoint: str) -> str:
    """Mint a one-time launch state bound to the discovered token endpoint."""

    _sweep_states()
    state = secrets.token_urlsafe(24)
    _states[state] = (token_endpoint, time.time() + _STATE_TTL_SECONDS)
    return state


def consume_state(state: str) -> str | None:
    """Return the state's token endpoint exactly once (then it's gone); else None."""

    entry = _states.pop(state, None)
    if entry is None:
        return None
    token_endpoint, expiry = entry
    return token_endpoint if time.time() < expiry else None


# --- Sessions ---------------------------------------------------------------


def create_session(token: SmartToken, *, provider: str | None = None) -> str:
    """Store a clinician session; returns the opaque session id for the cookie."""

    _sweep_sessions()
    sid = secrets.token_urlsafe(32)
    ttl = min(_SESSION_TTL_SECONDS, max(60, token.expires_in))
    _sessions[sid] = SmartSession(
        access_token=token.access_token,
        patient=token.patient,
        provider=provider,
        expires_at=time.time() + ttl,
    )
    return sid


def get_session(sid: str | None) -> SmartSession | None:
    """Return the live session for a cookie value, or None if absent/expired."""

    if not sid:
        return None
    session = _sessions.get(sid)
    if session is None:
        return None
    if session.expired:
        _sessions.pop(sid, None)
        return None
    return session
