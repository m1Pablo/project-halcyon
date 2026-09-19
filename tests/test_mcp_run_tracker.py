from copy import deepcopy
from concurrent.futures import Future
import threading

import pytest

from halcyon.command_executor import (
    CommandProgress,
    PendingCommandOutcome,
    TerminalCommandOutcome,
)
from halcyon.coach_models import derive_lesson_event_id, derive_lesson_id, derive_revision_id
from halcyon.coach_scoring import (
    CoachingIssue,
    CoachingReport,
    LatencyEstimate,
    PracticeRecommendation,
    ScoreMetrics,
)
from halcyon.mcp_local.run_tracker import RunTracker


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class PendingHarness:
    def __init__(self, *, cancel_result: bool = True) -> None:
        self.future: Future[TerminalCommandOutcome] = Future()
        self.cancel_result = cancel_result
        self.cancel_calls: list[str] = []
        self.subscribers = []

    def outcome(self) -> PendingCommandOutcome:
        def subscribe(callback):  # type: ignore[no-untyped-def]
            self.subscribers.append(callback)
            return lambda: (
                self.subscribers.remove(callback) if callback in self.subscribers else None
            )

        return PendingCommandOutcome(
            operation_id="operation-1",
            session_id="session-1",
            accepted={
                "ok": True,
                "code": "COACH_ACCEPTED",
                "raw_attempt": [{"note": 60}],
                "coach": {"phase": "resolving"},
            },
            completion=self.future,
            cancel_callback=self.cancel,
            subscribe_callback=subscribe,
        )

    def cancel(self, reason):  # type: ignore[no-untyped-def]
        self.cancel_calls.append(reason)
        return self.cancel_result

    def progress(self, phase: str = "authoring") -> None:
        update = CommandProgress(
            "operation-1",
            "session-1",
            phase,
            "bounded progress",
            10,
        )
        for subscriber in tuple(self.subscribers):
            subscriber(update)


class LockProbeUnsubscribe:
    def __init__(self, tracker: RunTracker) -> None:
        self.tracker = tracker
        self.calls = 0
        self.called_outside_lock: list[bool] = []

    def __call__(self) -> None:
        self.calls += 1
        acquired = threading.Event()

        def acquire_from_other_thread() -> None:
            with self.tracker._lock:
                acquired.set()

        probe = threading.Thread(target=acquire_from_other_thread, daemon=True)
        probe.start()
        outside = acquired.wait(timeout=1)
        self.called_outside_lock.append(outside)
        if outside:
            probe.join(timeout=1)


def pending_with_subscribe(
    harness: PendingHarness,
    subscribe_callback,  # type: ignore[no-untyped-def]
) -> PendingCommandOutcome:
    outcome = harness.outcome()
    return PendingCommandOutcome(
        outcome.operation_id,
        outcome.session_id,
        outcome.accepted,
        outcome.completion,
        outcome.cancel_callback,
        subscribe_callback,
    )


def test_run_tracker_records_status_and_logs() -> None:
    tracker = RunTracker()

    run_id, status = tracker.create_run(command="/panic", source="codex", timeout_s=10)
    tracker.mark_running(run_id)
    tracker.record_log_event({"run_id": run_id, "level": "INFO", "message": "started"})
    tracker.mark_completed(run_id)

    assert status["run_id"] == run_id
    assert tracker.get_status(run_id)["status"] == "completed"  # type: ignore[index]
    logs = tracker.get_logs(run_id)
    assert logs is not None
    assert logs["events"] == [{"run_id": run_id, "level": "INFO", "message": "started"}]
    assert not logs["truncated"]


def test_run_tracker_attributes_unowned_event_to_single_active_run() -> None:
    tracker = RunTracker()
    run_id, _status = tracker.create_run(command="/synth state", source="codex")
    tracker.mark_running(run_id)

    tracker.record_log_event({"level": "INFO", "message": "progress"})

    logs = tracker.get_logs(run_id)
    assert logs is not None
    assert logs["events"][0]["run_id"] == run_id


def test_run_tracker_does_not_guess_when_multiple_runs_active() -> None:
    tracker = RunTracker()
    first_id, _status = tracker.create_run(command="/synth state", source="codex")
    second_id, _status = tracker.create_run(command="/panic", source="codex")
    tracker.mark_running(first_id)
    tracker.mark_running(second_id)

    tracker.record_log_event({"level": "INFO", "message": "ambiguous"})

    assert tracker.get_logs(first_id)["events"] == []  # type: ignore[index]
    assert tracker.get_logs(second_id)["events"] == []  # type: ignore[index]


