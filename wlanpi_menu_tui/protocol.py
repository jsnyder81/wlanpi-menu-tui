"""Protocol v1 frame builders and client session state for wlanpi-menu-tui."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wlanpi_menu_tui import __version__

PROTOCOL_VERSION = 1
CLIENT_NAME = "wlanpi-menu-tui"
DEFAULT_SOCKET_PATH = "/run/wlanpi-menud/local.sock"

SUPPORTED_BLOCKS: list[str] = [
    "text",
    "kv",
    "table",
    "list",
    "qr",
    "status",
    "progress",
    "confirm",
]

SUPPORTED_KEYS: list[str] = [
    "up",
    "down",
    "left",
    "right",
    "center",
    "key1",
    "key2",
    "key3",
]

DEFAULT_TOPICS: list[str] = [
    "device.summary",
    "alerts",
    "complications",
]

MAX_CACHED_RESULT_PAGES = 4

FATAL_CLOSE_CODES = {1008, 4400, 4401, 4426}
FATAL_ERROR_CODES = {"AUTH_FAILED", "VERSION"}


@dataclass(slots=True)
class ResumeState:
    """Reconnect resume payload sent in hello.resume."""

    epoch: str
    session_id: str | None = None
    seq: dict[str, int] = field(default_factory=dict)
    menu_revision: int | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"epoch": self.epoch}
        if self.session_id:
            payload["session_id"] = self.session_id
        if self.seq:
            payload["seq"] = dict(self.seq)
        if self.menu_revision is not None:
            payload["menu_revision"] = self.menu_revision
        return payload


def build_hello(
    msg_id: str,
    *,
    cols: int = 80,
    rows: int = 24,
    colour: str = "colour",
    blocks: list[str] | None = None,
    keys: list[str] | None = None,
    resume: ResumeState | None = None,
    protocol: list[int] | None = None,
) -> dict[str, Any]:
    """Build a C->S hello frame for thin mode."""
    frame: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": "hello",
        "id": msg_id,
        "mode": "thin",
        "protocol": protocol if protocol is not None else [PROTOCOL_VERSION],
        "client": {
            "name": CLIENT_NAME,
            "version": __version__,
        },
        "capabilities": {
            "screen": {
                "cols": max(1, int(cols)),
                "rows": max(1, int(rows)),
            },
            "colour": colour,
            "blocks": list(blocks if blocks is not None else SUPPORTED_BLOCKS),
            "keys": list(keys if keys is not None else SUPPORTED_KEYS),
        },
    }
    if resume is not None:
        frame["resume"] = resume.to_payload()
    return frame


def build_subscribe(msg_id: str, topics: list[str] | None = None) -> dict[str, Any]:
    """Build a C->S subscribe frame."""
    return {
        "v": PROTOCOL_VERSION,
        "type": "subscribe",
        "id": msg_id,
        "topics": list(topics if topics is not None else DEFAULT_TOPICS),
    }


def build_unsubscribe(msg_id: str, topics: list[str]) -> dict[str, Any]:
    """Build a C->S unsubscribe frame."""
    return {
        "v": PROTOCOL_VERSION,
        "type": "unsubscribe",
        "id": msg_id,
        "topics": list(topics),
    }


def build_input(msg_id: str, key: str, action: str = "press") -> dict[str, Any]:
    """Build a C->S input frame."""
    if key not in SUPPORTED_KEYS:
        raise ValueError(f"Unsupported input key: {key!r}")
    frame: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": "input",
        "id": msg_id,
        "key": key,
    }
    if action != "press":
        frame["action"] = action
    return frame


def build_invoke(
    msg_id: str,
    node: str,
    *,
    params: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Build a C->S invoke frame."""
    frame: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": "invoke",
        "id": msg_id,
        "node": node,
    }
    if params:
        frame["params"] = dict(params)
    if confirmed:
        frame["confirmed"] = True
    return frame


def build_answer(
    msg_id: str,
    prompt_id: str,
    *,
    value: Any = ...,
    values: dict[str, Any] | None = None,
    confirmed: bool | None = None,
    cancel: bool = False,
) -> dict[str, Any]:
    """Build a C->S answer frame with exactly one of value, values, confirmed, or cancel."""
    frame: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": "answer",
        "id": msg_id,
        "prompt_id": prompt_id,
    }
    if cancel:
        frame["cancel"] = True
    elif confirmed is not None:
        frame["confirmed"] = bool(confirmed)
    elif values is not None:
        frame["values"] = dict(values)
    elif value is not ...:
        frame["value"] = value
    else:
        raise ValueError("answer frame requires one of value, values, confirmed, or cancel")
    return frame


def build_page(msg_id: str, job_id: str, cursor: str) -> dict[str, Any]:
    """Build a C->S page frame."""
    return {
        "v": PROTOCOL_VERSION,
        "type": "page",
        "id": msg_id,
        "job_id": job_id,
        "cursor": cursor,
    }


def build_cancel(msg_id: str, job_id: str) -> dict[str, Any]:
    """Build a C->S cancel frame."""
    return {
        "v": PROTOCOL_VERSION,
        "type": "cancel",
        "id": msg_id,
        "job_id": job_id,
    }


