from dataclasses import FrozenInstanceError, replace

import pytest

from halcyon.coach_models import (
    AUTHORING_LESSON_LIMITS,
    STORED_LESSON_LIMITS,
    Acquisition,
    AuthorMode,
    Difficulty,
    Fidelity,
    Hand,
    LatencyProfile,
    LessonEvent,
    LessonProvenance,
    LessonRequest,
    LessonSegment,
    LessonSelection,
    LessonTimeline,
    LessonValidationError,
    ResolvedLessonEvent,
    TempoChange,
    TimeSignature,
    create_lesson_revision,
    derive_lesson_event_id,
    derive_runtime_event_id,
    normalize_lesson_alias,
    resolve_timeline,
    resolve_loop_schedule,
    tick_positions_to_microseconds,
)

NOW = "2026-07-12T00:00:00+00:00"


def event(
    event_type: str,
    tick: int,
    delta: int,
    *,
    note: int = 60,
    channel: int = 1,
    velocity: int | None = None,
) -> LessonEvent:
    is_on = event_type == "note_on"
    return LessonEvent(
        event_id="temporary",
        event_type=event_type,  # type: ignore[arg-type]
        note=note,
        velocity=(80 if is_on else 0) if velocity is None else velocity,
        channel=channel,
        status=(0x90 if is_on else 0x80) | (channel - 1),
        source_port="halcyon://coach/test",
        content_origin="halcyon",
        absolute_tick=tick,
        delta_tick=delta,
    )


def make_lesson(
    *,
    events: tuple[LessonEvent, ...] | None = None,
    tempo_map: tuple[TempoChange, ...] = (TempoChange(0, 500_000),),
    segments: tuple[LessonSegment, ...] | None = None,
    max_polyphony: int = 8,
    aliases: tuple[str, ...] = ("Test Piece Main Melody",),
    ppq: int = 480,
):
    if events is None:
        events = (event("note_on", 0, 0), event("note_off", 480, 480))
    if segments is None:
        segments = (
            LessonSegment(
                "Main Melody",
                0,
                events[-1].absolute_tick,
                hand=Hand.RIGHT,
                difficulty=Difficulty.FOUNDATION,
            ),
        )
    return create_lesson_revision(
        lesson_id="test-piece",
        created_at=NOW,
        aliases=aliases,
        title="Test Piece",
        composer="Test Composer",
        difficulty=Difficulty.FOUNDATION,
        hand=Hand.RIGHT,
        ppq=ppq,
        tempo_map=tempo_map,
        time_signatures=(TimeSignature(0, 4, 4),),
        provenance=LessonProvenance(
            acquisition=Acquisition.ORIGINAL,
            citation="Original test material",
            fidelity=Fidelity.VERIFIED,
            authorization_timestamp=NOW,
        ),
        events=events,
        segments=segments,
        max_polyphony=max_polyphony,
    )


def limit_events(pair_count: int, duration_tick: int) -> tuple[LessonEvent, ...]:
    events: list[LessonEvent] = []
    for index in range(pair_count):
        events.append(event("note_on", 0, 0))
        end_tick = duration_tick if index == pair_count - 1 else 0
        events.append(event("note_off", end_tick, end_tick))
    return tuple(events)


def make_limit_lesson(*, pair_count: int, duration_tick: int, authoring: bool):
    return create_lesson_revision(
        lesson_id="authoring-boundary" if authoring else "stored-boundary",
        created_at=NOW,
        aliases=("exact boundary",),
        title="Exact Boundary",
        composer="Tester",
        difficulty=None,
        hand=None,
        ppq=1,
        tempo_map=(TempoChange(0, 1_000_000),),
        time_signatures=(TimeSignature(0, 4, 4),),
        provenance=LessonProvenance(Acquisition.ORIGINAL, "test", Fidelity.VERIFIED, NOW),
        events=limit_events(pair_count, duration_tick),
        segments=(),
        max_polyphony=1,
        limits=AUTHORING_LESSON_LIMITS if authoring else STORED_LESSON_LIMITS,
    )


