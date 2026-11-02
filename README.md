# Project Halcyon

Python MIDI controller, synth, and drum experiment.

The current iteration is a Textual TUI that listens to real MIDI input, logs note press/release events, shows a full-spectrum virtual keyboard, and streams MIDI note events to a Rust audio sidecar.

The first target controller is an Akai Professional LPK25 Wireless connected over Bluetooth MIDI. Python owns MIDI and UI for now; Rust owns realtime audio output.

The newest surface is a responsive performance cockpit: a persistent header identifies the controller, active `OSCILLATOR` or `COACH` mode, source cues, and pitch palette above one uninterrupted visualization-and-piano instrument. The full-width upper stage sits above a lower row shared by the command console and bounded MIDI activity view. Sound design and preset management live in a persistent Textual settings modal rather than competing with the instrument for permanent screen space.

Halcyon also includes a local AI Learning MIDI Coach. Human controller input and coach demonstrations enter one Python-owned MIDI event bus, then the same required audio consumer sends normalized note commands to the Rust oscillator/DSP sidecar. The coach can demonstrate a saved timed-MIDI lesson, capture a human-only attempt, score it deterministically, and reuse the saved revision without authoring it again.

The public control ABI is `halcyon/menu.xml`: chat, the shared conversational assistant, and MCP all resolve changes to registered slash commands. Those slash leaves call internal safe tools, but the assistant and external MCP clients do not get raw shell, file, MIDI-device, or network tools.

## License

Halcyon's source code is available under the [MIT License](LICENSE).
Imported music, MIDI files, and third-party assets retain their respective rights;
they are not bundled or relicensed by this project.

## Run

Use `uv` for all development commands:

```bash
uv run -m halcyon
```

If a macOS Python 3.15 environment fails to load Pydantic's native module, select Python 3.13 with `uv run --python 3.13 -m halcyon`. If iCloud prevents Cargo from updating its build cache, place it outside iCloud with `CARGO_TARGET_DIR=/tmp/halcyon-audio-build`.

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
- Show a command chat panel. Direct synth and coach slash commands work without OpenAI; the conversational assistant uses the LLM when a key is saved in Settings or supplied through `OPENAI_API_KEY`.
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
- Press `,` outside text editors to open Sound Settings, or use `Ctrl+S` or `F2` while typing. Press Escape, use Close, or click the dimmed backdrop to dismiss it.
- On compact terminals, use `F1` for Stage, `F2` for Settings, `F3` for MIDI, and `F4` for Console.
- Type a message or `/` command in the console. Enter submits once; Shift+Enter adds a newline. Pasted multiline text waits for Enter. There is no Send button.
- The shared assistant can answer natural-language messages directly or execute validated slash commands from `menu.xml`.
- `Ctrl+Q` quits.

User presets are saved outside the repository at
`$HALCYON_PRESETS_PATH` when set, otherwise
`$XDG_CONFIG_HOME/project-halcyon/synth-presets.json` or
`~/.config/project-halcyon/synth-presets.json`.

## Command Chat

The console keeps messages, commands, and progress logs in one in-memory transcript. Assistant replies render Markdown and fenced code; command chunks show their source, run status, and optional collapsed details. Chat, assistant child commands, and external MCP requests share the same tracked execution history.

Type a single-line slash prefix at the end of the composer to see registry-derived suggestions. Up/Down selects a suggestion and Tab accepts it. Without suggestions, Up on the first input line recalls an older entry; Down on the last line moves toward newer entries and restores your unsent draft. Recall is editable and never executes a command. History retains up to 100 safe entries in memory.

Drag-select transcript or composer text to copy it automatically, or use normal selection copy/paste shortcuts. On macOS, explicit paste reads the native clipboard and copy also uses `pbcopy`, with Textual clipboard fallback if native access fails. Right-click a transcript chunk for selection or raw-output copy. Fenced code has a **Copy code** control. A completed, failed, cancelled, or timed-out command offers **Re-run command** only when its exact safe slash input is still available; rerun validates it again and creates a fresh run. Copy, history recall, and polling never execute commands. Other platforms use Textual's clipboard path.

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

The in-app LLM is a conversational assistant shared by chat and MCP. It can answer musical questions, read bounded live app state including saved-song titles, lookup aliases, provenance, available segments and supported practice layers, and execute registered slash commands from `halcyon/menu.xml`. It inspects real command outcomes before choosing dependent steps and waits for long-running coach operations without blocking Textual. It cannot call raw synth tools, inject MIDI, access files or devices, or recursively ask itself. It cannot hear the audio or turn the oscillator engine into a sampled piano.

