import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from rich.text import Text

from halcyon.app import HalcyonApp
from halcyon.audio_bridge import AudioBridgeSnapshot
from halcyon.audio_viz import AudioVisualizationSnapshot
from halcyon.coach_highway import CoachVisualizationSnapshot, HighwayNoteSpan
from halcyon.event_bus import HalcyonMidiAdapter, MidiEventAdapter
from halcyon.keyboard import key_bounds
from halcyon.midi import MidiEvent, MidiOrigin
from halcyon.mcp_bridge import MCPBridge
from halcyon.oscillator_scope import ScopeMode


class FakeAudio:
    def start(self) -> bool:
        return True

    def apply_patch(self, patch: dict[str, float]) -> bool:
        return True

    def snapshot(self) -> AudioBridgeSnapshot:
        return AudioBridgeSnapshot(running=True, ready=True, text="Audio: ready")

    def visualization_snapshot(self) -> AudioVisualizationSnapshot:
        return AudioVisualizationSnapshot(
            socket_path="/tmp/halcyon-test.sock",
            sample_count=4,
            connected=True,
            frame_count=1,
            samples=(0.0, 0.5, 0.0, -0.5),
        )

    def all_notes_off(self) -> bool:
        return True

    def handle_midi_event(self, event: MidiEvent) -> bool:
        return True

    def close(self) -> None:
        pass


class FakeMidi:
    connected = False

    def list_ports(self) -> list[str]:
        return ["Akai LPK25 Wireless"]

    def connect(self) -> str:
        return "Akai LPK25 Wireless"

    def poll(self) -> list[MidiEvent]:
        return []

    def close(self) -> None:
        pass


def cockpit() -> HalcyonApp:
    app = HalcyonApp()
    app.audio = FakeAudio()  # type: ignore[assignment]
    app.midi = FakeMidi()  # type: ignore[assignment]
    return app


def test_chat_and_mcp_share_the_real_agent_service_and_session_without_double_execution():
    async def run():
        app = cockpit()
        client = MagicMock()

        def tool(command):
            return {
                "type": "function_call",
                "name": "run_slash_command",
                "call_id": "c1",
                "arguments": json.dumps({"command": command}),
            }

        def response(items=(), text=""):
            return SimpleNamespace(
                status="completed",
                output=list(items),
                output_text=text,
                usage=SimpleNamespace(total_tokens=20),
            )

        client.responses.create.side_effect = [
            response([tool("/synth set tone 0.22")], "I will darken the current patch."),
            response(text="Tone is now 0.22."),
            response([tool("/synth set tone 0.12")]),
            response(text="Tone is now 0.12."),
        ]
        app.assistant._client = client
        replies = []
        requests = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            original = app.chat_panel.append_event

            def write(event):
                if event.kind == "message" and event.role == "assistant":
                    replies.append(event.text)
                if event.kind == "message" and event.role == "user":
                    requests.append(event.text)
                original(event)

            app.chat_panel.append_event = write
            app._submit_natural_chat("make it darker")
            for _ in range(100):
                await asyncio.sleep(0.02)
                if replies:
                    break
            assert replies == ["Tone is now 0.22."]
            assert requests == ["make it darker"]
            assert app.synth_patch.values["tone"] == 0.22
            session = app._chat_assistant_session_id
            bridge = MCPBridge(api_url=app.local_control_server.api_url)
            answer = await asyncio.to_thread(
                bridge.handle_message,
                {
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "ask_tui",
                        "arguments": {
                            "goal": "a little darker still",
                            "session_id": session,
                            "max_wait_s": 1,
                        },
                    },
                },
            )
            payload = json.loads(answer["result"]["content"][0]["text"])
            while not payload.get("terminal"):
                answer = await asyncio.to_thread(
                    bridge.handle_message,
                    {
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "wait_for_run",
                            "arguments": {
                                "run_id": payload["run_id"],
                                "cursor": payload.get("next_cursor", 0),
                                "max_wait_s": 1,
                            },
                        },
                    },
                )
                payload = json.loads(answer["result"]["content"][0]["text"])
            assert payload["status"] == "completed"
            assert payload["result"]["reply"] == "Tone is now 0.12."
            assert app.synth_patch.values["tone"] == 0.12
            assert "make it darker" in str(
                client.responses.create.call_args_list[2].kwargs["input"]
            )
            assert "Tone is now 0.22." in str(
                client.responses.create.call_args_list[2].kwargs["input"]
            )
            assert client.responses.create.call_count == 4
            await pilot.pause()
            assert replies == ["Tone is now 0.22.", "Tone is now 0.12."]
            assert requests == ["make it darker", "a little darker still"]
            assert app._run_tracker.get_state()["runs_total"] == 4
            child = payload["result"]["steps"][0]["run_id"]
            assert app._run_tracker.get_status(child)["parent_run_id"] == payload["run_id"]

    asyncio.run(run())


