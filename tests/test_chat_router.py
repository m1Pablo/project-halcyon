import json
from unittest.mock import MagicMock, patch

import halcyon.chat_router as router
from halcyon.chat_router import RoutingDecision, run_chat_routing

_LLM = "halcyon.chat_router._call_router_llm"


def _llm_returning(payload: dict[str, object]) -> MagicMock:
    return MagicMock(return_value=json.dumps(payload))


def test_routing_decision_commands_flattens_stages() -> None:
    decision = RoutingDecision(
        action="menu",
        stages=(("/synth set tone 0.8",), ("/synth set release 0.05", "/panic")),
    )

    assert decision.commands == (
        "/synth set tone 0.8",
        "/synth set release 0.05",
        "/panic",
    )


def test_router_requires_openai_for_natural_language_without_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm = MagicMock()

    with patch(_LLM, llm):
        decision = run_chat_routing("panic")

    llm.assert_not_called()
    assert decision.action == "reject"
    assert decision.reason == router.MISSING_KEY_REASON


def test_router_parses_mocked_openai_json_and_preserves_valid_args() -> None:
    payload = {
        "action": "menu",
        "stages": [["/synth patch tone=0.85 release=0.05"]],
        "reason": "brighter and shorter",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing("make it brighter and shorter", api_key="sk-test")

    assert decision.action == "menu"
    assert decision.stages == (("/synth patch tone=0.85 release=0.05",),)
    assert decision.reason == "brighter and shorter"


def test_router_prompts_model_to_synthesize_subjective_patch() -> None:
    payload = {
        "action": "menu",
        "stages": [
            [
                "/synth patch tone=0.34 shape=0.32 attack=0.20 decay=0.44 "
                "sustain=0.70 release=0.58 drive=0.14"
            ]
        ],
        "reason": "best-effort inspired patch",
    }
    llm = _llm_returning(payload)

    with patch(_LLM, llm):
        decision = run_chat_routing(
            'Make it sound like Boards of Canada, not a preset called "Boards of Canada"',
            api_key="sk-test",
        )

    prompt = llm.call_args.args[0]
    assert "Treat subjective musical language as valid synth work" in prompt
    assert "Do not require a matching preset" in prompt
    assert "Treat volume as output trim" in prompt
    assert "preserve the current volume" in prompt
    assert "tone: Tone" in prompt
    assert "lower is darker/warmer" in prompt
    assert "volume: Volume" in prompt
    assert "preserve by omitting it" in prompt
    assert decision.action == "menu"
    assert decision.commands == (
        "/synth patch tone=0.34 shape=0.32 attack=0.20 decay=0.44 "
        "sustain=0.70 release=0.58 drive=0.14",
    )


def test_router_uses_model_for_preset_save_name_extraction() -> None:
    payload = {
        "action": "menu",
        "stages": [["/preset save Deep Time"]],
        "reason": "save current patch under requested name",
    }
    llm = _llm_returning(payload)

    with patch(_LLM, llm):
        decision = run_chat_routing("save this preset as Deep Time", api_key="sk-test")

    prompt = llm.call_args.args[0]
    assert "save this preset as Deep Time" in prompt
    assert "save this preset" not in decision.commands[0]
    assert decision.commands == ("/preset save Deep Time",)


def test_router_prompt_includes_app_and_chat_context() -> None:
    payload = {
        "action": "menu",
        "stages": [["/synth set tone 0.18"]],
        "reason": "darker relative adjustment",
    }
    llm = _llm_returning(payload)

    with patch(_LLM, llm):
        decision = run_chat_routing(
            "make it darker than that",
            api_key="sk-test",
            app_context={"patch": {"params": [{"id": "tone", "normalized": 0.34}]}},
            chat_context=[
                {"role": "user", "content": "make a Deep Time patch"},
                {"role": "assistant", "content": "Routing to `/synth patch tone=0.34`."},
            ],
        )

    prompt = llm.call_args.args[0]
    assert '"normalized":0.34' in prompt
    assert "make a Deep Time patch" in prompt
    assert decision.commands == ("/synth set tone 0.18",)


def test_router_drops_invalid_model_commands() -> None:
    payload = {
        "action": "menu",
        "stages": [["/synth set brightness 0.7", "/synth set tone 0.7"]],
        "reason": "mixed",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing("set some params and also do another", api_key="sk-test")

    assert decision.action == "menu"
    assert decision.stages == (("/synth set tone 0.7",),)


def test_router_drops_bad_arguments_and_rejects_when_all_invalid() -> None:
    payload = {
        "action": "menu",
        "stages": [["/synth set tone 2.0", "/delete files"]],
        "reason": "bad",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing("turn it all the way up and clean files", api_key="sk-test")

    assert decision.action == "reject"
    assert decision.reason == "No valid command selected."


def test_router_reject_action_surfaces_reason() -> None:
    payload = {"action": "reject", "stages": [], "reason": "not a synth request"}

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing("what is the weather and also tell me news", api_key="sk-test")

    assert decision.action == "reject"
    assert decision.reason == "not a synth request"


def test_router_missing_api_key_rejects_natural_language(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    decision = run_chat_routing("do the subtle thing and also another subtle thing")

    assert decision.action == "reject"
    assert decision.reason == router.MISSING_KEY_REASON
