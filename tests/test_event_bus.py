from dataclasses import replace

import pytest

from halcyon.event_bus import (
    DispatchReceipt,
    HalcyonMidiAdapter,
    MidiEventAdapter,
    MidiEventBus,
    ReceiptPoll,
)
from halcyon.midi import MidiEvent, MidiOrigin, NormalizedMidiEvent, audio_command_for_event


class StubReceipt:
    def __init__(self, snapshot: ReceiptPoll) -> None:
        self.snapshot = snapshot

    def poll(self) -> ReceiptPoll:
        return self.snapshot


class InvalidSnapshotReceipt:
    def poll(self) -> object:
        return object()


class RaisingReceipt:
    def poll(self) -> ReceiptPoll:
        raise RuntimeError("poll failed")


class RaisingPollAttribute:
    @property
    def poll(self) -> object:
        raise RuntimeError("attribute failed")


def adapters() -> tuple[MidiEventAdapter, HalcyonMidiAdapter]:
    return (
        MidiEventAdapter(
            origin=MidiOrigin.HUMAN,
            source_port="Akai LPK25 Wireless",
            session_id="app",
        ),
        HalcyonMidiAdapter(
            source_port="halcyon://coach/lesson-1",
            session_id="coach-1",
        ),
    )


def test_hardware_and_halcyon_adapters_emit_same_canonical_type() -> None:
    human, halcyon = adapters()

    human_event = human.note_on(60, 100)
    halcyon_event = halcyon.note_on(60, 90)

    assert isinstance(human_event, MidiEvent)
    assert isinstance(human_event, NormalizedMidiEvent)
    assert type(human_event) is type(halcyon_event)
    assert human_event.event_type == "press"  # backward-compatible UI spelling
    assert human_event.midi_event_type == "note_on"  # canonical runtime spelling
    assert halcyon_event.origin is MidiOrigin.HALCYON
    assert human_event.voice_id != halcyon_event.voice_id


def test_plan_canonical_note_on_constructs_as_press_audio_and_active_bus_voice() -> None:
    event = NormalizedMidiEvent(
        event_type="note_on",
        note=60,
        note_name="C4",
        velocity=100,
        channel=1,
        status=0x90,
        raw_message=(0x90, 60, 100),
        delta_time=0.0,
        source="halcyon://coach/test",
        elapsed=0.0,
        origin=MidiOrigin.HALCYON,
    )
    bus = MidiEventBus()

    dispatched = bus.publish(event).event

    assert dispatched.is_press and dispatched.midi_event_type == "note_on"
    assert audio_command_for_event(dispatched) == {
        "type": "note_on",
        "note": 60,
        "velocity": 100,
        "channel": 1,
        "voice_id": event.voice_id,
    }
    assert [voice.voice_id for voice in bus.active_voices()] == [event.voice_id]


def test_ordered_consumers_and_duplicate_event_ids_dispatch_once() -> None:
    human, _halcyon = adapters()
    clock_values = iter((1_000, 2_000))
    bus = MidiEventBus(clock=lambda: next(clock_values))
    calls: list[tuple[str, int, str]] = []
    bus.subscribe(lambda event: calls.append(("audio", event.sequence, event.event_id)))
    bus.subscribe(lambda event: calls.append(("ui", event.sequence, event.event_id)))
    event = human.note_on(64, 100)

    first = bus.publish(event)
    duplicate = bus.publish(event)
    second = bus.publish(human.note_on(67, 100))

    assert first.accepted and first.event.sequence == 1
    assert first.event.dispatched_monotonic_ns == 1_000
    assert duplicate.deduplicated and duplicate.consumer_count == 0
    assert second.event.sequence == 2
    assert calls == [
        ("audio", 1, event.event_id),
        ("ui", 1, event.event_id),
        ("audio", 2, second.event.event_id),
        ("ui", 2, second.event.event_id),
    ]


