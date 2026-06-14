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
  judge_samples: 1               # Self-consistency: sample each judge N times, aggregate
  judge_aggregate: median        # median | mean | majority (over successful samples)
  reasoning_effort: null         # Optional: low|medium|high
  max_turns: 20                  # Max autopilot turns
  parallel: off                  # off | per_task | full
  max_workers: 8                 # Max concurrent runs (for parallel modes + analyze judges)
  judge_timeout_seconds: 60      # Per-judge Copilot timeout in analyze (seconds)
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

### Judge Self-Consistency & Reliability

Single-shot judge scores are noisy. Set `runner.judge_samples > 1` to sample each
judge multiple times and aggregate the successful scores via `runner.judge_aggregate`
(`median` — default, `mean`, or `majority`). Each sample's outcome is classified as
`ok` / `parse_error` / `timeout` / `error`.

The aggregated `.scores.json` entry for a judge records this metadata (additive — older
consumers ignore the extra keys):

```json
{
  "name": "quality", "type": "judge", "score": 8,
  "reason": "[median of 3/3, σ=0.47] solid coverage",
  "passed": true,
  "samples": [8, 8, 9],
  "score_stddev": 0.47,
  "n_samples": 3,
  "outcomes": {"ok": 3, "parse_error": 0, "timeout": 0, "error": 0},
  "judge_model": "gpt-4.1",
  "judge_version": "1.0.18"
}
```

- `score` is the aggregate; `samples` are the individual successful scores.
- `score_stddev` is the population stddev of the samples (0 for a single sample).
- `outcomes` counts per-sample results; `judge_model`/`judge_version` pin the judge
  CLI/model that produced the score.

`analyze` prints a **Judge reliability** summary (to stderr): the number of judge
evaluations, per-sample outcome rates (ok/parse_error/timeout/error), and the mean/max
score spread (σ). The markdown/JSON reports show each per-run judge score with its
`±σ`, and the JSON report includes a `judge_stddevs` map per run.

> Cached judge scores are keyed by evaluator **name** only. If you change
> `judge_samples`, `judge_aggregate`, or `judge_model` and want existing scores
> re-evaluated with the new settings, re-run `analyze --re-eval`.

To assess whether the judge itself is calibrated, see the `examples/judge-calibration`
eval set: it pins Copilot output to fixed answers with known expected score bands.

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

For extra reliability, pair noisy judges with **deterministic ground-truth
evaluators** (`contains`/`regex`/`script`) that check language-neutral, objective
signals (function names, line numbers, required tokens). These score 0/1 with no LLM
call, so they don't drift between runs and anchor the judge. See the `gt-*` evaluators
in `examples/prompt-language` and `examples/judge-calibration`.

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

During `analyze`, judge evaluators are always run in parallel across traces (up to `max_workers`), independent of the `parallel` mode above. Each judge's Copilot invocation is bounded by `judge_timeout_seconds`. Scores files are written per trace, so parallel judging does not cause write conflicts.

## Secrets & `.env`

Place a `.env` file next to `eval-config.yaml` (`<project-dir>/.env`). Each `KEY=value`
line is loaded and made available to:

- the **container** (via `docker --env-file`), and
- **hooks**, **health checks**, and **script evaluators** (via the process environment).

### Quoting

Surrounding matching quotes are stripped, following standard dotenv semantics:

```dotenv
PLAIN=value            # -> value
DQUOTED="some value"   # -> some value
SQUOTED='some value'   # -> some value
```

The same normalized (quote-stripped) value is used everywhere. Internally the
container receives a sanitized temporary env file rather than the raw `.env`, so
hooks and the container always see **identical** values. Secret values are never
placed in `argv`, so they don't leak via `ps`.

### Secret masking

To reduce the risk of secrets leaking through evaluation artifacts, values from
`.env` and `GITHUB_TOKEN` are redacted (replaced with `***REDACTED***`) in:

- the **persisted run log** (`*.log`) — masked after `contains`/`regex` evaluators
  have read it, so masking can't affect their results, and
- the **text passed to judge evaluators** (captured conversation + output files),
  in both the `run` and `analyze` paths.

Values shorter than 6 characters are not masked, to avoid redacting trivial,
non-sensitive values (e.g. `1`, `true`).

> **Scope & limitations**
> - **All** `.env` values (≥6 chars) are treated as secrets, not just
>   secret-looking keys. Non-secret config (endpoints, regions, org names) in
>   `.env` is therefore also redacted from judge input. Keep purely informational
>   values out of `.env` (use `vars`) if you want the judge to see them.
> - Masking is applied to logs and judge input only. Files persisted under
>   `results/outputs/` are **not** redacted — avoid having Copilot write secrets
>   to `/workspace/output/`.
> - During `analyze`, secrets are collected from the **current** `.env` /
>   `GITHUB_TOKEN`. If a token was rotated after the `run`, OTel-sourced
>   conversation text may not be masked for the rotated value. Logs are already
>   masked at run time, so this only affects late judge runs.

