import pytest

from halcyon.app import HalcyonApp
from halcyon.coach_models import CoachError
from halcyon.menu import load_menu
from halcyon.mcp_tools import ToolExecutionError, execute_tool, tool_definitions
from halcyon.slash_commands import iter_slash_leaf_commands


class FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def synth_tool_state(self) -> dict[str, object]:
        return {"patch": {"preset": "Init"}, "audio": {"ready": True}}

    def synth_tool_set_param(self, param_id: str, normalized_value: float) -> dict[str, object]:
        self.calls.append((param_id, normalized_value))
        return {"ok": True, "param": param_id, "value": normalized_value}

    def synth_tool_apply_patch(self, values: dict[str, float]) -> dict[str, object]:
        self.calls.append(("patch", values))
        return {"ok": True, "values": values}

    def synth_tool_apply_preset(self, preset_name: str) -> dict[str, object]:
        self.calls.append(("preset", preset_name))
        return {"ok": True, "preset": preset_name}

    def synth_tool_save_preset(self, preset_name: str) -> dict[str, object]:
        self.calls.append(("save_preset", preset_name))
        return {"ok": True, "preset": preset_name}

    def synth_tool_all_notes_off(self) -> dict[str, object]:
        self.calls.append(("all_notes_off", None))
        return {"ok": True}

    def coach_tool_start_lesson(self, **arguments):  # type: ignore[no-untyped-def]
        self.calls.append(("coach_teach", arguments))
        return {"ok": True, "arguments": arguments}

    def coach_tool_replay_lesson(self, **arguments):  # type: ignore[no-untyped-def]
        self.calls.append(("coach_replay", arguments))
        return {"ok": True, "arguments": arguments}

    def coach_tool_start_attempt(self, **arguments):  # type: ignore[no-untyped-def]
        self.calls.append(("coach_attempt", arguments))
        return {"ok": True, "arguments": arguments}

    def coach_tool_feedback(self, attempt_id=None):  # type: ignore[no-untyped-def]
        self.calls.append(("coach_feedback", attempt_id))
        return {"ok": True, "attempt_id": attempt_id}

    def coach_tool_state(self) -> dict[str, object]:
        self.calls.append(("coach_state", None))
        return {"ok": True, "coach": {"phase": "idle"}}

    def coach_tool_stop(self) -> dict[str, object]:
        self.calls.append(("coach_stop", None))
        return {"ok": True, "code": "COACH_STOPPED"}


def test_tool_definitions_are_mcp_shaped() -> None:
    tools = tool_definitions()

    assert {tool["name"] for tool in tools} == {
        "get_synth_state",
        "list_synth_params",
        "set_synth_param",
        "apply_synth_patch",
        "apply_synth_preset",
        "save_synth_preset",
        "all_notes_off",
        "start_coach_lesson",
        "replay_coach_lesson",
        "start_coach_attempt",
        "get_coach_feedback",
        "get_coach_state",
        "stop_coach_session",
    }
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)
    assert "recent midi" not in repr(tools).lower()
    assert "midi log" not in repr(tools).lower()


def test_internal_coach_tool_definitions_match_the_six_menu_leaves() -> None:
    menu_tools = {
        payload["tool"]
        for command, payload in iter_slash_leaf_commands(load_menu())
        if command.startswith("/coach")
    }
    defined = {tool["name"] for tool in tool_definitions()}

    assert menu_tools == {
        "start_coach_lesson",
        "replay_coach_lesson",
        "start_coach_attempt",
        "get_coach_feedback",
        "get_coach_state",
        "stop_coach_session",
    }
    assert menu_tools <= defined
    assert all(
        tool["inputSchema"].get("additionalProperties") is False
        for tool in tool_definitions()
        if tool["name"] in menu_tools
    )


def test_execute_tool_validates_and_dispatches() -> None:
    host = FakeHost()

    result = execute_tool(host, "set_synth_param", {"param": "tone", "value": 0.8})

    assert result == {"ok": True, "param": "tone", "value": 0.8}
    assert host.calls == [("tone", 0.8)]


def test_execute_tool_saves_user_preset() -> None:
    host = FakeHost()

    result = execute_tool(host, "save_synth_preset", {"preset": "Dusty Pad"})

    assert result == {"ok": True, "preset": "Dusty Pad"}
    assert host.calls == [("save_preset", "Dusty Pad")]


def test_execute_tool_rejects_unknown_tool() -> None:
    with pytest.raises(ToolExecutionError, match="unknown tool"):
        execute_tool(FakeHost(), "delete_everything", {})


def test_execute_tool_dispatches_validated_coach_teach_only_as_internal_leaf() -> None:
    host = FakeHost()

    result = execute_tool(
        host,
        "start_coach_lesson",
        {
            "query": "Aphex Twin Avril 14th",
            "segment": "main melody",
            "tempo_percent": 75,
            "difficulty": "foundation",
            "hand": "right",
            "loops": 2,
            "author": "bounded",
        },
    )

    assert result["ok"] is True
    assert host.calls == [
        (
            "coach_teach",
            {
                "query": "Aphex Twin Avril 14th",
                "segment": "main melody",
                "tempo_percent": 75,
                "difficulty": "foundation",
                "hand": "right",
                "loops": 2,
                "author": "bounded",
            },
        )
    ]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("start_coach_lesson", {"query": "x", "tempo_percent": 24, "loops": 1, "author": "never"}),
        ("replay_coach_lesson", {"loops": 9}),
        ("start_coach_attempt", {"count_in": True}),
        ("get_coach_feedback", {"attempt_id": ""}),
        ("get_coach_state", {"raw_history": True}),
        ("stop_coach_session", {"all_origins": True}),
    ],
)
def test_internal_coach_tools_revalidate_bounds_and_reject_raw_options(
    tool: str, arguments: dict[str, object]
) -> None:
    with pytest.raises(ToolExecutionError):
        execute_tool(FakeHost(), tool, arguments)


def test_coach_error_becomes_stable_bounded_envelope() -> None:
    class ErrorHost(FakeHost):
        def coach_tool_state(self):  # type: ignore[no-untyped-def]
            raise CoachError(
                "NO_ACTIVE_LESSON",
                "No reusable lesson is active.",
                resolution="Run /coach teach QUERY first.",
                retryable=True,
            )

    result = execute_tool(ErrorHost(), "get_coach_state", {})

    assert result == {
        "ok": False,
        "error": {
            "code": "NO_ACTIVE_LESSON",
            "message": "No reusable lesson is active.",
            "retryable": True,
            "resolution": "Run /coach teach QUERY first.",
        },
    }


def test_app_tool_state_does_not_expose_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-do-not-emit")

    state = HalcyonApp().synth_tool_state()
    state_text = repr(state)

    assert "OPENAI_API_KEY" not in state_text
    assert "sk-test-do-not-emit" not in state_text
