"""MIDI input normalization for Project Halcyon."""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic_ns, perf_counter
from typing import Any

NOTE_OFF = 0x80
NOTE_ON = 0x90
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
PREFERRED_PORT_TERMS = ("lpk25", "akai", "wireless")
EVENT_TYPE_ALIASES = {
    "press": "press",
    "release": "release",
    "note_on": "press",
    "note_off": "release",
}

try:
    import rtmidi
except ImportError:  # pragma: no cover - exercised only when dependency is absent
    rtmidi = None  # type: ignore[assignment]


class MidiUnavailable(RuntimeError):
    """Raised when MIDI input cannot be initialized."""


class MidiOrigin(StrEnum):
    """Semantic producer of a MIDI event."""

    HUMAN = "human"
    HALCYON = "halcyon"


class VoiceIdAllocator:
    """Allocate process-local, nonzero unsigned 64-bit voice identities."""

    _MAX_U64 = (1 << 64) - 1

    def __init__(self, *, start: int = 1) -> None:
        if not 1 <= start <= self._MAX_U64:
            raise ValueError("voice ID start must be a nonzero u64")
        self._next = start
        self._lock = threading.Lock()

    def allocate(self) -> int:
        with self._lock:
            voice_id = self._next
            self._next = 1 if voice_id == self._MAX_U64 else voice_id + 1
        return voice_id


_VOICE_IDS = VoiceIdAllocator(start=(uuid.uuid4().int & ((1 << 63) - 1)) or 1)


def allocate_voice_id() -> int:
    """Return a nonzero u64 suitable for crossing the Rust boundary."""

    return _VOICE_IDS.allocate()


VoiceKey = tuple[MidiOrigin, str, int, int]


class MidiVoiceTracker:
    """Pair note releases to the oldest active note instance for one adapter."""

    def __init__(self, allocator: VoiceIdAllocator | None = None) -> None:
        self._allocator = allocator or _VOICE_IDS
        self._active: dict[VoiceKey, deque[int]] = defaultdict(deque)

    def note_on(
        self,
        *,
        origin: MidiOrigin | str,
        source_port: str,
        channel: int,
        note: int,
    ) -> int:
        key = (MidiOrigin(origin), source_port, channel, note)
        voice_id = self._allocator.allocate()
        self._active[key].append(voice_id)
        return voice_id

    def note_off(
        self,
        *,
        origin: MidiOrigin | str,
        source_port: str,
        channel: int,
        note: int,
    ) -> tuple[int, bool]:
        key = (MidiOrigin(origin), source_port, channel, note)
        voices = self._active.get(key)
        if not voices:
            return self._allocator.allocate(), False
        voice_id = voices.popleft()
        if not voices:
            self._active.pop(key, None)
        return voice_id, True

    def active_voice_ids(
        self,
        *,
        origin: MidiOrigin | str | None = None,
        source_port: str | None = None,
    ) -> tuple[int, ...]:
        wanted_origin = MidiOrigin(origin) if origin is not None else None
        return tuple(
            voice_id
            for (voice_origin, voice_source, _channel, _note), voices in self._active.items()
            if (wanted_origin is None or voice_origin is wanted_origin)
            and (source_port is None or voice_source == source_port)
            for voice_id in voices
        )

    def clear(self) -> None:
        self._active.clear()


