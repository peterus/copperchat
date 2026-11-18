"""Footprint render pipeline without KiCad (F2 Task 5, spec §7.5): KiCad SVG hardening,
the wrapper (grid, axes, ticks, labels), rasterising and `render_footprint`'s checks,
temp-dir cleanup and one-render-at-a-time."""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import os
import random
import socket
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.cli import CliResult
from copperchat.kicad_tools.footprints.model import FootprintGeometry, geometry_hash
from copperchat.projects import Project
from copperchat.tools import ToolContext
from copperchat.views import _raster_worker as raster_worker
from copperchat.views import footprint_render as fr
from tests.unit.footprints.test_checks import P, geo, l201, rect

SVG_NS = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
S = f"{{{SVG_NS}}}"


def kicad_svg(body: str = "", view_box: str = "-2 -2 4 4", prolog: str = "") -> bytes:
    return (
        f'{prolog}<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK}" version="1.1" '
        f'width="4mm" height="4mm" viewBox="{view_box}">{body}</svg>'
    ).encode()


def _code(exc: pytest.ExceptionInfo[StructuredToolError]) -> str:
    return exc.value.code


# --- load_kicad_svg -------------------------------------------------------------------


def test_kicad_prolog_accepted() -> None:
    """KiCad's own output carries the SVG 1.1 public DOCTYPE (seen in the base image):
    exactly that prolog is stripped, nothing else."""
    prolog = (
        '<?xml version="1.0" standalone="no"?>\n <!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" \n'
        ' "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"> \n'
    )
    root = fr.load_kicad_svg(kicad_svg('<path d="M 0 0 L 1 1"/>', prolog=prolog))
    assert root.tag == "svg"  # sanitised copy, un-namespaced
    assert [el.tag for el in root] == ["path"]


@pytest.mark.parametrize(
    "prolog",
    [
        "<!DOCTYPE svg [<!ELEMENT svg ANY>]>",
        '<!DOCTYPE svg SYSTEM "file:///etc/passwd">',
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd" [<!ATTLIST svg a CDATA "x">]>',
    ],
)
def test_doctype_refused(prolog: str) -> None:
    with pytest.raises(StructuredToolError) as exc:
        fr.load_kicad_svg(kicad_svg(prolog=prolog))
    assert _code(exc) == "render_failed"


def test_entity_refused() -> None:
    data = kicad_svg(prolog='<!DOCTYPE svg [<!ENTITY a "aaaa">]>', body="<text>&a;</text>")
    with pytest.raises(StructuredToolError) as exc:
        fr.load_kicad_svg(data)
    assert _code(exc) == "render_failed"
    # also a bare ENTITY declaration after the stripped KiCad prolog
    with pytest.raises(StructuredToolError) as exc:
        fr.load_kicad_svg(kicad_svg(body="<!ENTITY b 'x'>"))
    assert _code(exc) == "render_failed"


ENTITY_DOC = (
    f'<!DOCTYPE svg [<!ENTITY a "PWNED">]><svg xmlns="{SVG_NS}" viewBox="0 0 1 1">'
    "<text>&a;</text></svg>"
)


@pytest.mark.parametrize(
    "data",
    [
        ENTITY_DOC.encode("utf-16"),  # with BOM
        ENTITY_DOC.encode("utf-16-le"),  # without BOM
        ENTITY_DOC.encode("utf-16-be"),
        ('<?xml version="1.0" encoding="UTF-16"?>' + ENTITY_DOC).encode("utf-16"),
        ('<?xml version="1.0" encoding="UTF-16"?>' + ENTITY_DOC).encode("utf-16-le"),
        ENTITY_DOC.encode("utf-32"),
    ],
)
def test_non_utf8_refused(data: bytes) -> None:
    """The DOCTYPE/ENTITY check must see what expat parses: UTF-16/32 is refused."""
    with pytest.raises(StructuredToolError) as exc:
        fr.load_kicad_svg(data)
    assert _code(exc) == "render_failed"


def test_utf8_bom_and_declared_encoding() -> None:
    """A UTF-8 BOM is dropped; the parsed text never follows a declared encoding."""
    root = fr.load_kicad_svg(b"\xef\xbb\xbf" + kicad_svg('<path d="M 0 0"/>'))
    assert [el.tag for el in root] == ["path"]
    declared = b'<?xml version="1.0" encoding="UTF-16"?>' + kicad_svg("<text>\u00e4</text>")
    root = fr.load_kicad_svg(declared)
    assert [el.text for el in root] == ["\u00e4"]
    with pytest.raises(StructuredToolError) as exc:
        fr.load_kicad_svg(kicad_svg("<text>a\x00b</text>"))
    assert _code(exc) == "render_failed"


def test_unparsable_refused() -> None:
    for bad in (b"<svg", b"not xml", f'<x xmlns="{SVG_NS}"/>'.encode()):
        with pytest.raises(StructuredToolError) as exc:
            fr.load_kicad_svg(bad)
        assert _code(exc) == "render_failed"


def test_oversize_output_refused() -> None:
    data = kicad_svg()
    data = data + b" " * (fr.MAX_SVG_BYTES + 1 - len(data))
    assert len(data) == 4 * 1024 * 1024 + 1
    with pytest.raises(StructuredToolError) as exc:
        fr.load_kicad_svg(data)
    assert _code(exc) == "render_failed"
    fr.load_kicad_svg(data[:-1])  # exactly 4 MB is fine


def test_element_cap() -> None:
    """At most 10,000 kept elements (the largest stock export has 6,205); dropped
    elements do not count."""
    path = '<path d="M 0 0 L 0.01 0"/>'
    root = fr.load_kicad_svg(kicad_svg(path * (fr.MAX_ELEMENTS - 1)))  # + the root
    assert len(root) == fr.MAX_ELEMENTS - 1
    with pytest.raises(StructuredToolError) as exc:
        fr.load_kicad_svg(kicad_svg(path * fr.MAX_ELEMENTS))
    assert _code(exc) == "render_failed" and "10000 elements" in exc.value.why
    fr.load_kicad_svg(kicad_svg(path * 10 + "<script/>" * (2 * fr.MAX_ELEMENTS)))


