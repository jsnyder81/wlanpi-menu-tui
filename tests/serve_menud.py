#!/usr/bin/env python3
"""Run wlanpi-menud's own ``demo/serve_fake.py`` with the user layout file in
the temp socket directory instead of ``/var/lib/wlanpi-menud``.

The real service, the fake core and ``serve_fake`` are used unchanged; only
``Config(complications_user_file=...)`` is redirected so the Home tiles node
can save a layout without root. Run with wlanpi-menud's venv python:

    ../wlanpi-menud/.venv/bin/python tests/serve_menud.py /tmp/menud-dev
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

MENUD = Path(__file__).resolve().parents[2] / "wlanpi-menud"


def main() -> None:
    sockdir = Path(sys.argv[1])
    spec = importlib.util.spec_from_file_location("serve_fake", MENUD / "demo" / "serve_fake.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    real = mod.Config
    mod.Config = lambda **kw: real(complications_user_file=sockdir / "complications.json", **kw)
    asyncio.run(mod.main(sockdir))


if __name__ == "__main__":
    main()
