"""Stdio MCP bridge around Halcyon's local slash-command control server."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable
from urllib import error, request

from halcyon.mcp_tools import mcp_text_result

RequestJson = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


def _decode_message(payload: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(payload.decode("utf-8"))
    except Exception:
        return None
    return message if isinstance(message, dict) else None


def _read_message() -> dict[str, Any] | None:
    while True:
        first_line = sys.stdin.buffer.readline()
        if not first_line:
            return None
        stripped = first_line.strip()
        if not stripped:
            continue
        if stripped[:1] in {b"{", b"["}:
            return _decode_message(stripped)

        headers: dict[str, str] = {}
        for header_line in [first_line]:
            decoded = header_line.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line in {b"\r\n", b"\n"}:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            continue
        if length <= 0:
            continue
        return _decode_message(sys.stdin.buffer.read(length))


def _write_message(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
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
        self._api_url = api_url.rstrip("/")
        self._timeout_s = timeout_s
        self._request_json = request_json or self._default_request_json
        self._default_source = self._normalize_source(os.environ.get("HALCYON_MCP_SOURCE"))

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method", ""))
        request_id = message.get("id")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}

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
            name = str(params.get("name", "")).strip()
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            return self._result(request_id, self._call_tool(name, arguments))

        return self._error(request_id, -32601, f"method not found: {method}")

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
                "inputSchema": {"type": "object", "properties": {}, "required": []},
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
                        "command": {"type": "string"},
                        "source": {"type": "string"},
                        "timeout_s": {"type": "number"},
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "get_run_status",
                "description": "Get status for a Halcyon slash-command run_id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"}},
                    "required": ["run_id"],
                },
            },
            {
                "name": "get_run_logs",
                "description": "Fetch structured logs for a Halcyon slash-command run_id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"}},
                    "required": ["run_id"],
                },
            },
            {
                "name": "get_app_state",
                "description": "Get local Halcyon MCP state and run tracker status.",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
        ]

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_commands":
            return self._tool_response(self._request("GET", "/commands", None))
        if name == "run_slash_command":
            body: dict[str, Any] = {
                "command": str(arguments.get("command", "")),
                "source": self._normalize_source(arguments.get("source")),
            }
            if "timeout_s" in arguments and arguments.get("timeout_s") is not None:
                body["timeout_s"] = arguments.get("timeout_s")
            return self._tool_response(self._request("POST", "/run_slash", body))
        if name == "get_run_status":
            run_id = str(arguments.get("run_id", "")).strip()
            if not run_id:
                return self._tool_response({"error": "run_id is required"}, is_error=True)
            return self._tool_response(self._request("GET", f"/run_status/{run_id}", None))
        if name == "get_run_logs":
            run_id = str(arguments.get("run_id", "")).strip()
            if not run_id:
                return self._tool_response({"error": "run_id is required"}, is_error=True)
            return self._tool_response(self._request("GET", f"/run_logs/{run_id}", None))
        if name == "get_app_state":
            return self._tool_response(self._request("GET", "/state", None))
        return self._tool_response({"error": f"Unknown tool: {name}"}, is_error=True)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            result = self._request_json(method, path, payload)
        except Exception as exc:
            return {"error": str(exc), "status_code": 500}
        if "status_code" not in result:
            result["status_code"] = 200
        return result

    def _default_request_json(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(
            f"{self._api_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self._timeout_s) as response:
                body = response.read().decode("utf-8")
                status_code = response.status
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            status_code = exc.code
        parsed = _parse_json_body(body)
        parsed["status_code"] = status_code
        return parsed

    def _tool_response(
        self, payload: dict[str, Any], *, is_error: bool | None = None
    ) -> dict[str, Any]:
        response = mcp_text_result(payload)
        response["isError"] = bool(
            payload.get("error") or int(payload.get("status_code", 200)) >= 400
            if is_error is None
            else is_error
        )
        return response

    def _normalize_source(self, value: Any) -> str:
        source = str(value or getattr(self, "_default_source", "codex"))
        source = source.strip().lower() or "codex"
        if source not in {"codex", "claude", "llm", "debugger"}:
            return "codex"
        return source

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


def _parse_json_body(body: str) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except Exception:
        return {"raw": body}
    return parsed if isinstance(parsed, dict) else {"payload": parsed}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Halcyon local MCP stdio bridge")
    parser.add_argument("--api-url", default="http://127.0.0.1:8769")
    parser.add_argument("--timeout-s", type=float, default=15.0)
    args = parser.parse_args(argv)
    MCPBridge(api_url=args.api_url, timeout_s=args.timeout_s).run()


if __name__ == "__main__":
    main()
