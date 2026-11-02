"""Bounded truthful run tracking for immediate and pending local MCP commands."""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import Future
import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import math
import re
import threading
import time
from typing import Any, Literal
import uuid

from halcyon.assistant import render_command_terminal
from halcyon.command_executor import (
    CommandProgress,
    PendingCommandOutcome,
    TerminalCommandOutcome,
)
from halcyon.console_events import ConsoleEvent

RunState = Literal["queued", "running", "completed", "failed", "cancelled", "timed_out"]
TerminalRunState = Literal["completed", "failed", "cancelled", "timed_out"]
TERMINAL_RUN_STATES: set[RunState] = {"completed", "failed", "cancelled", "timed_out"}

_MAX_COMMAND_CHARS = 500
_MAX_SOURCE_CHARS = 80
_MIN_TIMEOUT_S = 0.001
_MAX_TIMEOUT_S = 86_400.0
_CAPACITY_ERROR = "Run tracker capacity is full; all retained runs are active."
_INVALID_RESULT_ERROR = "Pending command completion was invalid."

_PUBLIC_PAYLOAD_KEYS = frozenset(
    {
        "attempt_id",
        "authored",
        "event_count",
        "expected_note_count",
        "fidelity",
        "key_signature",
        "keyboard",
        "keys",
        "lesson_id",
        "matched_note_count",
        "metrics",
        "observed_note_count",
        "provenance",
        "provenance_label",
        "report",
    }
)
_PRIVATE_PAYLOAD_TOKENS = frozenset(
    {
        "accesstoken",
        "accesstokens",
        "admission",
        "admissions",
        "admissionstate",
        "admissionstates",
        "apikey",
        "apikeys",
        "authoring",
        "authoringoutput",
        "authoringoutputs",
        "authoringproposal",
        "authoringproposals",
        "authoringrequest",
        "authoringrequests",
        "authoringresponse",
        "authoringresponses",
        "command",
        "commandid",
        "commandids",
        "commands",
        "consumer",
        "consumererror",
        "consumererrors",
        "consumers",
        "clientcredential",
        "clientcredentials",
        "credential",
        "credentials",
        "dispatch",
        "dispatches",
        "dispatchid",
        "dispatchids",
        "dispatchresult",
        "dispatchresults",
        "event",
        "eventhistories",
        "eventhistory",
        "eventlog",
        "eventlogs",
        "events",
        "histories",
        "history",
        "key",
        "keys",
        "lesson",
        "lessonpath",
        "lessons",
        "log",
        "logging",
        "logs",
        "midi",
        "midievent",
        "midievents",
        "midihistories",
        "midihistory",
        "midilog",
        "midilogs",
        "midimessage",
        "midimessages",
        "midinote",
        "midinotes",
        "note",
        "notes",
        "password",
        "passwords",
        "path",
        "paths",
        "port",
        "ports",
        "private",
        "privatekey",
        "privatekeys",
        "privatepath",
        "prompt",
        "prompts",
        "provider",
        "provideroutput",
        "provideroutputs",
        "providerproposal",
        "providerproposals",
        "providerrequest",
        "providerrequests",
        "providerresponse",
        "providerresponses",
        "providers",
        "queue",
        "queuename",
        "queuenames",
        "queues",
        "raw",
        "rawattempt",
        "rawmessage",
        "rawmessages",
        "rawnotes",
        "recentmidi",
        "receipt",
        "receiptid",
        "receiptids",
        "receipts",
        "receiptstate",
        "receiptstates",
        "requiredreceiptcount",
        "secret",
        "secrets",
        "sourceport",
        "sourceports",
        "systemprompt",
        "token",
        "tokens",
    }
)
_ACRONYM_BOUNDARY_RE = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_LESSON_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\Z")
_CONTENT_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_LESSON_EVENT_ID_RE = re.compile(r"e1-[0-9a-f]{64}\Z")
_REPORT_METRIC_KEYS = (
    "scoring_version",
    "expected_note_count",
    "observed_note_count",
    "matched_note_count",
    "pitch_accuracy_percent",
    "missed_count",
    "wrong_pitch_count",
    "extra_count",
    "onset_mean_deviation_ms",
    "onset_p95_deviation_ms",
    "held_duration_mean_deviation_ms",
    "completion_percent",
    "overall_score_percent",
    "truncated",
    "tolerances_ms",
    "weights_percent",
)


def _default_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(slots=True)
class RunRecord:
    run_id: str
    command: str
    source: str
    status: RunState
    created_at: str
    timeout_s: float
    parent_run_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    deadline_monotonic: float | None = None
    operation_id: str | None = None
    session_id: str | None = None
    cancellation_requested: Literal["timeout"] | None = None
    accepted: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    events_truncated: bool = False
    events_total: int = 0
    explicit_failure: bool = False
    pending_cancel: Callable[[Literal["stop", "timeout"]], bool] | None = field(
        default=None,
        repr=False,
    )
    pending_authoritative: bool = field(default=False, repr=False)
    pending_completion: Future[TerminalCommandOutcome] | None = field(default=None, repr=False)
    progress_unsubscribe: Callable[[], None] | None = field(default=None, repr=False)

    def as_status_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "command": self.command,
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "timeout_s": self.timeout_s,
            "operation_id": self.operation_id,
            "session_id": self.session_id,
            "cancellation_requested": self.cancellation_requested,
        }
        if self.parent_run_id is not None:
            payload["parent_run_id"] = self.parent_run_id
        if self.accepted is not None:
            payload["accepted"] = copy.deepcopy(self.accepted)
        if self.result is not None:
            payload["result"] = copy.deepcopy(self.result)
        return payload


