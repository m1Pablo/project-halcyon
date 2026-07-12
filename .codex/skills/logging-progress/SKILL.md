---
name: logging-progress
description: "Use for durable Codex workflow progress: append-only, single-writer personal logs with typed CLAIM, DONE, BLOCKER, GATE, READY, and WAVE_COMPLETE evidence; the coordinator log is the coordination spine and the old shared ledger is read-only history."
---

# Logging Progress

Read this skill at session start. Each participant writes only to its own dated log:

```text
.codex/logs/{agent-slug}-{YYYY-MM-DD}.md
```

Logs are append-only and single-writer. Never edit, reorder, or delete prior entries. Never write to another participant's log. Correct mistakes with a new entry.

`.codex/ledger/LEDGER.md` is historical read-only: never modify it and never require it for current state.

## Reading discipline

Read your own log for today and only the exact entries named by a handoff (`{agent}-{YYYY-MM-DD}.md @ HH:MM`). Do not sweep unrelated logs. The coordinator log is the durable coordination spine and points to lane evidence needed for resumption.

## Entry format

```markdown
## {YYYY-MM-DD HH:MM} | {EVENT}

- **Task:** {lane or task}
- **Plan:** {.codex/plans/{date}-{slug}.md | none ({reason})}
- **Findings:** {observed facts; label inference}
- **Decisions:** {choice and reason}
- **Artifacts:** {exact paths, hashes, or checks}
- **Blockers:** none | {failure, output, attempted remedy}
- **Next:** {immediate next action}
```

Keep entries compact. `Plan:` is mandatory on `CLAIM`; later entries inherit it until another explicit plan or `Plan: none ({reason})`. A deliberate none stops inheritance. Shorthand such as “Wave 2” is valid only after the full plan path appears in the same log.

## Typed events

| Event | Meaning | Writer |
| --- | --- | --- |
| `CLAIM` | work begins | any participant |
| `NOTE` | meaningful progress or decision | any participant |
| `BLOCKER` / `RESOLVED` | work stops / blocker clears | any participant |
| `DONE` | owned work satisfies acceptance evidence | any participant |
| `GATE` | human or prerequisite boundary | coordinator or responsible role |
| `WAVE_COMPLETE` | all lanes evidenced; next wave unlocks | coordinator only |
| `READY` | validated handoff is ready | coordinator, Shepherd, or Envoy |

`DONE` must name concrete evidence matching the plan. `BLOCKER` must preserve the failing command, relevant output, and attempts. A returned lane with only `CLAIM` is stale: the coordinator may redispatch once with that failure context, then escalates.

Entries that pause or end work (`DONE`, `BLOCKER`, `GATE`, `WAVE_COMPLETE`) require `Next:`. End the final chat message with the exact log path and DONE/BLOCKER timestamp so the coordinator can verify without searching.

Never duplicate an entry elsewhere, keep decisions only in chat, log every file read, or end a working period without `DONE` or `BLOCKER`.
