"""Deterministic, offline scoring for bounded Halcyon coach attempts.

Scoring consumes a resolved integer-microsecond lesson timeline and post-bus MIDI
events. It deliberately does not read the display log, persist raw attempts, or call a
model. Device latency is removed before a chord-aware sequence alignment, after which
fixed/versioned rubric weights produce a bounded aggregate report.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from typing import Literal

from halcyon.coach_models import LatencyProfile, LessonTimeline, ResolvedLessonEvent
from halcyon.midi import MidiEvent, MidiOrigin

ScoreStatus = Literal["complete", "empty", "truncated"]
LatencySource = Literal["calibrated", "estimated", "default"]


@dataclass(frozen=True, slots=True)
class ScoreConfig:
    """Fixed, versioned scoring rubric and bounded tolerances."""

    version: str = "halcyon-score-v1"
    pitch_weight_percent: int = 45
    onset_weight_percent: int = 25
    duration_weight_percent: int = 15
    completion_weight_percent: int = 15
    min_onset_tolerance_us: int = 45_000
    max_onset_tolerance_us: int = 180_000
    jitter_tolerance_multiplier: int = 3
    duration_tolerance_us: int = 90_000
    chord_window_us: int = 35_000
    latency_min_matches: int = 3
    min_latency_offset_us: int = -250_000
    max_latency_offset_us: int = 500_000
    max_latency_jitter_us: int = 80_000
    max_observed_events: int = 2_048
    max_issues: int = 8
    max_tips: int = 3

    def __post_init__(self) -> None:
        weights = (
            self.pitch_weight_percent,
            self.onset_weight_percent,
            self.duration_weight_percent,
            self.completion_weight_percent,
        )
        if not self.version or len(self.version) > 64:
            raise ValueError("score version must contain 1..64 characters")
        if any(weight < 0 for weight in weights) or sum(weights) != 100:
            raise ValueError("score weights must be nonnegative and sum to 100")
        if not 1 <= self.min_onset_tolerance_us <= self.max_onset_tolerance_us:
            raise ValueError("onset tolerance bounds are invalid")
        if self.jitter_tolerance_multiplier < 1:
            raise ValueError("jitter_tolerance_multiplier must be positive")
        if self.duration_tolerance_us < 1 or self.chord_window_us < 0:
            raise ValueError("duration and chord tolerances must be valid")
        if self.latency_min_matches < 1:
            raise ValueError("latency_min_matches must be positive")
        if self.min_latency_offset_us > self.max_latency_offset_us:
            raise ValueError("latency offset bounds are invalid")
        if self.max_latency_jitter_us < 0:
            raise ValueError("max_latency_jitter_us cannot be negative")
        if not 1 <= self.max_observed_events <= 2_048:
            raise ValueError("max_observed_events must be between 1 and 2048")
        if not 1 <= self.max_issues <= 8:
            raise ValueError("max_issues must be between 1 and 8")
        if not 1 <= self.max_tips <= 3:
            raise ValueError("max_tips must be between 1 and 3")

    def weights_public_dict(self) -> dict[str, int]:
        return {
            "pitch": self.pitch_weight_percent,
            "onset": self.onset_weight_percent,
            "duration": self.duration_weight_percent,
            "completion": self.completion_weight_percent,
        }


DEFAULT_SCORE_CONFIG = ScoreConfig()


@dataclass(frozen=True, slots=True)
class ScoreRequest:
    """One memory-only attempt scored against one already-resolved lesson selection."""

    attempt_id: str
    session_id: str
    timeline: LessonTimeline
    observed_events: tuple[MidiEvent, ...]
    capture_started_ns: int
    capture_ended_ns: int
    latency_profile: LatencyProfile | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if not self.attempt_id or len(self.attempt_id) > 128:
            raise ValueError("attempt_id must contain 1..128 characters")
        if not self.session_id or len(self.session_id) > 128:
            raise ValueError("session_id must contain 1..128 characters")
        if not isinstance(self.observed_events, tuple):
            raise TypeError("observed_events must be a tuple")
        if not isinstance(self.capture_started_ns, int) or not isinstance(
            self.capture_ended_ns, int
        ):
            raise TypeError("capture timestamps must be integer nanoseconds")
        if self.capture_started_ns < 0 or self.capture_ended_ns < self.capture_started_ns:
            raise ValueError("capture_ended_ns must be at or after capture_started_ns")


@dataclass(frozen=True, slots=True)
class LatencyEstimate:
    """Bounded global latency removed from observed onsets before alignment."""

    source: LatencySource
    offset_us: int
    jitter_us: int
    tolerance_us: int
    match_count: int

    def to_public_dict(self) -> dict[str, float | int | str]:
        return {
            "source": self.source,
            "offset_ms": _milliseconds(self.offset_us),
            "jitter_ms": _milliseconds(self.jitter_us),
            "onset_tolerance_ms": _milliseconds(self.tolerance_us),
            "match_count": self.match_count,
        }


@dataclass(frozen=True, slots=True)
class ScoreMetrics:
    """Aggregate-only fixed-rubric attempt measurements."""

    scoring_version: str
    expected_note_count: int
    observed_note_count: int
    matched_note_count: int
    pitch_accuracy_percent: float
    missed_count: int
    wrong_pitch_count: int
    extra_count: int
    onset_mean_deviation_ms: float | None
    onset_p95_deviation_ms: float | None
    held_duration_mean_deviation_ms: float | None
    completion_percent: float
    overall_score_percent: float
    truncated: bool
    onset_tolerance_ms: float
    duration_tolerance_ms: float
    weights_percent: tuple[tuple[str, int], ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "scoring_version": self.scoring_version,
            "expected_note_count": self.expected_note_count,
            "observed_note_count": self.observed_note_count,
            "matched_note_count": self.matched_note_count,
            "pitch_accuracy_percent": self.pitch_accuracy_percent,
            "missed_count": self.missed_count,
            "wrong_pitch_count": self.wrong_pitch_count,
            "extra_count": self.extra_count,
            "onset_mean_deviation_ms": self.onset_mean_deviation_ms,
            "onset_p95_deviation_ms": self.onset_p95_deviation_ms,
            "held_duration_mean_deviation_ms": self.held_duration_mean_deviation_ms,
            "completion_percent": self.completion_percent,
            "overall_score_percent": self.overall_score_percent,
            "truncated": self.truncated,
            "tolerances_ms": {
                "onset": self.onset_tolerance_ms,
                "duration": self.duration_tolerance_ms,
            },
            "weights_percent": dict(self.weights_percent),
        }


@dataclass(frozen=True, slots=True)
class CoachingIssue:
    """One bounded, cited mismatch; never a raw event-history record."""

    code: str
    message: str
    expected_lesson_event_id: str | None = None
    expected_tick: int | None = None
    expected_note: int | None = None
    observed_note: int | None = None
    observed_offset_ms: float | None = None
    deviation_ms: float | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "expected_lesson_event_id": self.expected_lesson_event_id,
            "expected_tick": self.expected_tick,
            "expected_note": self.expected_note,
            "observed_note": self.observed_note,
            "observed_offset_ms": self.observed_offset_ms,
            "deviation_ms": self.deviation_ms,
        }


@dataclass(frozen=True, slots=True)
class PracticeRecommendation:
    """Deterministic next action derived only from aggregate scoring."""

    action: Literal["increase_tempo", "repeat_segment", "slow_down"]
    tempo_percent: int
    segment_label: str | None
    loops: int
    focus: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "tempo_percent": self.tempo_percent,
            "segment_label": self.segment_label,
            "loops": self.loops,
            "focus": self.focus,
        }


@dataclass(frozen=True, slots=True)
class CoachingReport:
    """Bounded deterministic result safe for chat, app state, and MCP run output."""

    attempt_id: str
    session_id: str
    status: ScoreStatus
    lesson_id: str
    revision_id: str
    content_id: str
    segment_label: str | None
    tempo_percent: int
    metrics: ScoreMetrics
    latency: LatencyEstimate
    issues: tuple[CoachingIssue, ...]
    tips: tuple[str, ...]
    recommendation: PracticeRecommendation

    def to_public_dict(self) -> dict[str, object]:
        """Return a JSON-safe aggregate envelope with hard-bounded collections."""

        return {
            "schema_version": 1,
            "attempt_id": self.attempt_id,
            "session_id": self.session_id,
            "status": self.status,
            "lesson": {
                "lesson_id": self.lesson_id,
                "revision_id": self.revision_id,
                "content_id": self.content_id,
                "segment_label": self.segment_label,
                "tempo_percent": self.tempo_percent,
            },
            "metrics": self.metrics.to_public_dict(),
            "latency": self.latency.to_public_dict(),
            "issues": [issue.to_public_dict() for issue in self.issues[:8]],
            "tips": list(self.tips[:3]),
            "recommendation": self.recommendation.to_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class _ExpectedNote:
    lesson_event_id: str
    note: int
    channel: int
    onset_us: int
    absolute_tick: int
    duration_us: int | None = None


@dataclass(frozen=True, slots=True)
class _ObservedNote:
    voice_id: int
    event_id: str
    note: int
    channel: int
    onset_us: int
    sequence: int
    duration_us: int | None = None


@dataclass(frozen=True, slots=True)
class _AlignmentStep:
    kind: Literal["paired", "missed", "extra"]
    expected: _ExpectedNote | None
    observed: _ObservedNote | None


def score_attempt(
    request: ScoreRequest,
    *,
    config: ScoreConfig = DEFAULT_SCORE_CONFIG,
) -> CoachingReport:
    """Score one bounded human attempt without persistence or external calls."""

    if len(request.observed_events) > config.max_observed_events:
        raise ValueError(
            f"observed_events exceeds the {config.max_observed_events}-event scoring limit"
        )

    expected = _pair_expected_notes(request.timeline.events)
    observed = _pair_observed_notes(request)
    latency = _latency_estimate(expected, observed, request.latency_profile, config)
    adjusted = tuple(replace(note, onset_us=note.onset_us - latency.offset_us) for note in observed)
    alignment = _align_notes(expected, adjusted, latency.tolerance_us, config)

    exact_pairs: list[tuple[_ExpectedNote, _ObservedNote]] = []
    wrong_pairs: list[tuple[_ExpectedNote, _ObservedNote]] = []
    missed: list[_ExpectedNote] = []
    extras: list[_ObservedNote] = []
    for step in alignment:
        if step.kind == "missed":
            assert step.expected is not None
            missed.append(step.expected)
        elif step.kind == "extra":
            assert step.observed is not None
            extras.append(step.observed)
        else:
            assert step.expected is not None and step.observed is not None
            if step.expected.note == step.observed.note:
                exact_pairs.append((step.expected, step.observed))
            else:
                wrong_pairs.append((step.expected, step.observed))

    onset_deviations = tuple(
        abs(observed_note.onset_us - expected_note.onset_us)
        for expected_note, observed_note in exact_pairs
    )
    duration_deviations = tuple(
        abs(observed_note.duration_us - expected_note.duration_us)
        for expected_note, observed_note in exact_pairs
        if expected_note.duration_us is not None and observed_note.duration_us is not None
    )
    missing_release_count = sum(
        1
        for expected_note, observed_note in exact_pairs
        if expected_note.duration_us is not None and observed_note.duration_us is None
    )

    expected_count = len(expected)
    correct_count = len(exact_pairs)
    pitch_denominator = expected_count + len(extras)
    pitch_accuracy = _percent(correct_count, pitch_denominator)
    completion = _percent(expected_count - len(missed), expected_count)
    onset_quality = _timing_quality(onset_deviations, latency.tolerance_us)
    duration_quality = _duration_quality(exact_pairs, config.duration_tolerance_us)
    overall = _weighted_score(
        pitch_accuracy,
        onset_quality,
        duration_quality,
        completion,
        config,
    )

    metrics = ScoreMetrics(
        scoring_version=config.version,
        expected_note_count=expected_count,
        observed_note_count=len(observed),
        matched_note_count=correct_count,
        pitch_accuracy_percent=pitch_accuracy,
        missed_count=len(missed),
        wrong_pitch_count=len(wrong_pairs),
        extra_count=len(extras),
        onset_mean_deviation_ms=_mean_ms(onset_deviations),
        onset_p95_deviation_ms=_percentile_ms(onset_deviations, 95),
        held_duration_mean_deviation_ms=_mean_ms(duration_deviations),
        completion_percent=completion,
        overall_score_percent=overall,
        truncated=request.truncated,
        onset_tolerance_ms=_milliseconds(latency.tolerance_us),
        duration_tolerance_ms=_milliseconds(config.duration_tolerance_us),
        weights_percent=tuple(config.weights_public_dict().items()),
    )
    status: ScoreStatus
    if request.truncated:
        status = "truncated"
    elif not observed:
        status = "empty"
    else:
        status = "complete"

    issues = _build_issues(
        exact_pairs,
        wrong_pairs,
        missed,
        extras,
        latency.tolerance_us,
        config,
    )[: config.max_issues]
    tips = _build_tips(status, metrics, missing_release_count)[: config.max_tips]
    recommendation = _build_recommendation(
        status,
        metrics,
        request.timeline,
        missing_release_count,
    )
    return CoachingReport(
        attempt_id=request.attempt_id,
        session_id=request.session_id,
        status=status,
        lesson_id=request.timeline.lesson_id,
        revision_id=request.timeline.revision_id,
        content_id=request.timeline.content_id,
        segment_label=request.timeline.segment_label,
        tempo_percent=request.timeline.tempo_percent,
        metrics=metrics,
        latency=latency,
        issues=issues,
        tips=tips,
        recommendation=recommendation,
    )


def _pair_expected_notes(events: tuple[ResolvedLessonEvent, ...]) -> tuple[_ExpectedNote, ...]:
    indexed_events = tuple(enumerate(events))
    for _index, event in indexed_events:
        if not isinstance(event.absolute_tick, int) or not isinstance(event.absolute_us, int):
            raise TypeError("resolved lesson time must use integer ticks and microseconds")
        if not isinstance(event.delta_tick, int) or not isinstance(event.delta_us, int):
            raise TypeError("resolved lesson deltas must use integer ticks and microseconds")
        if event.absolute_tick < 0 or event.absolute_us < 0:
            raise ValueError("resolved lesson time cannot be negative")

    ordered = sorted(
        indexed_events,
        key=lambda item: (item[1].absolute_us, item[1].absolute_tick, item[0]),
    )
    active: dict[tuple[int, int], deque[int]] = defaultdict(deque)
    notes: list[_ExpectedNote] = []
    for _ordinal, event in ordered:
        key = (event.channel, event.note)
        if event.event_type == "note_on":
            active[key].append(len(notes))
            notes.append(
                _ExpectedNote(
                    lesson_event_id=event.lesson_event_id,
                    note=event.note,
                    channel=event.channel,
                    onset_us=event.absolute_us,
                    absolute_tick=event.absolute_tick,
                )
            )
        elif event.event_type == "note_off" and active[key]:
            note_index = active[key].popleft()
            onset_us = notes[note_index].onset_us
            notes[note_index] = replace(
                notes[note_index],
                duration_us=max(0, event.absolute_us - onset_us),
            )
    return tuple(notes)


def _pair_observed_notes(request: ScoreRequest) -> tuple[_ObservedNote, ...]:
    seen_event_ids: set[str] = set()
    eligible: list[MidiEvent] = []
    for event in request.observed_events:
        if event.origin is not MidiOrigin.HUMAN:
            continue
        if event.sequence <= 0:
            continue
        if (
            not request.capture_started_ns
            <= event.dispatched_monotonic_ns
            <= request.capture_ended_ns
        ):
            continue
        if event.session_id != request.session_id:
            continue
        if event.attempt_id is not None and event.attempt_id != request.attempt_id:
            continue
        if event.event_id in seen_event_ids:
            continue
        seen_event_ids.add(event.event_id)
        eligible.append(event)

    eligible.sort(
        key=lambda event: (
            event.dispatched_monotonic_ns,
            event.sequence,
            event.event_id,
        )
    )
    active: dict[int, int] = {}
    notes: list[_ObservedNote] = []
    for event in eligible:
        if event.is_press:
            if event.voice_id in active:
                continue
            active[event.voice_id] = len(notes)
            notes.append(
                _ObservedNote(
                    voice_id=event.voice_id,
                    event_id=event.event_id,
                    note=event.note,
                    channel=event.channel,
                    onset_us=(event.dispatched_monotonic_ns - request.capture_started_ns) // 1_000,
                    sequence=event.sequence,
                )
            )
            continue
        if not event.is_release:
            continue
        note_index = active.get(event.voice_id)
        if note_index is None:
            continue
        active_note = notes[note_index]
        if active_note.note != event.note or active_note.channel != event.channel:
            continue
        active.pop(event.voice_id, None)
        duration_us = (
            event.dispatched_monotonic_ns - request.capture_started_ns
        ) // 1_000 - active_note.onset_us
        notes[note_index] = replace(active_note, duration_us=max(0, duration_us))
    return tuple(notes)


def _latency_estimate(
    expected: tuple[_ExpectedNote, ...],
    observed: tuple[_ObservedNote, ...],
    profile: LatencyProfile | None,
    config: ScoreConfig,
) -> LatencyEstimate:
    if profile is not None:
        offset_us = min(
            config.max_latency_offset_us,
            max(config.min_latency_offset_us, profile.offset_us),
        )
        jitter_us = min(config.max_latency_jitter_us, max(0, profile.jitter_us))
        return LatencyEstimate(
            source="calibrated",
            offset_us=offset_us,
            jitter_us=jitter_us,
            tolerance_us=_onset_tolerance(jitter_us, config),
            match_count=max(0, profile.sample_count),
        )

    expected_by_pitch: dict[int, list[_ExpectedNote]] = defaultdict(list)
    observed_by_pitch: dict[int, list[_ObservedNote]] = defaultdict(list)
    for note in expected:
        expected_by_pitch[note.note].append(note)
    for note in observed:
        observed_by_pitch[note.note].append(note)

    candidates: list[int] = []
    for pitch in sorted(expected_by_pitch.keys() & observed_by_pitch.keys()):
        expected_notes = sorted(expected_by_pitch[pitch], key=lambda note: note.onset_us)
        observed_notes = sorted(observed_by_pitch[pitch], key=lambda note: note.onset_us)
        if len(expected_notes) != len(observed_notes):
            continue
        for expected_note, observed_note in zip(expected_notes, observed_notes, strict=True):
            offset = observed_note.onset_us - expected_note.onset_us
            if config.min_latency_offset_us <= offset <= config.max_latency_offset_us:
                candidates.append(offset)

    if len(candidates) >= config.latency_min_matches:
        preliminary_offset = _median_int(candidates)
        preliminary_jitter = _median_int(
            [abs(candidate - preliminary_offset) for candidate in candidates]
        )
        if preliminary_jitter <= config.max_latency_jitter_us:
            radius = max(
                config.min_onset_tolerance_us,
                max(1, preliminary_jitter) * config.jitter_tolerance_multiplier,
            )
            inliers = [
                candidate
                for candidate in candidates
                if abs(candidate - preliminary_offset) <= radius
            ]
            if len(inliers) >= config.latency_min_matches:
                offset_us = _median_int(inliers)
                jitter_us = _median_int([abs(value - offset_us) for value in inliers])
                return LatencyEstimate(
                    source="estimated",
                    offset_us=offset_us,
                    jitter_us=jitter_us,
                    tolerance_us=_onset_tolerance(jitter_us, config),
                    match_count=len(inliers),
                )

    return LatencyEstimate(
        source="default",
        offset_us=0,
        jitter_us=0,
        tolerance_us=config.min_onset_tolerance_us,
        match_count=0,
    )


def _onset_tolerance(jitter_us: int, config: ScoreConfig) -> int:
    derived = jitter_us * config.jitter_tolerance_multiplier
    return min(
        config.max_onset_tolerance_us,
        max(config.min_onset_tolerance_us, derived),
    )


def _align_notes(
    expected: tuple[_ExpectedNote, ...],
    observed: tuple[_ObservedNote, ...],
    tolerance_us: int,
    config: ScoreConfig,
) -> tuple[_AlignmentStep, ...]:
    expected_ordered = _chord_order(expected, config.chord_window_us)
    observed_ordered = _chord_order(observed, config.chord_window_us)
    expected_count = len(expected_ordered)
    observed_count = len(observed_ordered)
    gap_cost = 1_000

    width = observed_count + 1
    directions = bytearray((expected_count + 1) * width)
    previous = [index * gap_cost for index in range(width)]
    for observed_index in range(1, width):
        directions[observed_index] = 2

    for expected_index in range(1, expected_count + 1):
        current = [expected_index * gap_cost] + [0] * observed_count
        directions[expected_index * width] = 1
        expected_note = expected_ordered[expected_index - 1]
        for observed_index in range(1, observed_count + 1):
            observed_note = observed_ordered[observed_index - 1]
            diagonal = previous[observed_index - 1] + _pair_cost(
                expected_note,
                observed_note,
                tolerance_us,
            )
            delete = previous[observed_index] + gap_cost
            insert = current[observed_index - 1] + gap_cost
            if diagonal <= delete and diagonal <= insert:
                current[observed_index] = diagonal
                directions[expected_index * width + observed_index] = 0
            elif delete <= insert:
                current[observed_index] = delete
                directions[expected_index * width + observed_index] = 1
            else:
                current[observed_index] = insert
                directions[expected_index * width + observed_index] = 2
        previous = current

    steps: list[_AlignmentStep] = []
    expected_index = expected_count
    observed_index = observed_count
    while expected_index or observed_index:
        direction = directions[expected_index * width + observed_index]
        if expected_index and observed_index and direction == 0:
            steps.append(
                _AlignmentStep(
                    kind="paired",
                    expected=expected_ordered[expected_index - 1],
                    observed=observed_ordered[observed_index - 1],
                )
            )
            expected_index -= 1
            observed_index -= 1
        elif expected_index and (not observed_index or direction == 1):
            steps.append(
                _AlignmentStep(
                    kind="missed",
                    expected=expected_ordered[expected_index - 1],
                    observed=None,
                )
            )
            expected_index -= 1
        else:
            steps.append(
                _AlignmentStep(
                    kind="extra",
                    expected=None,
                    observed=observed_ordered[observed_index - 1],
                )
            )
            observed_index -= 1
    steps.reverse()
    return tuple(steps)


def _pair_cost(
    expected: _ExpectedNote,
    observed: _ObservedNote,
    tolerance_us: int,
) -> int:
    onset_deviation = abs(observed.onset_us - expected.onset_us)
    if expected.note == observed.note:
        return min(2_200, (onset_deviation * 400) // max(1, tolerance_us))
    if onset_deviation <= tolerance_us * 2:
        pitch_distance = min(12, abs(observed.note - expected.note))
        return 1_250 + pitch_distance * 20
    return 2_200


def _chord_order(notes: tuple[object, ...], chord_window_us: int) -> tuple:
    ordered = sorted(
        notes,
        key=lambda note: (
            note.onset_us,
            note.note,
            note.channel,
        ),
    )
    result: list[object] = []
    index = 0
    while index < len(ordered):
        chord_start = ordered[index].onset_us
        end = index + 1
        while end < len(ordered) and ordered[end].onset_us - chord_start <= chord_window_us:
            end += 1
        result.extend(
            sorted(
                ordered[index:end],
                key=lambda note: (note.note, note.channel, note.onset_us),
            )
        )
        index = end
    return tuple(result)


def _timing_quality(deviations_us: tuple[int, ...], tolerance_us: int) -> float:
    if not deviations_us:
        return 0.0
    scale = max(1, tolerance_us * 4)
    values = [max(0.0, 100.0 - deviation * 100.0 / scale) for deviation in deviations_us]
    return _rounded(sum(values) / len(values))


def _duration_quality(
    pairs: list[tuple[_ExpectedNote, _ObservedNote]],
    tolerance_us: int,
) -> float:
    expected_durations = [pair for pair in pairs if pair[0].duration_us is not None]
    if not expected_durations:
        return 0.0
    scale = max(1, tolerance_us * 4)
    values: list[float] = []
    for expected, observed in expected_durations:
        if observed.duration_us is None:
            values.append(0.0)
            continue
        deviation = abs(observed.duration_us - expected.duration_us)
        values.append(max(0.0, 100.0 - deviation * 100.0 / scale))
    return _rounded(sum(values) / len(values))


def _weighted_score(
    pitch: float,
    onset: float,
    duration: float,
    completion: float,
    config: ScoreConfig,
) -> float:
    total = (
        pitch * config.pitch_weight_percent
        + onset * config.onset_weight_percent
        + duration * config.duration_weight_percent
        + completion * config.completion_weight_percent
    ) / 100
    return _rounded(min(100.0, max(0.0, total)))


def _build_issues(
    exact_pairs: list[tuple[_ExpectedNote, _ObservedNote]],
    wrong_pairs: list[tuple[_ExpectedNote, _ObservedNote]],
    missed: list[_ExpectedNote],
    extras: list[_ObservedNote],
    onset_tolerance_us: int,
    config: ScoreConfig,
) -> tuple[CoachingIssue, ...]:
    ranked: list[tuple[int, int, CoachingIssue]] = []
    for expected, observed in wrong_pairs:
        ranked.append(
            (
                expected.onset_us,
                0,
                CoachingIssue(
                    code="WRONG_PITCH",
                    message=f"Play MIDI note {expected.note} here, not {observed.note}.",
                    expected_lesson_event_id=expected.lesson_event_id,
                    expected_tick=expected.absolute_tick,
                    expected_note=expected.note,
                    observed_note=observed.note,
                    observed_offset_ms=_milliseconds(observed.onset_us),
                ),
            )
        )
    for expected in missed:
        ranked.append(
            (
                expected.onset_us,
                1,
                CoachingIssue(
                    code="MISSED_NOTE",
                    message=f"Add MIDI note {expected.note} at this lesson position.",
                    expected_lesson_event_id=expected.lesson_event_id,
                    expected_tick=expected.absolute_tick,
                    expected_note=expected.note,
                ),
            )
        )
    for observed in extras:
        ranked.append(
            (
                observed.onset_us,
                2,
                CoachingIssue(
                    code="EXTRA_NOTE",
                    message=f"Leave out the extra MIDI note {observed.note} here.",
                    observed_note=observed.note,
                    observed_offset_ms=_milliseconds(observed.onset_us),
                ),
            )
        )
    for expected, observed in exact_pairs:
        signed_onset = observed.onset_us - expected.onset_us
        if abs(signed_onset) > onset_tolerance_us:
            correction = "earlier" if signed_onset > 0 else "later"
            ranked.append(
                (
                    expected.onset_us,
                    3,
                    CoachingIssue(
                        code="LATE_ONSET" if signed_onset > 0 else "EARLY_ONSET",
                        message=(
                            f"Start MIDI note {expected.note} {abs(signed_onset) / 1_000:.1f} ms "
                            f"{correction}."
                        ),
                        expected_lesson_event_id=expected.lesson_event_id,
                        expected_tick=expected.absolute_tick,
                        expected_note=expected.note,
                        observed_note=observed.note,
                        observed_offset_ms=_milliseconds(observed.onset_us),
                        deviation_ms=_milliseconds(signed_onset),
                    ),
                )
            )
        if expected.duration_us is None or observed.duration_us is None:
            if expected.duration_us is not None and observed.duration_us is None:
                ranked.append(
                    (
                        expected.onset_us,
                        4,
                        CoachingIssue(
                            code="MISSING_RELEASE",
                            message=(
                                f"No in-window release was captured for MIDI note {expected.note}."
                            ),
                            expected_lesson_event_id=expected.lesson_event_id,
                            expected_tick=expected.absolute_tick,
                            expected_note=expected.note,
                            observed_note=observed.note,
                            observed_offset_ms=_milliseconds(observed.onset_us),
                        ),
                    )
                )
            continue
        signed_duration = observed.duration_us - expected.duration_us
        if abs(signed_duration) > config.duration_tolerance_us:
            correction = "shorter" if signed_duration > 0 else "longer"
            ranked.append(
                (
                    expected.onset_us,
                    4,
                    CoachingIssue(
                        code="HOLD_TOO_LONG" if signed_duration > 0 else "HOLD_TOO_SHORT",
                        message=(
                            f"Hold MIDI note {expected.note} {abs(signed_duration) / 1_000:.1f} ms "
                            f"{correction}."
                        ),
                        expected_lesson_event_id=expected.lesson_event_id,
                        expected_tick=expected.absolute_tick,
                        expected_note=expected.note,
                        observed_note=observed.note,
                        observed_offset_ms=_milliseconds(observed.onset_us),
                        deviation_ms=_milliseconds(signed_duration),
                    ),
                )
            )
    ranked.sort(key=lambda item: (item[0], item[1], item[2].code))
    return tuple(item[2] for item in ranked)


def _build_tips(
    status: ScoreStatus,
    metrics: ScoreMetrics,
    missing_release_count: int,
) -> tuple[str, ...]:
    tips: list[str] = []
    if status == "truncated":
        tips.append("Repeat the full capture window so the ending can be scored fairly.")
    if status == "empty":
        tips.append("Play after the count-in; no in-window human note-on was captured.")
    if missing_release_count:
        tips.append(
            "Release every held key inside the capture window so note lengths can be scored."
        )
    if metrics.wrong_pitch_count or metrics.missed_count:
        tips.append("Loop the cited positions slowly and secure the note sequence first.")
    if metrics.extra_count:
        tips.append("Use a lighter, deliberate attack to remove the cited extra notes.")
    if (
        metrics.onset_p95_deviation_ms is not None
        and metrics.onset_p95_deviation_ms > metrics.onset_tolerance_ms
    ):
        tips.append("Practice with an even internal pulse and align each onset to the beat.")
    if (
        metrics.held_duration_mean_deviation_ms is not None
        and metrics.held_duration_mean_deviation_ms > metrics.duration_tolerance_ms
    ):
        tips.append("Release each key with the phrase instead of focusing only on attacks.")
    if not tips:
        tips.append("Keep the same relaxed motion, then try the recommended tempo step.")
    return tuple(tips)


def _build_recommendation(
    status: ScoreStatus,
    metrics: ScoreMetrics,
    timeline: LessonTimeline,
    missing_release_count: int,
) -> PracticeRecommendation:
    tempo = timeline.tempo_percent
    if status == "empty":
        return PracticeRecommendation(
            action="slow_down",
            tempo_percent=max(25, tempo - 10),
            segment_label=timeline.segment_label,
            loops=3,
            focus="enter after the count-in and complete the segment",
        )
    if status == "truncated":
        return PracticeRecommendation(
            action="repeat_segment",
            tempo_percent=tempo,
            segment_label=timeline.segment_label,
            loops=3,
            focus=(
                "complete the full capture window and release every held key"
                if missing_release_count
                else "complete the full capture window"
            ),
        )
    if missing_release_count:
        return PracticeRecommendation(
            action="repeat_segment",
            tempo_percent=tempo,
            segment_label=timeline.segment_label,
            loops=2,
            focus="release every held key inside the capture window",
        )
    if metrics.overall_score_percent >= 90 and not (
        metrics.missed_count or metrics.wrong_pitch_count or metrics.extra_count
    ):
        return PracticeRecommendation(
            action="increase_tempo",
            tempo_percent=min(200, tempo + 10),
            segment_label=timeline.segment_label,
            loops=2,
            focus="preserve accuracy at the next tempo",
        )
    if metrics.pitch_accuracy_percent < 80:
        focus = "secure pitches and remove omissions"
    elif (
        metrics.onset_p95_deviation_ms is not None
        and metrics.onset_p95_deviation_ms > metrics.onset_tolerance_ms
    ):
        focus = "stabilize onset timing"
    else:
        focus = "match held-note lengths"
    return PracticeRecommendation(
        action="slow_down" if metrics.overall_score_percent < 75 else "repeat_segment",
        tempo_percent=max(25, tempo - 10) if metrics.overall_score_percent < 75 else tempo,
        segment_label=timeline.segment_label,
        loops=3 if metrics.overall_score_percent < 75 else 2,
        focus=focus,
    )


def _percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return _rounded(numerator * 100.0 / denominator)


def _mean_ms(values_us: tuple[int, ...]) -> float | None:
    if not values_us:
        return None
    return _rounded(sum(values_us) / len(values_us) / 1_000)


def _percentile_ms(values_us: tuple[int, ...], percentile: int) -> float | None:
    if not values_us:
        return None
    ordered = sorted(values_us)
    rank = max(1, (len(ordered) * percentile + 99) // 100)
    return _milliseconds(ordered[rank - 1])


def _median_int(values: list[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _milliseconds(value_us: int) -> float:
    return _rounded(value_us / 1_000)


def _rounded(value: float) -> float:
    return round(value, 3)
