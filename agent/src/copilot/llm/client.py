"""The grounded-summary LLM client (PRP M1-5, FR-8/FR-11).

:class:`LLMClient` is the single LLM step of the walking skeleton: given the
minimum-necessary retrieved records it produces a
:class:`~copilot.schemas.output.GroundedSummary` whose every claim binds to
source records present in the input. Grounding is enforced by output **shape**
(Anthropic structured output against the ``GroundedSummary`` schema), not by
prompting alone.

Design rules that shape this module:

* **Minimum-necessary PHI to the model (NFR-4).** The user payload carries only
  the fields needed to reason and cite — record ids, resource types, names,
  values, and timestamps. It is sent to Anthropic over TLS; it is **never**
  written to a trace (the ``llm.summarize`` span records token counts and the
  model, not the payload).
* **Surface, don't fabricate (FR-11).** A model refusal or an unparseable
  response raises a typed :class:`LLMError` rather than inventing a summary.
* **Retry only transient failures.** Network errors, timeouts, 429, and 5xx are
  retried with exponential backoff via ``tenacity``; a permanent failure (4xx,
  refusal, parse error) fails fast.

The Anthropic SDK is constructed lazily: importing this module never requires a
key, and the "no key configured" error only fires when a live call is attempted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import Any

import pydantic
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from copilot.config import Settings, get_settings
from copilot.llm.prompts import SYSTEM_PROMPT
from copilot.logging import CORRELATION_ID_HEADER, current_correlation_id, get_logger
from copilot.observability import trace
from copilot.schemas.clinical import (
    Allergy,
    CriticalSet,
    Deltas,
    Encounter,
    LabResult,
    Medication,
    Problem,
)
from copilot.schemas.conversation import ConversationTurn, GroundedAnswer
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

__all__ = ["LLMClient", "LLMError"]

logger = get_logger(__name__)

# A grounded summary is small; this cap is generous headroom well under the
# non-streaming HTTP-timeout ceiling.
_MAX_TOKENS = 4096

# Placeholder marker in the default dev config — a key containing it is not real.
_PLACEHOLDER_MARKER = "xxxx"

# The follow-up system prompt: same grounding contract as SYSTEM_PROMPT, but the
# job is to answer a specific question against the *retained* records of the
# patient already in context (FR-7). Pronouns ("her", "his") resolve to that
# pinned patient — the prompt never re-selects a chart.
FOLLOWUP_SYSTEM_PROMPT = """\
You are a clinical co-pilot answering a follow-up question about the patient the \
clinician is already reviewing. You are given, as JSON: the follow-up `question`, \
the prior conversation `history` (oldest first), and the minimum-necessary \
retrieved `records` for that one patient (medications, allergies, labs, problems, \
and the deltas since the last visit). Each record carries a `source` object with \
a `resource_type` and `id`.

Answer only about the patient in these records. Pronouns in the question (e.g. \
"her", "his", "their") refer to that patient — never infer a different patient.

Hard rules:
- Every clinical claim in `answer` MUST carry the `source` of each record it is \
drawn from. Put those source pointers in the claim's `sources` list, copying \
`resource_type` and `id` verbatim from the input. If a record supplies a \
`timestamp`, copy it too.
- NEVER assert a clinical fact that is not backed by a retrieved record. Do not \
infer, extrapolate, or add general medical knowledge as if it were this patient's \
data. If you cannot cite it, do not say it.
- If the records do not contain what was asked, say so in `caveats` rather than \
guessing. Distinguish "no data on file" (the category was not retrieved) from \
"no known ..." (retrieved but empty).

