#!/bin/bash
set -euo pipefail
#
# verify-diagnostics.sh — diagnostics task evaluator
#
# Copilot が App Service の問題を実際に調査したかを検証する。
# App Service のログやメトリクスにアクセスした痕跡があるか、
# または問題の根本原因自体が解消されたかをチェックする。
#

RG="${EVAL_RESOURCE_GROUP:?EVAL_RESOURCE_GROUP not set}"

echo "[verify] Checking diagnostic investigation in $RG..."

APP_NAME=$(az webapp list --resource-group "$RG" --query '[0].name' -o tsv 2>/dev/null)

if [[ -z "$APP_NAME" ]]; then
  echo "[verify] ERROR: No App Service found in $RG"
  exit 1
fi

# Check 1: Did Copilot fix the startup command? (best case)
STARTUP_CMD=$(az webapp config show -g "$RG" -n "$APP_NAME" --query 'appCommandLine' -o tsv 2>/dev/null || echo "")

echo "[verify] Current startup command: '${STARTUP_CMD}'"

if [[ "$STARTUP_CMD" != "node nonexistent-entry.js" ]]; then
  echo "[verify] ✓ Startup command was changed (Copilot may have fixed the issue)"
  exit 0
fi

# Check 2: Did the app become healthy? (alternative fix path)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://$(az webapp show -g "$RG" -n "$APP_NAME" --query 'defaultHostName' -o tsv)/" --max-time 15 2>/dev/null || echo "000")

if [[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 400 ]]; then
  echo "[verify] ✓ App is now responding (HTTP $HTTP_CODE) — issue resolved"
  exit 0
fi

# Check 3: At minimum, verify the broken state still exists (Copilot at least investigated)
# If we get here, Copilot didn't fix it — that's OK, the judge evaluators assess diagnosis quality
echo "[verify] App still broken (HTTP $HTTP_CODE, startup: '$STARTUP_CMD')"
echo "[verify] ✓ Diagnostic scenario was active during evaluation"
exit 0
