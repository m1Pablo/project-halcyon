"""Console behavior through native Textual widgets, with no native clipboard I/O."""

import asyncio
from dataclasses import replace

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Button, Collapsible, Markdown, Static

from halcyon.chat_widgets import ChatPanel, ConsoleChunk, ConsoleContextMenu, ConsoleFence
from halcyon.console_events import ConsoleEvent
from halcyon.menu import load_menu


@pytest.fixture(autouse=True)
def no_native_clipboard(monkeypatch):
    monkeypatch.setattr("halcyon.console_clipboard.sys.platform", "linux")


class ConsoleApp(App):
    CSS = """
    ChatPanel { height: 1fr; }
    #chat-input-row { height: 4; }
    #chat-input { height: 4; width: 1fr; }
    #chat-prompt { width: 2; }
    """

    def __init__(self):
        super().__init__()
        self.panel = ChatPanel(menu_def=load_menu())
        self.submitted = []
        self.reruns = []

    def compose(self) -> ComposeResult:
        yield self.panel

    def on_chat_panel_submitted(self, message):
        self.submitted.append(message.text)

    def on_chat_panel_rerun_requested(self, message):
        self.reruns.append(message.command)


def event(text="hello", **kwargs):
    fields = dict(event_id="event", kind="message", role="user", source="chat", text=text)
    return ConsoleEvent(**(fields | kwargs))


@pytest.mark.parametrize(
    "role,source,label",
    [
        ("user", "You", "You"),
        ("assistant", "halCYON", "Halcyon"),
        ("client", "Codex", "Client · Codex"),
        ("user", "MCP", "You · MCP"),
    ],
)
def test_message_labels_deduplicate_only_identical_role_source(role, source, label):
    assert ConsoleChunk(event(role=role, source=source)).label_text() == label


@pytest.mark.parametrize(
    "kind,label", [("command", "Command · Command"), ("log", "Log · Log · info")]
)
def test_command_and_log_labels_keep_source_even_when_identical(kind, label):
    assert ConsoleChunk(event(kind=kind, source=kind.title())).label_text() == label


def test_literal_wrapped_and_multichunk_selection_and_ctrl_c():
    async def run():
        app = ConsoleApp()
        async with app.run_test(size=(55, 30)) as pilot:
            text = "[bold] literal [@click=evil] " + "wrapped " * 15
            app.panel.append_event(event(text))
            app.panel.append_event(event("second chunk", event_id="second"))
            await pilot.pause()
            chunks = list(app.panel.transcript.query(ConsoleChunk))[-2:]
            body = chunks[0].query_one(".console-body", Static)
            body2 = chunks[1].query_one(".console-body", Static)
            assert body.content == text
            assert body.region.height > 1
            app.screen.selections = {
                body: Selection(Offset(0, 0), None),
                body2: Selection(None, Offset(6, 0)),
            }
            assert app.screen.get_selected_text() == text + "\nsecond"
            chunks[0].focus()
            await pilot.press("ctrl+c")
            assert app.clipboard == text + "\nsecond"
            app.screen.clear_selection()
            await pilot.press("ctrl+c")
            assert app.clipboard == text + "\nsecond"

    asyncio.run(run())


def test_mouse_drag_highlights_extracts_and_autocopies():
    async def run():
        app = ConsoleApp()
        async with app.run_test() as pilot:
            app.panel.append_event(event("alpha bravo charlie"))
            await pilot.pause()
            await pilot.pause()
            body = list(app.panel.transcript.query(ConsoleChunk))[-1].query_one(".console-body")
            x, y = body.region.x, body.region.y
            assert app.screen.get_widget_at(x, y)[0] is body, (
                body.region,
                app.screen.get_widget_at(x, y),
                app.panel.transcript.region,
            )
            await pilot.mouse_down(offset=(x, y))
            await pilot.hover(offset=(x + 4, y))
            await pilot.mouse_up(offset=(x + 4, y))
            await pilot.pause()
            assert app.screen.get_selected_text() == "alpha"
            assert app.screen.selections
            assert app.clipboard == "alpha"

    asyncio.run(run())


