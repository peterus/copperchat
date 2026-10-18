"""Replays the tool calls of a recorded run (no model) to reproduce a tool failure:
./run.sh python replay.py results/runs/s3-v2-r1 [--upto N] [--debug N]"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness import Server

import geo
import kit
import tools


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--upto", type=int, default=10**6)
    ap.add_argument("--debug", type=int, default=-1, help="index of a sch_add_power call to explain")
    a = ap.parse_args()
    r = json.loads((Path(a.run_dir) / "result.json").read_text())
    proj = kit.fresh_blank()
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        kit.load_libraries(k)
        ctx = tools.Ctx(
            k=k, sch=k.get_schematic(), lib=kit.Library(k), project_dir=proj, out_dir=Path("work/replay"), version=r.get("tools_version", 1)
        )
        for i, c in enumerate(r["calls"][: a.upto]):
            if c["tool"] == "render_view":
                continue
            if i == a.debug:
                explain(ctx, c)
            res = tools.call(ctx, c["tool"], c["args"])
            same = ("error" in res) == (not c["ok"])
            print(
                f"{i:3d} {c['tool']:18s} {'ok ' if 'error' not in res else 'ERR'} {'' if same else '(differs from run!) '}{json.dumps(res)[:160]}"
            )


def explain(ctx, c) -> None:
    scene = geo.read_scene(ctx.sch)
    for spec in c["args"]["at"] if isinstance(c["args"]["at"], list) else [c["args"]["at"]]:
        p = tools.resolve_pin(scene, spec)
        lib_id = c["args"].get("symbol") or f"power:{c['args']['net']}"
        body = tools._power_body_dir(ctx, lib_id)
        for pts, at, rot, _n in tools._power_plans(scene, p, body):
            probs = geo.path_problems(pts, scene, [p], {p.sym_uuid}) if geo.same(pts[0], p.pos) else tools._blocked(scene, pts, pts[0])
            probe = tools._probe(ctx, lib_id, at, rot, None)
            hits = [b for b in scene.text_boxes() if geo.boxes_overlap(probe.occupied, b, 0.2)]
            print("  plan", spec, pts, at, rot, "path:", probs, "at:", scene.items_at(at), "box hits:", hits)


if __name__ == "__main__":
    main()
