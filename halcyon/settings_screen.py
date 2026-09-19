"""Persistent modal surface for Halcyon's application settings."""

from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from halcyon.controls import SynthRack

MAX_OPENAI_API_KEY_LENGTH = 512
MAX_CREDENTIAL_STATUS_LENGTH = 240


class SecretInput(Input):
    """A masked input with payload-free DOM events and no clipboard copy/cut."""

    class ValueChanged(Message):
        """Payload-free replacement for Textual's value-bearing Changed message."""

    class SubmitRequested(Message):
        """Payload-free replacement for Textual's value-bearing Submitted message."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            password=True,
            max_length=MAX_OPENAI_API_KEY_LENGTH,
            **kwargs,
        )

    def action_copy(self) -> None:
        """Keep a selected secret out of every Textual clipboard path."""

    def action_cut(self) -> None:
        """Keep a selected secret out of every Textual clipboard path."""

    def post_message(self, message: Message) -> bool:
        """Keep stock Input messages containing plaintext off the DOM message queue."""

        if isinstance(message, Input.Changed):
            return super().post_message(self.ValueChanged())
        if isinstance(message, Input.Submitted):
            return super().post_message(self.SubmitRequested())
        if isinstance(message, Input.Blurred):
            return False
        return super().post_message(message)


class SoundSettingsScreen(ModalScreen[None]):
    """Own the complete lifecycle of Halcyon's persistent settings modal."""

    class SaveOpenAIKeyRequested(Message):
        """Request persistence without carrying the secret in the message."""

    class DeleteOpenAIKeyRequested(Message):
        """Request deletion without carrying credential material."""

    AUTO_FOCUS = "#close-settings"
    BINDINGS = [Binding("escape", "close", "Close", show=False, priority=True)]

    def __init__(self, synth_rack: SynthRack) -> None:
        super().__init__(id="sound-settings-screen")
        self.synth_rack = synth_rack
        self._size_class = ""
        self._credential_configured = False
        self._credential_stored = False
        self._credential_busy = False
        self._credential_error = False
        self._credential_input_available = True
        self._credential_status_text = "OpenAI key not configured."
        self._delete_confirmation = False
        self._save_request_pending = False
        self._delete_request_pending = False
        self.openai_key_input = SecretInput(
            placeholder="Paste a new OpenAI API key",
            id="openai-key-input",
            select_on_focus=True,
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-drawer"):
            with Horizontal(id="settings-header"):
                with Vertical(id="settings-heading"):
                    yield Static("SETTINGS", classes="panel-title")
                    yield Static(
                        "Configure Halcyon without leaving the performance stage.",
                        classes="settings-help",
                    )
                yield Button("Close  Esc", id="close-settings", compact=True)
            with ScrollableContainer(id="settings-scroll"):
                yield Static("OPENAI CREDENTIALS", classes="settings-section-title")
                yield Static(
                    "Masked here and stored in your operating system credential vault. "
                    "Coach authoring also needs a configured model.",
                    classes="settings-help",
                    id="openai-credential-help",
                    markup=False,
                )
                yield Static(
                    self._credential_status_text,
                    id="openai-credential-status",
                    markup=False,
                )
                with Horizontal(id="openai-credential-actions"):
                    yield self.openai_key_input
                    yield Button(
                        "Save securely",
                        id="openai-key-save",
                        variant="success",
                    )
                    yield Button("Delete saved key", id="openai-key-delete")
                yield Static("", id="openai-credential-feedback", markup=False)
                yield self.synth_rack

    def on_mount(self) -> None:
        self._apply_size_class(self.size.width, self.size.height)
        self._refresh_credential_controls()

    def on_screen_suspend(self, _event: events.ScreenSuspend) -> None:
        """Forget transient credential UI state whenever the modal leaves view."""

        self._clear_openai_key_input()
        self._reset_delete_confirmation(clear_feedback=True)

    def on_screen_resume(self, _event: events.ScreenResume) -> None:
        """Apply status that may have arrived before this installed screen mounted."""

        self._refresh_credential_controls()

    def on_unmount(self) -> None:
        self.openai_key_input.value = ""
        self._save_request_pending = False
        self._delete_confirmation = False
        self._delete_request_pending = False

    def on_resize(self, event: events.Resize) -> None:
        self._apply_size_class(event.size.width, event.size.height)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Keep the existing preset save actions reachable below credential settings."""

        if event.widget.id == "preset-name-input":
            self.call_after_refresh(self._scroll_preset_save_actions_visible)

    def on_click(self, event: events.Click) -> None:
        """Dismiss only when Textual identifies this screen as the click target."""
        if event.widget is self:
            event.stop()
            self.action_close()

    def on_button_pressed(self, message: Button.Pressed) -> None:
        button_id = str(message.button.id or "")
        if button_id == "close-settings":
            message.stop()
            self.action_close()
        elif button_id == "openai-key-save":
            message.stop()
            self._request_openai_key_save()
        elif button_id == "openai-key-delete":
            message.stop()
            self._request_openai_key_delete()

    @on(SecretInput.SubmitRequested)
    def _on_openai_key_submitted(self, message: SecretInput.SubmitRequested) -> None:
        message.stop()
        self._request_openai_key_save()

    @on(SecretInput.ValueChanged)
    def _on_openai_key_changed(self, message: SecretInput.ValueChanged) -> None:
        message.stop()
        self._reset_delete_confirmation(clear_feedback=True)

    def action_close(self) -> None:
        self._clear_openai_key_input()
        self._reset_delete_confirmation(clear_feedback=True)
        self.dismiss()

    def take_openai_key(self) -> str | None:
        """Return the pending masked value once and immediately blank the widget."""

        if not self.is_mounted or not self._credential_input_available:
            self.openai_key_input.value = ""
            return None
        key_input = self.openai_key_input
        value = key_input.value.strip()
        key_input.value = ""
        self._save_request_pending = False
        self._refresh_credential_controls()
        return value or None

    def set_openai_credential_status(
        self,
        text: str,
        *,
        configured: bool,
        stored: bool,
        busy: bool,
        error: bool = False,
        input_available: bool = True,
    ) -> None:
        """Update bounded, non-secret credential state supplied by the app."""

        normalized = " ".join(str(text).split())[:MAX_CREDENTIAL_STATUS_LENGTH]
        self._credential_status_text = normalized or "OpenAI credential status unavailable."
        self._credential_configured = bool(configured)
        self._credential_stored = bool(stored)
        self._credential_busy = bool(busy)
        self._credential_error = bool(error)
        self._credential_input_available = bool(input_available)
        if not self._credential_input_available:
            self.openai_key_input.value = ""
        self._save_request_pending = False
        self._delete_request_pending = False
        self._reset_delete_confirmation(clear_feedback=True)
        self._refresh_credential_controls()

    def _request_openai_key_save(self) -> None:
        if not self._credential_input_available:
            self._clear_openai_key_input()
            self._set_credential_feedback(
                "Secure credential entry is unavailable.",
                error=True,
            )
            return
        if self._credential_busy or self._save_request_pending:
            return
        key_input = self.openai_key_input
        if not key_input.value.strip():
            self._set_credential_feedback("Enter an OpenAI API key.", error=True)
            return
        self._reset_delete_confirmation(clear_feedback=True)
        self._save_request_pending = True
        self._refresh_credential_controls()
        self.post_message(self.SaveOpenAIKeyRequested())

    def _request_openai_key_delete(self) -> None:
        if self._credential_busy or self._delete_request_pending or not self._credential_stored:
            return
        if not self._delete_confirmation:
            self._delete_confirmation = True
            self._set_credential_feedback(
                "Press Confirm delete to remove the saved key.",
                error=False,
            )
            self._refresh_credential_controls()
            return
        self._delete_confirmation = False
        self._delete_request_pending = True
        self._set_credential_feedback("")
        self._refresh_credential_controls()
        self.post_message(self.DeleteOpenAIKeyRequested())

    def _clear_openai_key_input(self) -> None:
        self._save_request_pending = False
        self.openai_key_input.value = ""
        if self.is_mounted:
            self._refresh_credential_controls()

    def _reset_delete_confirmation(self, *, clear_feedback: bool) -> None:
        self._delete_confirmation = False
        if clear_feedback and self.is_mounted:
            self._set_credential_feedback("")
        self._refresh_credential_controls()

    def _set_credential_feedback(self, text: str, *, error: bool = False) -> None:
        if not self.is_mounted:
            return
        feedback = self.query_one("#openai-credential-feedback", Static)
        feedback.update(" ".join(str(text).split())[:MAX_CREDENTIAL_STATUS_LENGTH])
        feedback.set_class(bool(error), "error")

    def _refresh_credential_controls(self) -> None:
        if not self.is_mounted:
            return
        status = self.query_one("#openai-credential-status", Static)
        status.update(self._credential_status_text)
        status.set_class(self._credential_configured, "configured")
        status.set_class(self._credential_stored, "stored")
        status.set_class(self._credential_busy, "busy")
        status.set_class(self._credential_error, "error")

        save = self.query_one("#openai-key-save", Button)
        delete = self.query_one("#openai-key-delete", Button)
        self.openai_key_input.disabled = (
            self._credential_busy or not self._credential_input_available
        )
        save.disabled = (
            self._credential_busy
            or self._save_request_pending
            or not self._credential_input_available
        )
        delete.disabled = (
            self._credential_busy or self._delete_request_pending or not self._credential_stored
        )
        delete.label = "Confirm delete" if self._delete_confirmation else "Delete saved key"

    def _scroll_preset_save_actions_visible(self) -> None:
        if not self.is_mounted:
            return
        self.query_one("#preset-save-confirm", Button).scroll_visible(
            animate=False,
            immediate=True,
        )

    def _apply_size_class(self, width: int, height: int) -> None:
        if width < 110 or height < 36:
            size_class = "-compact"
        elif width < 150 or height < 48:
            size_class = "-medium"
        else:
            size_class = "-wide"
        if size_class == self._size_class:
            return
        for candidate in ("-wide", "-medium", "-compact"):
            self.set_class(candidate == size_class, candidate)
        self._size_class = size_class
