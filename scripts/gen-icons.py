"""Generate CopperChat's PWA icons into frontend/public/icons/.

Run: uv run --with pillow python scripts/gen-icons.py  (the PNGs are committed)
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "frontend" / "public" / "icons"
COPPER = (184, 100, 31, 255)  # --cc-accent (light theme)
DARK = (15, 17, 21, 255)  # --cc-bg (dark theme)
WHITE = (255, 255, 255, 255)
SUPERSAMPLE = 4


def icon(size: int, *, maskable: bool = False, badge: bool = False) -> Image.Image:
    s = size * SUPERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0) if badge else DARK)
    d = ImageDraw.Draw(img)
    fg = WHITE if badge else COPPER
    pad = s * (0.24 if maskable else 0.14)  # maskable: keep the art inside the safe zone
    w = max(2, round(s * 0.06))
    top, bottom = pad, s - pad * 1.45
    # chat bubble
    d.rounded_rectangle([pad, top, s - pad, bottom], radius=s * 0.11, outline=fg, width=w)
    tail_x = pad + (s - 2 * pad) * 0.22
    d.polygon(
        [(tail_x, bottom - w / 2), (tail_x, s - pad * 0.95), (tail_x + s * 0.14, bottom - w / 2)],
        fill=fg,
    )
    # a PCB trace with two pads inside the bubble
    cy = (top + bottom) / 2
    left, right = pad + (s - 2 * pad) * 0.25, s - pad - (s - 2 * pad) * 0.25
    mid = (left + right) / 2
    trace = [
        (left, cy + s * 0.05),
        (mid - s * 0.05, cy + s * 0.05),
        (mid + s * 0.05, cy - s * 0.05),
        (right, cy - s * 0.05),
    ]
    d.line(trace, fill=fg, width=w, joint="curve")
    r = s * 0.055
    for cx, y in ((left, cy + s * 0.05), (right, cy - s * 0.05)):
        d.ellipse([cx - r, y - r, cx + r, y + r], fill=fg)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    icon(192).save(OUT / "icon-192.png", optimize=True)
    icon(512).save(OUT / "icon-512.png", optimize=True)
    icon(512, maskable=True).save(OUT / "icon-maskable-512.png", optimize=True)
    icon(180).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)
    icon(72, badge=True).save(OUT / "badge-72.png", optimize=True)


if __name__ == "__main__":
    main()
