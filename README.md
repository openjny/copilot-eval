# copilot-eval

Environment-isolated A/B evaluation framework for [GitHub Copilot](https://github.com/features/copilot) customizations.

Prove whether a set of primitives (skills, custom agents, instructions, hooks, MCP servers) actually improves outcomes — with reproducible eval runs, statistical rigor, and zero infrastructure.

## Quick Start

```bash
git clone https://github.com/openjny/copilot-eval.git
cd copilot-eval

# Prerequisites: Docker, uv, gh auth login
cp .env.example .env    # Configure credentials
```

> **Note**: By default, traces are collected to a local JSONL file — no Jaeger or
> `docker-compose` needed. Jaeger is optional; only start it (`docker-compose up -d`)
> if you set `runner.collector: jaeger` in `eval-config.yaml` (e.g. to browse traces
> interactively in the Jaeger UI).

### Try the prompt-language example

```bash
# Run eval (2 tasks × 2 variants × 3 epochs = 12 runs, ~2 min)
uv run copilot-eval run --config-dir examples/prompt-language

# Analyze
uv run copilot-eval analyze --run-id <RUN_ID> --config-dir examples/prompt-language -o markdown
```

## Documentation

- [Vision](docs/vision.md) — project vision, target users, positioning, and non-goals
- [Configuration Guide](docs/configuration.md) — eval-config.yaml, evaluators, fixtures, hooks, parallel modes
- [Architecture](docs/architecture.md) — execution flow, Docker design, OTel tracing, report generation

## CLI

```
uv run copilot-eval <command> [options]
```

Global options (before the command) control diagnostic logging:

| Option | Description |
|--------|-------------|
| `--log-level [debug\|info\|warning\|error\|critical]` | Diagnostic log level (default: `info`, or `$EVAL_LOG_LEVEL`) |
| `--log-format [plain\|json]` | Diagnostic log format (default: `plain`, or `$EVAL_LOG_FORMAT`) |

Diagnostic output (progress, warnings, errors) is emitted via Python's `logging`
module to stderr, so it can be filtered by level and rendered as JSON for CI. It is
kept separate from user-facing output (run banners, result tables, reports), which
stays on stdout. Example:

```bash
# Quiet CI run with machine-parseable diagnostics
uv run copilot-eval --log-level warning --log-format json run --config-dir examples/prompt-language
```

| Command | Description |
|---------|-------------|
| `list --config-dir <dir>` | List tasks and variants |
| `validate --config-dir <dir>` | Check config schema, fixtures, script/variant references, and var interpolation |
| `build --config-dir <dir> [--variant NAME]` | Build Docker images |
| `run --config-dir <dir> [--task NAME] [--epochs N] [--dry-run] [--no-build]` | Execute eval runs (fails fast on pre-flight checks before any Docker work) |
| `analyze --run-id <ID> [--config-dir <dir>] [-o table\|json\|markdown] [-a paired\|median\|mean] [--jaeger-url URL] [--skip-eval] [--re-eval]` | Analyze results |

## Examples

| Example | What it evaluates |
|---------|-------------------|
| [prompt-language](examples/prompt-language/) | English vs Japanese prompts on code tasks |
| [azure-skills](examples/azure-skills/) | Azure Skills Plugin impact on Azure operations |
| [judge-calibration](examples/judge-calibration/) | Judge reliability/calibration via fixed-answer tasks |

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
└── docker-compose.yml     # Jaeger (optional; only needed for runner.collector: jaeger)
```

The framework tags each run with `eval.test_id`, `eval.variant`, `eval.scenario`, `eval.fixture` (empty unless the task declares multiple fixtures), and `eval.epoch` via `OTEL_RESOURCE_ATTRIBUTES`, enabling A/B comparison in the collected traces (a local file by default, or Jaeger's UI when `runner.collector: jaeger`).

> **Note**: `COPILOT_HOME` must be writable for OTel span correlation to work correctly. The entrypoint handles this by copying auth from a read-only mount to a writable directory.

## Upgrading

### v0.2.0 — OTel generalization

- **Breaking**: Default trace collector changed from `jaeger` to `file` (JSONL file exporter).

- Existing configs without an explicit `runner.collector` now use `file`.
- To keep Jaeger, set:

```yaml
runner:
  collector: jaeger
```

- The `file` collector requires no external services — traces are stored as `.traces/*.jsonl` files in the results directory.

## Development

Run the unit tests (pure logic: config parsing/validation, report aggregation, trace parsing):

```bash
uv run --group dev pytest
```

Lint and type-check (the same gates run in CI):

```bash
uv run ruff check eval tests          # lint
uv run ruff format --check eval tests  # format check (use without --check to apply)
uv run mypy                            # strict type check (eval package)
```

These checks (ruff lint → ruff format → mypy → pytest) run automatically on every push to `main`
and on every pull request via GitHub Actions (`.github/workflows/ci.yml`, Python 3.13).

## License

MIT
