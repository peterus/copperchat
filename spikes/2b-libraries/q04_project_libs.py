"""Q4: project-specific libraries via project sym-lib-table / fp-lib-table with ${KIPRJMOD}."""

from kipy.geometry import Vector2
from kipy.proto.common.types import LibraryTableScope, LibraryType

from harness import Recorder, Server, fresh_project, main_guard
from libdata import fetch_footprints, fetch_symbols

# demo -> (project symbol library nickname, project footprint library nickname, table quirk)
CASES = {
    "ecc83": ("ecc83-pp", "Footprints", "${KIPRJMOD}"),
    "cm5_minima": ("CM5IO", "CM5IO", "${KIPRJMOD}"),
    "pic_programmer": ("pic_programmer", "footprints", "${KIPRJMOD}/libs/ + ${KICAD10_FOOTPRINT_DIR} rows"),
    "interf_u": ("interf_u", "interf_u", "legacy $(KIPRJMOD) in fp table, (options \"Legacy\") sym row"),
}


def one(rec, demo, sym_nick, fp_nick, quirk):
    with Server(fresh_project(demo)) as s:
        k = s.client(300000)
        s.open_schematic()
        s.open_board()
        k.load_all_libraries()
        sts = k.wait_for_libraries(interval_ms=100, timeout_s=300)
        proj = {x.nickname: x for x in k.get_library_statuses(scope=LibraryTableScope.LTS_PROJECT)}
        rec.note(f"{demo}_project_rows", {n: (x.status, x.error_message) for n, x in proj.items() if n in (sym_nick, fp_nick)})
        rec.check(f"{demo}_project_scope_rows_listed", sym_nick in proj and fp_nick in proj, sorted(proj)[:6])
        rec.check(f"{demo}_project_rows_loaded", all(proj[n].status == 3 for n in (sym_nick, fp_nick) if n in proj),
                  {n: (proj[n].status, proj[n].error_message) for n in (sym_nick, fp_nick) if n in proj})
        syms = list(k.get_library_symbols([sym_nick]))
        fps = list(k.get_library_footprints([fp_nick]))
        rec.check(f"{demo}_project_symbols_listed", len(syms) > 0, f"{len(syms)} symbols, first {syms[0] if syms else None}")
        rec.check(f"{demo}_project_footprints_listed", len(fps) > 0, f"{len(fps)} footprints, first {fps[0] if fps else None}")
        d = fetch_symbols(k, syms)
        rec.note(f"{demo}_project_symbol_detail", [(r.lib_id, r.description[:40], r.pin_count, r.footprint) for r in list(d.values())[:3]])
        fd = fetch_footprints(k, fps)
        rec.note(f"{demo}_project_footprint_detail", [(r.lib_id, r.description[:40], r.pad_count) for r in list(fd.values())[:3]])
        # placing from the project library
        sch = k.get_schematic()
        c = sch.begin_commit()
        try:
            inst = sch.place_symbol_from_library(syms[0], Vector2.from_xy_mm(250, 250))
            rec.check(f"{demo}_place_from_project_lib", True, f"{syms[0]} -> pins {len(inst.definition.pins)}")
        except Exception as e:  # noqa: BLE001
            rec.check(f"{demo}_place_from_project_lib", False, f"{type(e).__name__}: {e}")
        finally:
            sch.drop_commit(c)
        # stock libs still there next to project libs; order of the status listing
        names = [x.nickname for x in k.get_library_statuses(LibraryType.LT_SYMBOL)]
        rec.note(f"{demo}_symbol_lib_order_head", names[:5] + ["..."] + [f"{sym_nick}@{names.index(sym_nick)}/{len(names)}"])
        rec.check(f"{demo}_stock_still_loaded", len(list(k.get_library_symbols(["Device"]))) > 500, "Device lib")
        rec.note(f"{demo}_quirk", quirk)


def main() -> int:
    rec = Recorder("q04_project_libs")
    for demo, (sn, fn, quirk) in CASES.items():
        try:
            one(rec, demo, sn, fn, quirk)
        except Exception as e:  # noqa: BLE001
            rec.check(f"{demo}_crashed", False, f"{type(e).__name__}: {e}")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