def test_run_tracker_owner_suffix_attributes_event() -> None:
    tracker = RunTracker()
    run_id, _status = tracker.create_run(command="/panic", source="codex")
    tracker.mark_running(run_id)

    tracker.record_log_event({"owner_id": f"external-{run_id}", "message": "owned"})

    logs = tracker.get_logs(run_id)
    assert logs is not None
    assert logs["events"][0]["run_id"] == run_id


def test_bind_pending_retains_bounded_accepted_progress_and_terminal_result() -> None:
    tracker = RunTracker(max_events_per_run=3)
    run_id, _status = tracker.create_run(command="/coach teach Test", source="codex")
    harness = PendingHarness()

    assert tracker.bind_pending(run_id, harness.outcome())
    harness.progress()
    running = tracker.get_status(run_id)

    assert running is not None
    assert running["status"] == "running"
    assert running["operation_id"] == "operation-1"
    assert running["session_id"] == "session-1"
    assert "raw_attempt" not in repr(running)
    logs = tracker.get_logs(run_id)
    assert logs is not None
    assert logs["events"][-1]["kind"] == "progress"
    assert logs["events"][-1]["phase"] == "authoring"

    harness.future.set_result(
        TerminalCommandOutcome(
            "operation-1",
            "session-1",
            "completed",
            {
                "ok": True,
                "code": "COACH_ATTEMPT_COMPLETE",
                "report": {"metrics": {"overall_score_percent": 88}, "raw_events": [1, 2]},
            },
        )
    )

    terminal = tracker.get_status(run_id)
    assert terminal is not None
    assert terminal["status"] == "completed"
    assert "report" not in terminal["result"]
    assert "raw_events" not in repr(terminal)
    assert harness.subscribers == []


def test_bind_after_deadline_cancels_once_and_waits_for_authoritative_future() -> None:
    clock = FakeClock()
    tracker = RunTracker(clock=clock)
    run_id, _status = tracker.create_run(
        command="/coach replay",
        source="codex",
        timeout_s=1,
    )
    harness = PendingHarness()
    lock_was_available = threading.Event()
    subscription_was_installed: list[bool] = []

    def cancel(reason):  # type: ignore[no-untyped-def]
        subscription_was_installed.append(bool(harness.subscribers))
        acquired = threading.Event()

        def acquire_from_other_thread() -> None:
            with tracker._lock:
                acquired.set()

        probe = threading.Thread(target=acquire_from_other_thread)
        probe.start()
        assert acquired.wait(timeout=1), "cancel was invoked while tracker lock was held"
        probe.join(timeout=1)
        lock_was_available.set()
        return harness.cancel(reason)

    base = harness.outcome()
    outcome = PendingCommandOutcome(
        base.operation_id,
        base.session_id,
        base.accepted,
        base.completion,
        cancel,
        base.subscribe_callback,
    )
    clock.now = 2

    assert tracker.bind_pending(run_id, outcome)

    running = tracker.get_status(run_id)
    assert running is not None
    assert running["status"] == "running"
    assert running["cancellation_requested"] == "timeout"
    assert running["finished_at"] is None
    assert lock_was_available.is_set()
    assert subscription_was_installed == [True]
    assert harness.cancel_calls == ["timeout"]

    harness.future.set_result(
        TerminalCommandOutcome(
            "operation-1",
            "session-1",
            "cancelled",
            {"ok": False, "error": {"message": "Cleanup settled."}},
        )
    )

    terminal = tracker.get_status(run_id)
    assert terminal is not None
    assert terminal["status"] == "timed_out"
    assert terminal["finished_at"] is not None
    assert harness.cancel_calls == ["timeout"]


def test_timeout_requests_cancel_outside_lock_and_stays_running_until_future_settles() -> None:
    clock = FakeClock()
    tracker = RunTracker(clock=clock)
    run_id, _status = tracker.create_run(
        command="/coach teach Test",
        source="codex",
        timeout_s=5,
    )
    harness = PendingHarness()
    lock_was_available = threading.Event()

    def cancel(reason):  # type: ignore[no-untyped-def]
        acquired = threading.Event()

        def acquire_from_other_thread() -> None:
            with tracker._lock:
                acquired.set()

        probe = threading.Thread(target=acquire_from_other_thread)
        probe.start()
        assert acquired.wait(timeout=1), "cancel was invoked while tracker lock was held"
        probe.join(timeout=1)
        lock_was_available.set()
        return harness.cancel(reason)

    outcome = harness.outcome()
    outcome = PendingCommandOutcome(
        outcome.operation_id,
        outcome.session_id,
        outcome.accepted,
        outcome.completion,
        cancel,
        outcome.subscribe_callback,
    )
    assert tracker.bind_pending(run_id, outcome)
    clock.now = 6

    status = tracker.get_status(run_id)

    assert lock_was_available.is_set()
    assert harness.cancel_calls == ["timeout"]
    assert status is not None
    assert status["status"] == "running"
    assert status["cancellation_requested"] == "timeout"
    assert status["finished_at"] is None
    logs = tracker.get_logs(run_id)
    assert logs is not None
    assert {event["kind"] for event in logs["events"]} >= {
        "cancellation_requested",
        "cancellation_dispatch",
    }

    harness.future.set_result(
        TerminalCommandOutcome(
            "operation-1",
            "session-1",
            "cancelled",
            {
                "ok": False,
                "error": {"code": "SESSION_CANCELLED", "message": "Cleanup settled."},
            },
        )
    )
    terminal = tracker.get_status(run_id)
    assert terminal is not None
    assert terminal["status"] == "timed_out"
    assert terminal["result"]["error"]["code"] == "SESSION_CANCELLED"
    assert terminal["finished_at"] is not None


