from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

import halcyon.coach_scoring as scoring_module
from halcyon.coach_models import LatencyProfile, LessonTimeline, ResolvedLessonEvent
from halcyon.coach_scoring import (
    DEFAULT_SCORE_CONFIG,
    CoachingReport,
    ScoreConfig,
    ScoreRequest,
    score_attempt,
)
from halcyon.midi import MidiEvent, MidiOrigin

CAPTURE_STARTED_NS = 10_000_000_000
ATTEMPT_ID = "attempt-1"
SESSION_ID = "session-1"


def _all_mapping_keys(value: object) -> tuple[str, ...]:
    if isinstance(value, dict):
        return tuple(value) + tuple(
            key for child in value.values() for key in _all_mapping_keys(child)
        )
    if isinstance(value, (list, tuple)):
        return tuple(key for child in value for key in _all_mapping_keys(child))
    return ()


def _timeline(
    notes: tuple[tuple[int, int, int], ...],
    *,
    tempo_percent: int = 100,
    segment_label: str | None = "main",
) -> LessonTimeline:
    raw_events: list[tuple[int, int, str, int, int]] = []
    for ordinal, (note, onset_us, duration_us) in enumerate(notes):
        raw_events.append((onset_us, 1, "note_on", note, ordinal))
        raw_events.append((onset_us + duration_us, 0, "note_off", note, ordinal))
    raw_events.sort(key=lambda item: (item[0], item[1], item[3], item[4]))

    events: list[ResolvedLessonEvent] = []
    previous_tick = 0
    previous_us = 0
    for absolute_us, _order, event_type, note, ordinal in raw_events:
        absolute_tick = absolute_us // 1_000
        events.append(
            ResolvedLessonEvent(
                lesson_event_id=f"lesson-event-{ordinal}-{event_type}",
                event_type=event_type,
                note=note,
                velocity=96 if event_type == "note_on" else 0,
                channel=1,
                status=0x90 if event_type == "note_on" else 0x80,
                source_port="halcyon://coach/lesson-1",
                content_origin="locally_authored_bounded",
                absolute_tick=absolute_tick,
                delta_tick=absolute_tick - previous_tick,
                absolute_us=absolute_us,
                delta_us=absolute_us - previous_us,
            )
        )
        previous_tick = absolute_tick
        previous_us = absolute_us

    duration_us = max((event.absolute_us for event in events), default=0)
    duration_ticks = max((event.absolute_tick for event in events), default=0)
    return LessonTimeline(
        lesson_id="lesson-1",
        revision_id="revision-1",
        content_id="a" * 64,
        segment_label=segment_label,
        hand=None,
        difficulty=None,
        tempo_percent=tempo_percent,
        ppq=480,
        start_tick=0,
        end_tick=duration_ticks,
        duration_ticks=duration_ticks,
        duration_us=duration_us,
        events=tuple(events),
    )


def _midi_event(
    *,
    event_type: str,
    note: int,
    timestamp_ns: int,
    voice_id: int,
    sequence: int,
    origin: MidiOrigin = MidiOrigin.HUMAN,
    attempt_id: str | None = ATTEMPT_ID,
    session_id: str = SESSION_ID,
    event_id: str | None = None,
) -> MidiEvent:
    is_press = event_type == "note_on"
    return MidiEvent(
        event_type=event_type,
        note=note,
        note_name=f"note-{note}",
        velocity=96 if is_press else 0,
        channel=1,
        status=0x90 if is_press else 0x80,
        raw_message=(0x90 if is_press else 0x80, note, 96 if is_press else 0),
        delta_time=99.0,
        source="controller",
        elapsed=123.0,
        event_id=event_id or f"event-{voice_id}-{event_type}-{sequence}",
        sequence=sequence,
        session_id=session_id,
        attempt_id=attempt_id,
        voice_id=voice_id,
        origin=origin,
        received_monotonic_ns=timestamp_ns,
        dispatched_monotonic_ns=timestamp_ns,
        delta_us=99_000_000,
    )


