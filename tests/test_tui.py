"""Headless Textual pilot tests for wlanpi-menu-tui against the schema-validated stub server."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.widgets import Static

from tests.stub_server import StubMenudServer, validate_frame
from wlanpi_menu_tui.app import MenuTuiApp
from wlanpi_menu_tui.blocks import render_block
from wlanpi_menu_tui.cli import build_parser


def _widget_text(app: MenuTuiApp, selector: str) -> str:
    widget = app.query_one(selector, Static)
    return str(widget.render())


async def _wait_until(predicate: Any, *, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    assert predicate(), "Condition not met before timeout"


async def test_cli_socket_override_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(["--socket", "/tmp/custom-menud.sock"])
    assert args.socket == "/tmp/custom-menud.sock"


async def test_handshake_subscribe_and_status_footer(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(90, 28)) as pilot:
            hello = await stub.wait_for_frame(lambda f: f["type"] == "hello")
            assert hello["mode"] == "thin"
            assert hello["protocol"] == [1]
            caps = hello["capabilities"]
            assert set(caps["blocks"]) == {
                "text",
                "kv",
                "table",
                "list",
                "qr",
                "status",
                "progress",
                "confirm",
            }
            assert set(caps["keys"]) == {
                "up",
                "down",
                "left",
                "right",
                "center",
                "key1",
                "key2",
                "key3",
            }

            sub = await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            assert set(sub["topics"]) == {"device.summary", "alerts", "complications"}

            await _wait_until(lambda: "wlanpi-stub" in _widget_text(app, "#status-footer"))
            await pilot.pause()

            footer_txt = _widget_text(app, "#status-footer")
            assert "wlanpi-stub" in footer_txt
            assert "192.0.2.10" in footer_txt
            assert "46.5C" in footer_txt
            assert "Uplink: OK" in footer_txt
            assert "Low battery 15%" in footer_txt

            tree_txt = _widget_text(app, "#menu-tree-view")
            assert "Menu Tree [rev 1] (classic)" in tree_txt
            assert "Network (network)" in tree_txt
            assert "Reboot (system.reboot) [confirm]" in tree_txt


async def test_all_block_types_and_unknown_fallback(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(90, 36)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")

            view_all_blocks = {
                "v": 1,
                "type": "view",
                "id": stub.next_server_id(),
                "screen": "result",
                "node": "apps.scanner.scan",
                "title": "All Blocks Demo",
                "page": {"index": 0, "count": 2, "more": True},
                "blocks": [
                    {
                        "type": "text",
                        "style": "heading",
                        "text": "Heading Block Text",
                    },
                    {
                        "type": "kv",
                        "text": "eth0: 192.0.2.10\nPSK: supersecret",
                        "items": [
                            {"key": "eth0", "value": "192.0.2.10"},
                            {"key": "PSK", "value": "supersecret", "secret": True},
                        ],
                    },
                    {
                        "type": "table",
                        "text": "SSID  CH  RSSI\nCorp  36  -51",
                        "columns": [
                            {"key": "ssid", "label": "SSID", "priority": 0},
                            {"key": "ch", "label": "CH", "align": "right", "priority": 1},
                            {"key": "rssi", "label": "RSSI", "align": "right", "priority": 0},
                        ],
                        "rows": [
                            ["CorpNet", 36, -51],
                            ["GuestWiFi", 6, -63],
                        ],
                    },
                    {
                        "type": "list",
                        "selected": 1,
                        "text": "  Item A\n> Item B",
                        "items": [
                            {"label": "Item A", "detail": "first"},
                            {"label": "Item B", "detail": "second", "branch": True},
                        ],
                    },
                    {
                        "type": "qr",
                        "data": "WIFI:S:WLAN Pi;T:WPA;P:example;;",
                        "caption": "Join profiler",
                        "text": "SSID: WLAN Pi",
                    },
                    {
                        "type": "status",
                        "level": "ok",
                        "label": "Service",
                        "text": "Profiler active",
                    },
                    {
                        "type": "progress",
                        "pct": 65,
                        "label": "Capturing",
                        "text": "Capturing packets 65%",
                    },
                    {
                        "type": "confirm",
                        "prompt_id": "p91c5",
                        "message": "Stop the profiler?",
                        "confirm_label": "Stop",
                        "cancel_label": "Keep",
                        "selected": "cancel",
                        "text": "Stop the profiler? [Stop] [Keep]",
                    },
                ],
            }
            await stub.send_frame(view_all_blocks)
            await _wait_until(lambda: "All Blocks Demo" in _widget_text(app, "#screen-view"))
            await pilot.pause()

            screen_txt = _widget_text(app, "#screen-view")
            assert "Heading Block Text" in screen_txt
            assert "192.0.2.10" in screen_txt
            assert "••••••••" in screen_txt
            assert "supersecret" not in screen_txt
            assert "CorpNet" in screen_txt
            assert "GuestWiFi" in screen_txt
            assert "> Item B ▸" in screen_txt
            assert "Join profiler" in screen_txt
            assert "SSID: WLAN Pi" in screen_txt
            assert any(ch in screen_txt for ch in ("█", "▀", "▄"))
            assert "[OK]" in screen_txt
            assert "Profiler active" in screen_txt
            assert "65%" in screen_txt
            assert "Stop the profiler?" in screen_txt
            assert "[Stop]" in screen_txt

            # Test unknown block type fallback (bypassing schema validation on stub send)
            unknown_view = {
                "v": 1,
                "type": "view",
                "id": stub.next_server_id(),
                "screen": "result",
                "title": "Unknown Block View",
                "blocks": [
                    {
                        "type": "future_chart",
                        "text": "Plain text fallback for future_chart: 42 Mbps",
                    }
                ],
            }
            await stub.send_frame(unknown_view, validate=False)
            await _wait_until(
                lambda: "Plain text fallback for future_chart: 42 Mbps"
                in _widget_text(app, "#screen-view")
            )

    # Also verify capability-based fallback on schema-valid block when only 'text' is supported
    kv_block = {
        "type": "kv",
        "text": "Fallback KV plain line",
        "items": [{"key": "eth0", "value": "10.0.0.1"}],
    }
    assert render_block(kv_block, supported_blocks=["text"]) == "Fallback KV plain line"


async def test_keyboard_to_input_mapping(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(80, 24)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            await pilot.pause()

            key_sequence = [
                ("up", "up"),
                ("down", "down"),
                ("left", "left"),
                ("right", "right"),
                ("enter", "center"),
                ("f1", "key1"),
                ("f2", "key2"),
                ("f3", "key3"),
            ]
            for pressed_key, expected_protocol_key in key_sequence:
                start_idx = len(stub.received_frames)
                await pilot.press(pressed_key)
                frame = await stub.wait_for_frame(
                    lambda f, k=expected_protocol_key: f["type"] == "input" and f["key"] == k,
                    start_index=start_idx,
                )
                validate_frame(frame, direction="c2s")
                assert frame["key"] == expected_protocol_key


async def test_prompt_flows_select_confirm_and_form(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05, session="new")
        async with app.run_test(size=(80, 24)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")

            # 1. Selection prompt (select second option with down + enter)
            select_prompt = {
                "v": 1,
                "type": "prompt",
                "id": stub.next_server_id(),
                "prompt_id": "p91c2",
                "job_id": "j5521",
                "node": "apps.scanner.scan",
                "kind": "select",
                "title": "Scan adapter",
                "message": "More than one adapter can scan. Pick one.",
                "options": [
                    {
                        "value": {"iface": "wlan0", "namespace": "ns_wlan0"},
                        "label": "wlan0",
                        "detail": "ns_wlan0, monitor",
                    },
                    {
                        "value": {"iface": "wlan1", "namespace": None},
                        "label": "wlan1",
                        "detail": "root, monitor",
                    },
                ],
                "expires_sec": 120,
            }
            await stub.send_frame(select_prompt)
            await _wait_until(lambda: "Scan adapter" in _widget_text(app, "#screen-view"))

            start_idx = len(stub.received_frames)
            await pilot.press("down", "enter")
            ans_select = await stub.wait_for_frame(
                lambda f: f["type"] == "answer" and f["prompt_id"] == "p91c2",
                start_index=start_idx,
            )
            assert ans_select["value"] == {"iface": "wlan1", "namespace": None}

            # 2. Confirmation prompt
            confirm_prompt = {
                "v": 1,
                "type": "prompt",
                "id": stub.next_server_id(),
                "prompt_id": "p91c3",
                "node": "system.reboot",
                "kind": "confirm",
                "title": "Reboot",
                "message": "Reboot the WLAN Pi now?",
                "confirm_label": "Reboot",
                "cancel_label": "Cancel",
            }
            await stub.send_frame(confirm_prompt)
            await _wait_until(lambda: "Reboot the WLAN Pi now?" in _widget_text(app, "#screen-view"))

            # m1 regression: destructive confirm defaults to Cancel, so a bare
            # Enter must NOT confirm.
            assert app.prompt_confirm_choice == "cancel"
            start_idx = len(stub.received_frames)
            await pilot.press("enter")
            ans_confirm = await stub.wait_for_frame(
                lambda f: f["type"] == "answer" and f["prompt_id"] == "p91c3",
                start_index=start_idx,
            )
            assert ans_confirm["confirmed"] is False

            # Explicitly toggling to Confirm then Enter confirms.
            await stub.send_frame({**confirm_prompt, "id": stub.next_server_id(), "prompt_id": "p91c3b"})
            await _wait_until(lambda: app.active_prompt is not None and app.active_prompt["prompt_id"] == "p91c3b")
            start_idx = len(stub.received_frames)
            await pilot.press("right", "enter")
            ans_confirm = await stub.wait_for_frame(
                lambda f: f["type"] == "answer" and f["prompt_id"] == "p91c3b",
                start_index=start_idx,
            )
            assert ans_confirm["confirmed"] is True

            # 3. Parameter form prompt (enum + integer)
            form_prompt = {
                "v": 1,
                "type": "prompt",
                "id": stub.next_server_id(),
                "prompt_id": "p91c4",
                "node": "system.reg_domain.set",
                "kind": "form",
                "title": "Set Domain",
                "fields": [
                    {
                        "name": "country",
                        "label": "Country",
                        "type": "enum",
                        "choices": [
                            {"value": "US", "label": "United States"},
                            {"value": "GB", "label": "United Kingdom"},
                        ],
                    },
                    {
                        "name": "channel",
                        "label": "Channel",
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 165,
                        "default": 36,
                    },
                ],
            }
            await stub.send_frame(form_prompt)
            await _wait_until(lambda: "Set Domain" in _widget_text(app, "#screen-view"))

            start_idx = len(stub.received_frames)
            # Cycle enum from US -> GB with right, move down to channel, increment 36 -> 37 with right, submit
            await pilot.press("right", "down", "right", "enter")
            ans_form = await stub.wait_for_frame(
                lambda f: f["type"] == "answer" and f["prompt_id"] == "p91c4",
                start_index=start_idx,
            )
            assert ans_form["values"] == {"country": "GB", "channel": 37}


async def test_job_lifecycle_and_up_down_paging(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        # Register 4 follow-up pages (pages 2..5) to verify paging + 4-page cache cap
        for pnum in range(2, 6):
            prev_cursor = f"cursor_page_{pnum}"
            next_cursor = f"cursor_page_{pnum + 1}" if pnum < 5 else None
            stub.page_handlers[("j5521", prev_cursor)] = {
                "v": 1,
                "type": "job",
                "id": "s:page",
                "job_id": "j5521",
                "node": "apps.scanner.scan",
                "state": "result",
                "result": {
                    "title": "Wi-Fi Scan",
                    "total": 80,
                    "cursor": next_cursor,
                    "blocks": [
                        {
                            "type": "text",
                            "text": f"Scan Results Page {pnum}",
                        }
                    ],
                },
            }

        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(80, 24)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")

            # 1. Queued
            await stub.send_frame(
                {
                    "v": 1,
                    "type": "job",
                    "id": stub.next_server_id(),
                    "job_id": "j5521",
                    "node": "apps.scanner.scan",
                    "state": "queued",
                    "shared": False,
                }
            )
            await _wait_until(lambda: "[QUEUED]" in _widget_text(app, "#screen-view"))

            # 2. Progress
            await stub.send_frame(
                {
                    "v": 1,
                    "type": "job",
                    "id": stub.next_server_id(),
                    "job_id": "j5521",
                    "node": "apps.scanner.scan",
                    "state": "progress",
                    "progress": {"pct": 50, "message": "Scanning 5 GHz"},
                }
            )
            await _wait_until(lambda: "Scanning 5 GHz" in _widget_text(app, "#screen-view"))

            # 3. Result Page 1
            await stub.send_frame(
                {
                    "v": 1,
                    "type": "job",
                    "id": stub.next_server_id(),
                    "job_id": "j5521",
                    "node": "apps.scanner.scan",
                    "state": "result",
                    "result": {
                        "title": "Wi-Fi Scan",
                        "total": 80,
                        "cursor": "cursor_page_2",
                        "blocks": [
                            {"type": "text", "text": "Scan Results Page 1"},
                        ],
                    },
                }
            )
            await _wait_until(lambda: "Scan Results Page 1" in _widget_text(app, "#screen-view"))

            # Press down -> sends page frame for cursor_page_2 -> renders Page 2
            start_idx = len(stub.received_frames)
            await pilot.press("down")
            page_req = await stub.wait_for_frame(
                lambda f: f["type"] == "page" and f["cursor"] == "cursor_page_2",
                start_index=start_idx,
            )
            assert page_req["job_id"] == "j5521"
            await _wait_until(lambda: "Scan Results Page 2" in _widget_text(app, "#screen-view"))

            # Press up -> steps back to cached Page 1 locally
            await pilot.press("up")
            await _wait_until(lambda: "Scan Results Page 1" in _widget_text(app, "#screen-view"))

            # Step down through Page 2 (cached), then fetch Page 3, Page 4, Page 5
            await pilot.press("down")
            await _wait_until(lambda: "Scan Results Page 2" in _widget_text(app, "#screen-view"))
            await pilot.press("down")
            await _wait_until(lambda: "Scan Results Page 3" in _widget_text(app, "#screen-view"))
            await pilot.press("down")
            await _wait_until(lambda: "Scan Results Page 4" in _widget_text(app, "#screen-view"))
            await pilot.press("down")
            await _wait_until(lambda: "Scan Results Page 5" in _widget_text(app, "#screen-view"))

            # Verify cached pages are bounded to MAX_CACHED_RESULT_PAGES (4)
            job_state = app.jobs["j5521"]
            assert len(job_state.pages) == 4
            assert [p.page_number for p in job_state.pages] == [2, 3, 4, 5]


async def test_reconnect_resume_and_full_resnapshot(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock, epoch="epoch-first-1111") as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(80, 24)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            await _wait_until(lambda: app.epoch == "epoch-first-1111")
            await _wait_until(lambda: app.topic_seq.get("device.summary") == 1)

            # Test sequence gap detection -> marks topic stale until snapshot
            await stub.send_frame(
                {
                    "v": 1,
                    "type": "event",
                    "id": stub.next_server_id(),
                    "topic": "device.summary",
                    "seq": 5,
                    "snapshot": False,
                    "data": {"hostname": "wlanpi-gap", "mode": "classic"},
                }
            )
            await _wait_until(lambda: "STALE: device.summary" in _widget_text(app, "#status-footer"))
            stub.topic_seq["device.summary"] = 5

            # 1. Disconnect & reconnect with SAME epoch and matching seq -> welcome.resumed = True
            start_idx = len(stub.received_frames)
            await stub.disconnect_client(code=1001, reason="Transient drop")
            hello_resume_1 = await stub.wait_for_frame(
                lambda f: f["type"] == "hello" and "resume" in f,
                start_index=start_idx,
            )
            assert hello_resume_1["resume"]["epoch"] == "epoch-first-1111"
            assert hello_resume_1["resume"]["session_id"] == "s-stub-01"
            assert hello_resume_1["resume"]["seq"]["device.summary"] == 5
            await _wait_until(lambda: app.resumed is True)

            # 2. Server restarts with a NEW epoch -> mismatch triggers full resnapshot (resumed=False)
            stub.epoch = "epoch-second-2222"
            stub.menu_revision = 9
            stub.topic_seq["device.summary"] = 1
            stub.topic_snapshots["device.summary"] = {
                "hostname": "wlanpi-resynced",
                "mode": "wiperf",
                "ip": "198.51.100.7",
                "cpu_temp": "51.0C",
            }
            start_idx = len(stub.received_frames)
            await stub.disconnect_client(code=1001, reason="Service restarted")
            hello_resume_2 = await stub.wait_for_frame(
                lambda f: f["type"] == "hello" and "resume" in f,
                start_index=start_idx,
            )
            assert hello_resume_2["resume"]["epoch"] == "epoch-first-1111"

            await _wait_until(lambda: app.epoch == "epoch-second-2222")
            await _wait_until(lambda: "wlanpi-resynced" in _widget_text(app, "#status-footer"))
            await pilot.pause()

            assert app.resumed is False
            assert app.menu_tree is not None and app.menu_tree.revision == 9
            assert "device.summary" not in app.stale_topics
            assert "198.51.100.7" in _widget_text(app, "#status-footer")


async def test_readable_error_frames_version_auth_and_conflict(tmp_path: Path) -> None:
    # 1. Fatal VERSION error on handshake
    sock_ver = tmp_path / "ver.sock"
    async with StubMenudServer(sock_ver) as stub_ver:
        stub_ver.handshake_error = {
            "v": 1,
            "type": "error",
            "id": "c1",
            "code": "VERSION",
            "message": "No common protocol version",
            "fatal": True,
            "close_code": 4426,
            "supported": [1],
        }
        app_ver = MenuTuiApp(
            socket_path=str(sock_ver),
            reconnect_delay=0.05,
            protocol_versions=[99],
        )
        async with app_ver.run_test(size=(90, 24)) as pilot:
            await _wait_until(lambda: app_ver.fatal_error is True)
            await pilot.pause()
            banner = _widget_text(app_ver, "#error-banner")
            assert "FATAL ERROR: VERSION" in banner
            assert "No common protocol version" in banner
            assert "[1]" in banner
            assert "4426" in banner

    # 2. Fatal AUTH_FAILED error
    sock_auth = tmp_path / "auth.sock"
    async with StubMenudServer(sock_auth) as stub_auth:
        stub_auth.handshake_error = {
            "v": 1,
            "type": "error",
            "id": "c1",
            "code": "AUTH_FAILED",
            "message": "User not in group wlanpi-menud",
            "fatal": True,
            "close_code": 4401,
        }
        app_auth = MenuTuiApp(socket_path=str(sock_auth), reconnect_delay=0.05)
        async with app_auth.run_test(size=(90, 24)) as pilot:
            await _wait_until(lambda: app_auth.fatal_error is True)
            await pilot.pause()
            banner = _widget_text(app_auth, "#error-banner")
            assert "FATAL ERROR: AUTH_FAILED" in banner
            assert "User not in group wlanpi-menud" in banner
            assert "4401" in banner

    # 3. Non-fatal CONFLICT and FORBIDDEN errors mid-session
    sock_mid = tmp_path / "mid.sock"
    async with StubMenudServer(sock_mid) as stub_mid:
        app_mid = MenuTuiApp(socket_path=str(sock_mid), reconnect_delay=0.05)
        async with app_mid.run_test(size=(90, 24)) as pilot:
            await stub_mid.wait_for_frame(lambda f: f["type"] == "subscribe")
            await stub_mid.send_frame(
                {
                    "v": 1,
                    "type": "error",
                    "id": "c15",
                    "code": "CONFLICT",
                    "message": "Scan already in progress",
                    "core_status": 409,
                    "retry_after_sec": 5,
                }
            )
            await _wait_until(
                lambda: "Scan already in progress" in _widget_text(app_mid, "#error-banner")
            )
            banner = _widget_text(app_mid, "#error-banner")
            assert "ERROR: CONFLICT" in banner
            assert "409" in banner
            assert "5s" in banner
            assert app_mid.fatal_error is False


async def test_integer_string_prompts_and_job_cancel(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05, session="new")
        async with app.run_test(size=(80, 24)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")

            # Integer prompt
            await stub.send_frame(
                {
                    "v": 1,
                    "type": "prompt",
                    "id": stub.next_server_id(),
                    "prompt_id": "p_int1",
                    "kind": "integer",
                    "title": "Capture Duration",
                    "minimum": 5,
                    "maximum": 60,
                    "default": 10,
                }
            )
            await _wait_until(lambda: "Capture Duration" in _widget_text(app, "#screen-view"))
            start_idx = len(stub.received_frames)
            await pilot.press("up", "up", "enter")
            ans_int = await stub.wait_for_frame(
                lambda f: f["type"] == "answer" and f["prompt_id"] == "p_int1",
                start_index=start_idx,
            )
            assert ans_int["value"] == 12

            # String prompt
            await stub.send_frame(
                {
                    "v": 1,
                    "type": "prompt",
                    "id": stub.next_server_id(),
                    "prompt_id": "p_str1",
                    "kind": "string",
                    "title": "Custom Filter",
                    "default": "",
                }
            )
            await _wait_until(lambda: "Custom Filter" in _widget_text(app, "#screen-view"))
            start_idx = len(stub.received_frames)
            await pilot.press("t", "c", "p", "enter")
            ans_str = await stub.wait_for_frame(
                lambda f: f["type"] == "answer" and f["prompt_id"] == "p_str1",
                start_index=start_idx,
            )
            assert ans_str["value"] == "tcp"

            # Job progress -> cancel with 'c'
            await stub.send_frame(
                {
                    "v": 1,
                    "type": "job",
                    "id": stub.next_server_id(),
                    "job_id": "j9001",
                    "node": "utils.speedtest.run",
                    "state": "progress",
                    "progress": {"pct": None, "message": "Testing download"},
                }
            )
            await _wait_until(lambda: "Testing download" in _widget_text(app, "#screen-view"))
            start_idx = len(stub.received_frames)
            await pilot.press("c")
            cancel_frame = await stub.wait_for_frame(
                lambda f: f["type"] == "cancel" and f["job_id"] == "j9001",
                start_index=start_idx,
            )
            validate_frame(cancel_frame, direction="c2s")

            await stub.send_frame(
                {
                    "v": 1,
                    "type": "job",
                    "id": cancel_frame["id"],
                    "job_id": "j9001",
                    "node": "utils.speedtest.run",
                    "state": "cancelled",
                }
            )
            await _wait_until(lambda: "Job Cancelled" in _widget_text(app, "#screen-view"))


async def test_recorded_pilot_dump_menu_to_result_to_confirm(tmp_path: Path) -> None:
    """End-to-end pilot scenario capturing screen dumps across menu -> result -> confirm."""
    sock = tmp_path / "local.sock"

    async def _scripted_flow(server: StubMenudServer, frame: dict[str, Any]) -> None:
        if frame["type"] == "input" and frame["key"] == "down":
            # Move cursor to System
            await server.send_frame(
                {
                    "v": 1,
                    "type": "view",
                    "id": frame["id"],
                    "screen": "menu",
                    "node": "system",
                    "title": "Main Menu",
                    "breadcrumb": ["Main Menu"],
                    "blocks": [
                        {
                            "type": "list",
                            "selected": 2,
                            "text": "  Network\n  Apps\n> System",
                            "items": [
                                {"label": "Network", "node": "network", "branch": True},
                                {"label": "Apps", "node": "apps", "branch": True},
                                {"label": "System", "node": "system", "branch": True},
                            ],
                        }
                    ],
                    "hints": {"left": "Back", "center": "Open"},
                }
            )
        elif frame["type"] == "input" and frame["key"] == "center":
            # Step 2: Push a result view/job showing interface & profiler status
            await server.send_frame(
                {
                    "v": 1,
                    "type": "job",
                    "id": frame["id"],
                    "job_id": "j7001",
                    "node": "network.interfaces",
                    "state": "result",
                    "result": {
                        "title": "Interface Summary",
                        "cursor": None,
                        "blocks": [
                            {
                                "type": "kv",
                                "text": "eth0: 192.0.2.10\nMode: Classic",
                                "items": [
                                    {"key": "eth0", "value": "192.0.2.10"},
                                    {"key": "Mode", "value": "Classic"},
                                ],
                            },
                            {
                                "type": "status",
                                "level": "ok",
                                "label": "Link",
                                "text": "1000Mb/s Full Duplex",
                            },
                        ],
                    },
                }
            )
        elif frame["type"] == "input" and frame["key"] == "key1":
            # Step 3: Trigger confirmation prompt for system.reboot
            await server.send_frame(
                {
                    "v": 1,
                    "type": "prompt",
                    "id": frame["id"],
                    "prompt_id": "p7002",
                    "node": "system.reboot",
                    "kind": "confirm",
                    "title": "Reboot",
                    "message": "Reboot the WLAN Pi now?",
                    "confirm_label": "Reboot",
                    "cancel_label": "Cancel",
                }
            )
        elif frame["type"] == "answer" and frame["prompt_id"] == "p7002":
            await server.send_frame(
                {
                    "v": 1,
                    "type": "view",
                    "id": frame["id"],
                    "screen": "result",
                    "node": "system.reboot",
                    "title": "Reboot Confirmed",
                    "blocks": [
                        {
                            "type": "status",
                            "level": "warn",
                            "label": "System",
                            "text": "Rebooting WLAN Pi now...",
                        }
                    ],
                }
            )

    async with StubMenudServer(sock) as stub:
        stub.on_client_frame = _scripted_flow
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05, session="new")
        async with app.run_test(size=(84, 26)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            await _wait_until(lambda: "Main Menu" in _widget_text(app, "#screen-view"))

            # Navigate down to System on menu screen
            await pilot.press("down")
            await _wait_until(lambda: "> System ▸" in _widget_text(app, "#screen-view"))
            menu_dump = (
                f"HEADER: {_widget_text(app, '#header-bar')}\n"
                f"SCREEN:\n{_widget_text(app, '#screen-view')}\n"
                f"FOOTER:\n{_widget_text(app, '#status-footer')}"
            )
            assert "Main Menu" in menu_dump
            assert "> System ▸" in menu_dump

            # Open result screen
            await pilot.press("enter")
            await _wait_until(lambda: "Interface Summary" in _widget_text(app, "#screen-view"))
            result_dump = (
                f"HEADER: {_widget_text(app, '#header-bar')}\n"
                f"SCREEN:\n{_widget_text(app, '#screen-view')}\n"
                f"FOOTER:\n{_widget_text(app, '#status-footer')}"
            )
            assert "Interface Summary" in result_dump
            assert "1000Mb/s Full Duplex" in result_dump

            # Open confirm prompt via key1 shortcut
            await pilot.press("f1")
            await _wait_until(
                lambda: "Reboot the WLAN Pi now?" in _widget_text(app, "#screen-view")
            )
            confirm_dump = (
                f"HEADER: {_widget_text(app, '#header-bar')}\n"
                f"SCREEN:\n{_widget_text(app, '#screen-view')}\n"
                f"FOOTER:\n{_widget_text(app, '#status-footer')}"
            )
            assert "Prompt: Reboot" in confirm_dump
            assert "Reboot the WLAN Pi now?" in confirm_dump
            assert "[Reboot]" in confirm_dump

            # Cancel is the default; toggle to Reboot, then confirm with enter
            start_idx = len(stub.received_frames)
            await pilot.press("right", "enter")
            ans = await stub.wait_for_frame(
                lambda f: f["type"] == "answer" and f["prompt_id"] == "p7002", start_index=start_idx
            )
            assert ans["confirmed"] is True
            await _wait_until(
                lambda: "Rebooting WLAN Pi now..." in _widget_text(app, "#screen-view")
            )
            post_confirm_dump = _widget_text(app, "#screen-view")
            assert "Reboot Confirmed" in post_confirm_dump
            assert "Rebooting WLAN Pi now..." in post_confirm_dump


# ----------------------------------------------------------------------
# Regression tests for phase1-review findings M1, M2, m1 and the 32-row
# layout bug found on-device. These mirror the real wlanpi-menud frame
# sequences rather than the simplified single-frame stub replies.
# ----------------------------------------------------------------------


def _busy_view(stub: StubMenudServer, job_id: str, node: str, text: str) -> dict[str, Any]:
    return {
        "v": 1,
        "type": "view",
        "id": stub.next_server_id(),
        "screen": "busy",
        "node": node,
        "title": "Speedtest",
        "job_id": job_id,
        "blocks": [{"type": "progress", "pct": None, "label": text, "text": text}],
        "hints": {"left": "Back"},
    }


def _job(stub: StubMenudServer, job_id: str, node: str, state: str, **extra: Any) -> dict[str, Any]:
    return {"v": 1, "type": "job", "id": stub.next_server_id(), "job_id": job_id, "node": node,
            "state": state, **extra}


async def test_m1_dual_job_view_emission_keeps_cancel_reachable(tmp_path: Path) -> None:
    """M1: job + view for the same transition must not disable c/Escape cancel."""
    sock = tmp_path / "local.sock"
    node = "utils.speedtest.run"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(80, 24)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")

            for job_id, key in (("jcancel-c", "c"), ("jcancel-esc", "escape")):
                await stub.send_job_transition(
                    _job(stub, job_id, node, "queued", shared=False),
                    _busy_view(stub, job_id, node, "Starting…"),
                )
                await stub.send_job_transition(
                    _job(stub, job_id, node, "progress", progress={"pct": None, "message": "Testing download"}),
                    _busy_view(stub, job_id, node, "Testing download"),
                )
                await _wait_until(
                    lambda jid=job_id: app.jobs.get(jid) is not None and app.jobs[jid].state == "progress"
                    and "Testing download" in _widget_text(app, "#screen-view")
                )
                # The trailing view frame wins the screen, but the job stays
                # active and cancellable.
                assert app.display_mode == "view"
                assert app.active_job_id == job_id

                start_idx = len(stub.received_frames)
                await pilot.press(key)
                cancel = await stub.wait_for_frame(
                    lambda f: f["type"] == "cancel", start_index=start_idx
                )
                assert cancel["job_id"] == job_id
                # No thin input should have been sent for c/Escape
                assert not any(f["type"] == "input" for f in stub.received_frames[start_idx:])

                # Server: job cancelled + cursor back to the menu view
                await stub.send_job_transition(
                    _job(stub, job_id, node, "cancelled"),
                    {"v": 1, "type": "view", "id": stub.next_server_id(), "screen": "menu", "node": "utils",
                     "title": "Utils", "blocks": [{"type": "list", "selected": 0, "text": "> Speedtest",
                                                    "items": [{"label": "Speedtest", "node": node}]}]},
                )
                await _wait_until(lambda: app.current_view is not None and app.current_view["screen"] == "menu")

            # On a plain menu view (no live job) c/Escape send nothing
            start_idx = len(stub.received_frames)
            await pilot.press("c", "escape")
            await pilot.pause(0.1)
            assert not any(f["type"] == "cancel" for f in stub.received_frames[start_idx:])


async def test_m1_dual_job_view_result_pages_via_thin_input_only(tmp_path: Path) -> None:
    """M1: a view-driven result pages through thin input, never page frames."""
    sock = tmp_path / "local.sock"
    node = "apps.scanner.scan"
    job_id = "j-scan"

    def result_view(idx: int, frame_id: str) -> dict[str, Any]:
        return {
            "v": 1, "type": "view", "id": frame_id, "screen": "result", "node": node, "title": "Wi-Fi Scan",
            "job_id": job_id, "page": {"index": idx, "count": 3, "more": idx < 2},
            "blocks": [{"type": "text", "text": f"Live Scan Page {idx + 1}"}],
        }

    page = {"idx": 0}

    async def on_input(server: StubMenudServer, frame: dict[str, Any]) -> None:
        if frame["type"] == "input" and frame["key"] in ("up", "down"):
            page["idx"] = max(0, min(2, page["idx"] + (1 if frame["key"] == "down" else -1)))
            await server.send_frame(result_view(page["idx"], frame["id"]))

    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(80, 24)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            stub.on_client_frame = on_input

            await stub.send_job_transition(
                _job(stub, job_id, node, "queued", shared=False), _busy_view(stub, job_id, node, "Starting…")
            )
            await stub.send_job_transition(
                _job(stub, job_id, node, "result",
                     result={"title": "Wi-Fi Scan", "total": 45, "cursor": "cur-2",
                             "blocks": [{"type": "text", "text": "Live Scan Page 1"}]}),
                result_view(0, stub.next_server_id()),
            )
            await _wait_until(lambda: "Live Scan Page 1" in _widget_text(app, "#screen-view"))
            assert app.display_mode == "view"
            assert "(page 1/3+)" in _widget_text(app, "#screen-view")

            start_idx = len(stub.received_frames)
            await pilot.press("down")
            await _wait_until(lambda: "Live Scan Page 2" in _widget_text(app, "#screen-view"))
            await pilot.press("down")
            await _wait_until(lambda: "Live Scan Page 3" in _widget_text(app, "#screen-view"))
            await pilot.press("up")
            await _wait_until(lambda: "Live Scan Page 2" in _widget_text(app, "#screen-view"))

            sent = stub.received_frames[start_idx:]
            assert [f["key"] for f in sent if f["type"] == "input"] == ["down", "down", "up"]
            assert not any(f["type"] == "page" for f in sent), "page frames would desync server cursor.page"
            # Late job frame for the same job must not steal the screen back
            await stub.send_frame(_job(stub, job_id, node, "result",
                                       result={"title": "Wi-Fi Scan", "cursor": None,
                                               "blocks": [{"type": "text", "text": "stale job page"}]}))
            await pilot.pause(0.1)
            assert app.display_mode == "view"
            assert "Live Scan Page 2" in _widget_text(app, "#screen-view")


async def test_m2_real_complications_payload_renders_in_footer(tmp_path: Path) -> None:
    """M2: wlanpi-menud complications items {app_id,label,value,status} render."""
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(100, 24)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            await _wait_until(lambda: "Uplink: OK" in _widget_text(app, "#status-footer"))

            await stub.send_frame({
                "v": 1, "type": "event", "id": stub.next_server_id(), "topic": "complications", "seq": 2,
                "data": [
                    {"app_id": "gps", "label": "GPS", "value": "8 sats", "status": "ok"},
                    {"app_id": "kismet", "label": "Kismet", "value": "off", "status": "unknown"},
                ],
            })
            await _wait_until(lambda: "GPS: 8 sats" in _widget_text(app, "#status-footer"))
            await pilot.pause()
            footer = _widget_text(app, "#status-footer")
            assert "Kismet: off" in footer
            assert "Uplink" not in footer  # list replaced, not merged


def _tall_menu_nodes() -> list[dict[str, Any]]:
    """A menu comparable to wlanpi-menud's real tree (~70 nodes)."""
    nodes: list[dict[str, Any]] = []
    for b in range(6):
        bid = f"b{b}"
        nodes.append({"id": bid, "label": f"Branch {b}", "parent": None, "order": b, "kind": "branch"})
        for a in range(11):
            nodes.append({"id": f"{bid}.a{a}", "label": f"Action {b}.{a}", "parent": bid, "order": a,
                          "kind": "action", "view": "kv", "mutates": False})
    return nodes


