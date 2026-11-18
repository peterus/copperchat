"""`fp_get` without KiCad: output caps (author call 16) and the input schema (F1 Task 5)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, cast

import pytest

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.footprints import read
from copperchat.kicad_tools.footprints.model import FootprintGeometry, Pad, Shape
from copperchat.kicad_tools.footprints.specs import TOOL_SPECS
from copperchat.projects import Project
from copperchat.tools import ToolContext


def _pad(i: int) -> Pad:
    return Pad(
        number=str(i + 1),
        type="smd",
        shape="rect",
        at=(i * 0.5, 0.0),
        rotation=0.0,
        size=(0.3, 0.3),
        roundrect_ratio=None,
        drill=None,
        layers=("F.Cu", "F.Mask", "F.Paste"),
    )


def _polygon(i: int) -> Shape:
    points = tuple((i * 0.01 + k * 0.001, float(k % 7)) for k in range(100))
    return Shape(
        kind="polygon",
        layer="F.Fab",
        points=points,
        center=None,
        size=None,
        width=0.1,
        closed=True,
    )


def _large() -> FootprintGeometry:
    return FootprintGeometry(
        lib_id="Big:BGA_2000",
        scope="global",
        description="synthetic",
        keywords="",
        mounting="smd",
        exclude_from_bom=False,
        exclude_from_pos=False,
        dnp=False,
        fields={},
        pads=tuple(_pad(i) for i in range(2000)),
        shapes=tuple(_polygon(i) for i in range(10_000)),
        texts=(),
        models=(),
        overrides=(),
    )


def test_full_detail_truncates_large_footprint() -> None:
    g = _large()
    full = g.to_dict("full")
    assert len(full["shapes"]) == 500
    assert all(len(s["points"]) <= 64 for s in full["shapes"])
    assert full["truncated"] is True
    assert len(full["pads"]) == 1000
    assert full["pads_truncated"] is True
    assert full["pad_count"] == 2000
    assert "fp_render" in full["note"]
    assert full["graphics"]["F.Fab"]["items"] == 10_000  # the summary stays exact

    summary = g.to_dict("summary")
    assert len(summary["pads"]) == 256
    assert summary["pads_truncated"] is True
    assert summary["pad_count"] == 2000
    assert "shapes" not in summary and "truncated" not in summary


def test_small_footprint_is_not_truncated() -> None:
    g = _large()
    small = dataclasses.replace(g, pads=g.pads[:2], shapes=g.shapes[:1])
    d = small.to_dict("full")
    assert d["pad_count"] == 2 and len(d["pads"]) == 2
    assert "pads_truncated" not in d
    assert len(d["shapes"][0]["points"]) == 64  # one long polygon is still capped
    assert d["shapes"][0]["points_truncated"] is True
    assert d["truncated"] is True and "fp_render" in d["note"]
    plain = dataclasses.replace(g, pads=g.pads[:2], shapes=(_square(),)).to_dict("full")
    assert "truncated" not in plain and "note" not in plain
    assert "points_truncated" not in plain["shapes"][0]


def _square() -> Shape:
    return Shape(
        kind="polygon",
        layer="F.Fab",
        points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        center=None,
        size=None,
        width=0.1,
        closed=True,
    )


def _spec() -> Any:
    return next(s for s in TOOL_SPECS if s.name == "fp_get")


def test_fp_get_schema() -> None:
    schema = _spec().input_schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["lib_id"]
    assert schema["properties"]["lib_id"]["maxLength"] == 256
    assert schema["properties"]["detail"]["enum"] == ["summary", "full"]
    assert not _spec().mutating


@pytest.fixture
def ctx(tmp_path: Path, make_settings: Any) -> ToolContext:
    d = tmp_path / "demo"
    d.mkdir()
    project = Project(
        "a1b2c3d4e5f6", "demo", "demo/demo.kicad_pro", d, d / "demo.kicad_pro", None, None
    )
    settings: Settings = make_settings()
    # never reached: argument errors come before any KiCad access
    return ToolContext(project=project, session=cast(KicadSession, None), settings=settings)


@pytest.mark.parametrize(
    "args",
    [
        {"lib_id": "NoColon"},
        {"lib_id": ":Name"},
        {"lib_id": "Lib:"},
        {"lib_id": 5},
        {"lib_id": "L:" + "x" * 300},
        {"lib_id": "Lib:Name", "detail": "everything"},
    ],
)
async def test_fp_get_invalid_argument(ctx: ToolContext, args: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError) as err:
        await read.fp_get(ctx, args)
    assert err.value.code == "invalid_argument"


async def test_fp_get_refused_when_disabled(ctx: ToolContext, make_settings: Any) -> None:
    ctx.settings = make_settings(footprint_tools=False)
    with pytest.raises(StructuredToolError) as err:
        await read.fp_get(ctx, {"lib_id": "Inductor_SMD:L_Bourns-SRN4018"})
    assert err.value.code == "footprint_tools_disabled"
