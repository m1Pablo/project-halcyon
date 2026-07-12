"""Bounds-safe Unicode Braille raster and oscillator geometry helpers."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

Point = tuple[float, float]

_DOT_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)


class BrailleCanvas:
    """A terminal-cell canvas backed by a 2-by-4 logical-pixel dot plane."""

    def __init__(self, columns: int, rows: int) -> None:
        self.columns = max(0, int(columns))
        self.rows = max(0, int(rows))
        self.pixel_width = self.columns * 2
        self.pixel_height = self.rows * 4
        self._cells = [[0 for _ in range(self.columns)] for _ in range(self.rows)]

    def point(self, x: float, y: float) -> None:
        """Set one logical pixel, silently clipping non-finite/out-of-bounds input."""
        if not math.isfinite(x) or not math.isfinite(y):
            return
        pixel_x, pixel_y = round(x), round(y)
        if not (0 <= pixel_x < self.pixel_width and 0 <= pixel_y < self.pixel_height):
            return
        cell_x, dot_x = divmod(pixel_x, 2)
        cell_y, dot_y = divmod(pixel_y, 4)
        self._cells[cell_y][cell_x] |= _DOT_BITS[dot_y][dot_x]

    def line(self, start: Point, end: Point) -> None:
        """Rasterize a clipped line, including a degenerate one-point line."""
        clipped = _clip_line(start, end, self.pixel_width, self.pixel_height)
        if clipped is None:
            return
        x0, y0, x1, y1 = (round(value) for point in clipped for value in point)
        dx = abs(x1 - x0)
        step_x = 1 if x0 < x1 else -1
        dy = -abs(y1 - y0)
        step_y = 1 if y0 < y1 else -1
        error = dx + dy
        while True:
            self.point(x0, y0)
            if x0 == x1 and y0 == y1:
                break
            doubled = error * 2
            if doubled >= dy:
                error += dy
                x0 += step_x
            if doubled <= dx:
                error += dx
                y0 += step_y

    def polyline(self, points: Iterable[Point], *, closed: bool = False) -> None:
        """Rasterize adjoining line segments from an iterable of points."""
        vertices = tuple(points)
        if not vertices:
            return
        if len(vertices) == 1:
            self.point(*vertices[0])
            return
        for start, end in zip(vertices, vertices[1:], strict=False):
            self.line(start, end)
        if closed:
            self.line(vertices[-1], vertices[0])

    def circle(self, center: Point, radius: float, *, steps: int | None = None) -> None:
        """Rasterize a circle; a zero radius is a single point."""
        if not all(math.isfinite(value) for value in (*center, radius)) or radius < 0:
            return
        if radius == 0:
            self.point(*center)
            return
        count = max(12, int(steps if steps is not None else math.ceil(radius * 6)))
        points = tuple(
            (
                center[0] + radius * math.cos(math.tau * index / count),
                center[1] + radius * math.sin(math.tau * index / count),
            )
            for index in range(count)
        )
        self.polyline(points, closed=True)

    def polar(
        self,
        center: Point,
        radii: Sequence[float],
        *,
        x_scale: float = 1.0,
        y_scale: float = 1.0,
    ) -> None:
        """Rasterize a closed polar path from finite, non-negative radii."""
        if not radii:
            return
        count = len(radii)
        points = []
        for index, radius in enumerate(radii):
            if not math.isfinite(radius):
                continue
            angle = math.tau * index / count
            radius = max(0.0, radius)
            points.append(
                (
                    center[0] + math.cos(angle) * radius * x_scale,
                    center[1] + math.sin(angle) * radius * y_scale,
                )
            )
        self.polyline(points, closed=len(points) > 1)

    def mask_at(self, column: int, row: int) -> int:
        """Return a cell's eight-dot mask, or zero outside the canvas."""
        if 0 <= column < self.columns and 0 <= row < self.rows:
            return self._cells[row][column]
        return 0

    def character_at(self, column: int, row: int) -> str:
        """Encode one cell as a Unicode Braille character."""
        return chr(0x2800 + self.mask_at(column, row))

    def row_text(self, row: int) -> str:
        """Return one terminal-cell row with stable canvas width."""
        if not 0 <= row < self.rows:
            return " " * self.columns
        return "".join(self.character_at(column, row) for column in range(self.columns))

    def rows_text(self) -> tuple[str, ...]:
        """Return every encoded terminal row."""
        return tuple(self.row_text(row) for row in range(self.rows))


