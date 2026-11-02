"""Pure, bounded projection contracts for the internal coach note highway."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Literal

from halcyon.coach_models import LoopSchedule, ScheduledLoopEvent

MAX_HIGHWAY_SPANS = 96
MAX_TARGET_NOTES = 8
HIGHWAY_LOOKAHEAD_US = 4_000_000
HIGHWAY_TRAIL_US = 500_000

HighwayCueKind = Literal["halcyon", "target"]
HighwayProjectionError = Literal["malformed_note_pairs"]


@dataclass(frozen=True, slots=True)
class HighwayNoteSpan:
    """One paired note projected relative to the sampled strike rail."""

    note: int
    channel: int
    loop_index: int
    timeline_ordinal: int
    start_offset_us: int
    end_offset_us: int
    cue_kind: HighwayCueKind
    required: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.note <= 127:
            raise ValueError("highway note must be between 0 and 127")
        if not 1 <= self.channel <= 16:
            raise ValueError("highway channel must be between 1 and 16")
        if self.loop_index < 0 or self.timeline_ordinal < 0:
            raise ValueError("highway schedule indexes must be nonnegative")
        if self.end_offset_us < self.start_offset_us:
            raise ValueError("highway span cannot end before it starts")
        if self.cue_kind not in {"halcyon", "target"}:
            raise ValueError("highway cue kind is invalid")
        if not isinstance(self.required, bool):
            raise ValueError("highway required flag must be boolean")


@dataclass(frozen=True, slots=True)
class HighwayProjection:
    """Bounded result from one pure schedule projection."""

    spans: tuple[HighwayNoteSpan, ...] = ()
    target_notes: tuple[int, ...] = ()
    target_offset_us: int | None = None
    error: HighwayProjectionError | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "spans", tuple(self.spans))
        object.__setattr__(self, "target_notes", tuple(self.target_notes))
        if len(self.spans) > MAX_HIGHWAY_SPANS:
            raise ValueError("highway projection exceeds its span limit")
        if len(self.target_notes) > MAX_TARGET_NOTES:
            raise ValueError("highway projection exceeds its target-note limit")
        if any(not 0 <= note <= 127 for note in self.target_notes):
            raise ValueError("highway target note must be between 0 and 127")


@dataclass(frozen=True, slots=True)
class CoachReviewSummary:
    """Aggregate-only review fields already allowed by the coach contract."""

    status: str
    overall_score_percent: float | None
    matched_note_count: int | None
    expected_note_count: int | None
    tips: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tips", tuple(self.tips))
        if len(self.status) > 32:
            raise ValueError("review status exceeds its bounded length")
        if len(self.tips) > 3 or any(len(tip) > 300 for tip in self.tips):
            raise ValueError("review tips exceed their bounded limits")


@dataclass(frozen=True, slots=True)
class CoachVisualizationSnapshot:
    """Owner-thread-only immutable presentation state for the coach highway."""

    active: bool
    phase: str
    sampled_at_ns: int
    presentation_anchor_ns: int | None = None
    presentation_deadline_ns: int | None = None
    segment_label: str | None = None
    tempo_percent: int | None = None
    loop_count: int = 0
    fidelity: str | None = None
    spans: tuple[HighwayNoteSpan, ...] = ()
    current_loop: int | None = None
    target_notes: tuple[int, ...] = ()
    target_offset_us: int | None = None
    count_in_remaining: int | None = None
    review: CoachReviewSummary | None = None
    projection_error: HighwayProjectionError | None = None
    practice_level: str = "full"
    required_note_count: int = 0
    ghost_note_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "spans", tuple(self.spans))
        object.__setattr__(self, "target_notes", tuple(self.target_notes))
        if len(self.phase) > 24:
            raise ValueError("coach phase exceeds its bounded length")
        if self.segment_label is not None and len(self.segment_label) > 80:
            raise ValueError("segment label exceeds its bounded length")
        if self.fidelity is not None and len(self.fidelity) > 24:
            raise ValueError("fidelity exceeds its bounded length")
        if len(self.spans) > MAX_HIGHWAY_SPANS:
            raise ValueError("coach snapshot exceeds its span limit")
        if len(self.target_notes) > MAX_TARGET_NOTES:
            raise ValueError("coach snapshot exceeds its target-note limit")
        if any(not 0 <= note <= 127 for note in self.target_notes):
            raise ValueError("coach target note must be between 0 and 127")
        if self.practice_level not in {"melody", "essentials", "full"}:
            raise ValueError("coach practice level is invalid")
        if self.required_note_count < 0 or self.ghost_note_count < 0:
            raise ValueError("coach note counts must be nonnegative")


@dataclass(frozen=True, slots=True)
class _PairedSpan:
    note: int
    channel: int
    loop_index: int
    timeline_ordinal: int
    start_us: int
    end_us: int
    required: bool


def project_note_spans(
    schedule: LoopSchedule,
    *,
    elapsed_us: int,
    cue_kind: HighwayCueKind,
    lookahead_us: int = HIGHWAY_LOOKAHEAD_US,
    trail_us: int = HIGHWAY_TRAIL_US,
) -> HighwayProjection:
    """Pair and window one exact loop schedule without recomputing musical time.

    Notes are paired FIFO by loop, channel, and pitch. Any orphan onset or release
    invalidates the whole projection so presentation never fabricates note length.
    """

    if not isinstance(schedule, LoopSchedule):
        raise TypeError("schedule must be a LoopSchedule")
    if not isinstance(elapsed_us, int):
        raise TypeError("elapsed_us must be an integer")
    if lookahead_us < 0 or trail_us < 0:
        raise ValueError("projection windows must be nonnegative")
    if cue_kind not in {"halcyon", "target"}:
        raise ValueError("cue_kind must be halcyon or target")

    paired = _pair_schedule(schedule)
    if paired is None:
        return HighwayProjection(error="malformed_note_pairs")

    window_start_us = -trail_us
    window_end_us = lookahead_us
    visible = [
        HighwayNoteSpan(
            note=span.note,
            channel=span.channel,
            loop_index=span.loop_index,
            timeline_ordinal=span.timeline_ordinal,
            start_offset_us=max(window_start_us, span.start_us - elapsed_us),
            end_offset_us=min(window_end_us, span.end_us - elapsed_us),
            cue_kind=cue_kind,
            required=span.required,
        )
        for span in paired
        if span.end_us - elapsed_us >= window_start_us
        and span.start_us - elapsed_us <= window_end_us
    ]
    # Optional previews must not crowd required targets out of the bounded window.
    visible.sort(
        key=lambda span: (
            not span.required,
            span.start_offset_us,
            span.loop_index,
            span.timeline_ordinal,
        )
    )
    visible = visible[:MAX_HIGHWAY_SPANS]
    visible.sort(key=lambda span: (span.start_offset_us, span.loop_index, span.timeline_ordinal))

    upcoming = [span for span in paired if span.required and span.start_us >= elapsed_us]
    if upcoming:
        target_start_us = upcoming[0].start_us
        target_notes = tuple(
            sorted({span.note for span in upcoming if span.start_us == target_start_us})
        )[:MAX_TARGET_NOTES]
        target_offset_us = target_start_us - elapsed_us
    else:
        target_notes = ()
        target_offset_us = None

    return HighwayProjection(
        spans=tuple(visible),
        target_notes=target_notes,
        target_offset_us=target_offset_us,
    )


def _pair_schedule(schedule: LoopSchedule) -> tuple[_PairedSpan, ...] | None:
    pending: dict[tuple[int, int, int], deque[ScheduledLoopEvent]] = defaultdict(deque)
    paired: list[_PairedSpan] = []
    for scheduled in schedule.events:
        event = scheduled.event
        key = (scheduled.loop_index, event.channel, event.note)
        if event.event_type == "note_on":
            pending[key].append(scheduled)
            continue
        if not pending[key]:
            return None
        onset = pending[key].popleft()
        if scheduled.offset_us < onset.offset_us or event.required != onset.event.required:
            return None
        paired.append(
            _PairedSpan(
                note=event.note,
                channel=event.channel,
                loop_index=scheduled.loop_index,
                timeline_ordinal=onset.timeline_ordinal,
                start_us=onset.offset_us,
                end_us=scheduled.offset_us,
                required=onset.event.required,
            )
        )
    if any(queue for queue in pending.values()):
        return None
    paired.sort(key=lambda span: (span.start_us, span.loop_index, span.timeline_ordinal))
    return tuple(paired)
