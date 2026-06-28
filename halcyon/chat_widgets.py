"""Compact Textual chat controls for the Halcyon assistant."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, RichLog, Static


class ChatInput(Input):
    """Single-line chat input for Halcyon prompts."""


class ChatPanel(Vertical):
    """Assistant transcript plus prompt row."""

    class Submitted(Message):
        """Posted when the user submits a chat prompt."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, *, enabled: bool = False, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.enabled = enabled
        self.transcript = RichLog(id="chat-log", markup=True, wrap=True, highlight=False)
        self.input = ChatInput(
            id="chat-input",
            placeholder="Ask Halcyon",
            max_length=800,
        )
        self.send_button = Button("Send", id="chat-send", variant="primary")

    def compose(self) -> ComposeResult:
        yield self.transcript
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

    def on_button_pressed(self, message: Button.Pressed) -> None:
        if message.button is not self.send_button:
            return
        message.stop()
        self._submit()

    def write_user(self, text: str) -> None:
        self.transcript.write(f"[bold #8ecae6]You[/]: {text}")

    def write_assistant(self, text: str) -> None:
        self.transcript.write(f"[bold #95d5b2]Halcyon[/]: {text}")

    def write_system(self, text: str) -> None:
        self.transcript.write(f"[#a7b1bc]{text}[/]")

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def _submit(self) -> None:
        text = self.input.value.strip()
        if not text:
            return
        self.input.value = ""
        self.post_message(self.Submitted(text))
