# Project Halcyon

Python MIDI controller, synth, and drum experiment.

The current iteration is a Textual TUI that listens to real MIDI input, logs note press/release events, shows a full-spectrum virtual keyboard, and streams MIDI note events to a Rust audio sidecar.

The first target controller is an Akai Professional LPK25 Wireless connected over Bluetooth MIDI. Python owns MIDI and UI for now; Rust owns realtime audio output.

The newest surface is a responsive performance cockpit: the live oscillator and piano own the center stage, MIDI activity remains visible beside them, and the command console stays docked below. Sound design and preset management live in a persistent Textual settings modal rather than competing with the instrument for permanent screen space.

Halcyon also includes a local AI Learning MIDI Coach. Human controller input and coach demonstrations enter one Python-owned MIDI event bus, then the same required audio consumer sends normalized note commands to the Rust oscillator/DSP sidecar. The coach can demonstrate a saved timed-MIDI lesson, capture a human-only attempt, score it deterministically, and reuse the saved revision without authoring it again.

The public control ABI is `halcyon/menu.xml`: chat, natural-language routing, and MCP all resolve to registered slash commands. Those slash leaves call the internal safe synth tools, but the LLM and external MCP clients do not get raw shell, file, MIDI-device, or network tools.

## Run

Use `uv` for all development commands:

```bash
uv run -m halcyon
```

The app will:

- List available MIDI input ports.
- Prefer a port containing `lpk25`, `akai`, or `wireless`.
- Log origin, note, velocity, channel, status byte, delta time, elapsed time, and source port in a bounded 200-event activity view.
- Show a full MIDI `0..127` virtual keyboard with a sticky horizontal viewport and independent human, Halcyon, shared-note, and next-target states.
- Render live audio samples as geometric Unicode Braille animation in `Wave`, `Orbit`, or `Radial` mode.
- Open a scrollable Settings modal containing secure OpenAI credentials plus `Volume`, `Tone`, `Shape`, `Attack`, `Decay`, `Sustain`, `Release`, and `Drive` controls.
- Adjust synth controls with visible `-` / `+` buttons or focused arrow keys.
- Clearly distinguish the loaded preset from the highlighted candidate; explicitly load `Init`, `Pluck`, `Soft Pad`, `Bass`, `Lead`, or a user preset.
- Save a named user preset through the modal or slash-command chat/MCP. Built-in presets cannot be overwritten.
- Show a command chat panel. Direct `/` slash commands work without OpenAI; natural-language routing uses the LLM when a key is saved in Settings or supplied through `OPENAI_API_KEY`.
- Run reusable MIDI-coaching sessions through six registered `/coach` commands, with accepted/progress/final status kept truthful while longer work is pending.
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

Open Settings with `F2`, `,`, or `Ctrl+S`, paste an OpenAI API key under **OpenAI Credentials**, and choose **Save securely**. Halcyon stores it in the operating system credential vault through the native `keyring` backend, loads it on the next launch, and applies save/delete changes to new chat and lesson-authoring requests without restarting. The field is password-masked, never prefilled from storage, cleared when Settings closes, and excluded from clipboard copy/cut actions. **Delete saved key** requires confirmation and removes only the vault entry.

There is no plaintext file fallback. Halcyon does not read `.env` files or write the key into the repository, settings JSON, application logs, assistant context, MCP state, or the Rust audio-sidecar environment. If the operating system cannot provide an attested native credential backend, the in-app field and save action stay unavailable and the existing environment option still works.

Textual diagnostic event traces can contain raw keyboard or paste events before a password widget handles them. Halcyon therefore disables in-app credential entry whenever `TEXTUAL_LOG`, Textual debug tracing, or Textual devtools is enabled. Restart without those diagnostics to save or replace a key; loading an existing vault key, deleting it, and using `OPENAI_API_KEY` remain available.

To provide the key through the process environment instead:

```bash
export OPENAI_API_KEY=...
uv run -m halcyon
```

Optional model overrides:

```bash
export HALCYON_CHAT_MODEL=gpt-5.5
export HALCYON_COACH_MODEL=gpt-5.6-luna
```

Without an override, chat routing uses `gpt-5.5` and the latency-sensitive lesson author uses `gpt-5.6-luna`. `OPENAI_API_KEY` takes precedence when it is nonblank; Halcyon never copies an environment value into the credential vault. `HALCYON_CHAT_MODEL` and `HALCYON_COACH_MODEL` remain optional environment-only model selectors. Direct slash commands and saved lesson replay do not require natural-language routing, and a cache hit does not call the lesson-authoring provider.

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

