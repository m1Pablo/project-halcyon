"""Router-only Halcyon assistant constrained to menu slash commands."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import threading
from typing import Any, Callable

from halcyon.chat_router import (
    DEFAULT_ROUTER_MODEL,
    MISSING_KEY_REASON,
    RoutingDecision,
    run_chat_routing,
)
from halcyon.command_executor import (
    CommandProgress,
    ImmediateCommandOutcome,
    PendingCommandOutcome,
    TerminalCommandOutcome,
)
from halcyon.mcp_tools import execute_tool

DISABLED_MESSAGE = MISSING_KEY_REASON

ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]
ContextProvider = Callable[[], dict[str, Any]]
MAX_ASSISTANT_OPERATION_TEXT = 900
MAX_ASSISTANT_HISTORY_ENTRIES = 8
MAX_ASSISTANT_HISTORY_TEXT = 1_000


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
        self._lock = threading.RLock()
        selected_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        self._api_key = _normalize_optional_api_key(selected_key)
        self._client = client
        self._context_provider = context_provider
        self._history: list[dict[str, str]] = []

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._client or self._api_key)

    def set_api_key(self, api_key: str | None) -> None:
        """Apply a new credential to subsequent requests without losing history."""

        with self._lock:
            self._api_key = _normalize_optional_api_key(api_key)

    def ask(self, message: str) -> AssistantResult:
        with self._lock:
            client = self._client
            api_key = self._api_key
            chat_context = [dict(entry) for entry in self._history]
        decision = run_chat_routing(
            message,
            menu_def=self.menu_def,
            app_context=self._app_context(),
            chat_context=chat_context,
            client=client,
            api_key=api_key,
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
            context = self._context_provider()
        except Exception:
            return None
        return context if isinstance(context, dict) else None

    def _remember(self, user_message: str, assistant_message: str) -> None:
        with self._lock:
            self._history.extend(
                (
                    {
                        "role": "user",
                        "content": str(user_message)[:MAX_ASSISTANT_HISTORY_TEXT],
                    },
                    {
                        "role": "assistant",
                        "content": str(assistant_message)[:MAX_ASSISTANT_HISTORY_TEXT],
                    },
                )
            )
            del self._history[:-MAX_ASSISTANT_HISTORY_ENTRIES]


def _normalize_optional_api_key(api_key: str | None) -> str | None:
    if api_key is None:
        return None
    normalized = str(api_key).strip()
    return normalized or None


def openai_tool_definitions() -> list[dict[str, Any]]:
    """Return no raw tools; the LLM may only emit menu slash commands."""

    return []


def local_tool_executor(host: object) -> ToolExecutor:
    def _execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_tool(host, name, arguments)  # type: ignore[arg-type]

    return _execute


def render_command_accepted(outcome: PendingCommandOutcome | dict[str, Any]) -> str:
    """Render a bounded acknowledgement without exposing operation internals."""

    payload = outcome.accepted if isinstance(outcome, PendingCommandOutcome) else outcome
    operation = payload.get("operation") if isinstance(payload, dict) else None
    coach = payload.get("coach") if isinstance(payload, dict) else None
    operation_id = str(operation.get("operation_id", "")) if isinstance(operation, dict) else ""
    phase = str(coach.get("phase", "running"))[:128] if isinstance(coach, dict) else "running"
    suffix = f" ({operation_id[:48]})" if operation_id else ""
    return _bounded_operation_text(f"Coach accepted{suffix}. Phase: {phase}.")


def render_command_progress(progress: CommandProgress | dict[str, Any]) -> str:
    """Render one bounded progress line from whitelisted fields only."""

    if isinstance(progress, CommandProgress):
        phase = progress.phase
        message = progress.message
        percent = progress.percent
    else:
        phase = str(progress.get("phase", "running"))[:128]
        message = str(progress.get("message", "Coach work is running."))[:500]
        raw_percent = progress.get("percent")
        percent = raw_percent if isinstance(raw_percent, int) and 0 <= raw_percent <= 100 else None
    percentage = f" {percent}%" if percent is not None else ""
    return _bounded_operation_text(f"Coach {phase}{percentage}: {message}")


def render_command_terminal(
    outcome: TerminalCommandOutcome | ImmediateCommandOutcome | dict[str, Any],
) -> str:
    """Render only bounded aggregate terminal fields; raw MIDI is never inspected."""

    if isinstance(outcome, (TerminalCommandOutcome, ImmediateCommandOutcome)):
        payload = outcome.public
        terminal_status = outcome.status if isinstance(outcome, TerminalCommandOutcome) else None
    else:
        payload = outcome
        terminal_status = None
    if not isinstance(payload, dict):
        return "Coach finished."
    if not payload.get("ok", False):
        error = payload.get("error")
        if not isinstance(error, dict):
            return "Coach operation failed."
        code = str(error.get("code", "COACH_FAILED"))[:80]
        message = str(error.get("message", "Coach operation failed."))[:500]
        raw_resolution = error.get("resolution")
        resolution = (
            str(raw_resolution)[:500]
            if isinstance(raw_resolution, str) and raw_resolution.strip()
            else ""
        )
        if terminal_status == "timed_out":
            label = "timed out"
        elif terminal_status == "cancelled":
            label = "stopped"
        elif code == "SOURCE_REQUIRED":
            label = "needs input"
        else:
            label = "failed"
        next_step = f" Next: {resolution}" if resolution else ""
        return _bounded_operation_text(f"Coach {label} [{code}]: {message}{next_step}")

    report = payload.get("report")
    if not isinstance(report, dict):
        message = str(payload.get("message", "Coach operation completed."))[:500]
        return _bounded_operation_text(message)
    metrics = report.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    overall = metrics.get("overall_score_percent")
    pitch = metrics.get("pitch_accuracy_percent")
    completion = metrics.get("completion_percent")
    summary_parts = []
    if isinstance(overall, (int, float)):
        summary_parts.append(f"overall {overall:g}%")
    if isinstance(pitch, (int, float)):
        summary_parts.append(f"pitch {pitch:g}%")
    if isinstance(completion, (int, float)):
        summary_parts.append(f"completion {completion:g}%")
    tips = report.get("tips")
    safe_tips = [str(tip)[:180] for tip in tips[:3]] if isinstance(tips, list) else []
    score_text = ", ".join(summary_parts) or "aggregate feedback ready"
    tip_text = f" Tips: {'; '.join(safe_tips)}" if safe_tips else ""
    return _bounded_operation_text(f"Coach complete: {score_text}.{tip_text}")


def _bounded_operation_text(value: str) -> str:
    normalized = " ".join(str(value).split())
    return normalized[:MAX_ASSISTANT_OPERATION_TEXT]


def _decision_text(decision: RoutingDecision) -> str:
    if decision.action == "reject":
        return _bounded_operation_text(decision.reason or "I could not route that request.")
    commands = ", ".join(f"`{command}`" for command in decision.commands)
    return _bounded_operation_text(f"Routing to {commands}.")
