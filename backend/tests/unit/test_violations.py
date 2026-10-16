import json
import re
from pathlib import Path

from copperchat.kicad_tools.checks import SEVERITIES, parse_violations

REC = Path(__file__).resolve().parent.parent / "fixtures" / "kicad-cli"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
U = "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12


def test_drc_recorded_output_parses() -> None:
    raw = json.loads((REC / "drc.json").read_text())
    vs = parse_violations(raw, "pcb")
    n = (
        len(raw.get("violations", []))
        + len(raw.get("unconnected_items", []))
        + len(raw.get("schematic_parity", []))
    )
    assert len(vs) == n and n > 0
    for v in vs:
        assert set(v) == {"type", "severity", "description", "position_mm", "items"}
        assert v["severity"] in SEVERITIES
        for it in v["items"]:
            assert UUID.match(it["uuid"])


def test_erc_recorded_output_parses() -> None:
    raw = json.loads((REC / "erc.json").read_text())
    assert raw["coordinate_units"] == "mm"
    vs = parse_violations(raw, "sch")
    assert len(vs) == sum(len(s.get("violations", [])) for s in raw["sheets"])


def test_erc_violations_flattened_across_sheets() -> None:
    entry = {
        "type": "pin_not_connected",
        "severity": "error",
        "description": "Pin not connected",
        "items": [{"uuid": U, "pos": {"x": 1.27, "y": 2.54}, "description": "Pin 1"}],
    }
    raw = {
        "sheets": [
            {"path": "/", "violations": [entry]},
            {"path": "/amp/", "violations": [entry, entry]},
            {"path": "/empty/"},
        ]
    }
    vs = parse_violations(raw, "sch")
    assert len(vs) == 3 and vs[0]["position_mm"] == {"x": 1.27, "y": 2.54}


def test_synthetic_entry() -> None:
    raw = {
        "violations": [
            {
                "type": "clearance",
                "severity": "error",
                "description": "x",
                "items": [{"uuid": U, "pos": {"x": 10.5, "y": 2.0}, "description": "Pad 1"}],
            }
        ],
        "unconnected_items": [{"severity": "error", "description": "Missing connection"}],
    }
    v, u = parse_violations(raw, "pcb")
    assert v["position_mm"] == {"x": 10.5, "y": 2.0}
    assert v["items"][0]["position_mm"] == {"x": 10.5, "y": 2.0}
    assert u["type"] == "unconnected_items" and u["position_mm"] is None and u["items"] == []