def test_markdown_code_tables_raw_copy_details_and_safe_links(monkeypatch):
    async def run():
        app = ConsoleApp()
        opened = []
        monkeypatch.setattr(app, "open_url", opened.append)
        raw = "## Patch\n\n- **bright** tone\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```python\nprint('x')\n\n```\n"
        async with app.run_test(size=(100, 45)) as pilot:
            app.panel.append_event(event(raw, role="assistant", details="verbose [literal]"))
            await pilot.pause()
            chunk = list(app.panel.transcript.query(ConsoleChunk))[-1]
            markdown = chunk.query_one(Markdown)
            assert markdown.source == raw
            assert markdown.query("MarkdownTable")
            assert markdown.query("MarkdownBulletList")
            assert chunk.get_copy_text() == raw
            fence = chunk.query_one(ConsoleFence)
            code = fence.query_one("#code-content", Static)
            app.screen.selections = {code: Selection(Offset(0, 0), None)}
            assert app.screen.get_selected_text() == "print('x')"
            app.screen.clear_selection()
            fence.query_one(Button).press()
            await pilot.pause()
            assert app.clipboard == "print('x')\n\n"
            chunk.query_one(Collapsible).collapsed = False
            await pilot.pause()
            assert chunk.get_copy_text() == raw + "\n\nverbose [literal]"
            detail = chunk.query_one(".console-details", Static)
            app.screen.selections = {detail: Selection(Offset(0, 0), Offset(7, 0))}
            assert app.screen.get_selected_text() == "verbose"
            for href in ("https://example.com", "file:///etc/passwd", "custom://run"):
                markdown.post_message(Markdown.LinkClicked(markdown, href))
            await pilot.pause()
            assert opened == ["https://example.com"]

    asyncio.run(run())


def test_context_capture_copy_raw_and_rerun_are_separate():
    async def run():
        app = ConsoleApp()
        async with app.run_test() as pilot:
            app.panel.append_event(
                event("/panic", kind="command", status="completed", replay_command="/panic")
            )
            await pilot.pause()
            await pilot.pause()
            chunk = list(app.panel.transcript.query(ConsoleChunk))[-1]
            body = chunk.query_one(".console-body", Static)
            app.screen.selections = {body: Selection(Offset(2, 0), Offset(5, 0))}
            selected = app.screen.get_selected_text()
            x, y = body.region.x, body.region.y
            assert app.screen.get_widget_at(x, y)[0] is body, (
                body.region,
                app.screen.get_widget_at(x, y),
                app.panel.transcript.region,
            )
            await pilot.mouse_down(offset=(x, y), button=3)
            await pilot.mouse_up(offset=(x, y))
            await pilot.pause()
            assert isinstance(app.screen, ConsoleContextMenu)
            assert app.screen.selection_text == selected
            await pilot.click("#console-copy-selection")
            await pilot.pause()
            assert app.clipboard == selected
            assert not app.reruns
            app.panel.transcript.show_context_menu(chunk, "", 0, 0)
            await pilot.pause()
            await pilot.click("#console-copy-entry")
            await pilot.pause()
            assert app.clipboard == "/panic"
            app.panel.transcript.show_context_menu(chunk, "", 0, 0)
            await pilot.pause()
            await pilot.click("#console-rerun")
            await pilot.pause()
            assert app.reruns == ["/panic"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes",
    [
        dict(status="running"),
        dict(replay_command="echo unsafe"),
        dict(replay_command="/unknown"),
        dict(replay_command="/synth set bogus 9"),
        dict(kind="message"),
        dict(kind="log"),
        dict(replay_command=None),
    ],
)
def test_rerun_rejects_active_noncommand_missing_and_invalid(changes):
    async def run():
        app = ConsoleApp()
        async with app.run_test() as pilot:
            app.panel.append_event(
                event("/panic", kind="command", status="completed", replay_command="/panic", **{})
                if not changes
                else replace(
                    event("/panic", kind="command", status="completed", replay_command="/panic"),
                    **changes,
                )
            )
            await pilot.pause()
            chunk = list(app.panel.transcript.query(ConsoleChunk))[-1]
            assert not chunk.can_rerun()
            app.panel.transcript.show_context_menu(chunk, "", 0, 0)
            await pilot.pause()
            assert not app.screen.query("#console-rerun")
            await pilot.press("escape")
            assert not app.reruns

    asyncio.run(run())


