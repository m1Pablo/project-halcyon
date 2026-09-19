import asyncio
from pathlib import Path

from halcyon.app import HalcyonApp
from halcyon.audio_bridge import AudioBridgeSnapshot
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.midi import MidiEvent
from halcyon.preset_store import UserPresetStore
from halcyon.settings_screen import SecretInput, SoundSettingsScreen
from textual.message import Message
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Input, Static


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


class CredentialCaptureApp(HalcyonApp):
    """Capture payload-free UI requests without touching any credential backend."""

    CSS_PATH = str(Path(__file__).parents[1] / "halcyon" / "styles.tcss")

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.saved_openai_keys: list[str | None] = []
        self.save_messages: list[SoundSettingsScreen.SaveOpenAIKeyRequested] = []
        self.delete_messages: list[SoundSettingsScreen.DeleteOpenAIKeyRequested] = []
        self.stock_secret_messages: list[Message] = []

    def on_sound_settings_screen_save_open_aikey_requested(
        self,
        message: SoundSettingsScreen.SaveOpenAIKeyRequested,
    ) -> None:
        message.stop()
        self.save_messages.append(message)
        self.saved_openai_keys.append(self.settings_screen.take_openai_key())

    def on_sound_settings_screen_delete_open_aikey_requested(
        self,
        message: SoundSettingsScreen.DeleteOpenAIKeyRequested,
    ) -> None:
        message.stop()
        self.delete_messages.append(message)

    def on_input_changed(self, message: Input.Changed) -> None:
        if message.input is self.settings_screen.openai_key_input:
            self.stock_secret_messages.append(message)

    def on_input_submitted(self, message: Input.Submitted) -> None:
        if message.input is self.settings_screen.openai_key_input:
            self.stock_secret_messages.append(message)

    def on_input_blurred(self, message: Input.Blurred) -> None:
        if message.input is self.settings_screen.openai_key_input:
            self.stock_secret_messages.append(message)


def app_with_fakes(
    *,
    preset_store: UserPresetStore | None = None,
    app_type: type[HalcyonApp] = HalcyonApp,
) -> HalcyonApp:
    app = app_type(preset_store=preset_store)
    app.audio = TrackingAudio()  # type: ignore[assignment]
    app.midi = FakeMidi()  # type: ignore[assignment]
    if hasattr(app, "_start_openai_credential_operation"):
        app._start_openai_credential_operation = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: False
        )
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


def test_secret_input_is_masked_bounded_and_blocks_copy_and_cut() -> None:
    async def run() -> None:
        app = app_with_fakes()
        copied: list[str] = []
        app.copy_to_clipboard = copied.append  # type: ignore[method-assign]

        async with app.run_test(size=(180, 55)) as pilot:
            await pilot.press("f2")
            await pilot.pause()
            key_input = app.settings_screen.query_one("#openai-key-input", SecretInput)
            secret = "sk-test-never-copy-this-secret"
            key_input.value = secret
            key_input.focus()
            key_input.select_all()

            assert key_input.password
            assert key_input.max_length == 512
            assert secret not in key_input.render_line(0).text
            assert "•" in key_input.render_line(0).text

            key_input.action_copy()
            key_input.action_cut()
            await pilot.press("ctrl+c", "ctrl+x")

            assert copied == []
            assert key_input.value == secret

    asyncio.run(run())


def test_credential_save_is_payload_free_taken_once_and_immediately_cleared() -> None:
    async def run() -> None:
        app = app_with_fakes(app_type=CredentialCaptureApp)
        assert isinstance(app, CredentialCaptureApp)
        secret = "sk-test-message-payload-must-stay-empty"
        message_reprs: list[str] = []

        async with app.run_test(
            size=(180, 55),
            message_hook=lambda message: message_reprs.append(repr(message)),
        ) as pilot:
            await pilot.press("f2")
            await pilot.pause()
            key_input = app.settings_screen.query_one("#openai-key-input", SecretInput)

            key_input.value = "   "
            key_input.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.save_messages == []
            assert "Enter an OpenAI API key" in str(
                app.settings_screen.query_one("#openai-credential-feedback", Static).render()
            )

            key_input.value = f"  {secret}  "
            await pilot.press("enter")
            await pilot.pause()

            assert app.saved_openai_keys == [secret]
            assert len(app.save_messages) == 1
            assert secret not in repr(vars(app.save_messages[0]))
            assert key_input.value == ""
            assert app.settings_screen.take_openai_key() is None

            key_input.value = f"{secret}-blur"
            key_input.focus()
            await pilot.click("#close-settings")
            await pilot.pause()
            assert app.stock_secret_messages == []
            assert key_input.value == ""
            assert secret not in "\n".join(message_reprs)

    asyncio.run(run())


