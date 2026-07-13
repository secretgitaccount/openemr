# PRP-00 spike result — OpenEMR write path (LOCAL docker, localhost:8300)

**Verdict: REST-only write path. No FHIR-native writes.** Evidence gathered
read-only from the live local `GET /metadata` CapabilityStatement, the
`apis/routes/` route tables, the REST controllers/services, and the Week-1 OAuth
scope config. No records were created (read-only mandate) — nothing to clean up.
One gap left for PRP-05 to close with live POSTs: confirm the exact 2xx body
shapes (and delete the synthetic records afterward).

## Probes

| Probe | Result |
|---|---|
| `POST /fhir/Observation` | **NOT SUPPORTED** — CapabilityStatement `Observation.interaction = [search-type, read]`; no create route (would 404/405). |
| `POST /fhir/DocumentReference` (+Binary) | **NOT USABLE** — only `POST /fhir/DocumentReference/$docref` (CCD *generation*, not ingestion); no plain create route; `Binary` is read-only. |
| dedup / traceability | source doc: `documents.hash` column + `GET /api/patient/:pid/document?path=` returns `id/name/hash/docdate`. derived: bind to one ingestion-encounter per source; dedup `(pid, eid, date)`. FHIR identifier/tag dedup unavailable. |
| scopes vs current client | Current client is **read-only** (`openid offline_access api:fhir` + `user/*.read`). Missing all writes: `api:oemr`, `user/document.crs`, `user/encounter.crus`, `user/vital.crus`. |

## Recommendation for PRP-05 (base `http://localhost:8300/apis/default`)

- **Store source PDF →** `POST /api/patient/:pid/document` — multipart field name
  **`document`** (NOT `file`), with `path` (+ optional `eid`) as **query-string**
  params. Scopes: `api:oemr` + `user/document.crs`. `insertAtPath` returns
  `true` (not an id) → recover id via the `GET …/document?path=` listing.
- **Persist derived value →** create/reuse `POST /api/patient/:puuid/encounter`
  (`user/encounter.crus`), then `POST /api/patient/:pid/encounter/:eid/vital`
  (`user/vital.crus`) for vitals; use `soap_note` / `medical_problem` /
  `allergy` / `medication` REST endpoints for other derived data. All need the
  `api:oemr` base scope.
- **Dedup key:** source-content **SHA-256** → deterministic filename in a
  dedicated per-patient `path` folder (e.g. "Clinical Co-Pilot"), pre-checked
  against the document listing (`documents.hash`) before writing. Derived records
  bound to **one ingestion encounter per source document** so re-running
  `attach_and_extract` upserts instead of duplicating (satisfies FR-10).

## Blockers PRP-05 must handle (LOCAL setup)

1. **Re-register a write-scoped OAuth client** — current client is read-only.
   New OpenEMR clients register **disabled** (`is_enabled=0`) → admin must enable
   (`UPDATE oauth_clients SET is_enabled=1`). Already modeled as
   `ClientNotEnabledError` in `oauth.py`.
2. **Enable the Standard REST API (`api:oemr`)** in OpenEMR (Admin → Config →
   Connectors); the password-grant `admin` user needs patients/docs + encounter
   write ACLs (admin has these by default).
3. **Doc-upload gotcha:** `STANDARD_API.md` Example 5 (`-F file=@…`) is wrong for
   this build — the route reads `$_FILES['document']` and `path` from the query
   string. Use field name `document` + query params or the upload silently fails.

## Spec compliance
Requirement §1 accepts "FHIR resources **or OpenEMR records**." REST-persisted
OpenEMR records (documents, encounter, vitals/observations) satisfy it. The
citation contract still points back to the stored record's id + our extraction
`{doc, page, field}`.
