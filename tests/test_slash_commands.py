from halcyon.menu import load_menu
from halcyon.slash_commands import (
    build_slash_tree,
    iter_slash_leaf_commands,
    parse_slash_command,
    resolve_slash_command,
    suggest_slash_completion,
)


def test_build_slash_tree_uses_menu_definition_only() -> None:
    menu_def = load_menu()

    tree = build_slash_tree(menu_def)

    assert set(tree) == {"synth", "preset", "panic"}
    assert set(tree["synth"].children) == {"state", "params", "set", "patch"}
    assert tree["panic"].payload["tool"] == "all_notes_off"


def test_resolve_leaf_command_with_arguments() -> None:
    resolved = resolve_slash_command(load_menu(), "/synth set tone 0.8")

    assert resolved.status == "leaf"
    assert resolved.leaf_command == "/synth set"
    assert resolved.payload["tool"] == "set_synth_param"
    assert resolved.arguments == ("tone", "0.8")


def test_resolve_quoted_arguments_for_preset_names() -> None:
    resolved = resolve_slash_command(load_menu(), '/preset apply "Soft Pad"')

    assert resolved.status == "leaf"
    assert resolved.leaf_command == "/preset apply"
    assert resolved.arguments == ("Soft Pad",)


def test_resolve_group_lists_children_without_payload() -> None:
    resolved = resolve_slash_command(load_menu(), "/synth")

    assert resolved.status == "group"
    assert resolved.payload is None
    assert resolved.options == (
        ("state", "State"),
        ("params", "Params"),
        ("set", "Set"),
        ("patch", "Patch"),
    )


def test_unknown_command_suggests_from_tree() -> None:
    resolved = resolve_slash_command(load_menu(), "/synt")

    assert resolved.status == "error"
    assert "Did you mean `/synth`?" in resolved.message


def test_unknown_child_suggests_from_current_group() -> None:
    resolved = resolve_slash_command(load_menu(), "/synth stat")

    assert resolved.status == "error"
    assert "Did you mean `state`?" in resolved.message


def test_parse_slash_command_normalizes_tokens() -> None:
    assert parse_slash_command('/Preset Apply "Soft Pad"') == [
        "preset",
        "apply",
        "soft pad",
    ]


def test_completion_uses_tree_candidates() -> None:
    menu_def = load_menu()

    completion = suggest_slash_completion(menu_def, "/sy")
    child_completion = suggest_slash_completion(menu_def, "/synth p")

    assert completion is not None
    assert completion.remainder == "nth"
    assert completion.completed_token == "synth"
    assert completion.append_space is True
    assert child_completion is not None
    assert child_completion.completed_token == "patch"


def test_leaf_iteration_is_command_listing_source() -> None:
    commands = [command for command, _ in iter_slash_leaf_commands(load_menu())]

    assert commands == [
        "/synth state",
        "/synth params",
        "/synth set",
        "/synth patch",
        "/preset apply",
        "/preset save",
        "/panic",
    ]
