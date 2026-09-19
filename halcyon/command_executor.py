"""Execute registered slash commands through Halcyon's safe synth tools."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from math import isfinite
import re
from typing import Any, Callable, Literal

from halcyon.mcp_tools import ToolHost, execute_tool
from halcyon.slash_commands import SlashResolved, resolve_slash_command
from halcyon.synth_params import PARAM_BY_ID, PRESETS


class CommandExecutionError(ValueError):
    """Raised when a slash command cannot be executed safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INVALID_ARGUMENT",
        field: str | None = None,
        resolution: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.resolution = resolution

    def to_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": True,
        }
        if self.field is not None:
            payload["field"] = self.field
        if self.resolution is not None:
            payload["resolution"] = self.resolution
        return payload


ToolExecutor = Callable[[ToolHost, str, dict[str, Any]], Any]
CommandTerminalStatus = Literal["completed", "failed", "cancelled", "timed_out"]


@dataclass(frozen=True, slots=True)
class ImmediateCommandOutcome:
    """A JSON-safe command result that is already complete."""

    public: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.public, dict):
            raise TypeError("immediate public result must be an object")
        object.__setattr__(self, "public", dict(self.public))

    def to_public_dict(self) -> dict[str, Any]:
        return dict(self.public)


@dataclass(frozen=True, slots=True)
class CommandProgress:
    """One bounded progress update for a pending operation."""

    operation_id: str
    session_id: str
    phase: str
    message: str
    percent: int | None = None

    def __post_init__(self) -> None:
        for name in ("operation_id", "session_id", "phase", "message"):
            value = str(getattr(self, name)).strip()
            if not value or len(value) > (500 if name == "message" else 128):
                raise ValueError(f"{name} must be bounded non-empty text")
            object.__setattr__(self, name, value)
        if self.percent is not None and not 0 <= self.percent <= 100:
            raise ValueError("progress percent must be between 0 and 100")

    def to_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operation_id": self.operation_id,
            "session_id": self.session_id,
            "phase": self.phase,
            "message": self.message,
        }
        if self.percent is not None:
            payload["percent"] = self.percent
        return payload


@dataclass(frozen=True, slots=True)
class TerminalCommandOutcome:
    """The authoritative terminal result after operation cleanup has settled."""

    operation_id: str
    session_id: str
    status: CommandTerminalStatus
    public: dict[str, Any]

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed", "cancelled", "timed_out"}:
            raise ValueError("invalid terminal command status")
        if not self.operation_id or len(self.operation_id) > 128:
            raise ValueError("operation_id must be bounded non-empty text")
        if not self.session_id or len(self.session_id) > 128:
            raise ValueError("session_id must be bounded non-empty text")
        if not isinstance(self.public, dict):
            raise TypeError("terminal public result must be an object")
        object.__setattr__(self, "public", dict(self.public))

    def to_public_dict(self) -> dict[str, Any]:
        return dict(self.public)


ProgressSubscriber = Callable[[CommandProgress], None]
ProgressUnsubscribe = Callable[[], None]
ProgressSubscribe = Callable[[ProgressSubscriber], ProgressUnsubscribe]
CancelPending = Callable[[Literal["stop", "timeout"]], bool]


def _noop_subscribe(_subscriber: ProgressSubscriber) -> ProgressUnsubscribe:
    return lambda: None


@dataclass(frozen=True, slots=True)
class PendingCommandOutcome:
    """Opaque ownership for one nonblocking command operation."""

    operation_id: str
    session_id: str
    accepted: dict[str, Any]
    completion: Future[TerminalCommandOutcome]
    cancel_callback: CancelPending
    subscribe_callback: ProgressSubscribe = _noop_subscribe

    def __post_init__(self) -> None:
        if not self.operation_id or len(self.operation_id) > 128:
            raise ValueError("operation_id must be bounded non-empty text")
        if not self.session_id or len(self.session_id) > 128:
            raise ValueError("session_id must be bounded non-empty text")
        if not isinstance(self.accepted, dict):
            raise TypeError("accepted result must be an object")
        if not isinstance(self.completion, Future):
            raise TypeError("completion must be a Future")
        object.__setattr__(self, "accepted", dict(self.accepted))

    def cancel(self, reason: Literal["stop", "timeout"] = "stop") -> bool:
        if reason not in {"stop", "timeout"}:
            raise ValueError("cancellation reason must be stop or timeout")
        return bool(self.cancel_callback(reason))

    def subscribe(self, subscriber: ProgressSubscriber) -> ProgressUnsubscribe:
        return self.subscribe_callback(subscriber)

    def to_public_dict(self) -> dict[str, Any]:
        return dict(self.accepted)


