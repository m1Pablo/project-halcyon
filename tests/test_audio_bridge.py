import json
from io import StringIO
from typing import Any

from halcyon.audio_bridge import RustAudioBridge, default_sidecar_command
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.midi import MidiEvent, audio_command_for_event


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


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = StringIO()
        self.stderr = StringIO()
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


def make_event(event_type: str, velocity: int = 96) -> MidiEvent:
    return MidiEvent(
        event_type=event_type,
        note=60,
        note_name="C4",
        velocity=velocity,
        channel=1,
        status=0x90,
        raw_message=(0x90, 60, velocity),
        delta_time=0.01,
        source="LPK25",
        elapsed=1.0,
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
    }
    assert audio_command_for_event(make_event("release", velocity=0)) == {
        "type": "note_off",
        "note": 60,
        "channel": 1,
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
        {"type": "note_on", "note": 60, "velocity": 96, "channel": 1},
        {"type": "note_off", "note": 60, "channel": 1},
    ]


def test_bridge_sends_all_notes_off_before_shutdown() -> None:
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
    bridge.close()

    writes = [json.loads(line) for line in created[0].stdin.lines]
    assert writes == [{"type": "all_notes_off"}, {"type": "shutdown"}]
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
        {"type": "set_param", "name": "cutoff_hz", "value": 2400.0},
        {"type": "set_param", "name": "drive", "value": 2.0},
    ]

    created[0].returncode = 0
    bridge.start()
    second_writes = [json.loads(line) for line in created[1].stdin.lines]
    assert second_writes == [
        {"type": "set_param", "name": "cutoff_hz", "value": 2400.0},
        {"type": "set_param", "name": "drive", "value": 2.0},
    ]


def test_bridge_starts_visualization_and_passes_socket_env() -> None:
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
    assert bridge.visualization_snapshot().sample_count == 128
