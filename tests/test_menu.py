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
        "/coach teach": "start_coach_lesson",
        "/coach replay": "replay_coach_lesson",
        "/coach attempt": "start_coach_attempt",
        "/coach feedback": "get_coach_feedback",
        "/coach state": "get_coach_state",
        "/coach stop": "stop_coach_session",
        "/panic": "all_notes_off",
    }
    assert menu_def["Synth"]["shortcut"] == "synth"
    assert menu_def["__slash_commands__"]["items"][0]["shortcut"] == "panic"


def test_menu_descriptions_do_not_advertise_raw_midi_history() -> None:
    rendered = repr(load_menu()).lower()

    assert "recent midi" not in rendered
    assert "midi log" not in rendered


def test_mcp_attrs_are_loaded_from_xml() -> None:
    menu_def = load_menu()
    payloads = dict(iter_slash_leaf_commands(menu_def))

    assert payloads["/synth set"]["mcp_concurrency_group"] == "synth_patch"
    assert payloads["/synth set"]["mcp_poll_after_s"] == 1
    for command in (
        "/coach teach",
        "/coach replay",
        "/coach attempt",
        "/coach feedback",
    ):
        assert payloads[command]["mcp_concurrency_group"] == "coach_session"
    for command in ("/coach state", "/coach stop"):
        assert payloads[command]["mcp_single_flight"] is False
        assert payloads[command]["mcp_parallel_ok"] is True
    assert payloads["/panic"]["mcp_single_flight"] is False
    assert payloads["/panic"]["mcp_parallel_ok"] is True


def test_coach_menu_is_the_exact_public_slash_abi() -> None:
    payloads = dict(iter_slash_leaf_commands(load_menu()))

    coach = {
        command: payload for command, payload in payloads.items() if command.startswith("/coach")
    }

    assert list(coach) == [
        "/coach teach",
        "/coach replay",
        "/coach attempt",
        "/coach feedback",
        "/coach state",
        "/coach stop",
    ]
    assert coach["/coach teach"]["usage"] == (
        "/coach teach QUERY [--segment LABEL --tempo 25..200 --difficulty "
        "foundation|standard|challenge --hand left|right|both --loops 1..8 "
        "--author never|bounded]"
    )
    assert coach["/coach replay"]["usage"] == (
        "/coach replay [--segment LABEL --tempo 25..200 --loops 1..8]"
    )
    assert coach["/coach attempt"]["usage"] == (
        "/coach attempt [--segment LABEL --tempo 25..200 --count-in 0..8]"
    )
    assert coach["/coach feedback"]["usage"] == "/coach feedback [attempt_id]"
    assert coach["/coach state"]["usage"] == "/coach state"
    assert coach["/coach stop"]["usage"] == "/coach stop"


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
