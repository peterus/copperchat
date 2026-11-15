"""Q3: library location B — one library at the repo root, used by two projects in sub-folders
through a ${KIPRJMOD}-relative row in each project's fp-lib-table (stand-in, see repo.py).
Two api-servers (one per project) run at the same time, as CopperChat would run them."""

import tempfile
from pathlib import Path

from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.commands import library_commands_pb2 as lc
from kipy.proto.common.types import library_types_pb2 as lt

import l201
from fpdoc import FootprintDoc, kind, mm
from harness import Recorder, Server, main_guard
from repo import LIB, make_repo
from steps import create_l201, open_sch

FPID = f"{LIB}:{l201.NAME}"


def marker(text: str) -> bt.BoardText:
    t = l201.fab_ref_text()
    t.text.text = text
    t.text.position.y_nm = mm(-1.0)
    return t


def texts(fp: FootprintDoc) -> list[str]:
    return [i.text.text for i in fp.items() if kind(i) == "BoardText"]


def main() -> int:
    r = Recorder("q03_shared")
    repo = make_repo(Path(tempfile.mkdtemp(prefix="c3a-q3-")), board=False)
    repo.lib_dir.mkdir()
    tables = {p: (p.parent / "fp-lib-table").read_text() for p in (repo.main_pro, repo.io_pro)}
    with Server(repo.main_pro) as main_srv, Server(repo.io_pro) as io_srv:
        km, ki = main_srv.client(60000), io_srv.client(60000)
        open_sch(km, repo.main_pro)
        open_sch(ki, repo.io_pro)
        create_l201(km)
        r.check("created_at_repo_root", (repo.lib_dir / f"{l201.NAME}.kicad_mod").exists(),
                sorted(str(p.relative_to(repo.root)) for p in repo.root.rglob("*.kicad_mod")))
        # io has only its schematic open: the footprint library handler is not registered yet
        try:
            ki.get_library_footprints([LIB])
            r.check("io_fp_listing_needs_handler", False, "answered")
        except Exception as e:  # noqa: BLE001
            r.check("io_fp_listing_needs_handler", "no handler available" in str(e), str(e))
        # opening the footprint document loads io's footprint table (incl. the ${KIPRJMOD}/../.. row)
        fpi = FootprintDoc(ki, FPID)
        st = [s for s in ki.get_library_statuses() if s.nickname == LIB]
        r.note("io_status", [(lt.LibraryLoadStatus.Name(s.status), s.error_message) for s in st])
        names_io = [i.proto.entry_name for i in ki.get_library_footprints([LIB])]
        r.check("io_lists_shared_lib", l201.NAME in names_io, names_io)
        # io edits the shared footprint
        g = l201.geometry(fpi.items())
        r.check("io_reads_same_geometry", [p["pos"] for p in g["pads"]] == [(-1.1, 0.0), (1.1, 0.0)], g["pads"])
        cid = fpi.begin()
        fpi.create([marker("edited-by-io")])
        fpi.end(cid)
        fpi.save()
        fpi.close()

        # main sees the edit? (main still has its library cache from the create)
        fpm = FootprintDoc(km, FPID)
        before_reload = texts(fpm)
        fpm.close()
        r.note("main_texts_without_reload", before_reload)
        km._client.send(lc.ReloadLibrary(type=lt.LT_FOOTPRINT, scope=lt.LTS_PROJECT, nickname=[LIB]),
                        lt.LibraryCommandStatus)
        fpm = FootprintDoc(km, FPID)
        after_reload = texts(fpm)
        r.check("main_sees_io_edit_after_reload", "edited-by-io" in after_reload, after_reload)
        # a stale main save would overwrite io's edit (last writer wins, no lock)
        fpm.close()
        r.check("both_servers_alive", main_srv.alive() and io_srv.alive(), "")
    r.check("tables_untouched", all((p.parent / "fp-lib-table").read_text() == t for p, t in tables.items()), "")
    r.check("no_lib_copies_in_projects", not list(repo.root.glob("hardware/**/*.pretty")),
            list(repo.root.glob("hardware/**/*.pretty")))
    return r.finish()


if __name__ == "__main__":
    main_guard(main)
