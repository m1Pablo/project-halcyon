"""Bounded MIDI event context for UI and assistant features."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from halcyon.midi import MidiEvent, MidiOrigin

MAX_LOG_EVENTS = 200
MAX_QUERY_EVENTS = 128


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
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or not 1 <= max_events <= MAX_LOG_EVENTS
        ):
            raise ValueError(f"max_events must be between 1 and {MAX_LOG_EVENTS}")
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

    def query(
        self,
        *,
        origin: MidiOrigin | str | None = None,
        session_id: str | None = None,
        after_sequence: int | None = None,
        limit: int = MAX_QUERY_EVENTS,
    ) -> MidiEventQuery:
        """Return a bounded ascending internal event page.

        The cursor is the last bus sequence previously observed. A cursor older
        than the retained buffer is reported as expired instead of silently
        presenting an incomplete history as complete.
        """

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= MAX_QUERY_EVENTS:
            raise ValueError(f"limit must be between 1 and {MAX_QUERY_EVENTS}")
        if after_sequence is not None:
            if (
                isinstance(after_sequence, bool)
                or not isinstance(after_sequence, int)
                or after_sequence < 0
            ):
                raise ValueError("after_sequence must be a non-negative integer")
        wanted_origin = MidiOrigin(origin) if origin is not None else None
        if session_id is not None:
            if not isinstance(session_id, str):
                raise TypeError("session_id must be a string or None")
            if not session_id or len(session_id) > 128:
                raise ValueError("session_id must contain 1 through 128 characters")

        retained = tuple(self._events)
        sequenced = tuple(event for event in retained if event.sequence > 0)
        oldest_sequence = sequenced[0].sequence if sequenced else None
        cursor = 0 if after_sequence is None else after_sequence
        cursor_expired = bool(
            after_sequence is not None
            and oldest_sequence is not None
            and cursor < oldest_sequence - 1
        )
        matching = tuple(
            event
            for event in retained
            if event.sequence > cursor
            and (wanted_origin is None or event.origin is wanted_origin)
            and (session_id is None or event.session_id == session_id)
        )
        page = matching[:limit]
        return MidiEventQuery(
            events=page,
            next_sequence=page[-1].sequence if page else cursor,
            truncated=len(matching) > len(page),
            cursor_expired=cursor_expired,
        )


@dataclass(frozen=True, slots=True)
class MidiEventQuery:
    """One bounded internal page from :class:`MidiEventLog`."""

    events: tuple[MidiEvent, ...]
    next_sequence: int
    truncated: bool
    cursor_expired: bool
