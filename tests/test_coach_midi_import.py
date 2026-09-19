from pathlib import Path

import mido
import pytest

from halcyon.coach_midi_import import (
    MAX_MIDI_SOURCE_BYTES,
    import_midi_lesson,
    inspect_midi_file,
)
from halcyon.coach_models import (
    Acquisition,
    CoachError,
    Fidelity,
    Hand,
)

NOW = "2026-07-12T12:00:00+00:00"


def _save(path: Path, *tracks: mido.MidiTrack, midi_type: int = 1, ppq: int = 480) -> Path:
    midi = mido.MidiFile(type=midi_type, ticks_per_beat=ppq)
    midi.tracks.extend(tracks)
    midi.save(path)
    return path


def _two_track_midi(path: Path) -> Path:
    melody = mido.MidiTrack(
        [
            mido.MetaMessage("track_name", name="Melody", time=0),
            mido.MetaMessage("set_tempo", tempo=500_000, time=0),
            mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0),
            mido.Message("note_on", note=60, velocity=91, channel=0, time=0),
            # A velocity-zero note_on is the MIDI wire representation of note_off.
            mido.Message("note_on", note=60, velocity=0, channel=0, time=480),
        ]
    )
    accompaniment = mido.MidiTrack(
        [
            mido.MetaMessage("track_name", name="Accompaniment", time=0),
            # Later track order wins deterministic same-tick metadata collapse.
            mido.MetaMessage("set_tempo", tempo=600_000, time=0),
            mido.MetaMessage("time_signature", numerator=3, denominator=4, time=0),
            mido.Message("note_on", note=64, velocity=70, channel=1, time=240),
            mido.MetaMessage("set_tempo", tempo=400_000, time=0),
            mido.MetaMessage("time_signature", numerator=5, denominator=4, time=0),
            mido.Message("note_off", note=64, velocity=12, channel=1, time=240),
        ]
    )
    return _save(path, melody, accompaniment)


def _import(path: Path, *, track: int | str = 0):
    return import_midi_lesson(
        path,
        track=track,  # type: ignore[arg-type]
        lesson_id="aphex-twin-avril-14th-user-midi",
        aliases=("Aphex Twin Avril 14th",),
        title="Avril 14th",
        composer="Aphex Twin",
        segment_label="main melody",
        hand=Hand.RIGHT,
        created_at=NOW,
    )


def test_inspection_returns_aggregate_track_selection_facts_only(tmp_path: Path) -> None:
    source = _two_track_midi(tmp_path / "lesson.mid")

    summary = inspect_midi_file(source)

    assert summary.filename == "lesson.mid"
    assert len(summary.sha256) == 64
    assert summary.midi_type == 1
    assert summary.ppq == 480
    assert summary.track_count == 2
    assert summary.note_event_count == 4
    assert summary.tempo_change_count == 3
    assert summary.time_signature_change_count == 3
    assert not hasattr(summary, "events")
    assert summary.tracks[0].name == "Melody"
    assert summary.tracks[0].note_event_count == 2
    assert summary.tracks[0].note_on_count == 1
    assert summary.tracks[0].note_off_count == 1
    assert summary.tracks[0].channels == (1,)
    assert summary.tracks[0].minimum_note == summary.tracks[0].maximum_note == 60
    assert summary.tracks[0].max_polyphony == 1
    assert summary.tracks[0].unmatched_note_off_count == 0
    assert summary.tracks[0].hanging_note_count == 0


