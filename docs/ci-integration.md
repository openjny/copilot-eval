# CI Integration: PR Comments and Native Test Reports

When copilot-eval gates a PR (a change to a skill, custom agent, instruction,
hook, or MCP server), the result shouldn't be buried in CI logs. `analyze -o
markdown --compact` renders a condensed, PR-comment-friendly report you can
post directly with `gh pr comment` — no extra tooling required. For deeper CI
integration, `analyze -o junit`, `-o gha-summary`, and `-o html` give you a
native test report, a GitHub Actions step summary, and a self-contained
dashboard, respectively — all still driven by the same `analyze` command and
its statistical gates (`--min-epochs`, metric evaluators).

## Compact vs. full markdown

`-o markdown` (no `--compact`) renders the full report: per-metric tables,
tool-usage breakdowns, per-run details, and judge reasons. It's the right
format for `run`/`analyze` output committed to a file or read in a terminal.

`--compact` drops all of that and keeps only:

- A headline metrics/judge-score/pass@k table with a `Δ` column
- `**bold** ✅`/`❌` markers on deltas whose bootstrap CI excludes zero
  (multiple-comparison corrected, same as the full report)
- A one-line CI summary (`95% CI excludes zero for ...` / `N=<n> paired epochs`)
- Any data-sufficiency warnings (small sample size, low power, no paired epochs)

This keeps the comment scannable in a few seconds and comfortably under
GitHub's 65,536-character comment limit — the formatter truncates (with a
visible notice) as a last-resort safety net if a run has an unusually large
number of tasks/evaluators.

## Example

```bash
REPORT=$(uv run copilot-eval analyze --run-id "$RUN_ID" -o markdown --compact)
gh pr comment "$PR_NUMBER" --body "$REPORT"
```

Produces a comment like:

```markdown
## 📊 copilot-eval: code-review

| Metric | baseline | experimental | Δ |
|--------|----------|-------------|---|
| Score (thoroughness) | 7.2 ±0.8 | 8.1 ±0.6 | **+12.5%** ✅ |
| Duration (s) | 21.9 | 19.3 | -11.9% |

> 95% CI excludes zero for thoroughness. N=5 paired epochs.
```

## GitHub Actions workflow

See [`docs/examples/eval-pr-comment.yml.example`](examples/eval-pr-comment.yml.example)
for a full workflow: it runs the eval, posts (or updates) a single PR comment
with the compact report, and fails the job if `analyze`'s CI gates
(`--min-epochs`, metric evaluators) don't pass. Copy it into
`.github/workflows/eval-pr-comment.yml` and adjust `config-dir`/`epochs` for
your project — it's shipped as `.example` outside `.github/workflows/` so it
doesn't run as-is in this repo (which has no Copilot-customization PRs of its
own to gate) and doesn't require special `workflow`-scoped push permissions.

`analyze`'s exit code already reflects the CI gates (metric evaluator
thresholds, `--min-epochs`), so `fail-on-regression`-style behavior comes for
free: just don't swallow the command's exit status in your workflow step.

## JUnit XML (`-o junit`)

`analyze -o junit` emits standard JUnit XML (`<testsuites><testsuite><testcase>`,
via stdlib `xml.etree.ElementTree` — no `lxml`/`junit-xml` dependency) so any
CI system with native test-report support (GitHub Actions, Azure Pipelines,
Jenkins, GitLab CI, ...) can render copilot-eval results the same way it
renders unit test results:

- One `<testsuite>` per task.
- One `<testcase>` per metric/judge-score/pass@k comparison (`classname` is
  the task name, `name` is the metric).
- A comparison whose bootstrap CI excludes zero (multiple-comparison
  corrected) *and* moved in the unfavorable direction — metrics regress when
  they go up (duration, cost, tokens, ...), judge scores and pass@k/pass^k
  rates regress when they go down — renders as `<failure>`. Everything else
  is `<system-out>` with the values/delta/CI for context.

```bash
uv run copilot-eval analyze --run-id "$RUN_ID" -o junit > report.xml
```

Feed `report.xml` to your CI's test-report action (e.g.
[`dorny/test-reporter`](https://github.com/dorny/test-reporter) or GitHub's
built-in JUnit annotations) to get inline pass/fail annotations per metric.

## GitHub Actions step summary (`-o gha-summary`)

`analyze -o gha-summary` renders the same compact markdown as `-o markdown
--compact` and appends it to `$GITHUB_STEP_SUMMARY` when that env var is set
(GitHub Actions sets it automatically in every job step) — no `tee`/heredoc
plumbing required. Outside GitHub Actions (or if the env var is unset), it
falls back to printing the markdown to stdout.

```yaml
- name: Analyze results
  run: uv run copilot-eval analyze --run-id "${{ steps.run.outputs.run_id }}" -o gha-summary
```

## Self-contained HTML (`-o html`)

`analyze -o html` emits a single HTML file with all CSS inlined (no external
stylesheets, scripts, or fonts) — safe to upload as a build artifact or open
directly in a browser. Metric/judge-score/pass@k tables color-code
statistically significant deltas (green = improvement, red = regression) and
include a CSS-only bar per value for an at-a-glance sense of relative
magnitude.

```bash
uv run copilot-eval analyze --run-id "$RUN_ID" -o html > report.html
```

```yaml
- name: Analyze results
  run: uv run copilot-eval analyze --run-id "$RUN_ID" -o html > report.html
- uses: actions/upload-artifact@v4
  with:
    name: eval-report
    path: report.html
```
