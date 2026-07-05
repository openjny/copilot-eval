# Advanced Features

A documentation companion for
[docs/configuration.md → Advanced Configuration](../../docs/configuration.md#advanced-configuration).
Rather than testing a real customization, this eval set exists to exercise every
"advanced" knob in one readable config. The variants differ only by a var (no
custom Dockerfile), so it stays dependency-free — read it, `validate` it, and
`run --dry-run` it to see the matrix expand.

## Features demonstrated

| Feature | Where in `eval-config.yaml` | Docs |
|---------|-----------------------------|------|
| **Variant ordering** | `runner.variant_order: counterbalance` (+ `seed` for `random`) | [Variant Order](../../docs/configuration.md#variant-order-reducing-measurement-bias) |
| **Self-consistency** | `runner.judge_samples: 3`, `runner.judge_aggregate: median` | [Judge Self-Consistency](../../docs/configuration.md#judge-self-consistency--reliability) |
| **Output instruction** | `runner.output_instruction: "..."` (`""` disables) | [Output Instruction](../../docs/configuration.md#output-instruction) |
| **Multi-fixture matrix** | `tasks[].fixtures: [small-app, legacy-app]` | [Multiple fixtures per task](../../docs/configuration.md#multiple-fixtures-per-task-input-coverage-axis) |
| **Per-task health check** | `tasks[].health_check: scripts/health-check.sh` | [Health Check](../../docs/configuration.md#health-check) |
| **Dry-run mode** | `run --dry-run` (see below) | [Dry-Run Mode](../../docs/configuration.md#dry-run-mode) |

## Try it

```bash
# Validate config, fixtures, and the health-check script reference:
uv run copilot-eval validate --config-dir examples/advanced-features

# Print the plan + matrix size without building images or running anything:
uv run copilot-eval run --config-dir examples/advanced-features --dry-run
```

The dry-run prints the run banner (note `Order: counterbalance`) followed by the
matrix size — `4 epoch(s) × 2 variants × fixtures for each task (16 runs total)`,
i.e. 4 × 2 × 2 fixtures — without touching Docker.
