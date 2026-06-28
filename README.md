# Project Halcyon

Python MIDI controller, synth, and drum experiment.

The current iteration is a Textual TUI that listens to real MIDI input, logs note press/release events, shows a full-spectrum virtual keyboard, and streams MIDI note events to a Rust audio sidecar.

The first target controller is an Akai Professional LPK25 Wireless connected over Bluetooth MIDI. Python owns MIDI and UI for now; Rust owns realtime audio output.

The newest surface adds a hardware-style synth rack, a compact OpenAI chat input, and a local MCP-compatible tool bridge. The assistant and bridge can only inspect or change synth controls; they do not get shell, file, MIDI-device, or network tools.

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
- Show a horizontal synth rack with `Volume`, `Tone`, `Shape`, `Attack`, `Decay`, `Sustain`, `Release`, and `Drive` controls.
- Adjust synth controls with visible `-` / `+` buttons or focused arrow keys.
- Apply `Init`, `Pluck`, `Soft Pad`, `Bass`, and `Lead` presets.
- Save local user synth presets from the rack or through the assistant/MCP tool bridge.
- Show an assistant chat panel. It is disabled unless `OPENAI_API_KEY` is present in the process environment.
- Start a localhost MCP-compatible control server bound to `127.0.0.1`.
- Start the Rust sidecar with `uv run -- cargo run --manifest-path halcyon-audio/Cargo.toml --bin halcyon-audio-sidecar`.
- Send `note_on`, `note_off`, `all_notes_off`, `set_param`, and `shutdown` commands as newline-delimited JSON.
- Open a local Unix socket visualization channel for raw oscillator sample windows.
- Produce a small virtual-analog synth voice: saw/square blend oscillator, ADSR envelope, low-pass filter, velocity gain, soft clipping, and 8-voice polyphony.

Inside the app:

- `r` reconnects MIDI.
- `c` clears the event log.
- Type in `Ask Halcyon` and press Enter or `Send` to submit an assistant request.
- `Ctrl+Q` quits.

User presets are saved outside the repository at
`$HALCYON_PRESETS_PATH` when set, otherwise
`$XDG_CONFIG_HOME/project-halcyon/synth-presets.json` or
`~/.config/project-halcyon/synth-presets.json`.

## OpenAI Chat

The app does not read `.env` files itself. Load secrets into the shell environment before running the app:

```bash
export OPENAI_API_KEY=...
uv run -m halcyon
```

Optional model override:

```bash
export HALCYON_CHAT_MODEL=gpt-5.5
```

Automated tests mock the OpenAI client and never make live API calls.

## Local MCP Bridge

When the app is running, it starts a local HTTP control server on `127.0.0.1`, beginning at port `8769` and moving up if the port is busy. The chat log prints the selected URL.

Run the stdio bridge for local MCP clients with:

```bash
uv run -m halcyon.mcp_bridge --api-url http://127.0.0.1:8769
```

Available tools:

- `get_synth_state`
- `list_synth_params`
- `set_synth_param`
- `apply_synth_patch`
- `apply_synth_preset`
- `save_synth_preset`
- `all_notes_off`

## Audio Visualization Channel

The synth control protocol remains newline-delimited JSON over the Rust sidecar stdin/stdout.
Waveform visualization uses a separate local Unix domain socket so sample traffic never touches
the control stream.

Config:

```bash
export HALCYON_VIZ_SOCKET=/tmp/synth_viz.sock
export HALCYON_VIZ_SAMPLES=1024
export HALCYON_VIZ_FPS=60
```

Python listens on `HALCYON_VIZ_SOCKET`; Rust connects and sends best-effort raw frames. Each frame
starts with `HZV1` plus a little-endian `u32` sample count, followed by that many little-endian
`f32` samples. The Textual waveform renderer is intentionally deferred; the Python reader exposes
the latest sample window for a future widget.

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