def test_timeout_cancel_false_still_waits_for_authoritative_terminal() -> None:
    clock = FakeClock()
    tracker = RunTracker(clock=clock)
    run_id, _ = tracker.create_run(command="/coach replay", source="codex", timeout_s=1)
    harness = PendingHarness(cancel_result=False)
    tracker.bind_pending(run_id, harness.outcome())
    clock.now = 2

    assert tracker.get_status(run_id)["status"] == "running"  # type: ignore[index]
    assert harness.cancel_calls == ["timeout"]
    harness.future.set_result(
        TerminalCommandOutcome(
            "operation-1",
            "session-1",
            "completed",
            {"ok": True, "code": "COACH_REPLAY_COMPLETE"},
        )
    )

    assert tracker.get_status(run_id)["status"] == "completed"  # type: ignore[index]


def test_explicit_bound_mark_timeout_cancels_outside_lock_and_waits_for_future() -> None:
    tracker = RunTracker()
    run_id, _ = tracker.create_run(command="/coach replay", source="codex")
    harness = PendingHarness()
    lock_available: list[bool] = []

    def cancel(reason):  # type: ignore[no-untyped-def]
        acquired = threading.Event()

        def acquire_from_other_thread() -> None:
            with tracker._lock:
                acquired.set()

        probe = threading.Thread(target=acquire_from_other_thread)
        probe.start()
        lock_available.append(acquired.wait(timeout=1))
        probe.join(timeout=1)
        return harness.cancel(reason)

    outcome = harness.outcome()
    bound = PendingCommandOutcome(
        outcome.operation_id,
        outcome.session_id,
        outcome.accepted,
        outcome.completion,
        cancel,
        outcome.subscribe_callback,
    )
    assert tracker.bind_pending(run_id, bound)

    assert tracker.mark_timeout(run_id)
    assert not tracker.mark_timeout(run_id)
    running = tracker.get_status(run_id)
    assert lock_available == [True]
    assert harness.cancel_calls == ["timeout"]
    assert running is not None
    assert running["status"] == "running"
    assert running["cancellation_requested"] == "timeout"
    assert running["finished_at"] is None

    harness.future.set_result(
        TerminalCommandOutcome(
            "operation-1",
            "session-1",
            "cancelled",
            {"ok": False, "error": {"message": "Cleanup settled."}},
        )
    )
    assert tracker.get_status(run_id)["status"] == "timed_out"  # type: ignore[index]


def test_external_terminal_markers_cannot_override_bound_future() -> None:
    tracker = RunTracker()
    run_id, _ = tracker.create_run(command="/coach replay", source="codex")
    harness = PendingHarness()
    assert tracker.bind_pending(run_id, harness.outcome())

    assert not tracker.mark_completed(run_id, success=True, result={"forged": True})
    assert not tracker.mark_failed(
        run_id,
        error="forged failure",
        result={"forged": True},
    )
    assert not tracker.mark_cancelled(run_id, error="forged cancellation")
    running = tracker.get_status(run_id)
    assert running is not None
    assert running["status"] == "running"
    assert "result" not in running
    assert running["error"] is None

    harness.future.set_result(
        TerminalCommandOutcome(
            "operation-1",
            "session-1",
            "completed",
            {"ok": True, "code": "COACH_REPLAY_COMPLETE"},
        )
    )
    assert tracker.get_status(run_id)["status"] == "completed"  # type: ignore[index]


@pytest.mark.parametrize(
    ("operation_id", "session_id"),
    (("wrong-operation", "session-1"), ("operation-1", "wrong-session")),
)
def test_terminal_identity_must_match_operation_and_session(
    operation_id: str,
    session_id: str,
) -> None:
    tracker = RunTracker()
    run_id, _ = tracker.create_run(command="/coach replay", source="codex")
    harness = PendingHarness()
    probe = LockProbeUnsubscribe(tracker)
    outcome = pending_with_subscribe(harness, lambda _callback: probe)
    assert tracker.bind_pending(run_id, outcome)

    harness.future.set_result(
        TerminalCommandOutcome(
            operation_id,
            session_id,
            "completed",
            {"ok": True, "secret": "forged terminal"},
        )
    )

    status = tracker.get_status(run_id)
    assert status is not None
    assert status["status"] == "failed"
    assert status["result"]["error"]["code"] == "COMMAND_RESULT_INVALID"
    assert "forged terminal" not in repr(status)
    assert probe.calls == 1
    assert probe.called_outside_lock == [True]
    with tracker._lock:
        assert tracker._runs[run_id].progress_unsubscribe is None


