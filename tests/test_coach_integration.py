from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
import threading
import time
from types import SimpleNamespace
from typing import Callable

import pytest

from halcyon.app import HalcyonApp
from halcyon.audio_bridge import (
    AudioBridgeSnapshot,
    CommandAdmission,
    CommandSubmission,
)
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.coach_authoring import LessonService
from halcyon.coach_engine import CoachEngine, CoachPhase, ThreadingDeadlineScheduler
from halcyon.coach_models import (
    Acquisition,
    Fidelity,
    LessonEvent,
    LessonProvenance,
    LessonSegment,
    TempoChange,
    TimeSignature,
    create_lesson_revision,
)
from halcyon.coach_store import LessonStore
from halcyon.command_executor import (
    CommandProgress,
    ImmediateCommandOutcome,
    PendingCommandOutcome,
    TerminalCommandOutcome,
)
from halcyon.event_bus import HalcyonMidiAdapter, MidiEventAdapter
from halcyon.midi import MidiEvent, MidiOrigin


class FakeMidi:
    connected = False

    def __init__(self) -> None:
        self.close_calls = 0

    def list_ports(self) -> list[str]:
        return ["Synthetic MIDI"]

    def connect(self) -> str:
        return "Synthetic MIDI"

    def poll(self) -> list[MidiEvent]:
        return []

    def close(self) -> None:
        self.close_calls += 1


class CountingMidi(FakeMidi):
    def __init__(self) -> None:
        super().__init__()
        self.connect_calls = 0

    def connect(self) -> str:
        self.connect_calls += 1
        return "Synthetic MIDI"


class LegacyAudio:
    def __init__(self, *, status_text: str = "Audio: ready") -> None:
        self.status_text = status_text
        self.events: list[MidiEvent] = []
        self.start_calls = 0
        self.close_calls = 0
        self.panic_calls = 0

    def start(self) -> bool:
        self.start_calls += 1
        return True

    def apply_patch(self, _patch: dict[str, float]) -> bool:
        return True

    def snapshot(self) -> AudioBridgeSnapshot:
        return AudioBridgeSnapshot(running=True, ready=True, text=self.status_text)

    def visualization_snapshot(self) -> AudioVisualizationSnapshot:
        return AudioVisualizationSnapshot(
            socket_path="synthetic",
            sample_count=0,
            connected=False,
            frame_count=0,
            samples=(),
        )

    def handle_midi_event(self, event: MidiEvent) -> bool:
        self.events.append(event)
        return True

    def all_notes_off(self) -> bool:
        self.panic_calls += 1
        return True

    def close(self) -> None:
        self.close_calls += 1


class DeferredAdmissionAudio(LegacyAudio):
    def __init__(self) -> None:
        super().__init__()
        self.accept_admissions = False
        self.next_command_id = 1

    def handle_midi_event_submission(self, event: MidiEvent) -> CommandSubmission:
        self.events.append(event)
        command_id = self.next_command_id
        self.next_command_id += 1
        return CommandSubmission(
            command_id=command_id,
            submitted=True,
            status="pending",
            safety=event.is_release,
        )

    def admission(self, command_id: int) -> CommandAdmission | None:
        if not self.accept_admissions:
            return None
        return CommandAdmission(
            command_id=command_id,
            status="accepted",
            queue="safety" if command_id % 2 == 0 else "normal",
        )


class RecordingChat:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def write_codex(self, text: str) -> None:
        self.messages.append(("codex", text))

    def write_system(self, text: str) -> None:
        self.messages.append(("system", text))


@dataclass
class PendingHarness:
    pending: PendingCommandOutcome
    completion: Future[TerminalCommandOutcome]
    subscribers: list[Callable[[CommandProgress], None]]
    unsubscribe_calls: list[int]


def make_pending() -> PendingHarness:
    completion: Future[TerminalCommandOutcome] = Future()
    subscribers: list[Callable[[CommandProgress], None]] = []
    unsubscribe_calls: list[int] = []

    def subscribe(callback: Callable[[CommandProgress], None]) -> Callable[[], None]:
        subscribers.append(callback)

        def unsubscribe() -> None:
            unsubscribe_calls.append(1)

        return unsubscribe

    pending = PendingCommandOutcome(
        operation_id="operation-test",
        session_id="session-test",
        accepted={
            "ok": True,
            "code": "COACH_ACCEPTED",
            "message": "Coach operation accepted.",
        },
        completion=completion,
        cancel_callback=lambda _reason: True,
        subscribe_callback=subscribe,
    )
    return PendingHarness(pending, completion, subscribers, unsubscribe_calls)


