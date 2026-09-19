import pytest

from halcyon.midi import (
    MidiEvent,
    MidiOrigin,
    MidiVoiceTracker,
    VoiceIdAllocator,
    choose_preferred_port,
    normalize_message,
    note_name,
)


def runtime_event(event_type: str, *, velocity: int = 100) -> MidiEvent:
    return MidiEvent(
        event_type=event_type,
        note=60,
        note_name="C4",
        velocity=velocity,
        channel=1,
        status=0x90,
        raw_message=(0x90, 60, velocity),
        delta_time=0.0,
        source="test",
        elapsed=0.0,
    )


def test_note_name_uses_midi_octaves() -> None:
    assert note_name(60) == "C4"
    assert note_name(61) == "C#4"


def test_normalize_note_on_press() -> None:
    event = normalize_message([0x90, 60, 100], 0.25, source="LPK25", elapsed=1.5)

    assert event is not None
    assert event.event_type == "press"
    assert event.note == 60
    assert event.note_name == "C4"
    assert event.velocity == 100
    assert event.channel == 1
    assert event.status == 0x90
    assert event.delta_time == 0.25
    assert event.source == "LPK25"
    assert event.source_port == "LPK25"
    assert event.elapsed == 1.5
    assert event.origin is MidiOrigin.HUMAN
    assert event.voice_id > 0
    assert event.delta_us == 250_000


def test_note_on_with_zero_velocity_is_release() -> None:
    event = normalize_message([0x91, 64, 0], 0.1, source="LPK25", elapsed=2.0)

    assert event is not None
    assert event.event_type == "release"
    assert event.note_name == "E4"
    assert event.channel == 2
    assert event.status == 0x91
    assert event.raw_message == (0x91, 64, 0)
    assert not event.matched_voice


def test_canonical_and_legacy_event_types_normalize_with_strict_validation() -> None:
    canonical_on = runtime_event("note_on")
    canonical_off = runtime_event("note_off", velocity=0)

    assert canonical_on.is_press and canonical_on.event_type == "press"
    assert canonical_on.midi_event_type == "note_on"
    assert canonical_on.legacy_event_type == "press"
    assert canonical_off.is_release and canonical_off.event_type == "release"
    assert canonical_off.midi_event_type == "note_off"
    assert runtime_event("note_on", velocity=0).is_release

    with pytest.raises(ValueError, match="event_type must be one of"):
        runtime_event("pressed")


def test_voice_tracker_pairs_oldest_repeated_note_and_preserves_origin() -> None:
    voices = MidiVoiceTracker(VoiceIdAllocator(start=41))
    first = normalize_message(
        [0x92, 60, 100],
        0.01,
        source="halcyon://coach/lesson",
        elapsed=1.0,
        origin=MidiOrigin.HALCYON,
        session_id="coach-1",
        voice_tracker=voices,
    )
    second = normalize_message(
        [0x92, 60, 90],
        0.01,
        source="halcyon://coach/lesson",
        elapsed=1.1,
        origin=MidiOrigin.HALCYON,
        session_id="coach-1",
        voice_tracker=voices,
    )
    release = normalize_message(
        [0x82, 60, 0],
        0.01,
        source="halcyon://coach/lesson",
        elapsed=1.2,
        origin=MidiOrigin.HALCYON,
        session_id="coach-1",
        voice_tracker=voices,
    )

    assert first is not None and second is not None and release is not None
    assert (first.voice_id, second.voice_id) == (41, 42)
    assert release.voice_id == first.voice_id
    assert release.matched_voice
    assert release.origin is MidiOrigin.HALCYON
    assert release.session_id == "coach-1"
    assert voices.active_voice_ids(origin=MidiOrigin.HALCYON) == (second.voice_id,)


def test_unmatched_release_gets_safe_noncolliding_identity() -> None:
    voices = MidiVoiceTracker(VoiceIdAllocator(start=90))
    human_press = normalize_message(
        [0x90, 64, 100],
        0.01,
        source="LPK25",
        elapsed=1.0,
        voice_tracker=voices,
    )
    halcyon_release = normalize_message(
        [0x80, 64, 0],
        0.01,
        source="halcyon://coach/test",
        elapsed=1.1,
        origin=MidiOrigin.HALCYON,
        voice_tracker=voices,
    )

    assert human_press is not None and halcyon_release is not None
    assert halcyon_release.voice_id != human_press.voice_id
    assert not halcyon_release.matched_voice
    assert voices.active_voice_ids(origin=MidiOrigin.HUMAN) == (human_press.voice_id,)


def test_choose_preferred_port_favors_lpk25_wireless() -> None:
    ports = ["IAC Driver Bus 1", "Akai LPK25 Wireless:Akai LPK25 Wireless MIDI 1"]

    assert choose_preferred_port(ports) == 1
