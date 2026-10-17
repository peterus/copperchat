"""Q3: wires, labels, junctions, no-connects created via the API and whether they connect:
pin ends, T-joints with/without junction, wire splitting, wire over a foreign pin end,
crossing wires, label on a wire, off-grid near miss; netlist freshness headless
(GetSchematicNetlist, netlist export job without save, kicad-cli after save)."""

import hashlib
import json
import tempfile
import time
from pathlib import Path

from geom import pin_ends_abs
from harness import (
    Recorder,
    Server,
    by_id,
    cli_netlist,
    commit,
    fresh_fixture,
    job_netlist,
    junction,
    keep_artifact,
    load_libraries,
    local_label,
    main_guard,
    mm,
    nets_by_pin,
    no_connect,
    require_kicad,
    root_sch,
    run_cli,
    sid,
    v2,
    wire,
)


def png_hash(sch, out_dir: Path) -> str:
    from kipy.proto.schematic.schematic_jobs_pb2 import SJSM_SINGLE_SHEET

    out_dir.mkdir(parents=True, exist_ok=True)
    sch.for_sheet(list(sch.get_hierarchy())[0]).export_png(str(out_dir / "p.png"), dpi=100, sheet_mode=SJSM_SINGLE_SHEET)
    return hashlib.sha256(next(out_dir.rglob("*.png")).read_bytes()).hexdigest()


