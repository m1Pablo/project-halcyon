"""Owner-thread runtime for deterministic MIDI lesson playback and coaching."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from concurrent.futures import CancelledError, Executor, Future, ThreadPoolExecutor
import copy
from dataclasses import dataclass, field, replace
from enum import StrEnum
import threading
import time
from typing import Any, Literal, Protocol
import uuid

from halcyon.coach_authoring import AuthoringControl, LessonService
from halcyon.coach_models import (
    AuthorMode,
    CoachError,
    Difficulty,
    Hand,
    LessonRequest,
    LessonResolution,
    LessonSelection,
    LessonTimeline,
    LoopSchedule,
    ScheduledLoopEvent,
    derive_runtime_event_id,
    resolve_loop_schedule,
    resolve_timeline,
)
from halcyon.coach_scoring import CoachingReport, ScoreRequest, score_attempt
from halcyon.command_executor import (
    CommandProgress,
    ImmediateCommandOutcome,
    PendingCommandOutcome,
    TerminalCommandOutcome,
)
from halcyon.event_bus import (
    DispatchReceipt,
    DispatchResult,
    HalcyonMidiAdapter,
    MidiEventBus,
    ReceiptPoll,
)
from halcyon.midi import MidiEvent, MidiOrigin

MAX_CAPTURE_EVENTS = 2_048
MAX_RETAINED_REPORTS = 5
AUTHORING_PHASE_POLL_NS = 10_000_000
RECEIPT_POLL_NS = 10_000_000


class CoachPhase(StrEnum):
    IDLE = "idle"
    RESOLVING = "resolving"
    AUTHORING = "authoring"
    READY = "ready"
    DEMONSTRATING = "demonstrating"
    COUNT_IN = "count_in"
    CAPTURING = "capturing"
    SCORING = "scoring"
    REVIEW = "review"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DeadlineHandle(Protocol):
    def cancel(self) -> object: ...


class DeadlineScheduler(Protocol):
    """One-shot absolute monotonic-nanosecond scheduling seam."""

    def call_at_ns(self, deadline_ns: int, callback: Callable[[], None]) -> DeadlineHandle: ...


class ThreadingDeadlineScheduler:
    """Production-neutral timer; its callback must be marshalled by the dispatcher."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._clock_ns = clock_ns

    def call_at_ns(self, deadline_ns: int, callback: Callable[[], None]) -> DeadlineHandle:
        delay_s = max(0.0, (deadline_ns - self._clock_ns()) / 1_000_000_000)
        timer = threading.Timer(delay_s, callback)
        timer.daemon = True
        timer.start()
        return timer


@dataclass
class _Operation:
    operation_id: str
    session_id: str
    completion: Future[TerminalCommandOutcome] = field(default_factory=Future)
    _subscribers: dict[int, Callable[[CommandProgress], None]] = field(default_factory=dict)
    _next_subscriber: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def subscribe(self, callback: Callable[[CommandProgress], None]) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("progress subscriber must be callable")
        with self._lock:
            token = self._next_subscriber
            self._next_subscriber += 1
            self._subscribers[token] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(token, None)

        return unsubscribe

    def publish(self, progress: CommandProgress) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.values())
        for subscriber in subscribers:
            try:
                subscriber(progress)
            except Exception:
                continue


@dataclass
class _RetainedReceipt:
    """One private admission handle retained until it proves terminal safety."""

    receipt: DispatchReceipt
    purpose: Literal["playback", "cleanup"]
    event_id: str
    last_state: Literal["pending", "rejected_unsafe"] = "pending"


@dataclass
class _CoachSession:
    session_id: str
    phase: CoachPhase = CoachPhase.IDLE
    operation: _Operation | None = None
    resolution: LessonResolution | None = None
    timeline: LessonTimeline | None = None
    selection: LessonSelection | None = None
    loops: int = 1
    count_in: int = 4
    playback_id: str | None = None
    playback_purpose: Literal["teach", "replay"] | None = None
    playback_schedule: LoopSchedule | None = None
    playback_index: int = 0
    playback_anchor_ns: int | None = None
    previous_playback_offset_us: int = 0
    adapter: HalcyonMidiAdapter | None = None
    timing_handle: DeadlineHandle | None = None
    timing_generation: int = 0
    attempt_id: str | None = None
    capture_started_ns: int | None = None
    capture_ended_ns: int | None = None
    capture_duration_us: int | None = None
    capture_events: list[MidiEvent] = field(default_factory=list)
    capture_truncated: bool = False
    cancel_reason: Literal["stop", "timeout"] | None = None
    cleanup_error: CoachError | None = None
    pending_failure: CoachError | None = None
    cleanup_done: bool = False
    cleanup_submitted: bool = False
    receipt_protocol_unsafe: bool = False
    receipt_failed_safe: bool = False
    retained_receipts: list[_RetainedReceipt] = field(default_factory=list)
    receipt_poll_in_callback: bool = False
    receipt_poll_deferred: bool = False
    transitions: list[CoachPhase] = field(default_factory=lambda: [CoachPhase.IDLE])


