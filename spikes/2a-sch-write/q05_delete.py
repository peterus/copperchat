"""Q5: delete symbols, wires, labels, junctions, no-connects; what the push does to the
neighbours (segment merging, junction cleanup, dangling junctions - spike 2C notes);
netlist after deletes."""

from geom import pin_ends_abs
from harness import (
    Recorder,
    Server,
    by_id,
    commit,
    fresh_fixture,
    junction,
    load_libraries,
    local_label,
    main_guard,
    mm,
    nets_by_pin,
    no_connect,
    require_kicad,
    sid,
    v2,
    wire,
)


def main() -> int:
    require_kicad()
    rec = Recorder("q05_delete")
    proj = fresh_fixture("blank")
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        load_libraries(k)
        r1 = sch.place_symbol_from_library("Device:R", v2(50.8, 50.8), reference="R1")
        r2 = sch.place_symbol_from_library("Device:R", v2(101.6, 50.8), reference="R2")
        r3 = sch.place_symbol_from_library("Device:R", v2(76.2, 88.9), reference="R3")
        p1, p2, p3 = pin_ends_abs(r1), pin_ends_abs(r2), pin_ends_abs(r3)
        y = 63.5
        # R1.2 -> (50.8,63.5) -> (76.2,63.5) | (76.2,63.5) -> (101.6,63.5) -> R2.2 ; T to R3.1 with junction
        segs = [
            wire(p1["2"], (50.8, y)),
            wire((50.8, y), (76.2, y)),
            wire((76.2, y), (101.6, y)),
            wire((101.6, y), p2["2"]),
            wire(p3["1"], (76.2, y)),
        ]
        j = junction((76.2, y))
        lbl = local_label("KEEP", p1["1"])
        nc = no_connect(p2["1"])
        dangling_j = junction((152.4, 152.4))  # not on any wire
        with commit(sch, "build"):
            made = sch.create_items([*segs, j, lbl, nc, dangling_j])
        ids = [sid(m) for m in made]
        after = by_id(sch, ids)
        rec.check("dangling_junction_kept_after_push", ids[-1] in after, f"junction at (152.4,152.4) present={ids[-1] in after}")
        rec.check("collinear_segments_kept_separate_on_create", all(i in after for i in ids[:4]), f"{sum(i in after for i in ids[:4])}/4")
        nets = nets_by_pin(sch)
        rec.check(
            "before_delete_t_net",
            nets.get(("R1", "2")) == nets.get(("R2", "2")) == nets.get(("R3", "1")),
            (nets.get(("R1", "2")), nets.get(("R2", "2")), nets.get(("R3", "1"))),
        )

        # delete the T branch: does KiCad remove the now-useless junction / merge the two
        # collinear segments it joined?
        with commit(sch, "delete branch"):
            sch.remove_items([after[ids[4]]])
        now = by_id(sch, ids)
        rec.check("delete_wire_removed", ids[4] not in now, "branch wire gone")
        rec.check("delete_wire_keeps_junction", ids[5] in now, f"junction present={ids[5] in now}")
        rec.check(
            "delete_wire_keeps_neighbour_segments",
            ids[1] in now and ids[2] in now and mm(now[ids[1]].end) == (76.2, y),
            {i[:8]: (mm(w.start), mm(w.end)) for i, w in now.items() if type(w).__name__ == "SchematicLine"},
        )
        lines = sch.get_lines()
        rec.note("wires_after_branch_delete", [(mm(w.start), mm(w.end), sid(w)[:8]) for w in lines])

        nets = nets_by_pin(sch)
        rec.check("after_branch_delete_r3_disconnected", nets.get(("R3", "1")) != nets.get(("R1", "2")), nets.get(("R3", "1")))
        # delete a symbol: wires stay (dangling), netlist drops the symbol
        with commit(sch, "delete R2"):
            sch.remove_items([by_id(sch, [sid(r2)])[sid(r2)]])
        syms = [s.reference for s in sch.get_symbols()]
        rec.check("delete_symbol", "R2" not in syms, syms)
        left = by_id(sch, ids)
        rec.check("delete_symbol_leaves_wires", ids[3] in left, "wire to former R2.2 present")
        rec.check("delete_symbol_leaves_its_no_connect", ids[7] in left, "no-connect on former R2.1 present")
        nets = nets_by_pin(sch)
        rec.check("netlist_after_symbol_delete", not any(r == "R2" for r, _ in nets), sorted({r for r, _ in nets}))
        # delete label, junction, no-connect by id
        with commit(sch, "delete misc"):
            sch.remove_items_by_id([after[ids[6]].id, after[ids[5]].id, after[ids[7]].id])
        left = by_id(sch, ids)
        rec.check("delete_label_junction_no_connect", not any(i in left for i in ids[5:8]), sorted(i[:8] for i in left))
        nets = nets_by_pin(sch)
        rec.check("netlist_after_label_delete", not (nets.get(("R1", "1")) or "").endswith("KEEP"), nets.get(("R1", "1")))
        # two collinear segments end to end, nothing else at the joint
        with commit(sch, "collinear"):
            co = sch.create_items([wire((127.0, 127.0), (139.7, 127.0)), wire((139.7, 127.0), (152.4, 127.0))])
        co_now = by_id(sch, [sid(c) for c in co])
        rec.check("collinear_pair_kept_separate_on_create", len(co_now) == 2, {i[:8]: (mm(w.start), mm(w.end)) for i, w in co_now.items()})
        # deleting an unknown id
        from harness import new_kiid

        rec.attempt("delete_unknown_id", lambda: sch.remove_items_by_id([new_kiid()]))
        rec.check("document_modified_flag", sch.is_document_modified(), "is_document_modified after API edits")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
