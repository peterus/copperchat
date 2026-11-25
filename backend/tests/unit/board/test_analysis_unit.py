"""`pcb_analyze` without KiCad (SP4 P3a Task 5): registration, `preset_for`, the job module
and its result shape, `strip_fills`."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from copperchat.agent.profiles import PCB_READ, PROFILES, profile_specs
from copperchat.board import jobs
from copperchat.board.boardmd import BoardDoc, ClassOverride
from copperchat.board.geom import rect_poly
from copperchat.board.jobs import MAX_ENTRIES, analyse_job, strip_fills
from copperchat.board.presets import PRESETS
from copperchat.kicad_tools.board.analysis import pcb_analyze, preset_for
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
    filled = replace(zone(filled_area_mm2=99.0), filled=(poly,), filled_holes=((hole,),))
    keepout = zone("K", "", rule_area=True, keepout=frozenset({"tracks"}), kiid="k2")
    m = circuit(res("R1", "A", "B"), zones=(filled, keepout))
    out = strip_fills(m)
    assert out.zones == (replace(filled, filled=(), filled_holes=()), keepout)
    assert out.zones[0].filled_area_mm2 == 99.0
    assert replace(out, zones=m.zones) == m  # everything else kept
    assert strip_fills(out) == out