def test_subscribe_exception_cannot_strand_authoritative_completion() -> None:
    tracker = RunTracker()
    run_id, _ = tracker.create_run(command="/coach replay", source="codex")
    harness = PendingHarness()

    def subscribe_failure(_callback):  # type: ignore[no-untyped-def]
        raise RuntimeError("SECRET subscription internals")

    assert tracker.bind_pending(run_id, pending_with_subscribe(harness, subscribe_failure))
    running = tracker.get_status(run_id)
    assert running is not None and running["status"] == "running"
    logs = tracker.get_logs(run_id)
    assert logs is not None
    assert logs["events"][-1]["kind"] == "progress_subscription_unavailable"
    assert "SECRET" not in repr(logs)

    harness.future.set_result(
        TerminalCommandOutcome(
            "operation-1",
            "session-1",
            "completed",
            {"ok": True},
        )
    )
    assert tracker.get_status(run_id)["status"] == "completed"  # type: ignore[index]


def test_valid_terminal_detaches_once_outside_lock_and_eviction_does_not_repeat() -> None:
    tracker = RunTracker(max_runs=1)
    run_id, _ = tracker.create_run(command="/coach replay", source="codex")
    harness = PendingHarness()
    probe = LockProbeUnsubscribe(tracker)
    assert tracker.bind_pending(run_id, pending_with_subscribe(harness, lambda _cb: probe))

    harness.future.set_result(
        TerminalCommandOutcome(
            "operation-1",
            "session-1",
            "completed",
            {"ok": True},
        )
    )
    assert probe.calls == 1
    assert probe.called_outside_lock == [True]
    with tracker._lock:
        assert tracker._runs[run_id].progress_unsubscribe is None

    replacement_id, _ = tracker.create_run(command="/panic", source="codex")
    assert tracker.get_status(run_id) is None
    assert tracker.get_status(replacement_id) is not None
    assert probe.calls == 1


def test_eviction_detaches_a_stale_subscriber_once_outside_lock() -> None:
    tracker = RunTracker(max_runs=1)
    terminal_id, _ = tracker.create_run(command="/panic", source="codex")
    assert tracker.mark_completed(terminal_id)
    probe = LockProbeUnsubscribe(tracker)
    with tracker._lock:
        tracker._runs[terminal_id].progress_unsubscribe = probe

    replacement_id, _ = tracker.create_run(command="/synth state", source="codex")

    assert tracker.get_status(terminal_id) is None
    assert tracker.get_status(replacement_id) is not None
    assert probe.calls == 1
    assert probe.called_outside_lock == [True]


def test_bind_completion_race_detaches_returned_subscriber_once_outside_lock() -> None:
    tracker = RunTracker()
    run_id, _ = tracker.create_run(command="/coach replay", source="codex")
    harness = PendingHarness()
    probe = LockProbeUnsubscribe(tracker)

    def subscribe_and_complete(_callback):  # type: ignore[no-untyped-def]
        harness.future.set_result(
            TerminalCommandOutcome(
                "operation-1",
                "session-1",
                "completed",
                {"ok": True},
            )
        )
        return probe

    assert tracker.bind_pending(
        run_id,
        pending_with_subscribe(harness, subscribe_and_complete),
    )
    assert tracker.get_status(run_id)["status"] == "completed"  # type: ignore[index]
    assert probe.calls == 1
    assert probe.called_outside_lock == [True]
    with tracker._lock:
        assert tracker._runs[run_id].progress_unsubscribe is None


def test_invalid_pending_future_fails_with_fixed_bounded_result() -> None:
    tracker = RunTracker()
    run_id, _ = tracker.create_run(command="/coach replay", source="codex")
    harness = PendingHarness()
    tracker.bind_pending(run_id, harness.outcome())

    harness.future.set_exception(RuntimeError("SECRET /private/path"))

    status = tracker.get_status(run_id)
    assert status is not None
    assert status["status"] == "failed"
    assert status["result"]["error"]["code"] == "COMMAND_RESULT_INVALID"
    assert "SECRET" not in repr(status)


