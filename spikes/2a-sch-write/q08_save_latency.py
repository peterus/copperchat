"""Q8: mid-turn save() + save_copy (save_as root + byte copy of the hierarchy files) +
kicad-cli ERC latency on the ~100-symbol hierarchical demo, and whether the copy holds
just-edited sub-sheets and a newly created sheet."""

import hashlib
import json
import shutil
import statistics
import tempfile
import time
from pathlib import Path

from geom import pin_ends_abs
from harness import (
    Recorder,
    Server,
    by_id,
    cli_netlist,
    fresh_fixture,
    keep_artifact,
    load_libraries,
    local_label,
    main_guard,
    netlist_components,
    nets_by_pin,
    require_kicad,
    root_sch,
    run_cli,
    sid,
    update_symbols,
    v2,
)
from q07_sheets import add_sheet


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def save_copy(sch, proj: Path, dest: Path, skip_missing: bool = False) -> list[str]:
    """Foundation KicadSession.save_copy procedure (spike 1C Q4): root via the API, other
    hierarchy files byte-copied from the project dir."""
    root = root_sch(proj)
    dest.mkdir(parents=True, exist_ok=True)
    sch.save_as(str(dest / root.name), overwrite=True, include_project=False)
    missing = []
    for name in sorted({s.filename for s in sch.get_hierarchy()} - {root.name}):
        if skip_missing and not (proj / name).exists():
            missing.append(name)
            continue
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(proj / name, dest / name)
    return missing


def erc(sch_file: Path, out: Path) -> tuple[int, int]:
    r = run_cli("sch", "erc", "--format", "json", "--units", "mm", "-o", str(out), str(sch_file))
    vios = [v for s in json.loads(out.read_text())["sheets"] for v in s["violations"]] if out.exists() else []
    return r.returncode, len(vios)


