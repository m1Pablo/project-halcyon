from halcyon.keyboard import (
    BLACK_TARGET_STYLE,
    KEYBOARD_HEIGHT,
    LPK25_KEY_COUNT,
    LPK25_START_NOTE,
    WHITE_KEY_WIDTH,
    key_style,
    keyboard_inner_width,
    keyboard_notes,
    keyboard_text,
    keyboard_width,
    lpk25_keys,
)


def test_keyboard_defaults_to_lpk25_size() -> None:
    notes = keyboard_notes()

    assert len(notes) == LPK25_KEY_COUNT
    assert notes[0] == LPK25_START_NOTE
    assert notes[-1] == 72


def test_pressed_key_style_is_distinct() -> None:
    pressed_style = key_style(60, {60})
    idle_style = key_style(60, set())

    assert pressed_style != idle_style
    assert "#95d5b2" in pressed_style


def test_lpk25_geometry_is_fixed_width_and_ordered() -> None:
    keys = lpk25_keys()

    assert len(keys) == 25
    assert [key.note for key in keys] == list(range(48, 73))
    assert keyboard_inner_width(keys) == 15 * WHITE_KEY_WIDTH
    assert keyboard_width(keys) == 15 * WHITE_KEY_WIDTH + 2


def test_keyboard_render_is_one_horizontal_piano_surface() -> None:
    rendered = keyboard_text({60}, {61})
    lines = rendered.plain.splitlines()

    assert len(lines) == KEYBOARD_HEIGHT
    assert all(len(line) == keyboard_width() for line in lines)
    assert lines[0].startswith("┌")
    assert lines[0].endswith("┐")
    assert lines[-1].startswith("└")
    assert lines[-1].endswith("┘")
    assert "C3" in rendered.plain
    assert "C#3" in rendered.plain
    assert "C5" in rendered.plain


def test_target_key_style_is_distinct() -> None:
    target_style = key_style(61, set(), {61})

    assert target_style == BLACK_TARGET_STYLE
