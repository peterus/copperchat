"""Footprint render pipeline (spec §7.5, FP-C5): `kicad-cli fp export svg` of one
library footprint, wrapped in CopperChat's own SVG (mm grid, axes, ticks, dimension
labels) and rasterised to PNG with CairoSVG.

Hardening (spec §7.5, C10):
- one `kicad-cli fp export` at a time: the **whole** render (cli, parse, wrap,
  rasterise) runs under the module semaphore; a cancelled render keeps it until its
  worker thread (parse/wrap), its rasteriser process (killed, then reaped) and the
  temp-dir removal have finished;
- argv list only, `--fp=<name>` one element; the library directory comes from the
  footprint table (absolute, existing, `*.pretty`), the name never holds a path;
- KiCad's output must exist, be non-empty and stay under the rasterising caps (4 MB,
  10,000 elements, 50,000 points, 2,000 points in one element, lines 1,000 times the
  drawing long: about 2-3x the largest stock export each; cheap pre-filters only, as
  CairoSVG's cost has more dimensions, e.g. the stroke width); any `<!DOCTYPE` other than
  KiCad's own SVG 1.1 public prolog, and any `<!ENTITY`, is refused; it is re-serialised
  with `xml.etree` (never by string concatenation) keeping only the elements KiCad's
  `fp export svg` writes (`ALLOWED_TAGS`; anything else goes with its subtree, so no
  script, style, image, `use`, pattern, marker, mask, filter, clipPath, ...), without
  links (`href`), event handlers, and attribute values holding `url(` (fragment or not),
  a backslash (CSS escape), `image-set(` or `src(`; group `opacity` (an attribute or a
  `style` declaration) is dropped, as nested opacity groups multiply CairoSVG's work
  (`fill-opacity` / `stroke-opacity` stay), and an element KiCad hid with opacity 0 (its
  invisible REF** / name texts; also `0%`, `!important`, an unreadable value or a style
  with a backslash) goes with its subtree, decided before any value is filtered;
- CairoSVG runs in a process of its own (`_raster_worker`: argv only, `python -I`, a
  minimal environment without the app's secrets, a throw-away HOME, 1.5 GB address
  space, 30 s CPU and file-size rlimits), with a URL fetcher that refuses everything,
  never with `unsafe=True`; its stdin, stdout and stderr are files in the per-render
  temp dir (no pipes: nothing can block on a reader that stopped), the parent only waits
  for its exit; after `RASTER_TIMEOUT_S` (20 s wall clock), on an error or a
  cancellation its process group is killed and it is reaped before the render lock is
  released, and the render is `render_failed` ("render timed out"); its PNG output is
  size-checked before it is read, stderr is never quoted.

Coordinates: mm in footprint space (origin = anchor, y down). KiCad's SVG `viewBox` is
that space (spike Q8), so the wrapper draws in it directly and embeds KiCad's drawing as
a nested `<svg>` with the same `viewBox`.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import shutil
import signal
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.cli import run_kicad_cli
from copperchat.kicad_tools.footprints.checks import pitch_pad_pair
from copperchat.kicad_tools.footprints.geom import board_size
from copperchat.kicad_tools.footprints.model import FootprintGeometry, geometry_hash
from copperchat.library.service import closest_footprints, library_for
from copperchat.projects import agent_dir
from copperchat.tools.spec import ToolContext

SVG_NS = "http://www.w3.org/2000/svg"
XML_NS = "http://www.w3.org/XML/1998/namespace"
# What CairoSVG's rasterising costs (under the global render lock), bounded before it
# runs. Calibrated on all 15,468 stock footprint exports of the base image (all F/B
# layers; plan "As built (F2)"), stock maximum in brackets: bytes (466 KB), elements
# (6,205: a full-canvas fill each costs ~0.4 ms), coordinate pairs in all `d`/`points`
# (23,924: Xilinx FLG1925), pairs of one element (586: a WEEE logo; one self-crossing
# path tessellates superlinearly, 100k pairs ~226 s) and the summed outline length in
# drawing extents (364: CP_Radial_D40 hatching; a stroke across the drawing costs
# ~0.1-0.3 ms at 2000 px, wherever its pairs are).
MAX_SVG_BYTES = 4 * 1024 * 1024
MAX_ELEMENTS = 10_000
MAX_COORDS = 50_000
MAX_ELEMENT_COORDS = 2_000
MAX_SPAN = 1_000.0


@dataclass(frozen=True)
class SvgLimits:
    """The rasterising caps `load_kicad_svg` enforces (board renders bring their own,
    `views.board_render.BOARD_SVG_LIMITS`, B-C9)."""

    max_bytes: int
    max_elements: int
    max_coords: int
    max_element_coords: int
    max_span: float  # summed line length, in drawing extents


FOOTPRINT_SVG_LIMITS = SvgLimits(
    max_bytes=MAX_SVG_BYTES,
    max_elements=MAX_ELEMENTS,
    max_coords=MAX_COORDS,
    max_element_coords=MAX_ELEMENT_COORDS,
    max_span=MAX_SPAN,
)
# coordinate pairs charged per plain shape (its outline is a few Bezier curves)
SHAPE_COORDS = {"circle": 4, "ellipse": 4, "rect": 4, "line": 2}
MAX_DEPTH = 64  # KiCad's SVG nests 2-3 levels
MAX_VIEW_MM = 10_000.0
CLI_TIMEOUT_S = 60.0
MIN_PX_PER_MM = 40.0
TARGET_PX = 1200.0  # longest side, unless 40 px/mm needs more
MAX_SIDE_PX = 2000
# The rasteriser process (`_raster_worker`, argv only, `-I`: no user site, no PYTHON*
# variables, no cwd on sys.path). Its wall-clock limit: the largest stock footprint
# rasterises in ~3 s (Xilinx FLG1925, a cold font cache ~13 s), an adversarial one (24
# thick 1,990-point strokes: within every cap above) took 41 s under the render lock.
RASTER_WORKER = (sys.executable, "-I", "-m", "copperchat.views._raster_worker")
RASTER_TIMEOUT_S = 20.0
REAP_TIMEOUT_S = 5.0  # waiting for the SIGKILLed worker to be reaped
# a PNG of at most 2000 x 2000 px: raw RGBA plus filter bytes, zlib and chunk overhead
MAX_PNG_BYTES = 4 * MAX_SIDE_PX * MAX_SIDE_PX + 1024 * 1024
MAX_GRID_LINES = 500  # per axis
MAX_RAW_CHARS = 500  # KiCad output quoted in an error
MAX_LABEL_NUMBER = 16
FRONT = ("F.Cu", "F.Mask", "F.Paste", "F.SilkS", "F.Fab", "F.CrtYd")
BACK = ("B.Cu", "B.Mask", "B.Paste", "B.SilkS", "B.Fab", "B.CrtYd")
MAX_LAYERS = 32
LAYER_RE = re.compile(r"^(?:(?:F|B)\.(?:Cu|Mask|Paste|SilkS|Fab|CrtYd)|Edge\.Cuts)$")
GRID_LADDER = (0.1, 0.25, 0.5, 1.0)
# The elements KiCad's `fp export svg` writes (seen in the base image: svg, g, path,
# circle, text, title, desc) plus the other plain shapes its SVG plotter can emit. An
# allowlist, not a drop list: every element that instantiates or paints other elements
# (use, tref, pattern, marker, mask, filter, clipPath, gradients, ...) can be nested into
# an exponential "billion laughs" that holds the render lock, and none is needed here.
ALLOWED_TAGS = frozenset(
    {
        "svg",
        "g",
        "path",
        "circle",
        "ellipse",
        "rect",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "title",
        "desc",
    }
)
# KiCad's own prolog (seen in the base image): XML declaration + the SVG 1.1 public
# DOCTYPE without an internal subset. Exactly this is stripped; any other DOCTYPE or an
# ENTITY declaration is refused.
_KICAD_PROLOG = re.compile(
    r'\A\s*(?:<\?xml[^>]*\?>\s*)?<!DOCTYPE\s+svg\s+PUBLIC\s+"-//W3C//DTD SVG 1\.1//EN"\s+'
    r'"http://www\.w3\.org/Graphics/SVG/1\.1/DTD/svg11\.dtd"\s*>'
)
_XML_DECL = re.compile(r"\A\s*<\?xml[^>]*\?>")
_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
# CSS functions that load or reference a resource: url(...) (also a #fragment: the
# allowlisted elements need none), image-set("x.png"), src("x")
_CSS_REF = re.compile(r"(?:url|image-set|src)\s*\(", re.IGNORECASE)
# a `style` declaration of group opacity (not fill-opacity / stroke-opacity)
_OPACITY_DECL = re.compile(r"^\s*opacity\s*:(.*)$", re.IGNORECASE | re.DOTALL)
_IMPORTANT = re.compile(r"!\s*important\s*$", re.IGNORECASE)
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

# Colours (dark, like KiCad's footprint editor: its layer colours are made for it).
BG, MINOR, MAJOR, AXIS, TEXT, ACCENT = (
    "#14181d",
    "#262d35",
    "#3b4552",
    "#7d8a99",
    "#d0d7de",
    "#ffb000",
)
FONT = "DejaVu Sans, Arial, sans-serif"

_render_lock = asyncio.Semaphore(1)


@dataclass(frozen=True)
class RenderOutput:
    png: Path
    svg: Path  # the wrapper SVG (not KiCad's raw output)
    # px per mm of the PNG; the PNG is `extent_mm * scale_px_per_mm` px
    scale_px_per_mm: float
    # size of the whole PNG / wrapper viewBox in mm, margins (ticks, legend) included
    extent_mm: tuple[float, float]
    geometry_hash: str
    # top-left of the PNG / wrapper viewBox in footprint mm (y down):
    # footprint (x, y) = origin_mm + (px, py) / scale_px_per_mm
    origin_mm: tuple[float, float]
    grid_mm: float  # the minor grid actually drawn (after the 500-line cap)


@dataclass(frozen=True)
class _Wrapped:
    svg: bytes
    origin_mm: tuple[float, float]
    extent_mm: tuple[float, float]
    grid_mm: float
    major_mm: float


def _failed(
    why: str, suggestion: str = "Retry once; if it fails again use fp_get."
) -> StructuredToolError:
    return StructuredToolError("Footprint render failed", why, suggestion, code="render_failed")


def _invalid(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


# --- pure helpers ---------------------------------------------------------------------


def auto_grid(extent_mm: float) -> float:
    """Minor grid for the longer side of the drawing."""
    if extent_mm <= 5:
        return 0.1
    if extent_mm <= 20:
        return 0.25
    if extent_mm <= 50:
        return 0.5
    return 1.0


def _grid_steps(start: float) -> list[float]:
    """Grid candidates from `start` up: the ladder, then 5, 10, 50, 100, ... (every
    step divides the 1 mm / 5 mm major spacing or is a multiple of it)."""
    out = [g for g in GRID_LADDER if g >= start - 1e-9]
    for k in range(8):
        out += [5.0 * 10**k, 10.0 * 10**k]
    return out


def _coarsen(grid: float, span: float) -> float:
    for g in _grid_steps(grid):
        if span / g + 3 <= MAX_GRID_LINES:
            return g
    raise _failed("the drawing is too large for a grid")


def default_layers(g: FootprintGeometry) -> list[str]:
    """F.Cu, F.Mask, F.Paste, F.SilkS, F.Fab, F.CrtYd plus each B.* layer the footprint
    uses (pad layers, `*.X` counting for both sides; shape and text layers)."""
    used: set[str] = set()
    for pad in g.pads:
        for name in pad.layers:
            used.add("B." + name[2:] if name.startswith("*.") else name)
    used.update(s.layer for s in g.shapes)
    used.update(t.layer for t in g.texts)
    return [*FRONT, *(name for name in BACK if name in used)]


def _split(key: str) -> tuple[str, str]:
    if key.startswith("{"):
        ns, _, local = key[1:].partition("}")
        return ns, local
    return "", key


def _transparent(value: str) -> bool:
    """True for an opacity value of 0 or below (also `0%`, `0 !important`) and for one
    that cannot be read (a CSS comment, an escape, `nan`, ...): when in doubt hidden."""
    v = _IMPORTANT.sub("", value).strip()
    if v.endswith("%"):
        v = v[:-1]
    try:
        x = float(v)
    except ValueError:
        return True
    return not math.isfinite(x) or x <= 0.0


def _without_opacity(style: str) -> tuple[str, bool]:
    """`style` without its `opacity:` declarations (unchanged without one), and whether
    the element is hidden: one was 0 or unreadable, or the style holds a backslash (a
    CSS escape can spell `opacity` so that no plain match sees it)."""
    if "\\" in style:
        return style, True
    kept: list[str] = []
    hidden = found = False
    for decl in style.split(";"):
        m = _OPACITY_DECL.match(decl)
        if m:
            found = True
            hidden = hidden or _transparent(m.group(1))
        elif decl.strip():
            kept.append(decl)
    return (";".join(kept) if found else style), hidden


@dataclass
class _Tally:
    """What the kept elements will cost CairoSVG, against the caps of `limits`."""

    extent: float  # the drawing's longer side (viewBox), the unit of `span`
    limits: SvgLimits = FOOTPRINT_SVG_LIMITS
    elements: int = 0
    coords: int = 0
    span: float = 0.0
    peak: int = 0  # the most coordinate pairs of one element

    def add(self, coords: int, length: float) -> None:
        lim = self.limits
        self.elements += 1
        self.coords += coords
        self.span += length / self.extent
        self.peak = max(self.peak, coords)
        if coords > lim.max_element_coords:
            raise _failed(
                f"kicad-cli wrote an SVG element of more than {lim.max_element_coords} points"
            )
        if self.elements > lim.max_elements:
            raise _failed(f"kicad-cli wrote an SVG of more than {lim.max_elements} elements")
        if self.coords > lim.max_coords:
            raise _failed(f"kicad-cli wrote an SVG of more than {lim.max_coords} points")
        if self.span > lim.max_span:
            raise _failed(
                f"kicad-cli wrote an SVG whose lines are longer than {lim.max_span:.0f} "
                "times the drawing"
            )


def _path_cost(value: str) -> tuple[int, float]:
    """(coordinate pairs, length) of a `d` / `points` value: its numbers taken as
    consecutive points (KiCad writes absolute M/L paths; an arc's 7 numbers count as
    3.5 points and add a little length: an over-estimate is fine)."""
    nums = [float(t) for t in _NUMBER.findall(value)]
    pts = list(zip(nums[0::2], nums[1::2], strict=False))
    length = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    return (len(nums) + 1) // 2, length if math.isfinite(length) else math.inf


def _shape_cost(local: str, attrs: dict[str, str]) -> tuple[int, float]:
    """(coordinate pairs, outline length) of a circle, ellipse, rect or line."""

    def num(key: str) -> float:
        try:
            v = float(attrs.get(key, "0"))
        except ValueError:
            return 0.0
        return v if math.isfinite(v) else math.inf

    if local in ("circle", "ellipse"):
        r = abs(num("r")) if local == "circle" else max(abs(num("rx")), abs(num("ry")))
        return SHAPE_COORDS[local], 2 * math.pi * r
    if local == "rect":
        return SHAPE_COORDS[local], 2 * (abs(num("width")) + abs(num("height")))
    if local == "line":
        dx, dy = num("x2") - num("x1"), num("y2") - num("y1")
        return SHAPE_COORDS[local], math.hypot(dx, dy) if math.isfinite(dx + dy) else math.inf
    return 0, 0.0


def _clean(src: ET.Element, depth: int, tally: _Tally) -> ET.Element | None:
    """An un-namespaced copy of an SVG element without active content and without
    group opacity; None to drop (also a hidden element: opacity 0). `tally` adds up
    what the kept elements cost (`render_failed` above a `MAX_*` cap)."""
    if depth > MAX_DEPTH:
        raise _failed("kicad-cli wrote an SVG nested too deeply")
    ns, local = _split(src.tag) if isinstance(src.tag, str) else ("?", "")
    if ns != SVG_NS or local not in ALLOWED_TAGS:
        return None
    out = ET.Element(local)
    for key, value in src.attrib.items():
        kns, klocal = _split(key)
        name = klocal.lower()
        if kns and not (kns == XML_NS and klocal == "space"):
            continue  # inkscape:, sodipodi:, xml:base, ...
        # opacity first: a hidden element stays hidden whatever else its value holds
        if name == "opacity":  # group opacity: nested, it multiplies the work
            if _transparent(value):
                return None  # hidden (KiCad's invisible texts): drawn as nothing anyway
            continue
        if name == "style":
            value, hidden = _without_opacity(value)
            if hidden:
                return None
        if name.startswith("on") or name == "href":
            continue  # event handlers; links (xlink:href, href): nothing allowed needs one
        if "\\" in value or _CSS_REF.search(value):
            continue  # url()/image-set()/src(); a CSS escape (u\72 l() can hide one
        out.set(key, value)
    coords, length = _shape_cost(local, out.attrib)
    for key in ("d", "points"):
        if key in out.attrib:
            c, n = _path_cost(out.attrib[key])
            coords, length = coords + c, length + n
    if local in ("text", "tspan"):  # glyphs CairoSVG lays out
        coords += len(src.text or "") + len(src.tail or "")
    tally.add(coords, length)
    out.text = src.text
    out.tail = src.tail
    for child in src:
        cleaned = _clean(child, depth + 1, tally)
        if cleaned is not None:
            out.append(cleaned)
    return out


def load_kicad_svg(data: bytes, *, limits: SvgLimits = FOOTPRINT_SVG_LIMITS) -> ET.Element:
    """KiCad's SVG, parsed and sanitised (an un-namespaced `svg` element tree), or
    `render_failed` for output over a cap of `limits` (bytes, elements, points, line
    length), a hidden root, a foreign DOCTYPE, an ENTITY declaration or markup
    `xml.etree` cannot parse."""
    if len(data) > limits.max_bytes:
        raise _failed(f"kicad-cli wrote more than {limits.max_bytes} bytes of SVG")
    # UTF-8 only (a BOM is dropped): the checks below run on the decoded text and expat
    # parses that text, so a declared or BOM-signalled other encoding (UTF-16 hiding a
    # DOCTYPE from a byte search) can never change what is checked.
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _failed("kicad-cli's SVG is not UTF-8") from None
    if "\x00" in text:
        raise _failed("kicad-cli's SVG contains NUL characters")
    match = _KICAD_PROLOG.match(text) or _XML_DECL.match(text)
    # without its declaration expat reads the text as UTF-8, never a declared encoding
    body = text[match.end() :] if match else text
    if _DECLARATION.search(body):
        raise _failed("kicad-cli's SVG declares a DOCTYPE or an ENTITY; it was not parsed")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise _failed(f"kicad-cli's SVG could not be parsed: {str(exc)[:200]}") from None
    if root.tag != f"{{{SVG_NS}}}svg":
        raise _failed("kicad-cli's output is not an SVG document")
    extent = max(_view_box(root)[2:])
    cleaned = _clean(root, 0, _Tally(extent, limits))
    if cleaned is None:  # the root itself hidden (opacity 0): nothing to show
        raise _failed("kicad-cli's SVG is hidden as a whole (opacity 0)")
    return cleaned


def _num(v: float, digits: int = 6) -> str:
    text = f"{round(v, digits):.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _view_box(root: ET.Element) -> tuple[float, float, float, float]:
    parts = (root.get("viewBox") or "").replace(",", " ").split()
    try:
        x, y, w, h = (float(p) for p in parts)
    except ValueError:
        raise _failed("kicad-cli's SVG has no usable viewBox") from None
    if not all(map(math.isfinite, (x, y, w, h))) or not (0 < w <= MAX_VIEW_MM):
        raise _failed("kicad-cli's SVG has no usable viewBox")
    if not 0 < h <= MAX_VIEW_MM or max(abs(x), abs(y)) > MAX_VIEW_MM:
        raise _failed("kicad-cli's SVG has no usable viewBox")
    return x, y, w, h


def _short(number: str) -> str:
    return number if len(number) <= MAX_LABEL_NUMBER else number[:MAX_LABEL_NUMBER] + "…"


def _label_lines(g: FootprintGeometry) -> list[str]:
    """Dimension labels from the read-back geometry (never from tool input)."""
    lines: list[str] = []
    pad1 = next((p for p in g.pads if p.number == "1"), None)
    if pad1 is not None:
        (w, h), (x, y) = board_size(pad1), pad1.at  # as drawn (90/270 degrees swap)
        lines.append(f"pad 1: {_num(w, 3)} x {_num(h, 3)} mm at ({_num(x, 3)}, {_num(y, 3)})")
    pair = pitch_pad_pair(g)
    if pair is not None:
        a, b = pair
        pitch = math.hypot(b.at[0] - a.at[0], b.at[1] - a.at[1])
        lines.append(f"pitch 1 → {_short(b.number)}: {_num(pitch, 3)} mm")
    summary = g.layer_summary()
    crtyd = summary.get("F.CrtYd") or summary.get("B.CrtYd")
    if crtyd is not None:
        x0, y0, x1, y1 = crtyd["bbox"]
        lines.append(f"courtyard: {_num(x1 - x0, 3)} x {_num(y1 - y0, 3)} mm")
    return lines


def _sub(parent: ET.Element, tag: str, **attrs: str | float) -> ET.Element:
    el = ET.SubElement(parent, tag)
    for key, value in attrs.items():
        el.set(key.replace("_", "-"), value if isinstance(value, str) else _num(value))
    return el


def _positions(lo: float, hi: float, step: float) -> list[float]:
    first, last = math.ceil(lo / step - 1e-6), math.floor(hi / step + 1e-6)
    return [round(i * step, 6) for i in range(first, last + 1)]


def _multiple(v: float, step: float) -> bool:
    return abs(v / step - round(v / step)) < 1e-6


def _build(
    kicad_svg: ET.Element, geometry: FootprintGeometry, grid_mm: float | None, labels: bool
) -> _Wrapped:
    vx, vy, vw, vh = _view_box(kicad_svg)
    x0, y0, w, h = vx, vy, vw, vh
    x1, y1 = x0 + w, y0 + h
    extent = max(w, h)
    # the grid area: the drawing (plus the origin when it is near), padded, on the grid
    pad = 0.05 * extent
    near = -0.25 * extent
    with_origin = x0 + near <= 0 <= x1 - near and y0 + near <= 0 <= y1 - near
    if with_origin:
        x0, y0, x1, y1 = min(x0, 0.0), min(y0, 0.0), max(x1, 0.0), max(y1, 0.0)
    span = max(x1 - x0, y1 - y0) + 2 * pad
    grid = _coarsen(grid_mm or auto_grid(extent), span)
    major = max(1.0 if extent <= 20 else 5.0, grid)
    gx0, gy0 = math.floor((x0 - pad) / grid) * grid, math.floor((y0 - pad) / grid) * grid
    gx1, gy1 = math.ceil((x1 + pad) / grid) * grid, math.ceil((y1 + pad) / grid) * grid
    size = max(gx1 - gx0, gy1 - gy0)
    font = 0.02 * size
    dims = _label_lines(geometry) if labels else []
    legend = [f"grid {_num(grid, 3)} mm, major {_num(major, 3)} mm", *dims]
    left, top, right = 4.0 * font, 2.0 * font, font
    bottom = font + len(legend) * 1.5 * font + 0.5 * font
    ox, oy = gx0 - left, gy0 - top
    ow, oh = (gx1 - gx0) + left + right, (gy1 - gy0) + top + bottom

    root = ET.Element("svg")
    root.set("xmlns", SVG_NS)
    root.set("version", "1.1")
    root.set("width", f"{_num(ow)}mm")
    root.set("height", f"{_num(oh)}mm")
    root.set("viewBox", " ".join(_num(v) for v in (ox, oy, ow, oh)))
    _sub(root, "rect", x=ox, y=oy, width=ow, height=oh, fill=BG)

    minor_g = _sub(root, "g", id="grid-minor", stroke=MINOR, stroke_width=0.0012 * size)
    major_g = _sub(root, "g", id="grid-major", stroke=MAJOR, stroke_width=0.0025 * size)
    xs, ys = _positions(gx0, gx1, grid), _positions(gy0, gy1, grid)
    for v in xs:
        _sub(major_g if _multiple(v, major) else minor_g, "line", x1=v, y1=gy0, x2=v, y2=gy1)
    for v in ys:
        _sub(major_g if _multiple(v, major) else minor_g, "line", x1=gx0, y1=v, x2=gx1, y2=v)
    axes = _sub(root, "g", id="axes", stroke=AXIS, stroke_width=0.004 * size)
    if gx0 <= 0 <= gx1:
        _sub(axes, "line", x1=0.0, y1=gy0, x2=0.0, y2=gy1)
    if gy0 <= 0 <= gy1:
        _sub(axes, "line", x1=gx0, y1=0.0, x2=gx1, y2=0.0)

    ticks = _sub(root, "g", id="ticks", fill=TEXT, font_family=FONT, font_size=font)
    label_step = major * max(1, math.ceil(3.5 * font / major))
    for v in (v for v in xs if _multiple(v, label_step)):
        t = _sub(ticks, "text", x=v, y=gy0 - 0.5 * font, text_anchor="middle")
        t.set("class", "tick-x")
        t.text = _num(v, 3)
    for v in (v for v in ys if _multiple(v, label_step)):
        t = _sub(ticks, "text", x=gx0 - 0.4 * font, y=v + 0.35 * font, text_anchor="end")
        t.set("class", "tick-y")
        t.text = _num(v, 3)

    nested = _sub(root, "svg", x=vx, y=vy, width=vw, height=vh, overflow="visible")
    nested.set("viewBox", " ".join(_num(v) for v in (vx, vy, vw, vh)))
    nested.extend(list(kicad_svg))

    note = _sub(root, "g", id="legend", fill=TEXT, font_family=FONT, font_size=font)
    for i, text in enumerate(legend):
        t = _sub(note, "text", x=ox + 0.5 * font, y=gy1 + 1.6 * font + i * 1.5 * font)
        t.text = text
    if dims:
        group = _sub(root, "g", id="labels", fill=ACCENT)
        marks = _sub(group, "g", fill="none", stroke=ACCENT, stroke_width=0.004 * size)
        pad1 = next((p for p in geometry.pads if p.number == "1"), None)
        if pad1 is not None:
            _sub(marks, "circle", cx=pad1.at[0], cy=pad1.at[1], r=0.4 * font)
        pair = pitch_pad_pair(geometry)
        if pair is not None:
            (ax, ay), (bx, by) = pair[0].at, pair[1].at
            line = _sub(marks, "line", x1=ax, y1=ay, x2=bx, y2=by)
            line.set("stroke-dasharray", _num(0.6 * font))
    # depth is bounded by _clean's MAX_DEPTH, so the recursive serialiser is safe
    data = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    return _Wrapped(data, (ox, oy), (ow, oh), grid, major)


def build_wrapper(
    kicad_svg: ET.Element, geometry: FootprintGeometry, grid_mm: float | None, labels: bool
) -> bytes:
    """CopperChat's SVG around KiCad's (sanitised) drawing: mm grid (minor `grid_mm`,
    auto by default, coarsened to at most 500 lines per axis; major every 1 mm, every
    5 mm above 20 mm extent), axis lines through the origin, mm tick labels on the top
    and left edge, a legend stating the grid drawn, optional dimension labels, and
    KiCad's drawing as a nested `<svg>` with KiCad's `viewBox`."""
    return _build(kicad_svg, geometry, grid_mm, labels).svg


def _raster_size(
    extent_mm: tuple[float, float],
    max_side_px: int = MAX_SIDE_PX,
    min_px_per_mm: float = MIN_PX_PER_MM,
) -> tuple[int, int, float]:
    w, h = extent_mm
    longest = max(w, h)
    if not (math.isfinite(longest) and min(w, h) > 0):
        raise _failed("the drawing has no extent")
    scale = min(max(min_px_per_mm, TARGET_PX / longest), max_side_px / longest)
    return max(1, round(w * scale)), max(1, round(h * scale)), scale


def _worker_env(home: Path) -> dict[str, str]:
    """The worker's whole environment: no secret of the app's (tokens, passwords) is
    inherited; HOME (fontconfig's user cache) is a throw-away directory."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }


def _io_file(home: Path, name: str, flags: int) -> int:
    """A new file of the worker's I/O in its per-render directory (never an existing
    one: O_EXCL, O_NOFOLLOW), readable by the parent through the returned fd."""
    return os.open(home / name, flags | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _read_fd(fd: int, size: int) -> bytes:
    """The first `size` bytes of `fd` (fewer if it is shorter), from offset 0."""
    chunks: list[bytes] = []
    offset = 0
    while offset < size and (chunk := os.pread(fd, min(size - offset, 1 << 20), offset)):
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _open_worker_io(home: Path, svg: bytes) -> tuple[int, int, int]:
    """Blocking: `svg` written to the worker's stdin file; (stdin, stdout, stderr) fds,
    or `render_failed` with every fd closed."""
    fds: list[int] = []
    try:
        fds.append(_io_file(home, "worker-in.svg", os.O_WRONLY))
        _write_all(fds[0], svg)
        fds.append(os.open(home / "worker-in.svg", os.O_RDONLY | os.O_NOFOLLOW))
        fds.append(_io_file(home, "worker-out.png", os.O_RDWR))
        fds.append(_io_file(home, "worker-err.txt", os.O_RDWR))
        os.close(fds.pop(0))
    except OSError as exc:
        for fd in fds:
            os.close(fd)
        raise _failed(f"the rasteriser input could not be written ({type(exc).__name__})") from None
    return fds[0], fds[1], fds[2]


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the worker's process group (its own session: pgid == pid) while it has
    not been reaped, so the pid is not normally reused (strictly guaranteed only with
    the pidfd child watcher; a group that already exited or is no longer ours is
    ignored)."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)


async def _run_worker(
    svg: bytes,
    width: int,
    height: int,
    home: Path,
    *,
    timeout_s: float,
    limits: tuple[str, ...] = (),
) -> bytes:
    """The PNG from the worker process, or `render_failed` (exit code, timeout after
    `timeout_s`, oversized output). `limits`: the worker's optional CPU and memory argv
    items (board renders only; footprint renders pass none). Its stdin, stdout and
    stderr are files in `home` (no pipe, so the worker never blocks on the parent and
    the parent only waits for its exit). The worker is killed and reaped on every way
    out, a cancellation included (which waits for the reaping, then propagates)."""
    # file I/O of up to a board SVG (24 MiB) / a PNG (~17 MB): in a thread, not on the loop
    setup = asyncio.ensure_future(asyncio.to_thread(_open_worker_io, home, svg))
    try:
        in_fd, out_fd, err_fd = await asyncio.shield(setup)
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError):
            await _settle(setup)
        if not setup.cancelled() and setup.exception() is None:
            for fd in setup.result():
                os.close(fd)
        raise
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *RASTER_WORKER,
                str(width),
                str(height),
                *limits,
                cwd=home,
                env=_worker_env(home),
                stdin=in_fd,
                stdout=out_fd,
                stderr=err_fd,
                start_new_session=True,  # no terminal signals; its own process group
            )
        except OSError as exc:
            raise _failed(f"the rasteriser could not be started ({type(exc).__name__})") from None
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout_s)
        except TimeoutError:
            raise _failed(
                f"render timed out (rasterising took longer than {timeout_s:.0f} s)",
                "The drawing is too complex to render; use fp_get for its geometry.",
            ) from None
        finally:
            # killed and reaped on every way out, also when this task was cancelled:
            # the render lock is held until then (bounded: SIGKILL cannot be ignored)
            _kill_group(proc)
            await _settle(asyncio.ensure_future(asyncio.wait_for(proc.wait(), REAP_TIMEOUT_S)))
        if returncode != 0:
            # the worker writes only an exception class name; anything else (a
            # traceback of a failed start) is not quoted
            name = _read_fd(err_fd, 100).decode("ascii", "replace").strip()
            why = name if name.isidentifier() else f"exit code {returncode}"
            raise _failed(f"the drawing could not be rasterised ({why[:80]})")
        size = os.fstat(out_fd).st_size
        if size > MAX_PNG_BYTES:
            raise _failed("the rasteriser wrote more than a PNG of the drawing can hold")
        read = asyncio.ensure_future(asyncio.to_thread(_read_fd, out_fd, size))
        try:
            return await asyncio.shield(read)
        except asyncio.CancelledError:
            await _settle(read)  # the fd stays open until the thread is done with it
            raise
    finally:
        for fd in (in_fd, out_fd, err_fd):
            os.close(fd)


