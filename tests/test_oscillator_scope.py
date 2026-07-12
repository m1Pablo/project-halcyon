import math

import pytest
from textual.app import App, ComposeResult

from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.oscillator_scope import OscillatorScope, ScopeMode


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def snapshot(
    *,
    connected: bool = True,
    frame_count: int = 1,
    samples: tuple[float, ...] = (0.0, 0.5, 0.0, -0.5),
) -> AudioVisualizationSnapshot:
    return AudioVisualizationSnapshot(
        socket_path="/tmp/test-viz.sock",
        sample_count=len(samples),
        connected=connected,
        frame_count=frame_count,
        samples=samples,
    )


class ScopeApp(App[None]):
    def __init__(self, provider) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self.scope = OscillatorScope(provider, id="scope")

    def compose(self) -> ComposeResult:
        yield self.scope


def strip_text(scope: OscillatorScope) -> tuple[str, ...]:
    return tuple(scope.render_line(row).text for row in range(scope.size.height))


@pytest.mark.anyio
async def test_new_frame_changes_rows_and_identical_snapshot_reuses_cache() -> None:
    current = [snapshot(samples=tuple(0.0 for _ in range(32)))]
    app = ScopeApp(lambda: current[0])

    async with app.run_test(size=(30, 8)) as pilot:
        scope = app.scope
        scope._poll_snapshot()
        first = scope.render_line(0)
        cached_rows = scope._cached_rows

        scope._poll_snapshot()
        assert scope.render_line(0) is first
        assert scope._cached_rows is cached_rows

        current[0] = snapshot(
            frame_count=2,
            samples=tuple(math.sin(math.tau * index / 32) for index in range(32)),
        )
        scope._poll_snapshot()
        changed = strip_text(scope)
        await pilot.pause()

        assert scope._cached_rows is not cached_rows
        assert changed != tuple(row.text for row in cached_rows)


@pytest.mark.anyio
async def test_resize_style_and_mode_changes_invalidate_cached_rows() -> None:
    app = ScopeApp(lambda: snapshot())

    async with app.run_test(size=(30, 8)) as pilot:
        scope = app.scope
        scope._poll_snapshot()
        scope.render_line(0)
        initial = scope._cached_rows

        await pilot.resize_terminal(42, 12)
        resized = scope.render_line(0)
        assert resized.cell_length == scope.size.width
        assert scope._cached_rows is not initial

        after_resize = scope._cached_rows
        scope.styles.color = "red"
        scope.render_line(0)
        assert scope._cached_rows is not after_resize

        after_style = scope._cached_rows
        scope.set_mode(ScopeMode.ORBIT)
        scope.render_line(0)
        assert scope._cached_rows is not after_style


@pytest.mark.anyio
async def test_disconnected_idle_invalid_and_zero_size_states_are_safe() -> None:
    app = ScopeApp(lambda: snapshot(connected=False, samples=()))

    async with app.run_test(size=(24, 7)):
        scope = app.scope
        scope._poll_snapshot()
        rows = strip_text(scope)
        assert any("VIZ DISCONNECTED" in row for row in rows)
        assert all(len(row) == scope.size.width for row in rows)

        scope.set_snapshot(snapshot(samples=(float("nan"), float("inf"), 0.0)))
        assert len(strip_text(scope)) == scope.size.height
        assert scope.render_line(-1).cell_length == scope.size.width

    unmounted = OscillatorScope(lambda: snapshot())
    assert unmounted.render_line(0).cell_length == 0


@pytest.mark.anyio
async def test_keyboard_and_mouse_input_are_inert_but_set_mode_remains_available() -> None:
    app = ScopeApp(lambda: snapshot())

    async with app.run_test(size=(30, 8)) as pilot:
        scope = app.scope
        assert scope.mode is ScopeMode.WAVE
        assert not scope.can_focus

        await pilot.press("space")
        await pilot.press("right")
        await pilot.press("3")
        assert scope.mode is ScopeMode.WAVE

        clicked = await pilot.click(scope)
        assert clicked
        assert scope.mode is ScopeMode.WAVE

        scope.set_mode(ScopeMode.RADIAL)
        assert scope.mode is ScopeMode.RADIAL


@pytest.mark.anyio
async def test_provider_failure_falls_back_to_disconnected_state() -> None:
    def broken_provider() -> AudioVisualizationSnapshot:
        raise OSError("reader unavailable")

    app = ScopeApp(broken_provider)
    async with app.run_test(size=(22, 6)):
        rows = strip_text(app.scope)

    assert any("VIZ DISCONNECTED" in row for row in rows)
