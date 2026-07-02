# copilot-eval

A/B evaluation framework for [GitHub Copilot CLI](https://docs.github.com/copilot/concepts/agents/about-copilot-cli) customizations using [OpenTelemetry](https://opentelemetry.io/) telemetry.

Measure the effect of plugins, custom instructions, MCP servers, and other Copilot customizations with reproducible, containerized eval runs and automated analysis.

## Quick Start

```bash
git clone https://github.com/openjny/copilot-eval.git
cd copilot-eval

# Prerequisites: Docker, uv, gh auth login
cp .env.example .env    # Configure credentials
```

### Try the prompt-language example

```bash
# Run eval (2 tasks × 2 variants × 3 epochs = 12 runs, ~2 min)
uv run copilot-eval run --config-dir examples/prompt-language

# Analyze
uv run copilot-eval analyze --run-id <RUN_ID> --config-dir examples/prompt-language -o markdown
```

## CLI

```
uv run copilot-eval <command> [options]
```

| Command | Description |
|---------|-------------|
| `list --config-dir <dir>` | List tasks and variants |
| `build --config-dir <dir> [--variant NAME]` | Build Docker images |
| `run --config-dir <dir> [--task NAME] [--epochs N] [--dry-run] [--no-build]` | Execute eval runs |
| `analyze --run-id <ID> [--config-dir <dir>] [-o table\|json\|markdown] [-a paired\|median\|mean] [--jaeger-url URL] [--skip-eval] [--re-eval]` | Analyze results |

## Examples

| Example | What it evaluates |
|---------|-------------------|
| [prompt-language](examples/prompt-language/) | English vs Japanese prompts on code tasks |
| [azure-skills](examples/azure-skills/) | Azure Skills Plugin impact on Azure operations |
| [judge-calibration](examples/judge-calibration/) | Judge reliability/calibration via fixed-answer tasks |

## Documentation

- [Configuration Guide](docs/configuration.md) — eval-config.yaml, evaluators, fixtures, hooks, parallel modes
- [Architecture](docs/architecture.md) — execution flow, Docker design, OTel tracing, report generation

## Project Structure

```
copilot-eval/
├── eval/                  # Framework
│   ├── __init__.py        # Package marker
│   ├── __main__.py        # `python -m eval` entry
│   ├── cli.py             # CLI entry point
│   ├── config.py          # Config loading
│   ├── runner.py          # Docker execution + evaluators
│   ├── trace.py           # Jaeger trace parsing
│   └── report.py          # A/B comparison reports
├── docker/
│   ├── Dockerfile         # Base image (Node 20 + Copilot CLI)
│   └── entrypoint.sh      # Auth merging
├── examples/              # Eval sets
├── docs/                  # Detailed documentation
├── tests/                 # Pytest unit tests (config, report, trace)
└── docker-compose.yml     # Jaeger
```

The framework tags each run with `eval.test_id`, `eval.variant`, `eval.scenario`, and `eval.epoch` via `OTEL_RESOURCE_ATTRIBUTES`, enabling A/B comparison in Jaeger.

> **Note**: `COPILOT_HOME` must be writable for OTel span correlation to work correctly. The entrypoint handles this by copying auth from a read-only mount to a writable directory.

## Development

Run the unit tests (pure logic: config parsing/validation, report aggregation, trace parsing):

```bash
uv run --group dev pytest
```

Lint and type-check (the same gates run in CI):

```bash
uv run ruff check eval tests   # lint
uv run mypy                     # strict type check (eval package)
```

These three checks (ruff → mypy → pytest) run automatically on every push to `main`
and on every pull request via GitHub Actions (`.github/workflows/ci.yml`, Python 3.13).

## License

MIT
