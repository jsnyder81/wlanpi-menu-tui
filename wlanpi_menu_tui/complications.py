"""Complications for the TUI footer (wlanpi-menud complications spec §2.6, §4, §5).

The service announces ``complications.2`` in ``welcome.features``. Only then
does the TUI subscribe to ``complications.live``, ``.catalog`` and ``.config``
and draw its footer from the ``tui`` profile; against an older service the
footer keeps its ``device.summary`` + legacy ``complications`` rendering.

The user's profile decides which tiles and in what order; the TUI decides how
many fit (§4.5: in order until the line is full, at most 8). Overflow is cut
from the end and never reordered.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rich.cells import cell_len
from rich.markup import escape

FEATURE = "complications.2"
PROFILE = "tui"
DEFAULT_PROFILE = "default"
TOPIC_LIVE = "complications.live"
TOPIC_CATALOG = "complications.catalog"
TOPIC_CONFIG = "complications.config"
TOPICS: tuple[str, ...] = (TOPIC_LIVE, TOPIC_CATALOG, TOPIC_CONFIG)

FOOTER_MAX_TILES = 8
SEPARATOR = "  │  "
UNKNOWN_VALUE = "--"
ELLIPSIS = "…"

# Shipped ``tui`` default (§4.4), used until the config topic arrives.
SHIPPED_TUI_SHOW: tuple[str, ...] = (
    "wlanpi.mode", "wlanpi.eth0", "wlanpi.ip", "wlanpi.cpu_temp", "plugin.*", "local.*",
)

# §2.6 status vocabulary -> (marker glyph, Rich style). Colour is never the
# only signal: every judged status also changes the marker.
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "ok": ("✓", "green"),
    "info": ("", ""),
    "warn": ("▲", "yellow"),
    "error": ("✗", "bold red"),
    "off": ("○", "dim"),
    "unknown": ("?", "dim"),
}

# kv item ``level`` (§5.1) uses the four-level subset.
LEVEL_STYLE: dict[str, tuple[str, str]] = {k: STATUS_STYLE[k] for k in ("ok", "info", "warn", "error")}

# §2.7 icon names -> TUI glyphs. A name without a glyph draws none.
ICON_GLYPHS: dict[str, str] = {
    "battery": "▮",
    "bolt": "↯",
    "bluetooth": "ᛒ",
    "globe": "◍",
    "wifi": "≋",
    "ethernet": "⇄",
    "cpu": "▣",
    "users": "☺",
    "radio": "⌁",
    "shield": "◈",
    "dot": "•",
}


def normalize_status(status: Any) -> str:
    """An unrecognized status is drawn as ``unknown`` (§2.6)."""
    s = str(status or "").lower()
    if s == "warning":  # deprecated alias the service stores as warn
        return "warn"
    return s if s in STATUS_STYLE else "unknown"


def resolve(show: Iterable[str], live_ids: Iterable[str]) -> list[str]:
    """§4.3 resolution, before the capacity cut.

    Walk ``show`` in order; an exact id yields itself if live; a ``.*``
    pattern yields every live id it matches that no exact entry anywhere in
    the list names and that nothing earlier yielded, sorted by id.
    """
    show = list(show)
    live = sorted(set(live_ids))
    exact = {e for e in show if not e.endswith(".*")}
    out: list[str] = []
    seen: set[str] = set()
    for entry in show:
        if entry.endswith(".*"):
            prefix = entry[:-1]
            for i in live:
                if i.startswith(prefix) and i not in exact and i not in seen:
                    out.append(i)
                    seen.add(i)
        elif entry in live and entry not in seen:
            out.append(entry)
            seen.add(entry)
    return out


def show_for(config: Any, profile: str = PROFILE) -> list[str]:
    """The profile's show list from ``complications.config`` data, falling back
    to the service's ``default`` profile, then the shipped ``tui`` list."""
    surfaces = config.get("surfaces") if isinstance(config, dict) else None
    if isinstance(surfaces, dict):
        for name in (profile, DEFAULT_PROFILE):
            body = surfaces.get(name)
            if isinstance(body, dict) and isinstance(body.get("show"), list):
                return [str(e) for e in body["show"]]
    return list(SHIPPED_TUI_SHOW)


def resolved_tiles(live: Any, config: Any, profile: str = PROFILE) -> list[dict[str, Any]]:
    """Live records the profile selects, in profile order (not yet cut)."""
    if not isinstance(live, list):
        return []
    by_id = {str(r["app_id"]): r for r in live if isinstance(r, dict) and r.get("app_id")}
    return [by_id[i] for i in resolve(show_for(config, profile), by_id)]


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    out = ""
    for ch in text:
        if cell_len(out + ch) > width - 1:
            break
        out += ch
    return out + ELLIPSIS


def tile_plain(tile: dict[str, Any], *, value_width: int | None = None) -> str:
    """Plain text of one footer segment: ``[marker ][icon ]label: value``."""
    status = normalize_status(tile.get("status"))
    marker, _ = STATUS_STYLE[status]
    icon = ICON_GLYPHS.get(str(tile.get("icon") or ""), "")
    label = str(tile.get("label") or tile.get("app_id") or "")
    value = UNKNOWN_VALUE if status == "unknown" else str(tile.get("value") or UNKNOWN_VALUE)
    if value_width is not None:
        value = _clip(value, value_width)
    prefix = "".join(f"{g} " for g in (marker, icon) if g)
    return f"{prefix}{label}: {value}"


def tile_markup(tile: dict[str, Any], *, value_width: int | None = None) -> str:
    """Rich markup for one segment, every provider string escaped."""
    status = normalize_status(tile.get("status"))
    _, style = STATUS_STYLE[status]
    text = escape(tile_plain(tile, value_width=value_width))
    return f"[{style}]{text}[/{style}]" if style else text


def fit_tiles(tiles: list[dict[str, Any]], width: int, *, sep_len: int = len(SEPARATOR)) -> list[str]:
    """Footer segments that fit ``width`` columns, in order, at most 8 (§4.5).

    A tile that would overflow is shortened (never its label) when its label
    and at least one value character still fit; everything after is dropped.
    """
    out: list[str] = []
    used = 0
    for tile in tiles[:FOOTER_MAX_TILES]:
        gap = sep_len if out else 0
        room = width - used - gap
        plain = tile_plain(tile)
        if cell_len(plain) <= room:
            out.append(tile_markup(tile))
            used += gap + cell_len(plain)
            continue
        head = cell_len(tile_plain(tile, value_width=0))  # marker, icon, label and ": "
        if room - head >= 2:  # one value character plus the ellipsis
            out.append(tile_markup(tile, value_width=room - head))
        break
    return out


def level_markup(level: Any, text: str) -> str:
    """Style an already-escaped kv value by its ``level`` with a marker."""
    lv = str(level or "").lower()
    if lv not in LEVEL_STYLE:
        return text
    marker, style = LEVEL_STYLE[lv]
    body = f"{marker} {text}" if marker else text
    return f"[{style}]{body}[/{style}]" if style else body
