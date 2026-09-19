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
    assert '"teach/play this melody" is coach' in prompt
    assert '"make the synth sound like this artist/song" is a /synth patch' in prompt
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


def test_router_rejects_whole_plan_when_one_model_command_is_unknown() -> None:
    payload = {
        "action": "menu",
        "stages": [["/synth set brightness 0.7", "/synth set tone 0.7"]],
        "reason": "mixed",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing("set some params and also do another", api_key="sk-test")

    assert decision.action == "reject"
    assert decision.stages == ()
    assert decision.reason == "Routing output contained an invalid registered command."


def test_router_rejects_bad_arguments_without_partial_execution() -> None:
    payload = {
        "action": "menu",
        "stages": [["/synth set tone 2.0", "/delete files"]],
        "reason": "bad",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing("turn it all the way up and clean files", api_key="sk-test")

    assert decision.action == "reject"
    assert decision.commands == ()
    assert decision.reason == "Routing output contained an invalid registered command."


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


def test_router_maps_melody_learning_to_one_umbrella_coach_teach() -> None:
    payload = {
        "action": "menu",
        "stages": [
            [
                '/coach teach "Aphex Twin Avril 14th" --segment "main melody" '
                "--tempo 70 --loops 2 --author bounded"
            ]
        ],
        "reason": "reuse or create one local lesson",
    }
    llm = _llm_returning(payload)

    with patch(_LLM, llm):
        decision = run_chat_routing(
            "Teach me Aphex Twin's Avril 14th main melody",
            api_key="sk-test",
        )

    prompt = llm.call_args.args[0]
    assert "Lesson resolution is cache-first" in prompt
    assert "omit --author or emit --author bounded" in prompt
    assert "--author never only when the user explicitly asks for saved/cache-only" in prompt
    assert "keep the artist and work title in QUERY" in prompt
    assert "Every --author bounded command" in prompt
    assert decision.action == "menu"
    assert decision.commands == (
        '/coach teach "Aphex Twin Avril 14th" --segment "main melody" '
        "--tempo 70 --loops 2 --author bounded",
    )


def test_router_allows_ordinary_personal_use_cache_miss_authoring() -> None:
    payload = {
        "action": "menu",
        "stages": [
            [
                '/coach teach "Aphex Twin Avril 14th" --segment "main melody" '
                "--tempo 70 --author bounded"
            ]
        ],
        "reason": "teach with one reusable local approximation",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing(
            "Help me learn Aphex Twin's Avril 14th main melody",
            api_key="sk-test",
        )

    assert decision.action == "menu"
    assert decision.commands == (
        '/coach teach "Aphex Twin Avril 14th" --segment "main melody" --tempo 70 --author bounded',
    )


def test_router_allows_explicit_cache_only_opt_out() -> None:
    payload = {
        "action": "menu",
        "stages": [['/coach teach "Aphex Twin Avril 14th" --segment "main melody" --author never']],
        "reason": "use a saved lesson only",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing(
            "Use only my saved Avril 14th main melody lesson; do not generate one.",
            api_key="sk-test",
        )

    assert decision.action == "menu"
    assert decision.commands == (
        '/coach teach "Aphex Twin Avril 14th" --segment "main melody" --author never',
    )


def test_router_rejects_default_authoring_without_a_segment() -> None:
    payload = {
        "action": "menu",
        "stages": [['/coach teach "Aphex Twin Avril 14th" --author bounded']],
        "reason": "model omitted the required segment",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing(
            "Teach me Avril 14th",
            api_key="sk-test",
        )

    assert decision.action == "reject"
    assert decision.commands == ()
    assert decision.reason == (
        'Bounded lesson authoring requires an explicit segment such as --segment "main melody".'
    )


def test_router_rejects_whole_dependent_multi_coach_plan() -> None:
    payload = {
        "action": "menu",
        "stages": [
            ["/coach teach Test Melody --author never"],
            ["/coach replay", "/coach attempt", "/coach feedback"],
        ],
        "reason": "incorrectly expanded workflow",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing("teach and score this melody", api_key="sk-test")

    assert decision.action == "reject"
    assert decision.stages == ()
    assert decision.reason == "Coach routing must use one state-mutating command per request."


def test_router_rejects_entire_output_when_any_coach_command_is_invalid() -> None:
    payload = {
        "action": "menu",
        "stages": [["/coach attempt --count-in 99", "/synth set tone 0.4"]],
        "reason": "mixed invalid coach output",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing("practice then darken", api_key="sk-test")

    assert decision.action == "reject"
    assert decision.commands == ()
    assert decision.reason == "Coach routing output contained an invalid registered command."


def test_router_atomically_rejects_invalid_feedback_identifier_in_mixed_plan() -> None:
    invalid_attempt_id = "x" * 129
    payload = {
        "action": "menu",
        "stages": [[f"/coach feedback {invalid_attempt_id}", "/coach state"]],
        "reason": "invalid feedback identifier must not leave a partial plan",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing("show feedback and coach state", api_key="sk-test")

    assert decision.action == "reject"
    assert decision.commands == ()
    assert decision.reason == "Coach routing output contained an invalid registered command."


def test_router_allows_one_mutating_coach_command_plus_read_only_state() -> None:
    payload = {
        "action": "menu",
        "stages": [["/coach state", "/coach replay --loops 2"]],
        "reason": "inspect then replay",
    }

    with patch(_LLM, _llm_returning(payload)):
        decision = run_chat_routing("check state and replay twice", api_key="sk-test")

    assert decision.action == "menu"
    assert decision.commands == ("/coach state", "/coach replay --loops 2")


def test_router_rejects_malformed_or_oversized_plans_all_or_nothing() -> None:
    payloads = [
        {"action": "menu", "stages": ["/panic"], "reason": "bad stage shape"},
        {"action": "menu", "stages": [["/panic", 7]], "reason": "non-string"},
        {
            "action": "menu",
            "stages": [["/panic"]] * (router.MAX_ROUTER_STAGES + 1),
            "reason": "too many stages",
        },
        {
            "action": "menu",
            "stages": [["/panic"] * (router.MAX_ROUTER_COMMANDS_PER_STAGE + 1)],
            "reason": "too many commands",
        },
        {
            "action": "menu",
            "stages": [["/preset save " + "x" * router.MAX_ROUTER_COMMAND_CHARS]],
            "reason": "long command",
        },
    ]

    for payload in payloads:
        with patch(_LLM, _llm_returning(payload)):
            decision = run_chat_routing("route adversarial plan", api_key="sk-test")
        assert decision.action == "reject"
        assert decision.commands == ()


def test_router_bounds_user_response_and_reason() -> None:
    llm = _llm_returning(
        {
            "action": "menu",
            "stages": [["/panic"]],
            "reason": "r" * (router.MAX_ROUTER_REASON_CHARS + 200),
        }
    )
    message = "u" * router.MAX_ROUTER_USER_MESSAGE_CHARS + "PRIVATE_SUFFIX"

    with patch(_LLM, llm):
        decision = run_chat_routing(message, api_key="sk-test")

    prompt = llm.call_args.args[0]
    assert "PRIVATE_SUFFIX" not in prompt
    assert len(decision.reason) == router.MAX_ROUTER_REASON_CHARS

    oversized = json.dumps({"action": "menu", "stages": [["/panic"]], "reason": "ok"}).ljust(
        router.MAX_ROUTER_RESPONSE_CHARS + 1
    )
    with patch(_LLM, MagicMock(return_value=oversized)):
        rejected = run_chat_routing("panic", api_key="sk-test")
    assert rejected.action == "reject"
    assert rejected.reason == "Could not route request."


def test_router_context_is_recursively_bounded_private_and_keyboard_safe() -> None:
    payload = {"action": "menu", "stages": [["/coach state"]], "reason": "state"}
    llm = _llm_returning(payload)
    app_context = {
        "keyboard": {"pressed_count": 2},
        "keyboard_layout": "piano",
        "event_count": 9,
        "recent_midi": [{"note": 60}],
        "nested": {
            "observed_events": [{"note": 61}],
            "raw_history": ["PRIVATE_HISTORY"],
            "source_port": "PRIVATE_PORT",
            "provider_prompt": "PRIVATE_PROMPT",
            "credentials": "PRIVATE_CREDENTIAL",
            "openai_api_key": "PRIVATE_KEY",
            "apiKey": "PRIVATE_CAMEL_KEY",
            "access_token": "PRIVATE_TOKEN",
            "sourcePort": "PRIVATE_CAMEL_PORT",
            "password": "PRIVATE_PASSWORD",
            "filesystem_path": "/PRIVATE/PATH",
            "safe_string": "s" * router.MAX_CONTEXT_STRING_CHARS + "PRIVATE_TAIL",
            "safe_list": list(range(router.MAX_CONTEXT_LIST_ITEMS + 5)),
        },
    }

    with patch(_LLM, llm):
        decision = run_chat_routing(
            "show coach state",
            api_key="sk-test",
            app_context=app_context,
        )

    prompt = llm.call_args.args[0]
    assert '"keyboard":{"pressed_count":2}' in prompt
    assert '"keyboard_layout":"piano"' in prompt
    assert '"event_count":9' in prompt
    for private_value in (
        "PRIVATE_HISTORY",
        "PRIVATE_PORT",
        "PRIVATE_PROMPT",
        "PRIVATE_CREDENTIAL",
        "PRIVATE_KEY",
        "PRIVATE_CAMEL_KEY",
        "PRIVATE_TOKEN",
        "PRIVATE_CAMEL_PORT",
        "PRIVATE_PASSWORD",
        "/PRIVATE/PATH",
        "PRIVATE_TAIL",
    ):
        assert private_value not in prompt
    assert '"safe_list":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]' in prompt
    assert decision.commands == ("/coach state",)


def test_router_context_normalizes_aliases_and_preserves_only_safe_coach_aggregates() -> None:
    marker = "ROUTER-PRIVATE-MARKER"
    payload = {"action": "menu", "stages": [["/coach state"]], "reason": "state"}
    llm = _llm_returning(payload)
    alias_groups = (
        (
            "event_log",
            "event_logs",
            "eventLog",
            "eventLogs",
            "event_history",
            "event_histories",
            "eventHistory",
            "eventHistories",
            "history",
            "histories",
            "log",
            "logs",
            "midi_note",
            "midi_notes",
            "midiNote",
            "midiNotes",
            "midi_message",
            "midi_messages",
            "midiMessage",
            "midiMessages",
        ),
        (
            "client_credential",
            "client_credentials",
            "clientCredential",
            "clientCredentials",
            "private_key",
            "private_keys",
            "privateKey",
            "privateKeys",
            "receipt_state",
            "receipt_states",
            "receiptState",
            "receiptStates",
            "dispatch_result",
            "dispatch_results",
            "dispatchResult",
            "dispatchResults",
            "admission_state",
            "admission_states",
            "admissionState",
            "admissionStates",
        ),
        (
            "provider_request",
            "provider_requests",
            "providerRequest",
            "providerRequests",
            "provider_proposal",
            "provider_proposals",
            "providerProposal",
            "providerProposals",
            "provider_response",
            "provider_responses",
            "providerResponse",
            "providerResponses",
            "provider_output",
            "provider_outputs",
            "providerOutput",
            "providerOutputs",
            "openai_provider_response",
            "openaiProviderResponse",
            "authoring_request",
            "authoringRequest",
        ),
        (
            "receipt_count",
            "required_receipt_count",
            "admission_count",
            "dispatch_count",
            "queue_depth",
            "log_count",
            "history_count",
            "midi_event_count",
        ),
    )
    app_context = {
        "attempt_id": "attempt-safe",
        "lesson_id": "lesson-safe",
        "event_count": 9,
        "eventCount": 10,
        "keyboard": {"keys": ["C4", "E4"], "key_signature": "C"},
        "keys": ["C4", "E4"],
        "key_signature": "C",
        "coach": {
            "authored": True,
            "fidelity": {"timing": 0.97, "pitch": 1.0},
            "provenance": {"label": "curated-catalog"},
        },
        "report": {"metrics": {"overall_score_percent": 91}},
        "nested_aliases": [
            {f"group_{index}": {alias: marker for alias in aliases}}
            for index, aliases in enumerate(alias_groups)
        ],
        "unsafe_aggregate": {"event_count": marker},
    }

    with patch(_LLM, llm):
        decision = run_chat_routing(
            "show coach state",
            api_key="sk-test",
            app_context=app_context,
        )

    prompt = llm.call_args.args[0]
    assert '"attempt_id":"attempt-safe"' in prompt
    assert '"lesson_id":"lesson-safe"' in prompt
    assert '"event_count":9' in prompt
    assert '"eventCount":10' in prompt
    assert '"keyboard":{"key_signature":"C","keys":["C4","E4"]}' in prompt
    assert '"overall_score_percent":91' in prompt
    assert '"authored":true' in prompt
    assert '"fidelity":{"pitch":1.0,"timing":0.97}' in prompt
    assert '"provenance":{"label":"curated-catalog"}' in prompt
    assert marker not in prompt
    for blocked_key in alias_groups[-1]:
        assert f'"{blocked_key}"' not in prompt
    assert decision.commands == ("/coach state",)


def test_router_context_caps_dict_items_and_chat_history() -> None:
    payload = {"action": "menu", "stages": [["/panic"]], "reason": "panic"}
    llm = _llm_returning(payload)
    app_context = {f"safe_{index:02d}": index for index in range(50)}
    history = [
        {"role": "user", "content": f"history-{index}"}
        for index in range(router.MAX_CHAT_CONTEXT_ITEMS + 2)
    ]

    with patch(_LLM, llm):
        run_chat_routing(
            "panic",
            api_key="sk-test",
            app_context=app_context,
            chat_context=history,
        )

    prompt = llm.call_args.args[0]
    assert '"safe_39":39' in prompt
    assert '"safe_40":40' not in prompt
    assert "history-0" not in prompt
    assert "history-1" not in prompt
    assert "history-2" in prompt


def test_router_typeerror_fallback_keeps_json_mode_and_token_bound() -> None:
    create = MagicMock(
        side_effect=[
            TypeError("legacy token keyword"),
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "menu",
                                    "stages": [["/panic"]],
                                    "reason": "panic",
                                }
                            )
                        }
                    }
                ]
            },
        ]
    )
    client = MagicMock()
    client.chat.completions.create = create

    decision = run_chat_routing("panic", client=client)

    assert decision.commands == ("/panic",)
    fallback = create.call_args_list[1].kwargs
    assert fallback["max_tokens"] == 500
    assert fallback["response_format"] == {"type": "json_object"}
