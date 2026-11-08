"""Textual thin-mode client application for wlanpi-menud."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Collection
from typing import Any, ClassVar

from rich.markup import escape
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.widgets import Static
from websockets.asyncio.client import connect, unix_connect
from websockets.exceptions import ConnectionClosed

from wlanpi_menu_tui.blocks import render_blocks
from wlanpi_menu_tui.protocol import (
    DEFAULT_SOCKET_PATH,
    DEFAULT_TOPICS,
    FATAL_CLOSE_CODES,
    FATAL_ERROR_CODES,
    SESSION_DEFAULT,
    SUPPORTED_BLOCKS,
    JobState,
    MenuTreeState,
    ResumeState,
    build_answer,
    build_cancel,
    build_hello,
    build_input,
    build_invoke,
    build_page,
    build_subscribe,
)

log = logging.getLogger(__name__)

# Thin-mode keyboard to input-frame keys.
THIN_KEY_MAP: dict[str, str] = {
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "enter": "center",
    "space": "center",
    "f1": "key1",
    "1": "key1",
    "f2": "key2",
    "2": "key2",
    "f3": "key3",
    "3": "key3",
}

# Prompt kinds a thin client answers on the shared screen with input frames.
# string and form prompts need a keyboard, so the TUI edits those locally.
SHARED_SCREEN_PROMPTS = ("select", "confirm", "integer")


def describe_driver(driven_by: dict[str, Any]) -> str:
    """'wlanpi-webui (webui:alice)' for a view.driven_by object."""
    principal = driven_by.get("principal") or {}
    who = f"{principal.get('kind', '?')}:{principal.get('name', '?')}"
    surface = driven_by.get("surface")
    return f"{surface} ({who})" if surface else who


def describe_principal(principal: dict[str, Any] | None) -> str:
    if not principal:
        return "the user who started it"
    return f"{principal.get('kind', '?')}:{principal.get('name', '?')}"


class MainScroll(ScrollableContainer, can_focus=False):
    """Scroll pane for the main screen + menu tree.

    Not focusable: arrow keys belong to the thin-mode ``input`` mapping in
    ``MenuTuiApp.on_key``. A focused ScrollableContainer also consumes
    up/down as scroll bindings, so on a tall menu tree every navigation
    press scrolled the pane and pushed the result view off-screen.
    """


class MenuTuiApp(App[None]):
    """Textual thin-mode surface connecting to wlanpi-menud over local.sock."""

    TITLE = "WLAN Pi Menu TUI"

    CSS = """
    Screen {
        layout: vertical;
    }
    #header-bar {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    #error-banner {
        height: auto;
        background: $error;
        color: $text;
        padding: 0 1;
        display: none;
    }
    #main-scroll {
        height: 1fr;
        padding: 1 2;
    }
    #screen-view {
        width: 100%;
    }
    #menu-tree-view {
        width: 100%;
        margin-top: 1;
        color: $text-muted;
    }
    #status-footer {
        height: auto;
        min-height: 2;
        background: $surface;
        color: $text;
        padding: 0 1;
        border-top: solid $primary;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET_PATH,
        *,
        supported_blocks: Collection[str] = SUPPORTED_BLOCKS,
        subscribe_topics: list[str] | None = None,
        reconnect_delay: float = 0.5,
        protocol_versions: list[int] | None = None,
        session: str = SESSION_DEFAULT,
    ) -> None:
        super().__init__()
        self.socket_path = socket_path
        # hello.session: "default" attaches to the shared default session
        # (mirror and drive the panel's screen), "new" opens our own.
        self.session_request = session
        self.supported_blocks = list(supported_blocks)
        self.subscribe_topics = list(subscribe_topics if subscribe_topics is not None else DEFAULT_TOPICS)
        self.reconnect_delay = reconnect_delay
        self.protocol_versions = list(protocol_versions) if protocol_versions is not None else None

        # Connection & protocol state
        self._ws: Any = None
        self._msg_counter = 0
        self._connected = asyncio.Event()
        self._stop_reconnect = False

        # Session & reconnect state
        self.session_id: str | None = None
        self.session_kind: str | None = None  # welcome.session
        self.epoch: str | None = None
        self.principal: dict[str, Any] | None = None
        self.roles: list[str] = []
        self.negotiated_version: int | None = None
        self.resumed: bool | None = None

        # Menu, view, job, prompt, topic state
        self.menu_tree: MenuTreeState | None = None
        self.current_view: dict[str, Any] | None = None
        self.active_prompt: dict[str, Any] | None = None
        # Default session: the last prompt frame answered on the shared screen
        # (select/confirm/integer), kept for its invoked_by principal.
        self.shared_prompt: dict[str, Any] | None = None
        # One-line local notice (for example why a key did nothing).
        self.notice: str | None = None
        self.jobs: dict[str, JobState] = {}
        self.active_job_id: str | None = None
        self._pending_page_req_ids: set[str] = set()

        # Display mode: "view" | "job" | "prompt" | "menu_only"
        self.display_mode: str = "view"

        # Interactive prompt state
        self.prompt_cursor: int = 0
        self.prompt_confirm_choice: str = "cancel"
        self.prompt_scalar_value: str = ""
        self.prompt_field_idx: int = 0
        self.prompt_form_values: dict[str, Any] = {}
        self.confirm_selection: dict[str, str] = {}

        # Subscribed topics & sequence tracking
        self.topic_seq: dict[str, int] = {}
        self.topic_data: dict[str, Any] = {}
        self.stale_topics: set[str] = set()

        # Last rendered main-screen markup (scroll reset on change)
        self._last_main_text: str | None = None
        # Jobs whose lifecycle the server renders via thin-mode ``view``
        # frames (wlanpi-menud sends job + view for every transition).
        self._view_driven_jobs: set[str] = set()

        # Error state
        self.last_error: dict[str, Any] | None = None
        self.fatal_error: bool = False

    def compose(self) -> ComposeResult:
        yield Static("WLAN Pi Menu TUI — Connecting...", id="header-bar")
        yield Static("", id="error-banner")
        with MainScroll(id="main-scroll"):
            yield Static("Connecting to wlanpi-menud...", id="screen-view")
            yield Static("", id="menu-tree-view")
        yield Static("Waiting for status topics...", id="status-footer")

    def on_mount(self) -> None:
        self._run_ws_loop()

    def _next_id(self) -> str:
        self._msg_counter += 1
        return f"c{self._msg_counter}"

    async def _send_frame(self, frame: dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(frame))
            return True
        except Exception:
            log.debug("Failed to send frame %s", frame.get("type"), exc_info=True)
            return False

    def _build_resume_state(self) -> ResumeState | None:
        if not self.epoch:
            return None
        seq_map = dict(self.topic_seq)
        return ResumeState(
            epoch=self.epoch,
            session_id=self.session_id,
            seq=seq_map,
            menu_revision=self.menu_tree.revision if self.menu_tree is not None else None,
        )

    def _open_ws_connection(self) -> Any:
        if self.socket_path.startswith(("ws://", "wss://")):
            return connect(self.socket_path)
        return unix_connect(path=self.socket_path, uri="ws://localhost/ws")

    @work(exclusive=True)
    async def _run_ws_loop(self) -> None:
        """Maintain single push WebSocket connection with resume support."""
        while not self._stop_reconnect and not self.fatal_error:
            try:
                async with self._open_ws_connection() as ws:
                    self._ws = ws
                    self._connected.set()

                    cols = self.size.width if self.size.width > 0 else 80
                    rows = self.size.height if self.size.height > 0 else 24
                    resume = self._build_resume_state()
                    hello = build_hello(
                        self._next_id(),
                        cols=cols,
                        rows=rows,
                        blocks=self.supported_blocks,
                        resume=resume,
                        protocol=self.protocol_versions,
                        session=self.session_request,
                    )
                    await self._send_frame(hello)

                    async for raw in ws:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        frame = json.loads(raw)
                        await self._handle_server_frame(frame, had_resume=resume is not None)

            except ConnectionClosed as exc:
                self._ws = None
                self._connected.clear()
                code = exc.rcvd.code if exc.rcvd else None
                if code in FATAL_CLOSE_CODES or self.fatal_error:
                    self._stop_reconnect = True
                    self._record_close_error(code, exc.rcvd.reason if exc.rcvd else "")
                    self._refresh_ui()
                    break
            except asyncio.CancelledError:
                self._ws = None
                self._connected.clear()
                break
            except (OSError, ValueError, RuntimeError) as exc:
                self._ws = None
                self._connected.clear()
                log.debug("WebSocket connection error: %s", exc)

            if self._stop_reconnect or self.fatal_error:
                break
            await asyncio.sleep(self.reconnect_delay)

    def _record_close_error(self, close_code: int | None, reason: str) -> None:
        if self.last_error is not None:
            return
        if close_code == 4401:
            self.last_error = {
                "code": "AUTH_FAILED",
                "message": reason or "Authentication failed or session revoked",
                "fatal": True,
                "close_code": 4401,
            }
            self.fatal_error = True
        elif close_code == 4426:
            self.last_error = {
                "code": "VERSION",
                "message": reason or "No common protocol version",
                "fatal": True,
                "close_code": 4426,
            }
            self.fatal_error = True
        elif close_code in FATAL_CLOSE_CODES:
            self.last_error = {
                "code": "BAD_REQUEST",
                "message": reason or f"Connection closed ({close_code})",
                "fatal": True,
                "close_code": close_code,
            }
            self.fatal_error = True

    async def _handle_server_frame(self, frame: dict[str, Any], *, had_resume: bool) -> None:
        ftype = frame.get("type")

        if ftype == "welcome":
            old_epoch = self.epoch
            self.session_id = frame.get("session_id")
            # A server that predates welcome.session only had independent sessions.
            self.session_kind = frame.get("session") or "new"
            self.epoch = frame.get("epoch")
            self.principal = frame.get("principal")
            self.roles = list(frame.get("roles", []))
            self.negotiated_version = frame.get("version")
            self.resumed = bool(frame.get("resumed", False))

            if not self.resumed:
                # Full resnapshot follows on mismatch or initial connect
                if old_epoch and old_epoch != self.epoch:
                    self.topic_seq.clear()
                    self.stale_topics.clear()
                    self.jobs.clear()
                    self._view_driven_jobs.clear()
                    self.active_job_id = None
                # Subscribe on initial connect or if no topics were tracked yet
                if not had_resume or not self.topic_seq:
                    sub = build_subscribe(self._next_id(), self.subscribe_topics)
                    await self._send_frame(sub)

        elif ftype == "menu":
            self.menu_tree = MenuTreeState.from_frame(frame)
            if self.current_view is None and self.active_prompt is None and self.active_job_id is None:
                self.display_mode = "menu_only"

        elif ftype == "view":
            prev_screen = self.current_view.get("screen") if self.current_view else None
            self.current_view = frame
            if self.attached and frame.get("screen") != "prompt":
                # Another surface answered (or cancelled) the prompt: the
                # shared screen moved on, so drop any local prompt editor.
                self.active_prompt = None
                self.shared_prompt = None
            if frame.get("screen") != prev_screen:
                self.notice = None
            # Initialise any confirm block default selection
            for block in frame.get("blocks", []):
                if block.get("type") == "confirm":
                    pid = str(block.get("prompt_id", ""))
                    if pid:
                        self.confirm_selection[pid] = str(block.get("selected") or "cancel")
            view_job = frame.get("job_id")
            if view_job and frame.get("screen") in ("busy", "result"):
                # Server is driving this job's screen in thin mode: render and
                # page from view frames; job frames only update JobState.
                self._view_driven_jobs.add(str(view_job))
                self.active_job_id = str(view_job)
            if self.active_prompt is None:
                self.display_mode = "view"

        elif ftype == "prompt":
            if self.attached and frame.get("kind") in SHARED_SCREEN_PROMPTS:
                # Answered on the shared screen through view + input, so every
                # attached surface sees the same highlight.
                self.shared_prompt = frame
            else:
                self._init_prompt_state(frame)
                self.display_mode = "prompt"

        elif ftype == "job":
            job_id = str(frame["job_id"])
            node = str(frame.get("node", ""))
            req_id = str(frame.get("id", ""))
            is_page_reply = req_id in self._pending_page_req_ids
            self._pending_page_req_ids.discard(req_id)

            job = self.jobs.get(job_id)
            if job is None:
                job = JobState(job_id=job_id, node=node)
                self.jobs[job_id] = job
            job.apply_frame(frame, is_page_reply=is_page_reply)
            self.active_job_id = job_id

            if job.state == "result" and job.current_page:
                for block in job.current_page.blocks:
                    if block.get("type") == "confirm":
                        pid = str(block.get("prompt_id", ""))
                        if pid and pid not in self.confirm_selection:
                            self.confirm_selection[pid] = str(block.get("selected") or "cancel")

            if self.active_prompt is None and job_id not in self._view_driven_jobs:
                self.display_mode = "job"

        elif ftype == "event":
            topic = str(frame.get("topic", ""))
            seq = int(frame.get("seq", 0))
            snapshot = bool(frame.get("snapshot", False))
            prev_seq = self.topic_seq.get(topic)

            if snapshot or prev_seq is None:
                self.stale_topics.discard(topic)
            elif seq != prev_seq + 1:
                self.stale_topics.add(topic)

            self.topic_seq[topic] = seq
            self.topic_data[topic] = frame.get("data")

        elif ftype == "error":
            self.last_error = frame
            code = str(frame.get("code", ""))
            if bool(frame.get("fatal", False)) or code in FATAL_ERROR_CODES:
                self.fatal_error = True
                self._stop_reconnect = True

        self._refresh_ui()

    @property
    def attached(self) -> bool:
        """True when attached to the shared default session (mirror mode)."""
        return self.session_kind == "default"

    def _driver_hint(self) -> str | None:
        """'driven by …' when another surface produced the current view."""
        view = self.current_view
        if not self.attached or not view:
            return None
        driven_by = view.get("driven_by")
        if not isinstance(driven_by, dict) or driven_by.get("self"):
            return None
        return f"driven by {describe_driver(driven_by)}"

    def _shared_prompt_invoker(self) -> str:
        prompt = self.shared_prompt
        if prompt and self.current_view and prompt.get("job_id") == self.current_view.get("job_id"):
            return describe_principal(prompt.get("invoked_by"))
        return describe_principal(None)

    def _init_prompt_state(self, prompt: dict[str, Any]) -> None:
        self.active_prompt = prompt
        self.prompt_cursor = 0
        # Destructive confirmations default to Cancel (frames.schema.json
        # confirm_block.selected default; wlanpi-menud prompt_index 0).
        self.prompt_confirm_choice = "cancel"
        self.prompt_field_idx = 0
        self.prompt_form_values = {}

        kind = prompt.get("kind")
        default_val = prompt.get("default")
        if kind == "integer":
            init_int = default_val if isinstance(default_val, int) else (prompt.get("minimum") or 0)
            self.prompt_scalar_value = str(init_int)
        elif kind == "string":
            self.prompt_scalar_value = str(default_val) if isinstance(default_val, str) else ""
        elif kind == "form":
            for field_def in prompt.get("fields", []):
                fname = str(field_def.get("name", ""))
                ftype = str(field_def.get("type", "string"))
                fdef = field_def.get("default")
                if fdef is not None:
                    self.prompt_form_values[fname] = fdef
                elif ftype == "enum":
                    choices = field_def.get("choices") or []
                    self.prompt_form_values[fname] = choices[0]["value"] if choices else ""
                elif ftype == "integer":
                    self.prompt_form_values[fname] = field_def.get("minimum", 0)
                elif ftype == "adapter":
                    choices = field_def.get("choices") or []
                    self.prompt_form_values[fname] = (
                        choices[0]["value"] if choices else {"iface": "wlan0", "namespace": None}
                    )
                else:
                    self.prompt_form_values[fname] = ""

    # ------------------------------------------------------------------
    # Rendering methods
    # ------------------------------------------------------------------

    def _refresh_ui(self) -> None:
        if not self.is_mounted:
            return
        self.query_one("#header-bar", Static).update(self._render_header())
        self._update_error_banner()
        main_text = self._render_main_screen()
        if main_text != self._last_main_text:
            self._last_main_text = main_text
            self.query_one("#screen-view", Static).update(main_text)
            # Keep the active screen (menu/result/prompt) in view; the menu
            # tree below it is reference only.
            self.query_one("#main-scroll", MainScroll).scroll_home(animate=False)
        self.query_one("#menu-tree-view", Static).update(self._render_menu_tree_section())
        self.query_one("#status-footer", Static).update(self._render_status_footer())

    def _render_header(self) -> str:
        parts: list[str] = ["[bold]WLAN Pi[/bold]"]
        if self.menu_tree and self.menu_tree.device_mode:
            parts.append(f"mode:{escape(self.menu_tree.device_mode)}")
        if self.session_kind == "default":
            parts.append("[bold magenta]shared[/bold magenta]")
        elif self.session_id:
            parts.append(f"session:{escape(self.session_id)}")
        if self.resumed is True:
            parts.append("[green]resumed[/green]")
        elif self.resumed is False:
            parts.append("[cyan]snapshot[/cyan]")

        if self.display_mode == "view" and self.current_view:
            crumbs = self.current_view.get("breadcrumb") or []
            if not crumbs and self.current_view.get("node") and self.menu_tree:
                crumbs = self.menu_tree.breadcrumb_for(self.current_view.get("node"))
            if crumbs:
                parts.append(" › ".join(escape(str(c)) for c in crumbs))
            elif self.current_view.get("title"):
                parts.append(escape(str(self.current_view["title"])))
        elif self.display_mode == "job" and self.active_job_id in self.jobs:
            job = self.jobs[self.active_job_id]
            parts.append(f"job:{escape(job.node)}")
        elif self.display_mode == "prompt" and self.active_prompt:
            parts.append(f"prompt:{escape(str(self.active_prompt.get('title', '')))}")

        hint = self._driver_hint()
        if hint:
            parts.append(f"[magenta]{escape(hint)}[/magenta]")

        return "  │  ".join(parts)

    def _update_error_banner(self) -> None:
        banner = self.query_one("#error-banner", Static)
        if not self.last_error:
            banner.display = False
            banner.update("")
            return

        banner.display = True
        code = str(self.last_error.get("code", "ERROR"))
        msg = str(self.last_error.get("message", "Unknown error"))
        fatal = bool(self.last_error.get("fatal", False)) or code in FATAL_ERROR_CODES
        close_code = self.last_error.get("close_code")
        supported = self.last_error.get("supported")
        retry_after = self.last_error.get("retry_after_sec")
        core_status = self.last_error.get("core_status")

        severity = "FATAL ERROR" if fatal else "ERROR"
        details: list[str] = [f"[bold]\\[{severity}: {escape(code)}][/bold] {escape(msg)}"]
        if code == "VERSION" and supported is not None:
            details.append(f"Supported protocol versions: {escape(str(supported))}")
        if code == "AUTH_FAILED":
            details.append("Authentication failed (do not retry with the same credentials).")
        if close_code is not None:
            details.append(f"Close code: {close_code}")
        if core_status is not None:
            details.append(f"Core HTTP status: {core_status}")
        if retry_after is not None:
            details.append(f"Retry after: {retry_after}s")

        banner.update(" — ".join(details))

    def _render_main_screen(self) -> str:
        if self.display_mode == "prompt" and self.active_prompt is not None:
            return self._render_prompt(self.active_prompt)
        if self.display_mode == "job" and self.active_job_id and self.active_job_id in self.jobs:
            return self._render_job(self.jobs[self.active_job_id])
        if self.current_view is not None:
            return self._render_view(self.current_view)
        if self.menu_tree is not None:
            return "\n".join(escape(line) for line in self.menu_tree.render_tree_lines())
        if self.last_error is not None:
            code = escape(str(self.last_error.get("code", "ERROR")))
            msg = escape(str(self.last_error.get("message", "")))
            return f"[bold red]{code}[/bold red]: {msg}"
        return "Connecting to wlanpi-menud..."

    def _render_view(self, view: dict[str, Any]) -> str:
        lines: list[str] = []
        title = view.get("title")
        screen = view.get("screen", "menu")
        page_info = view.get("page")

        header_bits: list[str] = []
        if title:
            header_bits.append(f"[bold]{escape(str(title))}[/bold]")
        else:
            header_bits.append(f"[bold]{escape(str(screen).upper())}[/bold]")

        if isinstance(page_info, dict):
            idx = int(page_info.get("index", 0)) + 1
            count = page_info.get("count")
            more = page_info.get("more")
            if count:
                suffix = "+" if more else ""
                header_bits.append(f"[dim](page {idx}/{count}{suffix})[/dim]")

        lines.append("  ".join(header_bits))
        lines.append("")

        blocks = list(view.get("blocks", []))
        lines.append(
            render_blocks(
                blocks,
                supported_blocks=self.supported_blocks,
                menu_tree=self.menu_tree,
                confirm_selection=self.confirm_selection,
                max_width=max(40, (self.size.width or 80) - 6),
            )
        )
        return "\n".join(lines)

    def _render_job(self, job: JobState) -> str:
        lines: list[str] = []
        shared_badge = " [dim]\\[shared][/dim]" if job.shared else ""
        if job.state == "queued":
            lines.append(f"[bold]Job {escape(job.job_id)}[/bold] ({escape(job.node)}){shared_badge}")
            lines.append("")
            lines.append("  [cyan]\\[QUEUED][/cyan] Waiting to run...")
            return "\n".join(lines)

        if job.state == "progress":
            lines.append(f"[bold]Job {escape(job.job_id)}[/bold] ({escape(job.node)}){shared_badge}")
            lines.append("")
            prog_block = {
                "type": "progress",
                "pct": job.progress_pct,
                "label": job.progress_message or "Running",
                "text": job.progress_message or "Running...",
            }
            lines.append(
                render_blocks(
                    [prog_block],
                    supported_blocks=self.supported_blocks,
                    menu_tree=self.menu_tree,
                )
            )
            return "\n".join(lines)

        if job.state == "error":
            lines.append(f"[bold red]Job Failed ({escape(job.node)})[/bold red]")
            lines.append("")
            code = escape(str(job.error_code or "ERROR"))
            msg = escape(str(job.error_message or "Job failed"))
            status_str = f" (HTTP {job.error_core_status})" if job.error_core_status else ""
            lines.append(f"  [bold red]\\[{code}]{status_str}[/bold red] {msg}")
            return "\n".join(lines)

        if job.state == "cancelled":
            lines.append(f"[bold yellow]Job Cancelled ({escape(job.node)})[/bold yellow]")
            lines.append("")
            lines.append(f"  Job {escape(job.job_id)} was cancelled.")
            return "\n".join(lines)

        # state == "result"
        page = job.current_page
        if page is None:
            return f"[bold]Job {escape(job.job_id)}[/bold]: No result blocks"

        title = page.title or job.node
        meta_parts: list[str] = [f"page {page.page_number}"]
        if page.total is not None:
            meta_parts.append(f"total {page.total}")
        if page.cursor:
            meta_parts.append("↓ more")
        if job.current_idx > 0:
            meta_parts.append("↑ prev")
        meta = f" [dim]({', '.join(meta_parts)})[/dim]"

        lines.append(f"[bold]{escape(str(title))}[/bold]{shared_badge}{meta}")
        lines.append("")
        lines.append(
            render_blocks(
                page.blocks,
                supported_blocks=self.supported_blocks,
                menu_tree=self.menu_tree,
                confirm_selection=self.confirm_selection,
                max_width=max(40, (self.size.width or 80) - 6),
            )
        )
        return "\n".join(lines)

    def _render_prompt(self, prompt: dict[str, Any]) -> str:
        kind = str(prompt.get("kind", ""))
        title = str(prompt.get("title", "Prompt"))
        message = prompt.get("message")
        lines: list[str] = [f"[bold cyan]Prompt: {escape(title)}[/bold cyan]"]
        if message:
            lines.append(f"  {escape(str(message))}")
        lines.append("")

        if kind == "select":
            options = prompt.get("options") or []
            for idx, opt in enumerate(options):
                label = str(opt.get("label", ""))
                detail = opt.get("detail")
                detail_str = f" [dim]({escape(str(detail))})[/dim]" if detail else ""
                if idx == self.prompt_cursor:
                    lines.append(f"[bold reverse] > {escape(label)} [/bold reverse]{detail_str}")
                else:
                    lines.append(f"   {escape(label)}{detail_str}")
            lines.append("")
            lines.append("[dim]↑/↓ select · Enter confirm · ←/Esc cancel[/dim]")

        elif kind == "confirm":
            confirm_label = str(prompt.get("confirm_label") or "Confirm")
            cancel_label = str(prompt.get("cancel_label") or "Cancel")
            if self.prompt_confirm_choice == "confirm":
                c_btn = f"[bold reverse green] \\[{escape(confirm_label)}] [/bold reverse green]"
                x_btn = f"  \\[{escape(cancel_label)}] "
            else:
                c_btn = f"  \\[{escape(confirm_label)}] "
                x_btn = f"[bold reverse yellow] \\[{escape(cancel_label)}] [/bold reverse yellow]"
            lines.append(f"  {c_btn}   {x_btn}")
            lines.append("")
            lines.append("[dim]←/→ toggle · Enter submit · y/n shortcut · Esc cancel[/dim]")

        elif kind in ("integer", "string"):
            bounds = ""
            if kind == "integer":
                mn, mx = prompt.get("minimum"), prompt.get("maximum")
                if mn is not None or mx is not None:
                    bounds = f" [dim]({mn if mn is not None else '-∞'}..{mx if mx is not None else '∞'})[/dim]"
            lines.append(f"  Value{bounds}: [bold reverse] {escape(self.prompt_scalar_value)} [/bold reverse]")
            lines.append("")
            lines.append("[dim]Type value (↑/↓ step integer) · Enter submit · Esc cancel[/dim]")

        elif kind == "form":
            fields = prompt.get("fields") or []
            for idx, fdef in enumerate(fields):
                fname = str(fdef.get("name", ""))
                flabel = str(fdef.get("label", fname))
                ftype = str(fdef.get("type", "string"))
                fval = self.prompt_form_values.get(fname, "")
                val_str = self._format_form_field_value(fdef, fval)
                focused = idx == self.prompt_field_idx
                prefix = "> " if focused else "  "
                if focused:
                    lines.append(
                        f"[bold reverse] {prefix}{escape(flabel)} ({escape(ftype)}): {escape(val_str)} [/bold reverse]"
                    )
                else:
                    lines.append(f"  {prefix}{escape(flabel)} ({escape(ftype)}): {escape(val_str)}")
            lines.append("")
            lines.append("[dim]↑/↓ field · ←/→ change choice/value · Enter submit · Esc cancel[/dim]")

        return "\n".join(lines)

    def _format_form_field_value(self, fdef: dict[str, Any], val: Any) -> str:
        if bool(fdef.get("secret", False)) and val:
            return "•" * len(str(val))
        if fdef.get("type") == "enum":
            for choice in fdef.get("choices") or []:
                if choice.get("value") == val:
                    return f"{choice.get('label', val)} ({val})"
        if isinstance(val, dict) and "iface" in val:
            ns = val.get("namespace")
            return f"{val['iface']} ({ns or 'root'})"
        return str(val)

    def _render_menu_tree_section(self) -> str:
        if self.menu_tree is None:
            return ""
        selected_node = self.current_view.get("node") if self.current_view else None
        tree_lines = self.menu_tree.render_tree_lines(selected_node_id=selected_node)
        return "\n".join(escape(line) for line in tree_lines)

    def _render_status_footer(self) -> str:
        line1_parts: list[str] = []

        summary = self.topic_data.get("device.summary")
        if isinstance(summary, dict):
            for key in ("hostname", "mode", "eth0", "ip", "cpu_temp"):
                if key in summary and summary[key] is not None:
                    val = str(summary[key])
                    if key in ("hostname", "eth0", "ip", "cpu_temp"):
                        line1_parts.append(escape(val))
                    else:
                        line1_parts.append(f"{escape(key)}:{escape(val)}")

        complications = self.topic_data.get("complications")
        if isinstance(complications, list):
            for comp in complications:
                if isinstance(comp, dict):
                    # wlanpi-menud publishes {app_id, label, value, status}
                    label = comp.get("label") or comp.get("key")
                    value = comp.get("value")
                    txt = comp.get("text")
                    if label and value is not None and value != "":
                        line1_parts.append(f"[cyan]{escape(str(label))}: {escape(str(value))}[/cyan]")
                    elif txt:
                        line1_parts.append(f"[cyan]{escape(str(txt))}[/cyan]")

        alerts = self.topic_data.get("alerts")
        if isinstance(alerts, list) and alerts:
            for alert in alerts:
                if isinstance(alert, dict):
                    msg = alert.get("message") or alert.get("text") or str(alert)
                else:
                    msg = str(alert)
                line1_parts.append(f"[bold red]! {escape(msg)}[/bold red]")

        if self.stale_topics:
            stale_names = ",".join(sorted(self.stale_topics))
            line1_parts.append(f"[bold yellow]\\[STALE: {escape(stale_names)}][/bold yellow]")

        status_line = "  │  ".join(line1_parts) if line1_parts else "[dim]Status: waiting for events[/dim]"

        hint_parts: list[str] = []
        if self.notice:
            hint_parts.append(f"[yellow]{escape(self.notice)}[/yellow]")
        if self.current_view and isinstance(self.current_view.get("hints"), dict):
            for k, v in self.current_view["hints"].items():
                hint_parts.append(f"[bold]{escape(str(k))}[/bold]:{escape(str(v))}")
        hint_parts.append("[dim]↑↓←→/Enter nav · F1-F3 key1-3 · q quit[/dim]")
        hints_line = "  ".join(hint_parts)

        return f"{status_line}\n{hints_line}"

    # ------------------------------------------------------------------
    # Key handling (prompts, inline confirm, job paging, thin input)
    # ------------------------------------------------------------------

    def _find_inline_confirm_block(self) -> dict[str, Any] | None:
        blocks: list[dict[str, Any]] = []
        if self.display_mode == "job" and self.active_job_id in self.jobs:
            page = self.jobs[self.active_job_id].current_page
            if page:
                blocks = page.blocks
        elif self.display_mode == "view" and self.current_view:
            blocks = list(self.current_view.get("blocks", []))

        for block in blocks:
            if block.get("type") == "confirm" and block.get("prompt_id"):
                return block
        return None

    def _cancellable_job(self) -> JobState | None:
        """Return the active job if it is queued/running and on screen."""
        job = self.jobs.get(self.active_job_id) if self.active_job_id else None
        if job is None or job.state not in ("queued", "progress"):
            return None
        if self.display_mode == "job":
            return job
        view = self.current_view
        if (
            self.display_mode == "view"
            and view is not None
            and view.get("screen") in ("busy", "result")
            and view.get("job_id", job.job_id) == job.job_id
        ):
            return job
        return None

    async def on_key(self, event: events.Key) -> None:
        key = event.key

        if key == "q" and self.active_prompt is None:
            self.exit()
            return

        # 1. Active server prompt frame
        if self.display_mode == "prompt" and self.active_prompt is not None:
            event.stop()
            await self._handle_prompt_key(event)
            return

        # 2. Default session: a prompt shown on the shared screen.
        if (
            self.attached
            and self.display_mode == "view"
            and self.current_view is not None
            and self.current_view.get("screen") == "prompt"
            and await self._handle_shared_prompt_key(event)
        ):
            return

        # 3. Inline confirm block (in view or job result)
        confirm_block = self._find_inline_confirm_block()
        if confirm_block is not None:
            pid = str(confirm_block["prompt_id"])
            cur = self.confirm_selection.get(pid, str(confirm_block.get("selected") or "cancel"))
            if key in ("left", "right"):
                event.stop()
                self.confirm_selection[pid] = "confirm" if cur == "cancel" else "cancel"
                self._refresh_ui()
                return
            if key == "y":
                event.stop()
                self.confirm_selection[pid] = "confirm"
                await self._send_frame(build_answer(self._next_id(), pid, confirmed=True))
                self._refresh_ui()
                return
            if key == "n":
                event.stop()
                self.confirm_selection[pid] = "cancel"
                await self._send_frame(build_answer(self._next_id(), pid, confirmed=False))
                self._refresh_ui()
                return
            if key == "enter":
                event.stop()
                await self._send_frame(
                    build_answer(self._next_id(), pid, confirmed=(cur == "confirm"))
                )
                return

        # 4. Cancel an in-flight job with c/Escape, whether it is shown from
        #    job frames or from the server's thin-mode busy view.
        if key in ("escape", "c"):
            job_to_cancel = self._cancellable_job()
            if job_to_cancel is not None:
                event.stop()
                await self._send_frame(build_cancel(self._next_id(), job_to_cancel.job_id))
                return

        # 5. Job result up/down paging via page frames (only when the job is
        #    rendered from job frames; view-driven jobs page via thin input).
        if self.display_mode == "job" and self.active_job_id and self.active_job_id in self.jobs:
            job = self.jobs[self.active_job_id]
            if job.state == "result":
                if key == "down":
                    event.stop()
                    moved, cursor = job.step_down_or_cursor()
                    if moved:
                        self._refresh_ui()
                        return
                    if cursor:
                        req_id = self._next_id()
                        self._pending_page_req_ids.add(req_id)
                        job.pending_cursor = cursor
                        await self._send_frame(build_page(req_id, job.job_id, cursor))
                        return
                elif key == "up":
                    event.stop()
                    if job.step_up():
                        self._refresh_ui()
                        return

        # 6. Thin-mode keyboard to input frame mapping
        mapped = THIN_KEY_MAP.get(key)
        if mapped is not None:
            event.stop()
            await self._send_frame(build_input(self._next_id(), mapped))

    async def _handle_shared_prompt_key(self, event: events.Key) -> bool:
        """Keys on a default-session prompt screen. Returns True when handled.

        The prompt is answered through the shared cursor (input frames), so
        every attached surface sees the same highlight. A confirmation belongs
        to the principal that invoked it: when this connection may not answer
        (view.answerable is false) keys only explain why, and c/Escape do not
        cancel it.
        """
        view = self.current_view or {}
        key = event.key
        if view.get("answerable") is False:
            if key in THIN_KEY_MAP or key in ("escape", "c", "y", "n"):
                event.stop()
                self.notice = f"Only {self._shared_prompt_invoker()} can answer this confirmation"
                self._refresh_ui()
                return True
            return False
        if key in ("escape", "c"):
            event.stop()
            await self._send_frame(build_input(self._next_id(), "left"))  # cancel on the shared screen
            return True
        confirm = next((b for b in view.get("blocks", []) if b.get("type") == "confirm"), None)
        if confirm is not None:
            pid = str(confirm.get("prompt_id", ""))
            if key in ("left", "right"):
                event.stop()
                await self._send_frame(build_input(self._next_id(), "down"))  # toggle the shared highlight
                return True
            if key in ("enter", "space"):
                event.stop()
                await self._send_frame(build_input(self._next_id(), "center"))  # answer the shared highlight
                return True
            if key in ("y", "n") and pid:
                event.stop()
                await self._send_frame(build_answer(self._next_id(), pid, confirmed=key == "y"))
                return True
        return False

    async def _handle_prompt_key(self, event: events.Key) -> None:
        prompt = self.active_prompt
        if prompt is None:
            return

        pid = str(prompt["prompt_id"])
        kind = str(prompt.get("kind", ""))
        key = event.key

        if key == "escape":
            self.active_prompt = None
            self.display_mode = "view" if self.current_view else "job"
            await self._send_frame(build_answer(self._next_id(), pid, cancel=True))
            self._refresh_ui()
            return

        if kind == "select":
            options = prompt.get("options") or []
            if key == "up" and options:
                self.prompt_cursor = max(0, self.prompt_cursor - 1)
                self._refresh_ui()
            elif key == "down" and options:
                self.prompt_cursor = min(len(options) - 1, self.prompt_cursor + 1)
                self._refresh_ui()
            elif key == "left":
                self.active_prompt = None
                self.display_mode = "view" if self.current_view else "job"
                await self._send_frame(build_answer(self._next_id(), pid, cancel=True))
                self._refresh_ui()
            elif key in ("enter", "right") and options:
                chosen = options[self.prompt_cursor]["value"]
                self.active_prompt = None
                self.display_mode = "view" if self.current_view else "job"
                await self._send_frame(build_answer(self._next_id(), pid, value=chosen))
                self._refresh_ui()

        elif kind == "confirm":
            if key in ("left", "right", "up", "down"):
                self.prompt_confirm_choice = (
                    "cancel" if self.prompt_confirm_choice == "confirm" else "confirm"
                )
                self._refresh_ui()
            elif key == "y":
                self.active_prompt = None
                self.display_mode = "view" if self.current_view else "job"
                await self._send_frame(build_answer(self._next_id(), pid, confirmed=True))
                self._refresh_ui()
            elif key == "n":
                self.active_prompt = None
                self.display_mode = "view" if self.current_view else "job"
                await self._send_frame(build_answer(self._next_id(), pid, confirmed=False))
                self._refresh_ui()
            elif key == "enter":
                confirmed = self.prompt_confirm_choice == "confirm"
                self.active_prompt = None
                self.display_mode = "view" if self.current_view else "job"
                await self._send_frame(build_answer(self._next_id(), pid, confirmed=confirmed))
                self._refresh_ui()

        elif kind == "integer":
            if key in ("up", "right"):
                cur = int(self.prompt_scalar_value or "0")
                nxt = cur + 1
                if prompt.get("maximum") is not None:
                    nxt = min(int(prompt["maximum"]), nxt)
                self.prompt_scalar_value = str(nxt)
                self._refresh_ui()
            elif key in ("down", "left"):
                cur = int(self.prompt_scalar_value or "0")
                nxt = cur - 1
                if prompt.get("minimum") is not None:
                    nxt = max(int(prompt["minimum"]), nxt)
                self.prompt_scalar_value = str(nxt)
                self._refresh_ui()
            elif key == "backspace":
                self.prompt_scalar_value = self.prompt_scalar_value[:-1]
                self._refresh_ui()
            elif key == "enter":
                val = int(self.prompt_scalar_value or "0")
                self.active_prompt = None
                self.display_mode = "view" if self.current_view else "job"
                await self._send_frame(build_answer(self._next_id(), pid, value=val))
                self._refresh_ui()
            elif event.character and (event.character.isdigit() or event.character == "-"):
                self.prompt_scalar_value += event.character
                self._refresh_ui()

        elif kind == "string":
            if key == "backspace":
                self.prompt_scalar_value = self.prompt_scalar_value[:-1]
                self._refresh_ui()
            elif key == "enter":
                val = self.prompt_scalar_value
                self.active_prompt = None
                self.display_mode = "view" if self.current_view else "job"
                await self._send_frame(build_answer(self._next_id(), pid, value=val))
                self._refresh_ui()
            elif event.character and event.is_printable:
                self.prompt_scalar_value += event.character
                self._refresh_ui()

        elif kind == "form":
            fields = prompt.get("fields") or []
            if not fields:
                return
            if key == "up":
                if len(fields) > 1:
                    self.prompt_field_idx = max(0, self.prompt_field_idx - 1)
                else:
                    self._step_form_field(fields[0], -1)
                self._refresh_ui()
            elif key == "down":
                if len(fields) > 1:
                    self.prompt_field_idx = min(len(fields) - 1, self.prompt_field_idx + 1)
                else:
                    self._step_form_field(fields[0], 1)
                self._refresh_ui()
            elif key == "left":
                self._step_form_field(fields[self.prompt_field_idx], -1)
                self._refresh_ui()
            elif key == "right":
                self._step_form_field(fields[self.prompt_field_idx], 1)
                self._refresh_ui()
            elif key == "backspace":
                fdef = fields[self.prompt_field_idx]
                fname = str(fdef["name"])
                if fdef.get("type") in ("string", "integer"):
                    cur_s = str(self.prompt_form_values.get(fname, ""))[:-1]
                    self.prompt_form_values[fname] = (
                        int(cur_s) if (fdef.get("type") == "integer" and cur_s.lstrip("-").isdigit()) else cur_s
                    )
                    self._refresh_ui()
            elif key == "enter":
                values = dict(self.prompt_form_values)
                self.active_prompt = None
                self.display_mode = "view" if self.current_view else "job"
                await self._send_frame(build_answer(self._next_id(), pid, values=values))
                self._refresh_ui()
            elif event.character and event.is_printable:
                fdef = fields[self.prompt_field_idx]
                fname = str(fdef["name"])
                ftype = fdef.get("type")
                if ftype == "string":
                    self.prompt_form_values[fname] = str(self.prompt_form_values.get(fname, "")) + event.character
                    self._refresh_ui()
                elif ftype == "integer" and (event.character.isdigit() or event.character == "-"):
                    raw_s = str(self.prompt_form_values.get(fname, "")) + event.character
                    if raw_s.lstrip("-").isdigit():
                        self.prompt_form_values[fname] = int(raw_s)
                    self._refresh_ui()

    def _step_form_field(self, fdef: dict[str, Any], delta: int) -> None:
        fname = str(fdef["name"])
        ftype = fdef.get("type")
        if ftype in ("enum", "adapter"):
            choices = fdef.get("choices") or []
            if not choices:
                return
            vals = [c["value"] for c in choices]
            cur = self.prompt_form_values.get(fname)
            try:
                idx = vals.index(cur)
            except ValueError:
                idx = 0
            self.prompt_form_values[fname] = vals[(idx + delta) % len(vals)]
        elif ftype == "integer":
            step = int(fdef.get("step", 1))
            cur = int(self.prompt_form_values.get(fname, 0) or 0)
            nxt = cur + delta * step
            if fdef.get("minimum") is not None:
                nxt = max(int(fdef["minimum"]), nxt)
            if fdef.get("maximum") is not None:
                nxt = min(int(fdef["maximum"]), nxt)
            self.prompt_form_values[fname] = nxt

    async def invoke_node(
        self,
        node: str,
        *,
        params: dict[str, Any] | None = None,
        confirmed: bool = False,
    ) -> None:
        """Send an invoke frame for a shortcut node."""
        await self._send_frame(
            build_invoke(self._next_id(), node, params=params, confirmed=confirmed)
        )
