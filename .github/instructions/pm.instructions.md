---
applyTo: "**"
---

# Project Management & Issue Tracking

Guidelines for creating, organizing, and prioritizing issues in this repository.

## Issue Hierarchy

Use a two-level hierarchy: **Epics** (tracking issues) contain **sub-issues** (actionable work items).

### Epics

Epics are high-level tracking issues that group related work. They:
- Use the `epic` label
- Have a title prefixed with `Epic:`
- Contain a task list (`- [ ] ...`) linking to sub-issues
- Document motivation, success criteria, and dependency relationships
- Are NOT directly implementable — they track progress across sub-issues

### Sub-issues (actionable work)

Each sub-issue is a single implementable unit. Requirements:
- Title uses **conventional commit style**: `type(scope): description`
  - Types: `feat`, `fix`, `refactor`, `test`, `docs`, `dx`, `ci`, `perf`
  - Scope: the module or area affected (e.g., `cli`, `runner`, `judge`, `config`, `report`, `fixtures`)
- Body includes: Problem, Proposal, Acceptance Criteria, References
- References its parent Epic (e.g., "Part of Epic: ... (#N)")
- References related/blocking issues (e.g., "Unblocks: #66")

## Labels

### Priority (mutually exclusive — every issue gets exactly one)

| Label | Meaning | SLA guidance |
|-------|---------|--------------|
| `priority: critical` | Blocks other development or causes severe user harm | Address immediately |
| `priority: high` | Significant value for target users | Address next |
| `priority: medium` | Meaningful improvement, not blocking | Address soon |
| `priority: low` | Nice to have, no urgency | When bandwidth allows |

### Type (one per issue)

| Label | When to use |
|-------|-------------|
| `enhancement` | New feature or capability |
| `bug` | Something broken |
| `refactor` | Internal improvement, no user-visible change |
| `dx` | Developer experience improvement (tooling, errors, feedback) |
| `tests` | Test coverage or infrastructure |
| `documentation` | Docs additions or corrections |
| `performance` | Speed or resource optimization |
| `security` | Security hardening |

### Meta labels

| Label | When to use |
|-------|-------------|
| `epic` | Tracking issue for a group of related work |
| `good first issue` | Self-contained, well-specified, minimal context needed |

## Issue Body Template

```markdown
## Problem

[What's wrong or missing — concrete user pain, not abstract]

## Proposal

[What to do — specific enough to implement without further design]

## Acceptance Criteria

- [ ] [Testable condition 1]
- [ ] [Testable condition 2]
- [ ] [Testable condition 3]

## References

Part of Epic: [Epic title] (#N)
Unblocks: #X, #Y
Related: #Z
```

## Prioritization Criteria

When assigning priority, consider:

1. **Trust impact** — Does it affect the correctness of A/B conclusions? (→ critical/high)
2. **Blocking factor** — Does it unblock other high-value work? (→ critical/high)
3. **User pain frequency** — How often do users hit this? (→ high/medium)
4. **Effort-to-impact ratio** — Small effort, large impact? (→ bump up one tier)
5. **Alignment with vision** — Does it reinforce the project's differentiators? (→ bump up)

## Dependency Tracking

- Use "Unblocks: #N" and "Blocked by: #N" in issue bodies
- For hard dependencies, use GitHub's task list in the Epic to show ordering
- Prefer dependency-free issues for parallel development

## Closing Issues

When closing an issue via PR:
- Reference the issue in the PR body or commit message (`Closes #N`, `Fixes #N`)
- Update the Epic's task list checkbox
- If the fix is partial, leave the issue open and document remaining work