class CoachEngine:
    """Run one local MIDI coach session through the app-owned event bus."""

    def __init__(
        self,
        lesson_service: LessonService,
        bus: MidiEventBus,
        *,
        dispatcher: Callable[[Callable[[], None]], object],
        scheduler: DeadlineScheduler,
        executor: Executor | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        capture_tail_us: int = 500_000,
        default_count_in: int = 4,
    ) -> None:
        if not 0 <= capture_tail_us <= 5_000_000:
            raise ValueError("capture_tail_us must be between 0 and 5000000")
        if not 0 <= default_count_in <= 8:
            raise ValueError("default_count_in must be between 0 and 8")
        self._lesson_service = lesson_service
        self._bus = bus
        self._dispatcher = dispatcher
        self._scheduler = scheduler
        self._clock_ns = clock_ns
        self._capture_tail_us = int(capture_tail_us)
        self._default_count_in = int(default_count_in)
        self._owner_thread_id = threading.get_ident()
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="halcyon-coach-authoring",
        )
        self._owns_executor = executor is None
        self._worker_future: Future[LessonResolution] | None = None
        self._worker_control: AuthoringControl | None = None
        self._worker_session_id: str | None = None
        self._worker_operation_id: str | None = None
        self._session: _CoachSession | None = None
        self._reports: deque[dict[str, Any]] = deque(maxlen=MAX_RETAINED_REPORTS)
        self._deferred_owner_callbacks: deque[Callable[[], None]] = deque()
        self._deferred_lock = threading.Lock()
        self._capture_subscription = self._bus.subscribe(self.observe_event)
        self._unmounted = False

    @property
    def phase(self) -> CoachPhase:
        return self._session.phase if self._session is not None else CoachPhase.IDLE

    @property
    def session_id(self) -> str | None:
        return self._session.session_id if self._session is not None else None

    @property
    def active_operation_id(self) -> str | None:
        operation = self._session.operation if self._session is not None else None
        if operation is None or operation.completion.done():
            return None
        return operation.operation_id

    @property
    def transition_history(self) -> tuple[CoachPhase, ...]:
        if self._session is None:
            return (CoachPhase.IDLE,)
        return tuple(self._session.transitions)

    def teach(
        self,
        request: LessonRequest,
        *,
        loops: int = 1,
        count_in: int | None = None,
        deadline_monotonic: float | None = None,
        replace: bool = False,
    ) -> PendingCommandOutcome:
        self._enter_owner()
        self._ensure_mounted()
        self._validate_loops(loops)
        selected_count_in = self._default_count_in if count_in is None else count_in
        self._validate_count_in(selected_count_in)
        self._prepare_replacement(replace=replace)
        if self._worker_future is not None:
            raise self._playback_active("The lesson resolver is still settling.")

        session = _CoachSession(
            session_id=f"coach-{uuid.uuid4().hex}",
            loops=loops,
            count_in=selected_count_in,
        )
        self._session = session
        operation = self._begin_operation(session, CoachPhase.RESOLVING, "Resolving lesson")
        control = AuthoringControl(
            deadline_monotonic=deadline_monotonic,
            clock=lambda: self._clock_ns() / 1_000_000_000,
        )
        self._worker_control = control
        self._worker_session_id = session.session_id
        self._worker_operation_id = operation.operation_id
        try:
            future = self._executor.submit(
                self._lesson_service.resolve,
                request,
                control=control,
            )
        except Exception:
            self._clear_worker()
            self._fail(
                session,
                self._runtime_error(
                    "AUTHORING_UNAVAILABLE",
                    "The lesson resolver could not start.",
                ),
            )
            return self._pending(session, operation)
        self._worker_future = future
        future.add_done_callback(
            lambda settled, session_id=session.session_id, operation_id=operation.operation_id: (
                self._post_to_owner(
                    lambda: self._settle_resolution(session_id, operation_id, settled)
                )
            )
        )
        self._arm_authoring_monitor(session, operation)
        return self._pending(session, operation)

    def replay(
        self,
        *,
        segment: str | None = None,
        tempo_percent: int | None = None,
        loops: int | None = None,
    ) -> PendingCommandOutcome:
        self._enter_owner()
        session, resolution = self._require_lesson()
        self._ensure_idle_operation(session)
        selected_loops = session.loops if loops is None else loops
        self._validate_loops(selected_loops)
        timeline, selection = self._updated_timeline(
            session,
            resolution,
            segment=segment,
            tempo_percent=tempo_percent,
        )
        session.timeline = timeline
        session.selection = selection
        session.loops = selected_loops
        operation = self._begin_operation(session, CoachPhase.DEMONSTRATING, "Replaying lesson")
        self._start_playback(session, purpose="replay", loops=selected_loops)
        return self._pending(session, operation)

    def attempt(
        self,
        *,
        segment: str | None = None,
        tempo_percent: int | None = None,
        count_in: int | None = None,
    ) -> PendingCommandOutcome:
        self._enter_owner()
        session, resolution = self._require_lesson()
        self._ensure_idle_operation(session)
        selected_count_in = session.count_in if count_in is None else count_in
        self._validate_count_in(selected_count_in)
        timeline, selection = self._updated_timeline(
            session,
            resolution,
            segment=segment,
            tempo_percent=tempo_percent,
        )
        session.timeline = timeline
        session.selection = selection
        session.count_in = selected_count_in
        session.capture_duration_us = resolve_loop_schedule(
            resolution.lesson, timeline, 1
        ).total_duration_us
        operation = self._begin_operation(session, CoachPhase.COUNT_IN, "Preparing attempt")
        self._start_count_in(session)
        return self._pending(session, operation)

    def feedback(self, attempt_id: str | None = None) -> ImmediateCommandOutcome:
        self._enter_owner()
        session = self._session
        if session is not None and session.phase is CoachPhase.CAPTURING:
            if attempt_id is None or attempt_id == session.attempt_id:
                self._cancel_timing(session)
                session.capture_ended_ns = self._clock_ns()
                self._finish_capture(session)
                attempt_id = session.attempt_id

        report = self._find_report(attempt_id)
        if report is None:
            raise CoachError(
                "NO_ATTEMPT",
                "No bounded coaching report matches this request.",
                field="attempt_id",
                resolution="Complete /coach attempt, then retry /coach feedback.",
                retryable=True,
            )
        return ImmediateCommandOutcome(
            {
                "ok": True,
                "code": "COACH_FEEDBACK",
                "message": "Deterministic coaching feedback is ready.",
                "report": copy.deepcopy(report),
                "coach": self._state_public(),
            }
        )

    def state(self) -> ImmediateCommandOutcome:
        self._enter_owner()
        return ImmediateCommandOutcome(
            {
                "ok": True,
                "code": "COACH_STATE",
                "message": "Bounded coach state.",
                "coach": self._state_public(),
            }
        )

    def stop(self) -> ImmediateCommandOutcome:
        self._enter_owner()
        session = self._session
        if session is None:
            return ImmediateCommandOutcome(
                {
                    "ok": True,
                    "code": "COACH_STOPPED",
                    "message": "No coach work is active.",
                    "coach": self._state_public(),
                }
            )
        disposition = self._request_cancel(session, "stop")
        if disposition == "finishing":
            return ImmediateCommandOutcome(
                {
                    "ok": True,
                    "code": "COACH_FINISHING",
                    "message": (
                        "Coach resolution has crossed its cancellation fence; "
                        "this stop is too late and the operation is finishing."
                    ),
                    "coach": self._state_public(),
                }
            )
        pending = disposition == "pending"
        return ImmediateCommandOutcome(
            {
                "ok": True,
                "code": "COACH_STOP_REQUESTED" if pending else "COACH_STOPPED",
                "message": (
                    "Coach cancellation is settling."
                    if pending
                    else "Coach work stopped and Halcyon voices were released."
                ),
                "coach": self._state_public(),
            }
        )

    def unmount(self) -> None:
        self._enter_owner()
        if self._unmounted:
            return
        # Teardown is stronger than a late interactive stop. Persistence that already
        # owns the authoring commit fence may finish, but the unmounted runtime must
        # never turn that result into new playback.
        self._unmounted = True
        if self._session is not None:
            disposition = self._request_cancel(self._session, "stop")
            if disposition == "finishing":
                session = self._session
                session.cancel_reason = "stop"
                self._set_phase(session, CoachPhase.CANCELLING)
                self._progress(session, "cancelling", "Closing coach work safely", 95)
                self._cancel_timing(session)
                self._submit_cleanup_once(session)
                self._try_settle_cancellation(session)
        self._bus.unsubscribe(self._capture_subscription)
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)  # type: ignore[attr-defined]

    def observe_event(self, event: MidiEvent) -> None:
        """Capture one already-dispatched human event without republishing it."""

        self._assert_owner_thread()
        session = self._session
        if session is None or session.phase is not CoachPhase.CAPTURING:
            return
        if event.origin is not MidiOrigin.HUMAN:
            return
        started = session.capture_started_ns
        ended = session.capture_ended_ns
        if started is None or ended is None:
            return
        observed_ns = event.dispatched_monotonic_ns
        if observed_ns < started or observed_ns > ended:
            return
        if len(session.capture_events) >= MAX_CAPTURE_EVENTS:
            session.capture_truncated = True
            return
        session.capture_events.append(
            replace(
                event,
                session_id=session.session_id,
                attempt_id=session.attempt_id,
            )
        )

    def drain_deferred_callbacks(self) -> int:
        """Run callbacks retained when an app dispatcher rejected a late post."""

        self._assert_owner_thread()
        return self._drain_deferred_callbacks()

    # Internal tool-host aliases. They remain reachable only through menu.xml leaves.
    def coach_tool_start_lesson(self, **arguments: Any) -> PendingCommandOutcome:
        request = LessonRequest(
            query=str(arguments["query"]),
            segment=arguments.get("segment"),
            tempo_percent=int(arguments.get("tempo_percent", 100)),
            difficulty=(
                Difficulty(arguments["difficulty"])
                if arguments.get("difficulty") is not None
                else None
            ),
            hand=Hand(arguments["hand"]) if arguments.get("hand") is not None else None,
            author=AuthorMode(arguments.get("author", "never")),
        )
        return self.teach(request, loops=int(arguments.get("loops", 1)))

    def coach_tool_replay_lesson(self, **arguments: Any) -> PendingCommandOutcome:
        return self.replay(
            segment=arguments.get("segment"),
            tempo_percent=arguments.get("tempo_percent"),
            loops=arguments.get("loops"),
        )

    def coach_tool_start_attempt(self, **arguments: Any) -> PendingCommandOutcome:
        return self.attempt(
            segment=arguments.get("segment"),
            tempo_percent=arguments.get("tempo_percent"),
            count_in=arguments.get("count_in"),
        )

    def coach_tool_feedback(self, attempt_id: str | None = None) -> ImmediateCommandOutcome:
        return self.feedback(attempt_id)

    def coach_tool_state(self) -> ImmediateCommandOutcome:
        return self.state()

    def coach_tool_stop(self) -> ImmediateCommandOutcome:
        return self.stop()

    def _arm_authoring_monitor(self, session: _CoachSession, operation: _Operation) -> None:
        future = self._worker_future
        if future is None or future.done():
            return
        self._arm_timing(
            session,
            self._clock_ns() + AUTHORING_PHASE_POLL_NS,
            lambda: self._poll_authoring_phase(
                session.session_id,
                operation.operation_id,
            ),
        )

    def _poll_authoring_phase(self, session_id: str, operation_id: str) -> None:
        session = self._session
        future = self._worker_future
        control = self._worker_control
        if (
            session is None
            or session.session_id != session_id
            or session.operation is None
            or session.operation.operation_id != operation_id
            or session.cancel_reason is not None
            or future is None
        ):
            return
        checkpoint = control.last_checkpoint if control is not None else None
        if checkpoint is not None and checkpoint.startswith("authoring."):
            self._set_phase(session, CoachPhase.AUTHORING)
            self._progress(session, "authoring", "Bounded lesson authoring is in progress", 10)
        if not future.done():
            self._arm_authoring_monitor(session, session.operation)

    def _settle_resolution(
        self,
        session_id: str,
        operation_id: str,
        future: Future[LessonResolution],
    ) -> None:
        self._assert_owner_thread()
        session = self._session
        if (
            session is None
            or session.session_id != session_id
            or session.operation is None
            or session.operation.operation_id != operation_id
        ):
            return
        control = self._worker_control
        self._cancel_timing(session)
        self._clear_worker()
        try:
            resolution = future.result()
        except CancelledError:
            session.cancel_reason = session.cancel_reason or "stop"
            self._try_settle_cancellation(session)
            return
        except CoachError as exc:
            reason = session.cancel_reason or (control.reason if control is not None else None)
            if (
                session.pending_failure is not None
                or reason is not None
                or exc.code == "SESSION_CANCELLED"
            ):
                session.cancel_reason = reason or "stop"
                self._try_settle_cancellation(session)
            else:
                self._fail(session, exc)
            return
        except Exception:
            if session.pending_failure is not None:
                session.cancel_reason = session.cancel_reason or "stop"
                self._try_settle_cancellation(session)
                return
            self._fail(
                session,
                self._runtime_error(
                    "AUTHORING_UNAVAILABLE",
                    "The lesson resolver failed before a reusable lesson was ready.",
                ),
            )
            return

        if session.pending_failure is not None:
            session.cancel_reason = session.cancel_reason or "stop"
            self._try_settle_cancellation(session)
            return
        if session.cancel_reason is not None:
            self._try_settle_cancellation(session)
            return
        if resolution.authored:
            self._set_phase(session, CoachPhase.AUTHORING)
            self._progress(session, "authoring", "Bounded lesson authored and validated", 20)
        session.resolution = resolution
        session.timeline = resolution.timeline
        session.selection = LessonSelection.from_request(
            LessonRequest(
                query=resolution.matched_alias,
                segment=resolution.timeline.segment_label,
                tempo_percent=resolution.timeline.tempo_percent,
                difficulty=resolution.timeline.difficulty,
                hand=resolution.timeline.hand,
            )
        )
        self._set_phase(session, CoachPhase.READY)
        self._progress(session, "ready", "Lesson ready for demonstration", 25)
        self._start_playback(session, purpose="teach", loops=session.loops)

    def _start_playback(
        self,
        session: _CoachSession,
        *,
        purpose: Literal["teach", "replay"],
        loops: int,
    ) -> None:
        resolution = session.resolution
        timeline = session.timeline
        if resolution is None or timeline is None:
            self._fail(session, self._runtime_error("NO_ACTIVE_LESSON", "No lesson is ready."))
            return
        try:
            schedule = resolve_loop_schedule(resolution.lesson, timeline, loops)
        except CoachError as exc:
            self._fail(session, exc)
            return
        session.playback_id = f"playback-{uuid.uuid4().hex}"
        session.playback_purpose = purpose
        session.playback_schedule = schedule
        if purpose == "teach":
            session.capture_duration_us = schedule.boundaries_us[1]
        session.playback_index = 0
        session.previous_playback_offset_us = 0
        session.adapter = HalcyonMidiAdapter(
            source_port=f"halcyon://coach/{resolution.lesson.lesson_id}",
            session_id=session.session_id,
        )
        self._set_phase(session, CoachPhase.DEMONSTRATING)
        self._progress(session, "demonstrating", "Halcyon is demonstrating the lesson", 30)
        anchor_ns = self._clock_ns()
        session.playback_anchor_ns = anchor_ns
        self._drain_playback(session, now_ns=anchor_ns)

    def _drain_playback(self, session: _CoachSession, *, now_ns: int | None = None) -> None:
        if session.phase is not CoachPhase.DEMONSTRATING:
            return
        schedule = session.playback_schedule
        anchor_ns = session.playback_anchor_ns
        if schedule is None or anchor_ns is None:
            return
        current_ns = self._clock_ns() if now_ns is None else now_ns
        receipt_state = self._poll_receipts(session)
        if session.pending_failure is not None or receipt_state in {
            "rejected_unsafe",
            "failed_safe",
        }:
            self._begin_receipt_failure(session)
            return
        events = schedule.events
        try:
            while session.playback_index < len(events):
                scheduled = events[session.playback_index]
                deadline_ns = anchor_ns + scheduled.offset_us * 1_000
                if deadline_ns > current_ns:
                    break
                self._publish_playback_event(
                    session,
                    scheduled,
                    deadline_ns,
                    dispatched_ns=current_ns,
                )
                session.playback_index += 1
                receipt_state = self._poll_receipts(session)
                if session.pending_failure is not None or receipt_state in {
                    "rejected_unsafe",
                    "failed_safe",
                }:
                    self._begin_receipt_failure(session)
                    return
        except CoachError as exc:
            self._fail(session, exc)
            return

        next_deadline_ns: int
        if session.playback_index < len(events):
            next_event = events[session.playback_index]
            next_deadline_ns = anchor_ns + next_event.offset_us * 1_000
        else:
            next_deadline_ns = anchor_ns + schedule.total_duration_us * 1_000
            if current_ns >= next_deadline_ns:
                if receipt_state == "accepted":
                    self._finish_playback(session)
                else:
                    self._arm_receipt_poll(session)
                return
        if receipt_state == "pending":
            receipt_deadline_ns = current_ns + RECEIPT_POLL_NS
            if receipt_deadline_ns < next_deadline_ns:
                self._arm_receipt_poll(session)
                return
        self._arm_timing(
            session,
            next_deadline_ns,
            lambda: self._drain_playback(session),
        )

    def _publish_playback_event(
        self,
        session: _CoachSession,
        scheduled: ScheduledLoopEvent,
        deadline_ns: int,
        *,
        dispatched_ns: int,
    ) -> None:
        adapter = session.adapter
        resolution = session.resolution
        playback_id = session.playback_id
        anchor_ns = session.playback_anchor_ns
        if adapter is None or resolution is None or playback_id is None or anchor_ns is None:
            raise self._runtime_error("AUDIO_BACKPRESSURE", "Playback adapter is unavailable.")
        lesson_event = scheduled.event
        delta_us = max(0, scheduled.offset_us - session.previous_playback_offset_us)
        if lesson_event.event_type == "note_on":
            event = adapter.note_on(
                lesson_event.note,
                lesson_event.velocity,
                channel=lesson_event.channel,
                elapsed=scheduled.offset_us / 1_000_000,
                delta_time=delta_us / 1_000_000,
            )
        else:
            event = adapter.note_off(
                lesson_event.note,
                channel=lesson_event.channel,
                elapsed=scheduled.offset_us / 1_000_000,
                delta_time=delta_us / 1_000_000,
            )
        event = replace(
            event,
            event_id=derive_runtime_event_id(
                playback_id,
                scheduled.loop_index,
                lesson_event.lesson_event_id,
            ),
            lesson_id=resolution.lesson.lesson_id,
            lesson_event_id=lesson_event.lesson_event_id,
            playback_id=playback_id,
            status=lesson_event.status,
            raw_message=(lesson_event.status, lesson_event.note, lesson_event.velocity),
            received_monotonic_ns=dispatched_ns,
            scheduled_monotonic_ns=deadline_ns,
            delta_us=delta_us,
        )
        result = self._bus.publish(event)
        session.previous_playback_offset_us = scheduled.offset_us
        self._retain_dispatch_receipts(session, result, purpose="playback")

    def _finish_playback(self, session: _CoachSession) -> None:
        self._cancel_timing(session)
        session.retained_receipts.clear()
        session.receipt_protocol_unsafe = False
        session.receipt_failed_safe = False
        session.adapter = None
        session.playback_schedule = None
        session.playback_anchor_ns = None
        if session.playback_purpose == "teach":
            self._start_count_in(session)
            return
        session.playback_purpose = None
        self._set_phase(session, CoachPhase.READY)
        self._complete(
            session,
            code="COACH_REPLAY_COMPLETE",
            message="Lesson replay completed.",
        )

    def _start_count_in(self, session: _CoachSession) -> None:
        session.playback_purpose = None
        session.attempt_id = f"attempt-{uuid.uuid4().hex}"
        session.capture_started_ns = None
        session.capture_ended_ns = None
        session.capture_events.clear()
        session.capture_truncated = False
        self._set_phase(session, CoachPhase.COUNT_IN)
        self._progress(session, "count_in", "Count-in started", 55)
        if session.count_in == 0:
            self._start_capture(session)
            return
        timeline = session.timeline
        resolution = session.resolution
        if timeline is None or resolution is None:
            self._fail(session, self._runtime_error("NO_ACTIVE_LESSON", "No lesson is ready."))
            return
        deadline_ns = (
            self._clock_ns()
            + self._count_in_duration_us(
                resolution,
                timeline,
                session.count_in,
            )
            * 1_000
        )
        self._arm_timing(session, deadline_ns, lambda: self._start_capture(session))

    def _start_capture(self, session: _CoachSession) -> None:
        if session.cancel_reason is not None:
            self._finish_cancel(session)
            return
        timeline = session.timeline
        if timeline is None:
            self._fail(session, self._runtime_error("NO_ACTIVE_LESSON", "No lesson is ready."))
            return
        self._cancel_timing(session)
        session.capture_events.clear()
        session.capture_truncated = False
        session.capture_started_ns = self._clock_ns()
        duration_us = session.capture_duration_us
        if duration_us is None:
            resolution = session.resolution
            if resolution is None:
                self._fail(session, self._runtime_error("NO_ACTIVE_LESSON", "No lesson is ready."))
                return
            try:
                duration_us = resolve_loop_schedule(
                    resolution.lesson, timeline, 1
                ).total_duration_us
            except CoachError as exc:
                self._fail(session, exc)
                return
            session.capture_duration_us = duration_us
        session.capture_ended_ns = (
            session.capture_started_ns + (duration_us + self._capture_tail_us) * 1_000
        )
        self._set_phase(session, CoachPhase.CAPTURING)
        self._progress(session, "capturing", "Play the lesson now", 65)
        self._arm_timing(
            session,
            session.capture_ended_ns,
            lambda: self._finish_capture(session),
        )

    def _finish_capture(self, session: _CoachSession) -> None:
        if session.phase is not CoachPhase.CAPTURING:
            return
        self._cancel_timing(session)
        ended_ns = min(session.capture_ended_ns or self._clock_ns(), self._clock_ns())
        session.capture_ended_ns = ended_ns
        self._set_phase(session, CoachPhase.SCORING)
        self._progress(session, "scoring", "Scoring the bounded human attempt", 85)
        timeline = session.timeline
        attempt_id = session.attempt_id
        started_ns = session.capture_started_ns
        observed = tuple(session.capture_events)
        truncated = session.capture_truncated
        session.capture_events.clear()
        if timeline is None or attempt_id is None or started_ns is None:
            self._fail(session, self._runtime_error("NO_ATTEMPT", "Attempt state is incomplete."))
            return
        try:
            report: CoachingReport = score_attempt(
                ScoreRequest(
                    attempt_id=attempt_id,
                    session_id=session.session_id,
                    timeline=timeline,
                    observed_events=observed,
                    capture_started_ns=started_ns,
                    capture_ended_ns=ended_ns,
                    truncated=truncated,
                )
            )
        except Exception:
            self._fail(
                session,
                self._runtime_error(
                    "LESSON_CORRUPT",
                    "The bounded attempt could not be scored against this lesson.",
                ),
            )
            return
        public_report = report.to_public_dict()
        self._reports.append(copy.deepcopy(public_report))
        self._set_phase(session, CoachPhase.REVIEW)
        self._complete(
            session,
            code="COACH_ATTEMPT_COMPLETE",
            message="Attempt scored; deterministic feedback is ready.",
            report=public_report,
        )

    def _request_cancel(
        self, session: _CoachSession, reason: Literal["stop", "timeout"]
    ) -> Literal["pending", "settled", "finishing"]:
        operation = session.operation
        if operation is None or operation.completion.done():
            self._set_phase(session, CoachPhase.CANCELLING)
            self._submit_cleanup_once(session)
            receipt_state = self._poll_receipts(session)
            if receipt_state == "accepted":
                self._set_phase(session, CoachPhase.CANCELLED)
            elif receipt_state == "failed_safe":
                self._set_phase(session, CoachPhase.FAILED)
            else:
                self._arm_receipt_poll(session)
            return "pending" if receipt_state != "accepted" else "settled"
        if session.cancel_reason is not None:
            return "pending"
        if self._worker_control is not None and self._worker_session_id == session.session_id:
            accepted = self._worker_control.request_cancel(reason)
            control_reason = self._worker_control.reason
            if not accepted and control_reason is None:
                return "finishing"
            reason = control_reason or reason
        session.cancel_reason = reason
        self._set_phase(session, CoachPhase.CANCELLING)
        self._progress(session, "cancelling", "Cancelling coach work safely", 95)
        self._cancel_timing(session)
        # Future.cancel() may synchronously invoke settlement callbacks on this
        # owner thread. Submit cleanup first so no terminal observer can run before
        # owner-scoped release is represented by its admission receipts.
        self._submit_cleanup_once(session)
        if self._worker_future is not None and self._worker_session_id == session.session_id:
            self._worker_future.cancel()
        self._try_settle_cancellation(session)
        return "pending" if not operation.completion.done() else "settled"

    def _finish_cancel(self, session: _CoachSession) -> None:
        self._try_settle_cancellation(session)

    def _cleanup_once(self, session: _CoachSession) -> None:
        """Compatibility wrapper for the receipt-aware one-shot cleanup submission."""

        self._submit_cleanup_once(session)

    def _retain_dispatch_receipts(
        self,
        session: _CoachSession,
        result: DispatchResult,
        *,
        purpose: Literal["playback", "cleanup"],
    ) -> None:
        """Retain every unresolved required receipt without exposing it publicly."""

        if not result.accepted or result.deduplicated:
            session.receipt_protocol_unsafe = True
        if len(result.receipts) != result.required_receipt_count:
            session.receipt_protocol_unsafe = True
        if result.consumer_errors:
            session.pending_failure = session.pending_failure or self._audio_backpressure_error()
        for receipt in result.receipts:
            retained = _RetainedReceipt(
                receipt=receipt,
                purpose=purpose,
                event_id=result.event.event_id,
            )
            state = self._poll_one_receipt(session, retained)
            if state == "accepted":
                continue
            if state == "failed_safe":
                session.receipt_failed_safe = True
                continue
            retained.last_state = state
            session.retained_receipts.append(retained)

    def _poll_one_receipt(
        self,
        session: _CoachSession,
        retained: _RetainedReceipt,
    ) -> Literal["pending", "accepted", "rejected_unsafe", "failed_safe"]:
        try:
            snapshot = retained.receipt.poll()
        except Exception:
            session.receipt_protocol_unsafe = True
            return "rejected_unsafe"
        if not isinstance(snapshot, ReceiptPoll):
            session.receipt_protocol_unsafe = True
            return "rejected_unsafe"
        return snapshot.state

    def _poll_receipts(
        self, session: _CoachSession
    ) -> Literal["pending", "accepted", "rejected_unsafe", "failed_safe"]:
        unresolved: list[_RetainedReceipt] = []
        saw_rejected = False
        for retained in session.retained_receipts:
            state = self._poll_one_receipt(session, retained)
            if state == "accepted":
                continue
            if state == "failed_safe":
                session.receipt_failed_safe = True
                continue
            retained.last_state = state
            unresolved.append(retained)
            saw_rejected = saw_rejected or state == "rejected_unsafe"
        session.retained_receipts = unresolved
        if session.receipt_protocol_unsafe:
            return "rejected_unsafe"
        if saw_rejected:
            return "rejected_unsafe"
        if unresolved:
            return "pending"
        if session.receipt_failed_safe:
            return "failed_safe"
        return "accepted"

    def _begin_receipt_failure(self, session: _CoachSession) -> None:
        if session.pending_failure is None:
            session.pending_failure = self._audio_backpressure_error()
        session.cancel_reason = session.cancel_reason or "stop"
        self._set_phase(session, CoachPhase.CANCELLING)
        self._progress(session, "cancelling", "Waiting for safe audio admission", 95)
        self._cancel_timing(session)
        if self._worker_control is not None and self._worker_session_id == session.session_id:
            self._worker_control.request_cancel("stop")
        self._submit_cleanup_once(session)
        if self._worker_future is not None and self._worker_session_id == session.session_id:
            self._worker_future.cancel()
        self._try_settle_cancellation(session)

    def _submit_cleanup_once(self, session: _CoachSession) -> None:
        if session.cleanup_submitted:
            return
        session.cleanup_submitted = True
        session.cleanup_done = True
        self._cancel_timing(session)
        session.adapter = None
        session.playback_schedule = None
        session.playback_anchor_ns = None
        session.capture_events.clear()
        try:
            results = self._bus.release_halcyon_voices(session_id=session.session_id)
        except Exception:
            session.receipt_protocol_unsafe = True
            session.pending_failure = self._audio_backpressure_error()
            return
        for result in results:
            self._retain_dispatch_receipts(session, result, purpose="cleanup")

    def _try_settle_cancellation(self, session: _CoachSession) -> None:
        operation = session.operation
        if operation is None or operation.completion.done():
            return
        receipt_state = self._poll_receipts(session)
        worker_pending = self._worker_session_id == session.session_id
        if worker_pending:
            if receipt_state in {"pending", "rejected_unsafe"}:
                self._arm_receipt_poll(session)
            return
        if receipt_state == "failed_safe":
            self._fail(session, self._audio_backpressure_error(), cleanup=False)
            return
        if receipt_state in {"pending", "rejected_unsafe"}:
            self._arm_receipt_poll(session)
            return
        if session.pending_failure is not None:
            self._fail(session, session.pending_failure, cleanup=False)
            return
        reason = session.cancel_reason or "stop"
        self._set_phase(session, CoachPhase.CANCELLED)
        error = CoachError(
            "SESSION_CANCELLED",
            "Coach work was cancelled after owner-scoped cleanup.",
            resolution="Start a new /coach request when ready.",
            retryable=True,
        )
        self._terminal(
            session,
            status="timed_out" if reason == "timeout" else "cancelled",
            public=self._error_public(session, error),
        )

    def _arm_receipt_poll(self, session: _CoachSession) -> None:
        if session.receipt_poll_in_callback:
            session.receipt_poll_deferred = True
            return
        self._schedule_receipt_poll(session)

    def _schedule_receipt_poll(self, session: _CoachSession) -> None:
        self._arm_timing(
            session,
            self._clock_ns() + RECEIPT_POLL_NS,
            lambda: self._run_receipt_poll(session),
            allow_cancelling=True,
        )

    def _run_receipt_poll(self, session: _CoachSession) -> None:
        """Poll once and trampoline a synchronous scheduler without recursion."""

        if session.receipt_poll_in_callback:
            session.receipt_poll_deferred = True
            return
        session.receipt_poll_in_callback = True
        session.receipt_poll_deferred = False
        try:
            self._receipt_poll_due(session)
            if session.receipt_poll_deferred:
                session.receipt_poll_deferred = False
                # Keep the callback guard set while arming. An adversarial scheduler
                # that fires synchronously can request one later owner-thread poll,
                # but cannot recurse into another polling stack frame.
                self._schedule_receipt_poll(session)
        finally:
            session.receipt_poll_in_callback = False

    def _receipt_poll_due(self, session: _CoachSession) -> None:
        if session.phase is CoachPhase.DEMONSTRATING:
            self._drain_playback(session)
            return
        if session.phase is CoachPhase.CANCELLING:
            operation = session.operation
            if operation is not None and not operation.completion.done():
                self._try_settle_cancellation(session)
                return
            state = self._poll_receipts(session)
            if state == "accepted":
                self._set_phase(session, CoachPhase.CANCELLED)
            elif state == "failed_safe":
                self._set_phase(session, CoachPhase.FAILED)
            else:
                self._arm_receipt_poll(session)

    @staticmethod
    def _audio_backpressure_error() -> CoachError:
        return CoachError(
            "AUDIO_BACKPRESSURE",
            "Audio admission did not reach a confirmed safe state.",
            resolution="Stop or restart the audio integration, then retry.",
            retryable=True,
        )

    def _fail(self, session: _CoachSession, error: CoachError, *, cleanup: bool = True) -> None:
        if cleanup:
            session.pending_failure = error
            session.cancel_reason = session.cancel_reason or "stop"
            self._set_phase(session, CoachPhase.CANCELLING)
            self._cancel_timing(session)
            self._submit_cleanup_once(session)
            self._try_settle_cancellation(session)
            return
        self._set_phase(session, CoachPhase.FAILED)
        self._terminal(session, status="failed", public=self._error_public(session, error))

    def _complete(
        self,
        session: _CoachSession,
        *,
        code: str,
        message: str,
        report: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "ok": True,
            "code": code,
            "message": message,
            "coach": self._state_public(),
        }
        if report is not None:
            payload["report"] = copy.deepcopy(report)
        self._terminal(session, status="completed", public=payload)

    def _terminal(
        self,
        session: _CoachSession,
        *,
        status: Literal["completed", "failed", "cancelled", "timed_out"],
        public: dict[str, Any],
    ) -> None:
        operation = session.operation
        if operation is None or operation.completion.done():
            return
        public = dict(public)
        public["operation"] = {
            "operation_id": operation.operation_id,
            "session_id": session.session_id,
            "status": status,
        }
        operation.completion.set_result(
            TerminalCommandOutcome(
                operation_id=operation.operation_id,
                session_id=session.session_id,
                status=status,
                public=public,
            )
        )

    def _begin_operation(
        self, session: _CoachSession, phase: CoachPhase, message: str
    ) -> _Operation:
        self._ensure_no_receipt_barrier(session)
        operation = _Operation(
            operation_id=f"operation-{uuid.uuid4().hex}",
            session_id=session.session_id,
        )
        session.operation = operation
        session.cancel_reason = None
        session.cleanup_error = None
        session.pending_failure = None
        session.cleanup_done = False
        session.cleanup_submitted = False
        session.receipt_protocol_unsafe = False
        session.receipt_failed_safe = False
        session.retained_receipts.clear()
        session.receipt_poll_in_callback = False
        session.receipt_poll_deferred = False
        session.capture_events.clear()
        session.capture_truncated = False
        self._set_phase(session, phase)
        self._progress(session, phase.value, message, 0)
        return operation

    def _pending(self, session: _CoachSession, operation: _Operation) -> PendingCommandOutcome:
        accepted = {
            "ok": True,
            "code": "COACH_ACCEPTED",
            "message": "Coach operation accepted.",
            "operation": {
                "operation_id": operation.operation_id,
                "session_id": session.session_id,
                "status": "running",
                "poll_after_s": 1,
            },
            "coach": self._state_public(),
        }

        def cancel(reason: Literal["stop", "timeout"]) -> bool:
            if threading.get_ident() == self._owner_thread_id:
                return self._cancel_by_id(session.session_id, operation.operation_id, reason)
            self._post_to_owner(
                lambda: self._cancel_by_id(session.session_id, operation.operation_id, reason)
            )
            return True

        return PendingCommandOutcome(
            operation_id=operation.operation_id,
            session_id=session.session_id,
            accepted=accepted,
            completion=operation.completion,
            cancel_callback=cancel,
            subscribe_callback=operation.subscribe,
        )

    def _cancel_by_id(
        self,
        session_id: str,
        operation_id: str,
        reason: Literal["stop", "timeout"],
    ) -> bool:
        self._assert_owner_thread()
        session = self._session
        if (
            session is None
            or session.session_id != session_id
            or session.operation is None
            or session.operation.operation_id != operation_id
            or session.operation.completion.done()
        ):
            return False
        return self._request_cancel(session, reason) == "pending"

    def _progress(
        self,
        session: _CoachSession,
        phase: str,
        message: str,
        percent: int | None,
    ) -> None:
        operation = session.operation
        if operation is None or operation.completion.done():
            return
        operation.publish(
            CommandProgress(
                operation_id=operation.operation_id,
                session_id=session.session_id,
                phase=phase,
                message=message,
                percent=percent,
            )
        )

    def _arm_timing(
        self,
        session: _CoachSession,
        deadline_ns: int,
        callback: Callable[[], None],
        *,
        allow_cancelling: bool = False,
    ) -> None:
        self._cancel_timing(session)
        session.timing_generation += 1
        generation = session.timing_generation

        fired = False

        def due() -> None:
            nonlocal fired
            fired = True
            self._post_to_owner(
                lambda: self._run_timing_callback(
                    session.session_id,
                    generation,
                    callback,
                    allow_cancelling,
                )
            )

        try:
            handle = self._scheduler.call_at_ns(deadline_ns, due)
        except Exception:
            self._handle_timing_failure(session)
            return
        if not fired and session.timing_generation == generation:
            session.timing_handle = handle
        elif fired:
            # A synchronous scheduler may already have transitioned or re-armed.
            # Never overwrite that newer handle with this consumed one.
            try:
                handle.cancel()
            except Exception:
                pass

    def _handle_timing_failure(self, session: _CoachSession) -> None:
        error = self._runtime_error(
            "COACH_RUNTIME_UNAVAILABLE",
            "The coach timing scheduler is unavailable.",
        )
        if session.phase is CoachPhase.CANCELLING:
            session.pending_failure = session.pending_failure or error
            return
        if self._worker_session_id == session.session_id:
            session.pending_failure = error
            session.cancel_reason = "stop"
            self._set_phase(session, CoachPhase.CANCELLING)
            if self._worker_control is not None:
                self._worker_control.request_cancel("stop")
            self._submit_cleanup_once(session)
            if self._worker_future is not None:
                self._worker_future.cancel()
            self._try_settle_cancellation(session)
            return
        self._fail(session, error)

    def _run_timing_callback(
        self,
        session_id: str,
        generation: int,
        callback: Callable[[], None],
        allow_cancelling: bool,
    ) -> None:
        self._assert_owner_thread()
        session = self._session
        if (
            session is None
            or session.session_id != session_id
            or session.timing_generation != generation
            or (session.cancel_reason is not None and not allow_cancelling)
        ):
            return
        session.timing_handle = None
        callback()

    @staticmethod
    def _cancel_timing(session: _CoachSession) -> None:
        handle = session.timing_handle
        session.timing_handle = None
        session.timing_generation += 1
        if handle is not None:
            try:
                handle.cancel()
            except Exception:
                pass

    def _updated_timeline(
        self,
        session: _CoachSession,
        resolution: LessonResolution,
        *,
        segment: str | None,
        tempo_percent: int | None,
    ) -> tuple[LessonTimeline, LessonSelection]:
        current = session.selection or LessonSelection(
            segment=resolution.timeline.segment_label,
            tempo_percent=resolution.timeline.tempo_percent,
            difficulty=resolution.timeline.difficulty,
            hand=resolution.timeline.hand,
        )
        selection = LessonSelection(
            segment=current.segment if segment is None else segment,
            tempo_percent=current.tempo_percent if tempo_percent is None else tempo_percent,
            difficulty=current.difficulty,
            hand=current.hand,
        )
        return resolve_timeline(resolution.lesson, selection), selection

    def _count_in_duration_us(
        self,
        resolution: LessonResolution,
        timeline: LessonTimeline,
        beats: int,
    ) -> int:
        lesson = resolution.lesson
        tempo = lesson.tempo_map[0]
        for candidate in lesson.tempo_map:
            if candidate.absolute_tick > timeline.start_tick:
                break
            tempo = candidate
        signature = lesson.time_signatures[0]
        for candidate in lesson.time_signatures:
            if candidate.absolute_tick > timeline.start_tick:
                break
            signature = candidate
        numerator = tempo.microseconds_per_quarter * 4 * 100 * beats
        denominator = signature.denominator * timeline.tempo_percent
        return max(1, numerator // denominator)

    def _state_public(self) -> dict[str, Any]:
        session = self._session
        if session is None:
            return {
                "phase": CoachPhase.IDLE.value,
                "session_id": None,
                "operation_id": None,
                "lesson_id": None,
                "attempt_id": None,
                "reports_retained": len(self._reports),
            }
        resolution = session.resolution
        timeline = session.timeline
        return {
            "phase": session.phase.value,
            "session_id": session.session_id,
            "operation_id": self.active_operation_id,
            "lesson_id": resolution.lesson.lesson_id if resolution is not None else None,
            "cache_hit": resolution.cache_hit if resolution is not None else False,
            "fidelity": (
                resolution.lesson.provenance.fidelity.value if resolution is not None else None
            ),
            "segment": timeline.segment_label if timeline is not None else None,
            "tempo_percent": timeline.tempo_percent if timeline is not None else None,
            "loops": session.loops,
            "count_in": session.count_in,
            "playback_id": session.playback_id,
            "attempt_id": session.attempt_id,
            "reports_retained": len(self._reports),
            "last_report": copy.deepcopy(self._reports[-1]) if self._reports else None,
        }

    def _error_public(self, session: _CoachSession, error: CoachError) -> dict[str, Any]:
        return {
            "ok": False,
            "error": error.to_public_dict(),
            "coach": self._state_public(),
        }

    def _find_report(self, attempt_id: str | None) -> dict[str, Any] | None:
        if attempt_id is None:
            return copy.deepcopy(self._reports[-1]) if self._reports else None
        for report in reversed(self._reports):
            if report.get("attempt_id") == attempt_id:
                return copy.deepcopy(report)
        return None

    def _require_lesson(self) -> tuple[_CoachSession, LessonResolution]:
        self._ensure_mounted()
        session = self._session
        if session is None or session.resolution is None:
            raise CoachError(
                "NO_ACTIVE_LESSON",
                "No reusable lesson is active.",
                resolution="Run /coach teach QUERY first.",
                retryable=True,
            )
        return session, session.resolution

    @staticmethod
    def _validate_loops(loops: int) -> None:
        if isinstance(loops, bool) or not isinstance(loops, int) or not 1 <= loops <= 8:
            raise CoachError(
                "INVALID_ARGUMENT",
                "loops must be an integer from 1 through 8.",
                field="loops",
                resolution="Retry with --loops 1.",
                retryable=True,
            )

    @staticmethod
    def _validate_count_in(count_in: int) -> None:
        if isinstance(count_in, bool) or not isinstance(count_in, int) or not 0 <= count_in <= 8:
            raise CoachError(
                "INVALID_ARGUMENT",
                "count_in must be an integer from 0 through 8.",
                field="count_in",
                resolution="Retry with --count-in 4.",
                retryable=True,
            )

    def _prepare_replacement(self, *, replace: bool) -> None:
        session = self._session
        if session is None:
            return
        operation = session.operation
        if operation is None or operation.completion.done():
            self._ensure_no_receipt_barrier(session)
            return
        if not replace:
            raise self._playback_active("A coach operation is already running.")
        disposition = self._request_cancel(session, "stop")
        if not operation.completion.done():
            activity = "finishing" if disposition == "finishing" else "cancelling"
            raise self._playback_active(
                f"The previous coach operation is still {activity}; retry after it settles."
            )

    def _ensure_idle_operation(self, session: _CoachSession) -> None:
        if session.operation is not None and not session.operation.completion.done():
            raise CoachEngine._playback_active("A coach operation is already running.")
        self._ensure_no_receipt_barrier(session)

    def _ensure_no_receipt_barrier(self, session: _CoachSession) -> None:
        state = self._poll_receipts(session)
        if state in {"pending", "rejected_unsafe"}:
            raise self._playback_active(
                "Audio cleanup is still settling safely; retry after /coach state is terminal."
            )

    @staticmethod
    def _playback_active(message: str) -> CoachError:
        return CoachError(
            "PLAYBACK_ACTIVE",
            message,
            resolution="Use /coach state or /coach stop, then retry.",
            retryable=True,
        )

    @staticmethod
    def _runtime_error(code: str, message: str) -> CoachError:
        bounded = (message or "Coach runtime error.").strip()[:300]
        return CoachError(
            code,
            bounded,
            resolution="Use /coach state, stop if needed, then retry.",
            retryable=True,
        )

    def _set_phase(self, session: _CoachSession, phase: CoachPhase) -> None:
        if session.phase is phase:
            return
        session.phase = phase
        session.transitions.append(phase)

    def _clear_worker(self) -> None:
        self._worker_future = None
        self._worker_control = None
        self._worker_session_id = None
        self._worker_operation_id = None

    def _ensure_mounted(self) -> None:
        if self._unmounted:
            raise RuntimeError("coach engine is unmounted")

    def _post_to_owner(self, callback: Callable[[], None]) -> None:
        try:
            self._dispatcher(callback)
        except Exception:
            # Never fall back to mutating bus/session state from a worker or timer
            # thread. Retain the callback for a bounded owner-thread drain instead.
            with self._deferred_lock:
                self._deferred_owner_callbacks.append(callback)

    def _drain_deferred_callbacks(self) -> int:
        drained = 0
        while True:
            with self._deferred_lock:
                if not self._deferred_owner_callbacks:
                    return drained
                callback = self._deferred_owner_callbacks.popleft()
            callback()
            drained += 1

    def _enter_owner(self) -> None:
        self._assert_owner_thread()
        self._drain_deferred_callbacks()
        session = self._session
        if (
            session is not None
            and session.receipt_poll_deferred
            and not session.receipt_poll_in_callback
        ):
            session.receipt_poll_deferred = False
            self._run_receipt_poll(session)

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("CoachEngine state must run on its owning app thread")