def app_with_legacy_audio(*, status_text: str = "Audio: ready") -> tuple[HalcyonApp, LegacyAudio]:
    app = HalcyonApp()
    audio = LegacyAudio(status_text=status_text)
    app.audio = audio  # type: ignore[assignment]
    app.midi = FakeMidi()  # type: ignore[assignment]
    return app, audio


def synthetic_lesson():  # type: ignore[no-untyped-def]
    return create_lesson_revision(
        lesson_id="synthetic-melody",
        created_at="2026-07-12T00:00:00+00:00",
        aliases=("Synthetic Melody",),
        title="Synthetic Melody",
        composer="Halcyon Tests",
        difficulty="foundation",
        hand="right",
        ppq=100,
        tempo_map=(TempoChange(0, 100_000),),
        time_signatures=(TimeSignature(0, 4, 4),),
        provenance=LessonProvenance(
            acquisition=Acquisition.ORIGINAL,
            citation="Original synthetic integration fixture.",
            fidelity=Fidelity.VERIFIED,
            authorization_timestamp="2026-07-12T00:00:00+00:00",
        ),
        events=(
            LessonEvent(
                event_id="",
                event_type="note_on",
                note=60,
                velocity=90,
                channel=1,
                status=0x90,
                source_port="fixture://synthetic",
                content_origin="original",
                absolute_tick=0,
                delta_tick=0,
            ),
            LessonEvent(
                event_id="",
                event_type="note_off",
                note=60,
                velocity=0,
                channel=1,
                status=0x80,
                source_port="fixture://synthetic",
                content_origin="original",
                absolute_tick=100,
                delta_tick=100,
            ),
        ),
        segments=(LessonSegment("main", 0, 100, hand="right"),),
    )


def install_fast_coach(app: HalcyonApp) -> None:
    app.coach.unmount()
    app.coach = CoachEngine(
        app.lesson_service,
        app.event_bus,
        dispatcher=app._dispatch_to_app,
        scheduler=ThreadingDeadlineScheduler(),
        capture_tail_us=100_000,
        default_count_in=0,
    )


def test_duplicate_event_reaches_one_required_audio_consumer_exactly_once() -> None:
    app, audio = app_with_legacy_audio()
    human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")
    event = human.note_on(60, 90)

    first = app.event_bus.publish(event)
    duplicate = app.event_bus.publish(event)

    assert first.accepted
    assert first.consumer_errors == ()
    assert first.required_receipt_count == 1
    assert len(first.receipts) == 1
    assert first.receipts[0].poll().state == "accepted"
    assert duplicate.deduplicated
    assert duplicate.required_receipt_count == 0
    assert len(audio.events) == 1
    assert len(app.event_log) == 1
    app.on_unmount()

    after_unmount = human.note_on(61, 80)
    dispatched = app.event_bus.publish(after_unmount)
    assert dispatched.consumer_count == 0
    assert len(app.event_log) == 1


def test_same_note_repeated_voices_unmatched_release_clear_and_reconnect() -> None:
    app, audio = app_with_legacy_audio()
    human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")
    unmatched = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")
    halcyon = HalcyonMidiAdapter(source_port="halcyon://coach/test", session_id="coach-1")

    first_human = human.note_on(60, 90)
    second_human = human.note_on(60, 80)
    coach_press = halcyon.note_on(60, 70)
    for event in (first_human, second_human, coach_press):
        app._handle_event(event)

    assert app.human_pressed_notes == {60}
    assert app.halcyon_pressed_notes == {60}
    assert app.pressed_notes == {60}
    assert len(app.event_bus.active_voices(origin=MidiOrigin.HUMAN)) == 2

    app._handle_event(unmatched.note_off(60))
    assert len(app.event_bus.active_voices(origin=MidiOrigin.HUMAN)) == 2

    app._handle_event(human.note_off(60))
    assert app.human_pressed_notes == {60}
    app._handle_event(halcyon.note_off(60))
    assert app.human_pressed_notes == {60}
    assert app.halcyon_pressed_notes == set()

    retained_count = len(app.event_bus.active_voices())
    app.action_clear_log()
    assert len(app.event_log) == 0
    assert len(app.event_bus.active_voices()) == retained_count

    app.action_reconnect()
    assert audio.panic_calls == 1
    assert app.event_bus.active_voices() == ()
    assert app.pressed_notes == set()
    app.on_unmount()