def test_schema_one_revision_is_immutable_and_round_trips() -> None:
    lesson = make_lesson()

    assert lesson.schema_version == 1
    assert lesson.duration_ticks == 480
    assert lesson.duration_us == 500_000
    assert lesson.events[0].event_id == derive_lesson_event_id(
        lesson.lesson_id, lesson.revision_id, 0
    )
    assert type(lesson).from_dict(lesson.to_dict()) == lesson
    with pytest.raises(FrozenInstanceError):
        lesson.title = "Changed"  # type: ignore[misc]

    with pytest.raises(LessonValidationError, match="schema"):
        replace(lesson, schema_version=True)
    with pytest.raises(LessonValidationError, match="schema"):
        LatencyProfile(schema_version=True)


def test_content_identity_excludes_aliases_and_timestamps() -> None:
    first = make_lesson(aliases=("One Alias",))
    second = create_lesson_revision(
        lesson_id=first.lesson_id,
        created_at="2030-01-01T00:00:00+00:00",
        aliases=("Another Alias",),
        title="Different display title",
        composer="Different display composer",
        difficulty=first.difficulty,
        hand=first.hand,
        ppq=first.ppq,
        tempo_map=first.tempo_map,
        time_signatures=first.time_signatures,
        provenance=replace(first.provenance, citation="Different display citation"),
        events=tuple(
            replace(
                event,
                source_port="authorized://different-source",
                content_origin="different-authorized-origin",
            )
            for event in first.events
        ),
        segments=first.segments,
        max_polyphony=first.max_polyphony,
    )

    assert second.content_id == first.content_id
    assert second.revision_id == first.revision_id
    assert second.events[0].event_id == first.events[0].event_id


def test_capacity_declaration_does_not_change_musical_content_identity() -> None:
    one_voice = make_lesson(max_polyphony=1)
    eight_voice = make_lesson(max_polyphony=8)

    assert one_voice.max_polyphony == 1
    assert eight_voice.max_polyphony == 8
    assert one_voice.content_id == eight_voice.content_id
    assert one_voice.revision_id == eight_voice.revision_id
    assert tuple(event.event_id for event in one_voice.events) == tuple(
        event.event_id for event in eight_voice.events
    )


@pytest.mark.parametrize(
    ("replacement", "match"),
    [
        ({"note": 128}, "events.note"),
        ({"velocity": -1}, "events.velocity"),
        ({"channel": 17}, "events.channel"),
        ({"status": 0x91}, "status channel"),
        ({"event_type": "control_change"}, "event type"),
    ],
)
def test_midi_ranges_and_status_consistency_are_strict(replacement, match) -> None:
    with pytest.raises(LessonValidationError, match=match):
        replace(event("note_on", 0, 0), **replacement)


def test_tempo_and_signature_maps_start_at_zero_and_are_strict() -> None:
    with pytest.raises(LessonValidationError, match="denominator"):
        TimeSignature(0, 4, True)

    with pytest.raises(LessonValidationError, match="first tempo"):
        make_lesson(tempo_map=(TempoChange(1, 500_000),))

    lesson = make_lesson()
    payload = lesson.to_dict()
    payload["time_signatures"] = [{"absolute_tick": 1, "numerator": 4, "denominator": 4}]
    with pytest.raises(LessonValidationError, match="first time signature"):
        type(lesson).from_dict(payload)

    with pytest.raises(LessonValidationError, match="strictly increasing"):
        make_lesson(tempo_map=(TempoChange(0, 500_000), TempoChange(0, 400_000)))

    with pytest.raises(LessonValidationError, match="within lesson duration"):
        make_lesson(tempo_map=(TempoChange(0, 500_000), TempoChange(481, 400_000)))


def test_absolute_and_delta_ticks_must_agree() -> None:
    with pytest.raises(LessonValidationError, match="delta_tick"):
        make_lesson(events=(event("note_on", 0, 0), event("note_off", 480, 479)))

    with pytest.raises(LessonValidationError, match="monotonic"):
        make_lesson(
            events=(
                event("note_on", 100, 100),
                event("note_off", 50, 0),
            ),
            segments=(LessonSegment("bad", 0, 100),),
        )


