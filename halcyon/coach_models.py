"""Immutable lesson contracts and deterministic musical-time resolution."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Iterable, Literal, Mapping, Sequence
import unicodedata

LESSON_SCHEMA_VERSION = 1
LATENCY_SCHEMA_VERSION = 1
MAX_STORED_EVENTS = 2_048
MAX_STORED_DURATION_US = 300_000_000
MAX_AUTHORING_EVENTS = 512
MAX_AUTHORING_DURATION_US = 120_000_000
MAX_MAP_ENTRIES = 256
MAX_SEGMENTS = 128
RUST_TARGET_POLYPHONY = 8
MAX_PLAYBACK_LOOPS = 8
MIN_TEMPO_PERCENT = 25
MAX_TEMPO_PERCENT = 200
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")

EventType = Literal["note_on", "note_off"]


class Acquisition(StrEnum):
    LICENSED_MIDI = "licensed_midi"
    USER_SUPPLIED = "user_supplied"
    PUBLIC_DOMAIN = "public_domain"
    ORIGINAL = "original"
    LOCALLY_AUTHORED_BOUNDED = "locally_authored_bounded"


class Fidelity(StrEnum):
    VERIFIED = "verified"
    APPROXIMATE = "approximate"


class Difficulty(StrEnum):
    FOUNDATION = "foundation"
    STANDARD = "standard"
    CHALLENGE = "challenge"


class Hand(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"


class AuthorMode(StrEnum):
    NEVER = "never"
    BOUNDED = "bounded"


class CoachError(RuntimeError):
    """Stable, bounded coach error suitable for command envelopes."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        resolution: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.resolution = resolution
        self.retryable = retryable

    def to_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.field is not None:
            payload["field"] = self.field
        if self.resolution is not None:
            payload["resolution"] = self.resolution
        return payload


class LessonValidationError(CoachError):
    def __init__(self, message: str, *, field: str, code: str = "AUTHORING_INVALID") -> None:
        super().__init__(
            code,
            message,
            field=field,
            resolution=f"Correct {field} and retry with a schema-1 lesson.",
        )


def _require_int_range(value: Any, minimum: int, maximum: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise LessonValidationError(
            f"{field} must be an integer from {minimum} through {maximum}", field=field
        )


@dataclass(frozen=True)
class LessonLimits:
    """One coherent validation policy for a lesson acquisition surface."""

    max_events: int
    max_duration_us: int
    max_tempo_entries: int = MAX_MAP_ENTRIES
    max_time_signature_entries: int = MAX_MAP_ENTRIES
    max_segments: int = MAX_SEGMENTS
    max_polyphony: int = RUST_TARGET_POLYPHONY

    def __post_init__(self) -> None:
        for field_name in (
            "max_events",
            "max_duration_us",
            "max_tempo_entries",
            "max_time_signature_entries",
            "max_segments",
            "max_polyphony",
        ):
            _require_int_range(getattr(self, field_name), 1, 2**63 - 1, field_name)
        if self.max_polyphony > RUST_TARGET_POLYPHONY:
            raise LessonValidationError(
                f"max_polyphony cannot exceed the Rust target of {RUST_TARGET_POLYPHONY}",
                field="max_polyphony",
            )


STORED_LESSON_LIMITS = LessonLimits(
    max_events=MAX_STORED_EVENTS,
    max_duration_us=MAX_STORED_DURATION_US,
)
AUTHORING_LESSON_LIMITS = LessonLimits(
    max_events=MAX_AUTHORING_EVENTS,
    max_duration_us=MAX_AUTHORING_DURATION_US,
)


@dataclass(frozen=True)
class TempoChange:
    absolute_tick: int
    microseconds_per_quarter: int

    def __post_init__(self) -> None:
        _require_int_range(self.absolute_tick, 0, 2**63 - 1, "tempo_map.absolute_tick")
        _require_int_range(
            self.microseconds_per_quarter,
            1,
            60_000_000,
            "tempo_map.microseconds_per_quarter",
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "absolute_tick": self.absolute_tick,
            "microseconds_per_quarter": self.microseconds_per_quarter,
        }


@dataclass(frozen=True)
class TimeSignature:
    absolute_tick: int
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        _require_int_range(self.absolute_tick, 0, 2**63 - 1, "time_signatures.absolute_tick")
        _require_int_range(self.numerator, 1, 32, "time_signatures.numerator")
        if (
            isinstance(self.denominator, bool)
            or not isinstance(self.denominator, int)
            or self.denominator not in {1, 2, 4, 8, 16, 32, 64}
        ):
            raise LessonValidationError(
                "time signature denominator must be a power of two from 1 through 64",
                field="time_signatures.denominator",
            )

    def to_dict(self) -> dict[str, int]:
        return {
            "absolute_tick": self.absolute_tick,
            "numerator": self.numerator,
            "denominator": self.denominator,
        }


@dataclass(frozen=True)
class LessonProvenance:
    acquisition: Acquisition
    citation: str
    fidelity: Fidelity
    authorization_timestamp: str
    provider: str | None = None
    model_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "acquisition", Acquisition(self.acquisition))
        object.__setattr__(self, "fidelity", Fidelity(self.fidelity))
        _require_text(self.citation, "provenance.citation", 500)
        _validate_timestamp(self.authorization_timestamp, "provenance.authorization_timestamp")
        if self.provider is not None:
            _require_text(self.provider, "provenance.provider", 120)
        if self.model_version is not None:
            _require_text(self.model_version, "provenance.model_version", 120)
        if self.acquisition is Acquisition.LOCALLY_AUTHORED_BOUNDED:
            if self.fidelity is not Fidelity.APPROXIMATE:
                raise LessonValidationError(
                    "locally authored lessons must disclose approximate fidelity",
                    field="provenance.fidelity",
                )
            if self.provider is None or self.model_version is None:
                raise LessonValidationError(
                    "locally authored lessons require provider and model version",
                    field="provenance.provider",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "acquisition": self.acquisition.value,
            "citation": self.citation,
            "fidelity": self.fidelity.value,
            "authorization_timestamp": self.authorization_timestamp,
            "provider": self.provider,
            "model_version": self.model_version,
        }