## AI Learning MIDI Coach

Natural-language requests to teach, learn, demonstrate, play for imitation, practice, or score a melody route to exactly one umbrella `/coach teach` command. For example, “Teach me Aphex Twin's Avril 14th main melody” is treated as coaching, while “make the synth sound like Aphex Twin” remains sound-design work under `/synth patch`. The router may choose only registered commands from `halcyon/menu.xml`; it cannot inject raw MIDI or call a hidden coach tool.

The six coach commands are:

- `/coach teach QUERY [--segment LABEL --tempo 25..200 --difficulty foundation|standard|challenge --hand left|right|both --loops 1..8 --author never|bounded]` resolves a reusable lesson and runs demonstration, count-in, human capture, scoring, and feedback.
- `/coach replay [--segment LABEL --tempo 25..200 --loops 1..8]` demonstrates the active saved revision without authoring or overwriting it.
- `/coach attempt [--segment LABEL --tempo 25..200 --count-in 0..8]` captures and scores a fresh human-only attempt without replaying or authoring.
- `/coach feedback [attempt_id]` ends the current capture early or retrieves one bounded deterministic report.
- `/coach state` returns bounded phase, lesson settings, progress, cache/fidelity information, and the latest aggregate report.
- `/coach stop` requests cancellation and releases only the active session's Halcyon-owned voices; it is not a global panic.

### First use and offline replay

Lesson resolution is cache-first. On a cache hit, Halcyon loads the immutable saved revision and never calls the authoring provider. On a normal named-work cache miss with a segment such as `main melody`, Halcyon creates one short local educational MIDI approximation, validates it, saves it outside the repository, and reuses that exact saved revision thereafter. `--author never` is the explicit cache-only opt-out; if no saved lesson matches, the console asks for a segment or a generated first revision.

For example, “Help me learn Aphex Twin's Avril 14th main melody” routes to one cache-first coach command, or you can run `/coach teach "Aphex Twin Avril 14th" --segment "main melody" --tempo 70`. The provider returns a closed structured proposal, which Python validates, normalizes to timed MIDI, saves once, and reloads canonically before playback. A generated revision is labeled `approximate` so the coach does not present model output as an exact transcription. Refused, malformed, incompatible, cancelled, or over-limit proposals are not saved. Repeating the same request resolves the saved alias and works with the authoring provider unavailable, including after restarting the app.

The repository does not bundle that melody. The one-time generated lesson is stored as timed MIDI input in the external coach data directory and always plays through the same Rust oscillator path as other MIDI.

### Import user-supplied MIDI

A local Standard MIDI file can replace a rejected generated approximation without giving chat or MCP filesystem access. Select the musical track explicitly and provide both practical query spellings:

```bash
uv run -m halcyon.coach_import /path/to/lesson.mid \
  --track 0 \
  --title "Avril 14th" \
  --composer "Aphex Twin" \
  --segment "main melody" \
  --hand right \
  --alias "Aphex Twin Avril 14th" \
  --alias "Aphex Twin's Avril 14th" \
  --replace-approximate
```

Import is local-only and accepts regular format-0/1 `.mid` or `.midi` files up to 2 MiB. It validates the selected track against the same 2,048-event, 300-second, and eight-voice lesson bounds, rejects unbalanced notes, stores only normalized timed MIDI plus bounded source metadata, and labels the revision `user_supplied` / `verified`. Here, `verified` means exact to the selected bytes and track rather than generated from model recall. The source file is not needed for later playback.

`--replace-approximate` may supersede only an existing `locally_authored_bounded` / `approximate` revision. The old alias binding and revision remain immutable for recovery; a write-once compare-and-swap record atomically selects the imported replacement. Repeating the identical import is idempotent, while a different later file must use a distinct alias. The importer is not registered in `menu.xml` and is not reachable through chat, HTTP, or either MCP surface.

### Lesson storage and provenance

Coach data is outside the repository at `$HALCYON_COACH_PATH` when set, otherwise `$XDG_DATA_HOME/project-halcyon/coach` or `~/.local/share/project-halcyon/coach`:

```text
coach/
  catalog-v1.json
  aliases-v1/<normalized-alias-sha256>.json
  supersessions-v1/<normalized-alias-sha256>.json
  lessons/<lesson_id>/<content_id>.json
```