def test_eviction_scans_past_active_oldest_run() -> None:
    tracker = RunTracker(max_runs=2)
    active_id, _ = tracker.create_run(command="/coach teach Test", source="codex")
    tracker.mark_running(active_id)
    terminal_id, _ = tracker.create_run(command="/panic", source="codex")
    tracker.mark_completed(terminal_id)
    newest_id, _ = tracker.create_run(command="/synth state", source="codex")

    assert tracker.get_status(active_id) is not None
    assert tracker.get_status(terminal_id) is None
    assert tracker.get_status(newest_id) is not None
    assert tracker.get_state()["runs_total"] == 2


def test_hard_capacity_never_exceeds_two_and_never_evicts_active_runs() -> None:
    tracker = RunTracker(max_runs=2)
    first_id, _ = tracker.create_run(command="/coach teach A", source="codex")
    second_id, _ = tracker.create_run(command="/coach teach B", source="codex")

    with pytest.raises(RuntimeError) as caught:
        tracker.create_run(command="/coach teach C", source="codex")

    assert str(caught.value) == "Run tracker capacity is full; all retained runs are active."
    assert len(str(caught.value)) < 100
    assert tracker.get_state()["runs_total"] == 2
    assert tracker.get_status(first_id) is not None
    assert tracker.get_status(second_id) is not None

    assert tracker.mark_completed(first_id)
    newest_id, newest = tracker.create_run(command="/coach teach C", source="codex")
    assert newest["run_id"] == newest_id
    assert tracker.get_status(first_id) is None
    assert tracker.get_status(second_id) is not None
    assert tracker.get_status(newest_id) is not None
    assert tracker.get_state()["runs_total"] == 2


@pytest.mark.parametrize(
    "timeout_s",
    (
        float("nan"),
        float("inf"),
        float("-inf"),
        0,
        -1,
        86_400.001,
        10**1_000,
        True,
        "5",
    ),
)
def test_timeout_must_be_finite_numeric_and_within_hard_range(timeout_s) -> None:  # type: ignore[no-untyped-def]
    tracker = RunTracker()

    with pytest.raises(
        ValueError,
        match="timeout_s must be a finite number from 0.001 through 86400 seconds",
    ):
        tracker.create_run(command="/panic", source="codex", timeout_s=timeout_s)

    assert tracker.get_state()["runs_total"] == 0


def test_command_source_and_timeout_boundaries_are_hard() -> None:
    tracker = RunTracker()
    _run_id, status = tracker.create_run(
        command=f"  /coach teach {'C' * 700}  ",
        source="CODEX" * 100,
        timeout_s=0.001,
    )

    assert len(status["command"]) == 500
    assert len(status["source"]) == 80
    assert status["source"] == status["source"].lower()
    assert status["timeout_s"] == 0.001
    _other_id, maximum = tracker.create_run(
        command="/panic",
        source="   ",
        timeout_s=86_400,
    )
    assert maximum["source"] == "llm"
    assert maximum["timeout_s"] == 86_400.0


