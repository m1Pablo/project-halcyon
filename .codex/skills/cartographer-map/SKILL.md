---
name: cartographer-map
description: Mechanically instantiate or refresh the derived Campaign Atlas from `.codex` personal logs, plans, active-plan pointer, and agent roster; use for campaign-history maps, evidence tracing, dry-run territory audits, and Obsidian vault refreshes without changing source territory.
---

# Cartographer Map

The Campaign Atlas is a regenerable Obsidian map, never workflow authority. Personal logs are the source of truth and the coordinator log is the spine.

Generated surfaces:

| Path | Unit |
| --- | --- |
| `Atlas/Entries/` | one typed personal-log entry |
| `Atlas/Logs/` | one agent-day log |
| `Atlas/Agents/` | one roster or observed lane |
| `Atlas/Plans/` | one plan note |
| `Atlas/Campaigns/` | one plan-series campaign |
| `Atlas/Atlas.md` | counts, legend, and unmapped territory |

Entries link to their log, agent, evidenced plan/campaign, and adjacent entries. `plan-evidence` is `explicit`, `inherited`, `deliberate-none`, or `ambiguous`. A full `Plan:` path establishes sticky evidence; `Plan: none ({reason})` deliberately breaks it. Ambiguity stays unmapped and produces a warning.

Run from the repository root:

```bash
uv run .codex/skills/cartographer-map/scripts/build_vault.py --dry-run --root .
uv run .codex/skills/cartographer-map/scripts/build_vault.py --root .
```

Options: `--dest PATH`, `--campaign SLUG`, and `--dry-run`. Dry-run writes nothing. Real builds update only `Atlas.md` and generated folders in place, remove stale generated notes, preserve human-authored notes outside generated folders, and preserve user Obsidian settings.

The script is standard-library-only and mechanically read-only over `.codex/plans/`, `.codex/logs/`, `.codex/agents/`, and `.codex/plans/ACTIVE_PLAN.md`. Never hand-edit generated notes or treat a mapped event as authorization.
