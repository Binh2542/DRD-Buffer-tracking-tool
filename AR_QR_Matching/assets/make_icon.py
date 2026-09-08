"""One-off generator for icon.png/icon.ico - kept for future re-runs if the
design needs tweaking (e.g. a color update to match a future DRD Accounting
Tool re-theme). Not imported by the app itself. Needs Pillow (not an app
runtime dependency - run this with any interpreter that has it installed).

Same visual language as DRD Accounting Tool's icon (dark rounded background,
green scan-corner brackets) for a matching "family" look, but the center
glyph swaps DRD's single QR dot-grid for two overlapping cards + a check
badge - PCB and case matched together, this tool's actual job.
"""
from pathlib import Path

from PIL import Image, ImageDraw

BG = "#14171c"
ACCENT = "#21c98f"
TEXT = "#e6e9ee"
CARD_BACK = "#7d8794"

SIZE = 256
OUT_DIR = Path(__file__).resolve().parent


def _draw_bracket(draw, x, y, dx, dy, arm=40, width=15):
    """One L-shaped scan-corner bracket. (x, y) is the corner point; dx/dy
    (each +-1) point the two arms inward along each axis."""
    draw.line([(x, y), (x + dx * arm, y)], fill=ACCENT, width=width)
    draw.line([(x, y), (x, y + dy * arm)], fill=ACCENT, width=width)
    r = width / 2
    for cx, cy in ((x, y), (x + dx * arm, y), (x, y + dy * arm)):
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)


def build_icon() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=56, fill=BG)

    inset = 26
    _draw_bracket(draw, inset, inset, 1, 1)
    _draw_bracket(draw, SIZE - inset, inset, -1, 1)
    _draw_bracket(draw, inset, SIZE - inset, 1, -1)
    _draw_bracket(draw, SIZE - inset, SIZE - inset, -1, -1)

    # Two overlapping "cards" (PCB + plastic case) being matched.
    draw.rounded_rectangle([62, 92, 146, 172], radius=16, fill=CARD_BACK)
    draw.rounded_rectangle([110, 92, 194, 172], radius=16, fill=TEXT)

    # Verified-match badge (checkmark in a filled circle) over the overlap.
    bx, by, br = 176, 158, 34
    draw.ellipse([bx - br, by - br, bx + br, by + br], fill=ACCENT, outline=BG, width=4)
    draw.line(
        [(bx - 15, by), (bx - 4, by + 12), (bx + 17, by - 14)],
        fill=BG, width=8, joint="curve",
    )

    return img


def main():
    icon = build_icon()
    icon.save(OUT_DIR / "icon.png")
    # Windows .ico wants multiple sizes bundled together.
    icon.save(OUT_DIR / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Wrote {OUT_DIR / 'icon.png'} and {OUT_DIR / 'icon.ico'}")


if __name__ == "__main__":
    main()