CommandOutcome = ImmediateCommandOutcome | PendingCommandOutcome | TerminalCommandOutcome


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
    """Legacy synchronous command API; pending work must use the typed API."""

    outcome = execute_slash_command_outcome(
        host,
        menu_def,
        command,
        tool_executor=tool_executor,
    )
    if isinstance(outcome, PendingCommandOutcome):
        raise CommandExecutionError(
            "Command is still running; use execute_slash_command_outcome and poll its operation.",
            code="COMMAND_PENDING",
            resolution="Use the returned operation_id and terminal completion callback.",
        )
    return outcome.to_public_dict()


def execute_slash_command_outcome(
    host: ToolHost,
    menu_def: dict[str, Any],
    command: str,
    *,
    tool_executor: ToolExecutor = execute_tool,
) -> CommandOutcome:
    """Execute a registered command and preserve immediate versus pending truth."""

    invocation = prepare_slash_command(menu_def, command)
    result = tool_executor(host, invocation.tool, invocation.arguments)
    if isinstance(
        result,
        (ImmediateCommandOutcome, PendingCommandOutcome, TerminalCommandOutcome),
    ):
        return result
    if isinstance(result, dict):
        return ImmediateCommandOutcome(result)
    raise CommandExecutionError(
        f"Tool {invocation.tool} returned an unsupported command outcome.",
        code="COMMAND_RESULT_INVALID",
        resolution="Retry the registered slash command.",
    )


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
    if tool == "start_coach_lesson":
        return _coach_teach_arguments(resolved)
    if tool == "replay_coach_lesson":
        return _coach_replay_arguments(resolved)
    if tool == "start_coach_attempt":
        return _coach_attempt_arguments(resolved)
    if tool == "get_coach_feedback":
        _require_arg_count_range(resolved.leaf_command, args, 0, 1)
        attempt_id = args[0].strip() if args else None
        if attempt_id is not None and (not attempt_id or len(attempt_id) > 128):
            raise CommandExecutionError(
                "attempt_id must contain 1 through 128 characters.",
                field="attempt_id",
                resolution="Retry with /coach feedback or a shorter attempt identifier.",
            )
        return {"attempt_id": attempt_id}
    if tool in {"get_coach_state", "stop_coach_session"}:
        _require_arg_count(resolved.leaf_command, args, 0)
        return {}
    raise CommandExecutionError(f"Unsupported command tool: {tool}")


_COACH_ENUMS: dict[str, frozenset[str]] = {
    "difficulty": frozenset({"foundation", "standard", "challenge"}),
    "hand": frozenset({"left", "right", "both"}),
    "author": frozenset({"never", "bounded"}),
}
_INTEGER_RE = re.compile(r"^[0-9]+$")


def _coach_teach_arguments(resolved: SlashResolved) -> dict[str, Any]:
    positionals, options = _parse_coach_options(
        resolved,
        allowed={"segment", "tempo", "difficulty", "hand", "loops", "author"},
        allow_positionals=True,
    )
    query = " ".join(positionals).strip()
    if not query:
        _usage_error(resolved, "query", "a lesson query is required")
    if len(query) > 160:
        _usage_error(resolved, "query", "query must contain at most 160 characters")
    return {
        "query": query,
        "segment": _bounded_option(options, "segment", 80),
        "tempo_percent": _integer_option(options, "tempo", 25, 200, default=100),
        "difficulty": _enum_option(options, "difficulty"),
        "hand": _enum_option(options, "hand"),
        "loops": _integer_option(options, "loops", 1, 8, default=1),
        "author": _enum_option(options, "author", default="bounded"),
    }


def _coach_replay_arguments(resolved: SlashResolved) -> dict[str, Any]:
    positionals, options = _parse_coach_options(
        resolved,
        allowed={"segment", "tempo", "loops"},
        allow_positionals=False,
    )
    if positionals:
        _usage_error(resolved, "arguments", "replay accepts options only")
    return {
        "segment": _bounded_option(options, "segment", 80),
        "tempo_percent": _integer_option(options, "tempo", 25, 200),
        "loops": _integer_option(options, "loops", 1, 8),
    }