def test_saved_key_is_never_prefilled_and_status_controls_are_safe_and_busy() -> None:
    async def run() -> None:
        app = app_with_fakes()
        status_text = "Saved securely in the operating system credential vault."
        app.settings_screen.set_openai_credential_status(
            status_text,
            configured=True,
            stored=True,
            busy=True,
        )

        async with app.run_test(size=(180, 55)) as pilot:
            await pilot.press("f2")
            await pilot.pause()
            key_input = app.settings_screen.query_one("#openai-key-input", SecretInput)
            status = app.settings_screen.query_one("#openai-credential-status", Static)
            save = app.settings_screen.query_one("#openai-key-save", Button)
            delete = app.settings_screen.query_one("#openai-key-delete", Button)

            assert key_input.value == ""
            assert str(status.render()) == status_text
            assert status.has_class("configured")
            assert status.has_class("stored")
            assert status.has_class("busy")
            assert save.disabled
            assert delete.disabled

            app.settings_screen.set_openai_credential_status(
                "Ready.",
                configured=True,
                stored=True,
                busy=False,
            )
            assert not save.disabled
            assert not delete.disabled

            key_input.value = "transient-secret"
            await pilot.press("escape")
            await pilot.press("f2")
            await pilot.pause()
            assert key_input.value == ""
            assert str(status.render()) == "Ready."

            key_input.value = "must-clear-when-entry-becomes-unavailable"
            app.settings_screen.set_openai_credential_status(
                "Credential entry unavailable.",
                configured=True,
                stored=True,
                busy=False,
                error=True,
                input_available=False,
            )
            assert key_input.disabled
            assert key_input.value == ""
            assert save.disabled
            assert not delete.disabled

    asyncio.run(run())


def test_delete_requires_two_explicit_clicks_and_resets_on_new_typing() -> None:
    async def run() -> None:
        app = app_with_fakes(app_type=CredentialCaptureApp)
        assert isinstance(app, CredentialCaptureApp)
        app.settings_screen.set_openai_credential_status(
            "Saved key active.",
            configured=True,
            stored=True,
            busy=False,
        )

        async with app.run_test(size=(180, 55)) as pilot:
            await pilot.press("f2")
            await pilot.pause()
            delete = app.settings_screen.query_one("#openai-key-delete", Button)
            key_input = app.settings_screen.query_one("#openai-key-input", SecretInput)

            await pilot.click("#openai-key-delete")
            await pilot.pause()
            assert str(delete.label) == "Confirm delete"
            assert app.delete_messages == []

            key_input.value = "new-key-typing-resets-delete"
            await pilot.pause()
            assert str(delete.label) == "Delete saved key"
            assert app.delete_messages == []

            key_input.value = ""
            await pilot.pause()
            await pilot.click("#openai-key-delete")
            await pilot.pause(0.3)
            await pilot.click("#openai-key-delete")
            await pilot.pause()

            assert len(app.delete_messages) == 1
            assert "key" not in repr(vars(app.delete_messages[0])).lower()
            assert delete.disabled

    asyncio.run(run())


def test_credential_controls_fit_wide_and_compact_settings_drawers() -> None:
    async def run() -> None:
        for size, expected_width, size_class in (
            ((180, 55), 76, "-wide"),
            ((90, 30), 90, "-compact"),
        ):
            app = app_with_fakes()
            async with app.run_test(size=size) as pilot:
                await pilot.press("f2")
                await pilot.pause()
                screen = app.settings_screen
                key_input = await click_visible(pilot, screen, "#openai-key-input")
                actions = screen.query_one("#openai-credential-actions")
                save = screen.query_one("#openai-key-save", Button)
                delete = screen.query_one("#openai-key-delete", Button)
                drawer = screen.query_one("#settings-drawer")

                assert screen.has_class(size_class)
                assert drawer.region.width == expected_width
                assert 0 < key_input.region.width <= drawer.region.width
                assert 0 < actions.region.width <= drawer.region.width
                assert save.region.width > 0
                assert delete.region.width > 0
                assert drawer.region.x <= save.region.x < drawer.region.right
                assert drawer.region.x < delete.region.right <= drawer.region.right

    asyncio.run(run())


def test_every_settings_dismissal_path_clears_secret_and_delete_confirmation() -> None:
    async def run() -> None:
        app = app_with_fakes()
        app.settings_screen.set_openai_credential_status(
            "Saved key active.",
            configured=True,
            stored=True,
            busy=False,
        )

        async with app.run_test(size=(180, 55)) as pilot:
            base_screen = app.screen

            async def prepare() -> SecretInput:
                await pilot.press("f2")
                await pilot.pause()
                key_input = app.settings_screen.query_one("#openai-key-input", SecretInput)
                key_input.value = "must-be-forgotten-on-dismiss"
                await pilot.click("#openai-key-delete")
                await pilot.pause()
                assert (
                    str(app.settings_screen.query_one("#openai-key-delete", Button).label)
                    == "Confirm delete"
                )
                return key_input

            async def verify(key_input: SecretInput) -> None:
                await pilot.pause()
                assert app.screen is base_screen
                assert key_input.value == ""
                await pilot.press("f2")
                await pilot.pause()
                assert (
                    str(app.settings_screen.query_one("#openai-key-delete", Button).label)
                    == "Delete saved key"
                )
                await pilot.press("escape")
                await pilot.pause()

            key_input = await prepare()
            await pilot.press("escape")
            await verify(key_input)

            key_input = await prepare()
            await pilot.click("#close-settings")
            await verify(key_input)

            key_input = await prepare()
            await pilot.click(offset=(2, 20))
            await verify(key_input)

            key_input = await prepare()
            app.action_close_settings()
            await verify(key_input)

    asyncio.run(run())