@dataclass(frozen=True)
class LessonEvent:
    event_id: str
    event_type: EventType
    note: int
    velocity: int
    channel: int
    status: int
    source_port: str
    content_origin: str
    absolute_tick: int
    delta_tick: int

    def __post_init__(self) -> None:
        if self.event_id:
            _require_text(self.event_id, "events.event_id", 96)
        if self.event_type not in {"note_on", "note_off"}:
            raise LessonValidationError(
                "event type must be note_on or note_off", field="events.event_type"
            )
        _require_int_range(self.note, 0, 127, "events.note")
        _require_int_range(self.velocity, 0, 127, "events.velocity")
        _require_int_range(self.channel, 1, 16, "events.channel")
        _require_int_range(self.status, 0, 255, "events.status")
        _require_int_range(self.absolute_tick, 0, 2**63 - 1, "events.absolute_tick")
        _require_int_range(self.delta_tick, 0, 2**63 - 1, "events.delta_tick")
        _require_text(self.source_port, "events.source_port", 160)
        _require_text(self.content_origin, "events.content_origin", 80)
        command = self.status & 0xF0
        status_channel = (self.status & 0x0F) + 1
        if status_channel != self.channel:
            raise LessonValidationError(
                "event status channel must equal the declared channel", field="events.status"
            )
        if self.event_type == "note_on":
            if command != 0x90 or self.velocity == 0:
                raise LessonValidationError(
                    "note_on requires 0x9n status and positive velocity", field="events.status"
                )
        elif command not in {0x80, 0x90} or (command == 0x90 and self.velocity != 0):
            raise LessonValidationError(
                "note_off requires 0x8n status or velocity-zero 0x9n status",
                field="events.status",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "note": self.note,
            "velocity": self.velocity,
            "channel": self.channel,
            "status": self.status,
            "source_port": self.source_port,
            "content_origin": self.content_origin,
            "absolute_tick": self.absolute_tick,
            "delta_tick": self.delta_tick,
        }


@dataclass(frozen=True)
class LessonSegment:
    label: str
    start_tick: int
    end_tick: int
    hand: Hand | None = None
    difficulty: Difficulty | None = None

    def __post_init__(self) -> None:
        _require_text(self.label, "segments.label", 80)
        _require_int_range(self.start_tick, 0, 2**63 - 1, "segments.start_tick")
        _require_int_range(self.end_tick, 1, 2**63 - 1, "segments.end_tick")
        if self.end_tick <= self.start_tick:
            raise LessonValidationError(
                "segment end_tick must be greater than start_tick", field="segments.end_tick"
            )
        if self.hand is not None:
            object.__setattr__(self, "hand", Hand(self.hand))
        if self.difficulty is not None:
            object.__setattr__(self, "difficulty", Difficulty(self.difficulty))

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start_tick": self.start_tick,
            "end_tick": self.end_tick,
            "hand": self.hand.value if self.hand else None,
            "difficulty": self.difficulty.value if self.difficulty else None,
        }