def test_lifecycle_upsert_bounded_store_and_preserved_replay():
    async def run():
        app = ConsoleApp()
        async with app.run_test() as pilot:
            command = event(
                "/panic", kind="command", run_id="run", status="running", replay_command="/panic"
            )
            app.panel.append_event(command)
            await pilot.pause()
            app.panel.append_event(
                replace(command, event_id="finished", status="completed", replay_command=None)
            )
            await pilot.pause()
            commands = [
                chunk
                for chunk in app.panel.transcript.query(ConsoleChunk)
                if chunk.event.kind == "command"
            ]
            assert len(commands) == 1
            assert commands[0].can_rerun()
            assert commands[0].event.status == "completed"
            assert "run run" in commands[0].label_text()
        bounded = ChatPanel(menu_def=load_menu())
        for index in range(1055):
            bounded.append_event(event(str(index), event_id=str(index)))
        assert len(bounded.transcript.entries) <= 1000
        assert len(bounded.transcript._chunks) == len(bounded.transcript.entries)
        assert bounded.transcript.entries[-1].text == "1054"

    asyncio.run(run())


def test_composer_send_paste_history_draft_completion_and_escape(monkeypatch):
    async def run():
        app = ConsoleApp()
        monkeypatch.setattr("halcyon.chat_widgets.paste_text", lambda _: "pasted\nlines")
        async with app.run_test(size=(80, 35)) as pilot:
            panel = app.panel
            assert not app.query("#chat-send")
            panel.input.focus()
            await pilot.press("ctrl+v")
            assert panel.input.text == "pasted\nlines"
            assert not app.submitted
            await pilot.press("shift+enter", "x")
            assert panel.input.text == "pasted\nlines\nx"
            await pilot.press("enter")
            assert app.submitted == ["pasted\nlines\nx"]
            assert panel.input.text == ""
            panel.input.value = "draft"
            panel.input.cursor_position = 2
            await pilot.press("up")
            assert panel.input.text == "pasted\nlines\nx"
            await pilot.press("down")
            assert (panel.input.text, panel.input.cursor_position) == ("draft", 2)
            assert len(app.submitted) == 1
            panel.input.value = "first\nsecond"
            panel.input.cursor_position = len(panel.input.text)
            await pilot.press("up")
            assert panel.input.text == "first\nsecond"
            assert panel.input.selection.end[0] == 0
            panel.input.value = "/synth "
            panel.input.cursor_position = len(panel.input.text)
            await pilot.pause()
            assert len(panel.suggestions) > 1
            assert all(command.startswith("/synth ") for command, _ in panel.suggestions)
            await pilot.press("down")
            selected = panel.suggestions[panel._suggestion_index][0]
            assert panel.input.text == "/synth "
            await pilot.press("tab")
            assert panel.input.text == selected + " "
            panel.input.value = "/pre"
            panel.input.cursor_position = len(panel.input.text)
            await pilot.pause()
            assert panel.suggestions
            assert "preset" in panel.input.render_line(0).text
            assert panel.input.text == "/pre"
            await pilot.press("escape")
            assert not panel.suggestions and panel.input.has_focus
            await pilot.press("escape")
            assert not panel.input.has_focus
            assert len(app.submitted) == 1

    asyncio.run(run())


