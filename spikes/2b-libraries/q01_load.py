"""Q1: library load time + RSS (full stock libs, per type), and lazy behaviour without LoadAll."""

import json
import time

from kipy.proto.common.types import LibraryType

from harness import RESULTS as RESULTS_DIR, Recorder, Server, fresh_project, main_guard, rss_mb

SYM, FP = LibraryType.LT_SYMBOL, LibraryType.LT_FOOTPRINT


def load(rec, label, types):
    with Server(fresh_project()) as s:
        k = s.client(120000)
        s.open_schematic()
        base = rss_mb(s.proc.pid)
        t0 = time.monotonic()
        k.load_all_libraries(types)
        call_s = time.monotonic() - t0
        sts = k.wait_for_libraries(types, interval_ms=100, timeout_s=300)
        done_s = time.monotonic() - t0
        bad = [(x.nickname, x.status, x.error_message) for x in sts if x.status != 3]
        rec.check(f"{label}_all_libraries_loaded", not bad, f"{len(sts)} libraries, not loaded: {bad[:5]}")
        rss = rss_mb(s.proc.pid)
        t1 = time.monotonic()
        n_sym = len(k.get_library_symbols()) if types is None or SYM in (types if isinstance(types, list) else [types]) else -1
        list_sym_s = time.monotonic() - t1
        t1 = time.monotonic()
        n_fp = len(k.get_library_footprints()) if types is None or FP in (types if isinstance(types, list) else [types]) else -1
        list_fp_s = time.monotonic() - t1
        rec.note(
            label,
            dict(
                base_rss_mb=round(base), rss_mb=round(rss), load_call_s=round(call_s, 2),
                load_done_s=round(done_s, 2), libraries=len(sts), failed=bad[:5],
                symbols=n_sym, list_symbols_s=round(list_sym_s, 2),
                footprints=n_fp, list_footprints_s=round(list_fp_s, 2),
                rss_after_list_mb=round(rss_mb(s.proc.pid)),
            ),
        )
        return n_sym, n_fp, done_s, rss


def try_(rec, name, fn, expect=True):
    """Record an observed behaviour; the check passes when it matches `expect`."""
    try:
        t = time.monotonic()
        r = fn()
        rec.check(name + ("" if expect else "_WORKS_UNEXPECTEDLY"), expect, f"works: {r} {time.monotonic() - t:.2f}s")
    except Exception as e:  # noqa: BLE001
        rec.check(name + ("" if expect else "_refused_as_expected"), not expect, f"{type(e).__name__}: {e}")


def lazy(rec):
    """No / partial LoadAllLibraries: which per-library lazy paths work?"""
    # S1: schematic open, nothing listed or loaded: place + get symbol straight away.
    with Server(fresh_project()) as s:
        k = s.client(120000)
        s.open_schematic()
        sch = k.get_schematic()
        rec.note("s1_rss_before", round(rss_mb(s.proc.pid)))
        try_(rec, "s1_place_symbol_cold", lambda: sch.place_symbol_from_library("Device:R", _v2(0, 0)).reference_field, expect=False)
        try_(rec, "s1_get_symbol_cold_other_lib", lambda: k.get_library_symbol("Connector_Generic:Conn_01x02").id)
        try_(rec, "s1_place_after_lazy_get", lambda: sch.place_symbol_from_library("Connector_Generic:Conn_01x02", _v2(10, 0)).id)
        try_(rec, "s1_list_names_cold_Device", lambda: len(k.get_library_symbols(["Device"])))
        try_(rec, "s1_get_footprint_cold", lambda: k.get_library_footprint("Resistor_SMD:R_0805_2012Metric").id, expect=False)
        rec.note("s1_rss_after", round(rss_mb(s.proc.pid)))
    # S2: schematic AND board open (as KicadSession does), footprints lazily
    with Server(fresh_project()) as s:
        k = s.client(120000)
        s.open_schematic()
        s.open_board()
        try_(rec, "s2_get_footprint_with_board_open", lambda: k.get_library_footprint("Resistor_SMD:R_0805_2012Metric").id)
        try_(rec, "s2_list_footprints_one_lib", lambda: len(k.get_library_footprints(["Resistor_SMD"])))
        rec.note("s2_rss", round(rss_mb(s.proc.pid)))
    # S3: load_all symbols only, then a footprint request
    with Server(fresh_project()) as s:
        k = s.client(120000)
        s.open_schematic()
        k.load_all_libraries(SYM)
        k.wait_for_libraries(SYM, interval_ms=100)
        try_(rec, "s3_get_footprint_after_symbol_loadall", lambda: k.get_library_footprint("Resistor_SMD:R_0805_2012Metric").id, expect=False)
        try_(rec, "s3_list_footprints_after_symbol_loadall", lambda: len(k.get_library_footprints(["Resistor_SMD"])), expect=False)


def _v2(x, y):
    from kipy.geometry import Vector2

    return Vector2.from_xy_mm(x, y)


def per_library(rec):
    """Lazy-load every library one at a time (nickname list) and rank RSS/time cost."""
    with Server(fresh_project()) as s:
        k = s.client(120000)
        s.open_schematic()
        s.open_board()
        rows = []
        for kind, ltype, lister in (("sym", SYM, k.get_library_symbols), ("fp", FP, k.get_library_footprints)):
            nicks = [x.nickname for x in k.get_library_statuses(ltype)]
            for n in nicks:
                r0, t0 = rss_mb(s.proc.pid), time.monotonic()
                cnt = len(lister([n]))
                rows.append((kind, n, cnt, round(rss_mb(s.proc.pid) - r0, 1), round(time.monotonic() - t0, 3)))
        rec.note("per_library_final_rss_mb", round(rss_mb(s.proc.pid)))
        for kind in ("sym", "fp"):
            sub = [r for r in rows if r[0] == kind]
            tot = sum(r[3] for r in sub)
            top = sorted(sub, key=lambda r: -r[3])[:8]
            rec.note(f"per_library_{kind}", dict(libs=len(sub), total_delta_mb=round(tot), top=top,
                                                 median_delta_mb=sorted(r[3] for r in sub)[len(sub) // 2]))
        (RESULTS_DIR / "per_library.json").write_text(json.dumps(rows))


def main() -> int:
    rec = Recorder("q01_load")
    lazy(rec)
    per_library(rec)
    ns, nf, t_all, rss_all = load(rec, "load_all", None)
    rec.check("symbol_count_ge_20k", ns >= 20000, ns)
    rec.check("footprint_count_ge_15k", nf >= 15000, nf)
    load(rec, "load_symbols_only", SYM)
    load(rec, "load_footprints_only", FP)
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