Subjective mood, genre, artist, or song references can become best-effort `/synth patch ...` commands. Every command is validated before anything changes. Invalid calls return an error to the assistant so it can correct the request. Direct synth and coach slash commands bypass OpenAI; `/assistant ask "GOAL" [--session-id ID]` explicitly invokes it.

Conversations retain at most eight bounded history messages in memory, across at most 32 sessions. The UI continues its current session automatically; MCP callers pass the returned `session_id`. Unknown or expired sessions return an explicit error rather than silently pretending to remember. Requests are single-flight across both entry points and limited to 12 tool steps, 16 model turns, 32,000 reported tokens, and ten minutes. Limits stop further actions; they do not undo earlier commands. A timed-out in-flight model request cannot start more actions or admit an overlapping ask. API requests disable server-side storage and automatic retries, with a 30-second per-request timeout. Automated tests use fake model responses only.

Direct `/coach stop` and `/panic` remain available while an assistant request is busy. They do not wait for the model. An assistant timeout requests cleanup only for its own active child operation, not another caller's run.

For generated sound-design patches, `volume` is output trim rather than tone color. The assistant is instructed to preserve it unless the user requests a loudness change. The Rust core retains polyphony-aware mix gain and output headroom.

Current command surface:

- `/assistant ask "GOAL" [--session-id ID]`
- `/synth state`
- `/synth params`
- `/synth set <param> <0..1>`
- `/synth patch <param=value ...>`
- `/preset apply <name>`
- `/preset save <name>`
- `/panic`

## AI Learning MIDI Coach

Natural-language requests to teach or demonstrate a melody use `/coach teach`. For example, “Teach me Aphex Twin's Avril 14th main melody” is coaching, while “make the synth sound like Aphex Twin” is sound design under `/synth patch`. After a lesson is active, an explicit request to practice can use `/coach attempt`, and advice can use its aggregate feedback. The assistant may choose only registered commands; it cannot inject raw MIDI or call a hidden coach tool.

The six coach commands are:

- `/coach teach QUERY [--segment LABEL --tempo 25..200 --practice melody|essentials|full --difficulty foundation|standard|challenge --hand left|right|both --loops 1..8 --author never|bounded]` resolves a reusable lesson and demonstrates it. The command stops after the audible playback. Practice defaults to `full`.
- `/coach replay [--segment LABEL --tempo 25..200 --practice melody|essentials|full --loops 1..8]` demonstrates the active saved revision without authoring or overwriting it. Omitted practice retains the active layer.
- `/coach attempt [--segment LABEL --tempo 25..200 --practice melody|essentials|full --count-in 0..8]` captures and scores a fresh human-only attempt without replaying or authoring. Omitted practice retains the active layer.
- `/coach feedback [attempt_id]` ends the current capture early or retrieves one bounded deterministic report.
- `/coach state` returns bounded phase, lesson settings, progress, cache/fidelity information, and the latest aggregate report.
- `/coach stop` requests cancellation and releases only the active session's Halcyon-owned voices; it is not a global panic.

### Coach note highway

While a `/coach teach`, `/coach replay`, or `/coach attempt` operation is active, the performance
stage replaces the oscillator scope with an anticipatory falling-note highway and hides only the Wave, Orbit, and Radial controls.
The performance header remains visible in `COACH` mode. Violet bars are natural pitches that map
to white piano keys; cyan bars are sharp pitches that map to black piano keys. `◆`
identifies Halcyon's demonstration and `○` identifies an expected practice note in either color.
The chord's note names and time to the strike rail remain visible without relying on color alone.

The surface follows the active coach operation. Resolving and authoring show status without
inventing notes. Demonstration moves Halcyon bars toward the strike rail, then `/coach teach`
finishes, clears its cues, and restores the oscillator. Run `/coach attempt` when ready to practice:
count-in freezes the opening target near the rail and displays `4`, `3`, `2`, `1`; capture advances
expected targets without making live hit or miss claims; scoring clears the moving targets; and
review shows only the bounded aggregate score and tips already allowed by the coach contract.
Cancellation keeps the highway visible until owner-scoped voice cleanup settles.

The highway is a read-only view of the deterministic coach schedule. It does not dispatch notes,
score events, or add a command, HTTP route, or MCP tool. Public and assistant state remain
aggregate-only. Raw attempts, full lessons, note schedules, source paths, and timing anchors stay
inside the existing private runtime boundary.

When coaching ends, Halcyon reveals the same mounted oscillator scope and its controls in the mode
that was selected before the lesson. The scope is hidden during coaching, not recreated or reset. On a
short terminal, the highway reduces to a stable one-line cue such as `○ C4+E4 0.8s` above the same
strike rail rather than overlapping the keyboard or compact navigation.

