# copilot-evals

A/B evaluation framework for GitHub Copilot CLI customizations using OpenTelemetry.

## Architecture

```
eval/              Python package (CLI + framework logic)
├── cli.py         Click CLI: list, build, run, analyze
├── config.py      YAML config loading → dataclasses (Config, Task, Variant, Evaluator, Hooks)
├── runner.py      Single eval run: hooks → Docker container → evaluators
├── trace.py       Jaeger API: fetch + parse OTel traces
└── report.py      A/B comparison: build_report() → format_table/json/markdown

docker/            Container infrastructure
├── Dockerfile     Base image: Node 20 + Copilot CLI (version pinned)
└── entrypoint.sh  Auth merge + setup script execution

examples/          Eval sets (--config-dir)
└── azure-skills/  Azure Skills Plugin A/B evaluation
```

## Commands

```bash
uv run copilot-eval list --config-dir <dir>
uv run copilot-eval build --config-dir <dir>
uv run copilot-eval run --config-dir <dir> --task <name> [--epochs N] [--dry-run]
uv run copilot-eval analyze --run-id <id> [-o table|json|markdown]
```

## Conventions

- **Tasks** (not patterns/scenarios): eval task definitions following Inspect AI/SWE-bench naming
- **Evaluators**: unified list with `type: judge|script|contains|regex` (replaces separate verify/judges)
- **Hooks**: `before_run`/`after_run` per task for environment setup/teardown
- **Variants**: A/B environments defined by build scripts + run scripts
- **Config**: single `eval-config.yaml` with inline tasks/variants (split files also supported)
- **Vars**: `{key}` interpolation in prompts from `vars:` section

## Critical: COPILOT_HOME

COPILOT_HOME **must be writable** inside the container. OTel span correlation depends on session state.
The entrypoint merges host auth into a writable COPILOT_HOME, preserving `installed_plugins` from the image config.

## Critical: entrypoint.sh config merge

When merging host `config.json`, only auth keys are copied (`logged_in_users`, `last_logged_in_user`, `staff`).
Image-side keys like `installed_plugins` are preserved. If the merge fails, it falls back to the host config silently.

## Docker build

```bash
# Build context must be project root (for COPY paths)
docker build -f docker/Dockerfile --build-arg COPILOT_VERSION=1.0.18 \
  --build-arg VARIANT_BUILD_SCRIPT=<path> --secret id=github_token,env=GITHUB_TOKEN .
```

Variant build scripts install additional tools (e.g., Azure CLI, plugins) on top of the base image.

## Dependencies

- Python 3.10+, uv, Docker
- pyyaml, requests, click (see pyproject.toml)
- Jaeger (docker-compose.yml) for OTel trace collection