def test_history_cap_collapse_and_caret_boundary():
    panel = ChatPanel(menu_def=load_menu())
    for index in range(105):
        panel.input.value = str(index)
        panel.submit_input()
    panel.input.value = "104"
    panel.submit_input()
    assert len(panel.history) == 100
    assert panel.history[0] == "5"
    panel.input.value = "first\nlast"
    panel.input.cursor_position = 7
    assert not panel.recall_history(-1)
    assert panel.input.text == "first\nlast"
    panel.input.value = "/pre"
    panel.input.cursor_position = 1
    assert not panel.accept_slash_completion()
    assert not panel.suggestions


def test_scroll_follow_and_scrolled_up_eviction_anchor(monkeypatch):
    monkeypatch.setattr("halcyon.chat_widgets.MAX_CONSOLE_CHUNKS", 30)
    monkeypatch.setattr("halcyon.chat_widgets.EVICTION_BATCH", 5)

    async def run():
        app = ConsoleApp()
        async with app.run_test(size=(80, 25)) as pilot:
            for index in range(29):
                app.panel.append_event(event(f"line {index}", event_id=str(index)))
                await pilot.pause()
            transcript = app.panel.transcript
            assert transcript.max_scroll_y - transcript.scroll_y <= 2
            transcript.scroll_to(y=30, animate=False, force=True)
            await pilot.pause()
            anchor = list(transcript.query(ConsoleChunk))[15]
            before = anchor.virtual_region.y - transcript.scroll_y
            for index in range(29, 33):
                app.panel.append_event(event(f"line {index}", event_id=str(index)))
                await pilot.pause()
            assert anchor.virtual_region.y - transcript.scroll_y == before
            assert transcript.scroll_y < transcript.max_scroll_y - 2
            transcript.scroll_end(animate=False)
            await pilot.pause()
            app.panel.append_event(event("last", event_id="last"))
            await pilot.pause()
            assert transcript.max_scroll_y - transcript.scroll_y <= 2

    asyncio.run(run())


def test_real_drag_across_wrapped_unicode_and_multiple_chunks():
    async def run():
        app = ConsoleApp()
        async with app.run_test(size=(55, 32)) as pilot:
            text = "日本語 café " + "musical notes " * 8
            app.panel.append_event(event(text))
            app.panel.append_event(event("second chunk", event_id="second"))
            await pilot.pause()
            await pilot.pause()
            chunks = list(app.panel.transcript.query(ConsoleChunk))[-2:]
            first = chunks[0].query_one(".console-body")
            second = chunks[1].query_one(".console-body")
            assert first.region.height > 1
            await pilot.mouse_down(offset=(first.region.x, first.region.y))
            await pilot.hover(offset=(second.region.x + 5, second.region.y))
            await pilot.mouse_up(offset=(second.region.x + 5, second.region.y))
            await pilot.pause()
            selected = app.screen.get_selected_text()
            assert selected is not None
            assert text in selected
            assert selected.endswith("secon")
            assert app.clipboard == selected
            assert first.text_selection is not None
            # Rendering carries a visible selection style, not just extraction data.
            assert any(
                segment.style and segment.style.bgcolor
                for strip in first.render_lines(first.region.reset_offset)
                for segment in strip
            )

    asyncio.run(run())


def test_lifecycle_replacement_preserves_selection_and_expanded_detail():
    async def run():
        app = ConsoleApp()
        async with app.run_test() as pilot:
            command = event(
                "/panic", kind="command", run_id="run", status="running", details="first detail"
            )
            app.panel.append_event(command)
            await pilot.pause()
            await pilot.pause()
            chunk = list(app.panel.transcript.query(ConsoleChunk))[-1]
            body = chunk.query_one(".console-body", Static)
            detail = chunk.query_one(Collapsible)
            detail.collapsed = False
            app.screen.selections = {body: Selection(Offset(2, 0), Offset(8, 0))}
            app.panel.append_event(
                replace(command, event_id="complete", status="completed", details="new detail")
            )
            await pilot.pause()
            assert chunk.query_one(".console-body") is body
            assert not detail.collapsed
            assert body in app.screen.selections
            assert app.screen.get_selected_text() == "/panic"
            assert chunk.get_copy_text() == "/panic\n\nnew detail"

    asyncio.run(run())


