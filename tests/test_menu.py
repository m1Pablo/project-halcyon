from pathlib import Path

import pytest

from halcyon.menu import load_menu, load_menu_from_xml
from halcyon.slash_commands import iter_slash_leaf_commands


def test_load_menu_exposes_initial_command_surface() -> None:
    menu_def = load_menu()

    commands = {command: payload["tool"] for command, payload in iter_slash_leaf_commands(menu_def)}

    assert commands == {
        "/synth state": "get_synth_state",
        "/synth params": "list_synth_params",
        "/synth set": "set_synth_param",
        "/synth patch": "apply_synth_patch",
        "/preset apply": "apply_synth_preset",
        "/preset save": "save_synth_preset",
        "/panic": "all_notes_off",
    }
    assert menu_def["Synth"]["shortcut"] == "synth"
    assert menu_def["__slash_commands__"]["items"][0]["shortcut"] == "panic"


def test_mcp_attrs_are_loaded_from_xml() -> None:
    menu_def = load_menu()
    payloads = dict(iter_slash_leaf_commands(menu_def))

    assert payloads["/synth set"]["mcp_concurrency_group"] == "synth_patch"
    assert payloads["/synth set"]["mcp_poll_after_s"] == 1
    assert payloads["/panic"]["mcp_single_flight"] is False
    assert payloads["/panic"]["mcp_parallel_ok"] is True


def test_leaf_without_tool_is_rejected(tmp_path: Path) -> None:
    menu_path = tmp_path / "bad-menu.xml"
    menu_path.write_text(
        """
        <menu>
            <option name="Synth" shortcut="synth">
                <suboption name="State" shortcut="state" />
            </option>
        </menu>
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires tool"):
        load_menu_from_xml(menu_path)


def test_root_must_be_menu(tmp_path: Path) -> None:
    menu_path = tmp_path / "bad-menu.xml"
    menu_path.write_text("<commands />", encoding="utf-8")

    with pytest.raises(ValueError, match="Root element"):
        load_menu_from_xml(menu_path)
