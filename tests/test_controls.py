import asyncio

import pytest

from halcyon.app import HalcyonApp
from halcyon.audio_bridge import AudioBridgeSnapshot
from halcyon.controls import SynthKnob
from halcyon.preset_store import UserPresetStore


class FakeAudio:
    def __init__(self) -> None:
        self.patches: list[dict[str, float]] = []
        self.all_notes_off_count = 0

    def start(self) -> bool:
        return True

    def apply_patch(self, patch: dict[str, float]) -> bool:
        self.patches.append(dict(patch))
        return True

    def snapshot(self) -> AudioBridgeSnapshot:
        return AudioBridgeSnapshot(running=True, ready=True, text="Audio: fake ready")

    def all_notes_off(self) -> bool:
        self.all_notes_off_count += 1
        return True

    def handle_midi_event(self, event) -> bool:  # type: ignore[no-untyped-def]
        return True

    def close(self) -> None:
        pass


class FakeMidi:
    connected = False

    def list_ports(self) -> list[str]:
        return ["Akai LPK25 Wireless"]

    def connect(self) -> str:
        return "Akai LPK25 Wireless"

    def poll(self) -> list[object]:
        return []

    def close(self) -> None:
        pass


def test_knob_keypress_updates_patch_and_streams_to_audio(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def run() -> None:
        app = HalcyonApp()
        fake_audio = FakeAudio()
        app.audio = fake_audio  # type: ignore[assignment]
        app.midi = FakeMidi()  # type: ignore[assignment]

        async with app.run_test() as pilot:
            tone = app.query_one("#knob-tone", SynthKnob)
            tone.focus()
            before = len(fake_audio.patches)
            await pilot.press("right")
            await pilot.pause()

            assert len(fake_audio.patches) == before + 1
            assert "cutoff_hz" in fake_audio.patches[-1]
            assert app.synth_patch.preset_name is None

    asyncio.run(run())


def test_knob_buttons_increment_and_decrement(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def run() -> None:
        app = HalcyonApp()
        fake_audio = FakeAudio()
        app.audio = fake_audio  # type: ignore[assignment]
        app.midi = FakeMidi()  # type: ignore[assignment]

        async with app.run_test(size=(180, 52)) as pilot:
            before = app.synth_patch.values["volume"]
            await pilot.click("#knob-volume-up")
            await pilot.pause()
            after_up = app.synth_patch.values["volume"]
            await pilot.click("#knob-volume-down")
            await pilot.pause()

            assert after_up > before
            assert app.synth_patch.values["volume"] == pytest.approx(before)

    asyncio.run(run())


def test_chat_input_accepts_text_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def run() -> None:
        app = HalcyonApp()
        app.audio = FakeAudio()  # type: ignore[assignment]
        app.midi = FakeMidi()  # type: ignore[assignment]

        async with app.run_test(size=(140, 48)) as pilot:
            app.chat_panel.input.value = "make it warmer"
            await pilot.click("#chat-send")
            await pilot.pause()

            assert not app.chat_panel.input.disabled
            assert app.chat_panel.input.value == ""

    asyncio.run(run())


def test_user_preset_slot_saves_current_patch(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def run() -> None:
        store = UserPresetStore(path=tmp_path / "presets.json")
        app = HalcyonApp(preset_store=store)
        app.audio = FakeAudio()  # type: ignore[assignment]
        app.midi = FakeMidi()  # type: ignore[assignment]

        async with app.run_test(size=(180, 60)) as pilot:
            app.synth_rack.apply_patch({"tone": 0.21, "release": 0.42}, emit=False)
            await pilot.click("#user-preset-0")
            await pilot.pause()

            assert store.presets["User 1"]["tone"] == 0.21
            assert store.presets["User 1"]["release"] == 0.42
            assert store.path.exists()

            app.synth_rack.apply_patch({"tone": 0.8, "release": 0.1}, emit=False)
            app.synth_tool_apply_preset("User 1")

            assert app.synth_patch.values["tone"] == 0.21
            assert app.synth_patch.values["release"] == 0.42

    asyncio.run(run())