def test_public_state_is_aggregate_only_and_redacts_raw_audio_and_midi() -> None:
    secret = "SECRET_VALUE /private/audio.sock"
    app, _audio = app_with_legacy_audio(status_text=secret)
    human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port=secret)
    app._handle_event(human.note_on(64, 90))

    state = app.synth_tool_state()
    rendered = repr(state)

    assert state["audio"]["status"] == "ready"  # type: ignore[index]
    assert state["keyboard"]["human_voice_count"] == 1  # type: ignore[index]
    assert "recent_midi" not in state
    assert "raw_message" not in rendered
    assert "source_port" not in rendered
    assert "SECRET_VALUE" not in rendered
    assert "/private/audio.sock" not in rendered
    app.on_unmount()


def test_pending_chat_renders_accept_progress_and_one_terminal() -> None:
    app, _audio = app_with_legacy_audio()
    chat = RecordingChat()
    app.chat_panel = chat  # type: ignore[assignment]
    harness = make_pending()
    app._execute_tool_from_worker = (  # type: ignore[method-assign]
        lambda _name, _arguments: harness.pending
    )

    executed = app._execute_chat_slash_command("/coach teach Test Melody --segment main")
    assert executed
    assert chat.messages == [("codex", "Coach accepted. Phase: running.")]

    harness.subscribers[0](
        CommandProgress(
            operation_id="operation-test",
            session_id="session-test",
            phase="demonstrating",
            message="Halcyon is demonstrating the lesson",
            percent=30,
        )
    )
    harness.completion.set_result(
        TerminalCommandOutcome(
            operation_id="operation-test",
            session_id="session-test",
            status="completed",
            public={
                "ok": True,
                "message": "Attempt scored.",
                "report": {
                    "metrics": {
                        "overall_score_percent": 87,
                        "pitch_accuracy_percent": 90,
                        "completion_percent": 100,
                    },
                    "tips": ["Slow the difficult transition."],
                    "raw_events": ["SECRET_VALUE"],
                    "artifact_path": "/private/attempt.json",
                },
            },
        )
    )

    assert chat.messages[-2:] == [
        ("codex", "Coach demonstrating 30%: Halcyon is demonstrating the lesson"),
        (
            "codex",
            "Coach complete: overall 87%, pitch 90%, completion 100%. "
            "Tips: Slow the difficult transition.",
        ),
    ]
    assert "SECRET_VALUE" not in repr(chat.messages)
    assert "/private/attempt.json" not in repr(chat.messages)
    assert harness.unsubscribe_calls == [1]
    app.on_unmount()


def test_immediate_feedback_chat_uses_aggregate_renderer_without_raw_fields() -> None:
    app, _audio = app_with_legacy_audio()
    chat = RecordingChat()
    app.chat_panel = chat  # type: ignore[assignment]
    app._execute_tool_from_worker = (  # type: ignore[method-assign]
        lambda _name, _arguments: ImmediateCommandOutcome(
            {
                "ok": True,
                "message": "Feedback ready.",
                "report": {
                    "metrics": {
                        "overall_score_percent": 92,
                        "pitch_accuracy_percent": 96,
                        "completion_percent": 88,
                    },
                    "tips": ["Keep the pulse even."],
                    "raw_events": ["SECRET_NOTE_STREAM"],
                    "artifact_path": "/private/feedback.json",
                },
            }
        )
    )

    assert app._execute_chat_slash_command("/coach feedback")
    assert chat.messages == [
        (
            "codex",
            "Coach complete: overall 92%, pitch 96%, completion 88%. Tips: Keep the pulse even.",
        )
    ]
    assert "SECRET_NOTE_STREAM" not in repr(chat.messages)
    assert "/private/feedback.json" not in repr(chat.messages)
    app.on_unmount()


def test_chat_ignores_forged_progress_and_future_without_releasing_ownership() -> None:
    app, _audio = app_with_legacy_audio()
    chat = RecordingChat()
    app.chat_panel = chat  # type: ignore[assignment]
    harness = make_pending()
    app._execute_tool_from_worker = (  # type: ignore[method-assign]
        lambda _name, _arguments: harness.pending
    )
    assert app._execute_chat_slash_command("/coach teach Test Melody --segment main")
    before = list(chat.messages)

    harness.subscribers[0](
        CommandProgress(
            operation_id="operation-test",
            session_id="forged-session",
            phase="forged",
            message="SECRET_VALUE /private/progress.json",
            percent=100,
        )
    )
    harness.subscribers[0](object())  # type: ignore[arg-type]
    forged: Future[object] = Future()
    forged.set_result(
        TerminalCommandOutcome(
            operation_id="operation-test",
            session_id="session-test",
            status="completed",
            public={"ok": True, "message": "SECRET_VALUE /private/terminal.json"},
        )
    )
    app._render_chat_terminal("operation-test", "session-test", forged)

    key = ("operation-test", "session-test")
    assert chat.messages == before
    assert key in app._chat_pending_bindings
    assert harness.unsubscribe_calls == []

    harness.completion.set_result(
        TerminalCommandOutcome(
            operation_id="operation-test",
            session_id="session-test",
            status="completed",
            public={"ok": True, "message": "Authoritative completion."},
        )
    )
    assert chat.messages[-1] == ("codex", "Authoritative completion.")
    assert "SECRET_VALUE" not in repr(chat.messages)
    assert "/private/" not in repr(chat.messages)
    assert key not in app._chat_pending_bindings
    assert harness.unsubscribe_calls == [1]
    app.on_unmount()