`caveats` are plain-language limitations or hedges and are NOT grounded claims — \
do not attach sources to them. Be concise and specific."""


# ---------------------------------------------------------------------------
# Stub-LLM mode (load testing) — canned, source-bound output, zero Anthropic
# ---------------------------------------------------------------------------
#
# When ``Settings.copilot_llm_stub`` is set (env ``COPILOT_LLM_STUB=1``) the
# client returns a deterministic, grounded summary/answer built from the
# retrieved records instead of calling Anthropic. This lets the Locust load
# tests in ``loadtest/`` measure end-to-end throughput of the retrieve → gate →
# verify → stream path without any token spend. The canned claims cite real
# source records from the input so they survive the grounding verifier (M1-6).


def _first_source(critical_set: CriticalSet) -> SourceRef | None:
    """Return one real :class:`SourceRef` from the retrieved records, if any."""

    if critical_set.medications:
        return critical_set.medications[0].source
    if critical_set.problems:
        return critical_set.problems[0].source
    if critical_set.labs:
        return critical_set.labs[0].source
    if critical_set.allergies:
        return critical_set.allergies[0].source
    return None


def _stub_summary(critical_set: CriticalSet, deltas: Deltas) -> GroundedSummary:
    """Build a deterministic, source-bound summary for stub-LLM (load) mode."""

    must_knows: list[Claim] = []
    for med in critical_set.medications[:3]:
        must_knows.append(
            Claim(text=f"On {med.name} ({med.status}).", sources=[med.source])
        )
    for lab in critical_set.labs[:2]:
        if lab.abnormal:
            must_knows.append(
                Claim(
                    text=f"Abnormal {lab.name}: {lab.value} {lab.unit or ''}".strip() + ".",
                    sources=[lab.source],
                )
            )

    whats_changed: list[Claim] = [
        Claim(text=f"New medication since last visit: {med.name}.", sources=[med.source])
        for med in deltas.new_meds[:3]
    ]

    return GroundedSummary(
        headline="Stub-LLM load-test summary (no Anthropic call).",
        must_knows=must_knows,
        whats_changed=whats_changed,
        caveats=["Generated in stub-LLM mode for load testing; not clinical output."],
    )


def _stub_answer(critical_set: CriticalSet, deltas: Deltas) -> GroundedAnswer:
    """Build a deterministic, source-bound follow-up answer for stub-LLM mode."""

    source = _first_source(critical_set)
    if source is not None:
        answer = [Claim(text="Stub-LLM load-test answer.", sources=[source])]
        caveats: list[str] = ["Generated in stub-LLM mode for load testing."]
    else:
        answer = []
        caveats = ["No records on file; stub-LLM mode for load testing."]
    return GroundedAnswer(answer=answer, caveats=caveats)


class LLMError(RuntimeError):
    """The LLM step failed and produced no trustworthy summary (FR-11).

    ``retriable`` distinguishes a transient failure (network blip, 5xx, 429 — a
    retry may help) from a permanent one (refusal, parse failure, missing key).
    Messages never contain the clinical payload.
    """

    def __init__(self, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.retriable = retriable


# ---------------------------------------------------------------------------
# Minimum-necessary payload construction (NFR-4)
# ---------------------------------------------------------------------------


def _source(ref: SourceRef) -> dict[str, Any]:
    """A citable pointer the model copies verbatim into a claim's ``sources``."""

    payload: dict[str, Any] = {"resource_type": ref.resource_type, "id": ref.id}
    if ref.timestamp is not None:
        payload["timestamp"] = ref.timestamp.isoformat()
    return payload


def _medication(med: Medication) -> dict[str, Any]:
    return {
        "id": med.id,
        "name": med.name,
        "status": med.status,
        "dosage": med.dosage,
        "source": _source(med.source),
    }


def _allergy(allergy: Allergy) -> dict[str, Any]:
    return {
        "id": allergy.id,
        "substance": allergy.substance,
        "reaction": allergy.reaction,
        "criticality": allergy.criticality,
        "source": _source(allergy.source),
    }


def _lab(lab: LabResult) -> dict[str, Any]:
    return {
        "id": lab.id,
        "name": lab.name,
        "value": lab.value,
        "unit": lab.unit,
        "effective": lab.effective.isoformat() if lab.effective else None,
        "abnormal": lab.abnormal,
        "source": _source(lab.source),
    }


