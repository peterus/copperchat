"""Footprint render pipeline (spec §7.5, FP-C5): `kicad-cli fp export svg` of one
library footprint, wrapped in CopperChat's own SVG (mm grid, axes, ticks, dimension
labels) and rasterised to PNG with CairoSVG.

Hardening (spec §7.5, C10):
- one `kicad-cli fp export` at a time: the **whole** render (cli, parse, wrap,
  rasterise) runs under the module semaphore;
- argv list only, `--fp=<name>` one element; the library directory comes from the
  footprint table (absolute, existing, `*.pretty`), the name never holds a path;
- KiCad's output must exist, be non-empty and at most 10 MB; any `<!DOCTYPE` other than
  KiCad's own SVG 1.1 public prolog, and any `<!ENTITY`, is refused; scripts, styles,
  foreign objects, event handlers, non-fragment links and external `url(...)` are
  dropped while it is re-serialised with `xml.etree` (never by string concatenation);
- CairoSVG runs with a URL fetcher that refuses everything, never with `unsafe=True`.

Coordinates: mm in footprint space (origin = anchor, y down). KiCad's SVG `viewBox` is
that space (spike Q8), so the wrapper draws in it directly and embeds KiCad's drawing as
a nested `<svg>` with the same `viewBox`.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cairosvg.surface  # type: ignore[import-untyped]

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.cli import run_kicad_cli
from copperchat.kicad_tools.footprints.checks import pitch_pad_pair
from copperchat.kicad_tools.footprints.model import FootprintGeometry, geometry_hash
from copperchat.library.service import closest_footprints, library_for
from copperchat.projects import agent_dir
from copperchat.tools.spec import ToolContext

SVG_NS = "http://www.w3.org/2000/svg"
XML_NS = "http://www.w3.org/XML/1998/namespace"
MAX_SVG_BYTES = 10 * 1024 * 1024
MAX_DEPTH = 64  # KiCad's SVG nests 2-3 levels
MAX_VIEW_MM = 10_000.0
CLI_TIMEOUT_S = 60.0
MIN_PX_PER_MM = 40.0
TARGET_PX = 1200.0  # longest side, unless 40 px/mm needs more
MAX_SIDE_PX = 2000
MAX_GRID_LINES = 500  # per axis
MAX_RAW_CHARS = 500  # KiCad output quoted in an error
MAX_LABEL_NUMBER = 16
FRONT = ("F.Cu", "F.Mask", "F.Paste", "F.SilkS", "F.Fab", "F.CrtYd")
BACK = ("B.Cu", "B.Mask", "B.Paste", "B.SilkS", "B.Fab", "B.CrtYd")
MAX_LAYERS = 32
LAYER_RE = re.compile(r"^(?:(?:F|B)\.(?:Cu|Mask|Paste|SilkS|Fab|CrtYd)|Edge\.Cuts)$")
GRID_LADDER = (0.1, 0.25, 0.5, 1.0)
DROP_TAGS = frozenset(
    {
        "script",
        "style",
        "foreignobject",
        "set",
        "animate",
        "animatemotion",
        "animatetransform",
        "iframe",
        "object",
        "embed",
        "handler",
        "listener",
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
_EXTERNAL_URL = re.compile(r"""url\((?!\s*['"]?\s*#)[^)]*\)""", re.IGNORECASE)

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


def _refuse(url: str, resource_type: str) -> bytes:
    """CairoSVG URL fetcher: the wrapper never needs an external resource."""
    raise ValueError(f"external resource refused: {url[:100]}")


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


def _clean(src: ET.Element, depth: int) -> ET.Element | None:
    """An un-namespaced copy of an SVG element without active content; None to drop."""
    if depth > MAX_DEPTH:
        raise _failed("kicad-cli wrote an SVG nested too deeply")
    ns, local = _split(src.tag) if isinstance(src.tag, str) else ("?", "")
    if ns != SVG_NS or local.lower() in DROP_TAGS:
        return None
    out = ET.Element(local)
    for key, value in src.attrib.items():
        kns, klocal = _split(key)
        if klocal.lower().startswith("on"):
            continue
        if klocal == "href":  # any namespace (xlink:href, href)
            if value.strip().startswith("#"):
                out.set("href", value.strip())
            continue
        if kns and not (kns == XML_NS and klocal == "space"):
            continue  # inkscape:, sodipodi:, xml:base, ...
        if "\\" in value and "url" in value.lower():
            continue  # CSS escapes could hide a url(...)
        out.set(key, _EXTERNAL_URL.sub("none", value))
    out.text = src.text
    out.tail = src.tail
    for child in src:
        cleaned = _clean(child, depth + 1)
        if cleaned is not None:
            out.append(cleaned)
    return out


def load_kicad_svg(data: bytes) -> ET.Element:
    """KiCad's SVG, parsed and sanitised (an un-namespaced `svg` element tree), or
    `render_failed` for output over 10 MB, a foreign DOCTYPE, an ENTITY declaration or
    markup `xml.etree` cannot parse."""
    if len(data) > MAX_SVG_BYTES:
        raise _failed(f"kicad-cli wrote more than {MAX_SVG_BYTES} bytes of SVG")
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
    cleaned = _clean(root, 0)
    assert cleaned is not None
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
        (w, h), (x, y) = pad1.size, pad1.at
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


def rasterise(svg: bytes, extent_mm: tuple[float, float]) -> tuple[bytes, float]:
    """PNG of `svg` (whose viewBox is `extent_mm`) and its px/mm: at least 40 px/mm,
    the longer side at most 2000 px. Blocking: call it in `asyncio.to_thread`."""
    w, h = extent_mm
    longest = max(w, h)
    if not (math.isfinite(longest) and min(w, h) > 0):
        raise _failed("the drawing has no extent")
    scale = min(max(MIN_PX_PER_MM, TARGET_PX / longest), MAX_SIDE_PX / longest)
    width, height = max(1, round(w * scale)), max(1, round(h * scale))
    try:
        png = cairosvg.surface.PNGSurface.convert(
            bytestring=svg,
            url_fetcher=_refuse,
            unsafe=False,
            output_width=width,
            output_height=height,
        )
    except Exception as exc:
        raise _failed(f"the drawing could not be rasterised ({type(exc).__name__})") from None
    if not isinstance(png, bytes):
        raise _failed("the drawing could not be rasterised")
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
    if not math.isfinite(grid_mm) or grid_mm <= 0:
        raise _invalid("Invalid grid_mm", "grid_mm must be a positive number", "Omit it for auto")


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
            data = await asyncio.to_thread(_output_svg, tmp, name)
            wrapped = await asyncio.to_thread(_wrap, data, geometry, grid_mm, labels)
            png, scale = await asyncio.to_thread(rasterise, wrapped.svg, wrapped.extent_mm)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
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
