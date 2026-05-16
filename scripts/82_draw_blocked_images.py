#!/usr/bin/env python3
"""
Draw programmatic flat-design icons for words that get blocked by image AI censorship.
Uses Pillow to create simple, clear illustrations suitable for language flashcards.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from common import image_filename

OUTPUT_DIR = PROJECT_ROOT / "media" / "images"
SIZE = 512  # square canvas


def new_canvas(bg: tuple = (245, 242, 235, 255)) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (SIZE, SIZE), bg)
    draw = ImageDraw.Draw(img)
    return img, draw


def save(img: Image.Image, key: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / image_filename(key)
    img.save(path, "PNG")
    print(f"  [ok] {key} -> {path.name}")
    return path


# ---------------------------------------------------------------------------
# sigaretta  (cigarette)
# A horizontal cigarette: white cylinder body, orange/tan filter end, wisp of smoke
# ---------------------------------------------------------------------------
def draw_sigaretta() -> None:
    img, draw = new_canvas()
    cx, cy = SIZE // 2, SIZE // 2

    # Cigarette body dimensions
    body_w = 280
    body_h = 48
    filter_w = 64
    tip_w = 10

    x0 = cx - body_w // 2
    y0 = cy - body_h // 2
    x1 = x0 + body_w
    y1 = y0 + body_h

    # White paper body
    draw.rounded_rectangle([x0, y0, x1 - filter_w - tip_w, y1], radius=8, fill=(250, 248, 240))
    draw.rounded_rectangle([x0, y0, x1 - filter_w - tip_w, y1], radius=8, outline=(200, 195, 185), width=2)

    # Beige/tan filter
    fx0 = x1 - filter_w - tip_w
    draw.rectangle([fx0, y0, fx0 + filter_w, y1], fill=(210, 165, 110))
    draw.rectangle([fx0, y0, fx0 + filter_w, y1], outline=(185, 140, 90), width=2)

    # Lit tip (glowing orange)
    tx0 = x1 - tip_w
    draw.rounded_rectangle([tx0, y0 + 4, x1, y1 - 4], radius=4, fill=(255, 120, 30))

    # Smoke wisps above the lit tip
    tip_center_x = tx0 + tip_w // 2
    tip_top_y = y0 + 4

    for i, (ox, amp) in enumerate([(-4, 10), (4, -10), (-3, 8)]):
        points = []
        for t in range(0, 50, 2):
            px = tip_center_x + ox + int(amp * math.sin(t * 0.25 + i))
            py = tip_top_y - t - 10
            points.append((px, py))
        if len(points) >= 2:
            draw.line(points, fill=(180, 180, 180, 160), width=3)

    save(img, "sigaretta")


# ---------------------------------------------------------------------------
# fumo  (smoke)
# Three rising smoke wisps on a light background
# ---------------------------------------------------------------------------
def draw_fumo() -> None:
    img, draw = new_canvas()
    cx, cy = SIZE // 2, SIZE // 2

    # Draw three wavy smoke columns
    smoke_color = (140, 140, 150)
    base_ys = [cy + 100, cy + 110, cy + 105]
    offsets = [-60, 0, 60]
    amplitudes = [25, -25, 20]
    widths = [14, 18, 12]

    for col_x, base_y, amp, w in zip(offsets, base_ys, amplitudes, widths):
        col_cx = cx + col_x
        points = []
        for t in range(0, 160, 3):
            px = col_cx + int(amp * math.sin(t * 0.12))
            py = base_y - t
            points.append((px, py))
        if len(points) >= 2:
            # Draw with decreasing opacity (simulate fading)
            total = len(points)
            for i in range(len(points) - 1):
                alpha = int(220 * (1 - i / total))
                seg_w = max(2, int(w * (1 - i / total * 0.6)))
                draw.line([points[i], points[i + 1]], fill=(*smoke_color, alpha), width=seg_w)

    # Small flame / source dots at base
    for col_x, base_y in zip(offsets, base_ys):
        col_cx = cx + col_x
        r = 8
        draw.ellipse([col_cx - r, base_y - r, col_cx + r, base_y + r], fill=(255, 130, 40))

    save(img, "fumo")


# ---------------------------------------------------------------------------
# fumare  (to smoke — verb)
# Cigarette angled 30°, with smoke rising — action/verb feel
# ---------------------------------------------------------------------------
def draw_fumare() -> None:
    img, draw = new_canvas()
    cx, cy = SIZE // 2, SIZE // 2

    # We'll draw by rotating a temporary canvas then compositing
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    body_w = 260
    body_h = 44
    filter_w = 58
    tip_w = 10

    x0 = cx - body_w // 2
    y0 = cy - body_h // 2
    x1 = x0 + body_w
    y1 = y0 + body_h

    # White paper
    ld.rounded_rectangle([x0, y0, x1 - filter_w - tip_w, y1], radius=8, fill=(250, 248, 240))
    ld.rounded_rectangle([x0, y0, x1 - filter_w - tip_w, y1], radius=8, outline=(200, 195, 185), width=2)

    # Filter
    fx0 = x1 - filter_w - tip_w
    ld.rectangle([fx0, y0, fx0 + filter_w, y1], fill=(210, 165, 110))
    ld.rectangle([fx0, y0, fx0 + filter_w, y1], outline=(185, 140, 90), width=2)

    # Lit tip
    tx0 = x1 - tip_w
    ld.rounded_rectangle([tx0, y0 + 4, x1, y1 - 4], radius=4, fill=(255, 120, 30))

    # Rotate layer ~25 degrees
    rotated = layer.rotate(-25, resample=Image.BICUBIC, expand=False, center=(cx, cy))

    # Compose onto canvas
    bg = Image.new("RGBA", (SIZE, SIZE), (245, 242, 235, 255))
    bg.paste(rotated, mask=rotated)
    draw2 = ImageDraw.Draw(bg)

    # Smoke rising from the tip area (upper right of center after rotation)
    smoke_x = cx + 90
    smoke_y = cy - 60
    for i, (ox, amp) in enumerate([(-3, 12), (5, -14), (-5, 10)]):
        points = []
        for t in range(0, 80, 3):
            px = smoke_x + ox + int(amp * math.sin(t * 0.18 + i * 1.2))
            py = smoke_y - t
            points.append((px, py))
        if len(points) >= 2:
            total = len(points)
            for j in range(len(points) - 1):
                alpha = int(200 * (1 - j / total))
                seg_w = max(2, int(8 * (1 - j / total * 0.5)))
                draw2.line([points[j], points[j + 1]], fill=(150, 150, 160, alpha), width=seg_w)

    save(bg.convert("RGBA"), "fumare")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("Drawing blocked images programmatically...\n")
    draw_sigaretta()
    draw_fumo()
    draw_fumare()
    print("\nDone.")


if __name__ == "__main__":
    main()
