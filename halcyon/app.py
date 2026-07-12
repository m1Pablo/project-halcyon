"""Textual entrypoint for Project Halcyon."""

from __future__ import annotations

import threading
from typing import Any, Callable, ClassVar, TypeVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.widgets import DataTable, Footer, Header, Static

from halcyon.assistant import HalcyonAssistant, local_tool_executor
from halcyon.audio_bridge import RustAudioBridge
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.chat_widgets import ChatPanel
from halcyon.command_executor import (
    CommandExecutionError,
    execute_slash_command,
    prepare_slash_command,
)
from halcyon.controls import SynthRack
from halcyon.keyboard import keyboard_text, scroll_target_for_note
from halcyon.logs import MidiEventLog
from halcyon.menu import load_menu
from halcyon.mcp_local import LocalControlServer
from halcyon.mcp_local.run_tracker import RunTracker
from halcyon.mcp_tools import ToolExecutionError
from halcyon.midi import MidiEvent, MidiInput, MidiUnavailable
from halcyon.midi import note_name as midi_note_name
from halcyon.preset_store import UserPresetStore
from halcyon.slash_commands import iter_slash_leaf_commands, resolve_slash_command
from halcyon.synth_params import PRESETS, SynthPatch

T = TypeVar("T")


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
        self.menu_def = load_menu()
        self.assistant = HalcyonAssistant(
            menu_def=self.menu_def,
            context_provider=lambda: self._run_on_app_thread(self._assistant_context),
        )
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
            menu_def=self.menu_def,
            id="chat-panel",
        )
        self._run_tracker = RunTracker()
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
        if message.text.startswith("/"):
            self._execute_chat_slash_command(message.text)
            return
        self._submit_natural_chat(message.text)

    def _submit_natural_chat(self, text: str) -> None:
        self.chat_panel.write_system("Routing")
        thread = threading.Thread(
            target=self._run_assistant,
            args=(text,),
            name="halcyon-assistant",
            daemon=True,
        )
        thread.start()

    def _execute_chat_slash_command(self, command: str) -> bool:
        resolved = resolve_slash_command(self.menu_def, command)
        if resolved.status == "group":
            choices = ", ".join(
                f"{resolved.leaf_command} {shortcut}" for shortcut, _label in resolved.options
            )
            self.chat_panel.write_system(f"{resolved.message} {choices}")
            return False
        if resolved.status == "error":
            self.chat_panel.write_system(resolved.message)
            return False

        try:
            result = execute_slash_command(
                self,
                self.menu_def,
                command,
                tool_executor=lambda _host, name, arguments: self._execute_tool_from_worker(
                    name,
                    arguments,
                ),
            )
        except (CommandExecutionError, ToolExecutionError, ValueError, TypeError) as exc:
            self.chat_panel.write_system(f"Command failed: {exc}")
            return False

        self.chat_panel.write_codex(HalcyonApp._summarize_chat_command(command, result))
        return True

    @staticmethod
    def _summarize_chat_command(command: str, result: dict[str, Any]) -> str:
        if "error" in result:
            return f"{command}: {result['error']}"
        return f"Ran `{command}`."

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

    def _assistant_context(self) -> dict[str, Any]:
        return self.synth_tool_state()

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

    def mcp_local_list_commands(self) -> list[dict[str, Any]]:
        commands: list[dict[str, Any]] = []
        for command, payload in iter_slash_leaf_commands(self.menu_def):
            policy = self._mcp_command_policy(
                command,
                leaf_command=self._normalize_mcp_command(command).lower(),
                payload=payload,
            )
            commands.append(
                {
                    "command": command,
                    "usage": str(payload.get("usage") or command).strip(),
                    "description": str(payload.get("description", "")).strip(),
                    "single_flight": bool(policy["single_flight"]),
                    "parallel_ok": bool(policy["parallel_ok"]),
                    "concurrency_group": str(policy["concurrency_group"]),
                    "poll_after_s": int(policy["poll_after_s"]),
                    "expected_duration_s": int(policy["expected_duration_s"]),
                }
            )
        return commands

    def mcp_local_state(self) -> dict[str, Any]:
        return {
            "server": {
                "host": self.local_control_server.host,
                "port": self.local_control_server.port,
                "running": self.local_control_server.port is not None,
            },
            "tracker": self._run_tracker.get_state(),
            "synth": self.synth_tool_state(),
        }

    def mcp_local_run_slash(
        self,
        *,
        command: str,
        source: str,
        timeout_s: float | None = None,
    ) -> tuple[int, dict[str, Any]]:
        clean_command = self._normalize_mcp_command(command)
        source_key = self._normalize_mcp_source(source)
        if not clean_command.startswith("/"):
            return 400, {"error": "Slash command is required (must start with '/')."}

        try:
            invocation = prepare_slash_command(self.menu_def, clean_command)
        except CommandExecutionError as exc:
            return 400, {"error": str(exc)}

        policy = self._mcp_command_policy(
            clean_command,
            leaf_command=invocation.leaf_command,
            payload=invocation.payload,
        )
        if policy["single_flight"]:
            active = self._active_run_for_policy(policy)
            if active is not None:
                active_run_id = str(active.get("run_id", "")).strip()
                status = self._run_tracker.get_status(active_run_id) or dict(active)
                status["deduplicated"] = True
                status["deduplicated_run_id"] = active_run_id
                status["requested_command"] = clean_command
                status["deduplicated_reason"] = (
                    "A run in the same concurrency group is already active."
                )
                return 200, self._enrich_mcp_run_payload(status, policy=policy)

        timeout_value = self._coerce_mcp_timeout(timeout_s)
        run_id, _status = self._run_tracker.create_run(
            command=clean_command,
            source=source_key,
            timeout_s=timeout_value,
        )
        self._run_tracker.mark_running(run_id)
        self._record_run_event(run_id, "INFO", f"Started {clean_command}")

        try:
            result = execute_slash_command(
                self,
                self.menu_def,
                clean_command,
                tool_executor=lambda _host, name, arguments: self._execute_tool_from_worker(
                    name,
                    arguments,
                ),
            )
        except (CommandExecutionError, ToolExecutionError, ValueError, TypeError) as exc:
            error = str(exc)
            self._record_run_event(run_id, "ERROR", error)
            self._run_tracker.mark_failed(run_id, error=error)
            status = self._run_tracker.get_status(run_id) or {"run_id": run_id}
            return 400, self._enrich_mcp_run_payload(status, policy=policy)

        self._record_run_event(run_id, "INFO", f"Completed {clean_command}")
        self._run_tracker.mark_completed(run_id, success=True)
        status = self._run_tracker.get_status(run_id) or {"run_id": run_id}
        status["result"] = result
        status["effective_timeout_s"] = timeout_value
        return 200, self._enrich_mcp_run_payload(status, policy=policy)

    def mcp_local_run_status(self, run_id: str) -> dict[str, Any] | None:
        payload = self._run_tracker.get_status(run_id)
        if payload is None:
            return None
        return self._enrich_mcp_run_payload(payload)

    def mcp_local_run_logs(self, run_id: str) -> dict[str, Any] | None:
        payload = self._run_tracker.get_logs(run_id)
        if payload is None:
            return None
        return self._enrich_mcp_run_payload(payload)

    def audio_visualization_snapshot(self) -> AudioVisualizationSnapshot:
        """Return the latest raw waveform sample window for future UI widgets."""

        return self.audio.visualization_snapshot()

    def _execute_tool_from_worker(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        return self._run_on_app_thread(lambda: local_tool_executor(self)(name, arguments))

    def _run_assistant(self, message: str) -> None:
        result = self.assistant.ask(message)
        self._run_on_app_thread(lambda: self.chat_panel.write_assistant(result.text))
        if result.decision.action != "menu":
            return
        for command in result.decision.commands:
            self._run_on_app_thread(
                lambda command=command: self._execute_chat_slash_command(command)
            )

    def _run_on_app_thread(self, callback: Callable[[], T]) -> T:
        if threading.current_thread() is threading.main_thread():
            return callback()
        return self.call_from_thread(callback)

    @staticmethod
    def _normalize_mcp_command(command: str) -> str:
        return " ".join(str(command or "").strip().split())

    @staticmethod
    def _normalize_mcp_source(source: str) -> str:
        source_key = str(source or "").strip().lower() or "llm"
        if source_key not in {"codex", "claude", "llm", "debugger"}:
            return "llm"
        return source_key

    @staticmethod
    def _coerce_mcp_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return default

    @staticmethod
    def _coerce_mcp_int(value: Any, default: int, *, minimum: int = 0, maximum: int = 3600) -> int:
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _coerce_mcp_timeout(value: float | None) -> float:
        try:
            parsed = float(value) if value is not None else 60.0
        except (TypeError, ValueError):
            parsed = 60.0
        return max(5.0, parsed)

    def _resolve_leaf_command_payload(
        self, command: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        normalized = self._normalize_mcp_command(command).lower()
        if not normalized.startswith("/"):
            return None, None
        best_command: str | None = None
        best_payload: dict[str, Any] | None = None
        best_len = -1
        for leaf_command, payload in iter_slash_leaf_commands(self.menu_def):
            leaf_norm = self._normalize_mcp_command(leaf_command).lower()
            if normalized == leaf_norm or normalized.startswith(f"{leaf_norm} "):
                if len(leaf_norm) > best_len:
                    best_command = leaf_norm
                    best_payload = payload
                    best_len = len(leaf_norm)
        return best_command, best_payload

    def _mcp_command_policy(
        self,
        command: str,
        *,
        leaf_command: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_mcp_command(command).lower()
        resolved_leaf = leaf_command
        resolved_payload = payload
        if resolved_leaf is None or resolved_payload is None:
            matched_leaf, matched_payload = self._resolve_leaf_command_payload(command)
            resolved_leaf = resolved_leaf or matched_leaf
            resolved_payload = resolved_payload or matched_payload

        command_key = resolved_leaf or normalized
        payload_dict = resolved_payload or {}
        parallel_ok = self._coerce_mcp_bool(payload_dict.get("mcp_parallel_ok"), True)
        single_flight = self._coerce_mcp_bool(payload_dict.get("mcp_single_flight"), True)
        if not parallel_ok:
            single_flight = True

        group_raw = str(payload_dict.get("mcp_concurrency_group", "")).strip().lower()
        return {
            "normalized_command": normalized,
            "leaf_command": resolved_leaf,
            "command_key": command_key,
            "concurrency_group": group_raw or command_key,
            "single_flight": single_flight,
            "parallel_ok": parallel_ok,
            "poll_after_s": self._coerce_mcp_int(
                payload_dict.get("mcp_poll_after_s"), default=5, minimum=1, maximum=60
            ),
            "expected_duration_s": self._coerce_mcp_int(
                payload_dict.get("mcp_expected_duration_s"),
                default=0,
                minimum=0,
                maximum=3600,
            ),
        }

    def _active_run_for_policy(self, policy: dict[str, Any]) -> dict[str, Any] | None:
        target_group = str(policy.get("concurrency_group", "")).strip().lower()
        if not target_group:
            return None
        for entry in self._run_tracker.get_state().get("active_runs", []):
            if not isinstance(entry, dict):
                continue
            command = str(entry.get("command", "")).strip()
            run_policy = self._mcp_command_policy(command)
            run_group = str(run_policy.get("concurrency_group", "")).strip().lower()
            if run_group == target_group:
                return entry
        return None

    def _enrich_mcp_run_payload(
        self,
        payload: dict[str, Any],
        *,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = str(payload.get("command", "")).strip()
        if policy is None:
            policy = self._mcp_command_policy(command)
        payload.setdefault("single_flight", bool(policy["single_flight"]))
        payload.setdefault("parallel_ok", bool(policy["parallel_ok"]))
        payload.setdefault("command_key", str(policy["command_key"]))
        payload.setdefault("concurrency_group", str(policy["concurrency_group"]))
        payload.setdefault("poll_after_s", int(policy["poll_after_s"]))
        expected_duration_s = int(policy.get("expected_duration_s") or 0)
        if expected_duration_s > 0:
            payload.setdefault("expected_duration_s", expected_duration_s)
        status = str(payload.get("status", "")).strip().lower()
        if status in {"queued", "running"}:
            payload.setdefault("do_not_rerun", True)
            payload.setdefault(
                "guidance",
                "Run is still in progress. Poll this same run_id until it reaches "
                "completed/failed/cancelled/timeout; do not submit a duplicate command.",
            )
        else:
            payload.setdefault("do_not_rerun", False)
        return payload

    def _record_run_event(self, run_id: str, level: str, message: str) -> None:
        self._run_tracker.record_log_event(
            {
                "run_id": run_id,
                "level": level,
                "message": message,
            }
        )


def main() -> None:
    HalcyonApp().run()
