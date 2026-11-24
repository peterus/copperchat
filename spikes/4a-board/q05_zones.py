"""Q5: zones + DRC on OpenDriveHub (4 layers): GND planes on In1/In2, net-class widths, Freerouting
against filled planes (zones are exported as protected wiring), outer GND pours after routing,
RefillZones (blocking + task), stitching vias legalised by DRC, DRC via API task vs kicad-cli."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from kipy.board_types import Via, Zone
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode, Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer, ZoneType
from kipy.proto.common.types import project_settings_pb2
from kipy.project_types import NetClass, NetClassAssignments, NetClassPatternAssignment

import boards
import drc
import flow
import fanout
import freerouting
import metrics
import place
from harness import RESULTS, Recorder, Server, main_guard

MM = 1_000_000


def zone(net, layers, outline, inset=0.5, name="", priority=0) -> Zone:
    x0, y0, x1, y1 = outline
    z = Zone()
    z.type = ZoneType.ZT_COPPER
    z.layers = layers
    z.net = net
    z.name = name
    z.priority = priority
    ln = PolyLine()
    for x, y in ((x0 + inset, y0 + inset), (x1 - inset, y0 + inset), (x1 - inset, y1 - inset), (x0 + inset, y1 - inset)):
        ln.append(PolyLineNode.from_xy(round(x * MM), round(y * MM)))
    ln.closed = True
    poly = PolygonWithHoles()
    poly.outline = ln
    z.outline = poly
    return z


def power_classes(b, glob: set[str]) -> list[str]:
    """Generic: supply nets (global, not ground) -> class 'Power' 0.4 mm; ground handled by planes."""
    proj = b.get_project()
    proj.document.project.path = proj.document.project.path.rstrip("/") + "/"  # see q02 project_path_mismatch
    nc = project_settings_pb2.NetClass(name="Power", priority=1)
    nc.board.track_width.value_nm = round(0.4 * MM)
    nc.board.clearance.value_nm = round(0.2 * MM)
    proj.set_net_classes([NetClass(nc)])
    sup = sorted(n for n in glob if not place.is_ground(n))
    proj.set_net_class_assignments(NetClassAssignments(pattern_assignments=[
        NetClassPatternAssignment(project_settings_pb2.NetClassPatternAssignment(pattern=n, netclass="Power")) for n in sup]))
    return sup


def hybrid(r: Recorder, base: Path) -> None:
    """B: Freerouting ignores GND (net class), planes filled afterwards, GND SMD pads fanned out by
    fanout.py (DRC-legalised), outer pours last."""
    p, net = flow.prepare(base / "hybrid", "odh", None)
    with Server(p.pro) as srv:
        k = srv.client(600000)
        b, parts, info = flow.build(k, p, net, "odh")
        outline = info["outline"]
        # No net classes here: with 0.4 mm supply/GND tracks this dense placement leaves 6-9 connections
        # unrouted (part A and an earlier run). `--router.autorouter.ignore_net_classes=GND` did not stop
        # Freerouting from routing GND (229 GND segments imported), so: route everything, then delete the
        # GND copper through the API and connect GND to the planes by fanout.
        b.save()
        dsn = p.dir / "board.dsn"
        freerouting.export_dsn_cli(p.pcb, dsn)
        fr = freerouting.route(dsn, p.dir / "board.ses", passes=40)
        r.check("hybrid.freerouting", fr["rc"] == 0 and fr["ses"], fr)
        freerouting.import_ses(b, p.dir / "board.ses")
        gnd_cu = [t for t in list(b.get_tracks()) + list(b.get_vias()) if t.net.name == "GND"]
        b.remove_items(gnd_cu)
        r.note("hybrid.gnd_items_removed", len(gnd_cu))
        gnd = next(n for n in b.get_nets() if n.name == "GND")
        b.create_items([zone(gnd, [BoardLayer.BL_In1_Cu], outline, name="GND_In1"),
                        zone(gnd, [BoardLayer.BL_In2_Cu], outline, name="GND_In2")])
        b.refill_zones(block=True, max_poll_seconds=120)
        t0 = time.monotonic()
        fo = fanout.fanout(b, "GND")
        fo["seconds"] = round(time.monotonic() - t0, 1)
        r.check("hybrid.gnd_fanout", fo["failed"] == 0, fo)
        b.create_items([zone(gnd, [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu], outline, name="GND_outer")])
        b.refill_zones(block=True, max_poll_seconds=120)
        b.save()
        m = metrics.routing(b, parts)
        r.note("hybrid.metrics", m)
        viol, secs = drc.api_drc(b, zones_filled=True)
        ac = drc.api_counts(viol)
        r.note("hybrid.api_drc", {"seconds": round(secs, 2), **ac})
        r.check("hybrid.complete", ac.get("DRCET_UNCONNECTED_ITEMS", 0) == 0,
                f"unconnected={ac.get('DRCET_UNCONNECTED_ITEMS', 0)} dangling_vias={ac.get('DRCET_DANGLING_VIA', 0)}")
        for layer in ("F.Cu", "In1.Cu", "B.Cu"):
            boards.render(p.pcb, RESULTS / f"q05_hybrid_{layer.replace('.', '_')}.svg", layers=f"{layer},Edge.Cuts")
        boards.render(p.pcb, RESULTS / "q05_hybrid_signals.svg", layers="B.Cu,F.Cu,F.SilkS,Edge.Cuts")
        (RESULTS / "q05_hybrid.json").write_text(json.dumps({"freerouting": fr, "fanout": fo, "metrics": m,
                                                            "api_drc": ac}, indent=2, default=str))


def run() -> int:
    r = Recorder("q05_zones")
    base = Path(tempfile.mkdtemp(prefix="q05-"))
    hybrid(r, base)
    p, net = flow.prepare(base, "odh", None)
    with Server(p.pro) as srv:
        k = srv.client(600000)
        b, parts, info = flow.build(k, p, net, "odh")
        outline = info["outline"]
        nets = {n.name: n for n in b.get_nets()}
        gnd = nets["GND"]
        sup = power_classes(b, set(info["global_nets"]))
        r.note("power_class_nets", sup)

        # inner GND planes
        made = b.create_items([zone(gnd, [BoardLayer.BL_In1_Cu], outline, name="GND_In1"),
                               zone(gnd, [BoardLayer.BL_In2_Cu], outline, name="GND_In2")])
        t0 = time.monotonic()
        b.refill_zones(block=True, max_poll_seconds=120)
        r.note("refill_inner_s", round(time.monotonic() - t0, 2))
        zs = b.get_zones()
        filled = {z.name: sum(len(v) for v in z.filled_polygons.values()) for z in zs}
        r.check("inner_planes_filled", all(filled.get(n, 0) > 0 for n in ("GND_In1", "GND_In2")), filled)
        b.save()

        # route against the filled planes: GND pads reach the plane through vias
        dsn = p.dir / "board.dsn"
        freerouting.export_dsn_cli(p.pcb, dsn)
        txt = dsn.read_text()
        r.note("dsn_zone_export", f"protect wires={txt.count('(type protect)')} layers="
               f"{[ln.strip() for ln in txt.splitlines() if '(type ' in ln and 'signal' in ln or 'power' in ln][:6]}")
        fr = freerouting.route(dsn, p.dir / "board.ses", passes=40)
        r.check("freerouting_with_planes", fr["rc"] == 0 and fr["ses"], fr)
        res = freerouting.import_ses(b, p.dir / "board.ses")
        b.refill_zones(block=True, max_poll_seconds=120)

        # outer GND pours after routing (lower priority), refill as a task
        b.create_items([zone(gnd, [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu], outline, name="GND_outer")])
        t0 = time.monotonic()
        b.refill_zones(block=True, max_poll_seconds=120)
        r.note("refill_all_s", round(time.monotonic() - t0, 2))
        b.save()
        m = metrics.routing(b, parts)
        r.note("metrics_with_planes", m)

        # DRC: API task vs kicad-cli
        viol, secs = drc.api_drc(b, zones_filled=True)
        ac = drc.api_counts(viol)
        r.note("api_drc", {"seconds": round(secs, 2), **ac})
        d, csecs = drc.cli_drc(p.pcb, p.dir / "drc.json", parity=True)
        cc = drc.cli_counts(d)
        r.note("cli_drc", {"seconds": round(csecs, 2), **cc})
        # documents a weakness: Freerouting against pre-filled planes leaves connections open
        r.check("planes_in_dsn_leave_unrouted", cc.get("unconnected_items", 0) > 0,
                f"cli unconnected={cc.get('unconnected_items', 0)} api={ac.get('DRCET_UNCONNECTED_ITEMS', 0)}")
        r.check("drc_reports_courtyard_and_silk", any("silk" in t for t in cc) or any("SILK" in t for t in ac),
                {t: n for t, n in cc.items() if "silk" in t or "courtyard" in t})
        r.check("api_vs_cli_same_unconnected", ac.get("DRCET_UNCONNECTED_ITEMS", 0) == cc.get("unconnected_items", 0),
                (ac.get("DRCET_UNCONNECTED_ITEMS", 0), cc.get("unconnected_items", 0)))

        # stitching vias: grid over the board, keep only those DRC accepts (generic legaliser)
        x0, y0, x1, y1 = outline
        cand = []
        step = 5.0
        y = y0 + 2.5
        while y < y1 - 2:
            x = x0 + 2.5
            while x < x1 - 2:
                v = Via()
                v.position = Vector2.from_xy(round(x * MM), round(y * MM))
                v.net = gnd
                v.diameter, v.drill_diameter = round(0.6 * MM), round(0.3 * MM)
                cand.append(v)
                x += step
            y += step
        t0 = time.monotonic()
        made = b.create_items(cand)
        b.refill_zones(block=True, max_poll_seconds=120)
        viol, _ = drc.api_drc(b, zones_filled=True)
        ids = {m_.id.value for m_ in made}
        bad = {i.value for x in viol for i in x.items if i.value in ids}
        # a stitching via must sit in copper on both outer pours: drop dangling ones too
        b.remove_items([m_ for m_ in made if m_.id.value in bad])
        b.refill_zones(block=True, max_poll_seconds=120)
        viol2, _ = drc.api_drc(b, zones_filled=True)
        left = len(made) - len(bad)
        r.check("stitching_vias_legalised_by_drc", left > 0 and not any(i.value in ids for x in viol2 for i in x.items),
                f"{len(made)} candidates, {len(bad)} rejected by DRC, {left} kept, {time.monotonic() - t0:.1f}s; "
                f"after: {dict(drc.api_counts(viol2))}")
        b.save()
        for layer in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"):
            boards.render(p.pcb, RESULTS / f"q05_odh_{layer.replace('.', '_')}.svg", layers=f"{layer},Edge.Cuts")
        boards.render(p.pcb, RESULTS / "q05_odh_signals.svg", layers="B.Cu,F.Cu,F.SilkS,Edge.Cuts")
        (RESULTS / "q05_odh.json").write_text(json.dumps({"freerouting": fr, "metrics": m, "api_drc": ac,
                                                         "cli_drc": cc, "import": res.report[:500]}, indent=2, default=str))
    return r.finish()


if __name__ == "__main__":
    main_guard(run)