@dataclass(frozen=True)
class LessonRevision:
    schema_version: int
    lesson_id: str
    revision_id: str
    content_id: str
    created_at: str
    aliases: tuple[str, ...]
    title: str
    composer: str
    difficulty: Difficulty | None
    hand: Hand | None
    ppq: int
    tempo_map: tuple[TempoChange, ...]
    time_signatures: tuple[TimeSignature, ...]
    provenance: LessonProvenance
    events: tuple[LessonEvent, ...]
    segments: tuple[LessonSegment, ...]
    max_polyphony: int = RUST_TARGET_POLYPHONY

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != LESSON_SCHEMA_VERSION
        ):
            code = (
                "SCHEMA_UNSUPPORTED"
                if isinstance(self.schema_version, int)
                and not isinstance(self.schema_version, bool)
                and self.schema_version > LESSON_SCHEMA_VERSION
                else "LESSON_CORRUPT"
            )
            raise LessonValidationError(
                f"lesson schema {self.schema_version} is unsupported",
                field="schema_version",
                code=code,
            )
        _validate_identifier(self.lesson_id, "lesson_id")
        _require_text(self.revision_id, "revision_id", 80)
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_id):
            raise LessonValidationError(
                "content_id must be a SHA-256 hex digest", field="content_id"
            )
        _validate_timestamp(self.created_at, "created_at")
        _require_text(self.title, "title", 160)
        _require_text(self.composer, "composer", 160)
        _require_int_range(self.ppq, 1, 9_600, "ppq")
        _require_int_range(self.max_polyphony, 1, RUST_TARGET_POLYPHONY, "max_polyphony")
        if self.difficulty is not None:
            object.__setattr__(self, "difficulty", Difficulty(self.difficulty))
        if self.hand is not None:
            object.__setattr__(self, "hand", Hand(self.hand))
        aliases = tuple(dict.fromkeys(normalize_lesson_alias(alias) for alias in self.aliases))
        if not aliases:
            raise LessonValidationError(
                "at least one normalized alias is required", field="aliases"
            )
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "tempo_map", tuple(self.tempo_map))
        object.__setattr__(self, "time_signatures", tuple(self.time_signatures))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "segments", tuple(self.segments))
        validate_lesson_revision(self, STORED_LESSON_LIMITS)

    @property
    def duration_ticks(self) -> int:
        return self.events[-1].absolute_tick

    @property
    def duration_us(self) -> int:
        return tick_positions_to_microseconds((self.duration_ticks,), self.ppq, self.tempo_map)[
            self.duration_ticks
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "lesson_id": self.lesson_id,
            "revision_id": self.revision_id,
            "content_id": self.content_id,
            "created_at": self.created_at,
            "aliases": list(self.aliases),
            "title": self.title,
            "composer": self.composer,
            "difficulty": self.difficulty.value if self.difficulty else None,
            "hand": self.hand.value if self.hand else None,
            "ppq": self.ppq,
            "tempo_map": [entry.to_dict() for entry in self.tempo_map],
            "time_signatures": [entry.to_dict() for entry in self.time_signatures],
            "provenance": self.provenance.to_dict(),
            "events": [event.to_dict() for event in self.events],
            "segments": [segment.to_dict() for segment in self.segments],
            "max_polyphony": self.max_polyphony,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LessonRevision:
        try:
            _require_exact_keys(payload, _LESSON_KEYS, "lesson")
            provenance_payload = _mapping(payload["provenance"], "provenance")
            _require_exact_keys(provenance_payload, _PROVENANCE_KEYS, "provenance")
            return cls(
                schema_version=_strict_int(payload["schema_version"], "schema_version"),
                lesson_id=_strict_str(payload["lesson_id"], "lesson_id"),
                revision_id=_strict_str(payload["revision_id"], "revision_id"),
                content_id=_strict_str(payload["content_id"], "content_id"),
                created_at=_strict_str(payload["created_at"], "created_at"),
                aliases=tuple(_string_sequence(payload["aliases"], "aliases")),
                title=_strict_str(payload["title"], "title"),
                composer=_strict_str(payload["composer"], "composer"),
                difficulty=(
                    Difficulty(payload["difficulty"]) if payload["difficulty"] is not None else None
                ),
                hand=Hand(payload["hand"]) if payload["hand"] is not None else None,
                ppq=_strict_int(payload["ppq"], "ppq"),
                tempo_map=tuple(
                    _tempo_from_dict(item) for item in _list(payload["tempo_map"], "tempo_map")
                ),
                time_signatures=tuple(
                    _signature_from_dict(item)
                    for item in _list(payload["time_signatures"], "time_signatures")
                ),
                provenance=LessonProvenance(
                    acquisition=Acquisition(provenance_payload["acquisition"]),
                    citation=_strict_str(provenance_payload["citation"], "provenance.citation"),
                    fidelity=Fidelity(provenance_payload["fidelity"]),
                    authorization_timestamp=_strict_str(
                        provenance_payload["authorization_timestamp"],
                        "provenance.authorization_timestamp",
                    ),
                    provider=_optional_str(provenance_payload["provider"], "provenance.provider"),
                    model_version=_optional_str(
                        provenance_payload["model_version"], "provenance.model_version"
                    ),
                ),
                events=tuple(_event_from_dict(item) for item in _list(payload["events"], "events")),
                segments=tuple(
                    _segment_from_dict(item) for item in _list(payload["segments"], "segments")
                ),
                max_polyphony=_strict_int(payload["max_polyphony"], "max_polyphony"),
            )
        except LessonValidationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise LessonValidationError(
                "lesson manifest is malformed", field="lesson", code="LESSON_CORRUPT"
            ) from exc


@dataclass(frozen=True)
class LessonRequest:
    query: str
    segment: str | None = None
    tempo_percent: int = 100
    difficulty: Difficulty | None = None
    hand: Hand | None = None
    author: AuthorMode = AuthorMode.NEVER

    def __post_init__(self) -> None:
        query = re.sub(r"\s+", " ", self.query.strip())
        _require_text(query, "query", 160)
        object.__setattr__(self, "query", query)
        if self.segment is not None:
            segment = re.sub(r"\s+", " ", self.segment.strip())
            _require_text(segment, "segment", 80)
            object.__setattr__(self, "segment", segment)
        _require_int_range(
            self.tempo_percent, MIN_TEMPO_PERCENT, MAX_TEMPO_PERCENT, "tempo_percent"
        )
        if self.difficulty is not None:
            object.__setattr__(self, "difficulty", Difficulty(self.difficulty))
        if self.hand is not None:
            object.__setattr__(self, "hand", Hand(self.hand))
        object.__setattr__(self, "author", AuthorMode(self.author))

    @property
    def normalized_alias(self) -> str:
        return normalize_lesson_alias(self.query)


@dataclass(frozen=True)
class LessonSelection:
    segment: str | None = None
    tempo_percent: int = 100
    difficulty: Difficulty | None = None
    hand: Hand | None = None

    def __post_init__(self) -> None:
        _require_int_range(
            self.tempo_percent, MIN_TEMPO_PERCENT, MAX_TEMPO_PERCENT, "tempo_percent"
        )
        if self.segment is not None:
            _require_text(self.segment, "segment", 80)
        if self.difficulty is not None:
            object.__setattr__(self, "difficulty", Difficulty(self.difficulty))
        if self.hand is not None:
            object.__setattr__(self, "hand", Hand(self.hand))

    @classmethod
    def from_request(cls, request: LessonRequest) -> LessonSelection:
        return cls(
            segment=request.segment,
            tempo_percent=request.tempo_percent,
            difficulty=request.difficulty,
            hand=request.hand,
        )


@dataclass(frozen=True)
class ResolvedLessonEvent:
    lesson_event_id: str
    event_type: EventType
    note: int
    velocity: int
    channel: int
    status: int
    source_port: str
    content_origin: str
    absolute_tick: int
    delta_tick: int
    absolute_us: int
    delta_us: int

    def __post_init__(self) -> None:
        _require_text(self.lesson_event_id, "lesson_event_id", 128)
        if self.event_type not in {"note_on", "note_off"}:
            raise LessonValidationError(
                "resolved event type must be note_on or note_off", field="event_type"
            )
        _require_int_range(self.note, 0, 127, "note")
        _require_int_range(self.velocity, 0, 127, "velocity")
        _require_int_range(self.channel, 1, 16, "channel")
        _require_int_range(self.status, 0, 255, "status")
        _require_text(self.source_port, "source_port", 160)
        _require_text(self.content_origin, "content_origin", 80)
        command = self.status & 0xF0
        if (self.status & 0x0F) + 1 != self.channel:
            raise LessonValidationError("status channel must match channel", field="status")
        if self.event_type == "note_on" and (command != 0x90 or self.velocity == 0):
            raise LessonValidationError(
                "note_on requires 0x9n status and positive velocity", field="status"
            )
        if self.event_type == "note_off" and (
            command not in {0x80, 0x90} or (command == 0x90 and self.velocity != 0)
        ):
            raise LessonValidationError(
                "note_off requires 0x8n status or velocity-zero 0x9n status", field="status"
            )
        for field_name in ("absolute_tick", "delta_tick", "absolute_us", "delta_us"):
            _require_int_range(getattr(self, field_name), 0, 2**63 - 1, field_name)


@dataclass(frozen=True)
class LessonTimeline:
    lesson_id: str
    revision_id: str
    content_id: str
    segment_label: str | None
    hand: Hand | None
    difficulty: Difficulty | None
    tempo_percent: int
    ppq: int
    start_tick: int
    end_tick: int
    duration_ticks: int
    duration_us: int
    events: tuple[ResolvedLessonEvent, ...]

    def __post_init__(self) -> None:
        _validate_identifier(self.lesson_id, "lesson_id")
        _require_text(self.revision_id, "revision_id", 80)
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_id):
            raise LessonValidationError(
                "content_id must be a SHA-256 hex digest", field="content_id"
            )
        if self.segment_label is not None:
            _require_text(self.segment_label, "segment_label", 80)
        if self.hand is not None:
            object.__setattr__(self, "hand", Hand(self.hand))
        if self.difficulty is not None:
            object.__setattr__(self, "difficulty", Difficulty(self.difficulty))
        _require_int_range(
            self.tempo_percent, MIN_TEMPO_PERCENT, MAX_TEMPO_PERCENT, "tempo_percent"
        )
        _require_int_range(self.ppq, 1, 9_600, "ppq")
        _require_int_range(self.start_tick, 0, 2**63 - 1, "start_tick")
        if self.end_tick <= self.start_tick:
            raise LessonValidationError("end_tick must be after start_tick", field="end_tick")
        if self.duration_ticks != self.end_tick - self.start_tick:
            raise LessonValidationError(
                "duration_ticks must equal end_tick minus start_tick", field="duration_ticks"
            )
        _require_int_range(self.duration_us, 1, 2**63 - 1, "duration_us")
        object.__setattr__(self, "events", tuple(self.events))
        previous_tick = previous_us = 0
        for index, event in enumerate(self.events):
            if index and (event.absolute_tick < previous_tick or event.absolute_us < previous_us):
                raise LessonValidationError(
                    "resolved events must be monotonic", field=f"events[{index}]"
                )
            expected_tick_delta = (
                event.absolute_tick if index == 0 else event.absolute_tick - previous_tick
            )
            expected_us_delta = event.absolute_us if index == 0 else event.absolute_us - previous_us
            if event.delta_tick != expected_tick_delta or event.delta_us != expected_us_delta:
                raise LessonValidationError(
                    "resolved event deltas must match absolute positions",
                    field=f"events[{index}].delta_us",
                )
            if event.absolute_tick > self.duration_ticks or event.absolute_us > self.duration_us:
                raise LessonValidationError(
                    "resolved event exceeds timeline duration", field=f"events[{index}]"
                )
            previous_tick, previous_us = event.absolute_tick, event.absolute_us


