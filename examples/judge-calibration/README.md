# Judge Calibration

A reliability harness for the LLM judge itself. Instead of comparing two
environments, every task pins Copilot's output to a **fixed, known answer**, so
the judge's input is deterministic and its score should land in a documented
expected band. Running the judge repeatedly then tells you whether it is
*calibrated* (right score) and *reliable* (low variance).

## Tasks

| Task | Fixed answer quality | Expected `correctness` score |
|------|----------------------|------------------------------|
| **calib-high** | Complete, correct, with nuance | 9–10 |
| **calib-mid**  | Correct core, missing detail   | 4–6  |
| **calib-low**  | Factually wrong                | 1–2  |

Each task also has a deterministic `gt-mentions-404` (`type: regex`) evaluator.
`contains`/`regex` evaluators match against the run's **conversation log** (not
the output file), so this is an LLM-free anchor that the canonical answer text
appeared in the run — useful for spotting an off-topic or empty run independently
of the judge.

## How it works

- `runner.judge_samples: 5` — each judge is sampled five times per run.
- `runner.judge_aggregate: median` — the reported score is the median of the
  successful samples.
- `analyze` prints a **Judge reliability** summary: sample outcome rates
  (ok / parse_error / timeout / error) and the score spread (σ). Per-run judge
  cells in the markdown/JSON report show the score with its `±σ`.

## Run

```bash
uv run copilot-eval run --config-dir examples/judge-calibration
uv run copilot-eval analyze --run-id <RUN_ID> --config-dir examples/judge-calibration -o markdown
```

## Reading the results

1. **Calibration** — does each task's reported median fall inside its expected
   band above? A high σ or a median outside the band signals a miscalibrated or
   unstable judge (try a different `judge_model` or sharpen the rubric).
2. **Reliability** — check the Judge reliability summary. A non-zero
   parse_error/timeout rate means the judge prompt or model is unreliable;
   a large σ means single-shot scores would have been noisy.
3. **Ground truth** — `gt-mentions-404` should pass whenever Copilot echoes the
   canonical answer in its log; if it doesn't, the run likely went off-topic and
   the judge score isn't meaningful. (It checks the conversation log, so a run
   that writes the file silently without echoing it may still not match.)