async def rasterise(
    svg: bytes,
    extent_mm: tuple[float, float],
    *,
    max_side_px: int = MAX_SIDE_PX,
    min_px_per_mm: float = MIN_PX_PER_MM,
    timeout_s: float | None = None,
    memory_bytes: int | None = None,
) -> tuple[bytes, float]:
    """PNG of `svg` (whose viewBox is `extent_mm`) and its px/mm: at least
    `min_px_per_mm` (40), the longer side at most `max_side_px` (2000). CairoSVG runs in
    a worker process (`_raster_worker`: address-space, CPU and file-size (RLIMIT_FSIZE)
    rlimits, minimal environment; its stdin/stdout/stderr are temp files, not pipes)
    that is killed after `timeout_s` (default `RASTER_TIMEOUT_S`, read at call time;
    `render_failed`, "render timed out").

    Only with `memory_bytes` (the board path, B-C9) the worker gets its CPU limit
    (`ceil(timeout_s)` s) and address-space limit (`memory_bytes`) as two extra argv
    items; without it the argv is exactly the footprint one (width, height) and the
    worker keeps its own constants."""
    width, height, scale = _raster_size(extent_mm, max_side_px, min_px_per_mm)
    wall = RASTER_TIMEOUT_S if timeout_s is None else timeout_s
    limits: tuple[str, ...] = ()
    if memory_bytes is not None:
        limits = (str(math.ceil(wall)), str(memory_bytes))
    home = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="cc-fp-raster-"))
    try:
        png = await _run_worker(svg, width, height, home, timeout_s=wall, limits=limits)
    finally:
        await _settle(
            asyncio.ensure_future(asyncio.to_thread(shutil.rmtree, home, ignore_errors=True))
        )
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _failed("the drawing could not be rasterised (no PNG)")
    return png, scale


