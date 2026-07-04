---
applyTo: "**"
---

# Product Context

copilot-eval is an environment-isolated A/B evaluation framework for GitHub Copilot customizations. It measures whether a set of primitives (skills, custom agents, instructions, hooks, MCP servers) actually improves outcomes — with OTel telemetry, statistical rigor, and zero infrastructure.

See `docs/vision.md` for the full product vision.

## Target user

Individuals and teams who iteratively improve AI-first development workflows by crafting Copilot primitives and need evidence that changes work.

## Non-Goals — DO NOT implement

- NEVER build a generic LLM eval framework (prompt-in/text-out without an environment). That space is already well-served by existing tools.
- NEVER add a hosted platform, web UI, database backend, or SaaS layer.
- NEVER turn cost tracking into a FinOps/billing platform — cost data is valuable as a standardized metric for comparison and gating, but we don't do billing analytics, chargeback, or spend forecasting.
- NEVER add support for non-Copilot agent runtimes until Copilot coverage is comprehensive.
- NEVER add a mode that sacrifices environment isolation for convenience.

## Principles — ALWAYS

- ALWAYS keep a zero-dependency default path that requires no external services.
- ALWAYS surface statistical uncertainty (CI, sample size, variance) in reports — never present a clean number without its confidence context.
- ALWAYS pair LLM-as-judge evaluators with deterministic anchors (regex/contains/script) where possible to prevent judge drift from going unnoticed.
- ALWAYS preserve the environment-as-variant model (isolated, reproducible execution environments per variant) as the core abstraction — this is the project's differentiator from prompt-eval tools.
- ALWAYS design for CI-first consumption (machine-readable output, exit codes, broad CI compatibility).
