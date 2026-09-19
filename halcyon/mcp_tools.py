"""Safe synth tool contract shared by chat, HTTP, and MCP stdio."""

from __future__ import annotations

import json
from typing import Any, Protocol

from halcyon.coach_models import CoachError
from halcyon.synth_params import PARAMS, PRESETS


class ToolHost(Protocol):
    """Minimal host interface required by the Halcyon tool executor."""

    def synth_tool_state(self) -> dict[str, Any]: ...

    def synth_tool_set_param(self, param_id: str, normalized_value: float) -> dict[str, Any]: ...

    def synth_tool_apply_patch(self, values: dict[str, float]) -> dict[str, Any]: ...

    def synth_tool_apply_preset(self, preset_name: str) -> dict[str, Any]: ...

    def synth_tool_save_preset(self, preset_name: str) -> dict[str, Any]: ...

    def synth_tool_all_notes_off(self) -> dict[str, Any]: ...

    def coach_tool_start_lesson(
        self,
        *,
        query: str,
        segment: str | None,
        tempo_percent: int,
        difficulty: str | None,
        hand: str | None,
        loops: int,
        author: str,
    ) -> Any: ...

    def coach_tool_replay_lesson(
        self,
        *,
        segment: str | None,
        tempo_percent: int | None,
        loops: int | None,
    ) -> Any: ...

    def coach_tool_start_attempt(
        self,
        *,
        segment: str | None,
        tempo_percent: int | None,
        count_in: int | None,
    ) -> Any: ...

    def coach_tool_feedback(self, attempt_id: str | None = None) -> Any: ...

    def coach_tool_state(self) -> Any: ...

    def coach_tool_stop(self) -> Any: ...


class ToolExecutionError(ValueError):
    """Raised when a tool call is invalid or cannot complete."""


def tool_definitions() -> list[dict[str, Any]]:
    """Return internal leaf schemas; public MCP still exposes command control only."""

    param_enum = [param.param_id for param in PARAMS]
    return [
        {
            "name": "get_synth_state",
            "description": "Read current synth patch, audio status, and aggregate pressed-note state.",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "list_synth_params",
            "description": "List safe synth parameters, normalized ranges, labels, units, and preset names.",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "set_synth_param",
            "description": "Set one synth parameter using a normalized 0..1 value.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "param": {"type": "string", "enum": param_enum},
                    "value": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                "required": ["param", "value"],
            },
        },
        {
            "name": "apply_synth_patch",
            "description": "Apply a batch of normalized synth parameter values atomically from the UI perspective.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "values": {
                        "type": "object",
                        "additionalProperties": {"type": "number"},
                        "description": "Map of synth param id to normalized 0..1 value.",
                    }
                },
                "required": ["values"],
            },
        },
        {
            "name": "apply_synth_preset",
            "description": "Apply a named Halcyon built-in or saved user synth preset.",
            "inputSchema": {
                "type": "object",
                "properties": {"preset": {"type": "string"}},
                "required": ["preset"],
            },
        },
        {
            "name": "save_synth_preset",
            "description": "Save the current synth patch as a named user preset.",
            "inputSchema": {
                "type": "object",
                "properties": {"preset": {"type": "string"}},
                "required": ["preset"],
            },
        },
        {
            "name": "all_notes_off",
            "description": "Release all currently playing notes for stuck-note recovery.",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "start_coach_lesson",
            "description": (
                "Internal leaf for one registered /coach teach workflow: resolve a saved "
                "lesson or one local bounded approximation, demonstrate, capture, and score."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 160},
                    "segment": {"type": ["string", "null"], "maxLength": 80},
                    "tempo_percent": {"type": "integer", "minimum": 25, "maximum": 200},
                    "difficulty": {
                        "type": ["string", "null"],
                        "enum": ["foundation", "standard", "challenge", None],
                    },
                    "hand": {
                        "type": ["string", "null"],
                        "enum": ["left", "right", "both", None],
                    },
                    "loops": {"type": "integer", "minimum": 1, "maximum": 8},
                    "author": {"type": "string", "enum": ["never", "bounded"]},
                },
                "required": ["query", "tempo_percent", "loops", "author"],
            },
        },
        {
            "name": "replay_coach_lesson",
            "description": "Internal leaf for registered replay of the active saved lesson only.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "segment": {"type": ["string", "null"], "maxLength": 80},
                    "tempo_percent": {
                        "type": ["integer", "null"],
                        "minimum": 25,
                        "maximum": 200,
                    },
                    "loops": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 8,
                    },
                },
                "required": [],
            },
        },
        {
            "name": "start_coach_attempt",
            "description": (
                "Internal leaf to count in, capture only the player's post-bus MIDI, and score it."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "segment": {"type": ["string", "null"], "maxLength": 80},
                    "tempo_percent": {
                        "type": ["integer", "null"],
                        "minimum": 25,
                        "maximum": 200,
                    },
                    "count_in": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 8,
                    },
                },
                "required": [],
            },
        },
        {
            "name": "get_coach_feedback",
            "description": (
                "Internal leaf to end the current capture early or return one aggregate report; "
                "never returns raw MIDI history."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"attempt_id": {"type": ["string", "null"], "maxLength": 128}},
                "required": [],
            },
        },
        {
            "name": "get_coach_state",
            "description": "Internal leaf returning bounded aggregate coach state only.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "stop_coach_session",
            "description": (
                "Internal leaf requesting cancellation and owner-scoped Halcyon voice cleanup; "
                "it is not a global panic."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
        },
    ]