# --- render_footprint -----------------------------------------------------------------


def _parse(lib_id: object) -> tuple[str, str, str]:
    if not isinstance(lib_id, str):
        raise _invalid("Invalid lib_id", "lib_id must be a string", "e.g. 'Device:R_0805'")
    lib_id = lib_id.strip()
    nickname, sep, name = lib_id.partition(":")
    if not sep or not nickname or not name:
        raise _invalid(
            f"Invalid lib_id {lib_id[:80]!r}",
            "expected <library nickname>:<footprint name>",
            "e.g. 'Resistor_SMD:R_0805_2012Metric'",
        )
    if any(bad in name for bad in ("/", "\\", "\x00", "..")) or "\x00" in nickname:
        raise _invalid(
            f"Invalid footprint name {name[:80]!r}",
            "a footprint name must not contain '/', '\\', NUL or '..'",
            "Use the name fp_get or lib_search_footprints reports",
        )
    return lib_id, nickname, name


def _layers(layers: list[str] | None, geometry: FootprintGeometry) -> list[str]:
    if layers is None:
        return default_layers(geometry)
    ok = isinstance(layers, list) and 0 < len(layers) <= MAX_LAYERS
    if not ok or not all(isinstance(n, str) and LAYER_RE.fullmatch(n) for n in layers):
        raise _invalid(
            "Invalid layers",
            f"1 to {MAX_LAYERS} names like F.Cu, B.SilkS, F.CrtYd or Edge.Cuts",
            "Omit layers for the default set",
        )
    return list(dict.fromkeys(layers))


