"""Ordered, deduplicating ingress for all runtime MIDI events."""

from __future__ import annotations

import threading
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from time import monotonic_ns
from typing import Literal, Protocol, runtime_checkable

from halcyon.midi import (
    NOTE_OFF,
    NOTE_ON,
    MidiEvent,
    MidiOrigin,
    MidiVoiceTracker,
    NormalizedMidiEvent,
    allocate_voice_id,
    normalize_message,
    note_name,
)

MidiConsumer = Callable[[NormalizedMidiEvent], object]
VoiceOwnerKey = tuple[MidiOrigin, str, str, int, int]
ReceiptState = Literal["pending", "accepted", "rejected_unsafe", "failed_safe"]

_RECEIPT_STATES: frozenset[str] = frozenset(
    {"pending", "accepted", "rejected_unsafe", "failed_safe"}
)
_MAX_RECEIPT_CODE_CHARS = 80
_MAX_RECEIPT_MESSAGE_CHARS = 500
_MAX_CONSUMER_ERROR_CHARS = 500


@dataclass(frozen=True, slots=True)
class ReceiptPoll:
    """Immutable bounded snapshot returned by a nonblocking dispatch receipt.

    ``code`` and ``message`` are normalized to at most 80 and 500 characters.
    Constructing a snapshot with any state outside :data:`ReceiptState` fails.
    """

    state: ReceiptState
    code: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.state not in _RECEIPT_STATES:
            raise ValueError(f"unsupported receipt state: {self.state!r}")
        object.__setattr__(
            self,
            "code",
            _bounded_optional_text(self.code, limit=_MAX_RECEIPT_CODE_CHARS, field="code"),
        )
        object.__setattr__(
            self,
            "message",
            _bounded_optional_text(
                self.message,
                limit=_MAX_RECEIPT_MESSAGE_CHARS,
                field="message",
            ),
        )


@runtime_checkable
class DispatchReceipt(Protocol):
    """Thread-safe receipt whose poll operation never waits for admission.

    Implementations may be polled from a different thread than the consumer that
    created them, but ``poll`` must return immediately with one bounded snapshot.
    """

    def poll(self) -> ReceiptPoll: ...


@dataclass(frozen=True, slots=True)
class _ConsumerRegistration:
    consumer: MidiConsumer
    receipt_required: bool


@dataclass(frozen=True)
class ActiveVoice:
    """Source-aware ownership information for one live note instance."""

    voice_id: int
    origin: MidiOrigin
    source_port: str
    session_id: str
    note: int
    channel: int
    lesson_id: str | None
    playback_id: str | None

    @property
    def owner_key(self) -> VoiceOwnerKey:
        return (self.origin, self.source_port, self.session_id, self.channel, self.note)


@dataclass(frozen=True)
class DispatchResult:
    """Bounded result from one synchronous app-thread dispatch.

    ``receipts`` retains only runtime-valid receipts returned by consumers
    registered with ``receipt_required=True``. ``required_receipt_count`` counts
    every required consumer invoked for an accepted, nonduplicate event,
    including consumers that raise or violate the receipt protocol. Therefore
    ``len(receipts) == required_receipt_count`` precisely indicates that every
    required consumer supplied a valid receipt. Deduplicated dispatches invoke
    no consumers and report an empty receipt set and a zero required count.
    """

    accepted: bool
    deduplicated: bool
    event: NormalizedMidiEvent
    consumer_count: int
    consumer_errors: tuple[str, ...] = ()
    receipts: tuple[DispatchReceipt, ...] = ()
    required_receipt_count: int = 0


