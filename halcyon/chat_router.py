"""Natural-language router constrained to Halcyon's menu slash commands."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any, Iterable, Literal

from halcyon.command_executor import CommandExecutionError, prepare_slash_command
from halcyon.menu import load_menu
from halcyon.mcp_tools import list_synth_params
from halcyon.slash_commands import iter_slash_leaf_commands

DEFAULT_ROUTER_MODEL = "gpt-5.5"
MAX_ROUTER_USER_MESSAGE_CHARS = 2_000
MAX_ROUTER_RESPONSE_CHARS = 16_000
MAX_ROUTER_REASON_CHARS = 500
MAX_ROUTER_STAGES = 6
MAX_ROUTER_COMMANDS_PER_STAGE = 6
MAX_ROUTER_COMMANDS = 12
MAX_ROUTER_COMMAND_CHARS = 1_000
MAX_CONTEXT_DEPTH = 5
MAX_CONTEXT_DICT_ITEMS = 40
MAX_CONTEXT_LIST_ITEMS = 16
MAX_CONTEXT_STRING_CHARS = 500
MAX_CONTEXT_NODES = 256
MAX_RENDERED_CONTEXT_CHARS = 16_000
MAX_CHAT_CONTEXT_ITEMS = 8
NO_MATCH_REASON = "I could not map that to a Halcyon slash command."
MISSING_KEY_REASON = (
    "Natural-language routing is disabled: add an OpenAI API key in Settings or set "
    "OPENAI_API_KEY in your environment. Slash commands still work."
)

_ROUTER_SYSTEM_PROMPT = (
    "You are Halcyon's command-routing agent inside a local MIDI synth and learning TUI. "
    "Your only job is to map natural language synth or MIDI-coaching requests to registered slash "
    "commands, or reject unrelated requests. You cannot call tools. You cannot "
    "invent commands."
)

_ROUTER_PROMPT_TEMPLATE = """Map the user's request to Halcyon slash commands, or reject it.

Available commands, derived from menu.xml:
{menu_commands}

Current app context:
{app_context}

Recent chat context:
{chat_context}

Synth parameter metadata:
{param_metadata}

Command rules:
- Output action "menu" only when the request maps to one or more listed commands.
- Output action "reject" with empty stages for anything unrelated to this synth app.
- Include command arguments inline in the command string.
- Use normalized synth values from 0 to 1.
- Preserve command order. Put dependent commands in later stages.
- Requests to teach, learn, demonstrate, play for imitation, practice, or score a
  melody are coach work. Route the whole workflow to exactly one /coach teach;
  never expand it into separate author, replay, attempt, or feedback commands.
- Lesson resolution is cache-first. For an ordinary personal-use request to teach
  a named work, omit --author or emit --author bounded: a cache hit is reused and
  a cache miss authors one local educational approximation and saves it for reuse.
  Emit --author never only when the user explicitly asks for saved/cache-only
  playback with no generation.
- For /coach teach, keep the artist and work title in QUERY and put the requested
  portion, such as "main melody", in --segment. Every --author bounded command
  must include an explicit --segment.
- Distinguish melody learning from sound design: "teach/play this melody" is coach
  work, while "make the synth sound like this artist/song" is a /synth patch.
- Emit at most one state-mutating /coach command in the entire response. State is
  read-only; teach, replay, attempt, feedback, and stop are state-mutating.
- Treat subjective musical language as valid synth work. For mood words, genre
  references, artist/song references, or "make it sound like ..." requests,
  synthesize a best-effort /synth patch using the parameter semantics below.
  Do not require a matching preset or user-provided numbers.
- Treat volume as output trim, not a timbre control. For sound-design requests,
  preserve the current volume by omitting "volume" unless the user explicitly
  asks for louder, quieter, fade, mute, or a specific level.
- Keep generated patches playable and audible on a keyboard. Use extreme attack,
  sustain, release, tone, drive, or volume values only when the user clearly asks
  for an extreme sound.
- Use the current app context for relative requests like "more", "less", "this",
  "that", "current", or "make it ...".
- Use /preset apply only when the user explicitly asks to load/apply/use a preset.
- For free-form command arguments such as preset names, infer the intended
  argument from the user's meaning. Do not copy instruction words or referents
  into the argument.
