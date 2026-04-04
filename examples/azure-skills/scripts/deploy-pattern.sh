#!/bin/bash
set -euo pipefail
#
# deploy-pattern.sh — Deploy/reset Azure fixture for a specific pattern
#
# Usage:
#   ./scripts/deploy-pattern.sh resource-explorer         # Initial deploy
#   ./scripts/deploy-pattern.sh resource-explorer --reset  # Complete mode reset
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${PROJECT_DIR}/eval-config.yaml"

PATTERN="${1:?Pattern name required (e.g. resource-explorer)}"
MODE="${2:-}"

# Read config
AZURE_RG=$(yq '.azure.resource_group' "$CONFIG_FILE")
AZURE_LOCATION=$(yq '.azure.location' "$CONFIG_FILE")

BICEP_FILE="${PROJECT_DIR}/infra/patterns/${PATTERN}/main.bicep"
PARAM_FILE="${PROJECT_DIR}/infra/patterns/${PATTERN}/main.bicepparam"

if [[ ! -f "$BICEP_FILE" ]]; then
  echo "ERROR: $BICEP_FILE not found"
  exit 1
fi

# Ensure RG exists
az group show --name "$AZURE_RG" &>/dev/null || \
  az group create --name "$AZURE_RG" --location "$AZURE_LOCATION" --output none

DEPLOY_MODE="Incremental"
if [[ "$MODE" == "--reset" ]]; then
  DEPLOY_MODE="Complete"
  echo "[deploy] Resetting $PATTERN via Bicep Complete mode..."
else
  echo "[deploy] Deploying $PATTERN (Incremental)..."
fi

DEPLOY_ARGS=(
  --resource-group "$AZURE_RG"
  --mode "$DEPLOY_MODE"
  --template-file "$BICEP_FILE"
  --parameters location="$AZURE_LOCATION"
)

if [[ -f "$PARAM_FILE" ]]; then
  DEPLOY_ARGS+=(--parameters "@${PARAM_FILE}")
fi

az deployment group create "${DEPLOY_ARGS[@]}" --output table

echo "[deploy] Done: $PATTERN ($DEPLOY_MODE)"
