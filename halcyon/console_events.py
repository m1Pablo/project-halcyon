"""Bounded in-memory console presentation values, never a public run serializer."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

MAX_CONSOLE_TEXT = 8_000
CONSOLE_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "timed_out"})


def safe_console_text(text: str, limit: int = MAX_CONSOLE_TEXT) -> str:
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", str(text))
    text = re.sub(
        r"(?i)\b(?:OPENAI_API_KEY|api[_ -]?key|password|access[_ -]?token)\s*[:=]\s*\S+",
        "[redacted credential]",
        text,
    )
    text = re.sub(
        r"(?:/(?:Users|home|tmp|private|Volumes)/|[A-Za-z]:\\)[^\s\"<>]+",
        "[private path]",
        text,
    )
    return text[:limit]


@dataclass(frozen=True, slots=True)
class ConsoleEvent:
    event_id: str
    kind: Literal["message", "command", "log"]
    role: Literal["user", "assistant", "client", "system"]
    source: str
    text: str
    raw_text: str | None = None
    severity: Literal["debug", "info", "warning", "error"] = "info"
    run_id: str | None = None
    parent_run_id: str | None = None
    session_id: str | None = None
    operation_id: str | None = None
    status: str | None = None
    local_origin: str | None = None
    replay_command: str | None = None
    details: str | None = None

    def __post_init__(self) -> None:
        for name in ("text", "raw_text", "details"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, safe_console_text(value))
        for name in (
            "event_id",
            "source",
            "run_id",
            "parent_run_id",
            "session_id",
            "operation_id",
            "status",
            "local_origin",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, safe_console_text(value, 128 if name != "source" else 80)
                )
        # A truncated or scrubbed command is no longer an exact replay candidate.
        command = self.replay_command
        if command is not None and (
            not command.startswith("/")
            or "\n" in command
            or "\r" in command
            or safe_console_text(command) != command
        ):
            object.__setattr__(self, "replay_command", None)