def test_track_import_uses_global_metadata_and_normalizes_velocity_zero(tmp_path: Path) -> None:
    lesson = _import(_two_track_midi(tmp_path / "lesson.mid"), track=0)

    assert lesson.provenance.acquisition is Acquisition.USER_SUPPLIED
    assert lesson.provenance.fidelity is Fidelity.VERIFIED
    assert lesson.provenance.provider is None
    assert lesson.provenance.model_version is None
    assert [(item.absolute_tick, item.microseconds_per_quarter) for item in lesson.tempo_map] == [
        (0, 600_000),
        (240, 400_000),
    ]
    assert [
        (item.absolute_tick, item.numerator, item.denominator) for item in lesson.time_signatures
    ] == [(0, 3, 4), (240, 5, 4)]
    assert [
        (event.event_type, event.channel, event.status, event.velocity) for event in lesson.events
    ] == [
        ("note_on", 1, 0x90, 91),
        ("note_off", 1, 0x90, 0),
    ]
    assert [event.absolute_tick for event in lesson.events] == [0, 480]
    assert [event.delta_tick for event in lesson.events] == [0, 480]
    assert lesson.hand is Hand.RIGHT
    assert lesson.segments[0].label == "main melody"
    assert lesson.segments[0].hand is Hand.RIGHT


def test_all_tracks_are_merged_in_stable_tick_track_message_order(tmp_path: Path) -> None:
    lesson = _import(_two_track_midi(tmp_path / "lesson.mid"), track="all")

    assert [(event.note, event.event_type) for event in lesson.events] == [
        (60, "note_on"),
        (64, "note_on"),
        (60, "note_off"),
        (64, "note_off"),
    ]
    assert [event.delta_tick for event in lesson.events] == [0, 240, 240, 0]
    assert [event.channel for event in lesson.events] == [1, 2, 1, 2]
    assert lesson.max_polyphony == 2


def test_midi_defaults_are_added_when_global_metadata_is_absent(tmp_path: Path) -> None:
    track = mido.MidiTrack(
        [
            mido.Message("note_on", note=60, velocity=80, time=0),
            mido.Message("note_off", note=60, velocity=0, time=480),
        ]
    )

    lesson = _import(_save(tmp_path / "defaults.midi", track, midi_type=0))

    assert [(item.absolute_tick, item.microseconds_per_quarter) for item in lesson.tempo_map] == [
        (0, 500_000)
    ]
    assert [
        (item.absolute_tick, item.numerator, item.denominator) for item in lesson.time_signatures
    ] == [(0, 4, 4)]


@pytest.mark.parametrize("name", ["lesson.txt", "lesson.mid.txt", "lesson"])
def test_only_mid_and_midi_extensions_are_accepted(tmp_path: Path, name: str) -> None:
    source = tmp_path / name
    source.write_bytes(b"not relevant")

    with pytest.raises(CoachError, match=r"\.mid or \.midi") as caught:
        inspect_midi_file(source)

    assert caught.value.field == "path"


def test_source_must_be_a_bounded_regular_file(tmp_path: Path) -> None:
    directory = tmp_path / "directory.mid"
    directory.mkdir()
    with pytest.raises(CoachError, match="regular file"):
        inspect_midi_file(directory)

    oversized = tmp_path / "oversized.mid"
    oversized.write_bytes(b"x" * 65)
    with pytest.raises(CoachError, match="64 bytes") as caught:
        inspect_midi_file(oversized, max_source_bytes=64)
    assert caught.value.code == "MIDI_IMPORT_TOO_LARGE"

    with pytest.raises(CoachError, match=str(MAX_MIDI_SOURCE_BYTES)):
        inspect_midi_file(oversized, max_source_bytes=MAX_MIDI_SOURCE_BYTES + 1)


def test_malformed_empty_and_type_two_files_are_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mid"
    empty.write_bytes(b"")
    with pytest.raises(CoachError, match="empty"):
        inspect_midi_file(empty)

    malformed = tmp_path / "malformed.mid"
    malformed.write_bytes(b"not a MIDI file")
    with pytest.raises(CoachError, match="malformed or unsupported"):
        inspect_midi_file(malformed)

    type_two = _save(
        tmp_path / "type-two.mid",
        mido.MidiTrack([mido.MetaMessage("end_of_track")]),
        mido.MidiTrack([mido.MetaMessage("end_of_track")]),
        midi_type=2,
    )
    with pytest.raises(CoachError, match="type 2") as caught:
        inspect_midi_file(type_two)
    assert caught.value.code == "MIDI_IMPORT_UNSUPPORTED"


