import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from halcyon.agent import MAX_STEPS, public_context, run_agent
from halcyon.menu import load_menu


def call(command: str, *, name: str = "run_slash_command", arguments: str | None = None) -> dict:
    return {
        "type": "function_call",
        "name": name,
        "call_id": "call1",
        "arguments": arguments if arguments is not None else json.dumps({"command": command}),
        "status": "completed",
    }


def response(
    *items: dict, text: str = "", status: str = "completed", tokens: int = 10
) -> SimpleNamespace:
    return SimpleNamespace(
        output=list(items),
        output_text=text,
        status=status,
        usage=SimpleNamespace(total_tokens=tokens),
    )


def run(client, control=None, **kwargs):
    return run_agent(
        "help me",
        client=client,
        model="configured-model",
        menu_def=load_menu(),
        context=kwargs.pop("context", {}),
        history=kwargs.pop("history", []),
        control=control or MagicMock(return_value={"ok": True}),
        stopped=kwargs.pop("stopped", lambda: False),
        progress=kwargs.pop("progress", lambda _: None),
        **kwargs,
    )


def test_result_feedback_changes_next_step_and_reasoning_is_replayed():
    client = MagicMock()
    reasoning = {
        "type": "reasoning",
        "id": "r1",
        "summary": [],
        "encrypted_content": "encrypted",
        "status": "completed",
    }
    client.responses.create.side_effect = [
        response(reasoning, call("/coach replay --tempo 50"), text="I will replay more slowly."),
        response(call("/coach state")),
        response(text="No lesson is loaded. Which song should we use?"),
    ]
    control = MagicMock(
        side_effect=[
            {"ok": False, "status": "failed", "error": "No lesson loaded."},
            {"coach": {"phase": "idle"}},
        ]
    )
    result = run(client, control)
    assert result.stopped_reason is None
    assert len(result.tool_results) == 2
    assert result.tool_results[0]["ok"] is False
    assert control.call_args_list[1].args == ("run_slash_command", {"command": "/coach state"})
    inputs = client.responses.create.call_args_list[1].kwargs["input"]
    assert "No lesson loaded." in inputs[-1]["output"]
    assert inputs[1] == {
        key: reasoning[key] for key in ("type", "id", "summary", "encrypted_content")
    }
    assert "status" not in inputs[2]
    assert client.responses.create.call_args.kwargs["store"] is False
    assert client.responses.create.call_args.kwargs["model"] == "configured-model"
    assert client.responses.create.call_args.kwargs["parallel_tool_calls"] is False


@pytest.mark.parametrize(
    "item",
    [
        call("/assistant ask 'do it again'"),
        call("/shell rm something"),
        call("/synth set tone 2"),
        call("", name="set_synth_param"),
        call("", arguments="not-json"),
        call("", arguments="[]"),
        call("", name="get_app_state", arguments='{"extra": true}'),
    ],
)
def test_invalid_or_raw_tools_never_execute_and_receive_recoverable_errors(item):
    client = MagicMock()
    client.responses.create.side_effect = [response(item), response(text="I cannot do that.")]
    control = MagicMock()
    result = run(client, control)
    control.assert_not_called()
    assert result.tool_results[0]["ok"] is False
    assert "error" in client.responses.create.call_args.kwargs["input"][-1]["output"]


def test_multiple_calls_do_not_execute_in_parallel_or_partially():
    client = MagicMock()
    client.responses.create.side_effect = [
        response(call("/panic"), call("/coach attempt")),
        response(text="Please choose one action."),
    ]
    control = MagicMock()
    run(client, control)
    control.assert_not_called()


def test_step_limit_stops_repeating_mutations():
    client = MagicMock()
    client.responses.create.return_value = response(call("/synth state"))
    control = MagicMock(return_value={"ok": True})
    result = run(client, control)
    assert result.stopped_reason == "step_limit"
    assert control.call_count == MAX_STEPS


def test_no_actions_after_timeout_during_model_request():
    client = MagicMock()
    ended = False

    def model(**_kwargs):
        nonlocal ended
        ended = True
        return response(call("/coach attempt"))

    client.responses.create.side_effect = model
    control = MagicMock()
    result = run(client, control, stopped=lambda: ended)
    assert result.stopped_reason == "cancelled_or_timed_out"
    control.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_incomplete_response_never_executes_embedded_mutation(status):
    client = MagicMock()
    client.responses.create.return_value = response(call("/panic"), status=status)
    control = MagicMock()
    assert run(client, control).stopped_reason == "model_response_incomplete"
    control.assert_not_called()


