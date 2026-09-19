"""Stdio MCP bridge around Halcyon's local slash-command control server."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Any, Callable
from urllib import error, request
from urllib.parse import urlsplit

from halcyon.mcp_tools import mcp_text_result

RequestJson = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]

_MAX_COMMAND_CHARS = 512
_MAX_HTTP_RESPONSE_BYTES = 65_536
_MAX_RPC_FRAME_BYTES = 65_536
_MAX_RPC_HEADER_BYTES = 8_192
_MIN_TIMEOUT_S = 1.0
_MAX_RUN_TIMEOUT_S = 600.0
_MAX_BRIDGE_TIMEOUT_S = 60.0
_ALLOWED_SOURCES = frozenset({"codex", "claude", "llm", "debugger"})
_RUN_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_KNOWN_UPSTREAM_ERRORS = {
    "INVALID_ARGUMENTS": "Request contains unsupported arguments.",
    "INVALID_COMMAND": "command is invalid.",
    "INVALID_JSON_BODY": "Request body must be a JSON object.",
    "INVALID_REQUEST": "Request is invalid.",
    "INVALID_RUN_ID": "run_id is invalid.",
    "INVALID_SOURCE": "source is invalid.",
    "INVALID_TIMEOUT": "timeout_s is invalid.",
    "LOCAL_CONTROL_UNAVAILABLE": "Local control request could not be completed.",
    "LOCAL_RESPONSE_TOO_LARGE": "Local control response exceeded its limit.",
    "NOT_FOUND": "Local control endpoint was not found.",
    "REQUEST_TOO_LARGE": "Request body exceeded the local control limit.",
    "RUN_NOT_FOUND": "Run was not found.",
}


def _fixed_error(code: str, message: str, *, status_code: int = 400) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message},
        "status_code": status_code,
    }


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode_message(payload: bytes) -> dict[str, Any] | None:
    if len(payload) > _MAX_RPC_FRAME_BYTES:
        return None
    try:
        message = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except Exception:
        return None
    return message if isinstance(message, dict) else None


def _read_message() -> dict[str, Any] | None:
    while True:
        first_line = sys.stdin.buffer.readline(_MAX_RPC_FRAME_BYTES + 2)
        if not first_line:
            return None
        if len(first_line) > _MAX_RPC_FRAME_BYTES:
            while first_line and not first_line.endswith(b"\n"):
                first_line = sys.stdin.buffer.readline(_MAX_RPC_FRAME_BYTES + 2)
            continue
        stripped = first_line.strip()
        if not stripped:
            continue
        if stripped[:1] in {b"{", b"["}:
            return _decode_message(stripped)

        headers: dict[str, str] = {}
        header_bytes = len(first_line)
        for header_line in [first_line]:
            decoded = header_line.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        while True:
            line = sys.stdin.buffer.readline(_MAX_RPC_HEADER_BYTES + 1)
            if not line:
                return None
            header_bytes += len(line)
            if len(line) > _MAX_RPC_HEADER_BYTES or header_bytes > _MAX_RPC_HEADER_BYTES:
                return None
            if line in {b"\r\n", b"\n"}:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        length_text = headers.get("content-length", "0")
        if len(length_text) > 12:
            return None
        try:
            length = int(length_text)
        except ValueError:
            continue
        if length <= 0:
            continue
        if length > _MAX_RPC_FRAME_BYTES:
            return None
        payload = sys.stdin.buffer.read(length)
        if len(payload) != length:
            return None
        return _decode_message(payload)


def _write_message(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    )
    sys.stdout.write("\n")
    sys.stdout.flush()


class MCPBridge:
    """JSON-RPC stdio bridge for Halcyon command-control tools."""

    def __init__(
        self,
        *,
        api_url: str,
        timeout_s: float = 15.0,
        request_json: RequestJson | None = None,
    ) -> None:
        self._api_url = self._validate_api_url(api_url)
        if isinstance(timeout_s, bool):
            raise ValueError("Bridge timeout must be between 1 and 60 seconds.")
        try:
            timeout_value = float(timeout_s)
        except (TypeError, ValueError) as exc:
            raise ValueError("Bridge timeout must be between 1 and 60 seconds.") from exc
        if (
            not math.isfinite(timeout_value)
            or timeout_value < _MIN_TIMEOUT_S
            or timeout_value > _MAX_BRIDGE_TIMEOUT_S
        ):
            raise ValueError("Bridge timeout must be between 1 and 60 seconds.")
        self._timeout_s = timeout_value
        self._request_json = request_json or self._default_request_json
        self._default_source = self._normalize_source(os.environ.get("HALCYON_MCP_SOURCE"))

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method", ""))
        request_id = message.get("id")
        raw_params = message.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}

        if method == "notifications/initialized" or request_id is None:
            return None
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "halcyon-local", "version": "0.1.0"},
                },
            )
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": self._tools()})
        if method == "tools/call":
            if raw_params is not None and not isinstance(raw_params, dict):
                return self._error(request_id, -32602, "Invalid params.")
            name = str(params.get("name", "")).strip()
            arguments_value = params.get("arguments", {})
            if not isinstance(arguments_value, dict):
                return self._result(request_id, self._invalid_arguments())
            arguments = arguments_value
            return self._result(request_id, self._call_tool(name, arguments))

        return self._error(request_id, -32601, "Method not found.")

    def run(self) -> None:
        while True:
            message = _read_message()
            if message is None:
                return
            response = self.handle_message(message)
            if response is not None:
                _write_message(response)

    def _tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "list_commands",
                "description": "List available Halcyon slash commands from menu.xml.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "name": "run_slash_command",
                "description": (
                    "Run one registered slash command in the Halcyon TUI. Returns a run_id; "
                    "poll get_run_status/get_run_logs and do not rerun while "
                    "do_not_rerun=true."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_COMMAND_CHARS,
                            "pattern": "^/",
                        },
                        "source": {
                            "type": "string",
                            "enum": sorted(_ALLOWED_SOURCES),
                        },
                        "timeout_s": {
                            "type": "number",
                            "minimum": _MIN_TIMEOUT_S,
                            "maximum": _MAX_RUN_TIMEOUT_S,
                        },
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_run_status",
                "description": "Get status for a Halcyon slash-command run_id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "pattern": "^[0-9a-fA-F]{32}$",
                            "minLength": 32,
                            "maxLength": 32,
                        }
                    },
                    "required": ["run_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_run_logs",
                "description": "Fetch structured logs for a Halcyon slash-command run_id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "pattern": "^[0-9a-fA-F]{32}$",
                            "minLength": 32,
                            "maxLength": 32,
                        }
                    },
                    "required": ["run_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_app_state",
                "description": "Get local Halcyon MCP state and run tracker status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        ]

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_commands":
            if arguments:
                return self._invalid_arguments()
            return self._tool_response(self._request("GET", "/commands", None))
        if name == "run_slash_command":
            if not set(arguments).issubset({"command", "source", "timeout_s"}):
                return self._invalid_arguments()
            command = arguments.get("command")
            if not isinstance(command, str):
                return self._invalid_arguments("command is invalid.")
            if not command or len(command) > _MAX_COMMAND_CHARS or not command.startswith("/"):
                return self._invalid_arguments("command is invalid.")
            command = command.strip()
            source_value = arguments.get("source", self._default_source)
            if not isinstance(source_value, str):
                return self._invalid_arguments("source is invalid.")
            source = source_value
            if source not in _ALLOWED_SOURCES:
                return self._invalid_arguments("source is invalid.")
            body: dict[str, Any] = {
                "command": command,
                "source": source,
            }
            if "timeout_s" in arguments:
                timeout_value = arguments.get("timeout_s")
                if (
                    isinstance(timeout_value, bool)
                    or not isinstance(timeout_value, (int, float))
                    or not math.isfinite(float(timeout_value))
                    or not _MIN_TIMEOUT_S <= float(timeout_value) <= _MAX_RUN_TIMEOUT_S
                ):
                    return self._invalid_arguments("timeout_s is invalid.")
                body["timeout_s"] = float(timeout_value)
            return self._tool_response(self._request("POST", "/run_slash", body))
        if name == "get_run_status":
            run_id = self._validated_run_id(arguments)
            if run_id is None:
                return self._invalid_arguments("run_id is invalid.")
            return self._tool_response(self._request("GET", f"/run_status/{run_id}", None))
        if name == "get_run_logs":
            run_id = self._validated_run_id(arguments)
            if run_id is None:
                return self._invalid_arguments("run_id is invalid.")
            return self._tool_response(self._request("GET", f"/run_logs/{run_id}", None))
        if name == "get_app_state":
            if arguments:
                return self._invalid_arguments()
            return self._tool_response(self._request("GET", "/state", None))
        return self._tool_response(
            _fixed_error("UNKNOWN_TOOL", "Unknown command-control tool."),
            is_error=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            result = self._request_json(method, path, payload)
        except Exception:
            return _fixed_error(
                "LOCAL_CONTROL_UNAVAILABLE",
                "Local control request could not be completed.",
                status_code=503,
            )
        if not isinstance(result, dict):
            return _fixed_error(
                "INVALID_LOCAL_RESPONSE",
                "Local control returned an invalid response.",
                status_code=502,
            )
        result = dict(result)
        raw_status = result.get("status_code", 200)
        if isinstance(raw_status, bool):
            return _fixed_error(
                "INVALID_LOCAL_RESPONSE",
                "Local control returned an invalid response.",
                status_code=502,
            )
        try:
            status_code = int(raw_status)
        except (TypeError, ValueError):
            return _fixed_error(
                "INVALID_LOCAL_RESPONSE",
                "Local control returned an invalid response.",
                status_code=502,
            )
        if not 100 <= status_code <= 599:
            return _fixed_error(
                "INVALID_LOCAL_RESPONSE",
                "Local control returned an invalid response.",
                status_code=502,
            )
        result["status_code"] = status_code
        result = _sanitize_upstream_error(result)
        try:
            encoded = json.dumps(
                result,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            return _fixed_error(
                "INVALID_LOCAL_RESPONSE",
                "Local control returned an invalid response.",
                status_code=502,
            )
        if len(encoded) > _MAX_HTTP_RESPONSE_BYTES:
            return _fixed_error(
                "LOCAL_RESPONSE_TOO_LARGE",
                "Local control response exceeded the bridge limit.",
                status_code=502,
            )
        return result

    def _default_request_json(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(
            f"{self._api_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self._timeout_s) as response:
                body_bytes = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
                status_code = response.status
        except error.HTTPError as exc:
            body_bytes = exc.read(_MAX_HTTP_RESPONSE_BYTES + 1)
            status_code = exc.code
        if len(body_bytes) > _MAX_HTTP_RESPONSE_BYTES:
            return _fixed_error(
                "LOCAL_RESPONSE_TOO_LARGE",
                "Local control response exceeded the bridge limit.",
                status_code=502,
            )
        parsed = _parse_json_body(body_bytes)
        error_payload = parsed.get("error")
        if not (
            isinstance(error_payload, dict)
            and error_payload.get("code") == "INVALID_LOCAL_RESPONSE"
        ):
            parsed["status_code"] = status_code
        return parsed

    def _tool_response(
        self, payload: dict[str, Any], *, is_error: bool | None = None
    ) -> dict[str, Any]:
        response = mcp_text_result(payload)
        try:
            status_code = int(payload.get("status_code", 200))
        except (TypeError, ValueError):
            status_code = 500
        response["isError"] = bool(
            payload.get("error") or status_code >= 400 if is_error is None else is_error
        )
        return response

    def _invalid_arguments(self, message: str = "Tool arguments are invalid.") -> dict[str, Any]:
        return self._tool_response(
            _fixed_error("INVALID_ARGUMENTS", message),
            is_error=True,
        )

    @staticmethod
    def _validated_run_id(arguments: dict[str, Any]) -> str | None:
        if set(arguments) != {"run_id"}:
            return None
        value = arguments.get("run_id")
        if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
            return None
        return value.lower()

    def _normalize_source(self, value: Any) -> str:
        source = str(value or getattr(self, "_default_source", "codex"))
        source = source.strip().lower() or "codex"
        if source not in _ALLOWED_SOURCES:
            return "codex"
        return source

    @staticmethod
    def _validate_api_url(value: str) -> str:
        raw_value = str(value)
        try:
            parsed = urlsplit(raw_value)
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise ValueError("MCP bridge API URL must target 127.0.0.1.") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or port == 0
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in raw_value
            or "#" in raw_value
        ):
            raise ValueError("MCP bridge API URL must target 127.0.0.1.")
        return f"http://127.0.0.1:{port}"

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def _parse_json_body(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except Exception:
        return _fixed_error(
            "INVALID_LOCAL_RESPONSE",
            "Local control returned an invalid response.",
            status_code=502,
        )
    if not isinstance(parsed, dict):
        return _fixed_error(
            "INVALID_LOCAL_RESPONSE",
            "Local control returned an invalid response.",
            status_code=502,
        )
    return parsed


def _sanitize_upstream_error(payload: dict[str, Any]) -> dict[str, Any]:
    status_code = int(payload.get("status_code", 200))
    error_payload = payload.get("error")
    if status_code < 400 and error_payload is None:
        return payload

    if status_code < 400 and isinstance(error_payload, str) and payload.get("run_id"):
        sanitized = dict(payload)
        sanitized["error"] = "Command did not complete successfully."
        return sanitized

    if isinstance(error_payload, dict):
        code = str(error_payload.get("code", ""))
        message = _KNOWN_UPSTREAM_ERRORS.get(code)
        if message is not None:
            return _fixed_error(code, message, status_code=max(400, status_code))

    return _fixed_error(
        "LOCAL_CONTROL_REQUEST_FAILED",
        "Local control request did not succeed.",
        status_code=max(400, status_code),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Halcyon local MCP stdio bridge")
    parser.add_argument("--api-url", default="http://127.0.0.1:8769")
    parser.add_argument("--timeout-s", type=float, default=15.0)
    args = parser.parse_args(argv)
    MCPBridge(api_url=args.api_url, timeout_s=args.timeout_s).run()


if __name__ == "__main__":
    main()
