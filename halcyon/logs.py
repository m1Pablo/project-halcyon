"""Bounded MIDI event context for UI and assistant features."""

from __future__ import annotations

from collections import deque

from halcyon.midi import MidiEvent


def event_summary(event: MidiEvent) -> str:
    """Return a compact human-readable summary for an event."""

    return (
        f"{event.elapsed:.3f}s {event.event_type} {event.note_name} "
        f"({event.note}) vel={event.velocity} ch={event.channel} "
        f"status=0x{event.status:02X} delta={event.delta_time:.4f} "
        f"source={event.source}"
    )


class MidiEventLog:
    """A bounded in-memory MIDI event buffer."""

    def __init__(self, max_events: int = 200) -> None:
        if max_events < 1:
            raise ValueError("max_events must be at least 1")
        self.max_events = max_events
        self._events: deque[MidiEvent] = deque(maxlen=max_events)

    def __len__(self) -> int:
        return len(self._events)

    def append(self, event: MidiEvent) -> None:
        self._events.append(event)

    def clear(self) -> None:
        self._events.clear()

    def recent(self, limit: int | None = None) -> tuple[MidiEvent, ...]:
        if limit is None:
            return tuple(self._events)
        if limit < 1:
            return ()
        return tuple(self._events)[-limit:]

    def summaries(self, limit: int | None = None) -> tuple[str, ...]:
        return tuple(event_summary(event) for event in self.recent(limit))
