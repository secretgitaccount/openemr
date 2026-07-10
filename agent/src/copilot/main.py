"""FastAPI application factory and router wiring.

`create_app()` builds the ASGI application; `app` is the module-level instance
used by `uvicorn copilot.main:app`. Routers from later PRPs (health/ready in
M0-3, etc.) are mounted here — each PRP adds a single documented include line.
"""

from __future__ import annotations

from fastapi import FastAPI

from copilot import __version__
from copilot.config import get_settings
from copilot.logging import configure_logging
from copilot.health import router as health_router
from copilot.api.summary import router as summary_router
from copilot.api.chat import router as chat_router
from copilot.api.prewarm import router as prewarm_router
from copilot.api.ui import router as ui_router
from copilot.api.launch import router as launch_router
from copilot.middleware import CorrelationIdMiddleware


def create_app() -> FastAPI:
    """Construct and return the Clinical Co-Pilot FastAPI application."""

    # Touch settings at startup so misconfiguration fails fast and loudly.
    get_settings()

    # Install JSON structured logging before anything logs (M0-2).
    configure_logging()

    app = FastAPI(
        title="Clinical Co-Pilot",
        version=__version__,
        description=(
            "Conversational clinical co-pilot for OpenEMR — grounded, cited "
            "point-of-care synthesis over the OAuth2/SMART-FHIR API."
        ),
    )

    # --- Middleware ---
    # M0-2 correlation-logging: correlation ID + structured request logging.
    app.add_middleware(CorrelationIdMiddleware)

    # --- Router wiring ---
    app.include_router(health_router)  # M0-3 health-ready: /health, /ready
    app.include_router(summary_router)  # M1-7 orchestrator: POST /patients/{id}/summary
    app.include_router(chat_router)  # M2-5 conversation: start + follow-up messages
    app.include_router(prewarm_router)  # M2-5 prewarm: POST /prewarm
    app.include_router(ui_router)  # demo UI: GET / (page) + GET /patients
    app.include_router(launch_router)  # SMART EHR launch: GET /launch + /launch/callback
    # (later PRPs append their single include line here)

    return app


app = create_app()
