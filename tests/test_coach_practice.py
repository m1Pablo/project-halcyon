from dataclasses import replace

import pytest

import halcyon.coach_practice as practice
from halcyon.coach_models import (
    Acquisition,
    CoachError,
    Fidelity,
    LessonEvent,
    LessonProvenance,
    LessonRequest,
    LessonSegment,
    LessonSelection,
    TempoChange,
    TimeSignature,
    create_lesson_revision,
    resolve_loop_schedule,
    resolve_timeline,
)
from halcyon.coach_practice import (
    PracticeLevel,
    practice_levels,
    practice_required_event_ids,
)


def _lesson():
    raw = (
        ("note_on", 60, 0),
        ("note_on", 60, 120),
        ("note_on", 64, 120),
        ("note_off", 60, 240),
        ("note_off", 64, 360),
        ("note_off", 60, 480),
    )
    events = []
    previous = 0
    for kind, note, tick in raw:
        events.append(
            LessonEvent(
                event_id="temporary",
                event_type=kind,
                note=note,
                velocity=80 if kind == "note_on" else 0,
                channel=1,
                status=0x90 if kind == "note_on" else 0x80,
                source_port="halcyon://coach/test",
                content_origin="halcyon",
                absolute_tick=tick,
                delta_tick=tick - previous,
            )
        )
        previous = tick
    return create_lesson_revision(
        lesson_id="practice-test",
        created_at="2026-10-31T00:00:00+00:00",
        aliases=("practice test",),
        title="Practice Test",
        composer="Tester",
        difficulty=None,
        hand=None,
        ppq=480,
        tempo_map=(TempoChange(0, 500_000), TempoChange(240, 600_000)),
        time_signatures=(TimeSignature(0, 4, 4),),
        provenance=LessonProvenance(
            Acquisition.ORIGINAL,
            "Original synthetic test",
            Fidelity.VERIFIED,
            "2026-10-31T00:00:00+00:00",
        ),
        events=tuple(events),
        segments=(LessonSegment("middle", 180, 420), LessonSegment("ghost tail", 250, 400)),
        max_polyphony=3,
    )


@pytest.fixture
def annotated_lesson(monkeypatch):
    lesson = _lesson()
    monkeypatch.setitem(
        practice._PRACTICE_ANNOTATIONS,
        lesson.content_id,
        practice._PracticeAnnotation(3, frozenset({0}), frozenset({0, 1})),
    )
    return lesson


def test_reviewed_source_maps_are_content_bound_and_nested() -> None:
    avril = practice._PRACTICE_ANNOTATIONS[
        "c1d235e0e88f367bbc38470aa1f840ccf2edfb7f4db01b1006ba088db1034263"
    ]
    satie = practice._PRACTICE_ANNOTATIONS[
        "f9d6b81c3d811080d3522df9f0253e5d0badf95ab9dd48200f7bf603f6459331"
    ]
    assert avril.melody == avril.essentials == frozenset(range(199))
    assert len(satie.melody) == 124
    assert len(satie.essentials) == 278
    assert satie.melody < satie.essentials < frozenset(range(453))
    assert {41, 45, 49, 53, 57, 268, 272, 276, 280, 284} <= satie.melody
    assert {217, 223, 443, 449} <= satie.melody
    assert practice_levels("a" * 64) == ("full",)
    assert practice_levels("Avril 14th") == ("full",)
    assert practice_levels(next(iter(practice._PRACTICE_ANNOTATIONS))) == (
        "melody",
        "essentials",
        "full",
    )


def test_annotation_rejects_non_nested_or_out_of_range_ordinals() -> None:
    with pytest.raises(ValueError, match="nested"):
        practice._PracticeAnnotation(3, frozenset({0}), frozenset({1}))
    with pytest.raises(ValueError, match="nested"):
        practice._PracticeAnnotation(3, frozenset({3}), frozenset({3}))


def test_unknown_source_full_is_exact_and_reduction_is_actionable() -> None:
    lesson = _lesson()
    before = lesson.to_dict()
    assert practice_required_event_ids(lesson, "full") == frozenset(
        event.event_id for event in lesson.events
    )
    for level in ("melody", "essentials"):
        with pytest.raises(CoachError) as failure:
            resolve_timeline(lesson, LessonSelection(practice_level=level))
        assert failure.value.code == "PRACTICE_UNAVAILABLE"
        assert "--practice full" in failure.value.resolution
    assert lesson.to_dict() == before


def test_source_ordinals_pair_overlapping_same_pitch_fifo(annotated_lesson) -> None:
    lesson = annotated_lesson
    melody = practice_required_event_ids(lesson, "melody")
    essentials = practice_required_event_ids(lesson, "essentials")
    assert melody == frozenset({lesson.events[0].event_id, lesson.events[3].event_id})
    assert essentials == melody | {lesson.events[1].event_id, lesson.events[5].event_id}
    assert melody < essentials < practice_required_event_ids(lesson, "full")


@pytest.mark.parametrize("tempo", [25, 70, 100, 200])
def test_layer_preserves_all_source_events_and_tempo_does_not_choose_notes(
    annotated_lesson, tempo
) -> None:
    lesson = annotated_lesson
    before = lesson.to_dict()
    full = resolve_timeline(lesson, LessonSelection(tempo_percent=tempo))
    melody = resolve_timeline(
        lesson, LessonRequest("practice test", tempo_percent=tempo, practice_level="melody")
    )
    assert melody.practice_level is PracticeLevel.MELODY
    assert [event.required for event in melody.events] == [True, False, False, True, False, False]
    assert tuple(replace(event, required=True) for event in melody.events) == full.events
    assert melody.duration_us == full.duration_us
    assert lesson.to_dict() == before
    schedule = resolve_loop_schedule(lesson, melody, 2)
    assert len(schedule.events) == len(melody.events) * 2
    assert [event.event.required for event in schedule.events[:6]] == [
        True,
        False,
        False,
        True,
        False,
        False,
    ]


def test_clipped_carry_on_and_boundary_off_inherit_source_roles(annotated_lesson) -> None:
    lesson = annotated_lesson
    full = resolve_timeline(lesson, LessonSelection(segment="middle"))
    melody = resolve_timeline(lesson, LessonSelection(segment="middle", practice_level="melody"))
    assert tuple(replace(event, required=True) for event in melody.events) == full.events
    assert [event.required for event in melody.events] == [True, False, False, True, False, False]
    assert ":segment-on:" in melody.events[0].lesson_event_id
    assert ":segment-off:" in melody.events[-1].lesson_event_id
    assert melody.events[-1].required is False


def test_reduced_segment_without_required_notes_is_rejected(annotated_lesson) -> None:
    with pytest.raises(CoachError) as failure:
        resolve_timeline(
            annotated_lesson, LessonSelection(segment="ghost tail", practice_level="melody")
        )
    assert failure.value.code == "PRACTICE_UNAVAILABLE"
    assert "essentials or full" in failure.value.resolution


def test_annotation_count_mismatch_is_corrupt_not_a_guessed_arrangement(
    annotated_lesson, monkeypatch
) -> None:
    monkeypatch.setitem(
        practice._PRACTICE_ANNOTATIONS,
        annotated_lesson.content_id,
        practice._PracticeAnnotation(4, frozenset({0}), frozenset({0})),
    )
    with pytest.raises(CoachError) as failure:
        practice_required_event_ids(annotated_lesson, "melody")
    assert failure.value.code == "LESSON_CORRUPT"
