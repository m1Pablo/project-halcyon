# AGENTS.md

This file defines how agentic work is done in this repository.

Project Halcyon is a Python MIDI controller/synth/drum experiment moving from a small hand-coded Rich CLI toward a Textual TUI. Preserve the original intent while modernizing deliberately.

## Project Structure

- `halcyon.py` - original top-level menu app.
- `Modules/menu.py` - original Rich/input menu helper.
- `Modules/midi.py` - MIDI port listing and polling helpers.
- `Modules/synth.py` - original synth flow, currently incomplete.
- `halcyon/` - new package location for the next Textual iteration.
- `halcyon-audio/` - Rust workspace for the realtime oscillator sidecar and DSP core.

## Git Workflow

- The default branch is `main`. If a clone still has `master`, rename it to `main` before shipping new work.
- Implementation happens on feature branches. Use `codex/{short-description}` for Codex-led work unless the user gives a specific branch name.
- Keep `main` clean and push feature branches to GitHub for review.
- GitHub CLI and repository operations for this repository must use the `m1Pablo` account. Before any `gh` operation, verify the active account and, when needed, run `gh auth switch --hostname github.com --user m1Pablo`.
- Use and verify the repository `m1Pablo/project-halcyon`; prefer explicit `--repo m1Pablo/project-halcyon` arguments where supported.
- If access to `m1Pablo/project-halcyon` fails, stop and surface the blocker. Do not create a replacement repository or change `origin` as an automatic fallback.
- Stage explicit owned files only. Never use `git add .` or `git add -A`.
- Do not commit `.env`, `.env.*`, editor settings, secrets, or generated caches.
- Draft PRs are the default shipping surface unless the user explicitly asks for a direct push-only flow or ready-for-review PR.

## Product Direction

Near-term target:

- Get the app running in its current spirit.
- Prefer Textual for the primary UX; use Rich only inside Textual widgets when useful.
- Log MIDI input events clearly: event type, note, velocity, channel/status, delta time, and source port.
- Show a virtual keyboard with pressed/released key state.
- Sound output is not required for the first Textual iteration.
- The next iteration adds Rust sound output through `halcyon-audio/`; Python remains the MIDI/UI control plane.
- Current synth controls are Python-owned patch state streamed to Rust via `set_param`; keep the parameter registry in `halcyon/synth_params.py` as the UI/tooling source of truth.
- User synth presets are saved outside the repository through `halcyon/preset_store.py`.
- `halcyon/menu.xml` is the public command ABI for chat and MCP. The OpenAI assistant may only route natural language to registered slash commands or reject; it must not call raw synth tools directly.
- The local MCP bridge may only expose command-control tools: `list_commands`, `run_slash_command`, `get_run_status`, `get_run_logs`, and `get_app_state`. Safe synth tools in `halcyon/mcp_tools.py` remain internal slash-command leaves, including preset save/load but no shell, file, MIDI-device, or arbitrary network access.

## Validation Policy

All development commands run through `uv`. Do not use bare `python`, `pip`, `pytest`, `ruff`, or app entrypoints when a `uv run ...` equivalent exists.

Run checks appropriate to the change scope before reporting completion:

```bash
uv run ruff format
uv run ruff check
uv run pytest
uv run -- cargo fmt --all --check --manifest-path halcyon-audio/Cargo.toml
uv run -- cargo clippy --manifest-path halcyon-audio/Cargo.toml --all-targets -- -D warnings
uv run -- cargo test --manifest-path halcyon-audio/Cargo.toml
```

The app entrypoint is:

```bash
uv run -m halcyon
```

Until the repo has a full test harness, run the narrowest available `uv run ...` syntax/import checks and state any missing validation plainly.

## Engineering Notes

- Do not silently rewrite the original code. Migrate behavior into new modules with small, reviewable steps.
- Avoid hard-coded absolute paths; the original `/home/pi/...` path is legacy.
- Keep MIDI hardware optional in tests; use mocks or synthetic events.
- Prefer the real Bluetooth MIDI path at runtime. The primary controller is an Akai Professional LPK25 Wireless.
- Rust audio receives normalized events from Python over newline-delimited JSON stdin; do not duplicate MIDI device ownership in Rust until a later spec says so.
- Rust also receives synth parameter changes from Python over the same stdin protocol; replay the current patch after sidecar restart/reconnect.
- Rust sends waveform visualization samples over a separate Unix domain socket configured by `HALCYON_VIZ_SOCKET`; do not add sample payloads to the JSON control protocol.
- Visualization frames are raw `HZV1` + little-endian `u32` count + little-endian `f32` samples. Keep sending best-effort and off the audio thread.
- Keep audio callback code realtime-safe: no blocking calls, logging, heap allocation, or locks in the CPAL callback.
- Accept OpenAI keys only from `OPENAI_API_KEY` or the app's attested native OS credential vault. Environment values take precedence; never read `.env`, use a plaintext fallback, print API keys, expose them through MCP/app state or the Rust audio-sidecar environment, or make live OpenAI calls in automated tests.
- Local MCP control must bind to `127.0.0.1` and expose only command-control tools. Do not re-advertise raw synth tools through the public HTTP or stdio MCP surfaces.
- Standard MIDI file import must remain an explicit bounded local CLI operation: regular format-0/1 `.mid`/`.midi` files only, no private paths in persisted/public data, and no file-import surface in chat, HTTP, `menu.xml`, or MCP.
- New chat or MCP actions must be added first to `halcyon/menu.xml`; update the command executor/router tests so slash autocomplete, LLM routing, and MCP command listing stay derived from the same registry.
- Preset persistence must keep built-in presets read-only and write user presets outside the repo unless a test explicitly overrides `HALCYON_PRESETS_PATH`.
- Textual CSS is not web CSS. Verify with the Textual app, not browser assumptions.
- Do not commit secrets, generated logs, local plans, or downloaded third-party skill copies.
- Do not commit local editor settings such as `.vscode/`.