def _check_grid(grid_mm: float | None) -> None:
    if grid_mm is None:
        return
    if isinstance(grid_mm, bool) or not isinstance(grid_mm, int | float):
        raise _invalid("Invalid grid_mm", "grid_mm must be a number", "Omit it for auto")
    if not math.isfinite(grid_mm) or grid_mm not in GRID_LADDER:
        # off the ladder the grid would be changed silently (0.2 -> 0.25)
        raise _invalid(
            "Invalid grid_mm",
            f"grid_mm must be one of {', '.join(str(g) for g in GRID_LADDER)}",
            "Omit it for auto",
        )


async def _library_dir(ctx: ToolContext, nickname: str) -> Path:
    uri = await library_for(ctx.session).footprint_library_uri(nickname)
    if not uri:
        raise _failed(f"library {nickname[:80]!r} has no footprint table row")
    path = Path(uri)
    if not path.is_absolute() or path.suffix != ".pretty":
        raise _failed(
            f"library {nickname[:80]!r} does not resolve to an absolute *.pretty directory",
            "Ask the user to check the footprint library table",
        )
    if not await asyncio.to_thread(path.is_dir):
        raise _failed(
            f"the directory of library {nickname[:80]!r} does not exist",
            "Ask the user to check the footprint library table",
        )
    return path


