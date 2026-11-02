from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from halcyon.coach_highway import (
    CoachReviewSummary,
    CoachVisualizationSnapshot,
    HighwayNoteSpan,
)
from halcyon.coach_highway_widget import CoachNoteHighway
from halcyon.keyboard import key_bounds, keyboard_width


def snapshot(
    *,
    phase: str = "capturing",
    active: bool = True,
    sampled_at_ns: int = 1,
    spans: tuple[HighwayNoteSpan, ...] = (),
    target_notes: tuple[int, ...] = (),
    target_offset_us: int | None = None,
    count_in_remaining: int | None = None,
    review: CoachReviewSummary | None = None,
    practice_level: str = "full",
    required_note_count: int = 0,
    ghost_note_count: int = 0,
) -> CoachVisualizationSnapshot:
    return CoachVisualizationSnapshot(
        active=active,
        phase=phase,
        sampled_at_ns=sampled_at_ns,
        segment_label="main",
        tempo_percent=70,
        loop_count=2,
        fidelity="verified",
        spans=spans,
        current_loop=0,
        target_notes=target_notes,
        target_offset_us=target_offset_us,
        count_in_remaining=count_in_remaining,
        review=review,
        practice_level=practice_level,
        required_note_count=required_note_count,
        ghost_note_count=ghost_note_count,
    )


def span(
    note: int,
    start_us: int,
    end_us: int,
    *,
    cue: str = "target",
    ordinal: int = 0,
    required: bool = True,
) -> HighwayNoteSpan:
    return HighwayNoteSpan(
        note=note,
        channel=1,
        loop_index=0,
        timeline_ordinal=ordinal,
        start_offset_us=start_us,
        end_offset_us=end_us,
        cue_kind=cue,  # type: ignore[arg-type]
        required=required,
    )


class HighwayApp(App[None]):
    def __init__(self, value: CoachVisualizationSnapshot) -> None:
        super().__init__()
        self.value = value
        self.consumed: list[CoachVisualizationSnapshot] = []
        self.highway = CoachNoteHighway(
            lambda: self.value,
            snapshot_consumer=self.consumed.append,
            id="highway",
        )
        c4_bounds = key_bounds(60)
        assert c4_bounds is not None
        self.highway.set_viewport_origin(c4_bounds[0] - 20)

    def compose(self) -> ComposeResult:
        yield self.highway


def rows(widget: CoachNoteHighway) -> tuple[str, ...]:
    return tuple(widget.render_line(row).text for row in range(widget.size.height))


@pytest.mark.anyio
async def test_exact_deadline_places_target_on_strike_approach_deterministically() -> None:
    current = snapshot(
        spans=(span(60, 0, 500_000),),
        target_notes=(60,),
        target_offset_us=0,
    )
    app = HighwayApp(current)

    async with app.run_test(size=(120, 18)):
        app.highway.set_snapshot(current)
        first = rows(app.highway)
        second = rows(app.highway)

        assert first == second
        assert "COACH / CAPTURING" in first[0]
        assert "SEG main" in first[0]
        assert "VERIFIED" in first[0]
        assert "○ C4 0.0s" in first[0]
        assert "○" in first[-2]
        assert first[-1] == "═" * app.highway.size.width
        assert "STRIKE" not in "\n".join(first)
        assert "EXPECTED" not in "\n".join(first)
        assert "NEXT" not in "\n".join(first)
        assert not app.highway.can_focus


@pytest.mark.anyio
async def test_relative_time_moves_bars_down_without_advancing_coach_state() -> None:
    future = snapshot(
        sampled_at_ns=1,
        spans=(span(64, 2_000_000, 2_500_000),),
        target_notes=(64,),
        target_offset_us=2_000_000,
    )
    due = snapshot(
        sampled_at_ns=2,
        spans=(span(64, 0, 500_000),),
        target_notes=(64,),
        target_offset_us=0,
    )
    app = HighwayApp(future)

    async with app.run_test(size=(120, 18)):
        future_rows = rows(app.highway)
        future_row = next(
            index for index, row in enumerate(future_rows[1:-1], start=1) if "○" in row
        )
        app.highway.set_snapshot(due)
        due_rows = rows(app.highway)
        due_row = max(index for index, row in enumerate(due_rows[1:-1], start=1) if "○" in row)

        assert future_row < due_row
        assert due_row == app.highway.size.height - 2


