# Clinical Co-Pilot — Target User & Use Cases

This document is the source of truth the architecture traces back to. Every agent capability in `ARCHITECTURE.md` must map to a use case defined here. If a capability has no use case below, it should not be built.

**Scope decision:** the co-pilot has one narrow target user — the physician at the point of care. Other roles (nurse/MA, front office) appear in this document only to describe the surrounding workflow and to make the authorization boundary concrete and testable. They are not target users.

## 1. The User

**Dr. Alani Reyes — primary care physician, 20-patient clinic day.**

Not "a physician who needs help finding information." A specific person with a specific constraint: Dr. Reyes runs a full outpatient schedule — roughly 20 patients between 8:30 AM and 5:00 PM, in 15–20 minute slots. Between rooms she has, at best, **60–90 seconds** to reorient before walking into the next visit. In that window she needs to recall who this patient is, why they're here, what changed since she last saw them, what's on file, and what actually matters *today*.

Crucially, **she does not do a separate chart-prep session** — physicians don't pre-read twenty charts before clinic. Her need is *at the door*, in the moment. Today she meets it by scanning dense encounter notes and flipping between the problem list, the medication list, and recent labs — under time pressure, with the patient already waiting. The cost of that friction is missed medication changes, overlooked abnormal labs, and rushed context-building the co-pilot can compress.

**Why this user, and why narrow:** the point-of-care recall moment is exactly what the project scopes ("90 seconds between patient rooms"), and it maps cleanly onto data OpenEMR holds (schedule, encounters, problem list, medications, labs). It also has a hard, honest tolerance profile that constrains every downstream decision.

### What she needs (and doesn't)

* Needs: the *delta* since last visit, active meds and allergies, recent/abnormal labs, open problems, and today's likely agenda — fast, and grounded in her real chart.
* Does not need: generic medical education, a full-chart data dump, or anything she must verify by hand before trusting.

### Her tolerance profile (this constrains the architecture)

* **Latency:** first useful content in ~1–2 seconds; a multi-second "thinking" spinner loses her.
* **Trust:** zero tolerance for a confident wrong statement; she must be able to see the source of any claim.
* **Failure:** a partial answer that says what's missing is fine; a silent gap or a crash is not.

## 2. The Clinical Workflow — Who Does What

The co-pilot lives inside a real, multi-person workflow. Grounding it in OpenEMR's actual roles (see `AUDIT.md` for the permission detail) is what keeps the design honest:

* **Front office** books and checks in the day's patients. In OpenEMR terms this role has the calendar and patient-report access only — no clinical chart access.
* **Nurse / medical assistant** rooms each patient: vitals, medication reconciliation, history updates, note-taking. This maps to OpenEMR's **Clinicians** role, which holds the charting permissions (medical history, labs, prescriptions, notes) but is capped at *normal* sensitivity and cannot sign or code. **This is the person who actually preps the chart** — not the physician.
* **Physician (Dr. Reyes)** performs the visit. Her non-delegable work is in-room recall, clinical decision-making, and attestation (signing results, coding). In OpenEMR's role model her role is also granted `sensitivities/high` (nurses are not) — though our audit found this sensitivity distinction is *not* enforced on the FHIR read path, so it's a documented gap rather than a live guarantee (see UC-5). She arrives to an already-roomed, already-charted patient and needs instant synthesis of it.

The co-pilot's job is not to replace the nurse's prep. It is to give the *physician* an instant, grounded synthesis of everything — the nurse's fresh charting included — at the moment she opens the room.

## 3. Where the Agent Enters Her Day

The co-pilot is an embedded **side panel inside OpenEMR** — not a separate app she logs into. It enters at the **point of care**: when Dr. Reyes opens the next patient (room ready / patient selected), the panel surfaces within a second or two what changed since the last visit, active meds and allergies, new or abnormal labs, open problems, and anything needing attention today — each statement carrying a source she can tap. She can ask a follow-up in plain language ("any med changes from the specialist?") and get a grounded answer. Then she walks in.

There is deliberately **no "open the app and prep" step** for her. Behind the scenes the system pre-computes the day's scheduled patients' context in the background so the click is instant — but that prewarming is an automatic system optimization (documented in `ARCHITECTURE.md`), never a task she performs.

## 4. Use Cases

Each use case states the trigger, what the agent does, and — required — **why a conversational agent is the right shape** rather than a dashboard, a sorted list, or a better chart view.

