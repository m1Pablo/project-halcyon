"""Stdio MCP bridge around the running Halcyon local control server."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable
from urllib import error, request

from halcyon.mcp_tools import mcp_text_result, tool_definitions

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
    """JSON-RPC stdio bridge for local Halcyon synth tools."""

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
        if method == "tools/list":
            try:
                payload = self._request_json("GET", "/tools", None)
                tools = payload.get("tools", tool_definitions())
            except Exception:
                tools = tool_definitions()
            return self._result(request_id, {"tools": tools})
        if method == "tools/call":
            name = str(params.get("name", "")).strip()
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                payload = self._request_json(
                    "POST",
                    "/tools/call",
                    {"name": name, "arguments": arguments},
                )
                result = payload.get("result", payload)
                return self._result(request_id, mcp_text_result(result))
            except Exception as exc:
                return self._error(request_id, -32000, str(exc))

        return self._error(request_id, -32601, f"method not found: {method}")

    def run(self) -> None:
        while True:
            message = _read_message()
            if message is None:
                return
            response = self.handle_message(message)
            if response is not None:
                _write_message(response)

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
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"error": body or str(exc)}
            raise RuntimeError(str(parsed.get("error", exc))) from exc
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}

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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Halcyon local MCP stdio bridge")
    parser.add_argument("--api-url", default="http://127.0.0.1:8769")
    parser.add_argument("--timeout-s", type=float, default=15.0)
    args = parser.parse_args(argv)
    MCPBridge(api_url=args.api_url, timeout_s=args.timeout_s).run()


if __name__ == "__main__":
    main()
