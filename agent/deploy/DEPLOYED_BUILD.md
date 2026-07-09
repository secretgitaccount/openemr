# Deployed build — status & gap to full documentation parity

This records what the **currently deployed Railway build ("Build 1")** actually
does, versus what `ARCHITECTURE.md` / `PRD.md` describe as the target system, so
the gap is explicit and the next steps are unambiguous.

## Build 1 (deployed) — what is live

- **URL:** `https://copilot-agent-production-daf5.up.railway.app`, reading from
  Railway OpenEMR `https://openemr-production-7925.up.railway.app`.
- **Identity model: single service identity.** The agent authenticates with the
  OAuth2 **password grant** as one fixed user (`OPENEMR_DEV_USER=admin`). **Every
  request runs as `admin`, regardless of who is logged into OpenEMR.** It does
  **not** auto-detect the logged-in clinician.
- **Front end:** a standalone demo UI (`src/copilot/ui/index.html`) served by the
  agent — *not* a panel embedded inside OpenEMR.
- Everything downstream of identity is real: panel gate, role gate, parallel
  retrieval, grounded+cited synthesis, deterministic verification, rule engine,
  Langfuse observability + cost, and the Langfuse eval framework.

## The next step for the final build (the big one)

**Attach the agent to the specific clinician who is logged into OpenEMR**, via
the **SMART EHR-launch `authorization_code` flow** (ARCHITECTURE §2/§3/§6):

1. An embedded OpenEMR side panel performs a SMART launch on patient open.
2. OpenEMR returns a token **bound to that logged-in clinician**.
3. The panel passes that token to the agent; the agent uses it for all FHIR
   reads, so reads happen **as that doctor** and OpenEMR's own ACL + the panel
   gate key off their real identity/schedule.

This is what turns it from a single-`admin` service into a true multi-clinician,
borrowed-identity tool. It is two related pieces: **(a)** the `authorization_code`
/ SMART-launch exchange in `copilot.openemr.oauth` (today only password grant is
implemented), and **(b)** the embedded side-panel front end that initiates it.

## Other gaps to match all the documentation (honest list)

Beyond SMART launch, these doc claims are not yet fully met by the deployed build:

| Doc claim | Deployed reality | Effort |
| --- | --- | --- |
| ARCH §2: front end is an **embedded OpenEMR side panel** doing the SMART handshake | Standalone `index.html` served by the agent | Medium (paired with SMART launch) |
| ARCH §7.4: **circuit breakers** on OpenEMR + LLM | Not implemented in code | Small–medium |
| ARCH §7.4: **≥2 replicas** behind the load balancer | 1 replica (Railway default) | Trivial (Railway config) |
| ARCH §7.4: **private networking** agent↔OpenEMR (IPv6 internal) | Uses the public OpenEMR URL | Small (config; watch the HTTP:80 redirect-loop gotcha) |
| ARCH §2 / §4.4: `/ready` **asserts OpenEMR audit globals are on** | `audit_globals` check is a `skipped` TODO in `health.py` | Small (wire the assertion) |
| ARCH §5.3: **encryption at rest** via MariaDB `file_key_management` | Not verified on the Railway DB (managed MySQL cannot run it) | Verify / config |

**Not gaps — already disclosed as intentional tradeoffs** (ARCHITECTURE §6): the
FHIR high-sensitivity ACL is a documented un-remediated gap, and the rule engine
has deliberately bounded coverage. These are honest limitations, not
doc/build mismatches.

## Summary

SMART-launch per-user identity (+ the embedded panel) is the **most important**
remaining work and the headline difference between Build 1 and the documented
target. It is **not the only** item — the §7.4 production-hardening claims
(circuit breakers, ≥2 replicas, private networking, the audit-globals assertion)
and the at-rest-encryption claim also need closing for full parity. Most of those
are small/config; SMART launch and the embedded panel are the real remaining
engineering.
