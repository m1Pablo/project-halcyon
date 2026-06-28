from halcyon.keyboard import (
    BLACK_TARGET_STYLE,
    KEYBOARD_HEIGHT,
    MIDI_END_NOTE,
    MIDI_KEY_COUNT,
    MIDI_START_NOTE,
    LPK25_KEY_COUNT,
    LPK25_START_NOTE,
    WHITE_KEY_WIDTH,
    key_left,
    key_style,
    keyboard_keys,
    keyboard_inner_width,
    keyboard_notes,
    keyboard_text,
    keyboard_width,
    lpk25_keys,
)


def test_keyboard_defaults_to_full_midi_spectrum() -> None:
    notes = keyboard_notes()

    assert len(notes) == MIDI_KEY_COUNT
    assert notes[0] == MIDI_START_NOTE
    assert notes[-1] == MIDI_END_NOTE


def test_lpk25_span_is_still_available_for_physical_reference() -> None:
    keys = lpk25_keys()

    assert len(keys) == LPK25_KEY_COUNT
    assert [key.note for key in keys] == list(range(LPK25_START_NOTE, 73))


def test_pressed_key_style_is_distinct() -> None:
    pressed_style = key_style(60, {60})
    idle_style = key_style(60, set())

    assert pressed_style != idle_style
    assert "#95d5b2" in pressed_style


def test_keyboard_geometry_is_fixed_width_and_ordered() -> None:
    keys = keyboard_keys()

    assert len(keys) == 128
    assert [key.note for key in keys] == list(range(128))
    assert keyboard_inner_width(keys) == 75 * WHITE_KEY_WIDTH
    assert keyboard_width(keys) == 75 * WHITE_KEY_WIDTH + 2


def test_keyboard_render_is_one_horizontal_piano_surface() -> None:
    rendered = keyboard_text({0, 120, 127}, {61})
    lines = rendered.plain.splitlines()

    assert len(lines) == KEYBOARD_HEIGHT
    assert all(len(line) == keyboard_width() for line in lines)
    assert lines[0].startswith("┌")
    assert lines[0].endswith("┐")
    assert lines[-1].startswith("└")
    assert lines[-1].endswith("┘")
    assert "C-1" in rendered.plain
    assert "C#4" in rendered.plain
    assert "C9" in rendered.plain
    assert "G9" in rendered.plain


def test_key_left_covers_octave_shifted_extremes() -> None:
    assert key_left(0) is not None
    assert key_left(120) is not None
    assert key_left(127) is not None
    assert key_left(128) is None


def test_target_key_style_is_distinct() -> None:
    target_style = key_style(61, set(), {61})

    assert target_style == BLACK_TARGET_STYLE