@pytest.mark.anyio
async def test_cue_glyphs_phase_copy_and_count_in_are_non_color_redundant() -> None:
    demonstrating = snapshot(
        phase="demonstrating",
        spans=(span(60, 0, 400_000, cue="halcyon"),),
        target_notes=(60,),
        target_offset_us=0,
    )
    app = HighwayApp(demonstrating)

    async with app.run_test(size=(120, 18)):
        rendered = rows(app.highway)
        assert any("◆" in row for row in rendered[:-1])
        assert rendered[-1] == "═" * app.highway.size.width

        app.highway.set_snapshot(
            snapshot(
                phase="count_in",
                spans=(span(60, 0, 400_000),),
                target_notes=(60,),
                target_offset_us=0,
                count_in_remaining=2,
            )
        )
        rendered = rows(app.highway)
        assert "COACH / COUNT IN   2" in rendered[0]
        assert "○" in rendered[-2]

        expected = {
            "resolving": "Waiting for a validated lesson",
            "authoring": "approximation in progress",
            "scoring": "SCORING ATTEMPT",
            "cancelling": "RELEASING HALCYON VOICES",
        }
        for phase, copy in expected.items():
            app.highway.set_snapshot(snapshot(phase=phase))
            assert copy in "\n".join(rows(app.highway))


@pytest.mark.anyio
@pytest.mark.parametrize("cue,glyph", [("halcyon", "◆"), ("target", "○")])
@pytest.mark.parametrize("note,color", [(60, "#ab9df2"), (61, "#78dce8")])
async def test_note_color_depends_only_on_natural_or_sharp_pitch(
    cue: str,
    glyph: str,
    note: int,
    color: str,
) -> None:
    app = HighwayApp(snapshot(spans=(span(note, 0, 200_000, cue=cue),)))

    async with app.run_test(size=(120, 18)):
        onset = app.highway.render_line(app.highway.size.height - 2)
        glyph_styles = [str(segment.style) for segment in onset if glyph in segment.text]

        assert glyph_styles
        assert all(color in style for style in glyph_styles)


@pytest.mark.anyio
async def test_review_renders_only_bounded_aggregate_summary_and_actions() -> None:
    review = CoachReviewSummary(
        status="completed",
        overall_score_percent=87.4,
        matched_note_count=7,
        expected_note_count=8,
        tips=("Keep the pulse even.", "Release cleanly."),
    )
    app = HighwayApp(snapshot(phase="review", review=review))

    async with app.run_test(size=(120, 18)):
        rendered = "\n".join(rows(app.highway))

        assert "COMPLETED · 87% · 7/8 NOTES" in rendered
        assert "TIP · Keep the pulse even." in rendered
        assert "/coach attempt" in rendered
        assert "event_id" not in rendered
        assert "source_port" not in rendered


@pytest.mark.anyio
@pytest.mark.parametrize("note,expected_width", [(60, 7), (61, 5)])
@pytest.mark.parametrize("clip_edge", ["none", "left", "right"])
@pytest.mark.anyio
async def test_note_bars_use_exact_clipped_keyboard_key_bounds(
    note: int,
    expected_width: int,
    clip_edge: str,
) -> None:
    app = HighwayApp(
        snapshot(
            spans=(span(note, 0, 200_000),),
            target_notes=(note,),
            target_offset_us=0,
        )
    )

    async with app.run_test(size=(40, 10)):
        bounds = key_bounds(note)
        assert bounds is not None
        left, right = bounds
        width = app.highway.size.width
        if clip_edge == "left":
            origin = left + 2
        elif clip_edge == "right":
            origin = left - (width - 3)
        else:
            origin = left - 3
        app.highway.set_viewport_origin(origin)
        rendered = rows(app.highway)
        onset_row = rendered[-2]
        expected_left = max(0, left - app.highway.viewport_origin)
        expected_right = min(width, right - app.highway.viewport_origin)

        assert right - left == expected_width
        assert tuple(index for index, cell in enumerate(onset_row) if cell == "○") == tuple(
            range(expected_left, expected_right)
        )


@pytest.mark.anyio
async def test_snapshot_changes_never_recenter_the_keyboard_owned_viewport() -> None:
    app = HighwayApp(snapshot(spans=(span(60, 500_000, 800_000),)))

    async with app.run_test(size=(120, 18)):
        origin = app.highway.viewport_origin
        app.highway.set_snapshot(snapshot(sampled_at_ns=2, spans=(span(96, 0, 200_000),)))
        rendered = rows(app.highway)

        assert app.highway.viewport_origin == origin
        assert not any("○" in row for row in rendered[1:-1])
        app.highway.set_viewport_origin(keyboard_width() + 100)
        assert app.highway.viewport_origin == keyboard_width()


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(180, 55), (120, 42), (90, 30)])
async def test_wide_medium_compact_views_have_bounded_viewports_and_no_short_rows(
    size: tuple[int, int],
) -> None:
    app = HighwayApp(
        snapshot(
            spans=(span(60, 0, 500_000), span(72, 1_000_000, 1_500_000, ordinal=1)),
            target_notes=(60,),
            target_offset_us=0,
        )
    )

    async with app.run_test(size=size):
        rendered = rows(app.highway)
        bounds = key_bounds(60)
        assert bounds is not None
        left, right = bounds
        expected_left = left - app.highway.viewport_origin
        expected_right = right - app.highway.viewport_origin

        assert len(rendered) == app.highway.size.height
        assert all(len(row) == app.highway.size.width for row in rendered)
        assert tuple(index for index, cell in enumerate(rendered[-2]) if cell == "○") == tuple(
            range(expected_left, expected_right)
        )
        assert rendered[-1] == "═" * app.highway.size.width


