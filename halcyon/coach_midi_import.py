"""Bounded, local Standard MIDI File inspection and lesson import."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, Literal, Sequence

import mido

from halcyon.coach_models import (
    Acquisition,
    CoachError,
    Difficulty,
    Fidelity,
    Hand,
    LessonEvent,
    LessonProvenance,
    LessonRevision,
    LessonSegment,
    LessonValidationError,
    STORED_LESSON_LIMITS,
    TempoChange,
    TimeSignature,
    create_lesson_revision,
)

MAX_MIDI_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_SAFE_FILENAME_CHARS = 120
_DEFAULT_TEMPO = 500_000
_DEFAULT_TIME_SIGNATURE = (4, 4)

MidiTrackSelection = int | Literal["all"]


@dataclass(frozen=True)
class MidiTrackSummary:
    """Aggregate track facts suitable for a local track-selection UI."""

    index: int
    name: str | None
    message_count: int
    note_event_count: int
    note_on_count: int
    note_off_count: int
    channels: tuple[int, ...]
    minimum_note: int | None
    maximum_note: int | None
    first_note_tick: int | None
    last_note_tick: int | None
    duration_ticks: int
    max_polyphony: int
    unmatched_note_off_count: int
    hanging_note_count: int


@dataclass(frozen=True)
class MidiFileSummary:
    """Privacy-bounded MIDI facts; this deliberately exposes no note sequence."""

    filename: str
    sha256: str
    midi_type: int
    ppq: int
    track_count: int
    note_event_count: int
    tempo_change_count: int
    time_signature_change_count: int
    duration_ticks: int
    tracks: tuple[MidiTrackSummary, ...]


@dataclass(frozen=True)
class _NoteRecord:
    absolute_tick: int
    track_index: int
    message_index: int
    event_type: Literal["note_on", "note_off"]
    note: int
    velocity: int
    channel: int
    status: int


@dataclass(frozen=True)
class _MetadataRecord:
    absolute_tick: int
    track_index: int
    message_index: int
    values: tuple[int, ...]


@dataclass(frozen=True)
class _ParsedMidi:
    filename: str
    sha256: str
    midi_type: int
    ppq: int
    track_notes: tuple[tuple[_NoteRecord, ...], ...]
    track_summaries: tuple[MidiTrackSummary, ...]
    tempos: tuple[_MetadataRecord, ...]
    time_signatures: tuple[_MetadataRecord, ...]


def inspect_midi_file(
    path: str | Path,
    *,
    max_source_bytes: int = MAX_MIDI_SOURCE_BYTES,
) -> MidiFileSummary:
    """Inspect a local format-0/1 MIDI file without returning musical content."""

    parsed = _parse_midi_file(path, max_source_bytes=max_source_bytes)
    return MidiFileSummary(
        filename=parsed.filename,
        sha256=parsed.sha256,
        midi_type=parsed.midi_type,
        ppq=parsed.ppq,
        track_count=len(parsed.track_summaries),
        note_event_count=sum(track.note_event_count for track in parsed.track_summaries),
        tempo_change_count=len(parsed.tempos),
        time_signature_change_count=len(parsed.time_signatures),
        duration_ticks=max((track.duration_ticks for track in parsed.track_summaries), default=0),
        tracks=parsed.track_summaries,
    )


def import_midi_lesson(
    path: str | Path,
    *,
    track: MidiTrackSelection,
    lesson_id: str,
    aliases: Sequence[str],
    title: str,
    composer: str,
    segment_label: str,
    hand: Hand | str,
    created_at: str,
    difficulty: Difficulty | str | None = None,
    max_source_bytes: int = MAX_MIDI_SOURCE_BYTES,
) -> LessonRevision:
    """Convert one explicit track selection into an immutable verified lesson."""

    parsed = _parse_midi_file(path, max_source_bytes=max_source_bytes)
    try:
        return _build_lesson_revision(
            parsed,
            track=track,
            lesson_id=lesson_id,
            aliases=aliases,
            title=title,
            composer=composer,
            segment_label=segment_label,
            hand=hand,
            created_at=created_at,
            difficulty=difficulty,
        )
    except LessonValidationError as exc:
        raise _import_error(str(exc), field=exc.field or "lesson") from None


def _build_lesson_revision(
    parsed: _ParsedMidi,
    *,
    track: MidiTrackSelection,
    lesson_id: str,
    aliases: Sequence[str],
    title: str,
    composer: str,
    segment_label: str,
    hand: Hand | str,
    created_at: str,
    difficulty: Difficulty | str | None,
) -> LessonRevision:
    selected = _select_notes(parsed, track)
    if not selected:
        raise LessonValidationError(
            "selected MIDI track contains no note events",
            field="events",
            code="MIDI_IMPORT_INVALID",
        )

    peak, unmatched, hanging = _note_balance(selected)
    if unmatched:
        raise LessonValidationError(
            "selected MIDI track contains note_off events without matching note_on events",
            field="events",
            code="MIDI_IMPORT_INVALID",
        )
    if hanging:
        raise LessonValidationError(
            "selected MIDI track contains hanging notes",
            field="events",
            code="MIDI_IMPORT_INVALID",
        )

    duration_tick = selected[-1].absolute_tick
    if duration_tick <= 0:
        raise LessonValidationError(
            "selected MIDI track must have positive duration",
            field="events.absolute_tick",
            code="MIDI_IMPORT_INVALID",
        )

    source_port = f"halcyon://coach/user-supplied-midi/{parsed.sha256[:24]}"
    events: list[LessonEvent] = []
    previous_tick = 0
    for record in selected:
        events.append(
            LessonEvent(
                event_id="",
                event_type=record.event_type,
                note=record.note,
                velocity=record.velocity,
                channel=record.channel,
                status=record.status,
                source_port=source_port,
                content_origin="user_supplied_midi",
                absolute_tick=record.absolute_tick,
                delta_tick=record.absolute_tick - previous_tick,
            )
        )
        previous_tick = record.absolute_tick

    tempo_map = _tempo_map(parsed.tempos, end_tick=duration_tick)
    time_signatures = _time_signature_map(parsed.time_signatures, end_tick=duration_tick)
    provenance = LessonProvenance(
        acquisition=Acquisition.USER_SUPPLIED,
        citation=f"user-supplied MIDI; filename={parsed.filename}; sha256={parsed.sha256}",
        fidelity=Fidelity.VERIFIED,
        authorization_timestamp=created_at,
        provider=None,
        model_version=None,
    )
    segment = LessonSegment(
        label=segment_label,
        start_tick=0,
        end_tick=duration_tick,
        hand=hand,
        difficulty=difficulty,
    )
    return create_lesson_revision(
        lesson_id=lesson_id,
        created_at=created_at,
        aliases=aliases,
        title=title,
        composer=composer,
        difficulty=difficulty,
        hand=hand,
        ppq=parsed.ppq,
        tempo_map=tempo_map,
        time_signatures=time_signatures,
        provenance=provenance,
        events=events,
        segments=(segment,),
        max_polyphony=peak,
        limits=STORED_LESSON_LIMITS,
    )


def _parse_midi_file(path: str | Path, *, max_source_bytes: int) -> _ParsedMidi:
    _validate_source_limit(max_source_bytes)
    try:
        source = Path(path).expanduser()
    except (TypeError, ValueError, OSError) as exc:
        raise _import_error("MIDI source path is invalid", field="path") from exc
    if source.suffix.lower() not in {".mid", ".midi"}:
        raise _import_error("MIDI source must use a .mid or .midi extension", field="path")
    descriptor = -1
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise _import_error("MIDI source must be a regular file", field="path")
        if source_stat.st_size == 0:
            raise _import_error("MIDI source is empty", field="path")
        if source_stat.st_size > max_source_bytes:
            raise _import_error(
                f"MIDI source may not exceed {max_source_bytes} bytes",
                field="path",
                code="MIDI_IMPORT_TOO_LARGE",
            )
        with os.fdopen(descriptor, "rb") as source_file:
            descriptor = -1
            # Bound the allocation even if the open file grows after fstat().
            payload = source_file.read(max_source_bytes + 1)
    except CoachError:
        raise
    except (OSError, ValueError) as exc:
        raise _import_error("MIDI source is unavailable", field="path") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if len(payload) > max_source_bytes:
        raise _import_error(
            f"MIDI source may not exceed {max_source_bytes} bytes",
            field="path",
            code="MIDI_IMPORT_TOO_LARGE",
        )
    try:
        midi = mido.MidiFile(file=BytesIO(payload), clip=False)
    except Exception as exc:
        raise _import_error("MIDI source is malformed or unsupported", field="path") from exc
    if midi.type not in {0, 1}:
        raise _import_error(
            "MIDI type 2 is not supported; use a format-0 or format-1 file",
            field="path",
            code="MIDI_IMPORT_UNSUPPORTED",
        )
    if (
        isinstance(midi.ticks_per_beat, bool)
        or not isinstance(midi.ticks_per_beat, int)
        or not 1 <= midi.ticks_per_beat <= 9_600
    ):
        raise _import_error(
            "MIDI source must use PPQ timing from 1 through 9600",
            field="ppq",
            code="MIDI_IMPORT_UNSUPPORTED",
        )

    track_notes: list[tuple[_NoteRecord, ...]] = []
    track_summaries: list[MidiTrackSummary] = []
    tempos: list[_MetadataRecord] = []
    signatures: list[_MetadataRecord] = []
    for track_index, midi_track in enumerate(midi.tracks):
        absolute_tick = 0
        notes: list[_NoteRecord] = []
        track_name: str | None = None
        for message_index, message in enumerate(midi_track):
            delta = message.time
            if isinstance(delta, bool) or not isinstance(delta, int) or delta < 0:
                raise _import_error("MIDI source contains invalid delta timing", field="events")
            absolute_tick += delta
            if message.type == "track_name":
                track_name = _safe_track_name(message.name)
            elif message.type == "set_tempo":
                tempos.append(
                    _MetadataRecord(
                        absolute_tick,
                        track_index,
                        message_index,
                        (message.tempo,),
                    )
                )
            elif message.type == "time_signature":
                signatures.append(
                    _MetadataRecord(
                        absolute_tick,
                        track_index,
                        message_index,
                        (message.numerator, message.denominator),
                    )
                )
            elif message.type in {"note_on", "note_off"}:
                channel = message.channel + 1
                zero_velocity_on = message.type == "note_on" and message.velocity == 0
                event_type: Literal["note_on", "note_off"] = (
                    "note_off" if message.type == "note_off" or zero_velocity_on else "note_on"
                )
                command = 0x90 if message.type == "note_on" else 0x80
                notes.append(
                    _NoteRecord(
                        absolute_tick=absolute_tick,
                        track_index=track_index,
                        message_index=message_index,
                        event_type=event_type,
                        note=message.note,
                        velocity=message.velocity,
                        channel=channel,
                        status=command | (channel - 1),
                    )
                )
        note_tuple = tuple(notes)
        track_notes.append(note_tuple)
        peak, unmatched, hanging = _note_balance(note_tuple)
        note_values = [note.note for note in note_tuple]
        on_count = sum(note.event_type == "note_on" for note in note_tuple)
        track_summaries.append(
            MidiTrackSummary(
                index=track_index,
                name=track_name,
                message_count=len(midi_track),
                note_event_count=len(note_tuple),
                note_on_count=on_count,
                note_off_count=len(note_tuple) - on_count,
                channels=tuple(sorted({note.channel for note in note_tuple})),
                minimum_note=min(note_values) if note_values else None,
                maximum_note=max(note_values) if note_values else None,
                first_note_tick=note_tuple[0].absolute_tick if note_tuple else None,
                last_note_tick=note_tuple[-1].absolute_tick if note_tuple else None,
                duration_ticks=absolute_tick,
                max_polyphony=peak,
                unmatched_note_off_count=unmatched,
                hanging_note_count=hanging,
            )
        )

    return _ParsedMidi(
        filename=_safe_filename(source.name),
        sha256=hashlib.sha256(payload).hexdigest(),
        midi_type=midi.type,
        ppq=midi.ticks_per_beat,
        track_notes=tuple(track_notes),
        track_summaries=tuple(track_summaries),
        tempos=tuple(tempos),
        time_signatures=tuple(signatures),
    )


def _select_notes(parsed: _ParsedMidi, track: MidiTrackSelection) -> tuple[_NoteRecord, ...]:
    if track == "all":
        notes = [note for track_notes in parsed.track_notes for note in track_notes]
    elif isinstance(track, bool) or not isinstance(track, int):
        raise _import_error("track must be an integer index or 'all'", field="track")
    elif not 0 <= track < len(parsed.track_notes):
        raise _import_error("track index is outside the MIDI track range", field="track")
    else:
        notes = list(parsed.track_notes[track])
    return tuple(
        sorted(notes, key=lambda note: (note.absolute_tick, note.track_index, note.message_index))
    )


def _note_balance(notes: Sequence[_NoteRecord]) -> tuple[int, int, int]:
    active: defaultdict[tuple[int, int], int] = defaultdict(int)
    active_count = 0
    peak = 0
    unmatched = 0
    for note in notes:
        key = (note.channel, note.note)
        if note.event_type == "note_on":
            active[key] += 1
            active_count += 1
            peak = max(peak, active_count)
        elif active[key]:
            active[key] -= 1
            active_count -= 1
        else:
            unmatched += 1
    return peak, unmatched, active_count


def _tempo_map(records: Sequence[_MetadataRecord], *, end_tick: int) -> tuple[TempoChange, ...]:
    collapsed: dict[int, int] = {}
    for record in sorted(
        records,
        key=lambda item: (item.absolute_tick, item.track_index, item.message_index),
    ):
        if record.absolute_tick <= end_tick:
            collapsed[record.absolute_tick] = record.values[0]
    collapsed.setdefault(0, _DEFAULT_TEMPO)
    return tuple(TempoChange(tick, collapsed[tick]) for tick in sorted(collapsed))


def _time_signature_map(
    records: Sequence[_MetadataRecord], *, end_tick: int
) -> tuple[TimeSignature, ...]:
    collapsed: dict[int, tuple[int, int]] = {}
    for record in sorted(
        records,
        key=lambda item: (item.absolute_tick, item.track_index, item.message_index),
    ):
        if record.absolute_tick <= end_tick:
            collapsed[record.absolute_tick] = (record.values[0], record.values[1])
    collapsed.setdefault(0, _DEFAULT_TIME_SIGNATURE)
    return tuple(
        TimeSignature(tick, collapsed[tick][0], collapsed[tick][1]) for tick in sorted(collapsed)
    )


def _validate_source_limit(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_MIDI_SOURCE_BYTES
    ):
        raise _import_error(
            f"max_source_bytes must be from 1 through {MAX_MIDI_SOURCE_BYTES}",
            field="max_source_bytes",
        )


def _safe_filename(value: str) -> str:
    basename = Path(value).name
    sanitized = re.sub(r"[^A-Za-z0-9._ -]+", "_", basename)
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
    if not sanitized:
        sanitized = "user-supplied.mid"
    if len(sanitized) > _MAX_SAFE_FILENAME_CHARS:
        suffix = Path(sanitized).suffix[:10]
        stem_limit = _MAX_SAFE_FILENAME_CHARS - len(suffix)
        sanitized = f"{sanitized[:stem_limit].rstrip(' .')}{suffix}"
    return sanitized


def _safe_track_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    sanitized = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized[:80] or None


def _import_error(
    message: str,
    *,
    field: str,
    code: str = "MIDI_IMPORT_INVALID",
) -> CoachError:
    return CoachError(
        code,
        message,
        field=field,
        resolution="Choose a bounded format-0 or format-1 Standard MIDI file and retry.",
    )
