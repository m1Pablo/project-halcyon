from __future__ import annotations

from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
import heapq
import threading
import time
from typing import Any

import pytest

import halcyon.coach_engine as engine_module
from halcyon.coach_authoring import AuthoringControl
from halcyon.coach_engine import CoachEngine, CoachPhase
from halcyon.coach_models import (
    Acquisition,
    AuthorMode,
    CoachError,
    Fidelity,
    LessonEvent,
    LessonProvenance,
    LessonRequest,
    LessonResolution,
    LessonSegment,
    TempoChange,
    TimeSignature,
    create_lesson_revision,
    resolve_timeline,
)
from halcyon.event_bus import HalcyonMidiAdapter, MidiEventAdapter, MidiEventBus, ReceiptPoll
from halcyon.midi import MidiOrigin


class FakeClock:
    def __init__(self, now_ns: int = 1_000_000_000) -> None:
        self.now_ns = now_ns
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.now_ns


class FakeDispatcher:
    def __init__(self) -> None:
        self._callbacks: list[Any] = []
        self._lock = threading.Lock()
        self.available = threading.Event()

    def __call__(self, callback):  # type: ignore[no-untyped-def]
        with self._lock:
            self._callbacks.append(callback)
            self.available.set()

    def pump(self) -> int:
        count = 0
        while True:
            with self._lock:
                if not self._callbacks:
                    self.available.clear()
                    return count
                callback = self._callbacks.pop(0)
            callback()
            count += 1


@dataclass
class FakeHandle:
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeScheduler:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self._queue: list[tuple[int, int, FakeHandle, Any]] = []
        self._order = 0
        self.deadlines: list[int] = []

    def call_at_ns(self, deadline_ns: int, callback):  # type: ignore[no-untyped-def]
        self._order += 1
        handle = FakeHandle()
        heapq.heappush(self._queue, (deadline_ns, self._order, handle, callback))
        self.deadlines.append(deadline_ns)
        return handle

    def run_due(self, *, include_cancelled: bool = False) -> int:
        count = 0
        while self._queue and self._queue[0][0] <= self.clock.now_ns:
            _deadline, _order, handle, callback = heapq.heappop(self._queue)
            if not handle.cancelled or include_cancelled:
                callback()
                count += 1
        return count


class RaisingScheduler(FakeScheduler):
    def __init__(self, clock: FakeClock, *, fail_at_call: int) -> None:
        super().__init__(clock)
        self.fail_at_call = fail_at_call
        self.calls = 0

    def call_at_ns(self, deadline_ns: int, callback):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == self.fail_at_call:
            raise RuntimeError("scheduler internal path must not leak")
        return super().call_at_ns(deadline_ns, callback)


class ReentrantScheduler:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls = 0

    def call_at_ns(self, deadline_ns: int, callback):  # type: ignore[no-untyped-def]
        self.calls += 1
        handle = FakeHandle()
        self.clock.now_ns = deadline_ns
        callback()
        return handle


class RejectingDispatcher(FakeDispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.reject = False

    def __call__(self, callback):  # type: ignore[no-untyped-def]
        if self.reject:
            raise RuntimeError("dispatcher closed")
        super().__call__(callback)


class MutableReceipt:
    def __init__(self, state: str = "pending") -> None:
        self.state = state
        self.poll_threads: list[int] = []

    def poll(self) -> ReceiptPoll:
        self.poll_threads.append(threading.get_ident())
        return ReceiptPoll(self.state)  # type: ignore[arg-type]


class RaisingLaterReceipt:
    def __init__(self, *, raise_after: int, diagnostic: str) -> None:
        self.raise_after = raise_after
        self.diagnostic = diagnostic
        self.poll_calls = 0

    def poll(self) -> ReceiptPoll:
        self.poll_calls += 1
        if self.poll_calls > self.raise_after:
            raise RuntimeError(self.diagnostic)
        return ReceiptPoll("pending", code="queued", message=self.diagnostic)


class ReceiptConsumer:
    def __init__(self, *states: str, default: str = "pending") -> None:
        self._states = list(states)
        self.default = default
        self.events: list[Any] = []
        self.receipts: list[MutableReceipt] = []

    def __call__(self, event):  # type: ignore[no-untyped-def]
        state = self._states.pop(0) if self._states else self.default
        receipt = MutableReceipt(state)
        self.events.append(event)
        self.receipts.append(receipt)
        return receipt


class InlineExecutor(Executor):
    def submit(self, fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class QueuedExecutor(Executor):
    def __init__(self) -> None:
        self.future: Future[Any] | None = None

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.future = Future()
        return self.future


class StaticLessonService:
    def __init__(self, lesson, *, cache_hit: bool = True, authored: bool = False) -> None:
        self.lesson = lesson
        self.cache_hit = cache_hit
        self.authored = authored
        self.calls = 0

    def resolve(self, request: LessonRequest, *, control: AuthoringControl):
        self.calls += 1
        control.checkpoint("fake.before")
        timeline = resolve_timeline(self.lesson, request)
        control.checkpoint("fake.after")
        control.finish()
        return LessonResolution(
            lesson=self.lesson,
            timeline=timeline,
            cache_hit=self.cache_hit,
            authored=self.authored,
            matched_alias=request.normalized_alias,
        )


class BlockingLessonService(StaticLessonService):
    def __init__(self, lesson) -> None:
        super().__init__(lesson, cache_hit=False, authored=True)
        self.started = threading.Event()
        self.release = threading.Event()

    def resolve(self, request: LessonRequest, *, control: AuthoringControl):
        self.calls += 1
        control.checkpoint("authoring.before_provider")
        self.started.set()
        self.release.wait(timeout=5)
        control.checkpoint("fake.late_response")
        return super().resolve(request, control=control)


class CommitFenceLessonService(StaticLessonService):
    def __init__(self, lesson) -> None:
        super().__init__(lesson, cache_hit=False, authored=True)
        self.committing = threading.Event()
        self.release = threading.Event()

    def resolve(self, request: LessonRequest, *, control: AuthoringControl):
        self.calls += 1
        control.checkpoint("authoring.before_persistence")
        timeline = resolve_timeline(self.lesson, request)
        with control.commit_fence():
            self.committing.set()
            if not self.release.wait(timeout=5):
                raise AssertionError("commit-fence test did not release the worker")
        return LessonResolution(
            lesson=self.lesson,
            timeline=timeline,
            cache_hit=self.cache_hit,
            authored=self.authored,
            matched_alias=request.normalized_alias,
        )


class FailingLessonService:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, request: LessonRequest, *, control: AuthoringControl):
        self.calls += 1
        raise CoachError(
            "SOURCE_REQUIRED",
            "No reusable lesson exists.",
            resolution="Retry with --author bounded.",
            retryable=True,
        )


class CountingBus(MidiEventBus):
    def __init__(self, *, clock: FakeClock) -> None:
        super().__init__(clock=clock)
        self.release_calls = 0

    def release_halcyon_voices(self, **kwargs):  # type: ignore[no-untyped-def]
        self.release_calls += 1
        return super().release_halcyon_voices(**kwargs)


def _lesson(*, ppq: int = 480, start_tick: int = 0, end_tick: int = 480):
    return create_lesson_revision(
        lesson_id=f"test-lesson-{ppq}-{start_tick}",
        created_at="2026-07-12T00:00:00+00:00",
        aliases=("Test Melody",),
        title="Test Melody",
        composer="Halcyon Tests",
        difficulty="foundation",
        hand="right",
        ppq=ppq,
        tempo_map=(TempoChange(0, 500_000),),
        time_signatures=(TimeSignature(0, 4, 4),),
        provenance=LessonProvenance(
            acquisition=Acquisition.ORIGINAL,
            citation="Original deterministic test fixture.",
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
                source_port="fixture://lesson",
                content_origin="original",
                absolute_tick=start_tick,
                delta_tick=start_tick,
            ),
            LessonEvent(
                event_id="",
                event_type="note_off",
                note=60,
                velocity=0,
                channel=1,
                status=0x80,
                source_port="fixture://lesson",
                content_origin="original",
                absolute_tick=end_tick,
                delta_tick=end_tick - start_tick,
            ),
        ),
        segments=(LessonSegment("main", start_tick, end_tick, hand="right"),),
    )


