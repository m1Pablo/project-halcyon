import asyncio
from types import SimpleNamespace

from halcyon.app import HalcyonApp
from halcyon.audio_bridge import AudioBridgeSnapshot
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


class FakeAudio:
    def __init__(self) -> None:
        self.panic_count = 0

    def start(self) -> bool:
        return True

    def apply_patch(self, patch: dict[str, float]) -> bool:
        return True

    def snapshot(self) -> AudioBridgeSnapshot:
        return AudioBridgeSnapshot(running=True, ready=True, text="Audio: ready")

    def all_notes_off(self) -> bool:
        self.panic_count += 1
        return True

    def handle_midi_event(self, event) -> bool:  # type: ignore[no-untyped-def]
        return True

    def close(self) -> None:
        pass


class FakeMidi:
    connected = False

    def list_ports(self) -> list[str]:
        return []

    def connect(self) -> str:
        return "Test MIDI"

    def poll(self) -> list[object]:
        return []

    def close(self) -> None:
        pass


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


def test_multiline_input_shift_enter_and_enter_direct_slash_without_openai(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def run() -> None:
        app = HalcyonApp()
        audio = FakeAudio()
        app.audio = audio  # type: ignore[assignment]
        app.midi = FakeMidi()  # type: ignore[assignment]

        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.press("f4")
            app.chat_panel.input.focus()
            app.chat_panel.input.value = "first"
            app.chat_panel.input.cursor_position = len("first")
            await pilot.press("shift+enter")
            await pilot.press("s", "e", "c", "o", "n", "d")
            assert app.chat_panel.input.value == "first\nsecond"

            app.chat_panel.input.value = "/panic"
            app.chat_panel.input.cursor_position = len("/panic")
            await pilot.press("enter")
            await pilot.pause()

            assert app.chat_panel.input.value == ""
            assert audio.panic_count == 1

    asyncio.run(run())


def test_tab_completion_interaction_is_registry_derived(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def run() -> None:
        app = HalcyonApp()
        app.audio = FakeAudio()  # type: ignore[assignment]
        app.midi = FakeMidi()  # type: ignore[assignment]
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.press("f4")
            app.chat_panel.input.focus()
            app.chat_panel.input.value = "/pre"
            app.chat_panel.input.cursor_position = len("/pre")
            await pilot.press("tab")
            assert app.chat_panel.input.value == "/preset "

    asyncio.run(run())
