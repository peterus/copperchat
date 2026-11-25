"""`pcb_analyze` / `pcb_metrics` without KiCad (SP4 P3a Task 5): registration, `preset_for`,
the job module and its result shapes, `strip_fills`."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from copperchat.agent.profiles import PCB_READ, PROFILES, profile_specs
from copperchat.board import jobs
from copperchat.board.boardmd import BoardDoc, ClassOverride
from copperchat.board.geom import rect_poly
from copperchat.board.jobs import MAX_ENTRIES, analyse_job, metrics_job, strip_fills
from copperchat.board.presets import PRESETS
from copperchat.kicad_tools.board.analysis import metrics_data, pcb_analyze, pcb_metrics, preset_for
from copperchat.kicad_tools.board.api import DrcMarker, DrcResult
from copperchat.kicad_tools.board.specs import TOOL_SPECS
from copperchat.tools.registry import all_specs

from .builders import zone
from .builders_circuit import cap, circuit, conn, ic, ind, res, xtal

LIST_KEYS = (
    "decoupling",
    "switchers",
    "crystals",
    "pairs",
    "blocks",
    "critical_nets",
    "overrides_applied",
    "board_md_errors",
)
ALL_KEYS = ("parts", "nets", *LIST_KEYS)


def _board() -> Any:
    mcu = {
        "1": "+3V3",
        "2": "GND",
        "3": "XIN",
        "4": "XOUT",
        "5": "/USB_DP",
        "6": "/USB_DN",
        "7": "+3V3",
    }
    usb = {"1": "VBUS", "2": "/USB_DP", "3": "/USB_DN", "4": "GND"}
    buck = {"1": "VBUS", "2": "GND", "3": "SW", "4": "FB", "5": "VBUS", "6": "EN"}
    return circuit(
        ic("U1", mcu, (50.0, 40.0)),
        conn("J1", usb, (10.0, 40.0)),
        cap("C1", "+3V3", "GND", (51.0, 42.0)),
        xtal("Y1", "XIN", "XOUT", (53.0, 45.0)),
        cap("C4", "XIN", "GND", (52.0, 46.0)),
        cap("C5", "XOUT", "GND", (54.0, 46.0)),
        ic("U3", buck, (80.0, 20.0), sheet="/power/"),
        cap("C7", "VBUS", "GND", (79.0, 22.0), sheet="/power/"),
        ind("L1", "SW", "+3V3", (83.0, 23.0), sheet="/power/"),
        cap("C8", "+3V3", "GND", (85.0, 23.0), sheet="/power/"),
        res("R5", "+3V3", "FB", (84.0, 25.0), sheet="/power/"),
        res("R6", "FB", "GND", (84.0, 26.0), sheet="/power/"),
    )


# ---- registration --------------------------------------------------------------------------


def test_specs_registered() -> None:
    spec = next(s for s in all_specs() if s.name == "pcb_analyze")
    assert spec in TOOL_SPECS
    assert spec.handler is pcb_analyze
    assert not spec.mutating and not spec.changes_documents
    assert spec.prepare is None and spec.apply is None
    assert "pcb_analyze" in PCB_READ
    for kind in PROFILES:
        names = {s.name for s in profile_specs(kind, all_specs(), board_tools=False)}
        assert "pcb_analyze" in names, kind
    schema = spec.input_schema
    assert schema["additionalProperties"] is False
    Draft202012Validator.check_schema(schema)
    v = Draft202012Validator(schema)
    for ok in ({}, {"scope": "parts"}, {"scope": "nets"}, {"scope": "blocks"}, {"scope": "all"}):
        v.validate(ok)
    for bad in ({"scope": "everything"}, {"scope": 1}, {"extra": True}):
        with pytest.raises(ValidationError):
            v.validate(bad)
    assert "Read-only" in spec.description


# ---- preset_for ----------------------------------------------------------------------------


def test_preset_for_doc_then_layers() -> None:
    four = circuit(res("R1", "A", "B"), copper_layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    assert preset_for(four, BoardDoc(preset="jlcpcb-6l")) is PRESETS["jlcpcb-6l"]
    assert preset_for(four, BoardDoc()) is PRESETS["jlcpcb-4l"]
    assert preset_for(four, BoardDoc(preset="pcbway-4l")) is PRESETS["jlcpcb-4l"]
    two = circuit(res("R1", "A", "B"))
    assert preset_for(two, BoardDoc()) is PRESETS["jlcpcb-2l"]
    odd = circuit(res("R1", "A", "B"), copper_layers=("F.Cu",))
    assert preset_for(odd, BoardDoc()) is None


# ---- jobs ----------------------------------------------------------------------------------


def test_jobs_module_level() -> None:
    assert analyse_job.__module__ == "copperchat.board.jobs"
    assert strip_fills.__module__ == "copperchat.board.jobs"
    assert jobs.analyse_job is analyse_job  # importable by name: spawn pickling holds


def test_analyse_job_all() -> None:
    doc = BoardDoc(
        class_overrides=(ClassOverride("R5", "test_point", "test"),), errors=("row 3: bad width",)
    )
    data = analyse_job(_board(), doc)
    assert set(data) == set(ALL_KEYS) | {f"{k}_omitted" for k in ALL_KEYS}
    assert all(data[f"{k}_omitted"] == 0 for k in ALL_KEYS)
    assert data["parts"]["U3"] == "switching_regulator"
    assert data["parts"]["Y1"] == "crystal"
    assert data["nets"]["GND"] == "ground"
    assert data["nets"]["SW"] == "switch_node"
    assert data["switchers"] == [
        {
            "ic": "U3",
            "inductor": "L1",
            "sw_net": "SW",
            "vin_net": "VBUS",
            "in_caps": ["C7"],
            "out_caps": ["C8"],
            "fb_parts": ["R5", "R6"],
        }
    ]
    assert data["crystals"] == [
        {"ref": "Y1", "ic": "U1", "load_caps": ["C4", "C5"], "nets": ["XIN", "XOUT"]}
    ]
    assert data["pairs"] == [{"p": "/USB_DP", "n": "/USB_DN", "anchor": "U1"}]
    assert {"cap": "C1", "ic": "U1", "pin": "1", "supply_net": "+3V3", "ground_net": "GND"} in (
        data["decoupling"]
    )
    assert {b["anchor"] for b in data["blocks"]} >= {"U1", "U3"}
    assert data["overrides_applied"] == ["R5: other -> test_point"]
    assert data["board_md_errors"] == ["row 3: bad width"]


@pytest.mark.parametrize(
    ("scope", "keys"),
    [
        ("parts", ("parts", "decoupling", "switchers", "crystals", "overrides_applied")),
        ("nets", ("nets", "pairs", "critical_nets")),
        ("blocks", ("blocks",)),
        ("all", ALL_KEYS),
    ],
)
def test_analyse_job_scope(scope: str, keys: tuple[str, ...]) -> None:
    data = analyse_job(_board(), BoardDoc(), scope=scope)
    want = {*keys, "board_md_errors"}
    assert set(data) == want | {f"{k}_omitted" for k in want}
    assert data == {k: v for k, v in analyse_job(_board(), BoardDoc()).items() if k in data}


def test_analyse_job_caps_entries() -> None:
    n = MAX_ENTRIES + 7
    parts = [res(f"R{i}", f"N{i}", f"N{i + 1}", (float(i % 50), float(i // 50))) for i in range(n)]
    data = analyse_job(circuit(*parts), BoardDoc())
    assert len(data["parts"]) == MAX_ENTRIES and data["parts_omitted"] == 7
    assert list(data["parts"])[:3] == ["R0", "R1", "R2"]  # natural order kept
    assert len(data["nets"]) == MAX_ENTRIES and data["nets_omitted"] == n + 1 - MAX_ENTRIES


def test_strip_fills() -> None:
    poly = tuple(rect_poly(0, 0, 10, 10))
    hole = tuple(rect_poly(2, 2, 1, 1))
    filled = replace(
        zone(filled_area_mm2=99.0),
        filled=(poly,),
        filled_holes=((hole,),),
        filled_layers=("F.Cu",),
    )
    keepout = zone("K", "", rule_area=True, keepout=frozenset({"tracks"}), kiid="k2")
    m = circuit(res("R1", "A", "B"), zones=(filled, keepout))
    out = strip_fills(m)
    assert out.zones == (replace(filled, filled=(), filled_holes=(), filled_layers=()), keepout)
    assert out.zones[0].filled_area_mm2 == 99.0
    assert replace(out, zones=m.zones) == m  # everything else kept
    assert strip_fills(out) == out


# ---- pcb_metrics (registration, metrics_job, strip_fills equality) -------------------------


def test_metrics_spec_registered() -> None:
    spec = next(s for s in all_specs() if s.name == "pcb_metrics")
    assert spec in TOOL_SPECS
    assert spec.handler is pcb_metrics
    assert not spec.mutating and not spec.changes_documents
    assert "pcb_metrics" in PCB_READ
    schema = spec.input_schema
    assert schema["additionalProperties"] is False
    Draft202012Validator.check_schema(schema)
    v = Draft202012Validator(schema)
    for ok in ({}, {"only": ["M4"]}, {"only": [f"M{i}" for i in range(1, 15)]}):
        v.validate(ok)
    for bad in ({"only": ["M15"]}, {"only": ["M4", "M4"]}, {"only": "M4"}, {"x": 1}):
        with pytest.raises(ValidationError):
            v.validate(bad)
    assert "Read-only" in spec.description


def test_metrics_job_module_level() -> None:
    assert metrics_job.__module__ == "copperchat.board.jobs"


def _m3(report: Any) -> Any:
    return next(m for m in report.metrics if m.id == "M3")


def test_metrics_job_parity_passthrough() -> None:
    m = _board()
    drc = DrcResult(markers=(), unconnected=0, seconds=0.1)
    marker = DrcMarker("missing_footprint", "error", "U9 missing", (), None)
    only = frozenset({"M3"})
    with_parity = metrics_job(m, BoardDoc(), drc, None, only, (marker,))
    assert _m3(with_parity).value == 1
    assert _m3(with_parity).verdict == "fail"
    without = metrics_job(m, BoardDoc(), drc, None, only, None)
    assert _m3(without).value == 0
    assert [x.id for x in without.metrics] == ["M3"]


def test_strip_fills_keeps_m11_free_metrics() -> None:
    poly = tuple(rect_poly(0, 0, 100, 80))
    hole = tuple(rect_poly(40, 40, 5, 5))
    plane = replace(
        zone("P", "GND", ("In1.Cu",), poly, filled_area_mm2=7975.0),
        filled=(poly,),
        filled_holes=((hole,),),
    )
    m = replace(_board(), zones=(plane,), copper_layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    drc = DrcResult(markers=(), unconnected=2, seconds=0.1)
    only = frozenset(f"M{i}" for i in range(1, 15) if i != 11)
    preset = PRESETS["jlcpcb-4l"]
    full = metrics_job(m, BoardDoc(), drc, preset, only)
    stripped = metrics_job(strip_fills(m), BoardDoc(), drc, preset, only)
    assert full == stripped
    assert "M11" not in {x.id for x in full.metrics}


def test_metrics_data_shape() -> None:
    m = _board()
    report = metrics_job(m, BoardDoc(), None, None, frozenset({"M1", "M4"}))
    data = metrics_data(report, m)
    assert set(data) == {
        "stage",
        "metrics",
        "hard_failures",
        "reviews",
        "placement_hash",
        "board_hash",
    }
    assert [x["id"] for x in data["metrics"]] == ["M1", "M4"]
    assert set(data["metrics"][0]) == {
        "id",
        "value",
        "verdict",
        "hard",
        "review",
        "offenders",
        "omitted",
    }
    json.dumps(data)  # plain JSON
