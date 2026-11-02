from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from halcyon.coach_highway import (
    MAX_HIGHWAY_SPANS,
    CoachVisualizationSnapshot,
    project_note_spans,
)
from halcyon.coach_models import LoopSchedule, ResolvedLessonEvent, ScheduledLoopEvent


def _event(
    event_type: str, note: int, *, channel: int = 1, required: bool = True
) -> ResolvedLessonEvent:
    note_on = event_type == "note_on"
    return ResolvedLessonEvent(
        lesson_event_id=f"event-{event_type}-{note}-{channel}",
        event_type=event_type,  # type: ignore[arg-type]
        note=note,
        velocity=90 if note_on else 0,
        channel=channel,
        status=(0x90 if note_on else 0x80) + channel - 1,
        source_port="fixture://highway",
        content_origin="original",
        absolute_tick=0,
        delta_tick=0,
        absolute_us=0,
        delta_us=0,
        required=required,
    )


def _scheduled(
    ordinal: int,
    offset_us: int,
    event_type: str,
    note: int,
    *,
    loop_index: int = 0,
    channel: int = 1,
    required: bool = True,
) -> ScheduledLoopEvent:
    return ScheduledLoopEvent(
        loop_index=loop_index,
        timeline_ordinal=ordinal,
        offset_us=offset_us,
        event=_event(event_type, note, channel=channel, required=required),
    )


def _schedule(*events: ScheduledLoopEvent, total_us: int = 1_000_000) -> LoopSchedule:
    return LoopSchedule(
        boundaries_us=(0, total_us),
        events=tuple(events),
        total_duration_us=total_us,
    )


def test_projection_pairs_chords_repeated_and_overlapping_notes_fifo() -> None:
    schedule = _schedule(
        _scheduled(0, 100_000, "note_on", 60),
        _scheduled(1, 100_000, "note_on", 64),
        _scheduled(2, 200_000, "note_on", 60),
        _scheduled(3, 300_000, "note_off", 60),
        _scheduled(4, 350_000, "note_off", 64),
        _scheduled(5, 450_000, "note_off", 60),
    )

    chord = project_note_spans(schedule, elapsed_us=100_000, cue_kind="target")
    repeated = project_note_spans(schedule, elapsed_us=200_000, cue_kind="halcyon")

    assert chord.target_notes == (60, 64)
    assert chord.target_offset_us == 0
    assert [
        (span.note, span.timeline_ordinal, span.start_offset_us, span.end_offset_us)
        for span in chord.spans
    ] == [
        (60, 0, 0, 200_000),
        (64, 1, 0, 250_000),
        (60, 2, 100_000, 350_000),
    ]
    assert repeated.target_notes == (60,)
    assert repeated.target_offset_us == 0
    assert all(span.cue_kind == "halcyon" for span in repeated.spans)


@pytest.mark.parametrize(
    "events",
    [
        (_scheduled(0, 0, "note_off", 60),),
        (_scheduled(0, 0, "note_on", 60),),
    ],
)
def test_projection_fails_closed_on_orphan_note_pairs(
    events: tuple[ScheduledLoopEvent, ...],
) -> None:
    projection = project_note_spans(_schedule(*events), elapsed_us=0, cue_kind="target")

    assert projection.error == "malformed_note_pairs"
    assert projection.spans == ()
    assert projection.target_notes == ()
    assert projection.target_offset_us is None


def test_projection_clips_window_and_caps_in_schedule_order() -> None:
    events = tuple(
        event
        for index in range(100)
        for event in (
            _scheduled(index * 2, index * 10_000, "note_on", 36 + index % 48),
            _scheduled(index * 2 + 1, index * 10_000 + 5_000, "note_off", 36 + index % 48),
        )
    )
    schedule = _schedule(*events, total_us=1_100_000)

    projection = project_note_spans(
        schedule,
        elapsed_us=0,
        cue_kind="target",
        lookahead_us=1_100_000,
        trail_us=0,
    )

    assert len(projection.spans) == MAX_HIGHWAY_SPANS
    assert [span.timeline_ordinal for span in projection.spans] == list(range(0, 192, 2))
    assert projection.target_notes == (36,)


def test_projection_uses_loop_channel_note_identity_and_clips_bars() -> None:
    schedule = _schedule(
        _scheduled(0, 0, "note_on", 60, channel=1),
        _scheduled(1, 100_000, "note_on", 60, channel=2),
        _scheduled(2, 700_000, "note_off", 60, channel=1),
        _scheduled(3, 800_000, "note_off", 60, channel=2),
    )

    projection = project_note_spans(
        schedule,
        elapsed_us=600_000,
        cue_kind="target",
        lookahead_us=150_000,
        trail_us=50_000,
    )

    assert [
        (span.channel, span.start_offset_us, span.end_offset_us) for span in projection.spans
    ] == [
        (1, -50_000, 100_000),
        (2, -50_000, 150_000),
    ]


