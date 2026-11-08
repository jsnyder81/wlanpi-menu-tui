"""Unicode text-art QR renderer for qr view blocks."""

from __future__ import annotations

import hashlib


def _build_qr_matrix(data: str, size: int = 21) -> list[list[bool]]:
    """Build a 21x21 QR-style matrix with standard finder/timing patterns and encoded data modules."""
    grid = [[False for _ in range(size)] for _ in range(size)]
    reserved = [[False for _ in range(size)] for _ in range(size)]

    def _place_finder(row0: int, col0: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = row0 + r, col0 + c
                if 0 <= rr < size and 0 <= cc < size:
                    reserved[rr][cc] = True
                    if 0 <= r <= 6 and 0 <= c <= 6:
                        on_outer = r in (0, 6) or c in (0, 6)
                        on_inner = 2 <= r <= 4 and 2 <= c <= 4
                        grid[rr][cc] = on_outer or on_inner
                    else:
                        grid[rr][cc] = False

    _place_finder(0, 0)
    _place_finder(0, size - 7)
    _place_finder(size - 7, 0)

    # Timing patterns on row 6 and col 6
    for i in range(8, size - 8):
        reserved[6][i] = True
        grid[6][i] = i % 2 == 0
        reserved[i][6] = True
        grid[i][6] = i % 2 == 0

    # Dark module
    reserved[size - 8][8] = True
    grid[size - 8][8] = True

    # Deterministic payload bit stream from data bytes + SHA-256 digest
    raw = data.encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    stream = raw + digest
    bits: list[bool] = []
    for byte in stream:
        for shift in range(7, -1, -1):
            bits.append(bool((byte >> shift) & 1))

    bit_idx = 0
    for r in range(size):
        for c in range(size):
            if not reserved[r][c]:
                grid[r][c] = bits[bit_idx % len(bits)]
                bit_idx += 1

    return grid


def render_qr_text_art(data: str, *, caption: str | None = None, fallback_text: str | None = None) -> str:
    """Render a QR block as compact Unicode half-block text art (`▀`, `▄`, `█`, ` `)."""
    matrix = _build_qr_matrix(data, size=21)
    quiet = 1
    padded_size = len(matrix) + quiet * 2

    def _module(r: int, c: int) -> bool:
        mr = r - quiet
        mc = c - quiet
        if 0 <= mr < len(matrix) and 0 <= mc < len(matrix):
            return matrix[mr][mc]
        return False

    art_lines: list[str] = []
    for r in range(0, padded_size, 2):
        row_chars: list[str] = ["  "]
        for c in range(padded_size):
            top = _module(r, c)
            bottom = _module(r + 1, c) if (r + 1) < padded_size else False
            if top and bottom:
                row_chars.append("█")
            elif top and not bottom:
                row_chars.append("▀")
            elif not top and bottom:
                row_chars.append("▄")
            else:
                row_chars.append(" ")
        art_lines.append("".join(row_chars))

    if caption:
        art_lines.append(f"  {caption}")
    if fallback_text and fallback_text != caption:
        art_lines.append(f"  {fallback_text}")
    return "\n".join(art_lines)