def test_recursive_privacy_redacts_sensitive_shapes_and_preserves_coaching_metrics() -> None:
    class SecretObject:
        def __str__(self) -> str:
            return "DO-NOT-LEAK-OBJECT"

        def __repr__(self) -> str:
            return "DO-NOT-LEAK-OBJECT"

    marker = "DO-NOT-LEAK"
    private_value = {"marker": marker, "object": SecretObject()}
    filtered_aliases = (
        "raw_attempt",
        "events",
        "midi_events",
        "raw_message",
        "source_port",
        "lesson",
        "lesson_path",
        "system_prompt",
        "api_key",
        "access_token",
        "password_hash",
        "token",
        "credentials",
        "secrets",
        "paths",
        "event",
        "last_event",
        "midi_event",
        "recent_midi",
        "history",
        "command_history",
        "key",
        "private_key",
        "apiKey",
        "apiKeys",
        "accessToken",
        "privateKey",
        "sourcePort",
        "sourcePorts",
        "midiEvent",
        "midiEvents",
        "rawMessage",
        "recentMidi",
        "commandHistory",
        "rawEvent",
        "lastEvent",
    )
    internal_aliases = (
        "event_history",
        "midi_history",
        "midi_log",
        "midi_logs",
        "internal_receipt",
        "internal_receipts",
        "receipt",
        "receipts",
        "receipt_id",
        "receipt_ids",
        "command_id",
        "command_ids",
        "dispatch_id",
        "dispatch_ids",
        "admission",
        "admissions",
        "audio_admission",
        "audio_admissions",
        "queue",
        "queue_name",
        "playback_queue",
        "required_receipt_count",
        "consumer_error",
        "consumer_errors",
        "authoring_request",
        "authoring_proposal",
        "authoring_response",
        "authoring_output",
        "provider_request",
        "provider_proposal",
        "provider_response",
        "provider_output",
        "lesson_authoring_request",
        "coach_authoring_proposal",
        "openai_provider_response",
        "llm_provider_output",
    )
    camel_aliases = (
        "eventHistory",
        "midiHistory",
        "midiLog",
        "midiLogs",
        "internalReceipt",
        "internalReceipts",
        "rustReceipt",
        "rustReceipts",
        "receiptId",
        "receiptIds",
        "commandId",
        "commandIds",
        "dispatchId",
        "dispatchIds",
        "audioAdmission",
        "audioAdmissions",
        "queueName",
        "playbackQueue",
        "requiredReceiptCount",
        "consumerError",
        "consumerErrors",
        "authoringRequest",
        "authoringProposal",
        "authoringResponse",
        "authoringOutput",
        "providerRequest",
        "providerProposal",
        "providerResponse",
        "providerOutput",
        "lessonAuthoringRequest",
        "coachAuthoringProposal",
        "openaiProviderResponse",
        "llmProviderOutput",
    )
    hostile_aliases = (
        "event_log",
        "event_logs",
        "midi_note",
        "midi_notes",
        "midi_message",
        "midi_messages",
        "histories",
        "event_histories",
        "log",
        "logs",
        "client_credential",
        "client_credentials",
        "private_keys",
        "receipt_state",
        "receipt_states",
        "dispatch_result",
        "dispatch_results",
        "admission_state",
        "admission_states",
        "authoring_requests",
        "authoring_proposals",
        "authoring_responses",
        "authoring_outputs",
        "provider_requests",
        "provider_proposals",
        "provider_responses",
        "provider_outputs",
    )
    hostile_camel_aliases = (
        "eventLog",
        "eventLogs",
        "midiNote",
        "midiNotes",
        "midiMessage",
        "midiMessages",
        "eventHistory",
        "eventHistories",
        "clientCredential",
        "clientCredentials",
        "privateKeys",
        "receiptState",
        "receiptStates",
        "dispatchResult",
        "dispatchResults",
        "admissionState",
        "admissionStates",
        "authoringRequests",
        "authoringProposals",
        "authoringResponses",
        "authoringOutputs",
        "providerRequests",
        "providerProposals",
        "providerResponses",
        "providerOutputs",
    )
    diagnostic_aliases = (
        "receipt_count",
        "admission_count",
        "dispatch_count",
        "queue_count",
        "queue_depth",
        "log_count",
        "history_count",
        "midi_count",
        "midi_event_count",
        "consumer_error_count",
        "command_count",
        "provider_count",
        "authoring_count",
    )
    collapsed_aliases = (
        "eventlogs",
        "eventhistories",
        "midinotes",
        "midimessages",
        "midihistory",
        "midilogs",
        "clientcredentials",
        "privatekeys",
        "receiptstates",
        "receiptids",
        "requiredreceiptcount",
        "commandids",
        "dispatchids",
        "dispatchresults",
        "admissionstates",
        "queuename",
        "consumererrors",
        "authoringrequests",
        "provideroutputs",
        "apikeys",
        "accesstokens",
        "sourceports",
        "midievents",
        "rawmessages",
        "recentmidi",
    )
    payload = {
        "attempt_id": "attempt-7",
        "lesson_id": "lesson-7",
        "metrics": {
            "overall_score_percent": 91,
            "expected_note_count": 8,
            "observed_note_count": 7,
            "matched_note_count": 7,
            "eventLogs": private_value,
            "providerOutput": private_value,
        },
        "event_count": 8,
        "authored": True,
        "fidelity": {"timing": 0.97, "pitch": 1.0},
        "provenance": {"label": "curated-catalog", "providerOutput": private_value},
        "provenance_label": "curated-catalog",
        "key_signature": "C",
        "keys": ["C4", "E4"],
        "keyboard": {
            "keys": ["C4", "E4"],
            "key_signature": "C",
            "eventLogs": private_value,
        },
        "filtered_aliases": {key: private_value for key in filtered_aliases},
        "internal_aliases": {key: private_value for key in internal_aliases},
        "camel_aliases": {key: private_value for key in camel_aliases},
        "hostile_aliases": {key: private_value for key in hostile_aliases},
        "hostile_camel_aliases": {key: private_value for key in hostile_camel_aliases},
        "diagnostic_aliases": {key: private_value for key in diagnostic_aliases},
        "collapsed_aliases": {key: private_value for key in collapsed_aliases},
        "nested": [{"safe": "kept", "private_path": marker, "raw_notes": marker}],
        "payload": SecretObject(),
    }

    def assert_public_payload(public: dict) -> None:  # type: ignore[type-arg]
        assert public["attempt_id"] == "attempt-7"
        assert public["lesson_id"] == "lesson-7"
        assert public["metrics"] == {
            "overall_score_percent": 91,
            "expected_note_count": 8,
            "observed_note_count": 7,
            "matched_note_count": 7,
        }
        assert public["event_count"] == 8
        assert public["authored"] is True
        assert public["fidelity"] == {"timing": 0.97, "pitch": 1.0}
        assert public["provenance"] == {"label": "curated-catalog"}
        assert public["provenance_label"] == "curated-catalog"
        assert public["key_signature"] == "C"
        assert public["keys"] == ["C4", "E4"]
        assert public["keyboard"] == {
            "keys": ["C4", "E4"],
            "key_signature": "C",
        }
        assert public["filtered_aliases"] == {}
        assert public["internal_aliases"] == {}
        assert public["camel_aliases"] == {}
        assert public["hostile_aliases"] == {}
        assert public["hostile_camel_aliases"] == {}
        assert public["diagnostic_aliases"] == {}
        assert public["collapsed_aliases"] == {}
        assert public["nested"] == [{"safe": "kept"}]
        assert public["payload"] == "<redacted-object>"
        assert marker not in repr(public)
        assert "DO-NOT-LEAK-OBJECT" not in repr(public)
        assert "SecretObject" not in repr(public)

    tracker = RunTracker()
    run_id, _ = tracker.create_run(command="/coach replay", source="codex")
    harness = PendingHarness()
    base = harness.outcome()
    outcome = PendingCommandOutcome(
        base.operation_id,
        base.session_id,
        payload,
        base.completion,
        base.cancel_callback,
        base.subscribe_callback,
    )
    assert tracker.bind_pending(run_id, outcome)
    tracker.record_log_event(
        {
            "run_id": run_id,
            "level": "INFO",
            "message": "safe log",
            "nested": payload,
        }
    )

    running = tracker.get_status(run_id)
    logs = tracker.get_logs(run_id)
    assert running is not None and logs is not None
    accepted = running["accepted"]
    assert_public_payload(accepted)
    assert_public_payload(logs["events"][-1]["nested"])
    assert marker not in repr(running)
    assert "DO-NOT-LEAK-OBJECT" not in repr(running)
    assert marker not in repr(logs)
    assert "DO-NOT-LEAK-OBJECT" not in repr(logs)

    harness.future.set_result(
        TerminalCommandOutcome(
            "operation-1",
            "session-1",
            "completed",
            payload,
        )
    )
    terminal = tracker.get_status(run_id)
    assert terminal is not None
    result = terminal["result"]
    assert_public_payload(result)
    assert marker not in repr(terminal)
    assert "DO-NOT-LEAK-OBJECT" not in repr(terminal)


