import json

from halcyon.mcp_bridge import MCPBridge


def test_bridge_lists_tools_from_http() -> None:
    def request_json(method, path, payload):  # type: ignore[no-untyped-def]
        assert method == "GET"
        assert path == "/tools"
        assert payload is None
        return {"tools": [{"name": "get_synth_state", "inputSchema": {"type": "object"}}]}

    bridge = MCPBridge(api_url="http://127.0.0.1:8769", request_json=request_json)

    response = bridge.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": [{"name": "get_synth_state", "inputSchema": {"type": "object"}}]},
    }


def test_bridge_calls_tool_through_http() -> None:
    def request_json(method, path, payload):  # type: ignore[no-untyped-def]
        assert method == "POST"
        assert path == "/tools/call"
        assert payload == {"name": "set_synth_param", "arguments": {"param": "tone", "value": 0.7}}
        return {"result": {"ok": True, "patch": {"tone": 0.7}}}

    bridge = MCPBridge(api_url="http://127.0.0.1:8769", request_json=request_json)

    response = bridge.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {
                "name": "set_synth_param",
                "arguments": {"param": "tone", "value": 0.7},
            },
        }
    )

    result = response["result"]  # type: ignore[index]
    assert json.loads(result["content"][0]["text"]) == {"ok": True, "patch": {"tone": 0.7}}
    assert result["structuredContent"] == {"ok": True, "patch": {"tone": 0.7}}
