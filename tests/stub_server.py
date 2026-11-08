"""In-process Unix-socket WebSocket stub server with JSON Schema validation."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

from jsonschema import Draft202012Validator
from websockets.asyncio.server import ServerConnection, unix_serve
from websockets.exceptions import WebSocketException

log = logging.getLogger(__name__)

PROTOCOL_DIR = (
    Path(__file__).resolve().parents[2] / "wlanpi-menud" / "protocol"
)
FRAMES_SCHEMA_PATH = PROTOCOL_DIR / "frames.schema.json"
FRAMES_SCHEMA = json.loads(FRAMES_SCHEMA_PATH.read_text(encoding="utf-8"))
FRAME_VALIDATOR = Draft202012Validator(FRAMES_SCHEMA)

C2S_TYPES = {
    "auth",
    "hello",
    "subscribe",
    "unsubscribe",
    "input",
    "invoke",
    "answer",
    "page",
    "cancel",
}
S2C_TYPES = {
    "welcome",
    "menu",
    "view",
    "prompt",
    "job",
    "event",
    "error",
}


def validate_frame(frame: dict[str, Any], *, direction: str | None = None) -> None:
    """Validate a frame against protocol/frames.schema.json."""
    errors = list(FRAME_VALIDATOR.iter_errors(frame))
    if errors:
        msgs = "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
        raise AssertionError(f"Frame failed schema validation: {msgs}\nFrame: {frame!r}")
    if direction == "c2s":
        assert frame["type"] in C2S_TYPES, f"Unexpected C2S frame type: {frame['type']}"
    elif direction == "s2c":
        assert frame["type"] in S2C_TYPES, f"Unexpected S2C frame type: {frame['type']}"


DEFAULT_MENU_NODES: list[dict[str, Any]] = [
    {
        "id": "network",
        "label": "Network",
        "parent": None,
        "order": 10,
        "kind": "branch",
    },
    {
        "id": "network.interfaces",
        "label": "Interfaces",
        "parent": "network",
        "order": 10,
        "kind": "action",
        "view": "kv",
        "mutates": False,
    },
    {
        "id": "apps",
        "label": "Apps",
        "parent": None,
        "order": 20,
        "kind": "branch",
    },
    {
        "id": "apps.scanner.scan",
        "label": "Wi-Fi Scan",
        "parent": "apps",
        "order": 10,
        "kind": "action",
        "view": "table",
        "mutates": False,
    },
    {
        "id": "system",
        "label": "System",
        "parent": None,
        "order": 30,
        "kind": "branch",
    },
    {
        "id": "system.reboot",
        "label": "Reboot",
        "parent": "system",
        "order": 10,
        "kind": "action",
        "view": "status",
        "mutates": True,
        "confirm": {
            "title": "Reboot",
            "message": "Reboot the WLAN Pi now?",
        },
    },
]


class StubMenudServer:
    """In-process Unix socket WebSocket server validating every frame against frames.schema.json."""

    def __init__(
        self,
        socket_path: Path | str,
        *,
        epoch: str = "ep-initial-1234",
        session_id: str = "s-stub-01",
    ) -> None:
        self.socket_path = str(socket_path)
        self.epoch = epoch
        self.session_id = session_id
        self.menu_revision = 1
        self.device_mode = "classic"
        self.menu_nodes = list(DEFAULT_MENU_NODES)

        self.received_frames: list[dict[str, Any]] = []
        self.sent_frames: list[dict[str, Any]] = []
        self._frame_event = asyncio.Event()
        self._server: Any = None
        self._active_ws: ServerConnection | None = None
        self._server_seq = 0

        # Configurable handshake & topic state
        self.handshake_error: dict[str, Any] | None = None
        self.force_resnapshot: bool = False
        self.send_initial_view: bool = True
        self.initial_view: dict[str, Any] | None = None
        self.topic_seq: dict[str, int] = {
            "device.summary": 1,
            "complications": 1,
            "alerts": 1,
        }
        self.topic_snapshots: dict[str, Any] = {
            "device.summary": {
                "hostname": "wlanpi-stub",
                "mode": "classic",
                "eth0": "192.0.2.10",
                "cpu_temp": "46.5C",
            },
            "complications": [
                {
                    "provider": "acme",
                    "key": "uplink",
                    "text": "Uplink OK",
                    "ttl_sec": 30,
                }
            ],
            "alerts": ["Low battery 15%"],
        }
        self.resnapshot_jobs: list[dict[str, Any]] = []
        self.page_handlers: dict[tuple[str, str], dict[str, Any]] = {}
        self.on_client_frame: Callable[[StubMenudServer, dict[str, Any]], Any] | None = None

    def next_server_id(self) -> str:
        self._server_seq += 1
        return f"s:{self._server_seq}"

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.stop()

    async def start(self) -> None:
        p = Path(self.socket_path)
        if p.exists():
            p.unlink()
        self._server = await unix_serve(self._handle_client, path=self.socket_path)

    async def stop(self) -> None:
        if self._active_ws is not None:
            try:
                await self._active_ws.close(1000, "Server stopping")
            except (OSError, WebSocketException) as exc:
                log.debug("Ignoring close error on stop: %s", exc)
            self._active_ws = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def disconnect_client(self, code: int = 1001, reason: str = "Restarting") -> None:
        """Close the current client WebSocket to trigger a client reconnect."""
        ws = self._active_ws
        if ws is not None:
            self._active_ws = None
            await ws.close(code, reason)

    async def send_frame(
        self,
        frame: dict[str, Any],
        *,
        validate: bool = True,
    ) -> None:
        """Validate (by default) and send an S2C frame to the connected client."""
        if validate:
            validate_frame(frame, direction="s2c")
        ws = self._active_ws
        if ws is None:
            raise RuntimeError("No active client connection on stub server")
        self.sent_frames.append(frame)
        await ws.send(json.dumps(frame))

    async def wait_for_frame(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 3.0,
        start_index: int = 0,
    ) -> dict[str, Any]:
        """Wait until a received C2S frame matching predicate arrives."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        idx = start_index
        while True:
            while idx < len(self.received_frames):
                frame = self.received_frames[idx]
                idx += 1
                if predicate(frame):
                    return frame
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for C2S frame. Received so far: {self.received_frames!r}"
                )
            self._frame_event.clear()
            try:
                await asyncio.wait_for(self._frame_event.wait(), timeout=remaining)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Timed out waiting for C2S frame. Received so far: {self.received_frames!r}"
                ) from exc

    async def _handle_client(self, ws: ServerConnection) -> None:
        self._active_ws = ws
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                frame = json.loads(raw)
                validate_frame(frame, direction="c2s")
                self.received_frames.append(frame)
                self._frame_event.set()

                await self._process_c2s_frame(ws, frame)
        except (OSError, WebSocketException) as exc:
            log.debug("Client connection ended: %s", exc)
        finally:
            if self._active_ws is ws:
                self._active_ws = None

    async def _process_c2s_frame(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        ftype = frame["type"]
        req_id = frame["id"]

        if ftype == "hello":
            if self.handshake_error is not None:
                err_frame = dict(self.handshake_error)
                err_frame["id"] = req_id
                await self.send_frame(err_frame)
                close_code = int(err_frame.get("close_code", 4400))
                await ws.close(close_code, err_frame.get("message", "Fatal error"))
                return

            resume = frame.get("resume")
            resumed = False
            if isinstance(resume, dict) and not self.force_resnapshot:
                r_epoch = resume.get("epoch")
                r_seq = resume.get("seq") or {}
                if r_epoch == self.epoch and all(
                    r_seq.get(t) == s for t, s in self.topic_seq.items() if t in r_seq
                ):
                    resumed = True

            welcome = {
                "v": 1,
                "type": "welcome",
                "id": req_id,
                "session_id": self.session_id,
                "epoch": self.epoch,
                "principal": {
                    "kind": "local",
                    "name": "wlanpi",
                    "uid": 1000,
                    "did": "wlanpi-menud",
                },
                "roles": ["admin"],
                "version": 1,
                "mode": "thin",
                "resumed": resumed,
                "limits": {
                    "queue_frames": 64,
                    "max_frame_bytes": 262144,
                },
            }
            await self.send_frame(welcome)

            if not resumed:
                menu_frame = {
                    "v": 1,
                    "type": "menu",
                    "id": self.next_server_id(),
                    "revision": self.menu_revision,
                    "device_mode": self.device_mode,
                    "nodes": self.menu_nodes,
                }
                await self.send_frame(menu_frame)

                # If reconnecting with resume but not resumed, push topic snapshots + live jobs
                if isinstance(resume, dict):
                    for topic, data in self.topic_snapshots.items():
                        await self.send_frame(
                            {
                                "v": 1,
                                "type": "event",
                                "id": self.next_server_id(),
                                "topic": topic,
                                "seq": self.topic_seq.get(topic, 1),
                                "snapshot": True,
                                "data": data,
                            }
                        )
                    for job_frame in self.resnapshot_jobs:
                        jf = dict(job_frame)
                        jf["id"] = self.next_server_id()
                        await self.send_frame(jf)

                if self.send_initial_view:
                    view = self.initial_view or {
                        "v": 1,
                        "type": "view",
                        "id": self.next_server_id(),
                        "screen": "menu",
                        "node": "network",
                        "title": "Main Menu",
                        "breadcrumb": ["Main Menu"],
                        "blocks": [
                            {
                                "type": "list",
                                "selected": 0,
                                "text": "> Network\n  Apps\n  System",
                                "items": [
                                    {"label": "Network", "node": "network", "branch": True},
                                    {"label": "Apps", "node": "apps", "branch": True},
                                    {"label": "System", "node": "system", "branch": True},
                                ],
                            }
                        ],
                        "hints": {
                            "left": "Back",
                            "center": "Open",
                        },
                    }
                    await self.send_frame(view)

        elif ftype == "subscribe":
            for topic in frame.get("topics", []):
                if topic in self.topic_snapshots:
                    await self.send_frame(
                        {
                            "v": 1,
                            "type": "event",
                            "id": req_id,
                            "topic": topic,
                            "seq": self.topic_seq.get(topic, 1),
                            "snapshot": True,
                            "data": self.topic_snapshots[topic],
                        }
                    )

        elif ftype == "page":
            key = (frame["job_id"], frame["cursor"])
            if key in self.page_handlers:
                reply = dict(self.page_handlers[key])
                reply["id"] = req_id
                await self.send_frame(reply)

        if self.on_client_frame is not None:
            res = self.on_client_frame(self, frame)
            if asyncio.iscoroutine(res):
                await res
