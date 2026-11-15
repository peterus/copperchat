"""Q2: create a new, empty .pretty library. What does the API do, what is left outside it?"""

import tempfile
from pathlib import Path

from kipy.proto.common.commands import library_commands_pb2 as lc
from kipy.proto.common.types import library_types_pb2 as lt

from fpdoc import FootprintDoc
from harness import Recorder, Server, main_guard
from repo import LIB, LIB_URI, make_repo
from steps import SEED, open_sch


def status(k, nick: str):
    s = [x for x in k.get_library_statuses() if x.nickname == nick]
    return (lt.LibraryLoadStatus.Name(s[0].status), s[0].error_message) if s else None


def main() -> int:
    r = Recorder("q02_library")
    # 1) the table commands themselves
    repo = make_repo(Path(tempfile.mkdtemp(prefix="c3a-q2-")), board=False, table=False)
    with Server(repo.main_pro) as srv:
        k = srv.client(60000)
        c = k._client
        open_sch(k, repo.main_pro)
        add = lc.AddLibraryTableEntry(scope=lt.LTS_PROJECT)
        add.entry.nickname, add.entry.uri, add.entry.type = LIB, LIB_URI, lt.LT_FOOTPRINT
        cmds = {
            "GetLibraryTable": (lc.GetLibraryTable(type=lt.LT_FOOTPRINT, scope=lt.LTS_PROJECT), lc.LibraryTableResponse),
            "AddLibraryTableEntry": (add, lt.LibraryCommandStatus),
            "UpdateLibraryTableEntry": (lc.UpdateLibraryTableEntry(entry=add.entry, scope=lt.LTS_PROJECT), lt.LibraryCommandStatus),
            "DeleteLibraryTableEntry": (lc.DeleteLibraryTableEntry(type=lt.LT_FOOTPRINT, nickname=LIB, scope=lt.LTS_PROJECT), lt.LibraryCommandStatus),
        }
        for name, (cmd, resp) in cmds.items():
            try:
                c.send(cmd, resp)
                r.check(f"{name}_no_handler", False, "answered")
            except Exception as e:  # noqa: BLE001
                r.check(f"{name}_no_handler", "no handler available" in str(e), str(e))
        r.check("no_table_file_written", not (repo.main_pro.parent / "fp-lib-table").exists(),
                sorted(p.name for p in repo.main_pro.parent.iterdir()))
        # without a table row, saving into the nickname fails
        seed = FootprintDoc(k, SEED)
        try:
            seed.save_copy(f"{LIB}:X")
            r.check("save_without_table_row_fails", False, "saved")
        except Exception as e:  # noqa: BLE001
            r.check("save_without_table_row_fails", "failed to save" in str(e), str(e))
        seed.close()

    # 2) with the (stand-in) table row: missing dir / empty dir / reload
    repo = make_repo(Path(tempfile.mkdtemp(prefix="c3a-q2b-")), board=False)
    with Server(repo.main_pro) as srv:
        k = srv.client(60000)
        open_sch(k, repo.main_pro)
        r.note("status_row_dir_missing", status(k, LIB))
        seed = FootprintDoc(k, SEED)
        try:
            seed.save_copy(f"{LIB}:X")
            r.check("save_into_missing_dir_fails", False, "saved")
        except Exception as e:  # noqa: BLE001
            r.check("save_into_missing_dir_fails", "failed to save" in str(e), str(e))
        r.check("missing_dir_not_created", not repo.lib_dir.exists(), repo.lib_dir.exists())

        repo.lib_dir.mkdir()  # the one remaining non-API step: an empty directory
        r.note("status_empty_dir_before_reload", status(k, LIB))
        try:
            seed.save_copy(f"{LIB}:X")
            r.check("save_after_mkdir_needs_reload", False, "saved without reload")
        except Exception as e:  # noqa: BLE001
            r.check("save_after_mkdir_needs_reload", "failed to save" in str(e), str(e))
        rel = lc.ReloadLibrary(type=lt.LT_FOOTPRINT, scope=lt.LTS_PROJECT, nickname=[LIB])
        r.note("reload_result", str(k._client.send(rel, lt.LibraryCommandStatus)).strip())
        r.note("status_after_reload", status(k, LIB))
        seed.save_copy(f"{LIB}:X")
        r.check("save_into_empty_dir_after_reload_ok", (repo.lib_dir / "X.kicad_mod").exists(),
                sorted(p.name for p in repo.lib_dir.iterdir()))
        r.note("status_after_save", status(k, LIB))
        names = [i.proto.entry_name for i in k.get_library_footprints([LIB])]
        r.check("listed_after_save", names == ["X"], names)
        seed.close()
        # undo of a created footprint: there is no DeleteFootprint/DeleteLibraryItem command
        r.check("no_delete_library_item_command",
                not any(n for n in dir(lc) if "Delete" in n and "Table" not in n), [n for n in dir(lc) if "Delete" in n])
        # overwrite: SaveCopy onto an existing name replaces it silently?
        seed = FootprintDoc(k, "Inductor_SMD:L_Bourns-SRN6028")
        before = (repo.lib_dir / "X.kicad_mod").read_text()
        try:
            seed.save_copy(f"{LIB}:X")
            after = (repo.lib_dir / "X.kicad_mod").read_text()
            r.check("save_copy_overwrites_existing_silently", before != after and "SRN6028" in after,
                    "content replaced" if before != after else "unchanged")
        except Exception as e:  # noqa: BLE001
            r.check("save_copy_overwrites_existing_silently", False, str(e))
        seed.close()
        r.check("server_alive", srv.alive(), srv.log()[-300:])
    return r.finish()


if __name__ == "__main__":
    main_guard(main)
