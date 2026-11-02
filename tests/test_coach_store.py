from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading

import pytest

import halcyon.coach_store as coach_store
from halcyon.coach_models import (
    Acquisition,
    CoachError,
    Fidelity,
    LessonEvent,
    LessonProvenance,
    LessonSegment,
    TempoChange,
    TimeSignature,
    create_lesson_revision,
)
from halcyon.coach_store import (
    CATALOG_FILENAME,
    LessonReference,
    LessonStore,
    default_coach_path,
)

NOW = "2026-07-12T00:00:00+00:00"


def make_lesson(
    *,
    lesson_id: str = "store-piece",
    alias: str = "Store Piece Main",
    note: int = 60,
    created_at: str = NOW,
):
    events = (
        LessonEvent(
            event_id="temporary",
            event_type="note_on",
            note=note,
            velocity=80,
            channel=1,
            status=0x90,
            source_port=f"halcyon://coach/{lesson_id}",
            content_origin="halcyon",
            absolute_tick=0,
            delta_tick=0,
        ),
        LessonEvent(
            event_id="temporary",
            event_type="note_off",
            note=note,
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
        created_at=created_at,
        aliases=(alias,),
        title="Store Piece",
        composer="Test Composer",
        difficulty=None,
        hand=None,
        ppq=480,
        tempo_map=(TempoChange(0, 500_000),),
        time_signatures=(TimeSignature(0, 4, 4),),
        provenance=LessonProvenance(
            Acquisition.ORIGINAL,
            "Original test material",
            Fidelity.VERIFIED,
            NOW,
        ),
        events=events,
        segments=(LessonSegment("Main", 0, 480),),
    )


def lesson_reference(lesson) -> LessonReference:  # type: ignore[no-untyped-def]
    return LessonReference(lesson.lesson_id, lesson.content_id)


def persisted_supersession_pair(store: LessonStore):  # type: ignore[no-untyped-def]
    base = make_lesson(
        lesson_id="superseded-piece",
        alias="Stable Lesson Alias",
        note=60,
    )
    replacement = make_lesson(
        lesson_id=base.lesson_id,
        alias="Imported Lesson Archive",
        note=62,
    )
    store.persist(base)
    store.persist(replacement)
    return base, replacement


def synchronize_two_creates(monkeypatch, *predicates) -> None:  # type: ignore[no-untyped-def]
    barriers = tuple(threading.Barrier(2) for _ in predicates)
    real_create = coach_store._atomic_create_json

    def synchronized_create(path, payload):  # type: ignore[no-untyped-def]
        for predicate, barrier in zip(predicates, barriers, strict=True):
            if predicate(path):
                barrier.wait(timeout=10)
        return real_create(path, payload)

    monkeypatch.setattr(coach_store, "_atomic_create_json", synchronized_create)


def test_atomic_round_trip_commits_revision_before_catalog(tmp_path, monkeypatch) -> None:
    root = tmp_path / "coach"
    store = LessonStore(root=root)
    lesson = make_lesson()
    operations: list[tuple[str, Path]] = []
    real_write = coach_store._atomic_write_json
    real_create = coach_store._atomic_create_json

    def recording_write(path, payload):
        operations.append(("write", path))
        real_write(path, payload)

    def recording_create(path, payload):
        operations.append(("create", path))
        return real_create(path, payload)

    monkeypatch.setattr(coach_store, "_atomic_write_json", recording_write)
    monkeypatch.setattr(coach_store, "_atomic_create_json", recording_create)

    saved = store.persist(lesson)

    assert saved == lesson
    assert operations == [
        ("create", store.revision_path(lesson.lesson_id, lesson.content_id)),
        ("create", store.alias_binding_path(lesson.aliases[0])),
        ("write", root / CATALOG_FILENAME),
    ]
    assert LessonStore(root=root).resolve(" STORE_piece — MAIN ") == lesson
    assert not tuple(root.rglob("*.tmp"))


def test_concurrent_conflicting_alias_bindings_have_one_stable_winner(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "coach"
    first = make_lesson(lesson_id="race-first", alias="Race Alias", note=60)
    second = make_lesson(lesson_id="race-second", alias="Race Alias", note=62)
    synchronize_two_creates(
        monkeypatch,
        lambda path: path.parent.parent.name == "lessons",
        lambda path: path.parent.name == coach_store.ALIAS_BINDING_DIRNAME,
    )

    def attempt(lesson):  # type: ignore[no-untyped-def]
        try:
            return "saved", LessonStore(root=root).persist(lesson)
        except CoachError as exc:
            return "error", exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(attempt, (first, second)))

    saved = [value for status, value in results if status == "saved"]
    errors = [value for status, value in results if status == "error"]
    assert len(saved) == 1
    assert len(errors) == 1
    winner = saved[0]
    conflict = errors[0]
    assert conflict.code == "ALIAS_CONFLICT"
    assert conflict.field == "aliases"
    binding_path = LessonStore(root=root).alias_binding_path("race alias")
    binding_bytes = binding_path.read_bytes()
    assert json.loads(binding_bytes) == {
        "schema_version": 1,
        "alias": "race alias",
        "lesson_id": winner.lesson_id,
        "content_id": winner.content_id,
    }
    assert LessonStore(root=root).resolve("race alias") == winner
    assert binding_path.read_bytes() == binding_bytes
    assert all(
        LessonStore(root=root).revision_path(candidate.lesson_id, candidate.content_id).exists()
        for candidate in (first, second)
    )
    assert not tuple(root.rglob("*.tmp"))


def test_identical_concurrent_alias_binding_writes_are_idempotent(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "coach"
    lesson = make_lesson(lesson_id="race-identical", alias="Shared Race Alias")
    synchronize_two_creates(
        monkeypatch,
        lambda path: path.parent.name == coach_store.ALIAS_BINDING_DIRNAME,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        saved = tuple(
            executor.map(
                lambda _: LessonStore(root=root).persist(lesson),
                range(2),
            )
        )

    assert saved == (lesson, lesson)
    assert LessonStore(root=root).resolve("shared race alias") == lesson
    assert not tuple(root.rglob("*.tmp"))


@pytest.mark.parametrize(
    ("record_kind", "expected_code"),
    (("malformed", "LESSON_CORRUPT"), ("newer", "SCHEMA_UNSUPPORTED")),
)
def test_alias_binding_race_with_invalid_winner_fails_closed(
    tmp_path,
    monkeypatch,
    record_kind,
    expected_code,
) -> None:
    root = tmp_path / "coach"
    lesson = make_lesson(lesson_id="invalid-race", alias="Invalid Race Alias")
    real_create = coach_store._atomic_create_json
    winning_bytes: bytes | None = None

    def invalid_winner(path, payload):  # type: ignore[no-untyped-def]
        nonlocal winning_bytes
        if path.parent.name != coach_store.ALIAS_BINDING_DIRNAME:
            return real_create(path, payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        if record_kind == "malformed":
            winning_bytes = b"{not-json"
        else:
            newer = dict(payload)
            newer["schema_version"] = 2
            winning_bytes = (json.dumps(newer, sort_keys=True) + "\n").encode()
        path.write_bytes(winning_bytes)
        return False

    monkeypatch.setattr(coach_store, "_atomic_create_json", invalid_winner)

    with pytest.raises(CoachError) as caught:
        LessonStore(root=root).persist(lesson)

    assert caught.value.code == expected_code
    binding_path = LessonStore(root=root).alias_binding_path(lesson.aliases[0])
    assert binding_path.read_bytes() == winning_bytes
    assert not LessonStore(root=root).catalog_path.exists()
    assert not tuple(root.rglob("*.tmp"))


def test_concurrent_revision_metadata_never_overwrites_canonical_winner(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "coach"
    common = make_lesson(lesson_id="revision-race", alias="Unused Common Alias")
    first = replace(
        common,
        aliases=("revision race alpha",),
        title="Alpha metadata",
        provenance=replace(common.provenance, citation="Alpha provenance"),
    )
    second = replace(
        common,
        aliases=("revision race beta",),
        title="Beta metadata",
        provenance=replace(common.provenance, citation="Beta provenance"),
    )
    assert first.content_id == second.content_id
    synchronize_two_creates(
        monkeypatch,
        lambda path: path.parent.parent.name == "lessons",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        saved = tuple(
            executor.map(
                lambda candidate: LessonStore(root=root).persist(candidate),
                (first, second),
            )
        )

    store = LessonStore(root=root)
    canonical = store.load_revision(common.lesson_id, common.content_id)
    assert canonical in (first, second)
    assert saved == (canonical, canonical)
    manifest_path = store.revision_path(common.lesson_id, common.content_id)
    manifest_bytes = manifest_path.read_bytes()
    assert store.resolve(first.aliases[0]) == canonical
    assert store.resolve(second.aliases[0]) == canonical
    assert manifest_path.read_bytes() == manifest_bytes
    assert not tuple(root.rglob("*.tmp"))


def test_identical_content_deduplicates_without_mutating_revision(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    first = make_lesson(alias="First Alias")
    saved = store.persist(first)
    path = store.revision_path(first.lesson_id, first.content_id)
    original_bytes = path.read_bytes()
    duplicate = replace(
        make_lesson(alias="Second Alias", created_at="2030-01-01T00:00:00+00:00"),
        title="Different display metadata",
    )

    returned = store.persist(duplicate)

    assert returned == saved
    assert path.read_bytes() == original_bytes
    assert store.resolve("second alias") == saved
    assert len(tuple((store.root / "lessons" / first.lesson_id).glob("*.json"))) == 1

    store.catalog_path.write_text("{broken", encoding="utf-8")
    rebuilt = LessonStore(root=store.root)
    assert rebuilt.resolve("first alias") == saved
    assert rebuilt.resolve("second alias") == saved
    assert path.read_bytes() == original_bytes


def test_alias_binding_is_closed_normalized_and_named_by_hash(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    lesson = make_lesson(alias="  Durable_Alias — One  ")

    store.persist(lesson)

    alias = lesson.aliases[0]
    binding_path = store.alias_binding_path(alias)
    assert binding_path.name == f"{hashlib.sha256(alias.encode()).hexdigest()}.json"
    assert json.loads(binding_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "alias": alias,
        "lesson_id": lesson.lesson_id,
        "content_id": lesson.content_id,
    }


def test_equal_content_under_distinct_lesson_ids_preserves_each_manifest(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    first = make_lesson(lesson_id="first-identity", alias="First Identity")
    second = replace(
        make_lesson(lesson_id="second-identity", alias="Second Identity"),
        title="Different title",
        composer="Different composer",
        provenance=replace(first.provenance, citation="Separate authorization record"),
    )

    assert first.content_id == second.content_id
    assert first.revision_id == second.revision_id
    assert first.events[0].event_id != second.events[0].event_id

    store.persist(first)
    store.persist(second)
    first_path = store.revision_path(first.lesson_id, first.content_id)
    second_path = store.revision_path(second.lesson_id, second.content_id)

    assert first_path != second_path
    assert first_path.is_file() and second_path.is_file()
    assert store.resolve("first identity").provenance.citation == "Original test material"
    assert store.resolve("second identity").provenance.citation == "Separate authorization record"

    store.catalog_path.unlink()
    rebuilt = LessonStore(root=store.root)
    assert rebuilt.resolve("first identity") == first
    assert rebuilt.resolve("second identity") == second


def test_different_content_creates_an_immutable_revision(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    first = make_lesson(alias="Version One", note=60)
    second = make_lesson(alias="Version Two", note=62)

    store.persist(first)
    store.persist(second)

    assert first.content_id != second.content_id
    assert store.load_revision(first.lesson_id, first.content_id) == first
    assert store.load_revision(second.lesson_id, second.content_id) == second
    assert len(tuple((store.root / "lessons" / first.lesson_id).glob("*.json"))) == 2


def test_built_in_aliases_are_read_only_and_take_precedence(tmp_path) -> None:
    builtin = make_lesson(lesson_id="builtin", alias="Shared Alias", note=60)
    user = make_lesson(lesson_id="user-copy", alias="Shared Alias", note=62)
    store = LessonStore(root=tmp_path / "coach", builtins=(builtin,))

    store.persist(user)

    assert store.resolve("shared alias") == builtin
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert "shared alias" not in catalog["aliases"]
    assert not store.revision_path(builtin.lesson_id, builtin.content_id).exists()


def test_invalid_catalog_rebuilds_from_valid_manifests(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    lesson = make_lesson()
    store.persist(lesson)
    store.catalog_path.write_text("{broken", encoding="utf-8")

    rebuilt = LessonStore(root=store.root).resolve(lesson.aliases[0])

    assert rebuilt == lesson
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert catalog["aliases"][lesson.aliases[0]]["content_id"] == lesson.content_id


def test_boolean_catalog_schema_is_corrupt_and_rebuilt_as_integer(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    lesson = make_lesson()
    store.persist(lesson)
    store.catalog_path.write_text(
        '{"schema_version":true,"aliases":{},"excluded_aliases":{}}\n',
        encoding="utf-8",
    )

    assert LessonStore(root=store.root).resolve(lesson.aliases[0]) == lesson
    rebuilt = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert type(rebuilt["schema_version"]) is int
    assert rebuilt["schema_version"] == 1


def test_corrupt_revision_is_untouched_and_surfaces_recovery(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    lesson = make_lesson()
    store.persist(lesson)
    path = store.revision_path(lesson.lesson_id, lesson.content_id)
    corrupt = b"{not-json"
    path.write_bytes(corrupt)

    with pytest.raises(Exception) as caught:
        store.resolve(lesson.aliases[0])

    assert caught.value.code == "LESSON_CORRUPT"
    assert "restore" in caught.value.resolution.lower()
    assert path.read_bytes() == corrupt


def test_newer_lesson_schema_is_excluded_untouched_and_diagnosed(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    lesson = make_lesson()
    store.persist(lesson)
    path = store.revision_path(lesson.lesson_id, lesson.content_id)
    payload = lesson.to_dict()
    payload["schema_version"] = 2
    newer = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(newer)
    store.catalog_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(Exception) as resolved_error:
        store.resolve(lesson.aliases[0])
    assert resolved_error.value.code == "SCHEMA_UNSUPPORTED"
    with pytest.raises(Exception) as repeated_error:
        store.resolve(lesson.aliases[0])
    assert repeated_error.value.code == "SCHEMA_UNSUPPORTED"
    with pytest.raises(Exception) as fresh_error:
        LessonStore(root=store.root).resolve(lesson.aliases[0])
    assert fresh_error.value.code == "SCHEMA_UNSUPPORTED"
    assert store.diagnostics()[0].code == "SCHEMA_UNSUPPORTED"
    assert path.read_bytes() == newer

    with pytest.raises(Exception) as caught:
        store.load_revision(lesson.lesson_id, lesson.content_id)
    assert caught.value.code == "SCHEMA_UNSUPPORTED"


def test_rebuilt_corrupt_alias_remains_typed_across_store_instances(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    lesson = make_lesson()
    store.persist(lesson)
    path = store.revision_path(lesson.lesson_id, lesson.content_id)
    payload = lesson.to_dict()
    payload["events"][0]["note"] = 61
    corrupt = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(corrupt)
    store.catalog_path.write_text("{broken", encoding="utf-8")

    for candidate in (store, LessonStore(root=store.root)):
        with pytest.raises(Exception) as caught:
            candidate.resolve(lesson.aliases[0])
        assert caught.value.code == "LESSON_CORRUPT"
        assert path.read_bytes() == corrupt


@pytest.mark.parametrize(
    ("schema_version", "extra", "expected_code"),
    [
        (1, True, "LESSON_CORRUPT"),
        (2, False, "SCHEMA_UNSUPPORTED"),
        (True, False, "LESSON_CORRUPT"),
        (1.0, False, "LESSON_CORRUPT"),
    ],
)
def test_invalid_alias_binding_is_untouched_excluded_and_typed(
    tmp_path, schema_version, extra, expected_code
) -> None:
    store = LessonStore(root=tmp_path / "coach")
    lesson = make_lesson(alias="Authoritative Binding")
    store.persist(lesson)
    binding_path = store.alias_binding_path(lesson.aliases[0])
    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    payload["schema_version"] = schema_version
    if extra:
        payload["unexpected"] = True
    invalid = (json.dumps(payload, sort_keys=True) + "\n").encode()
    binding_path.write_bytes(invalid)
    store.catalog_path.write_text("{broken", encoding="utf-8")

    for candidate in (LessonStore(root=store.root), LessonStore(root=store.root)):
        with pytest.raises(Exception) as caught:
            candidate.resolve("authoritative binding")
        assert caught.value.code == expected_code
        assert binding_path.read_bytes() == invalid

    rebuilt = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert lesson.aliases[0] not in rebuilt["aliases"]
    assert rebuilt["excluded_aliases"][lesson.aliases[0]]["code"] == expected_code


def test_invalid_binding_alias_text_is_corrupt_not_authoring_input_error(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    lesson = make_lesson(alias="Original Binding Hash")
    store.persist(lesson)
    binding_path = store.alias_binding_path(lesson.aliases[0])
    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    payload["alias"] = " "
    invalid = (json.dumps(payload, sort_keys=True) + "\n").encode()
    binding_path.write_bytes(invalid)
    store.catalog_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(Exception) as caught:
        LessonStore(root=store.root).resolve(lesson.aliases[0])

    assert caught.value.code == "LESSON_CORRUPT"
    assert binding_path.read_bytes() == invalid
    rebuilt = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert lesson.aliases[0] not in rebuilt["aliases"]
    assert rebuilt["excluded_aliases"][lesson.aliases[0]]["code"] == "LESSON_CORRUPT"


def test_malformed_json_binding_hash_suppresses_manifest_seed(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    lesson = make_lesson(alias="Manifest Seed Binding")
    store.persist(lesson)
    binding_path = store.alias_binding_path(lesson.aliases[0])
    malformed = b"{not-json"
    binding_path.write_bytes(malformed)
    store.catalog_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(Exception) as caught:
        LessonStore(root=store.root).resolve("manifest seed binding")

    assert caught.value.code == "LESSON_CORRUPT"
    assert binding_path.read_bytes() == malformed
    rebuilt = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert lesson.aliases[0] not in rebuilt["aliases"]
    assert rebuilt["excluded_aliases"][lesson.aliases[0]]["code"] == "LESSON_CORRUPT"


def test_alias_miss_reconciles_revision_written_before_catalog_commit(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    store.rebuild_catalog()
    old_catalog = store.catalog_path.read_bytes()
    lesson = make_lesson()
    path = store.revision_path(lesson.lesson_id, lesson.content_id)
    path.parent.mkdir(parents=True)
    encoded = (json.dumps(lesson.to_dict(), sort_keys=True) + "\n").encode()
    path.write_bytes(encoded)

    assert store.catalog_path.read_bytes() == old_catalog
    assert LessonStore(root=store.root).resolve(lesson.aliases[0]) == lesson
    assert path.read_bytes() == encoded
    rebuilt = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert rebuilt["aliases"][lesson.aliases[0]]["content_id"] == lesson.content_id


def test_alias_collision_is_rejected_and_rebuild_keeps_original_identity(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    original = make_lesson(lesson_id="original", alias="Stable Alias", note=60)
    collision = make_lesson(lesson_id="collision", alias="Stable Alias", note=62)
    store.persist(original)
    binding_path = store.alias_binding_path("stable alias")
    binding_bytes = binding_path.read_bytes()
    store.catalog_path.write_text("{broken", encoding="utf-8")
    catalog_bytes = store.catalog_path.read_bytes()

    with pytest.raises(Exception) as caught:
        store.persist(collision)

    assert caught.value.code == "ALIAS_CONFLICT"
    assert caught.value.field == "aliases"
    assert binding_path.read_bytes() == binding_bytes
    assert store.catalog_path.read_bytes() == catalog_bytes
    assert not store.revision_path(collision.lesson_id, collision.content_id).exists()
    assert LessonStore(root=store.root).resolve("stable alias") == original


def test_alias_supersession_resolves_replacement_and_retains_immutable_base(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, replacement = persisted_supersession_pair(store)
    alias = base.aliases[0]
    binding_path = store.alias_binding_path(alias)
    base_path = store.revision_path(base.lesson_id, base.content_id)
    binding_bytes = binding_path.read_bytes()
    base_bytes = base_path.read_bytes()

    returned = store.supersede_alias(
        alias,
        expected=lesson_reference(base),
        replacement=lesson_reference(replacement),
    )

    assert returned == replacement
    assert store.resolve(alias) == replacement
    assert store.resolve(replacement.aliases[0]) == replacement
    assert binding_path.read_bytes() == binding_bytes
    assert base_path.read_bytes() == base_bytes
    assert store.load_revision(base.lesson_id, base.content_id) == base
    assert json.loads(store.alias_supersession_path(alias).read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "alias": alias,
        "expected": lesson_reference(base).to_dict(),
        "replacement": lesson_reference(replacement).to_dict(),
    }


def test_identical_alias_supersession_is_idempotent_and_returns_canonical_revision(
    tmp_path,
) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, replacement = persisted_supersession_pair(store)
    arguments = {
        "expected": lesson_reference(base),
        "replacement": lesson_reference(replacement),
    }
    first = store.supersede_alias(base.aliases[0], **arguments)
    path = store.alias_supersession_path(base.aliases[0])
    encoded = path.read_bytes()

    second = LessonStore(root=store.root).supersede_alias(base.aliases[0], **arguments)

    assert first == replacement
    assert second == store.load_revision(replacement.lesson_id, replacement.content_id)
    assert path.read_bytes() == encoded
    assert not tuple(path.parent.glob("*.tmp"))


def test_alias_supersession_rejects_self_reference_without_writing(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base = make_lesson(alias="Self Supersession")
    store.persist(base)
    reference = lesson_reference(base)

    with pytest.raises(Exception) as caught:
        store.supersede_alias(base.aliases[0], expected=reference, replacement=reference)

    assert caught.value.code == "ALIAS_CONFLICT"
    assert caught.value.field == "aliases"
    assert not store.alias_supersession_path(base.aliases[0]).exists()


def test_builtin_alias_cannot_be_superseded_or_shadowed(tmp_path) -> None:
    builtin = make_lesson(lesson_id="builtin-piece", alias="Packaged Lesson", note=60)
    replacement = make_lesson(
        lesson_id=builtin.lesson_id,
        alias="Local Imported Archive",
        note=62,
    )
    store = LessonStore(root=tmp_path / "coach", builtins=(builtin,))
    assert store.is_builtin_alias("  PACKAGED_lesson ")
    assert not store.is_builtin_alias(replacement.aliases[0])
    store.persist(replacement)
    path = store.alias_supersession_path(builtin.aliases[0])

    with pytest.raises(Exception) as caught:
        store.supersede_alias(
            builtin.aliases[0],
            expected=lesson_reference(builtin),
            replacement=lesson_reference(replacement),
        )

    assert caught.value.code == "ALIAS_CONFLICT"
    assert not path.exists()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "alias": builtin.aliases[0],
                "expected": lesson_reference(builtin).to_dict(),
                "replacement": lesson_reference(replacement).to_dict(),
            }
        ),
        encoding="utf-8",
    )
    assert LessonStore(root=store.root, builtins=(builtin,)).resolve(builtin.aliases[0]) == builtin


def test_wrong_expected_base_and_missing_target_fail_without_writing(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, replacement = persisted_supersession_pair(store)
    third = make_lesson(
        lesson_id=base.lesson_id,
        alias="Third Imported Archive",
        note=64,
    )
    store.persist(third)

    with pytest.raises(Exception) as wrong:
        store.supersede_alias(
            base.aliases[0],
            expected=lesson_reference(replacement),
            replacement=lesson_reference(third),
        )
    assert wrong.value.code == "ALIAS_CONFLICT"
    assert not store.alias_supersession_path(base.aliases[0]).exists()

    missing_alias = "Missing Local Target"
    with pytest.raises(Exception) as missing:
        store.supersede_alias(
            missing_alias,
            expected=lesson_reference(base),
            replacement=lesson_reference(replacement),
        )
    assert missing.value.code == "ALIAS_CONFLICT"
    assert not store.alias_supersession_path(missing_alias).exists()


def test_resolve_rejects_supersession_whose_expected_reference_is_not_the_binding(
    tmp_path,
) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, replacement = persisted_supersession_pair(store)
    path = store.alias_supersession_path(base.aliases[0])
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "alias": base.aliases[0],
                "expected": lesson_reference(replacement).to_dict(),
                "replacement": lesson_reference(base).to_dict(),
            }
        ),
        encoding="utf-8",
    )
    encoded = path.read_bytes()

    with pytest.raises(Exception) as caught:
        store.resolve(base.aliases[0])

    assert caught.value.code == "LESSON_CORRUPT"
    assert path.read_bytes() == encoded


def test_missing_or_corrupt_replacement_fails_preflight_without_writing(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, replacement = persisted_supersession_pair(store)
    path = store.alias_supersession_path(base.aliases[0])
    missing = LessonReference(base.lesson_id, "f" * 64)

    with pytest.raises(Exception) as missing_error:
        store.supersede_alias(
            base.aliases[0],
            expected=lesson_reference(base),
            replacement=missing,
        )
    assert missing_error.value.code == "LESSON_CORRUPT"
    assert not path.exists()

    replacement_path = store.revision_path(replacement.lesson_id, replacement.content_id)
    corrupt = b"{not-json"
    replacement_path.write_bytes(corrupt)
    with pytest.raises(Exception) as corrupt_error:
        store.supersede_alias(
            base.aliases[0],
            expected=lesson_reference(base),
            replacement=lesson_reference(replacement),
        )
    assert corrupt_error.value.code == "LESSON_CORRUPT"
    assert replacement_path.read_bytes() == corrupt
    assert not path.exists()


def test_missing_base_revision_fails_preflight_without_writing(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, replacement = persisted_supersession_pair(store)
    base_path = store.revision_path(base.lesson_id, base.content_id)
    base_path.unlink()

    with pytest.raises(Exception) as caught:
        store.supersede_alias(
            base.aliases[0],
            expected=lesson_reference(base),
            replacement=lesson_reference(replacement),
        )

    assert caught.value.code == "LESSON_CORRUPT"
    assert not store.alias_supersession_path(base.aliases[0]).exists()


def test_second_alias_replacement_conflicts_and_preserves_first_record(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, replacement = persisted_supersession_pair(store)
    second = make_lesson(
        lesson_id=base.lesson_id,
        alias="Second Imported Archive",
        note=64,
    )
    store.persist(second)
    store.supersede_alias(
        base.aliases[0],
        expected=lesson_reference(base),
        replacement=lesson_reference(replacement),
    )
    path = store.alias_supersession_path(base.aliases[0])
    first_record = path.read_bytes()

    with pytest.raises(Exception) as caught:
        store.supersede_alias(
            base.aliases[0],
            expected=lesson_reference(base),
            replacement=lesson_reference(second),
        )

    assert caught.value.code == "ALIAS_CONFLICT"
    assert path.read_bytes() == first_record
    assert store.resolve(base.aliases[0]) == replacement


@pytest.mark.parametrize(
    ("record_kind", "expected_code"),
    [("malformed", "LESSON_CORRUPT"), ("newer", "SCHEMA_UNSUPPORTED")],
)
def test_invalid_alias_supersession_is_untouched_and_fails_closed(
    tmp_path,
    record_kind,
    expected_code,
) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, replacement = persisted_supersession_pair(store)
    store.supersede_alias(
        base.aliases[0],
        expected=lesson_reference(base),
        replacement=lesson_reference(replacement),
    )
    path = store.alias_supersession_path(base.aliases[0])
    if record_kind == "malformed":
        invalid = b"{not-json"
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = 2
        invalid = (json.dumps(payload, sort_keys=True) + "\n").encode()
    path.write_bytes(invalid)

    for candidate in (store, LessonStore(root=store.root)):
        with pytest.raises(Exception) as resolved:
            candidate.resolve(base.aliases[0])
        assert resolved.value.code == expected_code
        with pytest.raises(Exception) as repeated:
            candidate.supersede_alias(
                base.aliases[0],
                expected=lesson_reference(base),
                replacement=lesson_reference(replacement),
            )
        assert repeated.value.code == expected_code
        assert path.read_bytes() == invalid


def test_resolve_rejects_supersession_with_missing_target_or_replacement(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, replacement = persisted_supersession_pair(store)
    missing_alias = "Unbound Supersession Target"
    missing_target_path = store.alias_supersession_path(missing_alias)
    missing_target_path.parent.mkdir(parents=True)
    missing_target_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "alias": "unbound supersession target",
                "expected": lesson_reference(base).to_dict(),
                "replacement": lesson_reference(replacement).to_dict(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception) as target_error:
        store.resolve(missing_alias)
    assert target_error.value.code == "LESSON_CORRUPT"

    missing_replacement = LessonReference(base.lesson_id, "e" * 64)
    replacement_path = store.alias_supersession_path(base.aliases[0])
    replacement_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "alias": base.aliases[0],
                "expected": lesson_reference(base).to_dict(),
                "replacement": missing_replacement.to_dict(),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(Exception) as replacement_error:
        store.resolve(base.aliases[0])
    assert replacement_error.value.code == "LESSON_CORRUPT"


def test_catalog_rebuild_preserves_alias_supersession_overlay(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, replacement = persisted_supersession_pair(store)
    store.supersede_alias(
        base.aliases[0],
        expected=lesson_reference(base),
        replacement=lesson_reference(replacement),
    )
    binding_path = store.alias_binding_path(base.aliases[0])
    supersession_path = store.alias_supersession_path(base.aliases[0])
    binding_bytes = binding_path.read_bytes()
    supersession_bytes = supersession_path.read_bytes()
    store.catalog_path.write_text("{broken", encoding="utf-8")

    rebuilt_catalog = store.rebuild_catalog()
    fresh = LessonStore(root=store.root)

    assert rebuilt_catalog["aliases"][base.aliases[0]] == lesson_reference(base).to_dict()
    assert fresh.resolve(base.aliases[0]) == replacement
    assert binding_path.read_bytes() == binding_bytes
    assert supersession_path.read_bytes() == supersession_bytes


def test_atomic_supersession_race_never_overwrites_competing_record(
    tmp_path,
    monkeypatch,
) -> None:
    store = LessonStore(root=tmp_path / "coach")
    base, requested = persisted_supersession_pair(store)
    competing = make_lesson(
        lesson_id=base.lesson_id,
        alias="Competing Imported Archive",
        note=64,
    )
    store.persist(competing)
    path = store.alias_supersession_path(base.aliases[0])
    real_create = coach_store._atomic_create_json

    def competing_create(candidate, _payload):
        assert candidate == path
        competing_payload = {
            "schema_version": 1,
            "alias": base.aliases[0],
            "expected": lesson_reference(base).to_dict(),
            "replacement": lesson_reference(competing).to_dict(),
        }
        assert real_create(candidate, competing_payload)
        return False

    monkeypatch.setattr(coach_store, "_atomic_create_json", competing_create)

    with pytest.raises(Exception) as caught:
        store.supersede_alias(
            base.aliases[0],
            expected=lesson_reference(base),
            replacement=lesson_reference(requested),
        )

    assert caught.value.code == "ALIAS_CONFLICT"
    assert (
        json.loads(path.read_text(encoding="utf-8"))["replacement"]
        == lesson_reference(competing).to_dict()
    )
    assert not real_create(
        path,
        {
            "schema_version": 1,
            "alias": base.aliases[0],
            "expected": lesson_reference(base).to_dict(),
            "replacement": lesson_reference(requested).to_dict(),
        },
    )
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))
    assert store.resolve(base.aliases[0]) == competing


def test_newer_catalog_schema_is_not_rebuilt_or_modified(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    store.root.mkdir(parents=True)
    newer = b'{"schema_version":2,"aliases":{}}\n'
    store.catalog_path.write_bytes(newer)

    with pytest.raises(Exception) as caught:
        store.resolve("anything")

    assert caught.value.code == "SCHEMA_UNSUPPORTED"
    assert store.catalog_path.read_bytes() == newer


def test_default_path_uses_xdg_data_and_env_override_outside_repo(tmp_path, monkeypatch) -> None:
    xdg = tmp_path / "xdg-data"
    override = tmp_path / "explicit-coach"
    monkeypatch.delenv("HALCYON_COACH_PATH", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    assert default_coach_path() == xdg / "project-halcyon" / "coach"

    monkeypatch.setenv("HALCYON_COACH_PATH", str(override))
    store = LessonStore()
    store.persist(make_lesson())

    assert store.root == override
    assert store.catalog_path.is_file()
    repository = Path(__file__).resolve().parents[1]
    assert not store.root.resolve().is_relative_to(repository)


def library_lesson(
    *,
    lesson_id: str,
    aliases: tuple[str, ...],
    title: str,
    segments: tuple[str, ...] = ("main melody",),
    imported: bool = True,
    note: int = 60,
    segment_hand: str | None = None,
    segment_difficulty: str | None = None,
):
    base = make_lesson(lesson_id=lesson_id, alias=aliases[0], note=note)
    return create_lesson_revision(
        lesson_id=lesson_id,
        created_at=NOW,
        aliases=aliases,
        title=title,
        composer="Test Composer",
        difficulty=None,
        hand=None,
        ppq=base.ppq,
        tempo_map=base.tempo_map,
        time_signatures=base.time_signatures,
        provenance=LessonProvenance(
            Acquisition.USER_SUPPLIED if imported else Acquisition.LOCALLY_AUTHORED_BOUNDED,
            "Private source /Users/private/song.mid sk-private-citation",
            Fidelity.VERIFIED if imported else Fidelity.APPROXIMATE,
            NOW,
            provider=None if imported else "synthetic",
            model_version=None if imported else "synthetic",
        ),
        events=base.events,
        segments=tuple(
            LessonSegment(label, 0, 480, hand=segment_hand, difficulty=segment_difficulty)
            for label in segments
        ),
    )


def library_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_library_lists_effective_imports_before_approximations_without_writing(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    old = library_lesson(
        lesson_id="avril",
        aliases=("Aphex Twin Avril 14th",),
        title="Avril approximation",
        imported=False,
    )
    imported = library_lesson(
        lesson_id="avril",
        aliases=("Aphex Twin Avril 14th imported archive", "Aphex Twin's Avril 14th"),
        title="Avril 14th",
        note=62,
    )
    satie = library_lesson(
        lesson_id="satie",
        aliases=("Satie Gymnopedie No 1", "Gymnopédie No. 1"),
        title="Gymnopédie No. 1",
        segments=("complete piece",),
    )
    generated = library_lesson(
        lesson_id="generated",
        aliases=("Avril 14",),
        title="A generated approximation",
        imported=False,
    )
    for lesson in (old, imported, satie, generated):
        store.persist(lesson)
    store.supersede_alias(
        old.aliases[0], expected=lesson_reference(old), replacement=lesson_reference(imported)
    )
    before = library_tree(store.root)

    entries = store.library()

    assert library_tree(store.root) == before
    assert [entry["title"] for entry in entries] == [
        "Avril 14th",
        "Gymnopédie No. 1",
        "A generated approximation",
    ]
    assert entries[0] == {
        "title": "Avril 14th",
        "composer": "Test Composer",
        "query": "aphex twin avril 14th",
        "aliases": sorted((old.aliases[0], *imported.aliases)),
        "acquisition": "user_supplied",
        "fidelity": "verified",
        "practice_levels": ["full"],
        "segments": ["main melody"],
        "segment_hands": {"main melody": None},
        "segment_difficulties": {"main melody": None},
    }
    assert entries[1]["segments"] == ["complete piece"]
    assert entries[2]["fidelity"] == "approximate"
    for entry, expected in zip(entries, (imported, satie, generated), strict=True):
        assert store.resolve(entry["query"]) == expected
        assert all(store.resolve(alias) == expected for alias in entry["aliases"])
    rendered = json.dumps(entries)
    assert "/Users/private" not in rendered
    assert "sk-private-citation" not in rendered
    assert "event" not in rendered
    assert "source_port" not in rendered
    assert "note" not in rendered


@pytest.mark.parametrize("catalog_state", ["missing", "corrupt", "stale"])
def test_library_discovers_saved_lessons_without_repairing_catalog(tmp_path, catalog_state) -> None:
    store = LessonStore(root=tmp_path / "coach")
    lesson = make_lesson()
    store.persist(lesson)
    if catalog_state == "missing":
        store.catalog_path.unlink()
    elif catalog_state == "corrupt":
        store.catalog_path.write_text("{broken", encoding="utf-8")
    else:
        store.catalog_path.write_text(
            '{"schema_version":1,"aliases":{},"excluded_aliases":{}}', encoding="utf-8"
        )
    before = library_tree(store.root)

    entries = LessonStore(root=store.root).library()

    assert entries[0]["query"] == lesson.aliases[0]
    assert library_tree(store.root) == before


def test_library_missing_root_does_not_create_it(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "absent")

    assert store.library() == []
    assert not store.root.exists()


def test_library_preserves_builtin_alias_precedence(tmp_path) -> None:
    local = make_lesson(alias="shared alias", note=61)
    builtin = make_lesson(lesson_id="builtin", alias="shared alias", note=62)
    LessonStore(root=tmp_path / "coach").persist(local)
    store = LessonStore(root=tmp_path / "coach", builtins=(builtin,))
    before = library_tree(store.root)

    entries = store.library()

    assert len(entries) == 1
    assert store.resolve(entries[0]["query"]) == builtin
    assert library_tree(store.root) == before


def test_library_skips_corrupt_entries_and_bad_supersessions_without_writes(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    bad = make_lesson(lesson_id="bad", alias="bad entry")
    good = make_lesson(lesson_id="good", alias="good entry")
    base, replacement = persisted_supersession_pair(store)
    store.persist(bad)
    store.persist(good)
    store.supersede_alias(
        base.aliases[0], expected=lesson_reference(base), replacement=lesson_reference(replacement)
    )
    store.revision_path(bad.lesson_id, bad.content_id).write_text("{broken", encoding="utf-8")
    store.alias_supersession_path(base.aliases[0]).write_text("{broken", encoding="utf-8")
    before = library_tree(store.root)

    entries = store.library()

    aliases = [alias for entry in entries for alias in entry["aliases"]]
    assert "bad entry" not in aliases
    assert base.aliases[0] not in aliases
    assert "good entry" in aliases
    assert replacement.aliases[0] in aliases
    assert library_tree(store.root) == before


def test_library_rejects_newer_catalog_without_writes(tmp_path) -> None:
    store = LessonStore(root=tmp_path / "coach")
    store.persist(make_lesson())
    store.catalog_path.write_text('{"schema_version":2}', encoding="utf-8")
    before = library_tree(store.root)

    with pytest.raises(CoachError) as caught:
        store.library()

    assert caught.value.code == "SCHEMA_UNSUPPORTED"
    assert library_tree(store.root) == before


def test_library_bounds_entries_aliases_segments_and_keeps_imports_first(tmp_path) -> None:
    lessons = tuple(
        library_lesson(
            lesson_id=f"piece-{index}",
            aliases=tuple(f"piece {index} alias {alias}" for alias in range(12)),
            title=f"Piece {index:02}",
            segments=tuple(f"section {part}" for part in range(20)),
            imported=index == 17,
        )
        for index in range(18)
    )
    store = LessonStore(root=tmp_path / "coach", builtins=lessons)

    entries = store.library()

    assert len(entries) == 16
    assert entries[0]["title"] == "Piece 17"
    assert all(len(entry["aliases"]) == 8 for entry in entries)
    assert all(len(entry["segments"]) == 16 for entry in entries)
    assert all(len(entry["segment_hands"]) == 16 for entry in entries)
    assert all(len(entry["segment_difficulties"]) == 16 for entry in entries)
    assert all(store.resolve(entry["query"]) is not None for entry in entries)
    assert not store.root.exists()


def test_library_reports_segment_selection_overrides_and_lesson_fallbacks(tmp_path) -> None:
    fallback = replace(
        library_lesson(lesson_id="fallback", aliases=("fallback",), title="Fallback"),
        hand="left",
        difficulty="standard",
    )
    override = replace(
        library_lesson(
            lesson_id="override",
            aliases=("override",),
            title="Override",
            segment_hand="right",
            segment_difficulty="foundation",
        ),
        hand="both",
        difficulty="challenge",
    )
    store = LessonStore(root=tmp_path / "coach", builtins=(fallback, override))

    first, second = store.library()

    assert first["segment_hands"] == {"main melody": "left"}
    assert first["segment_difficulties"] == {"main melody": "standard"}
    assert second["segment_hands"] == {"main melody": "right"}
    assert second["segment_difficulties"] == {"main melody": "foundation"}


def test_library_reports_practice_support_for_effective_source_without_writes(
    tmp_path, monkeypatch
) -> None:
    old = library_lesson(lesson_id="old", aliases=("saved song",), title="Old", note=60)
    imported = library_lesson(
        lesson_id="old", aliases=("imported song",), title="Imported", note=62
    )
    store = LessonStore(root=tmp_path / "coach")
    store.persist(old)
    store.persist(imported)
    store.supersede_alias(
        "saved song", expected=lesson_reference(old), replacement=lesson_reference(imported)
    )
    seen = []

    def supported(content_id):
        seen.append(content_id)
        return ("melody", "essentials", "full") if content_id == imported.content_id else ("full",)

    monkeypatch.setattr("halcyon.coach_store.practice_levels", supported)
    before = library_tree(store.root)

    entries = store.library()

    assert len(entries) == 1
    assert entries[0]["practice_levels"] == ["melody", "essentials", "full"]
    assert seen == [imported.content_id]
    assert library_tree(store.root) == before
    assert "content_id" not in entries[0]
