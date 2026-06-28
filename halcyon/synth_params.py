"""Synth parameter metadata, patch state, and preset mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, log
from typing import Literal

Scale = Literal["linear", "log"]


def clamp(value: float, minimum: float, maximum: float) -> float:
    """Clamp a floating point value into an inclusive range."""

    return min(max(float(value), minimum), maximum)


def clamp_normalized(value: float) -> float:
    """Clamp a UI normalized value into the `0..1` range."""

    return clamp(value, 0.0, 1.0)


@dataclass(frozen=True)
class SynthParam:
    """A single synth parameter exposed as a normalized UI control."""

    param_id: str
    label: str
    rust_name: str
    unit: str
    minimum: float
    maximum: float
    default: float
    scale: Scale = "linear"
    display_precision: int = 2
    knob_step: float = 0.02

    def normalized_from_dsp(self, value: float) -> float:
        value = clamp(value, self.minimum, self.maximum)
        if self.scale == "log":
            min_log = log(self.minimum)
            max_log = log(self.maximum)
            return (log(value) - min_log) / (max_log - min_log)
        return (value - self.minimum) / (self.maximum - self.minimum)

    def dsp_from_normalized(self, value: float) -> float:
        value = clamp_normalized(value)
        if self.scale == "log":
            min_log = log(self.minimum)
            max_log = log(self.maximum)
            return exp(min_log + ((max_log - min_log) * value))
        return self.minimum + ((self.maximum - self.minimum) * value)

    @property
    def default_normalized(self) -> float:
        return self.normalized_from_dsp(self.default)

    def format_dsp_value(self, value: float) -> str:
        value = clamp(value, self.minimum, self.maximum)
        if self.unit == "Hz":
            return f"{value:.0f} Hz"
        if self.unit == "ms":
            return f"{value * 1000:.0f} ms"
        if self.unit == "%":
            return f"{value * 100:.0f}%"
        if self.unit == "x":
            return f"{value:.1f}x"
        return f"{value:.{self.display_precision}f}"

    def snapshot(self, normalized_value: float) -> dict[str, str | float]:
        dsp_value = self.dsp_from_normalized(normalized_value)
        return {
            "id": self.param_id,
            "label": self.label,
            "rust_name": self.rust_name,
            "normalized": clamp_normalized(normalized_value),
            "value": dsp_value,
            "display": self.format_dsp_value(dsp_value),
            "unit": self.unit,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "default": self.default,
            "scale": self.scale,
        }


PARAMS: tuple[SynthParam, ...] = (
    SynthParam(
        "volume",
        "Volume",
        "master_gain",
        "%",
        0.0,
        1.0,
        0.24,
        display_precision=2,
    ),
    SynthParam(
        "tone",
        "Tone",
        "cutoff_hz",
        "Hz",
        80.0,
        12_000.0,
        3_600.0,
        scale="log",
    ),
    SynthParam(
        "shape",
        "Shape",
        "saw_square_blend",
        "%",
        0.0,
        1.0,
        0.45,
    ),
    SynthParam(
        "attack",
        "Attack",
        "attack_seconds",
        "ms",
        0.0,
        2.0,
        0.006,
        display_precision=3,
        knob_step=0.01,
    ),
    SynthParam(
        "decay",
        "Decay",
        "decay_seconds",
        "ms",
        0.0,
        2.0,
        0.12,
        display_precision=3,
    ),
    SynthParam(
        "sustain",
        "Sustain",
        "sustain_level",
        "%",
        0.0,
        1.0,
        0.72,
    ),
    SynthParam(
        "release",
        "Release",
        "release_seconds",
        "ms",
        0.0,
        4.0,
        0.25,
        display_precision=3,
    ),
    SynthParam(
        "drive",
        "Drive",
        "drive",
        "x",
        1.0,
        8.0,
        1.0,
        display_precision=1,
    ),
)

PARAM_BY_ID = {param.param_id: param for param in PARAMS}
PARAM_BY_RUST_NAME = {param.rust_name: param for param in PARAMS}
PARAM_ORDER = tuple(param.param_id for param in PARAMS)


def default_patch_values() -> dict[str, float]:
    """Return the default patch as normalized UI values."""

    return {param.param_id: param.default_normalized for param in PARAMS}


PRESETS: dict[str, dict[str, float]] = {
    "Init": default_patch_values(),
    "Pluck": {
        "volume": 0.32,
        "tone": 0.72,
        "shape": 0.20,
        "attack": 0.00,
        "decay": 0.08,
        "sustain": 0.00,
        "release": 0.04,
        "drive": 0.16,
    },
    "Soft Pad": {
        "volume": 0.28,
        "tone": 0.48,
        "shape": 0.42,
        "attack": 0.22,
        "decay": 0.36,
        "sustain": 0.68,
        "release": 0.30,
        "drive": 0.08,
    },
    "Bass": {
        "volume": 0.42,
        "tone": 0.34,
        "shape": 0.74,
        "attack": 0.00,
        "decay": 0.12,
        "sustain": 0.54,
        "release": 0.05,
        "drive": 0.32,
    },
    "Lead": {
        "volume": 0.36,
        "tone": 0.82,
        "shape": 0.58,
        "attack": 0.01,
        "decay": 0.10,
        "sustain": 0.66,
        "release": 0.08,
        "drive": 0.24,
    },
}


def validate_patch_values(values: dict[str, float]) -> dict[str, float]:
    """Validate and clamp a normalized patch mapping."""

    unknown = sorted(set(values) - set(PARAM_BY_ID))
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"unknown synth parameter(s): {joined}")
    return {param_id: clamp_normalized(value) for param_id, value in values.items()}


def normalized_to_dsp_patch(values: dict[str, float]) -> dict[str, float]:
    """Convert normalized UI parameter values to Rust DSP parameter values."""

    validated = validate_patch_values(values)
    return {
        PARAM_BY_ID[param_id].rust_name: PARAM_BY_ID[param_id].dsp_from_normalized(value)
        for param_id, value in validated.items()
    }


@dataclass
class SynthPatch:
    """Mutable synth patch state owned by the Python UI/control plane."""

    values: dict[str, float] = field(default_factory=default_patch_values)
    preset_name: str | None = "Init"

    def set_param(self, param_id: str, normalized_value: float) -> dict[str, float]:
        if param_id not in PARAM_BY_ID:
            raise ValueError(f"unknown synth parameter: {param_id}")
        normalized = clamp_normalized(normalized_value)
        self.values[param_id] = normalized
        self.preset_name = None
        param = PARAM_BY_ID[param_id]
        return {param.rust_name: param.dsp_from_normalized(normalized)}

    def apply_patch(
        self, values: dict[str, float], *, preset_name: str | None = None
    ) -> dict[str, float]:
        validated = validate_patch_values(values)
        self.values.update(validated)
        self.preset_name = preset_name
        return normalized_to_dsp_patch(validated)

    def apply_preset(self, preset_name: str) -> dict[str, float]:
        try:
            preset = PRESETS[preset_name]
        except KeyError as exc:
            available = ", ".join(PRESETS)
            raise ValueError(
                f"unknown synth preset: {preset_name}. Available: {available}"
            ) from exc
        return self.apply_patch(preset, preset_name=preset_name)

    def dsp_patch(self) -> dict[str, float]:
        return normalized_to_dsp_patch(self.values)

    def snapshots(self) -> list[dict[str, str | float]]:
        return [param.snapshot(self.values[param.param_id]) for param in PARAMS]

    def snapshot(self) -> dict[str, object]:
        return {
            "preset": self.preset_name,
            "params": self.snapshots(),
        }
