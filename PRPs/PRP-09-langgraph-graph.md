# PRP-09 — LangGraph supervisor + 2 workers

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-06, PRP-08 · **Blocks:** PRP-10 · **Needs key:** no (workers stubbed in tests)

## Goal (atomic)
The inspectable multi-agent graph (FR-5): one supervisor routing to
`intake-extractor` and `evidence-retriever`, with **explicit, logged handoffs**
and correlation-ID-scoped spans (each worker a child of the supervisor span).

## Context / files owned
- `agent/src/copilot/graph/state.py`, `graph/supervisor.py`, `graph/workers.py`,
  `agent/tests/test_graph.py`.
- `intake-extractor` wraps PRP-06 (`attach_and_extract`); `evidence-retriever`
  wraps PRP-08 (`retrieve_evidence`). Supervisor decides ordering + termination.

## Contract
- `GraphState` — `{correlation_id, patient_id, question, attachments,
  extracted: list, evidence: list, handoffs: list[Handoff], done: bool}`.
- `Handoff` — `{from_node, to_node, reason, at}` (inspectable routing record).
- `run_graph(GraphInput) -> GraphResult` — supervisor: needs extraction (a doc
  is attached / referenced)? → intake-extractor; needs evidence? →
  evidence-retriever; both satisfied? → done. Every decision appends a `Handoff`.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_graph.py -q
```
- Routing (stubbed workers): a doc-bearing question visits extractor → retriever
  → done; an evidence-only question **skips** extraction; handoff log records
  each decision with a human-readable reason (test).
- correlation_id present on every node/span; worker spans nest under supervisor
  (assert on the emitted trace structure).
- No infinite loops: supervisor terminates; a max-steps guard is tested.
- Runs with workers stubbed — no live key.

## Builder prompt (backend-dev → qa)
> Implement a LangGraph graph in `graph/`: `GraphState` (carrying correlation_id,
> question, attachments, extracted, evidence, handoffs, done), a supervisor that
> routes to an `intake-extractor` node (wraps PRP-06) and an `evidence-retriever`
> node (wraps PRP-08) and terminates when both needs are met, appending an
> inspectable `Handoff{from,to,reason,at}` on every decision. Nest worker spans
> under the supervisor span with correlation_id. Add a max-steps guard. Test
> (workers stubbed, no key): doc-question routes extractor→retriever→done;
> evidence-only skips extraction; handoff log + reasons present; spans nest;
> terminates. ruff + pytest green. Hand to qa.