class MidiEventBus:
    """Assign sequence order and fan one event out to registered consumers once."""

    def __init__(
        self,
        *,
        max_seen_event_ids: int = 4096,
        clock: Callable[[], int] = monotonic_ns,
        owner_thread_id: int | None = None,
    ) -> None:
        if max_seen_event_ids < 1:
            raise ValueError("max_seen_event_ids must be at least 1")
        self._max_seen_event_ids = max_seen_event_ids
        self._clock = clock
        self._owner_thread_id = owner_thread_id or threading.get_ident()
        self._next_consumer_id = 1
        self._sequence = 0
        self._consumers: OrderedDict[int, _ConsumerRegistration] = OrderedDict()
        self._seen: OrderedDict[str, NormalizedMidiEvent] = OrderedDict()
        self._active: OrderedDict[int, ActiveVoice] = OrderedDict()
        self._refcounts: Counter[VoiceOwnerKey] = Counter()

    def subscribe(self, consumer: MidiConsumer, *, receipt_required: bool = False) -> int:
        """Register an ordered consumer and return its unsubscribe token."""

        self._assert_owner_thread()
        if not callable(consumer):
            raise TypeError("consumer must be callable")
        if not isinstance(receipt_required, bool):
            raise TypeError("receipt_required must be a bool")
        token = self._next_consumer_id
        self._next_consumer_id += 1
        self._consumers[token] = _ConsumerRegistration(
            consumer=consumer,
            receipt_required=receipt_required,
        )
        return token

    def unsubscribe(self, token: int) -> bool:
        self._assert_owner_thread()
        return self._consumers.pop(token, None) is not None

    def publish(self, event: NormalizedMidiEvent) -> DispatchResult:
        """Publish once; duplicates perform no state or consumer/audio work."""

        self._assert_owner_thread()
        previous = self._seen.get(event.event_id)
        if previous is not None:
            return DispatchResult(
                accepted=False,
                deduplicated=True,
                event=previous,
                consumer_count=0,
            )

        safe_event = self._reconcile_voice(event)
        self._sequence += 1
        safe_event = replace(
            safe_event,
            sequence=self._sequence,
            dispatched_monotonic_ns=self._clock(),
        )
        self._remember(safe_event)

        registrations = tuple(self._consumers.items())
        errors: list[str] = []
        receipts: list[DispatchReceipt] = []
        required_receipt_count = 0
        for token, registration in registrations:
            if registration.receipt_required:
                required_receipt_count += 1
            try:
                returned = registration.consumer(safe_event)
            except Exception as exc:  # consumers are isolated; dispatch remains exactly once
                if registration.receipt_required:
                    errors.append(
                        _bounded_consumer_error(
                            f"consumer {token}: required DispatchReceipt consumer raised "
                            f"{type(exc).__name__}"
                        )
                    )
                else:
                    errors.append(
                        _bounded_consumer_error(f"consumer {token}: {type(exc).__name__}: {exc}")
                    )
                continue
            if not registration.receipt_required:
                continue
            receipt_error = _validate_required_receipt(returned)
            if receipt_error is not None:
                errors.append(_bounded_consumer_error(f"consumer {token}: {receipt_error}"))
                continue
            receipts.append(returned)

        return DispatchResult(
            accepted=True,
            deduplicated=False,
            event=safe_event,
            consumer_count=len(registrations),
            consumer_errors=tuple(errors),
            receipts=tuple(receipts),
            required_receipt_count=required_receipt_count,
        )

    def publish_many(self, events: Iterable[NormalizedMidiEvent]) -> tuple[DispatchResult, ...]:
        return tuple(self.publish(event) for event in events)

    def active_voices(
        self,
        *,
        origin: MidiOrigin | str | None = None,
        source_port: str | None = None,
        session_id: str | None = None,
    ) -> tuple[ActiveVoice, ...]:
        wanted_origin = MidiOrigin(origin) if origin is not None else None
        return tuple(
            voice
            for voice in self._active.values()
            if (wanted_origin is None or voice.origin is wanted_origin)
            and (source_port is None or voice.source_port == source_port)
            and (session_id is None or voice.session_id == session_id)
        )

    def active_refcount(
        self,
        *,
        origin: MidiOrigin | str,
        source_port: str,
        session_id: str,
        channel: int,
        note: int,
    ) -> int:
        key = (MidiOrigin(origin), source_port, session_id, channel, note)
        return self._refcounts[key]

    def release_origin(
        self,
        origin: MidiOrigin | str,
        *,
        source_port: str | None = None,
        session_id: str | None = None,
        elapsed: float = 0.0,
    ) -> tuple[DispatchResult, ...]:
        """Emit matched note-offs for only the selected owner scope."""

        selected = self.active_voices(
            origin=origin,
            source_port=source_port,
            session_id=session_id,
        )
        releases = (
            MidiEvent(
                event_type="release",
                note=voice.note,
                note_name=note_name(voice.note),
                velocity=0,
                channel=voice.channel,
                status=NOTE_OFF | (voice.channel - 1),
                raw_message=(NOTE_OFF | (voice.channel - 1), voice.note, 0),
                delta_time=0.0,
                source=voice.source_port,
                elapsed=elapsed,
                session_id=voice.session_id,
                lesson_id=voice.lesson_id,
                playback_id=voice.playback_id,
                voice_id=voice.voice_id,
                origin=voice.origin,
                matched_voice=True,
            )
            for voice in selected
        )
        return self.publish_many(releases)

    def release_halcyon_voices(
        self,
        *,
        source_port: str | None = None,
        session_id: str | None = None,
        elapsed: float = 0.0,
    ) -> tuple[DispatchResult, ...]:
        """Cleanup primitive for cancel, reconnect, unmount, and coach failure."""

        return self.release_origin(
            MidiOrigin.HALCYON,
            source_port=source_port,
            session_id=session_id,
            elapsed=elapsed,
        )

    def reset_after_panic(self) -> None:
        """Forget every active voice after the explicit global audio panic succeeds."""

        self._assert_owner_thread()
        self._active.clear()
        self._refcounts.clear()

    def _reconcile_voice(self, event: NormalizedMidiEvent) -> NormalizedMidiEvent:
        if event.is_press:
            existing = self._active.get(event.voice_id)
            if existing is not None:
                raise ValueError(f"voice_id {event.voice_id} is already active")
            voice = ActiveVoice(
                voice_id=event.voice_id,
                origin=event.origin,
                source_port=event.source_port,
                session_id=event.session_id,
                note=event.note,
                channel=event.channel,
                lesson_id=event.lesson_id,
                playback_id=event.playback_id,
            )
            self._active[event.voice_id] = voice
            self._refcounts[voice.owner_key] += 1
            return event

        existing = self._active.get(event.voice_id)
        if existing is None:
            return event
        event_key = (
            event.origin,
            event.source_port,
            event.session_id,
            event.channel,
            event.note,
        )
        if existing.owner_key != event_key:
            # A malformed/cross-owner release remains visible, but its replacement ID
            # cannot silence the actual owner in the Rust engine.
            return replace(event, voice_id=allocate_voice_id(), matched_voice=False)

        self._active.pop(event.voice_id)
        self._refcounts[existing.owner_key] -= 1
        if self._refcounts[existing.owner_key] <= 0:
            del self._refcounts[existing.owner_key]
        return event

    def _remember(self, event: NormalizedMidiEvent) -> None:
        self._seen[event.event_id] = event
        while len(self._seen) > self._max_seen_event_ids:
            self._seen.popitem(last=False)

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("MidiEventBus must be used from its owning app thread")


