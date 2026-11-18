"""`fp_render` without KiCad (F2 Task 6, spec §7.5): input schema and bounds, the result
(filenames only, one PNG ImageBlock), the SVG download-name sidecar (index call 3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from copperchat.agent.profiles import FP_READ
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.footprints import render as tool
from copperchat.kicad_tools.footprints.specs import TOOL_SPECS
from copperchat.projects import Project
from copperchat.tools import ToolContext
from copperchat.views.footprint_render import RenderOutput

LIB_ID = "OpenDriveHub:L_Bourns_SRN3012BTA"
STEM = "0123456789abcdef0123456789abcdef"


def _spec() -> Any:
    return next(s for s in TOOL_SPECS if s.name == "fp_render")


def test_fp_render_schema() -> None:
    spec = _spec()
    assert not spec.mutating and "fp_render" in FP_READ
    schema = spec.input_schema
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["lib_id"]
    props = schema["properties"]
    assert set(props) == {"lib_id", "layers", "grid_mm", "labels"}
    assert props["lib_id"]["maxLength"] == 256
    layers = props["layers"]
    assert layers["maxItems"] == 32 and layers["minItems"] == 1
    assert layers["items"]["pattern"] == (
        r"^(?:(?:F|B)\.(?:Cu|Mask|Paste|SilkS|Fab|CrtYd)|Edge\.Cuts)$"
    )
    assert props["grid_mm"]["enum"] == [0.1, 0.25, 0.5, 1.0]
    assert props["labels"] == {
        "type": "boolean",
        "default": True,
        "description": props["labels"]["description"],
    }


@pytest.fixture
def ctx(tmp_path: Path, make_settings: Any) -> ToolContext:
    d = tmp_path / "demo"
    d.mkdir()
    project = Project(
        "a1b2c3d4e5f6", "demo", "demo/demo.kicad_pro", d, d / "demo.kicad_pro", None, None
    )
    return ToolContext(project=project, session=cast(KicadSession, None), settings=make_settings())


class FakeRender:
    """`render_footprint` stand-in: writes `<STEM>.png` / `.svg` like the real one."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(
        self,
        ctx: ToolContext,
        lib_id: str,
        layers: list[str] | None,
        grid_mm: float | None,
        labels: bool,
        *,
        notes: list[dict[str, Any]] | None = None,
    ) -> RenderOutput:
        self.calls.append((lib_id, layers, grid_mm, labels))
        if notes is not None and lib_id.startswith("Noted:"):
            notes.append({"kind": "footprint_libraries_loaded", "seconds": 1.5})
        out = ctx.project.dir / ".kicad-agent" / "renders"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{STEM}.png").write_bytes(b"\x89PNG-data")
        (out / f"{STEM}.svg").write_bytes(b"<svg/>")
        return RenderOutput(
            out / f"{STEM}.png",
            out / f"{STEM}.svg",
            48.0,
            (6.123456789, 5.5),
            "h" * 64,
            (-3.25, -2.75),
            0.25,
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRender:
    f = FakeRender()
    monkeypatch.setattr(tool, "render_footprint", f)
    return f


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"lib_id": "NoColon"},
        {"lib_id": 5},
        {"lib_id": "L:" + "x" * 255},
        {"lib_id": LIB_ID, "extra": 1},
        {"lib_id": LIB_ID, "layers": []},
        {"lib_id": LIB_ID, "layers": "F.Cu"},
        {"lib_id": LIB_ID, "layers": ["F.Cu"] * 33},
        {"lib_id": LIB_ID, "layers": ["In1.Cu"]},
        {"lib_id": LIB_ID, "layers": ["F.Cu\n"]},
        {"lib_id": LIB_ID, "layers": ["f.cu"]},
        {"lib_id": LIB_ID, "layers": ["Edge.Cuts", "B.Courtyard"]},
        {"lib_id": LIB_ID, "layers": [1]},
        {"lib_id": LIB_ID, "grid_mm": 0.2},
        {"lib_id": LIB_ID, "grid_mm": 0},
        {"lib_id": LIB_ID, "grid_mm": True},
        {"lib_id": LIB_ID, "grid_mm": "0.5"},
        {"lib_id": LIB_ID, "grid_mm": float("nan")},
        {"lib_id": LIB_ID, "grid_mm": 10**400},
        {"lib_id": LIB_ID, "labels": "yes"},
        {"lib_id": LIB_ID, "labels": 1},
    ],
)
async def test_fp_render_invalid_argument(
    ctx: ToolContext, fake: FakeRender, args: dict[str, Any]
) -> None:
    with pytest.raises(StructuredToolError) as err:
        await tool.fp_render(ctx, args)
    assert err.value.code == "invalid_argument", err.value.what
    assert fake.calls == []


