"""Router-only Halcyon assistant constrained to menu slash commands."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Callable

from halcyon.chat_router import (
    DEFAULT_ROUTER_MODEL,
    MISSING_KEY_REASON,
    RoutingDecision,
    run_chat_routing,
)
from halcyon.mcp_tools import execute_tool

DISABLED_MESSAGE = MISSING_KEY_REASON

ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]
ContextProvider = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class AssistantResult:
    text: str
    decision: RoutingDecision
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    enabled: bool = True


class HalcyonAssistant:
    """Compatibility wrapper around the slash-command router."""

    def __init__(
        self,
        *,
        menu_def: dict[str, Any] | None = None,
        api_key: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        context_provider: ContextProvider | None = None,
    ) -> None:
        self.menu_def = menu_def
        self.model = model or os.environ.get("HALCYON_CHAT_MODEL") or DEFAULT_ROUTER_MODEL
        self._api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        self._client = client
        self._context_provider = context_provider
        self._history: list[dict[str, str]] = []

    @property
    def enabled(self) -> bool:
        return bool(self._client or self._api_key)

    def ask(self, message: str) -> AssistantResult:
        decision = run_chat_routing(
            message,
            menu_def=self.menu_def,
            app_context=self._app_context(),
            chat_context=list(self._history),
            client=self._client,
            api_key=self._api_key,
            model=self.model,
        )
        text = _decision_text(decision)
        self._remember(message, text)
        return AssistantResult(
            text=text,
            decision=decision,
            enabled=self.enabled or decision.action == "menu",
        )

    def _app_context(self) -> dict[str, Any] | None:
        if self._context_provider is None:
            return None
        try:
            return self._context_provider()
        except Exception:
            return None

    def _remember(self, user_message: str, assistant_message: str) -> None:
        self._history.extend(
            (
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            )
        )
        del self._history[:-8]


def openai_tool_definitions() -> list[dict[str, Any]]:
    """Return no raw tools; the LLM may only emit menu slash commands."""

    return []


def local_tool_executor(host: object) -> ToolExecutor:
    def _execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_tool(host, name, arguments)  # type: ignore[arg-type]

    return _execute


def _decision_text(decision: RoutingDecision) -> str:
    if decision.action == "reject":
        return decision.reason or "I could not route that request."
    commands = ", ".join(f"`{command}`" for command in decision.commands)
    return f"Routing to {commands}."
