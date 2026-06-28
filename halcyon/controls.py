"""Textual synth controls for the Python-owned patch state."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Static

from halcyon.synth_params import (
    PARAMS,
    PARAM_BY_ID,
    PRESETS,
    SynthParam,
    SynthPatch,
    clamp_normalized,
)

USER_PRESET_SLOT_COUNT = 4


class SynthKnob(Widget, can_focus=True):
    """A compact synth control with explicit decrement/increment buttons."""

    DEFAULT_CSS = """
    SynthKnob {
        width: 18;
        height: 7;
    }
    """

    normalized_value = reactive(0.0)

    class Changed(Message):
        """Posted when a knob value changes."""

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
        yield Static(self.param.label, classes="knob-label")
        yield self.value_label
        with Horizontal(classes="knob-adjust-row"):
            yield Button("-", id=f"{self.id}-down", classes="knob-step")
            yield self.meter
            yield Button("+", id=f"{self.id}-up", classes="knob-step")

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
        if event.key in {"right", "up"}:
            self.set_normalized(self.normalized_value + step)
        elif event.key in {"left", "down"}:
            self.set_normalized(self.normalized_value - step)
        elif event.key == "pageup":
            self.set_normalized(self.normalized_value + (step * 5))
        elif event.key == "pagedown":
            self.set_normalized(self.normalized_value - (step * 5))
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
        marker_count = 6
        filled = round(self.normalized_value * marker_count)
        indicator = "#" * filled + "-" * (marker_count - filled)
        value = self.param.format_dsp_value(self.param.dsp_from_normalized(self.normalized_value))
        self.value_label.update(value)
        self.meter.update(f"[{indicator}]")


class SynthRack(Widget):
    """Horizontal synth rack with knobs and preset buttons."""

    DEFAULT_CSS = """
    SynthRack {
        width: 100%;
        height: 13;
    }
    """

    class ParamChanged(Message):
        """Posted when a single synth param changes."""

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
        """Posted when a preset is applied."""

        def __init__(
            self, rack: "SynthRack", preset_name: str, dsp_patch: dict[str, float]
        ) -> None:
            super().__init__()
            self.rack = rack
            self.preset_name = preset_name
            self.dsp_patch = dsp_patch

    class PresetSaved(Message):
        """Posted when a user preset should be persisted."""

        def __init__(
            self,
            rack: "SynthRack",
            preset_name: str,
            values: dict[str, float],
        ) -> None:
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
        self._save_armed = False
        self._knobs: dict[str, SynthKnob] = {}
        self._user_slot_names: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="synth-rack-inner"):
            with Horizontal(id="knob-row"):
                for param in PARAMS:
                    knob = SynthKnob(param, self.patch.values[param.param_id])
                    self._knobs[param.param_id] = knob
                    yield knob
            with Horizontal(id="preset-row"):
                for preset_name in PRESETS:
                    yield Button(
                        preset_name,
                        id=f"preset-{preset_name.lower().replace(' ', '-')}",
                        classes="built-in-preset",
                    )
                yield Button("Save", id="preset-save")
                for slot_index, preset_name in enumerate(self._display_user_slots()):
                    button_id = f"user-preset-{slot_index}"
                    self._user_slot_names[button_id] = preset_name
                    yield Button(
                        self._user_button_label(preset_name),
                        id=button_id,
                        classes=self._user_button_classes(preset_name),
                    )

    def on_synth_knob_changed(self, message: SynthKnob.Changed) -> None:
        message.stop()
        dsp_patch = self.patch.set_param(message.param_id, message.normalized_value)
        self.post_message(
            self.ParamChanged(self, message.param_id, message.normalized_value, dsp_patch)
        )

    def on_button_pressed(self, message: Button.Pressed) -> None:
        label = str(message.button.label)
        button_id = str(message.button.id or "")
        if button_id == "preset-save":
            message.stop()
            self._save_armed = not self._save_armed
            self._refresh_preset_buttons()
            return
        if button_id.startswith("user-preset-"):
            message.stop()
            self._handle_user_preset_button(button_id)
            return
        if label not in PRESETS:
            return
        message.stop()
        dsp_patch = self.apply_preset(label, emit=False)
        self.post_message(self.PresetApplied(self, label, dsp_patch))

    def apply_preset(self, preset_name: str, *, emit: bool = True) -> dict[str, float]:
        if preset_name in PRESETS:
            dsp_patch = self.patch.apply_preset(preset_name)
        else:
            try:
                values = self.user_presets[preset_name]
            except KeyError as exc:
                raise ValueError(f"unknown synth preset: {preset_name}") from exc
            dsp_patch = self.patch.apply_patch(values, preset_name=preset_name)
        self._sync_knobs(emit=emit)
        if emit:
            self.post_message(self.PresetApplied(self, preset_name, dsp_patch))
        return dsp_patch

    def apply_patch(self, values: dict[str, float], *, emit: bool = True) -> dict[str, float]:
        dsp_patch = self.patch.apply_patch(values)
        self._sync_knobs(emit=False)
        if emit:
            for param_id, normalized_value in values.items():
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
        self._refresh_preset_buttons()

    def _handle_user_preset_button(self, button_id: str) -> None:
        preset_name = self._user_slot_names[button_id]
        if self._save_armed or preset_name not in self.user_presets:
            self._save_armed = False
            values = dict(self.patch.values)
            self.user_presets[preset_name] = values
            self._refresh_preset_buttons()
            self.post_message(self.PresetSaved(self, preset_name, values))
            return

        dsp_patch = self.apply_preset(preset_name, emit=False)
        self.post_message(self.PresetApplied(self, preset_name, dsp_patch))

    def _display_user_slots(self) -> list[str]:
        slots = list(self.user_presets)[:USER_PRESET_SLOT_COUNT]
        fallback_index = 1
        while len(slots) < USER_PRESET_SLOT_COUNT:
            candidate = f"User {fallback_index}"
            fallback_index += 1
            if candidate not in slots:
                slots.append(candidate)
        return slots

    def _refresh_preset_buttons(self) -> None:
        if not self.is_mounted:
            return
        save_button = self.query_one("#preset-save", Button)
        save_button.label = "Pick slot" if self._save_armed else "Save"
        save_button.set_class(self._save_armed, "save-armed")

        for slot_index, preset_name in enumerate(self._display_user_slots()):
            button_id = f"user-preset-{slot_index}"
            self._user_slot_names[button_id] = preset_name
            button = self.query_one(f"#{button_id}", Button)
            button.label = self._user_button_label(preset_name)
            button.set_class(preset_name in self.user_presets, "saved-preset")
            button.set_class(preset_name not in self.user_presets, "empty-preset")

    def _user_button_label(self, preset_name: str) -> str:
        if preset_name in self.user_presets:
            return _short_label(preset_name)
        return f"+ {_short_label(preset_name)}"

    def _user_button_classes(self, preset_name: str) -> str:
        return (
            "user-preset saved-preset"
            if preset_name in self.user_presets
            else "user-preset empty-preset"
        )


def _short_label(label: str, *, max_length: int = 10) -> str:
    if len(label) <= max_length:
        return label
    return f"{label[: max_length - 1]}."
