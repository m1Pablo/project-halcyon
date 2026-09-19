"""Subprocess bridge from the Textual app to the Rust audio sidecar."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from halcyon.audio_viz import AudioVisualizationReader, AudioVisualizationSnapshot
from halcyon.midi import MidiEvent, MidiOrigin, audio_command_for_event

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIO_MANIFEST = PROJECT_ROOT / "halcyon-audio" / "Cargo.toml"
SAFETY_COMMANDS = {"note_off", "all_notes_off", "shutdown"}
TERMINAL_ADMISSION_CODES = {
    "AUDIO_SAFETY_ADMISSION_FAILED",
    "AUDIO_SAFETY_ADMISSION_STOPPED",
    "AUDIO_SAFETY_ADMISSION_TIMEOUT",
    "INVALID_AUDIO_COMMAND",
}
MAX_PENDING_NORMAL_COMMANDS = 2048
MAX_PENDING_SAFETY_COMMANDS = 64
MAX_RETAINED_ADMISSIONS = MAX_PENDING_NORMAL_COMMANDS + MAX_PENDING_SAFETY_COMMANDS
SIDECAR_DENIED_ENVIRONMENT = frozenset({"OPENAI_API_KEY"})


@dataclass(frozen=True)
class CommandAdmission:
    """Structured sidecar queue-admission outcome."""

    command_id: int
    status: str
    queue: str
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class CommandSubmission:
    """Immediate pipe-submission result; admission may still be pending."""

    command_id: int | None
    submitted: bool
    status: str
    safety: bool
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class CleanupSubmission:
    """Structured owner-cleanup submission without claiming sidecar admission."""

    commands: tuple[CommandSubmission, ...]

    @property
    def command_ids(self) -> tuple[int, ...]:
        return tuple(
            command.command_id for command in self.commands if command.command_id is not None
        )


@dataclass(frozen=True)
class CleanupStatus:
    """Aggregate polled admission state for a prior cleanup submission."""

    status: str
    command_ids: tuple[int, ...]
    accepted: int
    pending: int
    rejected: int


@dataclass(frozen=True)
class _VoiceMutation:
    action: str
    event: MidiEvent | None = None


@dataclass(frozen=True)
class AudioBridgeSnapshot:
    """A small status snapshot safe for Textual to render."""

    running: bool
    ready: bool
    text: str
    fatal: bool = False
    pending_commands: int = 0


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
        self._write_lock = threading.RLock()
        self._reader_threads: list[threading.Thread] = []
        self._generation = 0
        self._ready = False
        self._last_status = "Audio: stopped"
        self._last_error: str | None = None
        self._current_patch: dict[str, float] = {}
        self._next_command_id = 1
        self._pending_commands: OrderedDict[int, str] = OrderedDict()
        self._admissions: OrderedDict[int, CommandAdmission] = OrderedDict()
        self._pending_voice_mutations: dict[int, _VoiceMutation] = {}
        self._fatal = False
        self._transport_terminal = False
        self._active_events: dict[int, MidiEvent] = {}

    @property
    def process(self) -> Any | None:
        return self._process

    def start(self) -> bool:
        with self._write_lock:
            with self._lock:
                transport_terminal = self._transport_terminal
            if self.is_running and not transport_terminal:
                return True

            with self._lock:
                self._generation += 1
                generation = self._generation

            previous = self._process
            if previous is not None and previous.stdin is not None:
                try:
                    previous.stdin.close()
                except (OSError, ValueError):
                    pass
            if previous is not None:
                self.visualization.close()
            self._process = None

            with self._lock:
                if self._pending_commands:
                    self._fail_pending_locked(
                        code="AUDIO_RESTARTED",
                        message="audio sidecar restarted before command admission",
                    )
                self._ready = False
                self._last_status = "Audio: starting Rust sidecar..."
                self._last_error = None
                self._fatal = False
                self._transport_terminal = False
                self._pending_commands.clear()
                self._pending_voice_mutations.clear()
                self._active_events.clear()

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
                self._process = None
                with self._lock:
                    self._last_status = "Audio: unavailable"
                    self._last_error = str(exc)
                return False

            self._start_reader(self._process.stdout, "stdout", generation)
            self._start_reader(self._process.stderr, "stderr", generation)
            self._replay_patch()
            return True

    @property
    def is_running(self) -> bool:
        process = self._process
        with self._lock:
            transport_terminal = self._transport_terminal
        return process is not None and process.poll() is None and not transport_terminal

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
            fatal = self._fatal
            pending_commands = len(self._pending_commands)

        if process is not None and not running and ready:
            ready = False
            status = f"Audio: stopped (exit {process.poll()})"
        elif process is not None and not running and status.startswith("Audio: starting"):
            status = f"Audio: stopped (exit {process.poll()})"

        if error and not ready:
            status = f"{status}\n{error[-180:]}"
        return AudioBridgeSnapshot(
            running=running,
            ready=ready,
            text=status,
            fatal=fatal,
            pending_commands=pending_commands,
        )

    def visualization_snapshot(self) -> AudioVisualizationSnapshot:
        return self.visualization.snapshot()

    def note_on(
        self,
        note: int,
        velocity: int,
        channel: int,
        *,
        voice_id: int | None = None,
    ) -> bool:
        command: dict[str, str | int | float] = {
            "type": "note_on",
            "note": int(note),
            "velocity": int(velocity),
            "channel": int(channel),
        }
        if voice_id is not None:
            command["voice_id"] = int(voice_id)
        return self.send(command)

    def note_off(self, note: int, channel: int, *, voice_id: int | None = None) -> bool:
        command: dict[str, str | int | float] = {
            "type": "note_off",
            "note": int(note),
            "channel": int(channel),
        }
        if voice_id is not None:
            command["voice_id"] = int(voice_id)
        return self.send(command)

    def all_notes_off(self) -> bool:
        """Legacy compatibility: report pipe submission, not sidecar admission."""

        return self.all_notes_off_submission().submitted

    def all_notes_off_submission(self) -> CommandSubmission:
        return self._submit(
            {"type": "all_notes_off"},
            mutation=_VoiceMutation("clear"),
        )

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
        """Legacy compatibility: report pipe submission, not sidecar admission."""

        return self.handle_midi_event_submission(event).submitted

    def handle_midi_event_submission(self, event: MidiEvent) -> CommandSubmission:
        command = audio_command_for_event(event)
        if command is None:
            return CommandSubmission(
                command_id=None,
                submitted=False,
                status="rejected",
                safety=False,
                code="UNSUPPORTED_MIDI_EVENT",
                message="event does not map to an audio command",
            )
        action = "add" if event.is_press else "remove"
        return self._submit(command, mutation=_VoiceMutation(action, event))

    def active_voice_ids(self, *, origin: MidiOrigin | str | None = None) -> tuple[int, ...]:
        wanted_origin = MidiOrigin(origin) if origin is not None else None
        with self._lock:
            return tuple(
                voice_id
                for voice_id, event in self._active_events.items()
                if wanted_origin is None or event.origin is wanted_origin
            )

    def pending_voice_ids(self, *, action: str | None = None) -> tuple[int, ...]:
        """Return pending voice mutations without conflating them with admitted voices."""

        with self._lock:
            return tuple(
                mutation.event.voice_id
                for mutation in self._pending_voice_mutations.values()
                if mutation.event is not None and (action is None or mutation.action == action)
            )

    def release_origin(
        self,
        origin: MidiOrigin | str,
        *,
        source_port: str | None = None,
        session_id: str | None = None,
    ) -> CleanupSubmission:
        """Submit owner note-offs and return command IDs for admission polling."""

        wanted_origin = MidiOrigin(origin)
        with self._lock:
            selected_by_id = {
                event.voice_id: event
                for event in self._active_events.values()
                if event.origin is wanted_origin
                and (source_port is None or event.source_port == source_port)
                and (session_id is None or event.session_id == session_id)
            }
            pending_removals = {
                mutation.event.voice_id
                for mutation in self._pending_voice_mutations.values()
                if mutation.action == "remove" and mutation.event is not None
            }
            for mutation in self._pending_voice_mutations.values():
                event = mutation.event
                if mutation.action != "add" or event is None:
                    continue
                if event.origin is not wanted_origin:
                    continue
                if source_port is not None and event.source_port != source_port:
                    continue
                if session_id is not None and event.session_id != session_id:
                    continue
                selected_by_id.setdefault(event.voice_id, event)
            selected = tuple(
                event
                for voice_id, event in selected_by_id.items()
                if voice_id not in pending_removals
            )

        submissions = tuple(
            self._submit(
                {
                    "type": "note_off",
                    "note": event.note,
                    "channel": event.channel,
                    "voice_id": event.voice_id,
                },
                mutation=_VoiceMutation("remove", event),
            )
            for event in selected
        )
        return CleanupSubmission(submissions)

    def release_halcyon_voices(
        self,
        *,
        source_port: str | None = None,
        session_id: str | None = None,
    ) -> CleanupSubmission:
        return self.release_origin(
            MidiOrigin.HALCYON,
            source_port=source_port,
            session_id=session_id,
        )

    def admission(self, command_id: int) -> CommandAdmission | None:
        with self._lock:
            return self._admissions.get(command_id)

    def cleanup_status(self, cleanup: CleanupSubmission) -> CleanupStatus:
        accepted = 0
        pending = 0
        rejected = 0
        for command in cleanup.commands:
            if not command.submitted or command.command_id is None:
                rejected += 1
                continue
            admission = self.admission(command.command_id)
            if admission is None:
                pending += 1
            elif admission.status == "accepted":
                accepted += 1
            else:
                rejected += 1

        if not cleanup.commands:
            status = "empty"
        elif pending:
            status = "pending"
        elif rejected and accepted:
            status = "partial"
        elif rejected:
            status = "rejected"
        else:
            status = "accepted"
        return CleanupStatus(
            status=status,
            command_ids=cleanup.command_ids,
            accepted=accepted,
            pending=pending,
            rejected=rejected,
        )

    def send(self, command: dict[str, str | int | float]) -> bool:
        """Legacy compatibility: report pipe submission, not sidecar admission."""

        return self.send_submission(command).submitted

    def send_submission(self, command: dict[str, str | int | float]) -> CommandSubmission:
        return self._submit(command)

    def _submit(
        self,
        command: dict[str, str | int | float],
        *,
        mutation: _VoiceMutation | None = None,
    ) -> CommandSubmission:
        command_type = str(command.get("type", ""))
        is_safety = command_type in SAFETY_COMMANDS or (
            command_type == "note_on" and command.get("velocity") == 0
        )
        pending_type = "note_off" if is_safety and command_type == "note_on" else command_type
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                with self._lock:
                    if process is not None:
                        self._ready = False
                        self._fatal = True
                        self._last_status = "Audio: sidecar stopped"
                        self._last_error = "Rust audio sidecar is not running"
                        self._fail_pending_locked(
                            code="AUDIO_NOT_RUNNING",
                            message=self._last_error,
                        )
                return CommandSubmission(
                    command_id=None,
                    submitted=False,
                    status="rejected",
                    safety=is_safety,
                    code="AUDIO_NOT_RUNNING",
                    message="Rust audio sidecar is not running",
                )

            with self._lock:
                if self._transport_terminal:
                    return CommandSubmission(
                        command_id=None,
                        submitted=False,
                        status="rejected",
                        safety=is_safety,
                        code="AUDIO_SIDECAR_EOF",
                        message=self._last_error
                        or "audio sidecar acknowledgement stream is unavailable",
                    )
                if self._fatal and not is_safety:
                    return CommandSubmission(
                        command_id=None,
                        submitted=False,
                        status="rejected",
                        safety=False,
                        code="AUDIO_FATAL",
                        message="normal commands are blocked until the audio sidecar restarts",
                    )
                pending_safety = sum(
                    pending_type in SAFETY_COMMANDS
                    for pending_type in self._pending_commands.values()
                )
                pending_normal = len(self._pending_commands) - pending_safety
                queue_full = (is_safety and pending_safety >= MAX_PENDING_SAFETY_COMMANDS) or (
                    not is_safety and pending_normal >= MAX_PENDING_NORMAL_COMMANDS
                )
                if queue_full:
                    self._last_status = "Audio: command backpressure"
                    queue_name = "safety" if is_safety else "normal"
                    self._last_error = (
                        f"too many {queue_name} commands are awaiting sidecar admission"
                    )
                    self._ready = False
                    self._fatal = True
                    return CommandSubmission(
                        command_id=None,
                        submitted=False,
                        status="rejected",
                        safety=is_safety,
                        code="AUDIO_BACKPRESSURE",
                        message=self._last_error,
                    )
                command_id = self._next_command_id
                self._next_command_id += 1

            payload = dict(command)
            payload["command_id"] = command_id
            line = json.dumps(payload, separators=(",", ":")) + "\n"
            with self._lock:
                # Register before writing: a fast sidecar acknowledgement must not race
                # ahead and leave an already-admitted command stuck as pending.
                self._pending_commands[command_id] = pending_type
                if mutation is not None:
                    self._pending_voice_mutations[command_id] = mutation
            try:
                process.stdin.write(line)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                with self._lock:
                    self._pending_commands.pop(command_id, None)
                    self._pending_voice_mutations.pop(command_id, None)
                    self._last_status = "Audio: write failed"
                    self._last_error = str(exc)
                    self._ready = False
                    self._fatal = True
                    admission = CommandAdmission(
                        command_id=command_id,
                        status="rejected",
                        queue="safety" if is_safety else "normal",
                        code="AUDIO_WRITE_FAILED",
                        message=str(exc),
                    )
                    self._remember_admission_locked(admission)
                    self._fail_pending_locked(
                        code="AUDIO_WRITE_FAILED",
                        message=str(exc),
                    )
                return CommandSubmission(
                    command_id=command_id,
                    submitted=False,
                    status="rejected",
                    safety=is_safety,
                    code="AUDIO_WRITE_FAILED",
                    message=str(exc),
                )
            with self._lock:
                immediate = self._admissions.get(command_id)
            if immediate is None:
                return CommandSubmission(
                    command_id=command_id,
                    submitted=True,
                    status="pending",
                    safety=is_safety,
                )
            return CommandSubmission(
                command_id=command_id,
                submitted=True,
                status=immediate.status,
                safety=is_safety,
                code=immediate.code,
                message=immediate.message,
            )

    def close(self) -> None:
        with self._write_lock:
            process = self._process
            if process is None:
                self.visualization.close()
                return

            with self._lock:
                # Invalidate readers before intentional stream closure. Cleanup and
                # shutdown still use the old process, but their admissions are swept
                # explicitly below instead of being accepted by stale reader threads.
                self._generation += 1

            if process.poll() is None:
                self.release_halcyon_voices()
                self.shutdown()
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except (OSError, ValueError):
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
                self._fail_pending_locked(
                    code="AUDIO_BRIDGE_CLOSED",
                    message="audio bridge closed before command admission",
                )
                self._active_events.clear()
            self.visualization.close()
            self._process = None

    def _start_reader(self, stream: TextIO | None, stream_name: str, generation: int) -> None:
        if stream is None:
            return
        thread = threading.Thread(
            target=self._read_stream,
            args=(stream, stream_name, generation),
            daemon=True,
        )
        thread.start()
        self._reader_threads.append(thread)

    def _read_stream(self, stream: TextIO, stream_name: str, generation: int) -> None:
        stream_error: str | None = None
        try:
            for raw_line in stream:
                with self._lock:
                    if generation != self._generation:
                        return
                line = raw_line.strip()
                if not line:
                    continue
                if stream_name == "stdout":
                    self._handle_stdout_line(line, generation)
                else:
                    self._handle_stderr_line(line, generation)
        except (OSError, ValueError) as exc:
            stream_error = str(exc)

        if stream_name == "stdout":
            self._handle_stdout_end(generation, stream_error=stream_error)

    def _handle_stderr_line(self, line: str, generation: int | None = None) -> None:
        with self._lock:
            if not self._is_current_generation_locked(generation):
                return
            self._last_error = line

    def _handle_stdout_end(
        self,
        generation: int,
        *,
        stream_error: str | None = None,
    ) -> None:
        with self._lock:
            if not self._is_current_generation_locked(generation):
                return
            process = self._process
            exit_code = process.poll() if process is not None else None
            if stream_error:
                message = f"Rust audio sidecar stdout failed: {stream_error}"
            elif exit_code is None:
                message = "Rust audio sidecar stdout closed unexpectedly"
            else:
                message = f"Rust audio sidecar exited with code {exit_code}"
            self._ready = False
            self._last_status = "Audio: sidecar stopped"
            self._last_error = message
            self._fatal = True
            self._transport_terminal = True
            self._fail_pending_locked(code="AUDIO_SIDECAR_EOF", message=message)

    def _handle_stdout_line(self, line: str, generation: int | None = None) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            with self._lock:
                if not self._is_current_generation_locked(generation):
                    return
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
                if not self._is_current_generation_locked(generation):
                    return
                self._ready = status == "audio_ready" and not self._fatal
                self._last_status = f"Audio: {status}{suffix}"
                if not self._fatal:
                    self._last_error = None
        elif message_type == "error":
            with self._lock:
                if not self._is_current_generation_locked(generation):
                    return
                self._ready = False
                self._last_status = "Audio: sidecar error"
                self._last_error = str(payload.get("message", "unknown error"))
                self._fatal = True
                self._fail_pending_locked(
                    code=str(payload.get("code") or "AUDIO_SIDECAR_ERROR"),
                    message=self._last_error,
                )
        elif message_type == "admission":
            try:
                command_id = int(payload["command_id"])
            except (KeyError, TypeError, ValueError):
                with self._lock:
                    if not self._is_current_generation_locked(generation):
                        return
                    self._ready = False
                    self._last_status = "Audio: invalid admission response"
                    self._last_error = "sidecar omitted a valid command_id"
                    self._fatal = True
                    self._fail_pending_locked(
                        code="INVALID_ADMISSION_RESPONSE",
                        message=self._last_error,
                    )
                return
            admission = CommandAdmission(
                command_id=command_id,
                status=str(payload.get("status", "rejected")),
                queue=str(payload.get("queue", "unknown")),
                code=str(payload["code"]) if payload.get("code") is not None else None,
                message=(str(payload["message"]) if payload.get("message") is not None else None),
            )
            with self._lock:
                if not self._is_current_generation_locked(generation):
                    return
                self._pending_commands.pop(command_id, None)
                mutation = self._pending_voice_mutations.pop(command_id, None)
                self._remember_admission_locked(admission)
                if admission.status == "accepted":
                    if mutation is not None:
                        self._apply_voice_mutation_locked(mutation)
                else:
                    self._ready = False
                    self._last_status = "Audio: command backpressure"
                    self._last_error = admission.message or admission.code or "command rejected"
                    self._fatal = True
                    if admission.code in TERMINAL_ADMISSION_CODES:
                        self._fail_pending_locked(
                            code=admission.code or "AUDIO_SIDECAR_TERMINATED",
                            message=self._last_error,
                        )

    def _is_current_generation_locked(self, generation: int | None) -> bool:
        return generation is None or generation == self._generation

    def _apply_voice_mutation_locked(self, mutation: _VoiceMutation) -> None:
        if mutation.action == "clear":
            self._active_events.clear()
        elif mutation.action == "add" and mutation.event is not None:
            self._active_events[mutation.event.voice_id] = mutation.event
        elif mutation.action == "remove" and mutation.event is not None:
            self._active_events.pop(mutation.event.voice_id, None)

    def _fail_pending_locked(self, *, code: str, message: str) -> None:
        for command_id, command_type in tuple(self._pending_commands.items()):
            admission = CommandAdmission(
                command_id=command_id,
                status="rejected",
                queue="safety" if command_type in SAFETY_COMMANDS else "normal",
                code=code,
                message=message,
            )
            self._remember_admission_locked(admission)
        self._pending_commands.clear()
        self._pending_voice_mutations.clear()

    def _remember_admission_locked(self, admission: CommandAdmission) -> None:
        self._admissions[admission.command_id] = admission
        while len(self._admissions) > MAX_RETAINED_ADMISSIONS:
            self._admissions.popitem(last=False)

    def _replay_patch(self) -> None:
        with self._lock:
            patch = dict(self._current_patch)
        for name, value in patch.items():
            self.send({"type": "set_param", "name": name, "value": value})

    def _sidecar_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for name in tuple(env):
            if name.upper() in SIDECAR_DENIED_ENVIRONMENT:
                env.pop(name, None)
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
