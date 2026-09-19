import io
import json
from types import SimpleNamespace

import pytest

from halcyon.mcp_bridge import (
    MCPBridge,
    _MAX_RPC_FRAME_BYTES,
    _decode_message,
    _parse_json_body,
    _read_message,
)


RUN_ID = "a" * 32


def _tool_names(response: dict[str, object]) -> set[str]:
    result = response["result"]  # type: ignore[index]
    return {tool["name"] for tool in result["tools"]}  # type: ignore[index]


def test_bridge_lists_only_command_control_tools() -> None:
    bridge = MCPBridge(api_url="http://127.0.0.1:8769")

    response = bridge.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response is not None
    assert _tool_names(response) == {
        "list_commands",
        "run_slash_command",
        "get_run_status",
        "get_run_logs",
        "get_app_state",
    }
    tools = response["result"]["tools"]  # type: ignore[index]
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)


def test_bridge_runs_slash_command_through_http(monkeypatch) -> None:
    monkeypatch.delenv("HALCYON_MCP_SOURCE", raising=False)

    def request_json(method, path, payload):  # type: ignore[no-untyped-def]
        assert method == "POST"
        assert path == "/run_slash"
        assert payload == {"command": "/panic", "source": "codex"}
        return {"run_id": RUN_ID, "status": "queued", "do_not_rerun": True}

    bridge = MCPBridge(api_url="http://127.0.0.1:8769", request_json=request_json)

    response = bridge.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {
                "name": "run_slash_command",
                "arguments": {"command": "/panic"},
            },
        }
    )

    result = response["result"]  # type: ignore[index]
    payload = json.loads(result["content"][0]["text"])
    assert payload["run_id"] == RUN_ID
    assert payload["do_not_rerun"] is True
    assert result["structuredContent"]["status"] == "queued"


def test_bridge_fetches_run_status_and_logs() -> None:
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def request_json(method, path, payload):  # type: ignore[no-untyped-def]
        requests.append((method, path, payload))
        if path.startswith("/run_status/"):
            return {"run_id": RUN_ID, "status": "completed"}
        return {"run_id": RUN_ID, "events": [], "truncated": False}

    bridge = MCPBridge(api_url="http://127.0.0.1:8769", request_json=request_json)

    status_response = bridge.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "status",
            "method": "tools/call",
            "params": {"name": "get_run_status", "arguments": {"run_id": RUN_ID}},
        }
    )
    logs_response = bridge.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "logs",
            "method": "tools/call",
            "params": {"name": "get_run_logs", "arguments": {"run_id": RUN_ID}},
        }
    )

    assert status_response is not None
    assert logs_response is not None
    assert requests == [
        ("GET", f"/run_status/{RUN_ID}", None),
        ("GET", f"/run_logs/{RUN_ID}", None),
    ]


def test_bridge_rejects_raw_synth_tool_calls() -> None:
    bridge = MCPBridge(api_url="http://127.0.0.1:8769")

    response = bridge.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "bad",
            "method": "tools/call",
            "params": {"name": "set_synth_param", "arguments": {"param": "tone"}},
        }
    )

    result = response["result"]  # type: ignore[index]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {
        "code": "UNKNOWN_TOOL",
        "message": "Unknown command-control tool.",
    }


@pytest.mark.parametrize(
    "api_url",
    [
        "http://0.0.0.0:8769",
        "http://localhost:8769",
        "https://127.0.0.1:8769",
        "http://127.0.0.1:8769/private",
        "http://user@127.0.0.1:8769",
        "http://127.0.0.1:8769?token=secret",
        "http://127.0.0.1:8769?",
        "http://127.0.0.1:8769#",
        "http://127.0.0.1:0",
    ],
)
def test_bridge_requires_exact_loopback_origin(api_url: str) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        MCPBridge(api_url=api_url)


@pytest.mark.parametrize("timeout_s", [float("nan"), float("inf"), 0, 61])
def test_bridge_requires_bounded_finite_transport_timeout(timeout_s: float) -> None:
    with pytest.raises(ValueError, match="between 1 and 60"):
        MCPBridge(api_url="http://127.0.0.1:8769", timeout_s=timeout_s)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("list_commands", {"extra": True}),
        ("get_app_state", {"extra": True}),
        ("run_slash_command", {"command": "/panic", "extra": True}),
        ("run_slash_command", {"command": "panic"}),
        ("run_slash_command", {"command": " " * 600 + "/panic"}),
        ("run_slash_command", {"command": "/" + "x" * 512}),
        ("run_slash_command", {"command": "/panic", "source": "remote"}),
        ("run_slash_command", {"command": "/panic", "source": " CODEx "}),
        ("run_slash_command", {"command": "/panic", "timeout_s": None}),
        ("run_slash_command", {"command": "/panic", "timeout_s": True}),
        ("run_slash_command", {"command": "/panic", "timeout_s": float("nan")}),
        ("run_slash_command", {"command": "/panic", "timeout_s": float("inf")}),
        ("run_slash_command", {"command": "/panic", "timeout_s": 0.5}),
        ("run_slash_command", {"command": "/panic", "timeout_s": 601}),
        ("get_run_status", {"run_id": "../private"}),
        ("get_run_status", {"run_id": "a" * 31}),
        ("get_run_status", {"run_id": "a" * 33}),
        ("get_run_status", {"run_id": "z" * 32}),
        ("get_run_logs", {"run_id": RUN_ID, "extra": True}),
    ],
)
def test_bridge_rejects_invalid_arguments_before_http(
    name: str, arguments: dict[str, object]
) -> None:
    calls = []

    def request_json(*args):  # type: ignore[no-untyped-def]
        calls.append(args)
        return {}

    bridge = MCPBridge(
        api_url="http://127.0.0.1:8769",
        request_json=request_json,
    )
    response = bridge.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "invalid",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )

    result = response["result"]  # type: ignore[index]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "INVALID_ARGUMENTS"
    assert calls == []