class RunTracker:
    """Track immediate runs and bind pending operations without false completion."""

    _RUN_ID_SUFFIX_RE = re.compile(r"([0-9a-fA-F]{32})$")

    def __init__(
        self,
        *,
        max_runs: int = 256,
        max_events_per_run: int = 3000,
        max_global_events: int = 12000,
        clock: Callable[[], float] = time.monotonic,
        now_iso: Callable[[], str] = _default_now_iso,
    ) -> None:
        self._max_runs = max(1, int(max_runs))
        self._max_events_per_run = max(1, int(max_events_per_run))
        self._runs: OrderedDict[str, RunRecord] = OrderedDict()
        self._global_events: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_global_events)))
        self._active_run_ids: set[str] = set()
        self._clock = clock
        self._now_iso = now_iso
        self._lock = threading.RLock()
        self._console_observers: dict[
            str, tuple[Callable[[ConsoleEvent], None], threading.RLock]
        ] = {}
        self._console_notifications: deque[tuple[ConsoleEvent, tuple[str, ...]]] = deque(
            maxlen=1_000
        )
        self._console_notifying = False

    def subscribe(self, callback: Callable[[ConsoleEvent], None]) -> Callable[[], None]:
        """Observe future presentation events; never expose them through MCP payloads."""
        token = uuid.uuid4().hex
        delivery_lock = threading.RLock()
        with self._lock:
            self._console_observers[token] = (callback, delivery_lock)

        def unsubscribe() -> None:
            # Wait for an in-flight callback, but never hold the tracker lock while
            # waiting. After return no callback can begin for this subscription.
            with delivery_lock:
                with self._lock:
                    self._console_observers.pop(token, None)

        return unsubscribe

    def _queue_console_event_locked(
        self,
        record: RunRecord,
        *,
        event: dict[str, Any] | None = None,
        console_details: str | None = None,
    ) -> None:
        if not self._console_observers:
            return
        level = str((event or {}).get("level", "INFO")).lower()
        severity = "error" if level in {"error", "critical"} else level
        if severity not in {"debug", "info", "warning", "error"}:
            severity = "info"
        if event is None and record.status in {"failed", "timed_out"}:
            severity = "error"
        elif event is None and record.status == "cancelled":
            severity = "warning"
        details = event if event is not None else record.result or record.accepted
        rendered_details = json.dumps(details, ensure_ascii=False) if details else record.error
        if console_details:
            rendered_details = console_details + (
                f"\n\n{rendered_details}" if rendered_details else ""
            )
        self._console_notifications.append(
            (
                ConsoleEvent(
                    event_id=uuid.uuid4().hex,
                    kind="log" if event is not None else "command",
                    role="assistant" if record.source == "llm" else "client",
                    source=record.source,
                    text=str(event.get("message", event.get("kind", "Progress")))
                    if event is not None
                    else record.command,
                    severity=severity,
                    run_id=record.run_id,
                    parent_run_id=record.parent_run_id,
                    session_id=record.session_id,
                    operation_id=record.operation_id,
                    status=record.status,
                    details=rendered_details,
                ),
                tuple(self._console_observers),
            )
        )

    def _notify_console(self) -> None:
        # Synchronous drain, not a background queue. Reentrant callbacks may append
        # more events; this owner drains them in order after releasing the lock.
        with self._lock:
            if self._console_notifying:
                return
            self._console_notifying = True
        try:
            while True:
                with self._lock:
                    if not self._console_notifications:
                        self._console_notifying = False
                        return
                    event, tokens = self._console_notifications.popleft()
                    observers = tuple(
                        (token, self._console_observers[token])
                        for token in tokens
                        if token in self._console_observers
                    )
                for token, subscription in observers:
                    callback, delivery_lock = subscription
                    with delivery_lock:
                        with self._lock:
                            active = self._console_observers.get(token) is subscription
                        if not active:
                            continue
                        try:
                            callback(event)
                        except Exception:
                            # Presentation cannot change execution authority.
                            pass
        except BaseException:
            with self._lock:
                self._console_notifying = False
            raise

    @staticmethod
    def _is_terminal(state: RunState) -> bool:
        return state in TERMINAL_RUN_STATES

    @staticmethod
    def _event_is_error(event: dict[str, Any]) -> bool:
        level = str(event.get("level", "")).strip().upper()
        return level in {"ERROR", "CRITICAL"}

    def create_run(
        self,
        *,
        command: str,
        source: str,
        timeout_s: float = 300.0,
        parent_run_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        timeout_value = _validated_timeout(timeout_s)
        self._service_timeouts()
        run_id = uuid.uuid4().hex
        created_at = self._now_iso()
        deadline = self._clock() + timeout_value
        unsubscribes: list[Callable[[], None]] = []
        admitted = False
        payload: dict[str, Any] | None = None
        with self._lock:
            unsubscribes, admitted = self._evict_for_admission_locked()
            if admitted:
                record = RunRecord(
                    run_id=run_id,
                    command=_bounded_command(command),
                    source=_bounded_source(source),
                    status="queued",
                    created_at=created_at,
                    timeout_s=timeout_value,
                    parent_run_id=parent_run_id,
                    session_id=session_id,
                    deadline_monotonic=deadline,
                )
                self._runs[run_id] = record
                self._active_run_ids.add(run_id)
                payload = record.as_status_payload()
                self._queue_console_event_locked(record)
        self._call_unsubscribes(unsubscribes)
        if not admitted or payload is None:
            raise RuntimeError(_CAPACITY_ERROR)
        return run_id, payload

    def bind_pending(self, run_id: str, pending: PendingCommandOutcome) -> bool:
        """Bind one opaque pending operation and settle only from its terminal Future."""

        operation_id = pending.operation_id
        session_id = pending.session_id
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or self._is_terminal(record.status) or record.pending_authoritative:
                return False
            record.operation_id = operation_id
            record.session_id = session_id
            record.accepted = _bounded_payload(pending.accepted)
            record.pending_cancel = pending.cancel
            record.pending_authoritative = True
            record.pending_completion = pending.completion
            if record.started_at is None:
                record.started_at = self._now_iso()
            record.status = "running"
            self._active_run_ids.add(run_id)

            self._queue_console_event_locked(record)
        self._notify_console()

        try:
            pending.completion.add_done_callback(
                lambda future: self._settle_pending(
                    run_id,
                    operation_id,
                    session_id,
                    future,
                )
            )
        except Exception:
            self._fail_invalid_pending(run_id, operation_id, session_id, pending.completion)
            return True

        with self._lock:
            record = self._runs.get(run_id)
            should_subscribe = self._pending_matches_locked(
                record,
                operation_id,
                session_id,
            )
        if not should_subscribe:
            return True

        try:
            unsubscribe = pending.subscribe(
                lambda progress: self._record_pending_progress(
                    run_id,
                    operation_id,
                    session_id,
                    progress,
                )
            )
        except Exception:
            self._record_subscription_failure(run_id, operation_id, session_id)
            self._service_timeouts()
            return True
        if not callable(unsubscribe):
            self._record_subscription_failure(run_id, operation_id, session_id)
            self._service_timeouts()
            return True

        detach: Callable[[], None] | None = None
        with self._lock:
            record = self._runs.get(run_id)
            if self._pending_matches_locked(record, operation_id, session_id):
                if record is not None and record.progress_unsubscribe is None:
                    record.progress_unsubscribe = unsubscribe
                else:
                    detach = unsubscribe
            else:
                detach = unsubscribe
        self._call_unsubscribes([detach] if detach is not None else [])
        self._service_timeouts()
        return True

    def mark_running(self, run_id: str) -> bool:
        self._service_timeouts()
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or self._is_terminal(record.status):
                return False
            if record.started_at is None:
                record.started_at = self._now_iso()
            changed = record.status != "running"
            record.status = "running"
            self._active_run_ids.add(run_id)
            if changed:
                self._queue_console_event_locked(record)
        self._notify_console()
        return True

    def mark_failed(
        self,
        run_id: str,
        *,
        error: str,
        result: dict[str, Any] | None = None,
    ) -> bool:
        self._service_timeouts()
        unsubscribe: Callable[[], None] | None = None
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or self._is_terminal(record.status) or record.pending_authoritative:
                return False
            if result is not None:
                record.result = _bounded_payload(result)
            changed, unsubscribe = self._set_terminal_locked(
                run_id,
                status="failed",
                error=error,
                console_details=_coach_console_summary(record, result, status="failed"),
            )
        self._call_unsubscribes([unsubscribe] if unsubscribe is not None else [])
        return changed

    def mark_cancelled(self, run_id: str, *, error: str | None = None) -> bool:
        self._service_timeouts()
        unsubscribe: Callable[[], None] | None = None
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or self._is_terminal(record.status) or record.pending_authoritative:
                return False
            changed, unsubscribe = self._set_terminal_locked(
                run_id,
                status="cancelled",
                error=error,
            )
        self._call_unsubscribes([unsubscribe] if unsubscribe is not None else [])
        return changed

    def mark_timeout(self, run_id: str, *, error: str | None = None) -> bool:
        """Request bound cancellation, or terminalize an unbound immediate run."""

        cancel: Callable[[Literal["stop", "timeout"]], bool] | None = None
        unsubscribe: Callable[[], None] | None = None
        changed = False
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or self._is_terminal(record.status):
                return False
            if record.pending_authoritative:
                if record.cancellation_requested is not None:
                    return False
                cancel = self._request_timeout_locked(run_id, record)
                changed = True
            else:
                changed, unsubscribe = self._set_terminal_locked(
                    run_id,
                    status="timed_out",
                    error=error or "Run timed out after cancellation cleanup.",
                )
        self._call_unsubscribes([unsubscribe] if unsubscribe is not None else [])
        if cancel is not None:
            self._dispatch_timeout_cancel(run_id, cancel)
        return changed

    def mark_completed(
        self,
        run_id: str,
        *,
        success: bool | None = None,
        result: dict[str, Any] | None = None,
    ) -> bool:
        self._service_timeouts()
        unsubscribe: Callable[[], None] | None = None
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or self._is_terminal(record.status) or record.pending_authoritative:
                return False
            if result is not None:
                record.result = _bounded_payload(result)
            inferred_success = success
            if inferred_success is None:
                inferred_success = not record.explicit_failure and not any(
                    self._event_is_error(event) for event in record.events
                )
            if inferred_success:
                changed, unsubscribe = self._set_terminal_locked(
                    run_id,
                    status="completed",
                    console_details=_coach_console_summary(record, result, status="completed"),
                )
            else:
                changed, unsubscribe = self._set_terminal_locked(
                    run_id,
                    status="failed",
                    error=record.error or "Command failed.",
                    console_details=_coach_console_summary(record, result, status="failed"),
                )
        self._call_unsubscribes([unsubscribe] if unsubscribe is not None else [])
        return changed

    def record_log_event(self, event: dict[str, Any]) -> None:
        self._service_timeouts()
        with self._lock:
            payload = _bounded_payload(event)
            self._global_events.append(payload)
            for run_id in self._resolve_target_run_ids_locked(payload):
                self._append_event_locked(run_id, payload)
        self._notify_console()

    def get_status(self, run_id: str) -> dict[str, Any] | None:
        self._service_timeouts()
        with self._lock:
            record = self._runs.get(run_id)
            return record.as_status_payload() if record is not None else None

    def get_logs(self, run_id: str) -> dict[str, Any] | None:
        self._service_timeouts()
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            return {
                **record.as_status_payload(),
                "events": copy.deepcopy(record.events),
                "truncated": record.events_truncated,
                "next_cursor": record.events_total,
                "first_cursor": record.events_total - len(record.events),
            }

    def wait_for_run(
        self, run_id: str, *, cursor: int = 0, max_wait_s: float = 0.0
    ) -> dict[str, Any] | None:
        """Bounded incremental wait. Call only off the UI thread."""
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a nonnegative integer")
        if (
            isinstance(max_wait_s, bool)
            or not isinstance(max_wait_s, (int, float))
            or not math.isfinite(max_wait_s)
            or not 0 <= max_wait_s <= 10
        ):
            raise ValueError("max_wait_s must be between 0 and 10")
        deadline = time.monotonic() + max_wait_s
        while True:
            payload = self.get_logs(run_id)
            if payload is None:
                return None
            terminal = payload["status"] in TERMINAL_RUN_STATES
            if terminal or payload["next_cursor"] > cursor or time.monotonic() >= deadline:
                break
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        first = payload.pop("first_cursor")
        payload["events"] = payload["events"][max(0, cursor - first) :]
        payload["cursor_truncated"] = cursor < first
        payload["terminal"] = terminal
        result = payload.get("result") or {}
        payload["verdict"] = {
            "errors": [payload["error"]] if payload.get("error") else [],
            "warnings": result.get("warnings", []),
            "needs_attention": payload["status"] in {"failed", "cancelled", "timed_out"}
            or bool(result.get("warnings")),
        }
        return payload

    def get_state(self) -> dict[str, Any]:
        self._service_timeouts()
        with self._lock:
            active_run_ids = self._ordered_active_run_ids_locked()
            active_statuses = [
                self._runs[run_id].as_status_payload()
                for run_id in active_run_ids
                if run_id in self._runs
            ]
            active_status = active_statuses[0] if len(active_statuses) == 1 else None
            return {
                "active_run_id": active_status["run_id"] if active_status else None,
                "active_run": active_status,
                "active_run_ids": active_run_ids,
                "active_runs": active_statuses,
                "runs_total": len(self._runs),
                "global_events": len(self._global_events),
            }

    def _record_pending_progress(
        self,
        run_id: str,
        operation_id: str,
        session_id: str,
        progress: CommandProgress,
    ) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if (
                not isinstance(progress, CommandProgress)
                or not self._pending_matches_locked(record, operation_id, session_id)
                or progress.operation_id != operation_id
                or progress.session_id != session_id
            ):
                return
            self._append_event_locked(
                run_id,
                {
                    "run_id": run_id,
                    "level": "INFO",
                    "kind": "progress",
                    **progress.to_public_dict(),
                },
            )
        self._notify_console()

    def _settle_pending(
        self,
        run_id: str,
        operation_id: str,
        session_id: str,
        future: Future[TerminalCommandOutcome],
    ) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if (
                not self._pending_matches_locked(record, operation_id, session_id)
                or record is None
                or record.pending_completion is not future
            ):
                return
        if not future.done():
            return
        try:
            terminal = future.result()
            if (
                not isinstance(terminal, TerminalCommandOutcome)
                or terminal.operation_id != operation_id
                or terminal.session_id != session_id
            ):
                raise TypeError("pending completion returned an invalid terminal outcome")
        except Exception:
            self._fail_invalid_pending(run_id, operation_id, session_id, future)
            return

        unsubscribe: Callable[[], None] | None = None
        with self._lock:
            record = self._runs.get(run_id)
            if (
                not self._pending_matches_locked(record, operation_id, session_id)
                or record is None
                or record.pending_completion is not future
            ):
                return
            assert record is not None
            record.result = _bounded_payload(terminal.public)
            status: TerminalRunState = terminal.status
            if record.cancellation_requested == "timeout" and status == "cancelled":
                status = "timed_out"
            error = _terminal_error(record.result)
            _changed, unsubscribe = self._set_terminal_locked(
                run_id,
                status=status,
                error=error,
                console_details=_coach_console_summary(record, replace(terminal, status=status)),
            )
        self._call_unsubscribes([unsubscribe] if unsubscribe is not None else [])

    def _fail_invalid_pending(
        self,
        run_id: str,
        operation_id: str,
        session_id: str,
        future: Future[TerminalCommandOutcome],
    ) -> None:
        unsubscribe: Callable[[], None] | None = None
        with self._lock:
            record = self._runs.get(run_id)
            if (
                not self._pending_matches_locked(record, operation_id, session_id)
                or record is None
                or record.pending_completion is not future
            ):
                return
            assert record is not None
            record.result = {
                "ok": False,
                "error": {
                    "code": "COMMAND_RESULT_INVALID",
                    "message": _INVALID_RESULT_ERROR,
                    "retryable": True,
                },
            }
            _changed, unsubscribe = self._set_terminal_locked(
                run_id,
                status="failed",
                error=_INVALID_RESULT_ERROR,
            )
        self._call_unsubscribes([unsubscribe] if unsubscribe is not None else [])

    def _record_subscription_failure(
        self,
        run_id: str,
        operation_id: str,
        session_id: str,
    ) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if not self._pending_matches_locked(record, operation_id, session_id):
                return
            self._append_event_locked(
                run_id,
                {
                    "run_id": run_id,
                    "level": "WARNING",
                    "kind": "progress_subscription_unavailable",
                    "message": "Progress subscription is unavailable; terminal tracking continues.",
                },
            )
        self._notify_console()

    def _service_timeouts(self) -> None:
        actions: list[tuple[str, Callable[[Literal["stop", "timeout"]], bool]]] = []
        unsubscribes: list[Callable[[], None]] = []
        with self._lock:
            now = self._clock()
            for run_id in list(self._active_run_ids):
                record = self._runs.get(run_id)
                if record is None or self._is_terminal(record.status):
                    self._active_run_ids.discard(run_id)
                    continue
                deadline = record.deadline_monotonic
                if deadline is None or now < deadline:
                    continue
                if not record.pending_authoritative:
                    _changed, unsubscribe = self._set_terminal_locked(
                        run_id,
                        status="timed_out",
                        error=f"Run exceeded timeout of {record.timeout_s:.1f}s.",
                    )
                    if unsubscribe is not None:
                        unsubscribes.append(unsubscribe)
                    continue
                if record.cancellation_requested is None:
                    cancel = self._request_timeout_locked(run_id, record)
                    if cancel is not None:
                        actions.append((run_id, cancel))

        self._call_unsubscribes(unsubscribes)
        for run_id, cancel in actions:
            self._dispatch_timeout_cancel(run_id, cancel)

    def _request_timeout_locked(
        self,
        run_id: str,
        record: RunRecord,
    ) -> Callable[[Literal["stop", "timeout"]], bool] | None:
        record.cancellation_requested = "timeout"
        self._append_event_locked(
            run_id,
            {
                "run_id": run_id,
                "level": "WARNING",
                "kind": "cancellation_requested",
                "reason": "timeout",
                "message": "Timeout requested operation cancellation; cleanup is pending.",
            },
        )
        return record.pending_cancel

    def _dispatch_timeout_cancel(
        self,
        run_id: str,
        cancel: Callable[[Literal["stop", "timeout"]], bool],
    ) -> None:
        # Cancellation may synchronously publish progress or complete its Future.
        # It must never run while the tracker lock is held.
        try:
            accepted = bool(cancel("timeout"))
        except Exception:
            accepted = False
        with self._lock:
            record = self._runs.get(run_id)
            if (
                record is None
                or self._is_terminal(record.status)
                or not record.pending_authoritative
                or record.pending_cancel is not cancel
                or record.cancellation_requested != "timeout"
            ):
                return
            self._append_event_locked(
                run_id,
                {
                    "run_id": run_id,
                    "level": "INFO" if accepted else "WARNING",
                    "kind": "cancellation_dispatch",
                    "reason": "timeout",
                    "message": (
                        "Timeout cancellation was dispatched."
                        if accepted
                        else "Operation is already finishing; awaiting terminal cleanup."
                    ),
                },
            )
        self._notify_console()

    def _set_terminal_locked(
        self,
        run_id: str,
        *,
        status: TerminalRunState,
        error: str | None = None,
        console_details: str | None = None,
    ) -> tuple[bool, Callable[[], None] | None]:
        record = self._runs.get(run_id)
        if record is None or self._is_terminal(record.status):
            return False, None
        record.status = status
        record.error = str(error)[:500] if error is not None else None
        record.finished_at = self._now_iso()
        if status in {"failed", "timed_out"}:
            record.explicit_failure = True
        record.pending_cancel = None
        record.pending_authoritative = False
        record.pending_completion = None
        unsubscribe = self._take_unsubscribe_locked(record)
        self._active_run_ids.discard(run_id)
        self._queue_console_event_locked(record, console_details=console_details)
        if console_details:
            self._queue_console_event_locked(
                record,
                event={
                    "message": console_details,
                    "level": "ERROR"
                    if status in {"failed", "timed_out"}
                    else "WARNING"
                    if status == "cancelled"
                    else "INFO",
                },
            )
        return True, unsubscribe

    def _append_event_locked(self, run_id: str, event: dict[str, Any]) -> None:
        record = self._runs.get(run_id)
        if record is None or self._is_terminal(record.status):
            return
        payload = _bounded_payload(event)
        payload["run_id"] = run_id
        if self._event_is_error(payload):
            record.explicit_failure = True
        record.events.append(payload)
        record.events_total += 1
        self._queue_console_event_locked(record, event=payload)
        if len(record.events) > self._max_events_per_run:
            overflow = len(record.events) - self._max_events_per_run
            del record.events[:overflow]
            record.events_truncated = True

    def _evict_for_admission_locked(
        self,
    ) -> tuple[list[Callable[[], None]], bool]:
        unsubscribes: list[Callable[[], None]] = []
        while len(self._runs) >= self._max_runs:
            terminal_id = next(
                (
                    run_id
                    for run_id, record in self._runs.items()
                    if self._is_terminal(record.status)
                ),
                None,
            )
            if terminal_id is None:
                return unsubscribes, False
            record = self._runs.pop(terminal_id)
            self._active_run_ids.discard(terminal_id)
            unsubscribe = self._take_unsubscribe_locked(record)
            if unsubscribe is not None:
                unsubscribes.append(unsubscribe)
        return unsubscribes, True

    @staticmethod
    def _pending_matches_locked(
        record: RunRecord | None,
        operation_id: str,
        session_id: str,
    ) -> bool:
        return bool(
            record is not None
            and not RunTracker._is_terminal(record.status)
            and record.pending_authoritative
            and record.operation_id == operation_id
            and record.session_id == session_id
        )

    @staticmethod
    def _take_unsubscribe_locked(record: RunRecord) -> Callable[[], None] | None:
        unsubscribe = record.progress_unsubscribe
        record.progress_unsubscribe = None
        return unsubscribe

    def _call_unsubscribes(self, unsubscribes: list[Callable[[], None]]) -> None:
        for unsubscribe in unsubscribes:
            try:
                unsubscribe()
            except Exception:
                continue
        self._notify_console()

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
        return candidate if candidate in self._runs else None

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
        return active if len(active) == 1 else []


def _terminal_error(result: dict[str, Any]) -> str | None:
    error = result.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    return str(message)[:500] if message is not None else None


def _validated_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_s must be a finite number from 0.001 through 86400 seconds.")
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(
            "timeout_s must be a finite number from 0.001 through 86400 seconds."
        ) from exc
    if not math.isfinite(parsed) or not _MIN_TIMEOUT_S <= parsed <= _MAX_TIMEOUT_S:
        raise ValueError("timeout_s must be a finite number from 0.001 through 86400 seconds.")
    return parsed


def _bounded_command(value: str) -> str:
    return str(value or "").strip()[:_MAX_COMMAND_CHARS]


def _bounded_source(value: str) -> str:
    bounded = str(value or "").strip().lower()[:_MAX_SOURCE_CHARS]
    return bounded or "llm"


def _normalized_payload_key(raw_key: object) -> str | None:
    try:
        text = str(raw_key)
    except Exception:
        return None
    text = _ACRONYM_BOUNDARY_RE.sub("_", text)
    text = _CAMEL_BOUNDARY_RE.sub("_", text)
    normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return normalized or None


def _private_normalized_payload_key(normalized: str) -> bool:
    if normalized in _PUBLIC_PAYLOAD_KEYS:
        return False
    return not _PRIVATE_PAYLOAD_TOKENS.isdisjoint(normalized.split("_"))


def _bounded_text(value: object, max_chars: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= max_chars


def _integer_in_range(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _finite_number_in_range(
    value: object,
    minimum: float | None = None,
    maximum: float | None = None,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return bool(
        math.isfinite(parsed)
        and (minimum is None or parsed >= minimum)
        and (maximum is None or parsed <= maximum)
    )


def _bounded_report_lesson(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    required = {"lesson_id", "revision_id", "content_id", "segment_label", "tempo_percent"}
    if not required.issubset(value):
        return None
    lesson_id = value["lesson_id"]
    revision_id = value["revision_id"]
    content_id = value["content_id"]
    segment_label = value["segment_label"]
    tempo_percent = value["tempo_percent"]
    if not isinstance(lesson_id, str) or _LESSON_ID_RE.fullmatch(lesson_id) is None:
        return None
    if not isinstance(content_id, str) or _CONTENT_ID_RE.fullmatch(content_id) is None:
        return None
    if revision_id != f"r1-{content_id}":
        return None
    if segment_label is not None and not _bounded_text(segment_label, 80):
        return None
    if not _integer_in_range(tempo_percent, 25, 200):
        return None
    bounded = {
        "lesson_id": lesson_id,
        "revision_id": revision_id,
        "content_id": content_id,
        "segment_label": segment_label,
        "tempo_percent": tempo_percent,
    }
    if "practice_level" in value:
        practice_level = value["practice_level"]
        if not isinstance(practice_level, str) or practice_level not in {
            "melody",
            "essentials",
            "full",
        }:
            return None
        bounded["practice_level"] = practice_level
    if "ghost_note_count" in value:
        ghost_note_count = value["ghost_note_count"]
        if not _integer_in_range(ghost_note_count, 0, 2_048):
            return None
        bounded["ghost_note_count"] = ghost_note_count
    return bounded


def _bounded_report_metrics(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not set(_REPORT_METRIC_KEYS).issubset(value):
        return None
    if not _bounded_text(value["scoring_version"], 64):
        return None
    for key in (
        "expected_note_count",
        "observed_note_count",
        "matched_note_count",
        "missed_count",
        "wrong_pitch_count",
        "extra_count",
    ):
        if not _integer_in_range(value[key], 0, 2_048):
            return None
    for key in ("pitch_accuracy_percent", "completion_percent", "overall_score_percent"):
        if not _finite_number_in_range(value[key], 0, 100):
            return None
    for key in (
        "onset_mean_deviation_ms",
        "onset_p95_deviation_ms",
        "held_duration_mean_deviation_ms",
    ):
        if value[key] is not None and not _finite_number_in_range(value[key], 0):
            return None
    if not isinstance(value["truncated"], bool):
        return None
    tolerances = value["tolerances_ms"]
    if not isinstance(tolerances, dict) or not {"onset", "duration"}.issubset(tolerances):
        return None
    if not all(_finite_number_in_range(tolerances[key], 0) for key in ("onset", "duration")):
        return None
    weights = value["weights_percent"]
    weight_keys = ("pitch", "onset", "duration", "completion")
    if not isinstance(weights, dict) or not set(weight_keys).issubset(weights):
        return None
    if not all(_integer_in_range(weights[key], 0, 100) for key in weight_keys):
        return None
    if sum(weights[key] for key in weight_keys) != 100:
        return None
    return {
        "scoring_version": value["scoring_version"],
        "expected_note_count": value["expected_note_count"],
        "observed_note_count": value["observed_note_count"],
        "matched_note_count": value["matched_note_count"],
        "pitch_accuracy_percent": value["pitch_accuracy_percent"],
        "missed_count": value["missed_count"],
        "wrong_pitch_count": value["wrong_pitch_count"],
        "extra_count": value["extra_count"],
        "onset_mean_deviation_ms": value["onset_mean_deviation_ms"],
        "onset_p95_deviation_ms": value["onset_p95_deviation_ms"],
        "held_duration_mean_deviation_ms": value["held_duration_mean_deviation_ms"],
        "completion_percent": value["completion_percent"],
        "overall_score_percent": value["overall_score_percent"],
        "truncated": value["truncated"],
        "tolerances_ms": {
            "onset": tolerances["onset"],
            "duration": tolerances["duration"],
        },
        "weights_percent": {key: weights[key] for key in weight_keys},
    }


def _bounded_report_latency(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    required = {"source", "offset_ms", "jitter_ms", "onset_tolerance_ms", "match_count"}
    if (
        not required.issubset(value)
        or not isinstance(value["source"], str)
        or value["source"]
        not in {
            "calibrated",
            "estimated",
            "default",
        }
    ):
        return None
    if not _finite_number_in_range(value["offset_ms"]):
        return None
    if not _finite_number_in_range(value["jitter_ms"], 0):
        return None
    if not _finite_number_in_range(value["onset_tolerance_ms"], 0):
        return None
    if not _integer_in_range(value["match_count"], 0, 2_048):
        return None
    return {key: value[key] for key in required}


def _bounded_report_issue(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not {"code", "message"}.issubset(value):
        return None
    if not _bounded_text(value["code"], 128) or not _bounded_text(value["message"], 500):
        return None
    bounded: dict[str, Any] = {
        "code": value["code"],
        "message": value["message"],
    }
    if "expected_lesson_event_id" in value:
        lesson_event_id = value["expected_lesson_event_id"]
        if lesson_event_id is None:
            bounded["expected_lesson_event_id"] = None
        elif isinstance(lesson_event_id, str) and _LESSON_EVENT_ID_RE.fullmatch(lesson_event_id):
            bounded["expected_lesson_event_id"] = lesson_event_id
    if "expected_tick" in value:
        expected_tick = value["expected_tick"]
        if expected_tick is None or _integer_in_range(expected_tick, 0, 2**63 - 1):
            bounded["expected_tick"] = expected_tick
    for key in ("expected_note", "observed_note"):
        if key not in value:
            continue
        note = value[key]
        if note is None or _integer_in_range(note, 0, 127):
            bounded[key] = note
    for key in ("observed_offset_ms", "deviation_ms"):
        if key not in value:
            continue
        measurement = value[key]
        if measurement is None or _finite_number_in_range(measurement):
            bounded[key] = measurement
    return bounded


def _bounded_report_recommendation(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    required = {"action", "tempo_percent", "segment_label", "loops", "focus"}
    if not required.issubset(value):
        return None
    if not isinstance(value["action"], str) or value["action"] not in {
        "increase_tempo",
        "repeat_segment",
        "slow_down",
    }:
        return None
    if not _integer_in_range(value["tempo_percent"], 25, 200):
        return None
    if value["segment_label"] is not None and not _bounded_text(value["segment_label"], 80):
        return None
    if not _integer_in_range(value["loops"], 1, 8) or not _bounded_text(value["focus"], 500):
        return None
    return {key: value[key] for key in required}


def _bounded_coaching_report(value: object, *, depth: int) -> dict[str, Any] | None:
    if depth >= 6 or not isinstance(value, dict):
        return None
    required = {
        "schema_version",
        "attempt_id",
        "session_id",
        "status",
        "lesson",
        "metrics",
        "latency",
        "issues",
        "tips",
        "recommendation",
    }
    if not required.issubset(value) or type(value["schema_version"]) is not int:
        return None
    if value["schema_version"] != 1:
        return None
    if not _bounded_text(value["attempt_id"], 128) or not _bounded_text(value["session_id"], 128):
        return None
    if not isinstance(value["status"], str) or value["status"] not in {
        "complete",
        "empty",
        "truncated",
    }:
        return None
    lesson = _bounded_report_lesson(value["lesson"])
    metrics = _bounded_report_metrics(value["metrics"])
    latency = _bounded_report_latency(value["latency"])
    recommendation = _bounded_report_recommendation(value["recommendation"])
    issues_value = value["issues"]
    tips_value = value["tips"]
    if (
        lesson is None
        or metrics is None
        or latency is None
        or recommendation is None
        or not isinstance(issues_value, (list, tuple))
        or not isinstance(tips_value, (list, tuple))
    ):
        return None
    issues: list[dict[str, Any]] = []
    for issue_value in issues_value[:8]:
        issue = _bounded_report_issue(issue_value)
        if issue is None:
            return None
        issues.append(issue)
    tips: list[str] = []
    for tip in tips_value[:3]:
        if not _bounded_text(tip, 500):
            return None
        tips.append(tip)
    return {
        "schema_version": 1,
        "attempt_id": value["attempt_id"],
        "session_id": value["session_id"],
        "status": value["status"],
        "lesson": lesson,
        "metrics": metrics,
        "latency": latency,
        "issues": issues,
        "tips": tips,
        "recommendation": recommendation,
    }


def _coach_console_summary(
    record: RunRecord,
    outcome: TerminalCommandOutcome | dict[str, Any] | None,
    *,
    status: TerminalRunState | None = None,
) -> str | None:
    if not record.command.startswith("/coach ") or outcome is None:
        return None
    if isinstance(outcome, TerminalCommandOutcome):
        status = outcome.status
        payload = outcome.public
    else:
        payload = outcome
    if status != "completed" and payload.get("ok"):
        return None
    try:
        # UI-only whitelist projection before strict public report validation.
        # Never store this string in the record or a public result/log payload.
        return render_command_terminal(outcome)
    except Exception:
        return None


def _bounded_payload(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "..."
    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:40]:
            normalized = _normalized_payload_key(raw_key)
            if normalized is None:
                continue
            key = str(raw_key)[:80]
            if normalized == "report":
                report = _bounded_coaching_report(item, depth=depth + 1)
                if report is not None:
                    bounded[key] = report
                continue
            if _private_normalized_payload_key(normalized):
                continue
            if normalized == "reply" and isinstance(item, str):
                bounded[key] = item[:2_000]
                continue
            bounded[key] = _bounded_payload(item, depth=depth + 1)
        return bounded
    if isinstance(value, (list, tuple)):
        return [_bounded_payload(item, depth=depth + 1) for item in value[:16]]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "<redacted-object>"
