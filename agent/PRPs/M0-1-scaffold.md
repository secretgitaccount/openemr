# PRP M0-1 · Project scaffold

**Milestone:** M0 · **Depends on:** none (venv already provisioned) · **Blocks:** all other M0 PRPs

## Goal
Create the FastAPI application skeleton, typed config, and packaging so every other PRP has a place to plug in and the app boots.

## Context
- PRD FR-15, NFR-3; module layout in `PRPs/README.md`.
- Deps already installed in `agent/.venv` (`requirements.txt`). Do **not** recreate the venv.

## Spec
- Create the package `src/copilot/` with the layout from the README (empty `__init__.py` where needed; stub dirs `openemr/`, `schemas/`, `orchestrator/`, `verification/`).
- `config.py`: `Settings(BaseSettings)` via `pydantic-settings`, reading `.env` — fields for Anthropic, OpenEMR (base/fhir/oauth URLs, dev user/pass, client id/secret), Langfuse, `agent_port`, `log_level`. Provide a cached `get_settings()`.
- `main.py`: `create_app() -> FastAPI` that mounts routers (health added in M0-3), sets title/version, and is runnable via `uvicorn copilot.main:app`.
- `pyproject.toml`: project metadata, `[tool.pytest.ini_options] asyncio_mode = "auto"`, package discovery under `src/`. Make the package importable (`pip install -e .` or `src` on path).
- `Dockerfile`: `FROM python:3.13-slim`, install `requirements.txt`, copy `src/`, `CMD ["uvicorn","copilot.main:app","--host","0.0.0.0","--port","8000"]`.
- `README.md` (agent-level): how to run locally (`. .venv/bin/activate && uvicorn copilot.main:app --reload`).

## Validation
```bash
cd agent && . .venv/bin/activate
pip install -e . -q
python -c "from copilot.main import create_app; from copilot.config import get_settings; create_app(); get_settings(); print('OK')"
# app boots:
uvicorn copilot.main:app --port 8099 & sleep 3; curl -sf localhost:8099/docs >/dev/null && echo "APP UP"; kill %1
```

## Definition of done
`create_app()` and `get_settings()` import and run; the app serves `/docs`; `docker build` of the Dockerfile succeeds (optional check). No business logic yet.
