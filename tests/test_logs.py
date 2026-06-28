from halcyon.logs import MidiEventLog, event_summary
from halcyon.midi import MidiEvent


def make_event(note: int, elapsed: float = 0.0) -> MidiEvent:
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