async def test_layout_80x32_result_visible_after_invoke_with_tall_menu(tmp_path: Path) -> None:
    """On-device bug: at 32 rows arrow-key scrolling of the tree pane hid the result."""
    sock = tmp_path / "local.sock"
    node = "b5.a10"

    def menu_view(frame_id: str, selected: int) -> dict[str, Any]:
        items = [{"label": f"Action 5.{i}", "node": f"b5.a{i}"} for i in range(11)]
        return {"v": 1, "type": "view", "id": frame_id, "screen": "menu", "node": items[selected]["node"],
                "title": "Branch 5", "blocks": [{"type": "list", "selected": selected,
                                                 "text": "\n".join(i["label"] for i in items), "items": items}]}

    sel = {"i": 0}

    async def flow(server: StubMenudServer, frame: dict[str, Any]) -> None:
        if frame["type"] != "input":
            return
        if frame["key"] == "down":
            sel["i"] = min(10, sel["i"] + 1)
            await server.send_frame(menu_view(frame["id"], sel["i"]))
        elif frame["key"] == "center":
            await server.send_job_transition(
                _job(server, "j-lay", node, "queued", shared=False), _busy_view(server, "j-lay", node, "Starting…")
            )
            await server.send_job_transition(
                _job(server, "j-lay", node, "result",
                     result={"title": "Layout Result", "blocks": [{"type": "text", "text": "RESULT-VISIBLE"}]}),
                {"v": 1, "type": "view", "id": server.next_server_id(), "screen": "result", "node": node,
                 "title": "Layout Result", "job_id": "j-lay",
                 "blocks": [{"type": "text", "text": "RESULT-VISIBLE"}]},
            )

    async with StubMenudServer(sock) as stub:
        stub.menu_nodes = _tall_menu_nodes()
        stub.on_client_frame = flow
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(80, 32)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            await _wait_until(lambda: app.menu_tree is not None)
            scroll = app.query_one("#main-scroll")
            await pilot.pause()
            assert scroll.max_scroll_y > 0, "menu tree must overflow the pane for this test to be meaningful"

            await pilot.press(*(["down"] * 10))
            await _wait_until(lambda: app.current_view is not None and app.current_view["node"] == node)
            await pilot.press("enter")
            await _wait_until(lambda: "RESULT-VISIBLE" in _widget_text(app, "#screen-view"))
            await pilot.pause()

            screen_view = app.query_one("#screen-view")
            assert scroll.scroll_y == 0
            visible = scroll.content_region
            assert screen_view.region.y >= visible.y
            assert screen_view.region.y < visible.bottom
            # The result text is actually painted on the 80x32 screen
            painted = app.export_screenshot()
            assert "RESULT-VISIBLE" in painted
