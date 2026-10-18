"""Golden scenario tests (E10 Task 2, spec §12): the three reference circuits built from
the KiCad-created `blank` project **only through the agent tools** (real adapter, real
headless KiCad), scripted like spike 2D's `scripted.py`.

Each scenario asserts golden connectivity by role on the live netlist **and** on
`kicad-cli sch export netlist`, 0 ERC errors, every item on the grid, no symbol body
overlap, no wire interior over a pin end, 0805 passives, and that the change log matches
`kicad-cli sch diff` against the start snapshot.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from copperchat.changes.diff import diff_against_snapshot
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.history.service import HistoryService
from copperchat.history.store import Snapshot
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools.edit.geometry import in_segment_interior, on_grid
from copperchat.kicad_tools.edit.scene import read_scene, sheet_refs
from copperchat.tools.spec import ToolContext
from tests.kicad.edit import golden
from tests.kicad.edit.test_checks_real import history
from tests.support.edit import RUN_ID, call_tool, edit_ctx
from tests.support.kicad_cli import cli_netlist

pytestmark = pytest.mark.kicad

R0805 = "Resistor_SMD:R_0805_2012Metric"
C0805 = "Capacitor_SMD:C_0805_2012Metric"
LED0805 = "LED_SMD:LED_0805_2012Metric"
HEADER2 = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"

# A step: (tool, args) or (tool, args, alias). Strings like "{R}.1" are filled with the
# reference the placement of alias "R" got (references are assigned by the tools).
# A "@find:<alias>" `at` takes the first sch_find_space candidate of the previous step.
Step = tuple[Any, ...]

S1: list[Step] = [
    ("sch_place_symbol", {"lib_id": "Connector_Generic:Conn_01x02", "at": [60.96, 76.2],
                          "mirror": "y", "footprint": HEADER2}, "J"),
    ("sch_place_symbol", {"lib_id": "Device:R", "at": [86.36, 63.5], "value": "1k",
                          "footprint": R0805}, "R"),
    ("sch_place_symbol", {"lib_id": "Device:LED", "at": [86.36, 86.36], "rotation": 90,
                          "value": "red", "footprint": LED0805}, "D"),
    ("sch_add_power", {"net": "+5V", "at": ["{J}.1"]}),
    ("sch_add_power", {"net": "GND", "at": ["{J}.2"]}),
    ("sch_add_power", {"net": "PWR_FLAG", "at": ["{J}.1", "{J}.2"]}),
    ("sch_connect", {"from": "{R}.1", "to": "+5V"}),
    ("sch_connect", {"from": "{R}.2", "to": "{D}.A"}),
    ("sch_connect", {"from": "{D}.K", "to": "GND"}),
]  # fmt: skip

S2: list[Step] = [
    ("sch_place_symbol", {"lib_id": "Connector_Generic:Conn_01x02", "at": [50.8, 76.2],
                          "mirror": "y", "footprint": HEADER2}, "J"),
    ("sch_place_symbol", {"lib_id": "Regulator_Linear:AMS1117-3.3", "at": [101.6, 73.66]}, "U"),
    ("sch_place_symbol", {"lib_id": "Device:C", "at": [83.82, 81.28], "value": "10uF",
                          "footprint": C0805}, "CIN"),
    ("sch_place_symbol", {"lib_id": "Device:C", "at": [119.38, 81.28], "value": "22uF",
                          "footprint": C0805}, "COUT"),
    ("sch_add_power", {"net": "+5V", "at": ["{J}.1", "{U}.VI", "{CIN}.1"]}),
    ("sch_add_power", {"net": "GND", "at": ["{J}.2", "{U}.GND", "{CIN}.2", "{COUT}.2"]}),
    ("sch_add_power", {"net": "+3V3", "at": ["{U}.VO", "{COUT}.1"]}),
    ("sch_add_power", {"net": "PWR_FLAG", "at": ["{J}.1", "{J}.2"]}),
]  # fmt: skip

MCU = "MCU_Microchip_ATtiny:ATtiny85-20S"
TMP102 = "Sensor_Temperature:TMP102xxDRL"
SENSOR = "/Sensor/"

S3: list[Step] = [
    ("sch_place_symbol", {"lib_id": MCU, "at": [88.9, 101.6]}, "U"),
    ("sch_place_symbol", {"lib_id": "Device:C", "at": [55.88, 101.6], "value": "100nF",
                          "footprint": C0805}, "C"),
    ("sch_add_power", {"net": "+3V3", "at": ["{U}.VCC", "{C}.1"]}),
    ("sch_add_power", {"net": "GND", "at": ["{U}.GND", "{C}.2"]}),
    ("sch_add_power", {"net": "PWR_FLAG", "at": ["{U}.VCC", "{U}.GND"]}),
    ("sch_no_connect", {"pins": ["{U}.PB1", "{U}.PB3", "{U}.PB4", "{U}.PB5"]}),
    ("sch_add_label", {"net": "I2C_SDA", "at": ["{U}.PB0"]}),
    ("sch_add_label", {"net": "I2C_SCL", "at": ["{U}.PB2"]}),
    ("sch_place_symbol", {"lib_id": "Device:R", "at": [134.62, 83.82], "value": "4.7k",
                          "footprint": R0805}, "RSDA"),
    ("sch_place_symbol", {"lib_id": "Device:R", "at": [144.78, 83.82], "value": "4.7k",
                          "footprint": R0805}, "RSCL"),
    ("sch_add_power", {"net": "+3V3", "at": ["{RSDA}.1", "{RSCL}.1"]}),
    ("sch_connect", {"from": "{RSDA}.2", "to": "I2C_SDA"}),
    ("sch_connect", {"from": "{RSCL}.2", "to": "I2C_SCL"}),
    ("sch_add_sheet", {"name": "Sensor", "file": "sensor.kicad_sch", "at": [177.8, 88.9],
                       "size": [50.8, 38.1]}),
    ("sch_add_sheet_pin", {"sheet": "Sensor", "name": "I2C_SDA", "direction": "bidirectional"}),
    ("sch_add_sheet_pin", {"sheet": "Sensor", "name": "I2C_SCL", "direction": "bidirectional"}),
    ("sch_connect", {"from": "Sensor.I2C_SDA", "to": "I2C_SDA"}),
    ("sch_connect", {"from": "Sensor.I2C_SCL", "to": "I2C_SCL"}),
    ("sch_place_symbol", {"lib_id": TMP102, "at": [50.8, 35.56], "sheet": SENSOR}, "S"),
    ("sch_find_space", {"lib_id": "Device:C", "near": "{S}", "value": "100nF",
                        "sheet": SENSOR}),
    ("sch_place_symbol", {"lib_id": "Device:C", "at": "@find", "value": "100nF",
                          "footprint": C0805, "sheet": SENSOR}, "CS"),
    ("sch_add_power", {"net": "+3V3", "at": ["{S}.V+", "{CS}.1"]}),
    ("sch_add_power", {"net": "GND", "at": ["{S}.GND", "{CS}.2", "{S}.ADD0"]}),
    ("sch_no_connect", {"pins": ["{S}.ALERT"]}),
    ("sch_connect", {"from": "{S}.SDA", "to": "I2C_SDA"}),
    ("sch_connect", {"from": "{S}.SCL", "to": "I2C_SCL"}),
]  # fmt: skip

SCRIPTS = {"s1_led": S1, "s2_ldo": S2, "s3_mcu_i2c_subsheet": S3}


def _fill(value: Any, refs: dict[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: refs[m.group(1)], value)
    if isinstance(value, list):
        return [_fill(v, refs) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, refs) for k, v in value.items()}
    return value


async def run_script(
    ctx: ToolContext, steps: list[Step], refs: dict[str, str] | None = None
) -> dict[str, str]:
    """Run the steps through the real tool adapter; returns alias -> reference (`refs`:
    the aliases of earlier scripts, extended in place)."""
    refs = {} if refs is None else refs
    found: list[float] | None = None
    for step in steps:
        tool, args = step[0], _fill(step[1], refs)
        if args.get("at") == "@find":
            assert found is not None
            args["at"] = found
        out, err = await call_tool(ctx, tool, **args)
        assert not err, (tool, args, out)
        if tool == "sch_find_space":
            found = out["candidates"][0]["at_mm"]
        if len(step) > 2:
            refs[step[2]] = out["placed"]["ref"]
    return refs


async def blank_ctx(s: KicadSession, db: Database) -> ToolContext:
    """Editing context with a budget large enough for a whole circuit in one turn (the
    plan path is covered by test_undo / test_plan_flow)."""
    await s.ensure_symbol_libraries()
    await EditingSettingsRepo(db).put(s.project.id, change_budget=500)
    return edit_ctx(s, db)


async def start_snapshot(s: KicadSession, db: Database) -> tuple[HistoryService, Snapshot]:
    await s.save("sch")
    svc = history(s, db)
    return svc, await svc.store(s.project).snapshot(RUN_ID, "turn")


# The KiCad-saved schematic files of each scenario go to `$EXPORT_ENV/<scenario>/` (the
# Playwright viewer test's edited fixtures, frontend/e2e/fixtures/edited/).
EXPORT_ENV = "COPPERCHAT_EDITED_FIXTURES"


def refs_by_sheet_file(h: KicadHandle) -> dict[str, list[str]]:
    """Sheet file name -> the non-power references drawn on it."""
    out: dict[str, set[str]] = {}
    for ref in sheet_refs(h):
        names = {x.ref for x in read_scene(h, ref, boxes=False).symbols.values() if not x.power}
        out.setdefault(Path(ref.file).name, set()).update(names)
    return {f: sorted(r) for f, r in out.items()}


def export_fixture(
    name: str, root: Path, live: golden.Components, by_sheet: dict[str, list[str]], out: Path
) -> None:
    dest = out / name
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    others = sorted(p.name for p in root.parent.glob("*.kicad_sch") if p.name != root.name)
    files = [root.name, *others]  # the root sheet first
    for f in files:
        shutil.copy2(root.parent / f, dest / f)  # verbatim: written by KiCad's save
    manifest = {"sch": files, "refs": sorted(live), "sheets": by_sheet}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")


# -- checks ----------------------------------------------------------------------------


def layout_problems(h: KicadHandle) -> dict[str, list[str]]:
    """Off-grid items, symbol body overlaps, wire interiors over pin ends (every sheet)."""
    off: list[str] = []
    overlaps: list[str] = []
    over_pin: list[str] = []
    for ref in sheet_refs(h):
        sc = read_scene(h, ref)
        syms = list(sc.symbols.values())
        off += [f"{s.ref}@{ref.path}" for s in syms if not on_grid(s.at)]
        off += [f"pin {p.ref}.{p.number}" for s in syms for p in s.pins if not on_grid(p.at)]
        off += [f"wire {w.uuid}" for w in sc.wires.values()
                if not (on_grid(w.start) and on_grid(w.end))]  # fmt: skip
        off += [f"label {lb.text}" for lb in sc.labels.values() if not on_grid(lb.at)]
        off += [f"junction {u}" for u, p in sc.junctions.items() if not on_grid(p)]
        off += [f"no-connect {u}" for u, p in sc.no_connects.items() if not on_grid(p)]
        parts = [s for s in syms if not s.power]
        overlaps += [f"{a.ref}/{b.ref}" for a, b in combinations(parts, 2)
                     if a.body.overlaps(b.body, -0.05)]  # fmt: skip
        over_pin += [f"{p.ref}.{p.number} on wire {w.uuid}"
                     for p in sc.all_pins() for w in sc.wires.values()
                     if in_segment_interior(p.at, w.start, w.end)]  # fmt: skip
    return {"off_grid": off, "overlaps": overlaps, "wire_over_pin": over_pin}


async def check_change_log_matches_diff(
    s: KicadSession, ctx: ToolContext, svc: HistoryService, snap: Snapshot
) -> dict[str, Any]:
    """Every item the change log says a tool added / deleted shows up in `kicad-cli sch
    diff` with that kind (modified items: modified, or no net difference), and every diff
    entry without an own log row is flagged a side effect."""
    assert ctx.editing is not None
    log = await ctx.editing.changes.since(s.project.id, snap.created_at)
    own = [e for e in log if not e.side_effect]
    assert own
    diff = await diff_against_snapshot(svc, s.project, snap, log)
    entries = [e for es in diff["sheets"].values() for e in es]
    kinds: dict[str, set[str]] = {}
    for e in entries:
        kinds.setdefault(e["uuid"], set()).add(e["kind"])
    ops: dict[str, list[str]] = {}
    for e in own:
        ops.setdefault(e.uuid, []).append(e.op)
    for uuid, seq in ops.items():
        got = kinds.get(uuid, set())
        if "add" in seq and "delete" in seq:
            assert not got, (uuid, seq, got)  # added and removed again in the run
        elif "add" in seq:
            assert "added" in got, (uuid, seq, got)
        elif "delete" in seq:
            assert "removed" in got, (uuid, seq, got)
        else:
            assert got <= {"modified"}, (uuid, seq, got)
    for e in entries:
        if e["uuid"] not in ops:
            assert e["side_effect"] is True, e
    return diff


async def assert_scenario(
    name: str, s: KicadSession, ctx: ToolContext, tmp_path: Path
) -> dict[str, Any]:
    live = await s.call(golden.live_components)
    res = golden.check(name, golden.nets_of(live), live)
    assert res["golden"], (res["missing"], live)

    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    cli = golden.cli_components_full(cli_netlist(s.settings.kicad_cli, dest, tmp_path))
    res_cli = golden.check(name, golden.nets_of(cli), cli)
    assert res_cli["golden"], (res_cli["missing"], cli)
    assert set(cli) == set(live)

    if out := os.environ.get(EXPORT_ENV):  # scripts/make-edited-fixtures.py
        export_fixture(name, dest, live, await s.call(refs_by_sheet_file), Path(out))

    erc, err = await call_tool(ctx, "run_erc")
    assert not err, erc
    errors = [v for v in erc["violations"] if v["severity"] == "error"]
    assert not errors, errors

    problems = await s.call(layout_problems)
    assert problems == {"off_grid": [], "overlaps": [], "wire_over_pin": []}, problems
    return live


# -- the three scenarios ---------------------------------------------------------------


async def test_s1_led_resistor(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    ctx = await blank_ctx(s, edit_db)
    svc, snap = await start_snapshot(s, edit_db)
    await run_script(ctx, S1)
    await assert_scenario("s1_led", s, ctx, tmp_path)
    await check_change_log_matches_diff(s, ctx, svc, snap)


async def test_s2_ldo_with_caps_0805(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    ctx = await blank_ctx(s, edit_db)
    svc, snap = await start_snapshot(s, edit_db)
    refs = await run_script(ctx, S2)
    live = await assert_scenario("s2_ldo", s, ctx, tmp_path)
    assert live[refs["CIN"]]["footprint"] == live[refs["COUT"]]["footprint"] == C0805
    await check_change_log_matches_diff(s, ctx, svc, snap)


async def test_s3_mcu_decoupling_i2c_sensor_on_subsheet(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    ctx = await blank_ctx(s, edit_db)
    svc, snap = await start_snapshot(s, edit_db)
    refs = await run_script(ctx, S3)
    await assert_scenario("s3_mcu_i2c_subsheet", s, ctx, tmp_path)

    def sensor_sheet(h: KicadHandle) -> set[str]:
        sub = next(r for r in sheet_refs(h) if r.name == "Sensor")
        return {x.ref for x in read_scene(h, sub).symbols.values() if not x.power}

    assert await s.call(sensor_sheet) == {refs["S"], refs["CS"]}  # the sensor block
    diff = await check_change_log_matches_diff(s, ctx, svc, snap)
    assert len(diff["sheets"]) == 2  # root + the new sub-sheet
