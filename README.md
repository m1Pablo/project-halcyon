# Project Halcyon

Python MIDI controller, synth, and drum experiment.

The current iteration is a Textual TUI that listens to real MIDI input and logs note press/release events. The first target controller is an Akai Professional LPK25 Wireless connected over Bluetooth MIDI.

Sound output is intentionally out of scope for this first Textual pass.

## Run

Use `uv` for all development commands:

```bash
uv run -m halcyon
```

The app will:

- List available MIDI input ports.
- Prefer a port containing `lpk25`, `akai`, or `wireless`.
- Log note, velocity, channel, status byte, delta time, elapsed time, and source port.
- Show a 25-key virtual keyboard and highlight pressed keys.

Inside the app:

- `r` reconnects MIDI.
- `c` clears the event log.
- `Ctrl+Q` quits.

## Validate

```bash
uv run ruff format
uv run ruff check
uv run pytest
```

## Legacy Code

The original hand-coded Rich scripts remain in place:

- `halcyon.py`
- `Modules/menu.py`
- `Modules/midi.py`
- `Modules/synth.py`
