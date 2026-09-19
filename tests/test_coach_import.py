import json
from pathlib import Path

import mido
import pytest

from halcyon.coach_authoring import LessonService
from halcyon.coach_import import import_midi_to_store, main
from halcyon.coach_models import (
    Acquisition,
    AuthorMode,
    CoachError,
    Fidelity,
    Hand,
    LessonEvent,
    LessonProvenance,
    LessonRequest,
    LessonSegment,
    TempoChange,
    TimeSignature,
    create_lesson_revision,
    derive_lesson_id,
)
from halcyon.coach_store import LessonStore

NOW = "2026-07-12T12:00:00+00:00"
QUERY = "Aphex Twin Avril 14th"
POSSESSIVE_QUERY = "Aphex Twin's Avril 14th"


def _approximate_lesson():
    lesson_id = derive_lesson_id(QUERY)
    events = (
        LessonEvent(
            event_id="",
            event_type="note_on",
            note=60,
            velocity=80,
            channel=1,
            status=0x90,
            source_port=f"halcyon://coach/{lesson_id}",
            content_origin="halcyon",
            absolute_tick=0,
            delta_tick=0,
        ),
        LessonEvent(
            event_id="",
            event_type="note_off",
            note=60,
            velocity=0,
            channel=1,
            status=0x80,
            source_port=f"halcyon://coach/{lesson_id}",
            content_origin="halcyon",
            absolute_tick=480,
            delta_tick=480,
        ),
    )
    return create_lesson_revision(
        lesson_id=lesson_id,
        created_at=NOW,
        aliases=(QUERY,),
        title="Avril 14th",
        composer="Aphex Twin",
        difficulty=None,
        hand=Hand.RIGHT,
        ppq=480,
        tempo_map=(TempoChange(0, 500_000),),
        time_signatures=(TimeSignature(0, 4, 4),),
        provenance=LessonProvenance(
            acquisition=Acquisition.LOCALLY_AUTHORED_BOUNDED,
            citation="Generated educational approximation",
            fidelity=Fidelity.APPROXIMATE,
            authorization_timestamp=NOW,
            provider="openai",
            model_version="test-model",
        ),
        events=events,
        segments=(LessonSegment("main melody", 0, 480, hand=Hand.RIGHT),),
        max_polyphony=1,
    )


def _verified_original(alias: str):
    lesson = _approximate_lesson()
    return create_lesson_revision(
        lesson_id=lesson.lesson_id,
        created_at=NOW,
        aliases=(alias,),
        title=lesson.title,
        composer=lesson.composer,
        difficulty=None,
        hand=Hand.RIGHT,
        ppq=lesson.ppq,
        tempo_map=lesson.tempo_map,
        time_signatures=lesson.time_signatures,
        provenance=LessonProvenance(
            acquisition=Acquisition.ORIGINAL,
            citation="Original local exercise",
            fidelity=Fidelity.VERIFIED,
            authorization_timestamp=NOW,
        ),
        events=lesson.events,
        segments=lesson.segments,
        max_polyphony=1,
    )


def _midi(path: Path, *, melody_note: int = 88) -> Path:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    midi.tracks.append(
        mido.MidiTrack(
            [
                mido.MetaMessage("set_tempo", tempo=750_000, time=0),
                mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0),
                mido.Message("note_on", note=melody_note, velocity=90, channel=0, time=0),
                mido.Message("note_off", note=melody_note, velocity=0, channel=0, time=240),
                mido.Message("note_on", note=melody_note + 2, velocity=84, channel=0, time=0),
                mido.Message("note_off", note=melody_note + 2, velocity=0, channel=0, time=240),
            ]
        )
    )
    midi.tracks.append(
        mido.MidiTrack(
            [
                mido.Message("note_on", note=40, velocity=70, channel=0, time=0),
                mido.Message("note_on", note=44, velocity=70, channel=0, time=0),
                mido.Message("note_off", note=40, velocity=0, channel=0, time=480),
                mido.Message("note_off", note=44, velocity=0, channel=0, time=0),
            ]
        )
    )
    midi.save(path)
    return path


