#!/bin/bash
set -euo pipefail
#
# prepare-diagnostics.sh — diagnostics task before_run hook
#
# 1. 環境をベースライン状態にリセット (reset-environment.sh を呼ぶ)
# 2. 意図的に壊れたアプリを App Service にデプロイして診断シナリオを作る
#
# Copilot は起動に失敗した App Service を発見し、原因を特定する必要がある。
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RG="${EVAL_RESOURCE_GROUP:?EVAL_RESOURCE_GROUP not set}"

# ── Step 1: Reset environment to baseline ──
echo "[prepare-diagnostics] Running environment reset..."
bash "$SCRIPT_DIR/reset-environment.sh"

# ── Step 2: Deploy a broken app to the App Service ──
echo "[prepare-diagnostics] Deploying broken app for diagnostics scenario..."

APP_NAME=$(az webapp list --resource-group "$RG" --query '[0].name' -o tsv 2>/dev/null)

if [[ -z "$APP_NAME" ]]; then
  echo "[prepare-diagnostics] WARNING: No App Service found in $RG, skipping broken app setup"
  exit 0
fi

FIXTURE_DIR="$SCRIPT_DIR/../fixtures/diagnostics"

if [[ ! -d "$FIXTURE_DIR" ]]; then
  echo "[prepare-diagnostics] WARNING: diagnostics fixture not found at $FIXTURE_DIR"
  exit 0
fi

# Package the broken app
TMPZIP=$(mktemp /tmp/broken-app-XXXXXX.zip)
trap "rm -f $TMPZIP" EXIT

(cd "$FIXTURE_DIR" && zip -r "$TMPZIP" . -x '*.git*' >/dev/null 2>&1)

# Deploy via zip deploy
az webapp deploy \
  --resource-group "$RG" \
  --name "$APP_NAME" \
  --src-path "$TMPZIP" \
  --type zip \
  --output none 2>/dev/null || {
    echo "[prepare-diagnostics] WARNING: zip deploy failed, falling back to startup command injection"
  }

# Set wrong startup command to ensure the app crashes
# The app requires('./config') which doesn't exist, but this makes the failure even more obvious
az webapp config set \
  --resource-group "$RG" \
  --name "$APP_NAME" \
  --startup-file "node nonexistent-entry.js" \
  --output none 2>/dev/null || true

# Restart to trigger the error state
az webapp restart \
  --resource-group "$RG" \
  --name "$APP_NAME" \
  --output none 2>/dev/null || true

# Wait for the crash to register in logs
sleep 15

echo "[prepare-diagnostics] ✓ Broken app deployed — App Service should show startup errors"
echo "[prepare-diagnostics]   - Missing module: require('./config')"
echo "[prepare-diagnostics]   - Wrong startup command: node nonexistent-entry.js"
