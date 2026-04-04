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

# Variant build-time setup (optional)
# Pass VARIANT_BUILD_SCRIPT=variants/scripts/build-xxx.sh to apply
ARG VARIANT_BUILD_SCRIPT=
ARG VARIANT_NAME=baseline
COPY ${VARIANT_BUILD_SCRIPT:-docker/noop.sh} /tmp/variant-build.sh
RUN --mount=type=secret,id=github_token \
    if [ -n "${VARIANT_BUILD_SCRIPT}" ]; then \
      export GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null || echo "") && \
      chmod +x /tmp/variant-build.sh && \
      /tmp/variant-build.sh; \
    fi && \
    rm -f /tmp/variant-build.sh

LABEL variant=${VARIANT_NAME}

WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
