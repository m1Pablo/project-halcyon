# Project Halcyon

Python MIDI controller, synth, and drum experiment.

The current iteration is a Textual TUI that listens to real MIDI input, logs note press/release events, shows a full-spectrum virtual keyboard, and streams MIDI note events to a Rust audio sidecar.

The first target controller is an Akai Professional LPK25 Wireless connected over Bluetooth MIDI. Python owns MIDI and UI for now; Rust owns realtime audio output.

## Run

Use `uv` for all development commands:

```bash
uv run -m halcyon
```

The app will:

- List available MIDI input ports.
- Prefer a port containing `lpk25`, `akai`, or `wireless`.
- Log note, velocity, channel, status byte, delta time, elapsed time, and source port.
- Show a full MIDI `0..127` virtual keyboard with a sticky horizontal viewport and highlight pressed keys.
- Start the Rust sidecar with `uv run -- cargo run --manifest-path halcyon-audio/Cargo.toml --bin halcyon-audio-sidecar`.
- Send `note_on`, `note_off`, `all_notes_off`, `set_param`, and `shutdown` commands as newline-delimited JSON.
- Produce a small virtual-analog synth voice: saw/square blend oscillator, ADSR envelope, low-pass filter, velocity gain, soft clipping, and 8-voice polyphony.

Inside the app:

- `r` reconnects MIDI.
- `c` clears the event log.
- `Ctrl+Q` quits.

## Validate

```bash
uv run ruff format
uv run ruff check
uv run pytest
uv run -- cargo fmt --all --check --manifest-path halcyon-audio/Cargo.toml
uv run -- cargo clippy --manifest-path halcyon-audio/Cargo.toml --all-targets -- -D warnings
uv run -- cargo test --manifest-path halcyon-audio/Cargo.toml
```

## Legacy Code

The original hand-coded Rich scripts remain in place:

- `halcyon.py`
- `Modules/menu.py`
- `Modules/midi.py`
- `Modules/synth.py`
