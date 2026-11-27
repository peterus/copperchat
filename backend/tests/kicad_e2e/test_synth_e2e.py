"""The whole board flow on `sp4_synth` with scripted approvals (SP4 P9a, spec §18.2, §19):
sync to fab package through the real tools, every hard gate passing, pinned as a metrics
golden. Run with `-m kicad_e2e` (own CI job; the kicad shards deselect it)."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import pytest

from copperchat.board import metrics as board_metrics
from copperchat.board.model import BoardModel, placement_hash
from copperchat.history.restore import restore_snapshot
from copperchat.kicad_tools.board.read import board_model
from tests.kicad_e2e.conftest import SynthFlow
from tests.kicad_e2e.flow import call
from tests.support.conversations import TEST_CID

pytestmark = [
    pytest.mark.kicad,
    pytest.mark.kicad_e2e,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.timeout(420),
]

GOLDEN = Path(__file__).parent / "goldens" / "sp4_synth_metrics.json"
FLOW_BUDGET_S = 180.0  # spec §18.2: the whole flow in at most 3 minutes
DECAP_MAX_MM = 5.0  # M8 hard threshold (owner D2)
REL_TOL, ABS_TOL = 0.10, 0.5  # review values: ±10 % or ±0.5, whichever is larger


def _recorded_only() -> frozenset[str]:
    """Round 2 (R1): M15/M16 behind a switch that is still off - their values (Y-junction,
    dogleg and pad-loop counts) move with the fill's run-to-run variance and are recorded
    only (verdict exact); R4 switches them on and pins them."""
    off = {"M15": board_metrics.M15_HARD, "M16": board_metrics.M16_HARD}
    return frozenset(mid for mid, on in off.items() if not on)


FAB_FILES = (
    "bom-jlcpcb.csv",
    "cpl-jlcpcb.csv",
    "fab-report.md",
    "gerbers/sp4_synth-F_Cu.gtl",
    "gerbers/sp4_synth-B_Cu.gbl",
    "gerbers/sp4_synth-Edge_Cuts.gm1",
    "drill/sp4_synth-PTH.drl",
    "sp4_synth-jlcpcb.zip",
)


def _metrics(flow: SynthFlow) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in flow.result.metrics["metrics"]}


async def _model(flow: SynthFlow, run: str) -> BoardModel:
    return await board_model(flow.rig.ctx(run))


async def _ok(flow: SynthFlow, run: str, name: str, /, **args: Any) -> dict[str, Any]:
    data, err = await call(flow.rig, run, name, **args)
    assert not err, json.dumps(data, indent=1)
    return data


async def test_full_flow_hard_gates(synth_flow: SynthFlow) -> None:
    """The final `pcb_check full` has no hard failure (M2 = 0 among them), the package was
    exported, all in ≤ 3 min."""
    res = synth_flow.result
    m = _metrics(synth_flow)
    failing = {k: (v["value"], v["offenders"][:5]) for k, v in m.items() if v["verdict"] == "fail"}
    assert res.metrics["hard_failures"] == 0, failing
    assert m["M1"]["verdict"] == "pass"
    assert m["M2"]["value"] == 0
    assert m["M3"]["verdict"] != "fail" and res.metrics["errors"] == []
    assert not res.metrics.get("parity"), res.metrics.get("parity")
    assert m["M4"]["value"] == 0
    assert m["M5"]["verdict"] != "fail"
    assert m["M7"]["value"] == 0
    assert m["M8"]["value"] is None or m["M8"]["value"] <= DECAP_MAX_MM
    for mid in ("M12", "M13", "M14"):
        assert m[mid]["verdict"] != "fail", m[mid]
    for path in FAB_FILES:
        assert path in res.fab_files, res.fab_files
    assert res.seconds <= FLOW_BUDGET_S, [(s.tool, s.seconds) for s in res.steps]


def _track_length(model: BoardModel) -> float:
    return sum(math.dist(t.start, t.end) for t in model.tracks)


def _golden_now(flow: SynthFlow, model: BoardModel) -> dict[str, Any]:
    metrics = {k: {"value": v["value"], "verdict": v["verdict"]} for k, v in _metrics(flow).items()}
    return {
        "metrics": dict(sorted(metrics.items())),
        "tracks": len(model.tracks) + len(model.arcs),
        "vias": len(model.vias),
        "track_length_mm": round(_track_length(model), 1),
        "fill_seconds": flow.result.fill.get("seconds"),
    }


def _close(got: Any, want: Any) -> bool:
    if isinstance(want, bool) or not isinstance(want, int | float):
        return bool(got == want)
    if not isinstance(got, int | float):
        return False
    return abs(got - want) <= max(REL_TOL * abs(want), ABS_TOL)


async def test_metrics_golden(synth_flow: SynthFlow) -> None:
    """`goldens/sp4_synth_metrics.json`: hard metrics and every verdict exact, review values
    and counts within ±10 % (or ±0.5); `fill_seconds` (and M15/M16 while switched off,
    `_recorded_only`) recorded only. `UPDATE_GOLDENS=1`
    rewrites it (explain every change in the PR)."""
    now = _golden_now(synth_flow, await _model(synth_flow, "run-golden"))
    if os.environ.get("UPDATE_GOLDENS") == "1":
        GOLDEN.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n")
    want = json.loads(GOLDEN.read_text())
    hard = {m["id"] for m in synth_flow.result.metrics["metrics"] if m["hard"]}
    diffs = []
    for mid, w in want["metrics"].items():
        g = now["metrics"].get(mid)
        exact = g is not None and g["verdict"] == w["verdict"]
        if mid in hard:
            exact = exact and g is not None and g["value"] == w["value"]
        recorded = mid in _recorded_only()
        if not exact or g is None or (not recorded and not _close(g["value"], w["value"])):
            diffs.append((mid, g, w))
    for key in ("tracks", "vias", "track_length_mm"):
        if not _close(now[key], want[key]):
            diffs.append((key, now[key], want[key]))
    assert set(now["metrics"]) == set(want["metrics"])
    assert not diffs, diffs


def _routed_passive(model: BoardModel) -> tuple[str, float]:
    """A two-pad part with a track ending in one of its pads (it carries copper)."""
    ends = {(t.layer, t.start) for t in model.tracks} | {(t.layer, t.end) for t in model.tracks}
    for part in sorted(model.parts, key=lambda p: p.ref):
        if len(part.pads) != 2 or part.ref[0] not in "RC":
            continue
        for pad in part.pads:
            for layer, (x, y) in ends:
                if (
                    layer in pad.layers
                    and abs(x - pad.x) <= pad.w / 2
                    and abs(y - pad.y) <= pad.h / 2
                ):
                    return part.ref, part.x
    raise AssertionError("no routed passive")


async def test_approvals_bound_to_hashes(synth_flow: SynthFlow) -> None:
    """Both checkpoints approved and current after the flow; moving a routed passive
    (`unroute: true`) makes both stale and the export refuses (Review Focus 2)."""
    status = await _ok(synth_flow, "run-appr-1", "pcb_status")
    cps = status["checkpoints"]
    for stage in ("placement", "board"):
        assert cps[stage]["approved"] and not cps[stage]["stale"], cps
        assert cps[stage]["hash"] == synth_flow.result.hashes[stage]
    ref, x = _routed_passive(await _model(synth_flow, "run-appr-1"))
    await _ok(
        synth_flow, "run-appr-2", "pcb_move", items=[{"ref": ref, "x": x + 0.5}], unroute=True
    )
    cps = (await _ok(synth_flow, "run-appr-3", "pcb_status"))["checkpoints"]
    assert cps["placement"]["stale"] and cps["board"]["stale"], cps
    data, err = await call(synth_flow.rig, "run-appr-3", "pcb_fab_export", preset="jlcpcb")
    assert err and data["error"]["code"] == "not_approved", data


async def test_undo_to_placement(synth_flow: SynthFlow) -> None:
    """History restore of the snapshot taken before turn 4's first mutation (routing):
    the approved placement is back, no copper, the session answers (spec §19 item 7)."""
    rig = synth_flow.rig
    snap = synth_flow.result.snapshots.get(4)
    assert snap is not None, synth_flow.result.snapshots
    await restore_snapshot(rig.history, rig.project, snap, conversation_id=TEST_CID)
    model = await _model(synth_flow, "run-undo-1")
    assert placement_hash(model) == synth_flow.result.hashes["placement"]
    assert model.tracks == () and model.arcs == () and model.vias == ()
    status = await _ok(synth_flow, "run-undo-2", "pcb_status")
    assert status["placement_hash"] == synth_flow.result.hashes["placement"]
