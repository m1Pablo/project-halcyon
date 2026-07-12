import asyncio

from halcyon.app import HalcyonApp
from halcyon.audio_bridge import AudioBridgeSnapshot
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.midi import MidiEvent
from halcyon.oscillator_scope import ScopeMode


class FakeAudio:
    def start(self) -> bool:
        return True

    def apply_patch(self, patch: dict[str, float]) -> bool:
        return True

    def snapshot(self) -> AudioBridgeSnapshot:
        return AudioBridgeSnapshot(running=True, ready=True, text="Audio: ready")

    def visualization_snapshot(self) -> AudioVisualizationSnapshot:
        return AudioVisualizationSnapshot(
            socket_path="/tmp/halcyon-test.sock",
            sample_count=4,
            connected=True,
            frame_count=1,
            samples=(0.0, 0.5, 0.0, -0.5),
        )

    def all_notes_off(self) -> bool:
        return True

    def handle_midi_event(self, event: MidiEvent) -> bool:
        return True

    def close(self) -> None:
        pass


class FakeMidi:
    connected = False

    def list_ports(self) -> list[str]:
        return ["Akai LPK25 Wireless"]

    def connect(self) -> str:
        return "Akai LPK25 Wireless"

    def poll(self) -> list[MidiEvent]:
        return []

    def close(self) -> None:
        pass


def cockpit() -> HalcyonApp:
    app = HalcyonApp()
    app.audio = FakeAudio()  # type: ignore[assignment]
    app.midi = FakeMidi()  # type: ignore[assignment]
    return app


def test_runtime_size_classes_and_non_overlapping_wide_medium_layouts() -> None:
    async def run() -> None:
        for size, expected in (((180, 55), "-wide"), ((120, 42), "-medium")):
            app = cockpit()
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                assert app.screen.has_class(expected)
                panes = [
                    app.query_one("#performance-stage"),
                    app.query_one("#activity-panel"),
                ]
                assert all(pane.display and pane.region.width > 0 for pane in panes)
                for index, pane in enumerate(panes):
                    for other in panes[index + 1 :]:
                        assert not pane.region.overlaps(other.region)
                console = app.query_one("#command-console")
                assert console.display and console.region.height >= 8

    asyncio.run(run())


def test_compact_workspace_navigation_is_deterministic_and_reachable() -> None:
    async def run() -> None:
        app = cockpit()
        async with app.run_test(size=(90, 30)) as pilot:
            assert app.screen.has_class("-compact")
            assert app.query_one("#performance-stage").display

            await pilot.press("f2")
            await pilot.pause()
            assert app.screen is app.settings_screen
            assert app.settings_screen.has_class("-compact")
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not app.settings_screen

            await pilot.press("f3")
            assert app.query_one("#activity-panel").display

            await pilot.press("f4")
            assert app.query_one("#command-console").display
            assert app.query_one("#chat-input").region.height > 0

            await pilot.press("f1")
            assert app.query_one("#performance-stage").display

    asyncio.run(run())


def test_scope_controls_and_complete_midi_activity_update() -> None:
    async def run() -> None:
        app = cockpit()
        async with app.run_test(size=(180, 55)) as pilot:
            initial_mode = app.scope.mode

            for _ in range(3):
                await pilot.click("#oscillator-scope")
                assert app.scope.mode is initial_mode

            await pilot.click("#scope-orbit")
            assert app.scope.mode is ScopeMode.ORBIT
            await pilot.click("#scope-radial")
            assert app.scope.mode is ScopeMode.RADIAL
            await pilot.click("#scope-wave")
            assert app.scope.mode is ScopeMode.WAVE

            event = MidiEvent(
                event_type="press",
                note=127,
                note_name="G9",
                velocity=101,
                channel=2,
                status=0x91,
                raw_message=(0x91, 127, 101),
                delta_time=0.0123,
                source="Akai LPK25 Wireless",
                elapsed=1.25,
            )
            app._handle_event(event)
            await pilot.pause()

            assert app.event_count == 1
            assert app.event_table.columns.keys()
            assert [column.label.plain for column in app.event_table.columns.values()] == [
                "Time",
                "Event",
                "Note",
                "Vel",
                "Ch",
                "Status",
                "Delta",
                "Source",
            ]
            assert app.keyboard_scroll.scroll_x > 0
            assert "EVENTS 1" in str(app.status_rail.render())

    asyncio.run(run())