@dataclass
class Runtime:
    clock: FakeClock
    dispatcher: FakeDispatcher
    scheduler: FakeScheduler
    bus: CountingBus
    engine: CoachEngine

    def settle(self) -> None:
        while True:
            fired = self.scheduler.run_due()
            pumped = self.dispatcher.pump()
            if fired == 0 and pumped == 0:
                return

    def advance_to(self, deadline_ns: int) -> None:
        self.clock.now_ns = deadline_ns
        self.settle()


def _runtime(
    service,
    *,
    executor: Executor | None = None,
    capture_tail_us: int = 0,
) -> Runtime:
    clock = FakeClock()
    dispatcher = FakeDispatcher()
    scheduler = FakeScheduler(clock)
    bus = CountingBus(clock=clock)
    engine = CoachEngine(
        service,
        bus,
        dispatcher=dispatcher,
        scheduler=scheduler,
        executor=executor or InlineExecutor(),
        clock_ns=clock,
        capture_tail_us=capture_tail_us,
        default_count_in=0,
    )
    return Runtime(clock, dispatcher, scheduler, bus, engine)


def _teach(runtime: Runtime, *, loops: int = 1):
    pending = runtime.engine.teach(
        LessonRequest(
            query="Test Melody",
            segment="main",
            author=AuthorMode.NEVER,
        ),
        loops=loops,
        count_in=0,
    )
    runtime.settle()
    return pending


def test_teach_exact_playback_uses_one_anchor_bus_only_and_scores_human_capture(
    monkeypatch,
) -> None:
    lesson = _lesson()
    runtime = _runtime(StaticLessonService(lesson))
    observed_on_bus = []
    runtime.bus.subscribe(observed_on_bus.append)
    captured_requests = []
    real_score = engine_module.score_attempt

    def capture_score(request):  # type: ignore[no-untyped-def]
        captured_requests.append(request)
        return real_score(request)

    monkeypatch.setattr(engine_module, "score_attempt", capture_score)
    pending = _teach(runtime)

    assert runtime.engine.phase is CoachPhase.DEMONSTRATING
    halcyon_on = observed_on_bus[0]
    assert halcyon_on.origin is MidiOrigin.HALCYON
    assert halcyon_on.lesson_id == lesson.lesson_id
    assert halcyon_on.lesson_event_id == lesson.events[0].event_id
    assert halcyon_on.playback_id
    assert halcyon_on.event_id.startswith("p1-")
    assert halcyon_on.scheduled_monotonic_ns == 1_000_000_000

    runtime.advance_to(1_500_000_000)
    assert runtime.engine.phase is CoachPhase.CAPTURING
    human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="akai://lpk25")
    human_on = runtime.bus.publish(human.note_on(60, 90)).event
    runtime.advance_to(1_900_000_000)
    human_off = runtime.bus.publish(human.note_off(60)).event
    runtime.advance_to(2_000_000_000)

    terminal = pending.completion.result(timeout=0)
    assert terminal.status == "completed"
    assert terminal.public["code"] == "COACH_ATTEMPT_COMPLETE"
    assert runtime.engine.phase is CoachPhase.REVIEW
    request = captured_requests[0]
    assert [event.event_id for event in request.observed_events] == [
        human_on.event_id,
        human_off.event_id,
    ]
    assert all(event.session_id == pending.session_id for event in request.observed_events)
    assert all(event.attempt_id == request.attempt_id for event in request.observed_events)
    assert [event.voice_id for event in request.observed_events] == [
        human_on.voice_id,
        human_off.voice_id,
    ]
    assert terminal.public["report"]["metrics"]["observed_note_count"] == 1
    assert "observed_events" not in repr(terminal.public)


