"""`pcb_check` pure parts (P3b Task 2): the `kicad-cli pcb drc --format json` parser, the
overall verdict, the schema and the result rows."""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest

from copperchat.board.metrics import Metric, MetricsReport
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board.api import DrcMarker
from copperchat.kicad_tools.board.check import (
    MAX_LISTED,
    crosscheck,
    parse_cli_drc,
    parse_level,
    verdict,
)
from copperchat.kicad_tools.board.specs_check import PCB_CHECK_SCHEMA

CLI_REPORT = """
{
  "$schema": "https://schemas.kicad.org/drc.v1.json",
  "coordinate_units": "mm",
  "date": "2026-11-25T10:00:00+0000",
  "kicad_version": "9.99.0",
  "source": "sp4_synth.kicad_pcb",
  "violations": [
    {"description": "Clearance violation (netclass 'Default' clearance 0.2000 mm)",
     "items": [
       {"description": "Pad 1 [GND] of C1 on F.Cu", "pos": {"x": 10.5, "y": 20.25},
        "uuid": "11111111-1111-1111-1111-111111111111"},
       {"description": "Track [+3V3] on F.Cu", "pos": {"x": 11.0, "y": 20.0},
        "uuid": "22222222-2222-2222-2222-222222222222"}
     ],
     "severity": "error", "type": "clearance"},
    {"description": "Silkscreen overlap", "items": [
       {"description": "Reference field of R1", "pos": {"x": 1, "y": 2},
        "uuid": "33333333-3333-3333-3333-333333333333"}],
     "severity": "warning", "type": "silk_overlap"}
  ],
  "unconnected_items": [
    {"description": "Missing connection", "items": [], "severity": "error",
     "type": "unconnected_items"},
    {"description": "Missing connection", "items": [], "severity": "error",
     "type": "unconnected_items"},
    {"description": "Missing connection", "items": [], "severity": "error",
     "type": "unconnected_items"}
  ],
  "schematic_parity": [
    {"description": "Missing footprint U5", "items": [
       {"description": "Symbol U5", "uuid": "44444444-4444-4444-4444-444444444444"}],
     "severity": "warning", "type": "missing_footprint"}
  ]
}
"""


def test_parse_cli_report() -> None:
    violations, parity, unconnected = parse_cli_drc(json.loads(CLI_REPORT))
    assert unconnected == 3
    assert [v.type for v in violations] == ["clearance", "silk_overlap"]
    clearance = violations[0]
    assert clearance.severity == "error"
    assert clearance.kiids == (
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    )
    assert clearance.pos == (10.5, 20.25)
    assert clearance.text.startswith("Clearance violation")
    assert violations[1].severity == "warning"
    assert len(parity) == 1
    assert parity[0].type == "missing_footprint"
    assert parity[0].kiids == ("44444444-4444-4444-4444-444444444444",)
    assert parity[0].pos is None


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"violations": None, "unconnected_items": "x", "schematic_parity": 5},
        {"violations": [None, 1, "x", {"items": "x"}, {"type": 5, "severity": []}]},
    ],
)
def test_parse_tolerates_odd_shapes(doc: dict[str, Any]) -> None:
    """Missing or odd keys never raise: odd entries are read with empty fields."""
    violations, parity, unconnected = parse_cli_drc(doc)
    assert parity == () and unconnected == 0
    assert all(isinstance(v, DrcMarker) for v in violations)


def test_parse_severity_mapping_and_bounds() -> None:
    doc = {
        "violations": [
            {"type": "x", "severity": "exclusion", "description": "d", "items": []},
            {"type": "y", "severity": "ignore", "description": "d" * 5000, "items": []},
            {"type": "z", "severity": "error", "excluded": True, "items": []},
            {"type": "p", "severity": "error", "items": [{"pos": {"x": "a", "y": 1}}]},
        ]
    }
    v, _, _ = parse_cli_drc(doc)
    assert [m.severity for m in v] == ["exclusion", "warning", "exclusion", "error"]
    assert len(v[1].text) <= 500
    assert v[3].pos is None


def _metric(mid: str, verdict_: str) -> Metric:
    return Metric(mid, 0, verdict_, None, None, ())  # type: ignore[arg-type]


def _report(*verdicts: str) -> MetricsReport:
    metrics = tuple(_metric(f"M{i + 1}", v) for i, v in enumerate(verdicts))
    return MetricsReport(metrics, verdicts.count("fail"), verdicts.count("review"), "placement")


def test_verdict() -> None:
    assert verdict(_report("pass", "review", "fail", "n/a")) == "fail"
    assert verdict(_report("pass", "review", "n/a")) == "review"
    assert verdict(_report("pass", "n/a", "pass")) == "pass"
    assert verdict(_report()) == "pass"


def test_schema() -> None:
    jsonschema.validate({"level": "quick"}, PCB_CHECK_SCHEMA)
    jsonschema.validate({"level": "full"}, PCB_CHECK_SCHEMA)
    for bad in ({}, {"level": "medium"}, {"level": "full", "x": 1}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, PCB_CHECK_SCHEMA)
    assert parse_level({"level": "full"}) == "full"
    for bad_args in ({}, {"level": "medium"}, {"level": "quick", "x": 1}, {"level": 1}):
        with pytest.raises(StructuredToolError) as err:
            parse_level(bad_args)
        assert err.value.code == "invalid_argument"


def _mk(t: str, kiids: tuple[str, ...], severity: str = "error") -> DrcMarker:
    return DrcMarker(t, severity, "x", kiids, None)  # type: ignore[arg-type]


def test_crosscheck_counts_differences() -> None:
    api = (
        _mk("clearance", ("a", "b")),
        _mk("DRCE_SILK_OVERLAP", ("c",), "warning"),
        _mk("unconnected_items", ("d", "e")),  # M2's, never compared
        _mk("track_width", ("f",)),
    )
    cli = (
        _mk("clearance", ("x", "y")),  # pad KIIDs differ in the saved copy: types count
        _mk("silk_overlap", ("c",), "warning"),
        _mk("hole_clearance", ("g",)),
        _mk("unconnected_items", ("z",)),
    )
    assert crosscheck(cli, api) == {"only_cli": 1, "only_api": 1}
    assert crosscheck(cli + cli[:1], api) == {"only_cli": 2, "only_api": 1}
    assert crosscheck((), ()) == {"only_cli": 0, "only_api": 0}


def test_max_listed() -> None:
    assert MAX_LISTED == 50
