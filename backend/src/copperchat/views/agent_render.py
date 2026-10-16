"""The agent view: a board drawn for a model to read, not for a human to admire.

mm grid with labelled lines, every footprint boxed and labelled with its
reference and mm position, net-coloured copper, airwires for unrouted
connections and DRC markers. Pure function of `board_snapshot()` data, so it is
snapshot-tested without KiCad.
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from copperchat.views.colors import net_color

Rgb = tuple[int, int, int]
THEMES: dict[str, dict[str, Rgb]] = {
    "agent": {
        "bg": (18, 20, 24),
        "outline": (230, 210, 80),
        "fp": (150, 150, 160),
        "grid": (45, 48, 56),
        "text": (235, 235, 235),
        "air": (255, 255, 255),
        "err": (255, 60, 60),
        "warn": (255, 170, 0),
    },
    "dark": {
        "bg": (0, 0, 0),
        "outline": (230, 210, 80),
        "fp": (140, 140, 140),
        "grid": (40, 40, 40),
        "text": (240, 240, 240),
        "air": (220, 220, 220),
        "err": (255, 60, 60),
        "warn": (255, 170, 0),
    },
    "light": {
        "bg": (250, 250, 248),
        "outline": (120, 100, 0),
        "fp": (90, 90, 100),
        "grid": (215, 215, 215),
        "text": (20, 20, 20),
        "air": (60, 60, 60),
        "err": (200, 0, 0),
        "warn": (200, 120, 0),
    },
}
MAX_PX = 2400
MARGIN_MM = 3.0


def _bounds(snap: dict[str, Any]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for s in snap["outline"]:
        xs += [s["start_mm"]["x"], s["end_mm"]["x"]]
        ys += [s["start_mm"]["y"], s["end_mm"]["y"]]
    for f in snap["footprints"]:
        b = f["bbox_mm"]
        if b:
            xs += [b["x"], b["x"] + b["w"]]
            ys += [b["y"], b["y"] + b["h"]]
    if not xs:
        return (0.0, 0.0, 10.0, 10.0)
    return (
        min(xs) - MARGIN_MM,
        min(ys) - MARGIN_MM,
        max(xs) - min(xs) + 2 * MARGIN_MM,
        max(ys) - min(ys) + 2 * MARGIN_MM,
    )


def grid_step(w_mm: float) -> float:
    """Round grid pitch giving at most ~20 lines across."""
    for step in (1, 2, 5, 10, 20, 50):
        if w_mm / step <= 20:
            return float(step)
    return 100.0


def _font(size: float) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=max(10, round(size)))


def _draw_grid(
    d: ImageDraw.ImageDraw,
    box: tuple[float, float, float, float],
    to_px: Callable[[float, float], tuple[float, float]],
    line: tuple[int, ...],
    label: Rgb,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    x0, y0, w, h = box
    step = grid_step(max(w, h))
    gx = math.ceil(x0 / step) * step
    while gx < x0 + w:
        d.line([to_px(gx, y0), to_px(gx, y0 + h)], fill=line)
        d.text(to_px(gx, y0), f" {gx:g}", fill=label, font=font)
        gx += step
    gy = math.ceil(y0 / step) * step
    while gy < y0 + h:
        d.line([to_px(x0, gy), to_px(x0 + w, gy)], fill=line)
        d.text(to_px(x0, gy), f" {gy:g}", fill=label, font=font)
        gy += step


def _marker_color(colors: dict[str, Rgb], marker: dict[str, Any]) -> Rgb:
    return colors["err"] if marker.get("severity") == "error" else colors["warn"]


def render_board(
    snap: dict[str, Any],
    *,
    region_box: tuple[float, float, float, float] | None,
    layers: list[str] | None,
    theme: str,
    markers: list[dict[str, Any]],
    px_per_mm: float = 12.0,
) -> bytes:
    """PNG of the board (or `region_box`, mm) from a `board_snapshot()` dict."""
    t = THEMES.get(theme, THEMES["agent"])
    x0, y0, w, h = region_box or _bounds(snap)
    scale = min(px_per_mm, MAX_PX / max(w, h))
    img = Image.new("RGB", (max(1, round(w * scale)), max(1, round(h * scale))), t["bg"])
    d = ImageDraw.Draw(img)
    font = _font(scale * 0.9)
    pad_font = _font(scale * 0.8)

    def to_px(x: float, y: float) -> tuple[float, float]:
        return ((x - x0) * scale, (y - y0) * scale)

    def p(pt: dict[str, float]) -> tuple[float, float]:
        return to_px(pt["x"], pt["y"])

    _draw_grid(d, (x0, y0, w, h), to_px, t["grid"], t["text"], font)
    for s in snap["outline"]:
        d.line([p(s["start_mm"]), p(s["end_mm"])], fill=t["outline"], width=2)

    copper = [lay for lay in layers or [] if lay.endswith(".Cu")]
    for tr in snap["tracks"]:
        if layers and tr["layer"] not in layers:
            continue
        points = [
            p(tr["start_mm"]),
            *([p(tr["mid_mm"])] if "mid_mm" in tr else []),
            p(tr["end_mm"]),
        ]
        d.line(
            points, fill=net_color(tr["net"], theme), width=max(1, round(tr["width_mm"] * scale))
        )
    if not layers or copper:
        for v in snap["vias"]:
            cx, cy = p(v["position_mm"])
            r = 0.4 * scale
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=net_color(v["net"], theme))

    taken: list[tuple[float, float, float, float]] = []
    for f in snap["footprints"]:
        prefix = "F." if f["side"] == "front" else "B."
        if layers and not any(lay.startswith(prefix) for lay in layers):
            continue
        b = f["bbox_mm"]
        if b:
            d.rectangle(
                [to_px(b["x"], b["y"]), to_px(b["x"] + b["w"], b["y"] + b["h"])], outline=t["fp"]
            )
        quarter = round(f.get("rotation_deg", 0.0) / 90) % 2 == 1
        for pad in f["pads"]:
            cx, cy = p(pad["position_mm"])
            size = pad.get("size_mm") or {"x": 0.7, "y": 0.7}
            sx, sy = (size["y"], size["x"]) if quarter else (size["x"], size["y"])
            rx, ry = max(2.0, sx * scale / 2), max(2.0, sy * scale / 2)
            col = net_color(pad["net"], theme) if pad["net"] else t["fp"]
            d.rectangle([cx - rx, cy - ry, cx + rx, cy + ry], fill=col, outline=t["text"])
            if min(rx, ry) >= 7:
                d.text((cx, cy), pad["number"], fill=t["bg"], font=pad_font, anchor="mm")
        pos = f["position_mm"]
        label = f"{f['ref']} ({pos['x']:g},{pos['y']:g})"
        lx, ly = to_px(b["x"], b["y"]) if b else p(pos)
        tw, th = d.textbbox((0, 0), label, font=font)[2:]
        box = (lx, ly - th - 2, lx + tw, ly - 2)
        # De-clutter: move a label up until it no longer overlaps an earlier one.
        while box[1] > -th and any(
            not (box[2] < o[0] or box[0] > o[2] or box[3] < o[1] or box[1] > o[3]) for o in taken
        ):
            box = (box[0], box[1] - th - 1, box[2], box[3] - th - 1)
        taken.append(box)
        d.text((box[0], box[1]), label, fill=t["text"], font=font)

    for u in snap["unconnected"]:
        d.line([p(u["from_mm"]), p(u["to_mm"])], fill=t["air"], width=1)
    for m in markers:
        if m.get("position_mm"):
            cx, cy = p(m["position_mm"])
            r = 0.8 * scale
            d.polygon(
                [(cx, cy - r), (cx + r, cy + r), (cx - r, cy + r)],
                outline=_marker_color(t, m),
                width=2,
            )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def overlay_grid(
    img: Image.Image,
    origin_mm: tuple[float, float],
    px_per_mm: float,
    markers: list[dict[str, Any]],
    theme: str,
) -> bytes:
    """mm grid + check markers over a KiCad export whose pixel->mm mapping is known.

    `origin_mm` is the page position of pixel (0, 0) (spike 1C, Q5: page origin,
    `mm = px * 25.4 / dpi`).
    """
    t = THEMES.get(theme, THEMES["light"])
    out = img.convert("RGB")
    d = ImageDraw.Draw(out, "RGBA")
    w_mm, h_mm = out.width / px_per_mm, out.height / px_per_mm
    font = _font(max(12.0, out.width / 120))

    def to_px(x: float, y: float) -> tuple[float, float]:
        return ((x - origin_mm[0]) * px_per_mm, (y - origin_mm[1]) * px_per_mm)

    box = (origin_mm[0], origin_mm[1], w_mm, h_mm)
    _draw_grid(d, box, to_px, (*t["grid"], 150), t["err"], font)
    for m in markers:
        if m.get("position_mm"):
            cx, cy = to_px(m["position_mm"]["x"], m["position_mm"]["y"])
            r = max(8.0, 1.5 * px_per_mm)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=_marker_color(t, m), width=3)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()
