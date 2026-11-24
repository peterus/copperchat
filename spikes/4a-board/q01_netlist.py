"""Q1: schematic -> board. Netlist export (kicad-cli and schematic API job), ImportNetlist into an
empty board, project-local library resolution, sheet info per footprint, re-sync after a schematic
change (placement kept), removal of a deleted symbol."""

from __future__ import annotations

import collections
import tempfile
import time
from pathlib import Path

from kipy.geometry import Vector2
from kipy.proto.common.types import DocumentType

import boards
from harness import Recorder, Server, main_guard

MM = 1_000_000


def open_both(k, p: boards.Proj):
    k.open_document(str(p.sch), DocumentType.DOCTYPE_SCHEMATIC)
    k.open_document(str(p.pcb), DocumentType.DOCTYPE_PCB)
    return k.get_schematic(), k.get_board()


def run() -> int:
    r = Recorder("q01_netlist")
    base = Path(tempfile.mkdtemp(prefix="q01-"))

    # --- small board ------------------------------------------------------------------------
    p = boards.ecc83(base)
    net, secs = p.netlist()
    r.note("ecc83.cli_netlist_s", round(secs, 2))
    with Server(p.pro) as srv:
        k = srv.client(120000)
        try:
            k.get_board()
            r.check("board_needs_open_document", False, "get_board worked without OpenDocument")
        except Exception as e:  # noqa: BLE001
            r.check("board_needs_open_document", "no handler" in str(e), e)
        sch, b = open_both(k, p)
        dry = b.import_netlist(str(net), dry_run=True)
        r.check("dry_run_no_change", dry.new_footprint_count == 15 and len(b.get_footprints()) == 0,
                f"dry new={dry.new_footprint_count} fps={len(b.get_footprints())}")
        t0 = time.monotonic()
        res = b.import_netlist(str(net))
        r.note("ecc83.import_s", round(time.monotonic() - t0, 2))
        fps = b.get_footprints()
        r.check("ecc83_import", res.error_count == 0 and len(fps) == 15,
                f"err={res.error_count} warn={res.warning_count} new={res.new_footprint_count} fps={len(fps)}")
        pos = collections.Counter((f.position.x, f.position.y) for f in fps)
        r.note("ecc83.positions_after_import", {f"{x / MM:.2f},{y / MM:.2f}": n for (x, y), n in pos.items()})
        r.note("ecc83.nets", len(b.get_nets()))
        f0 = fps[0]
        r.check("sheet_info_per_footprint", bool(f0.proto.symbol_sheet_name) and len(f0.proto.symbol_path.path) > 0,
                f"sheet_name={f0.proto.symbol_sheet_name!r} file={f0.proto.symbol_sheet_filename!r} "
                f"path={[x.value for x in f0.proto.symbol_path.path]} filters={f0.proto.symbol_footprint_filters!r}")
        pads = b.get_pads()
        r.check("pads_have_nets", sum(1 for q in pads if q.net.name) > 0.8 * len(pads),
                f"{sum(1 for q in pads if q.net.name)}/{len(pads)} pads with a net")

        # re-sync: place every footprint on a grid, change a value in the schematic (API, unsaved),
        # export the netlist through the schematic API job, re-import -> placement kept
        moved = {}
        for i, f in enumerate(fps):
            f.position = Vector2.from_xy((20 + (i % 5) * 15) * MM, (20 + (i // 5) * 15) * MM)
            moved[f.reference_field.text.value] = (f.position.x, f.position.y)
        b.update_items(fps)
        syms = sch.get_symbols()
        r1 = next(s for s in syms if s.reference == "R1") if hasattr(syms[0], "reference") else None
        if r1 is None:
            r.check("sch_symbol_lookup", False, f"symbol attrs: {[a for a in dir(syms[0]) if not a.startswith('_')]}")
        else:
            old = r1.value
            r1.value = "4.7K"
            sch.update_items(r1)
            out = p.dir / "netlist-api.net"
            t0 = time.monotonic()
            job = sch.export_netlist(str(out))
            r.note("ecc83.api_netlist_s", round(time.monotonic() - t0, 2))
            sch.save()
            job2 = sch.export_netlist(str(p.dir / "netlist-api2.net"))
            stale = '"4.7K"' not in out.read_text() and '"4.7K"' not in (p.dir / "netlist-api2.net").read_text()
            # documents a gap: in a headless api-server the schematic job handler caches its own
            # copy of the schematic (EESCHEMA_JOBS_HANDLER::m_cliSchematic), loaded from disk once
            r.check("api_netlist_job_is_stale", stale and '"4.7K"' in p.sch.read_text(),
                    f"job ok={job.succeeded}/{job2.succeeded}; neither unsaved nor saved edit in the API job "
                    f"netlist; saved .kicad_sch has it={'4.7K' in p.sch.read_text()}")
            # workaround: save + fresh kicad-cli process
            out, secs = p.netlist(p.dir / "netlist-2.net")
            res2 = b.import_netlist(str(out))
            fps2 = {f.reference_field.text.value: f for f in b.get_footprints()}
            kept = all((fps2[ref].position.x, fps2[ref].position.y) == xy for ref, xy in moved.items())
            r.check("resync_keeps_placement", kept and res2.new_footprint_count == 0,
                    f"new={res2.new_footprint_count} kept={kept}")
            r.check("resync_updates_value", fps2["R1"].value_field.text.value == "4.7K",
                    f"R1 value {old} -> {fps2['R1'].value_field.text.value}; report: "
                    + " | ".join(line for line in res2.report.splitlines() if "R1 " in line))
            # removal: delete a symbol in the schematic, re-sync with delete_extra_footprints
            victim = next(s for s in sch.get_symbols() if s.reference == "R2")
            sch.remove_items(victim)
            sch.save()
            out, _ = p.netlist(p.dir / "netlist-3.net")
            res3 = b.import_netlist(str(out), delete_extra_footprints=True)
            refs = {f.reference_field.text.value for f in b.get_footprints()}
            r.check("resync_deletes_removed", "R2" not in refs and len(refs) == 14,
                    f"refs={len(refs)}; report: " + " | ".join(x for x in res3.report.splitlines() if "R2" in x))
            # same with delete_extra_footprints=False keeps an orphan? (re-add not possible: R2 is gone)
        b.save()
        r.check("board_saved", "(footprint" in p.pcb.read_text(), p.pcb.stat().st_size)

    # --- OpenDriveHub main --------------------------------------------------------------------
    o = boards.odh(base)
    net, secs = o.netlist()
    r.note("odh.cli_netlist_s", round(secs, 2))
    with Server(o.pro) as srv:
        k = srv.client(300000)
        t0 = time.monotonic()
        sch, b = open_both(k, o)
        r.note("odh.open_docs_s", round(time.monotonic() - t0, 2))
        t0 = time.monotonic()
        res = b.import_netlist(str(net))
        r.note("odh.import_s", round(time.monotonic() - t0, 2))
        r.note("odh.rss_mb", round(srv.rss()))
        fps = b.get_footprints()
        r.check("odh_import", res.error_count == 0 and len(fps) > 70,
                f"err={res.error_count} warn={res.warning_count} new={res.new_footprint_count} fps={len(fps)}")
        r.note("odh.report_errors_warnings",
               [x for x in res.report.splitlines() if not x.startswith(("Processing", "Added", "Changed", "Updated", "Connected", "Add net"))][:30])
        local = [f for f in fps if f.definition.id.library == "OpenDriveHub"] if hasattr(fps[0], "definition") else []
        libs = collections.Counter(f.proto.definition.id.library_nickname for f in fps)
        r.check("project_local_library", libs.get("OpenDriveHub", 0) >= 1,
                f"libraries={dict(libs)} local={len(local)}")
        l201 = next((f for f in fps if f.reference_field.text.value == "L201"), None)
        r.check("l201_custom_footprint", l201 is not None and len(l201.definition.pads) == 2,
                f"L201 {l201.proto.definition.id if l201 else None} pads={len(l201.definition.pads) if l201 else 0}")
        sheets = collections.Counter(f.proto.symbol_sheet_name for f in fps)
        r.check("odh_sheets", len(sheets) >= 4, dict(sheets))
        r.note("odh.nets", len(b.get_nets()))
        r.note("odh.pads", len(b.get_pads()))
        b.save()
        r.note("odh.pcb_bytes", o.pcb.stat().st_size)
    return r.finish()


if __name__ == "__main__":
    main_guard(run)
