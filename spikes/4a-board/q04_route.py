"""Q4: routing. (a) tracks/vias/arcs created directly through the API, DRC feedback per item, interactive
router reachability; (b) Freerouting round trip (DSN export -> Freerouting headless -> ImportSpecctraSession)
on ecc83 and OpenDriveHub, judged by metrics, DRC and renders. (c) lives in q04c_astar.py."""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import time
from pathlib import Path

from kipy.board_types import ArcTrack, Track, Via
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer

import boards
import drc
import flow
import freerouting
import metrics
import place
from harness import RESULTS, Recorder, Server, main_guard

MM = 1_000_000


def v(x: float, y: float) -> Vector2:
    return Vector2.from_xy(round(x * MM), round(y * MM))


def octilinear(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]]:
    """45-degree dogleg: diagonal first, then straight."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    d = min(abs(dx), abs(dy))
    mid = (a[0] + math.copysign(d, dx), a[1] + math.copysign(d, dy))
    return [a, mid, b] if 0 < d and (abs(dx) != abs(dy)) else [a, b]


def tracks_for(path, net, layer, width=0.3):
    out = []
    for (ax, ay), (bx, by) in zip(path, path[1:]):
        t = Track()
        t.start, t.end, t.layer, t.width = v(ax, ay), v(bx, by), layer, round(width * MM)
        if net is not None:
            t.net = net
        out.append(t)
    return out


def part_a(r: Recorder, base: Path) -> None:
    p, net = flow.prepare(base / "a", "ecc83", None)
    with Server(p.pro) as srv:
        k = srv.client(300000)
        b, parts, info = flow.build(k, p, net, "ecc83")
        try:
            k.run_action("pcbnew.InteractiveRouter.SingleTrack")
            r.check("interactive_router_headless", False, "run_action accepted")
        except Exception as e:  # noqa: BLE001
            r.check("interactive_router_headless", "headless" in str(e) or "no handler" in str(e), e)

        nets = {n.name: n for n in b.get_nets()}
        pads = b.get_pads()
        by_net: dict[str, list] = {}
        for q in pads:
            if q.net.name:
                by_net.setdefault(q.net.name, []).append(q)
        two = sorted((n for n, ps in by_net.items() if len(ps) == 2 and n != "GND"),
                     key=lambda n: math.dist(*[(q.position.x, q.position.y) for q in by_net[n]]))
        viol0, secs0 = drc.api_drc(b)
        c0 = drc.api_counts(viol0)
        r.note("api_drc_placed", {"seconds": round(secs0, 2), **c0})

        n1 = two[0]
        a_, b_ = [(q.position.x / MM, q.position.y / MM) for q in by_net[n1]]
        path = octilinear(a_, b_)
        t0 = time.monotonic()
        made = b.create_items(tracks_for(path, nets[n1], BoardLayer.BL_B_Cu))
        r.note("create_tracks_s", round(time.monotonic() - t0, 3))
        conn = b.get_connected_items(by_net[n1][0])
        r.check("track_connects_pads", any(getattr(i, "id", None) and i.id.value == by_net[n1][1].id.value for i in conn),
                f"net {n1}: {len(made)} segments {[(round(x, 2), round(y, 2)) for x, y in path]}; connected items={len(conn)}")
        viol1, secs1 = drc.api_drc(b)
        c1 = drc.api_counts(viol1)
        r.note("api_drc_after_track", dict(c1))
        viol1b, _ = drc.api_drc(b)
        r.note("api_drc_after_track_again", dict(drc.api_counts(viol1b)))
        r.check("drc_unconnected_drops", c1.get("DRCET_UNCONNECTED_ITEMS", 0) == c0.get("DRCET_UNCONNECTED_ITEMS", 0) - 1,
                f"unconnected {c0.get('DRCET_UNCONNECTED_ITEMS', 0)} -> {c1.get('DRCET_UNCONNECTED_ITEMS', 0)}; {secs1:.2f}s")

        # deliberate violation: a second net's track straight across the first -> marker names our item
        n2 = next(n for n in two[1:] if n != n1)
        c_ = [(q.position.x / MM, q.position.y / MM) for q in by_net[n1]]
        mx, my = (path[0][0] + path[1][0]) / 2, (path[0][1] + path[1][1]) / 2  # middle of our 1st segment
        bad = tracks_for([(mx - 2, my), (mx + 2, my)], nets[n2], BoardLayer.BL_B_Cu)
        bad = b.create_items(bad)
        viol2, _ = drc.api_drc(b)
        mine = [x for x in viol2 if any(i.value == bad[0].id.value for i in x.items)]
        r.check("drc_marker_names_item", any("SHORT" in drc.board_rules_pb2.DrcErrorType.Name(x.error_type)
                                                 or "CLEARANCE" in drc.board_rules_pb2.DrcErrorType.Name(x.error_type) for x in mine),
                [(drc.board_rules_pb2.DrcErrorType.Name(x.error_type), x.description[:80], len(x.items)) for x in mine][:3])
        b.remove_items(bad)

        # via + arc + a track without net
        via = Via()
        via.position = v(mx + 4, my)
        via.net = nets[n1]
        via.diameter, via.drill_diameter = round(0.6 * MM), round(0.3 * MM)
        arc = ArcTrack()
        arc.start, arc.mid, arc.end = v(mx + 4, my), v(mx + 5.5, my + 1.5), v(mx + 4, my + 3)
        arc.layer, arc.width, arc.net = BoardLayer.BL_F_Cu, round(0.3 * MM), nets[n1]
        nonet = tracks_for([(a_[0], a_[1]), (a_[0], a_[1] - 2)], None, BoardLayer.BL_F_Cu)[0]
        made = b.create_items([via, arc, nonet])
        back = b.get_items_by_id([m.id for m in made])
        kinds = [type(x).__name__ for x in back]
        nonet_back = next(x for x in back if isinstance(x, Track))
        r.check("via_arc_created", kinds.count("Via") == 1 and kinds.count("ArcTrack") == 1, kinds)
        r.check("netless_track_on_pad_gets_pad_net", nonet_back.net.name == n1,
                f"net after create={nonet_back.net.name!r} (created without a net, starting on a {n1} pad)")
        viol3, _ = drc.api_drc(b)
        r.note("drc_after_netless_track", dict(drc.api_counts(viol3)))
        b.remove_items(back)
        b.save()


def part_b(r: Recorder, base: Path) -> None:
    for name in ("ecc83", "odh"):
        p, net = flow.prepare(base / f"b-{name}", name, None)
        with Server(p.pro) as srv:
            k = srv.client(600000)
            b, parts, info = flow.build(k, p, net, name)
            dsn = p.dir / "board.dsn"
            r.note(f"{name}.dsn_cli_s", round(freerouting.export_dsn_cli(p.pcb, dsn), 2))
            # API job variant: does it see the live board? move one part, do not save, compare
            try:
                f = b.get_footprints()[0]
                f.position = Vector2.from_xy(f.position.x + 1_000_000, f.position.y)
                b.update_items(f)
                job = freerouting.export_dsn_api(b, p.dir / "api.dsn")
                live = (p.dir / "api.dsn").exists() and (p.dir / "api.dsn").read_text() != dsn.read_text()
                r.note(f"{name}.dsn_api_job", f"status={job.status} live_board={live}")
                f.position = Vector2.from_xy(f.position.x - 1_000_000, f.position.y)
                b.update_items(f)
            except Exception as e:  # noqa: BLE001
                r.note(f"{name}.dsn_api_job", repr(e))
            ses = p.dir / "board.ses"
            fr = freerouting.route(dsn, ses, passes=40 if name == "odh" else 20)
            r.check(f"{name}.freerouting_ran", fr["rc"] == 0 and fr["ses"], fr)
            if not fr["ses"]:
                continue
            t0 = time.monotonic()
            res = freerouting.import_ses(b, ses)
            r.check(f"{name}.import_ses", True, f"{time.monotonic() - t0:.2f}s skipped={res.skipped_count} "
                    f"report={res.report[:200]!r}")
            b.save()
            m = metrics.routing(b, parts)
            r.note(f"{name}.metrics", m)
            d, secs = drc.cli_drc(p.pcb, p.dir / "drc.json")
            counts = drc.cli_counts(d)
            r.note(f"{name}.drc", {"seconds": round(secs, 1), **counts})
            r.check(f"{name}.routed_complete", counts.get("unconnected_items", 0) == 0,
                    f"unconnected={counts.get('unconnected_items', 0)}")
            r.check(f"{name}.drc_copper_clean", not any(counts.get(t) for t in ("clearance", "shorting_items", "tracks_crossing")),
                    {t: counts.get(t, 0) for t in ("clearance", "shorting_items", "tracks_crossing", "hole_clearance")})
            cu = "B.Cu,In2.Cu,In1.Cu,F.Cu" if name == "odh" else "B.Cu,F.Cu"
            boards.render(p.pcb, RESULTS / f"q04_{name}_freerouting.svg", layers=f"{cu},F.SilkS,Edge.Cuts")
            boards.render(p.pcb, RESULTS / f"q04_{name}_freerouting_top.svg", layers="F.Cu,Edge.Cuts")
            boards.render(p.pcb, RESULTS / f"q04_{name}_freerouting_bottom.svg", layers="B.Cu,Edge.Cuts")
            shutil.copyfile(p.pcb, base / f"{name}-freerouted.kicad_pcb")
            (RESULTS / f"q04_{name}_freerouting.json").write_text(json.dumps({"freerouting": fr, "metrics": m,
                                                                               "drc": counts}, indent=2, default=str))


def run() -> int:
    r = Recorder("q04_route")
    base = Path(tempfile.mkdtemp(prefix="q04-"))
    part_a(r, base)
    part_b(r, base)
    _ = place
    return r.finish()


if __name__ == "__main__":
    main_guard(run)
