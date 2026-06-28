"""Safe synth tool contract shared by chat, HTTP, and MCP stdio."""

from __future__ import annotations

import json
from typing import Any, Protocol

from halcyon.synth_params import PARAMS, PRESETS


class ToolHost(Protocol):
    """Minimal host interface required by the Halcyon tool executor."""

    def synth_tool_state(self) -> dict[str, Any]: ...

    def synth_tool_set_param(self, param_id: str, normalized_value: float) -> dict[str, Any]: ...

    def synth_tool_apply_patch(self, values: dict[str, float]) -> dict[str, Any]: ...

    def synth_tool_apply_preset(self, preset_name: str) -> dict[str, Any]: ...

    def synth_tool_save_preset(self, preset_name: str) -> dict[str, Any]: ...

    def synth_tool_all_notes_off(self) -> dict[str, Any]: ...


class ToolExecutionError(ValueError):
    """Raised when a tool call is invalid or cannot complete."""


def tool_definitions() -> list[dict[str, Any]]:
    """Return MCP-shaped JSON schemas for the safe Halcyon synth tools."""

    param_enum = [param.param_id for param in PARAMS]
    return [
        {
            "name": "get_synth_state",
            "description": "Read current synth patch, audio status, pressed notes, and recent MIDI log summaries.",
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


def execute_tool(
    host: ToolHost, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Execute a safe synth tool against the running app host."""

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