def test_chat_missing_segment_shows_one_actionable_retry_without_rerunning() -> None:
    app, _audio = app_with_legacy_audio()
    chat = RecordingChat()
    app.chat_panel = chat  # type: ignore[assignment]
    harness = make_pending()
    calls: list[tuple[str, dict[str, object]]] = []

    def execute(name: str, arguments: dict[str, object]) -> PendingCommandOutcome:
        calls.append((name, arguments))
        return harness.pending

    app._execute_tool_from_worker = execute  # type: ignore[method-assign]
    assert app._execute_chat_slash_command("/coach teach Aphex Twin Avril 14th main melody")

    harness.completion.set_result(
        TerminalCommandOutcome(
            operation_id="operation-test",
            session_id="session-test",
            status="failed",
            public={
                "ok": False,
                "error": {
                    "code": "SOURCE_REQUIRED",
                    "message": "Bounded authoring requires an explicit educational segment.",
                    "resolution": (
                        'Name the segment, for example: /coach teach "work title" '
                        '--segment "main melody".'
                    ),
                },
            },
        )
    )

    assert calls == [
        (
            "start_coach_lesson",
            {
                "query": "Aphex Twin Avril 14th main melody",
                "segment": None,
                "tempo_percent": 100,
                "difficulty": None,
                "hand": None,
                "loops": 1,
                "author": "bounded",
            },
        )
    ]
    assert chat.messages[-1] == (
        "codex",
        "Coach needs input [SOURCE_REQUIRED]: Bounded authoring requires an explicit "
        'educational segment. Next: Name the segment, for example: /coach teach "work '
        'title" --segment "main melody".',
    )
    assert ("operation-test", "session-test") not in app._chat_pending_bindings
    app.on_unmount()


def test_authoritative_invalid_chat_future_cleans_up_with_fixed_safe_failure() -> None:
    app, _audio = app_with_legacy_audio()
    chat = RecordingChat()
    app.chat_panel = chat  # type: ignore[assignment]
    harness = make_pending()
    app._execute_tool_from_worker = (  # type: ignore[method-assign]
        lambda _name, _arguments: harness.pending
    )
    assert app._execute_chat_slash_command("/coach teach Test Melody --segment main")

    harness.completion.set_result(  # type: ignore[arg-type]
        {"message": "SECRET_VALUE /private/invalid-result.json"}
    )

    assert chat.messages[-1] == (
        "codex",
        "Coach failed [COACH_RESULT_INVALID]: The coach returned an invalid terminal result.",
    )
    assert "SECRET_VALUE" not in repr(chat.messages)
    assert "/private/invalid-result.json" not in repr(chat.messages)
    assert ("operation-test", "session-test") not in app._chat_pending_bindings
    assert harness.unsubscribe_calls == [1]
    app.on_unmount()


def test_chat_handles_already_completed_authoritative_future_once() -> None:
    app, _audio = app_with_legacy_audio()
    chat = RecordingChat()
    app.chat_panel = chat  # type: ignore[assignment]
    harness = make_pending()
    harness.completion.set_result(
        TerminalCommandOutcome(
            operation_id="operation-test",
            session_id="session-test",
            status="completed",
            public={"ok": True, "message": "Already settled."},
        )
    )
    app._execute_tool_from_worker = (  # type: ignore[method-assign]
        lambda _name, _arguments: harness.pending
    )

    assert app._execute_chat_slash_command("/coach teach Test Melody --segment main")
    assert chat.messages == [
        ("codex", "Coach accepted. Phase: running."),
        ("codex", "Already settled."),
    ]
    assert harness.unsubscribe_calls == [1]
    assert app._chat_pending_bindings == {}
    app.on_unmount()