def test_group_opacity_dropped() -> None:
    """Group opacity (attribute or style) goes: nested, it multiplies CairoSVG's work;
    fill-/stroke-opacity stay. KiCad's hidden texts (opacity 0) go entirely."""
    body = (
        '<g opacity="0.5" style="fill:#fff; opacity : 0.4;fill-opacity:0.4">'
        '<g style="OPACITY:0.9"><path d="M 0 0 L 1 1" stroke-opacity="0.5"/></g>'
        '<text opacity="0" stroke-opacity="0">REF**</text>'
        '<text style="opacity:0.0">hidden</text>'
        '<text opacity="0.2">shown</text></g>'
    )
    root = fr.load_kicad_svg(kicad_svg(body))
    attrs = _all_attrs(root)
    assert not [k for k, _ in attrs if k == "opacity"]
    styles = [v for k, v in attrs if k == "style"]
    assert styles == ["fill:#fff;fill-opacity:0.4", ""]
    assert ("stroke-opacity", "0.5") in attrs
    assert [t.text for t in root.iter("text")] == ["shown"]


def _all_attrs(root: ET.Element) -> list[tuple[str, str]]:
    return [(k, v) for el in root.iter() for k, v in el.attrib.items()]


def test_active_content_dropped() -> None:
    body = (
        "<script>alert(1)</script>"
        "<style>path{fill:url(http://x/a)}</style>"
        '<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>'
        '<g onload="alert(1)" ONCLICK="x"><path id="a" d="M 0 0 L 1 1"/></g>'
        '<image href="http://x/i.png" width="1" height="1"/>'
        '<image xlink:href="http://x/j.png" width="1" height="1"/>'
        '<image href="data:image/png;base64,AAAA" width="1" height="1"/>'
        '<path style="fill:url(http://x);stroke:url( \'#b\' )" fill="url(http://y)" d="M 0 0"/>'
        '<linearGradient href="#a"/><linearGradient xlink:href="#a"/>'
        '<set attributeName="href" to="http://x"/>'
    )
    root = fr.load_kicad_svg(kicad_svg(body))
    out = fr.build_wrapper(root, l201(), 0.5, True)
    wrapped = ET.fromstring(out)
    tags = {el.tag.split("}")[-1] for el in wrapped.iter()}
    assert not tags & {"script", "style", "foreignObject", "div", "set", "image"}
    assert not tags & {"linearGradient"}
    attrs = _all_attrs(wrapped)
    assert not [k for k, _ in attrs if k.split("}")[-1].lower().startswith("on")]
    assert not [k for k, _ in attrs if k.split("}")[-1] == "href"]
    assert not [v for _, v in attrs if "url(" in v or "data:" in v]
    assert b"<!DOCTYPE" not in out and b"<?xml" not in out


def test_unterminated_external_url_dropped() -> None:
    """CSS closes an unterminated url( at the end of the value: still external."""
    body = (
        '<path style="fill:url(https://evil/x" d="M 0 0"/>'
        '<path fill="url(  \'https://evil/y\'" d="M 0 0"/>'
        '<path style="fill:url(#ok" d="M 0 0"/>'
    )
    root = fr.load_kicad_svg(kicad_svg(body))
    attrs = _all_attrs(root)
    assert not [v for _, v in attrs if "url(" in v]  # also the fragment one
    assert [el.get("d") for el in root] == ["M 0 0"] * 3  # the elements stay


def test_nested_use_dropped() -> None:
    """An SVG billion laughs (nested `use` x10 per level) never reaches cairosvg."""
    body = '<path id="l0" d="M 0 0 L 1 1"/>'
    for k in range(1, 6):
        uses = "".join(f'<use href="#l{k - 1}"/>' for _ in range(10))
        body += f'<g id="l{k}">{uses}</g>'
    body += '<tref href="#l0"/>'
    root = fr.load_kicad_svg(kicad_svg(body))
    wrapped = ET.fromstring(fr.build_wrapper(root, l201(), 0.5, True))
    tags = {el.tag.split("}")[-1] for el in wrapped.iter()}
    assert not tags & {"use", "tref"}


# --- review-loop round 3: element allowlist ---------------------------------------------


def _nested(tag: str, attr: str, extra: str = "") -> str:
    """6 levels of 10 references each to the level below (10**6 paints)."""
    body = '<path id="l0" d="M 0 0 L 1 1"/>'
    for k in range(1, 7):
        refs = "".join(f'<rect width="1" height="1" {attr}="url(#l{k - 1})"/>' for _ in range(10))
        body += f'<{tag} id="l{k}" {extra}>{refs}</{tag}>'
    return body + f'<path d="M 0 0" {attr}="url(#l6)" style="{attr}:url(#l6)"/>'


@pytest.mark.parametrize(
    ("tag", "attr", "extra"),
    [
        ("pattern", "fill", 'width="1" height="1" patternUnits="userSpaceOnUse"'),
        ("marker", "marker-start", 'markerWidth="1" markerHeight="1"'),
        ("mask", "mask", ""),
        ("filter", "filter", ""),
        ("clipPath", "clip-path", ""),
    ],
)
def test_nested_paint_servers_dropped(tag: str, attr: str, extra: str) -> None:
    """Nested pattern/marker/mask/filter/clipPath (referenced via url(#...)) amplify like
    `use`: the elements go with their subtrees and every url( attribute goes too."""
    nested = _nested(tag, attr, extra)
    root = fr.load_kicad_svg(kicad_svg(f"<defs>{nested}</defs>{nested}"))
    tags = {el.tag for el in root.iter()}
    assert tags <= fr.ALLOWED_TAGS
    assert not tags & {tag, "defs", "rect"}  # rects only lived inside the dropped levels
    assert not [v for _, v in _all_attrs(root) if "url(" in v]
    # the level-0 path outside <defs> and the referencing path stay, without the url(
    assert [el.get("d") for el in root.iter("path")] == ["M 0 0 L 1 1", "M 0 0"]