def _output_svg(tmp: Path, name: str) -> bytes:
    """KiCad's SVG from the temp dir; checked to exist and be non-empty (C10: the exit
    code is no proof)."""
    expected = tmp / f"{name}.svg"
    files = [expected] if expected.is_file() else sorted(tmp.glob("*.svg"))
    files = [f for f in files if f.is_file() and not f.is_symlink()]
    if len(files) != 1:
        raise _failed("kicad-cli wrote no SVG")
    size = files[0].stat().st_size
    if size == 0:
        raise _failed("kicad-cli wrote an empty SVG")
    if size > MAX_SVG_BYTES:
        raise _failed(f"kicad-cli wrote more than {MAX_SVG_BYTES} bytes of SVG")
    return files[0].read_bytes()


def _wrap(
    data: bytes, geometry: FootprintGeometry, grid_mm: float | None, labels: bool
) -> _Wrapped:
    return _build(load_kicad_svg(data), geometry, grid_mm, labels)


def _prepare(
    tmp: Path, name: str, geometry: FootprintGeometry, grid_mm: float | None, labels: bool
) -> _Wrapped:
    """The CPU part after kicad-cli before rasterising (blocking, linear in the capped
    input): read, sanitise, wrap."""
    return _wrap(_output_svg(tmp, name), geometry, grid_mm, labels)