def test_editor_owns_copy_cut_selection_and_unsafe_history_is_not_retained():
    from textual.widgets.text_area import Selection as EditorSelection

    async def run():
        app = ConsoleApp()
        async with app.run_test() as pilot:
            app.panel.input.value = "copy this"
            app.panel.input.selection = EditorSelection((0, 0), (0, 4))
            app.panel.input.focus()
            await pilot.press("ctrl+c")
            assert app.clipboard == "copy"
            await pilot.press("ctrl+x")
            assert app.panel.input.text == " this"
            assert app.clipboard == "copy"
            app.panel.input.value = "OPENAI_API_KEY=sk-do-not-retain"
            await pilot.press("enter")
            assert app.submitted == ["OPENAI_API_KEY=sk-do-not-retain"]
            assert app.panel.history == []

    asyncio.run(run())


def test_context_does_not_capture_selection_outside_transcript():
    async def run():
        app = ConsoleApp()
        async with app.run_test() as pilot:
            app.panel.append_event(event("safe output"))
            await pilot.pause()
            await pilot.pause()
            unrelated = app.panel.slash_hint
            unrelated.update("not transcript content")
            app.screen.selections = {unrelated: Selection(Offset(0, 0), Offset(3, 0))}
            chunk = list(app.panel.transcript.query(ConsoleChunk))[-1]
            body = chunk.query_one(".console-body")
            await pilot.mouse_down(offset=(body.region.x, body.region.y), button=3)
            await pilot.mouse_up(offset=(body.region.x, body.region.y))
            await pilot.pause()
            assert isinstance(app.screen, ConsoleContextMenu)
            assert app.screen.selection_text == ""
            assert not app.screen.query("#console-copy-selection")
            await pilot.click("#console-copy-entry")
            await pilot.pause()
            assert app.clipboard == "safe output"

    asyncio.run(run())


def test_long_unbroken_line_and_fenced_code_selection_preserve_source():
    async def run():
        app = ConsoleApp()
        line = "x" * 250 + " café 日本語"
        async with app.run_test(size=(55, 32)) as pilot:
            app.panel.append_event(event(line))
            app.panel.append_event(
                event(f"```text\n{line}\n```", event_id="code", role="assistant")
            )
            await pilot.pause()
            await pilot.pause()
            chunks = list(app.panel.transcript.query(ConsoleChunk))[-2:]
            body = chunks[0].query_one(".console-body", Static)
            code = chunks[1].query_one("#code-content", Static)
            for widget in (body, code):
                app.screen.selections = {widget: Selection(Offset(0, 0), None)}
                assert app.screen.get_selected_text() == line

    asyncio.run(run())


