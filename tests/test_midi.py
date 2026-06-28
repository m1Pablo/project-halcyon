from halcyon.midi import choose_preferred_port, normalize_message, note_name


def test_note_name_uses_midi_octaves() -> None:
    assert note_name(60) == "C4"
    assert note_name(61) == "C#4"


def test_normalize_note_on_press() -> None:
    event = normalize_message([0x90, 60, 100], 0.25, source="LPK25", elapsed=1.5)

    assert event is not None
    assert event.event_type == "press"
    assert event.note == 60
    assert event.note_name == "C4"
    assert event.velocity == 100
    assert event.channel == 1
    assert event.status == 0x90
    assert event.delta_time == 0.25
    assert event.source == "LPK25"
    assert event.elapsed == 1.5


def test_note_on_with_zero_velocity_is_release() -> None:
    event = normalize_message([0x91, 64, 0], 0.1, source="LPK25", elapsed=2.0)

    assert event is not None
    assert event.event_type == "release"
    assert event.note_name == "E4"
    assert event.channel == 2


def test_choose_preferred_port_favors_lpk25_wireless() -> None:
    ports = ["IAC Driver Bus 1", "Akai LPK25 Wireless:Akai LPK25 Wireless MIDI 1"]

    assert choose_preferred_port(ports) == 1
