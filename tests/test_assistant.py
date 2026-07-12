import json
from unittest.mock import MagicMock, patch

from halcyon.assistant import DISABLED_MESSAGE, HalcyonAssistant, openai_tool_definitions
from halcyon.chat_router import MISSING_KEY_REASON
from halcyon.menu import load_menu

_LLM = "halcyon.chat_router._call_router_llm"


def test_openai_tool_definitions_expose_no_raw_synth_tools() -> None:
    assert openai_tool_definitions() == []


def test_assistant_returns_routing_decision_from_mocked_openai() -> None:
    payload = {
        "action": "menu",
        "stages": [["/synth patch tone=0.85 release=0.05"]],
        "reason": "brighter and shorter",
    }
    llm = MagicMock(return_value=json.dumps(payload))
    assistant = HalcyonAssistant(menu_def=load_menu(), api_key="sk-test", client=object())

    with patch(_LLM, llm):
        result = assistant.ask("make it brighter and shorter")

    assert result.text == "Routing to `/synth patch tone=0.85 release=0.05`."
    assert result.decision.commands == ("/synth patch tone=0.85 release=0.05",)
    assert result.tool_results == []
    assert result.enabled


def test_assistant_passes_live_context_to_router_prompt() -> None:
    payload = {
        "action": "menu",
        "stages": [["/preset save Deep Time"]],
        "reason": "save current patch",
    }
    llm = MagicMock(return_value=json.dumps(payload))
    assistant = HalcyonAssistant(
        menu_def=load_menu(),
        api_key="sk-test",
        client=object(),
        context_provider=lambda: {
            "patch": {"preset": None, "params": [{"id": "tone", "normalized": 0.22}]},
            "presets": {"built_in": ["Init"], "user": []},
        },
    )

    with patch(_LLM, llm):
        result = assistant.ask("save this preset as Deep Time")

    prompt = llm.call_args.args[0]
    assert '"normalized":0.22' in prompt
    assert '"user":[]' in prompt
    assert result.decision.commands == ("/preset save Deep Time",)


def test_assistant_passes_recent_chat_context_to_router_prompt() -> None:
    llm = MagicMock(
        side_effect=[
            json.dumps(
                {
                    "action": "menu",
                    "stages": [["/synth patch tone=0.34 release=0.58"]],
                    "reason": "initial patch",
                }
            ),
            json.dumps(
                {
                    "action": "menu",
                    "stages": [["/synth set tone 0.22"]],
                    "reason": "relative adjustment",
                }
            ),
        ]
    )
    assistant = HalcyonAssistant(menu_def=load_menu(), api_key="sk-test", client=object())

    with patch(_LLM, llm):
        assistant.ask("make a Deep Time patch")
        result = assistant.ask("make it darker")

    second_prompt = llm.call_args_list[1].args[0]
    assert "make a Deep Time patch" in second_prompt
    assert "/synth patch tone=0.34 release=0.58" in second_prompt
    assert result.decision.commands == ("/synth set tone 0.22",)


def test_assistant_missing_key_is_deterministic(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assistant = HalcyonAssistant(menu_def=load_menu())

    result = assistant.ask("do the subtle thing and also another subtle thing")

    assert result.text == MISSING_KEY_REASON
    assert result.text == DISABLED_MESSAGE
    assert result.decision.action == "reject"
    assert not result.enabled


def test_assistant_natural_language_requires_openai_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assistant = HalcyonAssistant(menu_def=load_menu())

    result = assistant.ask("all notes off")

    assert result.text == MISSING_KEY_REASON
    assert result.decision.action == "reject"
    assert not result.enabled