def test_canonical_coaching_report_preserves_citations_only_with_valid_values() -> None:
    marker = "HOSTILE-REPORT-MARKER"
    content_id = "a" * 64
    lesson_id = derive_lesson_id("Cited mismatch regression")
    revision_id = derive_revision_id(content_id)
    lesson_event_id = derive_lesson_event_id(lesson_id, revision_id, 0)
    report = CoachingReport(
        attempt_id="attempt-cited-1",
        session_id="session-cited-1",
        status="complete",
        lesson_id=lesson_id,
        revision_id=revision_id,
        content_id=content_id,
        segment_label="main",
        tempo_percent=100,
        metrics=ScoreMetrics(
            scoring_version="halcyon-score-v1",
            expected_note_count=1,
            observed_note_count=1,
            matched_note_count=0,
            pitch_accuracy_percent=0.0,
            missed_count=0,
            wrong_pitch_count=1,
            extra_count=0,
            onset_mean_deviation_ms=None,
            onset_p95_deviation_ms=None,
            held_duration_mean_deviation_ms=None,
            completion_percent=100.0,
            overall_score_percent=55.0,
            truncated=False,
            onset_tolerance_ms=45.0,
            duration_tolerance_ms=90.0,
            weights_percent=(
                ("pitch", 45),
                ("onset", 25),
                ("duration", 15),
                ("completion", 15),
            ),
        ),
        latency=LatencyEstimate(
            source="default",
            offset_us=0,
            jitter_us=0,
            tolerance_us=45_000,
            match_count=1,
        ),
        issues=(
            CoachingIssue(
                code="WRONG_PITCH",
                message="Play MIDI note 60 here, not 61.",
                expected_lesson_event_id=lesson_event_id,
                expected_tick=0,
                expected_note=60,
                observed_note=61,
                observed_offset_ms=0.0,
                deviation_ms=None,
            ),
        ),
        tips=("Repeat the cited note slowly.",),
        recommendation=PracticeRecommendation(
            action="repeat_segment",
            tempo_percent=90,
            segment_label="main",
            loops=2,
            focus="Correct the cited pitch.",
        ),
    )
    public_report = report.to_public_dict()

    def tracked_surfaces(payload: dict[str, object]) -> tuple[dict, dict, dict]:  # type: ignore[type-arg]
        tracker = RunTracker()
        run_id, _ = tracker.create_run(command="/coach feedback", source="codex")
        harness = PendingHarness()
        base = harness.outcome()
        outcome = PendingCommandOutcome(
            base.operation_id,
            base.session_id,
            payload,
            base.completion,
            base.cancel_callback,
            base.subscribe_callback,
        )
        assert tracker.bind_pending(run_id, outcome)
        tracker.record_log_event(
            {
                "run_id": run_id,
                "level": "INFO",
                "message": "bounded report",
                "nested": payload,
            }
        )
        running = tracker.get_status(run_id)
        logs = tracker.get_logs(run_id)
        assert running is not None and logs is not None
        accepted = running["accepted"]
        logged = logs["events"][-1]["nested"]
        harness.future.set_result(
            TerminalCommandOutcome(
                "operation-1",
                "session-1",
                "completed",
                payload,
            )
        )
        terminal = tracker.get_status(run_id)
        assert terminal is not None
        return accepted, logged, terminal["result"]

    for surface in tracked_surfaces({"report": public_report}):
        assert surface["report"] == public_report
        issue = surface["report"]["issues"][0]
        assert issue["expected_lesson_event_id"] == lesson_event_id
        assert issue["expected_note"] == 60
        assert issue["observed_note"] == 61

    invalid_report = deepcopy(public_report)
    first_issue = invalid_report["issues"][0]
    first_issue.update(
        {
            "expected_lesson_event_id": marker,
            "expected_note": True,
            "observed_note": 128,
            "expectedLessonEventId": lesson_event_id,
            "expected_notes": marker,
            "observedNotes": marker,
        }
    )
    second_issue = deepcopy(public_report["issues"][0])
    second_issue.update(
        {
            "expected_lesson_event_id": "e1-" + "g" * 64,
            "expected_note": 60.0,
            "observed_note": -1,
            "lesson_event_id": marker,
            "notes": marker,
        }
    )
    invalid_report["issues"].append(second_issue)
    hostile_payload = {
        "report": invalid_report,
        "lesson": {"lesson_id": marker},
        "lesson_object": {"title": marker},
        "lessonSummary": marker,
        "expected_lesson_event_id": marker,
        "expected_note": marker,
        "observed_note": marker,
        "expected_notes": marker,
    }
    for surface in tracked_surfaces(hostile_payload):
        assert marker not in repr(surface)
        assert set(surface) == {"report"}
        assert surface["report"]["lesson"] == public_report["lesson"]
        for issue in surface["report"]["issues"]:
            assert "expected_lesson_event_id" not in issue
            assert "expected_note" not in issue
            assert "observed_note" not in issue
            assert "expectedLessonEventId" not in issue
            assert "expected_notes" not in issue
            assert "observedNotes" not in issue
            assert "lesson_event_id" not in issue
            assert "notes" not in issue

    gross_invalid_reports = (
        marker,
        [marker, {"metrics": [marker]}],
        {
            "schema_version": 1,
            "attempt_id": "attempt-malformed",
            "session_id": "session-malformed",
            "status": "complete",
            "lesson": [{"lesson_id": marker}],
            "metrics": {"safe_data": [marker], "overall_score_percent": 99},
            "latency": {"data": [marker]},
            "issues": [{"code": "SAFE", "message": marker}],
            "tips": [marker],
            "recommendation": {"data": [marker]},
            "safe_data": [marker, {"nested": marker}],
            "arrays": [[marker]],
        },
    )
    for invalid_report_value in gross_invalid_reports:
        for surface in tracked_surfaces({"report": invalid_report_value, "safe": "kept"}):
            assert surface == {"safe": "kept"}
            assert marker not in repr(surface)


def test_event_and_result_bounds_are_hard_and_returned_payloads_are_copies() -> None:
    tracker = RunTracker(max_events_per_run=2)
    run_id, _ = tracker.create_run(command="/panic", source="codex")
    tracker.mark_running(run_id)
    for index in range(4):
        tracker.record_log_event(
            {"run_id": run_id, "level": "INFO", "message": f"{index}-" + "x" * 1_000}
        )

    logs = tracker.get_logs(run_id)
    assert logs is not None
    assert logs["truncated"] is True
    assert len(logs["events"]) == 2
    assert len(logs["events"][0]["message"]) == 500
    logs["events"][0]["message"] = "tampered"
    assert tracker.get_logs(run_id)["events"][0]["message"] != "tampered"  # type: ignore[index]


def test_explicit_mark_timeout_serializes_timed_out() -> None:
    tracker = RunTracker()
    run_id, _ = tracker.create_run(command="/panic", source="codex")

    assert tracker.mark_timeout(run_id)
    assert tracker.get_status(run_id)["status"] == "timed_out"  # type: ignore[index]