### UC-1 — "What changed since last visit?"

* **Trigger:** she opens a returning patient at the point of care.
* **Agent:** synthesizes the delta across encounters, meds, problems, and labs since the last visit date; surfaces new diagnoses, med changes, and new abnormal results, each cited.
* **Why an agent:** the "what changed" question spans multiple data types and requires synthesis relative to a moving reference point (last visit). A dashboard shows current state, not the diff; a sorted list can't reason about "new since March 2." Producing the delta is a reasoning task, and it's the single highest-value thing in her 90 seconds. (Justifies LLM synthesis and tool-chaining; the conversational surface itself is justified by UC-3.)

### UC-2 — Today's must-knows (meds, allergies, abnormal labs, open problems)

* **Trigger:** same patient open; delivered as the default summary.
* **Agent:** returns the prioritized critical set — active meds, allergies, recent/abnormal labs, open problems — fetched in parallel and streamed, each grounded.
* **Why an agent:** she needs the 20% that matters now, prioritized, not the whole chart. The agent decides salience (an abnormal potassium outranks a normal one) and expresses it in one grounded summary. A static widget would force her to scan and prioritize herself — the exact work the tool exists to remove.

### UC-3 — Follow-up questions in natural language

* **Trigger:** she asks "any changes from cardiology?" or "is she still on metformin?"
* **Agent:** interprets the question, retrieves the relevant records, answers with citations, and keeps context so she can ask a second follow-up without restating.
* **Why an agent:** this is inherently conversational and open-ended — she can't predict her question in advance, so no fixed UI can pre-answer it. Multi-turn context ("she" = the current patient) is exactly what a dashboard cannot provide. **This use case is what justifies multi-turn conversation existing in the system at all.**

### UC-4 — Safety flags surfaced proactively

* **Trigger:** during any summary, the agent's rule checks find a drug interaction, dosage concern, or allergy contraindication in the retrieved data.
* **Agent:** surfaces the flag with its source, independent of whether the model "noticed" it (the check is deterministic).
* **Why an agent:** a passive chart view relies on her spotting the interaction under time pressure. A tool that actively checks the data against clinical rules and raises the flag does something a static view structurally cannot.

### UC-5 — Enforcing who is asking (multi-user safety)

* **Trigger:** (a) a request for a patient not on her panel; or (b) a lower-privileged role in the same deployment (e.g. front office) requesting clinical-chart data their role cannot access.
* **Agent:** declines and explains, rather than fabricating or over-sharing; break-glass requires an explicit, logged reason.
* **Two boundaries, both real:**
    1. **Panel boundary (we build it).** "Is this patient on Dr. Reyes's schedule or care team?" OpenEMR cannot answer this — its ACL has no patient dimension — so our agent-side patient-panel gate does. Out-of-panel access requires a logged break-glass reason.
    2. **Role boundary (inherited from OpenEMR).** Because the agent acts as the logged-in user, it inherits OpenEMR's role gate at the API. The clearest demonstrable case is **Front Office vs. Physician**: the Front Office role has calendar/patient-report access only and *no* clinical-chart ACL, so a front-office session cannot retrieve chart data through the API, while the physician's can. Same question, different result by role — enforced by OpenEMR's existing ACL, inherited via borrowed identity, never re-implemented in the agent.
* **Known limitation (honest scope).** OpenEMR's *finer* sensitivity ACL (`sensitivities/high`) — which would separate a nurse from a physician on high-sensitivity records specifically — is **not enforced on OpenEMR's FHIR read path** (a gap our audit surfaced; it is enforced in the UI and on writes, but not FHIR reads). Closing it would require a targeted change in OpenEMR's FHIR layer, which we scoped out of this milestone. Our multi-user guarantees therefore rest on the panel gate (patient scoping) and OpenEMR's coarse role gate (role-level chart access), not on the high-sensitivity split.
* **Why it's testable:** a Front Office token vs. a Physician token on the chart endpoints returns a refusal vs. data (demonstrable today), and an out-of-panel patient request returns a refusal. The nurse is not a target user; the roles exist here to make the boundary demonstrable.

## 5. What This User Does *Not* Justify

To keep scope honest: there is no use case here for generic medical Q&A, for autonomous actions (ordering, prescribing), or for full-chart export. Those capabilities are therefore out of scope for the agent. Capability follows the user, not the other way around.
