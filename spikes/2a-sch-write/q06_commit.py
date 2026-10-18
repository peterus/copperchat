"""Q6: commit semantics: staged creates/updates/deletes before push, drop_commit,
cancellation between begin and push (client gone), and a compensating commit that
re-creates deleted items with their ORIGINAL UUIDs (spec 5.1 step 8)."""

import time

from google.protobuf.json_format import MessageToDict

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
    root_sch,
    sid,
    update_symbols,
    v2,
    wire,
)


def canon(item) -> dict:
    """Comparable content of an item incl. the symbol definition's children (unpacked);
    only the pins' own KIIDs are excluded: KiCad renews pin UUIDs on every create
    (api_handler_sch.cpp)."""
    d = MessageToDict(item.proto, preserving_proto_field_name=True)
    if "definition" in d:
        kids = []
        for child in item.definition.items:
            c = MessageToDict(child.item.proto, preserving_proto_field_name=True) if child.item is not None else {}
            if child.kind == "SchematicPin":
                c.pop("id", None)
            kids.append({"kind": child.kind, "unit": child.unit, "item": c})
        d["definition"]["items"] = sorted(kids, key=lambda x: repr(x))
    return d


def snapshot(sch) -> dict:
    items = [*sch.get_symbols(), *sch.get_lines(), *sch.get_labels(), *sch.get_junctions(), *sch.get_no_connects()]
    return {sid(i): canon(i) for i in items}