def _observed_notes(
    notes: tuple[tuple[int, int, int], ...],
    *,
    origin: MidiOrigin = MidiOrigin.HUMAN,
    attempt_id: str | None = ATTEMPT_ID,
    sequence_start: int = 1,
    voice_start: int = 1_000,
) -> tuple[MidiEvent, ...]:
    events: list[MidiEvent] = []
    sequence = sequence_start
    for index, (note, onset_us, duration_us) in enumerate(notes):
        voice_id = voice_start + index
        events.append(
            _midi_event(
                event_type="note_on",
                note=note,
                timestamp_ns=CAPTURE_STARTED_NS + onset_us * 1_000,
                voice_id=voice_id,
                sequence=sequence,
                origin=origin,
                attempt_id=attempt_id,
            )
        )
        sequence += 1
        events.append(
            _midi_event(
                event_type="note_off",
                note=note,
                timestamp_ns=CAPTURE_STARTED_NS + (onset_us + duration_us) * 1_000,
                voice_id=voice_id,
                sequence=sequence,
                origin=origin,
                attempt_id=attempt_id,
            )
        )
        sequence += 1
    return tuple(events)


def _request(
    timeline: LessonTimeline,
    observed: tuple[MidiEvent, ...],
    *,
    profile: LatencyProfile | None = None,
    truncated: bool = False,
    capture_ended_us: int | None = None,
) -> ScoreRequest:
    ended_us = capture_ended_us or max(timeline.duration_us + 500_000, 1_000_000)
    return ScoreRequest(
        attempt_id=ATTEMPT_ID,
        session_id=SESSION_ID,
        timeline=timeline,
        observed_events=observed,
        capture_started_ns=CAPTURE_STARTED_NS,
        capture_ended_ns=CAPTURE_STARTED_NS + ended_us * 1_000,
        latency_profile=profile,
        truncated=truncated,
    )


@pytest.fixture
def three_note_timeline() -> LessonTimeline:
    return _timeline(
        (
            (60, 0, 250_000),
            (62, 500_000, 250_000),
            (64, 1_000_000, 250_000),
        )
    )


def test_exact_attempt_is_perfect_versioned_bounded_and_repeatable(
    three_note_timeline: LessonTimeline,
) -> None:
    observed = _observed_notes(
        (
            (60, 0, 250_000),
            (62, 500_000, 250_000),
            (64, 1_000_000, 250_000),
        )
    )
    request = _request(three_note_timeline, observed)

    first = score_attempt(request)
    second = score_attempt(request)

    assert first == second
    assert isinstance(first, CoachingReport)
    assert first.status == "complete"
    assert first.metrics.pitch_accuracy_percent == 100.0
    assert first.metrics.completion_percent == 100.0
    assert first.metrics.overall_score_percent == 100.0
    assert first.metrics.missed_count == 0
    assert first.metrics.wrong_pitch_count == 0
    assert first.metrics.extra_count == 0
    assert first.metrics.scoring_version == "halcyon-score-v1"
    assert first.latency.source == "estimated"
    assert first.latency.offset_us == 0
    assert first.issues == ()
    assert first.recommendation.action == "increase_tempo"

    first_public = first.to_public_dict()
    second_public = second.to_public_dict()
    first_bytes = json.dumps(first_public, sort_keys=True, separators=(",", ":"))
    second_bytes = json.dumps(second_public, sort_keys=True, separators=(",", ":"))
    assert first_bytes == second_bytes
    assert len(first_public["issues"]) <= 8
    assert len(first_public["tips"]) <= 3
    assert first_public["metrics"]["weights_percent"] == {
        "pitch": 45,
        "onset": 25,
        "duration": 15,
        "completion": 15,
    }
    assert first_public["metrics"]["tolerances_ms"]["onset"] == 45.0
    forbidden = {
        "observed_events",
        "raw_message",
        "event_id",
        "voice_id",
        "source_port",
        "dispatched_monotonic_ns",
    }
    assert forbidden.isdisjoint(_all_mapping_keys(first_public))

    with pytest.raises(FrozenInstanceError):
        first.status = "empty"  # type: ignore[misc]


