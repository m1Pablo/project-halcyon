"""Unix-socket reader for Rust audio visualization sample frames."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import socket
import struct
import threading

VIZ_MAGIC = b"HZV1"
VIZ_HEADER = struct.Struct("<4sI")
DEFAULT_VIZ_SOCKET_PATH = "/tmp/synth_viz.sock"
DEFAULT_VIZ_SAMPLE_COUNT = 1024
MAX_VIZ_SAMPLE_COUNT = 16_384


@dataclass(frozen=True)
class AudioVisualizationSnapshot:
    """Thread-safe snapshot of the latest visualization sample window."""

    socket_path: str
    sample_count: int
    connected: bool
    frame_count: int
    samples: tuple[float, ...]


class AudioVisualizationReader:
    """Read raw f32 waveform frames from a Unix domain socket."""

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        sample_count: int | None = None,
    ) -> None:
        self.socket_path = socket_path or os.environ.get(
            "HALCYON_VIZ_SOCKET", DEFAULT_VIZ_SOCKET_PATH
        )
        self.sample_count = _configured_sample_count(sample_count)
        self._samples: tuple[float, ...] = tuple(0.0 for _ in range(self.sample_count))
        self._frame_count = 0
        self._connected = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._server: socket.socket | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="halcyon-audio-viz",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        server = self._server
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None
        with self._lock:
            self._connected = False
        _unlink_socket(self.socket_path)

    def snapshot(self) -> AudioVisualizationSnapshot:
        with self._lock:
            return AudioVisualizationSnapshot(
                socket_path=self.socket_path,
                sample_count=self.sample_count,
                connected=self._connected,
                frame_count=self._frame_count,
                samples=self._samples,
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._open_server() as server:
                    self._server = server
                    self._accept_loop(server)
            except OSError:
                self._stop.wait(0.2)
            finally:
                self._server = None
                with self._lock:
                    self._connected = False
                _unlink_socket(self.socket_path)

    def _open_server(self) -> socket.socket:
        _unlink_socket(self.socket_path)
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen(1)
        server.settimeout(0.2)
        return server

    def _accept_loop(self, server: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(0.2)
                with self._lock:
                    self._connected = True
                try:
                    self._read_frames(conn)
                except EOFError:
                    pass
                finally:
                    with self._lock:
                        self._connected = False

    def _read_frames(self, conn: socket.socket) -> None:
        while not self._stop.is_set():
            header = self._read_exact(conn, VIZ_HEADER.size)
            magic, sample_count = VIZ_HEADER.unpack(header)
            if magic != VIZ_MAGIC:
                raise EOFError("invalid visualization frame magic")
            if sample_count == 0 or sample_count > MAX_VIZ_SAMPLE_COUNT:
                raise EOFError("invalid visualization frame sample count")

            payload = self._read_exact(conn, sample_count * 4)
            samples = struct.unpack(f"<{sample_count}f", payload)
            with self._lock:
                self._samples = tuple(float(sample) for sample in samples)
                self._frame_count += 1

    def _read_exact(self, conn: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0 and not self._stop.is_set():
            try:
                chunk = conn.recv(remaining)
            except socket.timeout:
                continue
            if not chunk:
                raise EOFError("visualization socket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining:
            raise EOFError("visualization reader stopped")
        return b"".join(chunks)


def _configured_sample_count(sample_count: int | None) -> int:
    raw_value = sample_count
    if raw_value is None:
        raw_env = os.environ.get("HALCYON_VIZ_SAMPLES")
        raw_value = int(raw_env) if raw_env else DEFAULT_VIZ_SAMPLE_COUNT
    return max(1, min(int(raw_value), MAX_VIZ_SAMPLE_COUNT))


def _unlink_socket(socket_path: str) -> None:
    try:
        Path(socket_path).unlink()
    except FileNotFoundError:
        pass
