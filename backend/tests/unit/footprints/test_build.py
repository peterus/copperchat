"""`fp_create` target geometry (F4a Task 2, spec §7.1/§7.6): the build rules, checked
with the real `fp_check` rules (F2)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.footprints import build, geom
from copperchat.kicad_tools.footprints.build import CreateArgs, build_target, parse_create_args
from copperchat.kicad_tools.footprints.checks import CheckInput, run_checks
from copperchat.kicad_tools.footprints.model import (
    REVIEW_FIELD,
    SOURCE_FIELD,
    FootprintGeometry,
    Shape,
)
from copperchat.library.local import LocalLibrary

ROOT = Path("/repo/OpenDriveHub")
LIB = LocalLibrary(
    nickname="OpenDriveHub",
    dir=ROOT / "footprints/OpenDriveHub.pretty",
    uri="${KIPRJMOD}/../../footprints/OpenDriveHub.pretty",
    repo_root=ROOT,
    table=ROOT / "hw/main/fp-lib-table",
)
URL = "https://www.bourns.com/docs/product-datasheets/srn3012bta.pdf"


def l201_args(**kw: Any) -> dict[str, Any]:
    """The owner's L201 numbers: 1.3 x 3.5 mm pads at x = +-1.1, a 3 x 3 mm body."""
    args: dict[str, Any] = {
        "name": "L_Bourns_SRN3012BTA",
        "description": "Bourns SRN3012BTA shielded power inductor, 3.0x3.0 mm",
        "mounting": "smd",
        "pads": [
            {"number": "1", "type": "smd", "shape": "rect", "at": [-1.1, 0], "size": [1.3, 3.5]},
            {"number": "2", "type": "smd", "shape": "rect", "at": [1.1, 0], "size": [1.3, 3.5]},
        ],
        "body": {"rect": [3.0, 3.0]},
        "source": {"document": URL, "location": "p.2, 'Recommended layout'"},
    }
    args.update(kw)
    return args


def target(**kw: Any) -> FootprintGeometry:
    return build_target(parse_create_args(l201_args(**kw)), LIB)


def on(g: FootprintGeometry, layer: str) -> list[Shape]:
    return [s for s in g.shapes if s.layer == layer]


def copper_distance(g: FootprintGeometry, seg: geom.Seg) -> float:
    return min(geom.distance(seg, part) for p in g.pads for part in geom.pad_distance_parts(p))


def test_l201_target() -> None:
    g = target()
    assert g.lib_id == "OpenDriveHub:L_Bourns_SRN3012BTA" and g.scope == "local"
    assert g.mounting == "smd"
    assert [p.at for p in g.pads] == [(-1.1, 0.0), (1.1, 0.0)]
    assert all(p.layers == ("F.Cu", "F.Mask", "F.Paste") for p in g.pads)
    summary = g.layer_summary()
    assert summary["F.CrtYd"]["bbox"] == [-2.0, -2.0, 2.0, 2.0]
    assert [s.width for s in on(g, "F.CrtYd")] == [0.05]
    fab = on(g, "F.Fab")
    assert len(fab) == 1 and fab[0].width == 0.1 and fab[0].closed
    assert summary["F.Fab"]["bbox"] == [-1.5, -1.5, 1.5, 1.5]
    # pin 1 chamfer: min(1.0, 0.25 * 3) = 0.75 at the corner nearest pad 1 (top left)
    assert (-1.5, -0.75) in fab[0].points and (-0.75, -1.5) in fab[0].points
    assert (-1.5, -1.5) not in fab[0].points

    silk = on(g, "F.SilkS")
    assert silk and all(s.kind == "segment" and s.width == 0.12 for s in silk)
    marker = [s for s in silk if s.points[0][0] < -1.75]  # left of pad 1's outer side
    outline = [s for s in silk if s not in marker]
    assert len(marker) == 1
    (mx0, my0), (mx1, my1) = marker[0].points
    assert mx0 == mx1 and math.isclose(mx0, -1.75 - 0.12 - 0.06)  # edge 0.12 from copper
    assert math.isclose(abs(my1 - my0), 3.5 / 2)  # half the pad's extent on that side
    assert mx0 - 0.06 > -2.0  # inside the courtyard
    pad1 = next(p for p in g.pads if p.number == "1")
    edge = geom.distance(geom.Seg(*marker[0].points, 0.06), geom.pad_distance_parts(pad1)[0])
    assert edge == pytest.approx(0.12, abs=1e-6)
    assert outline
    for s in outline:
        assert copper_distance(g, geom.Seg(*s.points, 0.06)) >= 0.2 - 1e-9
        # on the body outline
        assert all(abs(abs(c) - 1.5) < 1e-9 for c in (s.points[0][1], s.points[1][1]))

    texts = {t.kind: t for t in g.texts}
    assert texts["reference"].layer == "F.SilkS" and texts["reference"].at == (0.0, -3.0)
    assert texts["reference"].value == "REF**" and texts["reference"].size == (1.0, 1.0)
    assert texts["value"].layer == "F.Fab" and texts["value"].at == (0.0, 3.0)
    assert texts["value"].value == "L_Bourns_SRN3012BTA"
    assert texts["user"].layer == "F.Fab" and texts["user"].value == "${REFERENCE}"
    assert texts["user"].at == (0.0, 0.0) and texts["user"].size == (1.0, 1.0)

    assert g.fields["Reference"] == "REF**"
    assert g.fields["Value"] == "L_Bourns_SRN3012BTA"
    assert g.fields["Datasheet"] == URL
    assert g.fields[SOURCE_FIELD] == f"{URL}, p.2, 'Recommended layout'"
    assert g.fields[REVIEW_FIELD] == "yes"
    assert g.models == ()

    report = run_checks(CheckInput(geometry=g, local=True))
    assert report.passed, report.findings
    assert report.counts["error"] == 0 and report.counts["warning"] == 0, report.findings


