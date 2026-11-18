"""`fp_get` / `fp_list_local` without KiCad: output caps (author call 16) and the input
schemas (F1 Tasks 5, 6)."""

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


def test_fp_list_local_schema() -> None:
    spec = next(s for s in TOOL_SPECS if s.name == "fp_list_local")
    schema = spec.input_schema
    assert schema["additionalProperties"] is False and schema["required"] == []
    assert schema["properties"]["query"]["maxLength"] == 200
    limit = schema["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 200, 50)
    assert not spec.mutating


@pytest.mark.parametrize(
    "args",
    [{"query": "x" * 201}, {"query": 5}, {"limit": 0}, {"limit": 201}, {"limit": True}],
)
async def test_fp_list_local_invalid_argument(ctx: ToolContext, args: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError) as err:
        await read.fp_list_local(ctx, args)
    assert err.value.code == "invalid_argument"


def test_to_dict_caps_repo_controlled_strings_and_lists() -> None:
    from copperchat.kicad_tools.footprints.model import Model3D, Text

    long = "x" * 600
    text = Text("user", "F.Fab", (0.0, 0.0), (1.0, 1.0), 0.0, long)
    model = Model3D("m.step", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    g = dataclasses.replace(
        _large(),
        pads=(),
        shapes=(),
        description=long,
        keywords=long,
        fields={f"F{i}": long for i in range(70)},
        texts=(text,) * 70,
        models=(model,) * 70,
    )
    for detail in ("summary", "full"):
        d = g.to_dict(detail)
        assert d["truncated"] is True and "500 chars" in d["note"]
        assert d["description"] == "x" * 500 + "…" and d["keywords"] == "x" * 500 + "…"
        assert len(d["fields"]) == 64 and set(d["fields"].values()) == {"x" * 500 + "…"}
        assert len(d["texts"]) == 64 and d["texts"][0]["value"] == "x" * 500 + "…"
        assert len(d["models"]) == 64
    # only the entry count over the cap
    many = dataclasses.replace(_large(), pads=(), shapes=(), models=(model,) * 65)
    assert many.to_dict()["truncated"] is True
    short = dataclasses.replace(
        _large(), pads=(), shapes=(), description="x" * 500, fields={"A": "y" * 500}
    ).to_dict()
    assert "truncated" not in short and short["description"] == "x" * 500


# -- fp_get against a fake session/service -------------------------------------------------


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from copperchat.library import service as service_mod
    from tests.unit.library.test_service import REPO, LocalSession, _policy

    session = LocalSession(tmp_path / REPO)

    async def policy(project: Any, projects_root: Path) -> Any:
        return _policy(session.root, project.dir)

    monkeypatch.setattr(service_mod, "repo_policy", policy)
    return session


def _fake_ctx(session: Any) -> ToolContext:
    return ToolContext(
        project=cast(Any, session.project),
        session=cast(KicadSession, session),
        settings=cast(Any, session.settings),
    )


async def test_fp_get_global_read(fake: Any) -> None:
    from tests.unit.library.test_service import SEED

    r = await read.fp_get(_fake_ctx(fake), {"lib_id": SEED})
    assert r.data is not None
    assert r.data["lib_id"] == SEED and r.data["scope"] == "global"
    assert r.data["review"] is None and r.data["source"] is None
    kinds = [n["kind"] for n in r.data["notes"]]
    assert "footprint_attributes_unavailable" in kinds


async def test_fp_get_local_read_review_and_source(
    fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kipy.proto.board import board_types_pb2 as bt

    from copperchat.kicad_tools.footprints.model import REVIEW_FIELD, SOURCE_FIELD
    from copperchat.library.service import library_for
    from tests.unit.library import test_service
    from tests.unit.library.test_service import LOCAL_URI, REPO

    instance = test_service._fixture("seed_srn4018.instance")
    for name, value in ((SOURCE_FIELD, "datasheet p. 4"), (REVIEW_FIELD, "yes")):
        field = bt.Field()
        field.name = name
        field.text.text.text = value
        instance.definition.items.add().Pack(field)
    real = test_service._fixture
    monkeypatch.setattr(
        test_service,
        "_fixture",
        lambda name: instance if name.endswith(".instance") else real(name),
    )
    fake.rows = {REPO: LOCAL_URI}
    info = await library_for(fake).local_library()
    assert info is not None
    info.lib.dir.mkdir(parents=True)
    (info.lib.dir / "L_Local.kicad_mod").write_bytes(b"")
    r = await read.fp_get(_fake_ctx(fake), {"lib_id": f"{REPO}:L_Local", "detail": "full"})
    assert r.data is not None and r.data["scope"] == "local"
    assert r.data["source"] == "datasheet p. 4"
    assert r.data["review"] == "needs_user_review"
    assert "shapes" in r.data


async def test_fp_get_unknown_is_not_found_with_closest(
    fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str]] = []

    async def closest(svc: Any, nickname: str, name: str) -> list[str]:
        asked.append((nickname, name))
        return ["Inductor_SMD:L_Bourns-SRN4018"]

    monkeypatch.setattr(read, "closest_footprints", closest)
    with pytest.raises(StructuredToolError) as err:
        await read.fp_get(_fake_ctx(fake), {"lib_id": "Inductor_SMD:L_Bourns-SRN4019"})
    assert err.value.code == "not_found"
    assert err.value.details == {"closest": ["Inductor_SMD:L_Bourns-SRN4018"]}
    assert asked == [("Inductor_SMD", "L_Bourns-SRN4019")]
