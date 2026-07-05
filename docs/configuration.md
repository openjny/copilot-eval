# Configuration Guide

## JSON Schema (IDE IntelliSense)

`eval-config.yaml` has a generated JSON Schema at
[`schemas/eval-config.schema.json`](../schemas/eval-config.schema.json),
covering `runner`, `variants`, `tasks`, `evaluators`, `vars`, and `hooks`.
Point your editor at it to get autocomplete, hover docs, and red squiggles on
typos (e.g. `timeout_secods` or `judge_batch: "tru"`) as you type, instead of
discovering them at run time.

**VS Code**: install the [YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml)
— this repo's [`.vscode/settings.json`](../.vscode/settings.json) already maps
it over `eval-config.yaml` and `examples/*/eval-config.yaml`.

**Other editors** (or configs outside `examples/`): add a header comment
pointing at the schema, relative to the YAML file:

```yaml
# yaml-language-server: $schema=../../schemas/eval-config.schema.json
vars: {}
...
```

The schema is generated from the `RunnerConfig`/`Variant`/`Task`/`Evaluator`
dataclasses in `eval/config.py` — after changing one of those, regenerate it:

```bash
uv run python scripts/generate_schema.py
```

`tests/test_schema.py` fails if the committed schema drifts from the
generator's output, or if any config under `examples/` (or the root
`eval-config.yaml`) stops validating against it.

> **Note**: the schema only covers the inline/top-level `eval-config.yaml`
> shape. Configs that split tasks/variants into `tasks/*.yaml` /
> `variants/*.yaml` files aren't schema-checked per-file (yet).

## Scaffolding a new project (`init`)

Starting from a blank `eval-config.yaml` is tedious — `init` generates a
minimal, already-runnable eval instead:

```bash
uv run copilot-eval init --config-dir my-eval
```

