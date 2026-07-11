"""In-memory, server-side cache of Claude-generated patient summaries.

**Why in-memory (not Redis/DB/disk).** A generated summary is PHI. Keeping it only
in the agent process's RAM creates **no new persistent PHI store** — it inherits
the running app's safeguards and is ephemeral (gone on restart), so it is
HIPAA-compliant *by inheritance*. A durable cache would trigger the full Security
Rule (encryption at rest, access control, audit, retention) **and a BAA with the
storage host** — deliberately avoided. If ever multi-replica, move this into the
already-encrypted+audited DB, not a new service.

**Why hash-keyed with no time limit.** Freshness is guaranteed by the *data*, not
a clock. The key is ``patient + generation_version + hash(exact LLM input)``:

* any change to the retrieved chart data changes the hash → auto-regenerate, so a
  **stale summary can never be served**;
* ``generation_version`` (model + prompt/rules) invalidates on intentional
  generation-logic changes;
* an unchanged chart's summary stays valid indefinitely — age alone never makes it
  wrong. We always run the cheap FHIR retrieval to compute the hash; we only skip
  the expensive Claude call when nothing changed.

**Content cache only.** The caller must authorize (panel + role gate) and audit
*before* consulting the cache — this module never authorizes or bypasses the audit
trail. A bounded LRU keeps RAM in check.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from copilot.schemas.clinical import CriticalSet, Deltas
from copilot.verification.gate import VerifiedSummary

__all__ = ["CachedSummary", "SummaryCache", "content_hash", "cache_key", "summary_cache"]


@dataclass(frozen=True, slots=True)
class CachedSummary:
    """A stored summary plus when Claude generated it."""

    verified: VerifiedSummary
    generated_at: datetime


def content_hash(critical_set: CriticalSet, deltas: Deltas | None) -> str:
    """Stable hash of the *exact clinical input to the LLM*.

    Excludes ``retrieved_at`` (which changes every fetch and would defeat the
    cache); everything else — meds, allergies, labs, problems, deltas, and each
    record's own source timestamps — is included, so any real chart change flips
    the hash.
    """

    payload = {
        "cs": critical_set.model_dump(mode="json", exclude={"retrieved_at"}),
        "deltas": deltas.model_dump(mode="json") if deltas is not None else None,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_key(
    patient_id: str, critical_set: CriticalSet, deltas: Deltas | None, version: str
) -> str:
    """Compose the full cache key: patient + generation version + data hash."""

    return f"{patient_id}|{version}|{content_hash(critical_set, deltas)}"


class SummaryCache:
    """A bounded, process-wide LRU map of cache-key → :class:`CachedSummary`."""

    def __init__(self, maxsize: int = 256) -> None:
        self._store: OrderedDict[str, CachedSummary] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: str) -> CachedSummary | None:
        entry = self._store.get(key)
        if entry is not None:
            self._store.move_to_end(key)  # mark most-recently-used
        return entry

    def put(self, key: str, value: CachedSummary) -> None:
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


#: The process-wide (server-side) cache. Shared across all requests to this agent.
summary_cache = SummaryCache()
