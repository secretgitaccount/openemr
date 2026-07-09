# Clinical Co-Pilot — Postman collection

A runnable Postman collection covering every public endpoint of the Clinical
Co-Pilot FastAPI service (`copilot.main:app`), with the conversation → follow-up
chain wired so you can exercise the multi-turn flow without reading any source.

## Files

| File | Purpose |
| --- | --- |
| `clinical-copilot.postman_collection.json` | One request per endpoint, grouped into `health`, `summary`, `conversation`, `prewarm` folders. |
| `clinical-copilot.postman_environment.json` | Local environment: `base_url`, `patient_id`, `provider_id` (and a `conversation_id` slot the chain fills in). |

## Endpoints covered

| Folder | Request | Endpoint |
| --- | --- | --- |
| health | Health (liveness) | `GET /health` |
| health | Ready (readiness) | `GET /ready` |
| summary | Patient summary (streamed NDJSON) | `POST /patients/{patient_id}/summary` |
| conversation | Start conversation | `POST /patients/{patient_id}/conversation` |
| conversation | Send follow-up message | `POST /conversations/{conversation_id}/messages` |
| prewarm | Prewarm today's schedule | `POST /prewarm` |

## Import + run in the Postman app

1. **Import** → drop in both JSON files (collection + environment).
2. In the environment selector (top-right), pick **Clinical Co-Pilot — Local**.
3. Start the service locally so `base_url` resolves:

   ```bash
   cd agent
   source .venv/bin/activate
   uvicorn copilot.main:app --port 8000
   ```

4. Run requests top-to-bottom. Set `patient_id` in the environment to a patient
   in your local OpenEMR/Synthea data.

### Headers

- **`X-Provider-Id`** — the acting provider. Defaults to `admin` server-side when
  absent; sourced here from the `{{provider_id}}` variable.
- **`X-Break-Glass-Reason`** — an explicit break-glass justification, set on the
  **summary** and **start-conversation** requests. Locally, Synthea patients have
  no schedule, so this header is the only way the *granted* happy path is
  reachable. Remove it (or blank it) to exercise the normal gated path, which
  refuses an out-of-panel patient with a single `refusal` event.

### Streaming responses

The summary, conversation, and follow-up endpoints stream newline-delimited JSON
(`application/x-ndjson`). Postman renders the full raw stream in the response body
once the stream completes.

### The conversation → follow-up chain

**Start conversation** has a test script that parses the NDJSON stream, finds the
`conversation` event, and stores its `conversation_id` into the collection's
`conversation_id` variable. The **Send follow-up message** request then targets
`/conversations/{{conversation_id}}/messages` automatically — so run
*Start conversation* first, then *Send follow-up message*.

If the start request is refused (out-of-panel / role-denied, e.g. no break-glass
header), no `conversation` event is emitted, nothing is captured, and the
follow-up request will `404`.

## Run headlessly with newman (CLI)

[`newman`](https://github.com/postmanlabs/newman) runs the collection from the
command line (install once with `npm install -g newman`):

```bash
# Whole collection against the local environment
newman run postman/clinical-copilot.postman_collection.json \
  -e postman/clinical-copilot.postman_environment.json

# Just the health folder (no OpenEMR data required)
newman run postman/clinical-copilot.postman_collection.json \
  -e postman/clinical-copilot.postman_environment.json \
  --folder health
```

Newman honours the same folder order, so the conversation chain works headlessly
too: the *Start conversation* test script sets `conversation_id` in the run's
variable scope before the *Send follow-up message* request executes.
