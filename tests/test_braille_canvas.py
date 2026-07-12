import math

import pytest

from halcyon.braille_canvas import (
    BrailleCanvas,
    finite_samples,
    orbit_geometry,
    radial_geometry,
    wave_geometry,
)


@pytest.mark.parametrize(
    ("point", "mask"),
    [
        ((0, 0), 0x01),
        ((0, 1), 0x02),
        ((0, 2), 0x04),
        ((1, 0), 0x08),
        ((1, 1), 0x10),
        ((1, 2), 0x20),
        ((0, 3), 0x40),
        ((1, 3), 0x80),
    ],
)
def test_each_logical_pixel_maps_to_the_standard_braille_bit(
    point: tuple[int, int], mask: int
) -> None:
    canvas = BrailleCanvas(1, 1)
    canvas.point(*point)

    assert canvas.mask_at(0, 0) == mask
    assert canvas.character_at(0, 0) == chr(0x2800 + mask)


def test_empty_and_full_cells_have_stable_encoding() -> None:
    canvas = BrailleCanvas(1, 1)
    assert canvas.character_at(0, 0) == "⠀"

    for y in range(4):
        for x in range(2):
            canvas.point(x, y)

    assert canvas.mask_at(0, 0) == 0xFF
    assert canvas.character_at(0, 0) == "⣿"


def test_points_and_lines_clip_without_large_offscreen_walks() -> None:
    canvas = BrailleCanvas(2, 1)
    canvas.point(-1, -1)
    canvas.point(float("nan"), 0)
    canvas.line((-1_000_000, 1), (1_000_000, 1))

    assert canvas.rows_text() == ("⠒⠒",)


def test_negative_coordinates_and_fully_external_line_are_ignored() -> None:
    canvas = BrailleCanvas(2, 2)
    canvas.line((-5, -5), (-2, -1))

    assert canvas.rows_text() == ("⠀⠀", "⠀⠀")


def test_degenerate_line_circle_and_polyline_are_deterministic() -> None:
    first = BrailleCanvas(2, 2)
    second = BrailleCanvas(2, 2)
    for canvas in (first, second):
        canvas.line((1, 1), (1, 1))
        canvas.circle((2, 4), 0)
        canvas.circle((2, 4), -1)
        canvas.polyline([(3, 7)])

    assert first.rows_text() == second.rows_text()
    assert any(mask for row in range(2) for mask in first._cells[row])


def test_circle_and_polar_paths_stay_within_the_canvas() -> None:
    canvas = BrailleCanvas(5, 3)
    canvas.circle((5, 6), 4)
    canvas.polar((5, 6), [4, float("nan"), 5, 3])

    assert len(canvas.rows_text()) == 3
    assert all(len(row) == 5 for row in canvas.rows_text())


def test_finite_samples_defends_against_invalid_values_and_clipping() -> None:
    values = finite_samples((float("nan"), float("inf"), -4.0, 2.0, 0.0))

    assert values == (0.0, 0.0, -1.0, 0.5, 0.0)
    assert finite_samples((0.0, 0.0)) == (0.0, 0.0)


def test_wave_geometry_forms_a_time_ordered_sine_wave() -> None:
    samples = tuple(math.sin(math.tau * index / 32) for index in range(32))
    points = wave_geometry(samples, 80, 32)

    assert points == wave_geometry(samples, 80, 32)
    assert all(a[0] < b[0] for a, b in zip(points, points[1:], strict=False))
    assert min(y for _, y in points) < 5
    assert max(y for _, y in points) > 26


def test_lagged_sine_forms_a_closed_orbit() -> None:
    samples = tuple(math.sin(math.tau * index / 64) for index in range(65))
    points = orbit_geometry(samples, 80, 40, lag=16)

    assert points[0] == points[-1]
    assert max(x for x, _ in points) - min(x for x, _ in points) > 60
    assert max(y for _, y in points) - min(y for _, y in points) > 30


def test_radial_geometry_is_closed_and_bounded() -> None:
    samples = tuple(math.sin(math.tau * index / 24) for index in range(24))
    points = radial_geometry(samples, 40, 20)

    assert points[0] == points[-1]
    assert all(0 <= x < 40 and 0 <= y < 20 for x, y in points)


@pytest.mark.parametrize("geometry", [wave_geometry, orbit_geometry, radial_geometry])
def test_geometry_handles_empty_small_and_zero_size_inputs(geometry) -> None:  # type: ignore[no-untyped-def]
    assert geometry((), 20, 10) == ()
    assert geometry((0.5,), 0, 10) == ()
    assert geometry((float("nan"), float("inf")), 0, 0) == ()
