# Project Halcyon

Python MIDI controller, synth, and drum experiment.

The current iteration is a Textual TUI that listens to real MIDI input, logs note press/release events, shows a full-spectrum virtual keyboard, and streams MIDI note events to a Rust audio sidecar.

The first target controller is an Akai Professional LPK25 Wireless connected over Bluetooth MIDI. Python owns MIDI and UI for now; Rust owns realtime audio output.

The newest surface is a responsive performance cockpit: the live oscillator and piano own the center stage, MIDI activity remains visible beside them, and the command console stays docked below. Sound design and preset management live in a persistent Textual settings modal rather than competing with the instrument for permanent screen space.

The public control ABI is `halcyon/menu.xml`: chat, natural-language routing, and MCP all resolve to registered slash commands. Those slash leaves call the internal safe synth tools, but the LLM and external MCP clients do not get raw shell, file, MIDI-device, or network tools.

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
- Render live audio samples as geometric Unicode Braille animation in `Wave`, `Orbit`, or `Radial` mode.
- Open a scrollable Sound Settings modal containing `Volume`, `Tone`, `Shape`, `Attack`, `Decay`, `Sustain`, `Release`, and `Drive` controls.
- Adjust synth controls with visible `-` / `+` buttons or focused arrow keys.
- Clearly distinguish the loaded preset from the highlighted candidate; explicitly load `Init`, `Pluck`, `Soft Pad`, `Bass`, `Lead`, or a user preset.
- Save a named user preset through the modal or slash-command chat/MCP. Built-in presets cannot be overwritten.
- Show a command chat panel. Direct `/` slash commands work without OpenAI; natural-language routing uses the LLM when `OPENAI_API_KEY` is present.
- Start a localhost MCP-compatible control server bound to `127.0.0.1`.
- Start the Rust sidecar with `uv run -- cargo run --manifest-path halcyon-audio/Cargo.toml --bin halcyon-audio-sidecar`.
- Send `note_on`, `note_off`, `all_notes_off`, `set_param`, and `shutdown` commands as newline-delimited JSON.
- Open a local Unix socket visualization channel for raw oscillator sample windows.
- Produce a small virtual-analog synth voice: saw/square blend oscillator, ADSR envelope, low-pass filter, velocity gain, soft clipping, and 8-voice polyphony.

Inside the app:

- `r` reconnects MIDI.
- `c` clears the event log.
- Click `Wave`, `Orbit`, or `Radial` to change the oscillator projection. Clicking the animated scope itself never changes modes.
- Press `,`, `Ctrl+S`, or `F2` to open Sound Settings. Press Escape, use Close, or click the dimmed backdrop to dismiss it.
- On compact terminals, use `F1` for Stage, `F2` for Settings, `F3` for MIDI, and `F4` for Console.
- Type `/` commands in `Ask Halcyon`; press Tab to accept slash completion and Enter or `Send` to submit.
- Natural-language messages are routed to validated slash commands from `menu.xml`, or rejected.
- `Ctrl+Q` quits.

User presets are saved outside the repository at
`$HALCYON_PRESETS_PATH` when set, otherwise
`$XDG_CONFIG_HOME/project-halcyon/synth-presets.json` or
`~/.config/project-halcyon/synth-presets.json`.

## Command Chat

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

The in-app LLM is a creative router, not a raw tool-using agent. It receives the slash commands from `halcyon/menu.xml`, current synth context, recent chat context, and concise synth parameter metadata, and may only return JSON like:

```json
{"action":"menu","stages":[["/synth set tone 0.8"]],"reason":"short reason"}
```

Subjective requests such as mood, genre, artist, or song references should become best-effort `/synth patch ...` commands, not failed preset lookups. Every returned command is validated against the registry and command executor before anything changes. Invalid model commands are dropped; all-invalid output rejects. Direct slash commands bypass OpenAI entirely.