def active_highway_snapshot(
    *,
    phase: str = "capturing",
    sampled_at_ns: int = 1,
    note: int = 60,
) -> CoachVisualizationSnapshot:
    return CoachVisualizationSnapshot(
        active=True,
        phase=phase,
        sampled_at_ns=sampled_at_ns,
        segment_label="main",
        tempo_percent=70,
        loop_count=1,
        fidelity="verified",
        spans=(
            HighwayNoteSpan(
                note=note,
                channel=1,
                loop_index=0,
                timeline_ordinal=0,
                start_offset_us=800_000,
                end_offset_us=1_200_000,
                cue_kind="target",
            ),
        ),
        current_loop=0,
        target_notes=(note,),
        target_offset_us=800_000,
    )


def test_runtime_size_classes_and_non_overlapping_wide_medium_layouts() -> None:
    async def run() -> None:
        cases = (
            ((180, 55), "-wide", 21, 14, 13),
            ((150, 48), "-wide", 18, 11, 9),
            ((120, 42), "-medium", 16, 9, 8),
            ((110, 40), "-medium", 15, 8, 7),
        )
        for size, expected, lower_height, log_height, scope_height in cases:
            app = cockpit()
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                assert app.screen.has_class(expected)
                workspace_host = app.query_one("#workspace-host")
                stage = app.query_one("#performance-stage")
                lower = app.query_one("#lower-workspace")
                console = app.query_one("#command-console")
                activity = app.query_one("#activity-panel")
                chat_log = app.query_one("#chat-log")
                scope = app.query_one("#oscillator-scope")
                keyboard_scroll = app.query_one("#keyboard-scroll")
                surface = app.query_one("#instrument-surface")

                assert not console.query(".panel-title")
                assert stage.region.x == workspace_host.region.x
                assert stage.region.width == workspace_host.region.width
                assert lower.region.height == lower_height
                assert console.display and activity.display
                assert console.region.y == activity.region.y == lower.region.y
                assert console.region.height == activity.region.height == lower_height
                assert not console.region.overlaps(activity.region)
                assert not stage.region.overlaps(console.region)
                assert not stage.region.overlaps(activity.region)
                assert chat_log.region.height == log_height
                assert scope.region.height == scope_height
                assert scope.region.width == keyboard_scroll.region.width
                assert keyboard_scroll.region.width == stage.content_region.width
                assert scope.region.bottom == keyboard_scroll.region.y
                assert surface.region.y == scope.region.y
                assert surface.region.bottom == keyboard_scroll.region.bottom
                if expected == "-wide":
                    assert keyboard_scroll.region.height == 12
                    assert app.keyboard.region.height == 11
                    assert app.keyboard.region.bottom == (
                        keyboard_scroll.scrollable_content_region.bottom
                    )
                    assert "C4" in app.keyboard.render_line(9).text
                    assert app.keyboard.render_line(10).text.startswith("└")

    asyncio.run(run())


