from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]
AGENTS_DIR = ROOT / ".codex/agents"
REQUIRED_FIELDS = {"name", "description", "developer_instructions"}
EXPECTED_AGENTS = {"architect", "cartographer", "envoy", "shepherd"}


def test_custom_agents_use_native_toml_schema() -> None:
    files = sorted(AGENTS_DIR.glob("*.toml"))
    definitions = [tomllib.loads(path.read_text(encoding="utf-8")) for path in files]

    assert {definition["name"] for definition in definitions} == EXPECTED_AGENTS
    for path, definition in zip(files, definitions, strict=True):
        assert REQUIRED_FIELDS <= definition.keys(), path
        assert all(
            isinstance(definition[field], str) and definition[field].strip()
            for field in REQUIRED_FIELDS
        ), path


def test_legacy_markdown_agent_manifests_are_removed() -> None:
    assert not list(AGENTS_DIR.glob("*.md"))


def test_cartographer_support_skill_exists() -> None:
    assert (ROOT / ".codex/skills/cartographer-map/SKILL.md").is_file()
    assert (ROOT / ".codex/skills/cartographer-map/scripts/build_vault.py").is_file()
