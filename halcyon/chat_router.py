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
NO_MATCH_REASON = "I could not map that to a Halcyon slash command."
MISSING_KEY_REASON = (
    "Natural-language routing is disabled: set OPENAI_API_KEY in your environment "
    "to enable it. Slash commands still work."
)

_ROUTER_SYSTEM_PROMPT = (
    "You are Halcyon's command-routing agent inside a local MIDI synth TUI. "
    "Your only job is to map natural language synth requests to registered slash "
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

    text = (message or "").strip()
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

    action = str(payload.get("action", "")).strip().lower()
    reason = str(payload.get("reason", "")).strip()
    if action != "menu":
        return RoutingDecision(action="reject", reason=reason or NO_MATCH_REASON)

    stages = _validate_stages(payload.get("stages"), menu_def)
    if not stages:
        return RoutingDecision(action="reject", reason="No valid command selected.")
    return RoutingDecision(action="menu", stages=stages, reason=reason)


def _validate_stages(raw_stages: object, menu_def: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    if isinstance(raw_stages, str):
        raw_stages = [[raw_stages]]
    if not isinstance(raw_stages, (list, tuple)):
        return ()

    stages: list[tuple[str, ...]] = []
    for raw_stage in raw_stages:
        if isinstance(raw_stage, str):
            raw_commands: list[object] = [raw_stage]
        elif isinstance(raw_stage, (list, tuple)):
            raw_commands = list(raw_stage)
        else:
            continue

        stage: list[str] = []
        for raw_command in raw_commands:
            command = " ".join(str(raw_command).split()).strip()
            if not command.startswith("/"):
                continue
            try:
                invocation = prepare_slash_command(menu_def, command)
            except CommandExecutionError:
                continue
            if invocation.command not in stage:
                stage.append(invocation.command)
        if stage:
            stages.append(tuple(stage))
    return tuple(stages)


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
        response = client.chat.completions.create(model=model, messages=messages)
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
            return str(message.get("content") or "")
        return ""

    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return str(getattr(message, "content", "") or "")


def _parse_router_payload(text: str) -> dict[str, Any] | None:
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
        return json.dumps(
            _trim_context(app_context),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return "- live app context unavailable"


def _render_chat_context(chat_context: list[dict[str, str]] | None) -> str:
    if not chat_context:
        return "- no recent chat context"
    return json.dumps(
        _trim_context(chat_context[-8:]),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _trim_context(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "..."
    if isinstance(value, dict):
        return {
            str(key): _trim_context(item, depth=depth + 1)
            for key, item in value.items()
            if not _looks_sensitive_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_trim_context(item, depth=depth + 1) for item in value[:16]]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def _looks_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in ("key", "token", "secret", "password"))


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
