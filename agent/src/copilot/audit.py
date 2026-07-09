"""Agent-side audit trail for panel-gate decisions (FR-14).

OpenEMR's ``api_log`` records every FHIR call the agent *makes* — but the two
events that matter most for panel scoping never reach it: a **refusal** is a
call the agent deliberately did *not* make, and a **break-glass override** is a
policy decision taken *before* any clinical read. Neither leaves a trace in
``api_log`` on its own, so the agent logs them itself here. These structured
events are the agent-side complement to OpenEMR's server-side log.

Two hard rules (mirroring ``copilot.observability``):

* **Correlation-tagged.** Every event carries the active request's correlation
  ID (stamped automatically by the ``copilot.logging`` processor), so a refusal
  or override can be tied back to the request that triggered it.
* **No clinical values.** By construction these events carry only record
  **IDs** (``patient_id`` / ``provider_id``) and the operator-supplied
  **reason** — never a lab value, diagnosis, or other clinical detail. The
  reason is retained verbatim: it is the justification an auditor needs, and
  redacting it would defeat the point of the audit trail.
"""

from __future__ import annotations

from copilot.logging import get_logger

__all__ = ["audit_refusal", "audit_break_glass"]

logger = get_logger(__name__)

#: structlog event names — stable identifiers a log pipeline can filter on.
REFUSAL_EVENT = "copilot.audit.refusal"
BREAK_GLASS_EVENT = "copilot.audit.break_glass"


def audit_refusal(patient_id: str, provider_id: str, reason: str) -> None:
    """Record that panel-gated access to ``patient_id`` was refused.

    Emitted when a patient is neither on the provider's schedule nor tied to
    them by a recent encounter, so no clinical retrieval happens. Carries the
    ids + human-readable reason (and, via the logging processor, the correlation
    ID); no clinical values.
    """

    logger.info(
        REFUSAL_EVENT,
        patient_id=patient_id,
        provider_id=provider_id,
        reason=reason,
    )


def audit_break_glass(patient_id: str, provider_id: str, reason: str) -> None:
    """Record a break-glass override granting out-of-panel access.

    Emitted when a clinician explicitly overrides panel scoping. The
    ``reason`` is the operator's justification and is retained verbatim for the
    audit trail; the event carries the ids + reason (and the correlation ID via
    the logging processor), never clinical values.
    """

    logger.warning(
        BREAK_GLASS_EVENT,
        patient_id=patient_id,
        provider_id=provider_id,
        reason=reason,
    )