def test_selected_bottom_text_keeps_viewport_on_append_and_extends_with_native_edge_scroll():
    async def run():
        app = ConsoleApp()
        selected_command = event(
            "/panic", event_id="37", kind="command", run_id="selected-run", status="running"
        )
        for index in range(40):
            app.panel.append_event(
                selected_command
                if index == 37
                else event(f"musical phrase {index:02}", event_id=str(index))
            )
        async with app.run_test(size=(70, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            transcript = app.panel.transcript
            transcript.scroll_end(animate=False, immediate=True)
            await pilot.pause()
            body = list(transcript.query(ConsoleChunk))[-3].query_one(".console-body")
            x, y = body.region.x, body.region.y
            assert transcript.content_region.contains(x, y)
            assert transcript.max_scroll_y - transcript.scroll_y <= 2
            await pilot.mouse_down(offset=(x, y))
            await pilot.hover(offset=(x + 6, y))
            selected = app.screen.get_selected_text()
            assert selected
            before = transcript.scroll_y
            app.panel.append_event(event("arriving while selecting", event_id="arrival"))
            await pilot.pause()
            assert transcript.scroll_y == before
            assert app.screen.get_selected_text() == selected
            app.panel.append_event(
                replace(
                    selected_command,
                    event_id="settled",
                    status="completed",
                    details="settled detail",
                )
            )
            await pilot.pause()
            assert transcript.scroll_y == before
            assert app.screen.get_selected_text() == selected
            app.screen._forward_event(
                events.MouseScrollUp(body, x + 6, y, 0, -1, 0, False, False, False)
            )
            await pilot.pause()
            assert transcript.scroll_y < before
            assert app.screen.get_selected_text() == selected
            before = transcript.scroll_y
            edge_y = transcript.content_region.y + 1
            # Pilot.hover always sets delta_y=0, which intentionally disables
            # native edge scrolling. Deliver the driver's real upward delta.
            app.screen._forward_event(
                events.MouseMove(
                    body,
                    x + 6,
                    edge_y,
                    0,
                    edge_y - y,
                    1,
                    False,
                    False,
                    False,
                )
            )
            await pilot.pause(1.5)
            assert transcript.scroll_y < before - 15
            assert not transcript.content_region.contains(
                *app.screen._select_state.start.pointer_start_offset
            )
            expanded = app.screen.get_selected_text()
            assert expanded and len(expanded) > len(selected)
            assert "musical phrase 25" in expanded
            assert transcript._owns_selection(), [
                (type(w).__name__, w.id, w.region)
                for w in app.screen.selections
                if transcript not in w.ancestors
            ]
            assert transcript.max_scroll_y - transcript.scroll_y > 2
            # A real release has no extra MouseMove; Pilot.mouse_up injects one.
            app.screen._forward_event(
                events.MouseUp(body, x + 6, edge_y, 0, 0, 1, False, False, False)
            )
            await pilot.pause()
            assert app.clipboard == app.screen.get_selected_text()
            app.screen.selections = {}
            transcript.scroll_end(animate=False, immediate=True)
            await pilot.pause()
            app.panel.append_event(event("following again", event_id="resumed"))
            await pilot.pause()
            assert transcript.max_scroll_y - transcript.scroll_y <= 2

    asyncio.run(run())


@pytest.mark.parametrize("interaction", ["selection", "scroll"])
def test_queued_follow_does_not_override_a_new_selection_or_scroll(interaction):
    async def run():
        app = ConsoleApp()
        for index in range(40):
            app.panel.append_event(event(f"phrase {index}", event_id=str(index)))
        async with app.run_test(size=(70, 30)) as pilot:
            transcript = app.panel.transcript
            transcript.scroll_end(animate=False, immediate=True)
            await pilot.pause()
            body = list(transcript.query(ConsoleChunk))[-3].query_one(".console-body")
            # Queue the mount, then deliver a user event before its deferred follow.
            app.panel.append_event(event("queued output", event_id="queued"))
            if interaction == "selection":
                app.screen._forward_event(
                    events.MouseDown(
                        body, body.region.x, body.region.y, 0, 0, 1, False, False, False
                    )
                )
            else:
                app.screen._forward_event(
                    events.MouseScrollUp(
                        body, body.region.x, body.region.y, 0, -1, 0, False, False, False
                    )
                )
            await pilot.pause()
            await pilot.pause()
            assert transcript.scroll_y < transcript.max_scroll_y
            if interaction == "selection":
                await pilot.mouse_up(offset=(body.region.x, body.region.y))

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [False, True])
def test_console_startup_does_not_claim_unverified_assistant_readiness(enabled):
    async def run():
        app = ConsoleApp()
        app.panel.enabled = enabled
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.panel.transcript.entries == []
            assert app.panel.input.has_focus

    asyncio.run(run())
