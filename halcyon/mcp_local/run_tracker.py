"""Bounded in-memory run tracking for local MCP command execution."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import threading
import time
import uuid
from typing import Any, Literal

RunState = Literal["queued", "running", "completed", "failed", "cancelled", "timeout"]

TERMINAL_RUN_STATES: set[RunState] = {"completed", "failed", "cancelled", "timeout"}


@dataclass(slots=True)
class RunRecord:
    run_id: str
    command: str
    source: str
    status: RunState
    created_at: str
    timeout_s: float
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    deadline_monotonic: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    events_truncated: bool = False
    explicit_failure: bool = False

    def as_status_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "command": self.command,
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "timeout_s": self.timeout_s,
        }


class RunTracker:
    """Track active and recent slash-command runs."""

    _RUN_ID_SUFFIX_RE = re.compile(r"([0-9a-fA-F]{32})$")

    def __init__(
        self,
        *,
        max_runs: int = 256,
        max_events_per_run: int = 3000,
        max_global_events: int = 12000,
    ) -> None:
        self._max_runs = max(16, int(max_runs))
        self._max_events_per_run = max(200, int(max_events_per_run))
        self._runs: OrderedDict[str, RunRecord] = OrderedDict()
        self._global_events: deque[dict[str, Any]] = deque(maxlen=max(500, int(max_global_events)))
        self._active_run_ids: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    @staticmethod
    def _is_terminal(state: RunState) -> bool:
        return state in TERMINAL_RUN_STATES

    @staticmethod
    def _event_is_error(event: dict[str, Any]) -> bool:
        level = str(event.get("level", "")).strip().upper()
        return level in {"ERROR", "CRITICAL"}

    def _evict_runs_if_needed_locked(self) -> None:
        while len(self._runs) > self._max_runs:
            run_id, record = next(iter(self._runs.items()))
            if run_id in self._active_run_ids or not self._is_terminal(record.status):
                break
            self._runs.pop(run_id, None)

    def _check_timeout_locked(self) -> None:
        now = time.monotonic()
        for run_id in list(self._active_run_ids):
            record = self._runs.get(run_id)
            if record is None or self._is_terminal(record.status):
                self._active_run_ids.discard(run_id)
                continue
            deadline = record.deadline_monotonic
            if deadline is None or now < deadline:
                continue
            self._set_terminal_locked(
                run_id,
                status="timeout",
                error=f"Run exceeded timeout of {record.timeout_s:.1f}s.",
            )

    def _set_terminal_locked(
        self,
        run_id: str,
        *,
        status: Literal["completed", "failed", "cancelled", "timeout"],
        error: str | None = None,
    ) -> bool:
        record = self._runs.get(run_id)
        if record is None or self._is_terminal(record.status):
            return False
        record.status = status
        record.error = error
        record.finished_at = self._now_iso()
        if status in {"failed", "timeout"}:
            record.explicit_failure = True
        self._active_run_ids.discard(run_id)
        self._evict_runs_if_needed_locked()
        return True

    def create_run(
        self, *, command: str, source: str, timeout_s: float = 300.0
    ) -> tuple[str, dict[str, Any]]:
        timeout_value = max(5.0, float(timeout_s))
        now_iso = self._now_iso()
        with self._lock:
            self._check_timeout_locked()
            run_id = uuid.uuid4().hex
            record = RunRecord(
                run_id=run_id,
                command=command.strip(),
                source=(source or "llm").strip().lower(),
                status="queued",
                created_at=now_iso,
                timeout_s=timeout_value,
                deadline_monotonic=time.monotonic() + timeout_value,
            )
            self._runs[run_id] = record
            self._active_run_ids.add(run_id)
            self._evict_runs_if_needed_locked()
            return run_id, record.as_status_payload()

    def mark_running(self, run_id: str) -> bool:
        with self._lock:
            self._check_timeout_locked()
            record = self._runs.get(run_id)
            if record is None or self._is_terminal(record.status):
                return False
            if record.started_at is None:
                record.started_at = self._now_iso()
            record.status = "running"
            self._active_run_ids.add(run_id)
            return True

    def mark_failed(self, run_id: str, *, error: str) -> bool:
        with self._lock:
            return self._set_terminal_locked(run_id, status="failed", error=error)

    def mark_cancelled(self, run_id: str, *, error: str | None = None) -> bool:
        with self._lock:
            return self._set_terminal_locked(run_id, status="cancelled", error=error)

    def mark_timeout(self, run_id: str, *, error: str | None = None) -> bool:
        with self._lock:
            return self._set_terminal_locked(run_id, status="timeout", error=error)

    def mark_completed(self, run_id: str, *, success: bool | None = None) -> bool:
        with self._lock:
            self._check_timeout_locked()
            record = self._runs.get(run_id)
            if record is None or self._is_terminal(record.status):
                return False
            inferred_success = success
            if inferred_success is None:
                inferred_success = not record.explicit_failure and not any(
                    self._event_is_error(event) for event in record.events
                )
            if inferred_success:
                return self._set_terminal_locked(run_id, status="completed")
            return self._set_terminal_locked(
                run_id,
                status="failed",
                error=record.error or "Command failed.",
            )

    def record_log_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._check_timeout_locked()
            payload = dict(event)
            self._global_events.append(payload)
            for run_id in self._resolve_target_run_ids_locked(payload):
                record = self._runs.get(run_id)
                if record is None or self._is_terminal(record.status):
                    continue
                per_run_event = dict(payload)
                per_run_event["run_id"] = run_id
                if self._event_is_error(per_run_event):
                    record.explicit_failure = True
                record.events.append(per_run_event)
                if len(record.events) > self._max_events_per_run:
                    overflow = len(record.events) - self._max_events_per_run
                    del record.events[:overflow]
                    record.events_truncated = True

    def get_status(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._check_timeout_locked()
            record = self._runs.get(run_id)
            if record is None:
                return None
            return record.as_status_payload()

    def get_logs(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._check_timeout_locked()
            record = self._runs.get(run_id)
            if record is None:
                return None
            return {
                **record.as_status_payload(),
                "events": list(record.events),
                "truncated": record.events_truncated,
            }

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            self._check_timeout_locked()
            active_run_ids = self._ordered_active_run_ids_locked()
            active_statuses = [
                self._runs[run_id].as_status_payload()
                for run_id in active_run_ids
                if run_id in self._runs
            ]
            if len(active_statuses) == 1:
                active_status = active_statuses[0]
                active_run_id = str(active_status["run_id"])
            else:
                active_status = None
                active_run_id = None
            return {
                "active_run_id": active_run_id,
                "active_run": active_status,
                "active_run_ids": active_run_ids,
                "active_runs": active_statuses,
                "runs_total": len(self._runs),
                "global_events": len(self._global_events),
            }

    def _extract_run_id_from_event_locked(self, event: dict[str, Any]) -> str | None:
        explicit = str(event.get("run_id", "")).strip().lower()
        if explicit and explicit in self._runs:
            return explicit

        owner_id = str(event.get("owner_id", "")).strip()
        if not owner_id:
            return None
        match = self._RUN_ID_SUFFIX_RE.search(owner_id)
        if not match:
            return None
        candidate = match.group(1).lower()
        if candidate in self._runs:
            return candidate
        return None

    def _ordered_active_run_ids_locked(self) -> list[str]:
        return [
            run_id
            for run_id, record in self._runs.items()
            if run_id in self._active_run_ids and not self._is_terminal(record.status)
        ]

    def _resolve_target_run_ids_locked(self, event: dict[str, Any]) -> list[str]:
        inferred_run_id = self._extract_run_id_from_event_locked(event)
        if inferred_run_id is not None:
            return [inferred_run_id]

        active = self._ordered_active_run_ids_locked()
        if len(active) == 1:
            return active
        return []