def list_synth_params() -> dict[str, Any]:
    return {
        "params": [
            {
                "id": param.param_id,
                "label": param.label,
                "rust_name": param.rust_name,
                "unit": param.unit,
                "minimum": param.minimum,
                "maximum": param.maximum,
                "default": param.default,
                "default_normalized": param.default_normalized,
                "scale": param.scale,
            }
            for param in PARAMS
        ],
        "presets": list(PRESETS),
    }


def execute_tool(host: ToolHost, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Execute one internal leaf against the running app host."""

    arguments = arguments or {}
    try:
        if name == "get_synth_state":
            return host.synth_tool_state()
        if name == "list_synth_params":
            return list_synth_params()
        if name == "set_synth_param":
            param = _required_string(arguments, "param")
            value = _required_float(arguments, "value")
            return host.synth_tool_set_param(param, value)
        if name == "apply_synth_patch":
            raw_values = arguments.get("values")
            if not isinstance(raw_values, dict):
                raise ToolExecutionError("values must be an object")
            values = {str(param): float(value) for param, value in raw_values.items()}
            return host.synth_tool_apply_patch(values)
        if name == "apply_synth_preset":
            return host.synth_tool_apply_preset(_required_string(arguments, "preset"))
        if name == "save_synth_preset":
            return host.synth_tool_save_preset(_required_string(arguments, "preset"))
        if name == "all_notes_off":
            return host.synth_tool_all_notes_off()
        if name == "start_coach_lesson":
            _require_allowed_arguments(
                arguments,
                {
                    "query",
                    "segment",
                    "tempo_percent",
                    "difficulty",
                    "hand",
                    "loops",
                    "author",
                },
            )
            return host.coach_tool_start_lesson(
                query=_bounded_string(arguments, "query", 160),
                segment=_optional_bounded_string(arguments, "segment", 80),
                tempo_percent=_bounded_int(arguments, "tempo_percent", 25, 200),
                difficulty=_optional_enum(
                    arguments,
                    "difficulty",
                    {"foundation", "standard", "challenge"},
                ),
                hand=_optional_enum(arguments, "hand", {"left", "right", "both"}),
                loops=_bounded_int(arguments, "loops", 1, 8),
                author=_required_enum(arguments, "author", {"never", "bounded"}),
            )
        if name == "replay_coach_lesson":
            _require_allowed_arguments(arguments, {"segment", "tempo_percent", "loops"})
            return host.coach_tool_replay_lesson(
                segment=_optional_bounded_string(arguments, "segment", 80),
                tempo_percent=_optional_bounded_int(arguments, "tempo_percent", 25, 200),
                loops=_optional_bounded_int(arguments, "loops", 1, 8),
            )
        if name == "start_coach_attempt":
            _require_allowed_arguments(arguments, {"segment", "tempo_percent", "count_in"})
            return host.coach_tool_start_attempt(
                segment=_optional_bounded_string(arguments, "segment", 80),
                tempo_percent=_optional_bounded_int(arguments, "tempo_percent", 25, 200),
                count_in=_optional_bounded_int(arguments, "count_in", 0, 8),
            )
        if name == "get_coach_feedback":
            _require_allowed_arguments(arguments, {"attempt_id"})
            return host.coach_tool_feedback(_optional_bounded_string(arguments, "attempt_id", 128))
        if name == "get_coach_state":
            _require_no_arguments(arguments)
            return host.coach_tool_state()
        if name == "stop_coach_session":
            _require_no_arguments(arguments)
            return host.coach_tool_stop()
    except CoachError as exc:
        return {"ok": False, "error": exc.to_public_dict()}
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ToolExecutionError):
            raise
        raise ToolExecutionError(str(exc)) from exc

    raise ToolExecutionError(f"unknown tool: {name}")


def mcp_text_result(result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a structured result in a conservative MCP content payload."""

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=True, separators=(",", ":")),
            }
        ],
        "structuredContent": result,
    }


def _required_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError(f"{key} must be a non-empty string")
    return value.strip()


def _required_float(arguments: dict[str, Any], key: str) -> float:
    if key not in arguments:
        raise ToolExecutionError(f"{key} is required")
    try:
        return float(arguments[key])
    except (TypeError, ValueError) as exc:
        raise ToolExecutionError(f"{key} must be a number") from exc


def _bounded_string(arguments: dict[str, Any], key: str, maximum: int) -> str:
    value = _required_string(arguments, key)
    if len(value) > maximum:
        raise ToolExecutionError(f"{key} must contain at most {maximum} characters")
    return value


def _optional_bounded_string(arguments: dict[str, Any], key: str, maximum: int) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError(f"{key} must be null or non-empty text")
    value = value.strip()
    if len(value) > maximum:
        raise ToolExecutionError(f"{key} must contain at most {maximum} characters")
    return value


def _bounded_int(arguments: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolExecutionError(f"{key} must be an integer from {minimum} through {maximum}")
    return value


def _optional_bounded_int(
    arguments: dict[str, Any], key: str, minimum: int, maximum: int
) -> int | None:
    if arguments.get(key) is None:
        return None
    return _bounded_int(arguments, key, minimum, maximum)


def _required_enum(arguments: dict[str, Any], key: str, allowed: set[str]) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise ToolExecutionError(f"{key} must be one of {'|'.join(sorted(allowed))}")
    return value


def _optional_enum(arguments: dict[str, Any], key: str, allowed: set[str]) -> str | None:
    if arguments.get(key) is None:
        return None
    return _required_enum(arguments, key, allowed)


def _require_no_arguments(arguments: dict[str, Any]) -> None:
    if arguments:
        raise ToolExecutionError("this tool does not accept arguments")


def _require_allowed_arguments(arguments: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ToolExecutionError(f"unknown argument: {unknown[0]}")
