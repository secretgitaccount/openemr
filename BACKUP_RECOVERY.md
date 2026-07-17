# Backup & Recovery — Clinical Co-Pilot (Week 2)

How each Week-2 artifact is backed up, the manual recovery procedure if automated
backup fails, and the RPO/RTO targets. The guiding principle: **the agent owns no
authoritative datastore of its own** — every artifact is either owned by OpenEMR
(which has its own backup path) or reproducible from the committed repo.

## Artifacts, ownership, and backup path

| Artifact | Authoritative store | Backup mechanism | Reproducible from repo alone? |
|---|---|---|---|
| **Eval golden set + baseline** | the git repo (`agent/tests/eval/golden/`) | git history + remote (GitLab) | **Yes** — cases + `baseline.json` are committed files; the gate runs `run_golden()` from them with no database |
| **Guideline corpus** (RAG source) | the git repo (`agent/src/copilot/rag/corpus/*.md`) | git history + remote | **Yes** — plain markdown, version-controlled |
| **FAISS / BM25 index** | derived (in-memory, built at runtime) | none needed — derived data | **Yes** — rebuilt in <1s from the committed corpus |
| **Source documents** (lab PDF / intake form) | OpenEMR documents store | OpenEMR's DB + document-volume backup | No — restored from OpenEMR backup |
| **Derived observations** (labs → encounter/vital records) | OpenEMR (MySQL) | OpenEMR's DB backup | No — but re-derivable by re-ingesting the source document (idempotent via SHA dedup) |
| **Observability data** (traces, per-encounter metrics) | Langfuse + structured logs | Langfuse retention; log aggregation | N/A — operational telemetry, PHI-free, not a system of record |

The eval golden set in particular **does not live only in a database** — it is
reproducible from the repo alone, satisfying the engineering requirement directly.

## RPO / RTO

| Data class | RPO (max data loss) | RTO (max downtime) | Basis |
|---|---|---|---|
| Repo-owned (golden set, corpus, code, index) | **0** | **minutes** | git remote is the backup; recovery = clone + rebuild venv + reinstall hooks |
| OpenEMR-owned (source docs, derived records) | = OpenEMR's DB backup interval (deployment-defined; e.g. daily snapshot → ≤24h) | = OpenEMR restore time | inherits OpenEMR's backup posture; the agent adds no new RPO risk because derived records are re-derivable from source |
| Observability telemetry | best-effort (non-authoritative) | N/A (agent serves without it — `record_event` no-ops when Langfuse is down) | metrics are diagnostic, not a system of record |

## Manual recovery procedures

**Agent service (repo-owned state):**
1. `git clone` the repo at the intended commit (GitLab remote is the backup).
2. `cd agent && python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .`
3. `make install-hooks` (re-activates the PR-blocking pre-push gate).
4. `make ci` to confirm the golden set + gate reproduce green from the repo alone.
5. Redeploy (Railway) per `agent/DEPLOY_RAILWAY.md`. The FAISS/BM25 index rebuilds
   automatically on first retrieval — nothing to restore.

**Derived OpenEMR records (if lost but the source document survives):**
- Re-run the chart-read ingest for the affected document
  (`POST /patients/{id}/chart-documents/{doc_id}/ingest`). The SHA-256 source
  dedup + note-level dedup make re-ingestion idempotent — no duplicate records.

**Source documents (if lost):** restored from OpenEMR's own database/document
backup — this is OpenEMR's recovery path, not the agent's.

## If automated backup fails

- **Repo remote unreachable:** any local clone (including a developer laptop or
  the running Railway build context) is a full copy; push it to a new remote.
- **Langfuse unavailable:** no recovery action needed — the agent degrades to
  serving without telemetry (`/ready` reports `langfuse` as non-gating); metrics
  still emit to the structured logs.
- **OpenEMR backup fails:** falls to OpenEMR's own DR runbook; the agent's
  derived records are re-derivable from surviving source documents as above.