def main() -> int:
    require_kicad()
    from kipy.errors import ApiError
    from kipy.schematic_types import GlobalLabel

    rec = Recorder("q06_commit")
    proj = fresh_fixture("blank")
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000, name="copperchat")
        sch = k.get_schematic()
        load_libraries(k)
        r1 = sch.place_symbol_from_library("Device:R", v2(50.8, 50.8), reference="R1")
        p1 = pin_ends_abs(r1)

        # --- staged create / update / delete are not visible before push
        c = sch.begin_commit()
        w = sch.create_items([wire(p1["2"], (50.8, 63.5))])[0]
        placed = sch.place_symbol_from_library("Device:C", v2(76.2, 50.8), reference="C1")
        rec.check("staged_create_response_has_uuid", bool(sid(w)) and bool(sid(placed)), (sid(w)[:8], sid(placed)[:8]))
        rec.check("staged_create_invisible_by_id", not by_id(sch, [sid(w), sid(placed)]), "GetItemsById before push")
        rec.check(
            "staged_create_invisible_in_get_items",
            len(sch.get_lines()) == 0 and [s.reference for s in sch.get_symbols()] == ["R1"],
            [s.reference for s in sch.get_symbols()],
        )
        rec.check("staged_create_invisible_in_netlist", nets_by_pin(sch).get(("C1", "1")) is None, "GetSchematicNetlist before push")
        g = by_id(sch, [sid(r1)])[sid(r1)]
        g.value = "STAGED"
        update_symbols(sch, [g])
        rec.note("staged_update_visible_before_push", by_id(sch, [sid(r1)])[sid(r1)].value == "STAGED")
        sch.push_commit(c, "staged")
        vis = by_id(sch, [sid(w), sid(placed), sid(r1)])
        rec.check("after_push_visible", sid(w) in vis and sid(placed) in vis and vis[sid(r1)].value == "STAGED", sorted(i[:8] for i in vis))

        c = sch.begin_commit()
        sch.remove_items([vis[sid(w)]])
        rec.check("staged_delete_still_visible_before_push", sid(w) in by_id(sch, [sid(w)]), "wire still returned")
        sch.push_commit(c, "delete")
        rec.check("after_push_delete_gone", sid(w) not in by_id(sch, [sid(w)]), "")
        try:
            res = sch.update_items([vis[sid(w)]])
            rec.check(
                "update_of_missing_item_raises",
                False,
                f"no exception; kipy returned {len(res)} item(s) and ignores the per-item status (editor.py update_items); still missing after: {sid(w) not in by_id(sch, [sid(w)])}",
            )
        except Exception as e:  # noqa: BLE001
            rec.check("update_of_missing_item_raises", True, f"{type(e).__name__}: {e}")

        try:
            res = sch.create_items([by_id(sch, [sid(placed)])[sid(placed)]])
            rec.check(
                "create_with_existing_uuid_raises",
                False,
                f"no exception; kipy returned {len(res)} item(s) (ISC_EXISTING dropped); symbols now: {sorted(s.reference for s in sch.get_symbols())}",
            )
        except Exception as e:  # noqa: BLE001
            rec.check("create_with_existing_uuid_raises", True, f"{type(e).__name__}: {e}")
        # --- drop_commit leaves the document unchanged (create + update + delete)
        before = snapshot(sch)
        c = sch.begin_commit()
        sch.create_items([local_label("DROPPED", (101.6, 101.6))])
        sch.place_symbol_from_library("Device:R", v2(127.0, 50.8), reference="R9")
        g = by_id(sch, [sid(r1)])[sid(r1)]
        g.value = "DROPME"
        update_symbols(sch, [g])
        sch.remove_items([by_id(sch, [sid(placed)])[sid(placed)]])
        sch.drop_commit(c)
        after = snapshot(sch)
        rec.check(
            "drop_commit_document_unchanged",
            after == before,
            {
                "added": sorted(set(after) - set(before)),
                "removed": sorted(set(before) - set(after)),
                "changed": [i for i in before if i in after and before[i] != after[i]],
            },
        )

        # --- cancellation: the client disappears between begin and push
        k_a = srv.client(timeout_ms=10000, name="copperchat-a")
        sch_a = k_a.get_schematic()
        stale = sch_a.begin_commit()
        ghost = sch_a.create_items([local_label("GHOST", (152.4, 101.6))])[0]
        k_a.close()  # socket gone, no EndCommit
        time.sleep(0.5)
        k_b = srv.client(timeout_ms=10000, name="copperchat-b")
        sch_b = k_b.get_schematic()
        cb = sch_b.begin_commit()
        made_b = sch_b.create_items([local_label("FROM_B", (152.4, 127.0))])[0]
        sch_b.push_commit(cb, "b")
        rec.check("other_client_push_visible", sid(made_b) in by_id(sch_b, [sid(made_b)]), "")
        rec.check("stale_commit_items_invisible", sid(ghost) not in by_id(sch_b, [sid(ghost)]), "GHOST label never applied")
        # the same client name reconnects (CopperChat uses a fixed name, session.py)
        k_a2 = srv.client(timeout_ms=10000, name="copperchat-a")
        sch_a2 = k_a2.get_schematic()
        ok2, res2 = rec.attempt("same_name_begin_commit_while_stale", lambda: sch_a2.begin_commit(), expect_ok=False)
        lone = sch_a2.create_items([local_label("AFTER_RECONNECT", (152.4, 152.4))])[0]
        rec.check(
            "same_name_writes_land_in_stale_commit",
            sid(lone) not in by_id(sch_a2, [sid(lone)]),
            "create_items after reconnect (no new commit) is visible?",
        )
        # evidence that they were staged (not discarded): pushing the stale commit applies them
        rec.attempt("push_stale_commit_by_old_id", lambda: sch_a2.push_commit(stale, "stale"))
        applied = by_id(sch_a2, [sid(ghost), sid(lone)])
        rec.check(
            "pushed_stale_commit_applies_ghost_and_reconnect_writes",
            sid(ghost) in applied and sid(lone) in applied,
            sorted(i[:8] for i in applied),
        )
        lone2 = sch_a2.create_items([local_label("AFTER_PUSH", (177.8, 152.4))])[0]
        rec.check("after_ending_stale_commit_writes_apply", sid(lone2) in by_id(sch_a2, [sid(lone2)]), "")
        # the same scenario ended with drop_commit instead
        k_c = srv.client(timeout_ms=10000, name="copperchat-c")
        sch_c = k_c.get_schematic()
        stale_c = sch_c.begin_commit()
        ghost_c = sch_c.create_items([local_label("GHOST_C", (152.4, 177.8))])[0]
        k_c.close()
        time.sleep(0.5)
        sch_c2 = srv.client(timeout_ms=10000, name="copperchat-c").get_schematic()
        lone_c = sch_c2.create_items([local_label("AFTER_RECONNECT_C", (177.8, 177.8))])[0]
        rec.attempt("drop_stale_commit_by_old_id", lambda: sch_c2.drop_commit(stale_c))
        rec.check("dropped_stale_items_gone", not by_id(sch_c2, [sid(ghost_c), sid(lone_c)]), "")
        lone_c2 = sch_c2.create_items([local_label("AFTER_DROP_C", (203.2, 177.8))])[0]
        rec.check("after_dropping_stale_commit_writes_apply", sid(lone_c2) in by_id(sch_c2, [sid(lone_c2)]), "")
        c2 = sch_a2.begin_commit()
        rec.attempt("second_begin_commit_same_client", lambda: sch_a2.begin_commit(), expect_ok=False)
        sch_a2.drop_commit(c2)
        rec.attempt("push_unknown_commit", lambda: sch_a2.push_commit(c2), expect_ok=False)
        del ApiError

    # --- compensating commit: delete items, re-create them from pre-images (same UUIDs)
    proj = fresh_fixture("blank")
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        load_libraries(k)
        u = sch.place_symbol_from_library("Amplifier_Operational:LM358", v2(101.6, 76.2), unit=2, reference="U1")
        u = by_id(sch, [sid(u)])[sid(u)]
        u.transform.mirror_x = True
        u.value = "TLV9002"
        from q02_fields import field

        u.user_fields = [field("MPN", "TLV9002IDR", (106.68, 81.28), visible=False)]
        u.attributes.do_not_populate = True
        update_symbols(sch, [u])
        r = sch.place_symbol_from_library("Device:R", v2(50.8, 76.2), reference="R1")
        pu, pr = pin_ends_abs(by_id(sch, [sid(u)])[sid(u)]), pin_ends_abs(r)
        gl = GlobalLabel()
        gl.text.value = "SIG_G"
        gl.position = v2(*pr["1"])
        made = sch.create_items(
            [
                wire(pr["2"], (50.8, 88.9)),
                wire((50.8, 88.9), (pu["5"][0], 88.9)),
                wire((pu["5"][0], 88.9), pu["5"]),
                junction((50.8, 88.9)),  # kept only if it is a real junction; see Q5
                local_label("MID", (63.5, 88.9)),
                no_connect(pu["6"]),
                gl,
            ]
        )
        targets = [sid(u), sid(r), *[sid(m) for m in made]]
        pre = by_id(sch, targets)
        rec.note("corner_junction_dropped_on_create", targets[5] not in pre)
        targets = list(pre)
        pre_canon = {i: canon(x) for i, x in pre.items()}
        nets_before = nets_by_pin(sch)
        rec.note("pre_images", {i[:8]: type(x).__name__ for i, x in pre.items()})
        c = sch.begin_commit()
        sch.remove_items(list(pre.values()))
        sch.push_commit(c, "delete all")
        rec.check("deleted_all_targets", not by_id(sch, targets), "")
        t0 = time.monotonic()
        c = sch.begin_commit()
        recreated = sch.create_items(list(pre.values()))
        sch.push_commit(c, "compensate")
        rec.note("compensate_ms", round((time.monotonic() - t0) * 1000, 1))
        back = by_id(sch, targets)
        rec.check(
            "recreate_with_original_uuids",
            sorted(back) == sorted(pre),
            {"missing": [i[:8] for i in pre if i not in back], "returned_ids": sorted(sid(x)[:8] for x in recreated)},
        )
        diff = {
            i[:8]: sorted(kk for kk in set(pre_canon[i]) | set(canon(back[i])) if pre_canon[i].get(kk) != canon(back[i]).get(kk))
            for i in back
            if canon(back[i]) != pre_canon[i]
        }
        rec.check("recreated_content_equal", not diff, diff)
        nets_after = nets_by_pin(sch)
        rec.check(
            "recreated_connectivity_equal",
            nets_after == nets_before,
            sorted(kk for kk in set(nets_before) | set(nets_after) if nets_before.get(kk) != nets_after.get(kk)),
        )
        # compensate an update: restore a pre-image through update_items
        cur = by_id(sch, [sid(u)])[sid(u)]
        cur.value = "OOPS"
        cur.position = v2(127.0, 76.2)
        update_symbols(sch, [cur])
        update_symbols(sch, [pre[sid(u)]])
        restored = by_id(sch, [sid(u)])[sid(u)]
        rec.check("update_compensated_from_pre_image", canon(restored) == pre_canon[sid(u)], (restored.value, mm(restored.position)))
        # re-create while another instance of the same library symbol still exists
        sib = sch.place_symbol_from_library("Device:R", v2(76.2, 127.0), reference="R2")
        pre_r = by_id(sch, [sid(r)])[sid(r)]
        with commit(sch):
            sch.remove_items([pre_r])
        with commit(sch):
            sch.create_items([pre_r])
        again = by_id(sch, [sid(r)])[sid(r)]
        sib_now = by_id(sch, [sid(sib)])[sid(sib)]
        rec.check(
            "recreate_with_sibling_keeps_library_link",
            str(again.definition.id) == "Device:R" == str(sib_now.definition.id) and sid(again) == sid(pre_r),
            f"{again.reference}: definition id {again.definition.id} (sibling {sib.reference}: {by_id(sch, [sid(sib)])[sid(sib)].definition.id})",
        )
        sch.save()
    with Server(proj) as srv:
        sch = srv.client(timeout_ms=60000).get_schematic()
        back = by_id(sch, targets)
        rec.check("original_uuids_persist_after_save_reopen", sorted(back) == sorted(targets), f"{len(back)}/{len(targets)}")
        rec.note("root", str(root_sch(proj)))
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
