"""Tiny localhost HTTP control server for safe Halcyon synth tools."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
import weakref

from halcyon.mcp_tools import ToolExecutionError, execute_tool, tool_definitions

if TYPE_CHECKING:
    from halcyon.app import HalcyonApp


def _resolve_port(preferred: int, host: str) -> int:
    for candidate in [preferred, *(preferred + step for step in range(1, 25))]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


class _ControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        control: "LocalControlServer",
    ) -> None:
        super().__init__(server_address, request_handler_class)
        self.control = control


class LocalControlServer:
    """Localhost control server exposing the safe synth tool executor."""

    def __init__(
        self,
        app: "HalcyonApp",
        *,
        host: str = "127.0.0.1",
        preferred_port: int = 8769,
    ) -> None:
        self._app_ref = weakref.ref(app)
        self._host = host
        self._preferred_port = preferred_port
        self._server: _ControlHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int | None:
        with self._lock:
            if self._server is None:
                return None
            return int(self._server.server_address[1])

    @property
    def api_url(self) -> str | None:
        port = self.port
        if port is None:
            return None
        return f"http://{self.host}:{port}"

    def start(self) -> tuple[str, int]:
        with self._lock:
            if self._server is not None and self._thread is not None and self._thread.is_alive():
                return self._host, int(self._server.server_address[1])

            port = _resolve_port(self._preferred_port, self._host)
            server = _ControlHTTPServer(
                (self._host, port),
                self._build_handler(),
                control=self,
            )
            thread = threading.Thread(
                target=server.serve_forever,
                name="halcyon-local-control",
                daemon=True,
            )
            thread.start()
            self._server = server
            self._thread = thread
            return self._host, port

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None

        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        app = self._app_ref()
        if app is None:
            raise ToolExecutionError("app is not available")
        call_from_thread = getattr(app, "call_from_thread", None)
        if callable(call_from_thread):
            return call_from_thread(lambda: execute_tool(app, name, arguments or {}))
        return execute_tool(app, name, arguments or {})

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        control = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _read_json(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    return {}
                if length <= 0:
                    return {}
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    return {}
                return payload if isinstance(payload, dict) else {}

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "host": control.host,
                            "port": control.port,
                            "tools": len(tool_definitions()),
                        },
                    )
                    return
                if path == "/tools":
                    self._json(HTTPStatus.OK, {"tools": tool_definitions()})
                    return
                if path == "/state":
                    self._handle_tool("get_synth_state", {})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                payload = self._read_json()
                if path == "/tools/call":
                    name = str(payload.get("name", "")).strip()
                    arguments = payload.get("arguments")
                    if not isinstance(arguments, dict):
                        arguments = {}
                    self._handle_tool(name, arguments)
                    return
                if path.startswith("/tool/"):
                    name = path.removeprefix("/tool/").strip()
                    self._handle_tool(name, payload)
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def _handle_tool(self, name: str, arguments: dict[str, Any]) -> None:
                if not name:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "tool name is required"})
                    return
                try:
                    result = control.call_tool(name, arguments)
                except ToolExecutionError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                    return
                self._json(HTTPStatus.OK, {"result": result})

        return Handler