def test_credentials_paths_raw_notes_and_provider_errors_are_not_forwarded():
    client = MagicMock()
    client.responses.create.side_effect = RuntimeError("sk-private-token /Users/private/person")
    result = run(
        client,
        secret="not-a-standard-secret",
        context={
            "api_key": "not-a-standard-secret",
            "events": [{"note": 60}],
            "audio": {"status": "ready"},
            "hint": "sk-other-key /Users/private/file",
        },
        history=[{"role": "user", "content": "not-a-standard-secret"}],
    )
    assert result.stopped_reason == "model_unavailable"
    serialized = str(client.responses.create.call_args.kwargs)
    assert "not-a-standard-secret" not in serialized
    assert "sk-other-key" not in serialized
    assert "/Users/private" not in serialized
    assert "note': 60" not in serialized
    assert "audio" in serialized


def test_feedback_retains_aggregate_scores_without_raw_events():
    context = public_context(
        {
            "result": {
                "report": {
                    "metrics": {"pitch_accuracy_percent": 81},
                    "observed_events": [{"note": 60}],
                }
            }
        }
    )
    assert context["result"]["report"]["metrics"]["pitch_accuracy_percent"] == 81
    assert "observed_events" not in str(context)


def saved_library():
    return [
        {
            "title": "Avril 14th",
            "composer": "Aphex Twin",
            "query": "aphex twin avril 14th",
            "aliases": ["aphex twin avril 14th", "aphex twin s avril 14th"],
            "acquisition": "user_supplied",
            "fidelity": "verified",
            "practice_levels": ["melody", "essentials", "full"],
            "segments": ["main melody"],
            "segment_hands": {"main melody": "right"},
            "segment_difficulties": {"main melody": None},
        },
        {
            "title": "Gymnopédie No. 1",
            "composer": "Erik Satie",
            "query": "gymnopedie no 1",
            "aliases": ["gymnopedie no 1", "erik satie gymnopédie no 1"],
            "acquisition": "user_supplied",
            "fidelity": "verified",
            "practice_levels": ["melody", "essentials", "full"],
            "segments": ["complete piece"],
            "segment_hands": {"complete piece": "both"},
            "segment_difficulties": {"complete piece": "standard"},
        },
    ]


def test_model_observes_saved_songs_queries_segments_and_selector_support():
    client = MagicMock()
    client.responses.create.return_value = response(text="Your two imported songs are available.")
    library = saved_library()
    library[0]["events"] = [{"note": 60}]
    library[0]["citation"] = "sk-hidden-key /Users/private/source.mid"
    context = {
        "lesson_library": library,
        "patch": {f"parameter_{i}": i / 100 for i in range(100)},
        "coach": {"phase": "ready", "fidelity": "approximate"},
    }
    run(client, context=context)
    instructions = client.responses.create.call_args.kwargs["instructions"]
    observed = json.loads(instructions.split("\nLive state (data):\n")[1])
    assert observed["lesson_library"][0]["query"] == "aphex twin avril 14th"
    assert observed["lesson_library"][0]["segments"] == ["main melody"]
    assert observed["lesson_library"][0]["segment_difficulties"] == {"main melody": None}
    assert observed["lesson_library"][1]["title"] == "Gymnopédie No. 1"
    assert observed["lesson_library"][1]["segments"] == ["complete piece"]
    assert observed["lesson_library"][1]["practice_levels"] == ["melody", "essentials", "full"]
    assert observed["lesson_library"][1]["aliases"] == saved_library()[1]["aliases"]
    assert "'note': 60" not in repr(observed)
    assert "events" not in observed["lesson_library"][0]
    assert "sk-hidden-key" not in instructions
    assert "/Users/private" not in instructions
    assert "Only use --author bounded when the user" in instructions
    assert "Authoring\nlimits apply to generation, not playback of imported MIDI" in instructions
    assert "or extra beginner usually means slower tempo" in instructions
    assert "do not equate it with foundation difficulty" in instructions
    assert "Note complexity is separate from tempo and --difficulty" in instructions
    assert "only if the saved source supports it" in instructions
    assert "Ghosts are dim optional source notes" in instructions
    assert "A melody-only source may have no additional notes to ghost" in instructions


