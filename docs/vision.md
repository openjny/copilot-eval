# Vision

> Make it easy to **prove** — with confidence intervals, not vibes — whether a Copilot customization actually helped.

copilot-eval exists for individuals and teams who are building AI-first development workflows. If you craft a set of skills, custom agents, instructions, or hooks to improve how your team works with Copilot, this tool tells you whether that investment paid off — reproducibly and statistically.

## Why This Exists

Generic LLM evaluation frameworks evaluate **prompts and model outputs** — string in, string out. Skill-level testing tools go further by benchmarking individual agent skills with structured validators. But when you customize Copilot, you're changing the **environment as a whole**: the combination of plugins, MCP servers, custom instructions, model selection, and hook scripts. A single skill may pass its unit tests yet degrade the overall workflow when combined with other primitives. The effect shows up in telemetry (token usage, tool calls, latency, cost) as much as in output quality.

No existing tool lets you say: "I added these instructions and this skill set to my team's Copilot config — here's a reproducible A/B comparison showing it improved code-review thoroughness by 14% while keeping cost flat, with a 95% confidence interval that excludes zero."

copilot-eval fills that gap — it's the **integration test** for your AI development environment, not a unit test for individual skills.

## What This Is

- **Environment-as-variant.** You don't just swap prompts — you swap entire Copilot environments (different plugins/instructions/skills installed in isolated, reproducible environments). This is the core abstraction other eval tools lack.
- **Telemetry-native.** OTel traces are first-class scored signals. Token counts, tool-call sequences, cost, and duration are metrics you can gate on — not just judge scores.
- **Statistically honest.** Bootstrap confidence intervals, paired-epoch deltas, sample-size warnings, survivorship-bias guards, judge self-consistency via multi-sampling. The tool tells you when it doesn't have enough data to conclude.
- **Zero-infra by default.** No external services required. Running an eval just works — one command, no setup.
- **CI-ready.** Designed to run as a regression gate in CI: detect when a config change degrades quality or blows up cost, and block the merge.

## What This Is NOT

- **Not a generic LLM eval framework.** We don't evaluate arbitrary prompts or models in isolation. We evaluate Copilot running inside configured environments. Prompt-level evals without an environment are already well served by existing tools.
- **Not a hosted platform or dashboard.** No SaaS, no database, no web UI. This is a CLI tool that produces reports. It stays in your terminal and CI.
- **Not a cost-management / FinOps platform.** Cost data is valuable as a standardized metric for comparison and gating, but we don't do billing analytics, chargeback, or spend forecasting.
- **Not multi-agent-framework agnostic (yet).** The primary target is GitHub Copilot. An extensibility point exists for future agent runtimes, but broadening beyond Copilot is not an active goal until Copilot coverage is comprehensive.
- **Not a replacement for manual testing.** copilot-eval measures aggregate behavior across epochs. It does not debug a single failing run interactively.

## Target Users

**Primary:** Individuals and teams who iteratively improve their AI-first development workflows by crafting Copilot primitives — custom instructions, skills, hooks, MCP server configurations, and agent definitions — and need evidence that changes work before rolling them out.

**Secondary:** Skill creators and tool authors who publish reusable Copilot customizations and want to ship proof (eval results) alongside their artifacts.

**Not the primary focus today:** Multi-tenant governance dashboards for large platform teams, or pure academic LLM benchmarking — needs we may revisit as the tool matures.

## Positioning

For developers and teams who tune Copilot workflows with custom primitives, copilot-eval is an environment-isolated A/B regression harness that proves whether a change helped — with OTel telemetry, statistical rigor, and zero infrastructure. Unlike generic LLM eval tools, it evaluates the *whole environment*, not just the prompt.

## Goals

- Serve as the default CI gate for Copilot customization regressions (CI integration, machine-readable output, cross-run baselines).
- Keep the zero-infra, single-command experience as the golden path.
- Make judge evaluation trustworthy enough to act on (calibration harness, self-consistency, deterministic anchors alongside LLM judges).
- Provide canonical eval-set templates so users can start measuring in minutes, not hours.
- Stay lean: minimal moving parts, no runtime services, no vendor lock-in.
