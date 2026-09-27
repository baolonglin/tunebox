#!/usr/bin/env python3
"""Generate the PWA icons in ``src/tunebox/web/`` from the same drawing as ``icon.svg``.

Why a script and not a checked-in export: the icons have to be PNG (iOS's ``apple-touch-icon``
ignores SVG, and a maskable Android icon must be a raster), and the alternative to generating them
is committing four binaries nobody can diff or correct. This is ~150 lines of stdlib — no Pillow, no
ImageMagick, no design tool — so the icon stays as reviewable as the rest of the project and a colour
change is a one-line edit followed by::

    python tools/make_icons.py

Anti-aliasing is 4x supersampling and an inside/outside test per shape, which is all a handful of
rounded rectangles and ellipses needs. Output is opaque truecolour PNG (colour type 2), because
every icon here sits on its own background anyway.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Callable, Iterable, Sequence

WEB = Path(__file__).resolve().parent.parent / "src" / "tunebox" / "web"

BG = (0x11, 0x14, 0x1A)
ACCENT = (0x6F, 0xB3, 0xFF)
INK = (0xF2, 0xF5, 0xF9)

#: Everything below is expressed on this grid and scaled to the output size, so the drawing is
#: written once at the size ``icon.svg`` uses.
GRID = 512.0

#: Subsamples per axis. 4 is enough for these shapes and keeps a 512px icon under a second.
SUPERSAMPLE = 4

Inside = Callable[[float, float], bool]


# ----------------------------------------------------------------------- geometry


def rounded_rect(x0: float, y0: float, x1: float, y1: float, radius: float) -> Inside:
    """Filled rounded rectangle."""

    def inside(x: float, y: float) -> bool:
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        cx = min(max(x, x0 + radius), x1 - radius)
        cy = min(max(y, y0 + radius), y1 - radius)
        dx, dy = x - cx, y - cy
        return dx * dx + dy * dy <= radius * radius

    return inside


def ring(cx: float, cy: float, radius: float, width: float) -> Inside:
    """Circle outline of the given stroke width, centred on the nominal radius."""
    outer = radius + width / 2
    inner = radius - width / 2

    def inside(x: float, y: float) -> bool:
        distance_sq = (x - cx) ** 2 + (y - cy) ** 2
        return inner * inner <= distance_sq <= outer * outer

    return inside


def disc(cx: float, cy: float, rx: float, ry: float) -> Inside:
    def inside(x: float, y: float) -> bool:
        return ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0

    return inside


def rect_outline(x0: float, y0: float, x1: float, y1: float, radius: float, width: float) -> Inside:
    """Rounded-rectangle outline: the shape minus a smaller copy of itself."""
    outer = rounded_rect(x0, y0, x1, y1, radius)
    inner = rounded_rect(x0 + width, y0 + width, x1 - width, y1 - width, max(0.0, radius - width))

    def inside(x: float, y: float) -> bool:
        return outer(x, y) and not inner(x, y)

    return inside


def polygon(points: Sequence[tuple[float, float]]) -> Inside:
    """Even-odd fill of a closed polygon (the note's flag)."""

    def inside(x: float, y: float) -> bool:
        hits = False
        count = len(points)
        for index in range(count):
            ax, ay = points[index]
            bx, by = points[(index + 1) % count]
            if (ay > y) != (by > y):
                crossing = ax + (y - ay) * (bx - ax) / (by - ay)
                if x < crossing:
                    hits = not hits
        return hits

    return inside


# ------------------------------------------------------------------------ raster


class Canvas:
    def __init__(self, size: int, background: tuple[int, int, int]) -> None:
        self.size = size
        self.pixels = [list(background) for _ in range(size * size)]

    def draw(self, shape: Inside, colour: tuple[int, int, int], alpha: float = 1.0) -> None:
        """Composite ``colour`` wherever ``shape`` covers, anti-aliased by supersampling."""
        scale = GRID / self.size
        step = 1.0 / SUPERSAMPLE
        offsets = [(index + 0.5) * step for index in range(SUPERSAMPLE)]
        samples = SUPERSAMPLE * SUPERSAMPLE
        for py in range(self.size):
            for px in range(self.size):
                covered = 0
                for oy in offsets:
                    gy = (py + oy) * scale
                    for ox in offsets:
                        if shape((px + ox) * scale, gy):
                            covered += 1
                if not covered:
                    continue
                weight = alpha * covered / samples
                pixel = self.pixels[py * self.size + px]
                for channel in range(3):
                    pixel[channel] = round(
                        pixel[channel] * (1.0 - weight) + colour[channel] * weight
                    )

    def rows(self) -> Iterable[bytes]:
        for py in range(self.size):
            row = bytearray()
            for px in range(self.size):
                row.extend(self.pixels[py * self.size + px])
            yield bytes(row)


def write_png(path: Path, canvas: Canvas) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    # Filter byte 0 (None) per scanline: these images are small and compress fine without a
    # predictor, and "no filter" keeps this function readable.
    raw = b"".join(b"\x00" + row for row in canvas.rows())
    header = struct.pack(">IIBBBBB", canvas.size, canvas.size, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------- the icon


def paint(canvas: Canvas, *, maskable: bool) -> None:
    """The drawing from ``icon.svg``: a speaker grille with a note in it.

    The canvas already starts as :data:`BG`, so there is no background pass — and no rounded corners
    either, deliberately: iOS rounds an ``apple-touch-icon`` itself and Android masks a maskable one,
    so baking a radius in would show as a dark notch inside the system's own rounding.

    ``maskable`` is the Android variant: the launcher may crop it to any shape, so the artwork is
    pulled into the middle ~70% and the frame is dropped rather than being sliced off by a circle
    mask on one device and a squircle on another.
    """
    centre = GRID / 2
    shrink = 0.72 if maskable else 1.0

    def s(value: float) -> float:
        """Grid coordinate, pulled towards the centre for the maskable variant."""
        return centre + (value - centre) * shrink

    if not maskable:
        canvas.draw(rect_outline(64, 64, 448, 448, 72, 24), ACCENT)

    canvas.draw(ring(s(256), s(300), 96 * shrink, 18 * shrink), ACCENT, alpha=0.45)
    canvas.draw(disc(s(256), s(300), 44 * shrink, 44 * shrink), ACCENT, alpha=0.2)

    canvas.draw(rounded_rect(s(292), s(132), s(312), s(308), 10 * shrink), INK)
    canvas.draw(disc(s(268), s(308), 46 * shrink, 36 * shrink), INK)
    canvas.draw(
        polygon(
            [
                (s(292), s(132)),
                (s(380), s(158)),
                (s(380), s(202)),
                (s(292), s(176)),
            ]
        ),
        INK,
    )


def main() -> None:
    WEB.mkdir(parents=True, exist_ok=True)
    targets = (
        # 180 is iOS's apple-touch-icon; 192 and 512 are what the manifest declares; the maskable
        # copy is Android's, and must be its own file because it needs different margins.
        ("icon-180.png", 180, False),
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable.png", 512, True),
    )
    for name, size, maskable in targets:
        canvas = Canvas(size, BG)
        paint(canvas, maskable=maskable)
        write_png(WEB / name, canvas)
        print(f"wrote {WEB / name} ({size}x{size})")


if __name__ == "__main__":
    main()
