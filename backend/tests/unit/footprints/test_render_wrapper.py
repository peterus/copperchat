"""Footprint render pipeline without KiCad (F2 Task 5, spec §7.5): KiCad SVG hardening,
the wrapper (grid, axes, ticks, labels), rasterising and `render_footprint`'s checks,
temp-dir cleanup and one-render-at-a-time."""

from __future__ import annotations

import asyncio
import io
import itertools
import socket
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


def test_unparsable_refused() -> None:
    for bad in (b"<svg", b"not xml", f'<x xmlns="{SVG_NS}"/>'.encode()):
        with pytest.raises(StructuredToolError) as exc:
            fr.load_kicad_svg(bad)
        assert _code(exc) == "render_failed"


def test_oversize_output_refused() -> None:
    data = kicad_svg()
    data = data + b" " * (fr.MAX_SVG_BYTES + 1 - len(data))
    assert len(data) == 10 * 1024 * 1024 + 1
    with pytest.raises(StructuredToolError) as exc:
        fr.load_kicad_svg(data)
    assert _code(exc) == "render_failed"
    fr.load_kicad_svg(data[:-1])  # exactly 10 MB is fine


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
        '<use href="#a"/><use xlink:href="#a"/>'
        '<set attributeName="href" to="http://x"/>'
    )
    root = fr.load_kicad_svg(kicad_svg(body))
    out = fr.build_wrapper(root, l201(), 0.5, True)
    wrapped = ET.fromstring(out)
    tags = {el.tag.split("}")[-1] for el in wrapped.iter()}
    assert not tags & {"script", "style", "foreignObject", "div", "set"}
    attrs = _all_attrs(wrapped)
    assert not [k for k, _ in attrs if k.split("}")[-1].lower().startswith("on")]
    hrefs = [v for k, v in attrs if k.split("}")[-1] == "href"]
    assert hrefs == ["#a", "#a"]
    assert not [v for _, v in attrs if "http://x" in v or "http://y" in v or "data:" in v]
    styles = [v for k, v in attrs if k == "style" and "url(" in v]
    assert styles and all("#b" in v for v in styles)
    assert b"<!DOCTYPE" not in out and b"<?xml" not in out


# --- rasterise ------------------------------------------------------------------------


def test_url_fetcher_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = fr._refuse

    def spy(url: str, resource_type: str) -> bytes:
        calls.append(url)
        return real(url, resource_type)

    def no_socket(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(fr, "_refuse", spy)
    monkeypatch.setattr(socket, "socket", no_socket)
    svg = kicad_svg('<image href="http://127.0.0.1:1/x.png" width="2" height="2"/>')
    with pytest.raises(StructuredToolError) as exc:
        fr.rasterise(svg, (4.0, 4.0))
    assert _code(exc) == "render_failed"
    assert calls == ["http://127.0.0.1:1/x.png"]
    with pytest.raises(ValueError):
        real("http://127.0.0.1:1/x.png", "image/*")


def _png_size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as img:
        assert img.format == "PNG"
        return img.size


def test_raster_size() -> None:
    png, scale = fr.rasterise(kicad_svg('<rect x="-1" y="-1" width="2" height="2"/>'), (4.0, 4.0))
    w, h = _png_size(png)
    assert scale >= 40 and w >= 160 and h >= 160 and max(w, h) <= 2000
    assert w == round(4 * scale)
    wide = kicad_svg(view_box="0 0 100 10").replace(b'width="4mm" height="4mm"', b"")
    png, scale = fr.rasterise(wide, (100.0, 10.0))
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
    labels = "\n".join(t.text or "" for t in _group(root, "labels").iter(f"{S}text"))
    assert "<&>" in labels
    assert "pad 1: 0.6 x 0.8 mm at (-1, 0)" in labels
    assert "pitch 1 → <&>: 2 mm" in labels
    assert "courtyard: 3 x 1.5 mm" in labels
    assert b"&lt;&amp;&gt;" in out


def test_labels_off() -> None:
    out = fr.build_wrapper(fr.load_kicad_svg(kicad_svg()), l201(), None, False)
    assert ET.fromstring(out).find(f".//{S}g[@id='labels']") is None


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

    def raster(svg: bytes, extent: tuple[float, float]) -> tuple[bytes, float]:
        cli.events.append("raster")
        return real(svg, extent)

    monkeypatch.setattr(fr, "rasterise", raster)
    a, b = await asyncio.gather(
        fr.render_footprint(ctx, "Lib:R_1", None, None, True),
        fr.render_footprint(ctx, "Lib:R_2", None, None, True),
    )
    # the second kicad-cli run starts only after the first render was rasterised
    assert cli.events == ["start", "end", "raster", "start", "end", "raster"]
    assert a.png != b.png
