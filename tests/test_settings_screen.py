import asyncio
from pathlib import Path

from halcyon.app import HalcyonApp
from halcyon.audio_bridge import AudioBridgeSnapshot
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.midi import MidiEvent
from halcyon.preset_store import UserPresetStore
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widget import Widget


class TrackingAudio:
    def __init__(self) -> None:
        self.patches: list[dict[str, float]] = []

    def start(self) -> bool:
        return True

    def apply_patch(self, patch: dict[str, float]) -> bool:
        self.patches.append(dict(patch))
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


class CountingStore(UserPresetStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path=path)
        self.save_calls = 0

    def save(self, name: str, values: dict[str, float]) -> dict[str, float]:
        self.save_calls += 1
        return super().save(name, values)


def app_with_fakes(*, preset_store: UserPresetStore | None = None) -> HalcyonApp:
    app = HalcyonApp(preset_store=preset_store)
    app.audio = TrackingAudio()  # type: ignore[assignment]
    app.midi = FakeMidi()  # type: ignore[assignment]
    return app


async def click_visible(pilot: Pilot, screen: Screen, selector: str) -> Widget:
    widget = screen.query_one(selector)
    widget.scroll_visible(animate=False, immediate=True)
    await pilot.pause()
    assert await pilot.click(widget)
    return widget


def test_four_modal_cycles_preserve_identity_state_focus_and_scope_mode() -> None:
    async def run() -> None:
        for size in ((180, 55), (120, 42)):
            app = app_with_fakes()
            modal_identity = id(app.settings_screen)
            rack_identity = id(app.synth_rack)
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                base_screen = app.screen
                scope_mode = app.scope.mode
                scope_point = (
                    app.scope.region.x + 2,
                    app.scope.region.y + 1,
                )

                for _cycle in range(4):
                    app.chat_panel.input.focus()
                    await pilot.press("f2")
                    await pilot.pause()

                    assert app.screen is app.settings_screen
                    assert len(app.screen_stack) == 2
                    assert app.settings_screen.focused is not None
                    assert app.settings_screen.focused.screen is app.settings_screen

                    await pilot.press("f2")
                    await pilot.press("comma")
                    await pilot.press("ctrl+s")
                    assert app.screen is app.settings_screen
                    assert len(app.screen_stack) == 2

                    await click_visible(pilot, app.settings_screen, "#preset-pluck")
                    await click_visible(pilot, app.settings_screen, "#knob-tone-up")
                    await pilot.click(offset=(size[0] - 3, size[1] // 2))
                    await pilot.pause()
                    assert app.screen is app.settings_screen
                    assert app.synth_rack.selected_preset == "Pluck"

                    await pilot.click(offset=scope_point)
                    await pilot.pause()
                    assert app.screen is base_screen
                    assert len(app.screen_stack) == 1
                    assert app.scope.mode is scope_mode
                    assert app.chat_panel.input.has_focus
                    assert id(app.settings_screen) == modal_identity
                    assert id(app.synth_rack) == rack_identity
                    assert app.settings_screen.synth_rack is app.synth_rack

    asyncio.run(run())


def test_modal_escape_close_and_compact_internal_interactions_are_deterministic() -> None:
    async def run() -> None:
        app = app_with_fakes()
        async with app.run_test(size=(90, 30)) as pilot:
            base_screen = app.screen
            app.chat_panel.input.focus()
            await pilot.press("f2")
            await pilot.pause()
            assert app.screen is app.settings_screen
            assert app.settings_screen.has_class("-compact")
            assert app.settings_screen.query_one("#settings-drawer").region.width == 90

            await click_visible(pilot, app.settings_screen, "#preset-save")
            await click_visible(pilot, app.settings_screen, "#preset-name-input")
            await pilot.press("d", "r", "a", "f", "t")
            assert app.screen is app.settings_screen
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is base_screen
            assert app.chat_panel.input.has_focus

            await pilot.press("f2")
            await pilot.pause()
            assert app.settings_screen.query_one("#preset-save-flow").display
            assert app.settings_screen.query_one("#preset-name-input").value == "draft"
            await pilot.click("#close-settings")
            await pilot.pause()
            assert app.screen is base_screen
            assert app.chat_panel.input.has_focus

    asyncio.run(run())


def test_modal_messages_reach_existing_app_handlers_exactly_once(tmp_path: Path) -> None:
    async def run() -> None:
        store = CountingStore(tmp_path / "presets.json")
        app = app_with_fakes(preset_store=store)
        audio = app.audio
        assert isinstance(audio, TrackingAudio)

        async with app.run_test(size=(180, 55)) as pilot:
            await pilot.press("comma")
            await pilot.pause()
            assert app.screen is app.settings_screen

            patch_count = len(audio.patches)
            await click_visible(pilot, app.settings_screen, "#knob-tone-up")
            await pilot.pause()
            assert len(audio.patches) == patch_count + 1

            await click_visible(pilot, app.settings_screen, "#preset-pluck")
            await click_visible(pilot, app.settings_screen, "#preset-load")
            await pilot.pause()
            assert len(audio.patches) == patch_count + 2

            await click_visible(pilot, app.settings_screen, "#preset-save")
            name_input = app.settings_screen.query_one("#preset-name-input")
            name_input.value = "Modal Lead"
            await click_visible(pilot, app.settings_screen, "#preset-save-confirm")
            await pilot.pause()
            assert store.save_calls == 1
            assert store.presets["Modal Lead"] == app.synth_patch.values

            screen_identity = id(app.settings_screen)
            rack_identity = id(app.synth_rack)
            await pilot.press("escape")
            await pilot.press("f2")
            await pilot.pause()
            assert id(app.settings_screen) == screen_identity
            assert id(app.synth_rack) == rack_identity
            assert app.synth_patch.preset_name == "Modal Lead"
            assert app.synth_rack.selected_preset == "Modal Lead"

    asyncio.run(run())


def test_backdrop_over_keyboard_and_console_dismisses_without_underlying_input() -> None:
    async def run() -> None:
        for target_id in ("keyboard-scroll", "command-console"):
            app = app_with_fakes()
            async with app.run_test(size=(180, 55)) as pilot:
                await pilot.pause()
                target = app.query_one(f"#{target_id}")
                target_point = (target.region.x + 2, target.region.y + 1)
                app.chat_panel.input.focus()
                await pilot.press("f2")
                await pilot.pause()
                await pilot.click(offset=target_point)
                await pilot.pause()
                assert app.screen is not app.settings_screen
                assert app.chat_panel.input.has_focus

    asyncio.run(run())
