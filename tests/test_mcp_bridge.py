import json

from halcyon.mcp_bridge import MCPBridge


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


def test_bridge_runs_slash_command_through_http(monkeypatch) -> None:
    monkeypatch.delenv("HALCYON_MCP_SOURCE", raising=False)

    def request_json(method, path, payload):  # type: ignore[no-untyped-def]
        assert method == "POST"
        assert path == "/run_slash"
        assert payload == {"command": "/panic", "source": "codex"}
        return {"run_id": "run-1", "status": "queued", "do_not_rerun": True}

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
    assert payload["run_id"] == "run-1"
    assert payload["do_not_rerun"] is True
    assert result["structuredContent"]["status"] == "queued"


def test_bridge_fetches_run_status_and_logs() -> None:
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def request_json(method, path, payload):  # type: ignore[no-untyped-def]
        requests.append((method, path, payload))
        if path.startswith("/run_status/"):
            return {"run_id": "run-1", "status": "completed"}
        return {"run_id": "run-1", "events": [], "truncated": False}

    bridge = MCPBridge(api_url="http://127.0.0.1:8769", request_json=request_json)

    status_response = bridge.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "status",
            "method": "tools/call",
            "params": {"name": "get_run_status", "arguments": {"run_id": "run-1"}},
        }
    )
    logs_response = bridge.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "logs",
            "method": "tools/call",
            "params": {"name": "get_run_logs", "arguments": {"run_id": "run-1"}},
        }
    )

    assert status_response is not None
    assert logs_response is not None
    assert requests == [
        ("GET", "/run_status/run-1", None),
        ("GET", "/run_logs/run-1", None),
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
    assert "Unknown tool" in result["structuredContent"]["error"]