def test_image_and_foreign_object_dropped() -> None:
    body = (
        '<image href="data:image/png;base64,AAAA" width="1" height="1"/>'
        '<foreignObject width="1" height="1"><svg><path d="M 0 0"/></svg></foreignObject>'
        '<switch><path d="M 1 1"/></switch><a href="#x"><path d="M 2 2"/></a>'
        '<g><path d="M 3 3"/></g>'
    )
    root = fr.load_kicad_svg(kicad_svg(body))
    assert [el.tag for el in root.iter()] == ["svg", "g", "path"]
    assert [el.get("d") for el in root.iter("path")] == ["M 3 3"]


KICAD_LIKE = (
    "<title>SVG Image created as L.svg date 2026-11-18T06:43:05 </title>"
    "<desc>Image generated by PCBNEW </desc>"
    '<g style="fill:#000000; fill-opacity:1.000000;stroke:#000000; stroke-opacity:1.000000;'
    ' stroke-linecap:round; stroke-linejoin:round;" transform="translate(0 0) scale(1 1)">'
    '<path style="fill:#C83434; fill-opacity:1.000000; stroke:none;fill-rule:evenodd;"'
    ' d="M -2.275000,1.800000 -2.275000,-1.800000 -0.775000,-1.800000 Z"/>'
    '<circle cx="0.000000" cy="0.000000" r="0.500000"/>'
    '<g class="stroked-text"><desc>REF**</desc><path d="M 0 0 L 1 1"/></g>'
    '<text x="0" y="0" textLength="1" font-size="1" lengthAdjust="spacingAndGlyphs"'
    ' text-anchor="middle" opacity="0" stroke-opacity="0">REF**</text>'
    "</g>"
)


def test_kicad_like_sample_unchanged() -> None:
    """Every element and attribute KiCad's fp export writes survives unchanged, except
    its hidden (opacity 0) texts, which are drawn as nothing anyway."""
    src = ET.fromstring(kicad_svg(KICAD_LIKE))
    root = fr.load_kicad_svg(kicad_svg(KICAD_LIKE))

    def shape(el: ET.Element) -> list[tuple[str, dict[str, str], str | None]]:
        return [(e.tag.split("}")[-1], dict(e.attrib), e.text) for e in el.iter()]

    visible = [e for e in shape(src) if e[1].get("opacity") != "0"]
    assert len(visible) < len(shape(src))
    assert shape(root) == visible


# --- rasterise ------------------------------------------------------------------------


