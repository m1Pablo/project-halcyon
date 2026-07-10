"""Execute registered slash commands through Halcyon's safe synth tools."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Callable

from halcyon.mcp_tools import ToolHost, execute_tool
from halcyon.slash_commands import SlashResolved, resolve_slash_command
from halcyon.synth_params import PARAM_BY_ID, PRESETS


class CommandExecutionError(ValueError):
    """Raised when a slash command cannot be executed safely."""


ToolExecutor = Callable[[ToolHost, str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    command: str
    leaf_command: str
    tool: str
    arguments: dict[str, Any]
    payload: dict[str, Any]


def prepare_slash_command(menu_def: dict[str, Any], command: str) -> CommandInvocation:
    """Resolve and validate a slash command without executing it."""

    resolved = resolve_slash_command(menu_def, command)
    if resolved.status == "error":
        raise CommandExecutionError(resolved.message)
    if resolved.status == "group":
        options = ", ".join(shortcut for shortcut, _label in resolved.options)
        raise CommandExecutionError(
            f"Command group selected: {resolved.leaf_command}. Choose one of: {options}"
        )
    if resolved.payload is None:
        raise CommandExecutionError("Command has no executable payload.")

    tool = str(resolved.payload.get("tool", "")).strip()
    if not tool:
        raise CommandExecutionError(f"Command has no tool: {resolved.leaf_command}")

    arguments = _tool_arguments(tool, resolved)
    return CommandInvocation(
        command=resolved.command,
        leaf_command=resolved.leaf_command,
        tool=tool,
        arguments=arguments,
        payload=resolved.payload,
    )


def execute_slash_command(
    host: ToolHost,
    menu_def: dict[str, Any],
    command: str,
    *,
    tool_executor: ToolExecutor = execute_tool,
) -> dict[str, Any]:
    """Execute a slash command through the safe MCP-shaped tool contract."""

    invocation = prepare_slash_command(menu_def, command)
    return tool_executor(host, invocation.tool, invocation.arguments)


def _tool_arguments(tool: str, resolved: SlashResolved) -> dict[str, Any]:
    args = resolved.arguments
    if tool in {"get_synth_state", "list_synth_params", "all_notes_off"}:
        _require_arg_count(resolved.leaf_command, args, 0)
        return {}
    if tool == "set_synth_param":
        _require_arg_count(resolved.leaf_command, args, 2)
        param_id = _known_param(args[0])
        return {"param": param_id, "value": _normalized_value(args[1])}
    if tool == "apply_synth_patch":
        if not args:
            raise CommandExecutionError(
                f"Usage: {resolved.payload.get('usage') or '/synth patch <param=value ...>'}"
            )
        return {"values": _patch_values(args)}
    if tool == "apply_synth_preset":
        preset_name = _joined_name(resolved.leaf_command, args)
        return {"preset": preset_name}
    if tool == "save_synth_preset":
        preset_name = _joined_name(resolved.leaf_command, args)
        if preset_name in PRESETS:
            raise CommandExecutionError(
                f"{preset_name!r} is a built-in preset and cannot be overwritten"
            )
        return {"preset": preset_name}
    raise CommandExecutionError(f"Unsupported command tool: {tool}")


def _require_arg_count(leaf_command: str, args: tuple[str, ...], expected: int) -> None:
    if len(args) == expected:
        return
    plural = "" if expected == 1 else "s"
    raise CommandExecutionError(
        f"{leaf_command} expects {expected} argument{plural}; got {len(args)}."
    )


def _known_param(raw: str) -> str:
    param_id = raw.strip().lower()
    if param_id not in PARAM_BY_ID:
        raise CommandExecutionError(f"unknown synth parameter: {raw}")
    return param_id


def _normalized_value(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise CommandExecutionError(f"normalized value must be a number: {raw}") from exc
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise CommandExecutionError("normalized value must be between 0 and 1")
    return value


def _patch_values(args: tuple[str, ...]) -> dict[str, float]:
    values: dict[str, float] = {}
    for item in args:
        if "=" not in item:
            raise CommandExecutionError("patch arguments must use param=value syntax")
        raw_param, raw_value = item.split("=", 1)
        param_id = _known_param(raw_param)
        if param_id in values:
            raise CommandExecutionError(f"duplicate synth parameter: {param_id}")
        values[param_id] = _normalized_value(raw_value)
    return values


def _joined_name(leaf_command: str, args: tuple[str, ...]) -> str:
    if not args:
        raise CommandExecutionError(f"{leaf_command} expects a preset name.")
    name = " ".join(part.strip() for part in args if part.strip()).strip()
    if not name:
        raise CommandExecutionError(f"{leaf_command} expects a preset name.")
    return name
