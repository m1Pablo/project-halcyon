"""Local-only Standard MIDI import for immutable coach lessons."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Literal, Sequence

from halcyon.coach_midi_import import MidiTrackSelection, import_midi_lesson
from halcyon.coach_models import (
    Acquisition,
    CoachError,
    Difficulty,
    Fidelity,
    Hand,
    LessonRevision,
    derive_lesson_id,
    normalize_lesson_alias,
)
from halcyon.coach_store import LessonReference, LessonStore

MAX_IMPORT_ALIASES = 16


@dataclass(frozen=True)
class LocalMidiImportResult:
    """Aggregate-only result for the local import command."""

    status: Literal["imported", "already_imported"]
    lesson: LessonRevision
    aliases: tuple[str, ...]
    superseded_aliases: tuple[str, ...]
    selected_track: MidiTrackSelection

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "lesson_id": self.lesson.lesson_id,
            "content_id": self.lesson.content_id,
            "aliases": list(self.aliases),
            "superseded_aliases": list(self.superseded_aliases),
            "segments": [segment.label for segment in self.lesson.segments],
            "selected_track": self.selected_track,
            "event_count": len(self.lesson.events),
            "note_count": sum(event.event_type == "note_on" for event in self.lesson.events),
            "duration_seconds": round(self.lesson.duration_us / 1_000_000, 3),
            "max_polyphony": self.lesson.max_polyphony,
            "acquisition": self.lesson.provenance.acquisition.value,
            "fidelity": self.lesson.provenance.fidelity.value,
        }


def import_midi_to_store(
    path: str | Path,
    *,
    track: MidiTrackSelection,
    title: str,
    composer: str,
    segment_label: str,
    hand: Hand | str,
    aliases: Sequence[str],
    replace_approximate: bool,
    store: LessonStore | None = None,
    difficulty: Difficulty | str | None = None,
    created_at: str | None = None,
) -> LocalMidiImportResult:
    """Import one explicit MIDI track and safely bind its local lesson aliases."""

    active_store = store or LessonStore()
    normalized_aliases = _normalized_aliases(aliases)
    if any(active_store.is_builtin_alias(alias) for alias in normalized_aliases):
        raise _conflict(
            "Packaged lesson aliases cannot be replaced by a local MIDI import.",
            "Use the packaged lesson or choose a distinct local alias.",
        )
    current = {alias: active_store.resolve(alias) for alias in normalized_aliases}
    existing = tuple(lesson for lesson in current.values() if lesson is not None)
    lesson_ids = {lesson.lesson_id for lesson in existing}
    if len(lesson_ids) > 1:
        raise _conflict(
            "The requested aliases identify different lesson identities.",
            "Choose aliases for one existing lesson and retry.",
        )
    lesson_id = next(iter(lesson_ids), derive_lesson_id(normalized_aliases[0]))

    replaceable: dict[str, LessonRevision] = {}
    imported: dict[str, LessonRevision] = {}
    for alias, lesson in current.items():
        if lesson is None:
            continue
        if (
            lesson.provenance.acquisition is Acquisition.USER_SUPPLIED
            and lesson.provenance.fidelity is Fidelity.VERIFIED
        ):
            imported[alias] = lesson
            continue
        if (
            lesson.provenance.acquisition is Acquisition.LOCALLY_AUTHORED_BOUNDED
            and lesson.provenance.fidelity is Fidelity.APPROXIMATE
        ):
            if not replace_approximate:
                raise CoachError(
                    "MIDI_IMPORT_REPLACEMENT_REQUIRED",
                    "An approximate generated lesson already uses this alias.",
                    field="aliases",
                    resolution="Retry with --replace-approximate to preserve and supersede it.",
                )
            replaceable[alias] = lesson
            continue
        raise _conflict(
            "A requested alias already identifies a non-replaceable lesson.",
            "Use a distinct alias; only generated approximate lessons may be superseded.",
        )

    timestamp = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    draft = import_midi_lesson(
        path,
        track=track,
        lesson_id=lesson_id,
        aliases=("local MIDI import draft",),
        title=title,
        composer=composer,
        segment_label=segment_label,
        hand=hand,
        created_at=timestamp,
        difficulty=difficulty,
    )

    imported_references = {
        LessonReference(lesson.lesson_id, lesson.content_id) for lesson in imported.values()
    }
    candidate_reference = LessonReference(draft.lesson_id, draft.content_id)
    if imported_references and imported_references != {candidate_reference}:
        raise _conflict(
            "These aliases already identify a different user-supplied MIDI lesson.",
            "Keep the existing import or use a distinct alias for the new MIDI file.",
        )

    replaceable_references = {
        LessonReference(lesson.lesson_id, lesson.content_id) for lesson in replaceable.values()
    }
    if len(replaceable_references) > 1:
        raise _conflict(
            "The approximate aliases do not share one immutable base revision.",
            "Choose aliases bound to one generated revision and retry.",
        )

    free_aliases = tuple(alias for alias, lesson in current.items() if lesson is None)
    archive_alias = normalize_lesson_alias(
        f"{normalized_aliases[0][:120]} user supplied {draft.content_id[:12]}"
    )
    persisted_aliases = tuple(dict.fromkeys((archive_alias, *free_aliases)))
    candidate = replace(draft, aliases=persisted_aliases)

    already_imported = bool(imported) and not replaceable and not free_aliases
    canonical = active_store.persist(candidate)
    replacement = LessonReference(canonical.lesson_id, canonical.content_id)
    superseded: list[str] = []
    for alias, base in replaceable.items():
        active_store.supersede_alias(
            alias,
            expected=LessonReference(base.lesson_id, base.content_id),
            replacement=replacement,
        )
        superseded.append(alias)

    for alias in normalized_aliases:
        resolved = active_store.resolve(alias)
        if (
            resolved is None
            or LessonReference(resolved.lesson_id, resolved.content_id) != replacement
        ):
            raise CoachError(
                "LESSON_CORRUPT",
                "The imported lesson alias did not resolve to its persisted revision.",
                field="aliases",
                resolution="Leave the immutable records untouched and retry the local import.",
            )
        if (
            resolved.provenance.acquisition is not Acquisition.USER_SUPPLIED
            or resolved.provenance.fidelity is not Fidelity.VERIFIED
        ):
            raise CoachError(
                "LESSON_CORRUPT",
                "The imported lesson lost its verified user-supplied provenance.",
                field="provenance",
                resolution="Leave the immutable records untouched and restore the imported revision.",
            )

    return LocalMidiImportResult(
        status="already_imported" if already_imported else "imported",
        lesson=canonical,
        aliases=normalized_aliases,
        superseded_aliases=tuple(superseded),
        selected_track=track,
    )


def _normalized_aliases(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not values or len(values) > MAX_IMPORT_ALIASES:
        raise CoachError(
            "MIDI_IMPORT_INVALID",
            f"Provide from 1 through {MAX_IMPORT_ALIASES} lesson aliases.",
            field="aliases",
            resolution="Choose bounded descriptive aliases and retry.",
        )
    try:
        normalized = tuple(dict.fromkeys(normalize_lesson_alias(value) for value in values))
    except CoachError:
        raise CoachError(
            "MIDI_IMPORT_INVALID",
            "A lesson alias is invalid.",
            field="aliases",
            resolution="Choose bounded descriptive aliases and retry.",
        ) from None
    if not normalized:
        raise CoachError(
            "MIDI_IMPORT_INVALID",
            "At least one distinct lesson alias is required.",
            field="aliases",
            resolution="Choose a descriptive alias and retry.",
        )
    return normalized


def _conflict(message: str, resolution: str) -> CoachError:
    return CoachError(
        "ALIAS_CONFLICT",
        message,
        field="aliases",
        resolution=resolution,
    )


def _track_selection(value: str) -> MidiTrackSelection:
    if value.casefold() == "all":
        return "all"
    try:
        track = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("track must be a zero-based index or 'all'") from exc
    if track < 0:
        raise argparse.ArgumentTypeError("track must be a zero-based index or 'all'")
    return track


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import one bounded Standard MIDI file into Halcyon's local coach store."
    )
    parser.add_argument("midi_file")
    parser.add_argument("--track", required=True, type=_track_selection)
    parser.add_argument("--title", required=True)
    parser.add_argument("--composer", required=True)
    parser.add_argument("--segment", required=True)
    parser.add_argument("--hand", required=True, choices=tuple(item.value for item in Hand))
    parser.add_argument("--difficulty", choices=tuple(item.value for item in Difficulty))
    parser.add_argument("--alias", required=True, action="append")
    parser.add_argument(
        "--replace-approximate",
        action="store_true",
        help="Preserve and supersede an existing locally generated approximate lesson.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = import_midi_to_store(
            args.midi_file,
            track=args.track,
            title=args.title,
            composer=args.composer,
            segment_label=args.segment,
            hand=args.hand,
            difficulty=args.difficulty,
            aliases=args.alias,
            replace_approximate=args.replace_approximate,
        )
    except CoachError as exc:
        print(
            json.dumps(
                {"status": "error", "error": exc.to_public_dict()},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "code": "MIDI_IMPORT_FAILED",
                        "message": "The local MIDI import did not complete.",
                        "retryable": False,
                    },
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result.to_public_dict(), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
