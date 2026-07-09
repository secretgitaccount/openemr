# Deploy runbook — Clinical Co-Pilot agent on Railway

The agent ships as **its own Railway service** (PRD §12/§14), separate from
OpenEMR. It is a stateless FastAPI app packaged by `agent/Dockerfile`. This
runbook is the operator procedure; the artifacts (`Dockerfile`,
`deploy/railway.json`, `deploy/.env.railway.example`) are committed — only the
`railway up` and secret-setting steps are run by a human.

## What gets deployed

| Artifact | Purpose |
| --- | --- |
| `agent/Dockerfile` | Slim Python 3.13 image, non-root, binds `$PORT`, `HEALTHCHECK` → `/health`. |
| `deploy/railway.json` | Railway service config: Dockerfile builder, start command, `/health` healthcheck, restart policy. |
| `deploy/.env.railway.example` | Env template (no secrets) documenting required/optional variables. |

The **service root is `agent/`** — Railway builds the image from
`agent/Dockerfile` and reads config from `agent/deploy/railway.json`. When you
`railway init`/link, set the service's root directory to `agent`.

## Prerequisites

- Railway CLI installed and authenticated: `railway login`.
- A reachable OpenEMR instance the agent can authenticate to (see **Auth
  boundary** below — this is the load-bearing gotcha).
- A real `ANTHROPIC_API_KEY`.

## Steps

### 1. Link the project/service

```bash
cd agent
railway init            # first time — creates the project
# or, to attach to an existing project:
railway link
```

Point the Railway config at the committed file (it lives under `deploy/`, not
the service root), either in the dashboard (Settings → Config-as-code path =
`deploy/railway.json`) or via the environment variable:

```bash
railway variables --set RAILWAY_CONFIG_PATH=deploy/railway.json
```

### 2. Set environment variables (never commit secrets)

Use `deploy/.env.railway.example` as the checklist. Set each on the service —
secrets go through Railway variables, never into git:

```bash
railway variables \
  --set ANTHROPIC_API_KEY=sk-ant-... \
  --set ANTHROPIC_MODEL=claude-sonnet-5 \
  --set OPENEMR_BASE_URL=https://your-openemr.example.com \
  --set OPENEMR_FHIR_BASE=https://your-openemr.example.com/apis/default/fhir \
  --set OPENEMR_OAUTH_BASE=https://your-openemr.example.com/oauth2/default \
  --set OPENEMR_DEV_USER=admin \
  --set OPENEMR_DEV_PASS=... \
  --set LOG_LEVEL=INFO
```

`LANGFUSE_*` are optional — omit them and tracing degrades gracefully (it never
gates readiness). **Do not set `PORT`**: Railway injects it and the container
binds `$PORT` automatically.

### 3. Deploy

```bash
railway up              # builds agent/Dockerfile and deploys
```

### 4. Verify

```bash
railway domain          # prints the public URL, e.g. https://copilot-agent.up.railway.app
BASE=https://<your-domain>

curl -sf  "$BASE/health"   # -> {"status":"ok"}          (liveness, no dep I/O)
curl -sD- "$BASE/ready"    # -> 200 ready / 503 not_ready (probes deps, names failures)
```

- `/health` returning 200 means the process is live (this is the Railway
  healthcheck path in `railway.json`).
- `/ready` returning **503** is expected until the required dependencies
  (OpenEMR reachable, `ANTHROPIC_API_KEY` present) are satisfied. The JSON body
  names which dependency is down — read `checks.openemr` / `checks.anthropic`.

## Auth boundary (read before pointing at production OpenEMR)

The agent authenticates to OpenEMR via the **OAuth2 password grant**
(`oauth_password_grant`), which is a **dev** setting. The **public Railway
OpenEMR production image does NOT enable it by default.** So a deployed agent
pointed at an unconfigured production OpenEMR **cannot obtain a token** and
`/ready` will report OpenEMR unreachable / auth failing.

To run the deployed agent against OpenEMR you need one of:

1. **Password grant enabled on the target OpenEMR** — set `oauth_password_grant`
   on (Admin → Globals → Connectors, or the equivalent global), then register
   the client and set `OPENEMR_CLIENT_ID` / `OPENEMR_CLIENT_SECRET` /
   `OPENEMR_DEV_USER` / `OPENEMR_DEV_PASS` on the Railway service. This is the
   supported M0–M2 path and mirrors the local `development-easy` stack.
2. **SMART EHR-launch auth-code flow** — the production-correct browser-launch
   flow. **This is NOT built in M0–M2**; it is future work. Do not assume the
   deployed agent talks to production OpenEMR without one of these two in place.

Concretely: the safe, working deployment targets an OpenEMR instance you control
with the password grant enabled (e.g. your own Railway OpenEMR with the dev
global flipped, or a staging instance). Do not imply the agent works against a
stock production OpenEMR out of the box — it does not.

## Rollback

```bash
railway deployments           # list deployments
railway redeploy <id>         # roll back to a prior good build
```

## Troubleshooting

- **Build fails** — reproduce locally: `cd agent && docker build -t copilot-agent -f Dockerfile .`
- **Container boots but `/ready` is 503** — inspect `curl -s "$BASE/ready" | jq`.
  `checks.anthropic == not_configured` → `ANTHROPIC_API_KEY` missing/placeholder.
  `checks.openemr == unreachable` → wrong `OPENEMR_BASE_URL` or password grant
  disabled (see Auth boundary).
- **Crash on boot** — `railway logs`. The app boots with defaults even without a
  populated env, so a hard crash usually means a bad `railway.json` start command.