async def test_fp_render_refused_when_disabled(
    ctx: ToolContext, fake: FakeRender, make_settings: Any
) -> None:
    ctx.settings = make_settings(footprint_tools=False)
    with pytest.raises(StructuredToolError) as err:
        await tool.fp_render(ctx, {"lib_id": LIB_ID})
    assert err.value.code == "footprint_tools_disabled"
    assert fake.calls == []


async def test_fp_render_result(ctx: ToolContext, fake: FakeRender) -> None:
    result = await tool.fp_render(ctx, {"lib_id": LIB_ID})
    assert fake.calls == [(LIB_ID, None, None, True)]
    assert result.data == {
        "lib_id": LIB_ID,
        "image": f"{STEM}.png",
        "svg": f"{STEM}.svg",
        "svg_url": f"/api/projects/a1b2c3d4e5f6/renders/{STEM}.svg",
        "scale_px_per_mm": 48.0,
        "extent_mm": [6.1235, 5.5],
        "origin_mm": [-3.25, -2.75],
        "grid_mm": 0.25,
        "geometry_hash": "h" * 64,
    }
    assert not result.changed_docs  # read-only: no budget, no snapshot
    (img,) = result.images
    renders = ctx.project.dir / ".kicad-agent" / "renders"
    assert img.mime == "image/png" and img.data == b"\x89PNG-data"
    assert img.path == renders / f"{STEM}.png"
    sidecar = renders / f"{STEM}.svg.json"
    assert json.loads(sidecar.read_text()) == {
        "download_name": "OpenDriveHub_L_Bourns_SRN3012BTA.svg"
    }


async def test_fp_render_passes_arguments_and_notes(ctx: ToolContext, fake: FakeRender) -> None:
    args = {
        "lib_id": " Noted:R 0805 ",
        "layers": ["B.Cu", "Edge.Cuts"],
        "grid_mm": 1,
        "labels": False,
    }
    result = await tool.fp_render(ctx, args)
    assert fake.calls == [("Noted:R 0805", ["B.Cu", "Edge.Cuts"], 1.0, False)]
    assert result.data is not None
    assert result.data["notes"] == [{"kind": "footprint_libraries_loaded", "seconds": 1.5}]
    sidecar = ctx.project.dir / ".kicad-agent" / "renders" / f"{STEM}.svg.json"
    # unsanitised here; the renders route reduces it to [A-Za-z0-9_.+-]
    assert json.loads(sidecar.read_text()) == {"download_name": "Noted_R 0805.svg"}


async def test_fp_render_sidecar_failure_is_not_fatal(
    ctx: ToolContext, fake: FakeRender, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Path, *_a: Any, **_k: Any) -> int:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_text", refuse)
    result = await tool.fp_render(ctx, {"lib_id": LIB_ID})
    assert result.data is not None and result.data["svg"] == f"{STEM}.svg"
    assert len(result.images) == 1


async def test_fp_render_errors_pass_through(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*_a: Any, **_k: Any) -> RenderOutput:
        raise StructuredToolError("Footprint render failed", "x", "y", code="render_failed")

    monkeypatch.setattr(tool, "render_footprint", fail)
    with pytest.raises(StructuredToolError) as err:
        await tool.fp_render(ctx, {"lib_id": LIB_ID})
    assert err.value.code == "render_failed"
