from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

from halcyon.app import HalcyonApp, _OpenAICredentialOperation
from halcyon.audio_bridge import AudioBridgeSnapshot
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.chat_router import RoutingDecision
from halcyon.coach_authoring import (
    DEFAULT_COACH_MODEL,
    LessonService,
    OpenAILessonAuthoringProvider,
)
from halcyon.coach_store import LessonStore
from halcyon.midi import MidiEvent
from halcyon.openai_credentials import (
    CredentialSource,
    OPENAI_API_KEY_ENV,
    OpenAICredentialStore,
    StoredCredentialState,
)
from halcyon.preset_store import UserPresetStore
from textual.widgets import Static


STORED_KEY = "sk-halcyon-integration-stored-001"
REPLACEMENT_KEY = "sk-halcyon-integration-replacement-002"
ENVIRONMENT_KEY = "sk-halcyon-integration-environment-003"


class MemorySecretBackend:
    """Thread-safe fake whose repr cannot accidentally disclose its value."""

    available = True

    def __init__(self, secret: str | None = None) -> None:
        self._secret = secret
        self._lock = threading.Lock()
        self.fail_write = False
        self.read_calls = 0
        self.write_calls = 0
        self.delete_calls = 0

    def read(self) -> str | None:
        with self._lock:
            self.read_calls += 1
            return self._secret

    def write(self, secret: str) -> None:
        with self._lock:
            self.write_calls += 1
            if self.fail_write:
                raise RuntimeError("synthetic write failure")
            self._secret = secret

    def delete(self) -> None:
        with self._lock:
            self.delete_calls += 1
            self._secret = None

    @property
    def secret_for_assertion(self) -> str | None:
        with self._lock:
            return self._secret

    def __repr__(self) -> str:
        return "MemorySecretBackend(secret=<redacted>)"


class BlockingReadBackend(MemorySecretBackend):
    def __init__(self, secret: str) -> None:
        super().__init__(secret)
        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self.read_finished = threading.Event()

    def read(self) -> str | None:
        self.read_started.set()
        self.release_read.wait(timeout=5)
        try:
            return super().read()
        finally:
            self.read_finished.set()


class FakeAudio:
    def start(self) -> bool:
        return True

    def apply_patch(self, _patch: dict[str, float]) -> bool:
        return True

    def snapshot(self) -> AudioBridgeSnapshot:
        return AudioBridgeSnapshot(running=True, ready=True, text="Audio: fake ready")

    def visualization_snapshot(self) -> AudioVisualizationSnapshot:
        return AudioVisualizationSnapshot(
            socket_path="/tmp/halcyon-credential-test.sock",
            sample_count=0,
            connected=False,
            frame_count=0,
            samples=(),
        )

    def all_notes_off(self) -> bool:
        return True

    def handle_midi_event(self, _event: MidiEvent) -> bool:
        return True

    def close(self) -> None:
        pass


class FakeMidi:
    connected = False

    def list_ports(self) -> list[str]:
        return []

    def connect(self) -> str:
        return "Synthetic MIDI"

    def poll(self) -> list[MidiEvent]:
        return []

    def close(self) -> None:
        pass


class FakeLocalControlServer:
    """In-memory lifecycle double; it never binds a socket."""

    host = "127.0.0.1"

    def __init__(self) -> None:
        self.port: int | None = None

    def start(self) -> tuple[str, int]:
        self.port = 43123
        return self.host, self.port

    def stop(self) -> None:
        self.port = None


class FakeOpenAIClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def __repr__(self) -> str:
        return "FakeOpenAIClient(api_key=<redacted>)"