async def _settle(fut: asyncio.Future[Any]) -> None:
    """Wait until `fut` (a worker thread, or the reaping of the rasteriser process) has
    finished, also when this task is cancelled meanwhile (a thread cannot be stopped;
    leaving it running would let the next render start beside it). A cancellation that
    arrived is re-raised after."""
    cancelled = False
    while not fut.done():
        try:
            await asyncio.wait({fut})
        except asyncio.CancelledError:
            cancelled = True
    if not fut.cancelled():
        fut.exception()  # retrieved: the caller reports its own error
    if cancelled:
        raise asyncio.CancelledError


def _tail(result_text: str) -> str:
    text = result_text.strip()
    return text if len(text) <= MAX_RAW_CHARS else "…" + text[-MAX_RAW_CHARS:]


async def render_footprint(
    ctx: ToolContext,
    lib_id: str,
    layers: list[str] | None,
    grid_mm: float | None,
    labels: bool,
    *,
    notes: list[dict[str, Any]] | None = None,
) -> RenderOutput:
    """PNG + wrapper SVG of one library footprint under `.kicad-agent/renders/<uuid32>`.

    Checks before kicad-cli: the name (`invalid_argument`), the geometry
    (`not_found`), the library directory (`render_failed`). Library notes
    (`footprint_libraries_loaded`, `footprint_attributes_unavailable`) go to `notes`."""
    lib_id, nickname, name = _parse(lib_id)
    _check_grid(grid_mm)
    svc = library_for(ctx.session)
    geometry = await svc.footprint_geometry(lib_id, notes)
    if geometry is None:
        close = await closest_footprints(svc, nickname, name)
        raise StructuredToolError(
            f"Footprint {lib_id[:120]!r} is not in the libraries",
            "unknown footprint (format <library nickname>:<footprint name>, case-sensitive)",
            f"Closest matches: {close}" if close else "Use lib_search_footprints.",
            code="not_found",
            details={"closest": close},
        )
    chosen = _layers(layers, geometry)
    directory = await _library_dir(ctx, nickname)
    args = ["fp", "export", "svg", str(directory), f"--fp={name}", "--layers", ",".join(chosen)]
    async with _render_lock:
        tmp = Path(tempfile.mkdtemp(prefix="cc-fp-render-"))
        try:
            try:
                result = await run_kicad_cli(
                    ctx.settings, [*args, "-o", str(tmp)], timeout=CLI_TIMEOUT_S, cwd=tmp
                )
            except TimeoutError:
                raise _failed(
                    f"kicad-cli fp export took longer than {CLI_TIMEOUT_S:.0f} s"
                ) from None
            if result.returncode != 0:
                raise _failed(
                    f"kicad-cli fp export exited with {result.returncode}: "
                    f"{_tail(result.stderr or result.stdout)}"
                )
            work = asyncio.ensure_future(
                asyncio.to_thread(_prepare, tmp, name, geometry, grid_mm, labels)
            )
            try:
                wrapped = await asyncio.shield(work)
            except asyncio.CancelledError:
                await _settle(work)  # hold the semaphore until the thread has ended
                raise
            # a process of its own, killed after RASTER_TIMEOUT_S and reaped (also on
            # cancellation) before the semaphore is released
            png, scale = await rasterise(wrapped.svg, wrapped.extent_mm)
        finally:
            await _settle(
                asyncio.ensure_future(asyncio.to_thread(shutil.rmtree, tmp, ignore_errors=True))
            )
    out_dir = agent_dir(ctx.project) / "renders"
    stem = uuid.uuid4().hex
    png_path, svg_path = out_dir / f"{stem}.png", out_dir / f"{stem}.svg"

    def write() -> None:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            png_path.write_bytes(png)
            svg_path.write_bytes(wrapped.svg)
        except OSError as exc:
            for path in (png_path, svg_path):  # never leave half a render behind
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
            raise _failed(
                f"the render could not be stored ({type(exc).__name__})",
                "Ask the user to check the project directory (disk space, permissions)",
            ) from None

    await asyncio.to_thread(write)
    return RenderOutput(
        png_path,
        svg_path,
        scale,
        wrapped.extent_mm,
        geometry_hash(geometry),
        wrapped.origin_mm,
        wrapped.grid_mm,
    )