def test_unmount_unsubscribes_pending_chat_and_ignores_late_terminal() -> None:
    app, _audio = app_with_legacy_audio()
    chat = RecordingChat()
    app.chat_panel = chat  # type: ignore[assignment]
    harness = make_pending()
    app._execute_tool_from_worker = (  # type: ignore[method-assign]
        lambda _name, _arguments: harness.pending
    )
    assert app._execute_chat_slash_command("/coach teach Test Melody --segment main")
    before_unmount = list(chat.messages)

    app.on_unmount()
    harness.completion.set_result(
        TerminalCommandOutcome(
            operation_id="operation-test",
            session_id="session-test",
            status="completed",
            public={"ok": True, "message": "Late result must not render."},
        )
    )

    assert harness.unsubscribe_calls == [1]
    assert chat.messages == before_unmount


def test_pending_mcp_run_stays_running_until_authoritative_future_settles() -> None:
    app, _audio = app_with_legacy_audio()
    harness = make_pending()
    app._execute_tool_from_worker = (  # type: ignore[method-assign]
        lambda _name, _arguments: harness.pending
    )

    status_code, accepted = app.mcp_local_run_slash(
        command="/coach teach SECRET_VALUE --segment main",
        source="codex",
        timeout_s=30,
    )
    run_id = accepted["run_id"]

    assert status_code == 202
    assert accepted["status"] == "running"
    assert accepted["command"] == "/coach teach"
    assert "SECRET_VALUE" not in repr(accepted)

    harness.completion.set_result(
        TerminalCommandOutcome(
            operation_id="operation-test",
            session_id="session-test",
            status="completed",
            public={"ok": True, "message": "Attempt scored."},
        )
    )
    settled = app.mcp_local_run_status(run_id)

    assert settled is not None
    assert settled["status"] == "completed"
    assert settled["result"]["message"] == "Attempt scored."
    app.on_unmount()


def test_mcp_handles_synchronously_completed_pending_future_truthfully() -> None:
    app, _audio = app_with_legacy_audio()
    harness = make_pending()
    harness.completion.set_result(
        TerminalCommandOutcome(
            operation_id="operation-test",
            session_id="session-test",
            status="completed",
            public={"ok": True, "message": "Already settled."},
        )
    )
    app._execute_tool_from_worker = (  # type: ignore[method-assign]
        lambda _name, _arguments: harness.pending
    )

    status_code, settled = app.mcp_local_run_slash(
        command="/coach teach Test Melody --segment main",
        source="codex",
        timeout_s=30,
    )

    assert status_code == 200
    assert settled["status"] == "completed"
    assert settled["result"]["message"] == "Already settled."
    assert harness.subscribers == []
    app.on_unmount()


