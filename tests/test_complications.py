"""Complications spec §2.6, §4, §6.3 for the TUI surface.

Unit tests for resolution, status vocabulary and footer fitting, plus pilot
tests against the schema-validated stub (old service and new service).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.stub_server import PROTOCOL_DIR, StubMenudServer
from tests.test_tui import _wait_until, _widget_text
from wlanpi_menu_tui import complications as C
from wlanpi_menu_tui.app import MenuTuiApp
from wlanpi_menu_tui.blocks import render_block

SAMPLES = PROTOCOL_DIR / "samples"
VECTORS = SAMPLES / "complications" / "resolve-vectors.json"


def _sample(name: str) -> dict[str, Any]:
    return json.loads((SAMPLES / "frames" / "s2c" / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- resolution


@pytest.mark.parametrize("case", json.loads(VECTORS.read_text(encoding="utf-8"))["cases"],
                         ids=lambda c: c["name"])
def test_resolve_vectors(case: dict[str, Any]) -> None:
    """The shared vectors every surface tests against (§4.3)."""
    got = C.resolve(case["show"], case["live"])
    if "capacity" in case:
        got = got[: case["capacity"]]
    assert got == case["expect"]


def test_show_for_falls_back_to_default_then_shipped() -> None:
    assert C.show_for({"surfaces": {"tui": {"show": ["wlanpi.ip"], "source": "user"}}}) == ["wlanpi.ip"]
    assert C.show_for({"surfaces": {"default": {"show": ["wlanpi.mode"], "source": "default"}}}) == ["wlanpi.mode"]
    assert C.show_for(None) == list(C.SHIPPED_TUI_SHOW)


# ---------------------------------------------------------------- status vocabulary


def test_status_vocabulary_markers_and_styles() -> None:
    def seg(status: str) -> str:
        return C.tile_markup({"app_id": "local.x.y", "label": "L", "value": "v", "status": status})

    assert seg("ok") == "[green]✓ L: v[/green]"
    assert seg("info") == "L: v"
    assert seg("warn") == "[yellow]▲ L: v[/yellow]"
    assert seg("error") == "[bold red]✗ L: v[/bold red]"
    assert seg("off") == "[dim]○ L: v[/dim]"
    assert seg("unknown") == "[dim]? L: --[/dim]"   # value drawn as --
    assert seg("critical") == seg("unknown")          # unrecognized -> unknown
    assert seg("warning") == seg("warn")              # deprecated alias


def test_icons_map_to_glyphs_and_unknown_icon_draws_none() -> None:
    assert C.tile_plain({"label": "Wi-Fi", "value": "Home", "status": "info", "icon": "wifi"}) == "≋ Wi-Fi: Home"
    assert C.tile_plain({"label": "X", "value": "1", "status": "info", "icon": "nope"}) == "X: 1"


def test_tile_text_is_escaped() -> None:
    """A value of ``[red]x`` is drawn literally, never as markup (§6.3)."""
    from rich.text import Text

    m = C.tile_markup({"label": "L", "value": "[red]x", "status": "info"})
    assert Text.from_markup(m).plain == "L: [red]x"


def test_kv_level_rendering() -> None:
    out = render_block({"type": "kv", "text": "", "items": [
        {"key": "IP", "value": "192.0.2.10", "level": "ok"},
        {"key": "Temp", "value": "81C", "level": "error"},
        {"key": "Mode", "value": "Classic", "level": "info"},
        {"key": "Host", "value": "x"},
    ]})
    assert "[green]✓ 192.0.2.10[/green]" in out
    assert "[bold red]✗ 81C[/bold red]" in out
    assert "Classic" in out and "✓ Classic" not in out


def test_fits_above_rule_row_dimmed() -> None:
    out = render_block({"type": "list", "text": "", "selected": 0, "items": [
        {"label": "1 Mode"}, {"label": "- - fits above - -"}, {"label": "  CPU", "detail": "hidden"},
    ]})
    assert "[dim]- - fits above - -[/dim]" in out
    assert "[dim](hidden)[/dim]" in out


# ---------------------------------------------------------------- fitting


def _tiles(n: int) -> list[dict[str, Any]]:
    return [{"app_id": f"local.u.t{i}", "label": f"T{i}", "value": "v", "status": "info"} for i in range(n)]


def test_fit_caps_at_eight_and_keeps_order() -> None:
    got = C.fit_tiles(_tiles(12), 500)
    assert got == [f"T{i}: v" for i in range(8)]


def test_fit_cuts_tail_and_ellipsizes_value_never_label() -> None:
    tiles = [
        {"app_id": "wlanpi.mode", "label": "Mode", "value": "Classic", "status": "info"},
        {"app_id": "wlanpi.ip", "label": "IP", "value": "192.0.2.10", "status": "info"},
        {"app_id": "wlanpi.cpu_temp", "label": "Temp", "value": "48C", "status": "info"},
    ]
    # "Mode: Classic" (13) + sep (5) leaves 8: "IP: " (4) + "192…" (4)
    assert C.fit_tiles(tiles, 13 + 5 + 8) == ["Mode: Classic", "IP: 192…"]
    # Too little room for the label plus one value character: dropped, not cut.
    assert C.fit_tiles(tiles, 13 + 5 + 5) == ["Mode: Classic"]


# ---------------------------------------------------------------- pilot against the stub


def _install_v2_topics(stub: StubMenudServer) -> None:
    live = _sample("event.complications-live.json")["data"] + [
        {"app_id": "wlanpi.ip", "label": "IP", "value": "192.0.2.10", "status": "ok"},
        {"app_id": "wlanpi.cpu_temp", "label": "Temp", "value": "81C", "status": "error"},
        {"app_id": "local.wlanpi.inject", "label": "Inj", "value": "[red]x", "status": "warn"},
    ]
    stub.features = [C.FEATURE]
    stub.topic_snapshots[C.TOPIC_LIVE] = sorted(live, key=lambda r: r["app_id"])
    stub.topic_snapshots[C.TOPIC_CATALOG] = _sample("event.complications-catalog.json")["data"]
    stub.topic_snapshots[C.TOPIC_CONFIG] = _sample("event.complications-config.json")["data"]
    for t in C.TOPICS:
        stub.topic_seq[t] = 1


async def test_old_service_footer_unchanged(tmp_path: Path) -> None:
    """No welcome.features: the new topics are never asked for (§5.1)."""
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(120, 24)) as pilot:
            sub = await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            assert set(sub["topics"]) == {"device.summary", "alerts", "complications"}
            await _wait_until(lambda: "Uplink: OK" in _widget_text(app, "#status-footer"))
            await pilot.pause()
            footer = _widget_text(app, "#status-footer")
            assert "wlanpi-stub" in footer and "mode:classic" in footer and "192.0.2.10" in footer
            assert not app.complications_v2


async def test_new_service_footer_follows_tui_profile(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        _install_v2_topics(stub)
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(160, 24)) as pilot:
            sub = await stub.wait_for_frame(lambda f: f["type"] == "subscribe")
            assert set(sub["topics"]) == {"device.summary", "alerts", "complications", *C.TOPICS}
            await _wait_until(lambda: "Mode: Classic" in _widget_text(app, "#status-footer"))
            await pilot.pause()
            line = _widget_text(app, "#status-footer").splitlines()[0]
            # tui profile: mode, eth0 (not live), ip, cpu_temp, plugin.*, local.*
            order = ["wlanpi-stub", "• Mode: Classic", "✓ IP: 192.0.2.10", "✗ Temp: 81C",
                     "✓ ◍ Uplink: 412 Mb/s", "▲ Inj: [red]x", "✓ VPN: up", "! Low battery 15%"]
            idx = [line.index(s) for s in order]
            assert idx == sorted(idx), line
            # Legacy device.summary keys and the legacy topic are not drawn twice.
            assert "mode:classic" not in line
            assert "Uplink: OK" not in line
            # Not selected by the tui profile.
            assert "Batt" not in line and "Wi-Fi" not in line

            # An edit made elsewhere repaints without reconnecting.
            cfg = json.loads(json.dumps(stub.topic_snapshots[C.TOPIC_CONFIG]))
            cfg["surfaces"]["tui"] = {"show": ["wlanpi.cpu_temp", "wlanpi.mode"], "source": "user"}
            await stub.send_frame({"v": 1, "type": "event", "id": stub.next_server_id(),
                                   "topic": C.TOPIC_CONFIG, "seq": 2, "data": cfg})
            await _wait_until(lambda: "Uplink" not in _widget_text(app, "#status-footer"))
            line = _widget_text(app, "#status-footer").splitlines()[0]
            assert line.index("Temp: 81C") < line.index("Mode: Classic")
            assert "VPN" not in line
            assert sum(1 for f in stub.received_frames if f["type"] == "hello") == 1


async def test_new_service_footer_cut_to_width(tmp_path: Path) -> None:
    sock = tmp_path / "local.sock"
    async with StubMenudServer(sock) as stub:
        _install_v2_topics(stub)
        stub.topic_snapshots["alerts"] = []
        app = MenuTuiApp(socket_path=str(sock), reconnect_delay=0.05)
        async with app.run_test(size=(60, 24)) as pilot:
            await _wait_until(lambda: "Mode: Classic" in _widget_text(app, "#status-footer"))
            await pilot.pause()
            line = _widget_text(app, "#status-footer").splitlines()[0]
            assert len(line) <= 58, line
            assert "VPN" not in line  # the tail is cut, never reordered
            assert line.startswith("wlanpi-stub")
