"""Textual entrypoint for Project Halcyon."""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.widgets import DataTable, Footer, Header, Static

from halcyon.keyboard import key_left, keyboard_text
from halcyon.logs import MidiEventLog
from halcyon.midi import MidiEvent, MidiInput, MidiUnavailable


class VirtualKeyboard(Static):
    """A continuous 25-key keyboard surface for the LPK25 range."""

    def update_keys(
        self,
        pressed_notes: set[int],
        target_notes: set[int] | frozenset[int] = frozenset(),
    ) -> None:
        self.update(keyboard_text(pressed_notes, target_notes))


class HalcyonApp(App[None]):
    """Project Halcyon MIDI monitor."""

    CSS_PATH: ClassVar[str] = "styles.tcss"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("r", "reconnect", "Reconnect"),
        Binding("c", "clear_log", "Clear"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.midi = MidiInput()
        self.pressed_notes: set[int] = set()
        self.target_notes: set[int] = set()
        self.event_log = MidiEventLog(max_events=200)
        self.event_count = 0
        self.status = Static("Starting MIDI monitor...", id="status")
        self.keyboard = VirtualKeyboard(id="keyboard")
        self.keyboard_scroll = HorizontalScroll(id="keyboard-scroll")
        self.event_table = DataTable(id="event-log", zebra_stripes=True)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="app-shell"):
            yield Static("Project Halcyon", id="title")
            with Horizontal(id="workspace"):
                with Vertical(id="monitor-panel"):
                    yield self.status
                with Vertical(id="log-panel"):
                    yield Static("MIDI event log", id="log-title", classes="section-title")
                    yield self.event_table
            yield Static(
                "Akai LPK25 Wireless",
                id="keyboard-title",
                classes="section-title",
            )
            with self.keyboard_scroll:
                yield self.keyboard
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Project Halcyon"
        self.sub_title = "Textual MIDI logger"
        self.event_table.cursor_type = "none"
        self.event_table.add_columns(
            "Time", "Event", "Note", "Vel", "Ch", "Status", "Delta", "Source"
        )
        self.keyboard.update_keys(self.pressed_notes)
        self._connect_midi()
        self.set_interval(0.02, self._poll_midi)

    def on_shutdown(self) -> None:
        self.midi.close()

    def action_reconnect(self) -> None:
        self._connect_midi()

    def action_clear_log(self) -> None:
        self.event_table.clear()
        self.event_log.clear()
        self.event_count = 0

    def _connect_midi(self) -> None:
        try:
            ports = self.midi.list_ports()
            port = self.midi.connect()
        except MidiUnavailable as exc:
            self.status.update(f"[disconnected] {exc} Press r to retry.")
            return
        except Exception as exc:
            self.status.update(f"[error] MIDI connection failed: {exc}. Press r to retry.")
            return

        port_list = ", ".join(ports) if ports else "none"
        self.status.update(f"Input: {port}\nAvailable: {port_list}")

    def _poll_midi(self) -> None:
        if not self.midi.connected:
            return

        try:
            events = self.midi.poll()
        except Exception as exc:
            self.status.update(f"[error] MIDI polling failed: {exc}. Press r to reconnect.")
            self.midi.close()
            return

        for event in events:
            self._handle_event(event)

    def _handle_event(self, event: MidiEvent) -> None:
        if event.is_press:
            self.pressed_notes.add(event.note)
        elif event.is_release:
            self.pressed_notes.discard(event.note)

        self.keyboard.update_keys(self.pressed_notes, self.target_notes)
        self._scroll_keyboard_to(event.note)
        self.event_log.append(event)
        self.event_count += 1
        self.event_table.add_row(
            f"{event.elapsed:8.3f}s",
            event.event_type,
            f"{event.note_name} ({event.note})",
            str(event.velocity),
            str(event.channel),
            f"0x{event.status:02X}",
            f"{event.delta_time:.4f}",
            event.source,
            key=str(self.event_count),
        )
        if len(self.event_table.rows) > self.event_log.max_events:
            first_key = next(iter(self.event_table.rows.keys()))
            self.event_table.remove_row(first_key)
        self.event_table.move_cursor(row=len(self.event_table.rows) - 1)

    def _scroll_keyboard_to(self, note: int) -> None:
        left = key_left(note)
        if left is None:
            return

        viewport_width = self.keyboard_scroll.size.width
        scroll_x = max(0, left - max(8, viewport_width // 2))
        self.keyboard_scroll.scroll_to(x=scroll_x, animate=False, immediate=True)


def main() -> None:
    HalcyonApp().run()
