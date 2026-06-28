Sync all Context Engineering skills from `muratcankoylan/Agent-Skills-for-Context-Engineering` to `.codex/skills/context_engineering/`.

## What this command does

Downloads the latest versions of all files under `skills/` from the upstream GitHub repo's `main` branch. It compares SHA-256 hashes and skips unchanged local files.

## Run

From the repo root:

```bash
uv run scripts/sync_context_engineering_skills.py
```

Dry run:

```bash
uv run scripts/sync_context_engineering_skills.py --dry-run
```

Override destination:

```bash
uv run scripts/sync_context_engineering_skills.py --dest path/to/target
```

## Output

The command prints per-file status:

```text
  add        context-compression/SKILL.md
  update     memory-systems/SKILL.md
  unchanged  tool-design/SKILL.md
```

And a summary:

```text
Done: 2 added, 1 updated, 10 unchanged, 0 error(s)
```

## Notes

- Default destination is `.codex/skills/context_engineering/`.
- Synced third-party skill copies are ignored by git.
- Run through `uv` to match this repo's Codex workflow contract.
- This is the Codex-native replacement for the Aplazo repo's `.claude/commands/sync-context-engineering-skills.md`.