@pytest.mark.parametrize("selection", [-1, 2, True, None, "melody"])
def test_track_selection_is_explicit_and_validated(tmp_path: Path, selection: object) -> None:
    source = _two_track_midi(tmp_path / "lesson.mid")

    with pytest.raises(CoachError, match="track"):
        _import(source, track=selection)  # type: ignore[arg-type]


def test_empty_unmatched_and_hanging_note_selections_are_rejected(tmp_path: Path) -> None:
    empty = _save(
        tmp_path / "no-notes.mid",
        mido.MidiTrack([mido.MetaMessage("track_name", name="No notes")]),
        midi_type=0,
    )
    with pytest.raises(CoachError, match="no note events") as empty_error:
        _import(empty)
    assert empty_error.value.code == "MIDI_IMPORT_INVALID"

    unmatched = _save(
        tmp_path / "unmatched.mid",
        mido.MidiTrack([mido.Message("note_off", note=60, velocity=0, time=480)]),
        midi_type=0,
    )
    with pytest.raises(CoachError, match="without matching") as unmatched_error:
        _import(unmatched)
    assert unmatched_error.value.code == "MIDI_IMPORT_INVALID"

    hanging = _save(
        tmp_path / "hanging.mid",
        mido.MidiTrack([mido.Message("note_on", note=60, velocity=80, time=480)]),
        midi_type=0,
    )
    with pytest.raises(CoachError, match="hanging") as hanging_error:
        _import(hanging)
    assert hanging_error.value.code == "MIDI_IMPORT_INVALID"


def test_stored_polyphony_event_and_duration_limits_are_enforced(tmp_path: Path) -> None:
    chord_messages = [
        mido.Message("note_on", note=60 + index, velocity=80, time=0) for index in range(9)
    ]
    chord_messages.extend(
        mido.Message("note_off", note=60 + index, velocity=0, time=480 if index == 0 else 0)
        for index in range(9)
    )
    chord = _save(tmp_path / "nine-notes.mid", mido.MidiTrack(chord_messages), midi_type=0)
    with pytest.raises(CoachError, match="max_polyphony") as polyphony_error:
        _import(chord)
    assert polyphony_error.value.code == "MIDI_IMPORT_INVALID"

    event_messages: list[mido.Message] = []
    for _index in range(1_025):
        event_messages.append(mido.Message("note_on", note=60, velocity=80, time=1))
        event_messages.append(mido.Message("note_off", note=60, velocity=0, time=1))
    too_many = _save(tmp_path / "too-many-events.mid", mido.MidiTrack(event_messages), midi_type=0)
    with pytest.raises(CoachError, match="more than 2048") as event_error:
        _import(too_many)
    assert event_error.value.code == "MIDI_IMPORT_INVALID"

    too_long = _save(
        tmp_path / "too-long.mid",
        mido.MidiTrack(
            [
                mido.MetaMessage("set_tempo", tempo=1_000_000, time=0),
                mido.Message("note_on", note=60, velocity=80, time=0),
                mido.Message("note_off", note=60, velocity=0, time=301),
            ]
        ),
        midi_type=0,
        ppq=1,
    )
    with pytest.raises(CoachError, match="300 seconds") as duration_error:
        _import(too_long)
    assert duration_error.value.code == "MIDI_IMPORT_INVALID"


def test_import_is_deterministic_and_never_persists_a_private_path(tmp_path: Path) -> None:
    private_parent = tmp_path / "Users" / "private-person" / "Downloads"
    private_parent.mkdir(parents=True)
    source = _two_track_midi(private_parent / "Avril\n14th ♫.mid")

    first = _import(source)
    second = _import(source)

    assert first == second
    assert "private-person" not in first.provenance.citation
    assert "Downloads" not in first.provenance.citation
    assert "\n" not in first.provenance.citation
    assert first.provenance.citation.startswith("user-supplied MIDI; filename=Avril_14th _.mid")
    assert f"sha256={inspect_midi_file(source).sha256}" in first.provenance.citation
    assert all("private-person" not in event.source_port for event in first.events)
    assert all("Avril" not in event.source_port for event in first.events)