def test_projection_preserves_zero_duration_order_and_never_pairs_across_loops() -> None:
    zero_duration = _schedule(
        _scheduled(0, 250_000, "note_on", 72),
        _scheduled(1, 250_000, "note_off", 72),
    )
    projected = project_note_spans(
        zero_duration,
        elapsed_us=250_000,
        cue_kind="halcyon",
    )
    cross_loop = LoopSchedule(
        boundaries_us=(0, 500_000, 1_000_000),
        events=(
            _scheduled(0, 400_000, "note_on", 72, loop_index=0),
            _scheduled(1, 600_000, "note_off", 72, loop_index=1),
        ),
        total_duration_us=1_000_000,
    )

    assert projected.spans[0].start_offset_us == 0
    assert projected.spans[0].end_offset_us == 0
    assert (
        project_note_spans(
            cross_loop,
            elapsed_us=0,
            cue_kind="halcyon",
        ).error
        == "malformed_note_pairs"
    )


def test_snapshot_is_frozen_bounded_and_has_no_serialization_api() -> None:
    snapshot = CoachVisualizationSnapshot(
        active=True,
        phase="ready",
        sampled_at_ns=1,
        target_notes=tuple(range(8)),
    )

    with pytest.raises(FrozenInstanceError):
        snapshot.phase = "capturing"  # type: ignore[misc]
    assert not hasattr(snapshot, "to_dict")
    with pytest.raises(ValueError, match="target-note limit"):
        CoachVisualizationSnapshot(
            active=True,
            phase="ready",
            sampled_at_ns=1,
            target_notes=tuple(range(9)),
        )


def test_ghosts_keep_exact_spans_without_becoming_upcoming_targets() -> None:
    schedule = _schedule(
        _scheduled(0, 0, "note_on", 61, required=False),
        _scheduled(1, 100_000, "note_on", 60),
        _scheduled(2, 200_000, "note_off", 61, required=False),
        _scheduled(3, 300_000, "note_off", 60),
        _scheduled(4, 500_000, "note_on", 64, required=False),
        _scheduled(5, 600_000, "note_off", 64, required=False),
    )

    projected = project_note_spans(schedule, elapsed_us=0, cue_kind="target")

    assert projected.target_notes == (60,)
    assert projected.target_offset_us == 100_000
    assert [
        (span.note, span.required, span.start_offset_us, span.end_offset_us)
        for span in projected.spans
    ] == [(61, False, 0, 200_000), (60, True, 100_000, 300_000), (64, False, 500_000, 600_000)]
    after_required = project_note_spans(schedule, elapsed_us=400_000, cue_kind="target")
    assert after_required.target_notes == ()
    assert after_required.target_offset_us is None
    assert any(not span.required for span in after_required.spans)


def test_ghost_density_cannot_crowd_required_spans_out_of_projection_cap() -> None:
    ghost_events = tuple(
        event
        for index in range(MAX_HIGHWAY_SPANS)
        for event in (
            _scheduled(index * 2, index * 1_000, "note_on", 61, required=False),
            _scheduled(index * 2 + 1, index * 1_000 + 500, "note_off", 61, required=False),
        )
    )
    schedule = _schedule(
        *ghost_events,
        _scheduled(192, 200_000, "note_on", 60),
        _scheduled(193, 300_000, "note_off", 60),
    )

    projected = project_note_spans(schedule, elapsed_us=0, cue_kind="target")

    assert len(projected.spans) == MAX_HIGHWAY_SPANS
    assert projected.spans[-1].required
    assert projected.spans[-1].note == 60
    assert projected.target_notes == (60,)
    assert [span.start_offset_us for span in projected.spans] == sorted(
        span.start_offset_us for span in projected.spans
    )


def test_projection_rejects_inconsistent_on_off_roles() -> None:
    schedule = _schedule(
        _scheduled(0, 0, "note_on", 60),
        _scheduled(1, 200_000, "note_off", 60, required=False),
    )

    projected = project_note_spans(schedule, elapsed_us=0, cue_kind="target")

    assert projected.error == "malformed_note_pairs"
    assert projected.spans == ()
    assert projected.target_notes == ()


def test_snapshot_validates_practice_level_and_nonnegative_counts() -> None:
    snapshot = CoachVisualizationSnapshot(active=True, phase="capturing", sampled_at_ns=1)

    with pytest.raises(ValueError, match="practice level"):
        replace(snapshot, practice_level="easy")
    with pytest.raises(ValueError, match="nonnegative"):
        replace(snapshot, ghost_note_count=-1)