Each lesson revision is immutable and content-addressed within its lesson identity. Its saved musical payload is normalized `note_on`/`note_off` MIDI input plus integer PPQ ticks, tempo/time-signature maps, and segment metadata—not audio, model prose, notation, waveform samples, or a synth patch. Provenance records an acquisition label (`licensed_midi`, `user_supplied`, `public_domain`, `original`, or `locally_authored_bounded`), citation or attestation, authorization time, and provider/model identity when applicable. Fidelity is always explicit: `verified` or `approximate`.

### Privacy, visual identity, and safety

Raw human attempts are memory-only, bounded to 2,048 events, cleared after scoring or cancellation, and never persisted by v1. Halcyon retains at most five aggregate coaching reports per app session. The general UI activity log retains at most 200 events; its internal filtered query pages are capped at 128. Assistant context, run results, `/coach state`, and MCP app state expose bounded aggregates—not raw attempts, full lessons, source-port histories, private paths, authoring prompts, admission receipts, or credentials.

The keyboard and activity UI use text/shape as well as color:

- `● YOU` means a human-held note.
- `◆ HALCYON` means a coach-held note.
- `●◆ BOTH` means both independently hold the same pitch.
- `○ NEXT` means an upcoming target note.

Numeric voice identity crosses the Python/Rust boundary, so one origin releasing a shared pitch does not release the other's voice. `/coach stop`, timeout, replacement, and app teardown settle owner-scoped Halcyon cleanup before reporting a terminal cancellation. If authoring has already crossed its short persistence commit fence, stop reports that the operation is finishing instead of falsely claiming cancellation. `/panic` is the explicit global all-notes-off action. MIDI reconnect (`r`) also requests global audio safety and resets bus/keyboard state only after Rust accepts all-notes-off or the old sidecar is proven stopped; unsafe cleanup never becomes silent success.

Python remains the MIDI, lesson, session, command, and Textual UI control plane. Hardware events and synthetic Halcyon lesson events publish to one shared bus, which fans out to UI/log/capture consumers and exactly one required Rust-audio consumer. Rust—not React—owns the realtime oscillator and DSP, receiving normalized note events and numeric voice IDs over newline-delimited JSON.

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

Available tools (exactly five):

- `list_commands`
- `run_slash_command`
- `get_run_status`
- `get_run_logs`
- `get_app_state`

`run_slash_command` returns a `run_id` and polling guidance. MCP clients should poll the same run with `get_run_status` or `get_run_logs` while `do_not_rerun=true`, rather than submitting duplicate commands. The internal synth tools remain available only behind validated slash-command leaves.

Coach commands appear through `list_commands` and execute only through `run_slash_command`; adding the coach does not add raw playback, MIDI injection, attempt-history, filesystem, device, or authoring-provider tools. Both HTTP and stdio MCP surfaces remain command-control-only through the localhost server bound to `127.0.0.1`.

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

## Manual Akai and Audio Acceptance

Automated tests use synthetic MIDI and do not prove musical usefulness or physical audio behavior. Before declaring the coach ready, complete this listening gate with the Akai Professional LPK25 Wireless and audio output at a safe level:

1. Launch with `uv run -m halcyon` and confirm the Akai is the input. Configure an OpenAI key only when using natural-language routing or first-time generated authoring; direct replay of an imported lesson is offline.
2. Run `/coach teach "Aphex Twin Avril 14th" --segment "main melody" --tempo 70 --author never` (or make the same request in natural language). Confirm `user_supplied` / `verified` provenance and a cache hit on the imported revision.
3. Hear the Rust-rendered demonstration and see `◆ HALCYON` in the keyboard and activity log. Hold the same pitch on the Akai, confirm `●◆ BOTH`, and verify either origin can release independently without silencing the other.
4. Follow the count-in, play the phrase, inspect deterministic pitch/timing/duration feedback, then retry a smaller segment or lower tempo and judge whether the adaptation is useful.
5. Stop while Halcyon holds a note. Confirm no stuck coach voice, human-held notes remain unaffected, and reconnect completes without stale keyboard/audio state.
6. Quit, disable the authoring provider, relaunch, and repeat the same direct `/coach teach ... --author never` request. Confirm a cache hit and audible replay without regeneration.

The user must approve the melody's usefulness, sound, and feedback quality after this gate; passing the automated matrix alone is not that approval.

## Legacy Code

The original hand-coded Rich scripts remain in place:

- `halcyon.py`
- `Modules/menu.py`
- `Modules/midi.py`
- `Modules/synth.py`
