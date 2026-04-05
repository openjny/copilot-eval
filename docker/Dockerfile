FROM node:20-slim

ARG COPILOT_VERSION=1.0.18

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