def test_ppq_remainder_schedule_has_no_loop_drift_and_drains_boundary_order(monkeypatch) -> None:
    lesson = _lesson(ppq=3, start_tick=1, end_tick=2)
    runtime = _runtime(StaticLessonService(lesson))
    events = []
    runtime.bus.subscribe(events.append)
    real_resolve = engine_module.resolve_loop_schedule
    calls = []

    def counted(*args):  # type: ignore[no-untyped-def]
        calls.append(args)
        return real_resolve(*args)

    monkeypatch.setattr(engine_module, "resolve_loop_schedule", counted)
    _teach(runtime, loops=3)
    anchor = 1_000_000_000

    assert len(calls) == 1
    assert runtime.scheduler.deadlines[-1] == anchor + 166_667_000
    runtime.advance_to(anchor + 200_000_000)
    assert [event.midi_event_type for event in events[-2:]] == ["note_off", "note_on"]
    assert {event.scheduled_monotonic_ns for event in events[-2:]} == {anchor + 166_667_000}
    assert {event.received_monotonic_ns for event in events[-2:]} == {anchor + 200_000_000}
    runtime.advance_to(anchor + 333_334_000)
    assert runtime.scheduler.deadlines[-1] == anchor + 500_000_000
    runtime.advance_to(anchor + 500_000_000)

    assert runtime.engine.phase is CoachPhase.CAPTURING
    assert len(calls) == 1
    assert runtime.engine._session.capture_ended_ns == anchor + 666_667_000  # type: ignore[union-attr]


def test_replay_finishes_ready_and_each_playback_gets_fresh_runtime_identity() -> None:
    lesson = _lesson()
    runtime = _runtime(StaticLessonService(lesson))
    events = []
    runtime.bus.subscribe(events.append)
    first = _teach(runtime)
    runtime.advance_to(1_500_000_000)
    runtime.engine.feedback()
    assert first.completion.done()

    runtime.clock.now_ns = 3_000_000_000
    replay = runtime.engine.replay(loops=1)
    first_replay_id = events[-1].playback_id
    runtime.advance_to(3_500_000_000)

    assert replay.completion.result().status == "completed"
    assert runtime.engine.phase is CoachPhase.READY
    runtime.clock.now_ns = 4_000_000_000
    second = runtime.engine.replay()
    assert events[-1].playback_id != first_replay_id
    second.cancel()


def test_feedback_ends_capture_early_and_retains_only_aggregate_report() -> None:
    runtime = _runtime(StaticLessonService(_lesson()), capture_tail_us=500_000)
    pending = _teach(runtime)
    runtime.advance_to(1_500_000_000)

    feedback = runtime.engine.feedback()

    assert pending.completion.result().status == "completed"
    assert feedback.public["code"] == "COACH_FEEDBACK"
    assert feedback.public["report"]["status"] == "empty"
    state = runtime.engine.state().public["coach"]
    assert state["reports_retained"] == 1
    assert "capture_events" not in repr(state)
    assert runtime.engine._session.capture_events == []  # type: ignore[union-attr]


def test_capture_filters_halcyon_and_window_restamps_without_republishing() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    published = []
    runtime.bus.subscribe(published.append)
    pending = _teach(runtime)
    runtime.advance_to(1_500_000_000)
    before = len(published)
    outside = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://outside")
    runtime.clock.now_ns = 1_499_999_999
    runtime.bus.publish(outside.note_on(61, 70))
    runtime.clock.now_ns = 1_500_000_000
    inside = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://inside")
    runtime.bus.publish(inside.note_on(60, 90))

    assert len(published) == before + 2
    assert len(runtime.engine._session.capture_events) == 1  # type: ignore[union-attr]
    pending.cancel()
    assert runtime.engine._session.capture_events == []  # type: ignore[union-attr]


def test_capture_cap_is_sticky_and_cancel_discards_raw_events() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    pending = _teach(runtime)
    runtime.advance_to(1_500_000_000)
    human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="human://many")
    for note_index in range(2_049):
        runtime.bus.publish(human.note_on(note_index % 128, 1))

    session = runtime.engine._session
    assert session is not None
    assert len(session.capture_events) == 2_048
    assert session.capture_truncated
    pending.cancel()
    assert session.capture_events == []


def test_cancel_releases_only_halcyon_same_note_and_late_timer_is_ignored() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    pending = _teach(runtime)
    human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="akai://lpk25")
    human_on = runtime.bus.publish(human.note_on(60, 100)).event

    assert len(runtime.bus.active_voices(origin=MidiOrigin.HALCYON)) == 1
    assert len(runtime.bus.active_voices(origin=MidiOrigin.HUMAN)) == 1
    assert pending.cancel("stop") is False

    terminal = pending.completion.result()
    assert terminal.status == "cancelled"
    assert terminal.public["error"]["code"] == "SESSION_CANCELLED"
    assert runtime.bus.release_calls == 1
    assert runtime.bus.active_voices(origin=MidiOrigin.HALCYON) == ()
    assert [voice.voice_id for voice in runtime.bus.active_voices(origin=MidiOrigin.HUMAN)] == [
        human_on.voice_id
    ]
    runtime.scheduler.run_due(include_cancelled=True)
    runtime.dispatcher.pump()
    assert runtime.engine.phase is CoachPhase.CANCELLED
    assert runtime.bus.release_calls == 1


def test_required_consumer_error_never_claims_safe_success() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))

    def reject_halcyon(event):  # type: ignore[no-untyped-def]
        if event.origin is MidiOrigin.HALCYON:
            raise RuntimeError("audio queue full")

    runtime.bus.subscribe(reject_halcyon, receipt_required=True)
    pending = _teach(runtime)

    assert not pending.completion.done()
    assert runtime.engine.phase is CoachPhase.CANCELLING
    assert runtime.bus.release_calls == 1
    runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)
    assert not pending.completion.done()


