#!/usr/bin/env bash
#
# Headless Locust load runs at 10 and 50 concurrent users (PRP M3-4, PRD §13).
#
# Records p50/p95/p99 latency + error rate for each scenario to CSV. The agent
# MUST be running under stub-LLM mode so no run spends Anthropic tokens:
#
#   cd agent
#   . .venv/bin/activate
#   COPILOT_LLM_STUB=1 uvicorn copilot.main:app --port 8000 &
#   ./loadtest/run.sh
#
# Environment:
#   COPILOT_HOST         base URL of the running agent (default http://localhost:8000)
#   COPILOT_PATIENT_IDS  comma-separated FHIR Patient ids to summarise (default 1)
#   RUN_TIME             per-scenario duration (default 1m)
#   RESULTS_DIR          where CSVs are written (default loadtest/results)
#
# Output CSVs (Locust --csv writes *_stats.csv with p50/p95/p99 columns):
#   ${RESULTS_DIR}/users10_stats.csv, users10_stats_history.csv, ...
#   ${RESULTS_DIR}/users50_stats.csv, ...
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCUSTFILE="${SCRIPT_DIR}/locustfile.py"

HOST="${COPILOT_HOST:-http://localhost:8000}"
RUN_TIME="${RUN_TIME:-1m}"
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results}"

mkdir -p "${RESULTS_DIR}"

if ! command -v locust >/dev/null 2>&1; then
  echo "ERROR: 'locust' is not installed. Install with: pip install locust" >&2
  exit 127
fi

run_scenario() {
  local users="$1" spawn_rate="$2"
  echo "=== Locust: ${users} users (spawn-rate ${spawn_rate}) for ${RUN_TIME} against ${HOST} ==="
  locust \
    --locustfile "${LOCUSTFILE}" \
    --host "${HOST}" \
    --headless \
    --users "${users}" \
    --spawn-rate "${spawn_rate}" \
    --run-time "${RUN_TIME}" \
    --csv "${RESULTS_DIR}/users${users}" \
    --csv-full-history \
    --reset-stats \
    --only-summary
  echo "--- wrote ${RESULTS_DIR}/users${users}_stats.csv ---"
}

run_scenario 10 2
run_scenario 50 5

echo
echo "Done. Percentile latencies + error counts are in:"
echo "  ${RESULTS_DIR}/users10_stats.csv"
echo "  ${RESULTS_DIR}/users50_stats.csv"
echo "Transcribe the aggregated row into loadtest/BASELINES.md."
