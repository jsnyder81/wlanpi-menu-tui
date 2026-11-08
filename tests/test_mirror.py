"""Default-session (mirror) behaviour of the TUI against the schema-validated stub.

On the default session the TUI renders whatever view the service pushes,
whoever drove it, shows "driven by …" when it was another surface, answers
select/confirm/integer prompts on the shared screen with input frames, and
leaves a confirmation it may not answer alone (c/Escape included).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.stub_server import StubMenudServer
from tests.test_tui import _wait_until, _widget_text
from wlanpi_menu_tui.app import MenuTuiApp
from wlanpi_menu_tui.cli import build_parser
from wlanpi_menu_tui.protocol import build_hello

ALICE = {"kind": "webui", "name": "alice", "did": "wlanpi-menud"}
ME = {"kind": "local", "name": "wlanpi", "uid": 1000, "did": "wlanpi-menud"}


def menu_view(stub: StubMenudServer, selected: int, *, driven_by: dict[str, Any] | None = None) -> dict[str, Any]:
    items = [
        {"label": "Network", "node": "network", "branch": True},
        {"label": "Apps", "node": "apps", "branch": True},
        {"label": "System", "node": "system", "branch": True},
    ]
    text = "\n".join(("> " if i == selected else "  ") + it["label"] for i, it in enumerate(items))
    v: dict[str, Any] = {
        "v": 1, "type": "view", "id": stub.next_server_id(), "screen": "menu", "node": items[selected]["node"],
        "title": "Menu", "blocks": [{"type": "list", "selected": selected, "text": text, "items": items}],
        "hints": {"left": "Back", "center": "Select"},
    }
    if driven_by is not None:
        v["driven_by"] = driven_by
    return v


def confirm_view(stub: StubMenudServer, *, answerable: bool, selected: str = "cancel") -> dict[str, Any]:
    return {
        "v": 1, "type": "view", "id": stub.next_server_id(), "screen": "prompt", "node": "system.reboot",
        "title": "Reboot", "job_id": "jRb01", "answerable": answerable,
        "blocks": [{"type": "confirm", "prompt_id": "pRb01", "message": "Reboot the WLAN Pi now?",
                    "confirm_label": "Reboot", "cancel_label": "Cancel", "selected": selected,
                    "text": "Reboot the WLAN Pi now?"}],
        "hints": {"left": "Cancel", "center": "OK"} if answerable else {},
        "driven_by": {"principal": ALICE, "surface": "wlanpi-webui", "self": False},
    }


def confirm_prompt(stub: StubMenudServer, invoker: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": 1, "type": "prompt", "id": stub.next_server_id(), "prompt_id": "pRb01", "job_id": "jRb01",
        "node": "system.reboot", "invoked_by": invoker, "kind": "confirm", "title": "Reboot",
        "message": "Reboot the WLAN Pi now?", "confirm_label": "Reboot", "cancel_label": "Cancel",
    }


def c2s_after(stub: StubMenudServer, n: int, *types: str) -> list[dict[str, Any]]:
    return [f for f in stub.received_frames[n:] if f["type"] in types]


async def test_cli_session_flag_defaults_to_attach() -> None:
    assert build_parser().parse_args([]).session == "default"
    assert build_parser().parse_args(["--session", "new"]).session == "new"
    assert "session" not in build_hello("c1"), "a default attach is a plain thin hello"
    assert build_hello("c1", session="new")["session"] == "new"


async def test_hello_carries_requested_session(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05, session="new")
        async with app.run_test(size=(90, 28)):
            hello = await stub.wait_for_frame(lambda f: f["type"] == "hello")
            assert hello["session"] == "new"
            await _wait_until(lambda: app.session_kind == "new")
            assert not app.attached


async def test_mirror_renders_views_driven_elsewhere(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(100, 30)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            await _wait_until(lambda: app.attached)
            assert "shared" in _widget_text(app, "#header-bar")
            n = len(stub.received_frames)

            # Another surface moves the shared cursor twice; this TUI sends nothing.
            for sel in (1, 2):
                await stub.send_frame(menu_view(stub, sel, driven_by={"principal": ALICE, "surface": "wlanpi-webui",
                                                                      "self": False}))
                await _wait_until(lambda s=sel: app.current_view is not None
                                  and app.current_view["blocks"][0]["selected"] == s)
                await pilot.pause()
                screen = _widget_text(app, "#screen-view")
                assert ("> System" if sel == 2 else "> Apps") in screen
            assert "driven by wlanpi-webui (webui:alice)" in _widget_text(app, "#header-bar")
            assert not c2s_after(stub, n, "input", "answer", "invoke"), "mirroring is passive"

            # When this TUI drives, the hint goes away.
            await stub.send_frame(menu_view(stub, 0, driven_by={"principal": ME, "surface": "wlanpi-menu-tui",
                                                                "self": True}))
            await _wait_until(lambda: "driven by" not in _widget_text(app, "#header-bar"))
            await pilot.press("down")
            inp = await stub.wait_for_frame(lambda f: f["type"] == "input", start_index=n)
            assert inp["key"] == "down"


async def test_confirmation_of_another_principal_is_not_answerable(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(100, 30)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            await _wait_until(lambda: app.attached)
            await stub.send_frame(confirm_prompt(stub, ALICE))
            await stub.send_frame(confirm_view(stub, answerable=False))
            await _wait_until(lambda: app.current_view is not None and app.current_view["screen"] == "prompt")
            await pilot.pause()
            assert app.display_mode == "view" and app.active_prompt is None, "shown on the shared screen"
            assert "Reboot the WLAN Pi now?" in _widget_text(app, "#screen-view")
            n = len(stub.received_frames)
            for key in ("c", "escape", "enter", "y", "left", "down"):
                await pilot.press(key)
            await pilot.pause()
            assert not c2s_after(stub, n, "input", "answer", "cancel"), "c/Escape do not cancel another's confirm"
            assert "Only webui:alice can answer this confirmation" in _widget_text(app, "#status-footer")
            assert app.is_running, "q aside, keys do not quit or leave the screen"

            # The invoker answers elsewhere: the shared screen moves on and the notice clears.
            await stub.send_frame({"v": 1, "type": "view", "id": stub.next_server_id(), "screen": "result",
                                   "node": "system.reboot", "title": "Reboot", "job_id": "jRb01",
                                   "blocks": [{"type": "status", "level": "ok", "text": "Rebooting"}],
                                   "driven_by": {"principal": ALICE, "surface": "wlanpi-webui", "self": False}})
            await _wait_until(lambda: "Rebooting" in _widget_text(app, "#screen-view"))
            assert "Only" not in _widget_text(app, "#status-footer")


async def test_own_confirmation_on_shared_screen_uses_input(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(100, 30)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            await _wait_until(lambda: app.attached)
            await stub.send_frame(confirm_prompt(stub, ME))
            await stub.send_frame(confirm_view(stub, answerable=True))
            await _wait_until(lambda: app.current_view is not None and app.current_view["screen"] == "prompt")
            n = len(stub.received_frames)
            await pilot.press("right")  # toggles the shared highlight (server-side)
            f = await stub.wait_for_frame(lambda f: f["type"] == "input", start_index=n)
            assert f["key"] == "down"
            await pilot.press("enter")
            f = await stub.wait_for_frame(lambda f: f["type"] == "input" and f["key"] == "center", start_index=n)
            await pilot.press("c")  # c/Escape cancel on the shared screen
            f = await stub.wait_for_frame(lambda f: f["type"] == "input" and f["key"] == "left", start_index=n)
            await pilot.press("y")
            f = await stub.wait_for_frame(lambda f: f["type"] == "answer", start_index=n)
            assert f == {"v": 1, "type": "answer", "id": f["id"], "prompt_id": "pRb01", "confirmed": True}


async def test_keyboard_prompt_resolved_elsewhere_closes_local_editor(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(100, 30)) as pilot:
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            await _wait_until(lambda: app.attached)
            await stub.send_frame({"v": 1, "type": "prompt", "id": stub.next_server_id(), "prompt_id": "pHn01",
                                   "job_id": "jHn01", "node": "system.hostname", "invoked_by": ALICE,
                                   "kind": "string", "title": "Hostname"})
            await _wait_until(lambda: app.display_mode == "prompt")
            await pilot.press("a")
            assert app.prompt_scalar_value == "a"
            await stub.send_frame({"v": 1, "type": "view", "id": stub.next_server_id(), "screen": "busy",
                                   "node": "system.hostname", "title": "Hostname", "job_id": "jHn01",
                                   "blocks": [{"type": "progress", "pct": None, "text": "Working…"}],
                                   "driven_by": {"principal": ALICE, "surface": "wlanpi-webui", "self": False}})
            await _wait_until(lambda: app.active_prompt is None and app.display_mode == "view")
            assert "Working" in _widget_text(app, "#screen-view")


async def test_server_without_session_field_keeps_independent_behaviour(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        stub.legacy_welcome = True
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(100, 30)):
            await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            await _wait_until(lambda: app.session_kind == "new")
            await stub.send_frame(confirm_prompt(stub, ME))
            await _wait_until(lambda: app.display_mode == "prompt" and app.active_prompt is not None)
