# Clinical Co-Pilot agent — container image
#
# Deployed on Railway as its own service (PRD §12/§14). The build installs the
# pinned runtime deps first (better layer caching), then the package itself, and
# runs under a non-root user. The container honours Railway's injected $PORT and
# falls back to 8000 for local `docker run`.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install the package itself.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# Run as a non-root user (defence in depth; Railway does not require root).
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Liveness probe: /health performs no dependency I/O, so it is safe as a
# container-level check. Uses stdlib urllib (no curl in the slim base image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health', timeout=4).status==200 else sys.exit(1)"]

# Shell form so ${PORT} is expanded at runtime (Railway injects PORT).
CMD uvicorn copilot.main:app --host 0.0.0.0 --port ${PORT:-8000}
