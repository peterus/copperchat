"""`fp_check` without KiCad (F2 Task 3, spec §7.4): input schema and bounds (author call
18), symbol forms, pins from a lib symbol or a placed reference, the result shape, and
the rules running off the event loop."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.footprints import checks, read
from copperchat.kicad_tools.footprints.model import FootprintGeometry, geometry_hash
from copperchat.kicad_tools.footprints.specs import TOOL_SPECS
from copperchat.projects import Project
from copperchat.tools import ToolContext
from tests.unit.footprints.test_checks import l201

LIB_ID = "CopperChat:Test"


def _spec() -> Any:
    return next(s for s in TOOL_SPECS if s.name == "fp_check")


def _closed(schema: dict[str, Any], path: str = "$") -> None:
    """additionalProperties false on every object of the schema."""
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, path
        for key, sub in schema["properties"].items():
            _closed(sub, f"{path}.{key}")
    if schema.get("type") == "array":
        _closed(schema["items"], f"{path}[]")


def test_fp_check_schema() -> None:
    spec = _spec()
    assert not spec.mutating
    schema = spec.input_schema
    _closed(schema)
    assert schema["required"] == ["lib_id"]
    props = schema["properties"]
    assert props["lib_id"]["maxLength"] == 256
    assert props["symbol"]["maxLength"] == 256
    allow = props["allow_unmatched_pads"]
    assert allow["maxItems"] == 64 and allow["items"]["maxLength"] == 16
    exp = props["expected"]
    assert exp["required"] == ["pads"]
    assert exp["properties"]["pads"]["maxItems"] == 512
    pad = exp["properties"]["pads"]["items"]
    assert set(pad["required"]) == {"number", "at", "size"}
    assert pad["properties"]["number"]["maxLength"] == 16
    at = pad["properties"]["at"]
    assert (at["minItems"], at["maxItems"]) == (2, 2)
    assert (at["items"]["minimum"], at["items"]["maximum"]) == (-500, 500)
    size = pad["properties"]["size"]["items"]
    assert (size["exclusiveMinimum"], size["maximum"]) == (0, 200)
    drill = pad["properties"]["drill"]
    assert (drill["exclusiveMinimum"], drill["maximum"]) == (0, 50)
    for key in ("courtyard", "body"):
        item = exp["properties"][key]["items"]
        assert (item["exclusiveMinimum"], item["maximum"]) == (0, 200), key
    pitch = exp["properties"]["pitch"]
    assert (pitch["exclusiveMinimum"], pitch["maximum"]) == (0, 200)
    tol = exp["properties"]["tolerance_mm"]
    assert (tol["minimum"], tol["maximum"], tol["default"]) == (0.001, 1.0, 0.01)


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


def _pad(**kw: Any) -> dict[str, Any]:
    return {"number": "1", "at": [0.0, 0.0], "size": [1.0, 1.0]} | kw


def _exp(*pads: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"lib_id": LIB_ID, "expected": {"pads": list(pads or [_pad()]), **kw}}


@pytest.mark.parametrize(
    "args",
    [
        {"lib_id": "NoColon"},
        {"lib_id": LIB_ID, "symbol": "R"},
        {"lib_id": LIB_ID, "symbol": "201"},
        {"lib_id": LIB_ID, "symbol": "R 201"},
        {"lib_id": LIB_ID, "symbol": "R201a"},
        {"lib_id": LIB_ID, "symbol": ":L"},
        {"lib_id": LIB_ID, "symbol": "Device:"},
        {"lib_id": LIB_ID, "symbol": 5},
        {"lib_id": LIB_ID, "symbol": "Device:" + "L" * 250},
        {"lib_id": LIB_ID, "allow_unmatched_pads": "10"},
        {"lib_id": LIB_ID, "allow_unmatched_pads": [str(i) for i in range(65)]},
        {"lib_id": LIB_ID, "allow_unmatched_pads": ["x" * 17]},
        {"lib_id": LIB_ID, "allow_unmatched_pads": [""]},
        {"lib_id": LIB_ID, "allow_unmatched_pads": [10]},
        {"lib_id": LIB_ID, "expected": []},
        {"lib_id": LIB_ID, "expected": {}},
        {"lib_id": LIB_ID, "expected": {"pads": [_pad()] * 513}},
        _exp(_pad(at=[500.5, 0])),
        _exp(_pad(at=[0, -501])),
        _exp(_pad(at=[0])),
        _exp(_pad(at=[0, 0, 0])),
        _exp(_pad(at=[float("nan"), 0])),
        _exp(_pad(at=[True, 0])),
        _exp(_pad(size=[0, 1])),
        _exp(_pad(size=[1, 200.1])),
        _exp(_pad(size=[1, float("inf")])),
        _exp(_pad(drill=0)),
        _exp(_pad(drill=50.5)),
        _exp(_pad(number="")),
        _exp(_pad(number="x" * 17)),
        _exp(_pad(number=1)),
        _exp(_pad(extra=1)),
        _exp({"number": "1", "at": [0, 0]}),
        _exp(pitch=0),
        _exp(pitch=201),
        _exp(courtyard=[0, 1]),
        _exp(body=[1, 300]),
        _exp(body=[1]),
        _exp(tolerance_mm=0.0005),
        _exp(tolerance_mm=1.5),
        _exp(tolerance_mm="0.01"),
        _exp(extra=True),
    ],
)
async def test_fp_check_invalid_argument(ctx: ToolContext, args: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError) as err:
        await read.fp_check(ctx, args)
    assert err.value.code == "invalid_argument", err.value.what


async def test_fp_check_refused_when_disabled(ctx: ToolContext, make_settings: Any) -> None:
    ctx.settings = make_settings(footprint_tools=False)
    with pytest.raises(StructuredToolError) as err:
        await read.fp_check(ctx, {"lib_id": LIB_ID})
    assert err.value.code == "footprint_tools_disabled"


# --- with a fake library service ------------------------------------------------------


def _lib_pin(number: str, name: str, unit: int = 1) -> Any:
    return SimpleNamespace(number=number, name=name, unit=unit)


class FakeService:
    def __init__(self, geometry: FootprintGeometry | None) -> None:
        self.geometry = geometry
        self.symbols: dict[str, Any] = {}

    async def footprint_geometry(self, lib_id: str, notes: list[dict[str, Any]]) -> Any:
        notes.append({"kind": "footprint_libraries_loaded", "seconds": 1.0})
        return self.geometry

    async def get_symbol(self, lib_id: str) -> Any:
        if lib_id not in self.symbols:
            raise StructuredToolError("Symbol not found", "unknown", "search", code="not_found")
        return self.symbols[lib_id]


class FakeSession:
    """`call` serves the placed-symbol reader with `placed` (ref -> {number: name})."""

    def __init__(self, placed: dict[str, dict[str, str]] | None) -> None:
        self.placed = placed

    async def call(self, fn: Any) -> Any:
        assert fn.func is read._placed_pins, fn
        (ref,) = fn.args
        if self.placed is None:
            return None, []
        return self.placed.get(ref), sorted(self.placed)


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> FakeService:
    s = FakeService(l201())
    s.symbols["Device:L"] = SimpleNamespace(
        pins=[_lib_pin("1", "1"), _lib_pin("2", "2")], pins_known=True
    )
    monkeypatch.setattr(read, "library_for", lambda _session: s)

    async def closest(_svc: Any, _nickname: str, _name: str) -> list[str]:
        return ["CopperChat:Tst"]

    monkeypatch.setattr(read, "closest_footprints", closest)
    return s


def _with_session(ctx: ToolContext, placed: dict[str, dict[str, str]] | None) -> ToolContext:
    return dataclasses.replace(ctx, session=cast(KicadSession, FakeSession(placed)))


def _rules(data: dict[str, Any]) -> set[str]:
    return {f["rule"] for f in data["findings"]}


async def test_fp_check_runs_in_a_thread(
    ctx: ToolContext, svc: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    real = asyncio.to_thread

    async def spy(fn: Any, *args: Any, **kw: Any) -> Any:
        calls.append(fn)
        return await real(fn, *args, **kw)

    monkeypatch.setattr(read.asyncio, "to_thread", spy)
    r = await read.fp_check(ctx, {"lib_id": LIB_ID})
    assert calls == [checks.run_checks]
    data = r.data
    assert data is not None
    assert data["lib_id"] == LIB_ID
    assert data["passed"] is True
    assert set(data["counts"]) == {"error", "warning", "info"}
    assert data["geometry_hash"] == geometry_hash(l201())
    assert data["notes"] == [{"kind": "footprint_libraries_loaded", "seconds": 1.0}]
    assert data["truncated"] is False
    # scope "local" -> local=True (pending False until F4a): the review rule runs
    assert "needs_user_review" in _rules(data)
    assert "pad_count_summary" not in _rules(data)  # no symbol given


async def test_fp_check_stock_has_no_review_rule(ctx: ToolContext, svc: FakeService) -> None:
    svc.geometry = dataclasses.replace(l201(), scope="global")
    r = await read.fp_check(ctx, {"lib_id": LIB_ID})
    assert r.data is not None and "needs_user_review" not in _rules(r.data)


async def test_fp_check_not_found(ctx: ToolContext, svc: FakeService) -> None:
    svc.geometry = None
    with pytest.raises(StructuredToolError) as err:
        await read.fp_check(ctx, {"lib_id": "CopperChat:Tset"})
    assert err.value.code == "not_found"
    assert err.value.details == {"closest": ["CopperChat:Tst"]}


async def test_fp_check_lib_symbol(ctx: ToolContext, svc: FakeService) -> None:
    r = await read.fp_check(ctx, {"lib_id": LIB_ID, "symbol": "Device:L"})
    assert r.data is not None and r.data["passed"]
    summary = [f for f in r.data["findings"] if f["rule"] == "pad_count_summary"]
    assert summary[0]["message"] == "2 pins / 2 pads / 0 unnumbered"


async def test_fp_check_lib_symbol_mismatch(ctx: ToolContext, svc: FakeService) -> None:
    svc.symbols["Device:Q"] = SimpleNamespace(
        pins=[_lib_pin("1", "B"), _lib_pin("2", "C"), _lib_pin("3", "E")], pins_known=True
    )
    r = await read.fp_check(ctx, {"lib_id": LIB_ID, "symbol": "Device:Q"})
    assert r.data is not None and not r.data["passed"]
    assert "pin_without_pad" in _rules(r.data)


async def test_fp_check_unknown_lib_symbol(ctx: ToolContext, svc: FakeService) -> None:
    with pytest.raises(StructuredToolError) as err:
        await read.fp_check(ctx, {"lib_id": LIB_ID, "symbol": "Device:Nope"})
    assert err.value.code == "not_found"


async def test_fp_check_derived_symbol_without_pins(ctx: ToolContext, svc: FakeService) -> None:
    svc.symbols["Lib:Derived"] = SimpleNamespace(pins=[], pins_known=False)
    with pytest.raises(StructuredToolError) as err:
        await read.fp_check(ctx, {"lib_id": LIB_ID, "symbol": "Lib:Derived"})
    assert err.value.code == "library_error"


async def test_fp_check_placed_reference(ctx: ToolContext, svc: FakeService) -> None:
    c = _with_session(ctx, {"L201": {"1": "1", "2": "2"}, "U1": {"1": "A"}})
    r = await read.fp_check(c, {"lib_id": LIB_ID, "symbol": "L201"})
    assert r.data is not None and r.data["passed"]
    assert "pad_count_summary" in _rules(r.data)


async def test_fp_check_placed_reference_not_found(ctx: ToolContext, svc: FakeService) -> None:
    c = _with_session(ctx, {"L201": {"1": "1", "2": "2"}})
    with pytest.raises(StructuredToolError) as err:
        await read.fp_check(c, {"lib_id": LIB_ID, "symbol": "L202"})
    assert err.value.code == "not_found"
    assert "L201" in err.value.suggestion


async def test_fp_check_reference_without_schematic(ctx: ToolContext, svc: FakeService) -> None:
    c = _with_session(ctx, None)
    with pytest.raises(StructuredToolError) as err:
        await read.fp_check(c, {"lib_id": LIB_ID, "symbol": "#PWR01"})
    assert err.value.code == "not_found"


async def test_fp_check_expected_and_allow(ctx: ToolContext, svc: FakeService) -> None:
    pads = [_pad(number="1", at=[-1.1, 0], size=[1.3, 3.5]), _pad(at=[1.1, 0], size=[1.3, 3.5])]
    pads[1]["number"] = "2"
    ok = await read.fp_check(ctx, _exp(*pads, pitch=2.2, tolerance_mm=0.001))
    assert ok.data is not None and ok.data["passed"], ok.data
    pads[0]["at"] = [-1.0, 0]
    bad = await read.fp_check(ctx, _exp(*pads))
    assert bad.data is not None and not bad.data["passed"]
    assert "expected_mismatch" in _rules(bad.data)


async def test_fp_check_allow_unmatched(ctx: ToolContext, svc: FakeService) -> None:
    c = _with_session(ctx, {"U1": {"1": "A"}})
    plain = await read.fp_check(c, {"lib_id": LIB_ID, "symbol": "U1"})
    assert plain.data is not None
    sev = [f["severity"] for f in plain.data["findings"] if f["rule"] == "pad_without_pin"]
    assert sev == ["warning"]
    allowed = await read.fp_check(
        c, {"lib_id": LIB_ID, "symbol": "U1", "allow_unmatched_pads": ["2"]}
    )
    assert allowed.data is not None
    sev = [f["severity"] for f in allowed.data["findings"] if f["rule"] == "pad_without_pin"]
    assert sev == ["info"]


@pytest.mark.parametrize(
    ("pins", "polarized"),
    [
        ({"1": "K", "2": "A"}, True),
        ({"1": "1", "2": "2"}, False),  # names equal their numbers
        ({"1": "~", "2": "~"}, False),
        ({"1": "K", "2": "~"}, False),
        ({"1": "A", "2": "A"}, False),  # same name
        ({"1": "", "2": "A"}, False),
        ({"1": "K", "2": "A", "3": "G"}, False),  # not exactly 2 pins
    ],
)
def test_polarized(pins: dict[str, str], polarized: bool) -> None:
    assert read.is_polarized(pins) is polarized


async def test_symbol_pins_union_of_units(ctx: ToolContext, svc: FakeService) -> None:
    svc.symbols["Amp:Dual"] = SimpleNamespace(
        pins=[
            _lib_pin("1", "OUT", 1),
            _lib_pin("2", "-", 1),
            _lib_pin("5", "+", 2),
            _lib_pin("8", "V+", 0),
            _lib_pin("8", "V+", 0),  # once per body style
        ],
        pins_known=True,
    )
    assert await read.symbol_pins(ctx, "Amp:Dual") == (["1", "2", "5", "8"], False)
    svc.symbols["Device:D"] = SimpleNamespace(
        pins=[_lib_pin("1", "K"), _lib_pin("2", "A")], pins_known=True
    )
    assert await read.symbol_pins(ctx, "Device:D") == (["1", "2"], True)
