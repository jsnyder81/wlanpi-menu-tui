"""View block renderers with plain-text fallback for unknown block types."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from rich.markup import escape

from wlanpi_menu_tui.complications import level_markup
from wlanpi_menu_tui.protocol import SUPPORTED_BLOCKS, MenuTreeState
from wlanpi_menu_tui.qr import render_qr_text_art


def is_rule_label(label: str) -> bool:
    """A ``- - fits above - -`` style separator row (Home tiles capacity mark)."""
    s = label.strip()
    return len(s) > 4 and s.startswith("- -") and s.endswith("- -")


def _format_scalar(val: Any) -> str:
    if val is None:
        return "—"
    if isinstance(val, bool):
        return "Yes" if val else "No"
    return str(val)


def render_text_block(block: dict[str, Any]) -> str:
    """Render a 'text' view block according to its style."""
    raw = str(block.get("text", ""))
    escaped = escape(raw)
    style = block.get("style", "body")
    if style == "heading":
        return f"[bold cyan]{escaped}[/bold cyan]"
    if style == "mono":
        return f"[bold]{escaped}[/bold]"
    if style == "muted":
        return f"[dim]{escaped}[/dim]"
    return escaped


def render_kv_block(block: dict[str, Any], *, reveal_secrets: bool = False) -> str:
    """Render a 'kv' key-value block."""
    items = block.get("items")
    if not isinstance(items, list) or not items:
        return escape(str(block.get("text", "")))

    key_width = max((len(str(item.get("key", ""))) for item in items), default=8)
    lines: list[str] = []
    for item in items:
        key = str(item.get("key", ""))
        secret = bool(item.get("secret", False))
        raw_val = _format_scalar(item.get("value"))
        if secret and not reveal_secrets:
            val_str = "••••••••"
        else:
            val_str = level_markup(item.get("level"), escape(raw_val))
        pad = " " * max(1, key_width - len(key) + 2)
        lines.append(f"  [bold]{escape(key)}:[/bold]{pad}{val_str}")
    return "\n".join(lines)


def render_table_block(block: dict[str, Any], *, max_width: int = 76) -> str:
    """Render a 'table' block with column alignment and priority-aware column fitting."""
    columns = block.get("columns")
    rows = block.get("rows")
    if not isinstance(columns, list) or not columns or not isinstance(rows, list):
        return escape(str(block.get("text", "")))

    active_indices = list(range(len(columns)))

    def _compute_widths(indices: list[int]) -> dict[int, int]:
        widths: dict[int, int] = {}
        for idx in indices:
            col = columns[idx]
            label = str(col.get("label", col.get("key", "")))
            w = len(label)
            for row in rows:
                if isinstance(row, list) and idx < len(row):
                    w = max(w, len(_format_scalar(row[idx])))
            widths[idx] = w
        return widths

    widths = _compute_widths(active_indices)
    # Drop lowest-importance (highest priority number) columns if table exceeds max_width
    while len(active_indices) > 1:
        total_w = sum(widths[i] for i in active_indices) + 2 * (len(active_indices) - 1) + 2
        if total_w <= max_width:
            break
        droppable = sorted(
            active_indices,
            key=lambda i: (int(columns[i].get("priority", 0)), i),
            reverse=True,
        )
        active_indices.remove(droppable[0])
        widths = _compute_widths(active_indices)

    header_parts: list[str] = []
    rule_parts: list[str] = []
    for idx in active_indices:
        col = columns[idx]
        label = str(col.get("label", col.get("key", "")))
        align = col.get("align", "left")
        w = widths[idx]
        cell = label.rjust(w) if align == "right" else label.ljust(w)
        header_parts.append(f"[bold]{escape(cell)}[/bold]")
        rule_parts.append("─" * w)

    lines: list[str] = [
        "  " + "  ".join(header_parts),
        "  [dim]" + "  ".join(rule_parts) + "[/dim]",
    ]

    for row in rows:
        if not isinstance(row, list):
            continue
        row_parts: list[str] = []
        for idx in active_indices:
            col = columns[idx]
            align = col.get("align", "left")
            w = widths[idx]
            val = _format_scalar(row[idx]) if idx < len(row) else ""
            cell = val.rjust(w) if align == "right" else val.ljust(w)
            row_parts.append(escape(cell))
        lines.append("  " + "  ".join(row_parts))

    return "\n".join(lines)


def render_list_block(
    block: dict[str, Any],
    *,
    menu_tree: MenuTreeState | None = None,
) -> str:
    """Render a 'list' block with cursor selection and branch/detail indicators."""
    items = block.get("items")
    if not isinstance(items, list) or not items:
        return escape(str(block.get("text", "")))

    selected = block.get("selected")
    lines: list[str] = []
    for idx, item in enumerate(items):
        label = str(item.get("label", ""))
        detail = item.get("detail")
        node_id = item.get("node")
        is_branch = bool(item.get("branch", False))
        if not is_branch and node_id and menu_tree and node_id in menu_tree.nodes_by_id:
            is_branch = menu_tree.nodes_by_id[node_id].get("kind") == "branch"

        branch_suffix = " ▸" if is_branch else ""
        detail_suffix = f" [dim]({escape(str(detail))})[/dim]" if detail else ""
        is_sel = selected is not None and int(selected) == idx
        if is_rule_label(label) and not is_sel:
            lines.append(f"   [dim]{escape(label)}[/dim]")
        elif is_sel:
            lines.append(
                f"[bold reverse] > {escape(label)}{branch_suffix} [/bold reverse]{detail_suffix}"
            )
        else:
            lines.append(f"   {escape(label)}{branch_suffix}{detail_suffix}")

    return "\n".join(lines)


def render_qr_block(block: dict[str, Any]) -> str:
    """Render a 'qr' block as Unicode text-art with caption and text fallback."""
    data = str(block.get("data", ""))
    caption = block.get("caption")
    text = block.get("text")
    art = render_qr_text_art(
        data,
        caption=str(caption) if caption is not None else None,
        fallback_text=str(text) if text is not None else None,
    )
    return escape(art)


def render_status_block(block: dict[str, Any]) -> str:
    """Render a 'status' block with level badge, label, and text."""
    level = str(block.get("level", "info")).lower()
    badge_color = {
        "ok": "green",
        "info": "cyan",
        "warn": "yellow",
        "error": "red",
    }.get(level, "white")
    badge = f"[bold {badge_color}]\\[{level.upper()}][/bold {badge_color}]"
    label = block.get("label")
    text = str(block.get("text", ""))
    if label and label not in text:
        return f"  {badge} [bold]{escape(str(label))}:[/bold] {escape(text)}"
    return f"  {badge} {escape(text)}"


def render_progress_block(block: dict[str, Any]) -> str:
    """Render a 'progress' block with a visual progress bar."""
    pct = block.get("pct")
    label = block.get("label")
    text = str(block.get("text", ""))
    bar_width = 20
    if isinstance(pct, (int, float)):
        clamped = max(0.0, min(100.0, float(pct)))
        filled = round((clamped / 100.0) * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        pct_str = f"{clamped:.0f}%"
    else:
        bar = "═══▷" + "░" * (bar_width - 4)
        pct_str = "..."

    prefix = f"{escape(str(label))} " if label else ""
    line = f"  {prefix}[cyan]\\[{bar}][/cyan] {pct_str}"
    if text and text not in (str(label or ""), f"{label} {pct_str}".strip()):
        line += f"\n  [dim]{escape(text)}[/dim]"
    return line


def render_confirm_block(
    block: dict[str, Any],
    *,
    selected_choice: str | None = None,
) -> str:
    """Render a 'confirm' block with message and Confirm/Cancel buttons."""
    message = str(block.get("message") or block.get("text") or "")
    confirm_label = str(block.get("confirm_label") or "Confirm")
    cancel_label = str(block.get("cancel_label") or "Cancel")
    selected = selected_choice or str(block.get("selected") or "cancel")

    if selected == "confirm":
        btn_confirm = f"[bold reverse green] \\[{escape(confirm_label)}] [/bold reverse green]"
        btn_cancel = f"  \\[{escape(cancel_label)}] "
    else:
        btn_confirm = f"  \\[{escape(confirm_label)}] "
        btn_cancel = f"[bold reverse yellow] \\[{escape(cancel_label)}] [/bold reverse yellow]"

    return f"  [bold]{escape(message)}[/bold]\n\n  {btn_confirm}   {btn_cancel}"


def render_block(
    block: dict[str, Any],
    *,
    supported_blocks: Collection[str] = SUPPORTED_BLOCKS,
    menu_tree: MenuTreeState | None = None,
    confirm_selection: dict[str, str] | None = None,
    max_width: int = 76,
) -> str:
    """Render a single view block, falling back to block['text'] for unknown/unsupported types."""
    btype = str(block.get("type", ""))
    if btype not in supported_blocks:
        return escape(str(block.get("text", "")))

    if btype == "text":
        return render_text_block(block)
    if btype == "kv":
        return render_kv_block(block)
    if btype == "table":
        return render_table_block(block, max_width=max_width)
    if btype == "list":
        return render_list_block(block, menu_tree=menu_tree)
    if btype == "qr":
        return render_qr_block(block)
    if btype == "status":
        return render_status_block(block)
    if btype == "progress":
        return render_progress_block(block)
    if btype == "confirm":
        pid = str(block.get("prompt_id", ""))
        sel = confirm_selection.get(pid) if confirm_selection else None
        return render_confirm_block(block, selected_choice=sel)

    return escape(str(block.get("text", "")))


def render_blocks(
    blocks: list[dict[str, Any]],
    *,
    supported_blocks: Collection[str] = SUPPORTED_BLOCKS,
    menu_tree: MenuTreeState | None = None,
    confirm_selection: dict[str, str] | None = None,
    max_width: int = 76,
) -> str:
    """Render a sequence of view blocks separated by blank lines."""
    if not blocks:
        return "[dim]No content[/dim]"
    rendered = [
        render_block(
            b,
            supported_blocks=supported_blocks,
            menu_tree=menu_tree,
            confirm_selection=confirm_selection,
            max_width=max_width,
        )
        for b in blocks
    ]
    return "\n\n".join(rendered)
