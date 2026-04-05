#!/bin/bash
set -euo pipefail
# Build-time setup for azure-skills variant
# Installs Azure CLI + Azure Skills Plugin

curl -sL https://aka.ms/InstallAzureCLIDeb | bash

COPILOT_HOME="${COPILOT_HOME:-/root/.copilot}"
mkdir -p "$COPILOT_HOME"
copilot plugin install microsoft/azure-skills --config-dir "$COPILOT_HOME"