This creates `my-eval/` with `eval-config.yaml` (pointing at the JSON Schema
above), a `hello-world` task with one deterministic `contains` evaluator, a
`baseline`/`experimental` variant pair (the latter with a
`docker/Dockerfile.experimental` stub — layer the plugin/instructions/MCP
server you're testing on top of `copilot-eval:base` there), a matching empty
fixture, `.env.example`, and a `.gitignore` that excludes `results/` and
`.env`. Pass `--force` to overwrite an existing directory's files.

`.env.example` documents `COPILOT_GITHUB_TOKEN`, but export it in your shell
(or run `gh auth login`) rather than copying the file to `.env` — the
host-side checks that gate `run`/`build` read the process environment
directly (see [Secrets & `.env`](#secrets--env) below for what an actual
`.env` file *is* used for: hooks and the container's environment). Then run
`validate`, then `run --dry-run`, to confirm it works before wiring up your
own tasks and variants.

## Suggesting evaluators (`suggest-evaluators`)

Writing a good judge rubric from scratch is the hardest part of authoring an
eval — the blank-page problem. `suggest-evaluators` asks the judge model to
propose a starting evaluator set for a task and writes it as a ready-to-edit
task file:

```bash
uv run copilot-eval suggest-evaluators \
  --task-prompt "Review this PR for security vulnerabilities" \
  --fixture fixtures/sample-pr/ \
  --output tasks/security-review.yaml
```

The generated file bundles the task prompt with a mix of evaluators:

- **judge rubrics** in the structured `criterion` + `rubric` form (integer
  score → anchor descriptions), so the scale is calibrated and editable;
- **deterministic anchors** (`regex`/`contains`) that pin objective, LLM-free
  signals;
- **metric gates** (e.g. a `cost` threshold) where applicable.

It always emits at least one judge rubric *and* at least one deterministic
anchor (adding sensible defaults if the model omits one), and every proposed
evaluator is validated the same way the config loader validates them — so the
output is guaranteed to pass `validate`. Drop the file into your project's
`tasks/` directory, review the anchors, and run.

Key options:

- `--task-prompt <text>` / `--task-prompt-file <path>` — the task prompt (exactly one).
- `--output <file>` — where to write the task YAML (required).
- `--fixture <dir>` — summarize a fixture directory as task-input context.
- `--sample-output <file>` (repeatable) — sample agent outputs to inform the
  rubric. Omit all of them for **prompt-only mode**.
- `--task-name NAME` — task name (default: derived from the output filename).
- `--judge-model MODEL` — override the judge model (default: `runner.judge_model`).
- `--dry-run` — print the meta-prompt sent to the judge and exit (no model call).

This is a starting point, not a final rubric: the anchors and checks are meant
to be reviewed and tightened before you rely on the scores.

## Validation

Before running an eval, catch config typos and missing references early with:

```bash
uv run copilot-eval validate --config-dir <dir>
```

This checks YAML syntax/schema validity, that `eval-config.yaml` conforms to
`schemas/eval-config.schema.json` (see above), that every referenced fixture
directory exists on disk, that variant/task script references (Dockerfiles, run
scripts, hooks, health checks, script evaluators) point to real files, and that
every `{var}` placeholder in a prompt or `output_instruction` resolves for each
variant. Every check result is either a pass (`✓`), a **blocking** failure
(`✗`), or a non-blocking **warning** (`⚠`) — a missing fixture directory or an
unresolved `{var}` placeholder is only a warning, since the runtime tolerates
both (a missing fixture is simply not copied; an unresolved placeholder is
left as literal text, e.g. `"Emit JSON like {status}"`). Every failure and
warning includes a remediation hint. The command exits `0` unless there is at
least one blocking failure.

`copilot-eval run` also performs its own pre-flight readiness checks (Docker
daemon reachable, `GITHUB_TOKEN`/`COPILOT_GITHUB_TOKEN` set or `gh auth`
available, fixture directories present, sufficient disk space, and — unless
`--no-build` is passed — that the base Docker image exists) before doing any
Docker work, so a `run` fails fast with an actionable message instead of
20+ minutes in. As with `validate`, only blocking failures abort the run;
warnings are printed but the run proceeds. Pass `--skip-preflight` to bypass
these checks entirely (e.g. in CI environments where they may be noisy).

## Resuming a run

`run --resume --run-id <ID>` re-runs only the matrix cells (task × variant ×
epoch × fixture) that are **missing** or **failed** in an existing run,
instead of starting a new run-id from scratch:

```bash
uv run copilot-eval run --config-dir <dir> --resume --run-id 20240101-120000-abc123
```

A cell counts as done only if the run's manifest (`results/<run-id>/results.json`)
recorded it with status `completed`; anything else — `failed`, `timeout`,
`setup_failed`, or simply absent (e.g. the process was killed mid-run) — is
re-executed. New results are merged back into the same `results/<run-id>/`
directory (the run-id never changes), so `analyze` continues to work against
one manifest covering the whole matrix. Resuming a fully-successful run is a
no-op. If `results.json` is missing or unreadable, resume conservatively
treats every cell as missing and re-runs the whole matrix rather than risk
silently skipping something it couldn't verify.

`--resume` re-executes against the *current* config, so picking up a changed
prompt/evaluator/variant mid-iteration is expected. Changing the scheduling
strategy (`runner.parallel`, `variant_order`, `seed`) between the original
run and a resume, however, only prints a warning — the merged manifest may
then mix scheduling strategies across the original and resumed cells.

## Advanced Configuration

Beyond the basic `runner`/`variants`/`tasks` shape, several knobs tune *how* the
matrix is scheduled, sampled, and validated. They are opt-in — every default
reproduces the simplest behavior — so reach for them once you're past a first
working eval and want less measurement noise or broader input coverage:

| Feature | What it does | Details |
|---------|--------------|---------|
| **Dry-run mode** | Preview the plan + matrix size without building images or running anything | [Dry-Run Mode](#dry-run-mode) |
| **Variant ordering** | Rotate/shuffle which variant runs first per epoch to cancel order effects (`variant_order` + `seed`) | [Variant Order](#variant-order-reducing-measurement-bias) |
| **Multi-fixture tasks** | Run one task across several workspaces (`fixtures: [...]`) so a result isn't tied to one input | [Multiple fixtures per task](#multiple-fixtures-per-task-input-coverage-axis) |
| **Remote fixtures** | Declare a fixture by `url` + `sha256` (dataset-as-code); fetched, cached, and verified | [Remote fixtures](#remote-fixtures-dataset-as-code) |
| **Self-consistency** | Sample each judge N times and aggregate (`judge_samples` + `judge_aggregate`) to damp judge noise | [Judge Self-Consistency](#judge-self-consistency--reliability) |
| **Output instruction** | Customize or disable the sentence appended to every prompt (`output_instruction`) | [Output Instruction](#output-instruction) |
| **Per-task health check** | Gate a run on an environment-readiness script (`health_check`) | [Health Check](#health-check) |

A runnable, dependency-free config that exercises the config-level knobs (variant
ordering, multi-fixture, self-consistency, output instruction, health check) lives
in [`examples/advanced-features/`](../examples/advanced-features/) — `validate` it,
then `run --dry-run` it (the sixth feature) to see the whole matrix without
touching Docker.

## eval-config.yaml

Each eval set is defined by a single `eval-config.yaml` file. It contains global settings, variants, and tasks.

### External task/variant files

Tasks and variants can also be defined in separate YAML files:

- **`tasks/*.yaml`** — one task per file (file stem is used as fallback name)
- **`variants/*.yaml`** — one variant per file (file stem is used as fallback name)

When these directories exist and contain `.yaml` files, they are the **primary** source. Inline definitions in `eval-config.yaml` are used only as a fallback when no external files are found.

> **Note**: Only `.yaml` extension is recognized (not `.yml`). Each file must be a single mapping (not wrapped in a `tasks:` or `variants:` array).

**External task file example** (`tasks/code-review.yaml`):

```yaml
# No wrapping `tasks:` key — the file IS the task definition.
name: code-review
prompt: "Review the code for bugs and security issues."
fixture: sample-app
evaluators:
  - name: thoroughness
    type: judge
    prompt: "Rate on 1-10..."
```

**External variant file example** (`variants/with-plugin.yaml`):

```yaml
# No wrapping `variants:` key — the file IS the variant definition.
description: "Copilot CLI with custom plugin"
build:
  dockerfile: docker/Dockerfile.with-plugin
run:
  script: scripts/setup.sh
vars:
  feature: enabled
```

```yaml
vars:
  key: value                     # Global variables for prompt interpolation

runner:
  epochs: 3                     # Repetitions per task×variant
  timeout_seconds: 300           # Max seconds per Copilot run
  retries: 0                     # Retry a run this many times on transient failure (DockerError/timeout); 0 disables
  retry_delay: 5.0               # Base seconds between retries; doubles per attempt (exponential backoff), capped at 60s
  budget_limit: null             # Max estimated USD cost for a `run` invocation; null = unlimited. See "Cost governance" below.
  model: claude-sonnet-4         # Copilot model
  judge_model: claude-sonnet-4.6 # Model for LLM-as-Judge (separate from eval model)
  judge_samples: 1               # Self-consistency: sample each judge N times, aggregate
  judge_aggregate: median        # median | mean | majority (over successful samples)
  judge_batch: false             # Opt-in: score all of a task's judges in one LLM call
  reasoning_effort: null         # Optional: low|medium|high
  max_turns: 20                  # Max autopilot turns
  parallel: off                  # off | per_task | full
  max_workers: 8                 # Max concurrent runs (for parallel modes + analyze judges)
  variant_order: fixed           # fixed | counterbalance | random (order of variants per epoch)
  seed: null                     # Optional RNG seed for variant_order=random (reproducibility)
  judge_timeout_seconds: 60      # Per-judge Copilot timeout in analyze (seconds)
  judge_copilot_version: null    # Optional: expected host `copilot --version`; analyze warns on mismatch
  judge_max_conversation_chars: 8000  # Max chars of conversation passed to the judge
  judge_max_output_chars: 8000   # Max chars of output-file text passed to the judge
  output_format: text            # text | json
  capture_content: true          # Capture prompt/response content in OTel spans (needed by judge)
  output_instruction: Save all output files under /workspace/output/.  # Appended to every prompt; "" disables; supports {var} interpolation
  container_image_base: copilot-eval
  copilot_version: "1.x.x"       # Optional: Copilot CLI version pinned in the eval container; omit to inherit eval.config.DEFAULT_COPILOT_VERSION
  collector: file                 # file (default) | jaeger — trace collection backend
  # When collector: jaeger, these are used:
  jaeger_url: "http://localhost:16686"              # Jaeger query UI/API (host side)
  otel_endpoint: "http://host.docker.internal:4318" # OTLP collector endpoint (inside container)
  trace_fetch_limit: 2000        # analyze: max traces to request from Jaeger
  trace_fetch_retries: 5         # analyze: attempts to wait for trace ingestion
  trace_fetch_retry_delay: 2.0   # analyze: seconds between ingestion retries
  backend: docker                # Agent execution backend (docker is the only built-in; see Extensibility)
  resources:                    # Optional: Docker container resource limits (reduces metric noise)
    cpus: "2.0"                  # --cpus: number of CPUs available to the container
    memory: "4g"                 # --memory: memory limit (b|k|m|g units, or raw bytes)
    pids_limit: 100               # --pids-limit: max processes in the container

variants:
  - name: baseline
    description: "Control group"
    build:
      dockerfile: path/to/Dockerfile     # Optional: custom Dockerfile
    run:
      script: path/to/setup.sh           # Optional: sourced inside container before Copilot
    model: null                          # Optional: override runner.model per variant
    vars: {}                             # Variant-level variable overrides

tasks:
  - name: my-task
    prompt: "Do something with {key}"    # {key} interpolated from vars
    enabled: true
    fixture: my-fixture                  # Directory under fixtures/ to mount at /workspace
    # fixture: {url: https://…/set.tar.gz, sha256: "abc…"}  # Or: a remote dataset-as-code fixture
    # fixtures: [app-a, app-b]           # Or: run the task against multiple fixtures (input-coverage axis)
    timeout_seconds: null                # Override runner.timeout_seconds
    health_check: scripts/check.sh       # Script that must pass before running
    vars: {}                             # Task-level variable overrides
    hooks:
      before_run: scripts/setup.sh       # Run before Copilot
      after_run: scripts/cleanup.sh      # Run after Copilot
      on_failure: fail                   # before_run failure policy: fail | warn (default: fail)
    evaluators:
      - name: quality
        type: judge                      # judge | script | contains | regex | metric | python
        prompt: "Rate on 1-10..."
```

## Variable Resolution

Variables are merged in order: `global vars` → `task vars` → `variant vars`. Later values override earlier ones.

The prompt also gets an output-path instruction appended automatically so that generated files are available to judges — see [Output Instruction](#output-instruction) below to customize or disable it.

## Output Instruction

Judges score the files a run produces, so they need to end up somewhere the
framework can collect (`/workspace/output/`). To guarantee that, the framework
appends a short instruction to **every** task prompt (after a `\n\n` separator).
By default this is `"Save all output files under /workspace/output/."`. Control
it with `runner.output_instruction`:

| Value | Behavior |
|-------|----------|
| unset | The default sentence above (backward compatible). |
| `null` | Same as unset (the default sentence). |
| `""` (empty string) | Nothing is appended — disable it entirely. |
| custom string | Appended verbatim, with the same `{var}` interpolation as prompts (so it can adapt per variant). |

```yaml
runner:
  # Custom instruction, interpolated per variant (e.g. vars: { language: Japanese }):
  output_instruction: "Write any files under /workspace/output/. Respond in {language}."
```

**When to override or disable:**

- **Disable (`""`)** when the task prompt already names its own output path (a
  fixed appended sentence would be redundant), or to avoid injecting English
  into a non-English prompt where it would confound the comparison. See
  `examples/prompt-language`, which sets `output_instruction: ""` for exactly
  this reason.
- **Customize** when your workflow writes elsewhere, or to add per-variant
  guidance via `{var}` interpolation.

Because the instruction is interpolated like a prompt, `copilot-eval validate`
also checks that every `{var}` placeholder in it resolves for each variant.

## Trace Collector

`runner.collector` selects how OTel traces are captured and read back by `analyze`:

- **`file`** (default) — Copilot writes spans to a JSONL file inside the container
  (`/workspace/.traces/traces.jsonl`). After the run, it is copied to
  `results/<run_id>/.traces/traces.jsonl` and read directly by `analyze`. No
  external services are required — `jaeger_url`/`otel_endpoint` are ignored.
- **`jaeger`** — spans are exported over OTLP to a running Jaeger instance
  (`otel_endpoint`, used inside the container) and `analyze` fetches them back over
  Jaeger's HTTP API (`jaeger_url`, used on the host). Requires `docker-compose up -d`
  (see the repo's `docker-compose.yml`). Useful when you want to browse traces
  interactively in the Jaeger UI, or for compatibility with existing Jaeger-based
  workflows.

See [Architecture: Trace Collection](architecture.md#trace-collection) for the full
dual-abstraction design (`AgentRunner` + `TraceCollector`).

## Extensibility

Evaluators, runners (`runner.backend`), and trace collectors (`runner.collector`) are
all registry-driven (`EVALUATOR_REGISTRY` / `RUNNER_REGISTRY` / `COLLECTOR_REGISTRY`),
so a third-party package can register a new `type:`/`backend:`/`collector:` value
without forking the framework, via Python
[entry points](https://packaging.python.org/en/latest/specifications/entry-points/):

```toml
# my-plugin-package's pyproject.toml
[project.entry-points."copilot_eval.evaluators"]
my_type = "my_package.evaluators:MyEvaluator"

[project.entry-points."copilot_eval.runners"]
my_backend = "my_package.runners:MyRunner"

[project.entry-points."copilot_eval.collectors"]
my_collector = "my_package.collectors:MyCollector"
```

Each class implements the corresponding `Protocol` in `eval/protocols.py`
(`Evaluator` / `AgentRunner` / `TraceCollector`). Once the plugin package is
installed alongside `copilot-eval`, `copilot-eval` discovers it at CLI startup
and the new `type:`/`backend:`/`collector:` value validates and dispatches
like a built-in one — no changes to `eval.config` or `eval.runner` needed. For
a lighter-weight alternative that doesn't require packaging a plugin, see the
[Python Evaluator](#python-evaluator) above, which calls a plain `module:func`
in-process.

`docker` (via `DockerCLIRunner`) is the only built-in runner backend today —
it's registered like any other, not hardcoded, so the framework stays
"environment-isolated" rather than "Docker-isolated" (see
[docs/vision.md](vision.md)).

## Variants

Each variant gets its own Docker image built from a Dockerfile. The image inherits from `copilot-eval:base` (built from `docker/Dockerfile`).

```dockerfile
# Example: my-variant/Dockerfile
FROM copilot-eval:base
RUN copilot plugin install my-org/my-plugin
```

The optional `run.script` is sourced inside the container before Copilot runs (e.g., for authentication).

### Variant definition example

```yaml
variants:
  - name: with-plugin
    description: "Copilot CLI with custom plugin"
    build:
      dockerfile: docker/Dockerfile.with-plugin
    run:
      script: scripts/setup.sh
    model: gpt-4.1
    vars:
      feature: enabled
```

## Evaluators

Six evaluator types are supported:

| Type | Config | What it does |
|------|--------|-------------|
| `judge` | `prompt` **or** `criterion`+`rubric` | LLM scores the output on 1-10 scale |
| `script` | `script` | Bash script; exit 0 = pass |
| `contains` | `value` | Checks if string exists in output |
| `regex` | `value` | Checks if regex matches output |
| `metric` | `metric`, `op`, `value` | Thresholds a numeric run metric (pass/fail) |
| `python` | `script` (`module:func`) | Calls an in-process Python function with the run's `EvalContext` |

Each evaluator requires a unique `name` within its task and a valid `type`. The type-specific field(s) above are mandatory (e.g. `judge` requires a `prompt` or a `rubric`). Invalid types, missing required fields, duplicate names, invalid regex `value`s, and invalid `metric`/`op`/`value` are rejected at config load time with a clear `ConfigError`. Third-party plugins (see [Extensibility](#extensibility) below) may register additional `type` strings beyond these six.

### Python Evaluator

The `python` evaluator calls a function in-process instead of shelling out to a script, which is useful when you want direct access to the run's structured data (`Task`/`Variant` objects, log/work directories) instead of just an exit code:

```yaml
evaluators:
  - name: custom-check
    type: python
    script: my_package.evaluators:check_output   # module:func
```

- `script` must be a `module:func` reference resolvable via `importlib.import_module` — the module must be importable from wherever `copilot-eval` runs (e.g. installed in the same environment, or on `PYTHONPATH`). Both the module and function name must be non-empty (`module:func`, not `:func` or `module:`).
- The function is called as `func(context: EvalContext) -> EvalScore | None`.
- Like `script`/`contains`/`regex`, `python` runs **inline**, right after each task's container run (it is not deferred to `analyze`). Only the inline fields of `EvalContext` (`eval/protocols.py`) are populated: `task`, `variant`, `log_file`, `work_dir`, `token`. `conversation`, `output_files_text`, and `metrics` are always `None` for `python` evaluators — those are only filled in for `judge`/`metric` evaluators, which run later during `analyze` against the captured transcript/traces.
- Returning `None` means "not applicable to this context" (mirrors `script`/`contains`/`regex`); returning anything other than `EvalScore | None` raises a clear error instead of silently mis-scoring.

### Metric Evaluator

The `metric` evaluator turns collected telemetry into a pass/fail gate — useful for
CI, where you want to *fail the build* if a customization makes the agent slower or
more expensive rather than just observing the delta.

```yaml
evaluators:
  - name: cost-budget
    type: metric
    metric: cost            # RunMetrics field to assert on
    op: "<"                 # < <= > >= == !=
    value: 0.5              # numeric threshold
  - name: latency-budget
    type: metric
    metric: duration
    op: "<="
    value: 60
```

- Scores **deterministically** (pass → `1`, fail → `0`), like `contains`/`regex`, and
  is written to the same `*.scores.json` file, so it feeds the same pass/fail path and
  shows up in the report alongside other scores.
- Runs during `analyze` from the parsed traces — **no extra LLM calls**. It is
  recomputed on every `analyze` (idempotently) and runs even with `--skip-eval`, so a
  gate always reflects the current telemetry.
- **CI gating:** when any metric gate does not pass, `analyze` exits **non-zero** (with a
  summary of the failed gates), so a cost/latency/token regression can block a merge.
  Tasks with no metric evaluators are unaffected and still exit `0`.
- **Fails closed:** a metric value that can't be derived scores `null` and counts as a
  failure (never a silent pass). This covers: (1) a trace that yields no metrics at all
  for a metric-gated task; (2) an absent or `"?"` `github.copilot.cost` tag, which is
  rendered as `0.0` for reporting but treated as **unavailable** for a `cost` gate (so a
  `cost < X` budget can't silently pass on missing cost telemetry); (3) a metric-gated
  run recorded in the run manifest that produced no usable trace at all (dropped or
  timed-out telemetry) — reconciled against the manifest so its gate fails rather than
  being silently skipped; and (4) a metric-gated run that produced zero telemetry with no
  manifest to reconcile against, which fails the command rather than exiting `0`.

Assertable metrics (fields of `RunMetrics`):

| `metric` | Meaning |
|----------|---------|
| `duration` / `duration_seconds` | Wall-clock duration of the agent run (seconds) |
| `turn_count` | Number of agent turns |
| `tool_count` | Number of tool calls |
| `tool_duration` | Total time spent in tool calls (seconds) |
| `total_input_tokens` | Prompt tokens across chat spans |
| `total_output_tokens` | Completion tokens across chat spans |
| `total_cache_tokens` | Cache-read input tokens |
| `total_tokens` | `total_input_tokens + total_output_tokens` |
| `cost` | Reported run cost |

### Judge Evaluator

The judge sees both the **conversation output** (Copilot's terminal log) and any **files written to `/workspace/output/`**. This ensures correct scoring even when Copilot writes results to files without echoing them.

A judge is defined either with a free-form `prompt`, or with the structured `criterion`+`rubric` form below. Either way the framework appends the strict-JSON output contract (`Output ONLY valid JSON: {"score": N, "reason": "..."}`) automatically — you never write it by hand.

Judge scoring is done by `runner.judge_model` (defaults to `gpt-4.1`). OTel is disabled during judge calls to avoid contaminating traces.

The judge runs with the **host** Copilot CLI, which is not version-pinned like the eval container. To keep scoring reproducible and observable, `analyze` records the host `copilot --version` into each judge score's `meta.judge_version` and surfaces it in the report. Set `runner.judge_copilot_version` to the version you expect; `analyze` warns when the host differs.

The judge context is bounded by `runner.judge_max_conversation_chars` (conversation/log text) and `runner.judge_max_output_chars` (output-file text). When either budget is exceeded the context is truncated, `meta.truncation` is recorded, and the report flags how many judge runs saw truncated context — raise these limits if the judge is missing decisive evidence. Each judge score's `meta.outcome` (`ok`/`parse_error`/`error`/`timeout`/`not_found`) plus the captured `returncode`/`stderr` are aggregated into the report's "Judge runtime" section so host failures are no longer silently collapsed.

Judges run during `analyze` and are scored idempotently: a judge is (re)run only when no judge score yet exists for that run (non-judge `script`/`contains`/`regex` scores share the same `.scores.json` file, so file presence alone does not skip judging). Use `analyze --re-eval` to force all judges to re-run. Judge timeouts or unparseable output produce `score: null` and are surfaced as warnings rather than dropped.

### Structured Rubric

Instead of hand-writing the scale anchors and the strict-JSON line in every judge `prompt`, use the structured `criterion`+`rubric` form. The framework composes the prompt (criterion + anchors) and appends the JSON contract:

```yaml
evaluators:
  - name: thoroughness
    type: judge
    criterion: "How thoroughly does the response explain the architecture?"
    rubric:
      "10": "Complete: components, data flow, and key design decisions"
      "7":  "Good: most components and flow, minor gaps"
      "4":  "Partial: some components, missing the flow"
      "1":  "Minimal: vague or mostly missing"
```

This composes to a prompt like:

```
How thoroughly does the response explain the architecture?

Score from 1 to 10 using these anchors:
- 10: Complete: components, data flow, and key design decisions
- 7: Good: most components and flow, minor gaps
- 4: Partial: some components, missing the flow
- 1: Minimal: vague or mostly missing
```

Rules:

- A judge still produces **one scalar score** — the rubric structures anchors for a single axis, it does **not** introduce multi-dimensional aggregation (this preserves the report contract).
- `criterion` is required when `rubric` is set; `rubric` must be a non-empty mapping of integer scores to non-empty descriptions (keys may be quoted like `"10"` or bare integers). Anchors are listed high-to-low regardless of order.
- `prompt` and `rubric` are **mutually exclusive** on the same judge.
- Plain-string `prompt:` judges keep working unchanged — the rubric is optional sugar.

### Judge Self-Consistency & Reliability

Single-shot judge scores are noisy. Set `runner.judge_samples > 1` to sample each
judge multiple times and aggregate the successful scores via `runner.judge_aggregate`
(`median` — default, `mean`, or `majority`). Each sample's outcome is classified as
`ok` / `parse_error` / `timeout` / `error` (among others).

Scores are integers, so `median`/`mean` round to the nearest integer using
**round-half-up** (an average of `6.5` becomes `7`, not Python's banker's-rounding
`6`), and `majority` breaks ties toward the **lower** score for determinism. Pick
`median` (robust to outliers) unless you specifically want the mean's sensitivity
or a mode-style `majority` vote.

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

### Pass@k / Pass^k Reliability

Any evaluator's per-epoch `passed` bit (see the `.scores.json` shape above — it's
persisted by every evaluator type, not just `judge`) also feeds two agent-eval
reliability metrics, following [Anthropic's demystifying-evals
methodology](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents):

- **pass@k** — did the evaluator succeed at least once across `k` epochs? (the
  capability ceiling: "can it ever do this?")
- **pass^k** — did the evaluator succeed on *every* one of `k` epochs? (the
  consistency floor: "does it do this reliably?")

Both are computed per evaluator, per variant, over each **task-run** — a single
fixture's epochs (or, for single-fixture/legacy tasks, the task's only fixture,
which degrades pass@k/pass^k to a binary 0%/100% result) — and then averaged
across task-runs:

```
pass_at_k_rate  = mean(1 if any(epoch_results) else 0 for each task-run)
pass_all_k_rate = mean(1 if all(epoch_results) else 0 for each task-run)
```

The report renders one `pass@k (<evaluator>)` / `pass^k (<evaluator>)` row pair
per evaluator that produced scores, right after the judge scores, with the same
paired bootstrap CI as other metrics — except the delta is an **absolute
percentage-point** difference (the values are already 0-100 rates), e.g.
`baseline=67%, experimental=100% → +33%`. Use `runner.fixtures` (see
[Multiple fixtures per task](#multiple-fixtures-per-task-input-coverage-axis))
to get a real multi-task-run rate instead of the single-fixture binary case.

`k` is the number of epochs actually observed; when it's below 3 the report adds
an `insufficient_k` warning (surfaced alongside the other statistical-power
warnings) since "did it ever/always succeed" is mostly noise with fewer than 3
attempts — at `k=1`, pass@k and pass^k are identical and just restate the raw
success rate.

### Batched Judging (opt-in)

By default each judge evaluator is scored by its own Copilot call, so a task with
`n` judges sampled `judge_samples` times makes `n × judge_samples` calls. Set
`runner.judge_batch: true` to score **all** of a task's judges in a *single* call
per sample: one prompt asks for every criterion at once, returning a JSON object
keyed by evaluator name that is split back into per-evaluator scores. Calls drop
from `n_judges × judge_samples` to `judge_samples`.

This is an internal optimization only — the config still declares `n` independent
judges and the report contract is unchanged (1 judge = 1 scalar score). It is
**off by default** because batching trades accuracy for cost:

1. **Halo effect / cross-contamination** — scoring multiple criteria in one prompt
   lets the model drag one score toward another. Independent calls isolate each
   criterion.
2. **Failure blast radius** — an unparseable batched response fails *every*
   criterion at once; independently, a failure is per-criterion. (A parseable
   response with a missing/invalid key still fails only that one criterion.)
3. **Sample correlation** — within a self-consistency sample, per-criterion noise
   becomes correlated, so per-criterion σ is no longer independent.

Prefer the default (independent judges) when accuracy matters; enable
`judge_batch` for large judge suites where cost/latency dominates. A task with a
single judge behaves identically either way. Re-run `analyze --re-eval` to
re-score existing runs after toggling this.

To assess whether the judge itself is calibrated, see the `examples/judge-calibration`
eval set: it pins Copilot output to fixed answers with known expected score bands.

### Ground Truth in Judge Prompts

For reliable scoring, include the expected answer in the judge's `criterion` (or `prompt`):

```yaml
evaluators:
  - name: thoroughness
    type: judge
    criterion: |
      The code has these known issues:
      1. eval() with user input (line 36)
      2. Plaintext password storage (line 15)
      3. No auth on DELETE endpoint (line 27)
      Rate how many issues the review found.
    rubric:
      "10": "All 3 issues found with correct descriptions"
      "5":  "1-2 issues found"
      "1":  "No issues found"
```

For extra reliability, pair noisy judges with **deterministic ground-truth
evaluators** (`contains`/`regex`/`script`) that check language-neutral, objective
signals (function names, line numbers, required tokens). These score 0/1 with no LLM
call, so they don't drift between runs and anchor the judge. See the `gt-*` evaluators
in `examples/prompt-language` and `examples/judge-calibration`.

## Fixtures

Place files under `<config-dir>/fixtures/<fixture-name>/`. They are copied to a temp directory and mounted at `/workspace` inside the container (read-write). An `output/` subdirectory is automatically created. A fixture can also be a [remote dataset](#remote-fixtures-dataset-as-code) declared by `url` + `sha256` instead of a local directory.

### Multiple fixtures per task (input-coverage axis)

A task can be run against several fixtures so a customization is A/B-compared across diverse workspaces instead of a single, possibly cherry-picked one. Use the `fixtures` list instead of the singular `fixture`:

```yaml
tasks:
  - name: refactor
    prompt: "Refactor the module"
    fixtures: [small-app, legacy-app, monorepo]   # each becomes its own run
```

This expands the eval matrix from `variant × epoch` to `variant × fixture × epoch`. Each fixture is executed as its own run with its own persisted telemetry and output, keyed by a `__fixture__<name>` suffix on the run slug and an `eval.fixture` OTel resource tag. The `analyze` report pairs variants within each `(fixture, epoch)` cell and pools the paired deltas across fixtures, while per-run rows remain labelled `<fixture>#<epoch>` so the per-fixture breakdown stays visible.

The singular `fixture:` form keeps working unchanged and is equivalent to a single-element `fixtures:` list (no `__fixture__` suffix, empty `eval.fixture` tag).

### Remote fixtures (dataset-as-code)

Instead of vendoring a large or canonical evaluation input into every repo, a fixture can be declared by a remote **URL + expected sha256**. The archive/file is fetched on first use, verified against the checksum, cached content-addressed, and then mounted exactly like a local fixture:

```yaml
tasks:
  - name: code-review
    prompt: "Review the changes"
    fixture:
      url: https://example.com/datasets/code-review-v3.tar.gz
      sha256: "abc123…"   # required — the digest of the downloaded bytes
      # name: code-review  # optional; derived from the URL filename when omitted
```

Remote fixtures also work as entries in a multi-fixture `fixtures:` list, mixed freely with local directory names:

```yaml
    fixtures:
      - local-app
      - { url: https://example.com/remote-set.zip, sha256: "def456…" }
```

**Behavior**

- **Fetched + verified on first use.** On `run` (and `pin-fixtures`), each remote fixture is downloaded and its sha256 is checked. A **mismatch fails closed** — the run aborts with an actionable error before any Docker work, so an eval never executes against unverified inputs.
- **Content-addressed cache.** Verified downloads and their extracted content are cached under `<config-dir>/.fixtures-cache/` keyed by sha256. Later runs reuse the cache without re-downloading, and an **offline run against an already-cached fixture just works** (no network access). The cache is regenerable — add `.fixtures-cache/` to `.gitignore` (the scaffolded `.gitignore` already does).
- **Formats.** `.tar.gz` / `.tgz` / `.tar` and `.zip` archives are extracted (regular files and directories only; symlinks/special members and path-traversal entries are skipped for safety). Anything else is treated as a plain single file and placed in the workspace under its URL basename. Archives are extracted as-is — no top-level directory stripping — so the workspace layout matches the archive.
- **Name / identity.** The fixture's `name` (declared, or derived from the URL filename with the archive extension stripped, e.g. `code-review-v3.tar.gz` → `code-review-v3`) is its identity in the eval matrix, `fixtures.lock`, and the run manifest. If a name can't be derived, declare one explicitly.
- **Interoperates with pinning.** `pin-fixtures` fetches the remote fixture and records the **extracted content hash** in `fixtures.lock` (plus a `remote: {url, sha256}` provenance block), so `run --strict-fixtures` still gates on content reproducibility. Each run's `results.json` manifest likewise records the content hash and remote source (see below).

### Pinning fixtures (content-hash integrity)

Fixtures are plain directories with no integrity guarantees — between runs their files can be silently modified, which invalidates cross-run comparisons and leaves no proof that two runs used identical inputs. `pin-fixtures` addresses this by writing a content-addressed lockfile:

```bash
uv run copilot-eval pin-fixtures --config-dir <dir>
```

This walks every fixture referenced by the config's tasks and writes `<config-dir>/fixtures.lock`, a human-readable YAML manifest:

```yaml
# fixtures.lock — generated by `copilot-eval pin-fixtures`.
version: 1
fixtures:
  code-review:
    sha256: abc123…          # digest over the whole fixture's file contents
    total_size: 45678
    files:
      - path: src/app.py
        sha256: def456…
        size: 1234
```

The lockfile is fully content-addressed (no timestamps, deterministic ordering), so re-pinning unchanged fixtures produces an identical file and git diffs stay meaningful. Commit it alongside your config.

Hashing mirrors what the runner actually materializes into the sandbox: every regular file is hashed by content, and because the runner copies fixtures with `shutil.copytree(..., symlinks=False)`, **file symlinks are followed and hashed by their target content**. Empty directories, directory symlinks, and POSIX file modes are outside the integrity guarantee (they rarely change a fixture's effective input). Filenames are hashed as raw bytes, so non-UTF-8 names never break pinning.

At `run` time, fixtures are re-hashed and compared against the lockfile *before any Docker work* (so a forgotten re-pin fails fast, including under `--dry-run`):

- **Drift** (a fixture's contents changed, or a pinned fixture is missing) prints a warning but does not block the run.
- **`run --strict-fixtures`** turns any drift — plus a missing/malformed lockfile, an unsupported lockfile `version`, a referenced-but-unpinned fixture, or an explicitly-declared fixture missing on disk — into a hard failure (exit code 1), so CI can gate on fixture reproducibility.

Every run also records the fixtures' content hashes in its `results.json` manifest (under `fixtures:`) for post-hoc reproducibility auditing; for remote fixtures the entry also carries a `remote: {url, sha256}` block tying the recorded content hash to its source. After intentionally changing a fixture, re-run `pin-fixtures` to update the lockfile.


## Hooks

`before_run` and `after_run` scripts run on the **host** (not inside Docker). Environment variables `EVAL_<KEY>` are set from resolved vars. Use them for:

- Environment setup/teardown (e.g., Azure resource reset)
- Pre-deployment of test scenarios

### Failure handling

Hook exit codes are checked (a missing script is treated as success):

- **`before_run`** — controlled by `hooks.on_failure`. With the default `fail`, a non-zero exit aborts the run with `status: setup_failed` (the run is not executed). With `warn`, the failure is logged and the run continues.
- **`after_run`** — a non-zero exit is always logged and surfaced as a failing `hook` score, so the run is marked as not passed without aborting the batch.

Per-run errors are isolated: an exception during setup (e.g. missing `docker` binary, fixture copy failure, a hook raising) is caught and recorded as `status: setup_failed` for that run only — it never aborts the whole batch, and the run manifest is always written.

### Retrying transient failures

`runner.retries` (default `0`) retries a run when it fails with a **transient** error — a Docker daemon hiccup (`DockerError`) or a container timeout (`timeout_seconds` exceeded) — instead of immediately recording it as `setup_failed`/`timeout`. Deterministic failures (auth, hook, fixture errors) are never retried, since re-running them would just fail the same way.

Each retry waits `retry_delay * 2**attempt` seconds (exponential backoff), capped at 60s, and is logged with the attempt number and error. The manifest's `retry_count` field records how many retries a run needed (`0` = passed/failed on the first try), so `analyze` can distinguish a clean result from a flaky one that needed a re-run.

```yaml
runner:
  retries: 2       # up to 2 retries (3 attempts total) on DockerError/timeout
  retry_delay: 5.0 # 5s, then 10s between retries
```

## Cost governance

`run` and its LLM-as-judge evaluators both burn tokens/compute. `eval.services.cost_service` gives a **pre-flight, order-of-magnitude estimate** — not a bill (see `docs/vision.md`'s non-goals: no FinOps/billing platform) — plus an optional hard budget cap.

**Pre-flight estimate** (`run --estimate`): before doing any Docker/agent work, prints a cost breakdown (cells, judge calls, estimated tokens, estimated USD cost) and asks for confirmation, unless `--yes` is passed:

```
$ uv run copilot-eval run --estimate
==================================================
 Cost Estimate (pre-flight)
==================================================
 Cells:        6
 Judge calls:  12
 Agent tokens: 48,000 in / 12,000 out
 Judge tokens: 36,000 in / 2,400 out
 Basis:        default estimate (no historical run data found)
 Agent cost:   $0.8400
 Judge cost:   $0.4320
 Total cost:   $1.2720
==================================================
Proceed with this run? [Y/n]:
```

Token/cell estimates come from `eval.services.cost_service.load_historical_costs`, which averages token usage from the most recent past runs' persisted file-collector traces (`results/*/.traces/*.jsonl`) when available, falling back to conservative hardcoded defaults on a project's first run.

**Budget cap** (`runner.budget_limit`, or `run --budget-limit`): a maximum estimated USD cost. This gate is always applied (independent of `--estimate`) — `run` aborts with an error before any Docker/agent work if the estimate exceeds it:

```yaml
runner:
  budget_limit: 10.0   # abort if the pre-flight estimate exceeds $10
```

```
uv run copilot-eval run --budget-limit 5.0   # per-invocation override
```

**Judge cost tracking**: each judge `EvalScore.meta` records `judge_tokens_in`/`judge_tokens_out` (best-effort — parsed from the judge CLI response when it reports usage, else estimated from prompt/response length via a chars/4 heuristic, flagged `judge_tokens_estimated: true`). These are summed across a run's scores and printed in the run summary, and persisted under a top-level `cost` key in the run's `results.json` manifest alongside the pre-flight estimate:

```json
{
  "cost": {
    "estimate": { "cells": 6, "judge_calls": 12, "cost_total": 1.272, "...": "..." },
    "judge_tokens_in": 33800,
    "judge_tokens_out": 2210
  }
}
```

## Health Check

A per-task script that validates the environment is ready *before* Copilot runs.
Use it when a task depends on external state that a hook set up (a deployed
resource, a reachable service, a seeded database) and you'd rather skip a run
than let it execute against a half-ready environment and pollute the metrics.

```yaml
tasks:
  - name: deploy-check
    prompt: "Diagnose why the app returns 500s and fix it."
    health_check: scripts/health-check.sh   # path relative to the config dir
```

**Execution & format:**

- The path is resolved relative to the **config dir** first, then the project
  root. A **missing script is treated as a pass** (the check is skipped), so a
  broken path never silently blocks runs — `copilot-eval validate` flags an
  unresolvable `health_check` reference for you.
- The script runs on the **host** (like hooks, not inside Docker) via `bash`,
  after the `before_run` hook and before the container starts. Resolved
  task/variant vars are exported as `EVAL_<KEY>` (e.g. `variant_label` →
  `EVAL_VARIANT_LABEL`), alongside the process environment and `.env` values.
- **Exit code 0 → ready**, the run proceeds. **Non-zero → the run is skipped**
  with `status: setup_failed` (it is never executed, and no telemetry/output is
  produced for that cell). Script stdout/stderr is appended to the run log.
- If the script itself fails to *launch* (e.g. `bash` is unavailable or the OS
  refuses to start the subprocess), that raises a `HookError` for the cell,
  which the runner isolates as `setup_failed` — it never aborts the rest of the
  batch. (The script is invoked as `bash <script>`, so a missing execute bit or
  shebang does not by itself cause a launch failure.)

**Interaction with hooks:** the order per run is `before_run` hook →
`health_check` → container. The hook sets the environment up; the health check
verifies it. Their failure semantics differ: `before_run` obeys
`hooks.on_failure` (`fail` aborts the run, `warn` continues), whereas a failed
`health_check` **always** skips the run. See
[`examples/advanced-features/scripts/health-check.sh`](../examples/advanced-features/scripts/health-check.sh)
for a minimal script.

## Container Resource Limits

`runner.resources` maps to Docker's `--cpus` / `--memory` / `--pids-limit` flags on the `docker run` command used for every eval run. Without limits, containers freely compete for host CPU/memory/process resources, which introduces noise into duration and other runtime metrics — especially under `parallel: full`. All fields are optional and default to unset (no limit, i.e. current behavior before this option existed).

```yaml
runner:
  resources:
    cpus: "2.0"      # Number of CPUs (docker --cpus); positive number as a string
    memory: "4g"     # Memory limit (docker --memory); <number>[b|k|m|g], case-insensitive
    pids_limit: 100  # Max processes/threads in the container (docker --pids-limit); positive integer
```

Invalid values (e.g. `cpus: "abc"`, `memory: "4gb"`, `pids_limit: -1`) are rejected at config-load time with a `ConfigError`, so `copilot-eval validate` catches them before any container starts.

## Parallel Modes

| Mode | Behavior |
|------|----------|
| `off` | Sequential execution |
| `per_task` | Tasks run in parallel, variants within a task are sequential |
| `full` | All task×variant×epoch combinations run in parallel (up to `max_workers`) |

During `analyze`, judge evaluators are always run in parallel across traces (up to `max_workers`), independent of the `parallel` mode above. Each judge's Copilot invocation is bounded by `judge_timeout_seconds`. Scores files are written per trace, so parallel judging does not cause write conflicts.

## Progress Reporting

`run` and `analyze` show live progress so a 30+ minute parallel matrix isn't a silent black box:

- **Interactive terminal (TTY) with `rich` installed**: a live progress bar with percentage and ETA, plus a rolling list of per-cell status (`✓ completed`, `✗ failed`, `● running`). ETA is derived from the average duration of completed cells, divided by the effective concurrency (`max_workers`, or fewer if there are fewer tasks/variants to fill them).
- **Non-TTY (CI logs, pipes) or `rich` not installed**: one compact log line per completed/failed cell, e.g. `[12/40] completed: code-review/baseline/e1 (23s, completed)` or `[13/40] FAILED: code-review/baseline/e2 (timeout after 300s)`.
- **`analyze`** shows the same style of progress for judge scoring calls.

`rich` is an optional dependency (`pip install copilot-eval[progress]` / `uv sync --extra progress`); without it, output falls back to the plain log-line format even on a TTY.

Pass `--no-progress` to `run` or `analyze` to disable all progress output (useful for scripts that parse stdout, or to keep logs minimal).

## Dry-Run Mode

`run --dry-run` previews *what* a run would do without doing it — no Docker
build, no pre-flight readiness checks, no containers, no telemetry. Use it to
sanity-check the resolved plan and the matrix size before committing minutes (or
budget) to a real run:

```bash
uv run copilot-eval run --config-dir examples/advanced-features --dry-run
```

```text
==================================================
 Copilot Eval Runner
==================================================
 Model:    gpt-5.4-mini
 Effort:   default
 Max turns:unlimited
 Epochs:   4
 Timeout:  120s
 Parallel: off
 Collector: file
 Order:    counterbalance
 Run ID:   20260101-120000-abc123
 Vars:     {'language': 'English'}
 Variants:
   - baseline
   - experimental
 Tasks:
   - fix-bug
==================================================
[dry-run] Would run 4 epoch(s) × 2 variants × fixtures for each task (16 runs total).
```

**What it validates:** the config loads and passes schema/dataclass checks
(otherwise `run` errors before reaching dry-run), the task selection resolves
(`--task` names a real task; enabled tasks otherwise), and the effective
scheduling knobs — model, effort, epochs, `parallel`, `collector`, and
`variant_order` (with `seed`, shown as `Order: random (seed=42)`). The final line
reports the total matrix size: `epochs × variants × fixtures` summed over the
selected tasks (multi-fixture tasks count once per fixture).

**What it does *not* do:** dry-run returns *before* pre-flight, so it does **not**
check Docker, auth, disk space, or that fixture directories exist — use
[`validate`](#validation) for on-disk reference checks. It also skips image
builds and never executes hooks, health checks, or Copilot.

**Budget gate still applies:** the one thing that happens before the dry-run
early-return is the pre-flight cost estimate. It is always computed, and if it
exceeds `runner.budget_limit` / `--budget-limit` the command **aborts with an
error even under `--dry-run`** (see [Cost governance](#cost-governance)). With no
budget limit set (the default), dry-run always prints the plan.

**With `--resume`:** dry-run reports how many cells remain versus the full
matrix, e.g. `[dry-run] Would run 5 cell(s) out of 16 in the matrix (skipping 11
already completed).`

**With `--estimate`:** the full cost breakdown is additionally printed, but dry-run
skips the interactive "Proceed?" confirmation, since nothing runs anyway.

## Variant Order (reducing measurement bias)

In serial (`off`) and `per_task` modes, variants run one after another within each epoch. Always running them in the same order lets order effects (cache warmup, rate limits, time-of-day drift) accumulate on whichever variant runs first. `runner.variant_order` controls how variants are ordered per epoch:

| Mode | Behavior |
|------|----------|
| `fixed` | Config order, every epoch (default; backward compatible). |
| `counterbalance` | Cyclic rotation by epoch. Each variant occupies every position once per complete cycle of `N = len(variants)` epochs. This is position-balanced, not a full permutation/carryover counterbalance — to fully balance positions, set `epochs` to a multiple of `N` (otherwise the trailing partial cycle is imbalanced). |
| `random` | Shuffle each epoch. Set `runner.seed` for a reproducible schedule. |

Ordering applies to `off` and `per_task` modes. Under `full` parallel, true concurrency is decided by the thread pool, so ordering only affects submission order; the recorded per-run start times are what matter for analysis. With a `seed`, `random` ordering is reproducible in every mode (each task/epoch derives its own RNG, so parallel scheduling does not affect the result).

**Measurement-friendly preset.** For the least-biased comparison, run serially with counterbalanced order:

```yaml
runner:
  parallel: off
  variant_order: counterbalance
  epochs: 4            # a multiple of the number of variants for full balance
```

If you prefer randomization, use `variant_order: random` with a fixed `seed` so the run is reproducible.

## Execution Schedule Recording

Each run writes a `results.json` manifest under `results/<run-id>/`. It records the schedule so order/concurrency confounders can be analyzed after the fact:

- A top-level `schedule` block: `parallel`, `max_workers`, `variant_order`, `seed`.
- A top-level `fixtures` block: per-fixture content hashes (`sha256`, `total_size`, `file_count`) for the fixtures this run consumed, for reproducibility auditing (see [Pinning fixtures](#pinning-fixtures-content-hash-integrity)).
- Per run:
  - `fixture` — the fixture the run used (empty unless the task declares multiple fixtures via `fixtures:`).
  - `order_index` — scheduled position. In `off` it is the global execution sequence; in `per_task` it is the position within that task; in `full` it is the submission index. It reflects *intended* order, not actual start order under concurrency — use `started_at` for that.
  - `started_at` / `finished_at` — microsecond wall-clock timestamps (`started_at` is captured before hooks/health-check).
  - `duration_seconds` — total run wall time, including hooks, the Copilot container run, and non-judge evaluators (not Copilot execution time alone).

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

