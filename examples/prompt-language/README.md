# Prompt Language A/B Evaluation

Compares English vs Japanese prompts on identical code tasks.

| Variant | Description |
|---------|-------------|
| **english** | Prompt and response in English |
| **japanese** | Prompt and response in Japanese |

## Tasks

- **code-review** — Review a Node.js Express app with 7 intentional security issues
- **explain-architecture** — Explain architecture and design patterns of the same app

Each task pairs noisy LLM judges with **deterministic ground-truth evaluators**
(`gt-*`, `type: regex`) that match language-neutral code tokens (`eval(`, `password`,
`/search`). These score 0/1 with no LLM call, so they don't drift between runs and
anchor the judge scores. The runner also uses `judge_samples: 3` with
`judge_aggregate: median` so each judge is sampled three times and the median is
taken (self-consistency); `analyze` prints a **Judge reliability** summary with the
score spread (σ) and parse/timeout/error rates.

## Run

```bash
uv run copilot-eval run --config-dir examples/prompt-language
uv run copilot-eval analyze --run-id <RUN_ID> --config-dir examples/prompt-language -o markdown
```

## Results

Model: gpt-5.4-mini, judge: gpt-4.1, 2 tasks × 2 variants × 3 epochs = 12 runs.

### code-review

| Metric | english | japanese | Δ |
|--------|--------:|--------:|------:|
| Duration (s) | **21.9** | 26.4 | +20.2% |
| Input tokens | **95K** | 92K | +1.8% |
| Output tokens | **1,222** | 1,357 | +13.1% |

| Evaluator | english | japanese | Δ |
|-----------|:---:|:---:|---|
| thoroughness | 7 | **8** | +14% |
| actionability | **8** | 6 | -25% |

- **thoroughness**: English finds 5-6 of 7 issues. Japanese finds up to 7/7 — rate limiting and in-memory storage are caught as formal findings rather than side notes
- **actionability**: English provides more concrete fix directions per finding. Japanese identifies issues accurately but with fewer code-level suggestions

### explain-architecture

| Metric | english | japanese | Δ |
|--------|--------:|--------:|------:|
| Duration (s) | **17.2** | 24.0 | +1.1% |
| Input tokens | 93K | 93K | 0% |
| Output tokens | **1,418** | 1,586 | +11.8% |

| Evaluator | english | japanese | Δ |
|-----------|:---:|:---:|---|
| completeness | **10** | 9 | -10% |
| clarity | 9 | 9 | 0% |

- **completeness**: English consistently covers all 5 components. Japanese occasionally misses the GET /search endpoint as a distinct component
- **clarity**: Both produce well-structured output with clear headings — no meaningful difference

### Takeaways

- Language gap is small — no dramatic quality difference
- code-review: Japanese finds more issues (thoroughness), English gives better fixes (actionability)
- explain-architecture: English is more complete, clarity is identical
- Token usage is nearly equal on this model — language choice doesn't significantly affect cost