def test_receipt_poll_has_exact_states_and_bounded_diagnostics() -> None:
    states = ("pending", "accepted", "rejected_unsafe", "failed_safe")

    assert tuple(ReceiptPoll(state=state).state for state in states) == states
    snapshot = ReceiptPoll(
        state="rejected_unsafe",
        code=f"  {'C' * 100}  ",
        message=f"  {'M' * 600}  ",
    )

    assert snapshot.code == "C" * 80
    assert snapshot.message == "M" * 500
    with pytest.raises(ValueError, match="unsupported receipt state"):
        ReceiptPoll(state="unknown")
    with pytest.raises(TypeError, match="receipt code must be a string or None"):
        ReceiptPoll(state="pending", code=42)


def test_required_receipts_are_runtime_checkable_retained_and_counted() -> None:
    human, _halcyon = adapters()
    bus = MidiEventBus()
    optional_receipt = StubReceipt(ReceiptPoll("accepted"))
    pending_receipt = StubReceipt(ReceiptPoll("pending"))
    accepted_receipt = StubReceipt(ReceiptPoll("accepted"))
    bus.subscribe(lambda _event: optional_receipt)
    bus.subscribe(lambda _event: pending_receipt, receipt_required=True)
    bus.subscribe(lambda _event: None, receipt_required=True)
    bus.subscribe(lambda _event: accepted_receipt, receipt_required=True)

    result = bus.publish(human.note_on(60, 100))

    assert isinstance(pending_receipt, DispatchReceipt)
    assert result.accepted and not result.deduplicated
    assert result.consumer_count == 4
    assert result.required_receipt_count == 3
    assert result.receipts == (pending_receipt, accepted_receipt)
    assert optional_receipt not in result.receipts
    assert len(result.receipts) < result.required_receipt_count
    assert result.consumer_errors == ("consumer 3: required DispatchReceipt was not returned",)

    pending_receipt.snapshot = ReceiptPoll("accepted")
    assert result.receipts[0].poll().state == "accepted"


@pytest.mark.parametrize(
    ("returned", "error_fragment"),
    (
        (object(), "protocol violation"),
        (InvalidSnapshotReceipt(), "returned an invalid snapshot"),
        (RaisingReceipt(), "poll() protocol violation"),
        (RaisingPollAttribute(), "protocol violation"),
    ),
)
def test_invalid_required_receipt_is_a_visible_protocol_failure(
    returned: object,
    error_fragment: str,
) -> None:
    human, _halcyon = adapters()
    bus = MidiEventBus()
    bus.subscribe(lambda _event: returned, receipt_required=True)

    result = bus.publish(human.note_on(61, 100))

    assert result.accepted
    assert result.required_receipt_count == 1
    assert result.receipts == ()
    assert len(result.consumer_errors) == 1
    assert error_fragment in result.consumer_errors[0]


def test_consumer_exceptions_are_bounded_and_required_failure_is_not_success() -> None:
    human, _halcyon = adapters()
    bus = MidiEventBus()
    huge_error_type = type("Huge" + ("X" * 1_000), (RuntimeError,), {})

    def optional_failure(_event: NormalizedMidiEvent) -> None:
        raise ValueError("Y" * 2_000)

    def required_failure(_event: NormalizedMidiEvent) -> None:
        raise huge_error_type("Z" * 2_000)

    bus.subscribe(optional_failure)
    bus.subscribe(required_failure, receipt_required=True)

    result = bus.publish(human.note_on(62, 100))

    assert result.required_receipt_count == 1
    assert result.receipts == ()
    assert len(result.consumer_errors) == 2
    assert all(len(error) <= 500 for error in result.consumer_errors)
    assert "required DispatchReceipt consumer raised" in result.consumer_errors[1]


