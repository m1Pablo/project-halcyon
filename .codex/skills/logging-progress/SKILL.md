---
name: logging-progress
description: "Standardized logging protocol for Codex workflow agents. Defines personal logs and shared ledger entries."
---

# Logging Progress

All workflow agents use this skill. Read it at session start and follow it throughout.

## Filesystem Layout

All paths are relative to the repo root.

```text
.codex/
  plans/          # Architect writes plan/spec notes here
  ledger/
    LEDGER.md     # Shared high-signal coordination ledger
  logs/
    {agent}-{YYYY-MM-DD}.md
```

Create directories and files on first write if they do not exist. Never delete or overwrite existing entries; append only.

## When To Log

Log at these moments:

- Immediately after claiming a session or subtask.
- After meaningful discoveries or decisions.
- When an artifact is created, modified, or deleted.
- When a blocker appears or is resolved.
- Before handoff to another agent or the user.
- At session completion.

## Personal Log

File: `.codex/logs/{agent-slug}-{YYYY-MM-DD}.md`

Use this structure:

```markdown
## {YYYY-MM-DD HH:MM}

- **Task:** {session name or subtask}
- **Status:** IN PROGRESS | BLOCKED | DONE
- **Findings:** {observed facts; separate inference from fact}
- **Decisions:** {choices made and why}
- **Artifacts:** {files created, modified, or read}
- **Blockers:** none | {description and proposed resolution}
- **Next step:** {immediate next action}
```

Keep entries short.

## Shared Ledger

File: `.codex/ledger/LEDGER.md`

Mirror only coordination facts other agents or the user need. Do not copy full research notes.

Events:

| Event | When |
| --- | --- |
| `CLAIM` | Agent starts a session or subtask |
| `BLOCKER` | A blocker may affect other work |
| `RESOLVED` | A blocker is cleared |
| `READY` | A deliverable is ready for the next agent |
| `DONE` | A session or subtask completes with evidence |

Format:

```markdown
## {YYYY-MM-DD HH:MM} | {AGENT} | {EVENT}

{One to three lines. Who, what, and what it unblocks or requires.}
```

## Avoid

- Logging every file read.
- Keeping durable decisions only in chat.
- Writing blockers only to a personal log.
- Rewriting prior entries.
- Making the ledger too noisy to scan.