@dataclass(frozen=True)
class MidiEvent:
    """One immutable normalized runtime MIDI event.

    The original display fields remain positional and required for compatibility. New
    runtime identity fields have safe defaults so existing UI/test adapters continue to
    construct the same type while migrations move all producers onto the event bus.
    """

    event_type: str
    note: int
    note_name: str
    velocity: int
    channel: int
    status: int
    raw_message: tuple[int, ...]
    delta_time: float
    source: str
    elapsed: float
    event_id: str = ""
    sequence: int = 0
    session_id: str = "app"
    lesson_id: str | None = None
    lesson_event_id: str | None = None
    playback_id: str | None = None
    attempt_id: str | None = None
    voice_id: int = 0
    origin: MidiOrigin = MidiOrigin.HUMAN
    received_monotonic_ns: int = 0
    scheduled_monotonic_ns: int | None = None
    dispatched_monotonic_ns: int = 0
    delta_us: int | None = None
    matched_voice: bool = True

    def __post_init__(self) -> None:
        try:
            legacy_event_type = EVENT_TYPE_ALIASES[self.event_type]
        except KeyError as exc:
            valid = ", ".join(EVENT_TYPE_ALIASES)
            raise ValueError(f"event_type must be one of: {valid}") from exc
        if legacy_event_type == "press" and self.velocity == 0:
            legacy_event_type = "release"
        object.__setattr__(self, "event_type", legacy_event_type)
        if not self.event_id:
            object.__setattr__(self, "event_id", str(uuid.uuid4()))
        if self.voice_id == 0:
            object.__setattr__(self, "voice_id", allocate_voice_id())
        if not 1 <= self.voice_id <= VoiceIdAllocator._MAX_U64:
            raise ValueError("voice_id must be a nonzero u64")
        if not 0 <= self.note <= 127:
            raise ValueError("note must be between 0 and 127")
        if not 0 <= self.velocity <= 127:
            raise ValueError("velocity must be between 0 and 127")
        if not 1 <= self.channel <= 16:
            raise ValueError("channel must be between 1 and 16")
        if not 0 <= self.status <= 255:
            raise ValueError("status must be between 0 and 255")
        object.__setattr__(self, "origin", MidiOrigin(self.origin))
        if self.received_monotonic_ns == 0:
            object.__setattr__(self, "received_monotonic_ns", monotonic_ns())
        if self.delta_us is None:
            object.__setattr__(self, "delta_us", max(0, round(self.delta_time * 1_000_000)))

    @property
    def is_press(self) -> bool:
        return self.event_type == "press"

    @property
    def is_release(self) -> bool:
        return self.event_type == "release"

    @property
    def source_port(self) -> str:
        """Canonical name for the backward-compatible ``source`` field."""

        return self.source

    @property
    def midi_event_type(self) -> str:
        """Return the canonical runtime spelling: note_on or note_off."""

        return "note_on" if self.is_press else "note_off"

    @property
    def legacy_event_type(self) -> str:
        """Return the Textual compatibility spelling: press or release."""

        return self.event_type


# Explicit canonical name while retaining the repository's established MidiEvent import.
NormalizedMidiEvent = MidiEvent