class ReleaseCredentialReadOnUnmountApp(HalcyonApp):
    """Release native I/O only after Halcyon's unmount guards are installed."""

    CSS_PATH = str(Path(__file__).parents[1] / "halcyon" / "styles.tcss")

    def __init__(self, *, blocked_backend: BlockingReadBackend, **kwargs: Any) -> None:
        self.blocked_backend = blocked_backend
        self.credential_finish_calls: list[tuple[object, ...]] = []
        super().__init__(**kwargs)

    def _finish_openai_credential_operation(self, *args: Any, **kwargs: Any) -> None:
        self.credential_finish_calls.append((*args, kwargs))
        super()._finish_openai_credential_operation(*args, **kwargs)

    def on_unmount(self) -> None:
        super().on_unmount()
        self.blocked_backend.release_read.set()


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    assert predicate()


def install_fake_openai(monkeypatch: Any) -> list[str]:
    constructed_with: list[str] = []

    def fake_openai(*, api_key: str) -> FakeOpenAIClient:
        constructed_with.append(api_key)
        return FakeOpenAIClient(api_key)

    monkeypatch.setattr("openai.OpenAI", fake_openai)
    return constructed_with


def make_store(
    backend: MemorySecretBackend,
    *,
    environment: Mapping[str, str] | None = None,
) -> OpenAICredentialStore:
    return OpenAICredentialStore(
        backend=backend,
        environment={} if environment is None else environment,
    )


def make_app(
    tmp_path: Path,
    monkeypatch: Any,
    credential_store: OpenAICredentialStore,
    *,
    suffix: str,
    lesson_service: LessonService | None = None,
    app_type: type[HalcyonApp] = HalcyonApp,
    app_kwargs: dict[str, object] | None = None,
) -> HalcyonApp:
    lesson_root = tmp_path / f"owned-lessons-{suffix}"
    monkeypatch.setattr(
        "halcyon.app.LessonStore",
        lambda: LessonStore(root=lesson_root, builtins=()),
    )
    kwargs: dict[str, object] = {
        "preset_store": UserPresetStore(path=tmp_path / f"presets-{suffix}.json"),
        "credential_store": credential_store,
        "lesson_service": lesson_service,
    }
    kwargs.update(app_kwargs or {})
    app = app_type(**kwargs)  # type: ignore[arg-type]
    app.audio = FakeAudio()  # type: ignore[assignment]
    app.midi = FakeMidi()  # type: ignore[assignment]
    app.local_control_server = FakeLocalControlServer()  # type: ignore[assignment]
    return app


def provider_key(app: HalcyonApp) -> str | None:
    provider = app.lesson_service.provider
    if not isinstance(provider, OpenAILessonAuthoringProvider):
        return None
    client = provider.client
    return client.api_key if isinstance(client, FakeOpenAIClient) else None


def transcript_text(app: HalcyonApp) -> str:
    return "\n".join(str(getattr(line, "text", line)) for line in app.chat_panel.transcript.lines)


