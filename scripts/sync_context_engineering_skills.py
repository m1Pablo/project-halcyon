#!/usr/bin/env python3
"""Sync Context Engineering skills into this repo's .codex directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_OWNER = "muratcankoylan"
REPO_NAME = "Agent-Skills-for-Context-Engineering"
BRANCH = "main"
SKILLS_PREFIX = "skills/"

GITHUB_API_TREE_URL = (
    f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/git/trees/{BRANCH}?recursive=1"
)
RAW_BASE_URL = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}"
DEFAULT_DEST = Path(__file__).resolve().parent.parent / ".codex" / "skills" / "context_engineering"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _request_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "project-halcyon-codex-skill-sync",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _fetch_tree() -> list[dict[str, Any]]:
    payload = json.loads(_request_bytes(GITHUB_API_TREE_URL).decode("utf-8"))
    tree = payload.get("tree", [])
    return [
        entry
        for entry in tree
        if entry.get("type") == "blob"
        and entry.get("path", "").startswith(SKILLS_PREFIX)
        and len(entry["path"]) > len(SKILLS_PREFIX)
    ]


def _download_file(remote_path: str) -> bytes:
    return _request_bytes(f"{RAW_BASE_URL}/{remote_path}")


def sync_skills(dest: Path, *, dry_run: bool = False) -> None:
    added = 0
    updated = 0
    unchanged = 0
    errors = 0

    print(f"Fetching file tree from {REPO_OWNER}/{REPO_NAME}@{BRANCH} ...")
    entries = _fetch_tree()
    print(f"Found {len(entries)} file(s) under {SKILLS_PREFIX}\n")

    for entry in entries:
        relative = entry["path"][len(SKILLS_PREFIX) :]
        local_path = dest / relative

        try:
            content = _download_file(entry["path"])
        except urllib.error.HTTPError as exc:
            print(f"  ERROR      {relative} ({exc.code})")
            errors += 1
            continue
        except urllib.error.URLError as exc:
            print(f"  ERROR      {relative} ({exc.reason})")
            errors += 1
            continue

        if local_path.exists():
            existing_hash = _sha256(local_path.read_bytes())
            new_hash = _sha256(content)
            if existing_hash == new_hash:
                print(f"  unchanged  {relative}")
                unchanged += 1
                continue
            label = "update"
            updated += 1
        else:
            label = "add"
            added += 1

        if dry_run:
            print(f"  {label:<9} {relative} (dry-run)")
        else:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(content)
            print(f"  {label:<9} {relative}")

    print(f"\nDone: {added} added, {updated} updated, {unchanged} unchanged, {errors} error(s)")
    if dry_run:
        print("(dry-run mode: no files were written)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Sync Context Engineering skills from "
            "muratcankoylan/Agent-Skills-for-Context-Engineering."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files.",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help=f"Destination directory (default: {DEFAULT_DEST}).",
    )
    args = parser.parse_args()
    sync_skills(args.dest, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
