"""OpenAI-backed music assistant for safe synth parameter tools."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any, Callable

from halcyon.mcp_tools import ToolExecutionError, execute_tool, list_synth_params, tool_definitions

DEFAULT_CHAT_MODEL = "gpt-5.5"
DISABLED_MESSAGE = "Assistant is offline. Set OPENAI_API_KEY to enable chat."

ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]

SYSTEM_PROMPT = """You are Halcyon, a concise music-synth assistant inside a local MIDI TUI.
You help shape a tiny virtual-analog synth by using only the provided synth tools.
Prefer small, musical parameter changes. Use presets when the user asks for a broad sound.
Save user presets when the user asks to preserve or name the current patch.
Never claim sound was produced; describe the patch change you made."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class AssistantResult:
    text: str
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    enabled: bool = True


class HalcyonAssistant:
    """Small Responses API wrapper that executes only Halcyon synth tools."""

    def __init__(
        self,
        tool_executor: ToolExecutor,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._tool_executor = tool_executor
        self.model = model or os.environ.get("HALCYON_CHAT_MODEL") or DEFAULT_CHAT_MODEL
        self._api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        self._client = client
        if self._client is None and self._api_key:
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key)

    @property
    def enabled(self) -> bool:
        return self._client is not None and bool(self._api_key or self._client)

    def ask(self, message: str) -> AssistantResult:
        if not self.enabled:
            return AssistantResult(DISABLED_MESSAGE, enabled=False)

        input_payload = self._initial_input(message)
        tools = openai_tool_definitions()
        response = self._client.responses.create(  # type: ignore[union-attr]
            model=self.model,
            instructions=SYSTEM_PROMPT,
            input=input_payload,
            tools=tools,
        )

        tool_results: list[dict[str, Any]] = []
        for _ in range(3):
            tool_calls = _extract_tool_calls(response)
            if not tool_calls:
                break

            tool_outputs = []
            for tool_call in tool_calls:
                result = self._execute_tool_call(tool_call)
                tool_results.append(
                    {
                        "tool": tool_call.name,
                        "arguments": tool_call.arguments,
                        "result": result,
                    }
                )
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_call.call_id,
                        "output": json.dumps(result, ensure_ascii=True, separators=(",", ":")),
                    }
                )

            followup_kwargs: dict[str, Any] = {
                "model": self.model,
                "instructions": SYSTEM_PROMPT,
                "input": tool_outputs,
                "tools": tools,
            }
            response_id = _get_value(response, "id")
            if response_id:
                followup_kwargs["previous_response_id"] = response_id
            response = self._client.responses.create(**followup_kwargs)  # type: ignore[union-attr]

        text = _extract_output_text(response).strip()
        if not text and tool_results:
            last_result = tool_results[-1]["result"]
            if isinstance(last_result, dict) and "error" in last_result:
                text = f"Tool error: {last_result['error']}"
            else:
                text = "Patch updated."
        return AssistantResult(text or "No assistant response.", tool_results=tool_results)

    def _initial_input(self, message: str) -> list[dict[str, Any]]:
        state = self._safe_context_tool("get_synth_state", {})
        params = list_synth_params()
        context = {
            "synth_state": state,
            "synth_params": params,
        }
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Current Halcyon context:\n"
                            f"{json.dumps(context, ensure_ascii=True, separators=(',', ':'))}\n\n"
                            f"User request: {message}"
                        ),
                    }
                ],
            }
        ]

    def _safe_context_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._tool_executor(name, arguments)
        except Exception as exc:
            return {"error": str(exc)}

    def _execute_tool_call(self, tool_call: ToolCall) -> dict[str, Any]:
        try:
            return self._tool_executor(tool_call.name, tool_call.arguments)
        except (ToolExecutionError, ValueError, TypeError) as exc:
            return {"error": str(exc)}


def openai_tool_definitions() -> list[dict[str, Any]]:
    """Convert the MCP-shaped tool schemas into OpenAI function tools."""

    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["inputSchema"],
        }
        for tool in tool_definitions()
    ]


def local_tool_executor(host: object) -> ToolExecutor:
    def _execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_tool(host, name, arguments)  # type: ignore[arg-type]

    return _execute


def _extract_tool_calls(response: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in _iter_response_output(response):
        item_type = _get_value(item, "type")
        if item_type != "function_call":
            continue
        name = _get_value(item, "name")
        if not name:
            continue
        arguments = _parse_arguments(_get_value(item, "arguments"))
        call_id = str(_get_value(item, "call_id") or _get_value(item, "id") or name)
        calls.append(ToolCall(str(name), arguments, call_id))
    return calls


def _extract_output_text(response: Any) -> str:
    output_text = _get_value(response, "output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    parts: list[str] = []
    for item in _iter_response_output(response):
        if _get_value(item, "type") != "message":
            continue
        content = _get_value(item, "content") or []
        for block in content:
            text = _get_value(block, "text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _iter_response_output(response: Any) -> list[Any]:
    output = _get_value(response, "output")
    return list(output) if isinstance(output, list) else []


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _get_value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