def finite_samples(samples: Sequence[float]) -> tuple[float, ...]:
    """Return finite samples normalized to [-1, 1], preserving silence."""
    cleaned = tuple(float(value) if math.isfinite(value) else 0.0 for value in samples)
    peak = max((abs(value) for value in cleaned), default=0.0)
    if peak <= 1e-12:
        return tuple(0.0 for _ in cleaned)
    scale = max(1.0, peak)
    return tuple(max(-1.0, min(1.0, value / scale)) for value in cleaned)


def wave_geometry(samples: Sequence[float], width: int, height: int) -> tuple[Point, ...]:
    """Map samples to a left-to-right time-domain polyline."""
    width, height = max(0, int(width)), max(0, int(height))
    values = finite_samples(samples)
    if width == 0 or height == 0 or not values:
        return ()
    center = (height - 1) / 2
    amplitude = max(0.0, center - 0.5)
    denominator = max(1, len(values) - 1)
    return tuple(
        (
            (width - 1) * index / denominator,
            center - value * amplitude,
        )
        for index, value in enumerate(values)
    )


def orbit_geometry(
    samples: Sequence[float], width: int, height: int, *, lag: int | None = None
) -> tuple[Point, ...]:
    """Map a signal and lagged copy to a centered phase portrait."""
    width, height = max(0, int(width)), max(0, int(height))
    values = finite_samples(samples)
    if width == 0 or height == 0 or len(values) < 2:
        return ()
    offset = lag if lag is not None else max(1, len(values) // 4)
    offset = max(1, min(int(offset), len(values) - 1))
    center_x, center_y = (width - 1) / 2, (height - 1) / 2
    extent_x, extent_y = max(0.0, center_x - 0.5), max(0.0, center_y - 0.5)
    points = [
        (center_x + values[index] * extent_x, center_y - values[index - offset] * extent_y)
        for index in range(offset, len(values))
    ]
    if len(points) > 1:
        points.append(points[0])
    return tuple(points)


def radial_geometry(samples: Sequence[float], width: int, height: int) -> tuple[Point, ...]:
    """Map amplitude around a bounded, centered polar loop."""
    width, height = max(0, int(width)), max(0, int(height))
    values = finite_samples(samples)
    if width == 0 or height == 0 or not values:
        return ()
    center_x, center_y = (width - 1) / 2, (height - 1) / 2
    extent_x, extent_y = max(0.0, center_x - 0.5), max(0.0, center_y - 0.5)
    points = []
    for index, value in enumerate(values):
        angle = math.tau * index / len(values) - math.pi / 2
        radius = 0.62 + 0.32 * value
        points.append(
            (
                center_x + math.cos(angle) * extent_x * radius,
                center_y + math.sin(angle) * extent_y * radius,
            )
        )
    if len(points) > 1:
        points.append(points[0])
    return tuple(points)


def _clip_line(start: Point, end: Point, width: int, height: int) -> tuple[Point, Point] | None:
    """Clip a segment to raster bounds with the Liang-Barsky algorithm."""
    if width <= 0 or height <= 0 or not all(math.isfinite(value) for value in (*start, *end)):
        return None
    x0, y0 = start
    dx, dy = end[0] - x0, end[1] - y0
    lower, upper = 0.0, 1.0
    for p, q in (
        (-dx, x0),
        (dx, (width - 1) - x0),
        (-dy, y0),
        (dy, (height - 1) - y0),
    ):
        if p == 0:
            if q < 0:
                return None
            continue
        ratio = q / p
        if p < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return None
    return ((x0 + lower * dx, y0 + lower * dy), (x0 + upper * dx, y0 + upper * dy))
