"""In-memory, server-side cache of chart-document VLM extractions (PRP-17).

**Why in-memory (not Redis/DB/disk).** An extracted :class:`LabReport` /
:class:`IntakeFacts` is PHI. Keeping it only in the agent process's RAM creates
**no new persistent PHI store** — it inherits the running app's safeguards and is
ephemeral (gone on restart), so it is HIPAA-compliant *by inheritance*, exactly
like :mod:`copilot.orchestrator.summary_cache`. A durable cache would trigger the
full Security Rule (encryption at rest, access control, audit, retention) **and a
BAA with the storage host** — deliberately avoided. If ever multi-replica, move
this into the already-encrypted+audited DB, not a new service.

**Why key on ``(patient_id, document_id)`` only.** Both are stable, non-PHI
identifiers — no clinical value ever enters the key or a log line. The OpenEMR
``Binary/<id>`` document id is version-scoped (a replaced document gets a new id),
so a cached extraction can never go stale under a live id. Warming is what makes
the flow fast: when the doctor **reads** a chart document
(:func:`copilot.documents.chart_read.ingest_chart_document`) the extraction is
stored here; a subsequent **ask** grounded on the same document
(:func:`copilot.documents.chart_read.extract_chart_document`) reads the cache and
skips the second (expensive) VLM call entirely.

**Content cache only.** The caller must authorize and audit *before* consulting
the cache — this module never authorizes, fetches, or bypasses the audit trail. A
bounded LRU keeps RAM in check.
"""

from __future__ import annotations

from collections import OrderedDict

from copilot.documents.schemas import IntakeFacts, LabReport

__all__ = ["ExtractionCache", "cache_key", "extract_cache"]

#: A cached extraction is the same schema-validated model the read produced.
ExtractedReport = LabReport | IntakeFacts


def cache_key(patient_id: str, document_id: str) -> tuple[str, str]:
    """Compose the cache key: patient + stable OpenEMR document id (no PHI)."""

    return (patient_id, document_id)


class ExtractionCache:
    """A bounded, process-wide LRU map of cache-key -> extracted report."""

    def __init__(self, maxsize: int = 128) -> None:
        self._store: OrderedDict[tuple[str, str], ExtractedReport] = OrderedDict()
        self._maxsize = maxsize

    def get(self, patient_id: str, document_id: str) -> ExtractedReport | None:
        key = cache_key(patient_id, document_id)
        entry = self._store.get(key)
        if entry is not None:
            self._store.move_to_end(key)  # mark most-recently-used
        return entry

    def put(self, patient_id: str, document_id: str, value: ExtractedReport) -> None:
        key = cache_key(patient_id, document_id)
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


#: The process-wide (server-side) cache. Shared across all requests to this agent.
extract_cache = ExtractionCache()
