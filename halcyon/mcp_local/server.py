"""Tiny localhost HTTP control server for MCP slash-command control."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import socket
import threading
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse
import weakref

if TYPE_CHECKING:
    from halcyon.app import HalcyonApp


_LOCAL_HOST = "127.0.0.1"
_MAX_REQUEST_BODY_BYTES = 8_192
_MAX_RESPONSE_BODY_BYTES = 65_536
_MAX_COMMAND_CHARS = 512
_MIN_TIMEOUT_S = 1.0
_MAX_TIMEOUT_S = 600.0
_ALLOWED_SOURCES = frozenset({"codex", "claude", "llm", "debugger"})
_RUN_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _resolve_port(preferred: int, host: str) -> int:
    candidates = [
        candidate
        for candidate in [preferred, *(preferred + step for step in range(1, 25))]
        if 0 <= candidate <= 65_535
    ]
    for candidate in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
            if candidate == 0:
                return int(sock.getsockname()[1])
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
    """Localhost control server exposing registered slash-command operations."""

    def __init__(
        self,
        app: "HalcyonApp",
        *,
        host: str = "127.0.0.1",
        preferred_port: int = 8769,
    ) -> None:
        if host != _LOCAL_HOST:
            raise ValueError("Local control host must be 127.0.0.1.")
        if (
            isinstance(preferred_port, bool)
            or not isinstance(preferred_port, int)
            or not 0 <= preferred_port <= 65_535
        ):
            raise ValueError("Local control port must be between 0 and 65535.")
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

    def _app(self) -> "HalcyonApp":
        app = self._app_ref()
        if app is None:
            raise RuntimeError("app is not available")
        return app

    def _call_app(self, callback: Callable[["HalcyonApp"], Any]) -> Any:
        app = self._app()
        call_from_thread = getattr(app, "call_from_thread", None)
        if callable(call_from_thread):
            return call_from_thread(lambda: callback(app))
        return callback(app)

    def list_commands(self) -> list[dict[str, Any]]:
        return self._call_app(lambda app: app.mcp_local_list_commands())

    def app_state(self) -> dict[str, Any]:
        return self._call_app(lambda app: app.mcp_local_state())

    def run_slash(
        self,
        *,
        command: str,
        source: str,
        timeout_s: float | None,
    ) -> tuple[int, dict[str, Any]]:
        return self._call_app(
            lambda app: app.mcp_local_run_slash(
                command=command,
                source=source,
                timeout_s=timeout_s,
            )
        )

    def run_status(self, run_id: str) -> dict[str, Any] | None:
        return self._call_app(lambda app: app.mcp_local_run_status(run_id))

    def run_logs(self, run_id: str) -> dict[str, Any] | None:
        return self._call_app(lambda app: app.mcp_local_run_logs(run_id))

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        control = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                try:
                    raw = json.dumps(
                        payload,
                        ensure_ascii=True,
                        allow_nan=False,
                    ).encode("utf-8")
                except (TypeError, ValueError):
                    status = HTTPStatus.BAD_GATEWAY
                    raw = json.dumps(
                        _error_payload(
                            "INVALID_LOCAL_RESPONSE",
                            "Local control produced an invalid response.",
                        ),
                        ensure_ascii=True,
                        allow_nan=False,
                    ).encode("utf-8")
                if len(raw) > _MAX_RESPONSE_BODY_BYTES:
                    status = HTTPStatus.BAD_GATEWAY
                    raw = json.dumps(
                        _error_payload(
                            "LOCAL_RESPONSE_TOO_LARGE",
                            "Local control response exceeded the server limit.",
                        ),
                        ensure_ascii=True,
                        allow_nan=False,
                    ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _read_json(self) -> tuple[dict[str, Any] | None, int | None]:
                if self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                    return None, HTTPStatus.BAD_REQUEST
                media_type = self.headers.get("Content-Type", "").partition(";")[0]
                if media_type.strip().lower() != "application/json":
                    return None, HTTPStatus.BAD_REQUEST
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    self.close_connection = True
                    return None, HTTPStatus.BAD_REQUEST
                if length <= 0:
                    return None, HTTPStatus.BAD_REQUEST
                if length > _MAX_REQUEST_BODY_BYTES:
                    self.close_connection = True
                    return None, HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        return None, HTTPStatus.BAD_REQUEST
                    payload = json.loads(
                        raw.decode("utf-8"),
                        parse_constant=_reject_json_constant,
                    )
                except Exception:
                    return None, HTTPStatus.BAD_REQUEST
                if not isinstance(payload, dict):
                    return None, HTTPStatus.BAD_REQUEST
                return payload, None

            def _unavailable(self) -> None:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    _error_payload(
                        "LOCAL_CONTROL_UNAVAILABLE",
                        "Local control request could not be completed.",
                    ),
                )

            def _lookup_run_id(self, path: str, prefix: str) -> str | None:
                run_id = path.removeprefix(prefix)
                return run_id.lower() if _RUN_ID_RE.fullmatch(run_id) else None

            def do_GET(self) -> None:  # noqa: N802
                parsed_path = urlparse(self.path)
                if "?" in self.path or parsed_path.query:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        _error_payload("INVALID_REQUEST", "Query parameters are not supported."),
                    )
                    return
                path = parsed_path.path
                try:
                    if path == "/health":
                        self._json(
                            HTTPStatus.OK,
                            {
                                "ok": True,
                                "host": control.host,
                                "port": control.port,
                                "commands": len(control.list_commands()),
                            },
                        )
                        return
                    if path == "/commands":
                        self._json(HTTPStatus.OK, {"commands": control.list_commands()})
                        return
                    if path == "/state":
                        self._json(HTTPStatus.OK, control.app_state())
                        return
                    if path.startswith("/run_status/"):
                        run_id = self._lookup_run_id(path, "/run_status/")
                        if run_id is None:
                            self._json(
                                HTTPStatus.BAD_REQUEST,
                                _error_payload("INVALID_RUN_ID", "run_id is invalid."),
                            )
                            return
                        self._handle_lookup(control.run_status(run_id))
                        return
                    if path.startswith("/run_logs/"):
                        run_id = self._lookup_run_id(path, "/run_logs/")
                        if run_id is None:
                            self._json(
                                HTTPStatus.BAD_REQUEST,
                                _error_payload("INVALID_RUN_ID", "run_id is invalid."),
                            )
                            return
                        self._handle_lookup(control.run_logs(run_id))
                        return
                except Exception:
                    self._unavailable()
                    return
                self._json(
                    HTTPStatus.NOT_FOUND,
                    _error_payload("NOT_FOUND", "Endpoint was not found."),
                )

            def do_POST(self) -> None:  # noqa: N802
                parsed_path = urlparse(self.path)
                if "?" in self.path or parsed_path.query:
                    self.close_connection = True
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        _error_payload("INVALID_REQUEST", "Query parameters are not supported."),
                    )
                    return
                path = parsed_path.path
                if path != "/run_slash":
                    self.close_connection = True
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        _error_payload("NOT_FOUND", "Endpoint was not found."),
                    )
                    return

                payload, read_error = self._read_json()
                if read_error is not None:
                    code = (
                        "REQUEST_TOO_LARGE"
                        if read_error == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                        else "INVALID_JSON_BODY"
                    )
                    message = (
                        "Request body exceeds the local control limit."
                        if read_error == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                        else "Request body must be a JSON object."
                    )
                    self._json(read_error, _error_payload(code, message))
                    return
                assert payload is not None
                if not set(payload).issubset({"command", "source", "timeout_s"}):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        _error_payload(
                            "INVALID_ARGUMENTS",
                            "Request contains unsupported arguments.",
                        ),
                    )
                    return

                command = payload.get("command")
                if not isinstance(command, str):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        _error_payload("INVALID_COMMAND", "command must be text."),
                    )
                    return
                if not command or len(command) > _MAX_COMMAND_CHARS or not command.startswith("/"):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        _error_payload(
                            "INVALID_COMMAND",
                            "command must be a bounded registered slash command.",
                        ),
                    )
                    return
                command = command.strip()

                source_value = payload.get("source", "llm")
                if not isinstance(source_value, str):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        _error_payload("INVALID_SOURCE", "source is invalid."),
                    )
                    return
                source = source_value
                if source not in _ALLOWED_SOURCES:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        _error_payload("INVALID_SOURCE", "source is invalid."),
                    )
                    return

                timeout_s, timeout_valid = self._optional_float(
                    payload.get("timeout_s"),
                    present="timeout_s" in payload,
                )
                if not timeout_valid:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        _error_payload("INVALID_TIMEOUT", "timeout_s is invalid."),
                    )
                    return
                try:
                    status, result = control.run_slash(
                        command=command,
                        source=source,
                        timeout_s=timeout_s,
                    )
                except Exception:
                    self._unavailable()
                    return
                if (
                    isinstance(status, bool)
                    or not isinstance(status, int)
                    or not 100 <= status <= 599
                    or not isinstance(result, dict)
                ):
                    self._unavailable()
                    return
                self._json(status, result)

            def _handle_lookup(self, payload: dict[str, Any] | None) -> None:
                if payload is None:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        _error_payload("RUN_NOT_FOUND", "Run was not found."),
                    )
                    return
                self._json(HTTPStatus.OK, payload)

            @staticmethod
            def _optional_float(value: Any, *, present: bool) -> tuple[float | None, bool]:
                if not present:
                    return None, True
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return None, False
                parsed = float(value)
                if not math.isfinite(parsed) or parsed < _MIN_TIMEOUT_S or parsed > _MAX_TIMEOUT_S:
                    return None, False
                return parsed, True

        return Handler
