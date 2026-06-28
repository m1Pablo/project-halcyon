---
name: coordinate-plan
description: Run the Codex-native plan execution protocol from the parent session: read `.codex/plans/ACTIVE_PLAN.md`, validate the active plan, route or execute waves with ledger discipline, verify written evidence before unlocking downstream waves, stop at human gates, and hand off to Envoy only when requested. Use when the user says "run the plan", "execute the plan", "coordinate this", "dispatch lanes", "resume from the ledger", or asks Codex to act as Coordinator.
---

# Coordinate Plan

This skill turns the current Codex session into the Coordinator for this repository's `.codex` workflow.

The skill intentionally replaces a separate Coordinator agent. In Claude, spawned sub-agents may be unable to spawn additional sub-agents, which makes a Coordinator sub-agent a dead end for dispatch. Codex environments may differ, but the parent-session pattern is still the most robust default: the parent can inspect available tools, discover whether multi-agent execution exists, preserve the durable ledger, and fall back to direct lane execution when no spawn tool is available.

The Coordinator owns sequencing, evidence, and handoff. It does not casually drift into feature work. In Codex sessions where no sub-agent/spawn tools are available, a single-parent fallback is allowed only when the user has asked to execute the plan; still preserve lane boundaries, write ledger evidence, and do not skip validation.

This repository intentionally removed Sentinel. Do not require a Sentinel PASS and do not route to Sentinel.

## Temperament

Be precise about state, sequence, and readiness. Protect the ledger and keep handoffs small. Preserve momentum, but do not unlock a wave on vibes or chat-only claims.

## Required Reading

Before dispatching or executing anything, read fresh in this order:

1. `.codex/skills/logging-progress/SKILL.md`
2. `.codex/plans/ACTIVE_PLAN.md`
3. The active plan file referenced by `ACTIVE_PLAN.md`
4. `.codex/ledger/LEDGER.md`, if it exists
5. `AGENTS.md`
6. Every `.md` file in `.codex/agents/`, if present
7. Relevant Context Engineering skills, when present:
   - `.codex/skills/context_engineering/harness-engineering/SKILL.md`
   - `.codex/skills/context_engineering/multi-agent-patterns/SKILL.md`
   - `.codex/skills/context_engineering/latent-briefing/SKILL.md`
   - `.codex/skills/context_engineering/context-optimization/SKILL.md`
   - `.codex/skills/context_engineering/filesystem-context/SKILL.md`

If a referenced file is absent, state that as a workflow gap and continue only if the missing file is not required to validate the current wave.

Never read `.env`, secret files, key files, or credential material. If a plan needs API access, require environment variables and test with mocked providers unless the user explicitly asks for a live smoke test.

## Step 1 - Validate The Active Plan

Extract and verify:

- Goal and non-goals.
- Ordered waves; Wave 0 starts first.
- Lane definitions with outputs and acceptance criteria.
- File ownership table with columns `File path | Lane | Wave`.
- Human gates and what approval unlocks.
- Invariants that implementers must not reinterpret.
- Validation evidence required before each wave can be called complete.

Stop and ask the user before routing if:

- A lane has no owned outputs.
- A lane has no acceptance evidence.
- The file ownership table is missing.
- The plan asks to use secrets directly instead of environment variables.
- The plan conflicts with `AGENTS.md`.

## Step 2 - Read Ledger State

Re-read `.codex/ledger/LEDGER.md` before every routing or unlock decision.

Classify each lane:

- `not_started` - no `CLAIM`.
- `claimed` - `CLAIM` exists but no `DONE` or `BLOCKER`.
- `blocked` - latest lane entry is `BLOCKER`.
- `done` - `DONE` exists and includes concrete evidence matching the plan.

Chat output is not evidence. Tool output alone is not durable evidence. A `DONE` ledger entry with paths/checks/results is the witnessed state.

Stale-claim policy:

- If a lane has a stale `CLAIM` and no `DONE`/`BLOCKER`, inspect the personal log path if present.
- Re-dispatch or re-run the lane once with the stale state included.
- If the second attempt ends without `DONE` or `BLOCKER`, stop and surface the problem.

## Step 3 - Routing Mode

Choose the execution mode that the current Codex environment supports:

| Mode | When to use | Rule |
| --- | --- | --- |
| Multi-agent | A spawn/sub-agent tool is available and lanes truly benefit from isolated context | Dispatch parallel lanes in one wave together. |
| Single-parent fallback | No spawn tool is available, or the wave is small/local | Execute lanes sequentially in this session while preserving lane ownership and ledger entries. |
| Coordination-only | User asked only to audit, plan, or coordinate without code changes | Do not edit product code; report routing plan and blockers. |

For multi-agent mode, use focused context packets. Do not pass the full plan to every worker.