def main() -> int:
    require_kicad()
    rec = Recorder("q08_save_latency")
    proj = fresh_fixture("complex_hierarchy")
    tmp = Path(tempfile.mkdtemp(prefix="s2a-q08-"))
    with Server(proj) as srv:
        k = srv.client(timeout_ms=120000)
        sch = k.get_schematic()
        n_sym = len(sch.get_symbols())
        rec.check(
            "fixture_size_100_plus_symbols",
            n_sym >= 100,
            f"{n_sym} symbol unit instances, {len(list(sch.get_hierarchy()))} sheet instances",
        )
        tmp0 = Path(tempfile.mkdtemp(prefix="s2a-q08-nl-"))
        api = {kk: v for kk, v in nets_by_pin(sch).items() if not kk[0].startswith("#")}
        cli0 = cli_netlist(root_sch(proj), tmp0)
        diff = sorted(kk for kk in set(api) | set(cli0) if api.get(kk) != cli0.get(kk))
        rec.check("live_netlist_matches_cli_on_hierarchical_demo", not diff and len(api) > 100, {"pins": len(api), "diff": diff[:8]})
        load_libraries(k)
        inst = {s.name: s for s in sch.get_hierarchy()}
        sub = sch.for_sheet(inst["ampli_ht_vertical"])
        # edit an existing sub-sheet symbol + place a new one on the sub-sheet
        r201 = next(s for s in sub.get_symbols() if s.reference == "R201")
        r201 = by_id(sub, [sid(r201)])[sid(r201)]
        r201.value = "4k7-EDITED"
        update_symbols(sub, [r201])
        new_sub = sub.place_symbol_from_library("Device:R", v2(25.4, 25.4), reference="R299")
        sub.create_items([local_label("EDITED_NET", pin_ends_abs(new_sub)["1"])])
        # new sheet with a part on it
        add_sheet(sch, "Extra", "extra.kicad_sch", (25.4, 152.4), (25.4, 20.32), proj)
        extra = next(s for s in sch.get_hierarchy() if s.filename == "extra.kicad_sch")
        sch.for_sheet(extra).place_symbol_from_library("Device:R", v2(50.8, 50.8), reference="R401")
        rec.check("document_modified_before_save", sch.is_document_modified(), "")
        other = sch.for_sheet(inst["ampli_ht_horizontal"])
        rec.check(
            "new_symbol_on_shared_sheet_visible_in_other_instance",
            any(sid(s) == sid(new_sub) for s in other.get_symbols()),
            "ampli_ht.kicad_sch has 2 instances; GetItems on the 2nd",
        )

        # copy WITHOUT save first: sub-sheet bytes on disk are stale
        stale = tmp / "stale"
        missing = save_copy(sch, proj, stale, skip_missing=True)
        rec.check("copy_without_save_finds_all_hierarchy_files", not missing, f"not on disk yet: {missing}")
        nl = tmp / "stale.net"
        run_cli("sch", "export", "netlist", "-o", str(nl), str(stale / root_sch(proj).name))
        comps = netlist_components(nl.read_text()) if nl.exists() else {}
        rec.check(
            "copy_without_save_has_subsheet_edits",
            "R299" in comps and comps.get("R201", {}).get("value") == "4k7-EDITED",
            {"R299": "R299" in comps, "R201": comps.get("R201", {}).get("value"), "extra_file": (stale / "extra.kicad_sch").exists()},
        )

        before = {p.name: sha(p) for p in proj.glob("*.kicad_sch")}
        timings = {"save": [], "save_copy": [], "erc": [], "total": []}
        for i in range(3):
            if i:  # a small edit per round so every save has something to write
                g = by_id(sub, [sid(r201)])[sid(r201)]
                g.value = f"4k7-EDITED-{i}"
                update_symbols(sub, [g])
            t0 = time.monotonic()
            sch.save()
            t1 = time.monotonic()
            dest = tmp / f"copy{i}"
            save_copy(sch, proj, dest)
            t2 = time.monotonic()
            rc, n = erc(dest / root_sch(proj).name, tmp / f"erc{i}.json")
            t3 = time.monotonic()
            for key, v in (("save", t1 - t0), ("save_copy", t2 - t1), ("erc", t3 - t2), ("total", t3 - t0)):
                timings[key].append(round(v * 1000))
        rec.note("timings_ms", timings)
        rec.note("median_ms", {k2: statistics.median(v) for k2, v in timings.items()})
        rec.check("save_plus_copy_plus_erc_under_10s", statistics.median(timings["total"]) < 10000, timings["total"])
        rec.check("document_clean_after_save", not sch.is_document_modified(), "")
        after = {p.name: sha(p) for p in proj.glob("*.kicad_sch")}
        rec.note("files_rewritten_by_save", sorted(n for n in after if before.get(n) != after[n]))
        rec.check("erc_runs_on_copy", rc in (0, 5) and n >= 0, f"rc={rc}, {n} violations")

        cli = cli_netlist(dest / root_sch(proj).name, tmp)
        comps = netlist_components((tmp / "cli.net").read_text())
        rec.check(
            "copy_has_edited_subsheet_value", comps.get("R201", {}).get("value") == "4k7-EDITED-2", comps.get("R201", {}).get("value")
        )
        rec.check(
            "copy_has_new_subsheet_symbol",
            "R299" in comps and any(n2.endswith("EDITED_NET") for (r, _), n2 in cli.items() if r == "R299"),
            [n2 for (r, _), n2 in cli.items() if r == "R299"],
        )
        rec.check(
            "copy_has_new_sheet_file_and_symbol",
            (dest / "extra.kicad_sch").exists() and "R401" in comps,
            sorted(p.name for p in dest.iterdir()),
        )
        rec.check(
            "shared_sheet_second_instance_unique_ref_after_save",
            len([c for c in comps if c.startswith(("R29", "R39")) or "?" in c]) == 2,
            sorted(c for c in comps if c.startswith(("R29", "R39")) or "?" in c),
        )
        # workaround: save + revert (reload) makes the 2nd instance visible; then set its
        # per-instance reference through the sheet-bound Schematic
        sch.revert()
        sch = k.get_schematic()
        inst = {s.name: s for s in sch.get_hierarchy()}
        other = sch.for_sheet(inst["ampli_ht_horizontal"])
        g = next((s for s in other.get_symbols() if sid(s) == sid(new_sub)), None)
        if g is not None:
            g.reference = "R399"
            update_symbols(other, [g])
        refs = {
            n: [s.reference for s in sch.for_sheet(inst[n]).get_symbols() if sid(s) == sid(new_sub)]
            for n in ("ampli_ht_vertical", "ampli_ht_horizontal")
        }
        rec.check(
            "workaround_save_revert_then_per_instance_reference",
            refs == {"ampli_ht_vertical": ["R299"], "ampli_ht_horizontal": ["R399"]},
            refs,
        )
        sch.save()
        keep_artifact(proj, "q08_hierarchy")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
