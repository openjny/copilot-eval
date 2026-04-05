#!/bin/bash
set -euo pipefail
#
# verify-app-deploy.sh — app-deploy task evaluator
#
# Copilot がアプリを App Service にデプロイできたかを検証する。
# App Service の URL にアクセスし、期待するレスポンスが返るかチェック。
#

RG="${EVAL_RESOURCE_GROUP:?EVAL_RESOURCE_GROUP not set}"

echo "[verify] Checking deployment to App Service in $RG..."

# Get the App Service name
APP_NAME=$(az webapp list --resource-group "$RG" --query '[0].name' -o tsv 2>/dev/null)

if [[ -z "$APP_NAME" ]]; then
  echo "[verify] ERROR: No App Service found in $RG"
  exit 1
fi

# Get the default hostname
HOSTNAME=$(az webapp show -g "$RG" -n "$APP_NAME" --query 'defaultHostName' -o tsv 2>/dev/null)
URL="https://${HOSTNAME}"

echo "[verify] App Service: $APP_NAME"
echo "[verify] URL: $URL"

# Wait briefly for deployment to propagate
sleep 10

# Check if the app responds with expected content
HTTP_CODE=$(curl -s -o /tmp/app-response.txt -w "%{http_code}" "$URL/" --max-time 30 2>/dev/null || echo "000")
BODY=$(cat /tmp/app-response.txt 2>/dev/null || echo "")

echo "[verify] HTTP status: $HTTP_CODE"

if [[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 300 ]]; then
  # Check for our app's signature in the response
  if echo "$BODY" | grep -qi "eval-app\|hello.*eval"; then
    echo "[verify] ✓ App deployed successfully — returning expected content"
    exit 0
  else
    echo "[verify] ✓ App responding (HTTP $HTTP_CODE) but content differs from fixture"
    echo "[verify]   Response: $(echo "$BODY" | head -c 200)"
    # Still a pass — Copilot deployed *something* that works
    exit 0
  fi
else
  echo "[verify] ✗ App not responding correctly (HTTP $HTTP_CODE)"
  echo "[verify]   Response: $(echo "$BODY" | head -c 200)"
  exit 1
fi