def test_missed_note_is_counted_and_cited(three_note_timeline: LessonTimeline) -> None:
    observed = _observed_notes(((60, 0, 250_000), (64, 1_000_000, 250_000)))

    report = score_attempt(_request(three_note_timeline, observed))

    assert report.metrics.matched_note_count == 2
    assert report.metrics.missed_count == 1
    assert report.metrics.wrong_pitch_count == 0
    assert report.metrics.extra_count == 0
    assert any(issue.code == "MISSED_NOTE" and issue.expected_note == 62 for issue in report.issues)


def test_extra_note_is_counted_and_cited(three_note_timeline: LessonTimeline) -> None:
    observed = _observed_notes(
        (
            (60, 0, 250_000),
            (72, 250_000, 100_000),
            (62, 500_000, 250_000),
            (64, 1_000_000, 250_000),
        )
    )

    report = score_attempt(_request(three_note_timeline, observed))

    assert report.metrics.matched_note_count == 3
    assert report.metrics.extra_count == 1
    assert report.metrics.wrong_pitch_count == 0
    assert any(issue.code == "EXTRA_NOTE" and issue.observed_note == 72 for issue in report.issues)


def test_nearby_wrong_pitch_is_substitution_not_miss_plus_extra(
    three_note_timeline: LessonTimeline,
) -> None:
    observed = _observed_notes(
        (
            (60, 0, 250_000),
            (63, 500_000, 250_000),
            (64, 1_000_000, 250_000),
        )
    )

    report = score_attempt(_request(three_note_timeline, observed))

    assert report.metrics.matched_note_count == 2
    assert report.metrics.wrong_pitch_count == 1
    assert report.metrics.missed_count == 0
    assert report.metrics.extra_count == 0
    issue = next(issue for issue in report.issues if issue.code == "WRONG_PITCH")
    assert (issue.expected_note, issue.observed_note) == (62, 63)


def test_chord_alignment_ignores_simultaneous_input_order() -> None:
    timeline = _timeline(((60, 0, 300_000), (64, 0, 300_000), (67, 0, 300_000)))
    observed = _observed_notes(((67, 10_000, 300_000), (60, 0, 300_000), (64, 5_000, 300_000)))

    report = score_attempt(_request(timeline, observed))

    assert report.metrics.matched_note_count == 3
    assert report.metrics.wrong_pitch_count == 0
    assert report.metrics.missed_count == 0
    assert report.metrics.extra_count == 0
    assert report.metrics.pitch_accuracy_percent == 100.0


def test_nonuniform_onset_drift_is_reported_without_latency_overfit() -> None:
    timeline = _timeline(((60, 0, 200_000), (62, 500_000, 200_000)))
    observed = _observed_notes(((60, 70_000, 200_000), (62, 590_000, 200_000)))

    report = score_attempt(_request(timeline, observed))

    assert report.latency.source == "default"
    assert report.metrics.onset_mean_deviation_ms == 80.0
    assert report.metrics.onset_p95_deviation_ms == 90.0
    assert {issue.code for issue in report.issues} == {"LATE_ONSET"}
    assert all("earlier" in issue.message for issue in report.issues)
    assert any("internal pulse" in tip for tip in report.tips)


def test_held_duration_drift_is_measured_and_actionable(
    three_note_timeline: LessonTimeline,
) -> None:
    observed = _observed_notes(
        (
            (60, 0, 400_000),
            (62, 500_000, 400_000),
            (64, 1_000_000, 400_000),
        )
    )

    report = score_attempt(_request(three_note_timeline, observed))

    assert report.metrics.held_duration_mean_deviation_ms == 150.0
    assert {issue.code for issue in report.issues} == {"HOLD_TOO_LONG"}
    assert all("shorter" in issue.message for issue in report.issues)
    assert any("Release each key" in tip for tip in report.tips)