def test_height_only_resize_shares_space_with_keyboard_and_console_in_both_modes() -> None:
    async def run() -> None:
        for coaching in (False, True):
            app = cockpit()
            if coaching:
                snapshot = active_highway_snapshot()
                app.coach.visualization_snapshot = lambda: snapshot  # type: ignore[method-assign]
                render_rows = MagicMock(wraps=app.coach_highway._render_rows)
                app.coach_highway._render_rows = render_rows  # type: ignore[method-assign]
            async with app.run_test(size=(122, 42)) as pilot:
                for height, stage_rows, visual_rows, key_rows, lower_rows, log_rows in (
                    (42, 23, 8, 8, 16, 9),
                    (60, 34, 16, 11, 23, 16),
                    (80, 36, 18, 11, 41, 34),
                    (42, 23, 8, 8, 16, 9),
                ):
                    await pilot.resize_terminal(122, height)
                    await pilot.pause()
                    assert app.screen.has_class("-medium")
                    stage = app.query_one("#performance-stage")
                    visual = app.coach_highway if coaching else app.scope
                    keyboard_scroll = app.keyboard_scroll
                    lower = app.query_one("#lower-workspace")
                    console = app.query_one("#command-console")
                    log = app.query_one("#chat-log")

                    assert visual.display
                    assert stage.region.height == stage_rows
                    assert visual.region.height == visual_rows
                    assert app.keyboard.region.height == key_rows
                    assert app.keyboard.region.height == (
                        keyboard_scroll.scrollable_content_region.height
                    )
                    assert app.keyboard.region.bottom == (
                        keyboard_scroll.scrollable_content_region.bottom
                    )
                    assert lower.region.height == console.region.height == lower_rows
                    assert log.region.height == log_rows
                    assert visual.region.bottom == keyboard_scroll.region.y
                    assert visual.region.width == keyboard_scroll.region.width
                    assert stage.region.width == app.query_one("#workspace-host").region.width
                    assert not stage.region.overlaps(lower.region)
                    assert console.region.bottom <= app.query_one("#workspace-host").region.bottom
                    if coaching:
                        assert render_rows.call_args.kwargs["lookahead_us"] == 3_000_000
                        assert visual.render_line(visual.size.height - 1).text == (
                            "═" * visual.size.width
                        )

    asyncio.run(run())


def test_compact_workspace_navigation_is_deterministic_and_reachable() -> None:
    async def run() -> None:
        app = cockpit()
        async with app.run_test(size=(90, 30)) as pilot:
            assert app.screen.has_class("-compact")
            workspace_host = app.query_one("#workspace-host")
            stage = app.query_one("#performance-stage")
            lower = app.query_one("#lower-workspace")
            activity = app.query_one("#activity-panel")
            console = app.query_one("#command-console")
            assert stage.display
            assert stage.region == workspace_host.region
            assert not lower.display

            await pilot.press("f2")
            await pilot.pause()
            assert app.screen is app.settings_screen
            assert app.settings_screen.has_class("-compact")
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not app.settings_screen

            await pilot.press("f3")
            assert not stage.display
            assert lower.display
            assert activity.display
            assert not console.display
            assert activity.region == workspace_host.region

            await pilot.press("f4")
            assert not stage.display
            assert lower.display
            assert console.display
            assert not activity.display
            assert console.region == workspace_host.region
            assert app.query_one("#chat-input").region.height > 0

            await pilot.press("f1")
            assert stage.display
            assert stage.region == workspace_host.region
            assert not lower.display

    asyncio.run(run())


def test_short_terminals_use_full_height_compact_console_without_clipping() -> None:
    async def run() -> None:
        for size in ((110, 39), (90, 30)):
            app = cockpit()
            async with app.run_test(size=size) as pilot:
                assert app.screen.has_class("-compact")
                await pilot.press("f4")
                await pilot.pause()

                workspace_host = app.query_one("#workspace-host")
                console = app.query_one("#command-console")
                chat_log = app.query_one("#chat-log")
                assert console.display
                assert console.region == workspace_host.region
                assert chat_log.region.height >= 18

    asyncio.run(run())