def test_optional_consumer_error_fails_after_receipt_safe_cleanup() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    audio = ReceiptConsumer(default="accepted")
    runtime.bus.subscribe(audio, receipt_required=True)

    def reject_halcyon(event):  # type: ignore[no-untyped-def]
        if event.origin is MidiOrigin.HALCYON:
            raise RuntimeError("audio queue full")

    runtime.bus.subscribe(reject_halcyon)
    pending = _teach(runtime)

    terminal = pending.completion.result(timeout=0)
    assert terminal.status == "failed"
    assert terminal.public["error"]["code"] == "AUDIO_BACKPRESSURE"
    assert runtime.engine.phase is CoachPhase.FAILED
    assert runtime.bus.release_calls == 1
    assert audio.receipts
    assert all(receipt.state == "accepted" for receipt in audio.receipts)


def test_playback_waits_for_every_required_receipt_on_owner_scheduler() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    consumer = ReceiptConsumer(default="pending")
    runtime.bus.subscribe(consumer, receipt_required=True)
    owner_thread = threading.get_ident()

    pending = _teach(runtime)

    assert runtime.engine.phase is CoachPhase.DEMONSTRATING
    assert len(consumer.receipts) == 1
    assert runtime.scheduler.deadlines[-1] == 1_010_000_000
    consumer.receipts[0].state = "accepted"
    runtime.advance_to(1_010_000_000)
    assert runtime.scheduler.deadlines[-1] == 1_500_000_000

    runtime.advance_to(1_500_000_000)
    assert len(consumer.receipts) == 2
    assert runtime.engine.phase is CoachPhase.DEMONSTRATING
    assert not pending.completion.done()
    consumer.receipts[1].state = "accepted"
    runtime.advance_to(1_510_000_000)

    assert runtime.engine.phase is CoachPhase.CAPTURING
    assert not pending.completion.done()
    assert all(
        poll_thread == owner_thread
        for receipt in consumer.receipts
        for poll_thread in receipt.poll_threads
    )
    assert "receipt" not in repr(runtime.engine.state().public).lower()
    pending.cancel()


def test_timeout_waits_for_pending_cleanup_acceptance_and_preserves_human_same_note() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    consumer = ReceiptConsumer(default="pending")
    runtime.bus.subscribe(consumer, receipt_required=True)
    pending = _teach(runtime)
    human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="akai://lpk25")
    human_on = runtime.bus.publish(human.note_on(60, 100)).event

    assert pending.cancel("timeout")
    cleanup_receipts = [
        receipt
        for event, receipt in zip(consumer.events, consumer.receipts, strict=True)
        if event.origin is MidiOrigin.HALCYON and event.is_release
    ]
    assert len(cleanup_receipts) == 1
    assert not pending.completion.done()
    assert runtime.engine.phase is CoachPhase.CANCELLING
    assert runtime.bus.release_calls == 1
    assert [voice.voice_id for voice in runtime.bus.active_voices(origin=MidiOrigin.HUMAN)] == [
        human_on.voice_id
    ]

    for event, receipt in zip(consumer.events, consumer.receipts, strict=True):
        if event.origin is MidiOrigin.HALCYON:
            receipt.state = "accepted"
    runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)

    terminal = pending.completion.result(timeout=0)
    assert terminal.status == "timed_out"
    assert terminal.public["error"]["code"] == "SESSION_CANCELLED"
    assert runtime.bus.release_calls == 1
    assert [voice.voice_id for voice in runtime.bus.active_voices(origin=MidiOrigin.HUMAN)] == [
        human_on.voice_id
    ]


def test_rejected_unsafe_stays_cancelling_until_failed_safe_then_fails() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    consumer = ReceiptConsumer(default="rejected_unsafe")
    runtime.bus.subscribe(consumer, receipt_required=True)

    pending = _teach(runtime)

    assert runtime.engine.phase is CoachPhase.CANCELLING
    assert not pending.completion.done()
    assert runtime.bus.release_calls == 1
    runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)
    assert not pending.completion.done()
    for receipt in consumer.receipts:
        receipt.state = "failed_safe"
    runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)

    terminal = pending.completion.result(timeout=0)
    assert terminal.status == "failed"
    assert terminal.public["error"]["code"] == "AUDIO_BACKPRESSURE"
    assert runtime.engine.phase is CoachPhase.FAILED
    assert runtime.bus.release_calls == 1


def test_failed_safe_is_the_only_receipt_failure_that_can_terminalize() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    consumer = ReceiptConsumer(default="failed_safe")
    runtime.bus.subscribe(consumer, receipt_required=True)

    pending = _teach(runtime)

    terminal = pending.completion.result(timeout=0)
    assert terminal.status == "failed"
    assert terminal.public["error"]["code"] == "AUDIO_BACKPRESSURE"
    assert runtime.engine.phase is CoachPhase.FAILED
    assert runtime.bus.release_calls == 1


def test_failed_safe_waits_for_other_pending_playback_and_cleanup_receipts() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    failed_safe = ReceiptConsumer(default="failed_safe")
    pending_audio = ReceiptConsumer(default="pending")
    runtime.bus.subscribe(failed_safe, receipt_required=True)
    runtime.bus.subscribe(pending_audio, receipt_required=True)

    pending = _teach(runtime)

    assert runtime.engine.phase is CoachPhase.DEMONSTRATING
    assert not pending.completion.done()
    pending_audio.receipts[0].state = "accepted"
    runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)

    assert runtime.engine.phase is CoachPhase.CANCELLING
    assert not pending.completion.done()
    assert runtime.bus.release_calls == 1
    assert len(pending_audio.receipts) == 2
    pending_audio.receipts[-1].state = "accepted"
    runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)

    terminal = pending.completion.result(timeout=0)
    assert terminal.status == "failed"
    assert terminal.public["error"]["code"] == "AUDIO_BACKPRESSURE"


def test_required_receipt_count_mismatch_remains_nonterminal_and_private() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    runtime.bus.subscribe(lambda _event: None, receipt_required=True)

    pending = _teach(runtime)

    assert runtime.engine.phase is CoachPhase.CANCELLING
    assert not pending.completion.done()
    assert runtime.bus.release_calls == 1
    runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)
    assert not pending.completion.done()
    assert "receipt" not in repr(pending.accepted).lower()
    assert "receipt" not in repr(runtime.engine.state().public).lower()


