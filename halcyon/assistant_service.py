"""One tracked assistant service shared by Textual chat and local MCP."""

from __future__ import annotations

from collections import OrderedDict
import threading
import time
from typing import Any, Callable
import uuid

from halcyon.assistant import (
    HalcyonAssistant,
    MAX_ASSISTANT_HISTORY_ENTRIES,
    MAX_ASSISTANT_HISTORY_TEXT,
)
from halcyon.console_events import ConsoleEvent
from halcyon.mcp_local.run_tracker import RunTracker, TERMINAL_RUN_STATES

MAX_SESSIONS = 32
MAX_WALL_S = 600.0


class AssistantService:
    def __init__(
        self,
        assistant: HalcyonAssistant,
        tracker: RunTracker,
        *,
        command: Callable[[str, str, float], tuple[int, dict[str, Any]]],
        state: Callable[[], dict[str, Any]],
        display: Callable[[str, str], None] = lambda _kind, _text: None,
        event_sink: Callable[[ConsoleEvent], None] | None = None,
    ) -> None:
        self.assistant = assistant
        self.tracker = tracker
        self._command = command
        self._state = state
        self._display = display
        self._event_sink = event_sink
        self._sessions: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._active_run_id: str | None = None
        self._closed = threading.Event()

    def close(self) -> None:
        """Prevent further model turns and actions; do not wait on an in-flight API call."""
        self._closed.set()

    def start(
        self, goal: str, *, session_id: str | None = None, source: str = "llm"
    ) -> dict[str, Any]:
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 2_000:
            return {
                "started": False,
                "error": {
                    "code": "INVALID_GOAL",
                    "message": "Use a goal of 1 through 2000 characters.",
                },
            }
        with self._lock:
            if self._closed.is_set():
                return {
                    "started": False,
                    "error": {"code": "ASSISTANT_CLOSED", "message": "The app is closing."},
                }
            if self._worker is not None and self._worker.is_alive():
                return {
                    "started": False,
                    "active_run_id": self._active_run_id,
                    "error": {
                        "code": "ASSISTANT_BUSY",
                        "message": "An assistant request is still active. Wait for that run. Use /coach stop or /panic for immediate audio control.",
                    },
                }
            if session_id is not None and session_id not in self._sessions:
                return {
                    "started": False,
                    "error": {
                        "code": "SESSION_NOT_FOUND",
                        "message": "Assistant sessions live in memory. Omit session_id to start a new conversation.",
                    },
                }
            session_id = session_id or uuid.uuid4().hex
            goal = self.assistant.safe_text(goal)
            history = list(self._sessions.get(session_id, []))
            run_id, _ = self.tracker.create_run(
                command="/assistant ask", source=source, timeout_s=MAX_WALL_S, session_id=session_id
            )
            self.tracker.mark_running(run_id)
            # Accepted session IDs remain valid even if the first turn fails or
            # times out before producing history.
            self._sessions.setdefault(session_id, [])
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
            self._active_run_id = run_id
            self._worker = threading.Thread(
                target=self._work,
                args=(run_id, session_id, goal, history, source),
                name="halcyon-assistant-agent",
                daemon=True,
            )
            self._worker.start()
            return {
                "started": True,
                "run_id": run_id,
                "session_id": session_id,
                "status": "running",
            }

    def _show(
        self,
        kind: str,
        text: str,
        *,
        run_id: str,
        session_id: str,
        source: str,
        status: str = "running",
    ) -> None:
        if self._closed.is_set():
            return
        try:
            text = self.assistant.safe_text(text, limit=2_500)
            if self._event_sink is not None:
                # Progress is already emitted once by the tracker log observer.
                if kind != "progress":
                    self._event_sink(
                        ConsoleEvent(
                            event_id=uuid.uuid4().hex,
                            kind="message",
                            role="user" if kind == "request" else "assistant",
                            source=source,
                            text=text,
                            run_id=run_id,
                            session_id=session_id,
                            status=None if kind == "request" else status,
                            severity="error" if status == "failed" else "info",
                        )
                    )
            elif kind != "request":
                self._display(kind, text)
        except Exception:
            # UI availability is not evidence of command success or failure.
            pass

    def _work(
        self,
        run_id: str,
        session_id: str,
        goal: str,
        history: list[dict[str, str]],
        source: str,
    ) -> None:
        deadline = time.monotonic() + MAX_WALL_S

        def show(kind: str, text: str, status: str = "running") -> None:
            self._show(
                kind, text, run_id=run_id, session_id=session_id, source=source, status=status
            )

        show("request", goal)

        def stopped() -> bool:
            status = self.tracker.get_status(run_id)
            return (
                self._closed.is_set()
                or status is None
                or status["status"] in TERMINAL_RUN_STATES
                or time.monotonic() >= deadline
            )

        def progress(message: str) -> None:
            if not stopped():
                self.tracker.record_log_event(
                    {
                        "run_id": run_id,
                        "level": "INFO",
                        "kind": "assistant",
                        "message": self.assistant.safe_text(message, limit=500),
                    }
                )
                show("progress", message)

        def control(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if stopped():
                return {"ok": False, "error": "Assistant stopped before executing the next action."}
            if name == "get_app_state":
                return self._state()
            _code, result = self._command(
                arguments["command"], run_id, max(1.0, deadline - time.monotonic())
            )
            child_id = result.get("run_id")
            if result.get("deduplicated"):
                return {
                    "ok": False,
                    "run_id": child_id,
                    "error": "Another operation already owns this command group. Do not rerun or claim it as your work.",
                }
            if not child_id:
                return result
            cursor = 0
            while result.get("status") not in TERMINAL_RUN_STATES:
                if stopped():
                    # Request timeout cleanup only for this assistant's child operation.
                    self.tracker.mark_timeout(child_id)
                    return {
                        "ok": False,
                        "run_id": child_id,
                        "error": "Assistant stopped; child cleanup was requested. Inspect its run before retrying.",
                    }
                result = self.tracker.wait_for_run(child_id, cursor=cursor, max_wait_s=0.2) or {
                    "status": "failed",
                    "error": "Child run was lost.",
                }
                cursor = result.get("next_cursor", cursor)
            result = {
                key: result[key]
                for key in ("run_id", "status", "result", "error", "command", "parent_run_id")
                if key in result
            }
            result["ok"] = result.get("status") == "completed"
            progress(f"{result.get('command', 'Command')}: {result.get('status', 'unknown')}")
            return result

        try:
            result = self.assistant.ask(
                goal, control=control, history=history, stopped=stopped, progress=progress
            )
            public = {
                "ok": result.stopped_reason is None,
                "reply": self.assistant.safe_text(result.text),
                "steps": result.tool_results,
                "stopped_reason": result.stopped_reason,
                "diagnostic": result.diagnostic,
                "session_id": session_id,
                "warnings": ["One or more steps failed; inspect their run results."]
                if any(step.get("ok") is False for step in result.tool_results)
                else [],
            }
            if stopped():
                self.tracker.mark_cancelled(
                    run_id, error="Assistant stopped. Inspect any child run before retrying."
                )
                return
            # History records actual outcomes, not just the earlier plan.
            exchange = [
                {
                    "role": "user",
                    "content": self.assistant.safe_text(goal, limit=MAX_ASSISTANT_HISTORY_TEXT),
                },
                {
                    "role": "assistant",
                    "content": self.assistant.safe_text(
                        result.text + "\nStep outcomes: " + str(result.tool_results),
                        limit=MAX_ASSISTANT_HISTORY_TEXT,
                    ),
                },
            ]
            with self._lock:
                self._sessions[session_id] = (history + exchange)[-MAX_ASSISTANT_HISTORY_ENTRIES:]
                self._sessions.move_to_end(session_id)
                while len(self._sessions) > MAX_SESSIONS:
                    self._sessions.popitem(last=False)
            if result.stopped_reason:
                settled = self.tracker.mark_failed(
                    run_id, error=result.stopped_reason, result=public
                )
            else:
                settled = self.tracker.mark_completed(run_id, success=True, result=public)
            reply = public["reply"]
            if public["warnings"]:
                reply += "\n" + " ".join(public["warnings"])
            if settled:
                show("reply", reply, "failed" if result.stopped_reason else "completed")
        except Exception as exc:
            error = (
                "Assistant dependency is unavailable. Check the installed environment before retrying."
                if isinstance(exc, ImportError)
                else "Assistant request failed. Inspect child runs before retrying."
            )
            settled = self.tracker.mark_failed(run_id, error=error)
            if settled:
                show("reply", error, "failed")
