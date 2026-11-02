"""Textual entrypoint for Project Halcyon."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
import os
import shlex
import threading
from typing import Any, Callable, ClassVar, Literal, TypeVar
from uuid import uuid4

from rich.text import Text
from textual import constants as textual_constants
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.message import Message
from textual.widgets import Button, DataTable, Footer, Static
from textual.worker import get_current_worker

from halcyon.assistant import (
    HalcyonAssistant,
    local_tool_executor,
)
from halcyon.assistant_service import AssistantService
from halcyon.audio_bridge import RustAudioBridge
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.chat_widgets import ChatPanel
from halcyon.coach_authoring import (
    AuthoringProviderUnavailable,
    LessonService,
    OpenAILessonAuthoringProvider,
)
from halcyon.coach_engine import CoachEngine, ThreadingDeadlineScheduler
from halcyon.coach_highway import CoachVisualizationSnapshot
from halcyon.coach_highway_widget import CoachNoteHighway
from halcyon.coach_models import CoachError
from halcyon.coach_store import LessonStore
from halcyon.command_executor import (
    CommandExecutionError,
    ImmediateCommandOutcome,
    PendingCommandOutcome,
    TerminalCommandOutcome,
    execute_slash_command_outcome,
    prepare_slash_command,
)
from halcyon.controls import SynthRack
from halcyon.console_events import CONSOLE_TERMINAL_STATES, ConsoleEvent, safe_console_text
from halcyon.event_bus import (
    DispatchReceipt,
    MidiEventBus,
    ReceiptPoll,
    ReceiptState,
)
from halcyon.keyboard import keyboard_text, scroll_target_for_note
from halcyon.logs import MidiEventLog
from halcyon.menu import load_menu
from halcyon.mcp_local import LocalControlServer
from halcyon.mcp_local.run_tracker import RunTracker, TERMINAL_RUN_STATES
from halcyon.mcp_tools import ToolExecutionError
from halcyon.midi import MidiEvent, MidiInput, MidiOrigin, MidiUnavailable
from halcyon.oscillator_scope import OscillatorScope, ScopeMode
from halcyon.openai_credentials import (
    CredentialError,
    CredentialErrorCode,
    CredentialResolution,
    CredentialSource,
    CredentialStatus,
    OpenAICredentialStore,
    StoredCredentialState,
)
from halcyon.preset_store import UserPresetStore
from halcyon.settings_screen import SoundSettingsScreen
from halcyon.slash_commands import iter_slash_leaf_commands, resolve_slash_command
from halcyon.synth_params import PRESETS, SynthPatch
from halcyon.theme import HALCYON_THEME

T = TypeVar("T")


class _ImmediateDispatchReceipt:
    """Narrow compatibility receipt for legacy boolean audio fakes."""

    def __init__(self, state: ReceiptState) -> None:
        self._poll = ReceiptPoll(
            state=state,
            code="AUDIO_BACKPRESSURE" if state == "failed_safe" else None,
        )

    def poll(self) -> ReceiptPoll:
        return self._poll


class _AudioDispatchReceipt:
    """Private monotonic view of one Rust command admission."""

    def __init__(
        self,
        app: HalcyonApp,
        *,
        bridge: object,
        generation: int,
        command_id: int,
        safety: bool,
    ) -> None:
        self._app = app
        self._bridge = bridge
        self._generation = generation
        self._command_id = command_id
        self._safety = safety
        self._state: ReceiptState = "pending"
        self._lock = threading.Lock()

    @property
    def bridge(self) -> object:
        return self._bridge

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def command_id(self) -> int:
        return self._command_id

    @property
    def safety(self) -> bool:
        return self._safety

    def poll(self) -> ReceiptPoll:
        with self._lock:
            state = self._state
        if state == "pending":
            self._app._poll_audio_receipt(self)
            with self._lock:
                state = self._state
        return ReceiptPoll(
            state=state,
            code="AUDIO_BACKPRESSURE" if state in {"rejected_unsafe", "failed_safe"} else None,
        )

    def transition(self, state: ReceiptState) -> None:
        with self._lock:
            current = self._state
            allowed = (
                current == "pending" and state in {"accepted", "rejected_unsafe", "failed_safe"}
            ) or (current == "rejected_unsafe" and state == "failed_safe")
            if allowed:
                self._state = state


@dataclass(slots=True)
class _AudioContainment:
    generation: int
    bridge: object
    captured_process: object | None
    receipts: list[_AudioDispatchReceipt] = field(default_factory=list)
    started: bool = False
    confirmed_stopped: bool = False


@dataclass(slots=True)
class _GlobalSafetyAction:
    generation: int
    bridge: object
    receipt: DispatchReceipt
    reconnect_requested: bool


class _ConsoleEventMessage(Message):
    """Thread-safe presentation delivery, with no execution authority."""

    def __init__(self, event: ConsoleEvent) -> None:
        super().__init__()
        self.event = event


@dataclass(slots=True, repr=False)
class _OpenAICredentialOperation:
    """Secret-redacted argument passed to Textual's credential worker."""

    action: Literal["load", "save", "delete"]
    generation: int
    secret: str | None = field(default=None, repr=False)


class VirtualKeyboard(Static):
    """A continuous full-spectrum MIDI keyboard surface."""

    def update_keys(
        self,
        human_notes: set[int],
        target_notes: set[int] | frozenset[int] = frozenset(),
        *,
        halcyon_notes: set[int] | frozenset[int] = frozenset(),
    ) -> None:
        self.update(
            keyboard_text(
                human_notes,
                target_notes,
                halcyon_notes=halcyon_notes,
            )
        )


