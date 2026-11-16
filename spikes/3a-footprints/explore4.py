"""Exploration: when does an open board's PlaceFootprintFromLibrary see a just-created footprint?"""

import tempfile
from pathlib import Path

from kipy.geometry import Vector2
from kipy.proto.common.commands import library_commands_pb2 as lc
from kipy.proto.common.types import DocumentType, library_types_pb2 as lt

import l201
from harness import Server
from repo import LIB, make_repo
from steps import create_l201, open_sch

FPID = f"{LIB}:{l201.NAME}"


def place(k, label):
    b = k.get_board()
    c = b.begin_commit()
    try:
        fp = b.place_footprint_from_library(FPID, Vector2.from_xy_mm(50, 50))
        print(f"OK   {label}: {len(fp.definition.pads)} pads")
    except Exception as e:  # noqa: BLE001
        print(f"ERR  {label}: {e}")
    finally:
        b.drop_commit(c)


def reload(k, scope=lt.LTS_PROJECT):
    print("     reload", k._client.send(lc.ReloadLibrary(type=lt.LT_FOOTPRINT, scope=scope, nickname=[LIB]),
                                         lt.LibraryCommandStatus).code)


if __name__ == "__main__":
    for variant in ("board_before_create_then_sch_save", "board_before_create_sch_place_save"):
        repo = make_repo(Path(tempfile.mkdtemp(prefix="c3a-x4-")))
        repo.lib_dir.mkdir()
        pcb = str(repo.main_pro.with_suffix(".kicad_pcb"))
        with Server(repo.main_pro) as srv:
            k = srv.client(60000)
            open_sch(k, repo.main_pro)
            k.open_document(pcb, DocumentType.DOCTYPE_PCB)
            if "sch_place" in variant:
                k.get_library_symbol("Device:L")
                k.get_schematic().place_symbol_from_library("Device:L", Vector2.from_xy_mm(100, 100), reference="L201")
            create_l201(k)
            place(k, f"{variant}: before sch save")
            k.get_schematic().save()
            place(k, f"{variant}: plain")
            reload(k)
            place(k, f"{variant}: after project reload")
            print("     names", [i.proto.entry_name for i in k.get_library_footprints([LIB])])
            k.close_document(k.get_board().document)
            k.open_document(pcb, DocumentType.DOCTYPE_PCB)
            place(k, f"{variant}: after board close/reopen")
