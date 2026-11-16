"""Q6: which kipy wrappers (fork ab07e98 = upstream 76b47d25 + #147 fix) work for footprint
work, and which calls need raw protobuf. Each wrapper is called once; the outcome is recorded."""

import tempfile
from pathlib import Path

from kipy.board import Board
from kipy.board_types import BoardRectangle, Pad
from kipy.geometry import Vector2
from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.types import DocumentType, enums_pb2 as en, library_types_pb2 as lt

import l201
from harness import Recorder, Server, main_guard
from repo import LIB, make_repo
from steps import SEED, create_l201, open_sch

FPID = f"{LIB}:{l201.NAME}"


def outcome(fn):
    try:
        v = fn()
        return True, (str(v)[:160] if v is not None else "ok")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def main() -> int:
    r = Recorder("q06_kipy")
    repo = make_repo(Path(tempfile.mkdtemp(prefix="c3a-q6-")), board=False)
    repo.lib_dir.mkdir()
    table: dict[str, tuple[bool, str]] = {}
    with Server(repo.main_pro) as srv:
        k = srv.client(60000)
        open_sch(k, repo.main_pro)
        create_l201(k)
        doc = k.open_document(FPID, DocumentType.DOCTYPE_FOOTPRINT)
        table["KiCad.open_document(lib_id, DOCTYPE_FOOTPRINT)"] = (True, "ok")
        table["KiCad.get_open_documents(DOCTYPE_FOOTPRINT)"] = outcome(
            lambda: len(k.get_open_documents(DocumentType.DOCTYPE_FOOTPRINT)))
        fp = Board(k._client, doc)
        table["Board(doc).get_items()"] = outcome(lambda: len(fp.get_items()))
        table["Board(doc).get_items([pad, shape])"] = outcome(
            lambda: len(fp.get_items([en.KOT_PCB_PAD, en.KOT_PCB_SHAPE])))
        table["Board(doc).get_items([field])"] = outcome(lambda: len(fp.get_items([en.KOT_PCB_FIELD])))
        pad = fp.get_items([en.KOT_PCB_PAD])[0]
        table["Board(doc).get_items_by_id(footprint id)"] = outcome(
            lambda: type(fp.get_items_by_id([pad.proto.parent])[0]).__name__)
        c = fp.begin_commit()
        table["Board(doc).begin_commit()"] = (True, "ok")
        p2 = Pad(proto=pad.proto)
        p2.proto.id.value = ""
        p2.number = "3"
        p2.position = Vector2.from_xy_mm(0, 2.5)
        table["Board(doc).create_items(Pad)"] = outcome(lambda: type(fp.create_items([p2])[0]).__name__)
        rect = BoardRectangle()
        rect.layer = bt.BoardLayer.BL_F_Fab
        rect.top_left = Vector2.from_xy_mm(-1, -1)
        rect.bottom_right = Vector2.from_xy_mm(1, 1)
        table["Board(doc).create_items(BoardRectangle)"] = outcome(lambda: len(fp.create_items([rect])))
        pad.position = Vector2.from_xy_mm(-1.2, 0)
        table["Board(doc).update_items(Pad)"] = outcome(lambda: fp.update_items([pad])[0].position)
        table["Board(doc).remove_items(Pad)"] = outcome(lambda: fp.remove_items([pad]))
        table["Board(doc).drop_commit()"] = outcome(lambda: fp.drop_commit(c))
        table["Board(doc).save()"] = outcome(fp.save)
        table["Board(doc).save_as('copperchat:Copy')"] = outcome(lambda: fp.save_as(f"{LIB}:Copy"))
        table["Board(doc).is_document_modified()"] = outcome(fp.is_document_modified)
        table["Board(doc).get_as_string()"] = outcome(lambda: len(fp.get_as_string()))
        k.close_document(doc)
        table["KiCad.close_document(doc)"] = (True, "ok")
        table["KiCad.get_library_footprints([nick])"] = outcome(lambda: len(k.get_library_footprints([LIB])))
        table["KiCad.get_library_footprint(id)"] = outcome(lambda: len(k.get_library_footprint(FPID).pads))
        table["KiCad.get_library_statuses()"] = outcome(lambda: len(k.get_library_statuses()))
        table["KiCad.reload_library(LT_FOOTPRINT, LTS_PROJECT, [nick])"] = outcome(
            lambda: k.reload_library(lt.LT_FOOTPRINT, lt.LTS_PROJECT, [LIB]))
        table["KiCad.wait_for_libraries()"] = outcome(lambda: k.wait_for_libraries())
        table["KiCad.open_library_item(...) (GUI only)"] = outcome(
            lambda: k.open_library_item("Inductor_SMD", SEED.split(":")[1], DocumentType.DOCTYPE_FOOTPRINT))
        for name in ("add_library_table_entry", "get_library_table", "update_library_table_entry",
                     "delete_library_table_entry", "save_copy_of_document", "create_library"):
            table[f"KiCad.{name} (wrapper exists?)"] = (hasattr(k, name), "present" if hasattr(k, name) else "absent")
    for name, (ok, ev) in table.items():
        r.note(name, f"{'OK ' if ok else 'ERR'} {ev}")
    r.check("pads_and_shapes_via_wrapper", table["Board(doc).get_items([pad, shape])"][0], table["Board(doc).get_items([pad, shape])"])
    r.check("fields_break_board_wrapper", not table["Board(doc).get_items([field])"][0], table["Board(doc).get_items([field])"])
    r.check("save_as_not_usable_for_lib_ids", not table["Board(doc).save_as('copperchat:Copy')"][0]
            or not (repo.lib_dir / "Copy.kicad_mod").exists(), table["Board(doc).save_as('copperchat:Copy')"])
    r.check("no_table_wrappers", not any(v[0] for n, v in table.items() if "wrapper exists" in n), "")
    return r.finish()


if __name__ == "__main__":
    main_guard(main)
