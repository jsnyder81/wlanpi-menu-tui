"""Command-line entry point for wlanpi-menu-tui."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence

from wlanpi_menu_tui import __version__
from wlanpi_menu_tui.app import MenuTuiApp
from wlanpi_menu_tui.protocol import DEFAULT_SOCKET_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wlanpi-menu-tui",
        description="Textual thin-mode TUI client for wlanpi-menud.",
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("WLANPI_MENUD_SOCKET", DEFAULT_SOCKET_PATH),
        help=f"Path to wlanpi-menud Unix domain socket (default: {DEFAULT_SOCKET_PATH})",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    args = build_parser().parse_args(argv)
    app = MenuTuiApp(socket_path=args.socket)
    app.run()
    return 0
