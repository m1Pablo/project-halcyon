import json
import threading
import time
from io import StringIO
from typing import Any

from halcyon.audio_bridge import (
    MAX_RETAINED_ADMISSIONS,
    CleanupSubmission,
    RustAudioBridge,
    default_sidecar_command,
)
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.midi import MidiEvent, MidiOrigin, audio_command_for_event


class FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, value: str) -> int:
        self.lines.append(value)
        return len(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class ConcurrentWriteDetectingStdin(FakeStdin):
    def __init__(self) -> None:
        super().__init__()
        self._writing = False
        self.overlapped = False

    def write(self, value: str) -> int:
        if self._writing:
            self.overlapped = True
        self._writing = True
        time.sleep(0.001)
        try:
            return super().write(value)
        finally:
            self._writing = False


class ImmediateAckStdin(FakeStdin):
    def __init__(self) -> None:
        super().__init__()
        self.acknowledge: Any = None

    def flush(self) -> None:
        payload = json.loads(self.lines[-1])
        self.acknowledge(payload["command_id"])


class BlockingStdin(FakeStdin):
    def __init__(self) -> None:
        super().__init__()
        self.write_entered = threading.Event()
        self.release_write = threading.Event()
        self._block_once = True

    def write(self, value: str) -> int:
        if self._block_once:
            self._block_once = False
            self.write_entered.set()
            self.release_write.wait(timeout=1.0)
        return super().write(value)


class ClosedStdin(FakeStdin):
    def write(self, value: str) -> int:
        raise ValueError("I/O operation on closed file")


class FailSecondWriteStdin(FakeStdin):
    def write(self, value: str) -> int:
        if self.lines:
            raise ValueError("I/O operation on closed file")
        return super().write(value)


class FakeProcess:
    def __init__(self, stdin: FakeStdin | None = None) -> None:
        self.stdin = stdin or FakeStdin()
        self.stdout = None
        self.stderr = None
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class BlockingWaitProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.wait_entered = threading.Event()
        self.release_wait = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_entered.set()
        self.release_wait.wait(timeout=1.0)
        self.returncode = 0
        return self.returncode


class FakeVisualization:
    socket_path = "/tmp/fake-halcyon-viz.sock"
    sample_count = 128

    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    def start(self) -> None:
        self.started += 1

    def close(self) -> None:
        self.closed += 1

    def snapshot(self) -> AudioVisualizationSnapshot:
        return AudioVisualizationSnapshot(
            socket_path=self.socket_path,
            sample_count=self.sample_count,
            connected=False,
            frame_count=0,
            samples=(),
        )


def make_event(
    event_type: str,
    velocity: int = 96,
    *,
    voice_id: int = 11,
    origin: MidiOrigin = MidiOrigin.HUMAN,
    source: str = "LPK25",
    session_id: str = "app",
) -> MidiEvent:
    return MidiEvent(
        event_type=event_type,
        note=60,
        note_name="C4",
        velocity=velocity,
        channel=1,
        status=0x90,
        raw_message=(0x90, 60, velocity),
        delta_time=0.01,
        source=source,
        elapsed=1.0,
        voice_id=voice_id,
        origin=origin,
        session_id=session_id,
    )


def acknowledge(
    bridge: RustAudioBridge,
    command_id: int,
    *,
    status: str = "accepted",
    queue: str = "normal",
    code: str | None = None,
    message: str | None = None,
) -> None:
    bridge._handle_stdout_line(
        json.dumps(
            {
                "type": "admission",
                "command_id": command_id,
                "status": status,
                "queue": queue,
                "code": code,
                "message": message,
            }
        )
    )


def test_default_sidecar_command_runs_cargo_through_uv() -> None:
    command = default_sidecar_command()

    assert command[:4] == ["uv", "run", "--", "cargo"]
    assert "--manifest-path" in command
    assert "halcyon-audio-sidecar" in command


def test_audio_command_for_event_maps_press_and_release() -> None:
    assert audio_command_for_event(make_event("press")) == {
        "type": "note_on",
        "note": 60,
        "velocity": 96,
        "channel": 1,
        "voice_id": 11,
    }
    assert audio_command_for_event(make_event("release", velocity=0)) == {
        "type": "note_off",
        "note": 60,
        "channel": 1,
        "voice_id": 11,
    }


def test_bridge_serializes_midi_events_to_sidecar_protocol() -> None:
    created: list[FakeProcess] = []

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        return process

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    assert bridge.start()

    assert bridge.handle_midi_event(make_event("press"))
    assert bridge.handle_midi_event(make_event("release", velocity=0))

    writes = [json.loads(line) for line in created[0].stdin.lines]
    assert writes == [
        {
            "type": "note_on",
            "note": 60,
            "velocity": 96,
            "channel": 1,
            "voice_id": 11,
            "command_id": 1,
        },
        {"type": "note_off", "note": 60, "channel": 1, "voice_id": 11, "command_id": 2},
    ]


def test_bridge_close_uses_owner_cleanup_then_safety_shutdown() -> None:
    created: list[FakeProcess] = []

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        return process

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    bridge.handle_midi_event(
        make_event(
            "press",
            voice_id=21,
            origin=MidiOrigin.HALCYON,
            source="halcyon://coach/lesson",
            session_id="coach-1",
        )
    )
    bridge.handle_midi_event(make_event("press", voice_id=22))
    bridge.close()

    writes = [json.loads(line) for line in created[0].stdin.lines]
    assert writes[-2:] == [
        {"type": "note_off", "note": 60, "channel": 1, "voice_id": 21, "command_id": 3},
        {"type": "shutdown", "command_id": 4},
    ]
    assert all(command["type"] != "all_notes_off" for command in writes)
    assert created[0].stdin.closed


def test_bridge_applies_and_replays_patch_on_restart() -> None:
    created: list[FakeProcess] = []

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        return process

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.apply_patch({"cutoff_hz": 2400.0, "drive": 2.0})

    bridge.start()
    first_writes = [json.loads(line) for line in created[0].stdin.lines]
    assert first_writes == [
        {"type": "set_param", "name": "cutoff_hz", "value": 2400.0, "command_id": 1},
        {"type": "set_param", "name": "drive", "value": 2.0, "command_id": 2},
    ]

    created[0].returncode = 0
    bridge.start()
    second_writes = [json.loads(line) for line in created[1].stdin.lines]
    assert second_writes == [
        {"type": "set_param", "name": "cutoff_hz", "value": 2400.0, "command_id": 3},
        {"type": "set_param", "name": "drive", "value": 2.0, "command_id": 4},
    ]


def test_bridge_starts_visualization_and_passes_only_required_safe_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-must-not-reach-rust")
    monkeypatch.setenv("HALCYON_SAFE_TEST_VALUE", "preserved")
    created: list[FakeProcess] = []
    captured_env: list[dict[str, str]] = []
    visualization = FakeVisualization()

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        captured_env.append(kwargs["env"])
        return process

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=visualization,  # type: ignore[arg-type]
    )

    assert bridge.start()

    assert visualization.started == 1
    assert captured_env[0]["HALCYON_VIZ_SOCKET"] == "/tmp/fake-halcyon-viz.sock"
    assert captured_env[0]["HALCYON_VIZ_SAMPLES"] == "128"
    assert captured_env[0]["HALCYON_SAFE_TEST_VALUE"] == "preserved"
    assert "OPENAI_API_KEY" not in captured_env[0]
    assert bridge.visualization_snapshot().sample_count == 128