def test_duplicate_dispatch_never_invokes_or_creates_a_second_receipt() -> None:
    human, _halcyon = adapters()
    bus = MidiEventBus()
    created: list[StubReceipt] = []

    def required_consumer(_event: NormalizedMidiEvent) -> StubReceipt:
        receipt = StubReceipt(ReceiptPoll("accepted"))
        created.append(receipt)
        return receipt

    bus.subscribe(required_consumer, receipt_required=True)
    event = human.note_on(63, 100)

    first = bus.publish(event)
    duplicate = bus.publish(event)

    assert first.required_receipt_count == 1
    assert first.receipts == tuple(created)
    assert len(created) == 1
    assert duplicate.deduplicated and not duplicate.accepted
    assert duplicate.consumer_count == 0
    assert duplicate.required_receipt_count == 0
    assert duplicate.receipts == ()
    assert duplicate.consumer_errors == ()


def test_subscribe_requires_a_real_boolean_receipt_flag() -> None:
    bus = MidiEventBus()

    with pytest.raises(TypeError, match="receipt_required must be a bool"):
        bus.subscribe(lambda _event: None, receipt_required=1)


def test_same_note_human_and_halcyon_release_independently_with_refcounts() -> None:
    human, halcyon = adapters()
    bus = MidiEventBus()
    human_on = human.note_on(60, 100)
    halcyon_on = halcyon.note_on(60, 100)
    bus.publish(human_on)
    bus.publish(halcyon_on)

    assert len(bus.active_voices()) == 2
    assert (
        bus.active_refcount(
            origin=MidiOrigin.HUMAN,
            source_port=human_on.source_port,
            session_id="app",
            channel=1,
            note=60,
        )
        == 1
    )

    human_off = bus.publish(human.note_off(60)).event

    assert human_off.voice_id == human_on.voice_id
    assert [voice.voice_id for voice in bus.active_voices()] == [halcyon_on.voice_id]


def test_cross_owner_release_is_sanitized_and_cannot_release_human_voice() -> None:
    human, halcyon = adapters()
    bus = MidiEventBus()
    human_on = human.note_on(62, 100)
    bus.publish(human_on)
    malformed = replace(halcyon.note_off(62), voice_id=human_on.voice_id, matched_voice=True)

    dispatched = bus.publish(malformed).event

    assert dispatched.voice_id != human_on.voice_id
    assert not dispatched.matched_voice
    assert [voice.voice_id for voice in bus.active_voices()] == [human_on.voice_id]


def test_halcyon_cleanup_emits_concrete_releases_and_preserves_human() -> None:
    human, halcyon = adapters()
    bus = MidiEventBus()
    consumed: list[MidiEvent] = []
    bus.subscribe(consumed.append)
    human_on = human.note_on(65, 100)
    halcyon_on = halcyon.note_on(65, 100)
    bus.publish(human_on)
    bus.publish(halcyon_on)

    cleanup = bus.release_halcyon_voices(session_id="coach-1")

    assert len(cleanup) == 1
    release = cleanup[0].event
    assert release.midi_event_type == "note_off"
    assert release.voice_id == halcyon_on.voice_id
    assert release.origin is MidiOrigin.HALCYON
    assert release.raw_message == (0x80, 65, 0)
    assert [voice.voice_id for voice in bus.active_voices()] == [human_on.voice_id]
    assert consumed[-1] == release

    bus.reset_after_panic()
    assert bus.active_voices() == ()


def test_repeated_same_owner_note_instances_have_independent_refcount() -> None:
    human, _halcyon = adapters()
    bus = MidiEventBus()
    first = human.note_on(69, 100)
    second = human.note_on(69, 90)
    bus.publish(first)
    bus.publish(second)

    assert (
        bus.active_refcount(
            origin=MidiOrigin.HUMAN,
            source_port=first.source_port,
            session_id="app",
            channel=1,
            note=69,
        )
        == 2
    )

    bus.publish(human.note_off(69))
    assert [voice.voice_id for voice in bus.active_voices()] == [second.voice_id]
