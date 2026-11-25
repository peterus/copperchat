"""`pcb_check` pure parts (P3b Task 2): the `kicad-cli pcb drc --format json` parser, the
overall verdict, the schema and the result rows."""

from __future__ import annotations

import json
from pathlib import Path
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


def test_excluded_parity_dropped(tmp_path: Any) -> None:
    from copperchat.kicad_tools.board.check import _read_and_parse

    report = json.loads(CLI_REPORT)
    report["schematic_parity"].append(
        {"type": "extra_footprint", "severity": "exclusion", "description": "x", "items": []}
    )
    path = tmp_path / "drc.json"
    path.write_text(json.dumps(report))
    parsed = _read_and_parse(path)
    assert parsed is not None
    violations, parity = parsed
    assert len(violations) == 2 and [p.type for p in parity] == ["missing_footprint"]
    assert _read_and_parse(tmp_path / "missing.json") is None


class _CopySession:
    """save_copy of a session whose schematic has unsaved edits (or is fine)."""

    def __init__(self, modified: bool) -> None:
        self.modified = modified
        self.copies: list[str] = []

    async def save_copy(self, doc: str, dest: Any, *, save_modified: bool = True) -> Any:
        from copperchat.kicad_session.errors import DocumentModified

        assert save_modified is False or doc == "pcb"
        if doc == "sch" and self.modified:
            raise DocumentModified("the schematic has unsaved edits")
        self.copies.append(doc)
        return dest


def _project(tmp_path: Any, sch: bool) -> Any:
    from copperchat.projects import Project

    return Project(
        id="p" * 12, name="p", rel_path="p", dir=tmp_path, pro_file=tmp_path / "p.kicad_pro",
        sch_file=tmp_path / "p.kicad_sch" if sch else None, pcb_file=tmp_path / "p.kicad_pcb",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("sch", "modified", "code"),
    [(False, False, "drc_rejected"), (True, True, "schematic_unsaved")],
)
async def test_cli_parity_refusals(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, sch: bool, modified: bool, code: str
) -> None:
    """No schematic → drc_rejected; an unsaved schematic → schematic_unsaved (never a
    save); kicad-cli never runs and the copy dir is removed."""
    from types import SimpleNamespace

    from copperchat.kicad_tools.board import check as check_mod
    from copperchat.projects import agent_dir

    async def no_cli(*a: Any, **k: Any) -> Any:
        raise AssertionError("kicad-cli must not run")

    monkeypatch.setattr(check_mod, "run_kicad_cli", no_cli)
    session = _CopySession(modified)
    ctx: Any = SimpleNamespace(project=_project(tmp_path, sch), session=session, settings=None)
    with pytest.raises(StructuredToolError) as err:
        await check_mod.cli_parity(ctx)
    assert err.value.code == code
    assert session.copies == []
    root = agent_dir(ctx.project) / "board" / "check"
    assert not root.exists() or list(root.iterdir()) == []


async def test_render_evidence_skipped_after_write(tmp_path: Any) -> None:
    """pcb_render records no evidence when a board write was logged since its model
    read; with none it records one row."""
    from types import SimpleNamespace

    from copperchat.changes.repo import ChangeEntry, ChangeRepo
    from copperchat.db import Database
    from copperchat.kicad_tools.board.views import _record_evidence

    db = await Database.open(tmp_path / "r.db")
    try:
        project = _project(tmp_path, True)
        ctx: Any = SimpleNamespace(
            project=project, editing=SimpleNamespace(db=db), conversation_id="c" * 32,
            run_id="run-1",
        )  # fmt: skip
        out: Any = SimpleNamespace(placement_hash="a" * 64, board_hash="b" * 64)
        since = "2026-11-25T10:00:00.000Z"
        await _record_evidence(ctx, "top", out, since)
        await ChangeRepo(db).add_many(
            [ChangeEntry(project.id, "run-1", None, 0, "pcb_move", "modify", "board_move", "",
                         "", created_at="2026-11-25T10:00:01.000Z")]
        )  # fmt: skip
        await _record_evidence(ctx, "bottom", out, since)
        rows = await db.fetchall("SELECT view FROM board_reviews ORDER BY id")
        assert [r[0] for r in rows] == ["top"]
    finally:
        await db.close()


async def test_cli_parity_netlist_failure_rejected(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kicad-cli exits 0 with an empty parity list when it cannot build the schematic
    netlist: that must not read as "in sync"."""
    from types import SimpleNamespace

    from copperchat.kicad_tools.board import check as check_mod
    from copperchat.kicad_tools.cli import CliResult

    async def cli(settings: Any, args: list[str], *, timeout: float, cwd: Any) -> CliResult:
        out = args[args.index("-o") + 1]
        Path(out).write_text(json.dumps({"violations": [], "schematic_parity": []}))
        return CliResult(0, "", "Failed to fetch schematic netlist for parity tests.\n")

    monkeypatch.setattr(check_mod, "run_kicad_cli", cli)
    ctx: Any = SimpleNamespace(
        project=_project(tmp_path, True), session=_CopySession(False), settings=None
    )
    with pytest.raises(StructuredToolError) as err:
        await check_mod.cli_parity(ctx)
    assert err.value.code == "drc_rejected" and "netlist" in err.value.why