### Practice layers and ghost notes

Tempo and note complexity are independent. `--practice melody` targets a source-specific
melody selection; `--practice essentials` adds essential bass/chord anchors; `--practice full`
targets every note in the saved selection. These layers select original MIDI notes at their
original positions, not newly generated music. `--difficulty` still selects a declared segment
rating and is not a note-reduction control. “Beginner” alone normally means slower tempo.

Notes outside the chosen layer remain visible as dim, dotted ghosts, with purple naturals and
cyan sharps preserved. Demonstration sounds only required notes; ghosts never sound automatically
or activate required-key cues. They do not count as missed notes; playing them at the correct
time does not incur an extra-note penalty. Unrelated notes are still scored. The highway header
identifies the active layer and required/ghost counts; the strike rail and keyboard remain
adjacent and horizontally aligned. Original timing is unchanged, including a silent opening
when a melody begins after an accompaniment-only introduction.

Supported layers are listed in each saved library entry. Reduced layers are bound to reviewed
note mappings for the exact imported source, not guessed from pitch or note length. The current
Satie import has melody, essentials and full selections. The current Avril import contains only
the declared main melody, so all 199 existing notes remain targets in all three layers; it cannot
show accompaniment that was never imported. Other source revisions support `full` unless a
mapping exists. An unavailable reduced layer returns `PRACTICE_UNAVAILABLE`; use `--practice full`
instead. Source mappings still require musical approval in real practice.

After teaching a supported source, run `/coach replay --practice melody --tempo 25`, then
`/coach attempt` to practice the same layer. Move up with `/coach replay --practice essentials`
or restore `/coach replay --practice full`. Changing tempo alone preserves the active layer.

### First use and offline replay

Lesson teaching is saved-only by default (`--author never`). Halcyon loads the immutable saved revision without calling the authoring provider; a missing song returns an error rather than generating substitute music. The assistant receives the saved library's usable aliases, declared segments and hand/difficulty metadata, and prefers verified imported MIDI over generated practice artifacts. “Beginner” normally means slower playback of the same notes, not a different arrangement or an unsupported difficulty filter.

For example, with an imported lesson saved under that alias, run `/coach teach "Aphex Twin Avril 14th" --segment "main melody" --tempo 25` for slow playback. Use the segment labels actually declared by the imported lesson; the assistant can discover them without asking you to remember internal names. A melody-only import is not presented as a full piano score.

Generation remains available when explicitly requested with `--author bounded` and a named segment. The provider returns a closed structured proposal, which Python validates, saves outside the repository, and labels `approximate`, not an exact transcription. Invalid, refused, cancelled or over-limit proposals are not saved. The repository does not bundle these songs; imported and generated lessons play through the same Rust oscillator path.

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

The persistent performance legend, keyboard, and activity UI use text/shape as well as color:

- `● YOU` means a human-held note.
- `◆ HALCYON` means a coach-held note. Natural notes use violet; sharp notes use cyan.
- `●◆ BOTH` means both independently hold the same pitch.
- `♮ NATURAL` and `♯ SHARP` identify the pitch-class palette in the top legend.
- `○` on a key marks an upcoming expected note and follows the same natural-violet, sharp-cyan palette. The keyboard renderer contains no embedded text legend, keeping the strike rail directly adjacent to its aligned keys.

Numeric voice identity crosses the Python/Rust boundary, so one origin releasing a shared pitch does not release the other's voice. `/coach stop`, timeout, replacement, and app teardown settle owner-scoped Halcyon cleanup before reporting a terminal cancellation. If authoring has already crossed its short persistence commit fence, stop reports that the operation is finishing instead of falsely claiming cancellation. `/panic` is the explicit global all-notes-off action. MIDI reconnect (`r`) also requests global audio safety and resets bus/keyboard state only after Rust accepts all-notes-off or the old sidecar is proven stopped; unsafe cleanup never becomes silent success.

Python remains the MIDI, lesson, session, command, and Textual UI control plane. Hardware events and synthetic Halcyon lesson events publish to one shared bus, which fans out to UI/log/capture consumers and exactly one required Rust-audio consumer. Rust—not React—owns the realtime oscillator and DSP, receiving normalized note events and numeric voice IDs over newline-delimited JSON.

## Local MCP Bridge

When the app is running, it starts a local HTTP control server on `127.0.0.1`, beginning at port `8769` and moving up if the port is busy. The chat log prints the selected URL. The HTTP surface is command-control only:

