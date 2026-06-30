"""Textual entrypoint for Project Halcyon."""

from __future__ import annotations

import threading
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.widgets import DataTable, Footer, Header, Static

from halcyon.assistant import HalcyonAssistant, local_tool_executor
from halcyon.audio_bridge import RustAudioBridge
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.chat_widgets import ChatPanel
from halcyon.controls import SynthRack
from halcyon.keyboard import keyboard_text, scroll_target_for_note
from halcyon.logs import MidiEventLog
from halcyon.mcp_local import LocalControlServer
from halcyon.midi import MidiEvent, MidiInput, MidiUnavailable
from halcyon.midi import note_name as midi_note_name
from halcyon.preset_store import UserPresetStore
from halcyon.synth_params import PRESETS, SynthPatch


class VirtualKeyboard(Static):
    """A continuous full-spectrum MIDI keyboard surface."""

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

    def __init__(self, *, preset_store: UserPresetStore | None = None) -> None:
        super().__init__()
        self.midi = MidiInput()
        self.audio = RustAudioBridge()
        self.user_preset_store = preset_store or UserPresetStore.load()
        self.synth_patch = SynthPatch()
        self.assistant = HalcyonAssistant(self._execute_tool_from_worker)
        self.pressed_notes: set[int] = set()
        self.target_notes: set[int] = set()
        self.event_log = MidiEventLog(max_events=200)
        self.event_count = 0
        self.status = Static("Starting MIDI monitor...", id="status")
        self.audio_status = Static("Audio: starting Rust sidecar...", id="audio-status")
        self.synth_rack = SynthRack(
            self.synth_patch,
            user_presets=self.user_preset_store.presets,
            id="synth-rack",
        )
        self.chat_panel = ChatPanel(
            enabled=self.assistant.enabled,
            id="chat-panel",
        )
        self.local_control_server = LocalControlServer(self)
        self.keyboard = VirtualKeyboard(id="keyboard")
        self.keyboard_scroll = HorizontalScroll(id="keyboard-scroll")
        self.event_table = DataTable(id="event-log", zebra_stripes=True)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="app-shell"):
            yield Static("Project Halcyon", id="title")
            with Horizontal(id="workspace"):
                with Vertical(id="monitor-panel"):
                    yield Static("Input", id="input-title", classes="section-title")
                    yield self.status
                    yield Static("Audio sidecar", id="audio-title", classes="section-title")
                    yield self.audio_status
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
            yield Static("Synth", id="synth-title", classes="section-title")
            yield self.synth_rack
            yield Static("Assistant", id="chat-title", classes="section-title")
            yield self.chat_panel
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Project Halcyon"
        self.sub_title = "Textual MIDI logger"
        self.event_table.cursor_type = "none"
        self.event_table.add_columns(
            "Time", "Event", "Note", "Vel", "Ch", "Status", "Delta", "Source"
        )
        self.keyboard.update_keys(self.pressed_notes)
        self._start_audio()
        self._start_local_control_server()
        self._connect_midi()
        self.set_interval(0.02, self._poll_midi)
        self.set_interval(0.5, self._refresh_audio_status)

    def on_shutdown(self) -> None:
        self.local_control_server.stop()
        self.audio.close()
        self.midi.close()

    def action_reconnect(self) -> None:
        self.audio.all_notes_off()
        self._apply_dsp_patch(self.synth_patch.dsp_patch())
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

    def _start_audio(self) -> None:
        self.audio.start()
        self._apply_dsp_patch(self.synth_patch.dsp_patch())
        self._refresh_audio_status()

    def _start_local_control_server(self) -> None:
        try:
            host, port = self.local_control_server.start()
        except Exception as exc:
            self.chat_panel.write_system(f"MCP unavailable: {exc}")
            return
        self.chat_panel.write_system(f"MCP: http://{host}:{port}")

    def _refresh_audio_status(self) -> None:
        self.audio_status.update(self.audio.snapshot().text)

    def _poll_midi(self) -> None:
        if not self.midi.connected:
            return

        try:
            events = self.midi.poll()
        except Exception as exc:
            self.status.update(f"[error] MIDI polling failed: {exc}. Press r to reconnect.")
            self.audio.all_notes_off()
            self.midi.close()
            return

        for event in events:
            self._handle_event(event)

    def _handle_event(self, event: MidiEvent) -> None:
        if event.is_press:
            self.pressed_notes.add(event.note)
        elif event.is_release:
            self.pressed_notes.discard(event.note)

        self.audio.handle_midi_event(event)
        self.keyboard.update_keys(self.pressed_notes, self.target_notes)
        if event.is_press:
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
        scroll_x = scroll_target_for_note(
            note,
            current_scroll=int(self.keyboard_scroll.scroll_x),
            viewport_width=self.keyboard_scroll.size.width,
        )
        if scroll_x is not None:
            self.keyboard_scroll.scroll_to(x=scroll_x, animate=False, immediate=True)

    def on_synth_rack_param_changed(self, message: SynthRack.ParamChanged) -> None:
        self._apply_dsp_patch(message.dsp_patch)

    def on_synth_rack_preset_applied(self, message: SynthRack.PresetApplied) -> None:
        self._apply_dsp_patch(message.dsp_patch)
        self.chat_panel.write_system(f"Patch: {message.preset_name}")

    def on_synth_rack_preset_saved(self, message: SynthRack.PresetSaved) -> None:
        try:
            self.user_preset_store.save(message.preset_name, message.values)
        except ValueError as exc:
            self.chat_panel.write_system(f"Preset save failed: {exc}")
            return
        self.synth_rack.set_user_presets(self.user_preset_store.presets)
        self.chat_panel.write_system(f"Saved patch: {message.preset_name}")

    def on_chat_panel_submitted(self, message: ChatPanel.Submitted) -> None:
        self.chat_panel.write_user(message.text)
        if not self.assistant.enabled:
            self.chat_panel.write_assistant(
                "Assistant is offline. Set OPENAI_API_KEY to enable chat."
            )
            return
        self.chat_panel.write_system("Thinking")
        thread = threading.Thread(
            target=self._run_assistant,
            args=(message.text,),
            name="halcyon-assistant",
            daemon=True,
        )
        thread.start()

    def _apply_dsp_patch(self, dsp_patch: dict[str, float]) -> bool:
        return self.audio.apply_patch(dsp_patch)

    def synth_tool_state(self) -> dict[str, object]:
        audio = self.audio.snapshot()
        pressed = [
            {"note": note, "name": midi_note_name(note)} for note in sorted(self.pressed_notes)
        ]
        return {
            "patch": self.synth_patch.snapshot(),
            "audio": {
                "running": audio.running,
                "ready": audio.ready,
                "status": audio.text,
            },
            "presets": {
                "built_in": list(PRESETS),
                "user": self.user_preset_store.names(),
            },
            "pressed_notes": pressed,
            "recent_midi": list(self.event_log.summaries(16)),
        }

    def synth_tool_set_param(self, param_id: str, normalized_value: float) -> dict[str, object]:
        dsp_patch = self.synth_rack.apply_patch({param_id: normalized_value}, emit=False)
        self._apply_dsp_patch(dsp_patch)
        return self.synth_tool_state()

    def synth_tool_apply_patch(self, values: dict[str, float]) -> dict[str, object]:
        dsp_patch = self.synth_rack.apply_patch(values, emit=False)
        self._apply_dsp_patch(dsp_patch)
        return self.synth_tool_state()

    def synth_tool_apply_preset(self, preset_name: str) -> dict[str, object]:
        dsp_patch = self.synth_rack.apply_preset(preset_name, emit=False)
        self._apply_dsp_patch(dsp_patch)
        self.chat_panel.write_system(f"Patch: {preset_name}")
        return self.synth_tool_state()

    def synth_tool_save_preset(self, preset_name: str) -> dict[str, object]:
        self.user_preset_store.save(preset_name, self.synth_patch.values)
        self.synth_rack.set_user_presets(self.user_preset_store.presets)
        self.chat_panel.write_system(f"Saved patch: {preset_name}")
        return self.synth_tool_state()

    def synth_tool_all_notes_off(self) -> dict[str, object]:
        self.audio.all_notes_off()
        self.pressed_notes.clear()
        self.keyboard.update_keys(self.pressed_notes, self.target_notes)
        return self.synth_tool_state()

    def audio_visualization_snapshot(self) -> AudioVisualizationSnapshot:
        """Return the latest raw waveform sample window for future UI widgets."""

        return self.audio.visualization_snapshot()

    def _execute_tool_from_worker(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        if threading.current_thread() is threading.main_thread():
            return local_tool_executor(self)(name, arguments)
        return self.call_from_thread(lambda: local_tool_executor(self)(name, arguments))

    def _run_assistant(self, message: str) -> None:
        result = self.assistant.ask(message)
        self.call_from_thread(lambda: self.chat_panel.write_assistant(result.text))


def main() -> None:
    HalcyonApp().run()