def test_fake_agent_uses_supported_satie_practice_layer_without_generating_replacement():
    command = (
        '/coach teach "gymnopedie no 1" --segment "complete piece" '
        "--tempo 25 --practice melody --author never"
    )
    client = MagicMock()

    def model(**kwargs):
        observed = json.loads(kwargs["instructions"].split("\nLive state (data):\n")[1])
        assert "melody" in observed["lesson_library"][1]["practice_levels"]
        return response(call(command))

    model_calls = 0

    def choose_then_reply(**kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return model(**kwargs)
        return response(
            text="Playing saved melody targets; the remaining notes are optional ghosts."
        )

    client.responses.create.side_effect = choose_then_reply
    control = MagicMock(return_value={"ok": True, "status": "completed"})

    result = run(client, control, context={"lesson_library": saved_library()})

    control.assert_called_once_with("run_slash_command", {"command": command})
    assert result.stopped_reason is None
    assert "optional ghosts" in result.text


def test_fake_agent_uses_imported_avril_query_and_slows_without_unsupported_difficulty():
    command = (
        '/coach teach "aphex twin avril 14th" --segment "main melody" '
        "--tempo 25 --hand right --loops 2 --author never"
    )
    client = MagicMock()

    def choose_from_visible_library(**kwargs):
        library = json.loads(kwargs["instructions"].split("\nLive state (data):\n")[1])[
            "lesson_library"
        ]
        assert library[0]["acquisition"] == "user_supplied"
        assert library[0]["query"] in command
        assert library[0]["segments"][0] in command
        assert library[0]["segment_hands"]["main melody"] == "right"
        assert library[0]["segment_difficulties"]["main melody"] is None
        return response(call(command))

    calls = 0

    def model(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return choose_from_visible_library(**kwargs)
        return response(text="Playing your imported melody at 25%, without changing its notes.")

    client.responses.create.side_effect = model
    control = MagicMock(return_value={"ok": True, "status": "completed"})
    result = run(
        client,
        control,
        context={"lesson_library": saved_library()},
        history=[{"role": "assistant", "content": "The old Avril 14 lesson was approximate."}],
    )
    assert result.stopped_reason is None
    control.assert_called_once_with("run_slash_command", {"command": command})
    assert "25%" in result.text


def test_fake_agent_recovers_unknown_segment_from_refreshed_library():
    client = MagicMock()
    good_command = '/coach teach "aphex twin avril 14th" --segment "main melody" --author never'
    client.responses.create.side_effect = [
        response(call('/coach teach "aphex twin avril 14th" --segment "full song"')),
        response(call("", name="get_app_state", arguments="{}")),
        response(call(good_command)),
        response(text="The saved import is main melody; that is the available coverage."),
    ]
    control = MagicMock(
        side_effect=[
            {"ok": False, "error": "NO_MATCHING_SEGMENT"},
            {"lesson_library": saved_library()},
            {"ok": True, "status": "completed"},
        ]
    )
    result = run(client, control)
    assert result.stopped_reason is None
    assert control.call_args_list[1].args == ("get_app_state", {})
    assert control.call_args_list[2].args == ("run_slash_command", {"command": good_command})
    refreshed = json.loads(client.responses.create.call_args_list[2].kwargs["input"][-1]["output"])
    assert refreshed["lesson_library"][0]["segments"] == ["main melody"]


def test_uncertain_execution_stops_instead_of_blindly_retrying():
    client = MagicMock()
    client.responses.create.return_value = response(call("/coach attempt"))
    control = MagicMock(side_effect=RuntimeError("private operational error"))
    result = run(client, control)
    assert result.stopped_reason == "control_unavailable"
    assert result.tool_results[0]["ok"] is False
    control.assert_called_once()
    client.responses.create.assert_called_once()


def test_token_limit_prevents_next_action():
    client = MagicMock()
    client.responses.create.return_value = response(call("/panic"), tokens=32_000)
    control = MagicMock()
    assert run(client, control).stopped_reason == "token_limit"
    control.assert_not_called()


def test_replayed_sdk_items_match_the_responses_input_schema_without_output_only_fields():
    from openai.types.responses.response_input_param import ResponseInputParam
    from pydantic import TypeAdapter
    from halcyon.agent import _replay

    reasoning = SimpleNamespace(
        type="reasoning",
        id="rs1",
        summary=[SimpleNamespace(text="plan")],
        encrypted_content="opaque",
        status="completed",
    )
    message = SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", text="I will inspect state.")],
        phase="commentary",
        status="completed",
    )
    items = [_replay(reasoning), _replay(message), _replay(call("/synth state"))]
    TypeAdapter(ResponseInputParam).validate_python(items)
    json.dumps(items)
    assert items[1] == {
        "role": "assistant",
        "content": "I will inspect state.",
        "phase": "commentary",
    }
    assert all("status" not in item for item in items)
