from halcyon.mcp_local.run_tracker import RunTracker


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
