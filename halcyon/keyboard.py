"""LPK25 keyboard geometry and rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

from halcyon.midi import note_name

LPK25_START_NOTE = 48
LPK25_KEY_COUNT = 25
WHITE_KEY_WIDTH = 7
BLACK_KEY_WIDTH = 5
BLACK_NOTE_CLASSES = frozenset({1, 3, 6, 8, 10})
WHITE_NOTE_CLASSES = frozenset({0, 2, 4, 5, 7, 9, 11})

PANEL_STYLE = "on #171b21"
BLACK_IDLE_STYLE = "white on #16181d"
WHITE_IDLE_STYLE = "black on #f4f1de"
BLACK_PRESSED_STYLE = "bold black on #f0b429"
WHITE_PRESSED_STYLE = "bold black on #95d5b2"
BLACK_TARGET_STYLE = "bold white on #2d6cdf"
WHITE_TARGET_STYLE = "bold black on #9ad0f5"
COMBINED_STATE_STYLE = "bold black on #f97316"


@dataclass(frozen=True)
class KeyboardKey:
    """A single physical key in the LPK25 visual model."""

    note: int
    name: str
    is_black: bool
    left: int
    width: int


def is_black_note(note: int) -> bool:
    """Return whether the MIDI note maps to a black piano key."""

    return note % 12 in BLACK_NOTE_CLASSES


def keyboard_notes(
    start_note: int = LPK25_START_NOTE, key_count: int = LPK25_KEY_COUNT
) -> tuple[int, ...]:
    """Return the fixed 25-note LPK25 range."""

    return tuple(range(start_note, start_note + key_count))


def lpk25_keys(
    start_note: int = LPK25_START_NOTE, key_count: int = LPK25_KEY_COUNT
) -> tuple[KeyboardKey, ...]:
    """Return physical key positions for the fixed-width LPK25 replica."""

    keys: list[KeyboardKey] = []
    white_count = 0
    for note in keyboard_notes(start_note, key_count):
        if is_black_note(note):
            left = max(0, white_count * WHITE_KEY_WIDTH - (BLACK_KEY_WIDTH // 2))
            keys.append(
                KeyboardKey(
                    note=note,
                    name=note_name(note),
                    is_black=True,
                    left=left,
                    width=BLACK_KEY_WIDTH,
                )
            )
            continue

        keys.append(
            KeyboardKey(
                note=note,
                name=note_name(note),
                is_black=False,
                left=white_count * WHITE_KEY_WIDTH,
                width=WHITE_KEY_WIDTH,
            )
        )
        white_count += 1

    return tuple(keys)


def keyboard_width(keys: tuple[KeyboardKey, ...] | None = None) -> int:
    """Return the fixed render width for a key set."""

    key_set = keys if keys is not None else lpk25_keys()
    white_keys = sum(1 for key in key_set if not key.is_black)
    return white_keys * WHITE_KEY_WIDTH


def key_style(
    note: int,
    pressed_notes: set[int] | frozenset[int],
    target_notes: set[int] | frozenset[int] = frozenset(),
) -> str:
    """Return the render style for a note state."""

    is_black = is_black_note(note)
    is_pressed = note in pressed_notes
    is_target = note in target_notes

    if is_pressed and is_target:
        return COMBINED_STATE_STYLE
    if is_pressed:
        return BLACK_PRESSED_STYLE if is_black else WHITE_PRESSED_STYLE
    if is_target:
        return BLACK_TARGET_STYLE if is_black else WHITE_TARGET_STYLE
    return BLACK_IDLE_STYLE if is_black else WHITE_IDLE_STYLE


def _styled_row(
    cells: list[tuple[str, str]],
    *,
    width: int,
) -> Text:
    row = Text()
    for char, style in cells[:width]:
        row.append(char, style=style)
    return row


def _place_label(
    cells: list[tuple[str, str]],
    *,
    left: int,
    width: int,
    label: str,
    style: str,
) -> None:
    for offset, char in enumerate(label.center(width)[:width]):
        index = left + offset
        if 0 <= index < len(cells):
            cells[index] = (char, style)


def keyboard_text(
    pressed_notes: set[int] | frozenset[int],
    target_notes: set[int] | frozenset[int] = frozenset(),
    *,
    keys: tuple[KeyboardKey, ...] | None = None,
) -> Text:
    """Render a fixed-width LPK25 keyboard as rich text."""

    key_set = keys if keys is not None else lpk25_keys()
    width = keyboard_width(key_set)
    black_row = [(" ", PANEL_STYLE) for _ in range(width)]
    white_body_row = [(" ", PANEL_STYLE) for _ in range(width)]
    white_label_row = [(" ", PANEL_STYLE) for _ in range(width)]

    for key in key_set:
        style = key_style(key.note, pressed_notes, target_notes)
        if key.is_black:
            _place_label(
                black_row,
                left=key.left,
                width=key.width,
                label=key.name,
                style=style,
            )
            continue

        for row in (white_body_row, white_label_row):
            _place_label(
                row,
                left=key.left,
                width=key.width,
                label="",
                style=style,
            )
        _place_label(
            white_label_row,
            left=key.left,
            width=key.width,
            label=key.name,
            style=style,
        )

    text = Text()
    text.append(_styled_row(black_row, width=width))
    text.append("\n")
    text.append(_styled_row(white_body_row, width=width))
    text.append("\n")
    text.append(_styled_row(white_label_row, width=width))
    return text