def _problem(problem: Problem) -> dict[str, Any]:
    return {
        "id": problem.id,
        "name": problem.name,
        "clinical_status": problem.clinical_status,
        "onset": problem.onset.isoformat() if problem.onset else None,
        "source": _source(problem.source),
    }


def _encounter(encounter: Encounter) -> dict[str, Any]:
    return {
        "id": encounter.id,
        "kind": encounter.kind,
        "start": encounter.start.isoformat() if encounter.start else None,
        "source": _source(encounter.source),
    }


def _deltas(deltas: Deltas) -> dict[str, Any]:
    return {
        "reference_visit": (
            deltas.reference_visit.isoformat() if deltas.reference_visit else None
        ),
        "new_meds": [_medication(m) for m in deltas.new_meds],
        "stopped_meds": [_medication(m) for m in deltas.stopped_meds],
        "new_problems": [_problem(p) for p in deltas.new_problems],
        "new_labs": [_lab(lab) for lab in deltas.new_labs],
        "new_encounters": [_encounter(e) for e in deltas.new_encounters],
    }


def build_payload(critical_set: CriticalSet, deltas: Deltas) -> str:
    """Serialise the minimum-necessary records into the user-message JSON.

    Only ids, resource types, names, values, and timestamps are included — the
    fields the model needs to reason and to cite. ``missing`` names the tiers
    that were not retrieved so the model can say "no data on file" rather than
    inferring a negative finding.
    """

    data: dict[str, Any] = {
        "medications": [_medication(m) for m in critical_set.medications],
        "allergies": [_allergy(a) for a in critical_set.allergies],
        "labs": [_lab(lab) for lab in critical_set.labs],
        "problems": [_problem(p) for p in critical_set.problems],
        "missing": list(critical_set.missing),
        "deltas": _deltas(deltas),
    }
    return json.dumps(data, separators=(",", ":"))


