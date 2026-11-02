"""Selectable, bounded conversation and command console."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from rich.text import Text
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widgets import Button, Collapsible, Markdown, OptionList, Static, TextArea
from textual.widgets._markdown import MarkdownFence
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection

from halcyon.console_clipboard import copy_text, paste_text
from halcyon.command_executor import CommandExecutionError, prepare_slash_command
from halcyon.console_events import CONSOLE_TERMINAL_STATES, ConsoleEvent, safe_console_text
from halcyon.slash_commands import (
    SlashCompletion,
    build_slash_tree,
    resolve_slash_command,
    suggest_slash_completion,
)

MAX_CONSOLE_CHUNKS = 1000
EVICTION_BATCH = 50
MAX_HISTORY = 100


class ConsoleFence(MarkdownFence):
    """Native highlighted code with a source-only copy control."""

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Button("Copy code", classes="console-copy-code", compact=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        copy_text(self.app, self._token.content)
        self.app.notify("Copied", timeout=1)


class ConsoleMarkdown(Markdown):
    BLOCKS = {**Markdown.BLOCKS, "fence": ConsoleFence, "code_block": ConsoleFence}


class ConsoleChunk(Vertical, can_focus=True):
    """One identity with literal text or a native Markdown reply."""

    DEFAULT_CSS = """
    ConsoleChunk { height: auto; margin: 0 0 1 0; padding: 0 1; }
    ConsoleChunk > .console-label { height: 1; color: #a7b1bc; }
    ConsoleChunk > .console-body { height: auto; }
    ConsoleChunk.message.user > .console-label { color: #ff9f43; }
    ConsoleChunk.message.assistant > .console-label { color: #ab9df2; }
    ConsoleChunk.command { border-left: solid #a9dc76; }
    ConsoleChunk.command > .console-body { color: #a9dc76; }
    ConsoleChunk.log { color: #a7b1bc; }
    ConsoleChunk.warning > .console-label { color: #ffd866; }
    ConsoleChunk.error > .console-label { color: #ff6188; }
    ConsoleChunk:focus { background: $boost; }
    ConsoleChunk Markdown { padding: 0; margin: 0; background: transparent; }
    ConsoleChunk MarkdownFence { margin: 1 0; }
    ConsoleChunk .console-copy-code { height: 1; min-height: 1; width: auto; }
    ConsoleChunk Collapsible { padding: 0; border: none; }
    ConsoleChunk .console-details { height: auto; }
    """

    def __init__(self, event: ConsoleEvent) -> None:
        super().__init__(classes=f"{event.kind} {event.role} {event.severity}")
        self.event = event

    @property
    def transcript(self) -> ConsoleTranscript:
        assert isinstance(self.parent, ConsoleTranscript)
        return self.parent

    def label_text(self) -> str:
        event = self.event
        roles = {"user": "You", "assistant": "Halcyon", "client": "Client", "system": "System"}
        label = roles[event.role] if event.kind == "message" else event.kind.capitalize()
        parts = [label]
        if event.kind != "message" or label.casefold() != event.source.casefold():
            parts.append(event.source)
        if event.kind == "log":
            parts.append(event.severity)
        if event.status:
            parts.append(event.status)
        if event.run_id:
            parts.append(f"run {event.run_id[:8]}")
        if event.parent_run_id:
            parts.append(f"child of {event.parent_run_id[:8]}")
        return " · ".join(parts)

    def compose(self) -> ComposeResult:
        yield Static(self.label_text(), markup=False, classes="console-label")
        if self.event.kind == "message" and self.event.role == "assistant":
            yield ConsoleMarkdown(self.event.text, open_links=False, classes="console-body")
        else:
            body = f"$ {self.event.text}" if self.event.kind == "command" else self.event.text
            yield Static(body, markup=False, classes="console-body")
        if self.event.details:
            with Collapsible(title="Details", collapsed=True):
                yield Static(self.event.details, markup=False, classes="console-details")

    def get_copy_text(self) -> str:
        raw = self.event.raw_text if self.event.raw_text is not None else self.event.text
        details = self.query(Collapsible)
        if self.event.details and details and not details.first().collapsed:
            raw += "\n\n" + self.event.details
        return raw

    def can_rerun(self) -> bool:
        if not isinstance(self.parent, ConsoleTranscript):
            return False
        event = self.event
        eligible = bool(
            event.kind == "command"
            and event.status in CONSOLE_TERMINAL_STATES
            and event.replay_command
            and event.replay_command.startswith("/")
        )
        if not eligible:
            return False
        try:
            prepare_slash_command(self.transcript.panel.menu_def, event.replay_command or "")
        except CommandExecutionError:
            return False
        return True

    async def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:
            selection = (
                (self.screen.get_selected_text() or "") if self.transcript._owns_selection() else ""
            )
            self.transcript.show_context_menu(self, selection, event.screen_x, event.screen_y)
            event.stop()

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        if urlparse(event.href).scheme in {"http", "https"}:
            self.app.open_url(event.href)


class ConsoleContextMenu(ModalScreen[None]):
    """Explicit selection/raw copy and private registered-command replay."""

    BINDINGS = [Binding("escape", "dismiss", show=False)]
    DEFAULT_CSS = """
    ConsoleContextMenu { background: transparent; }
    ConsoleContextMenu > Vertical { width: 25; height: auto; background: $panel;
        border: solid #2c3545; padding: 0; }
    ConsoleContextMenu Button { width: 1fr; min-height: 1; height: 3; margin: 0; border: none; }
    """

    def __init__(self, chunk: ConsoleChunk, selection: str, x: int, y: int) -> None:
        super().__init__()
        self.chunk, self.selection_text, self.position = chunk, selection, (x, y)

    def compose(self) -> ComposeResult:
        with Vertical(id="console-context-menu"):
            if self.selection_text:
                yield Button("Copy selection", id="console-copy-selection")
            label = "Copy command" if self.chunk.event.kind == "command" else "Copy output"
            yield Button(label, id="console-copy-entry")
            if self.chunk.can_rerun():
                yield Button("Re-run command", id="console-rerun")

    def on_mount(self) -> None:
        x, y = self.position
        self.query_one("#console-context-menu").styles.offset = (
            max(0, min(x, self.size.width - 25)),
            max(0, min(y, self.size.height - 11)),
        )

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if not self.query_one("#console-context-menu").region.contains(
            event.screen_x, event.screen_y
        ):
            self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "console-rerun":
            if self.chunk.can_rerun():
                self.chunk.transcript.panel.post_message(
                    ChatPanel.RerunRequested(self.chunk.event.replay_command or "")
                )
        else:
            text = (
                self.selection_text
                if event.button.id == "console-copy-selection"
                else self.chunk.get_copy_text()
            )
            copy_text(self.app, text)
            self.app.notify("Copied", timeout=1)
        self.dismiss()


class ConsoleTranscript(VerticalScroll, can_focus=True):
    """Selectable chunk store, preserving scroll ownership on append/eviction."""

    BINDINGS = [Binding("ctrl+c,super+c", "copy", show=False)]
    DEFAULT_CSS = """
    ConsoleTranscript { height: 1fr; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.entries: list[ConsoleEvent] = []
        self._chunks: dict[str, ConsoleChunk] = {}
        self._follow_generation = 0

    @property
    def panel(self) -> ChatPanel:
        assert isinstance(self.parent, ChatPanel)
        return self.parent

    def compose(self) -> ComposeResult:
        for event in self.entries:
            yield self._chunks[self._key(event)]

    @staticmethod
    def _key(event: ConsoleEvent) -> str:
        return f"run:{event.run_id}" if event.kind == "command" and event.run_id else event.event_id

    def append_event(self, event: ConsoleEvent) -> None:
        key = self._key(event)
        existing = self._chunks.get(key)
        follow = self.max_scroll_y - self.scroll_y <= 2 and not self._selection_owns_viewport()
        if self.is_mounted:
            # Re-enable only after mounting, once queued user input has had a turn.
            self.anchor(False)
        if existing is not None:
            if event.replay_command is None:
                event = replace(event, replay_command=existing.event.replay_command)
            index = next(i for i, item in enumerate(self.entries) if self._key(item) == key)
            self.entries[index] = event
            if existing.event == event:
                return
            old = existing.event
            existing.event = event
            existing.set_classes(f"{event.kind} {event.role} {event.severity}")
            if existing.is_mounted:
                existing.query_one(".console-label", Static).update(existing.label_text())
                body = existing.query_one(".console-body")
                if isinstance(body, Static):
                    body.update(f"$ {event.text}" if event.kind == "command" else event.text)
                if old.details != event.details:
                    details = existing.query(".console-details")
                    if details:
                        details.first(Static).update(event.details or "")
                    elif event.details:
                        existing.mount(
                            Collapsible(
                                Static(event.details, markup=False, classes="console-details"),
                                title="Details",
                                collapsed=True,
                            )
                        )
                if follow:
                    self.call_after_refresh(self._follow_after_mount, self._follow_generation)
        else:
            self.entries.append(event)
            chunk = ConsoleChunk(event)
            self._chunks[key] = chunk
            if self.is_mounted:
                self.call_next(self._mount_chunk, chunk, follow, self._follow_generation)
        if len(self.entries) > MAX_CONSOLE_CHUNKS:
            evicted = self.entries[:EVICTION_BATCH]
            del self.entries[:EVICTION_BATCH]
            removed_height = 0
            for old in evicted:
                chunk = self._chunks.pop(self._key(old), None)
                if chunk is not None:
                    removed_height += (
                        chunk.outer_size.height
                        + chunk.styles.margin.top
                        + chunk.styles.margin.bottom
                    )
                    if chunk.is_mounted:
                        chunk.remove()
            if not follow:
                self.scroll_to(y=max(0, self.scroll_y - removed_height), animate=False, force=True)

    async def _mount_chunk(self, chunk: ConsoleChunk, follow: bool, generation: int) -> None:
        if chunk not in self._chunks.values():
            return
        await self.mount(chunk)
        if follow:
            self._follow_after_mount(generation)
            self.call_after_refresh(self._follow_after_mount, generation)

    def _follow_after_mount(self, generation: int) -> None:
        if generation != self._follow_generation or self._selection_owns_viewport():
            return
        # Textual's anchor uses negative scroll offsets for short content.
        # Keep short transcripts at zero; anchor only overflowing content.
        self.anchor(self.max_scroll_y > 0)
        self.scroll_end(animate=False, immediate=True)

    def _selection_owns_viewport(self) -> bool:
        if not self.is_mounted:
            return False
        if self._owns_selection():
            return True
        # The native selection start exists before the first drag creates a span.
        state = self.screen._select_state
        return bool(
            self.screen._selecting
            and state is not None
            and (state.start.container is self or self in state.start.container.ancestors)
        )

    def _suspend_follow(self) -> None:
        self._follow_generation += 1
        self.anchor(False)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1:
            self._suspend_follow()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._suspend_follow()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._suspend_follow()

    def action_copy(self) -> None:
        selected = self.screen.get_selected_text()
        if selected and self._owns_selection():
            copy_text(self.app, selected)
            self.app.notify("Copied", timeout=1)

    def _owns_selection(self) -> bool:
        return bool(self.screen.selections) and all(
            self in widget.ancestors for widget in self.screen.selections
        )

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button == 1:
            self.call_after_refresh(self.action_copy)

    def show_context_menu(self, chunk: ConsoleChunk, selection: str, x: int, y: int) -> None:
        self.app.push_screen(ConsoleContextMenu(chunk, selection, x, y))


class ChatInput(TextArea):
    """Multiline editor with explicit send, registry completion and draft history."""

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.load_text(value)

    @property
    def cursor_position(self) -> int:
        row, column = self.selection.end
        return sum(len(line) for line in self.text.splitlines(keepends=True)[:row]) + column

    @cursor_position.setter
    def cursor_position(self, position: int) -> None:
        prefix = self.text[: max(0, min(position, len(self.text)))]
        self.selection = Selection.cursor((prefix.count("\n"), len(prefix.rsplit("\n", 1)[-1])))

    def _chat_panel(self) -> ChatPanel | None:
        return next((widget for widget in self.ancestors if isinstance(widget, ChatPanel)), None)

    def render_line(self, y: int) -> Strip:
        line = super().render_line(y)
        panel = self._chat_panel()
        if (
            not self.has_focus
            or panel is None
            or not panel.suggestions
            or not self.selection.is_empty
        ):
            return line
        candidate = panel.suggestions[panel._suggestion_index][0]
        if not candidate.startswith(self.text) or self.cursor_position != len(self.text):
            return line
        cursor = self.cursor_screen_offset - self.content_region.offset
        remainder = candidate[len(self.text) :]
        if y != cursor.y or not remainder or cursor.x >= self.content_size.width:
            return line
        ghost = Strip([Segment(remainder, Style(color="#78dce8", dim=True))])
        end = min(self.content_size.width, cursor.x + ghost.cell_length)
        return Strip.join((line.crop(0, cursor.x), ghost.crop(0, end - cursor.x), line.crop(end)))

    async def _on_key(self, event: events.Key) -> None:
        panel = self._chat_panel()
        if panel is not None:
            handled = True
            if event.key == "shift+enter":
                self.insert("\n")
            elif event.key == "enter":
                panel.submit_input()
            elif event.key == "tab":
                handled = panel.accept_slash_completion()
            elif event.key in {"up", "down"}:
                delta = -1 if event.key == "up" else 1
                handled = panel.move_suggestion(delta) or panel.recall_history(delta)
            elif event.key == "escape":
                if panel.suggestions:
                    panel.dismiss_suggestions()
                else:
                    self.blur()
            else:
                handled = False
            if handled:
                event.prevent_default()
                event.stop()
                return
        await super()._on_key(event)

    def action_copy(self) -> None:
        if self.selected_text:
            copy_text(self.app, self.selected_text)

    def action_cut(self) -> None:
        if self.read_only:
            return
        start, end = self.selection
        result = (
            self._delete_cursor_line() if start == end else self._delete_via_keyboard(start, end)
        )
        if result is not None:
            copy_text(self.app, result.replaced_text)

    def action_paste(self) -> None:
        if not self.read_only:
            if result := self._replace_via_keyboard(paste_text(self.app), *self.selection):
                self.move_cursor(result.end_location)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button == 1:
            self.call_after_refresh(self._copy_mouse_selection)

    def _copy_mouse_selection(self) -> None:
        if self.selected_text:
            self.action_copy()
            self.app.notify("Copied", timeout=1)


class ChatPanel(Vertical):
    """One composer and transcript for messages, commands and logs."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class RerunRequested(Message):
        def __init__(self, command: str) -> None:
            super().__init__()
            self.command = command

    DEFAULT_CSS = """
    ChatPanel #chat-suggestions { height: auto; max-height: 5; padding: 0; border: none; }
    ChatPanel #chat-suggestions > .option-list--option-highlighted { color: #11151d; background: #78dce8; }
    ChatPanel #chat-slash-hint { height: 1; color: #78dce8; }
    """

    def __init__(
        self, *, enabled: bool = False, menu_def: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.enabled, self.menu_def = enabled, menu_def or {}
        self.transcript = ConsoleTranscript(id="chat-log")
        self.slash_hint = Static("", id="chat-slash-hint", markup=False)
        self.suggestion_list = OptionList(id="chat-suggestions")
        self.suggestion_list.can_focus = False
        self.suggestion_list.display = False
        self.suggestions: list[tuple[str, str]] = []
        self._suggestion_index = 0
        self._suggestion_text = ""
        self._dismissed_text: str | None = None
        self.history: list[str] = []
        self._history_index: int | None = None
        self._draft = ("", 0)
        self.input = ChatInput(
            id="chat-input",
            placeholder="Ask Halcyon or /command",
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            compact=True,
            highlight_cursor_line=False,
        )

    def compose(self) -> ComposeResult:
        yield self.transcript
        yield self.suggestion_list
        yield self.slash_hint
        with Horizontal(id="chat-input-row"):
            yield Static("›", id="chat-prompt", markup=False)
            yield self.input

    def on_mount(self) -> None:
        self.call_after_refresh(self.input.focus)

    def on_text_area_changed(self, message: TextArea.Changed) -> None:
        if message.text_area is self.input:
            self._refresh_slash_hint(self.input.value)

    def on_text_area_selection_changed(self, message: TextArea.SelectionChanged) -> None:
        if message.text_area is self.input:
            self._refresh_slash_hint(self.input.value)

    def on_option_list_option_selected(self, message: OptionList.OptionSelected) -> None:
        if message.option_list is self.suggestion_list:
            message.stop()
            self._suggestion_index = message.option_index
            self.accept_slash_completion()
            self.input.focus()

    def append_event(self, event: ConsoleEvent) -> None:
        self.transcript.append_event(event)

    write_event = append_event

    def _write_message(self, text: str, role: str, source: str) -> None:
        self.append_event(
            ConsoleEvent(event_id=uuid4().hex, kind="message", role=role, source=source, text=text)
        )

    def write_user(self, text: str) -> None:
        self._write_message(text, "user", "chat")

    def write_assistant(self, text: str) -> None:
        self._write_message(text, "assistant", "assistant")

    def write_codex(self, text: str) -> None:
        self._write_message(text, "client", "Codex")

    def write_system(self, text: str) -> None:
        self.append_event(
            ConsoleEvent(
                event_id=uuid4().hex, kind="log", role="system", source="Halcyon", text=text
            )
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_menu_def(self, menu_def: dict[str, Any]) -> None:
        self.menu_def = menu_def
        self._refresh_slash_hint(self.input.value)

    def slash_completion_for(self, text: str) -> SlashCompletion | None:
        return suggest_slash_completion(self.menu_def, text)

    def completed_slash_value(self, text: str) -> str | None:
        completion = self.slash_completion_for(text)
        return (
            None
            if completion is None
            else text + completion.remainder + (" " if completion.append_space else "")
        )

    def accept_slash_completion(self) -> bool:
        if "\n" in self.input.value or self.input.cursor_position != len(self.input.value):
            return False
        if self.suggestions:
            value = self.suggestions[self._suggestion_index][0] + " "
        else:
            value = self.completed_slash_value(self.input.value)
        if value is None:
            return False
        self.input.value = value
        self.input.cursor_position = len(value)
        self._refresh_slash_hint(value)
        return True

    def slash_group_options_for(self, text: str) -> tuple[str, ...]:
        resolved = resolve_slash_command(self.menu_def, text)
        return (
            tuple(f"{resolved.leaf_command} {shortcut}" for shortcut, _ in resolved.options)
            if resolved.status == "group"
            else ()
        )

    def submit_input(self) -> None:
        text = self.input.value.strip()
        if not text:
            return
        if safe_console_text(text) == text and (not self.history or self.history[-1] != text):
            self.history.append(text)
            del self.history[:-MAX_HISTORY]
        self._history_index = None
        self.input.value = ""
        self._refresh_slash_hint("")
        self.post_message(self.Submitted(text))

    def recall_history(self, delta: int) -> bool:
        row = self.input.selection.end[0]
        boundary = row == 0 if delta < 0 else row == self.input.text.count("\n")
        if not boundary or not self.history:
            return False
        if self._history_index is None:
            if delta > 0:
                return False
            self._draft = (self.input.value, self.input.cursor_position)
            self._history_index = len(self.history)
        self._history_index = max(0, min(len(self.history), self._history_index + delta))
        if self._history_index == len(self.history):
            self.input.value, position = self._draft
            self._history_index = None
        else:
            self.input.value = self.history[self._history_index]
            position = len(self.input.value)
        self.input.cursor_position = position
        self._refresh_slash_hint(self.input.value)
        return True

    def move_suggestion(self, delta: int) -> bool:
        if not self.suggestions:
            return False
        self._suggestion_index = (self._suggestion_index + delta) % len(self.suggestions)
        self.suggestion_list.highlighted = self._suggestion_index
        self._update_ghost()
        return True

    def dismiss_suggestions(self) -> None:
        self._dismissed_text = self.input.value
        self.suggestions = []
        self.suggestion_list.display = False
        self.slash_hint.update("")

    def _refresh_slash_hint(self, text: str) -> None:
        candidates: list[tuple[str, str]] = []
        if (
            text.startswith("/")
            and "\n" not in text
            and self.input.cursor_position == len(text)
            and text != self._dismissed_text
        ):
            body = text[1:]
            tokens = body.split()
            prefix = "" if body.endswith(" ") or not tokens else tokens.pop()
            level = build_slash_tree(self.menu_def)
            path: list[str] = []
            for token in tokens:
                node = level.get(token.lower())
                if node is None:
                    level = {}
                    break
                path.append(node.shortcut)
                level = node.children
            for token, node in sorted(level.items()):
                if token.startswith(prefix.lower()):
                    candidates.append(
                        ("/" + " ".join([*path, token]), node.description or node.label)
                    )
        if text != self._suggestion_text:
            self._suggestion_index = 0
        self._suggestion_text = text
        self.suggestions = candidates[:8]
        if self.is_mounted:
            self.suggestion_list.clear_options()
            self.suggestion_list.add_options(
                [
                    Option(Text(f"{command}  {description}"))
                    for command, description in self.suggestions
                ]
            )
            self.suggestion_list.highlighted = self._suggestion_index if self.suggestions else None
        self.suggestion_list.display = bool(self.suggestions)
        self._update_ghost()

    def _update_ghost(self) -> None:
        self.slash_hint.update(
            self.suggestions[self._suggestion_index][0] + "  [Tab]" if self.suggestions else ""
        )
        self.input.refresh()
