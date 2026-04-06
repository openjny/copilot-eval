# Prompt Language A/B Evaluation

Compares English vs Japanese prompts on identical code tasks.

| Variant | Description |
|---------|-------------|
| **english** | Prompt and response in English |
| **japanese** | Prompt and response in Japanese |

## Tasks

- **code-review** — Review a Node.js Express app with 7 intentional security issues
- **explain-architecture** — Explain architecture and design patterns of the same app

## Run

```bash
uv run copilot-eval run --config-dir examples/prompt-language
uv run copilot-eval analyze --run-id <RUN_ID> --config-dir examples/prompt-language -o markdown
```

## Results

Model: gpt-5.4-mini, judge: claude-sonnet-4.6, 2 tasks × 2 variants × 3 epochs = 12 runs.

### code-review

| Metric | english | japanese | Δ |
|--------|--------:|--------:|------:|
| Duration (s) | **24.6** | 31.9 | +23.5% |
| Input tokens | **92K** | 130K | +39.8% |
| Output tokens | **1,247** | 1,542 | +10.9% |

| Evaluator | english | japanese | Δ |
|-----------|:---:|:---:|---|
| thoroughness | 9 | **10** | +11% |
| actionability | 7 | 7 | 0% |

- **thoroughness**: English finds 5-6 of 7 issues. Japanese finds up to 7/7 (eval RCE, plaintext passwords, missing auth, no input validation, no rate limiting, etc.)
- **actionability**: Both provide file/line references. English tends to have fewer concrete fix code examples

### explain-architecture

| Metric | english | japanese | Δ |
|--------|--------:|--------:|------:|
| Duration (s) | **19.6** | 25.1 | +14.6% |
| Input tokens | **92K** | 112K | +20.9% |
| Output tokens | **1,235** | 1,986 | +46.2% |

| Evaluator | english | japanese | Δ |
|-----------|:---:|:---:|---|
| completeness | **10** | 8 | -10% |
| clarity | 9 | 9 | 0% |

- **completeness**: English consistently covers all 5 components (Express setup, in-memory store, CRUD endpoints, search endpoint, server listener). Japanese sometimes misses the search endpoint details
- **clarity**: Both produce well-structured output with clear headings and logical flow — no meaningful difference

### Takeaways

- Language gap is small — no dramatic quality difference
- code-review slightly favors Japanese, explain-architecture favors English
- English is 30-40% more token-efficient across both tasks
