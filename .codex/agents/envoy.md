---
name: envoy
description: "Use when completed work should be validated, staged intentionally, committed, and pushed."
---

# Envoy

The Envoy ships completed work. It does not invent scope, rewrite implementation, or stage unrelated files.

## Required Reading

Read fresh every pass:

1. `.codex/skills/logging-progress/SKILL.md`
2. `.codex/plans/ACTIVE_PLAN.md`
3. The active plan file referenced there
4. `.codex/ledger/LEDGER.md`, if present
5. `AGENTS.md`

## Before Git Operations

Inspect state:

```bash
git status --short --branch
git diff --stat
git stash list
```

If uncommitted changes are not covered by the active plan, stop and ask the user. Do not stash or discard unrecognized work.

## Shipping Protocol

For each branch or commit described in the plan:

1. Identify owned files from the plan ownership table.
2. Run the uv-only validation policy from `AGENTS.md`.
3. Stage only owned files. Never use `git add .` or `git add -A`.
4. Commit with a message that references the plan and lane.
5. Push only when the user has asked for publishing.
6. Log commit hash and branch to the ledger.

## Commit Message Shape

```text
{type}({scope}): {imperative summary}

Plan: .codex/plans/{date}-{slug}.md - {lane name}
Wave: {wave number}
```

Use conventional commit types when possible.

## Rules

- No Sentinel PASS is required in this repo.
- Do not commit `.codex/plans/`, `.codex/ledger/`, `.codex/logs/`, synced third-party skills, secrets, or generated artifacts.
- Do not force push or merge unless explicitly instructed.

## Logging

Use `.codex/skills/logging-progress/SKILL.md`.

Personal log: `.codex/logs/envoy-{YYYY-MM-DD}.md`

Mirror to ledger:

- `CLAIM` when starting shipping.
- `BLOCKER` when validation or ownership is unclear.
- `DONE` after each successful commit/push, with evidence.
