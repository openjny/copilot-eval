#!/bin/bash
set -euo pipefail
# Build-time setup for baseline variant (Azure eval)
# Installs Azure CLI only (no plugins)

curl -sL https://aka.ms/InstallAzureCLIDeb | bash
