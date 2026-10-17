"""Q7: new hierarchical sheet via the API: file written on save, page number, placing
on the new sheet, sheet pin <-> hierarchical label connectivity, loadable by kicad-cli;
sheet page numbers of an existing hierarchy (per-sheet annotation, spec 5.5)."""

import json
import tempfile
from pathlib import Path

from geom import pin_ends_abs
from harness import (
    Recorder,
    Server,
    by_id,
    cli_netlist,
    commit,
    fresh_fixture,
    keep_artifact,
    load_libraries,
    main_guard,
    mm,
    nets_by_pin,
    require_kicad,
    root_sch,
    run_cli,
    sid,
    v2,
    wire,
)


def new_sheet(name, filename, at, size):
    from kipy.schematic_types import SheetSymbol

    s = SheetSymbol()
    s.name_field.name = "Sheetname"
    s.name_field.text.value = name
    s.name_field.text.position = v2(at[0], at[1] - 1.27)
    s.filename_field.name = "Sheetfile"
    s.filename_field.text.value = filename
    s.filename_field.text.position = v2(at[0], at[1] + size[1] + 1.27)
    s.position = v2(*at)
    s.size = v2(*size)
    return s


def sheet_pin(text, at, side):
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLabelShape
    from kipy.schematic_types import SheetPin

    p = SheetPin()
    p.text.value = text
    p.position = v2(*at)
    p.text.position = v2(*at)
    p.side = side
    p.shape = SchematicLabelShape.SLSH_INPUT
    return p


def hier_label(text, at):
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLabelShape
    from kipy.schematic_types import HierarchicalLabel

    h = HierarchicalLabel()
    h.text.value = text
    h.position = v2(*at)
    h.text.position = v2(*at)
    h.shape = SchematicLabelShape.SLSH_INPUT
    return h


def add_sheet(sch, name, filename, at, size, project_dir: Path, one_commit: bool = False):
    """Workaround for the relative-filename save failure: create the sheet with an absolute
    file path (so KiCad resolves the new screen's file correctly), then set the Sheetfile
    field back to the project-relative name."""
    with commit(sch, "add sheet"):
        sh = sch.create_items([new_sheet(name, str(project_dir / filename), at, size)])[0]
        if one_commit:
            sh.filename_field.text.value = filename
            sch.update_items([sh])
    if not one_commit:
        cur = by_id(sch, [sid(sh)])[sid(sh)]
        cur.filename_field.text.value = filename
        with commit(sch, "relative sheet file"):
            sch.update_items([cur])
    return by_id(sch, [sid(sh)])[sid(sh)]


def page_map(sch) -> dict[str, str]:
    return {str(s.path.proto.path_human_readable): s.page_number for s in sch.get_hierarchy()}


