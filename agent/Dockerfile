# Clinical Co-Pilot agent — container image
#
# Deployed on Railway as its own service (PRD §12/§14). The build installs the
# pinned runtime deps first (better layer caching), then the package itself, and
# runs under a non-root user. The container honours Railway's injected $PORT and
# falls back to 8000 for local `docker run`.
#
# Week-2 (PRP-14) adds the hybrid-RAG + multi-agent stack: langgraph, faiss-cpu,
# rank-bm25, and sentence-transformers (which pulls torch). Two build choices
# keep that deploy-ready:
#   1. torch is installed from the CPU-only wheel index first, so the image does
#      not pull the multi-GB CUDA runtime it can never use on Railway.
#   2. the embedding + reranker weights are pre-fetched into an in-image HF cache
#      so query time is fully offline (HIPAA posture) and the first request does
#      not pay a cold model download. Pre-fetch is best-effort: a transient
#      HuggingFace outage at build time falls back to the code's lazy first-call
#      download rather than failing the whole image build.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    # Local, writable model cache — keeps weights in the image and off the
    # network at query time. Owned by appuser via the chown below.
    HF_HOME=/app/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/huggingface \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# Install the CPU-only torch wheel FIRST so sentence-transformers reuses it
# instead of resolving the default (CUDA-bearing) build from PyPI.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install the remaining dependencies (better layer caching).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install the package itself.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# Pre-fetch the RAG models into the in-image HF cache (best-effort — falls back
# to the lazy first-call download if HuggingFace is unreachable at build time).
RUN python -c "import copilot.rag.index as i, copilot.rag.retrieve as r; \
i._get_embedder(); r._get_reranker(); print('RAG models pre-fetched')" \
    || echo "warn: RAG model pre-fetch skipped (will lazy-load at first request)"

# Run as a non-root user (defence in depth; Railway does not require root). The
# chown covers the pre-fetched model cache so appuser can read it at runtime.
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
