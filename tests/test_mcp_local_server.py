import json
from types import SimpleNamespace
from urllib import error, request

import pytest

from halcyon.app import HalcyonApp
from halcyon.mcp_local.run_tracker import RunTracker
from halcyon.mcp_local.server import LocalControlServer
from halcyon.menu import load_menu


RUN_ID = "a" * 32


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
            "run_id": RUN_ID,
            "command": command,
            "source": source,
            "timeout_s": timeout_s,
            "status": "completed",
        }
        self.runs[RUN_ID] = payload
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
    try:
        with request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _http_raw(
    base_url: str,
    path: str,
    payload: bytes,
    *,
    content_type: str = "application/json",
) -> tuple[int, dict[str, object]]:
    req = request.Request(
        f"{base_url}{path}",
        data=payload,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


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
        assert run["run_id"] == RUN_ID

        status, logs = _http_json(base_url, "GET", f"/run_logs/{RUN_ID}")
        assert status == 200
        assert logs["events"] == []
    finally:
        server.stop()


def test_local_control_server_requires_exact_loopback_binding() -> None:
    fake_app = FakeServerApp()

    with pytest.raises(ValueError, match="127.0.0.1"):
        LocalControlServer(fake_app, host="0.0.0.0")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="127.0.0.1"):
        LocalControlServer(fake_app, host="localhost")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        ({"command": "/panic", "extra": True}, "INVALID_ARGUMENTS"),
        ({"command": "panic"}, "INVALID_COMMAND"),
        ({"command": "/panic", "source": "remote"}, "INVALID_SOURCE"),
        ({"command": "/panic", "source": " CODEx "}, "INVALID_SOURCE"),
        ({"command": "/panic", "timeout_s": None}, "INVALID_TIMEOUT"),
        ({"command": "/panic", "timeout_s": True}, "INVALID_TIMEOUT"),
        ({"command": "/panic", "timeout_s": float("nan")}, "INVALID_JSON_BODY"),
        ({"command": "/panic", "timeout_s": float("inf")}, "INVALID_JSON_BODY"),
        ({"command": "/panic", "timeout_s": 0.5}, "INVALID_TIMEOUT"),
        ({"command": "/panic", "timeout_s": 601}, "INVALID_TIMEOUT"),
        ({"command": "/" + "x" * 512}, "INVALID_COMMAND"),
        ({"command": " " * 600 + "/panic"}, "INVALID_COMMAND"),
    ],
)
def test_local_control_server_rejects_unbounded_or_extra_arguments(
    payload: dict[str, object], expected_code: str
) -> None:
    fake_app = FakeServerApp()
    server = LocalControlServer(fake_app, preferred_port=0)  # type: ignore[arg-type]
    host, port = server.start()
    try:
        status, response = _http_json(
            f"http://{host}:{port}",
            "POST",
            "/run_slash",
            payload,
        )
        assert status == 400
        assert response["error"]["code"] == expected_code  # type: ignore[index]
        assert fake_app.runs == {}
    finally:
        server.stop()


def test_local_control_server_bounds_body_and_run_identifiers() -> None:
    fake_app = FakeServerApp()
    server = LocalControlServer(fake_app, preferred_port=0)  # type: ignore[arg-type]
    host, port = server.start()
    base_url = f"http://{host}:{port}"
    try:
        status, response = _http_json(
            base_url,
            "POST",
            "/run_slash",
            {"command": "/" + "x" * 9_000},
        )
        assert status == 413
        assert response["error"]["code"] == "REQUEST_TOO_LARGE"  # type: ignore[index]

        for invalid_path in (
            "/run_status/" + "a" * 31,
            "/run_status/" + "a" * 33,
            "/run_status/" + "z" * 32,
            "/run_status/%61" + "a" * 31,
            f"/run_status/{RUN_ID}?extra=true",
        ):
            status, response = _http_json(base_url, "GET", invalid_path)
            assert status == 400
            assert response["error"]["code"] in {  # type: ignore[index]
                "INVALID_REQUEST",
                "INVALID_RUN_ID",
            }
    finally:
        server.stop()


def test_local_control_server_rejects_malformed_or_wrong_media_body() -> None:
    fake_app = FakeServerApp()
    server = LocalControlServer(fake_app, preferred_port=0)  # type: ignore[arg-type]
    host, port = server.start()
    base_url = f"http://{host}:{port}"
    try:
        for payload, content_type in (
            (b"{not-json", "application/json"),
            (b"[]", "application/json"),
            (b'{"command":NaN}', "application/json"),
            (b'{"command":"/panic"}', "text/plain"),
        ):
            status, response = _http_raw(
                base_url,
                "/run_slash",
                payload,
                content_type=content_type,
            )
            assert status == 400
            assert response["error"]["code"] == "INVALID_JSON_BODY"  # type: ignore[index]
        assert fake_app.runs == {}
    finally:
        server.stop()


def test_local_control_server_bounds_every_http_response() -> None:
    class HugeStateApp(FakeServerApp):
        def mcp_local_state(self) -> dict[str, object]:
            return {"payload": "x" * 70_000}

    app = HugeStateApp()
    server = LocalControlServer(app, preferred_port=0)  # type: ignore[arg-type]
    host, port = server.start()
    try:
        status, response = _http_json(f"http://{host}:{port}", "GET", "/state")
        assert status == 502
        assert response["error"]["code"] == "LOCAL_RESPONSE_TOO_LARGE"  # type: ignore[index]
        assert len(json.dumps(response)) < 1_000
    finally:
        server.stop()


def test_local_control_server_rejects_nonfinite_app_response() -> None:
    class NonfiniteStateApp(FakeServerApp):
        def mcp_local_state(self) -> dict[str, object]:
            return {"score": float("nan")}

    app = NonfiniteStateApp()
    server = LocalControlServer(app, preferred_port=0)  # type: ignore[arg-type]
    host, port = server.start()
    try:
        status, response = _http_json(f"http://{host}:{port}", "GET", "/state")
        assert status == 502
        assert response["error"]["code"] == "INVALID_LOCAL_RESPONSE"  # type: ignore[index]
        assert "NaN" not in repr(response)
    finally:
        server.stop()


def test_local_control_server_never_echoes_callback_exceptions() -> None:
    class BrokenApp(FakeServerApp):
        def mcp_local_run_slash(self, **_arguments):  # type: ignore[no-untyped-def]
            raise RuntimeError("SECRET /private/lesson.mid")

    server = LocalControlServer(BrokenApp(), preferred_port=0)  # type: ignore[arg-type]
    host, port = server.start()
    try:
        status, response = _http_json(
            f"http://{host}:{port}",
            "POST",
            "/run_slash",
            {"command": "/panic"},
        )
        assert status == 503
        assert response["error"]["code"] == "LOCAL_CONTROL_UNAVAILABLE"  # type: ignore[index]
        assert "SECRET" not in repr(response)
        assert "private" not in repr(response)
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
