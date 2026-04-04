#!/bin/bash
set -euo pipefail
# Azure Service Principal login for eval containers
# Called by entrypoint.sh via EVAL_SETUP_SCRIPT

if [[ -n "${AZURE_CLIENT_ID:-}" && -n "${AZURE_CLIENT_SECRET:-}" && -n "${AZURE_TENANT_ID:-}" ]]; then
  az login --service-principal \
    --username "$AZURE_CLIENT_ID" \
    --password "$AZURE_CLIENT_SECRET" \
    --tenant "$AZURE_TENANT_ID" \
    --output none

  if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
    az account set --subscription "$AZURE_SUBSCRIPTION_ID"
  fi

  echo "[eval] Azure: $(az account show --query '{name:name, user:user.name}' -o tsv)"
else
  echo "[eval] WARNING: Azure credentials not set, skipping az login"
fi
