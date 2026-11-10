# wlanpi-menu-tui

Textual thin-mode surface client for `wlanpi-menud`.

Connects to `/run/wlanpi-menud/local.sock` (or `--socket <path>`) over a single
push WebSocket speaking the `wlanpi-menud` protocol v1.

## Sessions

```
wlanpi-menu-tui                  # attach to the shared default session (the default)
wlanpi-menu-tui --session new    # an independent session with its own cursor
```

- **Attached** (`--session default`, also `WLANPI_MENUD_SESSION`): the TUI
  shows the service's default session, the same screen as the panel, the WebUI
  mirror and every other attached TUI. It renders each `view` as it arrives,
  whoever drove it, and the header shows `shared` and `driven by <surface>
  (<principal>)` when another surface made the last move. Keys drive the one
  shared cursor (last write wins). Select, confirm and integer prompts are
  answered on the shared screen, so every surface sees the same highlight.
  A confirmation another principal started is shown but not answerable: keys,
  including `c`/Escape, only explain who can answer it.
- **Independent** (`--session new`): a private cursor, prompts and jobs, as
  before.

## Features

- **Thin-mode handshake**: Sends `hello` (`mode="thin"`, plus `session: "new"`
  for an independent session) with screen, colour,
  block and key capabilities, plus `resume` state (`epoch`, `session_id`,
  per-topic `seq`, `menu_revision`) on reconnect.
- **View block rendering**: Renders `text`, `kv`, `table`, `list`, `qr` (as
  Unicode text-art), `status`, `progress`, and `confirm` blocks, falling back to
  each block's `text` representation for unknown block types.
- **Input mapping & keyboard shortcuts**: Maps arrow keys, Enter, and F1–F3/1–3
  to protocol `input` frames (`up`, `down`, `left`, `right`, `center`, `key1`–`key3`).
  `t` toggles the debug menu tree on/off (hidden by default).
  `q` quits the application.
  Inline confirmations support `left`/`right`, `y`/`n`, and Enter.
  Cancellable jobs support `c`/Escape.
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

## Installation

`wlanpi-menu-tui` ships as a Debian package built with dh-virtualenv. It is a
user-run client with no systemd unit.

```sh
sudo apt install ./wlanpi-menu-tui_<version>_arm64.deb
sudo usermod -aG wlanpi-menud "$USER"   # local.sock is 0660, group wlanpi-menud; log in again
wlanpi-menu-tui                          # or: wlanpi-menu-tui --socket <path>
```

| Path | What |
| --- | --- |
| `/opt/wlanpi-menu-tui/` | Private Python 3.13 virtualenv |
| `/usr/bin/wlanpi-menu-tui` | Link to the venv entry point |

## Building the Package

```sh
./build-package-native.sh        # Debian trixie container (podman or docker), runs lintian
ls dist/                         # wlanpi-menu-tui_<version>_<arch>.deb, lintian.txt
```

## Releases

Versions live in `debian/changelog` and match `pyproject.toml`. Add an entry
with `dch -i` (set `DEBFULLNAME` and `DEBEMAIL` first).
