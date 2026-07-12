---
name: coordinate-plan
description: "Run Codex-native plan execution from the parent session: validate `.codex/plans/ACTIVE_PLAN.md`, route focused lanes in dependency waves, verify personal-log evidence before unlocks, stop at human gates, resume from the coordinator spine, and hand off to Envoy only when authorized."
---

# Coordinate Plan

The parent Codex session is Coordinator. It owns sequence, evidence, gates, and handoffs—not feature implementation or git state. `.codex/logs/coordinator-{YYYY-MM-DD}.md` is the coordination spine. Each lane’s personal log is its single-writer authority; `.codex/ledger/LEDGER.md` is historical read-only.

## Read fresh

1. `.codex/skills/logging-progress/SKILL.md`
2. `.codex/plans/ACTIVE_PLAN.md` and the full referenced plan
3. Coordinator logs for this plan, then only the lane entries they cite
4. `AGENTS.md` and every `.toml` in `.codex/agents/`
5. Relevant context-engineering skills: harness engineering, multi-agent patterns, latent briefing, context optimization, and filesystem context

Never read secrets. Require environment variables and mocked providers unless the user explicitly authorizes a live smoke test.

## Validate before routing

Extract goal/non-goals, ordered waves, lane outputs and acceptance evidence, `File path | Lane | Wave`, human gates, invariants, and validation requirements. Stop if ownership/evidence is missing, secrets are embedded, or the plan conflicts with `AGENTS.md`.

Classify a lane from its latest exact personal-log entry: `not_started`, `claimed`, `blocked`, or `done`. Chat and terminal output are not durable evidence. A stale `CLAIM` gets one redispatch with prior failure context; a second return without `DONE` or `BLOCKER` is escalated.

## Route with current capabilities

Discover the collaboration capabilities available in the current Codex environment. Use isolated agents only when parallel lanes materially benefit from separate context; otherwise execute a small lane in the parent session if the user asked for plan execution. Never hardcode an unavailable tool syntax or create persona-shaped agents without useful isolation.

Build a focused packet per lane:

```text
You are executing lane: {lane}
Plan: {path}
Wave: {number}
Goal: {goal}
Outputs: {owned artifacts}
Acceptance criteria: {evidence}
Invariants: {applicable constraints}
Blocker protocol: append BLOCKER to your personal log and stop.

Logging:
Read .codex/skills/logging-progress/SKILL.md
Personal log: .codex/logs/{slug}-{YYYY-MM-DD}.md
This is the only log you write.
Write CLAIM immediately, DONE with exact evidence, or BLOCKER with failure details.
End your final message with log path and DONE/BLOCKER timestamp.
```

Add only hard blockers, soft inputs, relevant file paths, domain skills, and exact upstream evidence the consumer needs. Dispatch genuinely parallel lanes together.

## Verify and unlock

After every lane return, read its named log entry and confirm the plan’s evidence. Record the verification in the coordinator log with `lane-log.md @ HH:MM`. Missing evidence is a coordinator `BLOCKER`, not completion.

When a wave completes, append one of:

```markdown
## {timestamp} | WAVE_COMPLETE

Wave {N} complete. Unlocking Wave {N+1}.
Evidence: {lane log pointers}
Next: {first action}
```

```markdown
## {timestamp} | GATE

Wave {N} complete. Human gate required before Wave {N+1}.
Evidence: {lane log pointers}
Waiting for approval.
Next: {action after approval}
```

Stop at every human gate. The `Next:` line is required for compaction-safe resumption.

## Envoy handoff

Hand off only when required waves are evidenced (or the plan explicitly ends at a gate), ownership exists, and the user requested git/publish operations. Append `READY` with plan path, evidence pointers, ownership confirmation, and requested shipping scope, then use the `envoy` custom agent defined in `.codex/agents/envoy.toml`.

Never implement casually, edit acceptance criteria, unlock on chat claims, write another participant’s log, modify the historical ledger, skip a gate, manage git outside Envoy, or pass full noisy context to every lane.