@dataclass(frozen=True)
class ScheduledLoopEvent:
    """One zero-based loop occurrence at an exact schedule offset."""

    loop_index: int
    timeline_ordinal: int
    offset_us: int
    event: ResolvedLessonEvent

    def __post_init__(self) -> None:
        _require_int_range(self.loop_index, 0, MAX_PLAYBACK_LOOPS - 1, "loop_index")
        _require_int_range(self.timeline_ordinal, 0, 2**31 - 1, "timeline_ordinal")
        _require_int_range(self.offset_us, 0, 2**63 - 1, "offset_us")
        if not isinstance(self.event, ResolvedLessonEvent):
            raise LessonValidationError(
                "scheduled event must reference a resolved lesson event", field="event"
            )


@dataclass(frozen=True)
class LoopSchedule:
    """Remainder-preserving replay offsets for one selected lesson timeline."""

    boundaries_us: tuple[int, ...]
    events: tuple[ScheduledLoopEvent, ...]
    total_duration_us: int

    def __post_init__(self) -> None:
        boundaries = tuple(self.boundaries_us)
        events = tuple(self.events)
        if not 2 <= len(boundaries) <= MAX_PLAYBACK_LOOPS + 1:
            raise LessonValidationError(
                f"loop schedule must contain 1 through {MAX_PLAYBACK_LOOPS} loops",
                field="boundaries_us",
            )
        if boundaries[0] != 0:
            raise LessonValidationError(
                "loop schedule boundaries must begin at zero", field="boundaries_us"
            )
        previous = -1
        for boundary in boundaries:
            _require_int_range(boundary, 0, 2**63 - 1, "boundaries_us")
            if boundary < previous:
                raise LessonValidationError(
                    "loop schedule boundaries must be monotonic", field="boundaries_us"
                )
            previous = boundary
        _require_int_range(self.total_duration_us, 1, 2**63 - 1, "total_duration_us")
        if boundaries[-1] != self.total_duration_us:
            raise LessonValidationError(
                "last loop boundary must equal total duration", field="total_duration_us"
            )
        expected_order = sorted(
            events,
            key=lambda item: (item.offset_us, item.loop_index, item.timeline_ordinal),
        )
        if list(events) != expected_order:
            raise LessonValidationError(
                "scheduled events must use deterministic playback order", field="events"
            )
        if any(event.loop_index >= len(boundaries) - 1 for event in events):
            raise LessonValidationError(
                "scheduled event loop index exceeds the schedule", field="events.loop_index"
            )
        if any(event.offset_us > self.total_duration_us for event in events):
            raise LessonValidationError(
                "scheduled event exceeds the schedule duration", field="events.offset_us"
            )
        object.__setattr__(self, "boundaries_us", boundaries)
        object.__setattr__(self, "events", events)

    @property
    def loops(self) -> int:
        return len(self.boundaries_us) - 1


@dataclass(frozen=True)
class LatencyProfile:
    schema_version: int = LATENCY_SCHEMA_VERSION
    offset_us: int = 0
    jitter_us: int = 0
    sample_count: int = 0
    calibrated_at: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != LATENCY_SCHEMA_VERSION
        ):
            raise LessonValidationError(
                "latency profile schema is unsupported",
                field="schema_version",
                code="SCHEMA_UNSUPPORTED",
            )
        _require_int_range(self.offset_us, -1_000_000, 1_000_000, "offset_us")
        _require_int_range(self.jitter_us, 0, 1_000_000, "jitter_us")
        _require_int_range(self.sample_count, 0, 10_000, "sample_count")
        if self.calibrated_at is not None:
            _validate_timestamp(self.calibrated_at, "calibrated_at")

    @property
    def tolerance_us(self) -> int:
        return max(40_000, min(250_000, self.jitter_us * 3))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "offset_us": self.offset_us,
            "jitter_us": self.jitter_us,
            "sample_count": self.sample_count,
            "calibrated_at": self.calibrated_at,
        }


@dataclass(frozen=True)
class LessonResolution:
    lesson: LessonRevision
    timeline: LessonTimeline
    cache_hit: bool
    authored: bool
    matched_alias: str


def normalize_lesson_alias(value: str) -> str:
    """Normalize a descriptive request for deterministic catalog lookup."""

    if not isinstance(value, str):
        raise LessonValidationError("alias must be text", field="aliases")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"_+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    _require_text(normalized, "aliases", 160)
    return normalized


def derive_lesson_id(value: str) -> str:
    alias = normalize_lesson_alias(value)
    slug = re.sub(r"[^a-z0-9]+", "-", alias.encode("ascii", "ignore").decode()).strip("-")
    slug = (slug or "lesson")[:72].rstrip("-")
    return f"{slug}-{hashlib.sha256(alias.encode()).hexdigest()[:12]}"


def derive_revision_id(content_id: str) -> str:
    return f"r1-{content_id}"


def derive_lesson_event_id(lesson_id: str, revision_id: str, ordinal: int) -> str:
    digest = hashlib.sha256(f"{lesson_id}\0{revision_id}\0{ordinal}".encode()).hexdigest()
    return f"e1-{digest}"


def derive_runtime_event_id(playback_id: str, loop: int, lesson_event_id: str) -> str:
    _require_text(playback_id, "playback_id", 96)
    _require_int_range(loop, 0, 2**31 - 1, "loop")
    digest = hashlib.sha256(f"{playback_id}\0{loop}\0{lesson_event_id}".encode()).hexdigest()
    return f"p1-{digest}"