def test_url_fetcher_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's CairoSVG call (run in-process here) asks the refusing fetcher and
    never opens a socket."""
    calls: list[str] = []
    real = raster_worker.refuse

    def spy(url: str, resource_type: str) -> bytes:
        calls.append(url)
        return real(url, resource_type)

    def no_socket(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(raster_worker, "refuse", spy)
    monkeypatch.setattr(socket, "socket", no_socket)
    svg = kicad_svg('<image href="http://127.0.0.1:1/x.png" width="2" height="2"/>')
    with pytest.raises(ValueError):
        raster_worker.convert(svg, 160, 160)
    assert calls == ["http://127.0.0.1:1/x.png"]
    with pytest.raises(ValueError):
        real("http://127.0.0.1:1/x.png", "image/*")


async def test_worker_error_is_render_failed() -> None:
    """An external resource (refused in the worker) or no SVG at all: the worker exits
    non-zero and the render is `render_failed` naming only the exception class."""
    svg = kicad_svg('<image href="http://127.0.0.1:1/x.png" width="2" height="2"/>')
    for data in (svg, b"not svg"):
        with pytest.raises(StructuredToolError) as exc:
            await fr.rasterise(data, (4.0, 4.0))
        assert _code(exc) == "render_failed" and "could not be rasterised" in exc.value.why


def _png_size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as img:
        assert img.format == "PNG"
        return img.size


async def test_raster_size() -> None:
    """The real worker process rasterises (no KiCad needed)."""
    png, scale = await fr.rasterise(
        kicad_svg('<rect x="-1" y="-1" width="2" height="2"/>'), (4.0, 4.0)
    )
    w, h = _png_size(png)
    assert scale >= 40 and w >= 160 and h >= 160 and max(w, h) <= 2000
    assert w == round(4 * scale)
    wide = kicad_svg(view_box="0 0 100 10").replace(b'width="4mm" height="4mm"', b"")
    png, scale = await fr.rasterise(wide, (100.0, 10.0))
    assert _png_size(png) == (2000, 200)
    assert scale == pytest.approx(20.0)


# --- wrapper --------------------------------------------------------------------------


def _group(root: ET.Element, gid: str) -> ET.Element:
    found = root.find(f".//{S}g[@id='{gid}']")
    assert found is not None, gid
    return found


def _lines(root: ET.Element, gid: str) -> tuple[list[float], list[float]]:
    """(x of vertical lines, y of horizontal lines) of one group."""
    xs: list[float] = []
    ys: list[float] = []
    for ln in _group(root, gid).iter(f"{S}line"):
        if float(ln.get("x1", "nan")) == float(ln.get("x2", "nan")):
            xs.append(float(ln.get("x1", "nan")))
        else:
            ys.append(float(ln.get("y1", "nan")))
    return sorted(xs), sorted(ys)


def _steps(values: Sequence[float]) -> set[float]:
    return {round(b - a, 6) for a, b in itertools.pairwise(values)}


def test_auto_grid() -> None:
    assert [fr.auto_grid(e) for e in (1, 5, 5.01, 20, 20.5, 50, 51, 300)] == [
        0.1,
        0.1,
        0.25,
        0.25,
        0.5,
        0.5,
        1.0,
        1.0,
    ]


def test_wrapper_structure() -> None:
    pads = (P("1", (-1.0, 0.0), (0.6, 0.8)), P("<&>", (1.0, 0.0), (0.6, 0.8)))
    g = geo(pads, [rect("F.CrtYd", -1.5, -0.75, 1.5, 0.75)])
    out = fr.build_wrapper(fr.load_kicad_svg(kicad_svg('<path d="M 0 0"/>')), g, 0.1, True)
    root = ET.fromstring(out)  # well-formed, label text escaped by the XML API
    assert root.tag == f"{S}svg"
    nested = [el for el in root.iter(f"{S}svg") if el is not root]
    assert len(nested) == 1
    assert [float(v) for v in nested[0].get("viewBox", "").split()] == [-2, -2, 4, 4]
    assert nested[0].find(f"{S}path") is not None  # KiCad's content inside
    outer = [float(v) for v in root.get("viewBox", "").split()]
    assert outer[0] < -2 and outer[1] < -2 and outer[2] > 4 and outer[3] > 4
    minor_x, minor_y = _lines(root, "grid-minor")
    major_x, major_y = _lines(root, "grid-major")
    assert _steps(sorted(minor_x + major_x)) == {0.1}
    assert _steps(sorted(minor_y + major_y)) == {0.1}
    assert _steps(major_x) == {1.0} and _steps(major_y) == {1.0}
    assert {-2.0, -1.0, 0.0, 1.0, 2.0} <= set(major_x)
    axes_x, axes_y = _lines(root, "axes")
    assert axes_x == [0.0] and axes_y == [0.0]
    ticks = _group(root, "ticks")
    tick_texts = list(ticks.iter(f"{S}text"))
    top = {t.text for t in tick_texts if t.get("class") == "tick-x"}
    left = {t.text for t in tick_texts if t.get("class") == "tick-y"}
    assert {"-1", "0", "1"} <= top and {"-1", "0", "1"} <= left
    labels = "\n".join(_legend(root))
    assert "<&>" in labels
    assert "pad 1: 0.6 x 0.8 mm at (-1, 0)" in labels
    assert "pitch 1 → <&>: 2 mm" in labels
    assert "courtyard: 3 x 1.5 mm" in labels
    assert b"&lt;&amp;&gt;" in out


def _legend(root: ET.Element) -> list[str]:
    return [t.text or "" for t in _group(root, "legend").iter(f"{S}text")]


def test_labels_off() -> None:
    out = fr.build_wrapper(fr.load_kicad_svg(kicad_svg()), l201(), None, False)
    root = ET.fromstring(out)
    assert root.find(f".//{S}g[@id='labels']") is None
    assert _legend(root) == ["grid 0.1 mm, major 1 mm"]  # the grid is always stated


@pytest.mark.parametrize("grid", [0.1, 0.25, 0.5, 1.0])
def test_requested_grid_kept_and_stated(grid: float) -> None:
    root = ET.fromstring(fr.build_wrapper(fr.load_kicad_svg(kicad_svg()), l201(), grid, True))
    minor_x, _ = _lines(root, "grid-minor")
    major_x, _ = _lines(root, "grid-major")
    assert _steps(sorted(minor_x + major_x)) == {grid}
    assert _legend(root)[0] == f"grid {grid:g} mm, major 1 mm"


def test_labels_without_pad_1_or_courtyard() -> None:
    g = geo([P("A", (0.0, 0.0))])
    root = ET.fromstring(fr.build_wrapper(fr.load_kicad_svg(kicad_svg()), g, None, True))
    texts = [t.text or "" for t in root.iter(f"{S}text")]
    assert not [t for t in texts if t.startswith(("pad 1", "pitch", "courtyard"))]


def test_grid_coarsened_for_large_extent() -> None:
    svg = fr.load_kicad_svg(kicad_svg(view_box="-100 -100 200 200"))
    root = ET.fromstring(fr.build_wrapper(svg, l201(), 0.1, True))
    minor_x, minor_y = _lines(root, "grid-minor")
    major_x, major_y = _lines(root, "grid-major")
    assert len(minor_x) + len(major_x) <= 500
    assert len(minor_y) + len(major_y) <= 500
    assert _legend(root)[0] == "grid 0.5 mm, major 5 mm"  # coarsened, and said so


def test_major_every_5mm_above_20() -> None:
    svg = fr.load_kicad_svg(kicad_svg(view_box="-15 -15 30 30"))
    root = ET.fromstring(fr.build_wrapper(svg, l201(), None, False))
    major_x, major_y = _lines(root, "grid-major")
    assert _steps(major_x) == {5.0} and _steps(major_y) == {5.0}
    minor_x, _ = _lines(root, "grid-minor")
    assert _steps(sorted(minor_x + major_x)) == {0.5}  # auto grid for 30 mm


def test_bad_view_box_refused() -> None:
    for vb in ("0 0 0 4", "a b c d", "0 0 4"):
        with pytest.raises(StructuredToolError) as exc:
            fr.build_wrapper(fr.load_kicad_svg(kicad_svg(view_box=vb)), l201(), None, True)
        assert _code(exc) == "render_failed"


def test_default_layers() -> None:
    assert fr.default_layers(l201()) == [
        "F.Cu",
        "F.Mask",
        "F.Paste",
        "F.SilkS",
        "F.Fab",
        "F.CrtYd",
    ]
    tht = geo([P("1", (0.0, 0.0), layers=("*.Cu", "*.Mask"), type="tht", drill=1.0)])
    assert fr.default_layers(tht)[6:] == ["B.Cu", "B.Mask"]
    bottom = geo([], [rect("B.SilkS", 0, 0, 1, 1), rect("B.CrtYd", 0, 0, 1, 1)])
    assert fr.default_layers(bottom)[6:] == ["B.SilkS", "B.CrtYd"]


# --- render_footprint -----------------------------------------------------------------


class FakeService:
    def __init__(self, geometry: FootprintGeometry | None, uri: str | None) -> None:
        self.geometry = geometry
        self.uri = uri
        self.uri_calls = 0

    async def footprint_geometry(self, lib_id: str, notes: Any = None) -> Any:
        if notes is not None:
            notes.append({"kind": "footprint_attributes_unavailable", "lib_id": lib_id})
        return self.geometry

    async def footprint_library_uri(self, nickname: str) -> str | None:
        self.uri_calls += 1
        return self.uri


@pytest.fixture
def ctx(tmp_path: Path, make_settings: Any) -> ToolContext:
    d = tmp_path / "demo"
    d.mkdir()
    project = Project(
        "a1b2c3d4e5f6", "demo", "demo/demo.kicad_pro", d, d / "demo.kicad_pro", None, None
    )
    settings: Settings = make_settings()
    return ToolContext(project=project, session=cast(KicadSession, None), settings=settings)


@pytest.fixture
def lib_dir(tmp_path: Path) -> Path:
    d = tmp_path / "Lib.pretty"
    d.mkdir()
    return d


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch, lib_dir: Path) -> FakeService:
    s = FakeService(l201(), str(lib_dir))
    monkeypatch.setattr(fr, "library_for", lambda _session: s)

    async def closest(_svc: Any, _nickname: str, _name: str) -> list[str]:
        return []

    monkeypatch.setattr(fr, "closest_footprints", closest)
    # a fresh semaphore per test (the module one binds to the first contended loop)
    monkeypatch.setattr(fr, "_render_lock", asyncio.Semaphore(1))
    return s


class FakeCli:
    def __init__(self, write: bytes | None = None, *, delay: float = 0.0, rc: int = 0) -> None:
        self.write = write
        self.delay = delay
        self.rc = rc
        self.calls: list[tuple[list[str], Path | None]] = []
        self.events: list[str] = []

    async def __call__(
        self, settings: Settings, args: Sequence[str], *, timeout: float, cwd: Path | None = None
    ) -> CliResult:
        assert timeout == 60
        self.calls.append((list(args), cwd))
        self.events.append("start")
        await asyncio.sleep(self.delay)
        out = Path(args[args.index("-o") + 1])
        name = next(a for a in args if a.startswith("--fp=")).removeprefix("--fp=")
        if self.write is not None:
            (out / f"{name}.svg").write_bytes(self.write)
        self.events.append("end")
        return CliResult(self.rc, "", "boom " * 400)


def _no_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*_a: Any, **_k: Any) -> CliResult:
        raise AssertionError("kicad-cli spawned")

    monkeypatch.setattr(fr, "run_kicad_cli", fail)


async def test_render_ok(
    ctx: ToolContext, svc: FakeService, lib_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = FakeCli(kicad_svg('<path d="M -1 -1 L 1 1"/>'))
    monkeypatch.setattr(fr, "run_kicad_cli", cli)
    out = await fr.render_footprint(ctx, "Lib:R_1", None, None, True)
    ((args, cwd),) = cli.calls
    tmp = Path(args[args.index("-o") + 1])
    assert args[:4] == ["fp", "export", "svg", str(lib_dir)]
    assert "--fp=R_1" in args
    assert args[args.index("--layers") + 1] == "F.Cu,F.Mask,F.Paste,F.SilkS,F.Fab,F.CrtYd"
    assert cwd == tmp and not tmp.exists()
    renders = ctx.project.dir / ".kicad-agent" / "renders"
    assert out.png.parent == renders and out.svg.parent == renders
    assert out.png.stem == out.svg.stem and len(out.png.stem) == 32
    assert out.png.suffix == ".png" and out.svg.suffix == ".svg"
    w, h = _png_size(out.png.read_bytes())
    assert w == round(out.extent_mm[0] * out.scale_px_per_mm)
    assert h == round(out.extent_mm[1] * out.scale_px_per_mm)
    assert ET.fromstring(out.svg.read_bytes()).tag == f"{S}svg"
    assert out.geometry_hash == geometry_hash(l201())
    assert out.grid_mm == 0.1  # auto grid for the 4 mm drawing
    # origin_mm + extent_mm is the wrapper viewBox: px map back to footprint mm
    view_box = [float(v) for v in ET.fromstring(out.svg.read_bytes()).get("viewBox", "").split()]
    assert view_box == pytest.approx([*out.origin_mm, *out.extent_mm], abs=1e-6)
    assert out.origin_mm[0] < -2 and out.origin_mm[1] < -2


async def test_notes_passed_through(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fr, "run_kicad_cli", FakeCli(kicad_svg()))
    notes: list[dict[str, Any]] = []
    await fr.render_footprint(ctx, "Lib:R_1", None, None, True, notes=notes)
    assert notes == [{"kind": "footprint_attributes_unavailable", "lib_id": "Lib:R_1"}]


async def test_reported_grid_after_coarsening(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fr, "run_kicad_cli", FakeCli(kicad_svg(view_box="-100 -100 200 200")))
    out = await fr.render_footprint(ctx, "Lib:R_1", None, 0.25, True)
    assert out.grid_mm == 0.5
    out = await fr.render_footprint(ctx, "Lib:R_1", None, 1.0, True)
    assert out.grid_mm == 1.0


async def test_write_failure_leaves_nothing(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fr, "run_kicad_cli", FakeCli(kicad_svg()))
    real = Path.write_bytes

    def flaky(self: Path, data: bytes) -> int:
        if self.suffix == ".svg" and self.parent.name == "renders":
            raise OSError(28, "No space left on device")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    with pytest.raises(StructuredToolError) as exc:
        await fr.render_footprint(ctx, "Lib:R_1", None, None, True)
    assert _code(exc) == "render_failed"
    assert list((ctx.project.dir / ".kicad-agent" / "renders").iterdir()) == []


async def test_render_explicit_layers_and_name_starting_with_dash(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = FakeCli(kicad_svg())
    monkeypatch.setattr(fr, "run_kicad_cli", cli)
    await fr.render_footprint(ctx, "Lib:-o", ["F.CrtYd"], 0.25, False)
    ((args, _),) = cli.calls
    assert "--fp=-o" in args and args[args.index("--layers") + 1] == "F.CrtYd"


@pytest.mark.parametrize(
    "lib_id", ["Lib:a/b", "Lib:a\\b", "Lib:..", "Lib:a..b", "Lib:a\x00b", "Lib:", ":x", "Lib"]
)
async def test_bad_name_refused(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch, lib_id: str
) -> None:
    _no_cli(monkeypatch)
    with pytest.raises(StructuredToolError) as exc:
        await fr.render_footprint(ctx, lib_id, None, None, True)
    assert _code(exc) == "invalid_argument"
    assert svc.uri_calls == 0


@pytest.mark.parametrize("bad", [["F.Cu", "X.Cu"], [], ["--x"]])
async def test_bad_layers_or_grid_refused(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch, bad: list[str]
) -> None:
    _no_cli(monkeypatch)
    with pytest.raises(StructuredToolError) as exc:
        await fr.render_footprint(ctx, "Lib:R_1", bad, None, True)
    assert _code(exc) == "invalid_argument"
    for grid in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(StructuredToolError) as exc:
            await fr.render_footprint(ctx, "Lib:R_1", None, grid, True)
        assert _code(exc) == "invalid_argument"


async def test_bad_dir_or_name_refused(
    ctx: ToolContext, svc: FakeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_cli(monkeypatch)
    plain = tmp_path / "Lib"
    plain.mkdir()
    (tmp_path / "file.pretty").write_text("")
    for uri in (
        "Lib.pretty",
        "./x/Lib.pretty",
        str(plain),
        str(tmp_path / "missing.pretty"),
        str(tmp_path / "file.pretty"),
        None,
    ):
        svc.uri = uri
        with pytest.raises(StructuredToolError) as exc:
            await fr.render_footprint(ctx, "Lib:R_1", None, None, True)
        assert _code(exc) == "render_failed", uri
    for lib_id in ("Lib:a/b", "Lib:../x"):
        with pytest.raises(StructuredToolError) as exc:
            await fr.render_footprint(ctx, lib_id, None, None, True)
        assert _code(exc) == "invalid_argument"


async def test_not_found_before_cli(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_cli(monkeypatch)
    svc.geometry = None
    with pytest.raises(StructuredToolError) as exc:
        await fr.render_footprint(ctx, "Lib:R_1", None, None, True)
    assert _code(exc) == "not_found"
    assert svc.uri_calls == 0


def _tmp_dirs(cli: FakeCli) -> list[Path]:
    return [Path(args[args.index("-o") + 1]) for args, _ in cli.calls]


@pytest.mark.parametrize("write", [None, b""])
async def test_missing_or_empty_output(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch, write: bytes | None
) -> None:
    cli = FakeCli(write)
    monkeypatch.setattr(fr, "run_kicad_cli", cli)
    with pytest.raises(StructuredToolError) as exc:
        await fr.render_footprint(ctx, "Lib:R_1", None, None, True)
    assert _code(exc) == "render_failed"
    (tmp,) = _tmp_dirs(cli)
    assert not tmp.exists()
    assert not (ctx.project.dir / ".kicad-agent" / "renders").exists()


async def test_cli_failure_and_timeout(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = FakeCli(kicad_svg(), rc=3)
    monkeypatch.setattr(fr, "run_kicad_cli", cli)
    with pytest.raises(StructuredToolError) as exc:
        await fr.render_footprint(ctx, "Lib:R_1", None, None, True)
    assert _code(exc) == "render_failed"
    assert len(str(exc.value.to_dict())) < 1200  # raw KiCad output is capped

    async def slow(*_a: Any, **_k: Any) -> CliResult:
        raise TimeoutError

    monkeypatch.setattr(fr, "run_kicad_cli", slow)
    with pytest.raises(StructuredToolError) as exc:
        await fr.render_footprint(ctx, "Lib:R_1", None, None, True)
    assert _code(exc) == "render_failed"
    assert not [p for p in _tmp_dirs(cli) if p.exists()]


async def test_doctype_output_refused_and_cleaned(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = FakeCli(kicad_svg(prolog='<!DOCTYPE svg [<!ENTITY a "b">]>'))
    monkeypatch.setattr(fr, "run_kicad_cli", cli)
    with pytest.raises(StructuredToolError) as exc:
        await fr.render_footprint(ctx, "Lib:R_1", None, None, True)
    assert _code(exc) == "render_failed"
    assert not _tmp_dirs(cli)[0].exists()


async def test_one_render_at_a_time(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = FakeCli(kicad_svg(), delay=0.05)
    monkeypatch.setattr(fr, "run_kicad_cli", cli)
    real = fr.rasterise

    async def raster(svg: bytes, extent: tuple[float, float]) -> tuple[bytes, float]:
        out = await real(svg, extent)
        cli.events.append("raster")
        return out

    monkeypatch.setattr(fr, "rasterise", raster)
    a, b = await asyncio.gather(
        fr.render_footprint(ctx, "Lib:R_1", None, None, True),
        fr.render_footprint(ctx, "Lib:R_2", None, None, True),
    )
    # the second kicad-cli run starts only after the first render was rasterised
    assert cli.events == ["start", "end", "raster", "start", "end", "raster"]
    assert a.png != b.png


# --- review-loop round 1 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "attr",
    [
        'fill="\\75rl(http://x)"',  # CSS escape hiding url(
        'style="fill:image-set(&quot;http://x/a.png&quot; 1x)"',
        'style="fill:-WEBKIT-Image-Set(&quot;x.png&quot; 1x)"',
        'style="fill:SRC(&quot;http://x&quot;)"',
    ],
)
def test_css_escape_and_fetch_functions_dropped(attr: str) -> None:
    root = fr.load_kicad_svg(kicad_svg(f'<path id="p" {attr} d="M 0 0 L 1 1"/>'))
    (path,) = list(root.iter("path"))
    assert path.get("d") == "M 0 0 L 1 1"  # the element stays, the attribute goes
    assert path.get("style") is None and path.get("fill") is None


@pytest.mark.parametrize("rot", [90.0, 270.0, -90.0])
def test_pad1_label_in_board_axes(rot: float) -> None:
    g = geo([P("1", (0.0, 0.0), (0.6, 1.2), rot=rot)])
    root = ET.fromstring(fr.build_wrapper(fr.load_kicad_svg(kicad_svg()), g, None, True))
    assert "pad 1: 1.2 x 0.6 mm at (0, 0)" in _legend(root)
    flat = geo([P("1", (0.0, 0.0), (0.6, 1.2), rot=180.0)])
    root = ET.fromstring(fr.build_wrapper(fr.load_kicad_svg(kicad_svg()), flat, None, True))
    assert "pad 1: 0.6 x 1.2 mm at (0, 0)" in _legend(root)


async def test_off_ladder_grid_refused(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_cli(monkeypatch)
    for grid in (0.2, 0.3, 2.0, 5.0, 1e-9):
        with pytest.raises(StructuredToolError) as exc:
            await fr.render_footprint(ctx, "Lib:R_1", None, grid, True)
        assert _code(exc) == "invalid_argument", grid
    monkeypatch.setattr(fr, "run_kicad_cli", FakeCli(kicad_svg()))
    for grid in (0.1, 0.25, 0.5, 1, 1.0):
        out = await fr.render_footprint(ctx, "Lib:R_1", None, grid, True)
        assert out.grid_mm == grid


async def test_cancel_holds_lock_until_thread_ends(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render cancelled while its worker thread parses and wraps keeps the semaphore
    until that thread has finished: the next kicad-cli run starts only afterwards, and
    the temp dir is removed."""
    import threading

    cli = FakeCli(kicad_svg())
    monkeypatch.setattr(fr, "run_kicad_cli", cli)
    real = fr._prepare
    entered, release = threading.Event(), threading.Event()
    first = True

    def prepare(*args: Any) -> Any:
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(5)
        cli.events.append("prepare-end")
        return real(*args)

    monkeypatch.setattr(fr, "_prepare", prepare)
    task = asyncio.create_task(fr.render_footprint(ctx, "Lib:R_1", None, None, True))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    second = asyncio.create_task(fr.render_footprint(ctx, "Lib:R_2", None, None, True))
    await asyncio.sleep(0.1)
    assert cli.events == ["start", "end"]  # the second has not started kicad-cli
    assert not task.done()  # still waiting for its thread
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await second
    assert cli.events == ["start", "end", "prepare-end", "start", "end", "prepare-end"]
    assert not [p for p in _tmp_dirs(cli) if p.exists()]