def _coach_attempt_arguments(resolved: SlashResolved) -> dict[str, Any]:
    positionals, options = _parse_coach_options(
        resolved,
        allowed={"segment", "tempo", "count-in"},
        allow_positionals=False,
    )
    if positionals:
        _usage_error(resolved, "arguments", "attempt accepts options only")
    return {
        "segment": _bounded_option(options, "segment", 80),
        "tempo_percent": _integer_option(options, "tempo", 25, 200),
        "count_in": _integer_option(options, "count-in", 0, 8),
    }


def _parse_coach_options(
    resolved: SlashResolved,
    *,
    allowed: set[str],
    allow_positionals: bool,
) -> tuple[tuple[str, ...], dict[str, str]]:
    positionals: list[str] = []
    options: dict[str, str] = {}
    args = resolved.arguments
    index = 0
    options_started = False
    while index < len(args):
        token = args[index]
        if not token.startswith("--"):
            if options_started or not allow_positionals:
                _usage_error(
                    resolved,
                    "arguments",
                    "an unexpected positional coach argument was provided",
                )
            positionals.append(token)
            index += 1
            continue

        options_started = True
        raw_option = token[2:]
        if "=" in raw_option:
            option, value = raw_option.split("=", 1)
            has_inline_value = True
        else:
            option = raw_option
            value = ""
            has_inline_value = False
        option = option.strip().lower()
        if option not in allowed:
            valid = ", ".join(f"--{name}" for name in sorted(allowed))
            _usage_error(
                resolved,
                "option",
                f"an unsupported coach option was provided; use {valid}",
            )
        if not has_inline_value:
            index += 1
            if index >= len(args) or args[index].startswith("--"):
                _usage_error(resolved, option, f"--{option} requires a value")
            value = args[index]
        if option in options:
            _usage_error(resolved, option, f"--{option} may be supplied only once")
        if not value.strip():
            _usage_error(resolved, option, f"--{option} requires a non-empty value")
        options[option] = value.strip()
        index += 1
    return tuple(positionals), options


def _bounded_option(options: dict[str, str], name: str, maximum: int) -> str | None:
    value = options.get(name)
    if value is None:
        return None
    if len(value) > maximum:
        raise CommandExecutionError(
            f"--{name} must contain at most {maximum} characters.",
            field=name,
            resolution=f"Retry with a shorter --{name} value.",
        )
    return value


def _integer_option(
    options: dict[str, str],
    name: str,
    minimum: int,
    maximum: int,
    *,
    default: int | None = None,
) -> int | None:
    raw = options.get(name)
    if raw is None:
        return default
    if not _INTEGER_RE.fullmatch(raw):
        raise CommandExecutionError(
            f"--{name} must be an integer from {minimum} through {maximum}.",
            field=name,
            resolution=f"Retry with --{name} {minimum}.",
        )
    value = int(raw)
    if not minimum <= value <= maximum:
        raise CommandExecutionError(
            f"--{name} must be from {minimum} through {maximum}.",
            field=name,
            resolution=f"Retry with --{name} {minimum}.",
        )
    return value


def _enum_option(options: dict[str, str], name: str, *, default: str | None = None) -> str | None:
    raw = options.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    allowed = _COACH_ENUMS[name]
    if value not in allowed:
        choices = "|".join(sorted(allowed))
        raise CommandExecutionError(
            f"--{name} must be one of {choices}.",
            field=name,
            resolution=f"Retry with --{name} {sorted(allowed)[0]}.",
        )
    return value


def _usage_error(resolved: SlashResolved, field: str, message: str) -> None:
    usage = str((resolved.payload or {}).get("usage") or resolved.leaf_command)
    raise CommandExecutionError(
        f"{message}. Usage: {usage}",
        field=field,
        resolution=f"Retry using: {usage}",
    )


def _require_arg_count(leaf_command: str, args: tuple[str, ...], expected: int) -> None:
    if len(args) == expected:
        return
    plural = "" if expected == 1 else "s"
    raise CommandExecutionError(
        f"{leaf_command} expects {expected} argument{plural}; got {len(args)}."
    )


def _require_arg_count_range(
    leaf_command: str, args: tuple[str, ...], minimum: int, maximum: int
) -> None:
    if minimum <= len(args) <= maximum:
        return
    raise CommandExecutionError(
        f"{leaf_command} expects {minimum} through {maximum} arguments; got {len(args)}."
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
