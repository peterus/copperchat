"""`fp_check` on a real api-server (F2 Task 3, spec §7.4): stock footprints against a
library symbol and against an `expected` land pattern (the U202 case)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
from copperchat.kicad_tools.footprints.read import fp_check, fp_get
from copperchat.library.service import library_for
from copperchat.tools.spec import ToolContext
from tests.kicad.edit.helpers import v2

pytestmark = pytest.mark.kicad

# the stock VSON-10 (TPS63000 / U202) footprint; looked up in the base image's
# Package_SON.pretty (Texas_DRC0010J and Texas_DRC0010J_ThermalVias exist)
DRC0010J = "Package_SON:Texas_DRC0010J"
GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "fp_check_DRC0010J.json"


def ctx(session: KicadSession) -> ToolContext:
    return ToolContext(project=session.project, session=session, settings=session.settings)


def _rules(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for f in data["findings"]:
        out.setdefault(f["rule"], []).append(f)
    return out


async def test_stock_with_symbol(blank_nopcb_session: KicadSession) -> None:
    session = blank_nopcb_session
    assert await library_for(session).wait_ready(120)
    r = await fp_check(ctx(session), {"lib_id": SEED_LIB_ID, "symbol": "Device:L"})
    data = r.data
    assert data is not None
    assert data["lib_id"] == SEED_LIB_ID
    assert data["passed"], data["findings"]
    summary = _rules(data)["pad_count_summary"]
    assert summary[0]["severity"] == "info"
    assert summary[0]["message"] == "2 pins / 2 pads / 0 unnumbered"
    assert "needs_user_review" not in _rules(data)  # stock footprint
    assert len(data["geometry_hash"]) == 64
    assert "footprint_libraries_loaded" in {n["kind"] for n in data["notes"]}


async def test_reference_not_placed(blank_nopcb_session: KicadSession) -> None:
    with pytest.raises(StructuredToolError) as err:
        await fp_check(ctx(blank_nopcb_session), {"lib_id": SEED_LIB_ID, "symbol": "R201"})
    assert err.value.code == "not_found"


async def test_placed_references(blank_nopcb_session: KicadSession) -> None:
    """A placed reference reads the placed symbol's definition: all units' pins (the
    session works on a temporary copy of the blank project)."""
    session = blank_nopcb_session
    await session.ensure_symbol_libraries()

    def place(h: Any) -> None:
        sch = h.schematic()
        sch.place_symbol_from_library("Device:D", v2(50.8, 50.8), reference="D1")
        sch.place_symbol_from_library(
            "Amplifier_Operational:LM358", v2(101.6, 50.8), reference="U1"
        )

    async with session.commit("place D1, U1"):  # uncommitted items are not listed yet
        await session.call(place)
    diode = await fp_check(ctx(session), {"lib_id": SEED_LIB_ID, "symbol": "D1"})
    dual = await fp_check(ctx(session), {"lib_id": SEED_LIB_ID, "symbol": "U1"})
    assert diode.data is not None and dual.data is not None
    summary = _rules(diode.data)["pad_count_summary"][0]["message"]
    assert summary == "2 pins / 2 pads / 0 unnumbered"
    # only unit 1 is placed; the pins of units 2 and 3 (power) count as well
    assert _rules(dual.data)["pad_count_summary"][0]["message"].startswith("8 pins / 2 pads")
    missing = {f["pads"][0] for f in _rules(dual.data)["pin_without_pad"]}
    assert missing == {"3", "4", "5", "6", "7", "8"}
    assert not dual.data["passed"]


def _board_size(pad: dict[str, Any]) -> list[float]:
    w, h = pad["size"]
    return [h, w] if abs(pad["rotation"] % 180 - 90) < 1e-6 else [w, h]


async def test_stock_expected_u202_pattern(blank_nopcb_session: KicadSession) -> None:
    session = blank_nopcb_session
    got = await fp_get(ctx(session), {"lib_id": DRC0010J})
    assert got.data is not None
    pads = [
        {"number": p["number"], "at": list(p["at"]), "size": _board_size(p)}
        | ({"drill": min(p["drill"]["size"])} if p.get("drill") else {})
        for p in got.data["pads"]
        if p["number"]
    ]
    table = {"lib_id": DRC0010J, "pads": pads}
    if os.environ.get("UPDATE_GOLDENS") == "1":
        GOLDEN.write_text(json.dumps(table, indent=2) + "\n")
    # CopperChat's measured pad table, kept for the manual datasheet comparison (§16.3)
    assert table == json.loads(GOLDEN.read_text())

    ok = await fp_check(ctx(session), {"lib_id": DRC0010J, "expected": {"pads": pads}})
    assert ok.data is not None
    assert ok.data["passed"], ok.data["findings"]
    assert "expected_mismatch" not in _rules(ok.data)

    moved = [dict(p) for p in pads]
    first = next(i for i, p in enumerate(moved) if p["number"] == "1")
    moved[first]["at"] = [moved[first]["at"][0] + 0.1, moved[first]["at"][1]]
    bad = await fp_check(ctx(session), {"lib_id": DRC0010J, "expected": {"pads": moved}})
    assert bad.data is not None
    assert not bad.data["passed"]
    mismatch = _rules(bad.data)["expected_mismatch"]
    assert [f["pads"] for f in mismatch] == [["1"]]
    assert bad.data["geometry_hash"] == ok.data["geometry_hash"] == got.data["geometry_hash"]