def _bounded_optional_text(value: str | None, *, limit: int, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"receipt {field} must be a string or None")
    bounded = value.strip()[:limit]
    return bounded or None


def _bounded_consumer_error(message: str) -> str:
    return message[:_MAX_CONSUMER_ERROR_CHARS]


def _validate_required_receipt(value: object) -> str | None:
    if value is None:
        return "required DispatchReceipt was not returned"
    try:
        is_receipt = isinstance(value, DispatchReceipt)
        poll = getattr(value, "poll", None)
    except Exception:
        return "required DispatchReceipt protocol violation"
    if not is_receipt or not callable(poll):
        return "required DispatchReceipt protocol violation"
    try:
        snapshot = poll()
    except Exception:
        return "required DispatchReceipt.poll() protocol violation"
    if not isinstance(snapshot, ReceiptPoll):
        return "required DispatchReceipt.poll() returned an invalid snapshot"
    return None


class MidiEventAdapter:
    """Create canonical events for a single human or synthetic MIDI source."""

    def __init__(
        self,
        *,
        origin: MidiOrigin | str,
        source_port: str,
        session_id: str = "app",
        voice_tracker: MidiVoiceTracker | None = None,
    ) -> None:
        self.origin = MidiOrigin(origin)
        self.source_port = source_port
        self.session_id = session_id
        self._voices = voice_tracker or MidiVoiceTracker()

    def note_on(
        self,
        note: int,
        velocity: int,
        *,
        channel: int = 1,
        elapsed: float = 0.0,
        delta_time: float = 0.0,
    ) -> NormalizedMidiEvent:
        status = NOTE_ON | (channel - 1)
        event = normalize_message(
            [status, note, velocity],
            delta_time,
            source=self.source_port,
            elapsed=elapsed,
            origin=self.origin,
            session_id=self.session_id,
            voice_tracker=self._voices,
        )
        if event is None:  # range/command validation above makes this unreachable
            raise ValueError("note_on did not produce a normalized event")
        return event

    def note_off(
        self,
        note: int,
        *,
        channel: int = 1,
        elapsed: float = 0.0,
        delta_time: float = 0.0,
    ) -> NormalizedMidiEvent:
        status = NOTE_OFF | (channel - 1)
        event = normalize_message(
            [status, note, 0],
            delta_time,
            source=self.source_port,
            elapsed=elapsed,
            origin=self.origin,
            session_id=self.session_id,
            voice_tracker=self._voices,
        )
        if event is None:  # range/command validation above makes this unreachable
            raise ValueError("note_off did not produce a normalized event")
        return event


class HalcyonMidiAdapter(MidiEventAdapter):
    """Synthetic coach adapter using the exact hardware-normalized event type."""

    def __init__(self, *, source_port: str, session_id: str) -> None:
        super().__init__(
            origin=MidiOrigin.HALCYON,
            source_port=source_port,
            session_id=session_id,
        )
