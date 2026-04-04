#!/bin/bash
set -euo pipefail
#
# verify-resource-explorer.sh — Pattern 1 成功判定
#
# Copilot の出力に、デプロイ済みリソースが含まれているかチェックする。
# Vars are passed as EVAL_* environment variables from the runner.
#

RG="${EVAL_RESOURCE_GROUP:?EVAL_RESOURCE_GROUP not set}"

echo "[verify] Checking resources in $RG..."

# Get actual resource list from Azure
ACTUAL_RESOURCES=$(az resource list --resource-group "$RG" --query '[].name' -o tsv 2>/dev/null | sort)

if [[ -z "$ACTUAL_RESOURCES" ]]; then
  echo "[verify] ERROR: No resources found in $RG (or az cli not authenticated)"
  exit 1
fi

RESOURCE_COUNT=$(echo "$ACTUAL_RESOURCES" | wc -l)

# We expect at least 8 top-level resources:
#   VNet, App Service Plan, App Service, Storage, SQL Server, Log Analytics, App Insights, Private Endpoints
EXPECTED_MIN=8

echo "[verify] Found $RESOURCE_COUNT resources in $RG"
echo "$ACTUAL_RESOURCES" | while read -r name; do
  echo "  - $name"
done

if [[ "$RESOURCE_COUNT" -ge "$EXPECTED_MIN" ]]; then
  echo "[verify] ✓ Resource count ($RESOURCE_COUNT) >= expected minimum ($EXPECTED_MIN)"
  exit 0
else
  echo "[verify] ✗ Resource count ($RESOURCE_COUNT) < expected minimum ($EXPECTED_MIN)"
  exit 1
fi
