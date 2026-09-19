import pytest

from halcyon.logs import MAX_QUERY_EVENTS, MidiEventLog, event_summary
from halcyon.midi import MidiEvent, MidiOrigin


def make_event(
    note: int,
    elapsed: float = 0.0,
    *,
    sequence: int = 0,
    origin: MidiOrigin = MidiOrigin.HUMAN,
    session_id: str = "app",
) -> MidiEvent:
    return MidiEvent(
        event_type="press",
        note=note,
        note_name=f"N{note}",
        velocity=64,
        channel=1,
        status=0x90,
        raw_message=(0x90, note, 64),
        delta_time=0.01,
        source="test-port",
        elapsed=elapsed,
        sequence=sequence,
        origin=origin,
        session_id=session_id,
    )


def test_event_log_keeps_recent_events_only() -> None:
    event_log = MidiEventLog(max_events=2)

    event_log.append(make_event(60, 1.0))
    event_log.append(make_event(61, 2.0))
    event_log.append(make_event(62, 3.0))

    assert len(event_log) == 2
    assert [event.note for event in event_log.recent()] == [61, 62]


def test_event_log_summaries_are_bounded() -> None:
    event_log = MidiEventLog(max_events=5)
    for note in range(60, 64):
        event_log.append(make_event(note, float(note)))

    summaries = event_log.summaries(limit=2)

    assert len(summaries) == 2
    assert "N62 (62)" in summaries[0]
    assert "N63 (63)" in summaries[1]
    assert "source=test-port" in summaries[1]


def test_event_summary_includes_midi_fields() -> None:
    summary = event_summary(make_event(65, 1.25))

    assert "1.250s press N65 (65)" in summary
    assert "vel=64" in summary
    assert "status=0x90" in summary


def test_internal_query_filters_post_bus_events_in_ascending_order() -> None:
    event_log = MidiEventLog()
    event_log.append(make_event(60, sequence=1, session_id="attempt-1"))
    event_log.append(
        make_event(
            61,
            sequence=2,
            origin=MidiOrigin.HALCYON,
            session_id="lesson-1",
        )
    )
    event_log.append(make_event(62, sequence=3, session_id="attempt-1"))
    event_log.append(make_event(63, sequence=4, session_id="attempt-2"))

    page = event_log.query(
        origin=MidiOrigin.HUMAN,
        session_id="attempt-1",
        after_sequence=1,
        limit=2,
    )

    assert [event.sequence for event in page.events] == [3]
    assert page.next_sequence == 3
    assert not page.truncated
    assert not page.cursor_expired


def test_internal_query_is_hard_bounded_and_reports_truncation() -> None:
    event_log = MidiEventLog()
    for sequence in range(1, 131):
        event_log.append(make_event(60 + sequence % 12, sequence=sequence))

    page = event_log.query(limit=MAX_QUERY_EVENTS)

    assert len(page.events) == MAX_QUERY_EVENTS
    assert page.next_sequence == MAX_QUERY_EVENTS
    assert page.truncated
    with pytest.raises(ValueError, match="between 1 and 128"):
        event_log.query(limit=129)


def test_internal_query_reports_expired_cursor_after_retention_rollover() -> None:
    event_log = MidiEventLog(max_events=3)
    for sequence in range(10, 14):
        event_log.append(make_event(60, sequence=sequence))

    page = event_log.query(after_sequence=4)

    assert page.cursor_expired
    assert [event.sequence for event in page.events] == [11, 12, 13]


def test_internal_query_distinguishes_snapshot_from_explicit_zero_cursor() -> None:
    event_log = MidiEventLog(max_events=3)
    for sequence in range(1, 5):
        event_log.append(make_event(60, sequence=sequence))

    assert not event_log.query().cursor_expired
    assert event_log.query(after_sequence=0).cursor_expired
    assert not event_log.query(after_sequence=1).cursor_expired


def test_event_log_never_exceeds_the_ui_retention_limit() -> None:
    with pytest.raises(ValueError, match="between 1 and 200"):
        MidiEventLog(max_events=201)
    with pytest.raises(ValueError, match="between 1 and 200"):
        MidiEventLog(max_events=True)