def test_unclosed_note_cites_missing_release_without_inventing_duration() -> None:
    timeline = _timeline(((60, 0, 250_000),))
    observed_note_on = _observed_notes(((60, 0, 250_000),))[0]

    report = score_attempt(_request(timeline, (observed_note_on,)))

    assert report.metrics.matched_note_count == 1
    assert report.metrics.held_duration_mean_deviation_ms is None
    assert report.metrics.overall_score_percent == 85.0
    issue = next(issue for issue in report.issues if issue.code == "MISSING_RELEASE")
    assert issue.expected_note == 60
    assert issue.observed_note == 60
    assert issue.expected_lesson_event_id == "lesson-event-0-note_on"
    assert issue.observed_offset_ms == 0.0
    assert issue.deviation_ms is None
    assert "release" in report.tips[0].lower()
    assert report.recommendation.action == "repeat_segment"
    assert "release every held key" in report.recommendation.focus
    assert report.to_public_dict()["issues"][0]["deviation_ms"] is None


def test_truncated_unclosed_note_reports_uncertainty_and_full_window_retry() -> None:
    timeline = _timeline(((60, 0, 250_000),))
    observed_note_on = _observed_notes(((60, 0, 250_000),))[0]

    report = score_attempt(_request(timeline, (observed_note_on,), truncated=True))

    assert report.status == "truncated"
    assert report.metrics.held_duration_mean_deviation_ms is None
    assert {issue.code for issue in report.issues} == {"MISSING_RELEASE"}
    assert "full capture window" in report.tips[0]
    assert any("release" in tip.lower() for tip in report.tips)
    assert report.recommendation.action == "repeat_segment"
    assert "release" in report.recommendation.focus
    assert all(issue.deviation_ms is None for issue in report.issues)


def test_empty_attempt_is_explicit_and_recommends_a_retry(
    three_note_timeline: LessonTimeline,
) -> None:
    report = score_attempt(_request(three_note_timeline, ()))

    assert report.status == "empty"
    assert report.metrics.observed_note_count == 0
    assert report.metrics.missed_count == 3
    assert report.metrics.overall_score_percent == 0.0
    assert "no in-window human note-on" in report.tips[0]
    assert report.recommendation.action == "slow_down"


def test_truncated_attempt_is_explicit_and_does_not_claim_completion(
    three_note_timeline: LessonTimeline,
) -> None:
    observed = _observed_notes(((60, 0, 250_000),))

    report = score_attempt(_request(three_note_timeline, observed, truncated=True))

    assert report.status == "truncated"
    assert report.metrics.truncated is True
    assert "full capture window" in report.tips[0]
    assert report.recommendation.action == "repeat_segment"
    assert report.recommendation.focus == "complete the full capture window"


def test_tempo_scaled_resolved_timeline_is_scored_as_given() -> None:
    timeline = _timeline(
        ((60, 0, 400_000), (62, 1_000_000, 400_000), (64, 2_000_000, 400_000)),
        tempo_percent=50,
    )
    observed = _observed_notes(
        ((60, 0, 400_000), (62, 1_000_000, 400_000), (64, 2_000_000, 400_000))
    )

    report = score_attempt(_request(timeline, observed))

    assert report.metrics.overall_score_percent == 100.0
    assert report.tempo_percent == 50
    assert report.recommendation.tempo_percent == 60


def test_segment_timeline_starts_at_zero_and_preserves_segment_citation() -> None:
    timeline = _timeline(
        ((67, 0, 200_000), (69, 400_000, 200_000), (71, 800_000, 200_000)),
        segment_label="phrase-b",
    )
    observed = _observed_notes(((67, 0, 200_000), (69, 400_000, 200_000), (71, 800_000, 200_000)))

    report = score_attempt(_request(timeline, observed))

    assert report.metrics.overall_score_percent == 100.0
    assert report.segment_label == "phrase-b"
    assert report.to_public_dict()["lesson"]["segment_label"] == "phrase-b"


