# Configuration Guide

## eval-config.yaml

Each eval set is defined by a single `eval-config.yaml` file. It contains global settings, variants, and tasks.

```yaml
vars:
  key: value                     # Global variables for prompt interpolation

runner:
  epochs: 3                     # Repetitions per task×variant
  timeout_seconds: 300           # Max seconds per Copilot run
  model: claude-sonnet-4         # Copilot model
  judge_model: claude-sonnet-4.6 # Model for LLM-as-Judge (separate from eval model)
  reasoning_effort: null         # Optional: low|medium|high
  max_turns: 20                  # Max autopilot turns
  parallel: off                  # off | per_task | full
  max_workers: 8                 # Max concurrent runs (for parallel modes)
  output_format: text            # text | json
  capture_content: true          # Capture prompt/response content in OTel spans (needed by judge)
  container_image_base: copilot-eval
  copilot_version: "1.0.18"
  otel_endpoint: http://host.docker.internal:4318   # OTLP collector endpoint (inside container)
  jaeger_url: http://localhost:16686                # Jaeger query UI/API (host side)
  trace_fetch_limit: 2000        # analyze: max traces to request from Jaeger
  trace_fetch_retries: 5         # analyze: attempts to wait for trace ingestion
  trace_fetch_retry_delay: 2.0   # analyze: seconds between ingestion retries

variants:
  - name: baseline
    description: "Control group"
    dockerfile: path/to/Dockerfile       # Optional: custom Dockerfile
    run_script: path/to/setup.sh         # Optional: sourced inside container before Copilot
    model: null                          # Optional: override runner.model per variant
    vars: {}                             # Variant-level variable overrides

tasks:
  - name: my-task
    prompt: "Do something with {key}"    # {key} interpolated from vars
    enabled: true
    fixture: my-fixture                  # Directory under fixtures/ to mount at /workspace
    timeout_seconds: null                # Override runner.timeout_seconds
    health_check: scripts/check.sh       # Script that must pass before running
    vars: {}                             # Task-level variable overrides
    hooks:
      before_run: scripts/setup.sh       # Run before Copilot
      after_run: scripts/cleanup.sh      # Run after Copilot
    evaluators:
      - name: quality
        type: judge                      # judge | script | contains | regex
        prompt: "Rate on 1-10..."
```

## Variable Resolution

Variables are merged in order: `global vars` → `task vars` → `variant vars`. Later values override earlier ones.

The prompt also gets `"\nSave all output files under /workspace/output/."` appended automatically so that generated files are available to judges.

## Variants

Each variant gets its own Docker image built from a Dockerfile. The image inherits from `copilot-eval:base` (built from `docker/Dockerfile`).

```dockerfile
# Example: my-variant/Dockerfile
FROM copilot-eval:base
RUN copilot plugin install my-org/my-plugin
```

The optional `run_script` is sourced inside the container before Copilot runs (e.g., for authentication).

## Evaluators

Four evaluator types are supported:

| Type | Config | What it does |
|------|--------|-------------|
| `judge` | `prompt` | LLM scores the output on 1-10 scale |
| `script` | `script` | Bash script; exit 0 = pass |
| `contains` | `value` | Checks if string exists in output |
| `regex` | `value` | Checks if regex matches output |

Each evaluator requires a unique `name` within its task and a valid `type`. The type-specific field above is mandatory (e.g. `judge` requires `prompt`). Invalid types, missing required fields, duplicate names, and invalid regex `value`s are rejected at config load time with a clear `ConfigError`.

### Judge Evaluator

The judge sees both the **conversation output** (Copilot's terminal log) and any **files written to `/workspace/output/`**. This ensures correct scoring even when Copilot writes results to files without echoing them.

Judge scoring is done by `runner.judge_model` (defaults to the eval model if not set). OTel is disabled during judge calls to avoid contaminating traces.

Judges run during `analyze` and are scored idempotently: a judge is (re)run only when no judge score yet exists for that run (non-judge `script`/`contains`/`regex` scores share the same `.scores.json` file, so file presence alone does not skip judging). Use `analyze --re-eval` to force all judges to re-run. Judge timeouts or unparseable output produce `score: null` and are surfaced as warnings rather than dropped.

### Ground Truth in Judge Prompts

For reliable scoring, include the expected answer in the judge prompt:

```yaml
evaluators:
  - name: thoroughness
    type: judge
    prompt: |
      The code has these known issues:
      1. eval() with user input (line 36)
      2. Plaintext password storage (line 15)
      3. No auth on DELETE endpoint (line 27)
      Rate how many issues the review found on 1-10.
      Output ONLY valid JSON: {"score": N, "reason": "..."}
```

## Fixtures

Place files under `<config-dir>/fixtures/<fixture-name>/`. They are copied to a temp directory and mounted at `/workspace` inside the container (read-write). An `output/` subdirectory is automatically created.

## Hooks

`before_run` and `after_run` scripts run on the **host** (not inside Docker). Environment variables `EVAL_<KEY>` are set from resolved vars. Use them for:

- Environment setup/teardown (e.g., Azure resource reset)
- Pre-deployment of test scenarios

## Health Check

A script that validates the environment is ready before running Copilot. If it exits non-zero, the run is skipped with `status: setup_failed`.

## Parallel Modes

| Mode | Behavior |
|------|----------|
| `off` | Sequential execution |
| `per_task` | Tasks run in parallel, variants within a task are sequential |
| `full` | All task×variant×epoch combinations run in parallel (up to `max_workers`) |