def test_scope_controls_and_complete_midi_activity_update() -> None:
    async def run() -> None:
        app = cockpit()
        async with app.run_test(size=(180, 55)) as pilot:
            initial_mode = app.scope.mode

            for _ in range(3):
                await pilot.click("#oscillator-scope")
                assert app.scope.mode is initial_mode

            await pilot.click("#scope-orbit")
            assert app.scope.mode is ScopeMode.ORBIT
            await pilot.click("#scope-radial")
            assert app.scope.mode is ScopeMode.RADIAL
            await pilot.click("#scope-wave")
            assert app.scope.mode is ScopeMode.WAVE

            event = MidiEvent(
                event_type="press",
                note=127,
                note_name="G9",
                velocity=101,
                channel=2,
                status=0x91,
                raw_message=(0x91, 127, 101),
                delta_time=0.0123,
                source="Akai LPK25 Wireless",
                elapsed=1.25,
            )
            app._handle_event(event)
            await pilot.pause()

            assert app.event_count == 1
            assert app.event_table.columns.keys()
            assert [column.label.plain for column in app.event_table.columns.values()] == [
                "Time",
                "Origin",
                "Event",
                "Note",
                "Vel",
                "Ch",
                "Status",
                "Delta",
                "Source",
            ]
            origin_cell = app.event_table.get_cell_at((0, 1))
            assert isinstance(origin_cell, Text)
            assert origin_cell.plain == "● YOU"
            assert app.keyboard_scroll.scroll_x > 0
            assert "EVENTS 1" in str(app.status_rail.render())

    asyncio.run(run())


def test_origin_and_keyboard_cues_remain_available_at_every_size_class() -> None:
    async def run() -> None:
        for size in ((180, 55), (120, 42), (90, 30)):
            app = cockpit()
            async with app.run_test(size=size) as pilot:
                human = MidiEventAdapter(
                    origin=MidiOrigin.HUMAN,
                    source_port="human://layout",
                )
                halcyon = HalcyonMidiAdapter(
                    source_port="halcyon://coach/layout",
                    session_id="coach-layout",
                )
                app.target_notes = {60}
                for event in (
                    human.note_on(60, 90),
                    halcyon.note_on(60, 80),
                    human.note_on(62, 70),
                    halcyon.note_on(64, 60),
                ):
                    app._handle_event(event)
                await pilot.pause()

                rendered = app.keyboard.render()
                visible_key_rows = rendered.plain.splitlines()[1:7]
                assert any("●◆" in row for row in visible_key_rows)
                assert any("●" in row for row in visible_key_rows)
                assert any("◆" in row for row in visible_key_rows)
                assert any("○" in row for row in visible_key_rows)
                legend = app.query_one("#keyboard-legend")
                legend_text = legend.render()
                assert "● YOU" in legend_text.plain
                assert "◆ HALCYON" in legend_text.plain
                assert "○ EXPECTED" in legend_text.plain
                assert "●◆ BOTH" in legend_text.plain
                assert "♮ NATURAL" in legend_text.plain
                assert "♯ SHARP" in legend_text.plain
                assert "═ STRIKE" in legend_text.plain
                assert legend_text.plain.count("◆ HALCYON") == 1
                assert "NEXT" not in legend_text.plain
                halcyon_offset = legend_text.plain.index("◆ HALCYON")
                halcyon_style = next(
                    str(span.style)
                    for span in legend_text.spans
                    if span.start <= halcyon_offset < span.end
                )
                assert "rgb(252,252,250)" in halcyon_style
                assert "rgb(171,157,242)" not in halcyon_style
                assert "rgb(120,220,232)" not in halcyon_style

                if app.screen.has_class("-compact"):
                    await pilot.press("f3")
                origins = [app.event_table.get_cell_at((row, 1)) for row in range(4)]
                assert [cell.plain for cell in origins if isinstance(cell, Text)] == [
                    "● YOU",
                    "◆ HALCYON",
                    "● YOU",
                    "◆ HALCYON",
                ]
                assert "#ff9f43" in str(origins[0].style)
                assert "#ab9df2" in str(origins[1].style)

    asyncio.run(run())