def test_later_receipt_poll_exception_stays_unsafe_and_keeps_diagnostics_private() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    diagnostic = "private audio integration diagnostic"
    playback_receipt = RaisingLaterReceipt(raise_after=3, diagnostic=diagnostic)
    cleanup_receipts: list[MutableReceipt] = []

    def audio_consumer(_event):  # type: ignore[no-untyped-def]
        if playback_receipt.poll_calls == 0:
            return playback_receipt
        receipt = MutableReceipt("accepted")
        cleanup_receipts.append(receipt)
        return receipt

    runtime.bus.subscribe(audio_consumer, receipt_required=True)
    pending = _teach(runtime)

    assert runtime.engine.phase is CoachPhase.DEMONSTRATING
    runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)

    assert runtime.engine.phase is CoachPhase.CANCELLING
    assert not pending.completion.done()
    assert runtime.bus.release_calls == 1
    assert cleanup_receipts
    assert diagnostic not in repr(runtime.engine.state().public)
    assert diagnostic not in repr(pending.accepted)


def test_post_terminal_cleanup_barrier_rejects_every_replacement_entry() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    audio = ReceiptConsumer(default="accepted")
    runtime.bus.subscribe(audio, receipt_required=True)
    taught = _teach(runtime)
    runtime.advance_to(1_500_000_000)
    runtime.engine.feedback()
    assert taught.completion.done()

    session = runtime.engine._session
    assert session is not None
    synthetic = HalcyonMidiAdapter(
        source_port="halcyon://coach/post-terminal",
        session_id=session.session_id,
    )
    runtime.bus.publish(synthetic.note_on(60, 90))
    audio.default = "pending"
    stopped = runtime.engine.stop()

    assert stopped.public["code"] == "COACH_STOP_REQUESTED"
    assert runtime.engine.phase is CoachPhase.CANCELLING
    assert session.retained_receipts
    barrier_ids = [id(item) for item in session.retained_receipts]
    with pytest.raises(CoachError) as replay_error:
        runtime.engine.replay()
    with pytest.raises(CoachError) as attempt_error:
        runtime.engine.attempt()
    with pytest.raises(CoachError) as teach_error:
        runtime.engine.teach(LessonRequest(query="replacement"), replace=True)
    with pytest.raises(CoachError):
        runtime.engine._begin_operation(session, CoachPhase.COUNT_IN, "must not start")
    assert {replay_error.value.code, attempt_error.value.code, teach_error.value.code} == {
        "PLAYBACK_ACTIVE"
    }
    assert [id(item) for item in session.retained_receipts] == barrier_ids

    audio.receipts[-1].state = "accepted"
    runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)
    assert runtime.engine.phase is CoachPhase.CANCELLED
    audio.default = "accepted"
    replay = runtime.engine.replay()
    replay.cancel()


@pytest.mark.parametrize("worker_first", [False, True])
def test_cancel_waits_independently_for_worker_and_cleanup_receipt(worker_first: bool) -> None:
    service = BlockingLessonService(_lesson())
    executor = ThreadPoolExecutor(max_workers=1)
    runtime = _runtime(service, executor=executor)
    consumer = ReceiptConsumer("accepted", "pending", default="accepted")
    runtime.bus.subscribe(consumer, receipt_required=True)
    try:
        pending = runtime.engine.teach(
            LessonRequest(query="Test Melody", segment="main", author=AuthorMode.BOUNDED),
            count_in=0,
        )
        assert service.started.wait(timeout=2)
        synthetic = HalcyonMidiAdapter(
            source_port="halcyon://coach/seed",
            session_id=pending.session_id,
        )
        runtime.bus.publish(synthetic.note_on(60, 90))

        assert pending.cancel("stop")
        assert len(consumer.receipts) == 2
        cleanup_receipt = consumer.receipts[-1]
        assert cleanup_receipt.state == "pending"
        if worker_first:
            service.release.set()
            assert runtime.dispatcher.available.wait(timeout=2)
            runtime.dispatcher.pump()
            assert not pending.completion.done()
            cleanup_receipt.state = "accepted"
            runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)
        else:
            cleanup_receipt.state = "accepted"
            runtime.advance_to(runtime.clock.now_ns + engine_module.RECEIPT_POLL_NS)
            assert not pending.completion.done()
            service.release.set()
            assert runtime.dispatcher.available.wait(timeout=2)
            runtime.dispatcher.pump()

        assert pending.completion.result(timeout=0).status == "cancelled"
        assert runtime.bus.release_calls == 1
    finally:
        service.release.set()
        executor.shutdown(wait=True)


def test_provider_error_is_bounded_failed_terminal_and_retry_can_start() -> None:
    runtime = _runtime(FailingLessonService())
    pending = runtime.engine.teach(LessonRequest(query="missing"), count_in=0)
    runtime.settle()

    terminal = pending.completion.result()
    assert terminal.status == "failed"
    assert terminal.public["error"]["code"] == "SOURCE_REQUIRED"
    assert runtime.engine.phase is CoachPhase.FAILED


def test_blocked_resolution_stays_cancelling_and_prevents_replacement_until_settlement() -> None:
    service = BlockingLessonService(_lesson())
    executor = ThreadPoolExecutor(max_workers=1)
    runtime = _runtime(service, executor=executor)
    try:
        pending = runtime.engine.teach(
            LessonRequest(
                query="Test Melody",
                segment="main",
                author=AuthorMode.BOUNDED,
            ),
            count_in=0,
        )
        assert service.started.wait(timeout=2)
        runtime.advance_to(runtime.clock.now_ns + engine_module.AUTHORING_PHASE_POLL_NS)
        assert runtime.engine.phase is CoachPhase.AUTHORING
        stopped = runtime.engine.stop()

        assert stopped.public["code"] == "COACH_STOP_REQUESTED"
        assert runtime.engine.phase is CoachPhase.CANCELLING
        assert not pending.completion.done()
        with pytest.raises(CoachError, match="still cancelling") as caught:
            runtime.engine.teach(LessonRequest(query="replacement"), replace=True)
        assert caught.value.code == "PLAYBACK_ACTIVE"

        service.release.set()
        assert runtime.dispatcher.available.wait(timeout=2)
        runtime.dispatcher.pump()
        terminal = pending.completion.result(timeout=1)
        assert terminal.status == "cancelled"
        assert runtime.engine.phase is CoachPhase.CANCELLED
        assert runtime.bus.release_calls == 1
    finally:
        service.release.set()
        executor.shutdown(wait=True)