def create_lesson_revision(
    *,
    lesson_id: str,
    created_at: str,
    aliases: Sequence[str],
    title: str,
    composer: str,
    difficulty: Difficulty | str | None,
    hand: Hand | str | None,
    ppq: int,
    tempo_map: Sequence[TempoChange],
    time_signatures: Sequence[TimeSignature],
    provenance: LessonProvenance,
    events: Sequence[LessonEvent],
    segments: Sequence[LessonSegment],
    max_polyphony: int = RUST_TARGET_POLYPHONY,
    limits: LessonLimits = STORED_LESSON_LIMITS,
) -> LessonRevision:
    """Normalize IDs, validate, and return one immutable schema-1 revision."""

    _validate_identifier(lesson_id, "lesson_id")
    normalized_aliases = tuple(dict.fromkeys(normalize_lesson_alias(alias) for alias in aliases))
    raw_events = tuple(replace(event, event_id="") for event in events)
    canonical = _canonical_payload(
        ppq=ppq,
        tempo_map=tempo_map,
        time_signatures=time_signatures,
        events=raw_events,
        segments=segments,
    )
    content_id = hashlib.sha256(_canonical_json(canonical).encode()).hexdigest()
    revision_id = derive_revision_id(content_id)
    stable_events = tuple(
        replace(event, event_id=derive_lesson_event_id(lesson_id, revision_id, index))
        for index, event in enumerate(raw_events)
    )
    revision = LessonRevision(
        schema_version=LESSON_SCHEMA_VERSION,
        lesson_id=lesson_id,
        revision_id=revision_id,
        content_id=content_id,
        created_at=created_at,
        aliases=normalized_aliases,
        title=title,
        composer=composer,
        difficulty=Difficulty(difficulty) if difficulty is not None else None,
        hand=Hand(hand) if hand is not None else None,
        ppq=ppq,
        tempo_map=tuple(tempo_map),
        time_signatures=tuple(time_signatures),
        provenance=provenance,
        events=stable_events,
        segments=tuple(segments),
        max_polyphony=max_polyphony,
    )
    validate_lesson_revision(revision, limits)
    return revision


def validate_lesson_revision(
    lesson: LessonRevision, limits: LessonLimits = STORED_LESSON_LIMITS
) -> None:
    if not lesson.tempo_map or lesson.tempo_map[0].absolute_tick != 0:
        raise LessonValidationError("first tempo change must be at tick zero", field="tempo_map")
    if not lesson.time_signatures or lesson.time_signatures[0].absolute_tick != 0:
        raise LessonValidationError(
            "first time signature must be at tick zero", field="time_signatures"
        )
    _validate_strict_ticks(lesson.tempo_map, "tempo_map")
    _validate_strict_ticks(lesson.time_signatures, "time_signatures")
    if len(lesson.tempo_map) > limits.max_tempo_entries:
        raise LessonValidationError(
            f"tempo_map may contain at most {limits.max_tempo_entries} entries",
            field="tempo_map",
        )
    if len(lesson.time_signatures) > limits.max_time_signature_entries:
        raise LessonValidationError(
            f"time_signatures may contain at most {limits.max_time_signature_entries} entries",
            field="time_signatures",
        )
    if not lesson.events:
        raise LessonValidationError("lesson must contain events", field="events")
    if len(lesson.events) > limits.max_events:
        raise LessonValidationError(
            f"lesson has more than {limits.max_events} events", field="events"
        )
    if lesson.tempo_map[-1].absolute_tick > lesson.duration_ticks:
        raise LessonValidationError(
            "tempo_map entries must fall within lesson duration", field="tempo_map"
        )
    if lesson.time_signatures[-1].absolute_tick > lesson.duration_ticks:
        raise LessonValidationError(
            "time_signatures entries must fall within lesson duration", field="time_signatures"
        )
    if lesson.max_polyphony > limits.max_polyphony:
        raise LessonValidationError(
            f"max_polyphony may not exceed {limits.max_polyphony}", field="max_polyphony"
        )
    active: dict[tuple[int, int], deque[str]] = defaultdict(deque)
    previous_tick = 0
    peak = 0
    for index, event in enumerate(lesson.events):
        if event.absolute_tick < previous_tick:
            raise LessonValidationError(
                "events must be monotonic", field=f"events[{index}].absolute_tick"
            )
        expected_delta = event.absolute_tick if index == 0 else event.absolute_tick - previous_tick
        if event.delta_tick != expected_delta:
            raise LessonValidationError(
                "event delta_tick is inconsistent with absolute_tick",
                field=f"events[{index}].delta_tick",
            )
        expected_id = derive_lesson_event_id(lesson.lesson_id, lesson.revision_id, index)
        if event.event_id != expected_id:
            raise LessonValidationError(
                "event_id does not match the derived stable identity",
                field=f"events[{index}].event_id",
            )
        key = (event.channel, event.note)
        if event.event_type == "note_on":
            active[key].append(event.event_id)
            peak = max(peak, sum(len(values) for values in active.values()))
        elif not active[key]:
            raise LessonValidationError(
                "note_off has no matching note_on", field=f"events[{index}]"
            )
        else:
            active[key].popleft()
        previous_tick = event.absolute_tick
    if any(active.values()):
        raise LessonValidationError("lesson contains hanging notes", field="events")
    if peak > lesson.max_polyphony:
        raise LessonValidationError(
            "declared max_polyphony is lower than the actual note polyphony",
            field="max_polyphony",
        )
    if len(lesson.segments) > limits.max_segments:
        raise LessonValidationError(
            f"lesson may declare at most {limits.max_segments} segments", field="segments"
        )
    labels: set[str] = set()
    for index, segment in enumerate(lesson.segments):
        label = normalize_lesson_alias(segment.label)
        if label in labels:
            raise LessonValidationError(
                "segment labels must be unique", field=f"segments[{index}].label"
            )
        labels.add(label)
        if segment.end_tick > lesson.duration_ticks:
            raise LessonValidationError(
                "segment bounds exceed lesson duration", field=f"segments[{index}].end_tick"
            )
    if lesson.duration_us > limits.max_duration_us:
        raise LessonValidationError(
            f"lesson duration exceeds {limits.max_duration_us // 1_000_000} seconds",
            field="events.absolute_tick",
        )
    canonical_id = hashlib.sha256(
        _canonical_json(
            _canonical_payload(
                ppq=lesson.ppq,
                tempo_map=lesson.tempo_map,
                time_signatures=lesson.time_signatures,
                events=lesson.events,
                segments=lesson.segments,
            )
        ).encode()
    ).hexdigest()
    if lesson.content_id != canonical_id:
        raise LessonValidationError(
            "content_id does not match canonical content", field="content_id"
        )
    if lesson.revision_id != derive_revision_id(lesson.content_id):
        raise LessonValidationError(
            "revision_id does not derive from content_id", field="revision_id"
        )


