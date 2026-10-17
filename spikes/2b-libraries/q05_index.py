"""Q5: end-to-end warm-up (load -> list -> details -> index) and query latency / relevance."""

import statistics
import time
import tracemalloc

from harness import Recorder, Server, fresh_project, main_guard, rss_mb
from index import Index
from libdata import fetch_footprints, fetch_symbols, package_of

SYM_QUERIES = [  # (query, expected lib_id in top-5)
    ("resistor", "Device:R"),
    ("capacitor", "Device:C"),
    ("LM358", "Amplifier_Operational:LM358"),
    ("opamp dual", None),
    ("stm32f103", None),
    ("ESP32-WROOM", None),
    ("npn transistor", None),
    ("n-channel mosfet", None),
    ("ldo 3.3v", None),
    ("usb type-c receptacle", None),
    ("led", "Device:LED"),
    ("GND", "power:GND"),
    ("+3V3", "power:+3V3"),
    ("conn_01x04", "Connector_Generic:Conn_01x04"),
    ("AMS1117", None),
    ("ne555 timer", None),
    ("schottky diode", None),
    ("ferrite bead", "Device:FerriteBead"),
    ("crystal", None),
    ("74hc595 shift register", None),
]
FP_QUERIES = [
    ("0805 resistor", "Resistor_SMD:R_0805_2012Metric"),
    ("0603 capacitor", "Capacitor_SMD:C_0603_1608Metric"),
    ("sot-23", None),
    ("soic-8", None),
    ("qfn-32", None),
    ("pinheader 1x04 2.54", None),
    ("usb-c", None),
    ("led 0805", "LED_SMD:LED_0805_2012Metric"),
    ("tssop-20", None),
    ("lqfp-48", None),
]


def timed(idx, queries, rounds=5):
    lat = []
    results = {}
    for q, _ in queries:
        idx.search(q)  # warm
    for _ in range(rounds):
        for q, _ in queries:
            t = time.perf_counter()
            r = idx.search(q, limit=20)
            lat.append((time.perf_counter() - t) * 1000)
            results[q] = r
    lat.sort()
    return lat, results


def report(rec, label, idx, queries, ids_expected_rank):
    lat, res = timed(idx, queries)
    p50 = statistics.median(lat)
    p95 = lat[int(len(lat) * 0.95) - 1]
    rec.note(f"{label}_latency_ms", dict(p50=round(p50, 2), p95=round(p95, 2), max=round(lat[-1], 2), n=len(lat)))
    rec.check(f"{label}_query_lt_300ms", lat[-1] < 300, f"max {lat[-1]:.1f} ms, p95 {p95:.1f} ms")
    miss = []
    tops = {}
    for q, want in queries:
        top = [r[0] for r in res[q][:5]]
        tops[q] = top[:3]
        if want and want not in top:
            miss.append((q, want, top))
        rec.check(f"{label}_nonempty:{q}", bool(res[q]), top[:3])
    rec.check(f"{label}_expected_in_top5", not miss, miss)
    rec.note(f"{label}_top3", tops)


def main() -> int:
    rec = Recorder("q05_index")
    with Server(fresh_project()) as s:
        k = s.client(300000)
        s.open_schematic()
        s.open_board()
        T0 = time.monotonic()
        k.load_all_libraries()
        k.wait_for_libraries(interval_ms=100, timeout_s=300)
        t_load = time.monotonic() - T0
        t = time.monotonic()
        sym_ids = list(k.get_library_symbols())
        fp_ids = list(k.get_library_footprints())
        t_list = time.monotonic() - t

        # name-only index (what exists right after the listing)
        tracemalloc.start()
        t = time.monotonic()
        names = Index()
        for x in sym_ids:
            names.add(f"{x.library}:{x.name}")
        names.finish()
        t_names = time.monotonic() - t
        rec.note("name_only_index", dict(entries=len(sym_ids), build_s=round(t_names, 2)))
        name_qs = [(q, w) for q, w in SYM_QUERIES if w and q not in ("resistor", "capacitor", "ferrite bead")]
        report(rec, "sym_name_only", names, name_qs, None)
        rec.check("name_only_cannot_find_generic_terms", not names.search("resistor") and not names.search("capacitor"),
                  "'resistor'/'capacitor' find nothing by name alone (Device:R, Device:C) - details are needed")

        t = time.monotonic()
        sd = fetch_symbols(k, sym_ids)
        t_sd = time.monotonic() - t
        t = time.monotonic()
        fd = fetch_footprints(k, fp_ids)
        t_fd = time.monotonic() - t

        t = time.monotonic()
        sidx = Index()
        for r in sd.values():
            sidx.add(r.lib_id, r.description, r.keywords)
        sidx.finish()
        t_sidx = time.monotonic() - t
        t = time.monotonic()
        fidx = Index()
        for r in fd.values():
            fidx.add(r.lib_id, r.description, r.keywords)
        fidx.finish()
        t_fidx = time.monotonic() - t
        cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        rec.note("warmup_timeline_s", dict(
            api_load=round(t_load, 1), list_names=round(t_list, 2), fetch_symbol_details=round(t_sd, 1),
            fetch_footprint_details=round(t_fd, 1), build_symbol_index=round(t_sidx, 2),
            build_footprint_index=round(t_fidx, 2),
            total=round(t_load + t_list + t_sd + t_fd + t_sidx + t_fidx, 1)))
        rec.note("client_python_heap_mb", dict(index_and_records=round(cur / 1e6), peak=round(peak / 1e6)))
        rec.note("vocab", dict(symbols=len(sidx.vocab), footprints=len(fidx.vocab)))
        report(rec, "sym", sidx, SYM_QUERIES, None)
        report(rec, "fp", fidx, FP_QUERIES, None)

        # preference check demo: "0805 resistor" search -> package of each hit's footprint name
        hits = [h[0] for h in fidx.search("0805 resistor", 8)]
        rec.note("pkg_of_hits", [(h, package_of(h.split(":", 1)[1])) for h in hits])
        rec.note("server_rss_mb", round(rss_mb(s.proc.pid)))
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
