FROM node:20-slim

# Copilot CLI version to pin. Injected by cli._build_images from
# RunnerConfig.copilot_version (single source: eval/config.DEFAULT_COPILOT_VERSION).
# Required — pass --build-arg COPILOT_VERSION=... for manual builds.
ARG COPILOT_VERSION

RUN test -n "$COPILOT_VERSION" || \
      { echo "ERROR: COPILOT_VERSION build-arg is required" >&2; exit 1; }

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl git ca-certificates gnupg lsb-release uuid-runtime && \
    # GitHub Copilot CLI (version pinned)
    VERSION=${COPILOT_VERSION} curl -fsSL https://gh.io/copilot-install | bash && \
    # Cleanup
    apt-get clean && rm -rf /var/lib/apt/lists/*

LABEL copilot.version=${COPILOT_VERSION}

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