def test_bridge_never_echoes_transport_exceptions_or_malformed_bodies() -> None:
    def request_json(*_args):  # type: ignore[no-untyped-def]
        raise RuntimeError("SECRET /private/lesson.mid")

    bridge = MCPBridge(
        api_url="http://127.0.0.1:8769",
        request_json=request_json,
    )
    response = bridge.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "commands",
            "method": "tools/call",
            "params": {"name": "list_commands", "arguments": {}},
        }
    )
    result = response["result"]  # type: ignore[index]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "LOCAL_CONTROL_UNAVAILABLE"
    assert "SECRET" not in repr(result)
    assert "private" not in repr(result)

    malformed = _parse_json_body(b"SECRET /private/lesson.mid")
    assert malformed["error"]["code"] == "INVALID_LOCAL_RESPONSE"
    assert "SECRET" not in repr(malformed)
    assert "private" not in repr(malformed)


def test_bridge_rejects_non_object_tool_arguments() -> None:
    bridge = MCPBridge(api_url="http://127.0.0.1:8769")

    for arguments in (None, [], "{}", 1):
        response = bridge.handle_message(
            {
                "jsonrpc": "2.0",
                "id": "invalid",
                "method": "tools/call",
                "params": {"name": "list_commands", "arguments": arguments},
            }
        )
        result = response["result"]  # type: ignore[index]
        assert result["isError"] is True
        assert result["structuredContent"]["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.parametrize(
    "tool_name",
    [
        "set_synth_param",
        "start_coach_lesson",
        "play_midi",
        "get_midi_logs",
        "read_midi_history",
        "author_lesson",
    ],
)
def test_bridge_rejects_every_raw_tool_family(tool_name: str) -> None:
    bridge = MCPBridge(api_url="http://127.0.0.1:8769")
    response = bridge.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "raw",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": {}},
        }
    )
    result = response["result"]  # type: ignore[index]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "UNKNOWN_TOOL"


def test_bridge_sanitizes_structured_upstream_error_and_bounds_response() -> None:
    responses = iter(
        (
            {
                "error": {
                    "code": "HOST_FAILURE",
                    "message": "SECRET /private/lesson.mid",
                },
                "status_code": 500,
            },
            {"payload": "x" * 70_000},
            {"score": float("nan")},
        )
    )
    bridge = MCPBridge(
        api_url="http://127.0.0.1:8769",
        request_json=lambda *_args: next(responses),
    )

    for request_id in ("secret", "huge", "nonfinite"):
        response = bridge.handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "list_commands", "arguments": {}},
            }
        )
        result = response["result"]  # type: ignore[index]
        assert result["isError"] is True
        assert "SECRET" not in repr(result)
        assert "private" not in repr(result)
        assert "x" * 1_000 not in repr(result)


def test_bridge_rejects_nonfinite_json_constants() -> None:
    assert _decode_message(b'{"method":"ping","id":NaN}') is None
    parsed = _parse_json_body(b'{"score":Infinity}')
    assert parsed["error"]["code"] == "INVALID_LOCAL_RESPONSE"
    assert "Infinity" not in repr(parsed)


def test_bridge_stdio_reader_skips_oversized_newline_frame(monkeypatch) -> None:
    valid = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    stream = io.BytesIO(b"{" + b"x" * (_MAX_RPC_FRAME_BYTES + 8) + b"\n" + valid)
    monkeypatch.setattr(
        "halcyon.mcp_bridge.sys.stdin",
        SimpleNamespace(buffer=stream),
    )

    assert _read_message() == {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def test_bridge_stdio_reader_rejects_oversized_content_length(monkeypatch) -> None:
    stream = io.BytesIO(f"Content-Length: {_MAX_RPC_FRAME_BYTES + 1}\r\n\r\n".encode())
    monkeypatch.setattr(
        "halcyon.mcp_bridge.sys.stdin",
        SimpleNamespace(buffer=stream),
    )

    assert _read_message() is None
