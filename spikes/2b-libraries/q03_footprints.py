"""Q3: footprint listing/detail (description, pads), and package (0805) derivation."""

import collections
import time

from harness import Recorder, Server, fresh_project, main_guard, rss_mb
from libdata import IMPERIAL_TO_METRIC, fetch_footprints, package_of

PASSIVE_LIBS = ("Resistor_SMD", "Capacitor_SMD", "Inductor_SMD", "LED_SMD", "Diode_SMD")


def main() -> int:
    rec = Recorder("q03_footprints")
    with Server(fresh_project()) as s:
        k = s.client(300000)
        s.open_schematic()
        s.open_board()
        k.load_all_libraries()
        k.wait_for_libraries(interval_ms=100, timeout_s=300)
        ids = list(k.get_library_footprints())
        rec.check("listing_names_only", [f.name for f in ids[0].proto.DESCRIPTOR.fields] == ["library_nickname", "entry_name"], "")
        t = time.monotonic()
        fps = fetch_footprints(k, ids)
        fetch_s = time.monotonic() - t
        rec.note("fetch_all_footprints", dict(count=len(fps), seconds=round(fetch_s, 2), rss_mb=round(rss_mb(s.proc.pid))))
        rec.check("fetch_all_footprints_lt_30s", fetch_s < 30, f"{fetch_s:.1f}s for {len(fps)}")
        rec.note("coverage", dict(
            description=sum(1 for f in fps.values() if f.description),
            keywords=sum(1 for f in fps.values() if f.keywords),
            pads_ge1=sum(1 for f in fps.values() if f.pad_count > 0),
            smd=sum(1 for f in fps.values() if f.smd), tht=sum(1 for f in fps.values() if f.tht),
        ))
        r = fps["Resistor_SMD:R_0805_2012Metric"]
        rec.check("r0805_detail", r.pad_count == 2 and r.smd and "0805" in r.description, r)

        # --- package derivation -----------------------------------------------------------
        by_lib = collections.defaultdict(lambda: [0, 0, []])  # lib -> [total, derived, misses]
        for lid, f in fps.items():
            lib, name = lid.split(":", 1)
            if lib in PASSIVE_LIBS:
                by_lib[lib][0] += 1
                if package_of(name):
                    by_lib[lib][1] += 1
                else:
                    by_lib[lib][2].append(name)
        rec.note("name_derivation_by_lib", {lib: dict(total=v[0], derived=v[1], miss_sample=v[2][:6]) for lib, v in by_lib.items()})
        # consistency: name says 0805 -> description should say 0805 as well (independent evidence)
        agree = disagree = 0
        bad = []
        for lid, f in fps.items():
            pk = package_of(lid.split(":", 1)[1])
            if pk and f.description:
                if pk in f.description:
                    agree += 1
                else:
                    disagree += 1
                    bad.append(lid)
        rec.check("name_vs_description_agree", disagree == 0 or agree / (agree + disagree) > 0.98, dict(agree=agree, disagree=disagree, bad=bad[:5]))
        # with the passive rule used by the preference check (2 SMD pads) the disagreements disappear
        bad2 = [l for l in bad if fps[l].pad_count == 2 and fps[l].smd]
        rec.check("name_vs_description_agree_for_two_pad_smd", not bad2, bad2)
        chips2 = [l for l, f in fps.items() if package_of(l.split(":", 1)[1]) and f.pad_count == 2 and f.smd]
        rec.note("two_pad_smd_package_footprints", len(chips2))
        # every SMD R/C/L/LED/D chip footprint of the common sizes is found by package
        want = {p: sorted(l for l in fps if package_of(l.split(":", 1)[1]) == p and l.split(":", 1)[0] in PASSIVE_LIBS) for p in ("0402", "0603", "0805", "1206")}
        rec.note("footprints_per_package_in_passive_libs", {p: len(v) for p, v in want.items()})
        rec.note("0805_samples", want["0805"][:12])
        # metric-only trap
        rec.check("trap_01005_not_read_as_0402", package_of("C_01005_0402Metric") == "01005" and package_of("C_0402_1005Metric") == "0402", "")
        rec.check("hand_solder_variant", package_of("R_0805_2012Metric_Pad1.20x1.40mm_HandSolder") == "0805", "")
        rec.check("tht_not_matched", package_of("R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal") is None, "")
        # Footprint the symbol filters point to: does the default 'Device:R' (no footprint) need a choice? yes -> placement tool must set one.
        # Chip-size names outside the *_SMD libs (modules etc.) that would match anyway:
        outside = [l for l in fps if package_of(l.split(":", 1)[1]) and l.split(":", 1)[0] not in PASSIVE_LIBS]
        rec.note("package_named_footprints_outside_passive_libs", dict(count=len(outside), sample=outside[:8]))
        # Footprints that carry a size token but no Metric suffix (name-only derivation misses them)
        import re
        loose = [l for l in fps if l.split(":", 1)[0] in PASSIVE_LIBS and not package_of(l.split(":", 1)[1]) and re.search(r"(0402|0603|0805|1206)", l)]
        rec.note("passive_lib_names_with_size_token_not_derived", dict(count=len(loose), sample=loose[:10]))
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
