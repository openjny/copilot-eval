# Architecture

## Overview

```
eval-config.yaml
       ↓
  copilot-eval build     → Docker images per variant
  copilot-eval run       → Containers → OTel → Jaeger
  copilot-eval analyze   → Traces → A/B report
```

## Components

```
eval/
├── cli.py        Click CLI: list, build, run, analyze
├── config.py     YAML config → dataclasses (Config, Task, Variant, Evaluator, Hooks)
├── runner.py     Single eval run: hooks → Docker container → evaluators
├── trace.py      Jaeger API: fetch + parse OTel traces
└── report.py     A/B comparison: build_report() → format_table/json/markdown

docker/
├── Dockerfile     Base image: Node 20 + Copilot CLI (version pinned)
└── entrypoint.sh  Auth merge + setup script execution
```

## Execution Flow

```mermaid
sequenceDiagram
    participant CLI as copilot-eval run
    participant Runner as runner.py
    participant Docker as Container
    participant Jaeger as Jaeger

    CLI->>Runner: run_one(task, variant, epoch)
    Runner->>Runner: before_run hook
    Runner->>Runner: health_check
    Runner->>Runner: Copy fixture → tmpdir + create output/
    Runner->>Docker: docker run (tmpdir:/workspace)
    Docker->>Docker: entrypoint.sh (auth merge)
    Docker->>Docker: copilot -p "prompt" --yolo
    Docker->>Jaeger: OTel spans (OTLP)
    Docker-->>Runner: exit + log file
    Runner->>Runner: after_run hook
    Runner->>Runner: persist output files + non-judge evaluators (script/contains/regex)
    Runner->>Runner: Cleanup tmpdir
    Runner-->>CLI: RunResult (status from exit code)
```

`run_one` records the container exit code and maps it to a status:
`0` → `completed`, `124` (GNU `timeout`) → `timeout`, any other non-zero → `failed`
(health-check failures are `setup_failed`). `RunResult.passed` requires
`status == "completed"`, so timed-out or errored runs are never counted as passing.
After all runs finish, `run` writes a **`results.json` manifest** into the run
directory recording every run (task/variant/epoch, test_id, exit_code, status,
scores) plus the execution schedule: a top-level `schedule` block (parallel mode,
max_workers, variant_order, seed) and per-run timing (order_index, started_at,
finished_at, duration_seconds) so order/concurrency confounders can be analyzed
post-hoc. `analyze` reconciles against this manifest so failed/timeout/missing
runs are reported rather than silently dropped.

Judge (LLM-as-Judge) evaluators do **not** run during `run`. They run later in
`copilot-eval analyze`, which fetches traces, reconstructs the conversation, and
scores it:

```mermaid
sequenceDiagram
    participant CLI as copilot-eval analyze
    participant Jaeger as Jaeger
    participant Trace as trace.py
    participant Judge as Judge LLM

    CLI->>Jaeger: fetch_traces (server-side run_id filter) + reconcile manifest
    CLI->>Trace: extract_conversation (chronological by startTime)
    CLI->>Judge: run_judge (conversation + output files)
    Judge-->>CLI: {"score": N, "reason": "..."}
    CLI->>CLI: merge with existing scores → A/B report
```


## Docker Design

### Base Image

`docker/Dockerfile` provides a minimal base:
- `node:20-slim` + Copilot CLI (version pinned via `COPILOT_VERSION`)
- `entrypoint.sh` handles auth merging

### Variant Images

Each variant extends the base with its own Dockerfile:

```dockerfile
FROM copilot-eval:base
# Install tools, plugins, etc.
RUN copilot plugin install microsoft/azure-skills
```

### COPILOT_HOME

COPILOT_HOME **must be writable** inside the container. The entrypoint merges host auth (`logged_in_users`, `last_logged_in_user`, `staff`) into a writable copy, preserving image-side config like `installed_plugins`.

### Workspace

Fixtures are copied to a host tmpdir and mounted as `/workspace` (read-write). An `output/` subdirectory is created automatically for Copilot to write artifacts. The tmpdir is cleaned up in a `finally` block after evaluators run.

## OTel Tracing

Copilot CLI emits spans for each agent session:

```
invoke_agent (root)
  ├── chat {model}          # LLM API call (tokens in tags)
  ├── execute_tool {name}   # Tool execution
  │   └── permission
  └── chat {model}          # Next turn
```

Tags include `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `tool.name`, etc.

`trace.py` fetches spans from Jaeger's HTTP API using a **server-side tag filter**
on `eval.run_id` (so large runs aren't truncated by the request limit) and a
high, configurable `trace_fetch_limit`. `analyze` retries the fetch while trace
ingestion catches up with the expected run count (from the manifest), then
filters defensively by `eval.run_id` / `eval.test_id` as a safety net.

### Analyze accuracy

`analyze` guards against three correctness pitfalls:

- **Survivorship bias**: traces are reconciled against `results.json`. Completed
  runs with no ingested trace are warned as *missing*; failed/timeout runs are
  reported and excluded from metrics.
- **Judge scoring**: judge evaluators are (re)run based on whether a *judge score*
  already exists — not merely whether a `.scores.json` file exists (non-judge
  evaluators write to the same file). `--re-eval` forces all judges to re-run.
- **Unavailable judge scores**: judge timeouts / parse failures (`score: null`)
  are surfaced as warnings instead of disappearing from the report.

## Report Generation

`report.py` builds per-task A/B comparisons:

1. Groups results by task
2. Fetches traces from Jaeger for each run
3. Computes metrics (duration, turns, tokens, tool calls)
4. Supports three aggregation modes:
   - **paired** (default): Per-epoch delta → median
   - **median**: Independent median per variant
   - **mean**: Independent mean per variant
5. Outputs as table, JSON, or Markdown

### Trustworthy statistics

To avoid over-reading small, noisy runs (default `epochs=3`), every report
surfaces its own uncertainty:

- **Sample size**: per-variant `n` plus the shared **paired epoch** count.
- **Dispersion**: each metric value is shown as `value ±stddev` (min/max also in
  JSON), so a delta can be read against the spread it sits in.
- **Confidence interval**: the paired delta carries a bootstrap CI (seeded, so
  output is reproducible). `*` marks a delta whose CI excludes 0 (statistically
  supported); `ns` marks an *observed only* delta whose CI includes 0.
- **Insufficient-data warnings**: when a variant's `n` or the paired epoch count
  is below `MIN_RELIABLE_N` (5), the report warns that deltas are observed, not
  statistically supported.

### Reliability (anti-survivorship-bias)

Because `build_report` aggregates only surviving traces, a flaky variant whose
bad runs drop out could look "faster/better". The report now includes a
first-class **Reliability** table per task, computed from the persisted run
manifest + ingested trace ids:

- success rate, timeout rate, failed rate
- **missing-trace rate** (completed runs that produced no trace)
- **judge-score coverage** (share of judge evaluations that yielded a usable
  score) when the task has judges

When no manifest is available (older runs), reliability degrades to a simple
per-variant trace count, and the rest of the report still renders.