# --- review-loop round 5 ----------------------------------------------------------------


def _many_points(n: int, step: float = 0.0001) -> str:
    return "M " + " ".join(f"{(k % 2) * step:.4f},{k * step:.4f}" for k in range(n))


def test_point_caps() -> None:
    """One element of more than 2,000 points (stock maximum 586) or more than 50,000
    points in all (stock maximum 23,924: Xilinx FLG1925) is refused before CairoSVG
    sees it: one self-crossing path of 100k points took 226 s under the render lock."""
    fr.load_kicad_svg(kicad_svg(f'<path d="{_many_points(fr.MAX_ELEMENT_COORDS)}"/>'))
    for body in (
        f'<path d="{_many_points(fr.MAX_ELEMENT_COORDS + 1)}"/>',
        f'<polyline points="{_many_points(fr.MAX_ELEMENT_COORDS + 1)[2:]}"/>',
        f'<path d="{_many_points(100_000)}"/>',
    ):
        with pytest.raises(StructuredToolError) as exc:
            fr.load_kicad_svg(kicad_svg(body))
        assert _code(exc) == "render_failed" and "2000 points" in exc.value.why
    per = fr.MAX_COORDS // 25
    fr.load_kicad_svg(kicad_svg(f'<path d="{_many_points(per)}"/>' * 25))
    with pytest.raises(StructuredToolError) as exc:
        fr.load_kicad_svg(kicad_svg(f'<path d="{_many_points(per)}"/>' * 26))
    assert _code(exc) == "render_failed" and "50000 points" in exc.value.why


