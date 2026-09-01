#!/usr/bin/env python3
"""Build xout's deterministic, dependency-free social card."""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

W, H = 1200, 630
PAPER = (247, 243, 234)
INK = (23, 23, 23)
CRIMSON = (217, 35, 50)
MUTED = (110, 106, 99)
SURFACE = (217, 212, 201)
PALETTE = (PAPER, INK, CRIMSON, MUTED, SURFACE)
COLOR_INDEX = {color: index for index, color in enumerate(PALETTE)}

FONT = {
    "A": "01110 10001 10001 11111 10001 10001 10001",
    "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01111 10000 10000 10000 10000 10000 01111",
    "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111",
    "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01111 10000 10000 10111 10001 10001 01111",
    "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "11111 00100 00100 00100 00100 00100 11111",
    "J": "00111 00010 00010 00010 10010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 11001 10101 10011 10001 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110",
    "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 11011 10001",
    "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100",
    "Z": "11111 00001 00010 00100 01000 10000 11111",
    "0": "01110 10001 10011 10101 11001 10001 01110",
    "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11110 00001 00001 01110 00001 00001 11110",
    "4": "00010 00110 01010 10010 11111 00010 00010",
    "5": "11111 10000 10000 11110 00001 00001 11110",
    "6": "01110 10000 10000 11110 10001 10001 01110",
    "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00001 01110",
    ",": "00000 00000 00000 00000 00000 00110 00100",
    "-": "00000 00000 00000 11111 00000 00000 00000",
    ".": "00000 00000 00000 00000 00000 00110 00110",
    ":": "00000 00110 00110 00000 00110 00110 00000",
    "!": "00100 00100 00100 00100 00100 00000 00100",
    "?": "01110 10001 00001 00010 00100 00000 00100",
    "→": "00100 00100 00100 11111 00100 01100 00110",
    " ": "00000 00000 00000 00000 00000 00000 00000",
}
FONT = {k: tuple(int(row, 2) for row in v.split()) for k, v in FONT.items()}


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def put(canvas: bytearray, x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= x < W and 0 <= y < H:
        canvas[y * W + x] = COLOR_INDEX[color]


def rect(
    canvas: bytearray, x: int, y: int, w: int, h: int, color: tuple[int, int, int]
) -> None:
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            put(canvas, xx, yy, color)


def text(
    canvas: bytearray,
    x: int,
    y: int,
    value: str,
    scale: int,
    color: tuple[int, int, int],
    gap: int = 1,
) -> None:
    cursor = x
    for char in value.upper():
        glyph = FONT.get(char, FONT[" "])
        for row, bits in enumerate(glyph):
            for col in range(5):
                if bits & (1 << (4 - col)):
                    rect(
                        canvas,
                        cursor + col * scale,
                        y + row * scale,
                        scale,
                        scale,
                        color,
                    )
        cursor += 6 * scale + gap


def deterministic_zlib_store(data: bytes) -> bytes:
    """Emit a portable zlib stream using only uncompressed DEFLATE blocks."""
    result = bytearray(b"\x78\x01")
    position = 0
    while position < len(data):
        chunk = data[position : position + 65_535]
        position += len(chunk)
        result.append(1 if position == len(data) else 0)
        result.extend(struct.pack("<HH", len(chunk), 0xFFFF ^ len(chunk)))
        result.extend(chunk)
    result.extend(struct.pack(">I", zlib.adler32(data) & 0xFFFFFFFF))
    return bytes(result)


def main() -> None:
    output = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(".github/assets/social-card.png")
    )
    canvas = bytearray([COLOR_INDEX[PAPER]]) * (W * H)
    # Concrete red-pen behavior test: cross out the bad transcript and retain the survivor.
    text(canvas, 64, 48, "XOUT", 8, INK, gap=3)
    text(canvas, 64, 112, "X OUT THE BEHAVIOR. KEEP THE RULE.", 3, MUTED, gap=2)
    text(canvas, 64, 166, "FIX THE BUG.", 7, INK, gap=2)
    rect(canvas, 64, 248, 470, 118, PAPER)
    rect(canvas, 64, 248, 470, 6, INK)
    text(canvas, 82, 272, "SHOULD I START?", 5, INK, gap=2)
    for i in range(0, 420, 4):
        rect(canvas, 78 + i, 266 + i // 5, 12, 12, CRIMSON)
    rect(canvas, 64, 392, 470, 118, SURFACE)
    rect(canvas, 64, 392, 470, 6, INK)
    text(canvas, 82, 416, "FIXED. TESTS PASS.", 4, INK, gap=2)
    text(canvas, 610, 248, "GENERATED RULE", 3, MUTED, gap=2)
    rect(canvas, 610, 280, 526, 170, PAPER)
    rect(canvas, 610, 280, 526, 7, INK)
    text(canvas, 636, 320, "ACT FIRST.", 7, INK, gap=2)
    text(canvas, 636, 390, "REPORT AFTER.", 6, INK, gap=2)
    text(canvas, 64, 560, "CROSS OUT THE WRONG BEHAVIOR. KEEP THE RULE.", 3, INK, gap=2)
    rows: list[bytes] = []
    for y in range(H):
        row = canvas[y * W : (y + 1) * W]
        packed = bytes((row[x] << 4) | row[x + 1] for x in range(0, W, 2))
        rows.append(b"\x00" + packed)
    raw = b"".join(rows)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 4, 3, 0, 0, 0))
        + png_chunk(b"PLTE", b"".join(bytes(color) for color in PALETTE))
        + png_chunk(b"IDAT", deterministic_zlib_store(raw))
        + png_chunk(b"IEND", b"")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)


if __name__ == "__main__":
    main()
