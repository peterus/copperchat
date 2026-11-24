"""Q4c: own octilinear A* (router.py) for short 2-pin signal nets on F.Cu, committed through the API and
checked by the DRC task; then Freerouting for the rest (pre-routed tracks are exported as fixed wiring).
Compared with pure Freerouting (q04)."""

from __future__ import annotations

import json
import math
import tempfile
import time
from pathlib import Path

from kipy.proto.board.board_types_pb2 import BoardLayer

import boards
import drc
import flow
import freerouting
import metrics
import place
import router
from harness import RESULTS, Recorder, Server, main_guard

WIDTH, CLEAR = 0.25, 0.2  # mm (a little above the JLC minimum, as the agent would choose)


def run() -> int:
    r = Recorder("q04c_astar")
    base = Path(tempfile.mkdtemp(prefix="q04c-"))
    p, net = flow.prepare(base, "odh", None)
    with Server(p.pro) as srv:
        k = srv.client(600000)
        b, parts, info = flow.build(k, p, net, "odh")
        glob = set(info["global_nets"])
        nets = {n.name: n for n in b.get_nets()}
        by_net: dict[str, list] = {}
        for q in b.get_pads():
            if q.net.name:
                by_net.setdefault(q.net.name, []).append(q)
        short = sorted(((n, ps) for n, ps in by_net.items() if len(ps) == 2 and n not in glob
                        and math.dist((ps[0].position.x, ps[0].position.y), (ps[1].position.x, ps[1].position.y)) < 10e6),
                       key=lambda t: math.dist((t[1][0].position.x, t[1][0].position.y), (t[1][1].position.x, t[1][1].position.y)))
        r.note("short_2pin_nets", len(short))
        routed, failed, rejected = [], [], []
        t0 = time.monotonic()
        tg = 0.0
        for name, ps in short:
            # smd pads on the bottom would need a via: skip (prototype is single-layer F.Cu)
            if any(not any(cl.layer == BoardLayer.BL_F_Cu for cl in q.proto.pad_stack.copper_layers)
                   and q.proto.pad_stack.drill.diameter.x_nm == 0 for q in ps):
                continue
            t1 = time.monotonic()
            g = router.build_grid(b, info["outline"], BoardLayer.BL_F_Cu, CLEAR, WIDTH, res=0.05)
            tg += time.monotonic() - t1
            a, c = [(q.position.x / place.MM, q.position.y / place.MM) for q in ps]
            path = router.astar(g, a, c, name)
            if not path:
                failed.append(name)
                continue
            path = router.fix_ends(router.simplify(path), a, c)
            made = b.create_items(router.to_tracks(path, nets[name], BoardLayer.BL_F_Cu, WIDTH))
            viol, _ = drc.api_drc(b)
            ids = {m.id.value for m in made}
            bad = [x for x in viol if any(i.value in ids for i in x.items)]
            if bad:
                b.remove_items(made)
                rejected.append((name, drc.board_rules_pb2.DrcErrorType.Name(bad[0].error_type)))
            else:
                routed.append((name, len(path) - 1))
        secs = time.monotonic() - t0
        r.note("astar", {"routed": len(routed), "failed_no_path": failed, "drc_rejected": rejected,
                         "seconds": round(secs, 1), "grid_build_s": round(tg, 1), "segments_per_net": routed})
        r.check("astar_routes_short_nets", len(routed) >= 0.6 * len(short),
                f"{len(routed)}/{len(short)} routed, {len(failed)} no path, {len(rejected)} rejected by DRC")
        m_a = metrics.routing(b, parts)
        r.check("astar_geometry_clean", m_a["non_45_segments"] == 0 and m_a["acute_bends"] == 0,
                {k_: m_a[k_] for k_ in ("segments", "non_45_segments", "non_45_examples", "bend_histogram", "acute_bends")})
        b.save()
        boards.render(p.pcb, RESULTS / "q04c_odh_astar_top.svg", layers="F.Cu,F.Fab,Edge.Cuts")

        # the rest with Freerouting (pre-routed tracks stay: exported as wiring, imported back unchanged?)
        n_before = len(b.get_tracks())
        dsn = p.dir / "board.dsn"
        freerouting.export_dsn_cli(p.pcb, dsn)
        fr = freerouting.route(dsn, p.dir / "board.ses", passes=40)
        r.check("freerouting_after_astar", fr["rc"] == 0 and fr["ses"], fr)
        res = freerouting.import_ses(b, p.dir / "board.ses")
        b.save()
        m = metrics.routing(b, parts)
        d, _ = drc.cli_drc(p.pcb, p.dir / "drc.json")
        cc = drc.cli_counts(d)
        r.note("hybrid_metrics", m)
        r.note("hybrid_drc", dict(cc))
        r.check("hybrid_complete", cc.get("unconnected_items", 0) == 0, f"unconnected={cc.get('unconnected_items', 0)}; "
                f"tracks before import={n_before}, after={len(b.get_tracks())}; skipped={res.skipped_count}")
        boards.render(p.pcb, RESULTS / "q04c_odh_hybrid.svg", layers="B.Cu,In2.Cu,In1.Cu,F.Cu,F.SilkS,Edge.Cuts")
        (RESULTS / "q04c_odh.json").write_text(json.dumps({"astar": routed, "astar_metrics": m_a, "freerouting": fr,
                                                           "hybrid_metrics": m, "drc": cc}, indent=2, default=str))
    return r.finish()


if __name__ == "__main__":
    main_guard(run)