def tick_positions_to_microseconds(
    positions: Iterable[int],
    ppq: int,
    tempo_map: Sequence[TempoChange],
    tempo_percent: int = 100,
) -> dict[int, int]:
    """Resolve ticks exactly with one carried integer remainder across tempo changes."""

    _require_int_range(ppq, 1, 9_600, "ppq")
    _require_int_range(tempo_percent, MIN_TEMPO_PERCENT, MAX_TEMPO_PERCENT, "tempo_percent")
    changes = tuple(tempo_map)
    if not changes or changes[0].absolute_tick != 0:
        raise LessonValidationError("first tempo change must be at tick zero", field="tempo_map")
    _validate_strict_ticks(changes, "tempo_map")
    targets = sorted(set(positions))
    for target in targets:
        _require_int_range(target, 0, 2**63 - 1, "absolute_tick")
    denominator = ppq * tempo_percent
    current_tick = current_us = remainder = 0
    tempo_index = 0
    current_tempo = changes[0].microseconds_per_quarter
    resolved: dict[int, int] = {}

    def advance(target: int) -> None:
        nonlocal current_tick, current_us, remainder
        numerator = remainder + (target - current_tick) * current_tempo * 100
        whole, remainder = divmod(numerator, denominator)
        current_us += whole
        current_tick = target

    for target in targets:
        while tempo_index + 1 < len(changes) and changes[tempo_index + 1].absolute_tick <= target:
            tempo_index += 1
            advance(changes[tempo_index].absolute_tick)
            current_tempo = changes[tempo_index].microseconds_per_quarter
        advance(target)
        resolved[target] = current_us
    return resolved


def resolve_timeline(
    lesson: LessonRevision,
    selection: LessonSelection | LessonRequest | None = None,
) -> LessonTimeline:
    """Select and tempo-scale a balanced timeline using integer musical time."""

    if isinstance(selection, LessonRequest):
        selection = LessonSelection.from_request(selection)
    selection = selection or LessonSelection()
    segment = _select_segment(lesson, selection)
    start_tick = segment.start_tick if segment else 0
    end_tick = segment.end_tick if segment else lesson.duration_ticks
    selected_events = _clip_events(
        lesson,
        start_tick,
        end_tick,
        half_open=segment is not None,
    )
    relative_ticks = tuple(event.absolute_tick - start_tick for event in selected_events)
    absolute_positions = (start_tick, *(event.absolute_tick for event in selected_events), end_tick)
    lesson_us = tick_positions_to_microseconds(
        absolute_positions, lesson.ppq, lesson.tempo_map, selection.tempo_percent
    )
    start_us = lesson_us[start_tick]
    resolved_events: list[ResolvedLessonEvent] = []
    previous_tick = previous_us = 0
    for event, relative_tick in zip(selected_events, relative_ticks, strict=True):
        absolute_us = lesson_us[event.absolute_tick] - start_us
        resolved_events.append(
            ResolvedLessonEvent(
                lesson_event_id=event.event_id,
                event_type=event.event_type,
                note=event.note,
                velocity=event.velocity,
                channel=event.channel,
                status=event.status,
                source_port=event.source_port,
                content_origin=event.content_origin,
                absolute_tick=relative_tick,
                delta_tick=relative_tick - previous_tick,
                absolute_us=absolute_us,
                delta_us=absolute_us - previous_us,
            )
        )
        previous_tick, previous_us = relative_tick, absolute_us
    duration_ticks = end_tick - start_tick
    resolved_hand = selection.hand or ((segment.hand or lesson.hand) if segment else lesson.hand)
    resolved_difficulty = selection.difficulty or (
        (segment.difficulty or lesson.difficulty) if segment else lesson.difficulty
    )
    return LessonTimeline(
        lesson_id=lesson.lesson_id,
        revision_id=lesson.revision_id,
        content_id=lesson.content_id,
        segment_label=segment.label if segment else None,
        hand=resolved_hand,
        difficulty=resolved_difficulty,
        tempo_percent=selection.tempo_percent,
        ppq=lesson.ppq,
        start_tick=start_tick,
        end_tick=end_tick,
        duration_ticks=duration_ticks,
        duration_us=lesson_us[end_tick] - start_us,
        events=tuple(resolved_events),
    )


def resolve_loop_schedule(
    lesson: LessonRevision,
    timeline: LessonTimeline,
    loops: int,
) -> LoopSchedule:
    """Resolve replay offsets without multiplying a rounded one-loop duration."""

    _require_int_range(loops, 1, MAX_PLAYBACK_LOOPS, "loops")
    if (
        timeline.lesson_id != lesson.lesson_id
        or timeline.revision_id != lesson.revision_id
        or timeline.content_id != lesson.content_id
        or timeline.ppq != lesson.ppq
    ):
        raise LessonValidationError(
            "timeline identity does not match its lesson revision", field="timeline"
        )
    if timeline.end_tick > lesson.duration_ticks:
        raise LessonValidationError(
            "timeline bounds exceed the lesson revision", field="timeline.end_tick"
        )

    source_ticks = (
        timeline.start_tick,
        timeline.end_tick,
        *(timeline.start_tick + event.absolute_tick for event in timeline.events),
    )
    exact_positions = _tick_positions_to_numerators(source_ticks, lesson.tempo_map)
    denominator = lesson.ppq * timeline.tempo_percent
    start_numerator = exact_positions[timeline.start_tick]
    start_us = start_numerator // denominator
    duration_numerator = exact_positions[timeline.end_tick] - start_numerator

    def rounded_offset(relative_numerator: int) -> int:
        return (start_numerator + relative_numerator) // denominator - start_us

    boundaries = tuple(
        rounded_offset(loop_index * duration_numerator) for loop_index in range(loops + 1)
    )
    scheduled = [
        ScheduledLoopEvent(
            loop_index=loop_index,
            timeline_ordinal=ordinal,
            offset_us=rounded_offset(
                loop_index * duration_numerator
                + exact_positions[timeline.start_tick + event.absolute_tick]
                - start_numerator
            ),
            event=event,
        )
        for loop_index in range(loops)
        for ordinal, event in enumerate(timeline.events)
    ]
    scheduled.sort(key=lambda item: (item.offset_us, item.loop_index, item.timeline_ordinal))
    return LoopSchedule(
        boundaries_us=boundaries,
        events=tuple(scheduled),
        total_duration_us=boundaries[-1],
    )


def _tick_positions_to_numerators(
    positions: Iterable[int], tempo_map: Sequence[TempoChange]
) -> dict[int, int]:
    """Integrate source ticks into exact tempo numerators before division."""

    targets = sorted(set(positions))
    changes = tuple(tempo_map)
    current_tick = current_numerator = 0
    tempo_index = 0
    current_tempo = changes[0].microseconds_per_quarter
    resolved: dict[int, int] = {}
    for target in targets:
        _require_int_range(target, 0, 2**63 - 1, "absolute_tick")
        while tempo_index + 1 < len(changes) and changes[tempo_index + 1].absolute_tick <= target:
            change_tick = changes[tempo_index + 1].absolute_tick
            current_numerator += (change_tick - current_tick) * current_tempo * 100
            current_tick = change_tick
            tempo_index += 1
            current_tempo = changes[tempo_index].microseconds_per_quarter
        current_numerator += (target - current_tick) * current_tempo * 100
        current_tick = target
        resolved[target] = current_numerator
    return resolved


