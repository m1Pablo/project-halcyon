"""Persistent user synth preset storage."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any

from halcyon.synth_params import PRESETS, validate_patch_values

PRESET_FILE_VERSION = 1
MAX_PRESET_NAME_LENGTH = 40


def default_preset_path() -> Path:
    """Return the local user preset file path."""

    if override := os.environ.get("HALCYON_PRESETS_PATH"):
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "project-halcyon" / "synth-presets.json"


def normalize_preset_name(name: str) -> str:
    """Normalize and validate a user-facing preset name."""

    normalized = re.sub(r"\s+", " ", name.strip())
    if not normalized:
        raise ValueError("preset name must be non-empty")
    if normalized in PRESETS:
        raise ValueError(f"{normalized!r} is a built-in preset and cannot be overwritten")
    if len(normalized) > MAX_PRESET_NAME_LENGTH:
        raise ValueError(f"preset name must be {MAX_PRESET_NAME_LENGTH} characters or less")
    return normalized


@dataclass
class UserPresetStore:
    """JSON-backed user preset catalog outside the repository."""

    path: Path = field(default_factory=default_preset_path)
    presets: dict[str, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "UserPresetStore":
        preset_path = path or default_preset_path()
        store = cls(path=preset_path)
        if not preset_path.exists():
            return store

        try:
            payload = json.loads(preset_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return store

        raw_presets = payload.get("presets") if isinstance(payload, dict) else None
        if not isinstance(raw_presets, dict):
            return store

        for raw_name, raw_values in raw_presets.items():
            if not isinstance(raw_name, str) or not isinstance(raw_values, dict):
                continue
            try:
                name = normalize_preset_name(raw_name)
                values = _coerce_values(raw_values)
            except (TypeError, ValueError):
                continue
            store.presets[name] = values
        return store

    def names(self) -> list[str]:
        return list(self.presets)

    def get(self, name: str) -> dict[str, float]:
        normalized = normalize_preset_name(name)
        try:
            values = self.presets[normalized]
        except KeyError as exc:
            raise ValueError(f"unknown user preset: {normalized}") from exc
        return dict(values)

    def save(self, name: str, values: dict[str, float]) -> dict[str, float]:
        normalized = normalize_preset_name(name)
        validated = validate_patch_values(values)
        self.presets[normalized] = dict(validated)
        self.flush()
        return dict(validated)

    def flush(self) -> None:
        payload = {
            "version": PRESET_FILE_VERSION,
            "presets": self.presets,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _coerce_values(raw_values: dict[Any, Any]) -> dict[str, float]:
    coerced = {str(param_id): float(value) for param_id, value in raw_values.items()}
    return validate_patch_values(coerced)
