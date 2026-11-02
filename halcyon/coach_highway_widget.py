"""Display-only Textual piano roll for the coach visualization snapshot."""

from __future__ import annotations

from collections.abc import Callable

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from halcyon.coach_highway import CoachVisualizationSnapshot, HighwayNoteSpan
from halcyon.keyboard import is_black_note, key_bounds, keyboard_width
from halcyon.midi import note_name

SnapshotProvider = Callable[[], CoachVisualizationSnapshot]
SnapshotConsumer = Callable[[CoachVisualizationSnapshot], None]

_STAGE = Style.parse("#11151d on #11151d")
_GRID = Style.parse("#2c3545 on #11151d")
_MUTED = Style.parse("#8b95a7 on #11151d")
_TEXT = Style.parse("#fcfcfa on #11151d")
_SHARP = Style.parse("bold #78dce8 on #11151d")
_NATURAL = Style.parse("bold #ab9df2 on #11151d")
_GHOST_SHARP = Style.parse("dim #78dce8 on #11151d")
_GHOST_NATURAL = Style.parse("dim #ab9df2 on #11151d")
_COACH = Style.parse("bold #ab9df2 on #11151d")
_HUMAN = Style.parse("bold #ff9f43 on #11151d")
_RESULT = Style.parse("bold #ffd866 on #11151d")
_FAILURE = Style.parse("bold #ff6188 on #11151d")
_KEYBOARD_WIDTH = keyboard_width()
_KEY_BOUNDS = tuple(key_bounds(note) for note in range(128))