def test_timeout_terminal_waits_for_worker_cleanup_and_uses_timed_out_status() -> None:
    service = BlockingLessonService(_lesson())
    executor = ThreadPoolExecutor(max_workers=1)
    runtime = _runtime(service, executor=executor)
    try:
        pending = runtime.engine.teach(
            LessonRequest(query="Test Melody", segment="main", author=AuthorMode.BOUNDED),
            count_in=0,
        )
        assert service.started.wait(timeout=2)
        assert pending.cancel("timeout")
        assert not pending.completion.done()
        service.release.set()
        assert runtime.dispatcher.available.wait(timeout=2)
        runtime.dispatcher.pump()

        terminal = pending.completion.result(timeout=1)
        assert terminal.status == "timed_out"
        assert terminal.public["error"]["code"] == "SESSION_CANCELLED"
        assert runtime.bus.release_calls == 1
    finally:
        service.release.set()
        executor.shutdown(wait=True)


@pytest.mark.parametrize("failure_path", ("stop", "receipt", "scheduler"))
def test_queued_worker_settlement_never_precedes_cleanup_submission(failure_path: str) -> None:
    executor = QueuedExecutor()
    runtime = _runtime(StaticLessonService(_lesson()), executor=executor)
    runtime.engine._dispatcher = lambda callback: callback()
    pending = runtime.engine.teach(
        LessonRequest(query="Test Melody", segment="main", author=AuthorMode.BOUNDED),
        count_in=0,
    )
    cleanup_calls_at_terminal: list[int] = []
    pending.completion.add_done_callback(
        lambda _future: cleanup_calls_at_terminal.append(runtime.bus.release_calls)
    )
    session = runtime.engine._session
    assert session is not None

    if failure_path == "stop":
        runtime.engine.stop()
    elif failure_path == "receipt":
        runtime.engine._begin_receipt_failure(session)
    else:
        runtime.engine._handle_timing_failure(session)

    assert pending.completion.done()
    assert cleanup_calls_at_terminal == [1]
    assert runtime.bus.release_calls == 1


def test_late_stop_loses_commit_fence_then_distinct_playback_stop_cancels() -> None:
    service = CommitFenceLessonService(_lesson())
    executor = ThreadPoolExecutor(max_workers=1)
    runtime = _runtime(service, executor=executor)
    try:
        pending = runtime.engine.teach(
            LessonRequest(query="Test Melody", segment="main", author=AuthorMode.BOUNDED),
            count_in=0,
        )
        assert service.committing.wait(timeout=2)
        runtime.advance_to(runtime.clock.now_ns + engine_module.AUTHORING_PHASE_POLL_NS)
        session = runtime.engine._session
        assert session is not None
        assert runtime.engine.phase is CoachPhase.AUTHORING

        stopped = runtime.engine.stop()

        assert stopped.public["code"] == "COACH_FINISHING"
        assert "too late" in stopped.public["message"]
        assert "finishing" in stopped.public["message"]
        assert stopped.public["coach"]["phase"] == CoachPhase.AUTHORING.value
        assert session.cancel_reason is None
        assert CoachPhase.CANCELLING not in runtime.engine.transition_history
        assert not pending.completion.done()
        assert runtime.bus.release_calls == 0
        with pytest.raises(CoachError, match="still finishing") as caught:
            runtime.engine.teach(LessonRequest(query="replacement"), replace=True)
        assert caught.value.code == "PLAYBACK_ACTIVE"
        assert runtime.engine.phase is CoachPhase.AUTHORING
        assert session.cancel_reason is None
        assert runtime.bus.release_calls == 0

        service.release.set()
        assert runtime.dispatcher.available.wait(timeout=2)
        runtime.dispatcher.pump()

        assert runtime.engine.phase is CoachPhase.DEMONSTRATING
        assert session.resolution is not None
        assert session.cancel_reason is None
        assert runtime.bus.release_calls == 0

        playback_stop = runtime.engine.stop()
        assert playback_stop.public["code"] == "COACH_STOPPED"
        terminal = pending.completion.result(timeout=0)
        assert terminal.status == "cancelled"
        assert terminal.public["error"]["code"] == "SESSION_CANCELLED"
        assert runtime.bus.release_calls == 1
    finally:
        service.release.set()
        executor.shutdown(wait=True)


def test_late_timeout_loses_commit_fence_and_successful_teach_completes() -> None:
    service = CommitFenceLessonService(_lesson())
    executor = ThreadPoolExecutor(max_workers=1)
    runtime = _runtime(service, executor=executor)
    try:
        pending = runtime.engine.teach(
            LessonRequest(query="Test Melody", segment="main", author=AuthorMode.BOUNDED),
            count_in=0,
        )
        assert service.committing.wait(timeout=2)
        runtime.advance_to(runtime.clock.now_ns + engine_module.AUTHORING_PHASE_POLL_NS)
        session = runtime.engine._session
        assert session is not None

        assert pending.cancel("timeout") is False
        assert runtime.engine.phase is CoachPhase.AUTHORING
        assert session.cancel_reason is None
        assert CoachPhase.CANCELLING not in runtime.engine.transition_history
        assert not pending.completion.done()
        assert runtime.bus.release_calls == 0

        service.release.set()
        assert runtime.dispatcher.available.wait(timeout=2)
        runtime.dispatcher.pump()
        assert runtime.engine.phase is CoachPhase.DEMONSTRATING

        runtime.advance_to(runtime.clock.now_ns + 500_000_000)
        assert runtime.engine.phase is CoachPhase.CAPTURING
        runtime.advance_to(runtime.clock.now_ns + 500_000_000)

        terminal = pending.completion.result(timeout=0)
        assert terminal.status == "completed"
        assert terminal.public["code"] == "COACH_ATTEMPT_COMPLETE"
        assert runtime.engine.phase is CoachPhase.REVIEW
        assert CoachPhase.CANCELLED not in runtime.engine.transition_history
        assert runtime.bus.release_calls == 0
    finally:
        service.release.set()
        executor.shutdown(wait=True)


