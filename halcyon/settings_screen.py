"""Persistent modal surface for Halcyon's sound settings."""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from halcyon.controls import SynthRack


class SoundSettingsScreen(ModalScreen[None]):
    """Own the complete lifecycle of the persistent sound-settings modal."""

    AUTO_FOCUS = "#close-settings"
    BINDINGS = [Binding("escape", "close", "Close", show=False, priority=True)]

    def __init__(self, synth_rack: SynthRack) -> None:
        super().__init__(id="sound-settings-screen")
        self.synth_rack = synth_rack
        self._size_class = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-drawer"):
            with Horizontal(id="settings-header"):
                with Vertical(id="settings-heading"):
                    yield Static("SOUND SETTINGS", classes="panel-title")
                    yield Static(
                        "Shape the instrument without leaving the performance stage.",
                        classes="settings-help",
                    )
                yield Button("Close  Esc", id="close-settings", compact=True)
            with ScrollableContainer(id="settings-scroll"):
                yield self.synth_rack

    def on_mount(self) -> None:
        self._apply_size_class(self.size.width, self.size.height)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_size_class(event.size.width, event.size.height)

    def on_click(self, event: events.Click) -> None:
        """Dismiss only when Textual identifies this screen as the click target."""
        if event.widget is self:
            event.stop()
            self.dismiss()

    def on_button_pressed(self, message: Button.Pressed) -> None:
        if message.button.id == "close-settings":
            message.stop()
            self.action_close()

    def action_close(self) -> None:
        self.dismiss()

    def _apply_size_class(self, width: int, height: int) -> None:
        if width < 110 or height < 36:
            size_class = "-compact"
        elif width < 150 or height < 48:
            size_class = "-medium"
        else:
            size_class = "-wide"
        if size_class == self._size_class:
            return
        for candidate in ("-wide", "-medium", "-compact"):
            self.set_class(candidate == size_class, candidate)
        self._size_class = size_class
