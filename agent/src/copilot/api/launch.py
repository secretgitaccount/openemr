"""SMART EHR-launch endpoints — the clinician-identity handshake.

``GET /launch``           — entry point OpenEMR's launch button points at. Pins
                            the issuer, mints a CSRF state, and redirects the
                            browser to OpenEMR's ``/authorize``.
``GET /launch/callback``  — OpenEMR redirects back here with a ``code``; we
                            exchange it for a clinician-bound token + patient,
                            open a session, and bounce to the UI on that patient.

See :mod:`copilot.openemr.smart` for the handshake mechanics and
:mod:`copilot.smart_session` for the session/state stores.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from copilot.config import get_settings
from copilot.logging import get_logger
from copilot.openemr.oauth import OAuthError, register_client
from copilot.openemr.smart import (
    build_authorize_url,
    discover_endpoints,
    exchange_code,
    validate_issuer,
)
from copilot.smart_session import SESSION_COOKIE, consume_state, create_session, remember_state

__all__ = ["router"]

logger = get_logger(__name__)

router = APIRouter(tags=["smart-launch"])


def _secure_cookie() -> bool:
    return get_settings().agent_base_url.lower().startswith("https")


@router.get("/launch", response_model=None)
async def launch(
    iss: str = Query(..., description="FHIR issuer (OpenEMR) initiating the launch"),
    launch: str = Query(..., description="Opaque SMART launch context token"),
    aud: str | None = Query(default=None, description="Intended audience (FHIR base)"),
) -> RedirectResponse | JSONResponse:
    """Begin the EHR launch: pin the issuer, then redirect to ``/authorize``."""

    settings = get_settings()
    if not validate_issuer(iss, settings):
        logger.warning("openemr.smart.launch_rejected", reason="issuer_mismatch")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_issuer", "detail": "Launch issuer is not this agent's OpenEMR."},
        )

    try:
        credentials = register_client(settings=settings)
    except OAuthError as exc:
        return JSONResponse(status_code=502, content={"error": exc.code, "detail": "client unavailable"})

    endpoints = discover_endpoints(iss, settings=settings)
    state = remember_state(endpoints.token)
    url = build_authorize_url(
        endpoints=endpoints,
        launch=launch,
        state=state,
        aud=aud or iss,
        credentials=credentials,
        settings=settings,
    )
    logger.info("openemr.smart.launch_started")
    return RedirectResponse(url=url, status_code=302)


@router.get("/launch/callback", response_model=None)
async def launch_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse | JSONResponse:
    """Finish the handshake: exchange the code, open a session, open the patient."""

    settings = get_settings()
    if error:
        return JSONResponse(status_code=400, content={"error": error, "detail": "authorization denied"})
    token_endpoint = consume_state(state or "")
    if not code or token_endpoint is None:
        # Missing code, or an unknown/replayed/expired state — fail closed.
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_state", "detail": "Launch state missing, expired, or already used."},
        )

    try:
        credentials = register_client(settings=settings)
        token = exchange_code(
            code=code, token_endpoint=token_endpoint, credentials=credentials, settings=settings
        )
    except OAuthError as exc:
        logger.warning("openemr.smart.callback_failed", code=exc.code)
        return JSONResponse(status_code=502, content={"error": exc.code, "detail": "code exchange failed"})

    sid = create_session(token)
    target = "/"
    if token.patient:
        target = f"/?patient={token.patient}"
    response = RedirectResponse(url=target, status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(),
        max_age=token.expires_in,
        path="/",
    )
    logger.info("openemr.smart.session_opened", has_patient=bool(token.patient))
    return response