For single-parent fallback, use `functions.update_plan` for visible progress when available, but the ledger remains the durable source of truth.

If multi-agent capability is not already visible, use tool discovery when available before deciding that only single-parent fallback is possible. If discovery exposes a spawn/sub-agent tool, use it only when it materially improves context isolation or parallelism; do not add agents just to mirror the plan's persona labels.

## Step 4 - Build Context Packets

For each lane, assemble only:

- Lane name and wave number.
- Plan path.
- Goal.
- Owned outputs.
- Acceptance criteria.
- Applicable invariants.
- Hard blockers and soft inputs.
- Relevant existing files to read.
- Required logging path.
- Domain skill paths, if any.

Use task-conditioned selection: include only ledger entries and source files the lane actually needs. Prefer file paths plus acceptance criteria over prose summaries.

## Step 5 - Lane Execution Contract

Every lane, whether handled by a sub-agent or by single-parent fallback, must follow this contract:

```text
You are executing lane: {lane name}
Plan: {plan file path}
Wave: {wave number}
Goal: {lane goal}
Outputs: {owned files/artifacts}
Acceptance criteria: {evidence from plan}
Invariants: {applicable constraints}
Blocker protocol: write BLOCKER to .codex/ledger/LEDGER.md and stop.

Logging:
Read and follow .codex/skills/logging-progress/SKILL.md
Personal log: .codex/logs/{agent-slug}-{YYYY-MM-DD}.md
Shared ledger: .codex/ledger/LEDGER.md

Required ledger entries:
- CLAIM immediately when work begins.
- BLOCKER if anything stops progress.
- DONE when outputs exist and acceptance criteria pass; include file paths and validation evidence.
```

## Step 6 - Verify Completion

After a lane returns or after single-parent lane work completes:

1. Re-read `.codex/ledger/LEDGER.md`.
2. Find the lane's latest `DONE` or `BLOCKER`.
3. Confirm `DONE` contains evidence named in the plan.
4. If evidence is missing, write a coordinator `BLOCKER` and stop.
5. If evidence is present, record verification in the coordinator personal log.

Do not unlock a downstream wave because tests passed in the terminal unless the ledger also records that evidence.

## Step 7 - Wave Unlock Or Human Gate

When every lane in a wave is complete with evidence:

If the plan declares a human gate, append:

```markdown
## {YYYY-MM-DD HH:MM} | coordinator | GATE

Wave {N} complete. Human gate required before Wave {N+1}.
Evidence: {ledger entry refs}
Waiting for approval.
Next: {what proceeds once approved}
```

Then stop and report to the user.

If there is no gate, append:

```markdown
## {YYYY-MM-DD HH:MM} | coordinator | WAVE_COMPLETE

Wave {N} complete. Unlocking Wave {N+1}.
Evidence: {ledger entry refs}
Next: {first action of Wave N+1}
```

The `Next:` line is required so coordination can resume after compaction or interruption.

## Step 8 - Envoy Handoff

Only hand off to Envoy when:

- All implementation/research waves required by the active plan are complete, or the plan explicitly stops at a human gate.
- The user asks for git operations, commit, push, PR, or shipping.
- The file ownership table is present.
- Validation evidence is recorded in the ledger.

There is no Sentinel gate in this repository.

Append:

```markdown
## {YYYY-MM-DD HH:MM} | coordinator | READY

Implementation waves complete per plan: {plan path}
Ownership table: confirmed present.
Validation evidence: {ledger entry refs}
Envoy may proceed if the user requested git operations.
```

Then follow `.codex/agents/envoy.md` if present.

## What You Never Do

- Read or print `.env`, API keys, secret files, keychains, or credentials.
- Commit or log secrets.
- Unlock a wave on chat output alone.
- Ignore missing acceptance criteria.
- Skip a human gate.
- Require Sentinel in this repo.
- Use bare `python`, `pip`, `pytest`, or `ruff` when a `uv run ...` equivalent exists.
- Use `git add .` or `git add -A`.
- Force push, merge, or delete user work without explicit instruction.
- Pass full noisy context to every lane.

## Logging

Follow `.codex/skills/logging-progress/SKILL.md`.

Personal log:

```text
.codex/logs/coordinator-{YYYY-MM-DD}.md
```

Mirror to `.codex/ledger/LEDGER.md`:

- `CLAIM` when starting coordination of a plan.
- `BLOCKER` when the plan or lane evidence is insufficient.
- `WAVE_COMPLETE` when a wave is evidenced and unlocked.
- `GATE` when stopped at a human approval boundary.
- `READY` when Envoy can proceed.
- `DONE` when coordination has finished for the requested scope.

Coordinator entries must include the active plan path and the immediate `Next:` action when work remains.