For generated sound-design patches, `volume` is treated as output trim rather than tone color. The router prompt asks the model to preserve the current volume unless the user explicitly asks for a loudness change, and the Rust audio core applies polyphony-aware mix gain plus output headroom to reduce sudden headphone-level jumps.

Current command surface:

- `/synth state`
- `/synth params`
- `/synth set <param> <0..1>`
- `/synth patch <param=value ...>`
- `/preset apply <name>`
- `/preset save <name>`
- `/panic`

## Local MCP Bridge

When the app is running, it starts a local HTTP control server on `127.0.0.1`, beginning at port `8769` and moving up if the port is busy. The chat log prints the selected URL. The HTTP surface is command-control only:

- `GET /health`
- `GET /commands`
- `GET /state`
- `POST /run_slash`
- `GET /run_status/{run_id}`
- `GET /run_logs/{run_id}`

Run the stdio bridge for local MCP clients with:

```bash
uv run -m halcyon.mcp_bridge --api-url http://127.0.0.1:8769
```

Available tools:

- `list_commands`
- `run_slash_command`
- `get_run_status`
- `get_run_logs`
- `get_app_state`

`run_slash_command` returns a `run_id` and polling guidance. MCP clients should poll the same run with `get_run_status` or `get_run_logs` while `do_not_rerun=true`, rather than submitting duplicate commands. The internal synth tools remain available only behind validated slash-command leaves.

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
`f32` samples. `HALCYON_VIZ_SAMPLES` is bounded by the Python reader, and Rust bounds
`HALCYON_VIZ_FPS` to `1..120`.

The live `OscillatorScope` reads immutable snapshots from that channel at a bounded UI refresh
rate. It renders three sample-driven projections:

- **Wave**: a time-domain polyline.
- **Orbit**: the signal plotted against a lagged copy, producing loops and ellipses.
- **Radial**: amplitude wrapped around a polar path.

The renderer is a clean-room Halcyon implementation of the general Textual line-rendering and
Unicode Braille technique seen in Toad's Mandelbrot widget: every terminal cell packs a 2×4 logical
pixel raster into one Braille code point, and `render_line` returns styled Rich strips. Aplazo's
Textual app and Toad informed the interaction and layout direction; no Toad implementation or
constants were copied.

Visualization is deliberately non-critical. The Python listener survives peer disconnects,
returns to its accept loop, and marks the snapshot disconnected; the scope then shows
`VIZ DISCONNECTED` instead of crashing the MIDI/UI control plane. A connected stream without a
sample window shows `VIZ IDLE`.

## Responsive Interface

Halcyon chooses wide, medium, or compact behavior from the actual Textual screen dimensions.
Wide and medium layouts keep the performance stage and MIDI activity together. Compact layouts
switch deterministically among Stage, Settings, MIDI, and Console rather than compressing every
surface into one unreadable column.

Sound Settings is one persistent native `ModalScreen`. Textual's screen stack owns focus and input
isolation; the modal backdrop owns outside-click dismissal while interactions inside the drawer
remain contained. The same persistent synth rack distinguishes the loaded patch from the selected
preset candidate and provides explicit Load, Reset, and Save New flows with validation feedback.

## Codex Workflow

Repository work is spec-driven and uses append-only, single-writer personal logs in
`.codex/logs/`. The parent Codex session is the Coordinator and records routing, verified evidence,
human gates, and wave completion in `.codex/logs/coordinator-{YYYY-MM-DD}.md`. The old
`.codex/ledger/LEDGER.md` is historical only and must never be appended.

`Atlas/` is a mechanically derived Campaign Atlas built from plans, personal logs, the active-plan
pointer, and the agent roster. It is useful for navigation but never grants permission to unlock a
wave or ship work. Preview or rebuild it with:

```bash
uv run .codex/skills/cartographer-map/scripts/build_vault.py --dry-run --root .
uv run .codex/skills/cartographer-map/scripts/build_vault.py --root .
```

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