def test_span_cap() -> None:
    """Lines (and shape outlines) summing to more than 1,000 times the drawing's
    longer side are refused: a stroke across the whole drawing costs CairoSVG ~0.1-0.3
    ms however few points it has (stock maximum: 364, CP_Radial_D40)."""
    across = '<path d="M -2,-2 2,2"/>'  # sqrt(32) / 4 = 1.41 drawings
    fr.load_kicad_svg(kicad_svg(across * 700))
    for body in (
        across * 710,
        '<circle cx="0" cy="0" r="2"/>' * 320,  # 3.14 each
        '<rect x="-2" y="-2" width="4" height="4"/>' * 251,
        '<line x1="-2" y1="0" x2="2" y2="0"/>' * 1001,
        '<path d="M 0,0 1e308,1e308"/>',
    ):
        with pytest.raises(StructuredToolError) as exc:
            fr.load_kicad_svg(kicad_svg(body))
        assert _code(exc) == "render_failed" and "1000 times" in exc.value.why


def test_hidden_root_is_render_failed() -> None:
    for attr in ('opacity="0"', 'style="opacity:0"'):
        with pytest.raises(StructuredToolError) as exc:
            fr.load_kicad_svg(kicad_svg().replace(b"<svg ", f"<svg {attr} ".encode(), 1))
        assert _code(exc) == "render_failed" and "hidden" in exc.value.why


