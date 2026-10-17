"""Q4: move / rotate / mirror placed symbols that have wires attached; do fields and
wires follow; the client-side workaround (symbol + fields + wire ends in one commit);
sub-sheet updates through the unbound vs. the sheet-bound Schematic (spike 2C note)."""

from geom import pin_ends_abs
from harness import (
    MM,
    Recorder,
    Server,
    by_id,
    commit,
    fresh_fixture,
    load_libraries,
    local_label,
    main_guard,
    mm,
    nets_by_pin,
    require_kicad,
    sid,
    update_symbols,
    v2,
    wire,
)


def field_offsets(sym):
    p = sym.position
    return {
        f.name: (round((f.text.position.x - p.x) / MM, 4), round((f.text.position.y - p.y) / MM, 4))
        for f in (sym.reference_field, sym.value_field, sym.footprint_field)
    }


def shift_fields(sym, dx_nm, dy_nm):
    for f in (sym.reference_field, sym.value_field, sym.footprint_field, sym.datasheet_field, sym.description_field, *sym.user_fields):
        f.text.position = type(f.text.position).from_xy(f.text.position.x + dx_nm, f.text.position.y + dy_nm)


def main() -> int:
    require_kicad()
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    rec = Recorder("q04_transform")
    proj = fresh_fixture("blank")
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        load_libraries(k)

        def pair(ref_a, ref_b, y):
            a = sch.place_symbol_from_library("Device:R", v2(50.8, y), reference=ref_a)
            b = sch.place_symbol_from_library("Device:R", v2(101.6, y), orientation=O.SSO_90, reference=ref_b)
            pa, pb = pin_ends_abs(a), pin_ends_abs(b)
            ws = [wire(pa["2"], (pa["2"][0], pb["1"][1])), wire((pa["2"][0], pb["1"][1]), pb["1"])]
            lbl = local_label(f"TOP_{ref_a}", pa["1"])
            with commit(sch):
                made = sch.create_items([*ws, lbl])
            return a, b, made

        # --- 1: plain position update, nothing else
        a, b, made = pair("R1", "R2", 38.1)
        before = field_offsets(a)
        a = by_id(sch, [sid(a)])[sid(a)]
        a.position = v2(50.8 + 12.7, 38.1)
        update_symbols(sch, [a])
        a2 = by_id(sch, [sid(a)])[sid(a)]
        rec.check("move_position_read_back", mm(a2.position) == (63.5, 38.1), mm(a2.position))
        rec.check("move_symbol_moves_fields", field_offsets(a2) == before, {"before": before, "after": field_offsets(a2)})
        w0 = by_id(sch, [sid(made[0])])[sid(made[0])]
        rec.check("move_symbol_drags_attached_wires", mm(w0.start) == pin_ends_abs(a2)["2"], {"wire_start": mm(w0.start), "pin2_now": pin_ends_abs(a2)["2"]})
        nets = nets_by_pin(sch)
        rec.check("move_without_wire_update_breaks_connection", nets.get(("R1", "2")) != nets.get(("R2", "1")), (nets.get(("R1", "2")), nets.get(("R2", "1"))))

        # --- 2: workaround: symbol + its fields + attached wire ends + label in ONE commit
        a, b, made = pair("R3", "R4", 88.9)
        a = by_id(sch, [sid(a)])[sid(a)]
        before = field_offsets(a)
        dx = round(12.7 * MM)
        a.position = type(a.position).from_xy(a.position.x + dx, a.position.y)
        shift_fields(a, dx, 0)
        new_ends = pin_ends_abs(a)
        ws = by_id(sch, [sid(made[0]), sid(made[1]), sid(made[2])])
        w_a, w_b, lbl = ws[sid(made[0])], ws[sid(made[1])], ws[sid(made[2])]
        w_a.start = v2(*new_ends["2"])
        w_a.end = v2(new_ends["2"][0], mm(w_a.end)[1])
        w_b.start = v2(new_ends["2"][0], mm(w_b.start)[1])
        lbl.position = v2(*new_ends["1"])
        with commit(sch, "move R3 with wires"):
            update_symbols(sch, [a])
            sch.update_items([w_a, w_b, lbl])
        a2 = by_id(sch, [sid(a)])[sid(a)]
        nets = nets_by_pin(sch)
        rec.check("workaround_move_with_wires_one_commit", nets.get(("R3", "2")) == nets.get(("R4", "1")) and (nets.get(("R3", "1")) or "").endswith("TOP_R3"), (nets.get(("R3", "2")), nets.get(("R4", "1")), nets.get(("R3", "1"))))
        rec.check("workaround_fields_moved_with_symbol", field_offsets(a2) == before, field_offsets(a2))

        # --- 3: rotate + 4: mirror in place, wires re-attached by the client
        for n, (what, ref_a, ref_b) in enumerate((("rotate", "R5", "R6"), ("mirror", "R7", "R8"))):
            a, b, made = pair(ref_a, ref_b, 139.7 + 50.8 * n)
            a = by_id(sch, [sid(a)])[sid(a)]
            if what == "rotate":
                a.transform.orientation = O.SSO_90
            else:
                a.transform.mirror_x = True
            ends = pin_ends_abs(a)
            ws = by_id(sch, [sid(m) for m in made])
            w_a, lbl = ws[sid(made[0])], ws[sid(made[2])]
            w_a.start = v2(*ends["2"])
            lbl.position = v2(*ends["1"])
            with commit(sch, what):
                update_symbols(sch, [a])
                sch.update_items([w_a, lbl])
            got = by_id(sch, [sid(a)])[sid(a)]
            nets = nets_by_pin(sch)
            rec.check(f"{what}_read_back", (int(got.transform.orientation), got.transform.mirror_x) == ((2, False) if what == "rotate" else (1, True)), got.transform.proto)
            rec.check(f"{what}_with_wires_reattached_connected", nets.get((ref_a, "2")) == nets.get((ref_b, "1")) and (nets.get((ref_a, "1")) or "").endswith(f"TOP_{ref_a}"), (nets.get((ref_a, "2")), nets.get((ref_b, "1"))))
            rec.note(f"{what}_field_offsets_after", field_offsets(got))

    # --- 5: sub-sheet item updates: unbound Schematic vs. for_sheet (spike 2C finding)
    proj = fresh_fixture("complex_hierarchy")
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        inst = [s for s in sch.get_hierarchy() if s.filename == "ampli_ht.kicad_sch"]
        sub = sch.for_sheet(inst[0])
        sym = next(s for s in sub.get_symbols() if not s.reference.startswith("#"))
        start = mm(sym.position)
        moved = by_id(sch, [sid(sym)])[sid(sym)]
        moved.position = v2(start[0] + 2.54, start[1])
        err = None
        try:
            update_symbols(sch, [moved])
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        now = mm(by_id(sub, [sid(sym)])[sid(sym)].position)
        rec.check("subsheet_update_via_unbound_schematic_applies", now == (round(start[0] + 2.54, 4), start[1]), {"ref": sym.reference, "start": start, "now": now, "error": err})
        moved = by_id(sub, [sid(sym)])[sid(sym)]
        moved.position = v2(start[0] + 5.08, start[1])
        update_symbols(sub, [moved])
        now = mm(by_id(sub, [sid(sym)])[sid(sym)].position)
        rec.check("subsheet_update_via_for_sheet_applies", now == (round(start[0] + 5.08, 4), start[1]), now)
        other = sch.for_sheet(inst[1])
        rec.check("shared_subsheet_edit_visible_in_both_instances", mm(by_id(other, [sid(sym)])[sid(sym)].position) == now, "ampli_ht.kicad_sch is used by two sheet instances (one screen)")
        rec.note("subsheet_symbol", {"ref": sym.reference, "path_instances": [str(i.path.proto.path_human_readable) for i in inst]})
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