def test_note_balance_hanging_notes_and_declared_polyphony_are_enforced() -> None:
    with pytest.raises(LessonValidationError, match="no matching note_on"):
        make_lesson(events=(event("note_off", 480, 480),))

    with pytest.raises(LessonValidationError, match="hanging"):
        make_lesson(
            events=(event("note_on", 0, 0),),
            segments=(LessonSegment("bad", 0, 1),),
        )

    chord = (
        event("note_on", 0, 0, note=60),
        event("note_on", 0, 0, note=64),
        event("note_off", 480, 480, note=60),
        event("note_off", 480, 0, note=64),
    )
    with pytest.raises(LessonValidationError, match="max_polyphony"):
        make_lesson(events=chord, max_polyphony=1)
    with pytest.raises(LessonValidationError, match="1 through 8"):
        make_lesson(max_polyphony=9)


def test_declared_segment_bounds_must_fit_lesson() -> None:
    with pytest.raises(LessonValidationError, match="segment bounds"):
        make_lesson(segments=(LessonSegment("too long", 0, 481),))


def test_authoring_event_and_duration_limits_are_enforced() -> None:
    exact = make_limit_lesson(pair_count=256, duration_tick=120, authoring=True)
    assert len(exact.events) == 512
    assert exact.duration_us == 120_000_000

    pairs: list[LessonEvent] = []
    tick = 0
    for index in range((AUTHORING_LESSON_LIMITS.max_events // 2) + 1):
        pairs.append(event("note_on", tick, 0 if not pairs else tick - pairs[-1].absolute_tick))
        tick += 1
        pairs.append(event("note_off", tick, 1))
        tick += 1
    with pytest.raises(LessonValidationError, match="more than 512"):
        create_lesson_revision(
            lesson_id="event-limit",
            created_at=NOW,
            aliases=("event limit",),
            title="Event Limit",
            composer="Tester",
            difficulty=None,
            hand=None,
            ppq=480,
            tempo_map=(TempoChange(0, 500_000),),
            time_signatures=(TimeSignature(0, 4, 4),),
            provenance=LessonProvenance(Acquisition.ORIGINAL, "test", Fidelity.VERIFIED, NOW),
            events=tuple(pairs),
            segments=(),
            limits=AUTHORING_LESSON_LIMITS,
        )


def test_stored_event_and_duration_limits_accept_boundary_and_reject_above() -> None:
    exact = make_limit_lesson(pair_count=1_024, duration_tick=300, authoring=False)
    assert len(exact.events) == 2_048
    assert exact.duration_us == 300_000_000

    with pytest.raises(LessonValidationError, match="more than 2048"):
        make_limit_lesson(pair_count=1_025, duration_tick=300, authoring=False)
    with pytest.raises(LessonValidationError, match="300 seconds"):
        make_limit_lesson(pair_count=1, duration_tick=301, authoring=False)

    with pytest.raises(LessonValidationError, match="120 seconds"):
        create_lesson_revision(
            lesson_id="duration-limit",
            created_at=NOW,
            aliases=("duration limit",),
            title="Duration Limit",
            composer="Tester",
            difficulty=None,
            hand=None,
            ppq=1,
            tempo_map=(TempoChange(0, 1_000_000),),
            time_signatures=(TimeSignature(0, 4, 4),),
            provenance=LessonProvenance(Acquisition.ORIGINAL, "test", Fidelity.VERIFIED, NOW),
            events=(event("note_on", 0, 0), event("note_off", 121, 121)),
            segments=(),
            limits=AUTHORING_LESSON_LIMITS,
        )


def test_integer_timing_carries_remainder_and_applies_tempo_changes() -> None:
    tempos = (TempoChange(0, 500_000), TempoChange(2, 400_000))

    positions = tick_positions_to_microseconds((1, 2, 3, 4), 3, tempos)

    assert positions == {1: 166_666, 2: 333_333, 3: 466_666, 4: 600_000}
    assert tick_positions_to_microseconds((4,), 3, tempos)[4] == 600_000
    assert tick_positions_to_microseconds((4,), 3, tempos, tempo_percent=50)[4] == 1_200_000


def test_segment_timeline_is_relative_balanced_and_tempo_scaled() -> None:
    events = (
        event("note_on", 0, 0),
        event("note_off", 480, 480),
        event("note_on", 480, 0, note=62),
        event("note_off", 960, 480, note=62),
    )
    lesson = make_lesson(
        events=events,
        segments=(
            LessonSegment(
                "Second",
                240,
                720,
                hand=Hand.RIGHT,
                difficulty=Difficulty.FOUNDATION,
            ),
        ),
    )

    timeline = resolve_timeline(
        lesson,
        LessonSelection(
            segment="second",
            tempo_percent=50,
            hand=Hand.RIGHT,
            difficulty=Difficulty.FOUNDATION,
        ),
    )

    assert timeline.start_tick == 240
    assert timeline.end_tick == 720
    assert timeline.duration_ticks == 480
    assert timeline.duration_us == 1_000_000
    assert timeline.events[0].absolute_tick == 0
    assert timeline.events[0].absolute_us == 0
    assert timeline.events[-1].absolute_tick == 480
    assert timeline.events[-1].absolute_us == 1_000_000
    assert [item.event_type for item in timeline.events].count("note_on") == 2
    assert [item.event_type for item in timeline.events].count("note_off") == 2


def test_segment_timing_preserves_the_full_lesson_integer_remainder() -> None:
    lesson = make_lesson(
        ppq=3,
        events=(event("note_on", 0, 0), event("note_off", 2, 2)),
        segments=(LessonSegment("Fraction", 1, 2),),
    )

    timeline = resolve_timeline(lesson, LessonSelection(segment="fraction"))

    assert timeline.duration_us == 166_667
    assert timeline.events[0].absolute_us == 0
    assert timeline.events[-1].absolute_us == 166_667


def test_loop_schedule_carries_selected_tick_phase_across_repetitions() -> None:
    lesson = make_lesson(
        ppq=3,
        events=(event("note_on", 0, 0), event("note_off", 2, 2)),
        segments=(LessonSegment("Fraction", 1, 2),),
    )
    timeline = resolve_timeline(lesson, LessonSelection(segment="fraction"))

    schedule = resolve_loop_schedule(lesson, timeline, 3)

    assert schedule.boundaries_us == (0, 166_667, 333_334, 500_000)
    assert schedule.total_duration_us == 500_000
    assert schedule.total_duration_us != timeline.duration_us * 3
    assert [item.loop_index for item in schedule.events] == [0, 0, 1, 1, 2, 2]
    assert [item.offset_us for item in schedule.events] == [
        0,
        166_667,
        166_667,
        333_334,
        333_334,
        500_000,
    ]


def test_loop_schedule_rejects_identity_mismatch_and_unbounded_loops() -> None:
    lesson = make_lesson()
    timeline = resolve_timeline(lesson)

    with pytest.raises(LessonValidationError, match="1 through 8"):
        resolve_loop_schedule(lesson, timeline, 9)
    with pytest.raises(LessonValidationError, match="identity"):
        resolve_loop_schedule(lesson, replace(timeline, lesson_id="other-piece"), 1)


def test_explicit_segment_filters_never_fall_back_to_the_whole_lesson() -> None:
    lesson = replace(make_lesson(), hand=Hand.LEFT)

    with pytest.raises(Exception, match="requested hand"):
        resolve_timeline(
            lesson,
            LessonSelection(segment="main melody", hand=Hand.LEFT),
        )


def test_explicit_segment_inherits_declared_lesson_metadata() -> None:
    lesson = make_lesson(
        segments=(LessonSegment("Inherited", 0, 480),),
    )

    timeline = resolve_timeline(
        lesson,
        LessonSelection(
            segment="inherited",
            hand=Hand.RIGHT,
            difficulty=Difficulty.FOUNDATION,
        ),
    )

    assert timeline.hand is Hand.RIGHT
    assert timeline.difficulty is Difficulty.FOUNDATION


def test_explicit_segment_rejects_mismatch_with_inherited_lesson_metadata() -> None:
    lesson = make_lesson(
        segments=(LessonSegment("Inherited", 0, 480),),
    )

    with pytest.raises(Exception, match="requested hand"):
        resolve_timeline(
            lesson,
            LessonSelection(segment="inherited", hand=Hand.LEFT),
        )


def test_half_open_segment_excludes_note_on_at_end_boundary() -> None:
    events = (
        event("note_on", 0, 0, note=60),
        event("note_off", 480, 480, note=60),
        event("note_on", 480, 0, note=62),
        event("note_off", 960, 480, note=62),
    )
    lesson = make_lesson(
        events=events,
        segments=(
            LessonSegment("First", 0, 480),
            LessonSegment("Second", 480, 960),
        ),
    )

    first = resolve_timeline(lesson, LessonSelection(segment="first"))

    assert {item.note for item in first.events} == {60}
    assert [item.event_type for item in first.events] == ["note_on", "note_off"]
    assert first.events[-1].absolute_tick == first.duration_ticks == 480


def test_carried_note_released_at_segment_start_creates_no_zero_duration_note() -> None:
    events = (
        event("note_on", 0, 0, note=60),
        event("note_off", 480, 480, note=60),
        event("note_on", 480, 0, note=62),
        event("note_off", 960, 480, note=62),
    )
    lesson = make_lesson(
        events=events,
        segments=(
            LessonSegment("First", 0, 480),
            LessonSegment("Second", 480, 960),
        ),
    )

    second = resolve_timeline(lesson, LessonSelection(segment="second"))
    full = resolve_timeline(lesson)

    assert {item.note for item in second.events} == {62}
    assert [item.event_type for item in second.events] == ["note_on", "note_off"]
    assert second.events[0].absolute_tick == second.events[0].absolute_us == 0
    assert full.events[-1].lesson_event_id == lesson.events[-1].event_id


def test_missing_segment_hand_and_difficulty_are_not_guessed() -> None:
    lesson = replace(make_lesson(segments=()), hand=None, difficulty=None)
    with pytest.raises(Exception, match="does not declare hand metadata"):
        resolve_timeline(lesson, LessonSelection(hand=Hand.LEFT))
    with pytest.raises(Exception, match="not declared"):
        resolve_timeline(lesson, LessonSelection(segment="chorus"))


def test_request_alias_tempo_and_runtime_identity_are_deterministic() -> None:
    request = LessonRequest(
        "  Tést—Piece   Main Melody ",
        segment="Main Melody",
        tempo_percent=75,
        author=AuthorMode.BOUNDED,
    )

    assert request.normalized_alias == "tést piece main melody"
    assert normalize_lesson_alias("TEST_piece") == "test piece"
    assert derive_runtime_event_id("playback", 2, "event") == derive_runtime_event_id(
        "playback", 2, "event"
    )
    with pytest.raises(LessonValidationError, match="25 through 200"):
        LessonRequest("test", tempo_percent=201)


def test_scoring_handshake_types_can_be_constructed_directly() -> None:
    resolved = ResolvedLessonEvent(
        lesson_event_id="event-1",
        event_type="note_on",
        note=60,
        velocity=80,
        channel=1,
        status=0x90,
        source_port="halcyon://coach/test",
        content_origin="halcyon",
        absolute_tick=0,
        delta_tick=0,
        absolute_us=0,
        delta_us=0,
    )
    timeline = LessonTimeline(
        lesson_id="test-piece",
        revision_id="revision",
        content_id="0" * 64,
        segment_label=None,
        hand=None,
        difficulty=None,
        tempo_percent=100,
        ppq=480,
        start_tick=0,
        end_tick=480,
        duration_ticks=480,
        duration_us=500_000,
        events=(resolved,),
    )
    latency = LatencyProfile(offset_us=12_000, jitter_us=20_000, sample_count=5)

    assert timeline.events == (resolved,)
    assert latency.tolerance_us == 60_000


def test_loader_rejects_unknown_fields_and_newer_schema() -> None:
    lesson = make_lesson()
    payload = lesson.to_dict()
    payload["unexpected"] = True
    with pytest.raises(LessonValidationError, match="unknown fields"):
        type(lesson).from_dict(payload)

    payload = lesson.to_dict()
    payload["schema_version"] = 2
    with pytest.raises(LessonValidationError) as caught:
        type(lesson).from_dict(payload)
    assert caught.value.code == "SCHEMA_UNSUPPORTED"
