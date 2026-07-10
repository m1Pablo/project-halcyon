import json
from types import SimpleNamespace
from urllib import request

from halcyon.app import HalcyonApp
from halcyon.mcp_local.run_tracker import RunTracker
from halcyon.mcp_local.server import LocalControlServer
from halcyon.menu import load_menu


def _fake_halcyon_app() -> HalcyonApp:
    app = object.__new__(HalcyonApp)
    app.menu_def = load_menu()
    app._run_tracker = RunTracker()
    app.calls = []

    def execute_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        app.calls.append((name, arguments))
        return {"ok": True, "tool": name, "arguments": arguments}

    app._execute_tool_from_worker = execute_tool
    return app


def test_app_mcp_run_slash_dispatches_through_command_executor() -> None:
    app = _fake_halcyon_app()

    status_code, payload = app.mcp_local_run_slash(
        command="/synth set tone 0.7",
        source="codex",
        timeout_s=12,
    )

    assert status_code == 200
    assert payload["status"] == "completed"
    assert payload["run_id"]
    assert payload["do_not_rerun"] is False
    assert app.calls == [("set_synth_param", {"param": "tone", "value": 0.7})]


def test_app_mcp_run_slash_deduplicates_active_single_flight_group() -> None:
    app = _fake_halcyon_app()
    run_id, _status = app._run_tracker.create_run(
        command="/synth set tone 0.4",
        source="codex",
        timeout_s=60,
    )
    app._run_tracker.mark_running(run_id)

    status_code, payload = app.mcp_local_run_slash(
        command="/synth patch release=0.2",
        source="codex",
        timeout_s=None,
    )

    assert status_code == 200
    assert payload["deduplicated"] is True
    assert payload["deduplicated_run_id"] == run_id
    assert payload["do_not_rerun"] is True
    assert app.calls == []


def test_app_mcp_state_does_not_expose_environment_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-do-not-emit")
    app = _fake_halcyon_app()
    app.local_control_server = SimpleNamespace(host="127.0.0.1", port=8769)
    app.synth_tool_state = lambda: {"patch": {"preset": "Init"}}

    state = app.mcp_local_state()

    state_text = repr(state)
    assert "OPENAI_API_KEY" not in state_text
    assert "sk-test-do-not-emit" not in state_text


class FakeServerApp:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, object]] = {}

    def call_from_thread(self, callback):  # type: ignore[no-untyped-def]
        return callback()

    def mcp_local_list_commands(self) -> list[dict[str, object]]:
        return [{"command": "/panic", "description": "Release notes"}]

    def mcp_local_state(self) -> dict[str, object]:
        return {"tracker": {"runs_total": len(self.runs)}, "synth": {"patch": {}}}

    def mcp_local_run_slash(
        self,
        *,
        command: str,
        source: str,
        timeout_s: float | None,
    ) -> tuple[int, dict[str, object]]:
        payload = {
            "run_id": "run-1",
            "command": command,
            "source": source,
            "timeout_s": timeout_s,
            "status": "completed",
        }
        self.runs["run-1"] = payload
        return 200, payload

    def mcp_local_run_status(self, run_id: str) -> dict[str, object] | None:
        return self.runs.get(run_id)

    def mcp_local_run_logs(self, run_id: str) -> dict[str, object] | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        return {**run, "events": [], "truncated": False}


def _http_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_local_control_server_exposes_command_endpoints() -> None:
    fake_app = FakeServerApp()
    server = LocalControlServer(fake_app, preferred_port=0)  # type: ignore[arg-type]
    host, port = server.start()
    base_url = f"http://{host}:{port}"
    try:
        status, commands = _http_json(base_url, "GET", "/commands")
        assert status == 200
        assert commands["commands"] == [{"command": "/panic", "description": "Release notes"}]

        status, run = _http_json(
            base_url,
            "POST",
            "/run_slash",
            {"command": "/panic", "source": "codex"},
        )
        assert status == 200
        assert run["run_id"] == "run-1"

        status, logs = _http_json(base_url, "GET", "/run_logs/run-1")
        assert status == 200
        assert logs["events"] == []
    finally:
        server.stop()


def test_local_control_server_does_not_expose_raw_tools_route() -> None:
    fake_app = FakeServerApp()
    server = LocalControlServer(fake_app, preferred_port=0)  # type: ignore[arg-type]
    host, port = server.start()
    try:
        req = request.Request(f"http://{host}:{port}/tools", method="GET")
        try:
            request.urlopen(req, timeout=5)
        except Exception as exc:
            assert "HTTP Error 404" in str(exc)
        else:
            raise AssertionError("/tools should not be exposed")
    finally:
        server.stop()
