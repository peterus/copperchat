"""Q2: GetLibraryItems payload; GetItemsFromLibrary for symbols (pins, units, body styles, fields)."""

import time

from kipy.geometry import Vector2

from harness import Recorder, Server, fresh_project, main_guard, rss_mb
from libdata import fetch_symbols, symbol_from_proto

ETYPE = {0: "?", 1: "input", 2: "output", 3: "bidirectional", 4: "tri_state", 5: "passive",
         6: "free", 7: "unspecified", 8: "power_in", 9: "power_out", 10: "open_collector",
         11: "open_emitter", 12: "no_connect"}


def main() -> int:
    rec = Recorder("q02_symbols")
    with Server(fresh_project()) as s:
        k = s.client(300000)
        s.open_schematic()
        s.open_board()
        k.load_all_libraries()
        k.wait_for_libraries(interval_ms=100, timeout_s=300)

        ids = list(k.get_library_symbols())
        rec.note("list_entry_fields", [f.name for f in ids[0].proto.DESCRIPTOR.fields])
        rec.check("listing_names_only", [f.name for f in ids[0].proto.DESCRIPTOR.fields] == ["library_nickname", "entry_name"],
                  "GetLibraryItems returns only (library_nickname, entry_name)")

        t = time.monotonic()
        defs = fetch_symbols(k, ids)
        fetch_s = time.monotonic() - t
        rec.note("fetch_all_details", dict(symbols=len(defs), seconds=round(fetch_s, 2), batch=500,
                                           rss_mb=round(rss_mb(s.proc.pid))))
        rec.check("fetch_all_details_lt_10s", fetch_s < 10, f"{fetch_s:.2f}s for {len(defs)} symbols")

        n = len(defs)
        cov = dict(
            description=sum(1 for d in defs.values() if d.description),
            keywords=sum(1 for d in defs.values() if d.keywords),
            footprint_filters=sum(1 for d in defs.values() if d.footprint_filters),
            default_footprint=sum(1 for d in defs.values() if d.footprint),
            datasheet=sum(1 for d in defs.values() if d.datasheet and d.datasheet != "~"),
            pins_present=sum(1 for d in defs.values() if d.pins is not None),
            multi_unit=sum(1 for d in defs.values() if d.unit_count > 1),
            multi_body_style=sum(1 for d in defs.values() if len(d.body_styles) > 1),
            power=sum(1 for d in defs.values() if d.power),
        )
        rec.note("field_coverage_of_%d" % n, cov)
        derived = [d.lib_id for d in defs.values() if d.pins is None]
        rec.note("derived_symbols_without_items", dict(count=len(derived), share=round(len(derived) / n, 3), sample=derived[:5]))
        rec.check("derived_symbols_have_no_items_known_limit", len(derived) > 10000, f"{len(derived)} of {n} symbols come back without pins/graphics (extends symbols); see probe checks")

        # Pin detail for a base symbol and a multi-unit one
        qn = sorted(x for x in defs if x.startswith("Device:Q_NPN") and defs[x].pins)[0]
        for lid in ("Device:R", "Connector_Generic:Conn_01x04", qn):
            d = defs[lid]
            rec.note(f"pins_{lid}", [(p.number, p.name, ETYPE.get(p.etype, p.etype), round(p.x_nm / 1e6, 3), round(p.y_nm / 1e6, 3), p.unit, p.body_style) for p in d.pins])
        multi = next(d for d in defs.values() if d.unit_count >= 3 and d.pins)
        rec.note("multi_unit_sample", dict(lib_id=multi.lib_id, units=multi.unit_count, pins=[(p.number, p.name, ETYPE.get(p.etype, p.etype), p.unit) for p in multi.pins][:10]))
        bs = next((d for d in defs.values() if len(d.body_styles) > 1 and d.pins), None)
        if bs:
            rec.note("body_style_sample", dict(lib_id=bs.lib_id, styles=bs.body_styles, pin_body_styles=sorted({p.body_style for p in bs.pins})))
        rec.check("pin_number_name_type_position_unit", all(p.number and p.etype is not None for p in defs["Device:R"].pins), "Device:R")

        # Wrapper vs. raw: pin positions are relative to the symbol origin, unrotated (library coordinates)
        w = k.get_library_symbol("Device:R")
        rec.note("kipy_wrapper_pins_Device:R", [(p.number, p.name, p.position) for p in w.pins])

        # Derived symbols: pins via a probe placement that is dropped
        sch = k.get_schematic()
        n0 = len(list(sch.get_symbols()))
        probes = []
        ok = True
        for lid in derived[:40]:
            tt = time.monotonic()
            commit = sch.begin_commit()
            try:
                inst = sch.place_symbol_from_library(lid, Vector2.from_xy_mm(200, 200))
                npins = len(inst.definition.pins)
                probes.append((lid, npins, round((time.monotonic() - tt) * 1000)))
            finally:
                sch.drop_commit(commit)
        n1 = len(list(sch.get_symbols()))
        rec.check("probe_place_in_dropped_commit_leaves_doc", n0 == n1 and not sch.is_document_modified(),
                  f"symbols {n0}->{n1}, modified={sch.is_document_modified()}")
        rec.check("probe_place_returns_pins_for_derived", all(p[1] > 0 for p in probes), probes[:5])
        rec.note("probe_ms_median", sorted(p[2] for p in probes)[len(probes) // 2])
        # a derived symbol and its base agree on pin count?
        lm = defs["Amplifier_Operational:LM358"]
        rec.note("LM358_listing_pins", None if lm.pins is None else len(lm.pins))
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
