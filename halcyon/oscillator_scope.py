"""Live Textual oscillator visualization built on Halcyon's Braille raster."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from rich.segment import Segment
from textual.strip import Strip
from textual.widget import Widget

from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.braille_canvas import (
    BrailleCanvas,
    orbit_geometry,
    radial_geometry,
    wave_geometry,
)

SnapshotProvider = Callable[[], AudioVisualizationSnapshot]


class ScopeMode(StrEnum):
    """Available sample-driven oscillator projections."""

    WAVE = "wave"
    ORBIT = "orbit"
    RADIAL = "radial"


class OscillatorScope(Widget):
    """Render immutable audio snapshots as animated Braille geometry."""

    DEFAULT_CSS = """
    OscillatorScope {
        width: 100%;
        height: 100%;
        color: $accent;
        background: $surface;
    }
    OscillatorScope:focus {
        color: $accent-lighten-2;
    }
    """

    def __init__(
        self,
        snapshot_provider: SnapshotProvider,
        *,
        mode: ScopeMode | str = ScopeMode.WAVE,
        refresh_hz: float = 24.0,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.snapshot_provider = snapshot_provider
        self.mode = ScopeMode(mode)
        self.refresh_hz = max(1.0, min(float(refresh_hz), 60.0))
        self._snapshot = self._disconnected_snapshot()
        self._cache_key: tuple[object, ...] | None = None
        self._cached_rows: tuple[Strip, ...] = ()

    def on_mount(self) -> None:
        self._poll_snapshot()
        self.set_interval(1.0 / self.refresh_hz, self._poll_snapshot)

    def _poll_snapshot(self) -> None:
        try:
            snapshot = self.snapshot_provider()
        except Exception:
            snapshot = self._disconnected_snapshot()
        if (
            snapshot.frame_count != self._snapshot.frame_count
            or snapshot.connected != self._snapshot.connected
        ):
            self._snapshot = snapshot
            self._invalidate_scope()
        else:
            self._snapshot = snapshot

    def set_snapshot(self, snapshot: AudioVisualizationSnapshot) -> None:
        """Inject a snapshot directly for deterministic tests or offline callers."""
        if (
            snapshot.frame_count != self._snapshot.frame_count
            or snapshot.connected != self._snapshot.connected
        ):
            self._snapshot = snapshot
            self._invalidate_scope()
        else:
            self._snapshot = snapshot

    def set_mode(self, mode: ScopeMode | str) -> None:
        """Select a projection and invalidate all rendered rows."""
        selected = ScopeMode(mode)
        if selected != self.mode:
            self.mode = selected
            self._invalidate_scope()

    def render_line(self, y: int) -> Strip:
        width, height = max(0, self.size.width), max(0, self.size.height)
        if width == 0 or height == 0 or not 0 <= y < height:
            return Strip.blank(width, self.rich_style)
        self._ensure_cache(width, height)
        return self._cached_rows[y]

    def _ensure_cache(self, width: int, height: int) -> None:
        snapshot = self._snapshot
        key = (
            snapshot.frame_count,
            snapshot.connected,
            width,
            height,
            self.mode,
            repr(self.rich_style),
        )
        if key == self._cache_key:
            return
        self._cache_key = key
        if not snapshot.connected:
            self._cached_rows = self._status_rows(width, height, "VIZ DISCONNECTED")
            return
        if not snapshot.samples:
            self._cached_rows = self._status_rows(width, height, "VIZ IDLE")
            return

        canvas = BrailleCanvas(width, height)
        pixel_width, pixel_height = canvas.pixel_width, canvas.pixel_height
        if self.mode is ScopeMode.WAVE:
            points = wave_geometry(snapshot.samples, pixel_width, pixel_height)
        elif self.mode is ScopeMode.ORBIT:
            points = orbit_geometry(snapshot.samples, pixel_width, pixel_height)
        else:
            points = radial_geometry(snapshot.samples, pixel_width, pixel_height)
        canvas.polyline(points)
        self._cached_rows = tuple(
            Strip([Segment(canvas.row_text(row), self.rich_style)], width) for row in range(height)
        )

    def _status_rows(self, width: int, height: int, message: str) -> tuple[Strip, ...]:
        label = message[:width]
        label_row = height // 2
        left = max(0, (width - len(label)) // 2)
        rows = []
        for row in range(height):
            text = (" " * left + label).ljust(width) if row == label_row else " " * width
            rows.append(Strip([Segment(text, self.rich_style)], width))
        return tuple(rows)

    def _invalidate_scope(self) -> None:
        self._cache_key = None
        self._cached_rows = ()
        self.refresh()

    @staticmethod
    def _disconnected_snapshot() -> AudioVisualizationSnapshot:
        return AudioVisualizationSnapshot(
            socket_path="",
            sample_count=0,
            connected=False,
            frame_count=0,
            samples=(),
        )
