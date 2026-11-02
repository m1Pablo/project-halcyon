"""Reviewed, source-bound practice layers without rewriting saved MIDI lessons."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from halcyon.coach_models import LessonEvent, LessonRevision


class PracticeLevel(StrEnum):
    MELODY = "melody"
    ESSENTIALS = "essentials"
    FULL = "full"


@dataclass(frozen=True)
class _PracticeAnnotation:
    note_count: int
    melody: frozenset[int]
    essentials: frozenset[int]

    def __post_init__(self) -> None:
        if not self.melody <= self.essentials <= frozenset(range(self.note_count)):
            raise ValueError("practice annotations must be nested source note-on ordinals")


# These are zero-based NOTE_ON ordinals in the exact imported content, not pitches
# or all-event indices. They were reviewed against the source's voice/beat groups.
# This is a practice arrangement, not a claim of authoritative score transcription.
# Cadence octave/chord doublings remain ghosts around the top melodic voice.
_SATIE_MELODY = frozenset(
    (
        17,
        21,
        22,
        24,
        28,
        29,
        31,
        35,
        36,
        41,
        45,
        49,
        53,
        57,
        59,
        63,
        64,
        66,
        70,
        71,
        73,
        77,
        78,
        83,
        88,
        93,
        105,
        107,
        111,
        112,
        114,
        118,
        119,
        121,
        126,
        127,
        138,
        139,
        141,
        145,
        146,
        148,
        152,
        153,
        155,
        160,
        161,
        172,
        173,
        178,
        183,
        185,
        189,
        190,
        192,
        196,
        197,
        199,
        204,
        205,
        217,
        223,
        244,
        248,
        249,
        251,
        255,
        256,
        258,
        262,
        263,
        268,
        272,
        276,
        280,
        284,
        286,
        290,
        291,
        293,
        297,
        298,
        300,
        304,
        305,
        310,
        315,
        320,
        332,
        334,
        338,
        339,
        341,
        345,
        346,
        348,
        353,
        354,
        365,
        366,
        368,
        372,
        373,
        375,
        379,
        380,
        382,
        387,
        388,
        399,
        400,
        405,
        410,
        412,
        416,
        417,
        419,
        423,
        424,
        426,
        430,
        431,
        443,
        449,
    )
)
_SATIE_ANCHORS = frozenset(
    (
        0,
        1,
        4,
        5,
        8,
        9,
        12,
        13,
        16,
        18,
        23,
        25,
        30,
        32,
        37,
        38,
        42,
        43,
        46,
        47,
        50,
        51,
        54,
        55,
        58,
        60,
        65,
        67,
        72,
        74,
        79,
        80,
        84,
        85,
        89,
        90,
        94,
        95,
        97,
        98,
        101,
        102,
        106,
        108,
        113,
        115,
        120,
        122,
        128,
        129,
        133,
        134,
        140,
        142,
        147,
        149,
        154,
        156,
        162,
        163,
        167,
        168,
        174,
        175,
        179,
        180,
        184,
        186,
        191,
        193,
        198,
        200,
        206,
        207,
        210,
        218,
        224,
        227,
        228,
        231,
        232,
        235,
        236,
        239,
        240,
        243,
        245,
        250,
        252,
        257,
        259,
        264,
        265,
        269,
        270,
        273,
        274,
        277,
        278,
        281,
        282,
        285,
        287,
        292,
        294,
        299,
        301,
        306,
        307,
        311,
        312,
        316,
        317,
        321,
        322,
        324,
        325,
        328,
        329,
        333,
        335,
        340,
        342,
        347,
        349,
        355,
        356,
        360,
        361,
        367,
        369,
        374,
        376,
        381,
        383,
        389,
        390,
        394,
        395,
        401,
        402,
        406,
        407,
        411,
        413,
        418,
        420,
        425,
        427,
        432,
        434,
        438,
        444,
        450,
    )
)
_PRACTICE_ANNOTATIONS = {
    # Avril's imported source is already melody-only, including its dyads. Keeping
    # all notes is intentional: no fabricated accompaniment or ornament labels.
    "c1d235e0e88f367bbc38470aa1f840ccf2edfb7f4db01b1006ba088db1034263": _PracticeAnnotation(
        199, frozenset(range(199)), frozenset(range(199))
    ),
    "f9d6b81c3d811080d3522df9f0253e5d0badf95ab9dd48200f7bf603f6459331": _PracticeAnnotation(
        453, _SATIE_MELODY, _SATIE_MELODY | _SATIE_ANCHORS
    ),
}


def practice_levels(content_id: str) -> tuple[str, ...]:
    """Return available layers for this exact content, never a fuzzy title match."""

    if content_id in _PRACTICE_ANNOTATIONS:
        return tuple(level.value for level in PracticeLevel)
    return (PracticeLevel.FULL.value,)


def practice_required_event_ids(
    lesson: LessonRevision, level: PracticeLevel | str
) -> frozenset[str]:
    """Select original paired IDs using one immutable, source-specific annotation."""

    from halcyon.coach_models import CoachError

    level = PracticeLevel(level)
    if level is PracticeLevel.FULL:
        return frozenset(event.event_id for event in lesson.events)
    annotation = _PRACTICE_ANNOTATIONS.get(lesson.content_id)
    if annotation is None:
        raise CoachError(
            "PRACTICE_UNAVAILABLE",
            "This saved source has no reviewed reduced practice arrangement.",
            field="practice",
            resolution="Retry with --practice full; changing tempo does not need an arrangement.",
        )
    selected = annotation.melody if level is PracticeLevel.MELODY else annotation.essentials
    active: dict[tuple[int, int], deque[tuple[LessonEvent, bool]]] = defaultdict(deque)
    required: set[str] = set()
    ordinal = 0
    for event in lesson.events:
        key = (event.channel, event.note)
        if event.event_type == "note_on":
            included = ordinal in selected
            active[key].append((event, included))
            ordinal += 1
            if included:
                required.add(event.event_id)
        else:
            if not active[key]:
                raise CoachError("LESSON_CORRUPT", "Practice source note pairs are unbalanced.")
            _onset, included = active[key].popleft()
            if included:
                required.add(event.event_id)
    if ordinal != annotation.note_count or any(active.values()):
        raise CoachError("LESSON_CORRUPT", "Practice annotation does not match the saved source.")
    return frozenset(required)
