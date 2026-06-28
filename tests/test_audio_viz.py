import os
import socket
import struct
import time

from halcyon.audio_viz import AudioVisualizationReader, VIZ_HEADER, VIZ_MAGIC


def test_audio_visualization_reader_parses_raw_f32_frame() -> None:
    socket_path = f"/tmp/halcyon-viz-test-{os.getpid()}-{time.monotonic_ns()}.sock"
    reader = AudioVisualizationReader(socket_path=socket_path, sample_count=4)

    try:
        reader.start()
        _wait_for(lambda: os.path.exists(socket_path))

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            samples = (0.0, 0.25, -0.5, 1.0)
            payload = VIZ_HEADER.pack(VIZ_MAGIC, len(samples))
            payload += struct.pack("<4f", *samples)
            client.sendall(payload)
            _wait_for(lambda: reader.snapshot().frame_count == 1)
            snapshot = reader.snapshot()

        assert snapshot.connected
        assert snapshot.sample_count == 4
        assert snapshot.samples == samples
    finally:
        reader.close()


def _wait_for(predicate, *, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")
