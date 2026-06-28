import pytest

from halcyon.app import HalcyonApp
from halcyon.mcp_tools import ToolExecutionError, execute_tool, tool_definitions


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
    }
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)


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


def test_app_tool_state_does_not_expose_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-do-not-emit")

    state = HalcyonApp().synth_tool_state()
    state_text = repr(state)

    assert "OPENAI_API_KEY" not in state_text
    assert "sk-test-do-not-emit" not in state_text
