#!/bin/bash
set -euo pipefail
#
# reset-environment.sh — Reset the Azure environment to a known state.
# Called as before_run hook for all tasks to ensure reproducible eval runs.
#
# Uses Bicep Complete mode to declaratively reset the RG to the baseline state.
# Any resources Copilot created/modified in a previous run are removed or restored.
#

RG="${EVAL_RESOURCE_GROUP:?EVAL_RESOURCE_GROUP not set}"
LOCATION="${EVAL_LOCATION:-southeastasia}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BICEP_FILE="${SCRIPT_DIR}/../infra/patterns/resource-explorer/main.bicep"
PARAM_FILE="${SCRIPT_DIR}/../infra/patterns/resource-explorer/main.bicepparam"

echo "[reset] Resetting $RG to baseline state..."

# Ensure RG exists
az group show --name "$RG" &>/dev/null || \
  az group create --name "$RG" --location "$LOCATION" --output none

# Deploy in Complete mode: removes anything not in the template
DEPLOY_ARGS=(
  --resource-group "$RG"
  --mode Complete
  --template-file "$BICEP_FILE"
  --parameters location="$LOCATION"
  --output none
)

if [[ -f "$PARAM_FILE" ]]; then
  DEPLOY_ARGS+=(--parameters "@${PARAM_FILE}")
fi

az deployment group create "${DEPLOY_ARGS[@]}"

echo "[reset] Environment reset complete"
