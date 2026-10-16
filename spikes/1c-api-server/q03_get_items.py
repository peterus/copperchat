"""Q3: GetItems coverage on schematic (incl. hierarchy) and board; UUID format; units."""

import re

from harness import Recorder, Server, fresh_fixture, main_guard, require_kicad, run_cli

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def first_uuid(items) -> str:
    for it in items:
        ident = getattr(it, "id", None)
        if ident is not None:
            return str(getattr(ident, "value", ident))
    return ""


def probe(rec, name, fn, need_nonempty=True):
    try:
        items = list(fn())
    except Exception as e:  # noqa: BLE001
        return rec.check(name, False, f"{type(e).__name__}: {e}"), []
    ok = len(items) > 0 if need_nonempty else True
    rec.check(name, ok, f"count={len(items)} sample={str(items[:1])[:200]}")
    return ok, items


def main() -> int:
    require_kicad()
    rec = Recorder("q03_get_items")
    with Server(fresh_fixture("ecc83")) as srv:
        k = srv.client()
        sch = k.get_schematic()
        _, syms = probe(rec, "sch_symbols", sch.get_symbols)
        probe(rec, "sch_labels", sch.get_labels, need_nonempty=False)
        probe(rec, "sch_wires", sch.get_lines)
        try:
            net = sch.get_netlist()
            rec.check("sch_netlist", net is not None, str(net)[:300])
        except Exception as e:  # noqa: BLE001
            rec.check("sch_netlist", False, f"{type(e).__name__}: {e}")
        u = first_uuid(syms)
        rec.check("uuid_full_36_chars", bool(UUID_RE.match(u)), u)
        if syms:
            rec.note("sch_symbol_sample", syms[0])
            rec.note("sch_position_raw", getattr(syms[0], "position", None))

        board = k.get_board()
        _, fps = probe(rec, "pcb_footprints", board.get_footprints)
        probe(rec, "pcb_pads", board.get_pads)
        probe(rec, "pcb_tracks", board.get_tracks)
        probe(rec, "pcb_vias", board.get_vias, need_nonempty=False)
        probe(rec, "pcb_nets", board.get_nets)
        try:
            stack = board.get_stackup()
            rec.check("pcb_stackup", stack is not None, str(stack)[:300])
        except Exception as e:  # noqa: BLE001
            rec.check("pcb_stackup", False, f"{type(e).__name__}: {e}")
        layers = list(board.get_enabled_layers())
        rec.check("pcb_enabled_layers", len(layers) >= 2, layers)
        rec.check("pcb_uuid_full", bool(UUID_RE.match(first_uuid(fps))), first_uuid(fps))
        if fps:
            rec.note("pcb_footprint_ref", fps[0].reference_field.text.value)
            rec.note("pcb_position_raw_nm", fps[0].position)

    with Server(fresh_fixture("complex_hierarchy")) as srv:
        sch = srv.client().get_schematic()
        try:
            hier = sch.get_hierarchy()
            rec.check("sch_hierarchy", hier is not None, str(hier)[:300])
            rec.note("hierarchy", str(hier)[:2000])
        except Exception as e:  # noqa: BLE001
            rec.check("sch_hierarchy", False, f"{type(e).__name__}: {e}")
        _, hsyms = probe(rec, "sch_symbols_all_sheets", sch.get_symbols)
        refs = sorted(
            {s.reference for s in hsyms} if hsyms and hasattr(hsyms[0], "reference") else []
        )
        rec.note("symbols_scope", f"returned {len(hsyms)} symbols; refs={refs[:80]}")
        real = [s for s in hsyms if not str(getattr(s, "reference", "#")).startswith("#")]
        rec.note("non_power_symbol_instances", len(real))
        rec.note("symbol_instance_paths", sorted({str(s.path) for s in hsyms}))
        probe(rec, "sch_labels_hier", sch.get_labels)
        # Ground truth for the symbol count across all sheet instances: kicad-cli BOM-less
        # netlist export of the same file.
        sch_file = next(srv.project_dir.glob("complex_hierarchy.kicad_sch"))
        r = run_cli(
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "-o",
            str(srv.project_dir / "net.xml"),
            str(sch_file),
        )
        if r.returncode == 0:
            xml = (srv.project_dir / "net.xml").read_text()
            n = xml.count("<comp ref=")
            rec.note("cli_netlist_component_count", n)
            # One SchematicSymbolInstance per placed unit: multi-unit parts appear per unit.
            uniq = {s.reference for s in real}
            rec.check(
                "unique_refs_match_cli_netlist",
                len(uniq) == n,
                f"api {len(real)} unit instances / {len(uniq)} refs vs cli {n}",
            )
        else:
            rec.check(
                "unique_refs_match_cli_netlist",
                False,
                f"kicad-cli netlist failed rc={r.returncode}: {r.stderr[-300:]}",
            )
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
