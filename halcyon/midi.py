"""MIDI input normalization for Project Halcyon."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

NOTE_OFF = 0x80
NOTE_ON = 0x90
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
PREFERRED_PORT_TERMS = ("lpk25", "akai", "wireless")

try:
    import rtmidi
except ImportError:  # pragma: no cover - exercised only when dependency is absent
    rtmidi = None  # type: ignore[assignment]


class MidiUnavailable(RuntimeError):
    """Raised when MIDI input cannot be initialized."""


@dataclass(frozen=True)
class MidiEvent:
    """A normalized MIDI event the Textual app can render."""

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

    @property
    def is_press(self) -> bool:
        return self.event_type == "press"

    @property
    def is_release(self) -> bool:
        return self.event_type == "release"


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
    elif command == NOTE_OFF or (command == NOTE_ON and velocity == 0):
        event_type = "release"
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
    )


def audio_command_for_event(event: MidiEvent) -> dict[str, int | str] | None:
    """Translate a normalized MIDI event into the Rust sidecar IPC schema."""

    if event.is_press:
        return {
            "type": "note_on",
            "note": event.note,
            "velocity": event.velocity,
            "channel": event.channel,
        }
    if event.is_release:
        return {
            "type": "note_off",
            "note": event.note,
            "channel": event.channel,
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
            )
            if event is not None:
                events.append(event)
        return events

    def close(self) -> None:
        midi_in = self._midi_in
        self._midi_in = None
        self.port_index = None
        self.port_name = None
        if midi_in is None:
            return
        try:
            midi_in.close_port()
        finally:
            try:
                midi_in.delete()
            except AttributeError:
                pass