def build_followup_payload(
    question: str,
    history: list[ConversationTurn],
    critical_set: CriticalSet,
    deltas: Deltas,
) -> str:
    """Serialise a follow-up into the minimum-necessary user-message JSON.

    Carries the question, the prior turns so pronouns resolve, and the pinned
    patient's retrieved records (same minimum-necessary shape as
    :func:`build_payload`). Only ids, resource types, names, values, and
    timestamps of the records are included — never more than the one-shot summary
    already sends.
    """

    data: dict[str, Any] = {
        "question": question,
        "history": [{"role": turn.role, "text": turn.text} for turn in history],
        "records": json.loads(build_payload(critical_set, deltas)),
    }
    return json.dumps(data, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Retry classification
# ---------------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    """True for Anthropic failures worth retrying (network, timeout, 429, 5xx)."""

    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500
    return False


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LLMClient:
    """Produce a source-bound :class:`GroundedSummary` from retrieved records.

    ``client`` may be injected (tests wire an :class:`AsyncAnthropic` to a mock
    transport); otherwise one is constructed lazily from :class:`Settings` on
    first use, which is also when a missing API key is surfaced.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: AsyncAnthropic | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._model = self._settings.anthropic_model
        self._max_attempts = max_attempts
        # Load-test stub: when set, return canned source-bound output instead of
        # calling Anthropic (see ``_stub_summary`` / ``_stub_answer``).
        self._stub = self._settings.copilot_llm_stub

    # -- lifecycle ---------------------------------------------------------

    def _anthropic(self) -> AsyncAnthropic:
        """Return the Anthropic client, constructing it (and validating the key).

        Raises :class:`LLMError` — not at import, but here, when a live call is
        attempted without a real key configured.
        """

        if self._client is None:
            key = self._settings.anthropic_api_key
            if not key or _PLACEHOLDER_MARKER in key.lower():
                raise LLMError(
                    "ANTHROPIC_API_KEY is not configured; set a real key in the "
                    "environment / .env before calling the LLM.",
                    retriable=False,
                )
            self._client = AsyncAnthropic(api_key=key)
        return self._client

    # -- public API --------------------------------------------------------

    async def summarize(self, critical_set: CriticalSet, deltas: Deltas) -> GroundedSummary:
        """Summarise the retrieved records into a grounded, source-bound summary.

        Builds the minimum-necessary user payload, calls Sonnet with the
        ``GroundedSummary`` output schema attached, and returns the parsed model.
        The ``llm.summarize`` span records the model and token counts — never the
        payload. Raises :class:`LLMError` on a refusal or a parse failure (FR-11),
        or when transient retries are exhausted.
        """

        if self._stub:
            # Load-test path: never touch Anthropic (per the cost design).
            logger.info("llm.summarize.stub", model=self._model)
            return _stub_summary(critical_set, deltas)

        payload = build_payload(critical_set, deltas)
        client = self._anthropic()

        with trace("llm.summarize", metadata={"model": self._model}) as span:
            message = await self._call(
                client, payload, system=SYSTEM_PROMPT, output_format=GroundedSummary
            )

            if message.stop_reason == "refusal":
                logger.warning("llm.summarize.refusal", model=self._model)
                raise LLMError(
                    "the model refused to produce a summary; surfacing rather "
                    "than fabricating one.",
                    retriable=False,
                )

            summary = message.parsed_output
            if summary is None:
                logger.warning("llm.summarize.unparseable", model=self._model)
                raise LLMError(
                    "the model returned no parseable GroundedSummary.",
                    retriable=False,
                )

            usage = message.usage
            span.update(
                metadata={
                    "model": self._model,
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "must_knows": len(summary.must_knows),
                    "whats_changed": len(summary.whats_changed),
                }
            )
            return summary

    async def answer_followup(
        self,
        question: str,
        history: list[ConversationTurn],
        critical_set: CriticalSet,
        deltas: Deltas,
    ) -> GroundedAnswer:
        """Answer a follow-up about the pinned patient, grounded in its records.

        Builds the minimum-necessary user payload — the question, the prior turns
        (so pronouns like "her" resolve to the patient already in context), and
        that patient's retained records — calls Sonnet with the ``GroundedAnswer``
        output schema attached, and returns the parsed model. The ``llm.followup``
        span records the model and token counts — never the payload or the
        conversation text. Raises :class:`LLMError` on a refusal or a parse failure
        (FR-11), or when transient retries are exhausted.
        """

        if self._stub:
            # Load-test path: never touch Anthropic (per the cost design).
            logger.info("llm.followup.stub", model=self._model)
            return _stub_answer(critical_set, deltas)

        payload = build_followup_payload(question, history, critical_set, deltas)
        client = self._anthropic()

        with trace("llm.followup", metadata={"model": self._model}) as span:
            message = await self._call(
                client, payload, system=FOLLOWUP_SYSTEM_PROMPT, output_format=GroundedAnswer
            )

            if message.stop_reason == "refusal":
                logger.warning("llm.followup.refusal", model=self._model)
                raise LLMError(
                    "the model refused to answer the follow-up; surfacing rather "
                    "than fabricating one.",
                    retriable=False,
                )

            answer = message.parsed_output
            if answer is None:
                logger.warning("llm.followup.unparseable", model=self._model)
                raise LLMError(
                    "the model returned no parseable GroundedAnswer.",
                    retriable=False,
                )

            usage = message.usage
            span.update(
                metadata={
                    "model": self._model,
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "answer_claims": len(answer.answer),
                    "turns": len(history),
                }
            )
            return answer

    # -- internals ---------------------------------------------------------

    async def _call(
        self,
        client: AsyncAnthropic,
        payload: str,
        *,
        system: str,
        output_format: type[Any],
    ) -> Any:
        """Call ``messages.parse`` with tenacity retry on transient failures.

        The correlation id (when set) is threaded as ``X-Correlation-ID`` so the
        call is traceable end-to-end. ``system`` and ``output_format`` are supplied
        by the caller (summary vs follow-up) so the retry, correlation-id, and
        error-translation logic is shared. A parse failure (pydantic
        ``ValidationError``) is not transient and is translated into a typed
        :class:`LLMError` here.
        """

        cid = current_correlation_id()
        extra_headers = {CORRELATION_ID_HEADER: cid} if cid else None

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=0.2, max=2.0),
                retry=retry_if_exception(_is_transient),
                reraise=True,
            ):
                with attempt:
                    return await client.messages.parse(
                        model=self._model,
                        max_tokens=_MAX_TOKENS,
                        system=system,
                        messages=[{"role": "user", "content": payload}],
                        output_format=output_format,
                        extra_headers=extra_headers,
                    )
        except pydantic.ValidationError as exc:
            raise LLMError(
                "the model's structured output did not satisfy the "
                "GroundedSummary schema.",
                retriable=False,
            ) from exc
        except (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError) as exc:
            raise LLMError(
                "the LLM call failed to complete after retries.", retriable=True
            ) from exc
        except APIStatusError as exc:
            raise LLMError(
                f"the LLM call failed with HTTP {exc.status_code}.",
                retriable=exc.status_code >= 500,
            ) from exc
        raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# CLI smoke — LIVE end-to-end (retrieve -> Sonnet -> printed GroundedSummary)
# ---------------------------------------------------------------------------


def _print_summary(summary: GroundedSummary) -> None:
    print("  headline:", summary.headline)
    print("  must-knows:")
    for claim in summary.must_knows:
        srcs = ", ".join(f"{s.resource_type}/{s.id}" for s in claim.sources) or "(ungrounded)"
        print(f"    - {claim.text}  [{srcs}]")
    print("  what's changed:")
    for claim in summary.whats_changed:
        srcs = ", ".join(f"{s.resource_type}/{s.id}" for s in claim.sources) or "(ungrounded)"
        print(f"    - {claim.text}  [{srcs}]")
    if summary.caveats:
        print("  caveats:")
        for caveat in summary.caveats:
            print(f"    - {caveat}")


async def _smoke(patient_id: str | None) -> int:
    """Retrieve a patient's critical set, summarise it with Sonnet, print it.

    LIVE only: needs a running OpenEMR stack *and* ``ANTHROPIC_API_KEY``. The
    critical-set builder is imported lazily so this module has no import-time
    dependency on retrieval PRPs; if it is not present yet the smoke is BLOCKED
    with a clear message rather than crashing.
    """

    from copilot.logging import configure_logging, new_correlation_id, set_correlation_id
    from copilot.observability import flush

    configure_logging()
    settings = get_settings()
    correlation_id = new_correlation_id()
    set_correlation_id(correlation_id)
    print(f"LLM summarize smoke — model {settings.anthropic_model}")
    print(f"  correlation_id: {correlation_id}")

    try:
        from copilot.orchestrator.retrieve import build_critical_set  # type: ignore
    except Exception:
        print(
            "BLOCKED: no critical-set builder is available yet "
            "(copilot.orchestrator.retrieve.build_critical_set). This LIVE smoke "
            "depends on the M1 retrieval/orchestration PRPs; run it once they land.",
            file=sys.stderr,
        )
        return 3

    try:
        critical_set, deltas = await build_critical_set(patient_id)
    except Exception as exc:  # pragma: no cover - live path
        print(f"FAILED: could not retrieve the critical set: {exc}", file=sys.stderr)
        return 2

    client = LLMClient(settings=settings)
    try:
        summary = await client.summarize(critical_set, deltas)
    except LLMError as exc:
        print(f"FAILED: LLM step failed: {exc}", file=sys.stderr)
        return 2

    _print_summary(summary)
    flush()
    print("SMOKE OK — grounded, source-bound summary produced by Sonnet.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.llm.client",
        description="LIVE smoke: retrieve -> Sonnet -> grounded GroundedSummary.",
    )
    parser.add_argument(
        "--patient",
        dest="patient",
        default=None,
        help="FHIR Patient id to summarise (omit to auto-discover the first patient).",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_smoke(args.patient))


if __name__ == "__main__":
    raise SystemExit(main())
