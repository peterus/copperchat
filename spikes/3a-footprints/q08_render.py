"""Q8: kicad-cli fp export svg of the new footprint — usable for a dimension check?
Measures the SVG's physical size (mm), the scale (viewBox units per mm) and the drawn
extent per exported layer set, compared with the API geometry."""

import re
import shutil
import tempfile
import xml.etree.ElementTree as ET  # noqa: S405 - parses our own kicad-cli output
from pathlib import Path

import l201
from harness import Recorder, Server, cli, main_guard
from repo import make_repo
from steps import create_l201, open_sch

NUM = re.compile(r"-?\d+(?:\.\d+)?")


def svg_info(path: Path, with_text: bool = False) -> dict:
    root = ET.parse(path).getroot()  # noqa: S314
    w, h = root.get("width", ""), root.get("height", "")
    vb = [float(x) for x in root.get("viewBox", "0 0 0 0").split()]
    xs, ys = [], []
    texts = 0
    skip: set[int] = set()  # elements inside <g class="stroked-text"> (fields/texts drawn as strokes)
    for g in root.iter():
        if g.tag.split("}")[-1] == "g" and g.get("class") == "stroked-text":
            texts += 1
            if not with_text:
                skip.update(id(e) for e in g.iter())
    for el in root.iter():
        if id(el) in skip:
            continue
        tag = el.tag.split("}")[-1]
        if tag == "path" and el.get("d"):
            nums = [float(n) for n in NUM.findall(el.get("d"))]
            xs += nums[0::2]
            ys += nums[1::2]
        elif tag == "circle":
            cx, cy, rr = (float(el.get(a, 0)) for a in ("cx", "cy", "r"))
            xs += [cx - rr, cx + rr]
            ys += [cy - rr, cy + rr]
        elif tag == "rect" and el.get("width") and "%" not in el.get("width"):
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            xs += [x, x + float(el.get("width"))]
            ys += [y, y + float(el.get("height"))]
    unit_per_mm = vb[2] / float(w.rstrip("cm")) if w.endswith("cm") is False and w.endswith("mm") else None
    if w.endswith("cm"):
        unit_per_mm = vb[2] / (float(w[:-2]) * 10)
    ext = None
    if xs and unit_per_mm:
        ext = (round((max(xs) - min(xs)) / unit_per_mm, 3), round((max(ys) - min(ys)) / unit_per_mm, 3))
    colors = sorted(set(re.findall(r"stroke:(#[0-9A-Fa-f]{6})", path.read_text())))
    return {"width": w, "height": h, "viewBox": vb, "unit_per_mm": unit_per_mm, "extent_mm": ext,
            "text_groups": texts, "stroke_colors": colors,
            "bytes": path.stat().st_size}


def export(lib: Path, out: Path, layers: str | None) -> Path:
    out.mkdir(parents=True)  # kicad-cli does not create it (and still exits 0: check the file)
    args = ["fp", "export", "svg", "--fp", l201.NAME, "-o", str(out)]
    if layers:
        args += ["--layers", layers]
    p = cli(*args, str(lib))
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
    svgs = sorted(out.glob("*.svg"))
    assert len(svgs) == 1, svgs
    return svgs[0]


def main() -> int:
    r = Recorder("q08_render")
    repo = make_repo(Path(tempfile.mkdtemp(prefix="c3a-q8-")), board=False)
    repo.lib_dir.mkdir()
    with Server(repo.main_pro) as srv:
        k = srv.client(60000)
        open_sch(k, repo.main_pro)
        create_l201(k)
    res = Path(__file__).parent / "results"
    base = Path(tempfile.mkdtemp(prefix="c3a-q8-svg-"))
    cases = {
        "all": None,
        "crtyd": "F.CrtYd",
        "fab": "F.Fab",
        "cu": "F.Cu",
    }
    info = {}
    for name, layers in cases.items():
        svg = export(repo.lib_dir, base / name, layers)
        info[name] = svg_info(svg)
        shutil.copyfile(svg, res / f"q08_{name}.svg")
    r.note("svg", info)
    # path coordinates are stroke centerlines in mm (viewBox == size in mm, origin = footprint anchor)
    r.check("svg_scale_known", all(i["unit_per_mm"] for i in info.values()), {n: i["unit_per_mm"] for n, i in info.items()})
    cy = info["crtyd"]["extent_mm"]
    r.check("crtyd_extent", cy == (4.0, 4.0), cy)
    fab = info["fab"]["extent_mm"]
    # fab = 3.0x3.0 rect; the Value field and ${REFERENCE} text on F.Fab are stroked-text groups
    r.check("fab_extent_without_text", fab == (3.0, 3.0), fab)
    r.check("fab_texts_present", info["fab"]["text_groups"] >= 2, info["fab"]["text_groups"])
    cu = info["cu"]["extent_mm"]
    # copper: two 1.3x3.5 pads at x=+-1.1 -> 3.5 x 3.5
    r.check("cu_extent", cu == (3.5, 3.5), cu)
    miss = base / "missing" / "dir"
    p = cli("fp", "export", "svg", "--fp", l201.NAME, "-o", str(miss), str(repo.lib_dir))
    r.check("missing_out_dir_exit0_no_file", p.returncode == 0 and not list(base.glob("missing/**/*.svg")),
            (p.returncode, (p.stdout + p.stderr)[-200:]))
    # the page is the bounding box of everything incl. texts (+ a small margin)
    full = svg_info(res / "q08_all.svg", with_text=True)["extent_mm"]
    vb = info["all"]["viewBox"]
    r.note("all_extent_with_text_mm", full)
    r.check("page_is_content_bbox", 0 <= vb[2] - full[0] < 1.0 and 0 <= vb[3] - full[1] < 1.0, (vb, full))
    return r.finish()


if __name__ == "__main__":
    main_guard(main)
