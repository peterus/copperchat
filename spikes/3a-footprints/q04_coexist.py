"""Q4: footprint document next to the open schematic and board in ONE api-server.
Schematic commits (begin/drop/push) interleaved with footprint edits; board untouched;
the one-footprint-document bookkeeping of the api-server."""

import tempfile
from pathlib import Path

from kipy.geometry import Vector2
from kipy.proto.common.types import DocumentType

import l201
from fpdoc import FootprintDoc, kind
from harness import Recorder, Server, main_guard
from repo import LIB, make_repo
from steps import SEED, create_l201, open_sch

FPID = f"{LIB}:{l201.NAME}"


def main() -> int:
    r = Recorder("q04_coexist")
    repo = make_repo(Path(tempfile.mkdtemp(prefix="c3a-q4-")))
    repo.lib_dir.mkdir()
    with Server(repo.main_pro) as srv:
        k = srv.client(60000)
        open_sch(k, repo.main_pro)
        k.open_document(str(repo.main_pro.with_suffix(".kicad_pcb")), DocumentType.DOCTYPE_PCB)
        sch, board = k.get_schematic(), k.get_board()
        k.get_library_symbol("Device:L")  # loads the Device library lazily (spike 2B)
        board_items_before = len(board.get_items())

        # a schematic commit stays open while a footprint is created and saved
        commit = sch.begin_commit()
        sym = sch.place_symbol_from_library("Device:L", Vector2.from_xy_mm(100, 100), reference="L201")
        create_l201(k)
        r.check("fp_created_during_open_sch_commit", (repo.lib_dir / f"{l201.NAME}.kicad_mod").exists(), "")
        sch.drop_commit(commit)
        left = [s for s in sch.get_symbols() if s.id.value == sym.id.value]
        r.check("sch_drop_after_fp_save_exact", left == [], f"{len(left)} left")

        # push path: footprint document open while the schematic commit is pushed
        fp = FootprintDoc(k, FPID)
        commit = sch.begin_commit()
        sym = sch.place_symbol_from_library("Device:L", Vector2.from_xy_mm(100, 100), reference="L201")
        cid = fp.begin()
        fp.create([l201.fab_ref_text()])
        sch.push_commit(commit, "place L201")
        fp.end(cid, drop=True)  # dropped footprint commit next to a pushed schematic commit
        r.check("sch_push_with_fp_commit_open", any(s.id.value == sym.id.value for s in sch.get_symbols()), "")
        r.check("fp_drop_exact", sum(kind(i) == "BoardText" for i in fp.items()) == 1,
                [i.text.text for i in fp.items() if kind(i) == "BoardText"])
        fp.close()
        sch.save()
        r.check("sch_still_usable_after_fp_close", len(sch.get_symbols()) == 1, len(sch.get_symbols()))
        r.check("board_untouched", len(board.get_items()) == board_items_before,
                (board_items_before, len(board.get_items())))
        docs = {}
        for t in (DocumentType.DOCTYPE_SCHEMATIC, DocumentType.DOCTYPE_PCB, DocumentType.DOCTYPE_FOOTPRINT):
            try:
                docs[DocumentType.Name(t)] = len(k.get_open_documents(t))
            except Exception as e:  # noqa: BLE001
                docs[DocumentType.Name(t)] = str(e)
        r.note("open_docs_after_fp_close", docs)

        # bookkeeping: opening a second footprint without closing the first
        a = FootprintDoc(k, SEED)
        b = FootprintDoc(k, FPID)
        open_fp = [d.lib_id.entry_name for d in k.get_open_documents(DocumentType.DOCTYPE_FOOTPRINT)]
        r.check("only_one_fp_document", open_fp == [l201.NAME], open_fp)
        try:
            b.close()
            r.check("close_second_fails_stale_entry", False, "closed")
        except Exception as e:  # noqa: BLE001
            r.check("close_second_fails_stale_entry", "does not match" in str(e), str(e))
        try:
            a.close()
            r.check("close_first_fails_too", False, "closed")
        except Exception as e:  # noqa: BLE001
            r.check("close_first_fails_too", "does not match" in str(e), str(e))
        # the server still works: a third open replaces the footprint, schematic fine
        c = FootprintDoc(k, SEED)
        r.check("reopen_after_stale_entry", len(c.items()) > 0, len(c.items()))
        r.check("sch_fine_after_stale_entry", len(sch.get_symbols()) == 1, "")
        # editing the stock seed and saving it: refused — KiCad's read-only check or just file
        # permissions (the probe runs as a non-root user, /usr/share/kicad is root-owned)?
        cid = c.begin()
        c.create([l201.fab_ref_text()])
        c.end(cid)
        try:
            c.save()
            r.check("save_into_stock_library_refused", False, "saved into stock library")
        except Exception as e:  # noqa: BLE001
            r.check("save_into_stock_library_refused", True, str(e))
            import os
            r.note("stock_lib_dir_writable_by_probe_user",
                   os.access("/usr/share/kicad/footprints/Inductor_SMD.pretty", os.W_OK))
            r.note("server_log_tail", srv.log()[-600:])
        r.check("server_alive", srv.alive(), srv.log()[-400:])
        r.note("rss_mb", srv.rss())
    return r.finish()


if __name__ == "__main__":
    main_guard(main)
