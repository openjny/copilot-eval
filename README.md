# copilot-evals

A/B evaluation framework for [GitHub Copilot CLI](https://docs.github.com/copilot/concepts/agents/about-copilot-cli) customizations using [OpenTelemetry](https://opentelemetry.io/) telemetry.

Measure the effect of plugins, custom instructions, MCP servers, and other Copilot customizations with reproducible, containerized eval runs and automated analysis.

## How it works

```
eval-config.yaml          Define vars, model, timeouts
  + patterns/*.yaml        Define eval tasks (prompts, verification, LLM-as-Judge)
  + variants/*.yaml         Define A/B environments (e.g. baseline vs plugin)
       ↓
  python -m eval build     Build Docker images per variant
  python -m eval run       Execute A/B in disposable containers → OTel → Jaeger
  python -m eval analyze   Compare traces: turns, tokens, tools, judge scores
```

Each eval run:
1. Spins up a **disposable Docker container** per variant (clean Copilot state every time)
2. Runs `copilot -p "prompt" --yolo` with OTel telemetry enabled
3. Sends traces to **Jaeger** via OTLP
4. Runs **verification scripts** and **LLM-as-Judge** scoring
5. Produces A/B comparison reports (table / JSON / Markdown)

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [GitHub Copilot CLI](https://docs.github.com/copilot/concepts/agents/about-copilot-cli) authenticated (`gh auth login`)

### Setup

```bash
git clone https://github.com/openjny/copilot-evals.git
cd copilot-evals

# Start Jaeger (OTel backend)
docker compose up -d

# Create .env with your credentials
cp .env.example .env
# Edit .env as needed

# Install Python dependencies
uv sync
```

### Run an eval (using the Azure Skills example)

```bash
# Build container images for both variants
uv run python -m eval build --config-dir examples/azure-skills

# Run the eval (1 epoch by default)
uv run python -m eval run --config-dir examples/azure-skills --pattern resource-explorer

# Analyze results
uv run python -m eval analyze --run-id <run-id>

# Output as Markdown (for blog posts)
uv run python -m eval analyze --run-id <run-id> -o markdown

# Output as JSON (for programmatic use)
uv run python -m eval analyze --run-id <run-id> -o json
```

## CLI Reference

```
uv run python -m eval <command> [options]
```

| Command | Description |
|---------|-------------|
| `list` | List available patterns and variants |
| `build` | Build Docker images for all (or specific) variants |
| `run` | Execute A/B eval runs |
| `analyze` | Analyze traces from a previous run |

### `run` options

| Flag | Description | Default |
|------|-------------|---------|
| `--pattern` / `-p` | Run a specific pattern | All enabled |
| `--epochs` / `-n` | Number of repetitions | 1 |
| `--dry-run` | Show plan without executing | — |
| `--config-dir` | Directory with eval-config.yaml | Project root |

### `analyze` options

| Flag | Description | Default |
|------|-------------|---------|
| `--run-id` | Run ID to analyze (required) | — |
| `--output` / `-o` | Output format: `table`, `json`, `markdown` | `table` |
| `--jaeger-url` | Jaeger API URL | `http://localhost:16686` |

## Project Structure

```
copilot-evals/
├── eval/                      # Framework (cloud-agnostic)
│   ├── cli.py                 # CLI entry point
│   ├── config.py              # Config loading + validation
│   ├── runner.py              # Docker container execution
│   ├── trace.py               # Jaeger API + span parsing
│   └── report.py              # A/B comparison (table/json/md)
├── docker/
│   ├── Dockerfile             # Base image (Copilot CLI only)
│   └── entrypoint.sh          # Auth merging + setup script
├── eval-config.yaml           # Default config
├── examples/
│   └── azure-skills/          # Example: Azure Skills Plugin eval
│       ├── eval-config.yaml
│       ├── patterns/
│       ├── variants/
│       ├── scripts/
│       └── setup/
├── pyproject.toml
└── docker-compose.yml         # Jaeger
```

## Creating Your Own Eval

### 1. Create a config directory

```
my-eval/
├── eval-config.yaml
├── patterns/
│   └── my-pattern.yaml
└── variants/
    ├── baseline.yaml
    └── my-customization.yaml
```

### 2. Define your config

```yaml
# my-eval/eval-config.yaml
vars:
  project_name: my-project

runner:
  model: claude-sonnet-4
  epochs: 1
  timeout_seconds: 120
```

### 3. Define a pattern

```yaml
# my-eval/patterns/my-pattern.yaml
name: my-pattern
type: read
enabled: true
prompt: "Explain the architecture of {project_name}"

metrics:
  judges:
    - name: accuracy
      prompt: |
        Rate accuracy on a scale of 1-5.
        Output ONLY JSON: {"score": N, "reason": "..."}
```

### 4. Define variants

```yaml
# my-eval/variants/baseline.yaml
name: baseline
description: "Default Copilot CLI"

# my-eval/variants/my-customization.yaml
name: my-customization
description: "Copilot CLI with my plugin"
build:
  script: my-eval/variants/scripts/setup.sh
```

### 5. Run

```bash
uv run python -m eval build --config-dir my-eval
uv run python -m eval run --config-dir my-eval
```

## How OTel Tracing Works

Copilot CLI emits OpenTelemetry spans for each agent session:

```
invoke_agent (root)
  ├── chat {model}          # LLM API call
  ├── execute_tool {name}   # Tool execution
  │   └── permission        # Permission check
  └── chat {model}          # Next LLM turn
```

The framework tags each run with `eval.test_id`, `eval.variant`, `eval.scenario`, and `eval.epoch` via `OTEL_RESOURCE_ATTRIBUTES`, enabling A/B comparison in Jaeger.

> **Note**: `COPILOT_HOME` must be writable for OTel span correlation to work correctly. The entrypoint handles this by copying auth from a read-only mount to a writable directory.

## License

MIT