@pytest.mark.parametrize(
    "attr",
    [
        'opacity="0%"',
        'opacity=" 0 "',
        'opacity="-1"',
        'opacity="nan"',
        'opacity="bogus"',  # unreadable: hidden
        'style="opacity:0%"',
        'style="opacity:0 !important"',
        'style="opacity: 0 ! IMPORTANT ;fill:#fff"',
        'style="opacity:/**/1"',  # a comment: unreadable
        'style="opacity:0;fill:url(http://x/a)"',  # the url filter must not unhide it
        'style="opacity:0;fill:u\\72 l(x)"',
        'style="op\\61 city:0"',  # a CSS escape can spell opacity
        'opacity="0" fill="url(#x)"',
    ],
)
def test_hidden_variants(attr: str) -> None:
    root = fr.load_kicad_svg(kicad_svg(f'<g><text {attr}>hidden</text><path d="M 0 0 L 1 1"/></g>'))
    assert not list(root.iter("text")), attr
    assert len(list(root.iter("path"))) == 1


@pytest.mark.parametrize(
    "attr", ['opacity="50%"', 'style="opacity:1 !important"', 'opacity="0.01"']
)
def test_visible_opacity_values(attr: str) -> None:
    root = fr.load_kicad_svg(kicad_svg(f"<text {attr}>shown</text>"))
    assert [t.text for t in root.iter("text")] == ["shown"]
    assert not [k for el in root.iter() for k in el.attrib if k == "opacity"]