def test_l201_target_polarized_has_pin1_mark() -> None:
    g = target()
    report = run_checks(CheckInput(geometry=g, local=True, polarized=True))
    assert report.counts["warning"] == 0, report.findings


def test_pin1_none_and_custom_silk() -> None:
    g = target(pin1_marker="none", silkscreen={"segments": [[[-1.5, -1.9], [1.5, -1.9]]]})
    fab = on(g, "F.Fab")
    assert [s.kind for s in fab] == ["rect"]
    assert [s.points for s in on(g, "F.SilkS")] == [((-1.5, -1.9), (1.5, -1.9))]


def test_courtyard_rect_and_margin() -> None:
    g = target(courtyard={"rect": [5.0, 4.5], "center": [0.1, 0]})
    assert g.layer_summary()["F.CrtYd"]["bbox"] == [-2.4, -2.25, 2.6, 2.25]
    # margin rounded outward to 0.01 mm
    g = target(courtyard={"margin": 0.333})
    assert g.layer_summary()["F.CrtYd"]["bbox"] == [-2.09, -2.09, 2.09, 2.09]


def test_layers_by_type() -> None:
    pads = [
        {"number": "1", "type": "tht", "shape": "circle", "at": [0, 0], "size": [1.6, 1.6],
         "drill": {"size": [0.8, 0.8], "shape": "circle"}},
        {"number": "", "type": "npth", "shape": "circle", "at": [3, 0], "size": [1.0, 1.0],
         "drill": {"size": [1.0, 1.0], "shape": "circle"}},
        {"number": "3", "type": "connector", "shape": "rect", "at": [6, 0], "size": [1, 2]},
        {"number": "4", "type": "smd", "shape": "roundrect", "at": [9, 0], "size": [1, 2],
         "layers": ["B.Cu", "B.Mask", "B.Paste"]},
    ]  # fmt: skip
    g = target(pads=pads, mounting="tht", body={"rect": [12, 3], "center": [4.5, 0]})
    assert [p.layers for p in g.pads] == [
        ("*.Cu", "*.Mask"),
        ("*.Cu", "*.Mask"),
        ("F.Cu", "F.Mask"),
        ("B.Cu", "B.Mask", "B.Paste"),
    ]
    assert g.pads[3].roundrect_ratio == 0.25 and g.pads[0].roundrect_ratio is None
    assert g.pads[0].drill is not None and g.pads[0].drill.size == (0.8, 0.8)


def test_user_text_size_clamped_and_fields() -> None:
    g = target(body={"rect": [0.6, 0.6]}, pads=[
        {"number": "1", "type": "smd", "shape": "rect", "at": [-0.5, 0], "size": [0.3, 0.3]},
        {"number": "2", "type": "smd", "shape": "rect", "at": [0.5, 0], "size": [0.3, 0.3]},
    ])  # fmt: skip
    user = next(t for t in g.texts if t.kind == "user")
    assert user.size == (0.25, 0.25)
    g = target(
        fields={"Datasheet": "https://example.com/x.pdf", "Manufacturer  ": "Bourns"},
        source={"document": "docs/ds.pdf", "location": "p.2", "mpn": "SRN3012BTA-2R2M"},
        model={"path": "${KIPRJMOD}/3d/x.step", "offset": [0, 0, 0.1]},
    )
    assert g.fields["Datasheet"] == "https://example.com/x.pdf"
    assert g.fields["Manufacturer"] == "Bourns"
    assert g.fields[SOURCE_FIELD] == "docs/ds.pdf, p.2, MPN SRN3012BTA-2R2M"
    assert list(g.fields)[-2:] == [SOURCE_FIELD, REVIEW_FIELD]  # tool fields last
    assert [(m.path, m.offset, m.rotation, m.scale) for m in g.models] == [
        ("${KIPRJMOD}/3d/x.step", (0.0, 0.0, 0.1), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    ]
    # a repo-path source without a Datasheet field leaves the Datasheet empty
    g = target(source={"document": "docs/ds.pdf", "location": "p.2"})
    assert g.fields["Datasheet"] == ""


def test_silk_clipped_around_pads_crossing_the_body() -> None:
    # a body edge crossing a row of pads is cut into pieces 0.2 mm (edge) clear of each
    pads = [
        {"number": str(i + 1), "type": "smd", "shape": "oval", "at": [i * 1.27, -2.0],
         "size": [0.6, 1.6]}
        for i in range(4)
    ]  # fmt: skip
    body = {"rect": [5.0, 4.0], "center": [1.905, 0]}
    g = target(pads=pads, body=body, pin1_marker="none")  # the outline only
    outline = on(g, "F.SilkS")
    assert len(outline) > 4
    for s in outline:
        assert copper_distance(g, geom.Seg(*s.points, 0.06)) >= 0.2 - 1e-9


def test_parse_returns_typed_args() -> None:
    args = parse_create_args(l201_args(keywords="inductor", replace=True, symbol="Device:L"))
    assert isinstance(args, CreateArgs)
    assert args.replace and args.symbol == "Device:L" and args.keywords == "inductor"
    assert args.margin == build.DEFAULT_MARGIN and args.courtyard is None


def test_parse_refuses_unknown_and_missing() -> None:
    for bad in (l201_args(extra=1), {k: v for k, v in l201_args().items() if k != "body"}):
        with pytest.raises(StructuredToolError) as err:
            parse_create_args(bad)
        assert err.value.code == "invalid_argument"