def _select_segment(lesson: LessonRevision, selection: LessonSelection) -> LessonSegment | None:
    if selection.segment is not None:
        wanted = normalize_lesson_alias(selection.segment)
        segment = next(
            (item for item in lesson.segments if normalize_lesson_alias(item.label) == wanted),
            None,
        )
        if segment is None:
            raise CoachError(
                "NO_MATCHING_SEGMENT",
                "The requested segment is not declared for this lesson.",
                field="segment",
                resolution="Choose a declared segment label and retry.",
                retryable=True,
            )
        effective_hand = segment.hand or lesson.hand
        effective_difficulty = segment.difficulty or lesson.difficulty
        if selection.hand is not None and effective_hand is None:
            raise CoachError(
                "LESSON_METADATA_REQUIRED",
                "The requested segment does not declare hand metadata.",
                field="hand",
                resolution="Choose a lesson or segment with declared hand metadata.",
            )
        if selection.hand is not None and effective_hand is not selection.hand:
            raise CoachError(
                "NO_MATCHING_SEGMENT",
                "No declared segment matches the requested hand.",
                field="hand",
                resolution="Use left, right, or both only when declared by the lesson.",
            )
        if selection.difficulty is not None and effective_difficulty is None:
            raise CoachError(
                "LESSON_METADATA_REQUIRED",
                "The requested segment does not declare difficulty metadata.",
                field="difficulty",
                resolution="Choose a declared foundation, standard, or challenge segment.",
            )
        if selection.difficulty is not None and effective_difficulty is not selection.difficulty:
            raise CoachError(
                "NO_MATCHING_SEGMENT",
                "No declared segment matches the requested difficulty.",
                field="difficulty",
                resolution="Choose a declared difficulty and retry.",
            )
        return segment

    if selection.hand is None and selection.difficulty is None:
        return None
    candidates = [
        segment
        for segment in lesson.segments
        if (selection.hand is None or (segment.hand or lesson.hand) is selection.hand)
        and (
            selection.difficulty is None
            or (segment.difficulty or lesson.difficulty) is selection.difficulty
        )
    ]
    if candidates:
        return candidates[0]
    if selection.hand is not None and lesson.hand is None:
        raise CoachError(
            "LESSON_METADATA_REQUIRED",
            "This lesson does not declare hand metadata.",
            field="hand",
            resolution="Choose a lesson or segment with declared hand metadata.",
        )
    if selection.difficulty is not None and lesson.difficulty is None:
        raise CoachError(
            "LESSON_METADATA_REQUIRED",
            "This lesson does not declare difficulty metadata.",
            field="difficulty",
            resolution="Choose a declared foundation, standard, or challenge segment.",
        )
    if (selection.hand is None or lesson.hand is selection.hand) and (
        selection.difficulty is None or lesson.difficulty is selection.difficulty
    ):
        return None
    field_name = "hand" if selection.hand is not None else "difficulty"
    raise CoachError(
        "NO_MATCHING_SEGMENT",
        f"No declared lesson metadata matches the requested {field_name}.",
        field=field_name,
        resolution="Choose only metadata values declared by the lesson.",
    )


def _clip_events(
    lesson: LessonRevision,
    start_tick: int,
    end_tick: int,
    *,
    half_open: bool,
) -> tuple[LessonEvent, ...]:
    if half_open:
        return _clip_half_open_segment(lesson, start_tick, end_tick)

    active_before: dict[tuple[int, int], deque[LessonEvent]] = defaultdict(deque)
    for event in lesson.events:
        if event.absolute_tick >= start_tick:
            break
        key = (event.channel, event.note)
        if event.event_type == "note_on":
            active_before[key].append(event)
        elif active_before[key]:
            active_before[key].popleft()
    result: list[LessonEvent] = []
    active: dict[tuple[int, int], deque[LessonEvent]] = defaultdict(deque)
    for key in sorted(active_before):
        for ordinal, event in enumerate(active_before[key]):
            boundary = replace(
                event,
                event_id=f"{event.event_id}:segment-on:{ordinal}",
                absolute_tick=start_tick,
                delta_tick=0,
            )
            result.append(boundary)
            active[key].append(boundary)
    for event in lesson.events:
        if event.absolute_tick < start_tick or event.absolute_tick > end_tick:
            continue
        result.append(event)
        key = (event.channel, event.note)
        if event.event_type == "note_on":
            active[key].append(event)
        elif active[key]:
            active[key].popleft()
    for key in sorted(active):
        channel, note = key
        for ordinal, event in enumerate(active[key]):
            result.append(
                LessonEvent(
                    event_id=f"{event.event_id}:segment-off:{ordinal}",
                    event_type="note_off",
                    note=note,
                    velocity=0,
                    channel=channel,
                    status=0x80 | (channel - 1),
                    source_port=event.source_port,
                    content_origin=event.content_origin,
                    absolute_tick=end_tick,
                    delta_tick=0,
                )
            )
    return tuple(result)


def _clip_half_open_segment(
    lesson: LessonRevision, start_tick: int, end_tick: int
) -> tuple[LessonEvent, ...]:
    """Clip one declared segment as [start_tick, end_tick) without zero-length carry."""

    active: dict[tuple[int, int], deque[tuple[bool, LessonEvent]]] = defaultdict(deque)
    for event in lesson.events:
        if event.absolute_tick >= start_tick:
            break
        key = (event.channel, event.note)
        if event.event_type == "note_on":
            active[key].append((False, event))
        elif active[key]:
            active[key].popleft()

    at_start: list[LessonEvent] = []
    for event in lesson.events:
        if event.absolute_tick < start_tick:
            continue
        if event.absolute_tick > start_tick:
            break
        key = (event.channel, event.note)
        if event.event_type == "note_on":
            active[key].append((True, event))
            at_start.append(event)
            continue
        represented, _matched = active[key].popleft()
        if represented:
            at_start.append(event)

    boundary_on: list[LessonEvent] = []
    for key in sorted(active):
        represented_events: deque[tuple[bool, LessonEvent]] = deque()
        for ordinal, (represented, event) in enumerate(active[key]):
            if represented:
                represented_events.append((True, event))
                continue
            boundary = replace(
                event,
                event_id=f"{event.event_id}:segment-on:{ordinal}",
                absolute_tick=start_tick,
                delta_tick=0,
            )
            boundary_on.append(boundary)
            represented_events.append((True, boundary))
        active[key] = represented_events

    result = boundary_on + at_start
    for event in lesson.events:
        if event.absolute_tick <= start_tick:
            continue
        if event.absolute_tick >= end_tick:
            break
        key = (event.channel, event.note)
        result.append(event)
        if event.event_type == "note_on":
            active[key].append((True, event))
        else:
            active[key].popleft()

    for key in sorted(active):
        channel, note = key
        for ordinal, (_represented, event) in enumerate(active[key]):
            result.append(
                LessonEvent(
                    event_id=f"{event.event_id}:segment-off:{ordinal}",
                    event_type="note_off",
                    note=note,
                    velocity=0,
                    channel=channel,
                    status=0x80 | (channel - 1),
                    source_port=event.source_port,
                    content_origin=event.content_origin,
                    absolute_tick=end_tick,
                    delta_tick=0,
                )
            )
    return tuple(result)