def _import(path: Path, store: LessonStore, *, replace_approximate: bool = True):
    return import_midi_to_store(
        path,
        track=0,
        title="Avril 14th",
        composer="Aphex Twin",
        segment_label="main melody",
        hand=Hand.RIGHT,
        aliases=(QUERY, POSSESSIVE_QUERY),
        replace_approximate=replace_approximate,
        store=store,
        created_at=NOW,
    )


class _ExplodingProvider:
    provider_name = "must-not-run"
    model_version = "must-not-run"

    def propose(self, *_args, **_kwargs):
        raise AssertionError("cache resolution called the authoring provider")


def test_import_supersedes_only_approximation_and_resolves_offline(tmp_path: Path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base = store.persist(_approximate_lesson())
    base_manifest = store.revision_path(base.lesson_id, base.content_id)
    base_manifest_bytes = base_manifest.read_bytes()
    base_binding = store.alias_binding_path(QUERY)
    base_binding_bytes = base_binding.read_bytes()

    result = _import(_midi(tmp_path / "avril.mid"), store)

    assert result.status == "imported"
    assert result.superseded_aliases == ("aphex twin avril 14th",)
    assert result.lesson.provenance.acquisition is Acquisition.USER_SUPPLIED
    assert result.lesson.provenance.fidelity is Fidelity.VERIFIED
    assert result.lesson.lesson_id == base.lesson_id
    assert len(result.lesson.events) == 4
    assert {event.note for event in result.lesson.events} == {88, 90}
    assert 40 not in {event.note for event in result.lesson.events}
    assert base_manifest.read_bytes() == base_manifest_bytes
    assert base_binding.read_bytes() == base_binding_bytes
    assert store.load_revision(base.lesson_id, base.content_id) == base

    for alias in (QUERY, POSSESSIVE_QUERY, "Aphex Twin’s Avril 14th"):
        assert store.resolve(alias) == result.lesson

    service = LessonService(store=LessonStore(root=store.root), provider=_ExplodingProvider())
    resolution = service.resolve(
        LessonRequest(
            query=QUERY,
            segment="main melody",
            author=AuthorMode.NEVER,
        )
    )
    assert resolution.cache_hit is True
    assert resolution.authored is False
    assert resolution.lesson == result.lesson


def test_repeated_identical_import_is_idempotent(tmp_path: Path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    store.persist(_approximate_lesson())
    source = _midi(tmp_path / "avril.mid")
    first = _import(source, store)
    revision_paths = tuple(sorted((store.root / "lessons").glob("*/*.json")))
    supersession_path = store.alias_supersession_path(QUERY)
    supersession_bytes = supersession_path.read_bytes()

    second = _import(source, LessonStore(root=store.root))

    assert second.status == "already_imported"
    assert second.lesson.content_id == first.lesson.content_id
    assert tuple(sorted((store.root / "lessons").glob("*/*.json"))) == revision_paths
    assert supersession_path.read_bytes() == supersession_bytes


def test_different_file_cannot_replace_write_once_import(tmp_path: Path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    store.persist(_approximate_lesson())
    first = _import(_midi(tmp_path / "first.mid"), store)
    supersession_path = store.alias_supersession_path(QUERY)
    supersession_bytes = supersession_path.read_bytes()

    with pytest.raises(CoachError) as caught:
        _import(
            _midi(tmp_path / "different.mid", melody_note=91),
            LessonStore(root=store.root),
        )

    assert caught.value.code == "ALIAS_CONFLICT"
    assert LessonStore(root=store.root).resolve(QUERY) == first.lesson
    assert supersession_path.read_bytes() == supersession_bytes


def test_replacing_generated_lesson_requires_explicit_flag(tmp_path: Path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base = store.persist(_approximate_lesson())

    with pytest.raises(CoachError) as caught:
        _import(
            _midi(tmp_path / "avril.mid"),
            store,
            replace_approximate=False,
        )

    assert caught.value.code == "MIDI_IMPORT_REPLACEMENT_REQUIRED"
    assert store.resolve(QUERY) == base
    assert store.resolve(POSSESSIVE_QUERY) is None
    assert not store.alias_supersession_path(QUERY).exists()
    assert len(tuple((store.root / "lessons").glob("*/*.json"))) == 1


def test_non_generated_verified_lesson_cannot_be_superseded(tmp_path: Path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    original = store.persist(_verified_original(QUERY))

    with pytest.raises(CoachError) as caught:
        _import(_midi(tmp_path / "avril.mid"), store)

    assert caught.value.code == "ALIAS_CONFLICT"
    assert store.resolve(QUERY) == original
    assert not store.alias_supersession_path(QUERY).exists()


def test_packaged_approximation_is_rejected_before_any_local_write(tmp_path: Path) -> None:
    builtin = _approximate_lesson()
    store = LessonStore(root=tmp_path / "coach", builtins=(builtin,))

    with pytest.raises(CoachError) as caught:
        _import(_midi(tmp_path / "avril.mid"), store)

    assert caught.value.code == "ALIAS_CONFLICT"
    assert store.resolve(QUERY) == builtin
    assert store.resolve(POSSESSIVE_QUERY) is None
    assert not store.root.exists()


def test_missing_aliases_bind_directly_without_a_supersession(tmp_path: Path) -> None:
    store = LessonStore(root=tmp_path / "coach")

    result = _import(_midi(tmp_path / "avril.mid"), store)

    assert result.superseded_aliases == ()
    assert store.resolve(QUERY) == result.lesson
    assert store.resolve(POSSESSIVE_QUERY) == result.lesson
    assert not store.alias_supersession_path(QUERY).exists()


def test_retry_finishes_after_failure_between_persist_and_supersession(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base = store.persist(_approximate_lesson())
    source = _midi(tmp_path / "avril.mid")

    def interrupted(*_args, **_kwargs):
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(store, "supersede_alias", interrupted)
    with pytest.raises(RuntimeError, match="simulated"):
        _import(source, store)

    partial = LessonStore(root=store.root)
    assert partial.resolve(QUERY) == base
    imported = partial.resolve(POSSESSIVE_QUERY)
    assert imported is not None
    assert imported.provenance.acquisition is Acquisition.USER_SUPPLIED

    completed = _import(source, partial)

    assert completed.status == "imported"
    assert partial.resolve(QUERY) == imported
    assert partial.resolve(POSSESSIVE_QUERY) == imported


def test_cli_emits_only_aggregate_result_and_is_not_a_public_command(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    coach_root = tmp_path / "coach"
    monkeypatch.setenv("HALCYON_COACH_PATH", str(coach_root))
    LessonStore().persist(_approximate_lesson())
    source_parent = tmp_path / "private-name"
    source_parent.mkdir()
    source = _midi(source_parent / "avril.mid")

    exit_code = main(
        [
            str(source),
            "--track",
            "0",
            "--title",
            "Avril 14th",
            "--composer",
            "Aphex Twin",
            "--segment",
            "main melody",
            "--hand",
            "right",
            "--alias",
            QUERY,
            "--alias",
            POSSESSIVE_QUERY,
            "--replace-approximate",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert payload["status"] == "imported"
    assert payload["event_count"] == 4
    assert payload["note_count"] == 2
    assert payload["acquisition"] == "user_supplied"
    assert payload["fidelity"] == "verified"
    assert str(source) not in captured.out
    assert "private-name" not in captured.out
    assert "citation" not in payload
    assert "events" not in payload
    menu = (Path(__file__).parents[1] / "halcyon" / "menu.xml").read_text(encoding="utf-8")
    assert "halcyon-import-midi" not in menu
    assert "coach_import" not in menu
