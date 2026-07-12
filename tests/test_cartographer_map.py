from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / ".codex/skills/cartographer-map/scripts/build_vault.py"
SPEC = importlib.util.spec_from_file_location("build_vault", SCRIPT)
assert SPEC and SPEC.loader
build_vault = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_vault
SPEC.loader.exec_module(build_vault)


def make_territory(tmp_path: Path, log_body: str, *, two_plans: bool = False) -> object:
    codex = tmp_path / ".codex"
    (codex / "plans").mkdir(parents=True)
    (codex / "logs").mkdir()
    (codex / "agents").mkdir()
    plan = codex / "plans/2026-07-09-performance-cockpit.md"
    plan.write_text("# Performance cockpit\n", encoding="utf-8")
    if two_plans:
        (codex / "plans/2026-07-09-other-campaign.md").write_text(
            "# Other campaign\n", encoding="utf-8"
        )
    (codex / "plans/ACTIVE_PLAN.md").write_text(
        "current: .codex/plans/2026-07-09-performance-cockpit.md\n",
        encoding="utf-8",
    )
    (codex / "logs/worker-2026-07-09.md").write_text(log_body, encoding="utf-8")
    (codex / "agents/worker.toml").write_text(
        'name = "worker"\ndescription = "Test worker"\ndeveloper_instructions = "Run tests."\n',
        encoding="utf-8",
    )
    return build_vault.read_territory(tmp_path)


def test_explicit_codex_plan_path_is_resolved(tmp_path: Path) -> None:
    territory = make_territory(
        tmp_path,
        "## 2026-07-09 10:00 | CLAIM\n\n"
        "- **Plan:** `.codex/plans/2026-07-09-performance-cockpit.md`\n",
    )

    entry = territory.entries[0]
    assert entry.evidence == "explicit"
    assert entry.plan.stem == "2026-07-09-performance-cockpit"
    assert entry.plan.active is True


def test_toml_agent_roster_uses_declared_name_and_description(tmp_path: Path) -> None:
    territory = make_territory(tmp_path, "")

    assert territory.roster == {"worker": "Test worker"}


def test_plan_evidence_is_inherited_until_changed(tmp_path: Path) -> None:
    territory = make_territory(
        tmp_path,
        "## 2026-07-09 10:00 | CLAIM\n\n"
        "- **Plan:** `.codex/plans/2026-07-09-performance-cockpit.md`\n\n"
        "## 2026-07-09 10:05 | NOTE\n\nProgress continues.\n",
    )

    assert [entry.evidence for entry in territory.entries] == ["explicit", "inherited"]
    assert territory.entries[1].plan is territory.entries[0].plan


def test_deliberate_none_breaks_inheritance(tmp_path: Path) -> None:
    territory = make_territory(
        tmp_path,
        "## 2026-07-09 10:00 | CLAIM\n\n"
        "- **Plan:** `.codex/plans/2026-07-09-performance-cockpit.md`\n\n"
        "## 2026-07-09 10:05 | NOTE\n\n- **Plan:** none (user hotfix)\n\n"
        "## 2026-07-09 10:10 | DONE\n\nComplete.\n",
    )

    assert [entry.evidence for entry in territory.entries] == [
        "explicit",
        "deliberate-none",
        None,
    ]
    assert territory.entries[2].plan is None


def test_ambiguous_plan_evidence_is_not_guessed(tmp_path: Path) -> None:
    territory = make_territory(
        tmp_path,
        "## 2026-07-09 10:00 | CLAIM\n\n- **Plan:** current redesign plan\n",
        two_plans=True,
    )

    entry = territory.entries[0]
    assert entry.evidence == "ambiguous"
    assert entry.plan is None
    assert "ambiguous plan evidence" in territory.warnings[0]


def test_build_preserves_user_notes_outside_generated_folders(tmp_path: Path) -> None:
    territory = make_territory(
        tmp_path,
        "## 2026-07-09 10:00 | CLAIM\n\n"
        "- **Plan:** `.codex/plans/2026-07-09-performance-cockpit.md`\n",
    )
    atlas = tmp_path / "Atlas"
    atlas.mkdir()
    note = atlas / "My Notes.md"
    note.write_text("keep me", encoding="utf-8")

    build_vault.build_vault(territory, atlas)

    assert note.read_text(encoding="utf-8") == "keep me"
    assert (atlas / "Atlas.md").is_file()
    assert list((atlas / "Entries").glob("*.md"))