def test_bridge_serializes_concurrent_writes_and_assigns_unique_command_ids() -> None:
    stdin = ConcurrentWriteDetectingStdin()

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        return FakeProcess(stdin)

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    threads = [
        threading.Thread(target=bridge.note_on, args=(60 + index, 90, 1)) for index in range(8)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    writes = [json.loads(line) for line in stdin.lines]
    assert not stdin.overlapped
    assert len(writes) == 8
    assert sorted(command["command_id"] for command in writes) == list(range(1, 9))
    assert bridge.snapshot().pending_commands == 8


def test_structured_admission_updates_pending_and_surfaces_rejection() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    assert bridge.note_on(60, 100, 1, voice_id=55)
    assert bridge.snapshot().pending_commands == 1

    bridge._handle_stdout_line(
        json.dumps(
            {
                "type": "admission",
                "command_id": 1,
                "status": "rejected",
                "queue": "normal",
                "code": "AUDIO_BACKPRESSURE",
                "message": "normal command queue full",
            }
        )
    )

    admission = bridge.admission(1)
    snapshot = bridge.snapshot()
    assert admission is not None and admission.code == "AUDIO_BACKPRESSURE"
    assert snapshot.pending_commands == 0
    assert snapshot.fatal
    assert not snapshot.ready
    assert "normal command queue full" in snapshot.text


def test_immediate_admission_cannot_race_a_stale_pending_record() -> None:
    stdin = ImmediateAckStdin()

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        return FakeProcess(stdin)

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    stdin.acknowledge = lambda command_id: bridge._handle_stdout_line(
        json.dumps(
            {
                "type": "admission",
                "command_id": command_id,
                "status": "accepted",
                "queue": "normal",
            }
        )
    )
    bridge.start()

    submission = bridge.handle_midi_event_submission(make_event("press", voice_id=88))
    assert submission.submitted and submission.status == "accepted"
    assert bridge.snapshot().pending_commands == 0
    assert bridge.admission(1) is not None
    assert bridge.active_voice_ids() == (88,)


def test_safety_pending_reserve_is_bounded_and_exhaustion_is_visible() -> None:
    created: list[FakeProcess] = []

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        return process

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    for voice_id in range(1, 65):
        assert bridge.note_off(60, 1, voice_id=voice_id)

    assert not bridge.note_off(60, 1, voice_id=65)
    snapshot = bridge.snapshot()
    assert snapshot.pending_commands == 64
    assert snapshot.fatal and not snapshot.ready
    assert "safety commands" in snapshot.text
    assert len(created[0].stdin.lines) == 64


def test_velocity_zero_note_on_uses_bridge_safety_reserve() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()

    assert bridge.send({"type": "note_on", "note": 60, "velocity": 0, "channel": 1})
    assert bridge.snapshot().pending_commands == 1


def test_owner_release_sends_only_halcyon_voice_and_panic_remains_global() -> None:
    created: list[FakeProcess] = []

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        return process

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    human = make_event("press", voice_id=70)
    halcyon = make_event(
        "press",
        voice_id=71,
        origin=MidiOrigin.HALCYON,
        source="halcyon://coach/lesson",
        session_id="coach-7",
    )
    human_submission = bridge.handle_midi_event_submission(human)
    halcyon_submission = bridge.handle_midi_event_submission(halcyon)
    acknowledge(bridge, human_submission.command_id or 0)
    acknowledge(bridge, halcyon_submission.command_id or 0)
    assert bridge.active_voice_ids() == (70, 71)

    cleanup = bridge.release_halcyon_voices(session_id="coach-7")
    assert cleanup.command_ids == (3,)
    assert bridge.cleanup_status(cleanup).status == "pending"
    assert bridge.active_voice_ids() == (70, 71)
    acknowledge(bridge, 3, queue="safety")
    assert bridge.cleanup_status(cleanup).status == "accepted"
    assert bridge.active_voice_ids() == (70,)

    panic = bridge.all_notes_off_submission()
    assert panic.submitted and panic.status == "pending"
    assert bridge.active_voice_ids() == (70,)
    acknowledge(bridge, panic.command_id or 0, queue="safety")
    assert bridge.active_voice_ids() == ()

    writes = [json.loads(line) for line in created[0].stdin.lines]
    assert writes[-2] == {
        "type": "note_off",
        "note": 60,
        "channel": 1,
        "voice_id": 71,
        "command_id": 3,
    }
    assert writes[-1] == {"type": "all_notes_off", "command_id": 4}


def test_rapid_cancel_cleans_pending_note_on_in_fifo_admission_order() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    event = make_event(
        "note_on",
        voice_id=81,
        origin=MidiOrigin.HALCYON,
        source="halcyon://coach/rapid",
        session_id="coach-rapid",
    )

    note_on = bridge.handle_midi_event_submission(event)
    cleanup = bridge.release_halcyon_voices(session_id="coach-rapid")

    assert note_on.command_id == 1 and note_on.status == "pending"
    assert cleanup.command_ids == (2,)
    assert bridge.active_voice_ids() == ()
    assert bridge.pending_voice_ids(action="add") == (81,)
    assert bridge.pending_voice_ids(action="remove") == (81,)

    acknowledge(bridge, 1)
    assert bridge.active_voice_ids() == (81,)
    acknowledge(bridge, 2, queue="safety")
    assert bridge.active_voice_ids() == ()
    assert bridge.pending_voice_ids() == ()
    assert bridge.cleanup_status(cleanup).status == "accepted"


def test_rejected_cleanup_retains_voice_and_fatal_blocks_normal_but_allows_safety() -> None:
    created: list[FakeProcess] = []

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        process = FakeProcess()
        created.append(process)
        return process

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    event = make_event(
        "press",
        voice_id=91,
        origin=MidiOrigin.HALCYON,
        source="halcyon://coach/retry",
        session_id="coach-retry",
    )
    note_on = bridge.handle_midi_event_submission(event)
    acknowledge(bridge, note_on.command_id or 0)

    first_cleanup = bridge.release_halcyon_voices(session_id="coach-retry")
    acknowledge(
        bridge,
        first_cleanup.command_ids[0],
        status="rejected",
        queue="safety",
        code="AUDIO_SAFETY_ADMISSION_TIMEOUT",
        message="stalled",
    )

    assert bridge.active_voice_ids() == (91,)
    assert bridge.cleanup_status(first_cleanup).status == "rejected"
    blocked = bridge.send_submission({"type": "note_on", "note": 62, "velocity": 100, "channel": 1})
    assert not blocked.submitted and blocked.code == "AUDIO_FATAL"

    retry = bridge.release_halcyon_voices(session_id="coach-retry")
    assert retry.command_ids == (3,)
    assert json.loads(created[0].stdin.lines[-1])["type"] == "note_off"
    acknowledge(bridge, 3, queue="safety")
    assert bridge.active_voice_ids() == ()


def test_sidecar_error_rejects_pending_mutation_without_forgetting_voice() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    event = make_event("press", voice_id=101)
    submission = bridge.handle_midi_event_submission(event)

    bridge._handle_stdout_line(
        json.dumps(
            {
                "type": "error",
                "code": "INVALID_AUDIO_COMMAND",
                "message": "invalid command",
            }
        )
    )

    admission = bridge.admission(submission.command_id or 0)
    assert admission is not None and admission.status == "rejected"
    assert bridge.active_voice_ids() == ()
    assert bridge.pending_voice_ids() == ()
    assert bridge.snapshot().fatal


def test_terminal_safety_rejection_sweeps_all_other_pending_mutations() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    first = bridge.handle_midi_event_submission(make_event("press", voice_id=111))
    second = bridge.handle_midi_event_submission(make_event("press", voice_id=112))
    release = bridge.handle_midi_event_submission(make_event("release", velocity=0, voice_id=111))

    acknowledge(
        bridge,
        release.command_id or 0,
        status="rejected",
        queue="safety",
        code="AUDIO_SAFETY_ADMISSION_TIMEOUT",
        message="stalled callback",
    )

    assert bridge.snapshot().pending_commands == 0
    assert bridge.pending_voice_ids() == ()
    assert bridge.active_voice_ids() == ()
    assert bridge.admission(first.command_id or 0).status == "rejected"  # type: ignore[union-attr]
    assert bridge.admission(second.command_id or 0).status == "rejected"  # type: ignore[union-attr]


def test_invalid_command_terminal_rejection_sweeps_remaining_pending_ids() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    invalid = bridge.send_submission(
        {"type": "note_on", "note": 999, "velocity": 100, "channel": 1}
    )
    other = bridge.handle_midi_event_submission(make_event("press", voice_id=121))

    acknowledge(
        bridge,
        invalid.command_id or 0,
        status="rejected",
        code="INVALID_AUDIO_COMMAND",
        message="note out of range",
    )

    assert bridge.snapshot().pending_commands == 0
    assert bridge.pending_voice_ids() == ()
    other_admission = bridge.admission(other.command_id or 0)
    assert other_admission is not None and other_admission.status == "rejected"


def test_normal_backpressure_does_not_sweep_unrelated_pending_command() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    first = bridge.handle_midi_event_submission(make_event("press", voice_id=131))
    second = bridge.handle_midi_event_submission(make_event("press", voice_id=132))

    acknowledge(
        bridge,
        first.command_id or 0,
        status="rejected",
        code="AUDIO_BACKPRESSURE",
        message="normal queue boundary",
    )

    assert bridge.snapshot().pending_commands == 1
    assert bridge.admission(second.command_id or 0) is None
    assert bridge.pending_voice_ids() == (132,)


def test_closed_stream_value_error_is_structured_fatal_rejection() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(ClosedStdin()),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()

    submission = bridge.send_submission(
        {"type": "note_on", "note": 60, "velocity": 100, "channel": 1}
    )

    assert not submission.submitted
    assert submission.command_id == 1 and submission.code == "AUDIO_WRITE_FAILED"
    assert bridge.admission(1) is not None
    assert bridge.snapshot().fatal


def test_write_failure_sweeps_earlier_pending_voice_mutation() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(FailSecondWriteStdin()),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    first = bridge.handle_midi_event_submission(make_event("press", voice_id=141))

    failed = bridge.send_submission({"type": "note_on", "note": 62, "velocity": 100, "channel": 1})

    assert not failed.submitted and failed.code == "AUDIO_WRITE_FAILED"
    assert bridge.snapshot().pending_commands == 0
    assert bridge.pending_voice_ids() == ()
    first_admission = bridge.admission(first.command_id or 0)
    assert first_admission is not None and first_admission.status == "rejected"


def test_close_waits_for_inflight_serialized_write_before_closing_stdin() -> None:
    stdin = BlockingStdin()
    process = FakeProcess(stdin)
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: process,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    sent: list[bool] = []
    send_thread = threading.Thread(
        target=lambda: sent.append(
            bridge.send({"type": "note_on", "note": 60, "velocity": 100, "channel": 1})
        )
    )
    send_thread.start()
    assert stdin.write_entered.wait(timeout=1.0)

    close_thread = threading.Thread(target=bridge.close)
    close_thread.start()
    time.sleep(0.01)
    assert not stdin.closed
    assert close_thread.is_alive()

    stdin.release_write.set()
    send_thread.join(timeout=1.0)
    close_thread.join(timeout=1.0)

    assert sent == [True]
    assert stdin.closed
    assert not close_thread.is_alive()


def test_restart_waits_for_inflight_write_before_swapping_process() -> None:
    first_stdin = BlockingStdin()
    created = [FakeProcess(first_stdin), FakeProcess()]
    factory_calls = 0

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        nonlocal factory_calls
        process = created[factory_calls]
        factory_calls += 1
        return process

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    submissions: list[Any] = []
    send_thread = threading.Thread(
        target=lambda: submissions.append(
            bridge.send_submission({"type": "note_on", "note": 60, "velocity": 100, "channel": 1})
        )
    )
    send_thread.start()
    assert first_stdin.write_entered.wait(timeout=1.0)
    created[0].returncode = 0

    restart_thread = threading.Thread(target=bridge.start)
    restart_thread.start()
    time.sleep(0.01)
    assert factory_calls == 1

    first_stdin.release_write.set()
    send_thread.join(timeout=1.0)
    restart_thread.join(timeout=1.0)

    assert factory_calls == 2
    assert bridge.process is created[1]
    assert submissions and submissions[0].command_id == 1
    old_admission = bridge.admission(1)
    assert old_admission is not None and old_admission.code == "AUDIO_RESTARTED"


def test_stale_reader_cannot_mutate_replacement_generation_after_dispatch_race() -> None:
    created = [FakeProcess(), FakeProcess()]
    factory_calls = 0

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        nonlocal factory_calls
        process = created[factory_calls]
        factory_calls += 1
        return process

    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=process_factory,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    old_generation = bridge._generation
    handler_entered = threading.Event()
    release_handler = threading.Event()
    original_handler = bridge._handle_stdout_line

    def delayed_handler(line: str, generation: int | None = None) -> None:
        handler_entered.set()
        release_handler.wait(timeout=1.0)
        original_handler(line, generation)

    bridge._handle_stdout_line = delayed_handler  # type: ignore[method-assign]
    old_error = json.dumps(
        {
            "type": "error",
            "code": "OLD_GENERATION_FATAL",
            "message": "stale sidecar failed",
        }
    )
    old_reader = threading.Thread(
        target=bridge._read_stream,
        args=(StringIO(f"{old_error}\n"), "stdout", old_generation),
    )
    old_reader.start()
    assert handler_entered.wait(timeout=1.0)

    created[0].returncode = 1
    assert bridge.start()
    new_generation = bridge._generation
    bridge._handle_stdout_line(
        json.dumps({"type": "status", "status": "audio_ready"}),
        new_generation,
    )
    pending = bridge.handle_midi_event_submission(make_event("press", voice_id=151))
    assert pending.command_id is not None and pending.status == "pending"

    release_handler.set()
    old_reader.join(timeout=1.0)
    bridge._handle_stdout_line = original_handler  # type: ignore[method-assign]
    bridge._handle_stdout_line(
        json.dumps(
            {
                "type": "admission",
                "command_id": pending.command_id,
                "status": "accepted",
                "queue": "normal",
            }
        ),
        old_generation,
    )
    bridge._handle_stdout_line("old malformed stdout", old_generation)
    bridge._handle_stdout_line(
        json.dumps({"type": "status", "status": "old_stopped"}),
        old_generation,
    )
    bridge._handle_stderr_line("old stderr failure", old_generation)

    snapshot = bridge.snapshot()
    assert not old_reader.is_alive()
    assert snapshot.ready and not snapshot.fatal
    assert snapshot.pending_commands == 1
    assert bridge.admission(pending.command_id) is None
    assert bridge.active_voice_ids() == ()
    assert bridge._last_error is None
    assert snapshot.text == "Audio: audio_ready"


def test_current_generation_stdout_eof_terminally_resolves_pending_cleanup() -> None:
    process = FakeProcess()
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: process,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    note_on = bridge.handle_midi_event_submission(
        make_event(
            "press",
            voice_id=161,
            origin=MidiOrigin.HALCYON,
            source="halcyon://coach/eof",
            session_id="coach-eof",
        )
    )
    cleanup = bridge.release_halcyon_voices(session_id="coach-eof")
    assert bridge.cleanup_status(cleanup).status == "pending"

    bridge._read_stream(StringIO(), "stdout", bridge._generation)
    post_eof_safety = bridge.send_submission(
        {"type": "note_off", "note": 60, "channel": 1, "voice_id": 161}
    )

    snapshot = bridge.snapshot()
    note_on_admission = bridge.admission(note_on.command_id or 0)
    cleanup_admission = bridge.admission(cleanup.command_ids[0])
    assert snapshot.fatal and not snapshot.running and not snapshot.ready
    assert snapshot.pending_commands == 0
    assert bridge.pending_voice_ids() == ()
    assert note_on_admission is not None and note_on_admission.code == "AUDIO_SIDECAR_EOF"
    assert cleanup_admission is not None and cleanup_admission.code == "AUDIO_SIDECAR_EOF"
    assert bridge.cleanup_status(cleanup).status == "rejected"
    assert not post_eof_safety.submitted and post_eof_safety.code == "AUDIO_SIDECAR_EOF"
    assert "stdout closed unexpectedly" in snapshot.text


def test_intentional_close_invalidates_reader_before_stdout_eof() -> None:
    process = BlockingWaitProcess()
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: process,
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    closing_generation = bridge._generation

    close_thread = threading.Thread(target=bridge.close)
    close_thread.start()
    assert process.wait_entered.wait(timeout=1.0)
    bridge._read_stream(StringIO(), "stdout", closing_generation)
    process.release_wait.set()
    close_thread.join(timeout=1.0)

    snapshot = bridge.snapshot()
    assert not close_thread.is_alive()
    assert not snapshot.fatal and not snapshot.ready
    assert snapshot.text == "Audio: stopped"


def test_terminal_sweep_retains_more_than_legacy_history_window() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    submissions = tuple(
        bridge.handle_midi_event_submission(make_event("press", voice_id=1_000 + index))
        for index in range(300)
    )
    edge_status = CleanupSubmission(commands=(submissions[0], submissions[-1]))
    assert bridge.cleanup_status(edge_status).status == "pending"

    bridge._handle_stdout_line(
        json.dumps(
            {
                "type": "error",
                "code": "AUDIO_SIDECAR_ERROR",
                "message": "terminal sweep",
            }
        )
    )

    assert bridge.snapshot().pending_commands == 0
    assert bridge.cleanup_status(edge_status).status == "rejected"
    first = bridge.admission(submissions[0].command_id or 0)
    last = bridge.admission(submissions[-1].command_id or 0)
    assert first is not None and first.code == "AUDIO_SIDECAR_ERROR"
    assert last is not None and last.code == "AUDIO_SIDECAR_ERROR"
    assert len(bridge._admissions) == 300
    assert len(bridge._admissions) <= MAX_RETAINED_ADMISSIONS


def test_admission_history_remains_bounded_to_full_pending_capacity() -> None:
    bridge = RustAudioBridge(
        command=["sidecar"],
        process_factory=lambda command, **kwargs: FakeProcess(),
        visualization=FakeVisualization(),  # type: ignore[arg-type]
    )
    bridge.start()
    last_command_id = 0
    for index in range(MAX_RETAINED_ADMISSIONS + 1):
        submission = bridge.send_submission(
            {"type": "set_param", "name": "drive", "value": float(index)}
        )
        last_command_id = submission.command_id or 0
        acknowledge(bridge, last_command_id)

    assert len(bridge._admissions) == MAX_RETAINED_ADMISSIONS
    assert bridge.admission(1) is None
    latest = bridge.admission(last_command_id)
    assert latest is not None and latest.status == "accepted"