def test_coach_highway_tracks_keyboard_scroll_and_restores_same_scope() -> None:
    async def run() -> None:
        for size in ((180, 55), (120, 42), (90, 30)):
            app = cockpit()
            scope = app.scope
            scope.set_mode(ScopeMode.RADIAL)
            current = [active_highway_snapshot()]
            app.coach.visualization_snapshot = lambda: current[0]  # type: ignore[method-assign]

            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                highway = app.query_one("#coach-highway")
                stage = app.query_one("#performance-stage")
                header = app.query_one("#performance-header")
                toolbar = app.query_one("#scope-toolbar")
                controls = app.query_one("#scope-controls")
                mode_title = app.query_one("#surface-mode-title")
                performance_title = app.query_one("#keyboard-title")
                legend = app.query_one("#keyboard-legend")
                surface = app.query_one("#instrument-surface")
                keyboard_scroll = app.query_one("#keyboard-scroll")
                buttons = tuple(app.query_one(f"#scope-{mode.value}") for mode in ScopeMode)

                assert highway.display
                assert not scope.display
                assert header.display and toolbar.display
                assert not controls.display
                assert mode_title.render().plain == "COACH"
                assert performance_title.region.bottom <= highway.region.y
                assert mode_title.region.bottom <= highway.region.y
                assert legend.region.bottom <= highway.region.y
                assert highway.region.height >= 5
                assert highway.region.width == keyboard_scroll.region.width
                assert keyboard_scroll.region.width == stage.content_region.width
                assert not highway.region.overlaps(keyboard_scroll.region)
                assert header.region.bottom == surface.region.y == highway.region.y
                assert highway.region.bottom == keyboard_scroll.region.y
                rendered_highway = "\n".join(
                    highway.render_line(row).text for row in range(highway.size.height)
                )
                assert highway.render_line(highway.size.height - 1).text == (
                    "═" * highway.size.width
                )
                legend_copy = legend.render().plain
                for label in (
                    "● YOU",
                    "◆ HALCYON",
                    "○ EXPECTED",
                    "●◆ BOTH",
                    "♮ NATURAL",
                    "♯ SHARP",
                    "═ STRIKE",
                ):
                    assert label in legend_copy
                    assert label not in rendered_highway
                assert legend_copy.count("◆ HALCYON") == 1
                assert "NEXT" not in legend_copy
                assert all(button not in app.screen.focus_chain for button in buttons)
                assert app.target_notes == {60}

                c4_bounds = key_bounds(60)
                assert c4_bounds is not None
                manual_scroll = c4_bounds[0] - 2
                keyboard_scroll.scroll_x = float(manual_scroll)
                await pilot.pause()
                assert highway.viewport_origin == keyboard_scroll.scroll_offset.x
                assert highway.viewport_origin == manual_scroll
                onset = next(
                    highway.render_line(row).text
                    for row in range(1, highway.size.height - 1)
                    if "○" in highway.render_line(row).text
                )
                assert tuple(index for index, cell in enumerate(onset) if cell == "○") == tuple(
                    range(2, 2 + (c4_bounds[1] - c4_bounds[0]))
                )

                current[0] = active_highway_snapshot(sampled_at_ns=2, note=100)
                app.coach_highway._poll_snapshot()
                await pilot.pause()
                assert app.target_notes == {100}
                assert highway.viewport_origin == keyboard_scroll.scroll_offset.x
                note_bounds = key_bounds(100)
                assert note_bounds is not None
                visible_left = note_bounds[0] - highway.viewport_origin
                visible_right = note_bounds[1] - highway.viewport_origin
                assert 0 <= visible_left < visible_right <= highway.size.width
                onset = next(
                    highway.render_line(row).text
                    for row in range(1, highway.size.height - 1)
                    if "○" in highway.render_line(row).text
                )
                assert tuple(index for index, cell in enumerate(onset) if cell == "○") == tuple(
                    range(visible_left, visible_right)
                )

                app.query_one("#scope-wave").press()
                await pilot.pause()
                assert scope.mode is ScopeMode.RADIAL

                current[0] = CoachVisualizationSnapshot(
                    active=False,
                    phase="cancelled",
                    sampled_at_ns=2,
                )
                app.coach_highway._poll_snapshot()
                await pilot.pause()

                assert app.scope is scope
                assert scope.display
                assert toolbar.display
                assert controls.display
                assert mode_title.render().plain == "OSCILLATOR"
                assert not highway.display
                assert scope.mode is ScopeMode.RADIAL
                assert all(button in app.screen.focus_chain for button in buttons)
                assert app.target_notes == set()

    asyncio.run(run())
