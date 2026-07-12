"""Compact Textual chat controls for the Halcyon assistant."""

from __future__ import annotations

from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, RichLog, Static

from halcyon.slash_commands import (
    SlashCompletion,
    resolve_slash_command,
    suggest_slash_completion,
)


class ChatInput(Input):
    """Single-line chat input for Halcyon prompts."""

    def _on_key(self, event: events.Key) -> None:
        if event.key == "tab":
            panel = self._chat_panel()
            if panel is not None and panel.accept_slash_completion():
                event.prevent_default()
                event.stop()
                return
        super()._on_key(event)

    def _chat_panel(self) -> ChatPanel | None:
        widget = self.parent
        while widget is not None:
            if isinstance(widget, ChatPanel):
                return widget
            widget = widget.parent
        return None


class ChatPanel(Vertical):
    """Assistant transcript plus prompt row."""

    class Submitted(Message):
        """Posted when the user submits a chat prompt."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(
        self,
        *,
        enabled: bool = False,
        menu_def: dict[str, Any] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.enabled = enabled
        self.menu_def = menu_def or {}
        self.transcript = RichLog(id="chat-log", markup=True, wrap=True, highlight=False)
        self.slash_hint = Static("", id="chat-slash-hint")
        self.input = ChatInput(
            id="chat-input",
            placeholder="Ask Halcyon",
            max_length=800,
        )
        self.send_button = Button("Send", id="chat-send", variant="primary")

    def compose(self) -> ComposeResult:
        yield self.transcript
        yield self.slash_hint
        with Horizontal(id="chat-input-row"):
            yield Static("Ask", id="chat-prompt")
            yield self.input
            yield self.send_button

    def on_mount(self) -> None:
        if self.enabled:
            self.write_system("Assistant ready")
        else:
            self.write_system("Assistant offline")
        self.call_after_refresh(self.input.focus)

    def on_input_submitted(self, message: Input.Submitted) -> None:
        if message.input is not self.input:
            return
        message.stop()
        self._submit()

    def on_input_changed(self, message: Input.Changed) -> None:
        if message.input is not self.input:
            return
        self._refresh_slash_hint(message.value)

    def on_button_pressed(self, message: Button.Pressed) -> None:
        if message.button is not self.send_button:
            return
        message.stop()
        self._submit()

    def write_user(self, text: str) -> None:
        self.transcript.write(f"[bold #8ecae6]You[/]: {text}")

    def write_assistant(self, text: str) -> None:
        self.transcript.write(f"[bold #95d5b2]Halcyon[/]: {text}")

    def write_codex(self, text: str) -> None:
        self.transcript.write(f"[bold #f0b429]Codex[/]: {text}")

    def write_system(self, text: str) -> None:
        self.transcript.write(f"[#a7b1bc]{text}[/]")

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_menu_def(self, menu_def: dict[str, Any]) -> None:
        self.menu_def = menu_def
        self._refresh_slash_hint(self.input.value)

    def slash_completion_for(self, text: str) -> SlashCompletion | None:
        return suggest_slash_completion(self.menu_def, text)

    def completed_slash_value(self, text: str) -> str | None:
        completion = self.slash_completion_for(text)
        if completion is None:
            return None
        value = text + completion.remainder
        if completion.append_space:
            value += " "
        return value

    def accept_slash_completion(self) -> bool:
        value = self.completed_slash_value(self.input.value)
        if value is None:
            return False

        self.input.value = value
        self.input.cursor_position = len(value)
        self._refresh_slash_hint(value)
        return True

    def slash_group_options_for(self, text: str) -> tuple[str, ...]:
        resolved = resolve_slash_command(self.menu_def, text)
        if resolved.status != "group":
            return ()
        return tuple(f"{resolved.leaf_command} {shortcut}" for shortcut, _label in resolved.options)

    def _submit(self) -> None:
        text = self.input.value.strip()
        if not text:
            return
        self.input.value = ""
        self._refresh_slash_hint("")
        self.post_message(self.Submitted(text))

    def _refresh_slash_hint(self, text: str) -> None:
        hint = ""
        if text.startswith("/"):
            completion = self.slash_completion_for(text)
            if completion is not None:
                completed = text + completion.remainder
                if completion.append_space:
                    completed += " "
                hint = completed
            else:
                options = self.slash_group_options_for(text)
                if options:
                    hint = "  ".join(options)
        self.slash_hint.update(hint)