async def wait_async(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    assert predicate()


def test_synthetic_teach_uses_same_bus_audio_ui_capture_chat_and_tracker(tmp_path) -> None:
    async def run() -> None:
        lesson = synthetic_lesson()
        service = LessonService(
            store=LessonStore(root=tmp_path / "coach", builtins=(lesson,)),
            provider=None,
        )
        app = HalcyonApp(lesson_service=service)
        audio = DeferredAdmissionAudio()
        app.audio = audio  # type: ignore[assignment]
        app.midi = FakeMidi()  # type: ignore[assignment]
        install_fast_coach(app)

        async with app.run_test(size=(180, 55)) as pilot:
            status_code, accepted = app.mcp_local_run_slash(
                command="/coach teach Synthetic Melody --segment main",
                source="codex",
                timeout_s=30,
            )
            run_id = accepted["run_id"]
            assert status_code == 202
            assert accepted["status"] == "running"

            await wait_async(lambda: bool(audio.events))
            assert audio.events[0].origin is MidiOrigin.HALCYON
            assert app.coach.phase is CoachPhase.DEMONSTRATING
            assert app.mcp_local_run_status(run_id)["status"] == "running"  # type: ignore[index]

            audio.accept_admissions = True
            await wait_async(lambda: app.coach.phase is CoachPhase.CAPTURING)

            human = MidiEventAdapter(
                origin=MidiOrigin.HUMAN,
                source_port="synthetic://human",
            )
            app._handle_event(human.note_on(60, 90))
            assert app.human_pressed_notes == {60}
            app._handle_event(human.note_off(60))
            before_chat = len(app.chat_panel.transcript.lines)
            assert app._execute_chat_slash_command("/coach feedback")
            await pilot.pause()

            settled = app.mcp_local_run_status(run_id)
            assert settled is not None
            assert settled["status"] == "completed"
            assert settled["result"]["report"]["metrics"]["matched_note_count"] == 1
            assert len(app.chat_panel.transcript.lines) > before_chat
            assert {event.origin for event in app.event_log.recent()} == {
                MidiOrigin.HUMAN,
                MidiOrigin.HALCYON,
            }
            assert len(audio.events) == len(app.event_log)
            assert len(app.event_table.rows) == len(app.event_log)
            assert app.event_bus.active_voices(origin=MidiOrigin.HALCYON) == ()
            assert service.provider is None

    asyncio.run(run())


class FakeProcess:
    def __init__(self) -> None:
        self.exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code


class RejectingSafetyAudio(LegacyAudio):
    def __init__(self) -> None:
        super().__init__()
        self.process = FakeProcess()
        self.close_started = threading.Event()
        self.allow_stop = threading.Event()
        self.next_command_id = 1

    def start(self) -> bool:
        self.start_calls += 1
        self.process.exit_code = None
        return True

    def snapshot(self) -> AudioBridgeSnapshot:
        running = self.process.poll() is None
        return AudioBridgeSnapshot(running=running, ready=running, text="Audio")

    def handle_midi_event_submission(self, event: MidiEvent) -> CommandSubmission:
        command_id = self.next_command_id
        self.next_command_id += 1
        return CommandSubmission(
            command_id=command_id,
            submitted=True,
            status="pending",
            safety=event.is_release,
        )

    def admission(self, command_id: int) -> CommandAdmission:
        return CommandAdmission(
            command_id=command_id,
            status="rejected",
            queue="safety",
            code="AUDIO_BACKPRESSURE",
        )

    def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        self.allow_stop.wait(timeout=3)
        self.process.exit_code = 0


class InvalidAcceptedSubmissionAudio(RejectingSafetyAudio):
    def handle_midi_event_submission(self, event: MidiEvent) -> CommandSubmission:
        return CommandSubmission(
            command_id=None,
            submitted=True,
            status="accepted",
            safety=event.is_release,
        )


class InvalidAdmissionIdentityAudio(RejectingSafetyAudio):
    def __init__(self, identity_mode: str) -> None:
        super().__init__()
        self.identity_mode = identity_mode

    def admission(self, command_id: int) -> object:
        if self.identity_mode == "missing":
            return SimpleNamespace(status="accepted", queue="safety")
        return CommandAdmission(
            command_id=command_id + 100,
            status="accepted",
            queue="safety",
        )


class StructuredGlobalAudio(LegacyAudio):
    def __init__(
        self,
        *,
        stop_on_close: bool = True,
        block_close: bool = False,
    ) -> None:
        super().__init__()
        self.process = FakeProcess()
        self.stop_on_close = stop_on_close
        self.block_close = block_close
        self.close_started = threading.Event()
        self.allow_stop = threading.Event()
        self.admission_status: str | None = None
        self.global_submission_calls = 0
        self.global_command_id = 701
        self.patch_calls = 0

    def start(self) -> bool:
        self.start_calls += 1
        self.process.exit_code = None
        return True

    def apply_patch(self, _patch: dict[str, float]) -> bool:
        self.patch_calls += 1
        return True

    def snapshot(self) -> AudioBridgeSnapshot:
        running = self.process.poll() is None
        return AudioBridgeSnapshot(running=running, ready=running, text="Audio")

    def all_notes_off_submission(self) -> CommandSubmission:
        self.global_submission_calls += 1
        return CommandSubmission(
            command_id=self.global_command_id,
            submitted=True,
            status="pending",
            safety=True,
        )

    def admission(self, command_id: int) -> CommandAdmission | None:
        if self.admission_status is None:
            return None
        return CommandAdmission(
            command_id=command_id,
            status=self.admission_status,
            queue="safety",
            code="AUDIO_BACKPRESSURE" if self.admission_status == "rejected" else None,
        )

    def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        if self.block_close:
            self.allow_stop.wait(timeout=3)
        if self.stop_on_close:
            self.process.exit_code = 0


class SelectiveSafetyRejectingAudio(RejectingSafetyAudio):
    def __init__(self) -> None:
        super().__init__()
        self.safety_by_command: dict[int, bool] = {}

    def handle_midi_event_submission(self, event: MidiEvent) -> CommandSubmission:
        self.events.append(event)
        submission = super().handle_midi_event_submission(event)
        assert submission.command_id is not None
        self.safety_by_command[submission.command_id] = submission.safety
        return submission

    def admission(self, command_id: int) -> CommandAdmission:
        safety = self.safety_by_command[command_id]
        return CommandAdmission(
            command_id=command_id,
            status="rejected" if safety else "accepted",
            queue="safety" if safety else "normal",
            code="AUDIO_BACKPRESSURE" if safety else None,
        )


def wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def test_structured_accepted_submission_without_command_id_fails_closed() -> None:
    app = HalcyonApp()
    audio = InvalidAcceptedSubmissionAudio()
    app.audio = audio  # type: ignore[assignment]
    app.midi = FakeMidi()  # type: ignore[assignment]
    human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")

    normal = app._submit_audio_event(human.note_on(60, 90))
    assert normal.poll().state == "failed_safe"
    assert not audio.close_started.is_set()

    safety = app._submit_audio_event(human.note_off(60))
    assert safety.poll().state == "rejected_unsafe"
    assert audio.close_started.wait(timeout=1)
    audio.allow_stop.set()
    wait_until(lambda: safety.poll().state == "failed_safe")
    app.on_unmount()


@pytest.mark.parametrize("identity_mode", ["missing", "mismatched"])
@pytest.mark.parametrize("safety", [False, True])
def test_admission_identity_must_be_present_and_exact(
    identity_mode: str,
    safety: bool,
) -> None:
    app = HalcyonApp()
    audio = InvalidAdmissionIdentityAudio(identity_mode)
    app.audio = audio  # type: ignore[assignment]
    app.midi = FakeMidi()  # type: ignore[assignment]
    human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")
    event = human.note_off(60) if safety else human.note_on(60, 90)

    receipt = app._submit_audio_event(event)
    expected = "rejected_unsafe" if safety else "failed_safe"
    assert receipt.poll().state == expected
    if safety:
        assert audio.close_started.wait(timeout=1)
        audio.allow_stop.set()
        wait_until(lambda: receipt.poll().state == "failed_safe")
    else:
        assert not audio.close_started.is_set()
        audio.allow_stop.set()
    app.on_unmount()


def test_structured_global_safety_waits_for_acceptance_before_reconnect() -> None:
    async def run() -> None:
        app = HalcyonApp()
        audio = StructuredGlobalAudio()
        midi = CountingMidi()
        app.audio = audio  # type: ignore[assignment]
        app.midi = midi  # type: ignore[assignment]
        human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")

        async with app.run_test(size=(120, 42)):
            assert midi.connect_calls == 1
            assert audio.patch_calls == 1
            app._handle_event(human.note_on(60, 90))
            assert app.event_bus.active_voices()

            app.action_reconnect()
            assert audio.global_submission_calls == 1
            assert audio.panic_calls == 0
            await asyncio.sleep(0.05)
            assert app.event_bus.active_voices()
            assert midi.connect_calls == 1
            assert audio.patch_calls == 1

            audio.admission_status = "accepted"
            await wait_async(lambda: app.event_bus.active_voices() == ())
            await wait_async(lambda: midi.connect_calls == 2)
            assert audio.patch_calls == 2

    asyncio.run(run())


def test_rejected_global_safety_resumes_only_after_proven_stop() -> None:
    async def run() -> None:
        app = HalcyonApp()
        audio = StructuredGlobalAudio(block_close=True)
        audio.admission_status = "rejected"
        midi = CountingMidi()
        app.audio = audio  # type: ignore[assignment]
        app.midi = midi  # type: ignore[assignment]
        human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")

        async with app.run_test(size=(120, 42)):
            app._handle_event(human.note_on(60, 90))
            app.action_reconnect()
            action = app._global_safety_action
            assert action is not None
            receipt = action.receipt
            assert audio.close_started.wait(timeout=1)
            assert receipt.poll().state == "rejected_unsafe"
            assert app.event_bus.active_voices()
            assert midi.connect_calls == 1
            assert audio.patch_calls == 1

            audio.allow_stop.set()
            await wait_async(lambda: receipt.poll().state == "failed_safe")
            await wait_async(lambda: app.event_bus.active_voices() == ())
            await wait_async(lambda: midi.connect_calls == 2)
            await wait_async(lambda: audio.start_calls == 2)
            assert audio.patch_calls >= 2
            assert audio.panic_calls == 0

    asyncio.run(run())


def test_rejected_global_safety_without_stop_evidence_stays_unsafe() -> None:
    async def run() -> None:
        app = HalcyonApp()
        audio = StructuredGlobalAudio(stop_on_close=False)
        audio.admission_status = "rejected"
        midi = CountingMidi()
        app.audio = audio  # type: ignore[assignment]
        app.midi = midi  # type: ignore[assignment]
        human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")

        async with app.run_test(size=(120, 42)):
            app._handle_event(human.note_on(60, 90))
            app.action_reconnect()
            action = app._global_safety_action
            assert action is not None
            assert audio.close_started.wait(timeout=1)
            await asyncio.sleep(0.05)

            assert action.receipt.poll().state == "rejected_unsafe"
            assert app.event_bus.active_voices()
            assert midi.connect_calls == 1
            assert audio.patch_calls == 1
            assert audio.start_calls == 1
            assert app._global_safety_action is action

    asyncio.run(run())


def test_pending_global_safety_never_continues_after_unmount() -> None:
    async def run() -> None:
        app = HalcyonApp()
        audio = StructuredGlobalAudio()
        midi = CountingMidi()
        app.audio = audio  # type: ignore[assignment]
        app.midi = midi  # type: ignore[assignment]
        human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")

        async with app.run_test(size=(120, 42)):
            app._handle_event(human.note_on(60, 90))
            app.action_reconnect()
            assert app._global_safety_action is not None
            assert app.event_bus.active_voices()
            assert midi.connect_calls == 1
            assert audio.patch_calls == 1

        audio.admission_status = "accepted"
        assert not app._service_global_safety_action()
        assert app._global_safety_action is None
        assert app.event_bus.active_voices()
        assert midi.connect_calls == 1
        assert audio.patch_calls == 1

    asyncio.run(run())


def test_rejected_safety_receipt_stays_unsafe_until_captured_process_stops() -> None:
    app = HalcyonApp()
    audio = RejectingSafetyAudio()
    app.audio = audio  # type: ignore[assignment]
    app.midi = FakeMidi()  # type: ignore[assignment]
    human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")

    receipt = app._submit_audio_event(human.note_off(60))
    assert receipt.poll().state == "rejected_unsafe"
    assert audio.close_started.wait(timeout=1)
    assert receipt.poll().state == "rejected_unsafe"

    audio.allow_stop.set()
    wait_until(lambda: receipt.poll().state == "failed_safe")
    assert audio.close_calls == 1
    app.on_unmount()
    assert audio.close_calls == 1


def test_rejected_safety_containment_never_restarts_after_unmount() -> None:
    async def run() -> None:
        app = HalcyonApp()
        audio = RejectingSafetyAudio()
        app.audio = audio  # type: ignore[assignment]
        app.midi = FakeMidi()  # type: ignore[assignment]
        human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://test")

        async with app.run_test(size=(120, 42)) as pilot:
            app._handle_event(human.note_off(60))
            for _ in range(100):
                if audio.close_started.is_set():
                    break
                await asyncio.sleep(0.005)
            assert audio.close_started.is_set()
            await pilot.press("ctrl+q")

        audio.allow_stop.set()
        wait_until(lambda: audio.process.poll() is not None)
        await asyncio.sleep(0.05)
        assert audio.start_calls == 1
        assert audio.close_calls == 1

    asyncio.run(run())


def test_coach_waits_for_unsafe_containment_then_fails_and_restarts_safely(tmp_path) -> None:
    async def run() -> None:
        lesson = synthetic_lesson()
        service = LessonService(
            store=LessonStore(root=tmp_path / "coach", builtins=(lesson,)),
            provider=None,
        )
        app = HalcyonApp(lesson_service=service)
        audio = SelectiveSafetyRejectingAudio()
        app.audio = audio  # type: ignore[assignment]
        app.midi = FakeMidi()  # type: ignore[assignment]
        install_fast_coach(app)

        async with app.run_test(size=(140, 48)):
            status_code, accepted = app.mcp_local_run_slash(
                command="/coach teach Synthetic Melody --segment main",
                source="codex",
                timeout_s=30,
            )
            run_id = accepted["run_id"]
            assert status_code == 202
            await wait_async(audio.close_started.is_set)

            containment = next(iter(app._audio_containments.values()))
            assert containment.receipts
            assert containment.receipts[0].poll().state == "rejected_unsafe"
            assert app.mcp_local_run_status(run_id)["status"] == "running"  # type: ignore[index]

            audio.allow_stop.set()
            await wait_async(
                lambda: (app.mcp_local_run_status(run_id) or {}).get("status") == "failed"
            )
            settled = app.mcp_local_run_status(run_id)
            assert settled is not None
            assert settled["result"]["error"]["code"] == "AUDIO_BACKPRESSURE"
            assert containment.receipts[0].poll().state == "failed_safe"
            assert app.event_bus.active_voices(origin=MidiOrigin.HALCYON) == ()
            await wait_async(lambda: audio.start_calls == 2)

    asyncio.run(run())
