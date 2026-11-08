# wlanpi-menu-tui

Textual thin-mode surface client for `wlanpi-menud`.

Connects to `/run/wlanpi-menud/local.sock` (or `--socket <path>`) over a single
push WebSocket speaking the `wlanpi-menud` protocol v1.

## Features

- **Thin-mode handshake**: Sends `hello` (`mode="thin"`) with screen, colour,
  block and key capabilities, plus `resume` state (`epoch`, `session_id`,
  per-topic `seq`, `menu_revision`) on reconnect.
- **View block rendering**: Renders `text`, `kv`, `table`, `list`, `qr` (as
  Unicode text-art), `status`, `progress`, and `confirm` blocks, falling back to
  each block's `text` representation for unknown block types.
- **Input mapping**: Maps arrow keys, Enter, and F1–F3/1–3 to protocol `input`
  frames (`up`, `down`, `left`, `right`, `center`, `key1`–`key3`).
- **Interactive prompts**: Handles server `prompt` frames (`select`, `confirm`,
  `form`, `integer`, `string`) and answers with `answer` frames.
- **Job lifecycle & paging**: Displays `queued`, `progress`, `result`, `error`,
  and `cancelled` job states; pages large results on demand with `up`/`down` via
  `page` frames while bounding cached result pages to 4.
- **Status footer**: Subscribes to `device.summary`, `alerts`, and
  `complications` topics and renders live status, complications, and alerts.
- **Reconnect & error handling**: Automatically reconnects on transient drops,
  accepts full resnapshots when `resumed` is `false`, detects sequence gaps, and
  presents readable diagnostics for protocol errors including `VERSION` and
  `AUTH_FAILED`.