def main() -> int:
    require_kicad()
    from kipy.proto.schematic.schematic_types_pb2 import SheetSide

    rec = Recorder("q07_sheets")
    tmp = Path(tempfile.mkdtemp(prefix="s2a-q07-"))
    # the obvious way: Sheetfile relative to the parent, as KiCad writes it
    proj = fresh_fixture("blank")
    with Server(proj) as srv:
        sch = srv.client(timeout_ms=60000).get_schematic()
        with commit(sch, "add sheet"):
            sch.create_items([new_sheet("Power", "power.kicad_sch", (127.0, 50.8), (25.4, 25.4))])
        rec.attempt("relative_sheetfile_new_sheet_save", sch.save)
        rec.note("relative_sheetfile_files_after_save", sorted(p.name for p in proj.iterdir()))
    # create + field fix inside ONE commit (the staged sheet is not yet resolvable)
    proj = fresh_fixture("blank")
    with Server(proj) as srv:
        sch = srv.client(timeout_ms=60000).get_schematic()
        try:
            got = add_sheet(sch, "Power", "power.kicad_sch", (127.0, 50.8), (25.4, 25.4), proj, one_commit=True).filename_field.text.value
        except Exception as e:  # noqa: BLE001
            got = f"{type(e).__name__}: {e}"
        rec.check("workaround_field_fix_inside_create_commit", got == "power.kicad_sch", got)

    proj = fresh_fixture("blank")
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        load_libraries(k)
        sh = add_sheet(sch, "Power", "power.kicad_sch", (127.0, 50.8), (25.4, 25.4), proj)
        rec.check("workaround_sheetfile_relative_after_fix", sh.filename_field.text.value == "power.kicad_sch" and [s.filename for s in sch.get_hierarchy()][1] == "power.kicad_sch", [s.filename for s in sch.get_hierarchy()])
        hier = list(sch.get_hierarchy())
        rec.check("new_sheet_in_hierarchy", any(s.filename == "power.kicad_sch" for s in hier), [(s.name, s.filename, s.page_number) for s in hier])
        rec.check("new_sheet_page_number_assigned", page_map(sch).get("/Power/") == "2", page_map(sch))
        rec.check("new_sheet_symbol_page_number", by_id(sch, [sid(sh)])[sid(sh)].page_number == "2", by_id(sch, [sid(sh)])[sid(sh)].page_number)
        inst = next(s for s in sch.get_hierarchy() if s.filename == "power.kicad_sch")
        sub = sch.for_sheet(inst)

        # sheet pin on the sheet symbol (pins are part of the SheetSymbol item)
        cur = by_id(sch, [sid(sh)])[sid(sh)]
        cur.pins = [sheet_pin("VIN", (127.0, 63.5), SheetSide.SHS_LEFT)]
        with commit(sch, "sheet pin"):
            upd = sch.update_items([cur])[0]
        rec.check("sheet_pin_added", [p.text.value for p in upd.pins] == ["VIN"], [(p.text.value, mm(p.position)) for p in upd.pins])
        # part + hierarchical label on the new sheet
        rs = sub.place_symbol_from_library("Device:R", v2(76.2, 76.2), reference="R201")
        rec.check("place_on_new_sheet_path", str(rs.path.proto.path_human_readable) == "/Power/" or len(rs.path.proto.path) == 2, rs.path.proto)
        prs = pin_ends_abs(rs)
        with commit(sub, "hier label"):
            hl = sub.create_items([hier_label("VIN", prs["1"])])[0]
        rec.check("hier_label_on_sub_sheet", sid(hl) in by_id(sub, [sid(hl)]) and not any(lb.text.value == "VIN" for lb in sch.for_sheet(hier[0]).get_labels()), "label lives on the sub-sheet only")
        # root: R1 wired to the sheet pin
        r1 = sch.for_sheet(hier[0]).place_symbol_from_library("Device:R", v2(101.6, 63.5 - 3.81), reference="R1")
        p1 = pin_ends_abs(r1)
        with commit(sch, "wire"):
            sch.create_items([wire(p1["2"], (127.0, 63.5))])
        nets = nets_by_pin(sch)
        rec.check("sheet_pin_hier_label_connect_api", nets.get(("R1", "2")) is not None and nets.get(("R1", "2")) == nets.get(("R201", "1")), (nets.get(("R1", "2")), nets.get(("R201", "1"))))
        rec.check("no_sub_file_before_save", not (proj / "power.kicad_sch").exists(), sorted(p.name for p in proj.iterdir()))
        sch.save()
        rec.check("new_sheet_file_written_on_save", (proj / "power.kicad_sch").exists(), sorted(p.name for p in proj.iterdir()))
        cli = cli_netlist(root_sch(proj), tmp)
        rec.check("sheet_pin_hier_label_connect_cli", cli.get(("R1", "2")) is not None and cli.get(("R1", "2")) == cli.get(("R201", "1")), (cli.get(("R1", "2")), cli.get(("R201", "1"))))
        erc = tmp / "erc.json"
        run_cli("sch", "erc", "--format", "json", "--units", "mm", "-o", str(erc), str(root_sch(proj)))
        vios = [v for s in json.loads(erc.read_text())["sheets"] for v in s["violations"]]
        rec.check("erc_no_hierarchy_mismatch", not any(v["type"] in ("hier_label_mismatch", "pin_not_connected") and "VIN" in json.dumps(v) for v in vios), sorted({v["type"] for v in vios}))
        keep_artifact(proj, "q07_sheets")
        # the saved project is relocatable: load a copy elsewhere
        import shutil

        moved = Path(tempfile.mkdtemp(prefix="s2a-q07-moved-")) / "blank"
        shutil.copytree(proj, moved, ignore=shutil.ignore_patterns("*.lck"))
        cli2 = cli_netlist(root_sch(moved), tmp)
        rec.check("relocated_copy_loads_sub_sheet", cli2.get(("R201", "1")) == cli.get(("R201", "1")) is not None, cli2.get(("R201", "1")))

    # reopen: hierarchy, sub-sheet content and page numbers persist
    with Server(proj) as srv:
        sch = srv.client(timeout_ms=60000).get_schematic()
        rec.check("reopen_hierarchy_and_pages", page_map(sch) == {"/": "1", "/Power/": "2"}, page_map(sch))
        rec.check("reopen_sub_sheet_symbol", "R201" in [s.reference for s in sch.get_symbols()], [s.reference for s in sch.get_symbols()])

    # existing hierarchy: page numbers per sheet instance for annotation blocks
    proj = fresh_fixture("complex_hierarchy")
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        pm = page_map(sch)
        rec.check("existing_page_numbers", len(pm) == 3 and all(pm.values()), pm)
        by_path = {"/".join(p.value for p in s.path.proto.path): s.page_number for s in sch.get_hierarchy()}
        syms = [s for s in sch.get_symbols() if not s.reference.startswith("#")]
        mapped = {s.reference: by_path.get("/".join(p.value for p in s.path.proto.path)) for s in syms}
        rec.check("symbol_instance_path_to_page_number", all(mapped.values()), dict(list(mapped.items())[:6]))
        rec.note("refs_per_page", {pg: sorted(r for r, p in mapped.items() if p == pg)[:5] for pg in sorted(set(mapped.values()))})
        sheets = sch.get_sheet_symbols()
        rec.check("sheet_symbols_expose_page_number", all(s.page_number for s in sheets), [(s.name_field.text.value, s.page_number) for s in sheets])
        # a new sheet in an existing hierarchy gets the next free page number
        load_libraries(k)
        add_sheet(sch, "Extra", "extra.kicad_sch", (25.4, 152.4), (25.4, 20.32), proj)
        rec.check("new_sheet_next_page_number", page_map(sch).get("/Extra/") == "4", page_map(sch))
        sch.save()
        rec.check("new_sheet_file_written_in_existing_project", (proj / "extra.kicad_sch").exists(), sorted(p.name for p in proj.glob("*.kicad_sch")))
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