@dataclass(slots=True)
class MenuTreeState:
    """Resolved menu tree from a server 'menu' frame."""

    revision: int = 0
    device_mode: str | None = None
    nodes_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    children_by_parent: dict[str | None, list[dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def from_frame(cls, frame: dict[str, Any]) -> MenuTreeState:
        revision = int(frame.get("revision", 0))
        device_mode = frame.get("device_mode")
        nodes = list(frame.get("nodes", []))
        nodes_by_id: dict[str, dict[str, Any]] = {}
        children_by_parent: dict[str | None, list[dict[str, Any]]] = {}

        for node in nodes:
            nid = node["id"]
            nodes_by_id[nid] = node
            parent = node.get("parent")
            children_by_parent.setdefault(parent, []).append(node)

        for siblings in children_by_parent.values():
            siblings.sort(key=lambda n: (int(n.get("order", 0)), str(n.get("id", ""))))

        return cls(
            revision=revision,
            device_mode=device_mode,
            nodes_by_id=nodes_by_id,
            children_by_parent=children_by_parent,
        )

    def children_of(self, parent_id: str | None = None) -> list[dict[str, Any]]:
        return list(self.children_by_parent.get(parent_id, []))

    def breadcrumb_for(self, node_id: str | None) -> list[str]:
        if not node_id or node_id not in self.nodes_by_id:
            return []
        crumbs: list[str] = []
        curr: str | None = node_id
        seen: set[str] = set()
        while curr and curr in self.nodes_by_id and curr not in seen:
            seen.add(curr)
            node = self.nodes_by_id[curr]
            crumbs.append(str(node.get("label", curr)))
            curr = node.get("parent")
        crumbs.reverse()
        return crumbs

    def render_tree_lines(self, selected_node_id: str | None = None) -> list[str]:
        """Render the full menu tree hierarchy as human-readable lines."""
        lines: list[str] = []
        mode_tag = f" ({self.device_mode})" if self.device_mode else ""
        lines.append(f"Menu Tree [rev {self.revision}]{mode_tag}")

        def _walk(parent_id: str | None, depth: int) -> None:
            for node in self.children_of(parent_id):
                nid = node["id"]
                label = node.get("label", nid)
                kind = node.get("kind", "action")
                indent = "  " * depth
                prefix = "▸ " if kind == "branch" else "• "
                marker = "> " if nid == selected_node_id else "  "
                badges: list[str] = []
                if node.get("confirm"):
                    badges.append("confirm")
                if node.get("params"):
                    badges.append("params")
                if node.get("provider"):
                    badges.append(f"provider:{node['provider']}")
                badge_str = f" [{', '.join(badges)}]" if badges else ""
                lines.append(f"{marker}{indent}{prefix}{label} ({nid}){badge_str}")
                if kind == "branch":
                    _walk(nid, depth + 1)

        _walk(None, 0)
        return lines


@dataclass(slots=True)
class JobResultPage:
    """Single cached page of a job result."""

    page_number: int
    title: str | None
    blocks: list[dict[str, Any]]
    cursor: str | None
    total: int | None


@dataclass(slots=True)
class JobState:
    """Tracks a job's lifecycle and bounded result pages (max 4 pages cached)."""

    job_id: str
    node: str
    state: str = "queued"
    shared: bool = False
    cached_at: str | None = None
    progress_pct: float | None = None
    progress_message: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_core_status: int | None = None
    pages: list[JobResultPage] = field(default_factory=list)
    current_idx: int = 0
    total_pages_seen: int = 0
    pending_cursor: str | None = None

    def apply_frame(self, frame: dict[str, Any], *, is_page_reply: bool = False) -> None:
        self.state = frame["state"]
        if "shared" in frame:
            self.shared = bool(frame["shared"])
        if "cached_at" in frame:
            self.cached_at = frame["cached_at"]

        if self.state == "progress":
            prog = frame.get("progress") or {}
            self.progress_pct = prog.get("pct")
            self.progress_message = prog.get("message")
        elif self.state == "error":
            err = frame.get("error") or {}
            self.error_code = err.get("code")
            self.error_message = err.get("message")
            self.error_core_status = err.get("core_status")
        elif self.state == "result":
            res = frame.get("result") or {}
            self.pending_cursor = None
            if not is_page_reply or not self.pages:
                self.total_pages_seen = 1
                page = JobResultPage(
                    page_number=1,
                    title=res.get("title"),
                    blocks=list(res.get("blocks", [])),
                    cursor=res.get("cursor"),
                    total=res.get("total"),
                )
                self.pages = [page]
                self.current_idx = 0
            else:
                self.total_pages_seen += 1
                page = JobResultPage(
                    page_number=self.total_pages_seen,
                    title=res.get("title") or (self.pages[-1].title if self.pages else None),
                    blocks=list(res.get("blocks", [])),
                    cursor=res.get("cursor"),
                    total=res.get("total") if "total" in res else (self.pages[-1].total if self.pages else None),
                )
                self.pages.append(page)
                while len(self.pages) > MAX_CACHED_RESULT_PAGES:
                    self.pages.pop(0)
                self.current_idx = len(self.pages) - 1

    @property
    def current_page(self) -> JobResultPage | None:
        if not self.pages:
            return None
        idx = max(0, min(self.current_idx, len(self.pages) - 1))
        return self.pages[idx]

    def step_up(self) -> bool:
        """Move to the previous cached page if available."""
        if self.current_idx > 0:
            self.current_idx -= 1
            return True
        return False

    def step_down_or_cursor(self) -> tuple[bool, str | None]:
        """Advance to the next cached page, or return the cursor to fetch the next page."""
        if self.current_idx + 1 < len(self.pages):
            self.current_idx += 1
            return True, None
        curr = self.current_page
        if curr and curr.cursor:
            return False, curr.cursor
        return False, None