- Reject only when the request is outside the app, unsafe, or impossible to express
  with the listed slash commands.

Return JSON only with this shape:
{{"action":"menu|reject","stages":[["/synth set tone 0.8"]],"reason":"short reason"}}

User request:
{user_message}
"""


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    action: Literal["menu", "reject"]
    stages: tuple[tuple[str, ...], ...] = ()
    reason: str = ""

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(command for stage in self.stages for command in stage)


_MUTATING_COACH_LEAVES = frozenset(
    {
        "/coach teach",
        "/coach replay",
        "/coach attempt",
        "/coach feedback",
        "/coach stop",
    }
)

_SAFE_CONTEXT_KEYS = frozenset(
    {
        "attempt_id",
        "authored",
        "fidelity",
        "key_signature",
        "keyboard",
        "keyboard_layout",
        "keys",
        "lesson_id",
        "provenance",
        "provenance_label",
    }
)

# This is the only raw-family aggregate needed by routing. Its value is also
# constrained below so a misleading count key cannot carry an opaque payload.
_SAFE_CONTEXT_AGGREGATE_KEYS = frozenset({"event_count"})

_PRIVATE_CONTEXT_KEYS = frozenset(
    {
        "api_key",
        "capture_events",
        "credential",
        "credentials",
        "cwd",
        "event",
        "event_history",
        "event_histories",
        "event_log",
        "event_logs",
        "events",
        "file_path",
        "filesystem_path",
        "history",
        "histories",
        "log",
        "logs",
        "midi",
        "midi_events",
        "midi_history",
        "midi_log",
        "midi_logs",
        "midi_messages",
        "midi_notes",
        "observed_events",
        "openai_api_key",
        "password",
        "path",
        "private_path",
        "prompt",
        "raw_history",
        "raw_midi",
        "recent_midi",
        "secret",
        "source_port",
        "source_ports",
        "system_prompt",
        "token",
    }
)

_PRIVATE_CONTEXT_SEGMENTS = frozenset(
    {
        "admission",
        "admissions",
        "authoring",
        "credential",
        "credentials",
        "dispatch",
        "dispatches",
        "event",
        "events",
        "history",
        "histories",
        "log",
        "logs",
        "midi",
        "password",
        "passwords",
        "path",
        "paths",
        "prompt",
        "prompts",
        "provider",
        "providers",
        "queue",
        "queues",
        "receipt",
        "receipts",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)


@dataclass(slots=True)
class _ContextBudget:
    remaining: int = MAX_CONTEXT_NODES


def run_chat_routing(
    message: str,
    *,
    menu_def: dict[str, Any] | None = None,
    app_context: dict[str, Any] | None = None,
    chat_context: list[dict[str, str]] | None = None,
    client: Any | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> RoutingDecision:
    """Route natural language to validated slash commands or reject."""

    text = (message or "").strip()[:MAX_ROUTER_USER_MESSAGE_CHARS]
    if not text:
        return RoutingDecision(action="reject", reason="Empty request.")

    if client is None and not _resolve_api_key(api_key):
        return RoutingDecision(action="reject", reason=MISSING_KEY_REASON)

    menu_def = menu_def or load_menu()
    prompt = _ROUTER_PROMPT_TEMPLATE.format(
        menu_commands=_render_menu_commands(iter_slash_leaf_commands(menu_def)),
        app_context=_render_app_context(app_context),
        chat_context=_render_chat_context(chat_context),
        param_metadata=_render_param_metadata(),
        user_message=text,
    )
    try:
        raw = _call_router_llm(
            prompt,
            client=client,
            api_key=api_key,
            model=model or os.environ.get("HALCYON_CHAT_MODEL") or DEFAULT_ROUTER_MODEL,
        )
    except Exception:
        return RoutingDecision(action="reject", reason="Could not route request.")

    payload = _parse_router_payload(raw)
    if payload is None:
        return RoutingDecision(action="reject", reason="Could not route request.")

    raw_action = payload.get("action")
    raw_reason = payload.get("reason", "")
    if not isinstance(raw_action, str) or not isinstance(raw_reason, str):
        return RoutingDecision(action="reject", reason="Could not route request.")
    action = raw_action.strip().lower()
    reason = raw_reason.strip()[:MAX_ROUTER_REASON_CHARS]
    if action != "menu":
        return RoutingDecision(
            action="reject",
            reason=reason or NO_MATCH_REASON,
        )

    stages, validation_error = _validate_stages(payload.get("stages"), menu_def)
    if validation_error is not None:
        return RoutingDecision(action="reject", reason=validation_error)
    if stages is None:
        return RoutingDecision(action="reject", reason="Invalid command plan.")
    coach_violation = _coach_plan_violation(stages, menu_def)
    if coach_violation is not None:
        return RoutingDecision(action="reject", reason=coach_violation)
    return RoutingDecision(action="menu", stages=stages, reason=reason)


def _validate_stages(
    raw_stages: object,
    menu_def: dict[str, Any],
) -> tuple[tuple[tuple[str, ...], ...] | None, str | None]:
    if not isinstance(raw_stages, (list, tuple)) or not raw_stages:
        return None, "Routing output contained an invalid or malformed command plan."
    if len(raw_stages) > MAX_ROUTER_STAGES:
        return None, "Routing output exceeded command limits."

    stages: list[tuple[str, ...]] = []
    seen_commands: set[str] = set()
    total_commands = 0
    for raw_stage in raw_stages:
        if not isinstance(raw_stage, (list, tuple)) or not raw_stage:
            return None, "Routing output contained an invalid or malformed command plan."
        if len(raw_stage) > MAX_ROUTER_COMMANDS_PER_STAGE:
            return None, "Routing output exceeded command limits."
        total_commands += len(raw_stage)
        if total_commands > MAX_ROUTER_COMMANDS:
            return None, "Routing output exceeded command limits."

        stage: list[str] = []
        for raw_command in raw_stage:
            if not isinstance(raw_command, str) or len(raw_command) > MAX_ROUTER_COMMAND_CHARS:
                return None, "Routing output contained an invalid or malformed command plan."
            command = " ".join(raw_command.split()).strip()
            invalid_reason = (
                "Coach routing output contained an invalid registered command."
                if command.lower().startswith("/coach")
                else "Routing output contained an invalid registered command."
            )
            if (
                not command
                or len(command) > MAX_ROUTER_COMMAND_CHARS
                or not command.startswith("/")
            ):
                return None, invalid_reason
            try:
                invocation = prepare_slash_command(menu_def, command)
            except CommandExecutionError:
                return None, invalid_reason
            if invocation.command in seen_commands:
                return None, "Routing output contained a duplicate command."
            seen_commands.add(invocation.command)
            stage.append(invocation.command)
        stages.append(tuple(stage))
    return tuple(stages), None


def _coach_plan_violation(
    stages: tuple[tuple[str, ...], ...],
    menu_def: dict[str, Any],
) -> str | None:
    mutating = 0
    for stage in stages:
        for command in stage:
            invocation = prepare_slash_command(menu_def, command)
            if (
                invocation.leaf_command == "/coach teach"
                and invocation.arguments.get("author") == "bounded"
            ):
                if invocation.arguments.get("segment") is None:
                    return (
                        "Bounded lesson authoring requires an explicit segment such as "
                        '--segment "main melody".'
                    )
            if invocation.leaf_command in _MUTATING_COACH_LEAVES:
                mutating += 1
                if mutating > 1:
                    return "Coach routing must use one state-mutating command per request."
    return None


def _call_router_llm(
    prompt: str,
    *,
    client: Any | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_ROUTER_MODEL,
) -> str:
    client = client or _build_client(api_key)
    messages = [
        {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            max_completion_tokens=500,
        )
    except TypeError:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=500,
        )
    return _extract_chat_content(response)


def _build_client(api_key: str | None) -> Any:
    key = _resolve_api_key(api_key)
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    from openai import OpenAI

    return OpenAI(api_key=key)


def _resolve_api_key(api_key: str | None) -> str:
    if api_key is not None:
        return api_key.strip()
    return os.environ.get("OPENAI_API_KEY", "").strip()


def _extract_chat_content(response: Any) -> str:
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message", {})
            return str(message.get("content") or "")[: MAX_ROUTER_RESPONSE_CHARS + 1]
        return ""

    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return str(getattr(message, "content", "") or "")[: MAX_ROUTER_RESPONSE_CHARS + 1]


def _parse_router_payload(text: str) -> dict[str, Any] | None:
    if len(text) > MAX_ROUTER_RESPONSE_CHARS:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _render_menu_commands(commands: Iterable[tuple[str, dict[str, Any]]]) -> str:
    lines: list[str] = []
    for command, payload in commands:
        usage = str(payload.get("usage") or command).strip()
        description = str(payload.get("description", "")).strip()
        if description:
            lines.append(f"- {usage}: {description}")
        else:
            lines.append(f"- {usage}")
    return "\n".join(lines) or "- (none)"


def _render_app_context(app_context: dict[str, Any] | None) -> str:
    if not app_context:
        return "- no live app context provided"
    try:
        return _render_trimmed_context(app_context)
    except (TypeError, ValueError):
        return "- live app context unavailable"


def _render_chat_context(chat_context: list[dict[str, str]] | None) -> str:
    if not chat_context:
        return "- no recent chat context"
    return _render_trimmed_context(chat_context[-MAX_CHAT_CONTEXT_ITEMS:])


def _render_trimmed_context(value: Any) -> str:
    rendered = json.dumps(
        _trim_context(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return rendered[:MAX_RENDERED_CONTEXT_CHARS]


def _trim_context(
    value: Any,
    *,
    depth: int = 0,
    budget: _ContextBudget | None = None,
) -> Any:
    budget = budget or _ContextBudget()
    if depth >= MAX_CONTEXT_DEPTH or budget.remaining <= 0:
        return "..."
    budget.remaining -= 1
    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        for key, item in value.items():
            try:
                full_key = str(key)
            except Exception:
                continue
            if _looks_sensitive_key(full_key, item):
                continue
            rendered_key = full_key[:120]
            bounded[rendered_key] = _trim_context(
                item,
                depth=depth + 1,
                budget=budget,
            )
            if len(bounded) >= MAX_CONTEXT_DICT_ITEMS or budget.remaining <= 0:
                break
        return bounded
    if isinstance(value, (list, tuple)):
        return [
            _trim_context(item, depth=depth + 1, budget=budget)
            for item in value[:MAX_CONTEXT_LIST_ITEMS]
            if budget.remaining > 0
        ]
    if isinstance(value, str):
        return value[:MAX_CONTEXT_STRING_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def _looks_sensitive_key(key: str, value: Any) -> bool:
    first_pass = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", key)
    snake_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first_pass)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_key.lower()).strip("_")
    if not normalized:
        return True
    if normalized in _SAFE_CONTEXT_AGGREGATE_KEYS:
        return isinstance(value, bool) or not isinstance(value, int)
    if normalized in _SAFE_CONTEXT_KEYS:
        return False
    if normalized in _PRIVATE_CONTEXT_KEYS:
        return True
    segments = frozenset(normalized.split("_"))
    if "raw" in segments or segments.intersection(_PRIVATE_CONTEXT_SEGMENTS):
        return True
    if ("api" in segments or "private" in segments) and segments.intersection({"key", "keys"}):
        return True
    return {"source", "port"}.issubset(segments)


def _render_param_metadata() -> str:
    params = list_synth_params()
    semantics = {
        "volume": "global output trim; preserve by omitting it unless the user explicitly asks for a loudness change",
        "tone": "low-pass filter cutoff; lower is darker/warmer/muffled, higher is brighter/opener/sharper",
        "shape": "oscillator blend from saw at 0 to square at 1; lower is smoother/thinner, higher is hollower/buzzier",
        "attack": "fade-in time; lower is immediate/percussive, higher is slow/swelling",
        "decay": "time from peak to sustain; lower is plucky/tight, higher is slower/rounder",
        "sustain": "held level while a note is pressed; lower is plucked/fading, higher is pad/organ-like",
        "release": "fade-out after key release; lower is clipped/tight, higher is lingering/ambient",
        "drive": "soft-clipping amount; lower is clean, higher is saturated/gritty",
    }
    lines = []
    for param in params["params"]:
        hint = semantics.get(str(param["id"]), "normalized synth control")
        lines.append(
            f"- {param['id']}: {param['label']} ({param['unit']}), normalized 0..1; {hint}"
        )
    presets = ", ".join(params["presets"])
    if presets:
        lines.append(f"- presets: {presets}")
    return "\n".join(lines)
