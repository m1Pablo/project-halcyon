from types import SimpleNamespace

from halcyon.assistant import DISABLED_MESSAGE, HalcyonAssistant, openai_tool_definitions


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return SimpleNamespace(
                id="resp-1",
                output=[
                    {
                        "type": "function_call",
                        "name": "apply_synth_patch",
                        "arguments": '{"values":{"tone":0.85,"release":0.03}}',
                        "call_id": "call-1",
                    }
                ],
                output_text="",
            )
        return SimpleNamespace(id="resp-2", output=[], output_text="Brighter and shorter.")


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


def test_openai_tool_definitions_are_limited_to_synth_tools() -> None:
    names = {tool["name"] for tool in openai_tool_definitions()}

    assert names == {
        "get_synth_state",
        "list_synth_params",
        "set_synth_param",
        "apply_synth_patch",
        "apply_synth_preset",
        "save_synth_preset",
        "all_notes_off",
    }
    assert all(tool["type"] == "function" for tool in openai_tool_definitions())


def test_assistant_executes_tool_call_from_mocked_openai() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        if name == "get_synth_state":
            return {"patch": {"preset": "Init"}, "recent_midi": []}
        return {"ok": True, "arguments": arguments}

    client = FakeClient()
    assistant = HalcyonAssistant(executor, api_key="sk-test", client=client)

    result = assistant.ask("make it brighter and shorter")

    assert result.text == "Brighter and shorter."
    assert calls == [
        ("get_synth_state", {}),
        ("apply_synth_patch", {"values": {"tone": 0.85, "release": 0.03}}),
    ]
    assert client.responses.calls[1]["previous_response_id"] == "resp-1"


def test_assistant_missing_key_is_deterministic(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assistant = HalcyonAssistant(lambda name, arguments: {})

    result = assistant.ask("turn the tone up")

    assert result.text == DISABLED_MESSAGE
    assert not result.enabled


def test_assistant_surfaces_tool_errors() -> None:
    class ErrorResponses:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    id="resp-1",
                    output=[
                        {
                            "type": "function_call",
                            "name": "set_synth_param",
                            "arguments": '{"param":"tone","value":"bad"}',
                            "call_id": "call-1",
                        }
                    ],
                    output_text="",
                )
            return SimpleNamespace(id="resp-2", output=[], output_text="")

    def executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name == "get_synth_state":
            return {}
        raise ValueError("value must be a number")

    assistant = HalcyonAssistant(
        executor,
        api_key="sk-test",
        client=SimpleNamespace(responses=ErrorResponses()),
    )

    result = assistant.ask("break it")

    assert result.text == "Tool error: value must be a number"
