#!/bin/bash
set -euo pipefail

# --- Merge Copilot auth from mounted host config ---
# If /copilot-home-src is mounted (host auth), copy auth files to writable COPILOT_HOME
# COPILOT_HOME must be writable for session state + OTel span correlation
COPILOT_HOME="${COPILOT_HOME:-/root/.copilot}"
export COPILOT_HOME
mkdir -p "$COPILOT_HOME"

if [[ -d "/copilot-home-src" && -f "/copilot-home-src/config.json" ]]; then
  # Merge host auth into container's config.json, preserving installed_plugins
  if [[ -f "$COPILOT_HOME/config.json" ]]; then
    # Image has config (e.g. with plugin registrations) — merge host auth keys
    node -e "
      const fs = require('fs');
      const img = JSON.parse(fs.readFileSync('$COPILOT_HOME/config.json', 'utf8'));
      const host = JSON.parse(fs.readFileSync('/copilot-home-src/config.json', 'utf8'));
      // Copy auth keys from host, keep everything else from image
      for (const key of ['logged_in_users', 'last_logged_in_user', 'staff']) {
        if (host[key] !== undefined) img[key] = host[key];
      }
      fs.writeFileSync('$COPILOT_HOME/config.json', JSON.stringify(img, null, 2));
    " 2>/dev/null || cp /copilot-home-src/config.json "$COPILOT_HOME/config.json"
  else
    cp /copilot-home-src/config.json "$COPILOT_HOME/config.json" 2>/dev/null || true
  fi
  cp /copilot-home-src/session-store.db "$COPILOT_HOME/session-store.db" 2>/dev/null || true
fi

# --- Run setup script if provided (e.g. cloud auth) ---
if [[ -n "${EVAL_SETUP_SCRIPT:-}" && -f "${EVAL_SETUP_SCRIPT}" ]]; then
  source "$EVAL_SETUP_SCRIPT"
fi

# --- Execute the provided command ---
exec "$@"

# --- Execute the provided command ---
exec "$@"
