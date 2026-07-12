"""MIDI keyboard geometry and rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

from halcyon.midi import note_name

MIDI_START_NOTE = 0
MIDI_END_NOTE = 127
MIDI_KEY_COUNT = MIDI_END_NOTE - MIDI_START_NOTE + 1
LPK25_START_NOTE = 48
LPK25_KEY_COUNT = 25
WHITE_KEY_WIDTH = 7
BLACK_KEY_WIDTH = 5
VISIBLE_WHITE_KEY_COUNT = 30
VISIBLE_KEYBOARD_WIDTH = VISIBLE_WHITE_KEY_COUNT * WHITE_KEY_WIDTH + 2
BLACK_NOTE_CLASSES = frozenset({1, 3, 6, 8, 10})
WHITE_NOTE_CLASSES = frozenset({0, 2, 4, 5, 7, 9, 11})
KEYBOARD_HEIGHT = 11

PANEL_STYLE = "on #11151d"
FRAME_STYLE = "bold #596275 on #11151d"
BLACK_IDLE_STYLE = "#fcfcfa on #090b10"
WHITE_IDLE_STYLE = "#090b10 on #fcfcfa"
BLACK_PRESSED_STYLE = "bold #090b10 on #ff9f43"
WHITE_PRESSED_STYLE = "bold #090b10 on #ff9f43"
BLACK_TARGET_STYLE = "bold #090b10 on #78dce8"
WHITE_TARGET_STYLE = "bold #090b10 on #78dce8"
COMBINED_STATE_STYLE = "bold #090b10 on #ffd866"


@dataclass(frozen=True)
class KeyboardKey:
    """A single key in the full MIDI visual model."""

    note: int
    name: str
    is_black: bool
    left: int
    width: int


def is_black_note(note: int) -> bool:
    """Return whether the MIDI note maps to a black piano key."""

    return note % 12 in BLACK_NOTE_CLASSES


def keyboard_notes(
    start_note: int = MIDI_START_NOTE, key_count: int = MIDI_KEY_COUNT
) -> tuple[int, ...]:
    """Return the full MIDI note range by default."""

    return tuple(range(start_note, start_note + key_count))


def keyboard_keys(
    start_note: int = MIDI_START_NOTE, key_count: int = MIDI_KEY_COUNT
) -> tuple[KeyboardKey, ...]:
    """Return key positions for a fixed-width piano surface."""

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


def lpk25_keys(
    start_note: int = LPK25_START_NOTE, key_count: int = LPK25_KEY_COUNT
) -> tuple[KeyboardKey, ...]:
    """Return physical key positions for one unshifted 25-key LPK25 span."""

    return keyboard_keys(start_note, key_count)


def keyboard_width(keys: tuple[KeyboardKey, ...] | None = None) -> int:
    """Return the fixed render width for a framed key set."""

    return keyboard_inner_width(keys) + 2


def keyboard_inner_width(keys: tuple[KeyboardKey, ...] | None = None) -> int:
    """Return the fixed inner piano-bed width for a key set."""

    key_set = keys if keys is not None else keyboard_keys()
    white_keys = sum(1 for key in key_set if not key.is_black)
    return white_keys * WHITE_KEY_WIDTH


def key_left(note: int, keys: tuple[KeyboardKey, ...] | None = None) -> int | None:
    """Return the framed horizontal offset for a MIDI note, if rendered."""

    bounds = key_bounds(note, keys)
    if bounds is None:
        return None
    return bounds[0]


def key_bounds(note: int, keys: tuple[KeyboardKey, ...] | None = None) -> tuple[int, int] | None:
    """Return the framed left and right offsets for a MIDI note, if rendered."""

    key_set = keys if keys is not None else keyboard_keys()
    for key in key_set:
        if key.note == note:
            left = key.left + 1
            return left, left + key.width
    return None


def scroll_target_for_note(
    note: int,
    *,
    current_scroll: int,
    viewport_width: int,
    keys: tuple[KeyboardKey, ...] | None = None,
) -> int | None:
    """Return a scroll target only when a note is outside the visible viewport."""

    bounds = key_bounds(note, keys)
    if bounds is None or viewport_width <= 0:
        return None

    surface_width = keyboard_width(keys)
    visible_width = min(viewport_width, surface_width)
    max_scroll = max(0, surface_width - visible_width)
    scroll_left = min(max(current_scroll, 0), max_scroll)
    key_left_offset, key_right_offset = bounds
    scroll_right = scroll_left + visible_width

    if key_left_offset < scroll_left:
        return max(0, key_left_offset)
    if key_right_offset > scroll_right:
        return min(max_scroll, key_right_offset - visible_width)
    if scroll_left != current_scroll:
        return scroll_left
    return None


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


def _set_cell(
    cells: list[tuple[str, str]],
    *,
    index: int,
    char: str,
    style: str,
) -> None:
    if 0 <= index < len(cells):
        cells[index] = (char, style)


def _fill_span(
    cells: list[tuple[str, str]],
    *,
    left: int,
    width: int,
    char: str,
    style: str,
) -> None:
    for offset in range(width):
        _set_cell(cells, index=left + offset, char=char, style=style)


def _write_label(
    cells: list[tuple[str, str]],
    *,
    left: int,
    width: int,
    label: str,
    style: str,
) -> None:
    for offset, char in enumerate(label.center(width)[:width]):
        _set_cell(cells, index=left + offset, char=char, style=style)


def _new_grid(width: int) -> list[list[tuple[str, str]]]:
    return [[(" ", PANEL_STYLE) for _ in range(width)] for _ in range(KEYBOARD_HEIGHT)]


def _draw_frame(grid: list[list[tuple[str, str]]], *, width: int) -> None:
    for row_index in range(KEYBOARD_HEIGHT):
        _set_cell(grid[row_index], index=0, char="│", style=FRAME_STYLE)
        _set_cell(grid[row_index], index=width - 1, char="│", style=FRAME_STYLE)

    _fill_span(grid[0], left=1, width=width - 2, char="─", style=FRAME_STYLE)
    _fill_span(
        grid[KEYBOARD_HEIGHT - 1],
        left=1,
        width=width - 2,
        char="─",
        style=FRAME_STYLE,
    )
    _set_cell(grid[0], index=0, char="┌", style=FRAME_STYLE)
    _set_cell(grid[0], index=width - 1, char="┐", style=FRAME_STYLE)
    _set_cell(grid[KEYBOARD_HEIGHT - 1], index=0, char="└", style=FRAME_STYLE)
    _set_cell(grid[KEYBOARD_HEIGHT - 1], index=width - 1, char="┘", style=FRAME_STYLE)


def _draw_white_keys(
    grid: list[list[tuple[str, str]]],
    *,
    keys: tuple[KeyboardKey, ...],
    pressed_notes: set[int] | frozenset[int],
    target_notes: set[int] | frozenset[int],
) -> None:
    white_keys = [key for key in keys if not key.is_black]
    for key in white_keys:
        left = key.left + 1
        style = key_style(key.note, pressed_notes, target_notes)
        for row_index in range(1, KEYBOARD_HEIGHT - 1):
            _fill_span(
                grid[row_index],
                left=left,
                width=key.width,
                char=" ",
                style=style,
            )
        _write_label(
            grid[KEYBOARD_HEIGHT - 2],
            left=left,
            width=key.width,
            label=key.name,
            style=style,
        )

    for boundary_index in range(1, len(white_keys)):
        boundary = boundary_index * WHITE_KEY_WIDTH + 1
        for row_index in range(1, KEYBOARD_HEIGHT - 1):
            _set_cell(grid[row_index], index=boundary, char="│", style=FRAME_STYLE)
        _set_cell(grid[0], index=boundary, char="┬", style=FRAME_STYLE)
        _set_cell(
            grid[KEYBOARD_HEIGHT - 1],
            index=boundary,
            char="┴",
            style=FRAME_STYLE,
        )


def _draw_black_keys(
    grid: list[list[tuple[str, str]]],
    *,
    keys: tuple[KeyboardKey, ...],
    pressed_notes: set[int] | frozenset[int],
    target_notes: set[int] | frozenset[int],
) -> None:
    for key in keys:
        if not key.is_black:
            continue

        left = key.left + 1
        style = key_style(key.note, pressed_notes, target_notes)
        _write_label(grid[1], left=left, width=key.width, label="┌───┐", style=style)
        _write_label(grid[2], left=left, width=key.width, label="│   │", style=style)
        _write_label(grid[3], left=left, width=key.width, label=key.name, style=style)
        _write_label(grid[4], left=left, width=key.width, label="│   │", style=style)
        _write_label(grid[5], left=left, width=key.width, label="└─┬─┘", style=style)


def keyboard_text(
    pressed_notes: set[int] | frozenset[int],
    target_notes: set[int] | frozenset[int] = frozenset(),
    *,
    keys: tuple[KeyboardKey, ...] | None = None,
) -> Text:
    """Render a fixed-width MIDI piano surface as rich text."""

    key_set = keys if keys is not None else keyboard_keys()
    width = keyboard_width(key_set)
    grid = _new_grid(width)
    _draw_frame(grid, width=width)
    _draw_white_keys(
        grid,
        keys=key_set,
        pressed_notes=pressed_notes,
        target_notes=target_notes,
    )
    _draw_black_keys(
        grid,
        keys=key_set,
        pressed_notes=pressed_notes,
        target_notes=target_notes,
    )

    text = Text()
    for row_index, row in enumerate(grid):
        if row_index:
            text.append("\n")
        text.append(_styled_row(row, width=width))
    return text