def main() -> int:
    require_kicad()
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O
    from kipy.proto.common.types import DocumentType
    from kipy.schematic_types import GlobalLabel

    rec = Recorder("q03_connect")
    proj = fresh_fixture("blank")
    tmp = Path(tempfile.mkdtemp(prefix="s2a-q03-"))
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        load_libraries(k)

        def place(ref, at, rot=O.SSO_0, lib="Device:R"):
            s = sch.place_symbol_from_library(lib, v2(*at), orientation=rot, reference=ref)
            return s, pin_ends_abs(s)

        r1, p1 = place("R1", (50.8, 50.8))
        r2, p2 = place("R2", (76.2, 50.8))
        r3, p3 = place("R3", (63.5, 76.2))  # T-joint without junction
        r11, p11 = place("R11", (50.8, 101.6))
        r12, p12 = place("R12", (76.2, 101.6))
        r13, p13 = place("R13", (63.5, 127.0))  # T-joint with junction
        r5, p5 = place("R5", (127.0, 50.8))  # wire passes over its pin end
        r6, p6 = place("R6", (152.4, 50.8))  # label on a wire interior
        r7, p7 = place("R7", (177.8, 50.8))  # no-connect + near miss
        r8, p8 = place("R8", (127.0, 101.6))  # global label at pin end
        r9, p9 = place("R9", (152.4, 101.6))  # power symbol at pin end
        rec.note("pin_ends_R1", p1)

        y = 63.5
        route_a = [
            wire(p1["2"], (p1["2"][0], y)),
            wire((p1["2"][0], y), (p2["2"][0], y)),
            wire((p2["2"][0], y), p2["2"]),
        ]
        t_wire = wire(p3["1"], (p3["1"][0], y))  # ends on the interior of route_a[1]
        y2 = 114.3
        route_b = [
            wire(p11["2"], (p11["2"][0], y2)),
            wire((p11["2"][0], y2), (p12["2"][0], y2)),
            wire((p12["2"][0], y2), p12["2"]),
        ]
        t_wire_b = wire(p13["1"], (p13["1"][0], y2))
        j_b = junction((p13["1"][0], y2))
        over = wire((p5["2"][0] - 6.35, p5["2"][1]), (p5["2"][0] + 6.35, p5["2"][1]))
        over_lbl = local_label("OVER", (p5["2"][0] - 6.35, p5["2"][1]))
        cross_h = wire((120.65, 152.4), (133.35, 152.4))
        cross_v = wire((127.0, 146.05), (127.0, 158.75))
        cross_l = [local_label("XH", (120.65, 152.4)), local_label("XV", (127.0, 146.05))]
        w6 = wire(p6["2"], (p6["2"][0], p6["2"][1] + 10.16))
        lbl6 = local_label("MIDWIRE", (p6["2"][0], p6["2"][1] + 5.08))
        nc7 = no_connect(p7["1"])
        miss = wire((p7["2"][0] + 0.05, p7["2"][1]), (p7["2"][0] + 7.62, p7["2"][1]))
        miss_l = local_label("MISS", (p7["2"][0] + 7.62, p7["2"][1]))
        gl = GlobalLabel()
        gl.text.value = "VBUS_G"
        gl.position = v2(*p8["1"])
        items = [*route_a, t_wire, *route_b, t_wire_b, j_b, over, over_lbl, cross_h, cross_v, *cross_l, w6, lbl6, nc7, miss, miss_l, gl]
        with commit(sch, "wires"):
            created = sch.create_items(items)
        gnd = sch.place_symbol_from_library("power:GND", v2(*p9["2"]))
        rec.check("create_all_items", len(created) == len(items), f"{len(created)}/{len(items)}")
        ids = [sid(c) for c in created]
        back = by_id(sch, ids)
        rec.check("created_read_back_by_id", len(back) == len(ids), f"{len(back)}/{len(ids)}")
        jb = back[ids[items.index(j_b)]]
        rec.check("junction_read_back", mm(jb.position) == (p13["1"][0], y2), (mm(jb.position), jb.diameter))
        ncb = back[ids[items.index(nc7)]]
        rec.check("no_connect_read_back", mm(ncb.position) == p7["1"], mm(ncb.position))

        lines = sch.get_lines()
        rec.check("wires_not_split_or_merged_on_push", len(lines) == sum(1 for i in items if type(i).__name__ == "SchematicLine"), f"{len(lines)} wires on sheet")
        mid = by_id(sch, [ids[1], ids[items.index(route_b[1])]])
        rec.note("t_joint_host_wire_after", {i: (mm(w.start), mm(w.end)) for i, w in mid.items()})

        nets_api = nets_by_pin(sch)
        # export jobs run on EESCHEMA_JOBS_HANDLER's cached copy loaded from disk at the
        # first job (eeschema_jobs_handler.cpp getSchematic, m_cliSchematic)
        nets_job_unsaved = job_netlist(sch, tmp)
        rec.check("netlist_job_reflects_unsaved_edits", ("R1", "2") in nets_job_unsaved, f"{len(nets_job_unsaved)} pins in job netlist before save()")
        png0 = png_hash(sch, tmp / "png0")
        sch.save()
        nets_job_saved = job_netlist(sch, tmp)
        rec.check("netlist_job_reflects_edits_after_save", ("R1", "2") in nets_job_saved, f"{len(nets_job_saved)} pins after save() (job cache from first job)")
        rec.check("png_job_reflects_edits_after_save", png_hash(sch, tmp / "png1") != png0, "PNG export job after save() vs before")
        # workaround: save + close_document + open_document clears the job cache
        t0 = time.monotonic()
        doc = k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)[0]
        k.close_document(doc)
        k.open_document(str(root_sch(proj)), DocumentType.DOCTYPE_SCHEMATIC)
        sch = k.get_schematic()
        rec.note("close_open_document_ms", round((time.monotonic() - t0) * 1000, 1))
        nets_job = job_netlist(sch, tmp)
        rec.check("workaround_save_close_open_refreshes_jobs", ("R1", "2") in nets_job and png_hash(sch, tmp / "png2") != png0, f"{len(nets_job)} pins")
        rec.attempt("place_after_reopen_libraries_still_loaded", lambda: sch.place_symbol_from_library("Device:R", v2(228.6, 177.8), reference="R99").reference)
        nets_cli = cli_netlist(root_sch(proj), tmp)

        def same(n, a, b):
            return n.get(a) is not None and n.get(a) == n.get(b)

        for label, n in (("api", nets_api), ("job_after_reopen", nets_job), ("cli_saved", nets_cli)):
            rec.check(f"{label}_wire_pin_to_pin", same(n, ("R1", "2"), ("R2", "2")), (n.get(("R1", "2")), n.get(("R2", "2"))))
            rec.check(f"{label}_t_joint_without_junction_connects", same(n, ("R3", "1"), ("R1", "2")), (n.get(("R3", "1")), n.get(("R1", "2"))))
            rec.check(f"{label}_t_joint_with_junction_connects", same(n, ("R13", "1"), ("R11", "2")), (n.get(("R13", "1")), n.get(("R11", "2"))))
            rec.check(f"{label}_wire_over_foreign_pin_end_connects", (n.get(("R5", "2")) or "").endswith("OVER"), n.get(("R5", "2")))
            rec.check(f"{label}_label_on_wire_interior_names_net", (n.get(("R6", "2")) or "").endswith("MIDWIRE"), n.get(("R6", "2")))
            rec.check(f"{label}_near_miss_not_connected", not (n.get(("R7", "2")) or "").endswith("MISS"), n.get(("R7", "2")))
            rec.check(f"{label}_global_label_at_pin_end", n.get(("R8", "1")) == "VBUS_G", n.get(("R8", "1")))
            rec.check(f"{label}_power_symbol_at_pin_end", n.get(("R9", "2")) == "GND", n.get(("R9", "2")))
        nets_api = {k: v for k, v in nets_api.items() if not k[0].startswith("#")}  # power pins
        rec.check("three_netlists_agree", nets_api == nets_cli and nets_job == nets_cli, {
            "api_vs_cli": sorted(k for k in set(nets_api) | set(nets_cli) if nets_api.get(k) != nets_cli.get(k))[:8],
            "job_vs_cli": sorted(k for k in set(nets_job) | set(nets_cli) if nets_job.get(k) != nets_cli.get(k))[:8],
        })
        api_net_names = {net.name for net in sch.get_netlist()}
        rec.check("crossing_wires_without_junction_stay_separate", any(nm.endswith("XH") for nm in api_net_names) and any(nm.endswith("XV") for nm in api_net_names), sorted(n for n in api_net_names if n.endswith(("XH", "XV"))))
        rec.note("gnd_ref", gnd.reference)

        erc = tmp / "erc.json"
        r = run_cli("sch", "erc", "--format", "json", "--units", "mm", "--severity-all", "-o", str(erc), str(root_sch(proj)))
        rep = json.loads(erc.read_text()) if erc.exists() else {}
        vios = [v for s in rep.get("sheets", []) for v in s.get("violations", [])]
        types = sorted({v["type"] for v in vios})
        rec.check("erc_runs_on_saved_file", r.returncode in (0, 5) and erc.exists(), f"rc={r.returncode} types={types}")
        nc_pin_flagged = any(v["type"] == "pin_not_connected" and any(abs(i["pos"]["x"] - p7["1"][0]) < 0.01 and abs(i["pos"]["y"] - p7["1"][1]) < 0.01 for i in v.get("items", [])) for v in vios)
        rec.check("erc_no_connect_marker_suppresses_pin_not_connected", not nc_pin_flagged, types)
        rec.check("erc_flags_near_miss", any(v["type"] in ("pin_not_connected", "unconnected_wire_endpoint", "wire_dangling") for v in vios), types)
        rec.note("erc_violation_types", {t: sum(1 for v in vios if v["type"] == t) for t in types})
        keep_artifact(proj, "q03_connect")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