class HalcyonApp(App[None]):
    """Project Halcyon MIDI monitor."""

    CSS_PATH: ClassVar[str] = "styles.tcss"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("r", "reconnect", "Reconnect"),
        Binding("c", "clear_log", "Clear"),
        Binding("comma", "toggle_settings", "Settings"),
        Binding("ctrl+s", "toggle_settings", "Settings", priority=True),
        Binding("f1", "workspace('stage')", "Stage", show=False),
        Binding("f2", "workspace('controls')", "Controls", show=False),
        Binding("f3", "workspace('activity')", "Activity", show=False),
        Binding("f4", "workspace('console')", "Console", show=False),
    ]

    def __init__(
        self,
        *,
        preset_store: UserPresetStore | None = None,
        lesson_service: LessonService | None = None,
        credential_store: OpenAICredentialStore | None = None,
    ) -> None:
        super().__init__()
        self.register_theme(HALCYON_THEME)
        self.theme = HALCYON_THEME.name
        self.midi = MidiInput()
        self.audio = RustAudioBridge()
        self._closing = False
        self._resources_closed = False
        self._audio_state_lock = threading.Lock()
        self._audio_generation = 1
        self._audio_generation_bridge: object = self.audio
        self._closed_audio_generations: set[int] = set()
        self._audio_containments: dict[int, _AudioContainment] = {}
        self._global_safety_action: _GlobalSafetyAction | None = None
        self.user_preset_store = preset_store or UserPresetStore.load()
        self.synth_patch = SynthPatch()
        self.menu_def = load_menu()
        self.openai_credentials = credential_store or OpenAICredentialStore()
        initial_credential = self.openai_credentials.current
        initial_api_key = initial_credential.secret_for_client()
        self._credential_status = initial_credential.status
        self._credential_generation = 0
        self._credential_busy = False
        self.assistant = HalcyonAssistant(
            menu_def=self.menu_def,
            api_key=initial_api_key if initial_api_key is not None else "",
            context_provider=lambda: self._run_on_app_thread(self._assistant_context),
        )
        self.human_pressed_notes: set[int] = set()
        self.halcyon_pressed_notes: set[int] = set()
        self.pressed_notes: set[int] = set()
        self.target_notes: set[int] = set()
        self.event_log = MidiEventLog(max_events=200)
        self.event_count = 0
        self._midi_source = "MIDI offline"
        self._size_class = ""
        self._workspace = "stage"
        self._console_runs: OrderedDict[str, tuple[str, str, str | None]] = OrderedDict()
        self._owner_thread_id = threading.get_ident()
        self.event_bus = MidiEventBus()
        self._ui_bus_subscription = self.event_bus.subscribe(self._observe_dispatched_event)
        self._audio_bus_subscription = self.event_bus.subscribe(
            self._submit_audio_event,
            receipt_required=True,
        )
        self._owns_lesson_service = lesson_service is None
        self.lesson_service = lesson_service or LessonService(
            store=LessonStore(),
            provider=self._authoring_provider_for_key(initial_api_key),
        )
        self.coach = CoachEngine(
            self.lesson_service,
            self.event_bus,
            dispatcher=self._dispatch_to_app,
            scheduler=ThreadingDeadlineScheduler(),
        )
        self._coach_highway_active = False
        self.status = Static("Starting MIDI monitor...", id="status")
        self.audio_status = Static("Audio: starting Rust sidecar...", id="audio-status")
        self.status_rail = Static("HALCYON · starting", id="status-rail")
        self.synth_rack = SynthRack(
            self.synth_patch,
            user_presets=self.user_preset_store.presets,
            id="synth-rack",
        )
        self.settings_screen = SoundSettingsScreen(self.synth_rack)
        self._present_openai_credential_status(self._credential_status)
        self.install_screen(self.settings_screen, "sound-settings")
        self.chat_panel = ChatPanel(
            enabled=self.assistant.enabled,
            menu_def=self.menu_def,
            id="chat-panel",
        )
        self._run_tracker = RunTracker()
        self._console_unsubscribe = self._run_tracker.subscribe(self._receive_console_event)
        self._chat_assistant_session_id: str | None = None
        self.assistant_service = AssistantService(
            self.assistant,
            self._run_tracker,
            command=lambda command, parent, timeout: self._run_on_app_thread(
                lambda: self.mcp_local_run_slash(
                    command=command,
                    source="llm",
                    timeout_s=timeout,
                    parent_run_id=parent,
                    local_origin="assistant",
                )
            ),
            state=lambda: self._run_on_app_thread(self._assistant_context),
            event_sink=self._receive_console_event,
        )
        self.local_control_server = LocalControlServer(self)
        self.keyboard = VirtualKeyboard(id="keyboard")
        self.keyboard_scroll = HorizontalScroll(id="keyboard-scroll")
        self.event_table = DataTable(id="event-log", zebra_stripes=True)
        self.scope = OscillatorScope(self.audio_visualization_snapshot, id="oscillator-scope")
        self.coach_highway = CoachNoteHighway(
            lambda: self.coach.visualization_snapshot(),
            snapshot_consumer=self._present_coach_snapshot,
            id="coach-highway",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="app-shell"):
            yield self.status_rail
            # Compatibility status surfaces remain queryable while the rail owns display.
            yield self.status
            yield self.audio_status
            with Horizontal(id="compact-nav"):
                yield Button("Stage  F1", id="workspace-stage", compact=True)
                yield Button("Settings  F2", id="workspace-controls", compact=True)
                yield Button("MIDI  F3", id="workspace-activity", compact=True)
                yield Button("Console  F4", id="workspace-console", compact=True)
            with Vertical(id="workspace-host"):
                with Vertical(id="main-workspace"):
                    with Vertical(id="performance-stage", classes="workspace-pane"):
                        with Vertical(id="performance-header"):
                            with Horizontal(id="scope-toolbar"):
                                yield Static(
                                    "PERFORMANCE · Akai LPK25 Wireless",
                                    id="keyboard-title",
                                    classes="panel-title",
                                )
                                yield Static(
                                    "OSCILLATOR",
                                    id="surface-mode-title",
                                    classes="panel-title",
                                )
                                with Horizontal(id="scope-controls"):
                                    yield Button("Wave", id="scope-wave", compact=True)
                                    yield Button("Orbit", id="scope-orbit", compact=True)
                                    yield Button("Radial", id="scope-radial", compact=True)
                            yield Static(
                                Text.assemble(
                                    ("═ STRIKE", "bold #ffd866"),
                                    "   ",
                                    ("● YOU", "bold #ff9f43"),
                                    "   ",
                                    ("◆ HALCYON", "bold #fcfcfa"),
                                    "   ",
                                    ("○ EXPECTED", "bold #8b95a7"),
                                    "   ",
                                    ("●◆ BOTH", "bold #ffd866"),
                                    "   ",
                                    ("♮ NATURAL", "bold #ab9df2"),
                                    "   ",
                                    ("♯ SHARP", "bold #78dce8"),
                                ),
                                id="keyboard-legend",
                            )
                        with Vertical(id="instrument-surface"):
                            yield self.scope
                            yield self.coach_highway
                            with self.keyboard_scroll:
                                yield self.keyboard
                    with Horizontal(id="lower-workspace"):
                        with Vertical(id="command-console", classes="workspace-pane"):
                            yield self.chat_panel
                        with Vertical(id="activity-panel", classes="workspace-pane"):
                            yield Static("MIDI ACTIVITY", classes="panel-title")
                            yield self.event_table
        yield Footer()

    def on_mount(self) -> None:
        self._closing = False
        self.title = "Halcyon"
        self.sub_title = "Performance cockpit"
        self.event_table.cursor_type = "none"
        self.event_table.add_columns(
            "Time",
            "Origin",
            "Event",
            "Note",
            "Vel",
            "Ch",
            "Status",
            "Delta",
            "Source",
        )
        self._refresh_keyboard_state()
        self._apply_size_class(self.size.width, self.size.height)
        self.action_workspace("stage")
        self._refresh_scope_buttons()
        self.watch(
            self.keyboard_scroll,
            "scroll_x",
            self._sync_highway_viewport,
            init=True,
        )
        self._start_audio()
        self._start_local_control_server()
        self._connect_midi()
        self._start_openai_credential_operation("load")
        self.set_interval(0.02, self._poll_midi)
        self.set_interval(0.02, self._service_coach_callbacks)
        self.set_interval(0.5, self._refresh_audio_status)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_size_class(event.size.width, event.size.height)

    def _apply_size_class(self, width: int, height: int) -> None:
        if width < 110 or height < 40:
            size_class = "-compact"
        elif width < 150 or height < 48:
            size_class = "-medium"
        else:
            size_class = "-wide"
        if size_class == self._size_class or not self._is_mounted_now():
            return
        for candidate in ("-wide", "-medium", "-compact"):
            self.screen.set_class(candidate == size_class, candidate)
        self._size_class = size_class

    @staticmethod
    def _authoring_provider_for_key(
        api_key: str | None,
    ) -> OpenAILessonAuthoringProvider | None:
        if not api_key:
            return None
        try:
            return OpenAILessonAuthoringProvider.from_environment(api_key=api_key)
        except AuthoringProviderUnavailable:
            return None

    @on(SoundSettingsScreen.SaveOpenAIKeyRequested)
    def _on_save_openai_key_requested(
        self,
        message: SoundSettingsScreen.SaveOpenAIKeyRequested,
    ) -> None:
        """Transfer the transient value once without placing it in a Message."""

        message.stop()
        secret = self.settings_screen.take_openai_key()
        if not self._openai_credential_entry_available(self._credential_status):
            self._present_openai_credential_status(self._credential_status)
            return
        if secret is None:
            self._present_openai_credential_status(
                self._credential_status,
                error_text="Enter a valid OpenAI API key; the current configuration is unchanged.",
            )
            return
        if not self._start_openai_credential_operation("save", secret=secret):
            self._present_openai_credential_status(
                self._credential_status,
                error_text="Another credential operation is still running; try again.",
            )

    @on(SoundSettingsScreen.DeleteOpenAIKeyRequested)
    def _on_delete_openai_key_requested(
        self,
        message: SoundSettingsScreen.DeleteOpenAIKeyRequested,
    ) -> None:
        message.stop()
        if not self._start_openai_credential_operation("delete"):
            self._present_openai_credential_status(
                self._credential_status,
                error_text="Another credential operation is still running; try again.",
            )

    def _start_openai_credential_operation(
        self,
        action: Literal["load", "save", "delete"],
        *,
        secret: str | None = None,
    ) -> bool:
        if (
            self._closing
            or self._credential_busy
            or (
                action == "save"
                and not self._openai_credential_entry_available(self._credential_status)
            )
        ):
            return False
        self._credential_generation += 1
        operation = _OpenAICredentialOperation(
            action=action,
            generation=self._credential_generation,
            secret=secret,
        )
        self._credential_busy = True
        self._present_openai_credential_status(
            self._credential_status,
            busy_action=action,
        )
        self._run_openai_credential_operation(operation)
        return True

    @work(
        thread=True,
        name="openai-credential",
        group="openai-credential",
        exclusive=True,
        exit_on_error=False,
        description="Update secure OpenAI credential",
    )
    def _run_openai_credential_operation(
        self,
        operation: _OpenAICredentialOperation,
    ) -> None:
        """Perform blocking credential-vault I/O away from Textual's event loop."""

        resolution: CredentialResolution | None = None
        error_code: CredentialErrorCode | None = None
        try:
            if operation.action == "load":
                resolution = self.openai_credentials.load()
            elif operation.action == "save":
                resolution = self.openai_credentials.save(operation.secret)
            else:
                resolution = self.openai_credentials.delete()
        except CredentialError as exc:
            error_code = exc.code
        except Exception:
            error_code = self._unexpected_credential_error_code(operation.action)
        finally:
            operation.secret = None

        if get_current_worker().is_cancelled:
            return
        try:
            self.call_from_thread(
                self._finish_openai_credential_operation,
                operation.action,
                operation.generation,
                resolution,
                error_code,
            )
        except RuntimeError:
            # The app may have completed unmount while native keyring I/O was blocked.
            return

    def _finish_openai_credential_operation(
        self,
        action: Literal["load", "save", "delete"],
        generation: int,
        resolution: CredentialResolution | None,
        error_code: CredentialErrorCode | None,
    ) -> None:
        if self._closing or generation != self._credential_generation:
            return
        self._credential_busy = False
        if error_code is not None or resolution is None:
            self._present_openai_credential_status(
                self._credential_status,
                error_text=self._credential_error_text(action, error_code),
            )
            if action != "load":
                self.chat_panel.write_system(
                    "OpenAI credential update failed; the current configuration is unchanged."
                )
            return

        was_enabled = self.assistant.enabled
        self._credential_status = resolution.status
        api_key = resolution.secret_for_client()
        self.assistant.set_api_key(api_key)
        self.chat_panel.set_enabled(self.assistant.enabled)
        if self._owns_lesson_service:
            self.lesson_service.provider = self._authoring_provider_for_key(api_key)
        self._present_openai_credential_status(self._credential_status)
        self._announce_openai_credential_result(
            action,
            was_enabled=was_enabled,
            enabled=self.assistant.enabled,
            source=self._credential_status.source,
        )

    def _present_openai_credential_status(
        self,
        status: CredentialStatus,
        *,
        busy_action: Literal["load", "save", "delete"] | None = None,
        error_text: str | None = None,
    ) -> None:
        if error_text is not None:
            text = error_text
        elif busy_action == "load":
            text = "Checking the system credential vault…"
        elif busy_action == "save":
            text = "Saving the OpenAI key securely…"
        elif busy_action == "delete":
            text = "Deleting the saved OpenAI key…"
        else:
            text = self._openai_credential_status_text(status)
        input_available = self._openai_credential_entry_available(status)
        if not self._textual_credential_diagnostics_safe():
            text = (
                "Credential entry is disabled while Textual diagnostic logging or devtools "
                "is active. Restart without diagnostics to save or replace a key."
            )
        self.settings_screen.set_openai_credential_status(
            text,
            configured=status.configured,
            stored=status.stored is StoredCredentialState.PRESENT,
            busy=busy_action is not None,
            error=(
                error_text is not None
                or not status.storage_available
                or not self._textual_credential_diagnostics_safe()
                or (status.source is CredentialSource.ENVIRONMENT and not status.configured)
            ),
            input_available=input_available,
        )

    def _openai_credential_entry_available(self, status: CredentialStatus) -> bool:
        return status.storage_available and self._textual_credential_diagnostics_safe()

    def _textual_credential_diagnostics_safe(self) -> bool:
        return not (
            textual_constants.LOG_FILE
            or os.environ.get("TEXTUAL_LOG")
            or textual_constants.DEBUG
            or "debug" in self.features
            or "devtools" in self.features
            or self.devtools is not None
        )

    @staticmethod
    def _openai_credential_status_text(status: CredentialStatus) -> str:
        if status.source is CredentialSource.ENVIRONMENT:
            if not status.configured:
                return (
                    "OPENAI_API_KEY is set but invalid and overrides any saved key. "
                    "Remove or replace it, then restart Halcyon."
                )
            if status.stored is StoredCredentialState.PRESENT:
                return (
                    "OPENAI_API_KEY is active; a separate key is also stored securely "
                    "in the system credential vault."
                )
            if status.storage_available:
                return "OPENAI_API_KEY is active and takes precedence over saved credentials."
            return (
                "OPENAI_API_KEY is active. Persistent credential storage is unavailable "
                "on this system."
            )
        if status.source is CredentialSource.SYSTEM_KEYRING:
            return "OpenAI key loaded securely from the system credential vault."
        if status.stored is StoredCredentialState.PRESENT:
            return "The saved OpenAI key is invalid. Delete it and save a replacement."
        if not status.storage_available:
            return "OpenAI key not configured; secure credential storage is unavailable."
        return "OpenAI key not configured."

    @staticmethod
    def _unexpected_credential_error_code(
        action: Literal["load", "save", "delete"],
    ) -> CredentialErrorCode:
        return {
            "load": CredentialErrorCode.READ_FAILED,
            "save": CredentialErrorCode.WRITE_FAILED,
            "delete": CredentialErrorCode.DELETE_FAILED,
        }[action]

    @staticmethod
    def _credential_error_text(
        action: Literal["load", "save", "delete"],
        error_code: CredentialErrorCode | None,
    ) -> str:
        if error_code is CredentialErrorCode.INVALID_SECRET:
            return "That OpenAI API key has an invalid format; nothing was saved."
        if error_code is CredentialErrorCode.STORAGE_UNAVAILABLE:
            return "Secure credential storage is unavailable; the configuration is unchanged."
        return {
            "load": "Could not read the saved OpenAI key; no stored key was activated.",
            "save": "Could not save the OpenAI key securely; the configuration is unchanged.",
            "delete": "Could not delete the saved OpenAI key; the configuration is unchanged.",
        }[action]

    def _announce_openai_credential_result(
        self,
        action: Literal["load", "save", "delete"],
        *,
        was_enabled: bool,
        enabled: bool,
        source: CredentialSource,
    ) -> None:
        if action == "load":
            if enabled and not was_enabled:
                self.chat_panel.write_system("OpenAI credential loaded securely.")
            return
        if action == "save":
            if source is CredentialSource.ENVIRONMENT:
                message = (
                    "OpenAI credential saved securely; OPENAI_API_KEY remains active."
                    if enabled
                    else (
                        "OpenAI credential saved securely, but the invalid OPENAI_API_KEY "
                        "override still blocks it."
                    )
                )
                self.chat_panel.write_system(message)
            else:
                self.chat_panel.write_system("OpenAI credential saved securely.")
            return
        if enabled:
            self.chat_panel.write_system(
                "Saved OpenAI credential deleted; OPENAI_API_KEY remains active."
            )
        else:
            self.chat_panel.write_system(
                "Saved OpenAI credential deleted; natural-language routing is disabled."
            )

    def _dispatch_to_app(self, callback: Callable[[], None]) -> object:
        if self._closing:
            raise RuntimeError("app is closing")
        if threading.get_ident() == self._owner_thread_id:
            return callback()
        return self.call_from_thread(callback)

    def _service_coach_callbacks(self) -> None:
        if self._closing:
            return
        self.coach.drain_deferred_callbacks()
        self._service_global_safety_action()

    def _is_mounted_now(self) -> bool:
        try:
            running = getattr(self, "is_running", False)
            if callable(running):
                return bool(running())
            if running:
                return True
            mounted = getattr(self, "is_mounted", False)
            return bool(mounted) if isinstance(mounted, bool) else False
        except Exception:
            return False

    def _audio_generation_for(self, bridge: object) -> int:
        with self._audio_state_lock:
            if bridge is not self._audio_generation_bridge:
                self._audio_generation += 1
                self._audio_generation_bridge = bridge
            return self._audio_generation

    def _close_audio_generation_once(self, generation: int, bridge: object) -> bool:
        with self._audio_state_lock:
            if generation in self._closed_audio_generations:
                return False
            self._closed_audio_generations.add(generation)
        close = getattr(bridge, "close", None)
        if not callable(close):
            return False
        close()
        return True

    def _submit_audio_event(self, event: MidiEvent) -> DispatchReceipt:
        """Submit one bus event to the sole required audio consumer."""

        bridge = self.audio
        generation = self._audio_generation_for(bridge)
        submission_method = getattr(bridge, "handle_midi_event_submission", None)
        if callable(submission_method):
            try:
                submission = submission_method(event)
            except Exception:
                return self._rejected_audio_receipt(
                    bridge=bridge,
                    generation=generation,
                    safety=event.is_release,
                )
            return self._receipt_for_submission(
                bridge=bridge,
                generation=generation,
                submission=submission,
                event=event,
            )

        legacy_method = getattr(bridge, "handle_midi_event", None)
        if not callable(legacy_method):
            return _ImmediateDispatchReceipt("failed_safe")
        try:
            submitted = bool(legacy_method(event))
        except Exception:
            submitted = False
        return _ImmediateDispatchReceipt("accepted" if submitted else "failed_safe")

    def _receipt_for_submission(
        self,
        *,
        bridge: object,
        generation: int,
        submission: object,
        event: MidiEvent,
    ) -> DispatchReceipt:
        try:
            command_id = getattr(submission, "command_id", None)
            submitted = bool(getattr(submission, "submitted", False))
            status = str(getattr(submission, "status", "rejected")).strip().lower()
            safety = bool(getattr(submission, "safety", False)) or event.is_release
        except Exception:
            return self._rejected_audio_receipt(
                bridge=bridge,
                generation=generation,
                safety=event.is_release,
            )
        if isinstance(command_id, bool) or not isinstance(command_id, int) or command_id < 1:
            return self._rejected_audio_receipt(
                bridge=bridge,
                generation=generation,
                safety=safety,
            )

        receipt = _AudioDispatchReceipt(
            self,
            bridge=bridge,
            generation=generation,
            command_id=command_id,
            safety=safety,
        )
        if submitted and status == "accepted":
            receipt.transition("accepted")
        elif not submitted or status != "pending":
            self._reject_audio_receipt(receipt)
        return receipt

    def _rejected_audio_receipt(
        self,
        *,
        bridge: object,
        generation: int,
        safety: bool,
    ) -> DispatchReceipt:
        if not safety:
            return _ImmediateDispatchReceipt("failed_safe")
        receipt = _AudioDispatchReceipt(
            self,
            bridge=bridge,
            generation=generation,
            command_id=1,
            safety=True,
        )
        self._reject_audio_receipt(receipt)
        return receipt

    def _poll_audio_receipt(self, receipt: _AudioDispatchReceipt) -> None:
        with self._audio_state_lock:
            generation_is_current = (
                receipt.generation == self._audio_generation
                and receipt.bridge is self._audio_generation_bridge
            )
        if not generation_is_current:
            if receipt.safety:
                self._reject_audio_receipt(receipt)
            else:
                receipt.transition("failed_safe")
            return

        admission_method = getattr(receipt.bridge, "admission", None)
        if not callable(admission_method):
            if receipt.safety:
                self._reject_audio_receipt(receipt)
            else:
                receipt.transition("failed_safe")
            return
        try:
            admission = admission_method(receipt.command_id)
        except Exception:
            admission = None
            unavailable = True
        else:
            unavailable = False
        if admission is not None:
            try:
                admission_id = getattr(admission, "command_id", None)
                status = str(getattr(admission, "status", "rejected")).strip().lower()
            except Exception:
                self._reject_audio_receipt(receipt)
                return
            if (
                isinstance(admission_id, bool)
                or not isinstance(admission_id, int)
                or admission_id != receipt.command_id
            ):
                self._reject_audio_receipt(receipt)
                return
            if status == "accepted":
                receipt.transition("accepted")
            elif status != "pending":
                self._reject_audio_receipt(receipt)
            return
        if unavailable or self._audio_bridge_unavailable(receipt.bridge):
            self._reject_audio_receipt(receipt)

    @staticmethod
    def _audio_bridge_unavailable(bridge: object) -> bool:
        snapshot_method = getattr(bridge, "snapshot", None)
        if not callable(snapshot_method):
            return False
        try:
            snapshot = snapshot_method()
        except Exception:
            return True
        return bool(getattr(snapshot, "fatal", False)) or not bool(
            getattr(snapshot, "running", True)
        )

    def _reject_audio_receipt(self, receipt: _AudioDispatchReceipt) -> None:
        if not receipt.safety:
            receipt.transition("failed_safe")
            return
        receipt.transition("rejected_unsafe")
        start_thread = False
        with self._audio_state_lock:
            containment = self._audio_containments.get(receipt.generation)
            if containment is None:
                try:
                    captured_process = getattr(receipt.bridge, "process", None)
                except Exception:
                    captured_process = None
                containment = _AudioContainment(
                    generation=receipt.generation,
                    bridge=receipt.bridge,
                    captured_process=captured_process,
                )
                self._audio_containments[receipt.generation] = containment
            if containment.confirmed_stopped:
                receipt.transition("failed_safe")
                return
            if not any(candidate is receipt for candidate in containment.receipts):
                containment.receipts.append(receipt)
            if not containment.started:
                containment.started = True
                start_thread = True
        if start_thread:
            threading.Thread(
                target=self._contain_audio_generation,
                args=(receipt.generation,),
                name="halcyon-audio-containment",
                daemon=True,
            ).start()

    def _contain_audio_generation(self, generation: int) -> None:
        with self._audio_state_lock:
            containment = self._audio_containments.get(generation)
        if containment is None:
            return
        try:
            self._close_audio_generation_once(generation, containment.bridge)
        except Exception:
            pass
        confirmed = self._captured_audio_stopped(containment)
        receipts: tuple[_AudioDispatchReceipt, ...] = ()
        with self._audio_state_lock:
            current = self._audio_containments.get(generation)
            if current is not containment:
                return
            if confirmed:
                containment.confirmed_stopped = True
                receipts = tuple(containment.receipts)
        if not confirmed:
            return
        for receipt in receipts:
            receipt.transition("failed_safe")
        try:
            self._dispatch_to_app(
                lambda: self._restart_audio_generation(generation, containment.bridge)
            )
        except Exception:
            return

    @staticmethod
    def _captured_audio_stopped(containment: _AudioContainment) -> bool:
        process = containment.captured_process
        if process is not None:
            poll = getattr(process, "poll", None)
            if not callable(poll):
                return False
            try:
                return poll() is not None
            except Exception:
                return False
        try:
            current_process = getattr(containment.bridge, "process", None)
        except Exception:
            return False
        if current_process is not None:
            poll = getattr(current_process, "poll", None)
            if not callable(poll):
                return False
            try:
                if poll() is None:
                    return False
            except Exception:
                return False
        snapshot_method = getattr(containment.bridge, "snapshot", None)
        if not callable(snapshot_method):
            return False
        try:
            return not bool(getattr(snapshot_method(), "running", True))
        except Exception:
            return False

    def _restart_audio_generation(self, generation: int, bridge: object) -> None:
        if self._closing or self._resources_closed or not self._is_mounted_now():
            return
        with self._audio_state_lock:
            containment = self._audio_containments.get(generation)
            if (
                containment is None
                or not containment.confirmed_stopped
                or generation != self._audio_generation
                or bridge is not self._audio_generation_bridge
            ):
                return
            self._audio_generation += 1
            self._audio_containments.pop(generation, None)
            oldest_retained = max(0, self._audio_generation - 8)
            self._closed_audio_generations = {
                candidate
                for candidate in self._closed_audio_generations
                if candidate >= oldest_retained
            }
        start = getattr(bridge, "start", None)
        if callable(start):
            try:
                start()
                self._apply_dsp_patch(self.synth_patch.dsp_patch())
            except Exception:
                pass
        if not self._closing and self._is_mounted_now():
            self._refresh_audio_status()

    def action_workspace(self, workspace: str) -> None:
        if workspace not in {"stage", "controls", "activity", "console"}:
            return
        if workspace == "controls":
            self.action_toggle_settings()
            return
        self._workspace = workspace
        if not self._is_mounted_now():
            return
        shell = self.query_one("#app-shell", Vertical)
        for candidate in ("stage", "controls", "activity", "console"):
            shell.set_class(candidate == workspace, f"workspace-{candidate}")
            button = self.query_one(f"#workspace-{candidate}", Button)
            button.set_class(candidate == workspace, "selected")

    def action_toggle_settings(self) -> None:
        """Compatibility action: open the one persistent modal idempotently."""
        if self.screen is not self.settings_screen:
            self.push_screen(self.settings_screen)

    def action_close_settings(self) -> None:
        """Compatibility action delegated to the active modal lifecycle."""
        if self.screen is self.settings_screen:
            self.settings_screen.dismiss()

    def on_button_pressed(self, message: Button.Pressed) -> None:
        button_id = str(message.button.id or "")
        if button_id.startswith("workspace-"):
            message.stop()
            self.action_workspace(button_id.removeprefix("workspace-"))
            return
        scope_modes = {
            "scope-wave": ScopeMode.WAVE,
            "scope-orbit": ScopeMode.ORBIT,
            "scope-radial": ScopeMode.RADIAL,
        }
        if button_id in scope_modes:
            message.stop()
            if self._coach_highway_active:
                return
            self.scope.set_mode(scope_modes[button_id])
            self._refresh_scope_buttons()

    def _refresh_scope_buttons(self) -> None:
        if not self._is_mounted_now():
            return
        for mode in ScopeMode:
            button = self.query_one(f"#scope-{mode.value}", Button)
            button.set_class(self.scope.mode is mode, "selected")

    def _present_coach_snapshot(self, snapshot: CoachVisualizationSnapshot) -> None:
        """Present one read-only coach sample without influencing coach timing."""

        active = snapshot.active
        if active:
            next_targets = set(snapshot.target_notes)
        elif self._coach_highway_active:
            next_targets = set()
        else:
            next_targets = self.target_notes
        targets_changed = next_targets != self.target_notes
        visibility_changed = active != self._coach_highway_active
        self.target_notes = next_targets
        self._coach_highway_active = active
        if not self._is_mounted_now() or self._closing:
            return
        presentation_needs_sync = visibility_changed or self.coach_highway.display != active
        if presentation_needs_sync:
            self.query_one("#surface-mode-title", Static).update(
                "COACH" if active else "OSCILLATOR"
            )
            self.query_one("#scope-controls", Horizontal).display = not active
            self.scope.display = not active
            self.coach_highway.display = active
        if targets_changed or visibility_changed:
            self._refresh_keyboard_state()
        if targets_changed and next_targets:
            self._scroll_keyboard_to(min(next_targets))

    def _sync_highway_viewport(self, scroll_x: float) -> None:
        """Mirror the keyboard's reactive cell scroll into the coach projection."""

        self.coach_highway.set_viewport_origin(scroll_x)

    def on_unmount(self) -> None:
        """Release app-owned resources once at Textual's public exit boundary."""
        if self._resources_closed:
            return
        self._closing = True
        self._resources_closed = True
        self._console_unsubscribe()
        self._console_runs.clear()
        self.assistant_service.close()
        self.target_notes.clear()
        self._coach_highway_active = False
        self._credential_generation += 1
        self._credential_busy = False
        self.workers.cancel_group(self, "openai-credential")
        with self._audio_state_lock:
            self._global_safety_action = None

        try:
            self.coach.unmount()
        except Exception as exc:
            self.log.error(f"Failed to close coach engine: {type(exc).__name__}")

        try:
            self.event_bus.unsubscribe(self._ui_bus_subscription)
            self.event_bus.unsubscribe(self._audio_bus_subscription)
        except Exception as exc:
            self.log.error(f"Failed to close MIDI event bus: {type(exc).__name__}")

        audio = self.audio
        generation = self._audio_generation_for(audio)
        cleanups = (
            ("local control server", self.local_control_server.stop),
            (
                "audio bridge",
                lambda: self._close_audio_generation_once(generation, audio),
            ),
            ("MIDI input", self.midi.close),
        )
        for resource_name, cleanup in cleanups:
            try:
                cleanup()
            except Exception as exc:
                self.log.error(f"Failed to close {resource_name}: {type(exc).__name__}")

    def action_reconnect(self) -> None:
        self._global_all_notes_off(reconnect=True)

    def action_clear_log(self) -> None:
        self.event_table.clear()
        self.event_log.clear()
        self.event_count = 0
        self._refresh_status_rail()

    def _connect_midi(self) -> None:
        try:
            ports = self.midi.list_ports()
            port = self.midi.connect()
        except MidiUnavailable as exc:
            self.status.update(f"[disconnected] {exc} Press r to retry.")
            self._midi_source = "MIDI offline"
            self._refresh_status_rail()
            return
        except Exception as exc:
            self.status.update(f"[error] MIDI connection failed: {exc}. Press r to retry.")
            self._midi_source = "MIDI error"
            self._refresh_status_rail()
            return

        port_list = ", ".join(ports) if ports else "none"
        self.status.update(f"Input: {port}\nAvailable: {port_list}")
        self._midi_source = port
        self._refresh_status_rail()

    def _start_audio(self) -> None:
        self._audio_generation_for(self.audio)
        self.audio.start()
        self._apply_dsp_patch(self.synth_patch.dsp_patch())
        self._refresh_audio_status()

    def _start_local_control_server(self) -> None:
        try:
            host, port = self.local_control_server.start()
        except Exception as exc:
            self.chat_panel.write_system(f"MCP unavailable: {exc}")
            return
        self.chat_panel.write_system(f"MCP: http://{host}:{port}")

    def _refresh_audio_status(self) -> None:
        self.audio_status.update(self.audio.snapshot().text)
        self._refresh_status_rail()

    def _refresh_status_rail(self) -> None:
        if not self._is_mounted_now():
            return
        audio = self.audio.snapshot()
        try:
            viz = self.audio_visualization_snapshot()
        except Exception:
            viz = None
        audio_label = "AUDIO ready" if audio.ready else "AUDIO wait"
        viz_label = "VIZ linked" if viz is not None and viz.connected else "VIZ offline"
        preset = self.synth_patch.preset_name or "custom"
        self.status_rail.update(
            "  ".join(
                (
                    "[b]HALCYON[/b]",
                    f"MIDI {self._midi_source}",
                    audio_label,
                    viz_label,
                    f"PATCH {preset}",
                    f"EVENTS {self.event_count}",
                )
            )
        )

    def _poll_midi(self) -> None:
        if not self.midi.connected:
            return

        try:
            events = self.midi.poll()
        except Exception as exc:
            self.status.update(f"[error] MIDI polling failed: {exc}. Press r to reconnect.")
            self._global_all_notes_off()
            self.midi.close()
            return

        for event in events:
            self._handle_event(event)

    def _handle_event(self, event: MidiEvent) -> None:
        """Compatibility ingress: publish one normalized event exactly once."""

        self.event_bus.publish(event)

    def _observe_dispatched_event(self, event: MidiEvent) -> None:
        """Update bounded UI models from an already-dispatched bus event."""

        if self._closing:
            return
        self._refresh_keyboard_state()
        self.event_log.append(event)
        self.event_count += 1
        if not self._is_mounted_now():
            return
        if event.is_press:
            self._scroll_keyboard_to(event.note)
        origin = (
            Text("● YOU", style="bold #ff9f43")
            if event.origin is MidiOrigin.HUMAN
            else Text("◆ HALCYON", style="bold #ab9df2")
        )
        self.event_table.add_row(
            f"{event.elapsed:8.3f}s",
            origin,
            event.event_type,
            f"{event.note_name} ({event.note})",
            str(event.velocity),
            str(event.channel),
            f"0x{event.status:02X}",
            f"{event.delta_time:.4f}",
            event.source,
            key=str(self.event_count),
        )
        if len(self.event_table.rows) > self.event_log.max_events:
            first_key = next(iter(self.event_table.rows.keys()))
            self.event_table.remove_row(first_key)
        self.event_table.move_cursor(row=len(self.event_table.rows) - 1)
        self._refresh_status_rail()

    def _refresh_keyboard_state(self) -> None:
        human = {voice.note for voice in self.event_bus.active_voices(origin=MidiOrigin.HUMAN)}
        halcyon = {voice.note for voice in self.event_bus.active_voices(origin=MidiOrigin.HALCYON)}
        self.human_pressed_notes = human
        self.halcyon_pressed_notes = halcyon
        self.pressed_notes = human | halcyon
        if self._is_mounted_now() and not self._closing:
            self.keyboard.update_keys(
                human,
                self.target_notes,
                halcyon_notes=halcyon,
            )

    def _global_all_notes_off(self, *, reconnect: bool = False) -> bool:
        """Request global safety and continue only after admitted truth."""

        if self._closing:
            return False
        self._service_global_safety_action()
        with self._audio_state_lock:
            active = self._global_safety_action
            if active is not None:
                active.reconnect_requested = active.reconnect_requested or reconnect
                return False

        bridge = self.audio
        generation = self._audio_generation_for(bridge)
        structured_method = getattr(bridge, "all_notes_off_submission", None)
        if callable(structured_method):
            try:
                submission = structured_method()
            except Exception:
                receipt = self._rejected_audio_receipt(
                    bridge=bridge,
                    generation=generation,
                    safety=True,
                )
            else:
                receipt = self._receipt_for_global_safety_submission(
                    bridge=bridge,
                    generation=generation,
                    submission=submission,
                )
            action = _GlobalSafetyAction(
                generation=generation,
                bridge=bridge,
                receipt=receipt,
                reconnect_requested=reconnect,
            )
            with self._audio_state_lock:
                self._global_safety_action = action
            return self._service_global_safety_action()

        legacy_method = getattr(bridge, "all_notes_off", None)
        try:
            accepted = bool(legacy_method()) if callable(legacy_method) else False
        except Exception:
            accepted = False
        if accepted:
            self._complete_global_safety(reconnect=reconnect)
        return accepted

    def _receipt_for_global_safety_submission(
        self,
        *,
        bridge: object,
        generation: int,
        submission: object,
    ) -> DispatchReceipt:
        try:
            command_id = getattr(submission, "command_id", None)
            submitted = bool(getattr(submission, "submitted", False))
            status = str(getattr(submission, "status", "rejected")).strip().lower()
        except Exception:
            return self._rejected_audio_receipt(
                bridge=bridge,
                generation=generation,
                safety=True,
            )
        if isinstance(command_id, bool) or not isinstance(command_id, int) or command_id < 1:
            return self._rejected_audio_receipt(
                bridge=bridge,
                generation=generation,
                safety=True,
            )
        receipt = _AudioDispatchReceipt(
            self,
            bridge=bridge,
            generation=generation,
            command_id=command_id,
            safety=True,
        )
        if submitted and status == "accepted":
            receipt.transition("accepted")
        elif not submitted or status != "pending":
            self._reject_audio_receipt(receipt)
        return receipt

    def _service_global_safety_action(self) -> bool:
        with self._audio_state_lock:
            action = self._global_safety_action
        if action is None or self._closing:
            return False
        state = action.receipt.poll().state
        if state in {"accepted", "failed_safe"}:
            with self._audio_state_lock:
                if self._global_safety_action is not action:
                    return False
                self._global_safety_action = None
                generation_is_current = (
                    action.generation == self._audio_generation
                    and action.bridge is self._audio_generation_bridge
                )
                bridge_is_current = (
                    action.bridge is self.audio and action.bridge is self._audio_generation_bridge
                )
            safe_continuation = generation_is_current or (
                state == "failed_safe" and bridge_is_current
            )
            if safe_continuation and not self._closing:
                self._complete_global_safety(reconnect=action.reconnect_requested)
                return True
        return False

    def _complete_global_safety(self, *, reconnect: bool) -> None:
        if self._closing:
            return
        self.event_bus.reset_after_panic()
        self._refresh_keyboard_state()
        if reconnect and not self._closing:
            self._apply_dsp_patch(self.synth_patch.dsp_patch())
            self._connect_midi()

    def _scroll_keyboard_to(self, note: int) -> None:
        scroll_x = scroll_target_for_note(
            note,
            current_scroll=int(self.keyboard_scroll.scroll_x),
            viewport_width=self.keyboard_scroll.size.width,
        )
        if scroll_x is not None:
            self.keyboard_scroll.scroll_to(x=scroll_x, animate=False, immediate=True)

    def on_synth_rack_param_changed(self, message: SynthRack.ParamChanged) -> None:
        self._apply_dsp_patch(message.dsp_patch)
        self._refresh_status_rail()

    def on_synth_rack_preset_applied(self, message: SynthRack.PresetApplied) -> None:
        self._apply_dsp_patch(message.dsp_patch)
        self.chat_panel.write_system(f"Patch: {message.preset_name}")
        self._refresh_status_rail()

    def on_synth_rack_preset_saved(self, message: SynthRack.PresetSaved) -> None:
        try:
            self.user_preset_store.save(message.preset_name, message.values)
        except ValueError as exc:
            self.chat_panel.write_system(f"Preset save failed: {exc}")
            self.synth_rack.preset_save_failed(str(exc))
            return
        self.synth_rack.set_user_presets(self.user_preset_store.presets)
        self.synth_rack.preset_save_succeeded(message.preset_name)
        self.chat_panel.write_system(f"Saved patch: {message.preset_name}")
        self._refresh_status_rail()

    def on_chat_panel_submitted(self, message: ChatPanel.Submitted) -> None:
        if message.text.startswith("/"):
            self._execute_chat_slash_command(message.text)
            return
        self._submit_natural_chat(message.text)

    def on_chat_panel_rerun_requested(self, message: ChatPanel.RerunRequested) -> None:
        self._execute_chat_slash_command(message.command, local_origin="rerun")

    def _submit_natural_chat(self, text: str) -> None:
        # Starting the shared service is immediate; only its worker calls the model.
        self._run_assistant(text)

    def _execute_chat_slash_command(self, command: str, *, local_origin: str = "chat") -> bool:
        resolved = resolve_slash_command(self.menu_def, command)
        if resolved.status == "group":
            choices = ", ".join(
                f"{resolved.leaf_command} {shortcut}" for shortcut, _label in resolved.options
            )
            self.chat_panel.write_system(f"{resolved.message} {choices}")
            return False
        if resolved.status == "error":
            self.chat_panel.write_system(resolved.message)
            return False
        code, payload = self.mcp_local_run_slash(
            command=command, source="llm", local_origin=local_origin
        )
        if payload.get("deduplicated"):
            self._receive_console_event(
                ConsoleEvent(
                    event_id=uuid4().hex,
                    kind="log",
                    role="user",
                    source="You",
                    text="Already running; using the existing run.",
                    run_id=payload["run_id"],
                    status=payload["status"],
                )
            )
        if "run_id" not in payload:
            error = payload.get("error", "The command could not start.")
            if isinstance(error, dict):
                error = error.get("message", "The command could not start.")
            self.chat_panel.write_system(self.assistant.safe_text(str(error)))
        if payload.get("started") and payload.get("session_id"):
            self._chat_assistant_session_id = payload["session_id"]
        return code < 400

    def _remember_console_run(self, run_id: str, command: str, local_origin: str | None) -> None:
        safe = safe_console_text(self.assistant.safe_text(command, limit=8_000))
        replay = (
            command if safe == command and "\n" not in command and "\r" not in command else None
        )
        self._console_runs[run_id] = (safe, local_origin or "", replay)
        self._console_runs.move_to_end(run_id)
        while len(self._console_runs) > 1_000:
            self._console_runs.popitem(last=False)
        status = self._run_tracker.get_status(run_id)
        if status is not None:
            self._receive_console_event(
                ConsoleEvent(
                    event_id=f"private:{run_id}",
                    kind="command",
                    role="client",
                    source=str(status["source"]),
                    text=safe,
                    run_id=run_id,
                    parent_run_id=status.get("parent_run_id"),
                    session_id=status.get("session_id"),
                    operation_id=status.get("operation_id"),
                    status=status["status"],
                )
            )

    def _receive_console_event(self, event: ConsoleEvent) -> None:
        if self._closing:
            return
        if threading.get_ident() == self._owner_thread_id:
            self._render_console_event(event)
        else:
            # Never block an observer on the UI: unsubscribe waits for callbacks.
            self.post_message(_ConsoleEventMessage(event))

    def on__console_event_message(self, message: _ConsoleEventMessage) -> None:
        self._render_console_event(message.event)

    def _render_console_event(self, event: ConsoleEvent) -> None:
        if self._closing:
            return
        if event.kind == "command" and event.run_id:
            current = self._run_tracker.get_status(event.run_id)
            if (
                current is not None
                and current["status"] in CONSOLE_TERMINAL_STATES
                and event.status != current["status"]
            ):
                return
        private = self._console_runs.get(event.run_id or "")
        changes: dict[str, Any] = {
            "text": self.assistant.safe_text(event.text, limit=8_000),
            "raw_text": self.assistant.safe_text(event.raw_text, limit=8_000)
            if event.raw_text is not None
            else None,
            "details": self.assistant.safe_text(event.details, limit=8_000)
            if event.details is not None
            else None,
        }
        if private is not None:
            command, origin, replay = private
            changes["local_origin"] = origin or None
            if origin in {"chat", "rerun"}:
                if event.kind == "message" and event.role == "assistant":
                    changes["source"] = "Halcyon"
                else:
                    changes["source"] = "You" if origin == "chat" else "You · rerun"
                    changes["role"] = "user"
            elif origin == "assistant":
                changes["source"] = "Assistant"
            if event.kind == "command":
                changes.update(text=command, raw_text=command, replay_command=replay)
        self.chat_panel.append_event(replace(event, **changes))

    @staticmethod
    def _public_outcome_message(payload: dict[str, Any], *, fallback: str) -> str:
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:500]
        error = payload.get("error")
        if isinstance(error, dict):
            error_message = error.get("message")
            if isinstance(error_message, str) and error_message.strip():
                return error_message.strip()[:500]
        return fallback[:500]

    def _apply_dsp_patch(self, dsp_patch: dict[str, float]) -> bool:
        return self.audio.apply_patch(dsp_patch)

    def synth_tool_state(self) -> dict[str, object]:
        audio = self.audio.snapshot()
        human_voices = self.event_bus.active_voices(origin=MidiOrigin.HUMAN)
        halcyon_voices = self.event_bus.active_voices(origin=MidiOrigin.HALCYON)
        human_notes = {voice.note for voice in human_voices}
        halcyon_notes = {voice.note for voice in halcyon_voices}
        if audio.fatal:
            audio_label = "backpressure"
        elif audio.ready:
            audio_label = "ready"
        elif audio.running:
            audio_label = "starting"
        else:
            audio_label = "stopped"
        return {
            "patch": self.synth_patch.snapshot(),
            "audio": {
                "running": audio.running,
                "ready": audio.ready,
                "status": audio_label,
            },
            "presets": {
                "built_in": list(PRESETS),
                "user": self.user_preset_store.names(),
            },
            "keyboard": {
                "human_voice_count": len(human_voices),
                "halcyon_voice_count": len(halcyon_voices),
                "human_active_note_count": len(human_notes),
                "halcyon_active_note_count": len(halcyon_notes),
                "both_active_note_count": len(human_notes & halcyon_notes),
                "active_note_count": len(human_notes | halcyon_notes),
                "target_note_count": len(self.target_notes),
            },
            "coach": self._coach_public_state(),
        }

    def _assistant_context(self) -> dict[str, Any]:
        library = getattr(self.lesson_service.store, "library", None)
        try:
            lessons = library() if callable(library) else []
        except (CoachError, OSError):
            lessons = []
        return {
            "lesson_library": lessons,
            **self.synth_tool_state(),
        }

    def synth_tool_set_param(self, param_id: str, normalized_value: float) -> dict[str, object]:
        dsp_patch = self.synth_rack.apply_patch({param_id: normalized_value}, emit=False)
        self._apply_dsp_patch(dsp_patch)
        return self.synth_tool_state()

    def synth_tool_apply_patch(self, values: dict[str, float]) -> dict[str, object]:
        dsp_patch = self.synth_rack.apply_patch(values, emit=False)
        self._apply_dsp_patch(dsp_patch)
        return self.synth_tool_state()

    def synth_tool_apply_preset(self, preset_name: str) -> dict[str, object]:
        dsp_patch = self.synth_rack.apply_preset(preset_name, emit=False)
        self._apply_dsp_patch(dsp_patch)
        self.chat_panel.write_system(f"Patch: {preset_name}")
        return self.synth_tool_state()

    def synth_tool_save_preset(self, preset_name: str) -> dict[str, object]:
        self.user_preset_store.save(preset_name, self.synth_patch.values)
        self.synth_rack.set_user_presets(self.user_preset_store.presets)
        self.chat_panel.write_system(f"Saved patch: {preset_name}")
        return self.synth_tool_state()

    def synth_tool_all_notes_off(self) -> dict[str, object]:
        self._global_all_notes_off()
        return self.synth_tool_state()

    def coach_tool_start_lesson(self, **arguments: Any) -> PendingCommandOutcome:
        return self.coach.coach_tool_start_lesson(**arguments)

    def coach_tool_replay_lesson(self, **arguments: Any) -> PendingCommandOutcome:
        return self.coach.coach_tool_replay_lesson(**arguments)

    def coach_tool_start_attempt(self, **arguments: Any) -> PendingCommandOutcome:
        return self.coach.coach_tool_start_attempt(**arguments)

    def coach_tool_feedback(self, attempt_id: str | None = None) -> ImmediateCommandOutcome:
        return self.coach.coach_tool_feedback(attempt_id)

    def coach_tool_state(self) -> ImmediateCommandOutcome:
        return self.coach.coach_tool_state()

    def coach_tool_stop(self) -> ImmediateCommandOutcome:
        return self.coach.coach_tool_stop()

    def _coach_public_state(self) -> dict[str, Any]:
        try:
            payload = self.coach.state().to_public_dict()
        except Exception:
            return {
                "phase": "idle",
                "lesson_id": None,
                "attempt_id": None,
                "reports_retained": 0,
            }
        coach = payload.get("coach")
        return dict(coach) if isinstance(coach, dict) else {"phase": "idle"}

    def mcp_local_list_commands(self) -> list[dict[str, Any]]:
        commands: list[dict[str, Any]] = []
        for command, payload in iter_slash_leaf_commands(self.menu_def):
            policy = self._mcp_command_policy(
                command,
                leaf_command=self._normalize_mcp_command(command).lower(),
                payload=payload,
            )
            commands.append(
                {
                    "command": command,
                    "usage": str(payload.get("usage") or command).strip(),
                    "description": str(payload.get("description", "")).strip(),
                    "single_flight": bool(policy["single_flight"]),
                    "parallel_ok": bool(policy["parallel_ok"]),
                    "concurrency_group": str(policy["concurrency_group"]),
                    "poll_after_s": int(policy["poll_after_s"]),
                    "expected_duration_s": int(policy["expected_duration_s"]),
                }
            )
        return commands

    def mcp_local_state(self) -> dict[str, Any]:
        server = self.local_control_server
        tracker = self._run_tracker
        coach = getattr(self, "coach", None)
        if coach is None:
            coach_state: dict[str, Any] = {"phase": "idle"}
        else:
            try:
                raw_coach = coach.state().to_public_dict().get("coach", {})
            except Exception:
                raw_coach = {"phase": "idle"}
            coach_state = dict(raw_coach) if isinstance(raw_coach, dict) else {"phase": "idle"}
        return {
            "server": {
                "host": server.host,
                "port": server.port,
                "running": server.port is not None,
            },
            "tracker": tracker.get_state(),
            "assistant": {
                "enabled": bool(getattr(getattr(self, "assistant", None), "enabled", False)),
                "model": self.assistant.safe_text(self.assistant.model, limit=100),
                "chat_session_id": getattr(self, "_chat_assistant_session_id", None),
            },
            "synth": self.synth_tool_state(),
            "coach": coach_state,
        }

    def mcp_local_run_slash(
        self,
        *,
        command: str,
        source: str,
        timeout_s: float | None = None,
        parent_run_id: str | None = None,
        local_origin: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if parent_run_id is not None:
            parent = self._run_tracker.get_status(parent_run_id)
            if self._closing or parent is None or parent["status"] in TERMINAL_RUN_STATES:
                return 409, {"error": "The assistant request ended before command dispatch."}
        # Preserve quoted arguments and multiline goals until the shared shlex parser.
        clean_command = str(command or "").strip()
        source_key = self._normalize_mcp_source(source)
        if not clean_command.startswith("/"):
            return 400, {"error": "Slash command is required (must start with '/')."}

        try:
            invocation = prepare_slash_command(self.menu_def, clean_command)
        except CommandExecutionError as exc:
            return 400, {"error": str(exc)}

        if invocation.leaf_command in {"/coach stop", "/panic"}:
            self.assistant_service.interrupt(except_run_id=parent_run_id)

        if invocation.tool == "ask_assistant":
            if parent_run_id is not None:
                return 400, {"error": "Recursive assistant calls are not allowed."}
            outcome = execute_slash_command_outcome(
                self,
                self.menu_def,
                clean_command,
                tool_executor=lambda _host, _name, arguments: self.assistant_service.start(
                    **arguments, source=source_key
                ),
            )
            payload = outcome.to_public_dict()
            if payload.get("started"):
                self._remember_console_run(payload["run_id"], command, local_origin)
                payload.update(self._run_tracker.get_status(payload["run_id"]) or {})
                payload = self._enrich_mcp_run_payload(payload)
            return (202 if payload.get("started") else 409), payload

        policy = self._mcp_command_policy(
            clean_command,
            leaf_command=invocation.leaf_command,
            payload=invocation.payload,
        )
        if policy["single_flight"]:
            active = self._active_run_for_policy(policy)
            if active is not None:
                active_run_id = str(active.get("run_id", "")).strip()
                status = self._run_tracker.get_status(active_run_id) or dict(active)
                status["deduplicated"] = True
                status["deduplicated_run_id"] = active_run_id
                status["requested_command"] = self._public_tracker_command(
                    clean_command,
                    invocation.leaf_command,
                )
                status["deduplicated_reason"] = (
                    "A run in the same concurrency group is already active."
                )
                return 200, self._enrich_mcp_run_payload(status, policy=policy)

        timeout_value = self._coerce_mcp_timeout(timeout_s)
        run_id, _status = self._run_tracker.create_run(
            command=self._public_tracker_command(clean_command, invocation.leaf_command),
            source=source_key,
            timeout_s=timeout_value,
            parent_run_id=parent_run_id,
        )
        self._remember_console_run(run_id, command, local_origin)
        self._run_tracker.mark_running(run_id)
        self._record_run_event(run_id, "INFO", f"Started {invocation.leaf_command}")

        try:
            outcome = execute_slash_command_outcome(
                self,
                self.menu_def,
                clean_command,
                tool_executor=lambda _host, name, arguments: self._execute_tool_from_worker(
                    name,
                    arguments,
                ),
            )
        except (CommandExecutionError, ToolExecutionError, ValueError, TypeError) as exc:
            error = str(exc)
            self._record_run_event(run_id, "ERROR", error)
            self._run_tracker.mark_failed(run_id, error=error)
            status = self._run_tracker.get_status(run_id) or {"run_id": run_id}
            return 400, self._enrich_mcp_run_payload(status, policy=policy)

        if isinstance(outcome, PendingCommandOutcome):
            if not self._run_tracker.bind_pending(run_id, outcome):
                self._run_tracker.mark_failed(
                    run_id,
                    error="Pending coach operation could not be tracked.",
                )
            status = self._run_tracker.get_status(run_id) or {"run_id": run_id}
            status["effective_timeout_s"] = timeout_value
            http_status = (
                202 if str(status.get("status", "")).lower() in {"queued", "running"} else 200
            )
            return http_status, self._enrich_mcp_run_payload(status, policy=policy)

        result = outcome.to_public_dict()
        failed = outcome_is_failed = (
            (isinstance(outcome, TerminalCommandOutcome) and outcome.status != "completed")
            or result.get("ok") is False
            or "error" in result
        )
        if failed:
            error = self._public_outcome_message(result, fallback="Command failed.")
            self._record_run_event(run_id, "ERROR", error)
            self._run_tracker.mark_failed(run_id, error=error, result=result)
        else:
            self._record_run_event(run_id, "INFO", f"Completed {invocation.leaf_command}")
            self._run_tracker.mark_completed(run_id, success=True, result=result)
        status = self._run_tracker.get_status(run_id) or {"run_id": run_id}
        status["effective_timeout_s"] = timeout_value
        return (400 if outcome_is_failed else 200), self._enrich_mcp_run_payload(
            status,
            policy=policy,
        )

    def mcp_local_run_status(self, run_id: str) -> dict[str, Any] | None:
        payload = self._run_tracker.get_status(run_id)
        if payload is None:
            return None
        return self._enrich_mcp_run_payload(payload)

    def mcp_local_run_logs(self, run_id: str) -> dict[str, Any] | None:
        payload = self._run_tracker.get_logs(run_id)
        if payload is None:
            return None
        return self._enrich_mcp_run_payload(payload)

    def mcp_local_wait_for_run(
        self, run_id: str, *, cursor: int = 0, max_wait_s: float = 0.0
    ) -> dict[str, Any] | None:
        # LocalControlServer calls this directly from its HTTP worker, never the UI thread.
        payload = self._run_tracker.wait_for_run(run_id, cursor=cursor, max_wait_s=max_wait_s)
        return self._enrich_mcp_run_payload(payload) if payload is not None else None

    def audio_visualization_snapshot(self) -> AudioVisualizationSnapshot:
        """Return the latest raw waveform sample window for future UI widgets."""

        return self.audio.visualization_snapshot()

    def _execute_tool_from_worker(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        return self._run_on_app_thread(lambda: local_tool_executor(self)(name, arguments))

    def _run_assistant(
        self, message: str, session_id: str | None = None, new_session: bool = False
    ) -> None:
        selected_session = session_id if new_session else self._chat_assistant_session_id
        command = "/assistant ask " + shlex.quote(message)
        if selected_session:
            command += " --session-id " + selected_session
        _code, started = self._run_on_app_thread(
            lambda: self.mcp_local_run_slash(command=command, source="llm", local_origin="chat")
        )
        if not started.get("started"):
            error = started.get("error", {})
            text = (
                error.get("message", "The assistant request could not start.")
                if isinstance(error, dict)
                else str(error)
            )
            self.chat_panel.write_system(self.assistant.safe_text(text))
            return
        self._chat_assistant_session_id = started["session_id"]

    def _run_on_app_thread(self, callback: Callable[[], T]) -> T:
        if threading.get_ident() == getattr(self, "_owner_thread_id", threading.get_ident()):
            return callback()
        return self.call_from_thread(callback)

    @staticmethod
    def _normalize_mcp_command(command: str) -> str:
        return " ".join(str(command or "").strip().split())

    @staticmethod
    def _public_tracker_command(command: str, leaf_command: str) -> str:
        del command
        return " ".join(str(leaf_command or "").strip().split())[:500]

    @staticmethod
    def _normalize_mcp_source(source: str) -> str:
        source_key = str(source or "").strip().lower() or "llm"
        if source_key not in {"codex", "claude", "llm", "debugger"}:
            return "llm"
        return source_key

    @staticmethod
    def _coerce_mcp_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return default

    @staticmethod
    def _coerce_mcp_int(value: Any, default: int, *, minimum: int = 0, maximum: int = 3600) -> int:
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _coerce_mcp_timeout(value: float | None) -> float:
        try:
            parsed = float(value) if value is not None else 60.0
        except (TypeError, ValueError):
            parsed = 60.0
        return max(5.0, parsed)

    def _resolve_leaf_command_payload(
        self, command: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        normalized = self._normalize_mcp_command(command).lower()
        if not normalized.startswith("/"):
            return None, None
        best_command: str | None = None
        best_payload: dict[str, Any] | None = None
        best_len = -1
        for leaf_command, payload in iter_slash_leaf_commands(self.menu_def):
            leaf_norm = self._normalize_mcp_command(leaf_command).lower()
            if normalized == leaf_norm or normalized.startswith(f"{leaf_norm} "):
                if len(leaf_norm) > best_len:
                    best_command = leaf_norm
                    best_payload = payload
                    best_len = len(leaf_norm)
        return best_command, best_payload

    def _mcp_command_policy(
        self,
        command: str,
        *,
        leaf_command: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_mcp_command(command).lower()
        resolved_leaf = leaf_command
        resolved_payload = payload
        if resolved_leaf is None or resolved_payload is None:
            matched_leaf, matched_payload = self._resolve_leaf_command_payload(command)
            resolved_leaf = resolved_leaf or matched_leaf
            resolved_payload = resolved_payload or matched_payload

        command_key = resolved_leaf or normalized
        payload_dict = resolved_payload or {}
        parallel_ok = self._coerce_mcp_bool(payload_dict.get("mcp_parallel_ok"), True)
        single_flight = self._coerce_mcp_bool(payload_dict.get("mcp_single_flight"), True)
        if not parallel_ok:
            single_flight = True

        group_raw = str(payload_dict.get("mcp_concurrency_group", "")).strip().lower()
        return {
            "normalized_command": normalized,
            "leaf_command": resolved_leaf,
            "command_key": command_key,
            "concurrency_group": group_raw or command_key,
            "single_flight": single_flight,
            "parallel_ok": parallel_ok,
            "poll_after_s": self._coerce_mcp_int(
                payload_dict.get("mcp_poll_after_s"), default=5, minimum=1, maximum=60
            ),
            "expected_duration_s": self._coerce_mcp_int(
                payload_dict.get("mcp_expected_duration_s"),
                default=0,
                minimum=0,
                maximum=3600,
            ),
        }

    def _active_run_for_policy(self, policy: dict[str, Any]) -> dict[str, Any] | None:
        target_group = str(policy.get("concurrency_group", "")).strip().lower()
        if not target_group:
            return None
        for entry in self._run_tracker.get_state().get("active_runs", []):
            if not isinstance(entry, dict):
                continue
            command = str(entry.get("command", "")).strip()
            run_policy = self._mcp_command_policy(command)
            run_group = str(run_policy.get("concurrency_group", "")).strip().lower()
            if run_group == target_group:
                return entry
        return None

    def _enrich_mcp_run_payload(
        self,
        payload: dict[str, Any],
        *,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = str(payload.get("command", "")).strip()
        if policy is None:
            policy = self._mcp_command_policy(command)
        payload.setdefault("single_flight", bool(policy["single_flight"]))
        payload.setdefault("parallel_ok", bool(policy["parallel_ok"]))
        payload.setdefault("command_key", str(policy["command_key"]))
        payload.setdefault("concurrency_group", str(policy["concurrency_group"]))
        payload.setdefault("poll_after_s", int(policy["poll_after_s"]))
        expected_duration_s = int(policy.get("expected_duration_s") or 0)
        if expected_duration_s > 0:
            payload.setdefault("expected_duration_s", expected_duration_s)
        status = str(payload.get("status", "")).strip().lower()
        if status in {"queued", "running"}:
            payload.setdefault("do_not_rerun", True)
            payload.setdefault(
                "guidance",
                "Run is still in progress. Poll this same run_id until it reaches "
                "completed/failed/cancelled/timeout; do not submit a duplicate command.",
            )
        else:
            payload.setdefault("do_not_rerun", False)
        return payload

    def _record_run_event(self, run_id: str, level: str, message: str) -> None:
        self._run_tracker.record_log_event(
            {
                "run_id": run_id,
                "level": level,
                "message": message,
            }
        )


def main() -> None:
    HalcyonApp().run()
