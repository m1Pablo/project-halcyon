"""Textual synth controls and explicit preset-management UI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, Static

from halcyon.synth_params import (
    PARAMS,
    PARAM_BY_ID,
    PRESETS,
    SynthParam,
    SynthPatch,
    clamp_normalized,
)

PARAM_HELP = {
    "volume": "Master output level.",
    "tone": "Filter brightness and upper harmonic detail.",
    "shape": "Blend between saw and square oscillator shapes.",
    "attack": "Time for a new note to reach full level.",
    "decay": "Time for the initial peak to settle.",
    "sustain": "Level held while a key remains pressed.",
    "release": "Time for sound to fade after note-off.",
    "drive": "Harmonic saturation applied after the voice mix.",
}


class SynthKnob(Widget, can_focus=True):
    """A readable synth control with keyboard and button adjustment."""

    normalized_value = reactive(0.0)

    class Changed(Message):
        def __init__(self, knob: "SynthKnob", param_id: str, normalized_value: float) -> None:
            super().__init__()
            self.knob = knob
            self.param_id = param_id
            self.normalized_value = normalized_value

    def __init__(self, param: SynthParam, normalized_value: float, **kwargs: object) -> None:
        super().__init__(id=f"knob-{param.param_id}", **kwargs)
        self.param = param
        self.value_label = Static("", classes="knob-value")
        self.meter = Static("", classes="knob-meter")
        self.normalized_value = clamp_normalized(normalized_value)

    def compose(self) -> ComposeResult:
        with Vertical(classes="knob-copy"):
            yield Static(self.param.label, classes="knob-label")
            yield Static(PARAM_HELP[self.param.param_id], classes="knob-help")
        yield self.value_label
        with Horizontal(classes="knob-adjust-row"):
            yield Button("−", id=f"{self.id}-down", classes="knob-step", compact=True)
            yield self.meter
            yield Button("+", id=f"{self.id}-up", classes="knob-step", compact=True)

    def on_mount(self) -> None:
        self._refresh()

    def watch_normalized_value(self, _: float) -> None:
        self._refresh()

    def set_normalized(self, value: float, *, emit: bool = True) -> None:
        value = clamp_normalized(value)
        if value == self.normalized_value:
            return
        self.normalized_value = value
        if emit:
            self.post_message(self.Changed(self, self.param.param_id, value))

    def action_reset(self) -> None:
        self.set_normalized(self.param.default_normalized)

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        step = self.param.knob_step
        moves = {
            "right": step,
            "up": step,
            "left": -step,
            "down": -step,
            "pageup": step * 5,
            "pagedown": -step * 5,
        }
        if event.key in moves:
            self.set_normalized(self.normalized_value + moves[event.key])
        elif event.key == "home":
            self.set_normalized(0.0)
        elif event.key == "end":
            self.set_normalized(1.0)
        elif event.key == "r":
            self.action_reset()
        else:
            return
        event.stop()

    def on_button_pressed(self, message: Button.Pressed) -> None:
        button_id = str(message.button.id or "")
        if button_id.endswith("-down"):
            self.set_normalized(self.normalized_value - self.param.knob_step)
            message.stop()
        elif button_id.endswith("-up"):
            self.set_normalized(self.normalized_value + self.param.knob_step)
            message.stop()

    def _refresh(self) -> None:
        marker_count = 18
        filled = round(self.normalized_value * marker_count)
        value = self.param.format_dsp_value(self.param.dsp_from_normalized(self.normalized_value))
        self.value_label.update(value)
        self.meter.update("━" * filled + "─" * (marker_count - filled))


class SynthRack(Widget):
    """Scrollable drawer content with sound controls and a preset manager."""

    class ParamChanged(Message):
        def __init__(
            self,
            rack: "SynthRack",
            param_id: str,
            normalized_value: float,
            dsp_patch: dict[str, float],
        ) -> None:
            super().__init__()
            self.rack = rack
            self.param_id = param_id
            self.normalized_value = normalized_value
            self.dsp_patch = dsp_patch

    class PresetApplied(Message):
        def __init__(
            self, rack: "SynthRack", preset_name: str, dsp_patch: dict[str, float]
        ) -> None:
            super().__init__()
            self.rack = rack
            self.preset_name = preset_name
            self.dsp_patch = dsp_patch

    class PresetSaved(Message):
        def __init__(self, rack: "SynthRack", preset_name: str, values: dict[str, float]) -> None:
            super().__init__()
            self.rack = rack
            self.preset_name = preset_name
            self.values = values

    def __init__(
        self,
        patch: SynthPatch,
        *,
        user_presets: dict[str, dict[str, float]] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.patch = patch
        self.user_presets = dict(user_presets or {})
        self.selected_preset = patch.preset_name or "Init"
        self._knobs: dict[str, SynthKnob] = {}
        self._preset_buttons: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static("PRESETS", classes="settings-section-title")
        yield Static("Loaded  ·  Init", id="preset-loaded-state")
        yield Static("Select a candidate, then load it.", id="preset-help")
        with Vertical(id="preset-list"):
            for index, name in enumerate(PRESETS):
                button_id = f"preset-{name.lower().replace(' ', '-')}"
                self._preset_buttons[button_id] = name
                yield Button(self._preset_label(name), id=button_id, classes="preset-option")
            for index, name in enumerate(self.user_presets):
                button_id = f"user-preset-{index}"
                self._preset_buttons[button_id] = name
                yield Button(self._preset_label(name), id=button_id, classes="preset-option")
        with Horizontal(id="preset-actions"):
            yield Button("Load selected", id="preset-load", variant="primary")
            yield Button("Save new…", id="preset-save")
            yield Button("Reset", id="synth-reset")
        yield Static("", id="preset-feedback")
        with Vertical(id="preset-save-flow"):
            yield Static("SAVE PRESET", classes="settings-subtitle")
            yield Static(
                "Save the current sound as a new user preset. Built-ins are protected.",
                classes="settings-help",
            )
            yield Input(placeholder="Preset name", id="preset-name-input", max_length=40)
            with Horizontal(classes="save-actions"):
                yield Button("Save preset", id="preset-save-confirm", variant="success")
                yield Button("Cancel", id="preset-save-cancel")
        yield Static("SOUND PARAMETERS", classes="settings-section-title")
        yield Static(
            "Arrow keys adjust · Page Up/Down moves faster · R resets a control",
            classes="settings-help",
        )
        with Vertical(id="knob-row"):
            for param in PARAMS:
                knob = SynthKnob(param, self.patch.values[param.param_id])
                self._knobs[param.param_id] = knob
                yield knob

    def on_mount(self) -> None:
        self._set_save_flow(False)
        self._refresh_preset_state()

    def on_synth_knob_changed(self, message: SynthKnob.Changed) -> None:
        message.stop()
        dsp_patch = self.patch.set_param(message.param_id, message.normalized_value)
        self._refresh_preset_state()
        self.post_message(
            self.ParamChanged(self, message.param_id, message.normalized_value, dsp_patch)
        )

    def on_button_pressed(self, message: Button.Pressed) -> None:
        button_id = str(message.button.id or "")
        if button_id in self._preset_buttons:
            message.stop()
            self.selected_preset = self._preset_buttons[button_id]
            self._refresh_preset_state()
        elif button_id == "preset-load":
            message.stop()
            dsp_patch = self.apply_preset(self.selected_preset, emit=False)
            self.post_message(self.PresetApplied(self, self.selected_preset, dsp_patch))
        elif button_id == "synth-reset":
            message.stop()
            self.selected_preset = "Init"
            dsp_patch = self.apply_preset("Init", emit=False)
            self.post_message(self.PresetApplied(self, "Init", dsp_patch))
        elif button_id == "preset-save":
            message.stop()
            self._set_save_flow(True)
            self.query_one("#preset-name-input", Input).focus()
        elif button_id == "preset-save-cancel":
            message.stop()
            self.query_one("#preset-name-input", Input).value = ""
            self._set_feedback("")
            self._set_save_flow(False)
            self.query_one("#preset-save", Button).focus()
        elif button_id == "preset-save-confirm":
            message.stop()
            self._request_save()

    def on_input_submitted(self, message: Input.Submitted) -> None:
        if message.input.id == "preset-name-input":
            message.stop()
            self._request_save()

    def _request_save(self) -> None:
        name = self.query_one("#preset-name-input", Input).value.strip()
        if not name:
            self._set_feedback("Enter a preset name.", error=True)
            return
        if name in PRESETS:
            self._set_feedback("Built-in presets cannot be overwritten.", error=True)
            return
        values = dict(self.patch.values)
        self.post_message(self.PresetSaved(self, name, values))

    def preset_save_succeeded(self, preset_name: str) -> None:
        self.selected_preset = preset_name
        self.patch.preset_name = preset_name
        self._set_feedback(f"Saved and loaded · {preset_name}")
        self._set_save_flow(False)
        self._refresh_preset_state()

    def preset_save_failed(self, error: str) -> None:
        self._set_feedback(error, error=True)

    def apply_preset(self, preset_name: str, *, emit: bool = True) -> dict[str, float]:
        if preset_name in PRESETS:
            dsp_patch = self.patch.apply_preset(preset_name)
        else:
            try:
                values = self.user_presets[preset_name]
            except KeyError as exc:
                raise ValueError(f"unknown synth preset: {preset_name}") from exc
            dsp_patch = self.patch.apply_patch(values, preset_name=preset_name)
        self.selected_preset = preset_name
        self._sync_knobs(emit=emit)
        self._refresh_preset_state()
        if emit:
            self.post_message(self.PresetApplied(self, preset_name, dsp_patch))
        return dsp_patch

    def apply_patch(self, values: dict[str, float], *, emit: bool = True) -> dict[str, float]:
        dsp_patch = self.patch.apply_patch(values)
        self._sync_knobs(emit=False)
        self._refresh_preset_state()
        if emit:
            for param_id in values:
                param = PARAM_BY_ID[param_id]
                normalized = self.patch.values[param_id]
                self.post_message(
                    self.ParamChanged(
                        self,
                        param_id,
                        normalized,
                        {param.rust_name: param.dsp_from_normalized(normalized)},
                    )
                )
        return dsp_patch

    def _sync_knobs(self, *, emit: bool) -> None:
        for param_id, knob in self._knobs.items():
            knob.set_normalized(self.patch.values[param_id], emit=emit)

    def set_user_presets(self, user_presets: dict[str, dict[str, float]]) -> None:
        self.user_presets = dict(user_presets)
        if not self.is_mounted:
            return
        known = set(self._preset_buttons.values())
        for name in self.user_presets:
            if name in known:
                continue
            index = sum(button_id.startswith("user-preset-") for button_id in self._preset_buttons)
            button_id = f"user-preset-{index}"
            self._preset_buttons[button_id] = name
            self.query_one("#preset-list", Vertical).mount(
                Button(self._preset_label(name), id=button_id, classes="preset-option")
            )

    def _refresh_preset_state(self) -> None:
        if not self.is_mounted:
            return
        loaded = self.patch.preset_name or "Custom (edited)"
        self.query_one("#preset-loaded-state", Static).update(f"Loaded  ·  {loaded}")
        for button_id, name in self._preset_buttons.items():
            button = self.query_one(f"#{button_id}", Button)
            button.label = self._preset_label(name)
            button.set_class(name == self.selected_preset, "candidate")
            button.set_class(name == self.patch.preset_name, "loaded")

    def _preset_label(self, name: str) -> str:
        state = "✓ LOADED" if name == self.patch.preset_name else ""
        candidate = "▶" if name == self.selected_preset else " "
        kind = "BUILT-IN" if name in PRESETS else "USER"
        return f"{candidate}  {name}   {kind}   {state}".rstrip()

    def _set_save_flow(self, visible: bool) -> None:
        if self.is_mounted:
            self.query_one("#preset-save-flow").display = visible

    def _set_feedback(self, text: str, *, error: bool = False) -> None:
        if not self.is_mounted:
            return
        feedback = self.query_one("#preset-feedback", Static)
        feedback.update(text)
        feedback.set_class(error, "error")
