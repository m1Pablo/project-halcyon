from halcyon.keyboard import (
    BLACK_HALCYON_STYLE,
    BLACK_TARGET_STYLE,
    BOTH_MARKER_STYLE,
    COMBINED_STATE_STYLE,
    HUMAN_MARKER_STYLE,
    KEYBOARD_HEIGHT,
    MIDI_END_NOTE,
    MIDI_KEY_COUNT,
    MIDI_START_NOTE,
    NATURAL_HALCYON_MARKER_STYLE,
    NATURAL_TARGET_MARKER_STYLE,
    LPK25_KEY_COUNT,
    LPK25_START_NOTE,
    SHARP_HALCYON_MARKER_STYLE,
    SHARP_TARGET_MARKER_STYLE,
    VISIBLE_KEYBOARD_WIDTH,
    VISIBLE_WHITE_KEY_COUNT,
    WHITE_HALCYON_STYLE,
    WHITE_KEY_WIDTH,
    WHITE_TARGET_STYLE,
    key_bounds,
    key_left,
    key_markers,
    key_style,
    keyboard_keys,
    keyboard_inner_width,
    keyboard_notes,
    keyboard_text,
    keyboard_width,
    lpk25_keys,
    scroll_target_for_note,
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
    assert "#ff9f43" in pressed_style


def test_keyboard_geometry_is_fixed_width_and_ordered() -> None:
    keys = keyboard_keys()

    assert len(keys) == 128
    assert [key.note for key in keys] == list(range(128))
    assert keyboard_inner_width(keys) == 75 * WHITE_KEY_WIDTH
    assert keyboard_width(keys) == 75 * WHITE_KEY_WIDTH + 2
    assert VISIBLE_KEYBOARD_WIDTH == VISIBLE_WHITE_KEY_COUNT * WHITE_KEY_WIDTH + 2


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


def test_sticky_scroll_ignores_adjacent_notes_inside_viewport() -> None:
    assert (
        scroll_target_for_note(
            47,
            current_scroll=0,
            viewport_width=VISIBLE_KEYBOARD_WIDTH,
        )
        is None
    )
    assert (
        scroll_target_for_note(
            48,
            current_scroll=0,
            viewport_width=VISIBLE_KEYBOARD_WIDTH,
        )
        is None
    )
    assert (
        scroll_target_for_note(
            50,
            current_scroll=0,
            viewport_width=VISIBLE_KEYBOARD_WIDTH,
        )
        is None
    )


def test_sticky_scroll_moves_only_when_note_leaves_viewport() -> None:
    note = 52
    bounds = key_bounds(note)
    assert bounds is not None
    _, right = bounds

    assert (
        scroll_target_for_note(
            note,
            current_scroll=0,
            viewport_width=VISIBLE_KEYBOARD_WIDTH,
        )
        == right - VISIBLE_KEYBOARD_WIDTH
    )


def test_sticky_scroll_can_reach_top_midi_note() -> None:
    bounds = key_bounds(127)
    assert bounds is not None
    left, right = bounds
    target = scroll_target_for_note(
        127,
        current_scroll=0,
        viewport_width=VISIBLE_KEYBOARD_WIDTH,
    )

    assert target is not None
    assert target <= left
    assert right <= target + VISIBLE_KEYBOARD_WIDTH
    assert target <= keyboard_width() - VISIBLE_KEYBOARD_WIDTH


def test_target_key_styles_follow_pitch_class_palette() -> None:
    natural_style = key_style(60, set(), {60})
    sharp_style = key_style(61, set(), {61})

    assert natural_style == WHITE_TARGET_STYLE
    assert "#ab9df2" in natural_style
    assert sharp_style == BLACK_TARGET_STYLE
    assert "#78dce8" in sharp_style


def test_human_halcyon_and_both_key_states_preserve_ownership_palette() -> None:
    human_style = key_style(61, {61})
    natural_halcyon_style = key_style(60, set(), halcyon_notes={60})
    sharp_halcyon_style = key_style(61, set(), halcyon_notes={61})
    both_style = key_style(61, {61}, halcyon_notes={61})

    assert "#ff9f43" in human_style
    assert natural_halcyon_style == WHITE_HALCYON_STYLE
    assert "#ab9df2" in natural_halcyon_style
    assert sharp_halcyon_style == BLACK_HALCYON_STYLE
    assert "#78dce8" in sharp_halcyon_style
    assert both_style == COMBINED_STATE_STYLE
    assert "#ffd866" in both_style


def test_key_markers_follow_pitch_class_and_preserve_ownership_overrides() -> None:
    natural_markers = key_markers(60, set(), {60}, {60})
    sharp_markers = key_markers(61, set(), {61}, {61})
    shared_markers = key_markers(60, {60}, {60}, {60})

    assert natural_markers == (
        ("◆", NATURAL_HALCYON_MARKER_STYLE),
        ("○", NATURAL_TARGET_MARKER_STYLE),
    )
    assert sharp_markers == (
        ("◆", SHARP_HALCYON_MARKER_STYLE),
        ("○", SHARP_TARGET_MARKER_STYLE),
    )
    assert shared_markers == (
        ("●◆", BOTH_MARKER_STYLE),
        ("○", NATURAL_TARGET_MARKER_STYLE),
    )


def test_keyboard_renders_source_markers_without_embedded_legend_words() -> None:
    rendered = keyboard_text({60, 62}, {60}, halcyon_notes={60, 61})
    compact_rows = rendered.plain.splitlines()[:7]

    assert all(
        word not in rendered.plain
        for word in ("YOU", "HALCYON", "BOTH", "NEXT", "NATURAL", "SHARP")
    )
    assert "●◆" in rendered.plain
    assert "○" in rendered.plain
    assert any("●◆" in row for row in compact_rows[1:])
    assert any("○" in row for row in compact_rows[1:])
    styles = {str(span.style) for span in rendered.spans}
    assert HUMAN_MARKER_STYLE in styles
    assert SHARP_HALCYON_MARKER_STYLE in styles
    assert NATURAL_TARGET_MARKER_STYLE in styles