def test_unmount_after_commit_fence_never_starts_playback() -> None:
    service = CommitFenceLessonService(_lesson())
    executor = ThreadPoolExecutor(max_workers=1)
    runtime = _runtime(service, executor=executor)
    observed = []
    runtime.bus.subscribe(observed.append)
    try:
        pending = runtime.engine.teach(
            LessonRequest(query="Test Melody", segment="main", author=AuthorMode.BOUNDED),
            count_in=0,
        )
        assert service.committing.wait(timeout=2)
        runtime.advance_to(runtime.clock.now_ns + engine_module.AUTHORING_PHASE_POLL_NS)

        runtime.engine.unmount()

        session = runtime.engine._session
        assert session is not None
        assert runtime.engine.phase is CoachPhase.CANCELLING
        assert session.cancel_reason == "stop"
        assert runtime.bus.release_calls == 1
        assert not pending.completion.done()

        service.release.set()
        assert runtime.dispatcher.available.wait(timeout=2)
        runtime.dispatcher.pump()

        terminal = pending.completion.result(timeout=0)
        assert terminal.status == "cancelled"
        assert terminal.public["error"]["code"] == "SESSION_CANCELLED"
        assert runtime.engine.phase is CoachPhase.CANCELLED
        assert runtime.bus.release_calls == 1
        assert observed == []
    finally:
        service.release.set()
        executor.shutdown(wait=True)


def test_cleanup_submission_failure_stays_unsafe_after_worker_and_preserves_human_voice() -> None:
    service = BlockingLessonService(_lesson())
    executor = ThreadPoolExecutor(max_workers=1)
    runtime = _runtime(service, executor=executor)
    cleanup_calls = []

    def fail_cleanup(**kwargs):  # type: ignore[no-untyped-def]
        cleanup_calls.append(kwargs)
        raise RuntimeError("audio safety queue unavailable")

    runtime.bus.release_halcyon_voices = fail_cleanup  # type: ignore[method-assign]
    try:
        pending = runtime.engine.teach(
            LessonRequest(query="Test Melody", segment="main", author=AuthorMode.BOUNDED),
            count_in=0,
        )
        assert service.started.wait(timeout=2)
        human = MidiEventAdapter(origin=MidiOrigin.HUMAN, source_port="akai://lpk25")
        human_event = runtime.bus.publish(human.note_on(60, 100)).event

        assert pending.cancel("stop")
        assert runtime.engine.phase is CoachPhase.CANCELLING
        assert not pending.completion.done()
        assert cleanup_calls == [{"session_id": pending.session_id}]
        assert [voice.voice_id for voice in runtime.bus.active_voices(origin=MidiOrigin.HUMAN)] == [
            human_event.voice_id
        ]

        service.release.set()
        assert runtime.dispatcher.available.wait(timeout=2)
        runtime.dispatcher.pump()
        assert not pending.completion.done()
        assert runtime.engine.phase is CoachPhase.CANCELLING
        assert cleanup_calls == [{"session_id": pending.session_id}]
        assert [voice.voice_id for voice in runtime.bus.active_voices(origin=MidiOrigin.HUMAN)] == [
            human_event.voice_id
        ]
    finally:
        service.release.set()
        executor.shutdown(wait=True)


@pytest.mark.parametrize(
    ("fail_at_call", "count_in"),
    [(1, 0), (2, 0), (2, 2)],
)
def test_scheduler_exceptions_fail_playback_capture_or_count_in_without_stranding(
    fail_at_call: int,
    count_in: int,
) -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    scheduler = RaisingScheduler(runtime.clock, fail_at_call=fail_at_call)
    runtime.scheduler = scheduler
    runtime.engine._scheduler = scheduler
    pending = runtime.engine.teach(
        LessonRequest(query="Test Melody", segment="main"),
        count_in=count_in,
    )
    runtime.settle()
    if not pending.completion.done():
        runtime.advance_to(1_500_000_000)

    terminal = pending.completion.result(timeout=0)
    assert terminal.status == "failed"
    assert terminal.public["error"]["code"] == "COACH_RUNTIME_UNAVAILABLE"
    assert "path" not in terminal.public["error"]["message"]


def test_synchronous_reentrant_scheduler_cannot_overwrite_newer_handle_or_strand() -> None:
    clock = FakeClock()
    bus = CountingBus(clock=clock)
    scheduler = ReentrantScheduler(clock)
    engine = CoachEngine(
        StaticLessonService(_lesson()),
        bus,
        dispatcher=lambda callback: callback(),
        scheduler=scheduler,
        executor=InlineExecutor(),
        clock_ns=clock,
        capture_tail_us=0,
        default_count_in=0,
    )

    pending = engine.teach(
        LessonRequest(query="Test Melody", segment="main"),
        count_in=0,
    )

    assert pending.completion.result(timeout=0).status == "completed"
    assert engine.phase is CoachPhase.REVIEW
    assert engine._session.timing_handle is None  # type: ignore[union-attr]
    assert scheduler.calls == 2