def note_name(note: int) -> str:
    """Return a conventional note name for a MIDI note number."""

    octave = (note // 12) - 1
    return f"{NOTE_NAMES[note % 12]}{octave}"


def normalize_message(
    message: list[int] | tuple[int, ...],
    delta_time: float,
    *,
    source: str,
    elapsed: float,
    origin: MidiOrigin | str = MidiOrigin.HUMAN,
    session_id: str = "app",
    voice_tracker: MidiVoiceTracker | None = None,
    received_ns: int | None = None,
) -> MidiEvent | None:
    """Normalize an rtmidi message into a note press/release event."""

    if len(message) < 3:
        return None

    status = int(message[0])
    command = status & 0xF0
    channel = (status & 0x0F) + 1
    note = int(message[1])
    velocity = int(message[2])

    if command == NOTE_ON and velocity > 0:
        event_type = "press"
        if voice_tracker is None:
            voice_id = allocate_voice_id()
        else:
            voice_id = voice_tracker.note_on(
                origin=origin,
                source_port=source,
                channel=channel,
                note=note,
            )
        matched_voice = True
    elif command == NOTE_OFF or (command == NOTE_ON and velocity == 0):
        event_type = "release"
        if voice_tracker is None:
            voice_id, matched_voice = allocate_voice_id(), False
        else:
            voice_id, matched_voice = voice_tracker.note_off(
                origin=origin,
                source_port=source,
                channel=channel,
                note=note,
            )
    else:
        return None

    return MidiEvent(
        event_type=event_type,
        note=note,
        note_name=note_name(note),
        velocity=velocity,
        channel=channel,
        status=status,
        raw_message=tuple(int(part) for part in message),
        delta_time=float(delta_time),
        source=source,
        elapsed=float(elapsed),
        session_id=session_id,
        voice_id=voice_id,
        origin=MidiOrigin(origin),
        received_monotonic_ns=received_ns or monotonic_ns(),
        matched_voice=matched_voice,
    )


def audio_command_for_event(event: MidiEvent) -> dict[str, int | str] | None:
    """Translate a normalized MIDI event into the Rust sidecar IPC schema."""

    if event.is_press:
        return {
            "type": "note_on",
            "note": event.note,
            "velocity": event.velocity,
            "channel": event.channel,
            "voice_id": event.voice_id,
        }
    if event.is_release:
        return {
            "type": "note_off",
            "note": event.note,
            "channel": event.channel,
            "voice_id": event.voice_id,
        }
    return None


def choose_preferred_port(
    ports: list[str], preferred_terms: tuple[str, ...] = PREFERRED_PORT_TERMS
) -> int | None:
    """Choose the best MIDI input port, preferring the Akai LPK25 Wireless."""

    if not ports:
        return None

    lowered_ports = [port.lower() for port in ports]
    for term in preferred_terms:
        for index, port in enumerate(lowered_ports):
            if term in port:
                return index
    return 0


class MidiInput:
    """Small polling wrapper around python-rtmidi."""

    def __init__(self) -> None:
        self._midi_in: Any | None = None
        self.port_index: int | None = None
        self.port_name: str | None = None
        self._started_at = perf_counter()
        self._voices = MidiVoiceTracker()

    @property
    def connected(self) -> bool:
        return self._midi_in is not None and self.port_name is not None

    def list_ports(self) -> list[str]:
        if rtmidi is None:
            raise MidiUnavailable("python-rtmidi is not installed.")
        midi_in = rtmidi.MidiIn()
        try:
            return list(midi_in.get_ports())
        finally:
            try:
                midi_in.delete()
            except AttributeError:
                pass

    def connect(self, port_index: int | None = None) -> str:
        if rtmidi is None:
            raise MidiUnavailable("python-rtmidi is not installed.")

        self.close()
        ports = self.list_ports()
        selected = choose_preferred_port(ports) if port_index is None else port_index
        if selected is None:
            raise MidiUnavailable("No MIDI input ports are available.")
        if selected < 0 or selected >= len(ports):
            raise MidiUnavailable(f"MIDI input port index {selected} is out of range.")

        midi_in = rtmidi.MidiIn()
        midi_in.ignore_types(sysex=False, timing=False, active_sense=True)
        midi_in.open_port(selected)

        self._midi_in = midi_in
        self.port_index = selected
        self.port_name = ports[selected]
        self._started_at = perf_counter()
        self._voices.clear()
        return self.port_name

    def poll(self, *, max_messages: int = 64) -> list[MidiEvent]:
        if self._midi_in is None or self.port_name is None:
            return []

        events: list[MidiEvent] = []
        for _ in range(max_messages):
            message = self._midi_in.get_message()
            if message is None:
                break

            raw_message, delta_time = message
            elapsed = perf_counter() - self._started_at
            event = normalize_message(
                raw_message,
                delta_time,
                source=self.port_name,
                elapsed=elapsed,
                origin=MidiOrigin.HUMAN,
                voice_tracker=self._voices,
            )
            if event is not None:
                events.append(event)
        return events

    def close(self) -> None:
        midi_in = self._midi_in
        self._midi_in = None
        self.port_index = None
        self.port_name = None
        self._voices.clear()
        if midi_in is None:
            return
        try:
            midi_in.close_port()
        finally:
            try:
                midi_in.delete()
            except AttributeError:
                pass