def test_explicit_calibration_is_subtracted_and_drives_jitter_tolerance(
    three_note_timeline: LessonTimeline,
) -> None:
    observed = _observed_notes(
        (
            (60, 120_000, 250_000),
            (62, 620_000, 250_000),
            (64, 1_120_000, 250_000),
        )
    )
    profile = LatencyProfile(offset_us=120_000, jitter_us=20_000, sample_count=16)

    report = score_attempt(_request(three_note_timeline, observed, profile=profile))

    assert report.latency.source == "calibrated"
    assert report.latency.offset_us == 120_000
    assert report.latency.jitter_us == 20_000
    assert report.latency.tolerance_us == 60_000
    assert report.metrics.onset_mean_deviation_ms == 0.0
    assert report.metrics.overall_score_percent == 100.0


def test_consistent_latency_is_estimated_from_high_confidence_matches(
    three_note_timeline: LessonTimeline,
) -> None:
    observed = _observed_notes(
        (
            (60, 110_000, 250_000),
            (62, 610_000, 250_000),
            (64, 1_110_000, 250_000),
        )
    )

    report = score_attempt(_request(three_note_timeline, observed))

    assert report.latency.source == "estimated"
    assert report.latency.offset_us == 110_000
    assert report.latency.match_count == 3
    assert report.metrics.onset_mean_deviation_ms == 0.0
    assert report.metrics.overall_score_percent == 100.0


def test_only_valid_human_in_window_attempt_events_are_scored() -> None:
    timeline = _timeline(((60, 0, 250_000),))
    valid = _observed_notes(((60, 0, 250_000),))
    halcyon = _observed_notes(
        ((72, 100_000, 100_000),),
        origin=MidiOrigin.HALCYON,
        sequence_start=10,
        voice_start=2_000,
    )
    wrong_attempt = _observed_notes(
        ((73, 150_000, 100_000),),
        attempt_id="other-attempt",
        sequence_start=20,
        voice_start=3_000,
    )
    unsequenced = _observed_notes(
        ((74, 200_000, 100_000),),
        sequence_start=0,
        voice_start=4_000,
    )
    outside = _observed_notes(
        ((75, 1_500_000, 100_000),),
        sequence_start=30,
        voice_start=5_000,
    )

    report = score_attempt(
        _request(
            timeline,
            valid + halcyon + wrong_attempt + unsequenced + outside,
            capture_ended_us=1_000_000,
        )
    )

    assert report.metrics.observed_note_count == 1
    assert report.metrics.matched_note_count == 1
    assert report.metrics.extra_count == 0
    assert report.metrics.overall_score_percent == 100.0


def test_wrong_session_events_are_excluded_with_or_without_attempt_id() -> None:
    timeline = _timeline(((60, 0, 250_000), (62, 400_000, 250_000)))
    same_session_stamped = _observed_notes(((60, 0, 250_000),))
    same_session_unstamped = tuple(
        replace(event, attempt_id=None)
        for event in _observed_notes(
            ((62, 400_000, 250_000),),
            sequence_start=10,
            voice_start=2_000,
        )
    )
    other_session_unstamped = tuple(
        replace(event, session_id="other-session", attempt_id=None)
        for event in _observed_notes(
            ((71, 100_000, 100_000),),
            sequence_start=20,
            voice_start=3_000,
        )
    )
    other_session_matching_attempt = tuple(
        replace(event, session_id="other-session", attempt_id=ATTEMPT_ID)
        for event in _observed_notes(
            ((72, 200_000, 100_000),),
            sequence_start=30,
            voice_start=4_000,
        )
    )

    report = score_attempt(
        _request(
            timeline,
            same_session_stamped
            + same_session_unstamped
            + other_session_unstamped
            + other_session_matching_attempt,
        )
    )

    assert report.metrics.observed_note_count == 2
    assert report.metrics.matched_note_count == 2
    assert report.metrics.extra_count == 0


