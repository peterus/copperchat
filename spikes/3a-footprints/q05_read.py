"""Q5: read any footprint (stock + local) with full pad geometry, courtyard, fab, 3D model,
WITHOUT a .kicad_pcb. Two routes: GetItemsFromLibrary (needs the footprint library handler)
and the footprint document (OpenDocument + GetItems + GetItemsById(footprint))."""

import statistics
import tempfile
import time
from pathlib import Path

from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.common.commands import library_commands_pb2 as lc
from kipy.proto.common.types import library_types_pb2 as lt
from kipy.util.proto import unpack_any

import l201
from fpdoc import FootprintDoc, kind
from harness import Recorder, Server, main_guard
from repo import LIB, make_repo
from steps import create_l201, open_sch

STOCK = [
    "Resistor_SMD:R_0805_2012Metric",
    "Inductor_SMD:L_Bourns-SRN4018",
    "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
    "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm",
]


def from_library(k, ids: list[str]) -> list:
    cmd = lc.GetItemsFromLibrary(type=lt.LT_FOOTPRINT)
    for i in ids:
        nick, _, name = i.partition(":")
        cmd.item_ids.add(library_nickname=nick, entry_name=name)
    return [unpack_any(a) for a in k._client.send(cmd, ec.GetItemsResponse).items]


def summarize_definition(fp: bt.Footprint) -> dict:
    items = [unpack_any(a) for a in fp.items]
    pads = [i for i in items if kind(i) == "Pad"]
    shapes = [i for i in items if kind(i) == "BoardGraphicShape"]
    models = [i for i in items if kind(i) == "Footprint3DModel"]
    p0 = pads[0] if pads else None
    return {
        "id": f"{fp.id.library_nickname}:{fp.id.entry_name}",
        "descr": fp.attributes.description[:40],
        "mounting": bt.FootprintMountingStyle.Name(fp.attributes.mounting_style),
        "pads": len(pads),
        "pad0": None if p0 is None else {
            "number": p0.number, "type": bt.PadType.Name(p0.type),
            "shape": bt.PadStackShape.Name(p0.pad_stack.copper_layers[0].shape),
            "pos_mm": (p0.position.x_nm / 1e6, p0.position.y_nm / 1e6),
            "size_mm": (p0.pad_stack.copper_layers[0].size.x_nm / 1e6, p0.pad_stack.copper_layers[0].size.y_nm / 1e6),
            "drill_mm": p0.pad_stack.drill.diameter.x_nm / 1e6,
            "layers": [bt.BoardLayer.Name(x) for x in p0.pad_stack.layers],
        },
        "crtyd": sum(bt.BoardLayer.Name(s.layer) == "BL_F_CrtYd" for s in shapes),
        "fab": sum(bt.BoardLayer.Name(s.layer) == "BL_F_Fab" for s in shapes),
        "models": [m.filename.rsplit("/", 1)[-1] for m in models],
    }


def main() -> int:
    r = Recorder("q05_read")
    repo = make_repo(Path(tempfile.mkdtemp(prefix="c3a-q5-")), board=False)
    repo.lib_dir.mkdir()
    with Server(repo.main_pro) as srv:
        k = srv.client(120000)
        open_sch(k, repo.main_pro)
        r.note("rss_sch_only_mb", round(srv.rss()))
        try:
            from_library(k, STOCK[:1])
            r.check("library_read_needs_handler", False, "answered without board / fp document")
        except Exception as e:  # noqa: BLE001
            r.check("library_read_needs_handler", "no handler available" in str(e), str(e))

        t0 = time.monotonic()
        first = FootprintDoc(k, STOCK[0])
        r.note("first_fp_open_s", round(time.monotonic() - t0, 2))
        r.note("rss_after_first_fp_open_mb", round(srv.rss()))
        st = [s for s in k.get_library_statuses() if s.type == lt.LT_FOOTPRINT]
        loaded = sum(s.status == lt.LLS_LOADED for s in st)
        r.check("first_open_loads_all_fp_libraries", loaded == len(st) and loaded > 100, f"{loaded}/{len(st)} loaded")
        first.close()

        create_l201(k)  # local footprint in the shared library
        ids = [*STOCK, f"{LIB}:{l201.NAME}"]

        # route 1: GetItemsFromLibrary (handler now registered, still no board)
        per = []
        for i in ids:
            t = time.monotonic()
            d = from_library(k, [i])
            per.append(time.monotonic() - t)
            assert len(d) == 1, i
        r.note("lib_single_ms", {"median": round(statistics.median(per) * 1000, 1), "max": round(max(per) * 1000, 1)})
        defs = from_library(k, ids)
        summ = [summarize_definition(d) for d in defs]
        r.note("definitions", summ)
        by = {s["id"]: s for s in summ}
        loc = by[f"{LIB}:{l201.NAME}"]
        r.check("local_from_library", loc["pads"] == 2 and loc["pad0"]["size_mm"] == (1.3, 3.5) and loc["crtyd"] == 1, loc)
        tht = by["Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"]
        r.check("tht_drill_and_layers", tht["pad0"]["type"] == "PT_PTH" and tht["pad0"]["drill_mm"] > 0
                and "BL_F_Cu" in tht["pad0"]["layers"], tht["pad0"])
        r.check("3d_model_in_definition", all(s["models"] for s in summ if not s["id"].startswith(LIB)),
                {s["id"]: s["models"] for s in summ})
        qfn = by["Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm"]
        r.check("qfn_pad_count", qfn["pads"] >= 33, qfn["pads"])

        # batch cost: a whole stock library in one request
        names = [f"Package_SO:{i.proto.entry_name}" for i in k.get_library_footprints(["Package_SO"])]
        t = time.monotonic()
        batch = from_library(k, names)
        r.note("lib_batch", {"n": len(batch), "s": round(time.monotonic() - t, 2)})
        r.check("batch_complete", len(batch) == len(names), (len(batch), len(names)))

        # route 2: footprint document per footprint (items + footprint-level data)
        per, descr = [], {}
        for i in ids:
            t = time.monotonic()
            fp = FootprintDoc(k, i)
            items = fp.items()
            parent = next(x.parent.value for x in items if kind(x) == "Pad")
            inst = fp.items_by_id([parent])[0]
            fp.close()
            per.append(time.monotonic() - t)
            descr[i] = inst.definition.attributes.description
        r.note("doc_read_ms", {"median": round(statistics.median(per) * 1000, 1), "max": round(max(per) * 1000, 1)})
        r.check("doc_route_stock_descriptions", all(descr[i] for i in STOCK), {i: descr[i][:40] for i in STOCK})
        # G2 evidence: the local footprint still carries the SEED's description, not L201's
        local = descr[f"{LIB}:{l201.NAME}"]
        r.check("doc_route_local_descr_is_seeds", local == descr["Inductor_SMD:L_Bourns-SRN4018"]
                and local != l201.DESCRIPTION, local[:80])
        r.note("rss_end_mb", round(srv.rss()))
        r.check("server_alive", srv.alive(), srv.log()[-300:])
    return r.finish()


if __name__ == "__main__":
    main_guard(main)