def _canonical_payload(
    *,
    ppq: int,
    tempo_map: Sequence[TempoChange],
    time_signatures: Sequence[TimeSignature],
    events: Sequence[LessonEvent],
    segments: Sequence[LessonSegment],
) -> dict[str, Any]:
    return {
        "ppq": ppq,
        "tempo_map": [entry.to_dict() for entry in tempo_map],
        "time_signatures": [entry.to_dict() for entry in time_signatures],
        "events": [
            {
                "event_type": event.event_type,
                "note": event.note,
                "velocity": event.velocity,
                "channel": event.channel,
                "status": event.status,
                "absolute_tick": event.absolute_tick,
                "delta_tick": event.delta_tick,
            }
            for event in events
        ],
        "segments": [segment.to_dict() for segment in segments],
    }


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _validate_strict_ticks(entries: Sequence[Any], field: str) -> None:
    previous = -1
    for entry in entries:
        if entry.absolute_tick <= previous:
            raise LessonValidationError(f"{field} ticks must be strictly increasing", field=field)
        previous = entry.absolute_tick


def _validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise LessonValidationError(
            f"{field} must use lowercase letters, digits, dots, dashes, or underscores",
            field=field,
        )


def _validate_timestamp(value: str, field: str) -> None:
    _require_text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LessonValidationError(f"{field} must be an ISO-8601 timestamp", field=field) from exc
    if parsed.tzinfo is None:
        raise LessonValidationError(f"{field} must include a timezone", field=field)


def _require_text(value: Any, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LessonValidationError(
            f"{field} must be non-empty text no longer than {maximum} characters", field=field
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LessonValidationError(
            f"{field} must be an object", field=field, code="LESSON_CORRUPT"
        )
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise LessonValidationError(f"{field} must be an array", field=field, code="LESSON_CORRUPT")
    return value


def _string_sequence(value: Any, field: str) -> list[str]:
    return [_strict_str(item, field) for item in _list(value, field)]


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LessonValidationError(
            f"{field} must be an integer", field=field, code="LESSON_CORRUPT"
        )
    return value


def _strict_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise LessonValidationError(f"{field} must be text", field=field, code="LESSON_CORRUPT")
    return value


def _optional_str(value: Any, field: str) -> str | None:
    return None if value is None else _strict_str(value, field)


def _require_exact_keys(payload: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    if set(payload) != expected:
        raise LessonValidationError(
            f"{field} has missing or unknown fields", field=field, code="LESSON_CORRUPT"
        )


def _tempo_from_dict(value: Any) -> TempoChange:
    payload = _mapping(value, "tempo_map")
    _require_exact_keys(
        payload, frozenset({"absolute_tick", "microseconds_per_quarter"}), "tempo_map"
    )
    return TempoChange(
        _strict_int(payload["absolute_tick"], "tempo_map.absolute_tick"),
        _strict_int(payload["microseconds_per_quarter"], "tempo_map.microseconds_per_quarter"),
    )


def _signature_from_dict(value: Any) -> TimeSignature:
    payload = _mapping(value, "time_signatures")
    _require_exact_keys(
        payload, frozenset({"absolute_tick", "numerator", "denominator"}), "time_signatures"
    )
    return TimeSignature(
        _strict_int(payload["absolute_tick"], "time_signatures.absolute_tick"),
        _strict_int(payload["numerator"], "time_signatures.numerator"),
        _strict_int(payload["denominator"], "time_signatures.denominator"),
    )


def _event_from_dict(value: Any) -> LessonEvent:
    payload = _mapping(value, "events")
    _require_exact_keys(payload, _EVENT_KEYS, "events")
    return LessonEvent(
        event_id=_strict_str(payload["event_id"], "events.event_id"),
        event_type=_strict_str(payload["event_type"], "events.event_type"),  # type: ignore[arg-type]
        note=_strict_int(payload["note"], "events.note"),
        velocity=_strict_int(payload["velocity"], "events.velocity"),
        channel=_strict_int(payload["channel"], "events.channel"),
        status=_strict_int(payload["status"], "events.status"),
        source_port=_strict_str(payload["source_port"], "events.source_port"),
        content_origin=_strict_str(payload["content_origin"], "events.content_origin"),
        absolute_tick=_strict_int(payload["absolute_tick"], "events.absolute_tick"),
        delta_tick=_strict_int(payload["delta_tick"], "events.delta_tick"),
    )


def _segment_from_dict(value: Any) -> LessonSegment:
    payload = _mapping(value, "segments")
    _require_exact_keys(payload, _SEGMENT_KEYS, "segments")
    return LessonSegment(
        label=_strict_str(payload["label"], "segments.label"),
        start_tick=_strict_int(payload["start_tick"], "segments.start_tick"),
        end_tick=_strict_int(payload["end_tick"], "segments.end_tick"),
        hand=Hand(payload["hand"]) if payload["hand"] is not None else None,
        difficulty=(
            Difficulty(payload["difficulty"]) if payload["difficulty"] is not None else None
        ),
    )


_LESSON_KEYS = frozenset(
    {
        "schema_version",
        "lesson_id",
        "revision_id",
        "content_id",
        "created_at",
        "aliases",
        "title",
        "composer",
        "difficulty",
        "hand",
        "ppq",
        "tempo_map",
        "time_signatures",
        "provenance",
        "events",
        "segments",
        "max_polyphony",
    }
)
_PROVENANCE_KEYS = frozenset(
    {
        "acquisition",
        "citation",
        "fidelity",
        "authorization_timestamp",
        "provider",
        "model_version",
    }
)
_EVENT_KEYS = frozenset(
    {
        "event_id",
        "event_type",
        "note",
        "velocity",
        "channel",
        "status",
        "source_port",
        "content_origin",
        "absolute_tick",
        "delta_tick",
    }
)
_SEGMENT_KEYS = frozenset({"label", "start_tick", "end_tick", "hand", "difficulty"})
