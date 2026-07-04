# copilot-eval

Environment-isolated A/B evaluation framework for GitHub Copilot customizations.

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

docs/              Detailed documentation
├── architecture.md   Execution flow, Docker design, OTel tracing
├── configuration.md  eval-config.yaml reference, evaluators, fixtures, hooks
└── vision.md         Project vision, target users, positioning, non-goals

examples/          Eval sets (--config-dir)
├── azure-skills/  Azure Skills Plugin A/B evaluation
└── prompt-language/  English vs Japanese prompt comparison
```

## Commands

```bash
uv run copilot-eval list --config-dir <dir>
uv run copilot-eval build --config-dir <dir>
uv run copilot-eval run --config-dir <dir> --task <name> [--epochs N] [--dry-run]
uv run copilot-eval analyze --run-id <id> [-o table|json|markdown]
```

## Conventions

- **Tasks**: eval task definitions with prompt, evaluators, hooks, fixture
- **Evaluators**: unified list with `type: judge|script|contains|regex`
- **Hooks**: `before_run`/`after_run` per task for environment setup/teardown
- **Variants**: A/B environments defined by `build.dockerfile` + `run.script`
- **Config**: `eval-config.yaml` with tasks/variants inline or in `tasks/*.yaml` / `variants/*.yaml` (primary)
- **Vars**: `{key}` interpolation in prompts; merged global → task → variant
- **Output dir**: Copilot writes artifacts to `/workspace/output/`; judge evaluator reads them

## Critical: COPILOT_HOME

COPILOT_HOME **must be writable** inside the container. OTel span correlation depends on session state.
The entrypoint merges host auth into a writable COPILOT_HOME, preserving `installed_plugins` from the image config.

## Critical: entrypoint.sh config merge

When merging host `config.json`, only auth keys are copied (`logged_in_users`, `last_logged_in_user`, `staff`).
Image-side keys like `installed_plugins` are preserved. If the merge fails, it falls back to the host config silently.

## Docker build

```bash
# Preferred: the CLI injects the pinned version (eval.config.DEFAULT_COPILOT_VERSION)
uv run copilot-eval build --config-dir <dir>

# Manual base image build — derive the version instead of hardcoding it:
docker build -f docker/Dockerfile \
  --build-arg COPILOT_VERSION="$(uv run python -c 'from eval.config import DEFAULT_COPILOT_VERSION; print(DEFAULT_COPILOT_VERSION)')" \
  -t copilot-eval:base .

# Variant image (FROM copilot-eval:base)
docker build -f examples/azure-skills/docker/Dockerfile.azure-skills \
  --secret id=github_token,env=GITHUB_TOKEN -t copilot-eval:azure-skills .
```

Each variant has its own Dockerfile that extends `copilot-eval:base` with variant-specific tools (e.g., Azure CLI, plugins, env vars).

## Dependencies

- Python 3.10+, uv, Docker
- pyyaml, requests, click (see pyproject.toml)
- Jaeger (docker-compose.yml) optional for interactive trace browsing

## Naming & Structure Conventions

Prefer widely-recognized names over custom ones. When creating or renaming files, follow established conventions. For example:

- **Root-level community files**: use standard uppercase names (e.g., `CONTRIBUTING.md`, `CHANGELOG.md`)
- **docs/**: use lowercase kebab-case (e.g., `docs/architecture.md`)

## Issue Tracking

See `docs/issue-guidelines.md` for details. Key conventions:

- **Epics**: tracking issues (label `epic`) with task lists grouping related work
- **Titles**: conventional commit style — `type(scope): description`
- **Priority labels**: `priority: critical` > `high` > `medium` > `low`
- **Issue body**: Problem → Proposal → Acceptance Criteria → References (link parent Epic, blockers)
- **Closing**: reference in PR/commit (`Closes #N`) and check off the Epic's task list