class CoachNoteHighway(Widget):
    """Render bounded coach timing without owning playback or scoring."""

    DEFAULT_CSS = """
    CoachNoteHighway {
        width: 100%;
        height: 100%;
        color: #fcfcfa;
        background: #11151d;
    }
    """

    def __init__(
        self,
        snapshot_provider: SnapshotProvider,
        *,
        snapshot_consumer: SnapshotConsumer | None = None,
        refresh_hz: float = 24.0,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.snapshot_provider = snapshot_provider
        self.snapshot_consumer = snapshot_consumer
        self.refresh_hz = max(1.0, min(float(refresh_hz), 60.0))
        self._snapshot = self._inactive_snapshot()
        self._viewport_origin = 0
        self._cache_key: tuple[object, ...] | None = None
        self._cached_rows: tuple[Strip, ...] = ()

    @property
    def snapshot(self) -> CoachVisualizationSnapshot:
        """The latest immutable sample, exposed only to local UI tests."""

        return self._snapshot

    @property
    def viewport_origin(self) -> int:
        """Return the keyboard-owned horizontal viewport origin."""

        return self._viewport_origin

    def on_mount(self) -> None:
        self._poll_snapshot()
        self.set_interval(1.0 / self.refresh_hz, self._poll_snapshot)

    def _poll_snapshot(self) -> None:
        try:
            snapshot = self.snapshot_provider()
        except Exception:
            snapshot = self._snapshot
        self.set_snapshot(snapshot)

    def set_snapshot(self, snapshot: CoachVisualizationSnapshot) -> None:
        """Inject one engine-owned snapshot without changing coach state."""

        if not isinstance(snapshot, CoachVisualizationSnapshot):
            raise TypeError("snapshot must be a CoachVisualizationSnapshot")
        changed = snapshot != self._snapshot
        self._snapshot = snapshot
        if changed:
            self._invalidate()
        if self.snapshot_consumer is not None:
            self.snapshot_consumer(snapshot)

    def set_viewport_origin(self, scroll_x: float) -> None:
        """Mirror the keyboard's cell-aligned horizontal scroll position."""

        origin = min(_KEYBOARD_WIDTH, max(0, round(float(scroll_x))))
        if origin != self._viewport_origin:
            self._viewport_origin = origin
            self._invalidate()

    def render_line(self, y: int) -> Strip:
        width, height = max(0, self.size.width), max(0, self.size.height)
        if width == 0 or height == 0 or not 0 <= y < height:
            return Strip.blank(width, self.rich_style)
        self._ensure_cache(width, height)
        return self._cached_rows[y]

    def _ensure_cache(self, width: int, height: int) -> None:
        layout = self._layout(width, height)
        lookahead_us = {
            "wide": 4_000_000,
            "medium": 3_000_000,
            "compact": 2_000_000,
        }[layout]
        key = (self._snapshot, width, height, layout, self._viewport_origin)
        if key == self._cache_key:
            return
        self._cache_key = key
        self._cached_rows = self._render_rows(
            self._snapshot,
            width=width,
            height=height,
            layout=layout,
            lookahead_us=lookahead_us,
        )

    def _render_rows(
        self,
        snapshot: CoachVisualizationSnapshot,
        *,
        width: int,
        height: int,
        layout: str,
        lookahead_us: int,
    ) -> tuple[Strip, ...]:
        cells = [[" " for _ in range(width)] for _ in range(height)]
        styles = [[_STAGE for _ in range(width)] for _ in range(height)]
        header = self._header(snapshot, layout)
        header_style = {
            "count_in": _RESULT,
            "capturing": _HUMAN,
            "demonstrating": _COACH,
        }.get(snapshot.phase, _TEXT)
        self._write(cells, styles, 0, 0, header, header_style)

        if height < 5:
            if height > 1:
                self._write(cells, styles, 1, 0, self._fallback(snapshot), _TEXT)
            if height > 2:
                self._draw_strike(cells, styles, height - 1, snapshot=snapshot)
            return self._to_strips(cells, styles)

        strike_y = height - 1
        lane_top = 1
        lane_bottom = strike_y - 1
        self._draw_octave_guides(cells, styles, lane_top, lane_bottom)

        if snapshot.phase == "review":
            self._draw_review(cells, styles, snapshot, lane_top, lane_bottom)
        elif snapshot.phase in {"resolving", "authoring", "scoring", "cancelling"}:
            self._draw_phase_copy(cells, styles, snapshot, lane_top, lane_bottom)
        elif snapshot.projection_error is not None:
            self._write(cells, styles, lane_top, 1, "TIMELINE UNAVAILABLE", _FAILURE)
        else:
            self._draw_spans(
                cells,
                styles,
                snapshot.spans,
                lane_top=lane_top,
                lane_bottom=lane_bottom,
                lookahead_us=lookahead_us,
            )
            self._draw_octave_labels(cells, styles, lane_top)

        self._draw_strike(cells, styles, strike_y, snapshot=snapshot)
        return self._to_strips(cells, styles)

    def _header(self, snapshot: CoachVisualizationSnapshot, layout: str) -> str:
        phase = snapshot.phase.replace("_", " ").upper()
        if snapshot.phase == "cancelling":
            phase = "STOPPING"
        parts = [f"COACH / {phase}"]
        if snapshot.phase == "count_in" and snapshot.count_in_remaining is not None:
            parts.append(str(snapshot.count_in_remaining))
        anticipation = self._anticipation_copy(snapshot)
        if anticipation:
            parts.append(anticipation)
        if (
            snapshot.required_note_count
            or snapshot.ghost_note_count
            or snapshot.practice_level != "full"
        ):
            parts.append(
                f"{snapshot.practice_level.upper()} {snapshot.required_note_count} PLAY / "
                f"{snapshot.ghost_note_count} GHOST"
            )
        if layout != "compact":
            if snapshot.tempo_percent is not None:
                parts.append(f"TEMPO {snapshot.tempo_percent}%")
            if snapshot.current_loop is not None and snapshot.loop_count:
                parts.append(f"LOOP {snapshot.current_loop + 1}/{snapshot.loop_count}")
            elif snapshot.loop_count:
                parts.append(f"LOOPS {snapshot.loop_count}")
        if layout == "wide":
            if snapshot.segment_label:
                parts.append(f"SEG {snapshot.segment_label}")
            if snapshot.fidelity:
                parts.append(snapshot.fidelity.upper())
        return "   ".join(parts)

    def _fallback(self, snapshot: CoachVisualizationSnapshot) -> str:
        anticipation = self._anticipation_copy(snapshot)
        if anticipation:
            return anticipation
        return {
            "resolving": "Waiting for a validated lesson",
            "authoring": "Creating a bounded approximation",
            "scoring": "SCORING ATTEMPT",
            "cancelling": "RELEASING HALCYON VOICES",
            "review": self._review_score(snapshot),
        }.get(snapshot.phase, "COACH READY")

    def _anticipation_copy(self, snapshot: CoachVisualizationSnapshot) -> str:
        if not snapshot.target_notes:
            return ""
        names = "+".join(note_name(note) for note in snapshot.target_notes)
        if snapshot.target_offset_us is None:
            return f"○ {names}"
        return f"○ {names} {max(0.0, snapshot.target_offset_us / 1_000_000):.1f}s"

    def _draw_phase_copy(
        self,
        cells: list[list[str]],
        styles: list[list[Style]],
        snapshot: CoachVisualizationSnapshot,
        lane_top: int,
        lane_bottom: int,
    ) -> None:
        copy = {
            "resolving": ("Waiting for a validated lesson", _MUTED),
            "authoring": (
                "Bounded educational approximation in progress · notes appear after validation",
                _MUTED,
            ),
            "scoring": ("SCORING ATTEMPT", _RESULT),
            "cancelling": ("RELEASING HALCYON VOICES", _FAILURE),
        }.get(snapshot.phase)
        if copy is None:
            return
        row = lane_top + max(0, (lane_bottom - lane_top) // 2)
        text, style = copy
        self._write(cells, styles, row, 1, text, style)

    def _draw_review(
        self,
        cells: list[list[str]],
        styles: list[list[Style]],
        snapshot: CoachVisualizationSnapshot,
        lane_top: int,
        lane_bottom: int,
    ) -> None:
        lines = [self._review_score(snapshot)]
        if snapshot.review is not None:
            lines.extend(f"TIP · {tip}" for tip in snapshot.review.tips)
        lines.append("AGAIN · /coach attempt   /coach replay")
        for row, line in enumerate(lines[: lane_bottom - lane_top + 1], start=lane_top):
            self._write(cells, styles, row, 1, line, _RESULT if row == lane_top else _MUTED)

    @staticmethod
    def _review_score(snapshot: CoachVisualizationSnapshot) -> str:
        review = snapshot.review
        if review is None:
            return "REVIEW READY"
        parts = [review.status.upper()]
        if review.overall_score_percent is not None:
            parts.append(f"{review.overall_score_percent:.0f}%")
        if review.matched_note_count is not None and review.expected_note_count is not None:
            parts.append(f"{review.matched_note_count}/{review.expected_note_count} NOTES")
        return " · ".join(parts)

    def _draw_spans(
        self,
        cells: list[list[str]],
        styles: list[list[Style]],
        spans: tuple[HighwayNoteSpan, ...],
        *,
        lane_top: int,
        lane_bottom: int,
        lookahead_us: int,
    ) -> None:
        lane_height = lane_bottom - lane_top + 1
        width = len(cells[0])
        # Ghosts are previews; they must never paint over a required target.
        for span in sorted(spans, key=lambda item: item.required):
            bounds = _KEY_BOUNDS[span.note]
            if bounds is None:
                continue
            key_left, key_right = bounds
            left = max(0, key_left - self._viewport_origin)
            right = min(width, key_right - self._viewport_origin)
            if left >= right:
                continue
            onset_y = self._time_row(span.start_offset_us, lane_top, lane_height, lookahead_us)
            end_y = self._time_row(span.end_offset_us, lane_top, lane_height, lookahead_us)
            top, bottom = sorted((onset_y, end_y))
            if span.required:
                style = _SHARP if is_black_note(span.note) else _NATURAL
                onset = "◆" if span.cue_kind == "halcyon" else "○"
            else:
                style = _GHOST_SHARP if is_black_note(span.note) else _GHOST_NATURAL
                onset = "◇"
            for row in range(top, bottom + 1):
                glyph = onset if row == onset_y else ("┃" if span.required else "┊")
                for column in range(left, right):
                    cells[row][column] = glyph
                    styles[row][column] = style

    @staticmethod
    def _time_row(offset_us: int, lane_top: int, lane_height: int, lookahead_us: int) -> int:
        bounded = min(lookahead_us, max(0, offset_us))
        progress = bounded / max(1, lookahead_us)
        return lane_top + round((lane_height - 1) * (1.0 - progress))

    def _draw_octave_guides(
        self,
        cells: list[list[str]],
        styles: list[list[Style]],
        lane_top: int,
        lane_bottom: int,
    ) -> None:
        width = len(cells[0])
        for note in range(0, 128, 12):
            bounds = _KEY_BOUNDS[note]
            if bounds is None:
                continue
            x = bounds[0] - self._viewport_origin
            if not 0 <= x < width:
                continue
            for row in range(lane_top, lane_bottom + 1):
                cells[row][x] = "┆"
                styles[row][x] = _GRID

    def _draw_octave_labels(
        self,
        cells: list[list[str]],
        styles: list[list[Style]],
        row: int,
    ) -> None:
        width = len(cells[0])
        for note in range(0, 128, 12):
            bounds = _KEY_BOUNDS[note]
            if bounds is None:
                continue
            x = bounds[0] - self._viewport_origin
            if not 0 <= x < width:
                continue
            self._write(cells, styles, row, x, note_name(note), _MUTED)

    def _draw_strike(
        self,
        cells: list[list[str]],
        styles: list[list[Style]],
        row: int,
        *,
        snapshot: CoachVisualizationSnapshot,
    ) -> None:
        pulse = (
            snapshot.phase == "count_in"
            and snapshot.count_in_remaining is not None
            and snapshot.count_in_remaining % 2 == 0
        )
        rail_style = _RESULT if pulse else _GRID
        for column in range(len(cells[row])):
            cells[row][column] = "═"
            styles[row][column] = rail_style

    def _layout(self, width: int, height: int) -> str:
        try:
            if self.screen.has_class("-compact"):
                return "compact"
            if self.screen.has_class("-medium"):
                return "medium"
            if self.screen.has_class("-wide"):
                return "wide"
        except Exception:
            pass
        if width < 70 or height < 8:
            return "compact"
        if width < 110 or height < 16:
            return "medium"
        return "wide"

    @staticmethod
    def _write(
        cells: list[list[str]],
        styles: list[list[Style]],
        row: int,
        column: int,
        text: str,
        style: Style,
    ) -> None:
        if not 0 <= row < len(cells):
            return
        for index, character in enumerate(text):
            x = column + index
            if not 0 <= x < len(cells[row]):
                break
            cells[row][x] = character
            styles[row][x] = style

    @staticmethod
    def _to_strips(
        cells: list[list[str]],
        styles: list[list[Style]],
    ) -> tuple[Strip, ...]:
        strips: list[Strip] = []
        for row_cells, row_styles in zip(cells, styles, strict=True):
            segments: list[Segment] = []
            start = 0
            for index in range(1, len(row_cells) + 1):
                if index == len(row_cells) or row_styles[index] != row_styles[start]:
                    segments.append(Segment("".join(row_cells[start:index]), row_styles[start]))
                    start = index
            strips.append(Strip(segments, len(row_cells)))
        return tuple(strips)

    def _invalidate(self) -> None:
        self._cache_key = None
        self._cached_rows = ()
        self.refresh()

    @staticmethod
    def _inactive_snapshot() -> CoachVisualizationSnapshot:
        return CoachVisualizationSnapshot(active=False, phase="idle", sampled_at_ns=0)


__all__ = ["CoachNoteHighway"]
