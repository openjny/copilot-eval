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
BICEP_FILE="${SCRIPT_DIR}/../infra/main.bicep"

echo "[reset] Resetting $RG to baseline state..."

# Ensure RG exists
az group show --name "$RG" &>/dev/null || \
  az group create --name "$RG" --location "$LOCATION" --output none

# Get SP object ID for SQL admin (from AZURE_CLIENT_ID in .env)
SQL_ADMIN_OID=""
if [[ -n "${AZURE_CLIENT_ID:-}" ]]; then
  SQL_ADMIN_OID=$(az ad sp show --id "$AZURE_CLIENT_ID" --query id -o tsv 2>/dev/null || echo "")
fi
if [[ -z "$SQL_ADMIN_OID" ]]; then
  echo "[reset] WARNING: Could not resolve SQL admin object ID, skipping reset"
  exit 0
fi

# Deploy in Complete mode: removes anything not in the template
az deployment group create \
  --resource-group "$RG" \
  --mode Complete \
  --template-file "$BICEP_FILE" \
  --parameters location="$LOCATION" sqlAdminObjectId="$SQL_ADMIN_OID" \
  --output none

echo "[reset] Environment reset complete"