- `GET /health`
- `GET /commands`
- `GET /state`
- `POST /run_slash`
- `POST /ask`
- `POST /wait_for_run`
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
- `ask_tui`
- `wait_for_run`

`run_slash_command` returns a `run_id` and polling guidance. MCP clients should poll the same run with `get_run_status` or `get_run_logs` while `do_not_rerun=true`, rather than submitting duplicate commands. The internal synth tools remain available only behind validated slash-command leaves.

Use `ask_tui(goal="Prepare the saved Avril 14th main melody at 25% tempo without authoring", max_wait_s=1)` to delegate a goal to Halcyon's own assistant. It dispatches through the registered `/assistant ask` command. Continue with `ask_tui(goal="Make it a little brighter", session_id="<returned session_id>")`. Goals accept up to 2,000 characters. The response identifies the tracked parent run and session. Assistant-executed command runs include `parent_run_id` and remain individually inspectable. Goals and command arguments are not copied into public run logs.

MCP callers can read `get_app_state.assistant.chat_session_id` to continue the UI conversation explicitly. External requests, assistant progress, and replies also appear in the app's chat. Different session IDs keep their conversational histories separate even though they control the same live instrument.

While `terminal=false`, call `wait_for_run(run_id="<returned run_id>", cursor=<previous next_cursor>, max_wait_s=10)`. Waits return only new events, a stable `next_cursor`, terminal results including `result.reply`, and a verdict with errors, warnings, and `needs_attention`. Event eviction is signaled with `cursor_truncated`. Run completion alone does not mean the musical goal succeeded: inspect the reply and verdict. Sessions disappear on restart; tracked runs are bounded and may be evicted. HTTP waits run off the UI thread, are capped at ten seconds, and the stdio bridge shortens them if its HTTP timeout is lower. Local HTTP requests reject foreign Host and Origin headers.

Coach commands appear through `list_commands` and execute through the same command executor whether chosen directly or by the assistant. Neither surface adds raw playback, MIDI injection, attempt-history, filesystem, device, or authoring-provider tools. Both HTTP and stdio MCP surfaces remain localhost-only on `127.0.0.1`.

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
On taller terminals, the performance stage and keyboard have bounded heights so extra rows grow the command console.

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

## Manual Akai, Audio, and Highway Acceptance

Automated tests use synthetic MIDI and do not prove musical usefulness or physical audio behavior. Before declaring the coach ready, complete this listening gate with the Akai Professional LPK25 Wireless and audio output at a safe level:

1. Launch with `uv run -m halcyon`, confirm the Akai is the input, select Orbit or Radial, and set audio to a safe level. Configure an OpenAI key only for natural-language routing or first-time generated authoring. Direct replay of an imported lesson is offline.
2. Run `/coach teach "Aphex Twin Avril 14th" --segment "main melody" --tempo 70 --author never` (or make the same request in natural language). Confirm `user_supplied` / `verified` provenance, a cache hit, and that the persistent header switches to `COACH`, the three scope controls hide, and the highway replaces the scope without interrupting audio.
3. Hear the Rust-rendered demonstration while `◆ HALCYON` bars meet the text-free strike rail in time. Confirm natural pitches are violet, sharp pitches are cyan, and the existing keyboard and activity cues remain correct. When playback ends, confirm the highway and all target/held-key cues clear, the oscillator returns, and advancing time produces no later coach notes.
4. Run `/coach attempt`, follow the visible count-in, then play from the Akai while `○` cues and note/time copy anticipate expected notes without live hit or miss claims. Confirm scoring and bounded review appear without an event-by-event attempt history.
5. Hold the same pitch as Halcyon, confirm `●◆ BOTH`, and verify either origin can release independently without silencing the other.
6. Stop while Halcyon holds a note. Confirm the highway remains in its stopping state until cleanup settles, no coach voice sticks, and a human-held note remains unaffected.
7. Confirm the previous Orbit or Radial mode and its controls return unchanged. Then cycle Wave, Orbit, and Radial to confirm the restored scope remains usable.
8. Resize through wide, medium, and compact terminals. Approve legibility, non-color cues, musical anticipation, and the compact fallback without keyboard or navigation overlap.
9. Quit, disable the authoring provider, relaunch, and repeat the same direct `/coach teach ... --author never` request. Confirm a cache hit and audible replay without regeneration.

The user must approve timing, visual legibility, musical usefulness, sound, and feedback quality after this gate. Passing the automated matrix alone is not hardware or listening acceptance.

## Legacy Code

The original hand-coded Rich scripts remain in place:

- `halcyon.py`
- `Modules/menu.py`
- `Modules/midi.py`
- `Modules/synth.py`