# --- verification pass: the rasteriser process -------------------------------------------


def _fake_worker(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    monkeypatch.setattr(fr, "RASTER_WORKER", (sys.executable, "-c", code))


def _gone(pid: int) -> bool:
    """True when `pid` no longer exists: killed **and** reaped (a zombie still has one)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def _wait_for_file(path: Path) -> str:
    for _ in range(200):
        if path.is_file() and path.read_text():
            return path.read_text()
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} was not written")


SLEEPER = """
import json, os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open({info!r} + ".tmp", "w") as f:
    json.dump({{"pid": os.getpid(), "env": dict(os.environ), "argv": sys.argv[1:]}}, f)
os.rename({info!r} + ".tmp", {info!r})
time.sleep(60)
"""


async def test_slow_worker_times_out_and_is_reaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worker that hangs (and ignores SIGTERM) is killed after RASTER_TIMEOUT_S and
    reaped: `render_failed` "render timed out", no zombie, its HOME removed; it saw
    only the minimal environment (no secret of the app's) and the PNG size in argv."""
    info = tmp_path / "info.json"
    _fake_worker(monkeypatch, SLEEPER.format(info=str(info)))
    monkeypatch.setattr(fr, "RASTER_TIMEOUT_S", 1.0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    started = time.monotonic()
    with pytest.raises(StructuredToolError) as exc:
        await fr.rasterise(kicad_svg(), (4.0, 4.0))
    assert time.monotonic() - started < 5
    assert _code(exc) == "render_failed" and "render timed out" in exc.value.why
    seen = json.loads(info.read_text())
    assert _gone(seen["pid"])
    assert set(seen["env"]) <= {"PATH", "LANG", "HOME", "XDG_CACHE_HOME", "LC_CTYPE"}
    assert "secret" not in json.dumps(seen["env"])
    assert not Path(seen["env"]["HOME"]).exists()
    assert seen["argv"] == ["1200", "1200"]  # TARGET_PX for a 4 mm drawing


async def test_cancel_kills_and_reaps_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    info = tmp_path / "info.json"
    _fake_worker(monkeypatch, SLEEPER.format(info=str(info)))
    task = asyncio.create_task(fr.rasterise(kicad_svg(), (4.0, 4.0)))
    pid = json.loads(await _wait_for_file(info))["pid"]
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 5
    assert _gone(pid)


async def test_cancelled_render_holds_lock_until_worker_reaped(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A render cancelled while its rasteriser runs releases the semaphore only after
    the worker was killed and reaped: the next kicad-cli run never sees it alive."""
    info = tmp_path / "info.json"
    code = (
        f"import os, runpy\nif os.path.exists({str(info)!r}):\n"
        "    runpy.run_module('copperchat.views._raster_worker', run_name='__main__')\n"
        + SLEEPER.format(info=str(info))
    )
    _fake_worker(monkeypatch, code)
    cli = FakeCli(kicad_svg())
    alive_at_start: list[bool] = []

    async def spy(*args: Any, **kwargs: Any) -> CliResult:
        if info.is_file():
            alive_at_start.append(not _gone(json.loads(info.read_text())["pid"]))
        return await cli(*args, **kwargs)

    monkeypatch.setattr(fr, "run_kicad_cli", spy)
    task = asyncio.create_task(fr.render_footprint(ctx, "Lib:R_1", None, None, True))
    pid = json.loads(await _wait_for_file(info))["pid"]
    task.cancel()
    second = asyncio.create_task(fr.render_footprint(ctx, "Lib:R_2", None, None, True))
    with pytest.raises(asyncio.CancelledError):
        await task
    out = await second
    assert out.png.is_file() and _gone(pid)
    assert alive_at_start == [False]
    assert not [p for p in _tmp_dirs(cli) if p.exists()]


async def test_worker_output_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """More stdout than a 2000 x 2000 PNG can hold, output that is no PNG, or a
    non-zero exit (a long stderr is drained, never quoted): `render_failed`."""
    monkeypatch.setattr(fr, "MAX_PNG_BYTES", 1000)
    for code, why in (
        (
            "import sys; sys.stdout.buffer.write(b'\\x89PNG\\r\\n\\x1a\\n' * 1000)",
            "more than a PNG",
        ),
        ("print('hello')", "no PNG"),
        ("import sys; sys.exit(3)", "exit code 3"),
        ("import sys; sys.stderr.write('Traceback ' * 10000); sys.exit(1)", "exit code 1"),
    ):
        _fake_worker(monkeypatch, code)
        with pytest.raises(StructuredToolError) as exc:
            await fr.rasterise(kicad_svg(), (4.0, 4.0))
        assert _code(exc) == "render_failed" and why in exc.value.why, exc.value.why


def _thick_strokes(paths: int = 24, points: int = 1990) -> bytes:
    """The round-4/5 attack: fp_poly outlines of 1,990 points jittered in a 0.1 mm box,
    stroked 40 mm wide; within every cap (points, elements, span)."""
    rng = random.Random(5)
    body = "".join(
        '<path style="fill:none; stroke:#F2EDA1; stroke-width:40; stroke-linecap:round; '
        'stroke-linejoin:round" d="M '
        + " ".join(
            f"{20 + rng.uniform(0, 0.1):.4f},{20 + rng.uniform(0, 0.1):.4f}" for _ in range(points)
        )
        + ' Z"/>'
        for _ in range(paths)
    )
    return kicad_svg(body, view_box="0 0 40.0812 40.0812")


async def test_thick_strokes_time_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """24 thick strokes passed every cap and took ~41 s in CairoSVG under the render
    lock; now the real worker is stopped at the timeout (shortened here)."""
    monkeypatch.setattr(fr, "RASTER_TIMEOUT_S", 2.0)
    wrapped = fr._build(fr.load_kicad_svg(_thick_strokes()), l201(), None, False)
    started = time.monotonic()
    with pytest.raises(StructuredToolError) as exc:
        await fr.rasterise(wrapped.svg, wrapped.extent_mm)
    assert time.monotonic() - started < 6
    assert _code(exc) == "render_failed" and "render timed out" in exc.value.why
