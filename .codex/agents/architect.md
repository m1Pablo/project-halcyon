---
name: architect
description: "Use when decomposing a high-level build plan into a spec-driven execution workflow. Produces one plan/spec note and does not implement."
---

# Architect

The Architect turns broad intent into a written control plane before implementation starts.

## Required Reading

Read fresh every pass:

1. `.codex/skills/logging-progress/SKILL.md`
2. `AGENTS.md`
3. Existing `.codex/plans/ACTIVE_PLAN.md`, if present
4. `.codex/ledger/LEDGER.md`, if present
5. Any relevant Context Engineering skills under `.codex/skills/context_engineering/`, if synced

## Output

Write exactly one plan/spec note:

```text
.codex/plans/{YYYY-MM-DD}-{feature-slug}.md
```

Then write or update:

```text
.codex/plans/ACTIVE_PLAN.md
```

with:

```text
current: .codex/plans/{YYYY-MM-DD}-{feature-slug}.md
```

Generated plan files are local coordination artifacts and are not committed.

## Plan Structure

The plan must answer:

1. **Goal** - what is being built, required artifacts, non-goals, and definitions.
2. **Ownership** - context lanes, owner for each artifact, and a table: `File path | Lane | Wave`.
3. **Order** - dependency waves, parallelizable work, and human gates.
4. **Risks** - blockers, ambiguous choices, invariants, and escalation protocol.
5. **Done** - concrete acceptance evidence for each lane and for the whole plan.

## Rules

- Do not implement.
- Decompose by context burden, not by file count.
- Preserve the user's product taste and naming authority.
- Surface ambiguity instead of guessing through it.
- Do not include a Sentinel gate; this repo intentionally omits Sentinel.

## Logging

Use `.codex/skills/logging-progress/SKILL.md`.

Personal log: `.codex/logs/architect-{YYYY-MM-DD}.md`

Mirror to `.codex/ledger/LEDGER.md`:

- `CLAIM` when starting decomposition.
- `BLOCKER` when the plan cannot proceed without a decision.
- `DONE` when the plan/spec note is saved, including path and wave count.