def test_saved_key_survives_a_full_app_restart_and_rehydrates_clients(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("HALCYON_COACH_MODEL", raising=False)
    constructed_with = install_fake_openai(monkeypatch)
    backend = MemorySecretBackend()

    async def run() -> None:
        first = make_app(
            tmp_path,
            monkeypatch,
            make_store(backend),
            suffix="first",
        )
        async with first.run_test(size=(120, 42)):
            await wait_until(lambda: not first._credential_busy)
            assert not first.assistant.enabled
            assert first.lesson_service.provider is None

            assert first._start_openai_credential_operation("save", secret=STORED_KEY)
            await wait_until(lambda: not first._credential_busy)
            assert first.assistant.enabled
            assert provider_key(first) == STORED_KEY
            assert first.lesson_service.provider.model_version == DEFAULT_COACH_MODEL
            assert backend.secret_for_assertion == STORED_KEY

        second = make_app(
            tmp_path,
            monkeypatch,
            make_store(backend),
            suffix="second",
        )
        assert not second.assistant.enabled
        async with second.run_test(size=(120, 42)):
            await wait_until(lambda: not second._credential_busy)
            assert second.assistant.enabled
            assert second.chat_panel.enabled
            assert second._credential_status.source is CredentialSource.SYSTEM_KEYRING
            assert second._credential_status.stored is StoredCredentialState.PRESENT
            assert provider_key(second) == STORED_KEY
            assert second.lesson_service.provider.model_version == DEFAULT_COACH_MODEL

    asyncio.run(run())
    assert backend.read_calls == 2
    assert backend.write_calls == 1
    assert constructed_with == [STORED_KEY, STORED_KEY]


def test_saving_from_settings_enables_chat_and_refreshes_app_owned_provider_live(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HALCYON_COACH_MODEL", "coach-integration-model")
    constructed_with = install_fake_openai(monkeypatch)
    backend = MemorySecretBackend()
    app = make_app(tmp_path, monkeypatch, make_store(backend), suffix="live-save")

    async def run() -> None:
        async with app.run_test(size=(180, 55)) as pilot:
            await wait_until(lambda: not app._credential_busy)
            app.action_toggle_settings()
            await pilot.pause()
            app.settings_screen.openai_key_input.value = REPLACEMENT_KEY

            save_button = app.settings_screen.query_one("#openai-key-save")
            save_button.scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            assert await pilot.click(save_button)
            await wait_until(lambda: not app._credential_busy)

            assert app.settings_screen.openai_key_input.value == ""
            assert app.assistant.enabled
            assert app.chat_panel.enabled
            assert provider_key(app) == REPLACEMENT_KEY
            assert backend.secret_for_assertion == REPLACEMENT_KEY

    asyncio.run(run())
    assert constructed_with == [REPLACEMENT_KEY]


def test_deleting_only_saved_key_disables_clients_and_authoring(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HALCYON_COACH_MODEL", "coach-integration-model")
    install_fake_openai(monkeypatch)
    backend = MemorySecretBackend(STORED_KEY)
    app = make_app(tmp_path, monkeypatch, make_store(backend), suffix="delete-stored")

    async def run() -> None:
        async with app.run_test(size=(120, 42)):
            await wait_until(lambda: not app._credential_busy)
            assert app.assistant.enabled
            assert provider_key(app) == STORED_KEY

            assert app._start_openai_credential_operation("delete")
            await wait_until(lambda: not app._credential_busy)

            assert not app.assistant.enabled
            assert not app.chat_panel.enabled
            assert app.lesson_service.provider is None
            assert app._credential_status.source is CredentialSource.NONE
            assert app._credential_status.stored is StoredCredentialState.ABSENT
            assert backend.secret_for_assertion is None

    asyncio.run(run())


def test_deleting_saved_key_preserves_environment_override_and_clients(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HALCYON_COACH_MODEL", "coach-integration-model")
    constructed_with = install_fake_openai(monkeypatch)
    backend = MemorySecretBackend(STORED_KEY)
    store = make_store(backend, environment={OPENAI_API_KEY_ENV: ENVIRONMENT_KEY})
    app = make_app(tmp_path, monkeypatch, store, suffix="delete-env")

    async def run() -> None:
        async with app.run_test(size=(120, 42)):
            await wait_until(lambda: not app._credential_busy)
            assert app._credential_status.source is CredentialSource.ENVIRONMENT
            assert app._credential_status.stored is StoredCredentialState.PRESENT
            assert provider_key(app) == ENVIRONMENT_KEY

            assert app._start_openai_credential_operation("delete")
            await wait_until(lambda: not app._credential_busy)

            assert app.assistant.enabled
            assert app.chat_panel.enabled
            assert provider_key(app) == ENVIRONMENT_KEY
            assert app._credential_status.source is CredentialSource.ENVIRONMENT
            assert app._credential_status.stored is StoredCredentialState.ABSENT
            assert backend.secret_for_assertion is None

    asyncio.run(run())
    assert constructed_with
    assert set(constructed_with) == {ENVIRONMENT_KEY}


def test_failed_save_keeps_previous_effective_key_provider_and_status(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HALCYON_COACH_MODEL", "coach-integration-model")
    constructed_with = install_fake_openai(monkeypatch)
    routed_with: list[str | None] = []

    def fake_route(
        *_args: object, api_key: str | None = None, **_kwargs: object
    ) -> RoutingDecision:
        routed_with.append(api_key)
        return RoutingDecision(action="reject", reason="synthetic routing result")

    monkeypatch.setattr("halcyon.assistant.run_chat_routing", fake_route)
    backend = MemorySecretBackend(STORED_KEY)
    app = make_app(tmp_path, monkeypatch, make_store(backend), suffix="failed-save")

    async def run() -> None:
        async with app.run_test(size=(120, 42)):
            await wait_until(lambda: not app._credential_busy)
            original_provider = app.lesson_service.provider
            original_status = app._credential_status
            backend.fail_write = True

            assert app._start_openai_credential_operation("save", secret=REPLACEMENT_KEY)
            await wait_until(lambda: not app._credential_busy)

            app.assistant.ask("keep using the established credential")
            assert routed_with[-1] == STORED_KEY
            assert app.lesson_service.provider is original_provider
            assert app._credential_status == original_status
            assert backend.secret_for_assertion == STORED_KEY
            assert "unchanged" in transcript_text(app).lower()

    asyncio.run(run())
    assert constructed_with == [STORED_KEY]


def test_injected_lesson_service_provider_is_never_reconfigured(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HALCYON_COACH_MODEL", "coach-integration-model")
    constructed_with = install_fake_openai(monkeypatch)
    sentinel_provider = object()
    service = LessonService(
        store=LessonStore(root=tmp_path / "injected-lessons", builtins=()),
        provider=sentinel_provider,  # type: ignore[arg-type]
    )
    backend = MemorySecretBackend(STORED_KEY)
    app = make_app(
        tmp_path,
        monkeypatch,
        make_store(backend),
        suffix="injected-service",
        lesson_service=service,
    )

    async def run() -> None:
        async with app.run_test(size=(120, 42)):
            await wait_until(lambda: not app._credential_busy)
            assert app.lesson_service is service
            assert service.provider is sentinel_provider

            assert app._start_openai_credential_operation("save", secret=REPLACEMENT_KEY)
            await wait_until(lambda: not app._credential_busy)
            assert service.provider is sentinel_provider

            assert app._start_openai_credential_operation("delete")
            await wait_until(lambda: not app._credential_busy)
            assert service.provider is sentinel_provider

    asyncio.run(run())
    assert constructed_with == []


def test_secret_never_appears_in_public_state_status_transcript_or_reprs(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HALCYON_COACH_MODEL", "coach-integration-model")
    install_fake_openai(monkeypatch)
    backend = MemorySecretBackend()
    store = make_store(backend)
    app = make_app(tmp_path, monkeypatch, store, suffix="redaction")

    async def run() -> None:
        async with app.run_test(size=(180, 55)) as pilot:
            await wait_until(lambda: not app._credential_busy)
            assert app._start_openai_credential_operation("save", secret=REPLACEMENT_KEY)
            await wait_until(lambda: not app._credential_busy)

            app.action_toggle_settings()
            await pilot.pause()
            status = app.settings_screen.query_one("#openai-credential-status", Static)
            public_blob = json.dumps(
                {
                    "synth": app.synth_tool_state(),
                    "assistant_context": app._assistant_context(),
                    "mcp": app.mcp_local_state(),
                    "credential_status": store.current.status.to_public_dict(),
                },
                sort_keys=True,
                default=str,
            )
            visible_blob = "\n".join((str(status.render()), transcript_text(app)))
            repr_blob = "\n".join(
                (
                    repr(store.current),
                    str(store.current),
                    repr(store.current.status),
                    repr(_OpenAICredentialOperation("save", 99, REPLACEMENT_KEY)),
                    repr(backend),
                    repr(app.lesson_service.provider),
                )
            )

            assert REPLACEMENT_KEY not in public_blob
            assert REPLACEMENT_KEY not in visible_blob
            assert REPLACEMENT_KEY not in repr_blob
            assert "api_key" not in public_blob.lower()
            assert app.settings_screen.openai_key_input.value == ""

    asyncio.run(run())


def test_blocked_keyring_read_cannot_apply_a_late_callback_after_unmount(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HALCYON_COACH_MODEL", "coach-integration-model")
    install_fake_openai(monkeypatch)
    backend = BlockingReadBackend(STORED_KEY)
    app = make_app(
        tmp_path,
        monkeypatch,
        make_store(backend),
        suffix="blocked-unmount",
        app_type=ReleaseCredentialReadOnUnmountApp,
        app_kwargs={"blocked_backend": backend},
    )
    assert isinstance(app, ReleaseCredentialReadOnUnmountApp)

    async def run() -> None:
        async with app.run_test(size=(120, 42)):
            started = await asyncio.to_thread(backend.read_started.wait, 2)
            assert started
            assert app._credential_busy
            assert not app.assistant.enabled
            assert app.credential_finish_calls == []

        finished = await asyncio.to_thread(backend.read_finished.wait, 2)
        assert finished
        await asyncio.sleep(0.02)
        assert app._closing
        assert not app.assistant.enabled
        assert app.lesson_service.provider is None
        assert app.credential_finish_calls == []

    asyncio.run(run())


def test_textual_diagnostics_disable_in_app_secret_entry(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TEXTUAL_LOG", str(tmp_path / "textual-diagnostics.log"))
    backend = MemorySecretBackend()
    app = make_app(
        tmp_path,
        monkeypatch,
        make_store(backend),
        suffix="diagnostics-disabled",
    )

    async def run() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await wait_until(lambda: not app._credential_busy)
            app.action_toggle_settings()
            await pilot.pause()

            status = app.settings_screen.query_one("#openai-credential-status", Static)
            assert app.settings_screen.openai_key_input.disabled
            assert app.settings_screen.query_one("#openai-key-save").disabled
            assert "diagnostic" in str(status.render()).lower()
            assert not app._start_openai_credential_operation("save", secret=STORED_KEY)
            assert backend.write_calls == 0

    asyncio.run(run())


def test_unavailable_secure_backend_disables_in_app_secret_entry(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    backend = MemorySecretBackend()
    backend.available = False
    app = make_app(
        tmp_path,
        monkeypatch,
        make_store(backend),
        suffix="backend-unavailable",
    )

    async def run() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await wait_until(lambda: not app._credential_busy)
            app.action_toggle_settings()
            await pilot.pause()

            status = app.settings_screen.query_one("#openai-credential-status", Static)
            assert not app._credential_status.storage_available
            assert app.settings_screen.openai_key_input.disabled
            assert app.settings_screen.query_one("#openai-key-save").disabled
            assert "unavailable" in str(status.render()).lower()
            assert not app._start_openai_credential_operation("save", secret=STORED_KEY)
            assert backend.write_calls == 0

    asyncio.run(run())


def test_invalid_environment_override_is_visible_as_an_error(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    backend = MemorySecretBackend(STORED_KEY)
    store = make_store(
        backend,
        environment={OPENAI_API_KEY_ENV: "invalid internal whitespace"},
    )
    app = make_app(
        tmp_path,
        monkeypatch,
        store,
        suffix="invalid-environment",
    )

    async def run() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await wait_until(lambda: not app._credential_busy)
            app.action_toggle_settings()
            await pilot.pause()

            status = app.settings_screen.query_one("#openai-credential-status", Static)
            assert app._credential_status.source is CredentialSource.ENVIRONMENT
            assert not app.assistant.enabled
            assert status.has_class("error")
            assert "set but invalid" in str(status.render()).lower()
            assert not app.settings_screen.openai_key_input.disabled

            assert app._start_openai_credential_operation("save", secret=REPLACEMENT_KEY)
            await wait_until(lambda: not app._credential_busy)
            assert backend.secret_for_assertion == REPLACEMENT_KEY
            assert not app.assistant.enabled
            assert "invalid openai_api_key" in transcript_text(app).lower()

    asyncio.run(run())
