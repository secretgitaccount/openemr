"""Smoke tests for the project scaffold (PRP M0-1)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from copilot.config import Settings, get_settings
from copilot.main import app, create_app


def test_create_app_returns_fastapi() -> None:
    built = create_app()
    assert isinstance(built, FastAPI)
    assert built.title == "Clinical Co-Pilot"
    assert built.version


def test_module_level_app_is_fastapi() -> None:
    assert isinstance(app, FastAPI)


def test_get_settings_is_cached_and_typed() -> None:
    settings = get_settings()
    assert isinstance(settings, Settings)
    # cached: same instance across calls
    assert get_settings() is settings
    # defaults let the app boot without a populated .env
    assert settings.openemr_base_url.startswith("http")
    assert isinstance(settings.agent_port, int)
    assert settings.log_level


def test_docs_endpoint_served() -> None:
    with TestClient(app) as client:
        resp = client.get("/docs")
        assert resp.status_code == 200
        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200
        assert openapi.json()["info"]["title"] == "Clinical Co-Pilot"
