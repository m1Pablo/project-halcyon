"""Subprocess bridge from the Textual app to the Rust audio sidecar."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from halcyon.audio_viz import AudioVisualizationReader, AudioVisualizationSnapshot
from halcyon.midi import MidiEvent, audio_command_for_event

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIO_MANIFEST = PROJECT_ROOT / "halcyon-audio" / "Cargo.toml"


@dataclass(frozen=True)
class AudioBridgeSnapshot:
    """A small status snapshot safe for Textual to render."""

    running: bool
    ready: bool
    text: str


class RustAudioBridge:
    """Manage the Rust oscillator sidecar process and its JSON command stream."""

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        process_factory: Callable[..., Any] | None = None,
        visualization: AudioVisualizationReader | None = None,
    ) -> None:
        self.command = list(command) if command is not None else default_sidecar_command()
        self._process_factory = process_factory or subprocess.Popen
        self.visualization = visualization or AudioVisualizationReader()
        self._process: Any | None = None
        self._lock = threading.Lock()
        self._reader_threads: list[threading.Thread] = []
        self._ready = False
        self._last_status = "Audio: stopped"
        self._last_error: str | None = None
        self._current_patch: dict[str, float] = {}

    @property
    def process(self) -> Any | None:
        return self._process

    def start(self) -> bool:
        if self.is_running:
            return True

        with self._lock:
            self._ready = False
            self._last_status = "Audio: starting Rust sidecar..."
            self._last_error = None

        try:
            self.visualization.start()
            self._process = self._process_factory(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self._sidecar_env(),
            )
        except OSError as exc:
            self.visualization.close()
            with self._lock:
                self._last_status = "Audio: unavailable"
                self._last_error = str(exc)
            return False

        self._start_reader(self._process.stdout, "stdout")
        self._start_reader(self._process.stderr, "stderr")
        self._replay_patch()
        return True

    @property
    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    def snapshot(self) -> AudioBridgeSnapshot:
        process = self._process
        running = self.is_running
        with self._lock:
            ready = self._ready
            status = self._last_status
            error = self._last_error

        if process is not None and not running and ready:
            ready = False
            status = f"Audio: stopped (exit {process.poll()})"
        elif process is not None and not running and status.startswith("Audio: starting"):
            status = f"Audio: stopped (exit {process.poll()})"

        if error and not ready:
            status = f"{status}\n{error[-180:]}"
        return AudioBridgeSnapshot(running=running, ready=ready, text=status)

    def visualization_snapshot(self) -> AudioVisualizationSnapshot:
        return self.visualization.snapshot()

    def note_on(self, note: int, velocity: int, channel: int) -> bool:
        return self.send(
            {
                "type": "note_on",
                "note": int(note),
                "velocity": int(velocity),
                "channel": int(channel),
            }
        )

    def note_off(self, note: int, channel: int) -> bool:
        return self.send({"type": "note_off", "note": int(note), "channel": int(channel)})

    def all_notes_off(self) -> bool:
        return self.send({"type": "all_notes_off"})

    def set_param(self, name: str, value: float) -> bool:
        value = float(value)
        with self._lock:
            self._current_patch[name] = value
        return self.send({"type": "set_param", "name": name, "value": value})

    def apply_patch(self, values: dict[str, float]) -> bool:
        success = True
        for name, value in values.items():
            success = self.set_param(name, value) and success
        return success

    def shutdown(self) -> bool:
        return self.send({"type": "shutdown"})

    def handle_midi_event(self, event: MidiEvent) -> bool:
        command = audio_command_for_event(event)
        if command is None:
            return False
        return self.send(command)

    def send(self, command: dict[str, str | int | float]) -> bool:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            return False

        line = json.dumps(command, separators=(",", ":")) + "\n"
        try:
            process.stdin.write(line)
            process.stdin.flush()
        except OSError as exc:
            with self._lock:
                self._last_status = "Audio: write failed"
                self._last_error = str(exc)
                self._ready = False
            return False
        return True

    def close(self) -> None:
        process = self._process
        if process is None:
            self.visualization.close()
            return

        if process.poll() is None:
            self.all_notes_off()
            self.shutdown()
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)

        with self._lock:
            self._ready = False
            self._last_status = "Audio: stopped"
        self.visualization.close()
        self._process = None

    def _start_reader(self, stream: TextIO | None, stream_name: str) -> None:
        if stream is None:
            return
        thread = threading.Thread(
            target=self._read_stream,
            args=(stream, stream_name),
            daemon=True,
        )
        thread.start()
        self._reader_threads.append(thread)

    def _read_stream(self, stream: TextIO, stream_name: str) -> None:
        for raw_line in stream:
            line = raw_line.strip()
            if not line:
                continue
            if stream_name == "stdout":
                self._handle_stdout_line(line)
            else:
                with self._lock:
                    self._last_error = line

    def _handle_stdout_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            with self._lock:
                self._last_status = f"Audio: {line[-180:]}"
            return

        message_type = payload.get("type")
        if message_type == "status":
            status = str(payload.get("status", "running"))
            sample_rate = payload.get("sample_rate")
            channels = payload.get("channels")
            suffix = ""
            if sample_rate and channels:
                suffix = f" ({sample_rate} Hz, {channels} ch)"
            with self._lock:
                self._ready = status == "audio_ready"
                self._last_status = f"Audio: {status}{suffix}"
                self._last_error = None
        elif message_type == "error":
            with self._lock:
                self._ready = False
                self._last_status = "Audio: sidecar error"
                self._last_error = str(payload.get("message", "unknown error"))

    def _replay_patch(self) -> None:
        with self._lock:
            patch = dict(self._current_patch)
        for name, value in patch.items():
            self.send({"type": "set_param", "name": name, "value": value})

    def _sidecar_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("HALCYON_VIZ_SOCKET", self.visualization.socket_path)
        env.setdefault("HALCYON_VIZ_SAMPLES", str(self.visualization.sample_count))
        return env


def default_sidecar_command() -> list[str]:
    override = os.environ.get("HALCYON_AUDIO_SIDECAR")
    if override:
        return shlex.split(override)
    return [
        "uv",
        "run",
        "--",
        "cargo",
        "run",
        "--quiet",
        "--manifest-path",
        str(AUDIO_MANIFEST),
        "--bin",
        "halcyon-audio-sidecar",
        "--",
    ]
