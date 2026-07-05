#!/bin/bash
set -euo pipefail
#
# health-check.sh — per-task health check for the advanced-features example.
#
# Runs on the HOST after the before_run hook and before the Copilot container
# starts. A non-zero exit skips the run with status: setup_failed (the run is
# never executed). Resolved task/variant vars are exported as EVAL_<KEY>.
#
# This demo check just verifies a required var is present; a real check would
# probe whatever external state the task depends on (a deployed resource, a
# reachable service, a seeded database, ...).

echo "[health] checking environment for variant=${EVAL_VARIANT_LABEL:-unknown}"

if [[ -z "${EVAL_VARIANT_LABEL:-}" ]]; then
  echo "[health] ✗ EVAL_VARIANT_LABEL not set — environment not ready"
  exit 1
fi

echo "[health] ✓ environment ready"
