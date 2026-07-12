#!/usr/bin/env python3
"""Build a derived Campaign Atlas from the repository's .codex territory."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HEADER_RE = re.compile(
    r"^##\s+(?P<stamp>\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?)"
    r"(?:\s*\|\s*(?P<tail>.*))?$"
)
LOG_RE = re.compile(r"^(?P<agent>.+)-(?P<date>\d{4}-\d{2}-\d{2})\.md$")
PLAN_FIELD_RE = re.compile(r"\*{0,2}Plan:\*{0,2}\s*`?([^`\n]+)", re.IGNORECASE)
PLAN_PATH_RE = re.compile(r"\.codex/plans/[A-Za-z0-9._-]+\.md")
GENERATED_FOLDERS = ("Entries", "Logs", "Agents", "Plans", "Campaigns")


def safe_name(value: str) -> str:
    return re.sub(r'[\[\]#^|\\/:*?"<>]', "-", value).strip().rstrip(".")


def campaign_slug(stem: str) -> str:
    slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)
    while re.search(r"-(?:v\d+|source|audited)$", slug, re.IGNORECASE):
        slug = re.sub(r"-(?:v\d+|source|audited)$", "", slug, flags=re.IGNORECASE)
    return slug


@dataclass
class Plan:
    path: Path
    active: bool = False

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def campaign(self) -> str:
        return campaign_slug(self.stem)


@dataclass
class Entry:
    agent: str
    log_name: str
    stamp: str
    event: str
    body: str
    sequence: int
    plan: Plan | None = None
    evidence: str | None = None
    warning: str | None = None

    @property
    def note(self) -> str:
        compact = self.stamp.replace(":", "").replace(" ", "-")
        return safe_name(f"{self.agent}-{compact}-{self.event}-{self.sequence + 1}")


@dataclass
class DailyLog:
    path: Path
    agent: str
    date: str
    entries: list[Entry] = field(default_factory=list)


@dataclass
class Territory:
    plans: list[Plan]
    logs: list[DailyLog]
    roster: dict[str, str]
    warnings: list[str]

    @property
    def entries(self) -> list[Entry]:
        return [entry for log in self.logs for entry in log.entries]

    @property
    def campaigns(self) -> set[str]:
        return {plan.campaign for plan in self.plans}


def parse_log(path: Path, warnings: list[str]) -> DailyLog | None:
    match = LOG_RE.match(path.name)
    if not match:
        warnings.append(f"{path.name}: invalid log filename; skipped")
        return None
    daily = DailyLog(path, match.group("agent"), match.group("date"))
    current: tuple[str, str, list[str]] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        header = HEADER_RE.match(line)
        if header:
            if current:
                stamp, event, body = current
                daily.entries.append(
                    Entry(
                        daily.agent,
                        path.name,
                        stamp,
                        event,
                        "\n".join(body).strip(),
                        len(daily.entries),
                    )
                )
            tail = (header.group("tail") or "NOTE").strip()
            tokens = [token.strip() for token in tail.split("|") if token.strip()]
            event = tokens[-1].upper() if tokens else "NOTE"
            if not re.fullmatch(r"[A-Z][A-Z_]*", event):
                event = "NOTE"
            current = (header.group("stamp"), event, [])
        elif current:
            current[2].append(line)
    if current:
        stamp, event, body = current
        daily.entries.append(
            Entry(daily.agent, path.name, stamp, event, "\n".join(body).strip(), len(daily.entries))
        )
    if not daily.entries and path.read_text(encoding="utf-8", errors="replace").strip():
        warnings.append(f"{path.name}: no dated entry headers")
    return daily


def resolve_evidence(log: DailyLog, plans: list[Plan], warnings: list[str]) -> None:
    by_stem = {plan.stem: plan for plan in plans}
    current: Plan | None = None
    for entry in log.entries:
        field_match = PLAN_FIELD_RE.search(entry.body)
        field = field_match.group(1).strip() if field_match else ""
        if re.match(r"none\b", field, re.IGNORECASE):
            entry.evidence = "deliberate-none"
            current = None
            continue

        paths = PLAN_PATH_RE.findall(entry.body)
        stems = {Path(path).stem for path in paths}
        candidates = [by_stem[stem] for stem in stems if stem in by_stem]
        if not candidates:
            candidates = [plan for plan in plans if plan.stem in entry.body]
        candidates = list({candidate.stem: candidate for candidate in candidates}.values())

        if len(candidates) == 1:
            entry.plan = candidates[0]
            entry.evidence = "explicit"
            current = candidates[0]
        elif len(candidates) > 1 or (
            field and not paths and not re.match(r"none\b", field, re.IGNORECASE)
        ):
            entry.evidence = "ambiguous"
            entry.warning = f"{entry.log_name} @ {entry.stamp}: ambiguous plan evidence"
            warnings.append(entry.warning)
            current = None
        elif current:
            entry.plan = current
            entry.evidence = "inherited"


def read_roster(directory: Path) -> dict[str, str]:
    roster: dict[str, str] = {}
    for path in sorted(directory.glob("*.toml")) if directory.is_dir() else []:
        with path.open("rb") as file:
            agent = tomllib.load(file)
        name = agent.get("name", path.stem)
        roster[str(name)] = str(agent.get("description", ""))
    return roster


def read_territory(root: Path) -> Territory:
    codex = root / ".codex"
    if not codex.is_dir():
        raise ValueError(f"{codex} not found")
    warnings: list[str] = []
    plan_dir = codex / "plans"
    plans = [Plan(path) for path in sorted(plan_dir.glob("*.md")) if path.name != "ACTIVE_PLAN.md"]
    active = plan_dir / "ACTIVE_PLAN.md"
    if active.is_file():
        active_stems = {
            Path(value).stem
            for value in re.findall(
                r"^current:\s*(\S+)", active.read_text(encoding="utf-8"), re.MULTILINE
            )
        }
        for plan in plans:
            plan.active = plan.stem in active_stems
    logs = []
    for path in sorted((codex / "logs").glob("*.md")) if (codex / "logs").is_dir() else []:
        parsed = parse_log(path, warnings)
        if parsed:
            resolve_evidence(parsed, plans, warnings)
            logs.append(parsed)
    return Territory(plans, logs, read_roster(codex / "agents"), warnings)


def frontmatter(**fields: object) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        escaped = str(value).replace('"', '\\"')
        lines.append(f'{key}: "{escaped}"')
    return "\n".join([*lines, "---", ""])


def entry_note(entry: Entry, previous: Entry | None, following: Entry | None) -> str:
    lines = [
        frontmatter(
            type="entry",
            agent=entry.agent,
            event=entry.event,
            timestamp=entry.stamp,
            plan=entry.plan.stem if entry.plan else None,
            plan_evidence=entry.evidence,
        ),
        f"# {entry.stamp} | {entry.event}",
        "",
        entry.body or "*(empty entry)*",
        "",
        "## Connections",
        "",
        f"- Log: [[{Path(entry.log_name).stem}]]",
        f"- Agent: [[{safe_name(entry.agent)}]]",
    ]
    if previous:
        lines.append(f"- Previous: [[{previous.note}]]")
    if following:
        lines.append(f"- Next: [[{following.note}]]")
    if entry.plan:
        lines.extend(
            [
                f"- Plan: [[{entry.plan.stem}]] ({entry.evidence})",
                f"- Campaign: [[{entry.plan.campaign}]]",
            ]
        )
    elif entry.evidence == "deliberate-none":
        lines.append("- Plan: none (deliberate)")
    elif entry.evidence == "ambiguous":
        lines.append("- Plan: ambiguous (unmapped)")
    return "\n".join(lines) + "\n"


def atlas_note(territory: Territory) -> str:
    counts = {
        name: sum(entry.evidence == name for entry in territory.entries)
        for name in ("explicit", "inherited", "deliberate-none", "ambiguous")
    }
    unmapped = [
        entry
        for entry in territory.entries
        if not entry.plan and entry.evidence != "deliberate-none"
    ]
    lines = [
        frontmatter(type="atlas", generated=datetime.now().strftime("%Y-%m-%d %H:%M")),
        "# Campaign Atlas",
        "",
        f"**{len(territory.campaigns)}** campaigns · **{len(territory.plans)}** plans · "
        f"**{len(territory.logs)}** logs · **{len(territory.entries)}** entries · **{len(territory.roster)}** roster agents",
        "",
        "Evidence: " + " · ".join(f"**{counts[key]}** {key}" for key in counts),
        "",
        "## Campaigns",
        "",
    ]
    lines.extend(f"- [[{slug}]]" for slug in sorted(territory.campaigns))
    lines.extend(["", "## Unmapped territory", ""])
    lines.extend(
        f"- [[{entry.note}]] — {entry.evidence or 'no plan evidence'}" for entry in unmapped
    )
    lines.extend(f"- Warning: {warning}" for warning in territory.warnings)
    if not unmapped and not territory.warnings:
        lines.append("- *(none)*")
    return "\n".join(lines) + "\n"


def build_vault(territory: Territory, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for folder in GENERATED_FOLDERS:
        shutil.rmtree(destination / folder, ignore_errors=True)
        (destination / folder).mkdir(parents=True)

    chains: dict[str, list[Entry]] = {}
    for entry in territory.entries:
        chains.setdefault(entry.agent, []).append(entry)
    for chain in chains.values():
        for index, entry in enumerate(chain):
            previous = chain[index - 1] if index else None
            following = chain[index + 1] if index + 1 < len(chain) else None
            (destination / "Entries" / f"{entry.note}.md").write_text(
                entry_note(entry, previous, following), encoding="utf-8"
            )

    for log in territory.logs:
        body = frontmatter(type="daily-log", agent=log.agent, date=log.date)
        body += (
            f"# {log.agent} — {log.date}\n\n"
            + "\n".join(f"- [[{entry.note}]]" for entry in log.entries)
            + "\n"
        )
        (destination / "Logs" / log.path.name).write_text(body, encoding="utf-8")
    for agent in sorted(set(territory.roster) | {log.agent for log in territory.logs}):
        (destination / "Agents" / f"{safe_name(agent)}.md").write_text(
            f"# {agent}\n", encoding="utf-8"
        )
    for plan in territory.plans:
        source = plan.path.read_text(encoding="utf-8", errors="replace")
        prefix = frontmatter(type="plan", campaign=plan.campaign, active=str(plan.active).lower())
        (destination / "Plans" / plan.path.name).write_text(prefix + source, encoding="utf-8")
    for campaign in territory.campaigns:
        plans = [plan for plan in territory.plans if plan.campaign == campaign]
        body = (
            f"# Campaign: {campaign}\n\n" + "\n".join(f"- [[{plan.stem}]]" for plan in plans) + "\n"
        )
        (destination / "Campaigns" / f"{safe_name(campaign)}.md").write_text(body, encoding="utf-8")
    (destination / "Atlas.md").write_text(atlas_note(territory), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--dest")
    parser.add_argument("--campaign")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        territory = read_territory(root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.campaign:
        selected = [plan for plan in territory.plans if plan.campaign == args.campaign]
        if not selected:
            print(f"error: no plans found for campaign '{args.campaign}'", file=sys.stderr)
            return 2
        selected_stems = {plan.stem for plan in selected}
        territory.plans = selected
        territory.logs = [
            DailyLog(
                log.path,
                log.agent,
                log.date,
                [
                    entry
                    for entry in log.entries
                    if entry.plan and entry.plan.stem in selected_stems
                ],
            )
            for log in territory.logs
        ]
        territory.logs = [log for log in territory.logs if log.entries]
    destination = Path(args.dest).resolve() if args.dest else root / "Atlas"
    print(
        f"territory: {len(territory.plans)} plans, {len(territory.logs)} logs, "
        f"{len(territory.entries)} entries, {len(territory.campaigns)} campaigns, "
        f"{len(territory.roster)} roster agents, {len(territory.warnings)} warnings"
    )
    for warning in territory.warnings:
        print(f"  warn: {warning}")
    if args.dry_run:
        print(f"dry-run: would write vault at {destination}")
        return 0
    build_vault(territory, destination)
    print(f"vault written: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
