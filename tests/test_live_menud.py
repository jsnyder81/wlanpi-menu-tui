"""Live integration: three TUIs against the real wlanpi-menud (fake core, tmp sockets).

Two TUIs attach to the default session and mirror and drive one shared
screen; a third (``--session new``) navigates independently. Needs the
sibling ``wlanpi-menud`` checkout with its ``.venv``; skipped otherwise.
Set ``LIVE_DUMP=<path>`` to also write the side-by-side screen dump.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.test_tui import _wait_until, _widget_text
from wlanpi_menu_tui.app import MenuTuiApp

MENUD = Path(__file__).resolve().parents[2] / "wlanpi-menud"
MENUD_PY = MENUD / ".venv" / "bin" / "python"
SERVE = MENUD / "demo" / "serve_fake.py"

pytestmark = pytest.mark.skipif(not (MENUD_PY.exists() and SERVE.exists()),
                                reason="needs the sibling wlanpi-menud checkout and its .venv")

WIDTH = 44


@pytest.fixture
async def menud():
    d = Path(tempfile.mkdtemp(prefix="menud-live-"))  # unix socket paths are limited to 108 bytes
    proc = await asyncio.create_subprocess_exec(str(MENUD_PY), str(SERVE), str(d), cwd=str(MENUD),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    assert proc.stdout is not None
    line = (await asyncio.wait_for(proc.stdout.readline(), 15)).decode().strip()
    assert line.startswith("READY "), line
    yield Path(line.split(" ", 1)[1])
    proc.terminate()
    await proc.wait()
    shutil.rmtree(d, ignore_errors=True)


def column(app: MenuTuiApp) -> list[str]:
    header = _widget_text(app, "#header-bar").split("  │  ")
    head = [p for p in header if p.startswith(("shared", "session:", "driven by"))]
    body = _widget_text(app, "#screen-view").splitlines()
    return [" | ".join(head)[:WIDTH], "-" * WIDTH] + [ln[:WIDTH] for ln in body[:12]]


def side_by_side(title: str, apps: dict[str, MenuTuiApp]) -> str:
    cols = {name: column(app) for name, app in apps.items()}
    height = max(len(c) for c in cols.values())
    out = [f"## {title}", "  ".join(n.ljust(WIDTH) for n in cols)]
    for i in range(height):
        out.append("  ".join((c[i] if i < len(c) else "").ljust(WIDTH) for c in cols.values()))
    return "\n".join(out) + "\n"


def screen(app: MenuTuiApp) -> str:
    return _widget_text(app, "#screen-view")


async def test_two_attached_mirror_and_one_independent(menud: Path) -> None:
    dump: list[str] = []
    a = MenuTuiApp(socket_path=str(menud), reconnect_delay=0.05)
    b = MenuTuiApp(socket_path=str(menud), reconnect_delay=0.05)
    n = MenuTuiApp(socket_path=str(menud), reconnect_delay=0.05, session="new")
    apps = {"TUI-A (attach)": a, "TUI-B (attach)": b, "TUI-N (--session new)": n}
    async with a.run_test(size=(100, 34)) as pa, b.run_test(size=(100, 34)) as pb, \
            n.run_test(size=(100, 34)) as pn:
        await _wait_until(lambda: all(x.current_view is not None for x in (a, b, n)), timeout=10)
        assert a.attached and b.attached and not n.attached
        assert a.session_id == b.session_id != n.session_id

        async def step(title: str, pilot, key: str, until) -> None:
            await pilot.press(key)
            await _wait_until(until, timeout=5)
            for p in (pa, pb, pn):
                await p.pause()
            dump.append(side_by_side(title, apps))

        dump.append(side_by_side("connected", apps))
        await step("A presses down: home -> menu (B mirrors, N untouched)", pa, "down",
                   lambda: b.current_view["screen"] == "menu")
        assert n.current_view["screen"] == "home"
        assert "driven by wlanpi-menu-tui" in _widget_text(b, "#header-bar")
        await step("B presses down: B drives the same cursor", pb, "down",
                   lambda: a.current_view["node"] == "bluetooth")
        await step("A presses enter: into Bluetooth on both", pa, "enter",
                   lambda: b.current_view.get("breadcrumb") == ["Bluetooth"])
        await step("N presses down: its own cursor, A/B untouched", pn, "down",
                   lambda: n.current_view["screen"] == "menu")
        await pn.press("down")
        await _wait_until(lambda: n.current_view["node"] == "bluetooth", timeout=5)
        await pn.press("enter")
        await _wait_until(lambda: n.current_view.get("breadcrumb") == ["Bluetooth"], timeout=5)
        await pn.press("left")
        await pn.press("up")
        await _wait_until(lambda: n.current_view["node"] == "network", timeout=5)
        dump.append(side_by_side("N: down, down, enter, left, up -> back on Network; A/B unchanged", apps))
        assert a.current_view.get("breadcrumb") == ["Bluetooth"] == b.current_view.get("breadcrumb")

        # Run an action from A: both attached TUIs show the result live.
        await pa.press("left")
        await _wait_until(lambda: b.current_view.get("node") == "bluetooth" and not b.current_view.get("breadcrumb"),
                          timeout=5)
        await pa.press("up")
        await _wait_until(lambda: b.current_view["node"] == "network", timeout=5)
        await pa.press("enter")
        await _wait_until(lambda: b.current_view.get("breadcrumb") == ["Network"], timeout=5)
        for _ in range(3):
            await pa.press("down")
        await _wait_until(lambda: b.current_view["node"] == "network.lldp", timeout=5)
        dump.append(side_by_side("A: left, up, enter, down x3 -> Network > LLDP Neighbour", apps))
        await step("A presses enter: LLDP runs, result mirrored on B", pa, "enter",
                   lambda: b.current_view["screen"] == "result" and "sw1" in screen(b))
        assert "sw1" in screen(a) and "sw1" not in screen(n)
        await step("B presses left: back to the menu on both", pb, "left",
                   lambda: a.current_view["screen"] == "menu")
        await step("N presses enter: opens Network on N only", pn, "enter",
                   lambda: n.current_view.get("breadcrumb") == ["Network"])
        assert a.current_view["node"] == "network.lldp" and b.current_view["node"] == "network.lldp"

    text = "\n".join(dump)
    print("\n" + text)
    if os.environ.get("LIVE_DUMP"):
        Path(os.environ["LIVE_DUMP"]).write_text(text)
