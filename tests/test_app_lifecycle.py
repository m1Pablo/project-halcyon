import asyncio
import os
from pathlib import Path
import time

from halcyon.app import HalcyonApp
from halcyon.audio_bridge import AudioBridgeSnapshot
from halcyon.audio_viz import AudioVisualizationReader, AudioVisualizationSnapshot


class TrackingServer:
    def __init__(self, *, fail_on_stop: bool = False) -> None:
        self.stop_calls = 0
        self.fail_on_stop = fail_on_stop

    def start(self) -> tuple[str, int]:
        return "127.0.0.1", 8765

    def stop(self) -> None:
        self.stop_calls += 1
        if self.fail_on_stop:
            raise RuntimeError("server stop failed")


class TrackingAudio:
    def __init__(self, socket_path: str | None = None) -> None:
        self.close_calls = 0
        self.visualization = AudioVisualizationReader(socket_path=socket_path)

    def start(self) -> bool:
        self.visualization.start()
        return True

    def apply_patch(self, patch: dict[str, float]) -> bool:
        return True

    def snapshot(self) -> AudioBridgeSnapshot:
        return AudioBridgeSnapshot(running=True, ready=True, text="Audio: ready")

    def visualization_snapshot(self) -> AudioVisualizationSnapshot:
        return self.visualization.snapshot()

    def all_notes_off(self) -> bool:
        return True

    def close(self) -> None:
        self.close_calls += 1
        self.visualization.close()


class TrackingMidi:
    connected = False

    def __init__(self) -> None:
        self.close_calls = 0

    def list_ports(self) -> list[str]:
        return []

    def connect(self) -> str:
        return "Test MIDI"

    def poll(self) -> list[object]:
        return []

    def close(self) -> None:
        self.close_calls += 1


async def wait_for_path(path: Path, *, exists: bool) -> None:
    for _ in range(100):
        if path.exists() is exists:
            return
        await asyncio.sleep(0.01)
    assert path.exists() is exists


def test_ctrl_q_closes_resources_and_visualization_socket_exactly_once() -> None:
    async def run() -> None:
        socket_path = Path(f"/tmp/halcyon-lifecycle-{os.getpid()}-{time.monotonic_ns()}.sock")
        app = HalcyonApp()
        server = TrackingServer()
        audio = TrackingAudio(str(socket_path))
        midi = TrackingMidi()
        app.local_control_server = server  # type: ignore[assignment]
        app.audio = audio  # type: ignore[assignment]
        app.midi = midi  # type: ignore[assignment]

        try:
            async with app.run_test() as pilot:
                await wait_for_path(socket_path, exists=True)
                await pilot.press("ctrl+q")

            assert server.stop_calls == 1
            assert audio.close_calls == 1
            assert midi.close_calls == 1
            assert not socket_path.exists()

            app.on_unmount()
            assert server.stop_calls == 1
            assert audio.close_calls == 1
            assert midi.close_calls == 1
            assert not socket_path.exists()
        finally:
            audio.visualization.close()

    asyncio.run(run())


def test_cleanup_attempts_every_resource_after_failure_and_is_idempotent() -> None:
    app = HalcyonApp()
    server = TrackingServer(fail_on_stop=True)
    audio = TrackingAudio()
    midi = TrackingMidi()
    app.local_control_server = server  # type: ignore[assignment]
    app.audio = audio  # type: ignore[assignment]
    app.midi = midi  # type: ignore[assignment]

    app.on_unmount()
    app.on_unmount()

    assert server.stop_calls == 1
    assert audio.close_calls == 1
    assert midi.close_calls == 1