@pytest.mark.anyio
async def test_under_five_rows_uses_note_and_time_fallback_without_next_copy() -> None:
    app = HighwayApp(snapshot(target_notes=(60, 64), target_offset_us=800_000))

    async with app.run_test(size=(90, 4)):
        rendered = rows(app.highway)

        assert len(rendered) < 5
        assert "○ C4+E4 0.8s" in rendered[1]
        assert "NEXT" not in "\n".join(rendered)
        assert rendered[-1] == "═" * app.highway.size.width


@pytest.mark.anyio
@pytest.mark.parametrize("note,color", [(60, "#ab9df2"), (61, "#78dce8")])
@pytest.mark.parametrize("cue", ["halcyon", "target"])
async def test_ghosts_are_dim_dotted_and_keep_pitch_palette(
    note: int, color: str, cue: str
) -> None:
    app = HighwayApp(
        snapshot(
            spans=(span(note, 0, 1_000_000, cue=cue, required=False),),
            practice_level="melody",
            required_note_count=1,
            ghost_note_count=2,
        )
    )

    async with app.run_test(size=(122, 18)):
        rendered = rows(app.highway)
        onset = app.highway.render_line(app.highway.size.height - 2)
        ghost_styles = [segment.style for segment in onset if "◇" in segment.text]

        assert ghost_styles
        assert all(
            style is not None and style.dim and color in str(style) for style in ghost_styles
        )
        assert any("┊" in row for row in rendered[1:-2])
        assert all("◆" not in row and "○" not in row for row in rendered[1:-1])
        assert "MELODY 1 PLAY / 2 GHOST" in rendered[0]
        assert rendered[-1] == "═" * app.highway.size.width


@pytest.mark.anyio
@pytest.mark.parametrize("ghost_last", [False, True])
async def test_required_note_paints_above_colliding_ghost_regardless_of_order(
    ghost_last: bool,
) -> None:
    required = span(61, 0, 1_000_000)
    ghost = span(61, 0, 1_000_000, required=False, ordinal=1)
    app = HighwayApp(snapshot(spans=(required, ghost) if ghost_last else (ghost, required)))

    async with app.run_test(size=(122, 18)):
        rendered = rows(app.highway)
        onset = app.highway.render_line(app.highway.size.height - 2)
        required_styles = [segment.style for segment in onset if "○" in segment.text]

        assert required_styles
        assert all(style is not None and style.bold and not style.dim for style in required_styles)
        assert all("◇" not in row and "┊" not in row for row in rendered[1:-1])


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(122, 60), (90, 30)])
async def test_layer_counts_do_not_displace_anticipation_or_add_strike_text(
    size: tuple[int, int],
) -> None:
    app = HighwayApp(
        snapshot(
            spans=(span(60, 1_000_000, 1_500_000), span(61, 0, 200_000, required=False)),
            target_notes=(60,),
            target_offset_us=1_000_000,
            practice_level="essentials",
            required_note_count=300,
            ghost_note_count=153,
        )
    )

    async with app.run_test(size=size):
        rendered = rows(app.highway)

        assert "○ C4 1.0s" in rendered[0]
        assert "ESSENTIALS 300 PLAY / 153 GHOST" in rendered[0]
        assert rendered[-1] == "═" * app.highway.size.width


@pytest.mark.anyio
@pytest.mark.parametrize("note", [60, 61])
@pytest.mark.parametrize("clip_edge", ["left", "right"])
async def test_ghost_notes_share_exact_key_bounds_and_horizontal_scroll(
    note: int, clip_edge: str
) -> None:
    app = HighwayApp(snapshot(spans=(span(note, 0, 200_000, required=False),)))

    async with app.run_test(size=(40, 10)):
        bounds = key_bounds(note)
        assert bounds is not None
        left, right = bounds
        origin = left + 2 if clip_edge == "left" else left - (app.highway.size.width - 3)
        app.highway.set_viewport_origin(origin)
        onset = rows(app.highway)[-2]

        assert tuple(index for index, glyph in enumerate(onset) if glyph == "◇") == tuple(
            range(
                max(0, left - app.highway.viewport_origin),
                min(app.highway.size.width, right - app.highway.viewport_origin),
            )
        )