def test_voice_id_pairing_handles_reversed_same_pitch_releases_and_unmatched_release() -> None:
    timeline = _timeline(((60, 0, 200_000), (60, 50_000, 300_000)))
    events = (
        _midi_event(
            event_type="note_off",
            note=60,
            timestamp_ns=CAPTURE_STARTED_NS,
            voice_id=999,
            sequence=1,
        ),
        _midi_event(
            event_type="note_on",
            note=60,
            timestamp_ns=CAPTURE_STARTED_NS,
            voice_id=1_001,
            sequence=2,
        ),
        _midi_event(
            event_type="note_on",
            note=60,
            timestamp_ns=CAPTURE_STARTED_NS + 50_000_000,
            voice_id=1_002,
            sequence=3,
        ),
        _midi_event(
            event_type="note_off",
            note=60,
            timestamp_ns=CAPTURE_STARTED_NS + 200_000_000,
            voice_id=1_001,
            sequence=4,
        ),
        _midi_event(
            event_type="note_off",
            note=60,
            timestamp_ns=CAPTURE_STARTED_NS + 350_000_000,
            voice_id=1_002,
            sequence=5,
        ),
    )

    report = score_attempt(_request(timeline, events))

    assert report.metrics.observed_note_count == 2
    assert report.metrics.matched_note_count == 2
    assert report.metrics.held_duration_mean_deviation_ms == 0.0


def test_musical_pitch_alignment_is_channel_independent_but_voice_pairing_is_not() -> None:
    timeline = _timeline(((60, 0, 250_000),))
    channel_two = tuple(
        replace(
            event,
            channel=2,
            status=0x91 if event.is_press else 0x81,
            raw_message=(0x91 if event.is_press else 0x81, event.note, event.velocity),
        )
        for event in _observed_notes(((60, 0, 250_000),))
    )

    report = score_attempt(_request(timeline, channel_two))

    assert report.metrics.matched_note_count == 1
    assert report.metrics.pitch_accuracy_percent == 100.0
    assert report.metrics.held_duration_mean_deviation_ms == 0.0


def test_latency_profile_defaults_to_none_for_wave_three_seam(
    three_note_timeline: LessonTimeline,
) -> None:
    request = ScoreRequest(
        attempt_id=ATTEMPT_ID,
        session_id=SESSION_ID,
        timeline=three_note_timeline,
        observed_events=(),
        capture_started_ns=CAPTURE_STARTED_NS,
        capture_ended_ns=CAPTURE_STARTED_NS + 2_000_000_000,
    )

    assert request.latency_profile is None
    assert request.truncated is False


def test_issue_tip_and_input_limits_are_hard_bounded() -> None:
    timeline = _timeline(tuple((48 + index, index * 100_000, 50_000) for index in range(12)))
    report = score_attempt(_request(timeline, ()))

    public = report.to_public_dict()
    assert len(report.issues) == 8
    assert len(public["issues"]) == 8
    assert len(report.tips) <= 3
    assert len(public["tips"]) <= 3

    too_many = tuple(
        _midi_event(
            event_type="note_on",
            note=60,
            timestamp_ns=CAPTURE_STARTED_NS,
            voice_id=index + 1,
            sequence=index + 1,
        )
        for index in range(DEFAULT_SCORE_CONFIG.max_observed_events + 1)
    )
    with pytest.raises(ValueError, match="2048-event scoring limit"):
        score_attempt(_request(timeline, too_many))


def test_rubric_validation_and_module_have_no_model_dependency() -> None:
    assert DEFAULT_SCORE_CONFIG.weights_public_dict() == {
        "pitch": 45,
        "onset": 25,
        "duration": 15,
        "completion": 15,
    }
    assert DEFAULT_SCORE_CONFIG.min_onset_tolerance_us == 45_000
    assert DEFAULT_SCORE_CONFIG.max_onset_tolerance_us == 180_000
    with pytest.raises(ValueError, match="sum to 100"):
        ScoreConfig(pitch_weight_percent=44)
    with pytest.raises(ValueError, match="between 1 and 8"):
        ScoreConfig(max_issues=9)
    assert "openai" not in scoring_module.__dict__
