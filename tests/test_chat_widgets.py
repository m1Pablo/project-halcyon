from types import SimpleNamespace

from halcyon.app import HalcyonApp
from halcyon.chat_widgets import ChatPanel
from halcyon.menu import load_menu


class FakeChatPanel:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def write_assistant(self, text: str) -> None:
        self.messages.append(("assistant", text))

    def write_codex(self, text: str) -> None:
        self.messages.append(("codex", text))

    def write_system(self, text: str) -> None:
        self.messages.append(("system", text))


def test_slash_completion_comes_from_menu_xml() -> None:
    panel = ChatPanel(menu_def=load_menu())

    completion = panel.slash_completion_for("/sy")

    assert completion is not None
    assert completion.completed_token == "synth"
    assert completion.remainder == "nth"


def test_chat_panel_accepts_slash_completion() -> None:
    panel = ChatPanel(menu_def=load_menu())

    assert panel.completed_slash_value("/pre") == "/preset "


def test_chat_panel_lists_group_options() -> None:
    panel = ChatPanel(menu_def=load_menu())

    options = panel.slash_group_options_for("/synth")

    assert "/synth state" in options
    assert "/synth set" in options
    assert "/synth patch" in options


def test_app_slash_input_executes_without_openai() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    chat = FakeChatPanel()
    app = SimpleNamespace(menu_def=load_menu(), chat_panel=chat)

    def execute_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        return {"ok": True}

    app._execute_tool_from_worker = execute_tool

    executed = HalcyonApp._execute_chat_slash_command(app, "/panic")  # type: ignore[arg-type]

    assert executed
    assert calls == [("all_notes_off", {})]
    assert chat.messages == [("codex", "Ran `/panic`.")]


def test_app_slash_group_lists_children_without_executing() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    chat = FakeChatPanel()
    app = SimpleNamespace(menu_def=load_menu(), chat_panel=chat)
    app._execute_tool_from_worker = lambda name, arguments: calls.append((name, arguments))

    executed = HalcyonApp._execute_chat_slash_command(app, "/preset")  # type: ignore[arg-type]

    assert not executed
    assert calls == []
    assert chat.messages
    assert "/preset apply" in chat.messages[0][1]
    assert "/preset save" in chat.messages[0][1]