def test_synchronous_scheduler_trampolines_pending_receipts_then_resumes_on_acceptance() -> None:
    clock = FakeClock()
    bus = CountingBus(clock=clock)
    scheduler = ReentrantScheduler(clock)
    audio = ReceiptConsumer(default="pending")
    bus.subscribe(audio, receipt_required=True)
    engine = CoachEngine(
        StaticLessonService(_lesson()),
        bus,
        dispatcher=lambda callback: callback(),
        scheduler=scheduler,
        executor=InlineExecutor(),
        clock_ns=clock,
        capture_tail_us=0,
        default_count_in=0,
    )

    pending = engine.teach(
        LessonRequest(query="Test Melody", segment="main"),
        count_in=0,
    )

    session = engine._session
    assert session is not None
    assert engine.phase is CoachPhase.DEMONSTRATING
    assert session.receipt_poll_deferred
    assert not pending.completion.done()
    assert scheduler.calls < 10

    for receipt in audio.receipts:
        receipt.state = "accepted"
    audio.default = "accepted"
    engine.state()

    assert pending.completion.result(timeout=0).status == "completed"
    assert engine.phase is CoachPhase.REVIEW
    assert not session.receipt_poll_deferred
    assert not session.receipt_poll_in_callback


def test_count_in_uses_one_rational_duration_and_allocates_fresh_attempt_identity() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    first = _teach(runtime)
    runtime.advance_to(1_500_000_000)
    first_feedback = runtime.engine.feedback()
    assert first.completion.done()
    first_attempt_id = first_feedback.public["report"]["attempt_id"]

    runtime.clock.now_ns = 3_000_000_000
    pending = runtime.engine.attempt(tempo_percent=75, count_in=2)
    state = runtime.engine.state().public["coach"]

    assert runtime.engine.phase is CoachPhase.COUNT_IN
    assert state["attempt_id"] != first_attempt_id
    assert runtime.scheduler.deadlines[-1] == 4_333_333_000
    pending.cancel()


def test_nested_report_envelopes_are_copies_not_mutable_engine_storage() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    _teach(runtime)
    runtime.advance_to(1_500_000_000)
    feedback = runtime.engine.feedback().public
    original_status = feedback["report"]["status"]
    feedback["report"]["status"] = "tampered"

    state = runtime.engine.state().public["coach"]
    assert state["last_report"]["status"] == original_status


def test_dispatcher_rejection_after_unmount_retains_late_settlement_for_owner_drain() -> None:
    service = BlockingLessonService(_lesson())
    executor = ThreadPoolExecutor(max_workers=1)
    clock = FakeClock()
    dispatcher = RejectingDispatcher()
    scheduler = FakeScheduler(clock)
    bus = CountingBus(clock=clock)
    engine = CoachEngine(
        service,
        bus,
        dispatcher=dispatcher,
        scheduler=scheduler,
        executor=executor,
        clock_ns=clock,
        default_count_in=0,
    )
    try:
        pending = engine.teach(
            LessonRequest(query="Test Melody", segment="main", author=AuthorMode.BOUNDED)
        )
        assert service.started.wait(timeout=2)
        engine.unmount()
        dispatcher.reject = True
        service.release.set()
        deadline = time.monotonic() + 2
        while engine._worker_future is not None and not engine._worker_future.done():
            assert time.monotonic() < deadline
            time.sleep(0.001)

        assert not pending.completion.done()
        assert engine.drain_deferred_callbacks() == 1
        assert pending.completion.result(timeout=0).status == "cancelled"
        assert bus.release_calls == 1
    finally:
        service.release.set()
        executor.shutdown(wait=True)


def test_executor_submit_error_uses_fixed_public_message() -> None:
    class FailingExecutor(Executor):
        def submit(self, fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("SECRET /private/provider/path")

    runtime = _runtime(StaticLessonService(_lesson()), executor=FailingExecutor())
    pending = runtime.engine.teach(LessonRequest(query="Test Melody"), count_in=0)

    terminal = pending.completion.result(timeout=0)
    assert terminal.status == "failed"
    assert terminal.public["error"]["message"] == "The lesson resolver could not start."
    assert "SECRET" not in repr(terminal.public)


def test_attempt_count_in_zero_is_immediate_and_reports_are_bounded_to_five() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    first = _teach(runtime)
    runtime.advance_to(1_500_000_000)
    runtime.engine.feedback()
    assert first.completion.done()

    for index in range(6):
        runtime.clock.now_ns = 3_000_000_000 + index * 1_000_000_000
        pending = runtime.engine.attempt(count_in=0)
        assert runtime.engine.phase is CoachPhase.CAPTURING
        runtime.engine.feedback()
        assert pending.completion.done()

    state = runtime.engine.state().public["coach"]
    assert state["reports_retained"] == 5


def test_unmount_is_idempotent_and_does_not_shutdown_injected_executor() -> None:
    class RecordingExecutor(InlineExecutor):
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            self.shutdown_calls += 1

    executor = RecordingExecutor()
    runtime = _runtime(StaticLessonService(_lesson()), executor=executor)
    _teach(runtime)

    runtime.engine.unmount()
    runtime.engine.unmount()

    assert executor.shutdown_calls == 0
    assert runtime.bus.release_calls == 1


def test_owner_thread_guard_rejects_direct_cross_thread_state_mutation() -> None:
    runtime = _runtime(StaticLessonService(_lesson()))
    errors = []

    thread = threading.Thread(target=lambda: _capture_error(runtime.engine.state, errors))
    thread.start()
    thread.join(timeout=2)

    assert isinstance(errors[0], RuntimeError)
    assert "owning app thread" in str(errors[0])


def _capture_error(callback, errors):  # type: ignore[no-untyped-def]
    try:
        callback()
    except Exception as exc:
        errors.append(exc)


def test_blocking_worker_never_calls_bus_from_its_thread() -> None:
    service = BlockingLessonService(_lesson())
    executor = ThreadPoolExecutor(max_workers=1)
    runtime = _runtime(service, executor=executor)
    try:
        pending = runtime.engine.teach(
            LessonRequest(query="Test Melody", segment="main", author=AuthorMode.BOUNDED),
            count_in=0,
        )
        assert service.started.wait(timeout=2)
        service.release.set()
        assert runtime.dispatcher.available.wait(timeout=2)
        assert runtime.engine.phase is CoachPhase.RESOLVING
        runtime.dispatcher.pump()
        assert runtime.engine.phase is CoachPhase.DEMONSTRATING
        pending.cancel()
    finally:
        service.release.set()
        executor.shutdown(wait=True)
