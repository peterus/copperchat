"""Q7: after save (+ ReloadLibrary), does the schematic side see the new footprint
(Footprint field, ERC footprint_link_issues) and does a board find it?"""

import json
import tempfile
from pathlib import Path

from kipy.geometry import Vector2
from kipy.proto.common.types import DocumentType

import l201
from harness import Recorder, Server, cli, main_guard
from repo import LIB, make_repo
from steps import create_l201, open_sch

FPID = f"{LIB}:{l201.NAME}"


def set_footprint(sch, ref: str, fpid: str) -> None:
    sym = next(s for s in sch.get_symbols() if s.reference_field.text.value == ref)
    sym.footprint_field.text.value = fpid
    sym.proto.ClearField("definition")  # spike 2A: never update with the definition
    sch.update_items(sym)


def erc(sch_path: Path, cfg: Path) -> dict:
    out = sch_path.parent / "erc.json"
    p = cli("sch", "erc", "--format", "json", "--severity-all", "-o", str(out), str(sch_path), config_home=cfg)
    assert p.returncode in (0, 5), (p.returncode, p.stderr)
    return json.loads(out.read_text())


def link_issues(report: dict) -> list[str]:
    out = []
    for sheet in report.get("sheets", []):
        for v in sheet.get("violations", []):
            if v.get("type") == "footprint_link_issues":
                out.append(v.get("description", "") + " | " + "; ".join(i.get("description", "") for i in v.get("items", [])))
    return out


def main() -> int:
    r = Recorder("q07_assign")
    repo = make_repo(Path(tempfile.mkdtemp(prefix="c3a-q7-")))
    repo.lib_dir.mkdir()
    sch_path = repo.main_pro.with_suffix(".kicad_sch")
    with Server(repo.main_pro) as srv:
        k = srv.client(60000)
        open_sch(k, repo.main_pro)
        k.open_document(str(repo.main_pro.with_suffix(".kicad_pcb")), DocumentType.DOCTYPE_PCB)
        sch, board = k.get_schematic(), k.get_board()
        k.get_library_symbol("Device:L")
        sch.place_symbol_from_library("Device:L", Vector2.from_xy_mm(100, 100), reference="L201")
        sch.place_symbol_from_library("Device:L", Vector2.from_xy_mm(120, 100), reference="L202")
        create_l201(k)
        set_footprint(sch, "L201", FPID)
        set_footprint(sch, "L202", f"{LIB}:Does_Not_Exist")  # negative control
        fps = {s.reference_field.text.value: s.footprint_field.text.value for s in sch.get_symbols()}
        r.check("footprint_field_set", fps == {"L201": FPID, "L202": f"{LIB}:Does_Not_Exist"}, fps)
        sch.save()

        # the board of the same server finds the new footprint right away (shared adapter, no reload)
        c = board.begin_commit()
        placed = board.place_footprint_from_library(FPID, Vector2.from_xy_mm(50, 50))
        pads = sorted((p.number, round(p.position.x / 1e6 - 50, 4)) for p in placed.definition.pads)
        r.check("board_places_new_footprint", pads == [("1", -1.1), ("2", 1.1)], pads)
        board.drop_commit(c)
        r.check("board_drop_exact", len(board.get_items()) == 0, len(board.get_items()))
        r.check("server_alive", srv.alive(), srv.log()[-300:])

    # ERC (separate kicad-cli process = fresh library load, like CopperChat's ERC job)
    report = erc(sch_path, Path(tempfile.mkdtemp(prefix="c3a-q7-cfg-")))
    issues = link_issues(report)
    r.note("footprint_link_issues", issues)
    r.check("erc_clean_for_L201", not any("L201" in i for i in issues), issues)
    r.check("erc_flags_negative_control_L202", any("L202" in i or "Does_Not_Exist" in i for i in issues), issues)
    r.note("erc_violation_types", sorted({v.get("type") for s in report.get("sheets", []) for v in s.get("violations", [])}))

    # a board opened afterwards (fresh server, board only): like stock footprints, the library must
    # be loaded first (spike 2B) — GetItemsFromLibrary loads that one library lazily
    with Server(repo.main_pro) as srv:
        k = srv.client(60000)
        k.open_document(str(repo.main_pro.with_suffix(".kicad_pcb")), DocumentType.DOCTYPE_PCB)
        board = k.get_board()
        c = board.begin_commit()
        try:
            board.place_footprint_from_library(FPID, Vector2.from_xy_mm(50, 50))
            r.check("fresh_board_place_needs_loaded_library", False, "placed cold")
        except Exception as e:  # noqa: BLE001
            r.check("fresh_board_place_needs_loaded_library", "not found" in str(e), str(e))
        d = k.get_library_footprint(FPID)
        r.check("fresh_board_reads_definition", len(d.pads) == 2, len(d.pads))
        placed = board.place_footprint_from_library(FPID, Vector2.from_xy_mm(50, 50))
        r.check("fresh_board_places_new_footprint", len(placed.definition.pads) == 2, len(placed.definition.pads))
        board.drop_commit(c)
        r.note("fresh_board_rss_mb", round(srv.rss()))
    return r.finish()


if __name__ == "__main__":
    main_guard(main)
